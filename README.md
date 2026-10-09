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

## 3. Scope: the stadium district bounding box

Every pathing and Pin report row is filtered, immediately on load, to a
fixed geographic box covering the stadium district — lots, rideshare drops,
the stadium itself, with margin:

```python
DEFAULT_DISTRICT_BBOX = (-115.2006395781, 36.0652499586, -115.1803492012, 36.1026977365)
# (min_lon, min_lat, max_lon, max_lat) -- roughly 1.1 x 2.6 miles
```

Anything outside it — including any GPS-noise outlier pings, which real
mobile location data reliably contains a handful of (spoofed GPS, bad
fixes, sometimes thousands of miles off) — is dropped before any other
processing runs. This is also the exact download extent used for the OSM
street network when `--street-match` is on, so the network download can't
blow up trying to cover a huge area because of a single bad ping (an
earlier version derived the download box from route pings directly, which
failed exactly that way).

Override with `--district-bbox MIN_LON MIN_LAT MAX_LON MAX_LAT` if the
district boundary ever needs to move or expand.

## 4. Real parking-lot boundaries, spatially matched

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

## 5. Noise removal

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
- **Repeat-visit staff devices** (`--staff-min-distinct-dates`, default 5):
  devices pinging inside the stadium footprint on 5+ distinct calendar
  dates are flagged as likely stadium staff and excluded before trip
  qualification. Validated against the LV Stadium Authority's own
  regulatory filing: an earlier district-wide version of this check
  flagged 16,840 devices — 2.9x the real 5,785-person workforce, clearly
  catching more than just staff (hotels, other nearby businesses, regular
  neighborhood traffic). Restricting specifically to pings inside the
  stadium footprint brought that to 282, a plausible 0.05x of the known
  workforce, with a confirmed 27.8-point local-vs-baseline skew among
  flagged devices. This replaces the old dwell-time-based employee filter
  below as the primary staff-detection method, since that filter has
  returned zero devices dropped on every real run — it was built for a
  single-event dwell-time signal that doesn't transfer to a year-spanning
  multi-event dataset. Run `find_likely_staff.py` for the full standalone
  diagnostic (per-scope breakdowns, distinct-date distributions, CSVs of
  every flagged device) this filter is based on.
- **Employee/staff devices** (`EMPLOYEE_DWELL_HOURS`, default 4h, legacy):
  approximates Azira's documented employee filter using the spread of
  `time_before` values for a device inside a lot polygon within a single
  visit. Kept as a secondary check after the repeat-visit filter above, in
  case it catches something that method misses (e.g. a very long single-day
  dwell), but has not fired on any real run so far. If a Dwell Time report
  for the same lots is available, it's a more accurate source for this
  filter and can replace the approximation in `filter_employee_devices()`.

A third filter (`--max-ping-radius-miles`, default 50) runs after routes are
built: any individual GPS ping more than 50 miles from the stadium is
dropped as noise. Real mobile location data includes a small number of
wildly wrong pings — spoofed GPS, bad fixes — and a single one thousands of
miles away is enough to wreck the street-network download's bounding box
(which takes a min/max over every route's pings) if left unfiltered. A
device's route survives with the bad ping removed as long as at least 2
valid points remain; if not, the whole route is dropped.

Lots with fewer than 38 qualifying devices are flagged `low` confidence in
the summary table, mirroring Azira's own default minimum-device threshold
for reliable aggregate shapes.

## 6. Extrapolating to actual attendance

`OFFICIAL_EVENT_CALENDAR` in the pipeline bakes in real attendance figures
for 38 known 2025 Allegiant Stadium events, sourced directly from the LV
Stadium Authority's own quarterly regulatory filings (Section 7.4 of the
Stadium Lease Agreement, audited by KPMG) — not scraped or estimated.

