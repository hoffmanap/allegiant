"""Rebuild index.html from index_template.html.

Volume / Local-vs-Visitor data come from output/ (the event-anchored, lot-qualified run).
Street Type data come from district_final/ (the district-wide run from
district_wide_street_analysis.py), with street_classification_refined.json merged in:
the refined type replaces the base type, and the routine/event pedestrian shares are
attached to each corridor for the tooltip.
"""
import argparse, json, os

def load_if_exists(path, default):
    return json.load(open(path)) if os.path.exists(path) else default

ap = argparse.ArgumentParser()
ap.add_argument("--output-dir", default="output", help="event-anchored run folder (Volume / Local vs. Visitor); the repo keeps this in data/")
ap.add_argument("--district-dir", default="district_final", help="district-wide run folder (Street Type maps)")
_a = ap.parse_args()
OUT, DW = _a.output_dir, _a.district_dir
data = {
    "flow_lines": json.load(open(f"{OUT}/flow_lines.json")),
    "lot_centroids": json.load(open(f"{OUT}/lot_centroids.json")),
    "lot_summary": json.load(open(f"{OUT}/lot_summary.json")),
    "routes": json.load(open(f"{OUT}/routes.json")),
}
event_meta = json.load(open(f"{OUT}/event_meta.json"))
data["event_meta"] = event_meta

classification = load_if_exists(f"{DW}/street_classification.json", {})
refined = load_if_exists(f"{DW}/street_classification_refined.json", {})
for corridor, r in refined.items():
    c = classification.get(corridor)
    if not c:
        continue
    c["base_type"] = c["type"]
    c["type"] = r["refined_type"]
    if r["refined_type"] == "insufficient_data":
        c["type_confidence"] = "low"
    for k in ("baseline_ped_share", "baseline_votes", "event_ped_share", "event_votes",
              "month_matched_baseline_ped_share", "event_shift", "event_pedestrian_flag"):
        c[k] = r.get(k)

subs = {
    "__DASHBOARD_DATA__": data,
    "__EVENT_META__": event_meta,
    "__EDGE_GEOMETRIES__": load_if_exists(f"{OUT}/edge_geometries.json", {}),
    "__EDGE_CORRIDORS__": load_if_exists(f"{OUT}/edge_corridors.json", {}),
    "__EXTRAPOLATION_CALIBRATION__": load_if_exists(f"{OUT}/extrapolation_calibration.json", {}),
    "__STREET_CLASSIFICATION__": classification,
    "__STREET_TYPE_EDGE_GEOMETRIES__": load_if_exists(f"{DW}/street_classification_edge_geometries.json", {}),
    "__STREET_TYPE_EDGE_CORRIDORS__": load_if_exists(f"{DW}/street_classification_edge_corridors.json", {}),
}
html = open("index_template.html").read()
for k, v in subs.items():
    html = html.replace(k, json.dumps(v))
assert "__" not in "".join(p for p in subs if p in html), "unreplaced placeholder"
open("index.html", "w").write(html)
json.dump(data, open(f"{OUT}/dashboard_data.json", "w"))
print("index.html written:", len(html) // 1024, "KB;",
      sum(1 for c in classification.values() if c["type"] != "excluded_private"), "public corridors typed")
