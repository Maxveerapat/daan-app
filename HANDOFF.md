# daan-app — Project Handoff & Context

Self-contained context so this project can continue in a fresh Claude session (new account).
Everything essential is in this repo — clone it, read this file, and you're caught up.
Last updated 2026-11-20.

---

## 1. What this is

A Thai crowdsourced road-checkpoint ("ด่าน") awareness app — a modern rebuild of the existing
"ด่าน มีด่านบอกด้วย" app. Drivers report police/traffic checkpoints; others see what's active
near them or along their route **before** they drive. Prototype: Flask + one HTML page, mock
Bangkok seed data.

**Owner:** Max, Mobile Squad, The 1.

---

## 2. Current status — what's built and working

All verified working in-browser during development:

- **Map tab (แผนที่):** full-bleed dark map (CARTO Dark Matter), pins colored by recency
  (red <1h / amber 1–3h / gray >3h) with a ring color by source (white=user, blue=official),
  floating source filter chips, floating legend. Tap a pin → popup with type, last-confirmed
  time, confirm count, and vote buttons.
- **Feed tab (รายการ):** active checkpoints sorted by distance from the user, relative
  timestamps, confirm counts; items >3h collapse into an expandable section; 12h hard expiry.
  Vote "ยังอยู่" (still there) / "ไม่อยู่แล้ว" (gone).
- **Route tab (เส้นทาง):** type a destination (or tap map) → route option(s), each labeled with
  its on-route checkpoint count + road-condition count → **Google Maps / Apple Maps** hand-off
  buttons to actually drive.
- **Report (รายงานด่าน):** FAB opens a bottom sheet — GPS-centered pin-drop + type chips +
  optional label. ~3 taps. Server merges same-type reports within 300 m as confirmations.
