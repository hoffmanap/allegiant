"""Re-type streets from existing district-wide outputs -- no heavy re-run needed.

Applies the final role logic (pp.assign_street_roles):
  arterial (OSM motorway/trunk/primary/secondary) -> gateway
  non-arterial with heavy pedestrian use (carries >= 5% of walking lot-to-stadium trips AND
    >= 300 ordinary-day pedestrian observations)  -> pedestrian_corridor
  everything else -> local_street

Usage (from the project folder):
  python retype_streets.py --district-dir district_wide_output_final2 ^
      --routes output\\routes.json --edge-corridors output\\edge_corridors.json
Writes street_classification_refined.json in the district folder (the previous file is
kept as street_classification_refined_prev.json). Safe to run more than once.
"""
import argparse, json, shutil
from pathlib import Path
import pathing_pipeline as pp

ap = argparse.ArgumentParser()
ap.add_argument("--district-dir", required=True)
ap.add_argument("--routes", required=True, help="routes.json from the stadium-anchored run")
ap.add_argument("--edge-corridors", required=True, help="edge_corridors.json from the same run")
ap.add_argument("--min-walkin-share", type=float, default=0.05)
ap.add_argument("--min-ped-observations", type=int, default=300)
ap.add_argument("--min-baseline-votes", type=int, default=200)
a = ap.parse_args()

d = Path(a.district_dir)
cls = json.load(open(d / "street_classification.json"))
ref_path = d / "street_classification_refined.json"
refined = json.load(open(ref_path))
counts, total = pp.compute_walkin_corridor_counts(json.load(open(a.routes)), json.load(open(a.edge_corridors)))
new = pp.assign_street_roles(cls, refined, counts, total, a.min_walkin_share,
                             a.min_ped_observations, a.min_baseline_votes)
shutil.copy(ref_path, d / "street_classification_refined_prev.json")
ref_path.write_text(json.dumps(new, indent=1))
from collections import Counter
print("walking lot trips:", total)
print(dict(Counter(v["refined_type"] for v in new.values())))
for k in ("gateway", "pedestrian_corridor"):
    print(k + ":", ", ".join(sorted(n for n, v in new.items() if v["refined_type"] == k)))
print("arterials that are also major walk-in routes:",
      ", ".join(sorted(n for n, v in new.items() if v["also_walkin_route"])) or "none")
