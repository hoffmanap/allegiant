#!/usr/bin/env python3
"""
pathing_pipeline.py
====================
Turns raw Azira Pinnacle pathing exports into clean, route-level data for
Allegiant Stadium parking-lot analysis.

INPUT (three Azira export files, read by header name -- see README.md):
  1. Pathing X report      -> every ping for every device, tagged to a polygon,
                               with a signed "time before" the visit (0 = inside
                               the polygon).
  2. Pin report             -> confirmed stadium visits (device, timestamp,
                               location name, lat/lon).
  3. Visitors Home report   -> each device's Common Evening Location (CEL),
                               used to classify local vs. visitor.

OUTPUT:
  output/routes.json        -> one record per qualifying trip: ordered ping
                               sequence from a parking lot to the stadium (or
                               stadium to lot), device metadata, lot, direction,
                               local/visitor flag.
  output/lot_summary.json   -> per-lot aggregate stats for the dashboard's lot
                               picker (trip counts, median trip time, local %).
  output/flow_lines.json    -> simplified route "desire lines" (lot centroid ->
                               waypoints -> stadium) aggregated into a small
                               number of representative paths per lot, weighted
                               by device count, for fast map rendering.

USAGE:
  python pathing_pipeline.py \
      --pathing data/pathing_x_report.csv \
      --pin data/pin_report.csv \
      --home data/visitors_home_report.csv \
      --lots data/parking_lots.geojson \
      --stadium-polygon "Allegiant Stadium" \
      --out output/

See README.md for full field definitions, assumptions, and how to point this
at your real Azira export column names if they differ from the defaults below.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import math
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

try:
    from shapely.geometry import Point, shape
except ImportError:  # pragma: no cover
    Point = None
    shape = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pathing_pipeline")


# --------------------------------------------------------------------------- #
# Config: column-name mapping. Azira's docs render header tables as images,
# so exact physical column names can vary by export vintage / account setup.
# Edit this mapping (or pass --column-map a JSON file) rather than the parsing
# logic below, if your files use different header text.
# --------------------------------------------------------------------------- #

DEFAULT_COLUMN_MAP = {
    "pathing": {
        "device_id": "Hashed Device ID",
        "polygon_name": ["Polygon Name", "Polygon ID"],  # "Polygon ID" appears on exports
                                                          # pulled with a single custom analysis
                                                          # area instead of per-lot polygons
        "time_before": "Time before appearance in polygon",  # signed seconds; 0 = in-polygon
        "lat": "Lat of Observation Point",
        "lon": "Lon of Observation Point",
    },
    "pathing_optional": {
        "timestamp": "Unix Timestamp of Observation Point",  # real per-ping epoch time, preferred
        "study_polygon": "Study Polygon",         # the report's study location (stadium)
        "category": "Category",
    },
    "pin": {
        "device_id": "Hashed Device ID",
        "timestamp": "Unix Timestamp of Visit",
        "lat": "Lat of Visit",
        "lon": "Lon of Visit",
    },
    "pin_optional": {
        "location_name": "location_name",  # not present in Angelica's export --
                                            # a Pin report is generated for one
                                            # study location, so every row IS a
                                            # stadium visit when this is absent
    },
    "home": {
        "device_id": "Hashed Ubermedia Id",
        "cel_lat": "Common Evening Lat",
        "cel_lon": "Common Evening Long",
    },
}

# Las Vegas-Henderson-Paradise CBSA, approx bounding circle used as the
# local/visitor cutoff if no explicit CBSA polygon is supplied.
# (center ~ downtown Las Vegas, radius chosen to cover Clark County, NV)
LAS_VEGAS_METRO_CENTER = (36.1699, -115.1398)
LAS_VEGAS_METRO_RADIUS_MILES = 45.0

MAX_POST_MINUTES = 60          # matches the user's configured pathing window
EMPLOYEE_DWELL_HOURS = 4.0     # Azira's default employee-dwell threshold
EMPLOYEE_MIN_DAYS_SEEN = 6     # Azira's default "or 6, whichever is larger"
EMPLOYEE_MIN_DAY_FRACTION = 0.20
HIGH_EMITTANCE_PING_CAP = 400  # generous per-device ping cap; tune per file size
MIN_DEVICES_FOR_LOT_CONFIDENCE = 38  # mirrors Azira's own default rendering minimum

# Exact Allegiant Stadium building footprint, [lon, lat] pairs (GeoJSON winding
# order). The Pin report covers every building footprint in the wider stadium
# district, not just the stadium itself, so a device only counts as a real
# stadium visit if its ping actually falls inside this polygon -- polygon_name
# / study-location matching alone is not enough for that file.
DEFAULT_STADIUM_POLYGON = [
    (-115.1831768, 36.0923076), (-115.1831136, 36.092163), (-115.183261, 36.092129),
    (-115.1834294, 36.0920949), (-115.1836347, 36.092046), (-115.1838676, 36.0919819),
    (-115.1841683, 36.0917677), (-115.1844651, 36.0914671), (-115.1845917, 36.0912433),
    (-115.1846882, 36.0910483), (-115.1847509, 36.0907443), (-115.1847509, 36.0904326),
    (-115.1845966, 36.0900196), (-115.18437, 36.089778), (-115.1840662, 36.0896027),
    (-115.1838214, 36.0895358), (-115.1834934, 36.0895187), (-115.1831072, 36.0895657),
    (-115.1826257, 36.0897624), (-115.1822871, 36.0900146), (-115.1819909, 36.090455),
    (-115.1818639, 36.0908398), (-115.1819221, 36.0912502), (-115.1820277, 36.0914788),
    (-115.1820999, 36.0916556), (-115.1821308, 36.0917118), (-115.1819725, 36.0917586),
    (-115.1820051, 36.091821), (-115.1819467, 36.0918415), (-115.1820563, 36.0920053),
    (-115.1821898, 36.0921379), (-115.182411, 36.0922612), (-115.1825831, 36.0923322),
    (-115.1828235, 36.0923628), (-115.1829128, 36.0923517), (-115.1831768, 36.0923076),
]

# The stadium DISTRICT bounding box -- everything this analysis cares about
# (lots, rideshare drops, the stadium itself, and the streets connecting
# them) lives inside this ~1.1 x 2.6 mile box. Pathing and Pin report rows
# outside it are dropped immediately on load as out-of-scope, and it's used
# directly as the OSM street-network download extent for --street-match
# (no separate buffer needed -- this box was already drawn to cover the
# district with margin). Format: (min_lon, min_lat, max_lon, max_lat).
DEFAULT_DISTRICT_BBOX = (-115.2006395781, 36.0652499586, -115.1803492012, 36.1026977365)


def filter_to_bbox(df: pd.DataFrame, bbox: tuple[float, float, float, float]) -> pd.DataFrame:
    """Keep only rows whose lat/lon falls inside the given (min_lon, min_lat,
    max_lon, max_lat) box. A simple, fast axis-aligned pre-filter -- applied
    before any other processing so nothing outside the stadium district
    (including any wild GPS-noise outlier) ever reaches downstream logic."""
    min_lon, min_lat, max_lon, max_lat = bbox
    mask = (
        (df["lat"] >= min_lat) & (df["lat"] <= max_lat)
        & (df["lon"] >= min_lon) & (df["lon"] <= max_lon)
    )
    return df[mask]


def point_in_polygon(lat: float, lon: float, polygon_lon_lat: list[tuple[float, float]]) -> bool:
    """Standard ray-casting point-in-polygon test for a single point.
    polygon_lon_lat is a list of (lon, lat) pairs (GeoJSON order); no
    external geo library required. Prefer points_in_polygon() below for
    anything beyond a handful of points -- this scalar version is only kept
    for clarity/testing."""
    x, y = lon, lat
    inside = False
    n = len(polygon_lon_lat)
    x1, y1 = polygon_lon_lat[0]
    for i in range(1, n + 1):
        x2, y2 = polygon_lon_lat[i % n]
        if y > min(y1, y2):
            if y <= max(y1, y2):
                if x <= max(x1, x2):
                    if y1 != y2:
                        xinters = (y - y1) * (x2 - x1) / (y2 - y1) + x1
                    if x1 == x2 or x <= xinters:
                        inside = not inside
        x1, y1 = x2, y2
    return inside


def points_in_polygon(lats: np.ndarray, lons: np.ndarray, polygon_lon_lat: list[tuple[float, float]]) -> np.ndarray:
    """Vectorized ray-casting point-in-polygon test for many points at once --
    this is what the pipeline actually uses, since production pathing/pin
    files can be tens of millions of rows and a per-row .apply() would be
    far too slow."""
    x = np.asarray(lons, dtype=float)
    y = np.asarray(lats, dtype=float)
    poly = np.array(polygon_lon_lat, dtype=float)
    xs, ys = poly[:, 0], poly[:, 1]
    n = len(poly)
    inside = np.zeros(len(x), dtype=bool)
    xj, yj = xs[-1], ys[-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        for i in range(n):
            xi, yi = xs[i], ys[i]
            denom = np.where(yj != yi, yj - yi, np.nan)
            x_intersect = (xj - xi) * (y - yi) / denom + xi
            intersect = ((yi > y) != (yj > y)) & (x < x_intersect)
            inside ^= intersect
            xj, yj = xi, yi
    return inside


def filter_to_polygon(df: pd.DataFrame, polygon_lon_lat: list[tuple[float, float]]) -> pd.DataFrame:
    """Keep only rows whose lat/lon actually falls inside the given polygon."""
    mask = points_in_polygon(df["lat"].to_numpy(), df["lon"].to_numpy(), polygon_lon_lat)
    return df[mask]


def load_geojson_polygons(path: str) -> dict[str, list[tuple[float, float]]]:
    """Load a FeatureCollection of named Polygon/MultiPolygon features into a
    dict of {name: [(lon, lat), ...]} using each feature's outer ring only
    (holes/inner rings and additional multipolygon parts are not needed for
    parking-lot-sized shapes). Used for both the real parking-lot boundaries
    and, if ever needed, a custom stadium footprint."""
    with open(path) as f:
        gj = json.load(f)
    lots = {}
    for feat in gj["features"]:
        name = feat["properties"]["name"]
        geom = feat["geometry"]
        if geom["type"] == "Polygon":
            ring = geom["coordinates"][0]
        elif geom["type"] == "MultiPolygon":
            ring = geom["coordinates"][0][0]
        else:
            continue
        lots[name] = [tuple(pt) for pt in ring]
    return lots


def assign_matched_lot(
    df: pd.DataFrame, lots: dict[str, list[tuple[float, float]]]
) -> pd.Series:
    """Spatially assign each row to the (first) named lot polygon its lat/lon
    actually falls inside, or NaN if it falls inside none of them. This is
    the real fix for lot detection -- Azira's 'Polygon Name' field (from its
    POI context library) tags points with whatever nearby retail/POI polygon
    matched, which is NOT the same as 'is this point inside one of our
    mapped event parking lots.' Assumes lots don't overlap; first match wins
    if they do.
    """
    lat = df["lat"].to_numpy()
    lon = df["lon"].to_numpy()
    matched = np.full(len(df), np.nan, dtype=object)
    unresolved = np.ones(len(df), dtype=bool)
    for name, poly in lots.items():
        if not unresolved.any():
            break
        idx = np.where(unresolved)[0]
        inside = points_in_polygon(lat[idx], lon[idx], poly)
        hit_idx = idx[inside]
        matched[hit_idx] = name
        unresolved[hit_idx] = False
    return pd.Series(matched, index=df.index)


# --------------------------------------------------------------------------- #
# Data containers
# --------------------------------------------------------------------------- #

@dataclass
class PipelineStats:
    raw_pings: int = 0
    raw_devices: int = 0
    devices_with_lot_ping: int = 0
    devices_confirmed_stadium: int = 0
    devices_dropped_no_stadium: int = 0
    devices_dropped_employee: int = 0
    devices_dropped_repeat_visit_staff: int = 0
    devices_dropped_high_emittance: int = 0
    devices_with_home: int = 0
    final_trips: int = 0
    notes: list = field(default_factory=list)

    def as_dict(self):
        return self.__dict__


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def load_table(path: str, required_cols: dict, optional_cols: dict | None = None) -> pd.DataFrame:
    """Load a delimited table (csv/tsv, optionally gzipped) and rename its
    columns to our internal canonical names. Delimiter and compression are
    both auto-detected from the filename so pathing.tsv.gz, visitor_home.tsv.gz,
    and plain .csv all just work. Fails loudly (with the actual header list)
    if a *required* column is missing; optional columns (e.g. a real
    per-ping timestamp) are included when present and silently skipped when
    not, so the pipeline can fall back gracefully.

    Each value in required_cols/optional_cols can be a single header string,
    or a LIST of candidate header strings tried in order -- e.g. a pathing
    export pulled with a single custom analysis polygon uses "Polygon ID"
    where the original per-lot export used "Polygon Name"; both resolve to
    our internal "polygon_name" automatically without needing a separate
    --column-map file for what's really the same field under a different
    vendor header.
    """
    lower = str(path).lower()
    is_gz = is_gzip_file(path)  # detected from actual file bytes, not the filename --
                                 # a real export can be gzip-compressed binary data
                                 # under a plain .tsv name (confirmed directly: one
                                 # export's own gzip header recorded its original
                                 # filename as ending in .tsv, no .gz at all)
    compression = "gzip" if is_gz else None
    # strip a trailing .gz from the name (if present) to inspect the "real"
    # extension underneath it -- used only as a fast-path hint for the
    # separator; falls back to content-sniffing below when ambiguous
    base = lower[:-3] if lower.endswith(".gz") else lower
    if base.endswith(".tsv"):
        sep = "\t"
    elif base.endswith(".csv"):
        sep = ","
    else:
        # Ambiguous filename (doesn't literally end in .tsv/.csv before the
        # .gz, e.g. an export named just "pathing_export.gz") -- sniff the
        # actual first line's content instead of guessing from the name, so
        # this works for any real export regardless of naming convention.
        opener = gzip.open if compression == "gzip" else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as f:
            first_line = f.readline()
        sep = "\t" if first_line.count("\t") >= first_line.count(",") else ","
    df = pd.read_csv(path, sep=sep, compression=compression)

    def resolve(candidates):
        """candidates is a str or list[str]; returns the first one present
        in df.columns, or None if none match."""
        options = [candidates] if isinstance(candidates, str) else candidates
        for opt in options:
            if opt in df.columns:
                return opt
        return None

    resolved_required = {}
    missing = []
    for canon, candidates in required_cols.items():
        found = resolve(candidates)
        if found is None:
            missing.append(candidates)
        else:
            resolved_required[canon] = found
    if missing:
        raise ValueError(
            f"{path}: expected column(s) {missing} not found. "
            f"Actual columns: {list(df.columns)}. "
            f"Update --column-map or DEFAULT_COLUMN_MAP if your export uses "
            f"different header names."
        )
    keep = dict(resolved_required)
    if optional_cols:
        for canon, candidates in optional_cols.items():
            found = resolve(candidates)
            if found is not None:
                keep[canon] = found
    inverse = {src: canon for canon, src in keep.items()}
    df = df.rename(columns=inverse)
    return df[list(keep.keys())]


def normalize_timestamp(series: pd.Series) -> pd.Series:
    """Azira timestamps may be delivered as raw Unix epoch seconds or as a
    pre-converted 'friendly date' string, depending on export settings.
    Detect which, and return a tz-aware UTC datetime series either way."""
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_datetime(series, unit="s", utc=True)
    parsed = pd.to_datetime(series, utc=True, errors="coerce")
    if parsed.isna().mean() > 0.5:
        log.warning(
            "More than half of timestamps failed to parse as dates; "
            "check whether this column is really epoch seconds."
        )
    return parsed


# --------------------------------------------------------------------------- #
# Cleaning stages
# --------------------------------------------------------------------------- #

def filter_high_emittance(pathing: pd.DataFrame, cap: int, stats: PipelineStats) -> pd.DataFrame:
    """Drop devices that emit an implausible number of pings -- Azira's docs
    flag 'high-emittance devices' as a known source of pathing-file noise."""
    counts = pathing.groupby("device_id").size()
    noisy = counts[counts > cap].index
    if len(noisy):
        stats.devices_dropped_high_emittance = len(noisy)
        stats.notes.append(
            f"Dropped {len(noisy)} device(s) exceeding {cap} pings "
            f"(high-emittance filter)."
        )
    return pathing[~pathing["device_id"].isin(noisy)]


def filter_employee_devices(
    pathing: pd.DataFrame,
    lot_polygon_names: list[str],
    stats: PipelineStats,
) -> pd.DataFrame:
    """Approximate Azira's documented employee filter: dwell > 4h AND
    seen on more days than max(20% of report days, 6 days). We approximate
    dwell using the spread of |time_before| for in/near-polygon pings per
    device per polygon, since we don't have raw per-day visit counts in a
    single pathing export. This is intentionally conservative -- see README
    for how to replace this with a proper multi-day Dwell Time report if
    you have one.
    """
    in_lot = pathing[pathing["polygon_name"].isin(lot_polygon_names)]
    if in_lot.empty:
        return pathing

    dwell = (
        in_lot.groupby("device_id")["time_before"]
        .agg(lambda s: (s.max() - s.min()) / 3600.0)  # hours spanned
        .rename("dwell_hours")
    )
    long_dwell_devices = dwell[dwell > EMPLOYEE_DWELL_HOURS].index

    if len(long_dwell_devices):
        stats.devices_dropped_employee = len(long_dwell_devices)
        stats.notes.append(
            f"Flagged {len(long_dwell_devices)} device(s) with >{EMPLOYEE_DWELL_HOURS}h "
            f"span in a lot polygon as likely employees/staff and excluded them. "
            f"NOTE: this is a single-event approximation of Azira's documented "
            f"multi-day employee filter -- see README."
        )
    return pathing[~pathing["device_id"].isin(long_dwell_devices)]


# Q4 2025 Stadium Authority filing (Attachment B1): 5,272 employed + 513 open
# positions. Used as a real sanity check on repeat-visit staff detection --
# see identify_repeat_visit_staff's docstring.
KNOWN_STADIUM_WORKFORCE = 5785
REPEAT_VISIT_STAFF_MIN_DISTINCT_DATES = 5


def identify_repeat_visit_staff(
    pathing: pd.DataFrame, stadium_polygon: list[tuple[float, float]],
    min_distinct_dates: int, stats: "PipelineStats",
) -> set[str]:
    """Identify devices that are very likely stadium staff rather than event
    attendees, using how many distinct calendar dates a device pings INSIDE
    THE STADIUM FOOTPRINT itself -- not just the wider district, which would
    also catch hotel employees, nearby residents, and other unrelated
    frequent traffic (validated directly: an earlier district-wide version
    of this check flagged 16,840 devices, 2.9x the real stadium workforce of
    5,785 -- clearly not staff-specific. Restricting to the stadium footprint
    brought that down to 282, a plausible 0.05x of the known workforce).

    This replaces filter_employee_devices as the primary staff-detection
    method. That function's within-single-visit dwell-time heuristic has
    consistently flagged zero devices across every real run of this
    pipeline -- it was built for a single-event context and doesn't work
    applied to a year-spanning multi-event dataset, where even a real
    staff member's individual shift just looks like an ordinary few-hour
    dwell. It's kept as a secondary check (still applied afterward) in
    case it ever does catch something this method misses, but it should
    not be relied on alone.

    Directly validated against real data: the 69 flagged devices that also
    appear in a full year's confirmed lot-to-stadium trips skewed 75.2%
    local vs. a 50.5% dataset-wide baseline -- confirming the mechanism
    (staff genuinely do skew local relative to attendees) even though their
    overall quantitative effect on the full-year local/visitor split was
    modest (-0.55 points), since they're a small share (2.2%) of trips.

    CAVEAT: this is a LOWER BOUND on true staff presence. Mobile Advertising
    IDs reset over time -- a real staff member's device can change hashed ID
    across the year, making repeat visits invisible to this method.
    """
    if "timestamp" not in pathing.columns or pathing["timestamp"].isna().all():
        stats.notes.append(
            "Repeat-visit staff detection skipped: no usable per-ping timestamp column "
            "(needs real timestamps, not just the time_before fallback)."
        )
        return set()

    stadium_pings = filter_to_polygon(pathing, stadium_polygon)
    if stadium_pings.empty:
        stats.notes.append("Repeat-visit staff detection: no pings fell inside the stadium footprint.")
        return set()

    local_dates = stadium_pings["timestamp"].dt.tz_convert("America/Los_Angeles").dt.date
    dates_per_device = stadium_pings.assign(local_date=local_dates).groupby("device_id")["local_date"].nunique()
    flagged = set(dates_per_device[dates_per_device >= min_distinct_dates].index)

    ratio = len(flagged) / KNOWN_STADIUM_WORKFORCE
    plausibility = (
        "plausible" if ratio <= 1.5 else
        "IMPLAUSIBLY HIGH -- likely catching more than just stadium staff; consider raising "
        "min_distinct_dates or checking the stadium footprint polygon"
    )
    stats.notes.append(
        f"Repeat-visit staff detection: {len(flagged)} devices appeared inside the stadium "
        f"footprint on {min_distinct_dates}+ distinct dates -- flagged as likely staff and "
        f"excluded. That's {ratio:.2f}x the known {KNOWN_STADIUM_WORKFORCE}-person stadium "
        f"workforce ({plausibility})."
    )
    return flagged


def filter_repeat_visit_staff(pathing: pd.DataFrame, flagged_ids: set[str], stats: "PipelineStats") -> pd.DataFrame:
    if not flagged_ids:
        return pathing
    before = pathing["device_id"].nunique()
    pathing = pathing[~pathing["device_id"].isin(flagged_ids)]
    after = pathing["device_id"].nunique()
    stats.devices_dropped_repeat_visit_staff = before - after
    return pathing


def haversine_miles(lat1, lon1, lat2, lon2) -> np.ndarray:
    r = 3958.8
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(a))


def classify_local_visitor(
    home_df: pd.DataFrame,
    metro_center: tuple[float, float] = LAS_VEGAS_METRO_CENTER,
    metro_radius_miles: float = LAS_VEGAS_METRO_RADIUS_MILES,
) -> pd.DataFrame:
    """Classify each device as 'local' or 'visitor' using its Common Evening
    Location (CEL) distance from the Las Vegas metro center. Devices with no
    CEL on file (insufficient 3-month history, per Azira docs) are 'unknown'.
    """
    out = home_df.copy()
    has_home = out["cel_lat"].notna() & out["cel_lon"].notna()
    out["distance_from_metro_mi"] = np.nan
    out.loc[has_home, "distance_from_metro_mi"] = haversine_miles(
        out.loc[has_home, "cel_lat"],
        out.loc[has_home, "cel_lon"],
        metro_center[0],
        metro_center[1],
    )
    out["residency"] = np.select(
        [~has_home, out["distance_from_metro_mi"] <= metro_radius_miles],
        ["unknown", "local"],
        default="visitor",
    )
    return out[["device_id", "residency", "distance_from_metro_mi"]]


def is_gzip_file(path: str) -> bool:
    """Detects gzip compression from the file's actual first two bytes
    (the gzip magic number 0x1f 0x8b), not its filename extension. Real
    exports can end up with a misleading name -- e.g. a file that's still
    gzip-compressed binary data but named with a plain .tsv extension
    (confirmed directly: a real export's own gzip header recorded its
    original filename as ending in .tsv, not .tsv.gz) -- so trusting the
    extension alone is not reliable enough here.
    """
    with open(path, "rb") as f:
        return f.read(2) == b"\x1f\x8b"


def load_resident_worker_report(path: str) -> pd.DataFrame:
    """Loads Azira/Near's own Resident/Worker Report -- a vendor-provided,
    authoritative classification for a (usually smaller) subset of devices,
    NOT a full-population sample. Empirically (validated against a real
    export): devices with neither flag true are absent from the report
    entirely -- it specifically targets devices already identified as
    resident and/or worker, so a device's ABSENCE from this report means
    "not specifically flagged," not "confirmed visitor." The existing
    CEL-distance-based classify_local_visitor() (fed by the full-population
    Visitor Home report) remains the right PRIMARY source for ordinary
    attendees; this report is an authoritative override/supplement for the
    devices it does cover -- most valuably, Worker=="true" as a real
    (not heuristic) staff-exclusion signal.

    Columns used (exact vendor headers, no column-map needed since this is
    loaded directly rather than through load_table): 'Device ID', 'Resident'
    ('true'/'false'/'unknown'), 'Worker' (same three values).
    """
    df = pd.read_csv(path, sep="\t", compression="gzip" if is_gzip_file(path) else None)
    df = df.rename(columns={"Device ID": "device_id", "Resident": "resident_flag", "Worker": "worker_flag"})
    return df[["device_id", "resident_flag", "worker_flag"]]


def get_worker_device_ids(rw_report: pd.DataFrame) -> set:
    """Extracts the set of device_ids Azira's own Resident/Worker Report
    flags Worker=='true' -- a real vendor signal, usable early in the
    pipeline (same stage as the repeat-visit staff heuristic) since it
    doesn't depend on residency classification at all."""
    return set(rw_report.loc[rw_report["worker_flag"] == "true", "device_id"])


