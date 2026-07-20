"""
Radar pipeline: fetch SMHI composite frames, reproject them to Web Mercator,
and colorize reflectivity (dBZ) into transparent PNGs a Leaflet map can overlay.

Why reprojection is needed
--------------------------
SMHI delivers the radar composite in SWEREF99 TM (EPSG:3006), a transverse
Mercator projection centered on Sweden. Web maps (Leaflet, Google Maps, ...)
render in Web Mercator (EPSG:3857). A rectangle in one projection is NOT a
rectangle in the other, so simply stretching the SMHI image between lat/lon
corners would misplace rain by a large margin, worst in northern Sweden.

The fix implemented here is an inverse-mapping warp:

  1. Decide the output rectangle in EPSG:3857 (the projected footprint of the
     radar area).
  2. For every OUTPUT pixel center, transform its 3857 coordinate back into
     3006 and ask: "which source pixel lives there?" (nearest neighbour).
  3. Copy that value. Output pixels that land outside the source image become
     fully transparent.

Inverse mapping (output -> source) rather than forward mapping guarantees
every output pixel gets exactly one value: no holes, no double-writes.
The pixel index mapping only depends on image geometry, never on the weather,
so it is computed once and reused for every frame (see _mapping()).

Data values
-----------
The GeoTIFF pixels are uint8. Per SMHI's docs:
  dBZ = pixel * 0.4 - 30      (gain 0.4, offset -30)
  0   = inside coverage, no echo
  255 = outside radar coverage (no data)
dBZ is a logarithmic measure of how strongly precipitation reflects the radar
beam; roughly, >5 dBZ is precipitation, ~25 dBZ steady rain, >45 dBZ downpour.
"""

from __future__ import annotations

import io
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import numpy as np
import requests
from PIL import Image
from pyproj import Transformer

# ---------------------------------------------------------------------------
# Constants from SMHI's radar API documentation
# ---------------------------------------------------------------------------

API_BASE = "https://opendata-download-radar.smhi.se/api/version/latest/area/sweden/product/comp"

# Corner coordinates of the composite in SWEREF99 TM (EPSG:3006), meters.
SRC_X_MIN, SRC_Y_MIN = 126_648.0, 5_983_984.0   # bottom-left  (easting, northing)
SRC_X_MAX, SRC_Y_MAX = 1_075_693.0, 7_771_252.0  # top-right

OUT_WIDTH = 720          # output PNG width in px (radar itself is ~2 km/px, so this is plenty)
REQUEST_TIMEOUT = 15     # seconds
USER_AGENT = "smhi-radar-webapp/1.0 (personal project)"

# always_xy=True => coordinates are given/returned as (x/lon, y/lat) consistently.
_T_3006_TO_3857 = Transformer.from_crs("EPSG:3006", "EPSG:3857", always_xy=True)
_T_3857_TO_3006 = Transformer.from_crs("EPSG:3857", "EPSG:3006", always_xy=True)
_T_3857_TO_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)


# ---------------------------------------------------------------------------
# Color lookup table: uint8 pixel value -> RGBA
# ---------------------------------------------------------------------------

def _build_lut() -> np.ndarray:
    """Precompute a 256x4 RGBA table so colorizing a frame is one array index.

    Colors are placed at dBZ "stops" and linearly interpolated between them.
    Everything below the precipitation threshold (~5 dBZ) is transparent, as
    is 255 (outside coverage).
    """
    stops = [  # (dBZ, R, G, B, A)
        (5,  61, 125, 214, 110),
        (12, 64, 170, 232, 145),
        (18, 72, 214, 194, 170),
        (24, 120, 224, 120, 190),
        (30, 255, 222, 89, 210),
        (36, 255, 160, 54, 225),
        (42, 255, 94, 58, 235),
        (50, 255, 45, 85, 245),
        (60, 238, 64, 206, 255),
    ]
    lut = np.zeros((256, 4), dtype=np.uint8)
    dbz_stops = np.array([s[0] for s in stops], dtype=np.float64)
    rgba_stops = np.array([s[1:] for s in stops], dtype=np.float64)

    values = np.arange(255, dtype=np.float64)          # 255 stays transparent
    dbz = values * 0.4 - 30.0
    for channel in range(4):
        lut[:255, channel] = np.interp(
            dbz, dbz_stops, rgba_stops[:, channel]
        ).astype(np.uint8)
    lut[:255][dbz < stops[0][0], 3] = 0                # below threshold -> invisible
    return lut


_LUT = _build_lut()


# ---------------------------------------------------------------------------
# Geometry: the reusable warp mapping
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WarpMapping:
    rows: np.ndarray          # source row index per output pixel
    cols: np.ndarray          # source col index per output pixel
    valid: np.ndarray         # bool mask: output pixel falls inside source image
    out_size: tuple[int, int]  # (width, height)
    latlon_bounds: tuple      # ((south, west), (north, east)) for Leaflet


