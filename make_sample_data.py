"""Generate synthetic pathing/pin/home data using the REAL parking-lot
polygon centroids (from parking_lots.geojson) and the REAL Allegiant Stadium
footprint, so the spatial point-in-polygon lot matching can be tested
end-to-end against realistic geometry.
"""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, ".")
from pathing_pipeline import load_geojson_polygons, DEFAULT_STADIUM_POLYGON

rng = np.random.default_rng(42)

STADIUM_VISIT_EPOCH = 1755302400

stadium_ring = DEFAULT_STADIUM_POLYGON
stadium = (
    sum(p[1] for p in stadium_ring) / len(stadium_ring),
    sum(p[0] for p in stadium_ring) / len(stadium_ring),
)

all_lots = load_geojson_polygons("data/parking_lots.geojson")
chosen_lot_names = ["Lot A", "Lot B", "Lot C", "Residence Inn"]  # mix of real lots + a hotel lot
lots = {}
for name in chosen_lot_names:
    ring = all_lots[name]
    lots[name] = (
        sum(p[1] for p in ring) / len(ring),  # lat
        sum(p[0] for p in ring) / len(ring),  # lon
    )

downtown_lv = (36.1699, -115.1398)
la_home = (34.0522, -118.2437)

pathing_rows, pin_rows, home_rows = [], [], []
device_n = 0

for lot_name, (lot_lat, lot_lon) in lots.items():
    n_devices = rng.integers(45, 70)
    for i in range(n_devices):
        device_n += 1
        did = f"dev_{device_n:05d}"
        is_local = rng.random() < 0.55
        arrives = rng.random() < 0.85

        walk_in_minutes = int(rng.integers(5, 25))
        walk_out_minutes = int(rng.integers(5, 30))
        route_bias = rng.choice([-1, 0, 1]) * 0.0006
        n_steps = 6

        for step in range(n_steps + 1):
            frac = step / n_steps
            # start a few meters inside the lot polygon (jittered near centroid)
            start_lat = lot_lat + rng.normal(0, 0.00015)
            start_lon = lot_lon + rng.normal(0, 0.00015)
            lat = start_lat + frac * (stadium[0] - start_lat) + route_bias * np.sin(frac * np.pi)
            lon = start_lon + frac * (stadium[1] - start_lon) + route_bias * np.cos(frac * np.pi)
            seconds_before = int((1 - frac) * walk_in_minutes * 60)
            ts = STADIUM_VISIT_EPOCH - seconds_before
            # Polygon Name left mostly blank/POI-context noise; the pipeline's
            # spatial lot matcher (not this field) is what should identify the
            # true lot. We do still tag actual stadium arrival for the fallback path.
            polygon = "Allegiant Stadium" if frac >= 0.999 else ""
            pathing_rows.append({
                "Study Polygon": "Allegiant Stadium",
                "Hashed Device ID": did,
                "Lat of Observation Point": round(lat, 6),
                "Lon of Observation Point": round(lon, 6),
                "Time before appearance in polygon": seconds_before,
                "Unix Timestamp of Observation Point": ts,
                "Local Date": "2026-08-15",
                "Local Time of Day": "18:00:00",
                "Local Day of Week": "Saturday",
                "Local Timezone of Observation Point": "America/Los_Angeles",
                "Polygon Id": "",
                "Polygon Name": polygon,
                "Category": "Stadium" if polygon else "",
            })

        if arrives:
            pin_rows.append({
                "Polygon ID": "",
                "Hashed Device ID": did,
                "Lat of Visit": stadium[0] + rng.normal(0, 0.0001),
                "Lon of Visit": stadium[1] + rng.normal(0, 0.0001),
                "Unix Timestamp of Visit": STADIUM_VISIT_EPOCH,
                "Date": "2026-08-15", "Time of Day": "18:00:00", "Day of Week": "Saturday",
                "Time Zone": "America/Los_Angeles",
            })
            if rng.random() < 0.9:
                for step in range(n_steps + 1):
                    frac = step / n_steps
                    end_lat = lot_lat + rng.normal(0, 0.00015)
                    end_lon = lot_lon + rng.normal(0, 0.00015)
                    lat = stadium[0] + frac * (end_lat - stadium[0]) - route_bias * np.sin(frac * np.pi)
                    lon = stadium[1] + frac * (end_lon - stadium[1]) - route_bias * np.cos(frac * np.pi)
                    seconds_after = int(frac * walk_out_minutes * 60)
                    ts = STADIUM_VISIT_EPOCH + seconds_after
                    polygon = ""
                    pathing_rows.append({
                        "Study Polygon": "Allegiant Stadium",
                        "Hashed Device ID": did,
                        "Lat of Observation Point": round(lat, 6),
                        "Lon of Observation Point": round(lon, 6),
                        "Time before appearance in polygon": -seconds_after,
                        "Unix Timestamp of Observation Point": ts,
                        "Local Date": "2026-08-15",
                        "Local Time of Day": "21:30:00",
                        "Local Day of Week": "Saturday",
                        "Local Timezone of Observation Point": "America/Los_Angeles",
                        "Polygon Id": "",
                        "Polygon Name": polygon,
                        "Category": "",
                    })

        base = downtown_lv if is_local else la_home
        home_rows.append({
            "Hashed Ubermedia Id": did,
            "Polygon Name": "Allegiant Stadium",
            "Visit Timestamp": STADIUM_VISIT_EPOCH,
            "Common Evening Lat": base[0] + rng.normal(0, 0.05),
            "Common Evening Long": base[1] + rng.normal(0, 0.05),
            "Date": "2026-08-15", "Time": "18:00:00", "Day of Week": "Saturday",
            "Time Zone": "America/Los_Angeles",
        })

# noise: devices near, but outside, mapped lot polygons/stadium (should be excluded)
district_buildings = [(36.0895, -115.1810), (36.0930, -115.1860)]
for i, (blat, blon) in enumerate(district_buildings):
    for _ in range(20):
        pin_rows.append({
            "Polygon ID": "",
            "Hashed Device ID": f"dev_noise_{i}_{_}",
            "Lat of Visit": blat + rng.normal(0, 0.0005),
            "Lon of Visit": blon + rng.normal(0, 0.0005),
            "Unix Timestamp of Visit": STADIUM_VISIT_EPOCH,
            "Date": "2026-08-15", "Time of Day": "18:00:00", "Day of Week": "Saturday",
            "Time Zone": "America/Los_Angeles",
        })

pathing_df = pd.DataFrame(pathing_rows)
pin_df = pd.DataFrame(pin_rows)
home_df = pd.DataFrame(home_rows)

pathing_df.to_csv("data/pathing.tsv.gz", sep="\t", index=False, compression="gzip")
pin_df.to_csv("data/pin_report.tsv.gz", sep="\t", index=False, compression="gzip")
home_df.to_csv("data/visitor_home.tsv.gz", sep="\t", index=False, compression="gzip")

print(f"pathing rows: {len(pathing_df)}  pin rows: {len(pin_df)}  home rows: {len(home_df)}  devices: {device_n}")
print("lots used:", list(lots.keys()))
print("stadium centroid:", stadium)
