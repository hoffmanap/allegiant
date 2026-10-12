# The Walk In — Allegiant Stadium Parking Route Analysis

Turns Azira Pinnacle mobile pathing data into a clean map of how people
actually walk from event parking lots to Allegiant Stadium (and back), with
a local-vs-visitor split and a lot-by-lot / aggregated picker.

```
stadium-pathing/
├── pathing_pipeline.py     # cleans + aggregates raw Azira exports (stadium-anchored pathing)
├── district_wide_street_analysis.py  # standalone street-type analysis (district-wide extract)
├── retype_streets.py       # re-applies the final street-role rules to existing outputs (fast)
├── build_dashboard.py      # builds index.html from index_template.html + output folders
├── make_sample_data.py     # generates synthetic test data for a dry run
├── index_template.html     # dashboard source
├── index.html              # the scrollytelling dashboard (generated)
├── parking_lots.geojson    # real parking-lot boundary polygons
├── data/                   # Azira exports live here
│   ├── pathing.tsv.gz
│   ├── pin_report.tsv.gz
│   └── visitor_home.tsv.gz
└── output/                 # pipeline writes JSON here; index.html reads it
```

## 0. Overview: two analyses, one project

This project answers **two different questions with two different sets of
devices**. Keeping them separate is the most important thing to understand
when explaining the work.

| | **A. Pathing exercise** | **B. Street classification** |
|---|---|---|
| Question | Which streets do people use to walk from a parking lot to the stadium, and how long does it take? | Which streets are vehicle gateways, which are pedestrian corridors, which are local access streets? |
| Population | Devices seen in a mapped parking lot *and* then at the stadium | Every device seen within 30 minutes before/after visiting *any* district building (merged polygon) |
| Data | Pathing X export anchored to the stadium, plus Pin and Visitors Home reports | Pathing X export `10178831_Clark_pathing_x_report.tsv.gz` (640 dates, 902,880 device-days) |
| Time bias | Mostly **event days** (event dates were 42.4% of device-days, about 5x the activity of other days) | Event dates are about 10.5% of device-days; includes ordinary days |
| Code | `pathing_pipeline.py` | `district_wide_street_analysis.py` |
| Dashboard maps | Volume; Local vs. Visitor | Street Roles (one consolidated map) |
| Good for | Event-day walk-in routes, lot attribution, attendance estimates, local vs. visitor | Day-to-day street roles; event-day change in pedestrian use |
| Not good for | Judging how streets are used on ordinary days (it over-represents pedestrians) | Counting people or lot-level questions |

### How the thinking evolved (and why the design is what it is)

1. **First pass: type streets from the pathing trips.** Street type was
   computed from the same lot-to-stadium trips. Result: nearly every street,
   including major arterials, came out as a pedestrian corridor.
2. **Fix one: mode was applied per trip, not per street.** One median speed
   labeled a whole drive-park-walk trip "walking", and that label was applied
   to every street on it. Mode is now classified per ping (rolling median of
   segment speeds), and a street's vote comes from only the pings that
   snapped to it. Gateways went from 1 to 7.
3. **Hypothesis: the trip population is event-skewed.** Because the pathing
   extract is anchored to stadium visits, it is dominated by event days and
   pedestrians. We tested it rather than assuming it: event dates were 42.4%
   of device-days (35.9 vs. 7.2 trips/day) in that data. Hypothesis confirmed.
4. **Broader extract.** A new extract (30 minutes before/after trips to any
   building in the district) was requested and analyzed with its own script,
   so street typing no longer depends on the lot-qualified trips. Event dates
   fell to 10.5% of device-days.
5. **Scale and sparse pings.** The first full run (1.06M legs) never finished
   (an uncached shortest-path search in gap bridging); fixed with caching and a
   seeded 150k-300k leg sample. Results were stable across seeds 0 and 2.
   Pings can be seconds or many minutes apart, so speeds over gaps longer
   than 60 s are ignored.
