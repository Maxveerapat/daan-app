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

## Report anti-spam (zero operating cost)

- **Live camera photo only** (in-app camera, no upload option).
- **On-device photo check:** MobileCLIP-S0 (free open model) runs in the phone's browser via
  transformers.js and scores whether the photo shows a checkpoint (police, cones, barriers, signs).
  ~23 MB download on first report, cached after. Text-side embeddings are precomputed in
  `static/clip_labels.json`. The photo never leaves the phone. Threshold `CLIP_THRESHOLD = 0.30`.
- **Server:** reporter's live GPS must be within 500 m of the pin; max 5 reports / 10 min per client.

Limitation: the photo check runs client-side, so a determined spammer calling the API directly can
skip it — GPS + rate limits still apply. Good enough for a tester build.

## Routing & search

Both run server-side, keys come from environment variables (Render → Environment):

- **`TOMTOM_KEY`** (recommended, free at https://developer.tomtom.com, ~2,500 requests/day):
  live-traffic route options with tolls allowed **and** avoided, each labeled Tollway / No tollway with
  traffic delay, ordered by travel time; plus typo-tolerant place autocomplete.
- `ORS_KEY` (optional fallback): OpenRouteService routes, no live traffic.
- No keys: OSRM demo (usually 1 route) + Photon autocomplete, Nominatim on submit for Thai queries.

Checkpoints are only *counted* per route — they never influence which routes are returned or their order.

## ⚠️ Scope boundary

This app **displays** checkpoints (Waze-style awareness) but is deliberately built to **never
route around them** — no ranking by checkpoint count, no forcing nav apps onto checkpoint-free
paths. See the "Scope boundary" section in `HANDOFF.md` before adding routing features.