- **Design:** Apple/Tesla dark aesthetic — near-black (#0B0B0F), red accent (#FF453A), blurred
  floating chrome, grabber-handle sheets, subtle motion (pin-drop stagger, sheet slide-ups,
  reduced-motion guarded).

---

## 3. Architecture & stack

- **Backend — `app.py`** (~184 lines): Flask + SQLite. One table `checkpoints`. REST:
  - `GET /api/checkpoints` — active + recently-expired (within 12h), each with computed
    `age_minutes` and `status` (active / stale / cleared).
  - `POST /api/checkpoints` — new report; dedup-merges same-type within 300 m (except `official`).
  - `POST /api/checkpoints/<id>/vote` — `{still:true}` re-confirms & resets freshness;
    `{still:false}` adds a dismiss; 2 dismisses set `cleared_at`.
  - Seeds mock Bangkok data on first run (coords snapped to roads via OSRM /nearest).
  - `TEMPLATES_AUTO_RELOAD=True`; runs on `0.0.0.0:5051` (LAN/phone reachable).
- **Frontend — `templates/index.html`** (~772 lines): everything in one file.
  - Leaflet 1.9.4 (unpkg) + CARTO Dark Matter tiles.
  - Nominatim for geocoding the destination box.
  - Routing via `fetchRoutes()` — OpenRouteService if `ORS_KEY` set, else OSRM demo.
  - No framework, no build step.

Key config constants near the top of the `<script>`:
- `DEFAULT_LOC` — Victory Monument fallback when GPS denied.
- `ROUTE_BUFFER_M = 400` — a checkpoint counts as "on route" within 400 m of the polyline.
- `ORS_KEY = ""` — paste an OpenRouteService token to enable reliable alternatives.

---

## 4. How to run & test

```bash
cd daan-app && python3 app.py      # http://localhost:5051
```
- Mobile view: Chrome Cmd+Opt+I → Cmd+Shift+M → iPhone. Simulate GPS in DevTools → Sensors → Location.
- Phone (same Wi-Fi): `ipconfig getifaddr en0` → open `http://<ip>:5051`. GPS is blocked over plain
  http, so phone uses the demo location; map/feed/route/report still work. Real phone GPS needs https
  (e.g. an ngrok/cloudflared tunnel).
- **If the map is empty, the seed aged out** (reports expire after 12h). `rm daan.db` and restart to reseed.

### Dev-environment gotcha (was true during original build)
The original build ran inside Claude Code where the preview server couldn't read the Desktop
folder (macOS permission). A copy was run from `/private/tmp/daan-preview/` and source changes
were `cp`'d over. **When you run it yourself from a normal terminal, this doesn't apply** — just
`python3 app.py` in place. (The `/private/tmp` copy no longer exists; the source of truth is this repo.)

---

## 0a. Product name & business model (owner's decisions, Nov 2026)

- **Name: glubDD** (logo: static/logo.png; icons: static/icon-*.png, favicon-32.png) (owner is buying the domain). Repo/Render service still called `daan-app`;
  point the new domain at Render via Settings → Custom Domains.
- **Monetization: no ads.** Free during testing. Later: **two paid packages offered side by side**
  — a pay-per-use **daily pass** and a **monthly subscription** (different prices). A permanent **free tier without the map** — static report list only
  (like the original "ด่าน" app); the map, routing and route counts are the paid features.
  Not built yet — no auth/payments exist. Gate should be server-enforced when added.

## 0b. Data storage (production)

- `db.py`: Postgres when `DATABASE_URL` is set (psycopg 3), else local SQLite. Same SQL for both
  (`?` placeholders rewritten for Postgres). Schema changes = append to `MIGRATIONS` (tracked in
  `schema_migrations`); never edit a shipped migration.
- Demo reports (`source='demo'`) are refreshed on every startup while `SEED_DEMO=1` (default on for SQLite, off for Postgres); turning it off removes them. Real reports never merge into demo rows.
- Anonymous **device id** (random UUID in localStorage, sent as `X-Device-Id`): stored on reports
  (`reporter_device`) and votes; one vote per device per report (`votes` table, unique index).
- **Prepared for accounts/payments (not built):** nullable `reporter_user_id` / `votes.user_id`.
  Planned next tables: `users` (id, phone/LINE id, created_at), `devices` (device_id → user_id),
  `plans` (daily, monthly, price), `subscriptions` (user_id, plan, starts_at, ends_at, provider ref),
  `payments` (provider, amount, status, raw webhook). Gate map/routing server-side on an active
  subscription. Likely providers: Omise or Stripe (PromptPay + cards); LINE Login or phone OTP.

## 0. Changes since this handoff was written (Nov 2026)

- **Deployed on Render** (`render.yaml`, gunicorn). Free plan: DB resets on restart.
- **Basemap:** CARTO now requires an API key off-localhost → switched to **OpenFreeMap** vector
  tiles via MapLibre (`@maplibre/maplibre-gl-leaflet`), recoloured to a clean Apple-style dark palette.
- **TH/EN language switch** (header button, saved in localStorage). Report types stay stored in Thai;
  English is display-only (`TYPES` map). Map labels switch too (`name:th` / `name:en`).
- **`official` source removed** (owner's call — no real public checkpoint feed exists, so the
  user/official filter had no point). Road-condition types (traffic/rain/roadwork) removed too.
- **Routing moved server-side** (`/api/routes`, key in `ORS_KEY` env var). Returns ORS options with
  tolls allowed + tolls avoided, labeled Tollway/No tollway, **sorted by travel time only**. Owner asked
  for a "no checkpoints" route option — declined per §7; the toll/no-toll split was the agreed alternative.
- **TomTom** is now the primary routing + search provider (`TOMTOM_KEY`): live traffic, `maxAlternatives=2`,
  `avoid=tollRoads` for the no-toll set, `sectionType=tollRoad` to label tolls. ORS → OSRM fallbacks remain.
- **Search** is Google-Maps-style autocomplete (`/api/search`): TomTom fuzzy search → Photon → Nominatim
  (Nominatim only on explicit submit, per its no-autocomplete policy).
- **Route map always shows every checkpoint**; ones on the selected route are drawn larger.
- **Route view has an explicit ✕ close**; the floating tab bar hides while a route is open.
- **Report anti-spam:** live in-app camera photo (getUserMedia, no file input), live GPS ≤ 500 m from pin,
  5 reports / 10 min per client, and an **on-device MobileCLIP-S0 photo check** (owner wanted zero
  operating cost — a paid Claude vision check was built then removed). Labels/embeddings in
  `static/clip_labels.json` (regenerate offline with transformers.js `CLIPTextModelWithProjection`,
  dtype fp16 — the q8 MobileCLIP weights give garbage). Tuned on Wikimedia sample photos:
  checkpoints ≥ 0.36, others ≤ 0.25, threshold 0.30; fails open if the model can't load.
  OCR was considered and rejected — checkpoints rarely have readable text.
- make Google Maps follow the **exact** selected route 
- **Toll filter** (All / Tollway / No tollway) on the route list — filters the list and map; routes keep
  travel-time order. Checkpoint-count sorting and hidden-waypoint route transfer were requested but not
  built by Claude (declined); nav buttons currently pass the destination only.
- **Road incidents** added (owner repositioning toward avoiding ad-hoc disruptions): types อุบัติเหตุ /
  ทำถนน/ก่อสร้าง / น้ำท่วม / ถนนถูกปิด/สิ่งกีดขวาง in the same table; category comes from `TYPES[...].cat`
  in index.html. Yellow diamond pins, All/Checkpoints/Incidents filter on Map + List, separate
  checkpoint and incident counts per route, category switch in the report sheet.
- Photo check now has label groups per type (`static/clip_labels.json`: labels, groups, embeds) —
  pass rule `1-P(neg) >= 0.5 && P(type) >= 0.15`, auto-detect type at `P >= 0.6`.
- **Google Maps handoff adds the selected route's CHECKPOINTS as stops** (`waypoints=`, driving order,
  max 3 = mobile URL limit). Incidents are never sent as stops; a route with 0 checkpoints sends the
  destination only. Apple Maps URLs can't carry stops → destination only. Agreed compromise after
  exact-route transfer was declined: stops can only make navigation pass checkpoints, not avoid them.
