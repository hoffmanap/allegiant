#!/usr/bin/env python3
"""
classify_travel_mode.py
========================
Classifies each consecutive-ping segment of a device's raw pathing trail as
walking, biking, or driving (car), using speed between pings. This is a
general GPS-trajectory technique, not specific to this stadium project --
it works on the raw pathing export directly, not on the already-filtered
stadium routes, so it's equally useful for "how are people getting to their
car in the lot" as it is for the core walking-infrastructure question.

THE CORE IDEA
  For every pair of consecutive pings from the same device, compute:
      speed = haversine_distance(ping_i, ping_i+1) / (time_i+1 - time_i)
  and bucket that speed into a mode using two thresholds (walk/bike cutoff,
  bike/car cutoff). This is standard in the GPS mode-detection literature,
  but it is a HEURISTIC, not a measurement -- read the caveats below before
  trusting the output for anything beyond a first-pass exploratory signal.

KNOWN LIMITATIONS (read before use)
  - GPS noise dominates at short time intervals. A 20-foot position error
    over a 10-second gap looks like a multi-mph "speed," easily
    misclassified. --min-segment-seconds discards intervals shorter than a
    configurable floor (default 10s) rather than compute a speed from them.
  - A single implausible speed spike (GPS jump, multipath reflection off a
    building) will get classified as "car" even mid-walk. --smooth-window
    applies a rolling majority vote across consecutive segments per device
    (default window 3) so an isolated one-segment spike doesn't flip the
    mode; set --smooth-window 1 to disable and see raw per-segment output.
  - Speed ranges genuinely overlap between modes: a jogger can hit bike
    speeds; a car in stop-and-go traffic near the stadium can be slower
    than a bike. The thresholds here are reasonable general-purpose
    defaults, not calibrated to this specific site -- treat mode labels as
    "most likely," especially right at a threshold boundary.
  - This cannot distinguish "car" from "bus" or "rideshare" -- all
    motor-vehicle speeds land in the same bucket. If that distinction
    matters, it needs a different signal entirely (e.g. matching against
    known transit routes), not available from speed alone.
  - Implausible speeds (default > 33 m/s, ~74 mph -- clearly a GPS error,
    not real travel on stadium-district streets) are excluded from
    classification entirely and marked "noise" rather than forced into a
    mode bucket.

DEFAULT THRESHOLDS (SI: meters/second; mph shown for reference)
  walking:  0.3 - 2.2 m/s   (~0.7 - 5.0 mph)   brisk-walk ceiling
  biking:   2.2 - 7.0 m/s   (~5.0 - 15.7 mph)  casual-to-fast cycling
  car:      7.0 - 33 m/s    (~15.7 - 74 mph)   anything faster is noise
  below 0.3 m/s (~0.7 mph) is treated as "stationary," not walking.
  All five thresholds are CLI flags -- override any of them per run.

USAGE
  # Classify every device's full pathing trail in the district export:
  python classify_travel_mode.py --pathing pathing.tsv.gz --out mode_output/

  # Restrict to only the devices that appear in a stadium-trip run's
  # routes.json (much smaller, much faster, and keeps the analysis scoped
  # to people who we've already confirmed actually went to the game):
  python classify_travel_mode.py --pathing pathing.tsv.gz \
      --device-filter output/routes.json --out mode_output/
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Reuse the same battle-tested column-loading and timestamp-parsing logic
# from the main pipeline, so this script handles the real export headers
# (and the epoch-vs-friendly-date ambiguity) the same way pathing_pipeline.py
# already does, instead of re-solving that problem here.
sys.path.insert(0, str(Path(__file__).parent))
from pathing_pipeline import DEFAULT_COLUMN_MAP, load_table, normalize_timestamp  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("classify_travel_mode")

# --------------------------------------------------------------------------- #
# Default thresholds -- see module docstring for the reasoning behind each.
# --------------------------------------------------------------------------- #
DEFAULT_STATIONARY_MAX_MPS = 0.3
DEFAULT_WALK_MAX_MPS = 2.2
DEFAULT_BIKE_MAX_MPS = 7.0
DEFAULT_IMPLAUSIBLE_MPS = 33.0
DEFAULT_MIN_SEGMENT_SECONDS = 10.0
DEFAULT_SMOOTH_WINDOW = 3


def haversine_meters(lat1, lon1, lat2, lon2) -> np.ndarray:
    """Great-circle distance in meters between two (arrays of) points."""
    r = 6_371_000.0
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(a))


def classify_speed(
    speed_mps: float,
    stationary_max: float,
    walk_max: float,
    bike_max: float,
    implausible_max: float,
) -> str:
    if speed_mps > implausible_max:
        return "noise"
    if speed_mps <= stationary_max:
        return "stationary"
    if speed_mps <= walk_max:
        return "walking"
    if speed_mps <= bike_max:
        return "biking"
    return "car"


def smooth_modes(modes: list[str], window: int) -> list[str]:
    """Rolling majority vote over a small window, so one noisy segment
    doesn't flip the mode label on its own. 'noise' and 'stationary' are
    excluded from the vote (they're not really competing "modes" the way
    walk/bike/car are) but still get relabeled if the surrounding real
    modes agree strongly enough."""
    if window <= 1 or len(modes) < 3:
        return modes
    half = window // 2
    out = []
    for i in range(len(modes)):
        lo, hi = max(0, i - half), min(len(modes), i + half + 1)
        neighborhood = [m for m in modes[lo:hi] if m in ("walking", "biking", "car")]
        if not neighborhood:
            out.append(modes[i])
            continue
        counts = pd.Series(neighborhood).value_counts()
        out.append(counts.idxmax())
    return out


def contiguous_runs(device_id: str, segment_records: list[dict]) -> list[dict]:
    """Collapse a device's segment-by-segment mode sequence into contiguous
    runs -- e.g. 'drove for 6.2 min, then walked for 11.4 min' -- which is
    far more directly useful than a flat per-segment table for spotting
    where someone likely parked (the drive->walk transition point)."""
    if not segment_records:
        return []
    runs = []
    current = {
        "device_id": device_id,
        "mode": segment_records[0]["mode"],
        "start_time": segment_records[0]["from_time"],
        "end_time": segment_records[0]["to_time"],
        "start_lat": segment_records[0]["from_lat"],
        "start_lon": segment_records[0]["from_lon"],
        "end_lat": segment_records[0]["to_lat"],
        "end_lon": segment_records[0]["to_lon"],
        "distance_m": segment_records[0]["distance_m"],
    }
    for seg in segment_records[1:]:
        if seg["mode"] == current["mode"]:
            current["end_time"] = seg["to_time"]
            current["end_lat"] = seg["to_lat"]
            current["end_lon"] = seg["to_lon"]
            current["distance_m"] += seg["distance_m"]
        else:
            runs.append(current)
            current = {
                "device_id": device_id,
                "mode": seg["mode"],
                "start_time": seg["from_time"],
                "end_time": seg["to_time"],
                "start_lat": seg["from_lat"],
                "start_lon": seg["from_lon"],
                "end_lat": seg["to_lat"],
                "end_lon": seg["to_lon"],
                "distance_m": seg["distance_m"],
            }
    runs.append(current)
    for run in runs:
        run["duration_s"] = (run["end_time"] - run["start_time"]).total_seconds()
    return runs


def process_device(device_id: str, df: pd.DataFrame, args) -> tuple[list[dict], list[dict]]:
    """Returns (segment_records, run_records) for one device's sorted pings."""
    df = df.sort_values("timestamp").reset_index(drop=True)
    if len(df) < 2:
        return [], []

    lats, lons, times = df["lat"].to_numpy(), df["lon"].to_numpy(), df["timestamp"].to_numpy()
    dist_m = haversine_meters(lats[:-1], lons[:-1], lats[1:], lons[1:])
    dt_s = (times[1:] - times[:-1]) / np.timedelta64(1, "s")

    segments = []
    for i in range(len(dist_m)):
        if dt_s[i] < args.min_segment_seconds:
            continue  # too short to trust a speed estimate from -- skip, don't fabricate
        speed = dist_m[i] / dt_s[i]
        mode = classify_speed(
            speed, args.stationary_max_mps, args.walk_max_mps,
            args.bike_max_mps, args.implausible_mps,
        )
        segments.append({
            "from_time": pd.Timestamp(times[i]), "to_time": pd.Timestamp(times[i + 1]),
            "from_lat": float(lats[i]), "from_lon": float(lons[i]),
            "to_lat": float(lats[i + 1]), "to_lon": float(lons[i + 1]),
            "distance_m": float(dist_m[i]), "duration_s": float(dt_s[i]),
            "speed_mps": float(speed), "mode": mode,
        })

    if not segments:
        return [], []

    smoothed = smooth_modes([s["mode"] for s in segments], args.smooth_window)
    for seg, m in zip(segments, smoothed):
        seg["mode"] = m
    for seg in segments:
        seg["device_id"] = device_id

    runs = contiguous_runs(device_id, segments)
    return segments, runs


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pathing", default=None,
                    help="Path to the pathing export (tsv/csv, .gz OK). If omitted, auto-detects "
                         "the pathing file by searching --pathing-dir for any file whose name "
                         "contains 'pathing' and ends in .gz -- so this works unmodified for any "
                         "export named pathing.tsv.gz, pathing_export.gz, my_pathing_data.gz, etc.")
    p.add_argument("--pathing-dir", default=".",
                    help="Directory to search for a pathing file when --pathing isn't given explicitly.")
    p.add_argument("--center-lat", type=float, default=None,
                    help="Center latitude for the analysis radius. If omitted (along with "
                         "--center-lon), defaults to the geographic centroid of the loaded pings "
                         "themselves -- so this works for any location's export without needing "
                         "to know or hardcode coordinates ahead of time.")
    p.add_argument("--center-lon", type=float, default=None, help="Center longitude (see --center-lat).")
    p.add_argument("--radius-miles", type=float, default=0.5,
                    help="Only pings within this many miles of the center point are analyzed. "
                         "Default: 0.5 miles.")
    p.add_argument("--device-filter", default=None,
                    help="Optional: a routes.json (or a plain text file, one device_id per line) "
                         "to restrict classification to only those devices -- e.g. the devices "
                         "already confirmed as real stadium trips by pathing_pipeline.py, instead "
                         "of every device in the whole district export.")
    p.add_argument("--stationary-max-mps", type=float, default=DEFAULT_STATIONARY_MAX_MPS)
    p.add_argument("--walk-max-mps", type=float, default=DEFAULT_WALK_MAX_MPS)
    p.add_argument("--bike-max-mps", type=float, default=DEFAULT_BIKE_MAX_MPS)
    p.add_argument("--implausible-mps", type=float, default=DEFAULT_IMPLAUSIBLE_MPS,
                    help="Speeds above this are GPS noise, not real travel -- excluded from "
                         "classification rather than forced into the 'car' bucket.")
    p.add_argument("--min-segment-seconds", type=float, default=DEFAULT_MIN_SEGMENT_SECONDS,
                    help="Ping-to-ping gaps shorter than this are skipped -- too short for a "
                         "trustworthy speed estimate given typical GPS position error.")
    p.add_argument("--smooth-window", type=int, default=DEFAULT_SMOOTH_WINDOW,
                    help="Rolling majority-vote window to suppress single-segment noise flicker. "
                         "Set to 1 to see raw, unsmoothed per-segment classification.")
    p.add_argument("--no-map", action="store_true", help="Skip generating mode_map.html.")
    p.add_argument("--max-map-segments", type=int, default=15000,
                    help="Cap on how many segments to draw on the map, for browser performance. "
                         "Randomly sampled if the real count is higher; the map notes this.")
    p.add_argument("--out", default="mode_output/", help="Output directory")
    return p.parse_args(argv)


