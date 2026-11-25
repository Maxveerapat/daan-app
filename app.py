import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
import time
from pathlib import Path

from flask import Flask, g, jsonify, render_template, request

import db as dbmod

APP_DIR = Path(__file__).parent
# Google Maps basemap (official Maps JavaScript API). Browser key — restrict it by HTTP referrer in
# Google Cloud. Without it the app uses OpenFreeMap styled to look like Google Maps.
GOOGLE_MAPS_KEY = os.environ.get("GOOGLE_MAPS_KEY", "").strip()
# Feature toggle: require a live photo (on-device check) when reporting. Off unless REPORT_PHOTO=1.
PHOTO_REQUIRED = os.environ.get("REPORT_PHOTO", "0") == "1"
# Demo reports are only seeded into an empty local (SQLite) DB unless SEED_DEMO=1 is set.
SEED_DEMO = os.environ.get("SEED_DEMO", "" if dbmod.IS_PG else "1") == "1"

ACTIVE_SECONDS = 3 * 3600          # fresh window: last confirmed within 3h
HIDE_AFTER_SECONDS = 12 * 3600     # drop from API entirely after 12h
DEDUP_RADIUS_M = 500               # same category + same direction within this radius -> one report
DEDUP_NODIR_M = 150                # if either report has no direction, only merge this close
DEDUP_MAX_ANGLE = 60               # directions within this many degrees count as "same side"
SNAP_MAX_M = 40                    # snap a directed pin to the matching carriageway if this close
DISMISS_THRESHOLD = 2              # this many "gone" votes clears a checkpoint

# ---------- report types ----------
# Types are stored as stable codes; display names (TH/EN) live in index.html. Older databases
# stored the Thai display name itself — those rows are migrated to codes on startup.
TYPE_CODES = {"general", "license_helmet", "alcohol", "black_smoke", "motorbike", "speed",
              "tax_insurance", "truck", "accident", "roadwork", "flood", "blocked"}
INCIDENT_TYPES = {"accident", "roadwork", "flood", "blocked"}
RETIRED_TYPES = {"license_helmet", "speed", "tax_insurance"}   # still displayed, no longer reportable   # everything else is a checkpoint


def angle_diff(a, b):
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def same_spot(lat, lng, heading, row):
    """Same physical checkpoint? Same side of the road when both directions are known."""
    d = haversine_m(lat, lng, row["lat"], row["lng"])
    rh = row["heading"]
    if heading is not None and rh is not None:
        return d <= DEDUP_RADIUS_M and angle_diff(heading, rh) <= DEDUP_MAX_ANGLE, d
    return d <= DEDUP_NODIR_M, d


def type_category(t):
    return "incident" if t in INCIDENT_TYPES else "checkpoint"


LEGACY_TYPES = {
    "ตั้งแล้ว ระวังด้วย": "general", "ใบขับขี่+หมวกกันน็อค": "license_helmet", "เช็คลมปาก": "alcohol",
    "ควันดำ": "black_smoke", "เน้น 2 ล้อ": "motorbike", "ส่องความเร็ว": "speed", "ภาษี พ.ร.บ.": "tax_insurance",
    "รถบรรทุก": "truck", "อุบัติเหตุ": "accident", "ทำถนน/ก่อสร้าง": "roadwork", "น้ำท่วม": "flood",
    "ถนนถูกปิด/สิ่งกีดขวาง": "blocked",
}

# ---------- report anti-spam ----------
REPORT_MAX_DIST_M = 1000           # reporter's live GPS must be within this of the pin
REPORT_RATE = (5, 600)             # max 5 reports per 10 minutes per client
# The live photo is checked ON THE PHONE by a free open model (MobileCLIP, see index.html) and
# never leaves the device. The server only enforces GPS proximity and rate limits.
_recent_reports = {}               # client id -> [timestamps]

# TomTom key (https://developer.tomtom.com → Dashboard → Keys). Free tier ~2,500 requests/day.
# Used for live-traffic routing + place search. Set it in Render → Environment as TOMTOM_KEY.
TOMTOM_KEY = os.environ.get("TOMTOM_KEY", "").strip()
TOMTOM_ROUTE_URL = "https://api.tomtom.com/routing/1/calculateRoute/{o_lat},{o_lng}:{d_lat},{d_lng}/json"
TOMTOM_SEARCH_URL = "https://api.tomtom.com/search/2/search/{q}.json"
PHOTON_URL = "https://photon.komoot.io/api/"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# Optional fallback: free OpenRouteService key (https://openrouteservice.org/dev/#/signup). Set it as an
# environment variable on the server (Render → Environment) so it never ships to the browser.
ORS_KEY = os.environ.get("ORS_KEY", "").strip()
ORS_URL = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{o_lng},{o_lat};{d_lng},{d_lat}?overview=full&geometries=geojson&alternatives=3"

app = Flask(__name__, instance_path=str(APP_DIR / "instance"))
app.config["TEMPLATES_AUTO_RELOAD"] = True


