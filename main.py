import os
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

from fire_api import (
    caloes_at_point,
    caloes_nearby,
    firms_nearby,
    check as fire_check,
    haversine_miles,
)

app = Flask(__name__)
app.json.sort_keys = False

_mesh_reports: list[dict] = []
_geocode_cache: dict[str, tuple] = {}


def geocode(query: str):
    key = query.lower().strip()
    if key in _geocode_cache:
        return _geocode_cache[key]
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1,
                    "countrycodes": "us", "addressdetails": "1"},
            headers={"User-Agent": "wildfire-mesh-evacuation-api/1.0"},
            timeout=8,
        )
        results = resp.json()
        if not results:
            _geocode_cache[key] = None
            return None
        r = results[0]
        addr = r.get("address", {})
        place = (
            addr.get("city") or addr.get("town") or addr.get("village") or
            addr.get("hamlet") or addr.get("locality") or addr.get("suburb") or
            addr.get("county") or ""
        )
        state = addr.get("state", "")
        name = f"{place}, {state}".strip(", ") if place else r.get("display_name", query).split(",")[0].strip()
        result = float(r["lat"]), float(r["lon"]), name
        _geocode_cache[key] = result
        return result
    except Exception:
        return None


def _err(msg: str, code: int = 400):
    return msg + "\n", code, {"Content-Type": "text/plain"}


def _radius() -> float:
    try:
        r = float(request.args.get("radius", 50))
        return max(1.0, min(r, 300.0))
    except (ValueError, TypeError):
        return 50.0


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "wildfire-evacuation-api",
        "time": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/v1/evacuate")
def evacuate():
    location_str = request.args.get("location") or request.args.get("address")

    if location_str:
        result = geocode(location_str)
        if result is None:
            return _err(f"Location not found: {location_str!r}. Try a more specific address.")
        lat, lon, display_name = result
    else:
        try:
            lat = float(request.args["lat"])
            lon = float(request.args["lon"])
            display_name = f"{lat:.4f}, {lon:.4f}"
        except (KeyError, ValueError, TypeError):
            return _err("Provide either ?location=City,CA  or  ?lat=X&lon=Y")
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            return _err("Invalid coordinates.")

    status, detail = fire_check(lat, lon, _radius())
    yes_no = "YES" if status not in ("YOU ARE SAFE", "MONITOR") else "NO"
    as_of = datetime.now(timezone.utc).strftime("%-m/%-d/%Y %-I:%M %p UTC")

    text = (
        f"Location:    {display_name}\n"
        f"Evacuate?    {yes_no}\n"
        f"Status:      {status} — {detail}\n"
        f"Last update: {as_of}\n"
    )
    return text, 200, {"Content-Type": "text/plain"}


@app.get("/api/v1/fires")
def fires():
    location_str = request.args.get("location") or request.args.get("address")
    if location_str:
        result = geocode(location_str)
        if result is None:
            return _err(f"Location not found: {location_str!r}")
        lat, lon, _ = result
    else:
        try:
            lat = float(request.args["lat"])
            lon = float(request.args["lon"])
        except (KeyError, ValueError, TypeError):
            return _err("Provide either ?location=City,CA  or  ?lat=X&lon=Y")

    radius = _radius()
    return jsonify({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "evacuation_zones_at_point": caloes_at_point(lat, lon),
        "evacuation_zones_nearby": caloes_nearby(lat, lon, radius),
        "hotspots": firms_nearby(lat, lon, radius),
    })


@app.post("/api/v1/mesh/report")
def mesh_report():
    data = request.get_json(silent=True)
    if not data:
        return _err("JSON body required")
    missing = {"node_id", "lat", "lon"} - data.keys()
    if missing:
        return _err(f"Missing fields: {sorted(missing)}")
    report = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "node_id": data["node_id"],
        "lat": data["lat"],
        "lon": data["lon"],
        "smoke_detected": bool(data.get("smoke_detected", False)),
        "temperature_c": data.get("temperature_c"),
        "message": data.get("message", ""),
    }
    _mesh_reports.append(report)
    if len(_mesh_reports) > 500:
        _mesh_reports.pop(0)
    return jsonify({"status": "received", "report": report}), 201


@app.get("/api/v1/mesh/reports")
def mesh_reports_list():
    return jsonify({"reports": list(reversed(_mesh_reports))})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"\n  Wildfire Evacuation API — http://0.0.0.0:{port}\n")
    print(f'  curl "http://localhost:{port}/api/v1/evacuate?location=Caliente,CA"\n')
    app.run(host="0.0.0.0", port=port, debug=False)
