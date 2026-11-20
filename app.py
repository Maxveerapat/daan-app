import json
import math
import os
import urllib.error
import urllib.request
import sqlite3
import time
from pathlib import Path

from flask import Flask, g, jsonify, render_template, request

APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "daan.db"

ACTIVE_SECONDS = 3 * 3600          # fresh window: last confirmed within 3h
HIDE_AFTER_SECONDS = 12 * 3600     # drop from API entirely after 12h
DEDUP_RADIUS_M = 300               # same type within this radius -> merge as confirmation
DISMISS_THRESHOLD = 2              # this many "gone" votes clears a checkpoint

# Free OpenRouteService key (https://openrouteservice.org/dev/#/signup). Set it as an
# environment variable on the server (Render → Environment) so it never ships to the browser.
ORS_KEY = os.environ.get("ORS_KEY", "").strip()
ORS_URL = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{o_lng},{o_lat};{d_lng},{d_lat}?overview=full&geometries=geojson&alternatives=3"

app = Flask(__name__, instance_path=str(APP_DIR / "instance"))
app.config["TEMPLATES_AUTO_RELOAD"] = True


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def haversine_m(lat1, lng1, lat2, lng2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """CREATE TABLE IF NOT EXISTS checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            label TEXT NOT NULL,
            type TEXT NOT NULL,
            reported_at INTEGER NOT NULL,
            last_confirmed_at INTEGER NOT NULL,
            confirms INTEGER NOT NULL DEFAULT 1,
            dismisses INTEGER NOT NULL DEFAULT 0,
            reports INTEGER NOT NULL DEFAULT 1,
            cleared_at INTEGER,
            source TEXT NOT NULL DEFAULT 'user',
            source_name TEXT
        )"""
    )
    db.execute("DELETE FROM checkpoints WHERE source = 'official'")  # legacy rows from older DBs
    if db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0:
        now = int(time.time())
        # All checkpoints are crowdsourced in-app (source = 'user'). There is no public
        # feed of Thai police checkpoints, so the old 'official' source was removed.
        seed = [
            # lat, lng, label, type, rep_min, conf_min, confirms, reports, source, source_name
            # coordinates snapped to the road network (OSRM /nearest) so pins sit on roads, not buildings
            (13.78062, 100.50344, "ถ.ราชวิถี ใกล้สะพานกรุงธน", "ใบขับขี่+หมวกกันน็อค", 55, 15, 4, 2, "user", None),
            (13.66041, 100.43404, "ถ.พระราม 2 หน้าโลตัส", "ส่องความเร็ว", 95, 40, 2, 3, "user", None),
            (13.72203, 100.52311, "ถ.สาทร ใกล้ รพ.เซนต์หลุยส์", "ตั้งแล้ว ระวังด้วย", 30, 8, 5, 2, "user", None),
            (13.76474, 100.53874, "วงเวียนอนุสาวรีย์ชัยฯ", "เช็คลมปาก", 70, 25, 3, 1, "user", None),
            (13.73052, 100.56972, "ถ.สุขุมวิท ซ.33", "เน้น 2 ล้อ", 170, 165, 1, 1, "user", None),
            (13.7097, 100.4601, "ถ.จรัญสนิทวงศ์ ซ.35", "ใบขับขี่+หมวกกันน็อค", 480, 470, 1, 1, "user", None),
            (13.8037, 100.55419, "ถ.กำแพงเพชร ใกล้ ม.เกษตร", "ภาษี พ.ร.บ.", 60, 35, 6, 4, "user", None),
            (13.74671, 100.53406, "แยกราชเทวี", "ตั้งแล้ว ระวังด้วย", 50, 20, 8, 5, "user", None),
        ]
        for lat, lng, label, typ, rep_m, conf_m, confirms, reports, source, sname in seed:
            db.execute(
                "INSERT INTO checkpoints (lat,lng,label,type,reported_at,last_confirmed_at,confirms,dismisses,reports,source,source_name) VALUES (?,?,?,?,?,?,?,0,?,?,?)",
                (lat, lng, label, typ, now - rep_m * 60, now - conf_m * 60, confirms, reports, source, sname),
            )
    db.commit()
    db.close()