def get_db():
    if "db" not in g:
        g.db = dbmod.DB()
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
    db = dbmod.migrate()
    db.execute("DELETE FROM checkpoints WHERE source = 'official'")  # legacy rows from older DBs
    for old, code in LEGACY_TYPES.items():
        db.execute("UPDATE checkpoints SET type = ? WHERE type = ?", (code, old))
    # Demo reports are tagged source='demo' and replaced with fresh ones on every startup, so they
    # never expire while SEED_DEMO is on and never mix with real reports. Turn SEED_DEMO off to remove them.
    db.execute("DELETE FROM checkpoints WHERE source = 'demo'")
    if SEED_DEMO:
        now = int(time.time())
        # All checkpoints are crowdsourced in-app (source = 'user'). There is no public
        # feed of Thai police checkpoints, so the old 'official' source was removed.
        seed = [
            # lat, lng, label, type, rep_min, conf_min, confirms, reports, source, source_name
            # coordinates snapped to the road network (OSRM /nearest) so pins sit on roads, not buildings
            (13.78062, 100.50344, "ถ.ราชวิถี ใกล้สะพานกรุงธน", "motorbike", 55, 15, 4, 2, "demo", None),
            (13.66041, 100.43404, "ถ.พระราม 2 หน้าโลตัส", "general", 95, 40, 2, 3, "demo", None),
            (13.72203, 100.52311, "ถ.สาทร ใกล้ รพ.เซนต์หลุยส์", "general", 30, 8, 5, 2, "demo", None),
            (13.76474, 100.53874, "วงเวียนอนุสาวรีย์ชัยฯ", "alcohol", 70, 25, 3, 1, "demo", None),
            (13.73052, 100.56972, "ถ.สุขุมวิท ซ.33", "motorbike", 170, 165, 1, 1, "demo", None),
            (13.7097, 100.4601, "ถ.จรัญสนิทวงศ์ ซ.35", "motorbike", 480, 470, 1, 1, "demo", None),
            (13.8037, 100.55419, "ถ.กำแพงเพชร ใกล้ ม.เกษตร", "black_smoke", 60, 35, 6, 4, "demo", None),
            (13.74671, 100.53406, "แยกราชเทวี", "general", 50, 20, 8, 5, "demo", None),
            # more checkpoints across Bangkok (Ratchaphruek to Bang Na), snapped to roads via OSRM /nearest
            (13.78348, 100.44182, "ถ.ราชพฤกษ์ ใกล้แยกตลิ่งชัน", "alcohol", 25, 8, 4, 2, "demo", None),
            (13.73899, 100.44603, "ถ.ราชพฤกษ์ ใกล้ถ.กัลปพฤกษ์", "motorbike", 60, 30, 3, 2, "demo", None),
            (13.70609, 100.45006, "ถ.ราชพฤกษ์ ใกล้แยกท่าพระ", "general", 140, 120, 2, 1, "demo", None),
            (13.81795, 100.43797, "ถ.ราชพฤกษ์ ใกล้ถ.รัตนาธิเบศร์", "general", 15, 5, 6, 3, "demo", None),
            (13.72312, 100.43614, "ถ.กัลปพฤกษ์", "motorbike", 200, 170, 1, 1, "demo", None),
            (13.7765, 100.479, "ถ.บรมราชชนนี ปิ่นเกล้า", "alcohol", 45, 20, 5, 2, "demo", None),
            (13.6681, 100.60474, "ถ.บางนา-ตราด กม.1", "general", 90, 70, 2, 1, "demo", None),
            (13.66504, 100.63787, "ถ.บางนา-ตราด หน้าเซ็นทรัลบางนา", "alcohol", 30, 12, 3, 2, "demo", None),
            (13.66297, 100.67596, "ถ.บางนา-ตราด กม.8", "general", 25, 8, 4, 2, "demo", None),
            (13.67007, 100.61981, "แยกบางนา ถ.สรรพาวุธ", "motorbike", 60, 30, 3, 2, "demo", None),
            (13.69314, 100.60393, "ถ.สุขุมวิท 101/1", "motorbike", 140, 120, 2, 1, "demo", None),
            (13.80268, 100.55359, "ถ.พหลโยธิน ห้าแยกลาดพร้าว", "alcohol", 15, 5, 6, 3, "demo", None),
            (13.81598, 100.59598, "ถ.ลาดพร้าว 101", "motorbike", 200, 170, 1, 1, "demo", None),
            (13.78917, 100.63992, "ถ.รามอินทรา กม.2", "general", 45, 20, 5, 2, "demo", None),
            (13.75699, 100.64593, "ถ.รามคำแหง หน้าม.ราม", "motorbike", 90, 70, 2, 1, "demo", None),
            (13.74499, 100.59602, "ถ.พัฒนาการ ซ.20", "alcohol", 30, 12, 3, 2, "demo", None),
            (13.69602, 100.54271, "ถ.พระราม 3 ใกล้สาธุประดิษฐ์", "general", 25, 8, 4, 2, "demo", None),
            (13.71783, 100.50289, "ถ.เจริญนคร", "motorbike", 60, 30, 3, 2, "demo", None),
            (13.68684, 100.4904, "ถ.สุขสวัสดิ์ ดาวคะนอง", "black_smoke", 140, 120, 2, 1, "demo", None),
            (13.72917, 100.47407, "ถ.เพชรเกษม ภาษีเจริญ", "alcohol", 15, 5, 6, 3, "demo", None),
            (13.71397, 100.4084, "ถ.เพชรเกษม หลักสอง", "truck", 200, 170, 1, 1, "demo", None),
            (13.83315, 100.51498, "ถ.งามวงศ์วาน แคราย", "general", 45, 20, 5, 2, "demo", None),
            (13.847, 100.5725, "ถ.วิภาวดีรังสิต หลักสี่", "black_smoke", 90, 70, 2, 1, "demo", None),
            (13.7608, 100.57731, "ถ.เพชรบุรีตัดใหม่ อโศก", "alcohol", 30, 12, 3, 2, "demo", None),
            # road incidents (same table; category comes from the type)
            (13.72258, 100.56003, "ถ.พระราม 4 ใกล้คลองเตย", "roadwork", 240, 40, 3, 2, "demo", None),
            (13.77610, 100.55870, "ถ.วิภาวดีรังสิต ขาเข้า ใกล้ดินแดง", "accident", 25, 10, 4, 3, "demo", None),
            (13.71862, 100.59672, "ถ.สุขุมวิท ซ.71", "flood", 70, 30, 2, 2, "demo", None),
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
        "heading": row["heading"] if "heading" in row.keys() else None,
    }