6. **Slow cars look like bikes.** Around the Strip, congested or sparsely-pinged
   cars fall in the 2.2-7 m/s "biking" band, and arterials still lost to
   walking by a few points. For street typing, "biking" is counted as vehicle
   traffic (`--biking-as-wheeled`, an assumption). Sanity check: ACS B08301 for
   the district's block group (BG 1, Tract 29.62: 623 workers) showed 88% of
   commuters driving, 6% bus, 3% walking, 0 biking. It describes residents,
   not people who work in the district, so it supports the assumption but is
   not proof. With it, arterials (Dean Martin Dr, Tropicana Ave, Valley View
   Blvd, Russell Rd, Hacienda Ave) are reliably gateways.
7. **Re-scoped the question.** The mode split is not the headline. What matters
   for design is the role each street plays, without letting event days skew
   the answer but still accounting for them. First attempt: type non-gateway
   streets by ordinary-day pedestrian *share* (>= 50%), with an event flag.
8. **Cadence confound.** Event dates fall mostly in the fall season, and GPS
   cadence varies by month (median gap about 3 s in Jan-Apr 2025, about 40 s in
   Jun-Jul 2025, 6-21 s afterwards). Dean Martin Dr showed that the apparent
   event effect disappears when event days are compared with non-event days
   from the same season. So the event comparison is month-matched.

9. **Share was the wrong measure; use volume, backed by the walk-in routes.**
   The share rule found almost no pedestrian streets and missed obvious ones:
   Polaris Avenue carries heavy car traffic *and* heavy pedestrian traffic, so
   its pedestrian share (42%) looks like any other street. What matters for
   design is how many people walk it. The roles are now defined by design intent:
   arterials are gateways; non-arterials with heavy pedestrian *volume* are
   pedestrian streets; everything else is local. And pedestrian designation must be
   supported by the walk-in routes (Volume map, analysis A), so the two analyses
   corroborate each other instead of standing alone.

### Final design (what the dashboard and outputs mean)

- **Gateway** = arterial by OpenStreetMap class (motorway / trunk / primary /
  secondary), regardless of its mode mix. Tertiary roads are no longer gateways
  by default.
- **Pedestrian street** = non-arterial that (a) carries >= 5% of all *walking*
  lot-to-stadium trips (analysis A) **and** (b) has >= 300 walking/stationary
  observations on ordinary days in the district-wide data (analysis B).
  Absolute volume, not share.
- **Local street** = everything else; drawn dashed when it has fewer than 200
  ordinary-day observations.
- **Dual role:** an arterial that is also a major walk-in route (West Hacienda
  Avenue, 14.8% of walking trips) stays a gateway and is flagged
  `also_walkin_route`.
- **Event days = a separate flag** (`event_pedestrian_flag`), measured against
  same-month ordinary days; it never decides the role.
- **Pedestrian observations are relative signals**, from speed between GPS
  pings, not counts of people. The walk-in routes are the measured support.

### Results (district-wide run, `--biking-as-wheeled`, 300k legs, seed 0, final roles)

| Role | Count | Streets |
|---|---|---|
| Gateway (arterial) | 7 | Dean Martin Dr, W Tropicana Ave, S Valley View Blvd, W Russell Rd, W Hacienda Ave, Joey Bishop Dr, W Sunset Rd |
| Pedestrian street | 3 | **Polaris Ave** (11.7% of walking trips, 3,304 ordinary-day observations), **Al Davis Way** (16.0%, 1,082), **Allegiant Stadium Way** (14.8%, 376) |
| Local street | 30 | everything else, including the former tertiary "gateways" (Wynn Rd, Oquendo Rd, Diablo Dr, Arville St, Post Rd) |

West Hacienda Avenue is an arterial that is also a major walk-in route (14.8%).
Event-day pedestrian surges (month-matched, flag only): Allegiant Stadium Way
+20.0 pts, Al Davis Way +16.2 pts, Graphic Center Dr +10.3 pts (a local street,
weak signal).

### Caveats to state when presenting this

- Mode is inferred from speed between pings; this cannot cleanly separate slow
  cars from walkers. Treat shares as relative.
- The walk-in routes come from stadium trips, so a street away from the stadium
  path cannot qualify as a pedestrian street even if people walk it (e.g. Rousso
  Rd and W Patrick Ln have high pedestrian share but no walk-in route support).
- Allegiant Stadium Way qualifies on 376 ordinary-day observations, close to the
  300 floor; its walk-in route volume is the stronger support.
- Why dense-ping months read less vehicle than sparse-ping months is not
  established; the month-matched comparison controls for it but does not explain it.