def row_to_dict(row, now):
    age = now - row["last_confirmed_at"]
    if row["cleared_at"] is not None:
        status = "cleared"
    elif age <= ACTIVE_SECONDS:
        status = "active"
    else:
        status = "stale"
    return {
        "id": row["id"],
        "lat": row["lat"],
        "lng": row["lng"],
        "label": row["label"],
        "type": row["type"],
        "reported_at": row["reported_at"],
        "last_confirmed_at": row["last_confirmed_at"],
        "age_minutes": age // 60,
        "confirms": row["confirms"],
        "dismisses": row["dismisses"],
        "reports": row["reports"],
        "status": status,
        "source": row["source"],
        "source_name": row["source_name"],
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/checkpoints")
def list_checkpoints():
    now = int(time.time())
    rows = get_db().execute(
        "SELECT * FROM checkpoints WHERE last_confirmed_at > ? ORDER BY last_confirmed_at DESC",
        (now - HIDE_AFTER_SECONDS,),
    ).fetchall()
    return jsonify({"now": now, "checkpoints": [row_to_dict(r, now) for r in rows]})


@app.route("/api/checkpoints", methods=["POST"])
def add_checkpoint():
    data = request.get_json(force=True)
    lat, lng = float(data["lat"]), float(data["lng"])
    label = (data.get("label") or "").strip() or "ไม่ระบุตำแหน่ง"
    typ = (data.get("type") or "").strip() or "ตั้งแล้ว ระวังด้วย"
    now = int(time.time())
    db = get_db()

    active = db.execute(
        "SELECT * FROM checkpoints WHERE cleared_at IS NULL AND source != 'official' AND last_confirmed_at > ? AND type = ?",
        (now - ACTIVE_SECONDS, typ),
    ).fetchall()
    for row in active:
        if haversine_m(lat, lng, row["lat"], row["lng"]) <= DEDUP_RADIUS_M:
            db.execute(
                "UPDATE checkpoints SET last_confirmed_at=?, confirms=confirms+1, reports=reports+1 WHERE id=?",
                (now, row["id"]),
            )
            db.commit()
            return jsonify({"merged": True, "id": row["id"]})

    cur = db.execute(
        "INSERT INTO checkpoints (lat,lng,label,type,reported_at,last_confirmed_at) VALUES (?,?,?,?,?,?)",
        (lat, lng, label, typ, now, now),
    )
    db.commit()
    return jsonify({"merged": False, "id": cur.lastrowid})


@app.route("/api/checkpoints/<int:cid>/vote", methods=["POST"])
def vote(cid):
    still = bool(request.get_json(force=True).get("still"))
    now = int(time.time())
    db = get_db()
    if still:
        db.execute(
            "UPDATE checkpoints SET last_confirmed_at=?, confirms=confirms+1, dismisses=0, cleared_at=NULL WHERE id=?",
            (now, cid),
        )
    else:
        db.execute("UPDATE checkpoints SET dismisses=dismisses+1 WHERE id=?", (cid,))
        row = db.execute("SELECT dismisses FROM checkpoints WHERE id=?", (cid,)).fetchone()
        if row and row["dismisses"] >= DISMISS_THRESHOLD:
            db.execute("UPDATE checkpoints SET cleared_at=? WHERE id=?", (now, cid))
    db.commit()
    return jsonify({"ok": True})


# ---------- routing ----------
# Returns the engine's normal route options (tolls allowed + tolls avoided), each tagged
# with whether it uses a tollway. Routes are ordered by travel time only — checkpoints are
# never used to pick, filter or rank routes (see HANDOFF.md, scope boundary).

def _http_json(url, body=None, headers=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _ors_routes(o, d, avoid_tolls):
    body = {
        "coordinates": [[o[1], o[0]], [d[1], d[0]]],
        "alternative_routes": {"target_count": 3, "share_factor": 0.6, "weight_factor": 1.6},
        "extra_info": ["tollways"],
    }
    if avoid_tolls:
        body["options"] = {"avoid_features": ["tollways"]}
    js = _http_json(ORS_URL, body, {"Authorization": ORS_KEY, "Content-Type": "application/json"})
    out = []
    for f in js.get("features", []):
        summ = f["properties"].get("summary", {})
        toll_vals = f["properties"].get("extras", {}).get("tollways", {}).get("summary", [])
        uses_toll = any(v.get("value") == 1 and v.get("distance", 0) > 0 for v in toll_vals)
        out.append({
            "coords": [[c[1], c[0]] for c in f["geometry"]["coordinates"]],
            "km": summ.get("distance", 0) / 1000,
            "mins": round(summ.get("duration", 0) / 60),
            "toll": uses_toll,
        })
    return out


def _osrm_routes(o, d):
    js = _http_json(OSRM_URL.format(o_lat=o[0], o_lng=o[1], d_lat=d[0], d_lng=d[1]))
    return [{
        "coords": [[c[1], c[0]] for c in rt["geometry"]["coordinates"]],
        "km": rt["distance"] / 1000,
        "mins": round(rt["duration"] / 60),
        "toll": None,  # unknown on OSRM
    } for rt in js.get("routes", [])]


def _same_route(a, b):
    return abs(a["km"] - b["km"]) < 0.3 and abs(a["mins"] - b["mins"]) <= 1


@app.route("/api/routes")
def routes():
    try:
        o = (float(request.args["olat"]), float(request.args["olng"]))
        d = (float(request.args["dlat"]), float(request.args["dlng"]))
    except (KeyError, ValueError):
        return jsonify({"error": "bad coordinates"}), 400
    found, engine = [], "osrm"
    if ORS_KEY:
        for avoid in (False, True):
            try:
                for r in _ors_routes(o, d, avoid):
                    if not any(_same_route(r, x) for x in found):
                        found.append(r)
                engine = "ors"
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
                app.logger.warning("ORS failed (avoid_tolls=%s): %s", avoid, e)
    if not found:
        try:
            found = _osrm_routes(o, d)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
            app.logger.warning("OSRM failed: %s", e)
            return jsonify({"error": "routing unavailable"}), 502
    found.sort(key=lambda r: r["mins"])  # travel time only
    return jsonify({"engine": engine, "routes": found})


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5051)), use_reloader=False)