For every date in a run's output that also appears in that calendar, the
pipeline computes a **sampling rate**: our confirmed unique device count for
that date, divided by the event's official attendance. Across all matched
dates, it computes a **pooled rate** (total our-devices / total official
attendance — weighting busier events more heavily, which is the
statistically appropriate way to combine a ratio across events of very
different sizes) and uses that as a single extrapolation multiplier.

That multiplier gets applied to every row of `lot_summary.json` as
`estimated_actual_devices` — "65 confirmed devices at Lot A" becomes "an
estimated ~110,000 actual people," on the scale of real attendance rather
than raw sample size. The full calibration — pooled rate, the simple mean
and standard deviation across matched dates, and the per-date table — is
also written to `extrapolation_calibration.json`.

**Read the caveat in that file before trusting a specific number.** The
sampling rate is not perfectly constant — in this project's own Q1–Q3 vs.
Q4 2025 comparison, it roughly doubled between periods. Treat any single
extrapolated figure as an order-of-magnitude estimate, and if you want
tighter estimates, consider computing separate multipliers per quarter or
per event type rather than trusting one flat number for the whole year.

## 7. Local vs. visitor

Classification comes from each device's Common Evening Location. A device is:

- **local** — CEL falls within 45 miles of downtown Las Vegas
  (`LAS_VEGAS_METRO_CENTER`, `LAS_VEGAS_METRO_RADIUS_MILES`)
- **visitor** — CEL falls outside that radius
- **unknown** — no CEL on file

The radius is a simple circle standing in for the Las Vegas–Henderson–Paradise
CBSA boundary. Swapping in the real CBSA polygon (or a ZIP/county list) for
a proper point-in-polygon test in `classify_local_visitor()` would improve
accuracy at the edges.

## 8. Street-segment matching (optional, `--street-match`)

By default, routes are drawn as straight-line interpolations between GPS
pings. Passing `--street-match` instead snaps every route onto real
OpenStreetMap street-centerline geometry, so the dashboard can show genuine
per-street device counts (graduated color), a local-vs-visitor divergence
view, and a ranked list of the busiest points where foot traffic enters the
mapped street network.

**This requires extra dependencies and network access:**

```bash
pip install osmnx networkx shapely pyproj --break-system-packages
```

```bash
python pathing_pipeline.py --street-match --out output/
```

On first run for a given area, this downloads the walking street network
from OpenStreetMap's Overpass API and caches it to `street_graph.graphml`
(default path, override with `--street-graph-cache`) so repeat runs over
the same area are instant.

**If the download times out** (a *connection* timeout — not a slow
response — almost always means a firewall or proxy is blocking that
specific host, common on corporate networks): the pipeline automatically
tries four public Overpass mirrors in turn before giving up, so a block on
the default `overpass-api.de` alone usually isn't fatal. If all four are
unreachable, or you'd rather not rely on that automatic retry, two
alternatives:

- **`--overpass-endpoint URL`** — use one specific mirror you know works
  (e.g. from a different network, or one your IT team has allow-listed).