def find_pathing_file(explicit_path: str | None, search_dir: str) -> str:
    """Auto-detects the pathing export when --pathing isn't given explicitly
    -- any file in search_dir whose name contains 'pathing' (case-insensitive)
    and ends in .gz. Works for pathing.tsv.gz, pathing_export.gz, or any
    other export someone happens to name that way, without hardcoding a
    single expected filename."""
    if explicit_path:
        return explicit_path
    d = Path(search_dir)
    candidates = sorted(
        p for p in d.iterdir()
        if p.is_file() and "pathing" in p.name.lower() and p.name.lower().endswith(".gz")
    )
    if not candidates:
        raise FileNotFoundError(
            f"No file containing 'pathing' with a .gz extension found in {d.resolve()}. "
            f"Pass --pathing path/to/file.gz to specify one explicitly."
        )
    if len(candidates) > 1:
        names = ", ".join(c.name for c in candidates)
        raise FileNotFoundError(
            f"Multiple candidate pathing files found in {d.resolve()}: {names}. "
            f"Pass --pathing path/to/file.gz to pick one explicitly."
        )
    return str(candidates[0])


def filter_to_radius(df: pd.DataFrame, center_lat: float, center_lon: float, radius_miles: float) -> pd.DataFrame:
    dist_m = haversine_meters(df["lat"].to_numpy(), df["lon"].to_numpy(), center_lat, center_lon)
    return df[dist_m <= radius_miles * 1609.344]


