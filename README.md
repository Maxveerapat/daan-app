# ด่าน — checkpoint alert (prototype)

A Thai crowdsourced road-checkpoint ("ด่าน") awareness app — a modern rebuild of the
"ด่าน มีด่านบอกด้วย" concept. Users report checkpoints on a dark, Apple/Tesla-style map;
others see what's active near them or along a route before they drive.

> **Prototype.** Single-file Flask backend + one HTML page. Seed data is mock Bangkok data.

## Run it

```bash
cd daan-app
python3 app.py          # serves on http://localhost:5051 (binds 0.0.0.0 for LAN/phone testing)
```

Open `http://localhost:5051`. For the mobile look, use your browser's device toolbar
(Chrome: Cmd+Opt+I → Cmd+Shift+M → pick iPhone). `localhost` is a secure context so
browser geolocation works there.

**Phone on same Wi-Fi:** find your machine IP (`ipconfig getifaddr en0`) and open
`http://<ip>:5051`. Note: browsers block GPS over plain http, so the phone falls back to a
demo location (Victory Monument); everything else works.

**Fresh data:** reports auto-expire from the feed after ~3h and vanish entirely after 12h.
If the map looks empty, the seed has aged out — delete `daan.db` and restart to reseed.

## Share with testers (public link via Render)

The repo includes `render.yaml`, `requirements.txt` and a gunicorn start command, so Render can host it for free:

1. Go to https://dashboard.render.com → **New → Blueprint** → connect GitHub → pick `maxveerapat/daan-app`
   (choose the branch that has `render.yaml`). Click **Apply**.
2. After ~2 min you get `https://daan-app-xxxx.onrender.com` — send that link to testers.
   It's https, so real phone GPS works.

Notes (free plan): the app sleeps after ~15 min idle, so the first visit takes ~30–60s to wake up.
The SQLite DB is reset on every restart/redeploy (seed data comes back fresh; tester reports are lost).

## Stack

- **Backend:** Flask + SQLite (`app.py`, ~184 lines). Seeds mock Bangkok checkpoints on first run.
- **Frontend:** one page (`templates/index.html`) — Leaflet + OpenFreeMap vector basemap (MapLibre,
  no API key, recoloured dark), TH/EN language switch, Nominatim geocoding. No build step.

## Structure

```
daan-app/
├── app.py                 # Flask API + SQLite schema + seed data
├── templates/index.html   # entire UI (HTML/CSS/JS in one file)
├── HANDOFF.md             # full context & decisions — READ THIS to continue the project
├── README.md
└── .gitignore
```

## Three tabs

- **แผนที่ (Map):** recency-colored pins (red <1h, amber 1–3h, gray >3h).
- **รายการ (Feed):** active checkpoints sorted by distance, 3h auto-expiry + collapse, confirm/clear votes.
- **เส้นทาง (Route):** enter a destination → route options (with/without tolls), each labeled with
  on-route checkpoint count → hand off to Google/Apple Maps to drive.

## Routing (route options with / without tolls)

Routing runs server-side at `/api/routes`. With an `ORS_KEY` environment variable set (free
OpenRouteService key: https://openrouteservice.org/dev/#/signup → Render → Environment), it returns
the engine's normal options with tolls allowed **and** avoided (typically 2–5), each labeled
Tollway / No tollway, ordered by travel time, with a checkpoint count shown for each.
Without the key it falls back to the OSRM demo (usually 1 route, no toll info).

## ⚠️ Scope boundary

This app **displays** checkpoints (Waze-style awareness) but is deliberately built to **never
route around them** — no ranking by checkpoint count, no forcing nav apps onto checkpoint-free
paths. See the "Scope boundary" section in `HANDOFF.md` before adding routing features.