- **`--street-osm-file path/to/file.osm`** — skip Overpass entirely by
  loading a manually downloaded OSM extract. Get one by pasting the
  district bbox from §3 into [Overpass Turbo](https://overpass-turbo.eu)
  and exporting as `.osm`, or by cropping a regional extract from
  [Geofabrik](https://download.geofabrik.de) (Nevada) to the district
  bbox with a tool like `osmium extract`. This is the reliable option if
  the network this pipeline runs on genuinely can't reach any Overpass
  mirror at all — download the file on a different network if needed, then
  transfer it over.

The download extent is the fixed `--district-bbox` from §3, not something
derived from route pings — an earlier version computed the box dynamically
from every route's GPS points, which broke badly the one time a stray
GPS-noise ping several thousand miles away slipped through and made the
"bounding box" try to cover most of a continent. The fixed district box
avoids that failure mode entirely.

**One parameter matters for accuracy, tuned for right-of-way areas where
pings legitimately fall well off the nearest mapped centerline** (parking-lot
interiors, plazas, medians):

- `--street-max-snap-distance-m` (default `60`): a ping farther than this
  from the nearest OSM-mapped street edge — measured in real meters via a
  proper local UTM projection, not raw lat/lon degrees — is left unmatched
  rather than force-snapped to whatever edge happens to be nearest. Without
  this cutoff, a ping standing in the middle of a parking lot would get
  silently attributed to some specific nearby street it may never have
  touched. The run log reports what percentage of pings matched within this
  distance; raise the threshold if that percentage looks too low for your
  geography, lower it if segments seem to be picking up implausible
  detours.

**What gets written:** each route in `routes.json` gets a `matched_edges`
field (its de-duplicated, in-order list of street-segment IDs), and
`edge_geometries.json` gives the lat/lon geometry for every segment any
route touched. The dashboard aggregates device-per-segment counts **live**,
filtered by whatever lots/residency are currently selected — the raw
per-route matched-edge data is shipped to the browser specifically so this
recomputes on every filter change rather than being frozen at one
pre-baked total.

If `--street-match` is never run, the dashboard falls back to the old
straight-line flow rendering automatically, with a banner noting that.

## 8b. Travel-mode classification and street-type labeling (gateway / pedestrian corridor / local)

Every qualifying trip now gets a single, consistent travel-mode label —
`"walking"`, `"biking"`, `"car"`, `"stationary"`, or `null` when there isn't
enough data — written into `routes.json` as `mode` (plus
`mode_median_speed_mps`). This is computed **once per trip**, from the
*median* speed across all of that trip's ping-to-ping segments
(`classify_route_mode` in `pathing_pipeline.py`), not by classifying each
segment independently. That distinction matters: a device genuinely
walking the whole way still produces some ping-to-ping segments that
compute as "stationary" (GPS sat still for a beat) and others that spike
toward "biking" (a noisy fix), purely from GPS jitter and uneven ping
spacing — nothing about how the person is actually moving changed. Voting
or labeling segment-by-segment bakes that flicker into the result; taking
the trip's median speed is robust to a handful of noisy segments and
guarantees one label, consistent by construction. Segments faster than
33 m/s (~74 mph — a spoofed/bad GPS fix, not a real speed on these roads)
are dropped as noise before the median is taken; segments under 2 seconds
apart are also dropped (GPS jitter at that resolution produces meaningless
instantaneous speeds). The thresholds themselves match
`classify_travel_mode.py` exactly (≤0.3 m/s stationary, ≤2.2 m/s walking,
≤7.0 m/s biking, else car) so the two scripts never disagree just because
one got tuned and not the other.

When `--street-match` is also used, this mode label is snapped onto real
street geometry and used to classify each **public** street corridor into
exactly one of three types, written to `street_classification.json`:

- **`gateway`** — the most common mode observed on this corridor is
  car/bike, AND OSM tags it as a major road (`primary`, `secondary`,
  `tertiary`, `trunk`, `motorway`, or a `_link` of one of those). A real
  vehicle entry point into the district, like Al Davis Way or Russell Road.
- **`pedestrian_corridor`** — the most common mode observed is
  walking (or stationary — e.g. a queued crowd).
- **`local_street`** — car/bike-dominant, but NOT a major road by OSM's
  hierarchy (a residential or unclassified street, or a named service
  road) — "primarily vehicle trips navigating to/from the stadium" on an
  ordinary street, exactly the third category asked for.
- **`unclassified`** — the corridor is a real public street but no trip
  with a usable mode classification happened to touch it.

**Private property is excluded before any of the above runs.** A corridor
is only eligible to be classified at all if it's a real public street —
`classify_corridor_privacy` requires it to (1) carry a real OSM street
`name` tag, (2) not be majority-tagged `service=parking_aisle` /
`driveway`, and (3) not have most of its geometry sitting inside a mapped
parking-lot polygon from `parking_lots.geojson`. Fail any of those and the
corridor gets `type: "excluded_private"` and is left out of gateway/
pedestrian/local labeling entirely — this is the Al-Davis-Way-yes,
parking-lot-pathways-no distinction, enforced structurally rather than by
a name-pattern guess. An edge with no name at all is always treated as
private-property infrastructure, even on the rare chance OSM just forgot
to tag a short public connector — in this district the unnamed edges are
overwhelmingly real parking-lot aisles and footways (the same ones
`build_edge_corridors` already gives descriptive fallback labels like
"Parking Aisle near Lot A"), so this errs toward not misclassifying
private infrastructure as a public street rather than the reverse.

Each corridor's vote tally in `mode_counts` is **per trip, not per raw
ping or per edge** — a trip that pings densely on one corridor doesn't
drown out other trips, and a trip that only clips a corner of a corridor
counts the same as one that follows it for blocks.

No extra dependencies or flags beyond `--street-match` itself are needed —
this is computed automatically whenever that flag is used, and
`street_classification.json` is written alongside the other street-match
outputs.

## 9. Running it

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
| `routes.json` | One record per qualifying trip: device, lot, direction (`to_stadium` / `from_stadium`), elapsed minutes, ordered lat/lon path, residency, and (if `--street-match` was used) `matched_edges` |
| `edge_geometries.json` | *(only with `--street-match`)* Lat/lon geometry for every street segment any route touched, keyed by the same edge IDs used in `routes.json`'s `matched_edges` |
| `flow_lines.json` | Routes clustered into representative, weighted paths per lot + direction — what the map draws |
| `lot_summary.json` | Per-lot aggregate stats: device count, median times, local/visitor split, confidence flag |
| `lot_centroids.json` | Lat/lon for each lot (from the real polygon geometry) and the stadium, for map markers |
| `pipeline_stats.json` | Funnel counts and notes on what was dropped and why |
| `event_meta.json` | `--event-name` / `--event-type` / `--event-date`, for multi-event comparison |

## 10. Rebuilding the dashboard

`index.html` reads its data from three embedded placeholders in
`index_template.html`. To refresh it after a new pipeline run:

```bash
python3 - <<'EOF'
import json, os

def load_if_exists(path, default):
    return json.load(open(path)) if os.path.exists(path) else default

data = {
    "flow_lines": json.load(open("output/flow_lines.json")),
    "lot_centroids": json.load(open("output/lot_centroids.json")),
    "lot_summary": json.load(open("output/lot_summary.json")),
    "routes": json.load(open("output/routes.json")),
}
event_meta = json.load(open("output/event_meta.json"))
data["event_meta"] = event_meta  # so a saved dashboard_data.json can be
                                  # loaded into another run's comparison picker

edge_geometries = load_if_exists("output/edge_geometries.json", {})
edge_corridors = load_if_exists("output/edge_corridors.json", {})
extrapolation_calibration = load_if_exists("output/extrapolation_calibration.json", {})
street_classification = load_if_exists("output/street_classification.json", {})

html = open("index_template.html").read()
html = html.replace("__DASHBOARD_DATA__", json.dumps(data))
html = html.replace("__EVENT_META__", json.dumps(event_meta))
html = html.replace("__EDGE_GEOMETRIES__", json.dumps(edge_geometries))
html = html.replace("__EDGE_CORRIDORS__", json.dumps(edge_corridors))
html = html.replace("__EXTRAPOLATION_CALIBRATION__", json.dumps(extrapolation_calibration))
html = html.replace("__STREET_CLASSIFICATION__", json.dumps(street_classification))
open("index.html", "w").write(html)

# Also save a standalone bundle -- this is the file to hand to the
# "Compare against another event" picker in a *different* game's dashboard.
json.dump(data, open("output/dashboard_data.json", "w"))
EOF
```

**All six placeholders must be replaced** — `index_template.html` has no
default values baked in, so any one left as a literal `__PLACEHOLDER__`
string breaks the page's JavaScript on load. If a particular output file
doesn't exist (e.g. `output/edge_geometries.json` and
`output/street_classification.json` when `--street-match` wasn't used for
this run), its placeholder is still replaced — with `{}` — rather than
left untouched; the dashboard's JS checks for an empty object and degrades
gracefully (straight-line GPS-interpolated routes instead of real street
segments, no "Street Type" map mode) with a banner explaining why, rather
than erroring.

Open `index.html` directly in a browser — no server required.

## 11. The interface

- **Narrative rail** (left, scrolls): a short guided intro that highlights
  different lot/residency views as you scroll. Clicking a highlighted word,
  or touching any control in the side panel, switches to manual mode — the
  guided tour stops overriding your selection, so nothing resets as you
  scroll back up.
- **Side panel** (right, sticky): map, lot picker, residency toggle, and
  charts all live in one fixed frame for the entire scroll, so filters and
  the map are always visible together.
- **Street segments, graduated by volume**: when the export includes
  `--street-match` data, streets are colored on a warm ramp (dim → amber →
  red) by how many devices used them, with thickness scaled the same way —
  the busiest streets are the thickest, brightest lines. A **Volume /
  Local vs. Visitor** toggle switches the same segments to a diverging
  teal-to-violet scale showing which streets skew local vs. visitor. An
  animated dash flow on every segment shows whether it's carrying more
  inbound or outbound traffic for the current filter. Without street-match
  data, the map falls back to straight-line GPS-interpolated routes
  automatically, with a banner explaining why.
- **Street Type mode** (third option in the Volume / Local vs. Visitor
  toggle): colors each real public street by its classification from
  `street_classification.json` — gateway (red-orange), pedestrian corridor
  (teal), or local street (violet) — per §8b. Parking-lot aisles,
  driveways, and footways aren't drawn in this mode at all (they're
  excluded from the classification entirely, not just hidden). A corridor
  whose type rests on too little evidence (fewer than 5 trip-votes, or a
  tie between two modes) is drawn dashed and dimmer rather than with the
  same solid confidence as a well-sampled street — hover any segment for
  its dominant mode, vote count, and confidence. A **mode filter** (All /
  🚶 Walking / 🚲 Biking / 🚗 Car) sits just below the direction filter and
  restricts every map/chart consumer to trips of that travel mode, same as
  the existing direction filter.