- Thresholds (5% of walking trips / 300 observations / 200 baseline / 10 pts) are judgment calls, exposed as flags.
- Local streets Ali Baba Ln, Ponderosa Way and Oquendo Rd are *less* pedestrian
  on event days (about -8 to -10 pts), probably event vehicle traffic; not flagged.
- Tertiary roads that earlier counted as gateways (Wynn, Oquendo, Diablo, Arville,
  Post) are now local streets under the arterial definition; revisit if the
  design intent treats some of them as district entries.
- The merged district polygon has not been split by building.

### Checks that were run

Seed stability (seeds 0 and 2: same 7 gateways, counts within 3%); event vs.
non-event shifts by street; ping cadence by month; event-skew date
distribution (42.4% vs. 10.5%); synthetic unit tests for the month-matched
shift; ACS commute-mode sanity check.

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

## 8c. Street-type classification uses a broader, district-wide trip population

Every other analysis in this pipeline — `routes.json`, `lot_summary.json`,
residency, extrapolation, the dashboard's Volume and Residency map modes —
is built from `find_qualifying_trips()`'s output: a device must have pinged
inside one of the mapped parking-lot polygons **and** been confirmed at the
stadium within `--max-post-minutes`. That's the right population for "how
do people who parked in Lot X get to the stadium," but it's the wrong
population for "what kind of street is Russell Road": a trip only shows up
in `routes.json` if a device's post-parking *walk* happened to clip that
road's sidewalk, because `build_route_sequences` only windows pings between
the lot-arrival ping and the stadium-confirmation ping. The vehicle itself
driving in on a gateway road, before ever reaching a mapped lot, never
appears in that population at all — so a real vehicle gateway with little
or no pedestrian sidewalk traffic was structurally invisible to the old
(lot-trip-only) street-type classification, and would misclassify as a
pedestrian corridor or go unclassified no matter how much real car traffic
it carried.

Street-TYPE classification (§8b) now runs against a separate, broader
population instead: `build_district_trajectory_legs()` takes **every**
device remaining in `pathing` after the pipeline's existing noise/staff/
high-emittance filtering — not just the lot-and-stadium-qualified ones —
and splits each device's full ping sequence into separate travel "legs"
wherever two consecutive pings are more than `--street-type-max-gap-minutes`
apart (default 20). That gap-split matters: without it, a single device's
arrival trip, in-stadium dwell, and departure trip would chain into one
nonsensical polyline spanning wildly different times of day. Because
Azira's Pathing X report is itself anchored to confirmed stadium visitors,
this broader population is still "people connected to a stadium visit" —
just without the additional requirement that they specifically parked in a
mapped lot — which recovers people who drove through on a gateway road
without ever pinging inside one of the mapped polygons (parked on-street,
used an unmapped lot, were dropped off, etc.).

These district-wide legs are snapped onto the street network with the same
`match_routes_to_street_segments` function the lot-qualified routes use, and
produce their **own** edge/corridor files — `street_classification_edge_geometries.json`
and `street_classification_edge_corridors.json` — kept separate from
`edge_geometries.json`/`edge_corridors.json`, which stay lot-trip-only for
Volume/Residency. `street_classification.json` itself is then built from
this broader population's corridor vote tallies, not the lot-trip ones.

**Mode classification for this population uses LOCAL, per-ping modes, not
one whole-leg label.** `classify_route_mode` (one median-speed label for an
entire trip, §8b) is the right tool for a lot-qualified route, because that
route is already bounded to one real activity — the walk from parking to
the stadium. A district-wide leg is bounded only by a 20-minute ping gap,
which can span *multiple* real activities on one device without ever
crossing that gap: drive in on a gateway road, park, then walk around
tailgating for 15 minutes. Applying one whole-leg median to that leg lets
the far more numerous slow walking-paced segments later in the leg drag
the *entire* leg's label to `"walking"` — including the genuinely fast
driving that happened earlier, on a completely different street — and that
single mislabeled vote then gets cast for every corridor the leg touched,
gateways included. This was confirmed on real data: Al Davis Way, Russell
Road, and Hacienda Avenue — all real vehicle gateways — showed 11:1 to 24:1
walking-to-car vote ratios under the whole-leg approach, and only 1 of
3,922 corridors classified as `gateway` district-wide.