def apply_resident_worker_overrides(
    residency: pd.DataFrame, rw_report: pd.DataFrame, stats: "PipelineStats",
) -> pd.DataFrame:
    """Overrides the CEL-heuristic residency classification with Azira's own
    Resident flag wherever this authoritative report covers a device. Only
    'true'/'false' are used as overrides -- 'unknown' in the vendor report
    is left to fall back to our own CEL-distance classification rather than
    downgrading an existing confident label to 'unknown'.
    """
    rw = rw_report.set_index("device_id")
    out = residency.copy().set_index("device_id")

    resident_true = rw.index[rw["resident_flag"] == "true"]
    resident_false = rw.index[rw["resident_flag"] == "false"]
    overridden_local = out.index.intersection(resident_true)
    overridden_visitor = out.index.intersection(resident_false)
    out.loc[overridden_local, "residency"] = "local"
    out.loc[overridden_visitor, "residency"] = "visitor"

    stats.notes.append(
        f"Resident/Worker report: overrode residency for {len(overridden_local)} device(s) to "
        f"'local' and {len(overridden_visitor)} device(s) to 'visitor' using Azira's own Resident "
        f"flag (authoritative, where this report covers them)."
    )
    return out.reset_index()


def find_qualifying_trips(
    pathing: pd.DataFrame,
    pin: Optional[pd.DataFrame],
    lot_polygon_names: list[str],
    stadium_name: str,
    max_post_minutes: int,
    stats: PipelineStats,
    stadium_polygon: Optional[list[tuple[float, float]]] = None,
) -> pd.DataFrame:
    """Core noise-removal step: keep only devices that (a) ping inside a
    selected lot polygon, and (b) have a confirmed stadium visit whose real
    elapsed time from the lot ping falls within max_post_minutes. Everything
    else -- a device that entered a lot but never reached the stadium
    in-window -- is dropped as noise.

    STADIUM-VISIT CONFIRMATION SOURCE: if a Pin report is supplied, it's
    used as an independent check that a real stadium visit occurred. If
    `pin` is None (no Pin report available), stadium visits are instead
    derived directly from the pathing file itself: any row whose
    polygon_name matches `stadium_name` counts as a confirmed visit. This is
    a slightly weaker signal (it relies on the same pathing export rather
    than a second, independent one) but works fine as long as your pathing
    file actually tags stadium-polygon rows with the stadium's name.

    TIMING SOURCE: if the pathing export includes a real per-ping
    "timestamp" column (e.g. "Unix Timestamp of Observation Point"), we use
    that directly against the stadium visit timestamp -- no assumption
    about what "Time Before" is anchored to is needed. This is the preferred
    path and what real Azira Pathing X (with Context) exports provide. If no
    absolute timestamp column is present, we fall back to treating
    "time_before" as already relative to the stadium visit (see the
    fallback branch below) -- less reliable, so a warning is logged.
    """
    lot_pings = pathing[pathing["polygon_name"].isin(lot_polygon_names)].copy()
    stats.devices_with_lot_ping = lot_pings["device_id"].nunique()
    if lot_pings.empty:
        stats.notes.append("No pings found for the selected lot(s).")
        return pd.DataFrame()

    if pin is not None:
        if "location_name" in pin.columns:
            stadium_visits = pin[pin["location_name"] == stadium_name].copy()
        else:
            # No location_name column: a Pin report is generated for a single
            # study location, so every row already is a stadium visit.
            stadium_visits = pin.copy()
            stadium_visits["location_name"] = stadium_name
        if "timestamp" not in stadium_visits.columns or stadium_visits["timestamp"].isna().all():
            stadium_visits = stadium_visits.assign(timestamp=pd.NaT)
    else:
        # Derive stadium visits from the pathing file's own stadium-polygon rows.
        stadium_visits = pathing[pathing["polygon_name"] == stadium_name].copy()
        stadium_visits["location_name"] = stadium_name
        if "timestamp" not in stadium_visits.columns:
            stadium_visits["timestamp"] = pd.NaT
        if stadium_polygon and not stadium_visits.empty:
            pre = len(stadium_visits)
            stadium_visits = filter_to_polygon(stadium_visits, stadium_polygon)
            stats.notes.append(
                f"Spatially filtered pathing-derived stadium visits to the stadium "
                f"footprint: {len(stadium_visits)} of {pre} rows were actually inside it."
            )

    if stadium_visits.empty:
        stats.notes.append(
            f"No stadium visits found matching '{stadium_name}' in "
            f"{'the Pin report' if pin is not None else 'the pathing file'}. "
            f"Check spelling / exact polygon label."
        )
        return pd.DataFrame()

    has_real_timestamp = "timestamp" in lot_pings.columns and lot_pings["timestamp"].notna().any()
    has_stadium_timestamp = stadium_visits["timestamp"].notna().any()

    if has_real_timestamp and has_stadium_timestamp:
        # One stadium visit time per device (earliest, in case of duplicates)
        stadium_time = (
            stadium_visits.sort_values("timestamp")
            .groupby("device_id", as_index=False)
            .first()[["device_id", "timestamp"]]
            .rename(columns={"timestamp": "stadium_time"})
        )
        merged = lot_pings.merge(stadium_time, on="device_id", how="inner")
        merged["elapsed_seconds"] = (merged["stadium_time"] - merged["timestamp"]).dt.total_seconds()

        # Outbound: lot ping before the stadium visit, closest to it (smallest positive elapsed)
        outbound = merged[merged["elapsed_seconds"] >= 0]
        outbound_arrival = (
            outbound.sort_values("elapsed_seconds")
            .groupby(["device_id", "polygon_name"], as_index=False)
            .first()
        )
        outbound_arrival["minutes_to_stadium"] = outbound_arrival["elapsed_seconds"] / 60.0
        outbound_arrival["direction"] = "to_stadium"

        # Inbound: lot ping after the stadium visit, closest to it (smallest |negative elapsed|)
        inbound = merged[merged["elapsed_seconds"] < 0].copy()
        inbound["_abs"] = inbound["elapsed_seconds"].abs()
        inbound_arrival = (
            inbound.sort_values("_abs")
            .groupby(["device_id", "polygon_name"], as_index=False)
            .first()
        )
        if not inbound_arrival.empty:
            inbound_arrival["minutes_to_stadium"] = inbound_arrival["elapsed_seconds"].abs() / 60.0
            inbound_arrival["direction"] = "from_stadium"

        candidates = pd.concat([outbound_arrival, inbound_arrival], ignore_index=True)
        candidates["minutes_to_stadium_seconds"] = candidates["elapsed_seconds"].abs()
        # Absolute clock time this leg began -- the lot-departure ping for an
        # inbound (to_stadium) trip, or the stadium-departure moment for an
        # outbound (from_stadium) trip. This is what lets the dashboard show
        # when trips actually happen, not just how long they took.
        candidates["departure_time"] = candidates["timestamp"].where(
            candidates["direction"] == "to_stadium", candidates["stadium_time"]
        )

    else:
        stats.notes.append(
            "WARNING: no absolute timestamp column found on the pathing file -- "
            "falling back to treating 'time_before' as already relative to the "
            "stadium visit. Validate this against a real device's trail before "
            "trusting the numbers (see README section 2)."
        )
        outbound = lot_pings[lot_pings["time_before"] >= 0]
        outbound_arrival = (
            outbound.sort_values("time_before")
            .groupby(["device_id", "polygon_name"], as_index=False)
            .first()
            .rename(columns={"time_before": "minutes_to_stadium_seconds"})
        )
        outbound_arrival["minutes_to_stadium"] = outbound_arrival["minutes_to_stadium_seconds"] / 60.0
        outbound_arrival["direction"] = "to_stadium"

        inbound = lot_pings[lot_pings["time_before"] < 0].copy()
        inbound["_abs"] = inbound["time_before"].abs()
        inbound_arrival = (
            inbound.sort_values("_abs")
            .groupby(["device_id", "polygon_name"], as_index=False)
            .first()
            .rename(columns={"time_before": "minutes_to_stadium_seconds"})
        )
        if not inbound_arrival.empty:
            inbound_arrival["minutes_to_stadium"] = inbound_arrival["minutes_to_stadium_seconds"].abs() / 60.0
            inbound_arrival["direction"] = "from_stadium"

        candidates = pd.concat([outbound_arrival, inbound_arrival], ignore_index=True)
        candidates["departure_time"] = pd.NaT  # no absolute time available in this fallback path

    # Confirm with the Pin report: device must have an independent stadium
    # visit record.
    confirmed_ids = set(stadium_visits["device_id"])
    candidates = candidates[candidates["device_id"].isin(confirmed_ids)]

    qualifying = candidates[candidates["minutes_to_stadium"] <= max_post_minutes].copy()

    stats.devices_confirmed_stadium = qualifying["device_id"].nunique()
    stats.devices_dropped_no_stadium = max(
        stats.devices_with_lot_ping - stats.devices_confirmed_stadium, 0
    )
    stats.notes.append(
        f"{stats.devices_confirmed_stadium} of {stats.devices_with_lot_ping} "
        f"lot-visiting devices confirmed a stadium arrival within "
        f"{max_post_minutes} minutes; {stats.devices_dropped_no_stadium} dropped as noise "
        f"({'real timestamps' if has_real_timestamp and has_stadium_timestamp else 'time_before fallback'}; "
        f"stadium confirmation via {'Pin report' if pin is not None else 'pathing file'})."
    )
    return qualifying


