# The Walk In — Allegiant Stadium Parking Route Analysis

Turns Azira Pinnacle mobile pathing data into a clean map of how people
actually walk from event parking lots to Allegiant Stadium (and back), with
a local-vs-visitor split and a lot-by-lot / aggregated picker.

```
stadium-pathing/
├── pathing_pipeline.py     # cleans + aggregates raw Azira exports
├── make_sample_data.py     # generates synthetic test data for a dry run
├── index.html              # the scrollytelling dashboard
├── parking_lots.geojson    # real parking-lot boundary polygons
├── data/                   # Azira exports live here
│   ├── pathing.tsv.gz
│   ├── pin_report.tsv.gz
│   └── visitor_home.tsv.gz
└── output/                 # pipeline writes JSON here; index.html reads it
```

## 1. Data sources

Built against three Azira Pinnacle reports, all tab-separated and gzipped:

| File | Azira report | What it gives us |
|---|---|---|
| `pathing.tsv.gz` | Pathing X (with Context) | Every ping for every device, with a signed **Time Before** value and, when present, a real **Unix timestamp** per ping |
| `pin_report.tsv.gz` *(optional)* | Pin Report | Independent confirmation of a device's stadium visit, with its own timestamp and lat/lon |
| `visitor_home.tsv.gz` | Visitors Home Report | Each device's **Common Evening Location (CEL)** — Azira's home-location proxy, used for local vs. visitor |

Column headers are read by name, not position, via `DEFAULT_COLUMN_MAP` at
the top of `pathing_pipeline.py`. The current defaults match this deployment's
real export headers:

```python
DEFAULT_COLUMN_MAP = {
    "pathing": {
        "device_id": "Hashed Device ID",
        "polygon_name": "Polygon Name",
        "time_before": "Time before appearance in polygon",
        "lat": "Lat of Observation Point",
        "lon": "Lon of Observation Point",
    },
    "pathing_optional": {
        "timestamp": "Unix Timestamp of Observation Point",
        "study_polygon": "Study Polygon",
        "category": "Category",
    },
    "pin": {
        "device_id": "Hashed Device ID",
        "timestamp": "Unix Timestamp of Visit",
        "lat": "Lat of Visit",
        "lon": "Lon of Visit",
    },
    "pin_optional": {
        "location_name": "location_name",
    },
    "home": {
        "device_id": "Hashed Ubermedia Id",
        "cel_lat": "Common Evening Lat",
        "cel_lon": "Common Evening Long",
    },
}
```

The Pin report has no `location_name` column because it's generated for a
single study location — every row in it is a candidate stadium visit,
subject to the spatial check described below. If a future export changes
any header names, the script fails loudly with the actual column list, so
this mapping can be updated in one place. `--column-map path.json` overrides
it without touching the script.

## 2. Timing: real timestamps first, signed offset as fallback

When the pathing export includes `Unix Timestamp of Observation Point`, the
pipeline uses it directly: it takes each device's confirmed stadium-visit
timestamp and computes actual elapsed seconds against each lot ping's own
timestamp. If that column is ever missing from an export, it falls back to
treating `Time before appearance in polygon` as already relative to the
stadium visit (positive = before, 0 = at the visit, negative = after), and
logs a `WARNING` when it does — that path is a weaker guarantee and worth a
manual check against a real device trail if it's ever the one in use.

## 3. Real parking-lot boundaries, spatially matched

Azira's `Polygon Name` field comes from its general POI context library — it
tags a ping with whatever nearby business polygon matched, which is not the
same as "inside one of our actual event parking lots." The pipeline instead
tests every pathing ping and every Pin report row against the exact lot
boundaries in `parking_lots.geojson` (standard `FeatureCollection` of
`Polygon`/`MultiPolygon` features, each with a `name` property), using a
vectorized point-in-polygon check. A ping only counts as being in a lot if
its coordinates genuinely fall inside one of the mapped shapes. The same
check is used for the stadium footprint itself, since the Pin report covers
every building in the wider stadium district, not just the stadium.