The fix: `classify_route_local_modes` assigns each **ping** its own local
mode, from a small centered rolling median (default 5 ping-to-ping
segments) of nearby speeds — enough to smooth the same GPS jitter
`classify_route_mode`'s median was built to resist, small enough that the
label actually tracks where a leg's real behavior changes. When computing
`corridor_mode_counts`, `match_routes_to_street_segments` then casts each
corridor's vote from the local modes of only the pings that snapped to
*that* corridor's edges, not the leg's single whole-trip mode — so a leg
that drove on a gateway and later walked elsewhere correctly casts a `car`
vote on the gateway and `walking` votes wherever it later walked, instead
of one label for both. A corridor reached only through a bridged gap
(`bridge_route_gaps` fills in a street-network path between two observed
points with no ping of its own there) has no local-mode evidence and votes
`"unknown"` rather than guessing. This local-mode path is used only for
the district-wide population; the lot-qualified routes' `mode` field in
`routes.json` is unaffected and still comes from the original
whole-trip `classify_route_mode`.

Two new flags control this:

- `--street-type-max-gap-minutes` (default `20.0`) — the gap threshold
  described above. Lower it if a district this compact is chaining
  obviously-unrelated trips into one leg; raise it if real legs are being
  cut short.
- `--street-type-use-lot-trips-only` — an escape hatch back to the old
  (lot-trip-only) behavior, for when the district-wide pass is too slow for
  a given machine/network (it processes every remaining device, not just
  the lot-qualified subset, so it can touch an order of magnitude more
  trip legs). Using it reproduces the original under-counting of vehicle
  traffic on gateway roads, so only reach for it as a fallback.

Because the dashboard's Street Type exhibits (§11) now draw from this
broader population's own edge files, they can show real streets — and real
gateway/local classifications — that never appear on the Volume or
Residency maps at all, since those two still reflect only the lot-qualified
trips.

## 8d. District-wide street typing (standalone script)

`district_wide_street_analysis.py` types streets from a district-wide pathing
extract (every device near any district building, not just stadium visitors).
It imports `pathing_pipeline.py`, so run it from the project folder.

```
python district_wide_street_analysis.py --pathing 10178831_Clark_pathing_x_report.tsv.gz ^
  --max-legs 300000 --seed 0 --biking-as-wheeled ^
  --event-calibration output\extrapolation_calibration.json ^
  --walkin-routes output\routes.json --walkin-edge-corridors output\edge_corridors.json ^
  --out district_wide_output_final2
```

**Population and performance**
- Per-device travel legs are split on gaps > `--street-type-max-gap-minutes` (20);
  legs under `--min-leg-pings` (4) are dropped; a seeded sample of `--max-legs`
  (default 150000; 0 = all) is snapped (`--seed`). The first run on all 1.06M
  legs never finished because gap bridging ran an uncached, uncapped
  shortest-path search; it now uses per-source capped searches with a cache
  (same output as before, roughly 60x faster on the test graph).
- Corridor votes use per-ping rolling-median local modes; segments longer than
  60 s are ignored (sparse pings make long-gap speeds meaningless).
- No staff/employee filtering: with one merged district polygon the
  repeat-visitor heuristic would flag anyone who lives or works in the district.

**Flags**
- `--biking-as-wheeled` pools "biking" votes with "car" (assumption, off by
  default; raw `mode_counts` are always written). Rationale in section 0.
- `--event-calibration <extrapolation_calibration.json>` supplies event dates for
  the event / non-event split. Without it everything is treated as baseline.
- `--walkin-routes`, `--walkin-edge-corridors`, `--min-walkin-share` (0.05),
  `--min-ped-observations` (300), `--min-baseline-votes` (200),
  `--event-shift-threshold` (0.10), `--min-event-votes` (100).
  (`--ped-share-threshold` is used only by the legacy share rule.)

**Typing rules**
- `refine_street_types` first computes, for every public corridor, the ordinary-day
  pedestrian share, the month-matched event shift and the event flag
  (`event_pedestrian_flag`).