- **Busiest entry points**: a ranked bar chart of the street segments where
  inbound trips first enter the mapped network — click a bar to fly the map
  to that segment. Requires street-match data.
- **Charts, not just a table**: devices-per-lot and local/visitor-split bar
  charts update live with your filters; a time-to-stadium histogram (5 or 10
  minute bins) shows the distribution of walk times for the current
  selection.
- **Event comparison**: drop another event's `dashboard_data.json` (produced
  by the same pipeline, ideally with `--event-name` / `--event-type` set) into
  the file picker to overlay its time-to-stadium distribution on the
  histogram — e.g. comparing a Raiders home game against a concert once both
  have been processed.

## 12. Known limitations

- **Street matching uses nearest-edge snapping per ping, not full
  path-continuity map-matching.** Each ping is independently assigned to
  whichever street edge is closest (within `--street-max-snap-distance-m`),
  which works well for reasonably dense ping sequences but can occasionally
  jump to a parallel street if pings are sparse near an intersection or two
  streets run close together. A proper Hidden-Markov-Model map matcher
  (e.g. `leuvenmapmatching`, or a hosted service) would handle that more
  robustly at the cost of real complexity and, for hosted options, another
  external dependency.
- **The OSM street network download needs real internet access** to
  Overpass at run time (cached afterward), and is bounded by whatever
  `--street-network-buffer-miles` you set — too tight a buffer means pings
  near the edge of the analysis area have nothing to snap onto.
- **Entry points are defined narrowly**: the first street segment each
  inbound trip's matched-edge sequence touches. If a device's earliest
  pings are all unmatched (see `--street-max-snap-distance-m`), its trip
  won't contribute an entry point even though it clearly went somewhere.
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