def filter_route_path_outliers(
    routes: list[dict],
    center: tuple[float, float],
    max_radius_miles: float,
    stats: "PipelineStats",
) -> list[dict]:
    """Drop individual pings (not necessarily whole routes) that are wildly
    far from the stadium -- GPS noise, bad fixes, or spoofed locations show
    up in real mobile location data as pings thousands of miles away, and
    left unfiltered they wreck anything that takes a min/max over lat/lon
    (most immediately: the street-network download bounding box, which will
    try to cover the distance from Hawaii to New York rather than a few
    miles around Las Vegas if even one such ping slips through). A route
    that loses so many points it has fewer than 2 left is dropped entirely.
    """
    kept_routes = []
    points_before = points_after = dropped_routes = 0
    center_lat, center_lon = center

    for r in routes:
        points_before += len(r["path"])
        lats = np.array([p[0] for p in r["path"]])
        lons = np.array([p[1] for p in r["path"]])
        dist = haversine_miles(lats, lons, center_lat, center_lon)
        mask = dist <= max_radius_miles
        new_path = [p for p, m in zip(r["path"], mask) if m]
        points_after += len(new_path)
        if len(new_path) < 2:
            dropped_routes += 1
            continue
        r["path"] = new_path
        kept_routes.append(r)

    dropped_points = points_before - points_after
    if dropped_points or dropped_routes:
        stats.notes.append(
            f"Dropped {dropped_points} outlier ping(s) more than {max_radius_miles} miles "
            f"from the stadium (GPS noise/bad fixes) and {dropped_routes} route(s) that had "
            f"too few valid points left afterward."
        )
    return kept_routes


def build_route_sequences(
    pathing: pd.DataFrame,
    qualifying_trips: pd.DataFrame,
    max_post_minutes: int,
) -> list[dict]:
    """For each qualifying device+lot trip, pull every ping for that device
    within the post window and order it into a simple lat/lon polyline.
    Windows on real timestamps when available (matching how trips were
    qualified); falls back to time_before ordering otherwise.
    """
    routes = []
    valid_devices = set(qualifying_trips["device_id"])
    device_pings = pathing[pathing["device_id"].isin(valid_devices)]
    has_real_timestamp = "timestamp" in pathing.columns and pathing["timestamp"].notna().any()

    for _, trip in qualifying_trips.iterrows():
        device_id = trip["device_id"]
        lot = trip["polygon_name"]
        direction = trip["direction"]
        dp = device_pings[device_pings["device_id"] == device_id].copy()

        if has_real_timestamp:
            dp = dp.sort_values("timestamp")
            anchor_time = trip["stadium_time"] if "stadium_time" in trip else trip.get("timestamp")
            if direction == "to_stadium":
                lo_t, hi_t = anchor_time - pd.Timedelta(seconds=trip["minutes_to_stadium_seconds"]), anchor_time
            else:
                lo_t, hi_t = anchor_time, anchor_time + pd.Timedelta(seconds=trip["minutes_to_stadium_seconds"])
            window_pings = dp[(dp["timestamp"] >= lo_t) & (dp["timestamp"] <= hi_t)]
            coords = list(zip(window_pings["lat"], window_pings["lon"]))
            # Real Unix seconds -- these differences are true elapsed time,
            # so speed computed from them (see classify_route_mode) is
            # trustworthy.
            times = [t.timestamp() for t in window_pings["timestamp"]]
        else:
            dp = dp.sort_values("time_before", ascending=False)
            if direction == "to_stadium":
                lo, hi = 0, trip["minutes_to_stadium_seconds"]
            else:
                lo, hi = -trip["minutes_to_stadium_seconds"], 0
            window_pings = dp[(dp["time_before"] >= lo) & (dp["time_before"] <= hi)]
            coords = list(zip(window_pings["lat"], window_pings["lon"]))
            # time_before is already seconds-before-the-anchor, so these
            # differences are also true elapsed time -- just on a relative
            # clock rather than a real one. Fine for speed, not for
            # wall-clock display.
            times = [float(t) for t in window_pings["time_before"]]

        if len(coords) < 2:
            continue
        if direction == "from_stadium":
            coords = list(reversed(coords))  # display stadium -> lot in walking order
            times = list(reversed(times))

        route_record = {
            "device_id": device_id,
            "lot": lot,
            "direction": direction,
            "minutes_to_stadium": round(float(trip["minutes_to_stadium"]), 1),
            "path": [[round(float(lat), 6), round(float(lon), 6)] for lat, lon in coords],
            "path_t": [round(float(t), 3) for t in times],
        }

        # Local (Pacific) departure hour/date, when a real timestamp was
        # available -- lets the dashboard show when trips actually happen,
        # not just how long they took. Null when only the time_before
        # fallback was usable (see the WARNING logged in find_qualifying_trips).
        dep = trip.get("departure_time")
        if pd.notna(dep):
            dep_local = pd.Timestamp(dep).tz_convert("America/Los_Angeles")
            route_record["departure_hour"] = int(dep_local.hour)
            route_record["departure_date"] = dep_local.strftime("%Y-%m-%d")
            route_record["departure_iso"] = dep_local.isoformat()
        else:
            route_record["departure_hour"] = None
            route_record["departure_date"] = None
            route_record["departure_iso"] = None

        routes.append(route_record)
    return routes


# --------------------------------------------------------------------------- #
# Travel-mode classification (per trip, not per raw ping)
# --------------------------------------------------------------------------- #

# Same thresholds as classify_travel_mode.py, kept in sync deliberately --
# a trip classified here and a device classified there should never
# disagree just because someone tuned one file and not the other.
MODE_STATIONARY_MAX_MPS = 0.3
MODE_WALK_MAX_MPS = 2.2
MODE_BIKE_MAX_MPS = 7.0
MODE_IMPLAUSIBLE_MPS = 33.0
MODE_MIN_SEGMENT_SECONDS = 2.0


MODE_MIN_SEGMENTS_FOR_CONFIDENCE = 2


def classify_route_mode(route: dict) -> dict:
    """Assign ONE mode label to an entire trip, instead of classifying each
    ping-to-ping segment independently and letting the label flicker.

    The complaint this fixes: a device that is genuinely walking the whole
    way sits around 1-1.5 m/s, but GPS jitter and uneven ping spacing mean
    any single segment can briefly compute as 0.1 m/s (looks "stationary")
    or 3 m/s (looks like "biking") even though nothing about how the person
    is moving actually changed. Classifying segment-by-segment and voting
    bakes that jitter into the result. Taking the trip's MEDIAN speed
    across all its segments is far more robust to a handful of noisy
    segments than either a per-segment vote or a mean (which a single bad
    segment can drag a long way), and it produces exactly one label per
    trip -- consistent by construction.

    Requires route["path"] and route["path_t"] (same length, time in
    seconds -- real Unix time when available, elapsed relative-clock
    seconds otherwise; both make valid speeds). Returns:
      {"mode": "walking"|"biking"|"car"|"stationary"|None,
       "median_speed_mps": float|None,
       "n_segments_used": int, "n_segments_total": int, "mode_confidence": "ok"|"low"}
    mode is None when there's not enough usable data (fewer than 2 points,
    or every segment was zero/negative/implausible-speed noise).

    mode_confidence is "low" when the label rests on fewer than
    MODE_MIN_SEGMENTS_FOR_CONFIDENCE (default 2) valid speed segments --
    concretely, a trip with only 2 usable GPS pings total. A single
    segment is a single speed measurement: real device data confirmed this
    is a real problem, not a theoretical one -- checking a full real run,
    27% of trips labeled "biking" rested on exactly one segment, where one
    noisy/sparse ping gap can land an otherwise-walking or otherwise-
    driving trip squarely in the narrow 2.2-7 m/s biking band just from
    GPS jitter or an unlucky gap in ping timing, not actual bike travel.
    The label is still returned (it's still the best available estimate
    for that one trip), but callers that AGGREGATE many trips -- most
    importantly street-corridor type classification, see
    classify_street_types -- should down-weight or exclude low-confidence
    votes so a handful of sparse-ping trips can't flip a corridor's type.
    """
    path = route.get("path") or []
    times = route.get("path_t") or []
    if len(path) < 2 or len(times) != len(path):
        return {
            "mode": None, "median_speed_mps": None,
            "n_segments_used": 0, "n_segments_total": 0, "mode_confidence": "low",
        }

    lats = np.array([p[0] for p in path])
    lons = np.array([p[1] for p in path])
    dt = np.diff(np.array(times, dtype=float))
    dist_m = haversine_miles(lats[:-1], lons[:-1], lats[1:], lons[1:]) * 1609.344
    n_total = len(dt)

    # A trip can be built from either a real Unix clock or the relative
    # time_before clock (see build_route_sequences); time_before always
    # runs most-negative -> 0 in the "to_stadium" ordering and was only
    # reversed, not re-signed, for "from_stadium" -- so dt should be
    # positive either way once points are in walking order. Guard anyway:
    # treat non-positive or sub-2-second gaps as too noisy to trust (GPS
    # ping jitter at that resolution produces wild, meaningless speeds).
    valid = (dt >= MODE_MIN_SEGMENT_SECONDS)
    speeds = np.full(n_total, np.nan)
    speeds[valid] = dist_m[valid] / dt[valid]
    speeds = speeds[valid]
    speeds = speeds[speeds <= MODE_IMPLAUSIBLE_MPS]  # drop noise/spoofed-fix spikes

    if len(speeds) == 0:
        return {
            "mode": None, "median_speed_mps": None,
            "n_segments_used": 0, "n_segments_total": n_total, "mode_confidence": "low",
        }

    median_speed = float(np.median(speeds))
    if median_speed <= MODE_STATIONARY_MAX_MPS:
        mode = "stationary"
    elif median_speed <= MODE_WALK_MAX_MPS:
        mode = "walking"
    elif median_speed <= MODE_BIKE_MAX_MPS:
        mode = "biking"
    else:
        mode = "car"

    return {
        "mode": mode,
        "median_speed_mps": round(median_speed, 2),
        "n_segments_used": int(len(speeds)),
        "n_segments_total": n_total,
        "mode_confidence": "ok" if len(speeds) >= MODE_MIN_SEGMENTS_FOR_CONFIDENCE else "low",
    }