@app.route("/")
def index():
    return render_template("index.html", photo_on=PHOTO_REQUIRED, gmaps_key=GOOGLE_MAPS_KEY)


# ---------- analytics (first-party, anonymous) ----------
STATS_TOKEN = os.environ.get("STATS_TOKEN", "").strip()
TZ_OFFSET = 7 * 3600   # report days in Bangkok time


EVENT_KINDS = {"report_open", "route_search", "nav_google", "nav_apple", "detail_open", "share",
               "install_shown", "install_click", "installed", "filter", "lang", "theme"}


def log_event(db, kind, standalone=False, meta=None):
    try:
        db.execute("INSERT INTO events (kind, device_id, standalone, created_at, meta) VALUES (?,?,?,?,?)",
                   (kind, _device_id() or _client_id(), bool(standalone), int(time.time()),
                    json.dumps(meta, ensure_ascii=False)[:500] if meta else None))
    except Exception as e:  # noqa: BLE001 — analytics must never break the app
        app.logger.warning("event log failed: %s", e)


@app.route("/api/visit", methods=["POST"])
def visit():
    db = get_db()
    d = request.get_json(silent=True) or {}
    meta = {k: str(d.get(k))[:60] for k in ("platform", "lang", "src", "theme") if d.get(k) is not None}
    meta["new"] = bool(d.get("new"))
    log_event(db, "visit", d.get("standalone", False), meta)
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/event", methods=["POST"])
def event():
    """Feature-usage events from the client (whitelisted kinds, small meta)."""
    d = request.get_json(silent=True) or {}
    kind = str(d.get("kind", ""))
    if kind not in EVENT_KINDS:
        return jsonify({"ok": False}), 400
    meta = d.get("meta") if isinstance(d.get("meta"), dict) else None
    if meta:
        meta = {str(k)[:20]: (v if isinstance(v, (int, float, bool)) else str(v)[:60]) for k, v in list(meta.items())[:8]}
    db = get_db()
    log_event(db, kind, d.get("standalone", False), meta)
    db.commit()
    return jsonify({"ok": True})