def load_device_filter(path: str) -> set[str] | None:
    if path is None:
        return None
    p = Path(path)
    if p.suffix == ".json":
        routes = json.loads(p.read_text())
        return {r["device_id"] for r in routes}
    return {line.strip() for line in p.read_text().splitlines() if line.strip()}


def find_densest_center(df: pd.DataFrame, grid_deg: float = 0.01) -> tuple[float, float]:
    """Finds a sensible auto-center point when none is given explicitly.
    Uses the busiest ~0.7-mile grid cell rather than the simple mean of all
    points -- a real district export can easily contain multiple spatially
    separate clusters (the area of interest, plus scattered unrelated
    activity elsewhere in a wider pull), and a plain average can land in
    the empty space between them, matching neither. This picks the actual
    densest area instead.
    """
    lat_bin = (df["lat"] / grid_deg).round() * grid_deg
    lon_bin = (df["lon"] / grid_deg).round() * grid_deg
    cell_counts = pd.Series(list(zip(lat_bin, lon_bin))).value_counts()
    best_lat_bin, best_lon_bin = cell_counts.idxmax()
    mask = (lat_bin == best_lat_bin) & (lon_bin == best_lon_bin)
    return float(df.loc[mask, "lat"].mean()), float(df.loc[mask, "lon"].mean())


