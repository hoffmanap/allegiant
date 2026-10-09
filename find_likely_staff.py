#!/usr/bin/env python3
"""
find_likely_staff.py
=====================
Tests whether "local" devices in the pathing sample are overrepresented
because a meaningful share are actually stadium staff/workers, not fans --
which would explain a real pattern already observed: this project's
CEL-based local/visitor split disagrees with the Stadium Authority's
official ticketing-based out-of-town percentages, and the disagreement does
NOT shrink with sample size (ruling out ordinary sampling noise as the
explanation).

WHY THIS RUNS ON RAW PATHING DATA, NOT routes.json
  routes.json only contains devices that completed the full lot-to-stadium
  confirmation pipeline through one of the 21 mapped PUBLIC lots. A stadium
  employee who parks in a dedicated staff lot, or badges in through a
  non-public entrance, would never appear there -- not because they aren't
  real, but because the confirmation logic was built around public fan
  behavior. This script instead looks for the repeat-visit signature
  directly in the raw district pings, before that filtering happens.

THE CORE IDEA
  A genuine fan attends one event (rarely a handful). A stadium worker shows
  up on dozens of different calendar dates across a year. Count how many
  DISTINCT DATES each device appears anywhere in the district bounding box,
  and flag devices above a threshold as likely staff -- then check whether
  those devices skew local relative to the district population as a whole.

CAVEAT (read before trusting the output)
  Mobile Advertising IDs can reset over time. A real staff member's device
  could show up under a different hashed ID every few months, which would
  make this UNDERCOUNT true staff, not overcount them -- so a low count of
  flagged devices here does not disprove the worker hypothesis; it's a
  lower bound. Correlate the output against a real staff roster or shift
  schedule if one is available, rather than treating this as definitive.

USAGE
  python find_likely_staff.py --pathing pathing.tsv.gz --home visitor_home.tsv.gz \
      --min-distinct-dates 5 --out staff_analysis/
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from pathing_pipeline import (  # noqa: E402
    DEFAULT_COLUMN_MAP, DEFAULT_DISTRICT_BBOX, DEFAULT_STADIUM_POLYGON,
    LAS_VEGAS_METRO_CENTER, LAS_VEGAS_METRO_RADIUS_MILES, classify_local_visitor,
    filter_to_bbox, filter_to_polygon, haversine_miles, load_table, normalize_timestamp,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("find_likely_staff")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pathing", default="pathing.tsv.gz")
    p.add_argument("--home", default="visitor_home.tsv.gz")
    p.add_argument("--min-distinct-dates", type=int, default=5,
                    help="Devices appearing in the district on at least this many distinct "
                         "calendar dates are flagged as likely staff/repeat-visitors. A real "
                         "fan showing up 5+ separate times across the year is possible but "
                         "uncommon; tune this up or down based on what the distribution below "
                         "actually looks like for your data before trusting the flag.")
    p.add_argument("--district-bbox", type=float, nargs=4, default=list(DEFAULT_DISTRICT_BBOX))
    p.add_argument("--out", default="staff_analysis/")
    return p.parse_args(argv)


KNOWN_STADIUM_WORKFORCE = 5785  # Q4 2025 Stadium Authority filing: 5,272 employed + 513 open
                                 # positions. Used as a real sanity check -- if a "likely staff"
                                 # count comes out multiples higher than this, the test is
                                 # catching something broader than stadium employment.


def analyze_frequency(pathing: pd.DataFrame, residency_lookup: dict, min_distinct_dates: int, label: str) -> dict:
    """Runs the distinct-date frequency test on whatever subset of pathing
    rows it's given, and returns a result dict. Called twice: once on the
    full district (broad, catches everything nearby -- hotels, other
    businesses, regular neighborhood traffic, not just stadium staff), and
    once restricted to pings actually inside the stadium footprint (much
    more specific to people who physically entered the building)."""
    dates_per_device = pathing.groupby("device_id")["local_date"].nunique()
    dist = dates_per_device.value_counts().sort_index()

    log.info("[%s] Distinct-date distribution (first 15 buckets):", label)
    for n_dates, count in dist.head(15).items():
        log.info("  %d distinct date(s): %d devices", n_dates, count)
    if len(dist) > 15:
        log.info("  ... up to %d distinct dates for the most frequent device", dist.index.max())

    likely_staff = dates_per_device[dates_per_device >= min_distinct_dates].index
    log.info(
        "[%s] %d devices (of %d total) appear on %d+ distinct dates -- flagged as likely staff.",
        label, len(likely_staff), len(dates_per_device), min_distinct_dates,
    )

    staff_residency = pd.Series([residency_lookup.get(d, "unknown") for d in likely_staff]).value_counts()
    all_residency = pd.Series([residency_lookup.get(d, "unknown") for d in dates_per_device.index]).value_counts()

    def pct_local(counts):
        total = counts.sum()
        return round(100 * counts.get("local", 0) / total, 1) if total else None

    flagged_vs_workforce = round(len(likely_staff) / KNOWN_STADIUM_WORKFORCE, 2)

    return {
        "scope": label,
        "total_devices": int(len(dates_per_device)),
        "min_distinct_dates_threshold": min_distinct_dates,
        "likely_staff_device_count": int(len(likely_staff)),
        "likely_staff_pct_of_total": round(100 * len(likely_staff) / max(len(dates_per_device), 1), 2),
        "flagged_count_vs_known_workforce_of_5785": flagged_vs_workforce,
        "plausibility_note": (
            f"Flagged count is {flagged_vs_workforce}x the real Q4 2025 stadium workforce "
            f"(5,785 total positions, incl. unfilled). {'This is IMPLAUSIBLY high for stadium staff specifically -- likely catching broader district traffic (hotels, other businesses, regular neighborhood activity), not just employees.' if flagged_vs_workforce > 1.5 else 'This is in a plausible range for stadium staff.'}"
        ),
        "pct_local_among_likely_staff": pct_local(staff_residency),
        "pct_local_among_all_devices": pct_local(all_residency),
        "distinct_date_distribution": {int(k): int(v) for k, v in dist.items()},
        "likely_staff_devices": likely_staff,
        "dates_per_device": dates_per_device,
    }


def main(argv=None):
    args = parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("Loading pathing export...")
    pathing = load_table(args.pathing, DEFAULT_COLUMN_MAP["pathing"], DEFAULT_COLUMN_MAP.get("pathing_optional"))
    pathing = filter_to_bbox(pathing, tuple(args.district_bbox))
    if "timestamp" not in pathing.columns or pathing["timestamp"].isna().all():
        log.error("No usable per-ping timestamp column -- this analysis needs real timestamps.")
        sys.exit(1)
    pathing["timestamp"] = normalize_timestamp(pathing["timestamp"])
    pathing["local_date"] = pathing["timestamp"].dt.tz_convert("America/Los_Angeles").dt.date

    log.info("Loading visitor home report for residency classification...")
    home = load_table(args.home, DEFAULT_COLUMN_MAP["home"])
    residency = classify_local_visitor(home, LAS_VEGAS_METRO_CENTER, LAS_VEGAS_METRO_RADIUS_MILES)
    residency_lookup = residency.set_index("device_id")["residency"].to_dict()

    log.info("=" * 70)
    log.info("PASS 1: District-wide (broad -- includes hotels, other businesses, regular traffic)")
    log.info("=" * 70)
    broad = analyze_frequency(pathing, residency_lookup, args.min_distinct_dates, "district-wide")

    log.info("=" * 70)
    log.info("PASS 2: Stadium-footprint-only (specific -- only pings actually inside the building)")
    log.info("=" * 70)
    pathing_stadium = filter_to_polygon(pathing, DEFAULT_STADIUM_POLYGON)
    log.info("%d of %d district pings actually fall inside the stadium footprint.", len(pathing_stadium), len(pathing))
    stadium = analyze_frequency(pathing_stadium, residency_lookup, args.min_distinct_dates, "stadium-footprint")

    for result in (broad, stadium):
        pd.DataFrame({
            "device_id": result["likely_staff_devices"],
            "distinct_dates": result["dates_per_device"].loc[result["likely_staff_devices"]].values,
            "residency": [residency_lookup.get(d, "unknown") for d in result["likely_staff_devices"]],
        }).to_csv(out_dir / f"likely_staff_devices_{result['scope']}.csv", index=False)
        result.pop("likely_staff_devices")
        result.pop("dates_per_device")

    combined = {
        "known_stadium_workforce_q4_2025": KNOWN_STADIUM_WORKFORCE,
        "district_wide": broad,
        "stadium_footprint_only": stadium,
        "caveat": (
            "Both passes are a LOWER BOUND on true staff presence -- MAID resets over time "
            "mean a real staff member's device can change hashed ID across the year, which "
            "would make repeat visits invisible to this method. The stadium-footprint pass is "
            "the more specific test; the district-wide pass is included for comparison and to "
            "show how much broader 'frequent district visitor' is than 'stadium employee.'"
        ),
    }
    (out_dir / "staff_analysis.json").write_text(json.dumps(combined, indent=2))

    log.info("=" * 70)
    log.info("SUMMARY")
    log.info("District-wide:      %d flagged (%.1fx known workforce) | %% local: %s%% vs %s%% baseline",
              broad["likely_staff_device_count"], broad["flagged_count_vs_known_workforce_of_5785"],
              broad["pct_local_among_likely_staff"], broad["pct_local_among_all_devices"])
    log.info("Stadium-footprint:  %d flagged (%.1fx known workforce) | %% local: %s%% vs %s%% baseline",
              stadium["likely_staff_device_count"], stadium["flagged_count_vs_known_workforce_of_5785"],
              stadium["pct_local_among_likely_staff"], stadium["pct_local_among_all_devices"])
    log.info("Wrote staff_analysis.json and two likely_staff_devices_*.csv files to %s", out_dir)


if __name__ == "__main__":
    main()