# --------------------------------------------------------------------------- #
# Aggregation for the front-end
# --------------------------------------------------------------------------- #

def snap_route_to_grid(path: list[list[float]], grid_deg: float = 0.0008) -> tuple:
    """Snap a route's points to a coarse grid so near-identical GPS traces
    can be grouped into a single 'representative' flow line and weighted by
    device count, instead of rendering thousands of raw overlapping polylines."""
    return tuple((round(lat / grid_deg) * grid_deg, round(lon / grid_deg) * grid_deg) for lat, lon in path)


def build_flow_lines(routes: list[dict], max_lines_per_lot: int = 12) -> dict:
    """Aggregate raw device routes into a small number of representative,
    weighted polylines per lot -- what the map actually renders."""
    from collections import defaultdict

    by_lot = defaultdict(lambda: defaultdict(list))
    for r in routes:
        key = snap_route_to_grid(r["path"])
        group_key = f'{r["lot"]}||{r["direction"]}'
        by_lot[group_key][key].append(r)

    flow_lines = {}
    for lot, groups in by_lot.items():
        ranked = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)
        lines = []
        for snapped_path, members in ranked[:max_lines_per_lot]:
            # use the actual (unsnapped) path of a representative member for
            # display smoothness, weight = number of devices in this cluster
            rep_path = members[0]["path"]
            avg_minutes = float(np.mean([m["minutes_to_stadium"] for m in members]))
            lines.append(
                {
                    "path": rep_path,
                    "device_count": len(members),
                    "avg_minutes_to_stadium": round(avg_minutes, 1),
                }
            )
        flow_lines[lot] = lines
    return flow_lines


# Real 2025 Allegiant Stadium event calendar -- every date here is sourced
# directly from the LV Stadium Authority's own quarterly regulatory filings
# (Section 7.4 of the Stadium Lease Agreement, audited by KPMG), not
# scraped or estimated. Used to calibrate how our device sample scales to
# actual attendance -- see compute_extrapolation_calibration below.
OFFICIAL_EVENT_CALENDAR = {
    "2025-01-05": {"name": "Raiders vs. Chargers", "attendance": 51707, "out_of_town_pct": 56},
    "2025-01-18": {"name": "Inter Miami vs. Club America", "attendance": 46089, "out_of_town_pct": 61},
    "2025-03-01": {"name": "National Rugby League", "attendance": 38107, "out_of_town_pct": 78},
    "2025-03-29": {"name": "Mayday 5525", "attendance": 15914, "out_of_town_pct": 82},
    "2025-04-19": {"name": "WrestleMania 41 Day 1", "attendance": 58538, "out_of_town_pct": 81},
    "2025-04-20": {"name": "WrestleMania 41 Day 2", "attendance": 60103, "out_of_town_pct": 84},
    "2025-04-26": {"name": "AC/DC", "attendance": 48899, "out_of_town_pct": 78},
    "2025-05-03": {"name": "Post Malone", "attendance": 53738, "out_of_town_pct": 69},
    "2025-05-31": {"name": "Kendrick Lamar", "attendance": 52328, "out_of_town_pct": 66},
    "2025-06-06": {"name": "Coldplay Night 1", "attendance": 49950, "out_of_town_pct": 77},
    "2025-06-07": {"name": "Coldplay Night 2", "attendance": 50518, "out_of_town_pct": 79},
    "2025-06-22": {"name": "Concacaf Gold Cup", "attendance": 30073, "out_of_town_pct": 42},
    "2025-06-28": {"name": "Shakira", "attendance": 46120, "out_of_town_pct": 59},
    "2025-07-05": {"name": "The Weeknd", "attendance": 51103, "out_of_town_pct": 58},
    "2025-07-25": {"name": "Beyonce Night 1", "attendance": 43642, "out_of_town_pct": 76},
    "2025-07-26": {"name": "Beyonce Night 2", "attendance": 49049, "out_of_town_pct": 83},
    "2025-08-02": {"name": "Raiders Stadium Practice", "attendance": 17295, "out_of_town_pct": None},
    "2025-08-16": {"name": "Raiders vs. 49ers", "attendance": 54186, "out_of_town_pct": 58},
    "2025-08-23": {"name": "UNLV vs. Idaho State", "attendance": 16592, "out_of_town_pct": None},
    "2025-09-06": {"name": "UNLV vs. UCLA", "attendance": 25433, "out_of_town_pct": None},
    "2025-09-13": {"name": "Canelo vs. Crawford", "attendance": 65302, "out_of_town_pct": 85},
    "2025-09-15": {"name": "Raiders vs. Chargers", "attendance": 58679, "out_of_town_pct": 58},
    "2025-09-19": {"name": "Chris Brown Night 1", "attendance": 42768, "out_of_town_pct": 64},
    "2025-09-20": {"name": "Chris Brown Night 2", "attendance": 46876, "out_of_town_pct": 69},
    "2025-09-28": {"name": "Raiders vs. Bears", "attendance": 59036, "out_of_town_pct": 66},
    "2025-10-04": {"name": "Paul McCartney", "attendance": 42926, "out_of_town_pct": 70},
    "2025-10-11": {"name": "UNLV vs. Air Force", "attendance": 18955, "out_of_town_pct": None},
    "2025-10-12": {"name": "Raiders vs. Titans", "attendance": 50402, "out_of_town_pct": 62},
    "2025-10-25": {"name": "HBCU Classic", "attendance": 30149, "out_of_town_pct": 65},
    "2025-11-01": {"name": "UNLV vs. New Mexico", "attendance": 14374, "out_of_town_pct": None},
    "2025-11-02": {"name": "Raiders vs. Jaguars", "attendance": 52401, "out_of_town_pct": 63},
    "2025-11-15": {"name": "UNLV vs. Utah State", "attendance": 18187, "out_of_town_pct": None},
    "2025-11-17": {"name": "Raiders vs. Cowboys", "attendance": 60327, "out_of_town_pct": 68},
    "2025-11-21": {"name": "UNLV vs. Hawaii", "attendance": 22755, "out_of_town_pct": None},
    "2025-11-23": {"name": "Raiders vs. Browns", "attendance": 53749, "out_of_town_pct": 61},
    "2025-12-07": {"name": "Raiders vs. Broncos", "attendance": 57018, "out_of_town_pct": 69},
    "2025-12-28": {"name": "Raiders vs. Giants", "attendance": 52136, "out_of_town_pct": 54},
    "2025-12-31": {"name": "Las Vegas Bowl", "attendance": 34269, "out_of_town_pct": 76},
}


def compute_extrapolation_calibration(routes: list[dict], event_calendar: dict) -> dict:
    """Calibrates how our device sample scales to actual attendance, by
    comparing our own confirmed-unique-device count against official
    attendance on every date we have both for.

    Uses a POOLED ratio (sum of our devices / sum of official attendance
    across all matched dates) as the primary extrapolation factor, not a
    simple average of each date's individual ratio -- pooling is the more
    statistically sound choice here because it naturally weights
    higher-attendance events more heavily, matching how a real scaling
    factor should behave, rather than letting one small, noisy event (e.g.
    a private event with a handful of confirmed devices) count equally
    with a 60,000-person game in an unweighted average.

    Both the pooled rate AND the simple mean/std of individual dates are
    returned, since the spread between them (and the per-date table) is
    itself informative -- a large spread means the rate genuinely varies by
    event and a single flat scaling factor should be used cautiously.
    """
    df = pd.DataFrame(routes)
    if df.empty or "departure_date" not in df.columns:
        return {"matched_dates": 0, "note": "No dated routes available for calibration."}

    per_date_devices = df.groupby("departure_date")["device_id"].nunique()

    rows = []
    for date, info in event_calendar.items():
        if date not in per_date_devices.index or not info.get("attendance"):
            continue
        our_devices = int(per_date_devices[date])
        rate = our_devices / info["attendance"]
        rows.append({
            "date": date, "name": info["name"], "official_attendance": info["attendance"],
            "our_confirmed_devices": our_devices, "sampling_rate_pct": round(rate * 100, 4),
        })

    if not rows:
        return {"matched_dates": 0, "note": "No dates overlapped between routes and the official event calendar."}

    total_devices = sum(r["our_confirmed_devices"] for r in rows)
    total_attendance = sum(r["official_attendance"] for r in rows)
    pooled_rate = total_devices / total_attendance

    individual_rates = [r["our_confirmed_devices"] / r["official_attendance"] for r in rows]
    mean_rate = sum(individual_rates) / len(individual_rates)
    variance = sum((r - mean_rate) ** 2 for r in individual_rates) / len(individual_rates)

    return {
        "matched_dates": len(rows),
        "pooled_sampling_rate_pct": round(pooled_rate * 100, 4),
        "mean_of_individual_rates_pct": round(mean_rate * 100, 4),
        "std_of_individual_rates_pct": round(variance ** 0.5 * 100, 4),
        "min_rate_pct": round(min(individual_rates) * 100, 4),
        "max_rate_pct": round(max(individual_rates) * 100, 4),
        "extrapolation_multiplier": round(1 / pooled_rate, 1) if pooled_rate else None,
        "per_date": sorted(rows, key=lambda r: r["date"]),
        "caveat": (
            "The pooled rate is a single flat multiplier applied across all lots/dates. "
            "The min/max range above shows real event-to-event variance -- treat any single "
            "extrapolated number as an order-of-magnitude estimate, not a precise count, "
            "especially for lots or dates far from the matched-date sample."
        ),
    }


def compute_lot_extrapolation(routes: list[dict], calibration: dict) -> dict:
    """Correctly extrapolates each lot's device count to an estimated actual
    attendee count, computed at the (lot, date) level -- NOT by multiplying
    a lot's year-aggregate device_count by a single pooled rate.

    WHY THE NAIVE VERSION IS WRONG: a lot's device_count in lot_summary.json
    is a cross-year aggregate (one count per lot, summed across every
    matched event -- a season-ticket holder who used the same lot for 10
    games contributes 1 to that count, correctly, once per lot regardless
    of how many games). The pooled sampling rate, despite being computed by
    summing across dates, is a PER-EVENT density: "on a typical single game
    day, one confirmed device represents this many actual attendees."
    Multiplying a multi-event aggregate by a per-event rate mixes two
    incompatible units and inflates the result by roughly the average
    number of events a device appears across. Validated: applying the
    naive version to a real run summed to 2.7x the entire stadium's actual
    2025 attendance across every event combined -- not a rounding error,
    a real bug.

    THE FIX: group routes by (lot, date), and for every date that has a
    calibration entry (i.e. is in the official event calendar), convert
    that lot-day's own device count using THAT DATE's own sampling rate --
    now the units match (a single day's devices vs. a single day's
    attendance), so the resulting per-day estimates can be validly summed
    across the year into one coherent per-lot total.

    Dates NOT in the official calendar are excluded from the sum entirely
    (undercounting rather than guessing) -- the returned dict also reports
    how many total trips were covered vs. left out, so the coverage gap is
    visible rather than silently smoothed over.
    """
    rate_by_date = {r["date"]: r["sampling_rate_pct"] / 100 for r in calibration.get("per_date", [])}
    if not rate_by_date:
        return {"per_lot": {}, "matched_trips": 0, "unmatched_trips": len(routes)}

    df = pd.DataFrame(routes)
    if df.empty or "departure_date" not in df.columns:
        return {"per_lot": {}, "matched_trips": 0, "unmatched_trips": len(routes)}

    matched_mask = df["departure_date"].isin(rate_by_date)
    matched = df[matched_mask]
    unmatched_count = int((~matched_mask).sum())

    per_lot: dict[str, float] = {}
    for (lot, date), g in matched.groupby(["lot", "departure_date"]):
        rate = rate_by_date[date]
        day_devices = g["device_id"].nunique()
        per_lot[lot] = per_lot.get(lot, 0.0) + (day_devices / rate if rate else 0.0)

    return {
        "per_lot": {lot: round(v) for lot, v in per_lot.items()},
        "matched_trips": int(matched_mask.sum()),
        "unmatched_trips": unmatched_count,
    }


