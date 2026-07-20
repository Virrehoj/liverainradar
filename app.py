"""
Flask backend for the SMHI radar web app.

Endpoints
---------
GET /                        the web app
GET /api/radar/frames        list of recent frames + overlay bounds
GET /api/radar/image/<key>   one warped, colorized radar frame as PNG
GET /api/geocode?q=...       place search (OpenStreetMap Nominatim)
GET /api/forecast?lat&lon    simplified SMHI point forecast

The browser never talks to SMHI or Nominatim directly; everything is proxied
here. That keeps API details (projection handling, response formats, polite
User-Agent headers) in one place and avoids browser CORS issues.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, render_template, request, Response

import radar

app = Flask(__name__)

FORECAST_URL = ("https://opendata-download-metfcst.smhi.se/api/category/pmp3g/"
                "version/2/geotype/point/lon/{lon}/lat/{lat}/data.json")
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

_session = requests.Session()
_session.headers["User-Agent"] = radar.USER_AGENT


@app.get("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Radar
# ---------------------------------------------------------------------------

@app.get("/api/radar/frames")
def radar_frames():
    count = min(max(int(request.args.get("count", 12)), 1), 36)
    try:
        frames = radar.list_frames(count)
    except requests.RequestException as exc:
        return jsonify(error=f"Could not reach SMHI radar API: {exc}"), 502
    radar.remember_links(frames)

    out = [{"key": f["key"], "valid": f["valid"], "kind": "observed",
            "url": f"/api/radar/image/{f['key']}"} for f in frames]

    # Forecast descriptors: keys the image endpoint knows how to compute,
    # timestamped +5 min per step after the newest observation.
    if frames and frames[-1]["valid"]:
        base = frames[-1]["key"]
        base_time = datetime.fromisoformat(frames[-1]["valid"].replace("Z", "+00:00"))
        for i in range(1, radar.FORECAST_STEPS + 1):
            valid = (base_time + timedelta(minutes=5 * i)).isoformat().replace("+00:00", "Z")
            out.append({"key": f"fc_{base}_{i}", "valid": valid, "kind": "forecast",
                        "url": f"/api/radar/image/fc_{base}_{i}"})

    return jsonify(
        bounds=radar.latlon_bounds(),
        frames=out,
        fetched=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/api/radar/image/<key>")
def radar_image(key: str):
    if not key.replace("_", "").isalnum():
        return jsonify(error="invalid frame key"), 400
    try:
        png = radar.render_frame(key)
    except KeyError:
        return jsonify(error="unknown frame; refresh the frame list"), 404
    except requests.RequestException as exc:
        return jsonify(error=f"Could not download frame from SMHI: {exc}"), 502
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------------------
# Search (geocoding)
# ---------------------------------------------------------------------------

@app.get("/api/geocode")
def geocode():
    query = (request.args.get("q") or "").strip()
    if len(query) < 2:
        return jsonify(results=[])
    try:
        resp = _session.get(
            NOMINATIM_URL,
            params={"q": query, "format": "jsonv2", "limit": 6,
                    "accept-language": "en",
                    # Bias (not restrict) results toward the radar's coverage.
                    "viewbox": "2,72,32,53", "bounded": 0},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        return jsonify(error=f"Geocoding failed: {exc}"), 502
    results = [{"name": item.get("display_name"),
                "lat": float(item["lat"]),
                "lon": float(item["lon"])}
               for item in resp.json()]
    return jsonify(results=results)


# ---------------------------------------------------------------------------
# Point forecast
# ---------------------------------------------------------------------------

_PARAMS = {"t": "temp", "ws": "wind", "wd": "windDir",
           "pmean": "precip", "Wsymb2": "symbol", "r": "humidity"}


@app.get("/api/forecast")
def forecast():
    try:
        lat = round(float(request.args["lat"]), 6)
        lon = round(float(request.args["lon"]), 6)
    except (KeyError, ValueError):
        return jsonify(error="lat and lon query parameters are required"), 400
    try:
        resp = _session.get(FORECAST_URL.format(lat=lat, lon=lon), timeout=15)
    except requests.RequestException as exc:
        return jsonify(error=f"Could not reach SMHI forecast API: {exc}"), 502
    if resp.status_code == 400:
        return jsonify(error="This place is outside SMHI's forecast area "
                             "(roughly the Nordics and nearby)."), 404
    if resp.status_code != 200:
        return jsonify(error=f"SMHI forecast API returned {resp.status_code}"), 502

    series = []
    for entry in resp.json().get("timeSeries", [])[:26]:
        point = {"time": entry["validTime"]}
        for p in entry.get("parameters", []):
            name = _PARAMS.get(p.get("name"))
            if name and p.get("values"):
                point[name] = p["values"][0]
        series.append(point)
    return jsonify(series=series)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