If `parking_lots.geojson` isn't found at runtime, the pipeline falls back to
matching lots by `Polygon Name` text instead (logged as a `WARNING`) — useful
for a quick look, but expect POI noise (gas stations, restaurants, etc.)
mixed in with real lots when that fallback is active.

## 4. Noise removal

A device is kept only if, within `--max-post-minutes` (default 60):

1. It has at least one ping inside a selected parking-lot polygon, and
2. It has a confirmed stadium visit — from the spatially-filtered Pin
   report if available, otherwise from the pathing file's own
   stadium-polygon rows, and
3. The elapsed time between the lot ping and the stadium visit is within
   the window.

Two additional filters run before that check:

- **High-emittance devices** (`HIGH_EMITTANCE_PING_CAP`, default 400 pings):
  a small number of devices generating an implausible number of pings are
  dropped entirely.
- **Employee/staff devices** (`EMPLOYEE_DWELL_HOURS`, default 4h): approximates
  Azira's documented employee filter using the spread of `time_before`
  values for a device inside a lot polygon within this single export. If a
  Dwell Time report for the same lots is available, it's a more accurate
  source for this filter and can replace the approximation in
  `filter_employee_devices()`.

Lots with fewer than 38 qualifying devices are flagged `low` confidence in
the summary table, mirroring Azira's own default minimum-device threshold
for reliable aggregate shapes.

## 5. Local vs. visitor

Classification comes from each device's Common Evening Location. A device is:

- **local** — CEL falls within 45 miles of downtown Las Vegas
  (`LAS_VEGAS_METRO_CENTER`, `LAS_VEGAS_METRO_RADIUS_MILES`)
- **visitor** — CEL falls outside that radius
- **unknown** — no CEL on file

The radius is a simple circle standing in for the Las Vegas–Henderson–Paradise
CBSA boundary. Swapping in the real CBSA polygon (or a ZIP/county list) for
a proper point-in-polygon test in `classify_local_visitor()` would improve
accuracy at the edges.

## 6. Running it

```bash
pip install pandas numpy --break-system-packages   # or use a venv
```

From the folder containing `pathing.tsv.gz`, `pin_report.tsv.gz`,
`visitor_home.tsv.gz`, and `parking_lots.geojson`:

```bash
python pathing_pipeline.py --out output/
```

No flags are required — those four filenames are the defaults. To point at
different files, restrict to specific lots, or change the time window:

```bash
python pathing_pipeline.py \
  --pathing data/pathing.tsv.gz \
  --pin data/pin_report.tsv.gz \
  --home data/visitor_home.tsv.gz \
  --lots-geojson parking_lots.geojson \
  --stadium-name "Allegiant Stadium" \
  --lots "Lot A" "Lot B" "Lot C" \
  --max-post-minutes 60 \
  --event-name "Raiders vs. Chiefs (2026-09-14)" \
  --event-type nfl_home_game \
  --event-date 2026-09-14 \
  --out output/
```

`--event-name` / `--event-type` / `--event-date` are optional but recommended
whenever you process a new game or event — they get written to
`output/event_meta.json` and, once bundled into that run's
`dashboard_data.json` (see §7), let the dashboard's event-comparison file
picker label the overlay correctly instead of just showing the filename.

To try the pipeline without real data first:

```bash
python make_sample_data.py
python pathing_pipeline.py --out output/
```

This writes five files to `output/`:

| File | Contents |
|---|---|
| `routes.json` | One record per qualifying trip: device, lot, direction (`to_stadium` / `from_stadium`), elapsed minutes, ordered lat/lon path, residency |
| `flow_lines.json` | Routes clustered into representative, weighted paths per lot + direction — what the map draws |
| `lot_summary.json` | Per-lot aggregate stats: device count, median times, local/visitor split, confidence flag |
| `lot_centroids.json` | Lat/lon for each lot (from the real polygon geometry) and the stadium, for map markers |
| `pipeline_stats.json` | Funnel counts and notes on what was dropped and why |
| `event_meta.json` | `--event-name` / `--event-type` / `--event-date`, for multi-event comparison |

