#!/usr/bin/env python3
"""Standalone street-TYPE (gateway / pedestrian corridor / local street)
analysis for a district-wide pathing export -- one NOT anchored to a single
stadium visit, but to a merged polygon covering many/all buildings in the
district (e.g. a 30-minutes-pre/post extract against every building
footprint in the district, not just Allegiant Stadium).

Why this is a SEPARATE script from pathing_pipeline.py's main(), rather than
just pointing --pathing at the new file: main() builds lot_summary.json,
residency, extrapolation, and routes.json around a specific "parked in a
mapped lot, then confirmed at the stadium" trip definition -- none of which
has a meaningful equivalent for "anyone who visited any building in the
district." A Pin report and visitor_home.tsv.gz exist to confirm/refine
THAT stadium-specific trip, and this file has no matching per-building
Pin-report equivalent (all buildings were merged into one polygon before
the extract). Running it through main() unmodified would either error out
looking for those files, or silently misapply stadium-specific qualification
logic to a population it was never designed for.

What this script actually produces instead: the same "every device's
movement within the district, not gated on which specific building (if
any) they stopped at" street-type classification that pathing_pipeline.py's
--street-match already computes for the ORIGINAL (stadium-anchored) pathing
file (see README.md section 8c) -- applied here to this broader, far less
event-skewed population, so it can be compared side by side against the
event-day one. Reuses pathing_pipeline.py's own loading, filtering,
leg-building, mode-classification, and street-matching functions directly
(no logic is duplicated/re-implemented here) so the two runs stay
consistent and any future fix to those functions benefits both.

Outputs to --out (default: district_wide_output/):
  street_classification.json                        -- per-corridor type (see README 8b/8c)
  street_classification_edge_geometries.json         -- this population's own edge geometries
  street_classification_edge_corridors.json          -- this population's own edge->corridor map
  date_distribution.json                             -- trips/day, event-day vs. non-event-day
                                                          share -- the actual thing this script
                                                          exists to let you check: is this new
                                                          file ALSO skewed toward a handful of
                                                          high-traffic days, or is it the broad,
                                                          representative sample it's meant to be?
  mode_summary.json                                  -- whole-leg and local-mode breakdowns,
                                                          for sanity-checking against the
                                                          event-anchored run's numbers
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

import pathing_pipeline as pp

log = logging.getLogger("district_wide_street_analysis")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pathing", required=True,
                    help="The district-wide pathing export (e.g. a merged-building-polygon "
                         "Pathing X report). Must use the same column names as pathing_pipeline.py's "
                         "DEFAULT_COLUMN_MAP['pathing'] -- confirmed compatible with Azira's "
                         "'Polygon ID' / 'Hashed Device ID' / 'Time before appearance in polygon' / "
                         "'Unix Timestamp of Observation Point' column set.")
    p.add_argument("--column-map", default=None,
                    help="JSON file overriding pathing_pipeline.DEFAULT_COLUMN_MAP['pathing'] / "
                         "['pathing_optional'], if this export's column headers differ.")
    p.add_argument("--district-bbox", type=float, nargs=4, default=list(pp.DEFAULT_DISTRICT_BBOX),
                    metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
                    help="Hard bounding box -- rows outside it are dropped before any other "
                         "processing. Defaults to the same box pathing_pipeline.py uses, so results "
                         "are directly comparable to the event-anchored run.")
    p.add_argument("--street-graph-cache", default="street_graph.graphml",
                    help="Re-uses the SAME cached street network pathing_pipeline.py already built "
                         "for this district, so this script doesn't need to re-hit OSM's Overpass "
                         "API (as long as --district-bbox matches what built that cache).")
    p.add_argument("--street-max-snap-distance-m", type=float, default=60.0)
    p.add_argument("--street-max-bridge-distance-m", type=float, default=400.0)
    p.add_argument("--street-type-max-gap-minutes", type=float, default=20.0,
                    help="Same meaning as pathing_pipeline.py's flag of the same name -- see "
                         "build_district_trajectory_legs.")
    p.add_argument("--biking-as-wheeled", action="store_true",
                   help="Pool 'biking' votes with 'car' votes when classifying street types "
                        "(slow/sparse vehicle traffic near the Strip reads as biking speed). "
                        "Assumption; default off. Raw mode_counts are still reported.")
    p.add_argument("--ped-share-threshold", type=float, default=0.50,
                   help="Non-gateway street is a pedestrian corridor if its ROUTINE (non-event-day) "
                        "walking+stationary share of classified votes is at least this. Default 0.50.")
    p.add_argument("--min-baseline-votes", type=int, default=200,
                   help="Minimum routine votes needed to type a non-gateway street; fewer -> "
                        "'insufficient_data'. Default 200.")
    p.add_argument("--event-shift-threshold", type=float, default=0.10,
                   help="A street is event_pedestrian_corridor if its event-day pedestrian share "
                        "exceeds its routine share by at least this. Default 0.10.")
    p.add_argument("--min-event-votes", type=int, default=100,
                   help="Minimum event-day votes to judge the event shift. Default 100.")
    p.add_argument("--min-leg-pings", type=int, default=4,
                    help="Drop legs with fewer than this many pings before mode classification. "
                         "A 2-ping leg has exactly one speed segment, so its 'mode' is a single "
                         "noisy measurement -- the same artifact that produced false 'biking' "
                         "labels earlier in this project. Default: 4 (at least 3 segments).")
    p.add_argument("--max-legs", type=int, default=150000,
                    help="After the min-leg-pings filter, randomly sample down to this many legs "
                         "(seeded, see --seed) before street snapping. A full district-wide "
                         "extract yields ~1M legs, which took >9 hours to snap with no end in "
                         "sight; street-type votes stabilize long before that. 0 = no cap.")
    p.add_argument("--seed", type=int, default=0,
                    help="Random seed for the --max-legs sample. Re-run with a different seed "
                         "to check that the classification is stable across samples.")
    p.add_argument("--event-calibration", default=None,
                    help="Optional path to an extrapolation_calibration.json from a prior "
                         "pathing_pipeline.py --street-match run, so date_distribution.json can "
                         "flag which of THIS file's busy days land on a known event date too.")
    p.add_argument("--out", default="district_wide_output",
                    help="Output directory (default: district_wide_output/).")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    column_map = dict(pp.DEFAULT_COLUMN_MAP)
    if args.column_map:
        with open(args.column_map) as f:
            overrides = json.load(f)
        column_map.update(overrides)

    log.info("Loading district-wide pathing report from %s...", args.pathing)
    pathing = pp.load_table(args.pathing, column_map["pathing"], column_map.get("pathing_optional"))
    raw_rows = len(pathing)
    raw_devices = pathing["device_id"].nunique()

    if "timestamp" in pathing.columns:
        pathing["timestamp"] = pp.normalize_timestamp(pathing["timestamp"])

    bbox = tuple(args.district_bbox)
    pathing = pp.filter_to_bbox(pathing, bbox)
    log.info(
        "Restricted to district bounding box %s: %d of %d pathing rows kept (%.1f%%).",
        bbox, len(pathing), raw_rows, 100 * len(pathing) / max(raw_rows, 1),
    )
    if pathing.empty:
        log.error("No pathing rows fall inside --district-bbox %s. Double-check the box "
                   "coordinates and that lat/lon columns are mapped correctly.", bbox)
        sys.exit(1)

    stats = pp.PipelineStats()
    pathing = pp.filter_high_emittance(pathing, pp.HIGH_EMITTANCE_PING_CAP, stats)
    for note in stats.notes:
        log.info(note)

    # No staff/employee filtering here, deliberately: this script has no
    # per-building Pin report or stadium-specific polygon to run
    # identify_repeat_visit_staff's "showed up inside the SAME small polygon
    # on 5+ distinct dates" heuristic against (it was built around a single
    # stadium footprint, and a merged-district polygon would just flag
    # anyone who lives or works in the district at all, which is not staff
    # noise for THIS analysis -- "do district residents/workers commute on
    # local streets or gateways" is itself part of what this run is meant
    # to show). If genuine commercial/delivery-fleet noise turns out to be
    # a problem here, filter it separately before passing --pathing in.

    n_devices_in_window = pathing["device_id"].nunique()
    log.info(
        "%d pings from %d distinct devices remain for leg-building (district-wide, not gated "
        "on visiting any particular building).", len(pathing), n_devices_in_window,
    )

    # --- Date-distribution check: the actual question this script exists to answer ---
    date_col = None
    for cand in ("local_date", "departure_date"):
        if cand in pathing.columns:
            date_col = cand
            break
    if date_col is None and "timestamp" in pathing.columns and pathing["timestamp"].notna().any():
        pathing = pathing.copy()
        pathing["_derived_date"] = pathing["timestamp"].dt.date.astype(str)
        date_col = "_derived_date"

    date_distribution = {"available": False}
    if date_col is not None:
        per_device_date = pathing.dropna(subset=[date_col]).drop_duplicates(["device_id", date_col])
        counts = per_device_date[date_col].value_counts().sort_index()
        event_dates = {}
        if args.event_calibration and Path(args.event_calibration).exists():
            calib = json.load(open(args.event_calibration))
            event_dates = {d["date"]: d["name"] for d in calib.get("per_date", [])}
        event_counts = [n for d, n in counts.items() if d in event_dates]
        non_event_counts = [n for d, n in counts.items() if d not in event_dates]
        date_distribution = {
            "available": True,
            "n_distinct_dates": int(len(counts)),
            "total_device_date_rows": int(counts.sum()),
            "top_20_busiest_dates": [
                {"date": d, "devices": int(n), "matched_event": event_dates.get(d)}
                for d, n in counts.sort_values(ascending=False).head(20).items()
            ],
            "event_dates_matched": len(event_counts),
            "mean_devices_per_event_date": round(float(np.mean(event_counts)), 1) if event_counts else None,
            "mean_devices_per_non_event_date": round(float(np.mean(non_event_counts)), 1) if non_event_counts else None,
            "pct_of_device_days_on_event_dates": round(100 * sum(event_counts) / max(counts.sum(), 1), 1) if event_counts else None,
        }
        log.info(
            "Date distribution: %d distinct dates, %d device-date rows. %s",
            date_distribution["n_distinct_dates"], date_distribution["total_device_date_rows"],
            (f"{date_distribution['pct_of_device_days_on_event_dates']}% of device-days fall on "
             f"a known event date (mean {date_distribution['mean_devices_per_event_date']}/event-date "
             f"vs {date_distribution['mean_devices_per_non_event_date']}/other date) -- compare this "
             f"to the original run's 42.4% to see whether this broader extract is actually less "
             f"event-skewed." if event_counts else
             "No --event-calibration given, or no dates in it matched this file -- pass "
             "output/extrapolation_calibration.json from the original run to get an event-day "
             "comparison here."),
        )
    else:
        log.warning("No usable date column found -- skipping date_distribution.json. This is the "
                     "single most important sanity check for this file (is it ALSO skewed toward a "
                     "handful of busy days?), so please check manually if this triggers.")
    (out_dir / "date_distribution.json").write_text(json.dumps(date_distribution, indent=2, default=str))

    # --- Ping-cadence diagnostic: does GPS sampling density change over time? ---
    # Sparse pings make travel look slower (see README 8c), so if cadence shifts month
    # to month, month-to-month swings in a corridor's mode mix may be a data artifact.
    try:
        if "timestamp" in pathing.columns and pathing["timestamp"].notna().any():
            cad = pathing[["device_id", "timestamp"]].dropna().sort_values(["device_id", "timestamp"])
            secs = cad["timestamp"].astype("datetime64[ns, UTC]" if cad["timestamp"].dt.tz is not None else "datetime64[ns]")
            secs = (secs - secs.min()).dt.total_seconds().to_numpy()
            same_dev = (cad["device_id"].to_numpy()[1:] == cad["device_id"].to_numpy()[:-1])
            gaps = np.diff(secs)
            ok = same_dev & (gaps > 0) & (gaps <= args.street_type_max_gap_minutes * 60)
            local_ts = cad["timestamp"].dt.tz_convert(pp.LOCAL_TZ) if cad["timestamp"].dt.tz is not None else cad["timestamp"]
            months = local_ts.dt.strftime("%Y-%m").to_numpy()[1:]
            gdf = pd.DataFrame({"month": months[ok], "gap": gaps[ok]})
            per_month = {}
            for m, g in gdf.groupby("month"):
                v = g["gap"].to_numpy()
                per_month[m] = {
                    "n_gaps": int(len(v)), "median_gap_s": round(float(np.median(v)), 1),
                    "p75_gap_s": round(float(np.percentile(v, 75)), 1),
                    "share_gap_le_15s": round(float((v <= 15).mean()), 3),
                    "share_gap_gt_60s": round(float((v > 60).mean()), 3),
                    "share_gap_gt_300s": round(float((v > 300).mean()), 3),
                }
            ping_months = pd.Series(local_ts.dt.strftime("%Y-%m").to_numpy())
            devs = cad["device_id"].reset_index(drop=True)
            for m, n in ping_months.value_counts().items():
                if m in per_month:
                    per_month[m]["n_pings"] = int(n)
                    per_month[m]["n_devices"] = int(devs[ping_months == m].nunique())
            (out_dir / "ping_cadence_by_month.json").write_text(json.dumps(dict(sorted(per_month.items())), indent=1))
            log.info("Ping cadence by month (median gap s / share >60s):")
            for m, s in sorted(per_month.items()):
                log.info("  %s: median %.0fs, >60s %.0f%%, %d pings", m, s["median_gap_s"],
                          100 * s["share_gap_gt_60s"], s.get("n_pings", 0))
    except Exception as exc:  # diagnostic only -- never block the main analysis
        log.warning("Ping-cadence diagnostic failed (%s); continuing.", exc)

    # --- Leg-building, mode classification (whole-leg + local) ---
    log.info("Building travel legs (max gap %.1f min)...", args.street_type_max_gap_minutes)
    legs = pp.build_district_trajectory_legs(pathing, max_gap_minutes=args.street_type_max_gap_minutes)
    n_leg_devices = len({l["device_id"] for l in legs})
    log.info("%d travel leg(s) from %d distinct device(s) before filtering.", len(legs), n_leg_devices)

    n_before = len(legs)
    legs = [l for l in legs if len(l["path"]) >= args.min_leg_pings]
    log.info("Dropped %d leg(s) with fewer than %d pings (too little speed evidence to trust a mode); %d remain.",
              n_before - len(legs), args.min_leg_pings, len(legs))

    if args.max_legs and len(legs) > args.max_legs:
        rng = np.random.default_rng(args.seed)
        keep = np.sort(rng.choice(len(legs), size=args.max_legs, replace=False))
        legs = [legs[i] for i in keep]
        log.info("Randomly sampled %d leg(s) (seed %d) for street snapping -- pass --max-legs 0 to "
                  "use all of them, but expect a very long run.", len(legs), args.seed)

    event_dates_for_slices = None
    if args.event_calibration and Path(args.event_calibration).exists():
        _calib = json.load(open(args.event_calibration))
        event_dates_for_slices = {x["date"]: x["name"] for x in _calib.get("per_date", [])}
    def _leg_tags(leg):
        t = pp.leg_slice_tags(leg["path_t"][0], event_dates_for_slices)
        base = "event" if "event" in t else ("non_event" if "non_event" in t else "all")
        month = next(x for x in t if x.startswith("date:"))[5:12]
        return [base, f"{base}|{month}"]
    leg_tags = [_leg_tags(l) for l in legs]
    slice_counts: dict = {}

    whole_leg_modes = []
    local_modes = []
    for i, leg in enumerate(legs):
        mr = pp.classify_route_mode(leg)
        whole_leg_modes.append(mr["mode"] if mr["mode_confidence"] == "ok" else None)
        local_modes.append(pp.classify_route_local_modes(leg))
        if (i + 1) % 25000 == 0:
            log.info("  mode-classified %d / %d legs...", i + 1, len(legs))

    mode_summary = {
        "whole_leg_mode_counts": dict(Counter(m or "unknown" for m in whole_leg_modes)),
        "note": "whole_leg_mode_counts is for sanity-checking against the event-anchored run's "
                "numbers only -- actual street-type classification below uses LOCAL (per-ping) "
                "modes, not this whole-leg figure (see README 8c).",
    }
    (out_dir / "mode_summary.json").write_text(json.dumps(mode_summary, indent=2))
    log.info("Whole-leg mode breakdown (diagnostic only): %s", mode_summary["whole_leg_mode_counts"])

    # --- Street-network matching + classification ---
    log.info("Loading/caching street network for bbox %s...", bbox)
    G = pp.get_street_graph(bbox, args.street_graph_cache)

    log.info("Snapping %d legs onto the street network (this can take a while for a large, "
              "district-wide device set)...", len(legs))
    (
        _per_leg_edges, edge_geometries, edge_corridors, match_stats,
        corridor_mode_counts, edge_tags, edge_names,
    ) = pp.match_routes_to_street_segments(
        legs, G, max_snap_distance_m=args.street_max_snap_distance_m,
        max_bridge_meters=args.street_max_bridge_distance_m,
        route_modes=whole_leg_modes, route_local_modes=local_modes,
        route_slice_tags=leg_tags, corridor_slice_counts_out=slice_counts,
    )
    log.info(
        "Street matching: %.1f%% of pings matched within %.0fm (%d distinct segments touched, "
        "%d corridors after merging).",
        match_stats["pct_matched"], args.street_max_snap_distance_m,
        len(edge_geometries), len(set(edge_corridors.values())),
    )
    (out_dir / "street_classification_edge_geometries.json").write_text(json.dumps(edge_geometries))
    (out_dir / "street_classification_edge_corridors.json").write_text(json.dumps(edge_corridors))

    street_classification = pp.classify_street_types(
        edge_corridors, edge_names, edge_tags, edge_geometries, corridor_mode_counts, lot_polygons=None,
        biking_as_wheeled=args.biking_as_wheeled,
    )
    (out_dir / "street_classification.json").write_text(json.dumps(street_classification, indent=2))

    refined = pp.refine_street_types(
        street_classification, slice_counts,
        ped_share_threshold=args.ped_share_threshold, min_baseline_votes=args.min_baseline_votes,
        event_shift_threshold=args.event_shift_threshold, min_event_votes=args.min_event_votes,
    )
    (out_dir / "street_classification_refined.json").write_text(json.dumps(refined, indent=1))
    refined_counts = Counter(v["refined_type"] for v in refined.values())
    log.info("Refined street types (gateways kept; others from routine baseline, event days flagged): %s. "
              "Written to %s.%s", dict(refined_counts), out_dir / "street_classification_refined.json",
              "" if event_dates_for_slices else " (No --event-calibration: no event-day flag.)")

    type_counts = Counter(v["type"] for v in street_classification.values())
    log.info(
        "Street-type classification (district-wide, non-event-anchored population): %d gateway(s), "
        "%d pedestrian corridor(s), %d local street(s), %d unclassified, %d private/excluded. "
        "Written to %s.",
        type_counts.get("gateway", 0), type_counts.get("pedestrian_corridor", 0),
        type_counts.get("local_street", 0), type_counts.get("unclassified", 0),
        type_counts.get("excluded_private", 0), out_dir / "street_classification.json",
    )
    log.info("Done. Compare %s against the event-anchored run's street_classification.json -- "
              "a corridor that's 'gateway' here but 'pedestrian_corridor' there (or vice versa) "
              "is exactly the routine-vs-event-day difference this extract exists to surface.",
              out_dir / "street_classification.json")


if __name__ == "__main__":
    main()