def build_lot_summary(
    routes: list[dict], residency: pd.DataFrame, lot_extrapolation: dict | None = None
) -> list[dict]:
    df = pd.DataFrame(routes)
    if df.empty:
        return []
    df = df.merge(residency, on="device_id", how="left")
    df["residency"] = df["residency"].fillna("unknown")
    per_lot_estimates = (lot_extrapolation or {}).get("per_lot", {})

    summary = []
    for lot, g in df.groupby("lot"):
        to_g = g[g["direction"] == "to_stadium"]
        from_g = g[g["direction"] == "from_stadium"]
        device_count = int(g["device_id"].nunique())
        summary.append(
            {
                "lot": lot,
                "device_count": device_count,
                "estimated_actual_devices": per_lot_estimates.get(lot),
                "median_minutes_to_stadium": round(float(to_g["minutes_to_stadium"].median()), 1) if len(to_g) else None,
                "median_minutes_from_stadium": round(float(from_g["minutes_to_stadium"].median()), 1) if len(from_g) else None,
                "local_pct": round(float((g["residency"] == "local").mean() * 100), 1),
                "visitor_pct": round(float((g["residency"] == "visitor").mean() * 100), 1),
                "unknown_pct": round(float((g["residency"] == "unknown").mean() * 100), 1),
                "confidence": "low" if g["device_id"].nunique() < MIN_DEVICES_FOR_LOT_CONFIDENCE else "ok",
            }
        )
    return sorted(summary, key=lambda x: -x["device_count"])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pathing", default="pathing.tsv.gz",
                    help="Path to Azira Pathing X report (tsv/csv, .gz OK). Default: pathing.tsv.gz")
    p.add_argument("--pin", default="pin_report.tsv.gz",
                    help="Path to Azira Pin report -- stadium visits (tsv/csv, .gz OK). "
                         "Optional: if this file doesn't exist, stadium arrivals are "
                         "derived directly from the pathing file's own stadium-polygon "
                         "rows instead. Default: pin_report.tsv.gz")
    p.add_argument("--home", default="visitor_home.tsv.gz",
                    help="Path to Azira Visitors Home report -- CEL (tsv/csv, .gz OK). Default: visitor_home.tsv.gz")
    p.add_argument("--stadium-name", default="Allegiant Stadium",
                    help="Exact location_name value used for the stadium in the Pin report "
                         "(only used if location_name is present)")
    p.add_argument("--stadium-geojson", default=None,
                    help="Path to a GeoJSON file with the exact stadium footprint polygon. "
                         "If given, Pin report rows are spatially filtered to this polygon "
                         "instead of trusting polygon_name/location_name -- use this when "
                         "your Pin report covers a wider district than just the stadium. "
                         "Defaults to the Allegiant Stadium footprint baked into this script.")
    p.add_argument("--district-bbox", type=float, nargs=4, default=list(DEFAULT_DISTRICT_BBOX),
                    metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
                    help="Hard bounding box for the whole analysis -- pathing and Pin report "
                         "rows outside it are dropped immediately on load, before any other "
                         "processing, and it's used directly as the OSM street-network download "
                         "extent for --street-match. Defaults to the stadium district box: "
                         f"{DEFAULT_DISTRICT_BBOX}")
    p.add_argument("--lots", nargs="*", default=None,
                    help="Restrict to specific lot names (default: all lots found -- from "
                         "--lots-geojson if given, else all polygon_name values in the pathing file)")
    p.add_argument("--lots-geojson", default="parking_lots.geojson",
                    help="Path to a GeoJSON FeatureCollection of named parking-lot boundary "
                         "polygons (Polygon or MultiPolygon, with a 'name' property). When "
                         "present, every pathing ping is spatially tested against these real "
                         "lot boundaries instead of trusting the pathing file's own "
                         "'Polygon Name' field, which comes from Azira's general POI library "
                         "and is not the same as 'is this point in one of our event lots.' "
                         "If this file doesn't exist, falls back to polygon_name matching. "
                         "Default: parking_lots.geojson")
    p.add_argument("--max-post-minutes", type=int, default=MAX_POST_MINUTES)
    p.add_argument("--post-minutes-sensitivity", type=int, nargs="*", default=None,
                    metavar="MINUTES",
                    help="Diagnostic only -- doesn't change the main output. Reports how many "
                         "additional devices would confirm a stadium arrival at each of the "
                         "given window widths (e.g. --post-minutes-sensitivity 60 90 120 180), "
                         "so you can see the real trade-off of raising --max-post-minutes before "
                         "committing to a value, instead of guessing and re-running blind.")
    p.add_argument("--max-ping-radius-miles", type=float, default=50.0,
                    help="Any individual GPS ping more than this many miles from the stadium "
                         "is dropped as noise before route paths are built or a street-network "
                         "bounding box is computed. Real mobile location data routinely includes "
                         "a small number of wildly wrong pings (spoofed GPS, bad fixes) that can "
                         "otherwise blow up any min/max-over-lat/lon calculation. Default: 50 miles "
                         "(comfortably covers the full Las Vegas metro with margin).")
    p.add_argument("--staff-min-distinct-dates", type=int, default=REPEAT_VISIT_STAFF_MIN_DISTINCT_DATES,
                    help="Devices pinging inside the stadium footprint on this many distinct "
                         "calendar dates or more are flagged as likely staff and excluded before "
                         "any trip qualification -- validated against a known workforce size of "
                         f"{KNOWN_STADIUM_WORKFORCE} (Q4 2025 filing); check the "
                         "'x known workforce' ratio logged at runtime before trusting a changed "
                         f"threshold. Default: {REPEAT_VISIT_STAFF_MIN_DISTINCT_DATES}")
    p.add_argument("--disable-staff-filter", action="store_true",
                    help="Skip repeat-visit staff detection entirely (e.g. to reproduce results "
                         "from before this filter existed, or if you have reason to believe it's "
                         "removing real attendees rather than staff).")
    p.add_argument("--resident-worker-report", default=None,
                    help="Optional: Azira/Near's own Resident/Worker Report (.tsv.gz). If given, "
                         "its Worker=='true' devices are excluded as confirmed staff (a real "
                         "vendor signal, used ALONGSIDE the repeat-visit heuristic -- the two "
                         "likely catch different devices, so both apply), and its Resident flag "
                         "overrides the CEL-distance local/visitor classification wherever it "
                         "covers a device. This report only covers a targeted subset (devices "
                         "already flagged resident and/or worker) -- most ordinary attendees "
                         "still fall back to the existing CEL-based classification.")
    p.add_argument("--column-map", default=None, help="Optional JSON file overriding DEFAULT_COLUMN_MAP")
    p.add_argument("--out", default="output/", help="Output directory")
    p.add_argument("--event-name", default="Allegiant Stadium event",
                    help="Human-readable label for this run, e.g. 'Raiders vs. Chiefs (2026-09-14)'. "
                         "Written to output for multi-event comparison in the dashboard.")
    p.add_argument("--event-type", default="unknown",
                    choices=["nfl_home_game", "concert", "other_event", "unknown"],
                    help="Event category, used to group/color comparisons across multiple runs.")
    p.add_argument("--event-date", default=None,
                    help="ISO date of the event (YYYY-MM-DD), optional.")
    p.add_argument("--street-match", action="store_true",
                    help="Snap routes onto real OpenStreetMap street-centerline geometry and "
                         "aggregate device counts per street segment (street_segments.json) "
                         "plus top district entry points (entry_points.json). Requires "
                         "'pip install osmnx networkx shapely' and internet access to "
                         "OpenStreetMap's Overpass API on first run for a given area (results "
                         "are cached to disk afterward). Off by default since it adds a real "
                         "dependency and a slow first-run download.")
    p.add_argument("--street-graph-cache", default="street_graph.graphml",
                    help="Where to cache the downloaded OSM street network, so repeat runs "
                         "over the same area don't re-download it. Default: street_graph.graphml")
    p.add_argument("--street-osm-file", default=None,
                    help="Path to a manually downloaded OSM XML file (.osm) covering the "
                         "district bbox -- use this to skip the Overpass API entirely if your "
                         "network can't reach it at all (common on corporate networks: a "
                         "connection *timeout*, not a slow response, usually means that "
                         "traffic is being blocked rather than the query being too large). "
                         "Get one from https://overpass-turbo.eu (draw/paste the district bbox, "
                         "export as .osm) or crop a regional extract from "
                         "https://download.geofabrik.de to the district bbox first.")
    p.add_argument("--overpass-endpoint", default=None,
                    help="Use exactly this Overpass API URL instead of trying the built-in "
                         "list of public mirrors. E.g. https://overpass.kumi.systems/api")
    p.add_argument("--street-network-buffer-miles", type=float, default=0.6,
                    help="DEPRECATED / no longer used: the street network download extent is "
                         "now the fixed --district-bbox, not a buffer derived from route pings "
                         "(that approach broke badly if a single GPS-noise outlier ping slipped "
                         "through). Kept only so old scripts that pass this flag don't error out.")
    p.add_argument("--street-max-snap-distance-m", type=float, default=60.0,
                    help="A ping farther than this from the nearest OSM street edge (in real "
                         "meters, not degrees) is left unmatched rather than force-snapped to "
                         "whatever edge happens to be nearest -- prevents fabricating a specific "
                         "street for pings that legitimately fall in a parking-lot interior or "
                         "plaza with no nearby mapped path. Raise this if too many pings are "
                         "going unmatched (see the pct_matched note in the run log); lower it if "
                         "segments look like they're picking up implausible detours. Default: 60m")
    p.add_argument("--street-max-bridge-distance-m", type=float, default=400.0,
                    help="A device confirmed in a lot and confirmed at the stadium walked SOME "
                         "real path between the two, even where GPS pings along the way were "
                         "sparse -- this fills those gaps with the actual shortest street-network "
                         "path between the two nearest confirmed points, as long as that real "
                         "path is no longer than this cap. Gaps that would need a longer bridge "
                         "are left open rather than guessed across an implausible distance. "
                         "Default: 400m (roughly a quarter mile, comfortably covers one lot-to-"
                         "street gap without bridging across genuinely unrelated segments).")
    p.add_argument("--street-keep-all-components", action="store_true",
                    help="By default, the street network is restricted to its single largest "
                         "connected component -- a manually-exported .osm file commonly has a "
                         "few small disconnected islands (a clipped way, a missing connector "
                         "node) which otherwise make route-gap bridging silently fail for almost "
                         "every gap that crosses between components. Set this flag to disable "
                         "that restriction and keep every component, e.g. if you've confirmed the "
                         "small components are real isolated pedestrian areas rather than an "
                         "export artifact.")
    return p.parse_args(argv)


# A handful of public Overpass mirrors, in case the default (overpass-api.de)
# is unreachable from a given network -- a corporate firewall/proxy blocking
# one specific host while allowing others is common, and is a different
# failure mode than a query that's genuinely too large (which would show up
# as a slow response, not a connection timeout).
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api",
    "https://overpass.kumi.systems/api",
    "https://overpass.openstreetmap.ru/api",
    "https://overpass.osm.ch/api",
]


def get_street_graph(
    bbox: tuple[float, float, float, float],
    cache_path: str,
    osm_xml_path: str | None = None,
    overpass_endpoint: str | None = None,
    try_all_mirrors: bool = True,
    keep_all_components: bool = False,
):
    """Load a walking-street-network graph for the given bounding box, using
    a local cache if one already exists so repeat runs don't re-hit OSM's
    Overpass API. bbox is (min_lon, min_lat, max_lon, max_lat).

    Three ways to get the actual network data, tried in this order:
      1. `osm_xml_path`, if given -- a manually downloaded .osm/.osm.xml file
         (e.g. a Geofabrik regional extract cropped to the district, or an
         Overpass Turbo export), loaded with no network access at all. This
         is the reliable option when a network genuinely can't reach any
         Overpass endpoint (corporate firewall/proxy blocking that traffic
         entirely, not just one specific mirror).
      2. `overpass_endpoint`, if given -- use exactly that Overpass mirror.
      3. Otherwise, if `try_all_mirrors` is True (the default), try each URL
         in OVERPASS_MIRRORS in turn and use the first one that responds --
         a *connection timeout* to one specific host (as opposed to a slow
         response) usually means that particular host is blocked or down,
         not that the query itself is too big.

    Whichever source it comes from -- including a cached file, so a stale
    cache from before this check existed still gets fixed on the next
    load -- the graph is checked for connectivity before being returned.
    A manually-exported .osm file (e.g. via Overpass Turbo) commonly ends
    up with a handful of small disconnected islands: a footway that wasn't
    fully captured, a service road missing its connecting node. Left alone,
    these make route-gap bridging silently fail for almost every gap (no
    path exists between disconnected components, regardless of distance),
    which looks identical in the stats to "every gap was just too far" --
    an easy, dangerous misdiagnosis. By default this restricts the graph to
    its single largest connected component and logs what got dropped;
    pass keep_all_components=True to disable that (not recommended unless
    you've confirmed the small components are real, isolated pedestrian
    areas rather than an export artifact).

    Requires osmnx (and its networkx/shapely dependencies), imported lazily
    here so the rest of the pipeline works without them installed unless
    --street-match is actually used.
    """
    import osmnx as ox

    def finalize(G, source: str):
        return _check_and_fix_connectivity(G, source, keep_all_components)

    cache = Path(cache_path)
    if cache.exists():
        log.info("Loading cached street network from %s...", cache)
        return finalize(ox.load_graphml(cache), f"cache ({cache})")

    if osm_xml_path:
        log.info("Loading street network from local OSM XML file %s (no network access)...", osm_xml_path)
        G = ox.graph_from_xml(osm_xml_path)
        ox.save_graphml(G, cache)
        log.info("Loaded street network: %d nodes, %d edges. Cached to %s.", len(G.nodes), len(G.edges), cache)
        return finalize(G, f"local file ({osm_xml_path})")

    endpoints_to_try = [overpass_endpoint] if overpass_endpoint else (
        OVERPASS_MIRRORS if try_all_mirrors else [ox.settings.overpass_url]
    )

    last_error = None
    for i, endpoint in enumerate(endpoints_to_try):
        ox.settings.overpass_url = endpoint
        # Give each mirror a shorter timeout when we're trying several in a
        # row -- her earlier failure was a full 180s *connection* timeout
        # (packets silently dropped, not rejected), which is the classic
        # signature of a firewall/proxy black-holing that specific host.
        # Waiting the full 180s per mirror would mean up to 12 minutes
        # before ever reaching a working one.
        ox.settings.requests_timeout = 180 if len(endpoints_to_try) == 1 else 45
        log.info(
            "Downloading OpenStreetMap walking-street network for bbox %s via %s "
            "(first run only -- result will be cached to %s for next time)...",
            bbox, endpoint, cache,
        )
        try:
            G = ox.graph_from_bbox(bbox, network_type="walk")
            ox.save_graphml(G, cache)
            log.info("Downloaded street network: %d nodes, %d edges. Cached to %s.", len(G.nodes), len(G.edges), cache)
            return finalize(G, f"Overpass ({endpoint})")
        except Exception as e:
            last_error = e
            log.warning("Overpass endpoint %s failed (%s); trying next option if any.", endpoint, e)

    raise RuntimeError(
        f"Could not reach any Overpass endpoint ({endpoints_to_try}). "
        f"Last error: {last_error}. If your network blocks this traffic entirely "
        f"(common on corporate networks), download an OSM extract manually -- e.g. from "
        f"https://download.geofabrik.de/north-america/us/nevada.html or via "
        f"https://overpass-turbo.eu -- and pass it with --street-osm-file path/to/file.osm"
    )


def _check_and_fix_connectivity(G, source: str, keep_all_components: bool):
    """Log the graph's connected-component breakdown, and by default
    restrict to the largest one. See get_street_graph's docstring for why
    this matters -- a fragmented graph makes route-gap bridging silently
    fail almost everywhere, indistinguishable in the stats from every gap
    legitimately being too far to bridge."""
    import networkx as nx

    components = list(nx.connected_components(G.to_undirected()))
    components.sort(key=len, reverse=True)
    total_nodes = len(G.nodes)

    if len(components) == 1:
        log.info("Street network from %s: single connected component, %d nodes. Good.", source, total_nodes)
        return G

    largest = components[0]
    pct_in_largest = 100 * len(largest) / total_nodes
    log.warning(
        "Street network from %s is FRAGMENTED: %d separate connected components "
        "(%d nodes total). Largest component has %d nodes (%.1f%%); the other %d "
        "component(s) hold the remaining %d nodes. This is a common artifact of "
        "manually-exported .osm files (a way that got clipped at the export "
        "boundary, a missing connector node) -- left unfixed, route-gap bridging "
        "will silently fail for almost every gap that crosses between components, "
        "since no path exists regardless of distance.",
        source, len(components), total_nodes, len(largest), pct_in_largest,
        len(components) - 1, total_nodes - len(largest),
    )

    if keep_all_components:
        log.warning("--street-keep-all-components was set: NOT restricting to the largest component.")
        return G

    log.info("Restricting to the largest connected component (%d of %d nodes kept).", len(largest), total_nodes)
    return G.subgraph(largest).copy()


