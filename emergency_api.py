import argparse
import csv
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# ============================================================
#  SET YOUR VARIABLES HERE
# ============================================================
MY_ADDRESS          = "Petaluma, CA"  # change to your address or city
MY_RADIUS_MILES     = 30
MY_INTERVAL_SECONDS = 300             # 300 = refresh every 5 min
# ============================================================

NIFC_INCIDENTS_URL = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Incident_Locations_Current/FeatureServer/0/query"
)
OUTAGE_QUERY_URL = (
    "https://services.arcgis.com/BLN4oKB0N1YSgvY8/ArcGIS/rest/services/"
    "Power_Outages_(View)/FeatureServer/0/query"
)
CALOES_URL = (
    "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services"
    "/CA_EVACUATIONS_CalOESHosted_view/FeatureServer/0/query"
)
FIRMS_KEY    = os.environ.get("FIRMS_KEY", "da9704de0ca790e6e26885aa00e960e8")
NOMINATIM    = "https://nominatim.openstreetmap.org/search"
USER_AGENT   = "local-alerts-monitor/1.0 (personal use)"
EARTH_RADIUS = 3958.8
CACHE_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")


# ------------------------------------------------------------
# HTTP helpers (no third-party libraries needed)
# ------------------------------------------------------------

def _get_json(url, params=None, timeout=15):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _get_text(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode()


# ------------------------------------------------------------
# Disk cache — saves last good response; used when APIs are down
# ------------------------------------------------------------

def _cache_path(key):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, key + ".json")


def _cache_save(key, data):
    try:
        with open(_cache_path(key), "w") as f:
            json.dump({"t": time.time(), "d": data}, f)
    except Exception:
        pass


def _cache_load(key):
    try:
        with open(_cache_path(key)) as f:
            return json.load(f)
    except Exception:
        return None


def _with_cache(key, fetch_fn):
    """Call fetch_fn(); if it fails, return the last cached result instead."""
    try:
        data = fetch_fn()
        _cache_save(key, data)
        return data
    except Exception:
        hit = _cache_load(key)
        if hit:
            age = round((time.time() - hit["t"]) / 60)
            print(f"  [offline] {key}: using data from {age} min ago", file=sys.stderr)
            return hit["d"]
        return None


# ------------------------------------------------------------
# Shared helpers
# ------------------------------------------------------------

def geocode_address(address):
    results = _get_json(NOMINATIM, params={"q": address, "format": "json", "limit": 1}, timeout=15)
    if not results:
        raise ValueError(f"Could not geocode: {address!r}")
    return float(results[0]["lat"]), float(results[0]["lon"])