## 7. Rebuilding the dashboard

`index.html` reads its data from two embedded placeholders in
`index_template.html`. To refresh it after a new pipeline run:

```bash
python3 - <<'EOF'
import json
data = {
    "flow_lines": json.load(open("output/flow_lines.json")),
    "lot_centroids": json.load(open("output/lot_centroids.json")),
    "lot_summary": json.load(open("output/lot_summary.json")),
    "routes": json.load(open("output/routes.json")),
}
event_meta = json.load(open("output/event_meta.json"))
data["event_meta"] = event_meta  # so a saved dashboard_data.json can be
                                  # loaded into another run's comparison picker
html = open("index_template.html").read()
html = html.replace("__DASHBOARD_DATA__", json.dumps(data))
html = html.replace("__EVENT_META__", json.dumps(event_meta))
open("index.html", "w").write(html)

# Also save a standalone bundle -- this is the file to hand to the
# "Compare against another event" picker in a *different* game's dashboard.
json.dump(data, open("output/dashboard_data.json", "w"))
EOF
```

Open `index.html` directly in a browser — no server required.

## 8. The interface

- **Narrative rail** (left, scrolls): a short guided intro that highlights
  different lot/residency views as you scroll. Clicking a highlighted word,
  or touching any control in the side panel, switches to manual mode — the
  guided tour stops overriding your selection, so nothing resets as you
  scroll back up.
- **Side panel** (right, sticky): map, lot picker, residency toggle, and
  charts all live in one fixed frame for the entire scroll, so filters and
  the map are always visible together.
- **Street snapping** (map toggle): best-effort routing of the drawn paths
  onto real streets/sidewalks via OSRM's public routing API, called directly
  from the browser. Off by default; toggle on to try it. Falls back silently
  to the raw GPS-derived path per route if the service is unavailable or
  returns no match — a status line under the toggle reports how many routes
  snapped successfully.
- **Charts, not just a table**: devices-per-lot and local/visitor-split bar
  charts update live with your filters; a time-to-stadium histogram (5 or 10
  minute bins) shows the distribution of walk times for the current
  selection.
- **Event comparison**: drop another event's `dashboard_data.json` (produced
  by the same pipeline, ideally with `--event-name` / `--event-type` set) into
  the file picker to overlay its time-to-stadium distribution on the
  histogram — e.g. comparing a Raiders home game against a concert once both
  have been processed.

## 9. Known limitations

- **Street snapping is a demo-tier convenience, not a production routing
  layer.** It calls OSRM's free public API directly from the visitor's
  browser (`router.project-osrm.org`) with no authentication and no SLA —
  expect occasional timeouts or rate-limiting, especially with many lots
  selected at once. Results are not cached beyond the current browser
  session. For a public-facing or high-traffic deployment, replace this
  with a self-hosted OSRM instance or a paid map-matching API (e.g. Mapbox),
  and precompute/cache the matched geometry server-side rather than calling
  it live per page load.
- **Event comparison depends on consistent pipeline runs.** The histogram
  overlay only works well if every event was processed with the same lot
  boundaries and the same `--max-post-minutes` window; comparing a 60-minute
  window against a 30-minute window will look like a real behavioral
  difference when it's actually a methodology difference.
- **Route geometry is grid-snapped, not map-matched.** `flow_lines.json`
  groups routes whose points fall in the same coarse cell (~90m,
  `snap_route_to_grid`) and shows one representative path per cluster,
  weighted by device count — not routed to actual sidewalks/roads. For
  publication-grade paths, run the qualifying routes through a map-matching
  service (e.g. OSRM).
- **Employee filtering is an approximation** from a single pathing export;
  replace with a real Dwell Time report if staff contamination matters.
- **CEL-based local/visitor is a proxy**, not a verified home address.
  Devices with thin location history come back `unknown`.
- **`--max-post-minutes` is a hard cutoff.** A device just over the line is
  excluded entirely rather than down-weighted.