def bridge_route_gaps(
    per_route_edges: list[list[str]], G, max_bridge_meters: float = 400.0
) -> tuple[list[list[str]], dict]:
    """A device confirmed in a lot and confirmed at the stadium walked SOME
    real path between the two, even if GPS pings along the way were sparse
    or fell outside the snap distance -- those gaps show up as visibly
    disconnected dashes on the map, which looks like noise but is really
    just missing connective tissue between two solid observations.

    For each route, wherever two consecutive matched edges don't actually
    share an endpoint node (a real topological gap, not just a rendering
    artifact), this fills the gap with the genuine shortest path along the
    real street network between them -- using actual street geometry, not a
    straight-line guess. If the shortest real path would be longer than
    max_bridge_meters, the gap is left open rather than bridged: a visible
    gap is more honest than fabricating a walk across an implausible
    distance, and this cap is exactly the "don't catch strays" guardrail.

    Returns (new_per_route_edges, stats) where stats reports how many gaps
    were found, how many were bridged, and how many were left open.
    """
    import networkx as nx

    G_undirected = G.to_undirected()

    def parse_key(key):
        lo, hi, k = key.split("_")
        return int(lo), int(hi), int(k)

    def canonical_key_from_uv(u, v, k=0):
        lo, hi = (u, v) if u <= v else (v, u)
        return f"{lo}_{hi}_{k}"

    stats = {
        "gaps_found": 0, "gaps_bridged": 0, "gaps_left_open": 0, "bridge_edges_added": 0,
        "gaps_left_open_no_path": 0,     # the two points aren't in the same connected component
        "gaps_left_open_too_far": 0,     # a real path exists but exceeds max_bridge_meters
    }
    new_per_route_edges = []

    for edge_seq in per_route_edges:
        if len(edge_seq) < 2:
            new_per_route_edges.append(edge_seq)
            continue
        new_seq = [edge_seq[0]]
        for i in range(1, len(edge_seq)):
            lo1, hi1, _ = parse_key(new_seq[-1])
            lo2, hi2, _ = parse_key(edge_seq[i])
            if {lo1, hi1} & {lo2, hi2}:
                new_seq.append(edge_seq[i])
                continue

            stats["gaps_found"] += 1
            best_path, best_length = None, None
            for a in (lo1, hi1):
                for b in (lo2, hi2):
                    try:
                        length = nx.shortest_path_length(G_undirected, a, b, weight="length")
                    except (nx.NetworkXNoPath, nx.NodeNotFound):
                        continue
                    if best_length is None or length < best_length:
                        best_length, best_path = length, nx.shortest_path(G_undirected, a, b, weight="length")

            if best_path is not None and best_length <= max_bridge_meters:
                for j in range(len(best_path) - 1):
                    new_seq.append(canonical_key_from_uv(best_path[j], best_path[j + 1]))
                stats["bridge_edges_added"] += len(best_path) - 1
                stats["gaps_bridged"] += 1
            else:
                stats["gaps_left_open"] += 1
                if best_path is None:
                    stats["gaps_left_open_no_path"] += 1
                else:
                    stats["gaps_left_open_too_far"] += 1

            new_seq.append(edge_seq[i])
        new_per_route_edges.append(new_seq)

    return new_per_route_edges, stats


def match_routes_to_street_segments(
    routes: list[dict], G, max_snap_distance_m: float = 60.0, max_bridge_meters: float = 400.0,
    lot_centroids: dict | None = None, route_modes: list[str | None] | None = None,
) -> tuple[list[list], dict, dict, dict, dict, dict, dict]:
    """Snap every route's ping sequence onto the nearest real street edges in
    G. Returns (per_route_edge_lists, edge_geometries, edge_corridors, stats)
    rather than a single pre-aggregated total, so the dashboard can
    recompute segment/entry-point counts live as the user changes the
    lot/residency filters -- exactly like every other chart in this
    dashboard already does with routes.json.

    Distance is computed in real meters (the graph is projected to an
    appropriate local UTM zone internally) rather than raw lat/lon degrees,
    and any ping farther than max_snap_distance_m from the nearest mapped
    edge is left UNMATCHED rather than force-snapped to whatever edge
    happens to be nearest. Right-of-way areas -- parking-lot interiors,
    plazas, medians -- routinely put a ping 30-100m from the nearest
    OSM-mapped centerline; snapping those to the nearest street regardless
    of distance would silently fabricate a specific street a device may
    never have actually walked on. Unmatched pings are simply skipped when
    building each route's edge sequence -- the route still contributes to
    every real edge it clearly did touch.

      per_route_edge_lists[i]: de-duplicated, in-order list of canonical
          edge keys (strings) that routes[i] touched.
      edge_geometries: {canonical_key: [[lat, lon], ...]} for every edge
          touched by any route, for drawing.
      edge_corridors: {canonical_key: corridor_key} -- OSM frequently
          digitizes one physical road as several separate edges (each
          direction of a divided road, or any road with more than one
          nearby parallel way), which would otherwise split a single
          street's real volume across multiple small counts. corridor_key
          is the street's OSM name when available (shared by every edge
          with that name, merging them for counting purposes), or the
          edge's own key when no name tag exists. Drawing still uses each
          edge's real individual geometry; only the aggregate count/color
          is shared across a named corridor's edges.
      stats: {"pings_matched", "pings_unmatched", "pct_matched"}

    route_modes, if given (one entry per route, from classify_route_mode --
    "walking"/"biking"/"car"/"stationary"/None), is used to additionally
    return corridor_mode_counts: {corridor_key: {mode: n_trips}} -- a vote
    is cast per (trip, corridor) pair, not per edge or per raw ping, so a
    single trip with many pings on one corridor doesn't drown out other
    trips, and a trip that merely clips the corner of a corridor counts
    the same as one that follows it for blocks. edge_tags and edge_names
    are also returned (keyed by canonical edge key) so a caller can apply
    street-type classification (classify_corridor_types) without having
    to re-derive OSM tag data that was already pulled here.
    """
    import numpy as _np
    import osmnx as ox
    import pyproj

    flat_lat, flat_lon, owner = [], [], []
    for i, r in enumerate(routes):
        for lat, lon in r["path"]:
            flat_lat.append(lat)
            flat_lon.append(lon)
            owner.append(i)

    if not flat_lat:
        return (
            [[] for _ in routes], {}, {},
            {"pings_matched": 0, "pings_unmatched": 0, "pct_matched": 0.0},
            {}, {}, {},
        )

    # Project both the graph and the query points to a local UTM zone so
    # nearest_edges distances are real meters, not mixed-scale lat/lon
    # degrees -- required for max_snap_distance_m to mean anything.
    G_proj = ox.project_graph(G)
    transformer = pyproj.Transformer.from_crs("EPSG:4326", G_proj.graph["crs"], always_xy=True)
    proj_x, proj_y = transformer.transform(_np.array(flat_lon), _np.array(flat_lat))

    raw_edges, dists = ox.distance.nearest_edges(G_proj, X=proj_x, Y=proj_y, return_dist=True)
    matched_mask = dists <= max_snap_distance_m
    n_matched = int(matched_mask.sum())
    n_unmatched = len(matched_mask) - n_matched

    def canonical_key(edge):
        u, v, k = (int(x) for x in edge)
        lo, hi = (u, v) if u <= v else (v, u)
        return f"{lo}_{hi}_{k}"

    per_route_raw = [[] for _ in routes]
    for owner_idx, edge, is_matched in zip(owner, raw_edges, matched_mask):
        if is_matched:
            per_route_raw[owner_idx].append(canonical_key(edge))
        # unmatched pings contribute nothing -- the route just has a gap
        # here rather than a fabricated edge

    per_route_edges = []
    for edge_seq in per_route_raw:
        deduped = []
        for e in edge_seq:
            if not deduped or deduped[-1] != e:
                deduped.append(e)
        per_route_edges.append(deduped)

    per_route_edges, bridge_stats = bridge_route_gaps(per_route_edges, G, max_bridge_meters=max_bridge_meters)
    if bridge_stats["gaps_found"]:
        log.info(
            "Route-gap bridging: %d gaps found (device pinged in a lot, then again near the "
            "stadium, with no matched street edge connecting the two observed points -- common "
            "when GPS pings are sparse or fall outside the snap distance mid-walk). %d bridged "
            "with the actual shortest street-network path between them (%d bridge edges added, "
            "under the %.0fm cap); %d left open -- of those, %d had NO PATH AT ALL in the street "
            "graph (the two points are in different, disconnected pieces of the network -- if "
            "this number is large, the graph itself is fragmented; see the connectivity check "
            "logged when the network loaded) and %d had a real path that simply exceeded the cap.",
            bridge_stats["gaps_found"], bridge_stats["gaps_bridged"],
            bridge_stats["bridge_edges_added"], max_bridge_meters, bridge_stats["gaps_left_open"],
            bridge_stats["gaps_left_open_no_path"], bridge_stats["gaps_left_open_too_far"],
        )

    edge_geometries = {}
    edge_names = {}   # canonical_key -> raw OSM name, or None if unnamed
    edge_nodes = {}   # canonical_key -> (lo, hi) endpoint node ids, for chain-merging unnamed edges
    edge_tags = {}    # canonical_key -> (highway_type, service_type) for descriptive fallback labels
    all_keys = {e for seq in per_route_edges for e in seq}
    for key in all_keys:
        lo, hi, k = key.split("_")
        lo, hi, k = int(lo), int(hi), int(k)
        data = G.get_edge_data(lo, hi, k) or G.get_edge_data(hi, lo, k)
        if data and "geometry" in data:
            coords = [[pt[1], pt[0]] for pt in data["geometry"].coords]
        else:
            coords = [[G.nodes[lo]["y"], G.nodes[lo]["x"]], [G.nodes[hi]["y"], G.nodes[hi]["x"]]]
        edge_geometries[key] = coords
        edge_nodes[key] = (lo, hi)

        name = (data or {}).get("name")
        if isinstance(name, list):  # osmnx gives a list when OSM tags conflict across a merged way
            name = name[0] if name else None
        edge_names[key] = name

        highway = (data or {}).get("highway")
        if isinstance(highway, list):
            highway = highway[0] if highway else None
        service = (data or {}).get("service")
        if isinstance(service, list):
            service = service[0] if service else None
        edge_tags[key] = (highway, service)

    edge_corridors = build_edge_corridors(edge_names, edge_nodes, edge_tags, edge_geometries, lot_centroids)

    corridor_mode_counts: dict[str, Counter] = {}
    if route_modes is not None:
        for i, edge_seq in enumerate(per_route_edges):
            mode = route_modes[i] if i < len(route_modes) else None
            mode_key = mode or "unknown"
            corridors_touched = {edge_corridors.get(e, e) for e in edge_seq}
            for c in corridors_touched:
                corridor_mode_counts.setdefault(c, Counter())[mode_key] += 1
    corridor_mode_counts = {c: dict(counter) for c, counter in corridor_mode_counts.items()}

    return per_route_edges, edge_geometries, edge_corridors, {
        "pings_matched": n_matched,
        "pings_unmatched": n_unmatched,
        "pct_matched": round(100 * n_matched / max(len(matched_mask), 1), 1),
        "route_gap_bridging": bridge_stats,
    }, corridor_mode_counts, edge_tags, edge_names


def normalize_street_name(name: str) -> str:
    """Standardize common OSM naming inconsistencies (directional prefixes,
    abbreviated vs. spelled-out suffixes) so 'W Russell Rd' and 'West
    Russell Road' merge into the same corridor instead of staying split
    just because two blocks of the same physical street were tagged
    slightly differently.
    """
    n = name.strip()
    prefix_map = {"N": "North", "S": "South", "E": "East", "W": "West"}
    suffix_map = {
        "St": "Street", "Rd": "Road", "Dr": "Drive", "Ave": "Avenue",
        "Blvd": "Boulevard", "Ln": "Lane", "Ct": "Court", "Pl": "Place",
        "Pkwy": "Parkway", "Hwy": "Highway", "Cir": "Circle", "Way": "Way",
    }
    parts = n.split()
    if parts and parts[0].rstrip(".") in prefix_map:
        parts[0] = prefix_map[parts[0].rstrip(".")]
    if parts and parts[-1].rstrip(".") in suffix_map:
        parts[-1] = suffix_map[parts[-1].rstrip(".")]
    return " ".join(parts)


HIGHWAY_TYPE_LABELS = {
    "service": "Service Road", "footway": "Footpath", "path": "Path",
    "pedestrian": "Pedestrian Way", "living_street": "Access Road",
    "residential": "Residential Street", "unclassified": "Local Road",
    "track": "Access Track", "steps": "Stairway", "corridor": "Walkway",
    "cycleway": "Bike Path", "alley": "Alley",
}
SERVICE_TYPE_LABELS = {
    "parking_aisle": "Parking Aisle", "driveway": "Driveway", "alley": "Alley",
    "drive-through": "Drive-Through Lane", "emergency_access": "Emergency Access Road",
}


def nearest_lot_name(lat: float, lon: float, lot_centroids: dict) -> str | None:
    """Finds the closest mapped lot to a given point, for building a
    descriptive fallback label like 'Parking Aisle near Lot A' instead of a
    raw OSM node-id string, when a street segment genuinely has no name."""
    if not lot_centroids:
        return None
    best_name, best_dist = None, None
    for name, c in lot_centroids.items():
        d = haversine_miles(lat, lon, c["lat"], c["lon"])
        if best_dist is None or d < best_dist:
            best_dist, best_name = d, name
    return best_name