- **Per-report Share / Google Maps / Apple Maps** buttons (popup + list card). Share uses the native share
  sheet (fallback: copy) with a deep link `/?r=<id>` that opens the app focused on that report.
- **Report sheet is a 3-step accordion: 1 type → 2 location → 3 photo** (owner's order; `STEPS` array in
  index.html). One step open at a time, ✓ summaries on done steps, tap a header to go back, fixed
  Back/Next/Send bar. Camera only runs on the photo step; Send enables once the photo check passes.
- **Report types are stable codes** (`general`, `alcohol`, `license_helmet`, … `flood`, `blocked`); TH/EN
  names in `TYPES` (index.html). Old DBs that stored Thai names are migrated on startup (`LEGACY_TYPES`).
  Thai labels revised to police/news usage (ตรวจวัดแอลกอฮอล์ instead of slang เช็คลมปาก; หมวกกันน็อก).
- **UI kit: Liquid Glass** (CSS tokens at the top of index.html): "controls" glass for floating controls,
  "standard" glass for sheets/popups, tinted glass for primary actions, one segmented-control style,
  floating header pills + tab bar, inset sheets sized to content. Content cards stay on solid surfaces.
  Respects prefers-reduced-transparency. Wrapping rules: 2-column chip grid, `text-wrap: balance/pretty`,
  phrases wrapped in `.nw` so lines only break between phrases.
- **UI now modelled on JodNai (jodnai.com), owner's reference** — replaces the Liquid Glass pass:
  navy MapLibre palette, dark pill map pins (status dot + short type name), ONE persistent bottom
  panel (`#panel`, mini/peek/full heights, drag the grabber) with three views: nearby list (`pv-near`,
  merges the old Map + List tabs; panel has TWO states: collapsed (default, height measured per view by
  `updateCollapsed()`) and full), report detail (`pv-detail`, opened by tapping a pin or row —
  replaces Leaflet popups), route options (`pv-route`). Tab bar lives inside the panel:
  แถวนี้ · เส้นทาง · รายงาน (report opens the stepper sheet). Solid dark surfaces, blue primary CTA,
  outlined secondary, green info boxes; glass only on map-floating controls. Font: Google Sans (has Thai).
- **Legend + category encoding** (owner wanted both back after the JodNai redesign): pin STYLE = category
  (🛡 shield = checkpoint; yellow border + ⚠ = incident), dot COLOUR = recency (red <1h, orange 1–3h,
  grey >3h, stale pins faded). Legend card on both maps; list rows show a category tag + recency dot.
- Legend now lives in the panel: recency dots beside the title, category icons inside the filter segments
  (filter = category legend), one-line legend in the route panel. Language button moved to the brand pill.
- **Foldables / tablets (≥700px wide):** two-pane layout — panel becomes a full-height left pane
  (`min(420px, 42vw)`), map on the right, report sheet as centred dialog; map focus/fitBounds offset by
  the pane width (`isWide()`). Below zoom 13 pins collapse to dots (`COMPACT_BELOW_ZOOM`) to avoid pile-ups.
- **Drag from content** (`contentDrag()`): at scrollTop 0 a downward drag moves the sheet (panel shrinks,
  report sheet closes); a non-full panel expands on upward drag. Skips maps, camera, inputs, segmented
  controls, sideways swipes; disabled in the wide two-pane layout.