def main(argv=None):
    args = parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    pathing_path = find_pathing_file(args.pathing, args.pathing_dir)
    log.info("Loading pathing export from %s...", pathing_path)
    pathing_cols = DEFAULT_COLUMN_MAP["pathing"]
    optional_cols = DEFAULT_COLUMN_MAP.get("pathing_optional")
    df = load_table(pathing_path, pathing_cols, optional_cols)

    if "timestamp" not in df.columns or df["timestamp"].isna().all():
        log.error(
            "No usable per-ping timestamp column found. Speed-based mode classification "
            "requires real timestamps (e.g. 'Unix Timestamp of Observation Point') -- this "
            "can't be estimated from the 'Time Before' relative-offset column alone."
        )
        sys.exit(1)
    df["timestamp"] = normalize_timestamp(df["timestamp"])

    if args.center_lat is not None and args.center_lon is not None:
        center_lat, center_lon = args.center_lat, args.center_lon
        log.info("Using given center point: (%.5f, %.5f)", center_lat, center_lon)
    else:
        center_lat, center_lon = find_densest_center(df)
        log.info(
            "No --center-lat/--center-lon given -- auto-centering on this export's own "
            "densest ~0.7-mile area: (%.5f, %.5f). Pass explicit coordinates to override.",
            center_lat, center_lon,
        )

    pre_radius = len(df)
    df = filter_to_radius(df, center_lat, center_lon, args.radius_miles)
    log.info(
        "Restricted to %.1f mile radius of (%.5f, %.5f): %d of %d pings kept (%.1f%%).",
        args.radius_miles, center_lat, center_lon, len(df), pre_radius, 100 * len(df) / max(pre_radius, 1),
    )
    if df.empty:
        log.error("No pings remain within the radius -- check the center point and --radius-miles.")
        sys.exit(1)

    device_filter = load_device_filter(args.device_filter)
    if device_filter is not None:
        pre = df["device_id"].nunique()
        df = df[df["device_id"].isin(device_filter)]
        log.info("Restricted to %d of %d devices via --device-filter.", df["device_id"].nunique(), pre)

    total_devices = df["device_id"].nunique()
    log.info("Classifying travel mode for %d devices (%d raw pings)...", total_devices, len(df))

    all_segments, all_runs = [], []
    for i, (device_id, group) in enumerate(df.groupby("device_id")):
        segments, runs = process_device(device_id, group, args)
        all_segments.extend(segments)
        all_runs.extend(runs)
        if (i + 1) % 5000 == 0:
            log.info("  ...%d of %d devices processed", i + 1, total_devices)

    if not all_segments:
        log.error("No segments produced -- check --min-segment-seconds isn't discarding everything.")
        sys.exit(1)

    seg_df = pd.DataFrame(all_segments)
    run_df = pd.DataFrame(all_runs)

    mode_counts = seg_df["mode"].value_counts()
    log.info("Segment mode breakdown: %s", mode_counts.to_dict())

    # Per-device summary: total distance/time per mode, plus a dominant mode
    # by distance -- the single most useful "what mode was this device
    # mostly using" answer.
    summaries = []
    for device_id, g in seg_df.groupby("device_id"):
        by_mode = g.groupby("mode").agg(distance_m=("distance_m", "sum"), duration_s=("duration_s", "sum"))
        real_modes = by_mode.loc[by_mode.index.isin(["walking", "biking", "car"])]
        dominant = real_modes["distance_m"].idxmax() if not real_modes.empty else "unknown"
        row = {"device_id": device_id, "dominant_mode": dominant, "n_segments": len(g)}
        for mode in ("walking", "biking", "car", "stationary", "noise"):
            row[f"{mode}_distance_m"] = float(by_mode["distance_m"].get(mode, 0.0))
            row[f"{mode}_duration_s"] = float(by_mode["duration_s"].get(mode, 0.0))
        summaries.append(row)
    summary_df = pd.DataFrame(summaries)

    seg_df.to_csv(out_dir / "mode_segments.csv", index=False)
    run_df.to_csv(out_dir / "mode_runs.csv", index=False)
    summary_df.to_csv(out_dir / "mode_device_summary.csv", index=False)

    stats = {
        "devices_processed": int(total_devices),
        "total_segments": len(seg_df),
        "segment_mode_counts": mode_counts.to_dict(),
        "dominant_mode_counts": summary_df["dominant_mode"].value_counts().to_dict(),
        "thresholds_mps": {
            "stationary_max": args.stationary_max_mps, "walk_max": args.walk_max_mps,
            "bike_max": args.bike_max_mps, "implausible_max": args.implausible_mps,
        },
        "min_segment_seconds": args.min_segment_seconds,
        "smooth_window": args.smooth_window,
    }
    (out_dir / "mode_stats.json").write_text(json.dumps(stats, indent=2))

    if not args.no_map:
        map_path = out_dir / "mode_map.html"
        build_mode_map_html(seg_df, center_lat, center_lon, args.radius_miles, map_path, args.max_map_segments)
        log.info("Wrote interactive map to %s -- open it in a browser to view.", map_path)

    log.info("Done. Wrote mode_segments.csv (%d rows), mode_runs.csv (%d rows), "
             "mode_device_summary.csv (%d devices) to %s", len(seg_df), len(run_df), len(summary_df), out_dir)
    log.info("Dominant mode across devices: %s", stats["dominant_mode_counts"])