def _stats(days, gran="day"):
    """Data for the interactive dashboard: current period + previous period (for deltas and the
    comparison line), per-bucket metrics *and* per-bucket breakdowns so the UI can cross-filter
    by clicking a period."""
    import datetime as dt
    from collections import Counter, defaultdict
    now = int(time.time())
    since, prev_since = now - days * 86400, now - 2 * days * 86400
    db = get_db()
    rows = db.execute(
        "SELECT kind, device_id, standalone, created_at, meta FROM events WHERE created_at >= ?", (prev_since,)
    ).fetchall()
    first_seen = {r["device_id"]: r["fs"] for r in db.execute(
        "SELECT device_id, MIN(created_at) AS fs FROM events WHERE kind = 'visit' GROUP BY device_id").fetchall()}
    local = lambda ts: dt.datetime.utcfromtimestamp(ts + TZ_OFFSET)

    def bucket(ts):
        d = local(ts)
        if gran == "month":
            return d.strftime("%Y-%m")
        if gran == "week":
            return (d - dt.timedelta(days=d.weekday())).strftime("%Y-%m-%d")
        return d.strftime("%Y-%m-%d")

    def keys(a, b):
        out, t = [], a
        while t <= b:
            k = bucket(t)
            if not out or out[-1] != k:
                out.append(k)
            t += 86400
        if bucket(b) != out[-1]:
            out.append(bucket(b))
        return out

    def empty():
        return {"visitors": set(), "new": set(), "visits": 0, "sa": 0, "cp": 0, "inc": 0, "merged": 0,
                "reporters": set(), "routes": 0, "route_users": set(), "nav": 0, "nav_users": set(),
                "report_open": set(), "inst_shown": set(), "inst_click": set(), "installed": set(),
                "src": Counter(), "plat": {}, "types": Counter()}

    cur_keys, prev_keys = keys(since, now), keys(prev_since, since - 1)
    cur = {k: empty() for k in cur_keys}
    prev = {k: empty() for k in prev_keys}
    tot, ptot = empty(), empty()
    feat, feat_users = Counter(), defaultdict(set)
    hours, points, daily_users = Counter(), [], defaultdict(set)

    for r in rows:
        kind, dev, ts = r["kind"], r["device_id"], r["created_at"]
        try:
            m = json.loads(r["meta"]) if r["meta"] else {}
        except ValueError:
            m = {}
        is_cur = ts >= since
        k = bucket(ts)
        bk = (cur if is_cur else prev).get(k)
        start = since if is_cur else prev_since
        for b, scope in ((bk, "bucket"), (tot if is_cur else ptot, "total")):
            if b is None:
                continue
            if kind == "visit":
                b["visits"] += 1; b["visitors"].add(dev)
                if r["standalone"]:
                    b["sa"] += 1
                fs = first_seen.get(dev, ts)
                if (scope == "bucket" and bucket(fs) == k) or (scope == "total" and fs >= start):
                    b["new"].add(dev)
                b["src"][m.get("src") or "direct"] += 1
                b["plat"].setdefault(dev, m.get("platform", "unknown"))
            elif kind == "report":
                b["inc" if m.get("cat") == "incident" else "cp"] += 1
                b["reporters"].add(dev); b["types"][m.get("type", "general")] += 1
                if m.get("merged"):
                    b["merged"] += 1
            elif kind == "route_search":
                b["routes"] += 1; b["route_users"].add(dev)
            elif kind in ("nav_google", "nav_apple"):
                b["nav"] += 1; b["nav_users"].add(dev)
            elif kind == "report_open":
                b["report_open"].add(dev)
            elif kind == "install_shown":
                b["inst_shown"].add(dev)
            elif kind == "install_click":
                b["inst_click"].add(dev)
            elif kind == "installed":
                b["installed"].add(dev)
        if is_cur:
            feat[kind] += 1; feat_users[kind].add(dev)
            if kind == "visit":
                daily_users[local(ts).strftime("%Y-%m-%d")].add(dev)
            if kind == "report":
                lt = local(ts); hours[(lt.weekday(), lt.hour)] += 1
                if "lat" in m and len(points) < 3000:
                    points.append([m["lat"], m["lng"], m.get("cat", "checkpoint")])

    pct = lambda a, b: round(100 * a / b, 1) if b else 0

    def ser(b, period=None):
        v = len(b["visitors"]); n = len(b["new"] & b["visitors"]); rep = b["cp"] + b["inc"]
        plat = Counter(b["plat"].values())
        return {
            "period": period, "visitors": v, "new": n, "returning": v - n, "returning_pct": pct(v - n, v),
            "visits": b["visits"], "reports": rep, "reports_cp": b["cp"], "reports_inc": b["inc"],
            "reporters": len(b["reporters"]), "contrib": pct(len(b["reporters"]), v), "merge_rate": pct(b["merged"], rep),
            "routes": b["routes"], "route_users": len(b["route_users"]), "nav": b["nav"],
            "shortcut_pct": pct(b["sa"], b["visits"]),
            "sources": b["src"].most_common(10), "platforms": plat.most_common(), "types": b["types"].most_common(),
            "funnel": {"report": [v, len(b["report_open"]), len(b["reporters"])],
                       "route": [v, len(b["route_users"]), len(b["nav_users"])],
                       "install": [len(b["inst_shown"]), len(b["inst_click"]), len(b["installed"])]},
        }

    timeline = [ser(cur[k], k) for k in cur_keys]
    prev_tl = [ser(prev[k], k) for k in prev_keys][-len(cur_keys):]
    prev_tl = [None] * (len(cur_keys) - len(prev_tl)) + prev_tl

    all_days = [(local(since) + dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days + 1)]
    dau = sum(len(daily_users.get(x, ())) for x in all_days) / max(1, len(all_days))
    mau = len({r["device_id"] for r in rows if r["kind"] == "visit" and r["created_at"] >= now - 30 * 86400})

    # weekly retention cohorts by first visit
    def wk(ts):
        x = local(ts); return (x - dt.timedelta(days=x.weekday())).date()
    visit_weeks = defaultdict(set)
    for r in db.execute("SELECT device_id, created_at FROM events WHERE kind = 'visit' AND created_at >= ?",
                        (now - 9 * 7 * 86400,)).fetchall():
        visit_weeks[r["device_id"]].add(wk(r["created_at"]))
    cohorts = defaultdict(set)
    for dev, fs in first_seen.items():
        if fs >= now - 8 * 7 * 86400:
            cohorts[wk(fs)].add(dev)
    retention = []
    for c in sorted(cohorts, reverse=True):
        devs = cohorts[c]
        weeks = []
        for n in range(6):
            w = c + dt.timedelta(weeks=n)
            if w > wk(now):
                break
            weeks.append(round(100 * sum(1 for x in devs if w in visit_weeks[x]) / len(devs)))
        retention.append({"cohort": c.isoformat(), "size": len(devs), "weeks": weeks})

    FEATS = ("route_search", "nav_google", "nav_apple", "detail_open", "vote", "share", "report_open", "report",
             "filter", "lang", "theme")
    return {
        "days": days, "gran": gran, "generated": local(now).strftime("%Y-%m-%d %H:%M"),
        "total": ser(tot), "prev": ser(ptot),
        "engagement": {"dau": round(dau, 1), "mau": mau, "stickiness": pct(dau, mau)},
        "timeline": timeline, "prev_timeline": prev_tl,
        "features": sorted([[f, feat[f], len(feat_users[f])] for f in FEATS if feat[f]], key=lambda x: -x[1]),
        "hours": [[w, h, n] for (w, h), n in hours.items()], "points": points, "retention": retention,
    }


