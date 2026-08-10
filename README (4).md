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
  --out output/
```

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

## 7. Rebuilding the dashboard

`index.html` reads its data from a single embedded JSON blob. To refresh it
after a new pipeline run:

```bash
python3 - <<'EOF'
import json
data = {
    "flow_lines": json.load(open("output/flow_lines.json")),
    "lot_centroids": json.load(open("output/lot_centroids.json")),
    "lot_summary": json.load(open("output/lot_summary.json")),
    "routes": json.load(open("output/routes.json")),
}
html = open("index_template.html").read()
open("index.html", "w").write(html.replace("__DASHBOARD_DATA__", json.dumps(data)))
EOF
```

Open `index.html` directly in a browser — no server required.

## 8. The interface

- **Scrollytelling rail** (left): narrative steps that drive the map as the
  reader scrolls. Lot names and "local"/"visitor" appear as colored inline
  spans; clicking one jumps the map to that filter immediately.
- **Sticky map** (right): Leaflet, dark basemap, one colored dot per lot,
  route lines colored by direction (amber = walking in, red = walking out),
  line weight scaled by device count.
- **Control panel**: lot chips for multi-select aggregation, a
  local/visitor/all toggle, and a sortable summary table for open-ended
  exploration.

## 9. Known limitations

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