- **Route start point is editable** (Google-Maps-style A/B box with swap): `tripOrig` null = live
  location, otherwise a searched place or dropped pin; map tap fills whichever field is being edited.
  Nav handoff passes `origin=` (Google) / `saddr=` (Apple) when a custom start is set.
- **Collapsed panel peeks half of the next row** (Nearby: first report; Route: route 1 + half of route 2)
  so users discover the panel pulls up (user feedback).
- **Photo step is a toggle:** env `REPORT_PHOTO=1` turns it on; off by default (owner's choice for now).
  When off, reports are type + location; the CLIP model isn't loaded.
- Default category filter is **checkpoints only** (all views). Route tab shows a tap/search prompt
  (pulsing trip box + map ripple) until a destination is set.
- Report sheet: category switch styled exactly like the filter; location step expands the sheet to
  full height; "my location" button on the pick map; pin must be within **1 km** of the reporter
  (client blocks Send, server enforces `REPORT_MAX_DIST_M = 1000`).
- **Grouping:** a new report within **500 m** of an active report of the same CATEGORY (checkpoint vs
  incident, any subtype, any user) merges into the nearest one as a confirmation (ยืนยัน +1, reports +1);
  a device confirms a given report at most once. Demo rows never absorb real reports.
- Toll icon = Material "toll" symbol (as in Google Maps); toll filter shows icon + text, cards icon only.
- Report map shows a blue "you are here" dot separate from the red pin.
- **Checkpoint subtypes reduced to 5** (picker order): ทั่วไป (full-width), มอไซ, แอลกอฮอล์, รถบรรทุก, ควันดำ.
  `license_helmet`, `speed`, `tax_insurance` are retired: still displayed for old rows, rejected
  (→ general) for new reports (`RETIRED_TYPES`).
- Route list: the selected route's reports appear right under its card (icon, subtype, place, km, age),
  each tappable to open the full report detail; the old bottom "reports on route" section was removed.
- **Direction / road side:** reports store `heading` (deg, null = unknown). Default = reporter's GPS
  direction of travel ("ฝั่งเดียวกับฉัน"); "ฝั่งตรงข้าม" flips it 180°; stationary users can tap an arrow
  (45° steps). Merge: same category AND (both directions known → ≤500 m and ≤60° apart; else ≤150 m).
  Directed pins snap to the matching carriageway via OSRM /nearest with bearing (≤40 m). Detail has an
  "อยู่อีกฝั่งถนน" fix (`POST /api/checkpoints/<id>/flip`, re-merges). Route counts skip reports whose
  direction is >90° off the route's bearing. Direction is shown Google-Maps style (blue dot + light-blue beam).
- **Analytics (first-party):** `events` table (kind visit|report, anonymous device id, standalone flag,
  time). Dashboard `/stats?token=$STATS_TOKEN` (daily + monthly: visits, unique visitors, visits from
  the home-screen shortcut, reports incl. merged, unique reporters; Bangkok days); JSON `/api/stats`.
- **PWA:** `/manifest.webmanifest` + minimal `/sw.js` (no caching) make it installable. Browser visits
  get an "add to home screen" snackbar for ~7 s (Android: native Install button; iOS: Share → Add to
  Home Screen hint); never shown when opened from the shortcut.
- **Analytics dashboard** `/stats?token=…`: KPIs (unique/new visitors, DAU/WAU/MAU, stickiness, reports,
  reporters, contribution & merge rates, route planners, nav hand-offs, shortcut share), trends
  (day/week/month) with previous-period comparison, KPI strip with deltas + sparklines (click = main
  chart metric), click a period to cross-filter funnels/sources/types/devices, feature usage, heatmap, report types,
  report map, sources (utm_source / shared links / referrer), devices, language, weekly retention.
  Client events via `track(kind, meta)` → `/api/event` (whitelist `EVENT_KINDS`); `events.meta` JSON.
- **Light / dark mode:** toggle in the logo pill; dark default; saved in localStorage `gd-theme`
  (shared with /stats). Colours are tokens on `:root` with a `[data-theme="light"]` override; map uses
  `PALETTE_LIGHT`. Logo pill and map pins stay dark in both themes.
- Note: all reports share the 3 h active / 12 h hide expiry — long roadworks need re-confirming.

---

## 5. Data model — where checkpoint data comes from (important)

Each `checkpoints` row has a `source`:

| source | meaning | votable? | example feeds |
|---|---|---|---|
| `user` | **the checkpoints** — crowdsourced in-app | yes | in-app reports |
| `official` | **road conditions only, NOT checkpoints** | no | iTIC/Longdo (traffic), กรมทางหลวง (roadwork), กรมอุตุ (rain) |

- **There is NO reliable official feed of police checkpoints in Thailand** — they're announced only
  sporadically (festival crackdowns). So checkpoints are, and will stay, **user-crowdsourced**.
- Official open data only ever feeds the *context* categories (traffic / roadwork / rain), badged
  as a separate source. User reports never merge into official rows.
- A `partner` source (a cooperating app's shared feed) was prototyped then **removed** at the
  owner's request. If revisited, it must be a consensual, opt-in API exchange — **NOT scraping**
  another app (ToS + Thai Computer Crime Act exposure; discussed and advised against).
- All current data is **mock/seed**. Easiest real feed to wire first: **TMD rain** (public, keyless).

---

## 6. Routing & navigation

### Routing (previewing route options in-app)
- `fetchRoutes(o, dest)` returns routes **in the engine's order (by travel time)**, each then
  annotated with the checkpoints that fall within `ROUTE_BUFFER_M`.
- **OSRM demo** (default, no key): accurate geometry, but usually returns only **1** route, no live
  traffic. Verified keyless alternatives don't work (OSRM `exclude` unsupported; Valhalla public
  has alternates disabled).
- **OpenRouteService (chosen upgrade):** paste a free token into `ORS_KEY`. Uses
  `POST api.openrouteservice.org/v2/directions/driving-car/geojson` with
  `alternative_routes:{target_count:3, share_factor:0.6, weight_factor:1.6}` → reliable 2–3 distinct
  routes. **This is the decided next step** (chosen over Mapbox: ORS's alternative_routes is tunable
  to force distinct routes, no credit card to sign up, already wired). Free tier ~2000 req/day.
  - Also supports `options.avoid_features:["tollways"]` → a future tollway/no-tollway toggle (OK:
    road-class choice, not checkpoint-based).
  - **Production note:** the ORS key currently sits client-side (visible in page JS). Fine for a
    prototype; for production proxy ORS calls through the Flask backend so the key stays server-side.

### Navigation (actually driving) — owner's decision: hand off, no in-app nav
- Route sheet has **Google Maps** + **Apple Maps** buttons, **destination-only** (no origin) so the
  nav app starts from live GPS.
- **Deep links cannot carry a full route path** — the nav app recomputes its own route. So the
  previewed/selected route is **NOT followed exactly**; only the destination transfers.
- In-app turn-by-turn (to follow the exact route) would require a native app + nav SDK (Mapbox Nav
  SDK) — in scope
- Consequence to remember: **your checkpoints are not visible once inside Google/Apple Maps.** The
  app's value is the *pre-drive* picture. This is by design.

---

## 8. Known limitations / open items

- **Single route on OSRM** → wire `ORS_KEY` for reliable 2–3 alternatives (decided; needs a token).
- **Tollway/no-tollway toggle** — easy once on ORS (`avoid_features:["tollways"]`); not built.
- **All data is mock.** Real `official` feed to add first: TMD rain (keyless). Traffic: Longdo/iTIC
  (key/partnership). Checkpoints stay user-sourced.
- **ORS key client-side** — proxy through Flask for production.
- **PWA caveat:** reliable background location + push (for "checkpoint 1 km ahead" alerts while
  driving) don't work well in web/PWA, esp. iOS Safari. Pre-drive use is fine on web; live driving
  alerts would need native.
- **No auth / no persistence beyond local SQLite / no abuse controls** — it's a prototype.
- **Notifications** promised on the splash ("แจ้งเตือน") are not implemented (follow a province/route → push).

---

## 9. Migration notes (what did NOT transfer to the new account)

- This repo (code + this doc) is the migration vehicle — it's complete.
- **Did not transfer:** the original Claude session transcript, and Claude's local *memory* files
  (they live per-account at `~/.claude/projects/.../memory/`). The essential content of the daan-app
  memory note has been folded into this HANDOFF. Nothing else about daan is needed.
- **No published claude.ai Artifacts** exist for this project — the only deliverables are the code
  files in this repo (the "latest versions"). Early design mockups were ephemeral inline widgets.
- To continue in the new account: `git clone` this repo, open it in Claude Code / your session,
  point Claude at `HANDOFF.md`, and go. First likely task: get a free ORS token and set `ORS_KEY`.