def _stats_allowed():
    return bool(STATS_TOKEN) and request.args.get("token") == STATS_TOKEN


@app.route("/api/stats")
def stats_json():
    if not _stats_allowed():
        return jsonify({"error": "forbidden"}), 403
    return jsonify(_stats(*_stats_args()))


@app.route("/stats")
def stats_page():
    if not _stats_allowed():
        return "Set STATS_TOKEN on the server and open /stats?token=…", 403
    days, gran = _stats_args()
    return render_template("stats.html", data=_stats(days, gran), token=request.args.get("token"))


def _stats_args():
    try:
        days = max(1, min(int(request.args.get("days", 30)), 730))
    except ValueError:
        days = 30
    gran = request.args.get("gran", "day" if days <= 31 else "week" if days <= 180 else "month")
    return days, gran if gran in ("day", "week", "month") else "day"


@app.route("/manifest.webmanifest")
def manifest():
    return app.response_class(json.dumps({
        "name": "glubDD", "short_name": "glubDD", "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#111214", "theme_color": "#111214", "lang": "th",
        "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}],
    }), mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    # Minimal network-first worker: makes the site installable, always serves fresh content.
    js = "self.addEventListener('install',e=>self.skipWaiting());" \
         "self.addEventListener('activate',e=>self.clients.claim());" \
         "self.addEventListener('fetch',e=>{});"
    return app.response_class(js, mimetype="application/javascript", headers={"Cache-Control": "no-cache"})


@app.route("/api/health")
def health():
    """Which storage is live (no secrets) — handy after changing DATABASE_URL."""
    try:
        n = get_db().scalar("SELECT COUNT(*) FROM checkpoints")
        return jsonify({"ok": True, "db": "postgres" if dbmod.IS_PG else "sqlite", "reports": n})
    except Exception as e:  # noqa: BLE001 — report connectivity problems, not crash
        app.logger.warning("health check failed: %s", e)
        return jsonify({"ok": False, "db": "postgres" if dbmod.IS_PG else "sqlite"}), 503


@app.route("/api/checkpoints")
def list_checkpoints():
    now = int(time.time())
    rows = get_db().execute(
        "SELECT * FROM checkpoints WHERE last_confirmed_at > ? ORDER BY last_confirmed_at DESC",
        (now - HIDE_AFTER_SECONDS,),
    ).fetchall()
    return jsonify({"now": now, "checkpoints": [row_to_dict(r, now) for r in rows]})


def _device_id():
    """Anonymous per-browser id (random UUID kept in localStorage). Becomes linkable to a user
    account once accounts exist. Not trusted for security on its own."""
    d = (request.headers.get("X-Device-Id") or "").strip()
    return d[:64] if d else None


def _client_id():
    return _device_id() or (request.headers.get("X-Forwarded-For") or request.remote_addr or "?").split(",")[0].strip()


def _rate_limited():
    now = time.time()
    n, window = REPORT_RATE
    hits = [t for t in _recent_reports.get(_client_id(), []) if now - t < window]
    _recent_reports[_client_id()] = hits
    return len(hits) >= n


@app.route("/api/checkpoints", methods=["POST"])
def add_checkpoint():
    data = request.get_json(force=True)
    try:
        lat, lng = float(data["lat"]), float(data["lng"])
        ulat, ulng = float(data["user_lat"]), float(data["user_lng"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "need_gps"}), 400
    label = (data.get("label") or "").strip()[:80] or "ไม่ระบุตำแหน่ง"
    typ = (data.get("type") or "").strip()
    typ = LEGACY_TYPES.get(typ, typ)
    if typ not in TYPE_CODES or typ in RETIRED_TYPES:
        typ = "general"

    try:
        heading = float(data["heading"]) % 360 if data.get("heading") is not None else None
    except (TypeError, ValueError):
        heading = None

    if _rate_limited():
        return jsonify({"ok": False, "error": "rate_limited"}), 429
    if haversine_m(lat, lng, ulat, ulng) > REPORT_MAX_DIST_M:
        return jsonify({"ok": False, "error": "too_far"}), 400
    _recent_reports.setdefault(_client_id(), []).append(time.time())
    if heading is not None:
        lat, lng = _snap_to_carriageway(lat, lng, heading)

    now = int(time.time())
    db = get_db()
    # Grouping: reports from any users of the same category (checkpoint vs incident) within
    # DEDUP_RADIUS_M of an active report are the same thing → merge into the nearest one and count
    # the new report as a confirmation (ยืนยัน). Each device confirms a given report at most once.
    active = db.execute(
        "SELECT * FROM checkpoints WHERE cleared_at IS NULL AND source != 'demo' AND last_confirmed_at > ?",
        (now - ACTIVE_SECONDS,),
    ).fetchall()
    cat = type_category(typ)
    cands = []
    for r in active:
        if type_category(r["type"]) != cat:
            continue
        ok, d = same_spot(lat, lng, heading, r)
        if ok:
            cands.append((d, r))
    cands.sort(key=lambda x: x[0])
    if cands:
        row = cands[0][1]
        device = _device_id() or _client_id()
        seen = db.execute("SELECT 1 FROM votes WHERE report_id = ? AND device_id = ?", (row["id"], device)).fetchone()
        if row["reporter_device"] == device or seen:
            db.execute("UPDATE checkpoints SET last_confirmed_at=? WHERE id=?", (now, row["id"]))
        else:
            db.execute(
                "UPDATE checkpoints SET last_confirmed_at=?, confirms=confirms+1, reports=reports+1, dismisses=0 WHERE id=?",
                (now, row["id"]),
            )
            db.execute("INSERT INTO votes (report_id, device_id, still_there, created_at) VALUES (?,?,?,?)", (row["id"], device, True, now))
        log_event(db, "report", meta={"type": typ, "cat": cat, "merged": True, "lat": round(lat, 3), "lng": round(lng, 3)})
        db.commit()
        return jsonify({"ok": True, "merged": True, "id": row["id"]})

    new_id = db.insert(
        "INSERT INTO checkpoints (lat,lng,label,type,reported_at,last_confirmed_at,reporter_device,heading) VALUES (?,?,?,?,?,?,?,?)",
        (lat, lng, label, typ, now, now, _device_id(), heading),
    )
    log_event(db, "report", meta={"type": typ, "cat": cat, "merged": False, "lat": round(lat, 3), "lng": round(lng, 3)})
    db.commit()
    return jsonify({"ok": True, "merged": False, "id": new_id})


def _snap_to_carriageway(lat, lng, heading):
    """On divided roads each direction is its own OSM way; OSRM /nearest with a bearing picks the
    carriageway running that way. Only small moves are applied; any failure keeps the pin as-is."""
    try:
        url = (f"https://router.project-osrm.org/nearest/v1/driving/{lng},{lat}"
               f"?number=1&bearings={int(heading)},45")
        wp = _http_json(url, timeout=4).get("waypoints", [])
        if wp and wp[0].get("distance", 1e9) <= SNAP_MAX_M:
            slng, slat = wp[0]["location"]
            return slat, slng
    except NET_ERRORS as e:
        app.logger.info("snap skipped: %s", e)
    return lat, lng


@app.route("/api/checkpoints/<int:cid>/flip", methods=["POST"])
def flip_side(cid):
    """'Wrong side' fix: reverse the report's direction, then merge it into a matching report on
    that side if one exists."""
    db = get_db()
    row = db.execute("SELECT * FROM checkpoints WHERE id = ?", (cid,)).fetchone()
    if row is None or row["heading"] is None:
        return jsonify({"ok": False, "error": "no_direction"}), 400
    h = (row["heading"] + 180) % 360
    db.execute("UPDATE checkpoints SET heading = ? WHERE id = ?", (h, cid))
    now = int(time.time())
    others = db.execute(
        "SELECT * FROM checkpoints WHERE id != ? AND cleared_at IS NULL AND source != 'demo' AND last_confirmed_at > ?",
        (cid, now - ACTIVE_SECONDS),
    ).fetchall()
    cat = type_category(row["type"])
    match = sorted(((d, r) for r in others if type_category(r["type"]) == cat
                    for ok, d in [same_spot(row["lat"], row["lng"], h, r)] if ok), key=lambda x: x[0])
    if match:
        target = match[0][1]
        db.execute(
            "UPDATE checkpoints SET confirms = confirms + ?, reports = reports + ?, last_confirmed_at = ? WHERE id = ?",
            (row["confirms"], row["reports"], max(row["last_confirmed_at"], target["last_confirmed_at"]), target["id"]),
        )
        db.execute("DELETE FROM checkpoints WHERE id = ?", (cid,))
        db.commit()
        return jsonify({"ok": True, "merged_into": target["id"]})
    db.commit()
    return jsonify({"ok": True, "heading": h})


@app.route("/api/checkpoints/<int:cid>/vote", methods=["POST"])
def vote(cid):
    still = bool(request.get_json(force=True).get("still"))
    now = int(time.time())
    db = get_db()
    if db.scalar("SELECT COUNT(*) FROM checkpoints WHERE id = ?", (cid,)) == 0:
        return jsonify({"ok": False, "error": "not_found"}), 404
    device = _device_id() or _client_id()
    prev = db.execute("SELECT still_there FROM votes WHERE report_id = ? AND device_id = ?", (cid, device)).fetchone()
    if prev is not None and bool(prev["still_there"]) == still:
        return jsonify({"ok": True, "already": True})   # same vote again: no double counting
    if prev is None:
        db.execute("INSERT INTO votes (report_id, device_id, still_there, created_at) VALUES (?,?,?,?)", (cid, device, still, now))
    else:
        db.execute("UPDATE votes SET still_there = ?, created_at = ? WHERE report_id = ? AND device_id = ?", (still, now, cid, device))
    if still:
        db.execute(
            "UPDATE checkpoints SET last_confirmed_at=?, confirms=confirms+1, dismisses=0, cleared_at=NULL WHERE id=?",
            (now, cid),
        )
    else:
        db.execute("UPDATE checkpoints SET dismisses=dismisses+1 WHERE id=?", (cid,))
        if db.scalar("SELECT dismisses FROM checkpoints WHERE id=?", (cid,)) >= DISMISS_THRESHOLD:
            db.execute("UPDATE checkpoints SET cleared_at=? WHERE id=?", (now, cid))
    log_event(db, "vote", meta={"still": still})
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
        "extra_info": ["tollways", "waycategory"],
    }
    if avoid_tolls:
        body["options"] = {"avoid_features": ["tollways"]}
    js = _http_json(ORS_URL, body, {"Authorization": ORS_KEY, "Content-Type": "application/json"})
    out = []
    for f in js.get("features", []):
        summ = f["properties"].get("summary", {})
        toll_vals = f["properties"].get("extras", {}).get("tollways", {}).get("summary", [])
        uses_toll = any(v.get("value") == 1 and v.get("distance", 0) > 0 for v in toll_vals)
        wc = f["properties"].get("extras", {}).get("waycategory", {}).get("values")
        # tollway/motorway sections as [start, end] indices into coords; None = unknown
        restricted = [[s, e] for s, e, v in wc if v & 3] if wc is not None else None
        out.append({
            "coords": [[c[1], c[0]] for c in f["geometry"]["coordinates"]],
            "km": summ.get("distance", 0) / 1000,
            "mins": round(summ.get("duration", 0) / 60),
            "toll": uses_toll,
            "restricted": restricted,
        })
    return out