def haversine_miles(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS * math.asin(math.sqrt(a))


def bbox(lat, lon, miles):
    d = miles / 69.0
    return f"{lon-d},{lat-d},{lon+d},{lat+d}"


def extract_field(props, *keys, default="Unknown"):
    for k in keys:
        if k in props and props[k] not in (None, ""):
            return props[k]
    return default


# ------------------------------------------------------------
# Wildfire incidents (NIFC)
# ------------------------------------------------------------

def fetch_incidents():
    def _do():
        return _get_json(NIFC_INCIDENTS_URL, params={
            "where": "IncidentTypeCategory <> 'RX'",
            "outFields": "IncidentName,IncidentSize,PercentContained,POOCity,POOCounty,POOState",
            "returnGeometry": "true", "outSR": "4326", "f": "json",
        }, timeout=20).get("features", [])
    return _with_cache("nifc_incidents", _do) or []


def build_location_desc(props):
    city, county, state = props.get("POOCity"), props.get("POOCounty"), props.get("POOState")
    parts = []
    if city:
        parts.append(str(city))
    if county:
        s = str(county)
        parts.append(s if s.lower().endswith("county") else f"{s} County")
    if state:
        parts.append(str(state))
    return ", ".join(parts) if parts else "No location available"


def nearby_fires(user_lat, user_lon, radius_miles):
    results = []
    for feat in fetch_incidents():
        props = feat.get("attributes", {}) or {}
        geom  = feat.get("geometry", {}) or {}
        if geom.get("x") is None or geom.get("y") is None:
            continue
        try:
            dist = haversine_miles(user_lat, user_lon, float(geom["y"]), float(geom["x"]))
        except (TypeError, ValueError):
            continue
        if dist > radius_miles:
            continue
        results.append({
            "name":           extract_field(props, "IncidentName"),
            "acres":          extract_field(props, "IncidentSize", default="Not reported"),
            "containment":    extract_field(props, "PercentContained", default="Not reported"),
            "location":       build_location_desc(props),
            "distance_miles": round(dist, 1),
        })
    return sorted(results, key=lambda f: f["distance_miles"])


def print_fire_list(fires, radius_miles):
    print(f"\n=== Wildfire incidents within {radius_miles} miles ===")
    if not fires:
        print("No active fires found in range.")
        return
    for i, fire in enumerate(fires, 1):
        containment = (f"{fire['containment']}%" if isinstance(fire["containment"], (int, float))
                       else str(fire["containment"]))
        print(f"\n{i}. {fire['name']}  (~{fire['distance_miles']} mi away)")
        print(f"   Acres burned : {fire['acres']}")
        print(f"   Containment  : {containment}")
        print(f"   Location     : {fire['location']}")


# ------------------------------------------------------------
# Evacuation zones (CalOES + NASA FIRMS)
# ------------------------------------------------------------

def caloes_at_point(lat, lon):
    key  = f"caloes_point_{lat:.2f}_{lon:.2f}"
    geom = json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}})
    def _do():
        feats = _get_json(CALOES_URL, params={
            "geometry": geom, "geometryType": "esriGeometryPoint",
            "spatialRel": "esriSpatialRelIntersects", "where": "1=1",
            "outFields": "STATUS,COUNTY,CITY", "returnGeometry": "false", "f": "json",
        }, timeout=10).get("features", [])
        return [f["attributes"] for f in feats]
    return _with_cache(key, _do) or []


def caloes_nearby(lat, lon, miles):
    key = f"caloes_nearby_{lat:.2f}_{lon:.2f}_{int(miles)}"
    def _do():
        feats = _get_json(CALOES_URL, params={
            "geometry": bbox(lat, lon, miles), "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects", "where": "1=1",
            "outFields": "STATUS,COUNTY", "returnCentroid": "true",
            "returnGeometry": "false", "outSR": "4326", "f": "json",
        }, timeout=10).get("features", [])
        zones = []
        for f in feats:
            c = f.get("centroid") or {}
            if c.get("x") and c.get("y"):
                zones.append({**f["attributes"],
                              "distance_miles": round(haversine_miles(lat, lon, c["y"], c["x"]), 1)})
        return sorted(zones, key=lambda z: z["distance_miles"])
    return _with_cache(key, _do) or []


def firms_nearby(lat, lon, miles):
    key = f"firms_{lat:.2f}_{lon:.2f}_{int(miles)}"
    def _do():
        text = _get_text(
            f"https://firms.modaps.eosdis.nasa.gov/api/area/csv"
            f"/{FIRMS_KEY}/VIIRS_SNPP_NRT/{bbox(lat, lon, miles)}/1",
            timeout=12,
        )
        fires = []
        for row in csv.DictReader(io.StringIO(text)):
            try:
                d = haversine_miles(lat, lon, float(row["latitude"]), float(row["longitude"]))
                fires.append({"distance_miles": round(d, 1)})
            except (ValueError, KeyError):
                continue
        return sorted(fires, key=lambda x: x["distance_miles"])
    return _with_cache(key, _do) or []


def evacuation_check(lat, lon, radius):
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


def print_evacuation_status(status, detail):
    print(f"\n=== Evacuation status (CalOES) ===")
    evacuate = "YES" if status not in ("YOU ARE SAFE", "MONITOR") else "NO"
    print(f"Evacuate? {evacuate}")
    print(f"Status  : {status} — {detail}")


# ------------------------------------------------------------
# Power outages (Cal OES)
# ------------------------------------------------------------

def fetch_active_outages():
    def _do():
        return _get_json(OUTAGE_QUERY_URL, params={
            "where": "OutageStatus='Active'",
            "outFields": ("UtilityCompany,StartDate,EstimatedRestoreDate,Cause,"
                          "ImpactedCustomers,County,OutageStatus,OutageType,IncidentId"),
            "returnGeometry": "true", "outSR": "4326", "f": "json",
        }, timeout=20).get("features", [])
    return _with_cache("outages", _do) or []