def build_mode_map_html(
    seg_df: pd.DataFrame, center_lat: float, center_lon: float,
    radius_miles: float, out_path: Path, max_segments: int,
) -> None:
    """Writes a self-contained Leaflet map (no server needed -- just open the
    file in a browser) showing every classified segment as a colored line:
    green for walking, blue for biking, red for car, gray for stationary.
    'noise' segments (implausible GPS speed spikes) are excluded from the
    map entirely, same as from classification.

    Works for any location: it's centered on whatever point was used for
    the radius filter (given explicitly or auto-derived from the data's own
    centroid), with a dashed circle drawn at the actual analysis radius so
    it's visually obvious what area the data covers.
    """
    MODE_COLORS = {"walking": "#3ec9a7", "biking": "#4a9eff", "car": "#e85d5d", "stationary": "#7d8394"}
    plot_df = seg_df[seg_df["mode"] != "noise"]

    sampled_note = ""
    if len(plot_df) > max_segments:
        plot_df = plot_df.sample(max_segments, random_state=0)
        sampled_note = f" (randomly sampled {max_segments:,} of {len(seg_df):,} total segments for performance)"

    segments_json = [
        {
            "path": [[round(float(r.from_lat), 6), round(float(r.from_lon), 6)],
                     [round(float(r.to_lat), 6), round(float(r.to_lon), 6)]],
            "mode": r.mode,
            "speed": round(float(r.speed_mps), 2),
        }
        for r in plot_df.itertuples()
    ]
    mode_counts = seg_df["mode"].value_counts().to_dict()

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Travel Mode Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html,body{{margin:0;height:100%;font-family:-apple-system,sans-serif;background:#0c0d10;}}
  #map{{position:absolute;inset:0;}}
  .leaflet-tile-pane{{filter:invert(1) hue-rotate(180deg) brightness(0.92) contrast(0.88) saturate(0.9);}}
  #legend{{position:absolute;top:12px;right:12px;z-index:1000;background:rgba(12,13,16,.9);
    color:#e8eaf0;padding:12px 16px;border-radius:8px;font-size:13px;line-height:1.9;}}
  #legend b{{display:inline-block;width:14px;height:14px;border-radius:3px;margin-right:8px;vertical-align:middle;}}
  #note{{position:absolute;bottom:12px;left:12px;z-index:1000;background:rgba(12,13,16,.9);
    color:#9aa1af;padding:8px 12px;border-radius:6px;font-size:11px;max-width:320px;}}
</style></head>
<body>
<div id="map"></div>
<div id="legend">
  <div><b style="background:{MODE_COLORS['walking']}"></b>Walking ({mode_counts.get('walking',0):,})</div>
  <div><b style="background:{MODE_COLORS['biking']}"></b>Biking ({mode_counts.get('biking',0):,})</div>
  <div><b style="background:{MODE_COLORS['car']}"></b>Car ({mode_counts.get('car',0):,})</div>
  <div><b style="background:{MODE_COLORS['stationary']}"></b>Stationary ({mode_counts.get('stationary',0):,})</div>
</div>
<div id="note">Centered on ({center_lat:.5f}, {center_lon:.5f}), {radius_miles} mile radius{sampled_note}.
Speed-based classification is a heuristic -- hover any segment for its exact speed.</div>
<script>
const SEGMENTS = {json.dumps(segments_json)};
const MODE_COLORS = {json.dumps(MODE_COLORS)};
const map = L.map('map').setView([{center_lat}, {center_lon}], 15);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  attribution: '&copy; OpenStreetMap contributors', maxZoom: 19
}}).addTo(map);
L.circle([{center_lat}, {center_lon}], {{radius: {radius_miles} * 1609.344, color:'#ffb648', weight:1, dashArray:'6,6', fill:false}}).addTo(map);
SEGMENTS.forEach(s => {{
  L.polyline(s.path, {{color: MODE_COLORS[s.mode] || '#888', weight: 3, opacity: 0.7}})
    .bindTooltip(`${{s.mode}} -- ${{s.speed}} m/s`)
    .addTo(map);
}});
</script>
</body></html>"""
    out_path.write_text(html)


if __name__ == "__main__":
    main()