def _osrm_routes(o, d):
    js = _http_json(OSRM_URL.format(o_lat=o[0], o_lng=o[1], d_lat=d[0], d_lng=d[1]))
    return [{
        "coords": [[c[1], c[0]] for c in rt["geometry"]["coordinates"]],
        "km": rt["distance"] / 1000,
        "mins": round(rt["duration"] / 60),
        "toll": None,  # unknown on OSRM
        "restricted": None,  # unknown on OSRM → no route points
    } for rt in js.get("routes", [])]


def _tomtom_routes(o, d, avoid_tolls):
    params = {
        "key": TOMTOM_KEY, "traffic": "true", "travelMode": "car", "routeType": "fastest",
        "maxAlternatives": 2, "routeRepresentation": "polyline",
        # `tollRoad` is deprecated by TomTom (may return HTTP 400) → `toll`. `motorway` is
        # requested so the app can keep route points off motorways.
        "sectionType": ["toll", "motorway"],
    }
    if avoid_tolls:
        params["avoid"] = "tollRoads"
    url = TOMTOM_ROUTE_URL.format(o_lat=o[0], o_lng=o[1], d_lat=d[0], d_lng=d[1])
    js = _http_json(url + "?" + urllib.parse.urlencode(params, doseq=True))
    out = []
    for rt in js.get("routes", []):
        summ = rt.get("summary", {})
        secs = rt.get("sections", [])
        coords = [[p["latitude"], p["longitude"]] for leg in rt.get("legs", []) for p in leg.get("points", [])]
        out.append({
            "coords": coords,
            "km": summ.get("lengthInMeters", 0) / 1000,
            "mins": round(summ.get("travelTimeInSeconds", 0) / 60),
            "delay_mins": round(summ.get("trafficDelayInSeconds", 0) / 60),
            "toll": any(sec.get("sectionType") == "TOLL" for sec in secs),
            # [startPointIndex, endPointIndex] of tollway/motorway sections (indices into coords)
            "restricted": [[sec["startPointIndex"], sec["endPointIndex"]] for sec in secs
                           if sec.get("sectionType") in ("TOLL", "MOTORWAY")
                           and "startPointIndex" in sec and "endPointIndex" in sec],
        })
    return out