def _mercator_footprint() -> tuple[float, float, float, float]:
    """Bounding box in EPSG:3857 of the (curved) projected radar rectangle.

    The four corners alone are not enough: the edges of the 3006 rectangle
    bow outward when reprojected, so we sample each edge densely and take
    min/max of all the projected points.
    """
    n = 64
    xs = np.linspace(SRC_X_MIN, SRC_X_MAX, n)
    ys = np.linspace(SRC_Y_MIN, SRC_Y_MAX, n)
    edge_x = np.concatenate([xs, xs, np.full(n, SRC_X_MIN), np.full(n, SRC_X_MAX)])
    edge_y = np.concatenate([np.full(n, SRC_Y_MIN), np.full(n, SRC_Y_MAX), ys, ys])
    mx, my = _T_3006_TO_3857.transform(edge_x, edge_y)
    return float(mx.min()), float(my.min()), float(mx.max()), float(my.max())


@lru_cache(maxsize=4)
def _mapping(src_w: int, src_h: int, out_w: int = OUT_WIDTH) -> WarpMapping:
    """Compute, once per source-image size, where every output pixel samples from."""
    mx_min, my_min, mx_max, my_max = _mercator_footprint()
    out_h = max(1, round(out_w * (my_max - my_min) / (mx_max - mx_min)))

    # Output pixel centers in EPSG:3857. Row 0 is the northern edge.
    px = mx_min + (np.arange(out_w) + 0.5) * (mx_max - mx_min) / out_w
    py = my_max - (np.arange(out_h) + 0.5) * (my_max - my_min) / out_h
    grid_x, grid_y = np.meshgrid(px, py)

    # Inverse map: where is each output pixel in SWEREF99 TM?
    sx, sy = _T_3857_TO_3006.transform(grid_x, grid_y)

    cols = np.floor((sx - SRC_X_MIN) / (SRC_X_MAX - SRC_X_MIN) * src_w).astype(np.int32)
    rows = np.floor((SRC_Y_MAX - sy) / (SRC_Y_MAX - SRC_Y_MIN) * src_h).astype(np.int32)
    valid = (cols >= 0) & (cols < src_w) & (rows >= 0) & (rows < src_h)
    cols = np.clip(cols, 0, src_w - 1)
    rows = np.clip(rows, 0, src_h - 1)

    # Leaflet wants the overlay bounds as lat/lon; since the image is linear
    # in 3857 and Leaflet displays in 3857, this placement is exact.
    west, south = _T_3857_TO_4326.transform(mx_min, my_min)
    east, north = _T_3857_TO_4326.transform(mx_max, my_max)
    return WarpMapping(rows, cols, valid, (out_w, out_h),
                       ((south, west), (north, east)))


def latlon_bounds() -> tuple:
    """Overlay bounds without needing a frame yet (source size is fixed anyway)."""
    # Nominal size at 2 km resolution; only the bounds part of the mapping
    # is used here and it does not depend on the source size.
    return _mapping(475, 894).latlon_bounds


# ---------------------------------------------------------------------------
# Fetching frames from SMHI
# ---------------------------------------------------------------------------

_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT


def _normalize_valid(entry: dict) -> str | None:
    """Return the frame's timestamp as unambiguous ISO-8601 UTC ('...Z').

    SMHI's `valid` field is not guaranteed to carry a timezone designator,
    and JavaScript parses timezone-less ISO strings as *local* time — which
    silently shows UTC wall-clock values as if they were local. Normalizing
    here means the browser always receives an explicit UTC instant and can
    convert it to the viewer's own timezone (CET/CEST or anything else).
    """
    v = entry.get("valid")
    if isinstance(v, (int, float)):                     # epoch milliseconds
        dt = datetime.fromtimestamp(v / 1000, tz=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    if isinstance(v, str) and v.strip():
        try:
            dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:                       # SMHI times are UTC
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    # Last resort: the key itself encodes UTC time as radar_YYMMDDHHMM.
    m = re.fullmatch(r"radar_(\d{10})", entry.get("key", ""))
    if m:
        dt = datetime.strptime(m.group(1), "%y%m%d%H%M").replace(tzinfo=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    return None


def list_frames(count: int = 12) -> list[dict]:
    """Return the newest `count` frames (oldest first) with their .tif links.

    The API is organized per UTC day, so shortly after midnight we must also
    look at the previous day to fill the animation window.
    """
    frames: list[dict] = []
    day = datetime.now(timezone.utc)
    for _ in range(3):  # today + up to two days back
        url = f"{API_BASE}/{day:%Y/%m/%d}"
        resp = _session.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            payload = resp.json()
            for f in payload.get("files", []):
                tif = next((fmt.get("link") for fmt in f.get("formats", [])
                            if fmt.get("key") == "tif"), None)
                if tif and f.get("key"):
                    frames.append({"key": f["key"],
                                   "valid": _normalize_valid(f),
                                   "tif": tif})
        if len(frames) >= count:
            break
        day -= timedelta(days=1)
    frames.sort(key=lambda f: f["key"])
    return frames[-count:]


# ---------------------------------------------------------------------------
# Rendering + a small bounded cache (frame key -> finished PNG bytes)
# ---------------------------------------------------------------------------

_png_cache: OrderedDict[str, bytes] = OrderedDict()
_src_cache: OrderedDict[str, np.ndarray] = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_MAX = 60
_tif_links: dict[str, str] = {}   # remembered from list_frames so /image/<key> works
_observed_order: list[str] = []   # newest-last, needed to build forecasts

FORECAST_STEPS = 6                # 6 x 5 min = +30 min ahead
FC_KEY_RE = re.compile(r"fc_(radar_\d{10})_(\d{1,2})")


def remember_links(frames: list[dict]) -> None:
    global _observed_order
    with _cache_lock:
        for f in frames:
            _tif_links[f["key"]] = f["tif"]
        while len(_tif_links) > 200:
            _tif_links.pop(next(iter(_tif_links)))
        _observed_order = [f["key"] for f in frames]


def _fetch_source(key: str) -> np.ndarray:
    """Raw uint8 source array for one observed frame (downloaded once)."""
    with _cache_lock:
        if key in _src_cache:
            _src_cache.move_to_end(key)
            return _src_cache[key]
        link = _tif_links.get(key)
    if link is None:
        raise KeyError(f"unknown frame key {key!r} (list frames first)")
    resp = _session.get(link, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    src = np.asarray(Image.open(io.BytesIO(resp.content)).convert("L"), dtype=np.uint8)
    with _cache_lock:
        _src_cache[key] = src
        while len(_src_cache) > _CACHE_MAX:
            _src_cache.popitem(last=False)
    return src


def render_frame(key: str) -> bytes:
    """Fetch/compute, warp and colorize one frame (observed or forecast)."""
    with _cache_lock:
        if key in _png_cache:
            _png_cache.move_to_end(key)
            return _png_cache[key]

    if key.startswith("fc_"):
        _build_forecast(key)              # fills the PNG cache for all steps
        with _cache_lock:
            return _png_cache[key]

    png = warp_and_colorize(_fetch_source(key))
    with _cache_lock:
        _png_cache[key] = png
        while len(_png_cache) > _CACHE_MAX:
            _png_cache.popitem(last=False)
    return png


def _build_forecast(fc_key: str) -> None:
    """Compute all forecast steps for the base frame the key refers to.

    Motion is averaged over the last two frame pairs (three frames) so a
    single noisy pair can't fling the rain in a wrong direction, then the
    latest frame is advected step by step. All steps are rendered at once:
    the expensive parts (motion field) are shared between them anyway.
    """
    import nowcast

    m = FC_KEY_RE.fullmatch(fc_key)
    if not m:
        raise KeyError(f"malformed forecast key {fc_key!r}")
    base, step = m.group(1), int(m.group(2))
    if not (1 <= step <= FORECAST_STEPS):
        raise KeyError("forecast step out of range")

    with _cache_lock:
        order = list(_observed_order)
    if base not in order:
        raise KeyError("forecast base frame is no longer current; refresh")
    idx = order.index(base)
    history = order[max(0, idx - 2):idx + 1]        # up to 3 newest frames
    sources = [_fetch_source(k) for k in history]

    if len(sources) >= 2:
        fields = [nowcast.motion_field(a, b) for a, b in zip(sources, sources[1:])]
        vy = np.mean([f[0] for f in fields], axis=0)
        vx = np.mean([f[1] for f in fields], axis=0)
    else:                                           # single frame: persistence
        vy = np.zeros_like(sources[-1], dtype=np.float32)
        vx = np.zeros_like(sources[-1], dtype=np.float32)

    future = nowcast.advect(sources[-1], vy, vx, FORECAST_STEPS)
    with _cache_lock:
        for i, arr in enumerate(future, start=1):
            _png_cache[f"fc_{base}_{i}"] = warp_and_colorize(arr)
        while len(_png_cache) > _CACHE_MAX:
            _png_cache.popitem(last=False)


def warp_and_colorize(src: np.ndarray) -> bytes:
    """The actual transformation of one source array into a display PNG."""
    src_h, src_w = src.shape
    m = _mapping(src_w, src_h)

    warped = src[m.rows, m.cols]          # nearest-neighbour resample, fully vectorized
    rgba = _LUT[warped]                   # colorize via lookup table
    rgba[~m.valid] = 0                    # outside the source image -> transparent

    img = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
