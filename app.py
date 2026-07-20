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

from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from flask import Flask, jsonify, render_template, request, Response

import radar

app = Flask(__name__)

# SMHI point forecast. NOTE: the older pmp3g v2 API was shut down 2026-03-31
# (every request returns 404); SNOW1g v1 is its replacement. Differences that
# matter here: "time" instead of "validTime", a flat `data` object instead of
# a parameters array, human-readable parameter names, and 9999 as the
# missing-value sentinel. Weather symbol codes (1-27) are unchanged.
FORECAST_URL = ("https://opendata-download-metfcst.smhi.se/api/category/snow1g/"
                "version/1/geotype/point/lon/{lon}/lat/{lat}/data.json")
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


@app.get("/api/radar/motion")
def radar_motion():
    """How the nowcast thinks the rain field is moving — for sanity-checking."""
    try:
        return jsonify(radar.get_motion())
    except KeyError as exc:
        return jsonify(error=str(exc)), 404
    except requests.RequestException as exc:
        return jsonify(error=f"Could not download frames from SMHI: {exc}"), 502


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

# SNOW1g parameter names -> our internal keys (the rest of the app and the
# frontend only ever see the internal names, which is why this API change
# stays contained to this one file).
_PARAMS = {"air_temperature": "temp", "wind_speed": "wind",
           "wind_from_direction": "windDir",
           "precipitation_amount_mean": "precip",
           "symbol_code": "symbol", "relative_humidity": "humidity"}
_MISSING = 9999                 # SMHI's sentinel for "no value"

# Wsymb2 codes that mean some form of precipitation (rain/sleet/snow/thunder).
_PRECIP_SYMBOLS = set(range(8, 28)) - {7}
_RAIN_INTENSITY_MIN = 0.1        # mm/h below this doesn't count as "raining"


def _aggregate_days(series: list[dict], tz_name: str | None, max_days: int = 8) -> list[dict]:
    """Group the forecast series into LOCAL calendar days.

    Two things make this less trivial than it looks:

    * Day boundaries live in the viewer's timezone, not UTC — rain at 00:30
      local time must count toward the local date. Hence the tz parameter.
    * SMHI's `pmean` is an intensity (mm/h) valid for the interval *ending*
      at each timestamp, and intervals grow from 1 h (first days) to 12 h
      (day 7+). Millimetres = intensity x interval length, and hour ranges
      get coarser further out — reported via `resolution_h` so the UI can
      mark them as approximate.
    """
    try:
        tzinfo = ZoneInfo(tz_name) if tz_name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        tzinfo = timezone.utc

    days: dict = {}
    prev_time = None
    for point in series:
        t = datetime.fromisoformat(point["time"].replace("Z", "+00:00"))
        interval_h = ((t - prev_time).total_seconds() / 3600.0) if prev_time else 1.0
        interval_h = min(max(interval_h, 1.0), 12.0)
        prev_time = t
        start_local = (t - timedelta(hours=interval_h)).astimezone(tzinfo)
        end_local = t.astimezone(tzinfo)

        day = days.setdefault(start_local.date(), {
            "temps": [], "mm": 0.0, "wet": [], "symbols": [], "res": 1.0})
        day["res"] = max(day["res"], interval_h)
        if point.get("temp") is not None:
            day["temps"].append(point["temp"])
        intensity = point.get("precip") or 0.0
        if intensity >= _RAIN_INTENSITY_MIN:
            day["mm"] += intensity * interval_h
            start_h = start_local.hour + start_local.minute / 60.0
            end_h = start_h + interval_h          # may pass 24: clamp on output
            day["wet"].append((start_h, end_h))
        if point.get("symbol") is not None:
            day["symbols"].append((start_local.hour, point["symbol"]))

    out = []
    for date in sorted(days)[:max_days]:
        d = days[date]
        if not d["temps"]:
            continue
        out.append({
            "date": date.isoformat(),
            "temp_min": round(min(d["temps"])),
            "temp_max": round(max(d["temps"])),
            "precip_mm": round(d["mm"], 1),
            "rain_periods": _merge_periods(d["wet"]),
            "symbol": _pick_symbol(d["symbols"], d["mm"]),
            "resolution_h": round(d["res"]),
        })
    return out


def _merge_periods(periods: list[tuple[float, float]]) -> list[dict]:
    """Merge touching wet intervals into ranges like 14-18 (local hours)."""
    merged: list[list[float]] = []
    for start, end in sorted(periods):
        if merged and start <= merged[-1][1] + 0.51:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [{"from": int(s), "to": min(24, int(-(-e // 1)))} for s, e in merged]


def _pick_symbol(symbols: list[tuple[int, int]], total_mm: float) -> int | None:
    """One representative symbol per day, by an explicit rule: the most
    frequent daytime (06-21) symbol — but if the day actually has rain,
    prefer the most frequent *precipitation* symbol so a wet afternoon
    isn't hidden behind a sunny morning."""
    if not symbols:
        return None
    daytime = [s for hour, s in symbols if 6 <= hour <= 21] or [s for _, s in symbols]
    counts = Counter(daytime)
    if total_mm >= 0.2:
        wet_counts = {s: n for s, n in counts.items() if s in _PRECIP_SYMBOLS}
        if not wet_counts:                    # rain fell outside daytime hours:
            all_counts = Counter(s for _, s in symbols)   # still don't show sun
            wet_counts = {s: n for s, n in all_counts.items() if s in _PRECIP_SYMBOLS}
        if wet_counts:
            return max(wet_counts, key=wet_counts.get)
    return counts.most_common(1)[0][0]


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
    if resp.status_code in (400, 404):
        return jsonify(error="This place is outside SMHI's forecast area "
                             "(roughly the Nordics and nearby)."), 404
    if resp.status_code != 200:
        return jsonify(error=f"SMHI forecast API returned {resp.status_code}"), 502

    series = []
    for entry in resp.json().get("timeSeries", []):
        time = entry.get("time") or entry.get("validTime")
        if not time:
            continue
        point = {"time": time}
        values = entry.get("data") or {}
        for src, dst in _PARAMS.items():
            v = values.get(src)
            if v is None or v == _MISSING:
                continue
            point[dst] = int(v) if dst == "symbol" else v
        series.append(point)

    days = _aggregate_days(series, request.args.get("tz"))
    return jsonify(series=series[:26], days=days)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