def _same_route(a, b):
    return abs(a["km"] - b["km"]) < 0.3 and abs(a["mins"] - b["mins"]) <= 1


NET_ERRORS = (urllib.error.URLError, TimeoutError, ValueError, KeyError)


@app.route("/api/routes")
def routes():
    try:
        o = (float(request.args["olat"]), float(request.args["olng"]))
        d = (float(request.args["dlat"]), float(request.args["dlng"]))
    except (KeyError, ValueError):
        return jsonify({"error": "bad coordinates"}), 400

    # Providers in order of preference. Each is asked for its normal options with tolls
    # allowed AND with tolls avoided.
    providers = []
    if TOMTOM_KEY:
        providers.append(("tomtom", _tomtom_routes))
    if ORS_KEY:
        providers.append(("ors", _ors_routes))

    found, engine = [], "osrm"
    for name, fn in providers:
        for avoid in (False, True):
            try:
                for r in fn(o, d, avoid):
                    if r["coords"] and not any(_same_route(r, x) for x in found):
                        found.append(r)
            except NET_ERRORS as e:
                app.logger.warning("%s failed (avoid_tolls=%s): %s", name, avoid, e)
        if found:
            engine = name
            break
    if not found:
        try:
            found = _osrm_routes(o, d)
        except NET_ERRORS as e:
            app.logger.warning("OSRM failed: %s", e)
            return jsonify({"error": "routing unavailable"}), 502
    found.sort(key=lambda r: r["mins"])  # travel time only
    return jsonify({"engine": engine, "traffic": engine == "tomtom", "routes": found})


