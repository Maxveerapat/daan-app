import math
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
    if db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0:
        now = int(time.time())
        # source: user = crowdsourced in-app
        #         official = government/agency open data (traffic, roadwork, weather)
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
            # official open data — road conditions, NOT checkpoints
            (13.75634, 100.50181, "ถ.ราชดำเนินกลาง หน้า รพ.กลาง", "จราจรติดขัด", 35, 10, 0, 1, "official", "iTIC / Longdo Traffic"),
            (13.74395, 100.53299, "ถ.เพชรบุรีตัดใหม่ ช่วงสะพาน", "ทำถนน+ซ่อมแซม", 300, 30, 0, 1, "official", "กรมทางหลวง"),
            (13.69004, 100.61011, "ถ.สุขุมวิท ซ.ปุณณวิถี", "ฝนตก", 45, 12, 0, 1, "official", "กรมอุตุนิยมวิทยา"),
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


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5051, use_reloader=False)
