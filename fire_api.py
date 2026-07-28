import csv
import io
import json
import math
import os

import requests

FIRMS_KEY  = os.environ.get("FIRMS_KEY", "da9704de0ca790e6e26885aa00e960e8")
CALOES_URL = (
    "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services"
    "/CA_EVACUATIONS_CalOESHosted_view/FeatureServer/0/query"
)

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "wildfire-evacuation-checker/1.0"


def haversine_miles(lat1, lon1, lat2, lon2):
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def bbox(lat, lon, miles):
    d = miles / 69.0
    return f"{lon-d},{lat-d},{lon+d},{lat+d}"


def geocode(query):
    try:
        r = SESSION.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1,
                    "countrycodes": "us", "addressdetails": "1"},
            timeout=10,
        )
        hits = r.json()
        if not hits:
            return None
        hit, addr = hits[0], hits[0].get("address", {})
        place = (addr.get("city") or addr.get("town") or addr.get("village")
                 or addr.get("hamlet") or addr.get("county") or "")
        name = f"{place}, {addr.get('state', '')}".strip(", ") or query
        return float(hit["lat"]), float(hit["lon"]), name
    except Exception:
        return None


def caloes_at_point(lat, lon):
    geom = json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}})
    try:
        feats = SESSION.get(CALOES_URL, params={
            "geometry": geom, "geometryType": "esriGeometryPoint",
            "spatialRel": "esriSpatialRelIntersects", "where": "1=1",
            "outFields": "STATUS,COUNTY,CITY", "returnGeometry": "false", "f": "json",
        }, timeout=10).json().get("features", [])
        return [f["attributes"] for f in feats]
    except Exception:
        return []


def caloes_nearby(lat, lon, miles):
    try:
        feats = SESSION.get(CALOES_URL, params={
            "geometry": bbox(lat, lon, miles), "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects", "where": "1=1",
            "outFields": "STATUS,COUNTY", "returnCentroid": "true",
            "returnGeometry": "false", "outSR": "4326", "f": "json",
        }, timeout=10).json().get("features", [])
    except Exception:
        return []
    zones = []
    for f in feats:
        c = f.get("centroid") or {}
        if c.get("x") and c.get("y"):
            zones.append({**f["attributes"],
                          "distance_miles": round(haversine_miles(lat, lon, c["y"], c["x"]), 1)})
    return sorted(zones, key=lambda z: z["distance_miles"])


def firms_nearby(lat, lon, miles):
    try:
        resp = SESSION.get(
            f"https://firms.modaps.eosdis.nasa.gov/api/area/csv"
            f"/{FIRMS_KEY}/VIIRS_SNPP_NRT/{bbox(lat, lon, miles)}/1",
            timeout=12,
        )
        fires = []
        for row in csv.DictReader(io.StringIO(resp.text)):
            try:
                d = haversine_miles(lat, lon, float(row["latitude"]), float(row["longitude"]))
                fires.append({"distance_miles": round(d, 1)})
            except (ValueError, KeyError):
                continue
        return sorted(fires, key=lambda x: x["distance_miles"])
    except Exception:
        return []


def check(lat, lon, radius):
    at_point = caloes_at_point(lat, lon)
    nearby   = caloes_nearby(lat, lon, radius)
    hotspots = firms_nearby(lat, lon, radius)

    orders   = [z for z in at_point if z["STATUS"] == "Evacuation Order"]
    warnings = [z for z in at_point if z["STATUS"] == "Evacuation Warning"]
    near_ord = [z for z in nearby   if z["STATUS"] == "Evacuation Order"]
    near_wrn = [z for z in nearby   if z["STATUS"] == "Evacuation Warning"]

    if orders:
        return "LEAVE NOW",          f"Evacuation Order — {orders[0]['COUNTY'].title()} County"
    if warnings:
        return "EVACUATE",           f"Evacuation Warning — {warnings[0]['COUNTY'].title()} County"
    if near_ord and near_ord[0]["distance_miles"] < 5:
        return "GET READY TO LEAVE", f"Evacuation Order zone {near_ord[0]['distance_miles']} mi away"
    if near_wrn and near_wrn[0]["distance_miles"] < 5:
        return "BE READY TO LEAVE",  f"Evacuation Warning zone {near_wrn[0]['distance_miles']} mi away"
    if nearby:
        return "MONITOR",            f"Evacuation zone {nearby[0]['distance_miles']} mi away"
    if hotspots:
        return "MONITOR",            f"Satellite fire hotspot {hotspots[0]['distance_miles']} mi away"
    return "YOU ARE SAFE",           "No active evacuation zones or fire detections nearby"