# ---------- place search (autocomplete) ----------
# TomTom fuzzy search (typo-tolerant, typeahead) when TOMTOM_KEY is set, else Photon (free).

def _tomtom_search(q, lat, lng, lang):
    params = {
        "key": TOMTOM_KEY, "typeahead": "true", "limit": 7, "countrySet": "TH",
        "language": "th-TH" if lang == "th" else "en-US",
    }
    if lat is not None:
        params.update(lat=lat, lon=lng)
    url = TOMTOM_SEARCH_URL.format(q=urllib.parse.quote(q, safe="")) + "?" + urllib.parse.urlencode(params)
    out = []
    for r in _http_json(url).get("results", []):
        addr = r.get("address", {})
        name = (r.get("poi") or {}).get("name") or addr.get("streetName") or addr.get("freeformAddress", "")
        out.append({
            "name": name,
            "address": addr.get("freeformAddress", ""),
            "lat": r["position"]["lat"],
            "lng": r["position"]["lon"],
            "dist_m": r.get("dist"),
        })
    return out


def _photon_search(q, lat, lng, lang):
    params = {"q": q, "limit": 7}
    if lat is not None:
        params.update(lat=lat, lon=lng, location_bias_scale=0.3)
    if lang == "en":
        params["lang"] = "en"
    js = _http_json(PHOTON_URL + "?" + urllib.parse.urlencode(params),
                    headers={"User-Agent": "daan-app-prototype"})
    out = []
    for f in js.get("features", []):
        p = f.get("properties", {})
        if p.get("countrycode") and p["countrycode"] != "TH":
            continue
        street = " ".join(x for x in (p.get("housenumber"), p.get("street")) if x)
        parts = [street, p.get("locality") or p.get("district"), p.get("city")]
        name = p.get("name") or street or p.get("city", "")
        lng_, lat_ = f["geometry"]["coordinates"]
        out.append({
            "name": name,
            "address": ", ".join(x for x in parts if x and x != name),
            "lat": lat_, "lng": lng_,
            "dist_m": haversine_m(lat, lng, lat_, lng_) if lat is not None else None,
        })
    return out


def _nominatim_search(q, lat, lng, lang):
    # Nominatim's usage policy forbids autocomplete, so this is only used on an explicit
    # submit (Enter / Check) — it handles Thai-script queries that Photon misses.
    params = {"format": "jsonv2", "limit": 5, "countrycodes": "th", "q": q, "accept-language": lang}
    if lat is not None:
        params.update(viewbox=f"{lng-0.5},{lat+0.5},{lng+0.5},{lat-0.5}")
    js = _http_json(NOMINATIM_URL + "?" + urllib.parse.urlencode(params),
                    headers={"User-Agent": "daan-app-prototype"})
    out = []
    for r in js:
        lat_, lng_ = float(r["lat"]), float(r["lon"])
        name = r.get("name") or r["display_name"].split(",")[0]
        addr = ", ".join(x.strip() for x in r["display_name"].split(",")[1:4])
        out.append({"name": name, "address": addr, "lat": lat_, "lng": lng_,
                    "dist_m": haversine_m(lat, lng, lat_, lng_) if lat is not None else None})
    return out


@app.route("/api/search")
def search():
    q = (request.args.get("q") or "").strip()[:120]
    if len(q) < 2:
        return jsonify({"results": []})
    lang = "en" if request.args.get("lang") == "en" else "th"
    try:
        lat, lng = float(request.args["lat"]), float(request.args["lng"])
    except (KeyError, ValueError):
        lat = lng = None
    results = []
    if TOMTOM_KEY:
        try:
            results = _tomtom_search(q, lat, lng, lang)
        except NET_ERRORS as e:
            app.logger.warning("TomTom search failed: %s", e)
    if not results:
        try:
            results = _photon_search(q, lat, lng, lang)
        except NET_ERRORS as e:
            app.logger.warning("Photon search failed: %s", e)
    if not results and request.args.get("final") == "1":
        try:
            results = _nominatim_search(q, lat, lng, lang)
        except NET_ERRORS as e:
            app.logger.warning("Nominatim search failed: %s", e)
    return jsonify({"results": results})

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5051)), use_reloader=False)