def build_edge_corridors(
    edge_names: dict, edge_nodes: dict,
    edge_tags: dict | None = None, edge_geometries: dict | None = None,
    lot_centroids: dict | None = None,
) -> dict:
    """Group edges into corridors two ways:
      1. Named edges: grouped by normalized street name (see
         normalize_street_name) -- this is the primary, most reliable grouping.
      2. Unnamed edges (parking-lot service roads, aisles -- common right next
         to a stadium, where OSM often has no name tag at all): chained
         together with their topological neighbors. An unnamed edge is
         merged with another unnamed edge sharing an endpoint node, as long
         as that node has exactly degree 2 within the unnamed-edge subgraph
         (i.e. it's a straight pass-through point, not a real branching
         intersection) -- this reconstructs a single continuous unnamed
         "corridor" out of what OSM otherwise stored as many tiny
         disconnected block segments, without merging unrelated unnamed
         roads that happen to meet at a real junction.

      Unnamed corridors get a DESCRIPTIVE label instead of a raw node-id
      string, built from whatever OSM tags ARE present even without a name
      -- the `highway` type (service road, footpath, pedestrian way, etc.)
      and, if present, the more specific `service` subtype (parking aisle,
      driveway) -- plus the nearest mapped parking lot, so "unnamed
      corridor 11603236237_11603236313" becomes "Parking Aisle near Lot A."
      Genuinely untagged edges fall back to a generic "Unnamed Path" rather
      than a raw ID.
    """
    corridors = {}

    # 1. Named edges: normalize and group directly.
    unnamed_keys = []
    for key, name in edge_names.items():
        if name:
            corridors[key] = normalize_street_name(name)
        else:
            unnamed_keys.append(key)

    if not unnamed_keys:
        return corridors

    # 2. Unnamed edges: union-find over shared degree-2 endpoints.
    parent = {key: key for key in unnamed_keys}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    node_to_unnamed_edges: dict = {}
    for key in unnamed_keys:
        lo, hi = edge_nodes[key]
        node_to_unnamed_edges.setdefault(lo, []).append(key)
        node_to_unnamed_edges.setdefault(hi, []).append(key)

    for node, keys_here in node_to_unnamed_edges.items():
        if len(keys_here) == 2:  # pass-through point within the unnamed subgraph
            union(keys_here[0], keys_here[1])

    # Build one descriptive label per merged unnamed-corridor group, using
    # the group's most common highway/service tags and its geometric
    # midpoint's nearest lot -- computed once per group, not per edge.
    groups: dict = {}
    for key in unnamed_keys:
        groups.setdefault(find(key), []).append(key)

    edge_tags = edge_tags or {}
    edge_geometries = edge_geometries or {}
    group_labels: dict = {}
    seen_labels: dict = {}
    for root, keys in groups.items():
        highways = [edge_tags.get(k, (None, None))[0] for k in keys if edge_tags.get(k, (None, None))[0]]
        services = [edge_tags.get(k, (None, None))[1] for k in keys if edge_tags.get(k, (None, None))[1]]

        if services:
            label = SERVICE_TYPE_LABELS.get(services[0], services[0].replace("_", " ").title())
        elif highways:
            label = HIGHWAY_TYPE_LABELS.get(highways[0], highways[0].replace("_", " ").title())
        else:
            label = "Unnamed Path"

        if lot_centroids:
            mid_key = keys[len(keys) // 2]
            geom = edge_geometries.get(mid_key)
            if geom:
                mid_pt = geom[len(geom) // 2]
                lot = nearest_lot_name(mid_pt[0], mid_pt[1], lot_centroids)
                if lot:
                    label = f"{label} near {lot}"

        # Disambiguate if the same descriptive label would otherwise collide
        # across multiple distinct physical corridors (e.g. two separate
        # "Parking Aisle near Lot A" groups) -- append a small counter.
        if label in seen_labels:
            seen_labels[label] += 1
            label = f"{label} ({seen_labels[label]})"
        else:
            seen_labels[label] = 1
        group_labels[root] = label

    for key in unnamed_keys:
        corridors[key] = group_labels[find(key)]

    return corridors


# --------------------------------------------------------------------------- #
# Street-type classification (gateway / pedestrian corridor / local street)
# --------------------------------------------------------------------------- #

# OSM highway tags that indicate a real, named, car-capable public road --
# the kind that can plausibly be a "gateway" (a major roadway / vehicle
# entry point) as opposed to a quiet local street. Everything else
# (residential, unclassified, living_street, and all the pedestrian/
# service/track tags) is "local" scale even when it's public.
MAJOR_ROAD_HIGHWAY_TAGS = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link",
}

# service= subtypes and highway= subtypes that mean "this centerline exists
# to serve one private site, not public through-traffic" -- the Al Davis
# Way vs. parking-lot-pathway distinction the classification is required to
# respect. A corridor only needs ONE of these signals to be excluded as
# private; having a real street name is what overrides it (a named road can
# still pass through a service-tagged segment at a driveway cut without
# losing its public status, so the name check comes first in
# classify_corridor_privacy below).
PRIVATE_SERVICE_TAGS = {"parking_aisle", "driveway", "drive-through"}
PRIVATE_FALLBACK_HIGHWAY_TAGS = {"service", "track", "footway", "path", "corridor", "steps"}


def classify_corridor_privacy(
    edge_corridors: dict, edge_names: dict, edge_tags: dict,
    edge_geometries: dict, lot_polygons: dict | None = None,
    lot_containment_threshold: float = 0.5,
) -> dict:
    """Decide, per corridor, whether it's a real public street (eligible for
    gateway/pedestrian-corridor/local classification) or private-property
    infrastructure that should be excluded entirely -- per the explicit
    requirement: classify Al Davis Way, not the pathways inside the parking
    lots.

    A corridor is PUBLIC if it has a real OSM street name (`name` tag) AND
    is not overwhelmingly made of parking-aisle/driveway-tagged edges AND
    doesn't sit mostly inside a mapped parking-lot polygon (a named road can
    still have a short driveway-tagged cut at a lot entrance without losing
    its public status -- that's why these are three separate checks, not
    one).

    A corridor with NO name at all is always treated as private-property
    infrastructure here, even if some of its edges happen to carry a
    "highway=residential"-type tag with no name recorded -- OSM occasionally
    omits names on short connector segments, but in this district the
    unnamed edges are overwhelmingly real parking-lot aisles and footways
    (confirmed by the existing descriptive fallback labels: "Parking Aisle
    near Lot X", "Footpath near Lot X", etc. -- see build_edge_corridors).
    This is a deliberate, conservative choice: it means a genuinely unnamed
    public connector road would be wrongly excluded, but it guarantees
    nothing inside a parking lot gets classified as a gateway/pedestrian/
    local public street, which is the requirement that matters here.

    Returns {corridor_key: {"is_public": bool, "reason": str,
    "majority_highway": str|None, "pct_inside_lot": float}}.
    """
    # Invert edge_corridors to get each corridor's constituent edges.
    corridor_edges: dict = {}
    for edge_key, corridor_key in edge_corridors.items():
        corridor_edges.setdefault(corridor_key, []).append(edge_key)

    lot_polygons = lot_polygons or {}

    result = {}
    for corridor_key, edges in corridor_edges.items():
        names = [edge_names.get(e) for e in edges if edge_names.get(e)]
        has_name = bool(names)

        highways = [edge_tags.get(e, (None, None))[0] for e in edges]
        services = [edge_tags.get(e, (None, None))[1] for e in edges if edge_tags.get(e, (None, None))[1]]
        highway_counts = Counter(h for h in highways if h)
        majority_highway = highway_counts.most_common(1)[0][0] if highway_counts else None

        n_private_service = sum(1 for s in services if s in PRIVATE_SERVICE_TAGS)
        pct_private_service = n_private_service / max(len(edges), 1)

        # Fraction of this corridor's geometry whose midpoint falls inside
        # ANY mapped parking-lot polygon -- a named road that merely runs
        # alongside a lot shouldn't trip this, so it's midpoint-based, not
        # "any vertex," and it's a fraction of the whole corridor, not any
        # single edge.
        n_inside_lot = 0
        for e in edges:
            geom = edge_geometries.get(e)
            if not geom:
                continue
            mid = geom[len(geom) // 2]
            if any(point_in_polygon(mid[0], mid[1], ring) for ring in lot_polygons.values()):
                n_inside_lot += 1
        pct_inside_lot = n_inside_lot / max(len(edges), 1)

        if not has_name:
            is_public, reason = False, "no OSM street name (treated as parking-lot/private infrastructure)"
        elif pct_private_service > 0.5:
            is_public, reason = False, "named, but majority of segments tagged as parking aisle/driveway"
        elif pct_inside_lot >= lot_containment_threshold:
            is_public, reason = False, f"named, but {pct_inside_lot:.0%} of its geometry sits inside a mapped parking lot"
        else:
            is_public, reason = True, "named public street"

        result[corridor_key] = {
            "is_public": is_public,
            "reason": reason,
            "majority_highway": majority_highway,
            "pct_inside_lot": round(pct_inside_lot, 3),
        }
    return result


# Below this many mode-classified trip-votes, a corridor's "dominant mode"
# isn't a real majority -- it's whatever happened to show up first. This is
# a trip-COUNT threshold (how many distinct trips voted), not a ping count.
STREET_TYPE_MIN_VOTES_FOR_CONFIDENCE = 5


def classify_street_types(
    edge_corridors: dict, edge_names: dict, edge_tags: dict, edge_geometries: dict,
    corridor_mode_counts: dict, lot_polygons: dict | None = None,
) -> dict:
    """Assign each real public street corridor one of three types, based on
    the single most common mode (from classify_route_mode, tallied per
    trip per corridor in match_routes_to_street_segments) actually observed
    on it, combined with OSM's own road-hierarchy tag:

      "gateway"            -- vehicle-dominant AND tagged as a major road
                               (primary/secondary/tertiary/trunk/motorway or
                               a _link of one of those) -- a real roadway
                               entry point, not just any street cars use.
      "pedestrian_corridor" -- walking (or stationary, e.g. queued crowds)
                               is the most common mode observed.
      "local_street"        -- vehicle-dominant but NOT a major road by OSM's
                               own hierarchy -- i.e. "primarily vehicle trips
                               navigating to/from the stadium" on an ordinary
                               residential/unclassified/service-with-a-name
                               street, exactly the third category asked for.

    Private-property corridors (parking-lot aisles, footways, driveways --
    see classify_corridor_privacy) are excluded entirely: they get
    type "excluded_private" and are left out of everything else. A public
    corridor that was never actually touched by any mode-classified trip
    (corridor_mode_counts has no entry, or every trip touching it had
    mode=None) gets type "unclassified" rather than a guess.

    Returns {corridor_key: {"type", "is_public", "dominant_mode",
    "mode_counts", "majority_highway", "reason", "type_confidence",
    "n_votes"}}.

    type_confidence is "low" when the type rests on too little evidence to
    trust as a real majority, rather than silently picking a winner anyway:
      - fewer than STREET_TYPE_MIN_VOTES_FOR_CONFIDENCE (default 5) total
        trip-votes landed on this corridor at all, or
      - the top mode is TIED with another mode (e.g. 2 biking vs. 2
        walking) -- Python's max() breaks ties by dict/insertion order,
        which is NOT a meaningful tiebreaker and would otherwise present a
        coin-flip as a confident "dominant mode."
    The type is still assigned (it's still the single best guess with the
    data in hand), but a caller -- especially the dashboard -- should
    visually distinguish low-confidence corridors (e.g. hatched/dashed
    rather than solid-colored) rather than presenting them with the same
    confidence as a corridor with hundreds of consistent votes.
    """
    privacy = classify_corridor_privacy(edge_corridors, edge_names, edge_tags, edge_geometries, lot_polygons)

    result = {}
    for corridor_key, priv in privacy.items():
        if not priv["is_public"]:
            result[corridor_key] = {
                "type": "excluded_private",
                "is_public": False,
                "dominant_mode": None,
                "mode_counts": corridor_mode_counts.get(corridor_key, {}),
                "majority_highway": priv["majority_highway"],
                "reason": priv["reason"],
                "type_confidence": None,
                "n_votes": 0,
            }
            continue

        mode_counts = {m: n for m, n in corridor_mode_counts.get(corridor_key, {}).items() if m != "unknown"}
        n_votes = sum(mode_counts.values())
        if not mode_counts:
            dominant_mode, street_type, type_confidence = None, "unclassified", None
        else:
            top_count = max(mode_counts.values())
            tied_modes = [m for m, n in mode_counts.items() if n == top_count]
            is_tied = len(tied_modes) > 1
            # Deterministic tiebreak (alphabetical) rather than relying on
            # dict insertion order -- still arbitrary, but at least
            # reproducible across runs, and type_confidence flags it either way.
            dominant_mode = sorted(tied_modes)[0] if is_tied else tied_modes[0]

            if dominant_mode in ("walking", "stationary"):
                street_type = "pedestrian_corridor"
            elif dominant_mode in ("biking", "car"):
                street_type = "gateway" if priv["majority_highway"] in MAJOR_ROAD_HIGHWAY_TAGS else "local_street"
            else:
                street_type = "unclassified"

            type_confidence = (
                "low" if (is_tied or n_votes < STREET_TYPE_MIN_VOTES_FOR_CONFIDENCE) else "ok"
            )

        result[corridor_key] = {
            "type": street_type,
            "is_public": True,
            "dominant_mode": dominant_mode,
            "mode_counts": corridor_mode_counts.get(corridor_key, {}),
            "majority_highway": priv["majority_highway"],
            "reason": priv["reason"],
            "type_confidence": type_confidence,
            "n_votes": n_votes,
        }
    return result



def main(argv=None):
    args = parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = PipelineStats()

    column_map = DEFAULT_COLUMN_MAP
    if args.column_map:
        with open(args.column_map) as f:
            column_map = json.load(f)

    log.info("Loading pathing, pin, and home reports...")
    pathing = load_table(args.pathing, column_map["pathing"], column_map.get("pathing_optional"))

    district_bbox = tuple(args.district_bbox)
    pre_bbox_rows = len(pathing)
    pathing = filter_to_bbox(pathing, district_bbox)
    log.info(
        "Restricted to the stadium district bounding box %s: %d of %d pathing rows kept "
        "(%.1f%%). Everything outside this box -- including any GPS-noise outlier pings -- "
        "is dropped before any other processing.",
        district_bbox, len(pathing), pre_bbox_rows, 100 * len(pathing) / max(pre_bbox_rows, 1),
    )
    if pathing.empty:
        log.error(
            "No pathing rows fall inside --district-bbox %s. Double-check the box "
            "coordinates and that lat/lon columns are mapped correctly.", district_bbox
        )
        sys.exit(1)

    stadium_polygon = DEFAULT_STADIUM_POLYGON
    if args.stadium_geojson:
        with open(args.stadium_geojson) as f:
            gj = json.load(f)
        stadium_polygon = gj["features"][0]["geometry"]["coordinates"][0]
        stadium_polygon = [tuple(pt) for pt in stadium_polygon]
        log.info("Loaded custom stadium footprint from %s (%d vertices).", args.stadium_geojson, len(stadium_polygon))

    pin_path = Path(args.pin)
    if pin_path.exists():
        pin = load_table(args.pin, column_map["pin"], column_map.get("pin_optional"))
        pin["timestamp"] = normalize_timestamp(pin["timestamp"])
        pin = filter_to_bbox(pin, district_bbox)
        pre_filter_rows = len(pin)
        pin = filter_to_polygon(pin, stadium_polygon)
        log.info(
            "Using Pin report at %s for stadium-visit confirmation -- spatially "
            "filtered to the stadium footprint (%d of %d district-bbox rows were actually "
            "inside the stadium polygon; the rest were other buildings in the district).",
            args.pin, len(pin), pre_filter_rows,
        )
        if pin.empty:
            log.error(
                "No Pin report rows fall inside the stadium polygon. Double-check "
                "--stadium-geojson / DEFAULT_STADIUM_POLYGON coordinates and that "
                "the Pin report's lat/lon columns are mapped correctly."
            )
    else:
        pin = None
        log.warning(
            "Pin report not found at %s -- deriving confirmed stadium visits "
            "directly from the pathing file's own stadium-polygon rows instead. "
            "This is a slightly weaker confirmation signal (see README section 3).",
            args.pin,
        )
    home = load_table(args.home, column_map["home"])

    stats.raw_pings = len(pathing)
    stats.raw_devices = pathing["device_id"].nunique()

    if "timestamp" in pathing.columns:
        pathing["timestamp"] = normalize_timestamp(pathing["timestamp"])

    lots_path = Path(args.lots_geojson) if args.lots_geojson else None
    if lots_path and lots_path.exists():
        lot_polygons = load_geojson_polygons(str(lots_path))
        log.info(
            "Spatially matching pathing pings against %d real parking-lot boundaries from %s...",
            len(lot_polygons), lots_path,
        )
        matched_lot = assign_matched_lot(pathing, lot_polygons)
        n_matched = matched_lot.notna().sum()
        log.info(
            "%d of %d pathing pings (%.1f%%) fell inside a mapped parking-lot polygon.",
            n_matched, len(pathing), 100 * n_matched / max(len(pathing), 1),
        )
        # Override polygon_name with the real matched lot where we have one;
        # leave everything else (stadium tags, unmatched POI context) as-is.
        pathing["polygon_name"] = matched_lot.fillna(pathing["polygon_name"])
        lot_polygon_names = args.lots or sorted(lot_polygons.keys())
    else:
        lot_polygons = {}
        if args.lots_geojson:
            log.warning(
                "--lots-geojson path '%s' not found -- falling back to matching lots by "
                "the pathing file's own 'Polygon Name' field (from Azira's POI context "
                "library, NOT necessarily your actual event parking lots). Results may "
                "include non-lot POIs. Provide a real lot-boundary GeoJSON for accurate results.",
                args.lots_geojson,
            )
        # Auto-detect lot polygons if --lots wasn't given: anything that isn't
        # the stadium itself and isn't an obvious "not a named polygon"
        # placeholder some exports use for unmatched context points.
        NON_LOT_PLACEHOLDERS = {"", "en_route", "unmatched", "none", "null"}
        lot_polygon_names = args.lots or sorted(
            p for p in pathing["polygon_name"].dropna().unique()
            if p != args.stadium_name and str(p).strip().lower() not in NON_LOT_PLACEHOLDERS
        )
    log.info("Analyzing %d lot polygon(s): %s", len(lot_polygon_names), lot_polygon_names)

    pathing = filter_high_emittance(pathing, HIGH_EMITTANCE_PING_CAP, stats)

    if not args.disable_staff_filter:
        flagged_staff = identify_repeat_visit_staff(
            pathing, stadium_polygon, args.staff_min_distinct_dates, stats
        )
        if args.resident_worker_report:
            rw_report = load_resident_worker_report(args.resident_worker_report)
            worker_ids = get_worker_device_ids(rw_report)
            new_from_rw = worker_ids - flagged_staff
            stats.notes.append(
                f"Resident/Worker report: {len(worker_ids)} device(s) flagged Worker=='true' "
                f"(a real vendor signal); {len(new_from_rw)} of those weren't already caught by "
                f"the repeat-visit heuristic -- both are applied together."
            )
            flagged_staff = flagged_staff | worker_ids
        pathing = filter_repeat_visit_staff(pathing, flagged_staff, stats)

    pathing = filter_employee_devices(pathing, lot_polygon_names, stats)

    if args.post_minutes_sensitivity:
        log.info("--- Post-window sensitivity check (diagnostic only, doesn't affect output) ---")
        dummy_stats = PipelineStats()
        for window in sorted(set(args.post_minutes_sensitivity)):
            trial = find_qualifying_trips(
                pathing, pin, lot_polygon_names, args.stadium_name, window, dummy_stats,
                stadium_polygon=stadium_polygon,
            )
            n_devices = trial["device_id"].nunique() if not trial.empty else 0
            log.info(
                "  %3d min window: %d confirmed devices (%d trips, both directions)",
                window, n_devices, len(trial),
            )
            dummy_stats = PipelineStats()  # reset notes between trials so they don't pile up
        log.info("--- End sensitivity check ---")

    qualifying = find_qualifying_trips(
        pathing, pin, lot_polygon_names, args.stadium_name, args.max_post_minutes, stats,
        stadium_polygon=stadium_polygon,
    )
    if qualifying.empty:
        log.error("No qualifying trips found -- check stadium name / lot names / date ranges.")
        sys.exit(1)

    routes = build_route_sequences(pathing, qualifying, args.max_post_minutes)

    # Drop GPS-noise outlier pings (spoofed/bad fixes thousands of miles
    # away are common in real mobile location data) before anything computes
    # a min/max over lat/lon -- most importantly the street-network download
    # bounding box below, which would otherwise try to cover a
    # Hawaii-to-New-York span instead of a few miles around the stadium.
    stadium_center_for_filter = (
        sum(p[1] for p in stadium_polygon) / len(stadium_polygon),
        sum(p[0] for p in stadium_polygon) / len(stadium_polygon),
    )
    routes = filter_route_path_outliers(
        routes, stadium_center_for_filter, args.max_ping_radius_miles, stats
    )

    stats.final_trips = len(routes)

    # Classify each trip's travel mode ONCE, from the trip's median speed
    # across its own ping sequence -- not per-ping, so the result can't
    # flicker the way a point-by-point speed check does (see
    # classify_route_mode's docstring). Attached onto each route now so
    # it's in routes.json for the dashboard regardless of whether
    # --street-match is used, and available below to tally per-street-
    # corridor mode votes if it is.
    mode_counts_summary: Counter = Counter()
    for r in routes:
        mode_result = classify_route_mode(r)
        r["mode"] = mode_result["mode"]
        r["mode_median_speed_mps"] = mode_result["median_speed_mps"]
        r["mode_confidence"] = mode_result["mode_confidence"]
        mode_counts_summary[mode_result["mode"] or "unclassified"] += 1
    log.info(
        "Trip-level travel-mode classification (one label per trip, from median speed "
        "across the trip -- not per-ping): %s",
        dict(mode_counts_summary),
    )

    residency = classify_local_visitor(home)
    if args.resident_worker_report:
        rw_report = load_resident_worker_report(args.resident_worker_report)
        residency = apply_resident_worker_overrides(residency, rw_report, stats)
    stats.devices_with_home = int((residency["residency"] != "unknown").sum())

    flow_lines = build_flow_lines(routes)

    calibration = compute_extrapolation_calibration(routes, OFFICIAL_EVENT_CALENDAR)
    extrapolation_multiplier = calibration.get("extrapolation_multiplier")
    if extrapolation_multiplier:
        log.info(
            "Extrapolation calibration: %d dates matched the official event calendar. "
            "Pooled sampling rate %.4f%% (our devices represent roughly 1 in every %.0f "
            "actual attendees on a given day) -- range across matched dates: %.4f%%-%.4f%%.",
            calibration["matched_dates"], calibration["pooled_sampling_rate_pct"],
            extrapolation_multiplier, calibration["min_rate_pct"], calibration["max_rate_pct"],
        )
        lot_extrapolation = compute_lot_extrapolation(routes, calibration)
        log.info(
            "Per-lot extrapolation: %d trips on calendar-matched dates were extrapolated "
            "(summed per lot using that date's OWN sampling rate, not the pooled average -- "
            "avoids the unit-mismatch bug of multiplying a year-aggregate device count by a "
            "single-event rate); %d trips on unmatched dates were left out of the estimate "
            "entirely rather than guessed.",
            lot_extrapolation["matched_trips"], lot_extrapolation["unmatched_trips"],
        )
    else:
        log.info(
            "Extrapolation calibration: no dates in this run overlapped the official event "
            "calendar -- estimated_actual_devices will be null in lot_summary.json."
        )
        lot_extrapolation = {}

    lot_summary = build_lot_summary(routes, residency, lot_extrapolation)

    # attach residency onto individual routes for the front end's local/
    # visitor toggle
    residency_lookup = residency.set_index("device_id")["residency"].to_dict()
    for r in routes:
        r["residency"] = residency_lookup.get(r["device_id"], "unknown")

    # lot centroid (median observed lot-ping location) -- handy for the map's
    # marker layer without requiring a separate lot-boundary file
    centroids = {}
    if lots_path and lots_path.exists():
        # Use the real polygon's own centroid (mean of vertices) -- reliable
        # even for lots with few or zero observed pings.
        for name, ring in lot_polygons.items():
            if name in lot_polygon_names:
                lons = [pt[0] for pt in ring]
                lats = [pt[1] for pt in ring]
                centroids[name] = {"lat": round(sum(lats) / len(lats), 6), "lon": round(sum(lons) / len(lons), 6)}
    else:
        lot_pts = pathing[pathing["polygon_name"].isin(lot_polygon_names)]
        for lot, g in lot_pts.groupby("polygon_name"):
            centroids[lot] = {"lat": round(float(g["lat"].median()), 6), "lon": round(float(g["lon"].median()), 6)}
    if pin is not None:
        stadium_pts = pin[pin["location_name"] == args.stadium_name] if "location_name" in pin.columns else pin
    else:
        stadium_pts = pathing[pathing["polygon_name"] == args.stadium_name]
    stadium_centroid = {
        "lat": round(float(stadium_pts["lat"].median()), 6),
        "lon": round(float(stadium_pts["lon"].median()), 6),
    } if not stadium_pts.empty else None

    (out_dir / "flow_lines.json").write_text(json.dumps(flow_lines, indent=2))
    (out_dir / "lot_summary.json").write_text(json.dumps(lot_summary, indent=2))
    (out_dir / "pipeline_stats.json").write_text(json.dumps(stats.as_dict(), indent=2))
    (out_dir / "extrapolation_calibration.json").write_text(json.dumps(calibration, indent=2))
    (out_dir / "lot_centroids.json").write_text(
        json.dumps({"lots": centroids, "stadium": stadium_centroid}, indent=2)
    )
    event_meta = {"name": args.event_name, "type": args.event_type, "date": args.event_date}
    (out_dir / "event_meta.json").write_text(json.dumps(event_meta, indent=2))

    if args.street_match:
        try:
            # Use the fixed district bounding box directly -- it was drawn to
            # cover the stadium district (lots, rideshare drops, right-of-way
            # margin) with intention, which is more reliable than deriving a
            # box from route pings (which blows up badly if even one
            # GPS-noise outlier slips through -- see filter_route_path_outliers).
            bbox = tuple(args.district_bbox)
            log.info("Street network download extent: the district bbox %s.", bbox)
            G = get_street_graph(
                bbox, args.street_graph_cache,
                osm_xml_path=args.street_osm_file,
                overpass_endpoint=args.overpass_endpoint,
                keep_all_components=args.street_keep_all_components,
            )
            log.info("Snapping %d routes onto the street network (max snap distance %.0fm)...",
                      len(routes), args.street_max_snap_distance_m)
            # Low-confidence mode labels (trips resting on a single speed
            # segment -- see classify_route_mode's docstring, confirmed on
            # real data to land "biking" disproportionately often from GPS
            # sparsity, not real bike travel) don't get a vote in street-
            # corridor typing -- counted as "unknown" instead, same as a
            # trip with no usable mode at all, so a handful of sparse-ping
            # trips can't flip what a corridor's dominant mode looks like.
            route_modes = [
                r.get("mode") if r.get("mode_confidence") == "ok" else None
                for r in routes
            ]
            (
                per_route_edges, edge_geometries, edge_corridors, match_stats,
                corridor_mode_counts, edge_tags, edge_names,
            ) = match_routes_to_street_segments(
                routes, G, max_snap_distance_m=args.street_max_snap_distance_m,
                max_bridge_meters=args.street_max_bridge_distance_m,
                lot_centroids=centroids, route_modes=route_modes,
            )
            for r, edges in zip(routes, per_route_edges):
                r["matched_edges"] = edges
            (out_dir / "edge_geometries.json").write_text(json.dumps(edge_geometries))
            (out_dir / "edge_corridors.json").write_text(json.dumps(edge_corridors))
            touched = len(edge_geometries)
            n_corridors = len(set(edge_corridors.values()))
            log.info(
                "Street matching done: %d distinct street segments touched (%d named-street "
                "corridors after merging divided/parallel carriageways of the same road). "
                "%.1f%% of pings matched within %.0fm (%d unmatched -- left as gaps rather than "
                "force-snapped to a distant/wrong street; raise --street-max-snap-distance-m if "
                "this seems too high). routes.json now includes each trip's matched_edges; the "
                "dashboard aggregates device-per-corridor counts live, filtered by the current selection.",
                touched, n_corridors, match_stats["pct_matched"], args.street_max_snap_distance_m,
                match_stats["pings_unmatched"],
            )
            (out_dir / "street_match_stats.json").write_text(json.dumps(match_stats, indent=2))

            street_classification = classify_street_types(
                edge_corridors, edge_names, edge_tags, edge_geometries,
                corridor_mode_counts, lot_polygons=lot_polygons,
            )
            (out_dir / "street_classification.json").write_text(json.dumps(street_classification, indent=2))
            type_counts = Counter(v["type"] for v in street_classification.values())
            log.info(
                "Street-type classification (public streets only -- parking-lot aisles/"
                "driveways/footways are excluded entirely): %d gateway(s), %d pedestrian "
                "corridor(s), %d local street(s), %d unclassified (no mode-classified trip "
                "touched them), %d private/excluded corridor(s). Written to "
                "street_classification.json.",
                type_counts.get("gateway", 0), type_counts.get("pedestrian_corridor", 0),
                type_counts.get("local_street", 0), type_counts.get("unclassified", 0),
                type_counts.get("excluded_private", 0),
            )
            n_low_confidence = sum(
                1 for v in street_classification.values()
                if v["type"] not in ("excluded_private", "unclassified") and v["type_confidence"] == "low"
            )
            if n_low_confidence:
                log.warning(
                    "%d of the typed corridors above rest on too little evidence to trust as a "
                    "real majority (fewer than %d trip-votes, or a tie between two modes) -- "
                    "flagged type_confidence='low' in street_classification.json rather than "
                    "silently picking a winner. Low-confidence corridors are the district's "
                    "quieter streets, which is expected -- just don't present them with the "
                    "same visual confidence as a corridor backed by hundreds of consistent trips.",
                    n_low_confidence, STREET_TYPE_MIN_VOTES_FOR_CONFIDENCE,
                )
        except ImportError:
            log.error(
                "--street-match requires osmnx, networkx, shapely, and pyproj. Install with: "
                "pip install osmnx networkx shapely pyproj --break-system-packages"
            )
        except Exception as e:
            log.error("Street matching failed: %s. Continuing without it -- other outputs are unaffected.", e)

    (out_dir / "routes.json").write_text(json.dumps(routes, indent=2))

    log.info("Done. %d qualifying trips across %d lots written to %s", len(routes), len(lot_summary), out_dir)
    for note in stats.notes:
        log.info("  - %s", note)


if __name__ == "__main__":
    main()