def format_epoch_ms(value):
    if value is None:
        return "Not reported"
    try:
        return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d %I:%M %p")
    except (TypeError, ValueError, OSError):
        return "Not reported"


def nearby_outages(user_lat, user_lon, radius_miles):
    results = []
    for feat in fetch_active_outages():
        props = feat.get("attributes", {}) or {}
        geom  = feat.get("geometry", {}) or {}
        if geom.get("x") is None or geom.get("y") is None:
            continue
        try:
            dist = haversine_miles(user_lat, user_lon, float(geom["y"]), float(geom["x"]))
        except (TypeError, ValueError):
            continue
        if dist > radius_miles:
            continue
        results.append({
            "utility":            props.get("UtilityCompany") or "Unknown utility",
            "started":            format_epoch_ms(props.get("StartDate")),
            "eta_restore":        format_epoch_ms(props.get("EstimatedRestoreDate")),
            "location":           (props.get("County") or "Unknown") + " County",
            "planned":            props.get("OutageType") or "Unknown",
            "cause":              props.get("Cause") or "Not reported",
            "impacted_customers": props.get("ImpactedCustomers"),
            "incident_id":        props.get("IncidentId") or "N/A",
            "distance_miles":     round(dist, 1),
        })
    return sorted(results, key=lambda o: o["distance_miles"])


def print_outage_status(outages, radius_miles):
    print(f"\n=== Power outage check within {radius_miles} mi ===")
    if not outages:
        print("No active tracked outages found near you.")
        return
    print(f"You ARE near {len(outages)} active outage(s):")
    for i, o in enumerate(outages, 1):
        print(f"\n{i}. {o['utility']}  (~{o['distance_miles']} mi away, incident {o['incident_id']})")
        print(f"   Started              : {o['started']}")
        print(f"   Estimated restoration: {o['eta_restore']}")
        print(f"   Location             : {o['location']}")
        print(f"   Planned outage?      : {o['planned']}")
        print(f"   Cause                : {o['cause']}")
        if o["impacted_customers"] is not None:
            print(f"   Customers impacted   : {o['impacted_customers']}")


# ------------------------------------------------------------
# Combined runner
# ------------------------------------------------------------

def run_checks(lat, lon, radius_miles):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n############################################")
    print(f"#  Local Alerts Check — {timestamp}")
    print(f"############################################")

    try:
        print_fire_list(nearby_fires(lat, lon, radius_miles), radius_miles)
    except Exception as e:
        print(f"[Error] Wildfire data unavailable: {e}", file=sys.stderr)

    try:
        status, detail = evacuation_check(lat, lon, radius_miles)
        print_evacuation_status(status, detail)
    except Exception as e:
        print(f"[Error] Evacuation data unavailable: {e}", file=sys.stderr)

    try:
        print_outage_status(nearby_outages(lat, lon, radius_miles), radius_miles)
    except Exception as e:
        print(f"[Error] Outage data unavailable: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Monitor wildfires, evacuations, and power outages.")
    loc = parser.add_mutually_exclusive_group()
    loc.add_argument("--address", help="Address or place name.")
    loc.add_argument("--lat",     type=float, help="Latitude.")
    parser.add_argument("--lon",      type=float, help="Longitude (required with --lat).")
    parser.add_argument("--radius",   type=float, default=MY_RADIUS_MILES)
    parser.add_argument("--interval", type=int,   default=MY_INTERVAL_SECONDS)
    parser.add_argument("--once",     action="store_true", help="Run once and exit.")
    args = parser.parse_args()

    if not args.address and args.lat is None:
        args.address = MY_ADDRESS

    if args.address:
        try:
            lat, lon = geocode_address(args.address)
        except Exception as e:
            print(f"Error geocoding address: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        if args.lon is None:
            parser.error("--lon is required when using --lat")
        lat, lon = args.lat, args.lon

    print(f"Location: {lat:.4f}, {lon:.4f}  |  radius: {args.radius} mi  |  interval: {args.interval}s")

    try:
        while True:
            run_checks(lat, lon, args.radius)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