- `assign_street_roles` then sets the final role (`refined_type`):
  - `gateway`: OSM arterial class (motorway / trunk / primary / secondary and links).
  - `pedestrian_corridor`: non-arterial with >= `--min-walkin-share` (0.05) of all
    walking lot-to-stadium trips AND >= `--min-ped-observations` (300) ordinary-day
    walking/stationary observations. Absolute volume, not share.
  - `local_street`: everything else (`low_data` when < 200 baseline observations).
  - Walk-in volumes come from the stadium-anchored run (`routes.json` +
    `edge_corridors.json`; trips with mode "walking"), via
    `--walkin-routes` / `--walkin-edge-corridors`.
- **Month-matched event comparison.** Event dates sit mostly in the fall season,
  and GPS ping cadence changes a lot by month, so the event shift compares
  event days with non-event days from the *same months* (non-event share
  re-weighted by the event votes' month mix). Output field:
  `month_matched_baseline_ped_share`.
- To re-apply the role rules to existing outputs without the heavy run:
  `python retype_streets.py --district-dir district_wide_output_final2 --routes output\routes.json --edge-corridors output\edge_corridors.json`
  (keeps the previous file as `street_classification_refined_prev.json`).

**Outputs:** `street_classification.json` (base typing),
`street_classification_refined.json` (final types, routine / event shares, shift,
flag), edge geometry and corridor files, `date_distribution.json` (event-skew
check), `mode_summary.json`, `ping_cadence_by_month.json` (data-quality check).

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
| `street_classification.json` | *(only with `--street-match`)* Per-corridor type (gateway/pedestrian_corridor/local_street/unclassified/excluded_private), built from the broader district-wide population — see §8c |
| `street_classification_edge_geometries.json` / `street_classification_edge_corridors.json` | *(only with `--street-match`, and not `--street-type-use-lot-trips-only`)* The district-wide edge/corridor files `street_classification.json` was actually matched against — kept separate from `edge_geometries.json`/`edge_corridors.json` because the two populations differ (§8c) |

## 10. Rebuilding the dashboard

`index.html` is generated from `index_template.html` by `build_dashboard.py`:

```bash
python3 build_dashboard.py
```

It reads two folders:
- `output/` — the event-anchored, lot-qualified run (`flow_lines`, `lot_centroids`,
  `lot_summary`, `routes`, `event_meta`, `edge_geometries`, `edge_corridors`,
  `extrapolation_calibration`). This feeds the **Volume** and **Local vs. Visitor** maps.
- `district_final/` — the district-wide run from section 8d
  (`street_classification.json`, `street_classification_refined.json`,
  `street_classification_edge_geometries.json`,
  `street_classification_edge_corridors.json`). This feeds both **Street Type**
  maps. The script merges the refined type, routine / event shares and event flag
  onto each corridor.

**All eight placeholders in the template must be replaced**; one left as a
literal `__PLACEHOLDER__` breaks the page's JavaScript. The build script
asserts this. Missing optional files become `{}` and the dashboard degrades
gracefully (straight-line paths, no Street Type modes) with a banner.
It also writes `output/dashboard_data.json`, the bundle another game's
dashboard can load in its comparison picker.

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
- **Street Roles** (third option in the map-view toggle): one consolidated
  map that colors every real public street by its role from
  `street_classification.json` — gateway (blue), pedestrian street (pink),
  local street (grey) — per §8b/§8c. Line weight is scaled within each role
  so busy gateways do not wash out the smaller pedestrian streets. A yellow
  halo marks an event-day surge. Clicking a legend row isolates one role, and
  the narrative steps use the same focus.
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

### Reading each map (also summarized on the dashboard)

- **Volume / Local vs. Visitor:** stadium-anchored walk-in trips, mostly event days.
- **Street Roles:** one map. Gateways are arterials; pedestrian streets are non-arterials with heavy walking use backed by the walk-in routes (Volume map); local streets are the rest. A yellow halo marks an event-day pedestrian surge measured against same-month ordinary days.
- The page opens with an introduction (what the tool is, the three street roles) and a controls card describing every filter. The narrative sits beside a sticky map and drives it as you scroll; touching any control pauses the tour until "Resume tour" is clicked. The filters live in a drawer under the map, the charts sit in a full-width section below, and Street Roles does not respond to the lot / date / residency filters. The theme is light with yellow highlighter marks.
- The dashboard shows a short explainer under the map that changes with the selected map, and a "Method" section above the footer covering the two populations, why we use two, how streets are typed, how event days are handled, and the limits.

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
