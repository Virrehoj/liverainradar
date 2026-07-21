# Nederbörd — live rain radar over Sweden

A Swedish-language web app that animates SMHI's weather radar on a
full-screen map, with place search, geolocation, and a full SMHI point
forecast (temperature, feels-like, wind/gusts, humidity, pressure,
visibility, cloud cover, sunrise/sunset) for the searched location.

Python (Flask) backend, plain HTML/CSS/JS + Leaflet frontend. No API keys —
sunrise/sunset is computed locally with `astral`, not fetched.

## Run it

```bash
pip install -r requirements.txt
python app.py
# open http://127.0.0.1:5000
```

## How it works

```
Browser (Leaflet map)
   │  /api/radar/frames          what frames exist + where the overlay goes
   │  /api/radar/image/<key>     one colorized PNG per timestamp
   │  /api/geocode?q=...         place search
   │  /api/forecast?lat&lon      hourly forecast
   ▼
Flask (app.py) ── radar.py ──► SMHI radar API (GeoTIFF composites)
   │
   ├──► SMHI point forecast API (pmp3g)
   └──► OpenStreetMap Nominatim (geocoding)
```

The browser never calls SMHI or Nominatim directly. Proxying through Flask
avoids CORS problems and keeps all the data-format knowledge in one place.

### The projection problem (the core of this project)

SMHI publishes the radar composite in **SWEREF99 TM (EPSG:3006)**, Sweden's
national transverse Mercator projection, with fixed corner coordinates
(E 126 648, N 5 983 984) to (E 1 075 693, N 7 771 252). Web maps render in
**Web Mercator (EPSG:3857)**. These projections bend the Earth differently: a
rectangle in one is a curved quadrilateral in the other. If you stretched the
SMHI image directly between lat/lon corners, rain in northern Sweden would be
drawn tens of kilometres from where it actually is.

`radar.py` therefore *warps* every frame using inverse mapping:

1. Compute the radar area's footprint in Web Mercator (sampling the edges,
   not just corners — edges bow outward when reprojected).
2. For every **output** pixel, transform its Web Mercator coordinate back
   into SWEREF99 TM and take the nearest source pixel.
3. Pixels that land outside the source image become transparent.

Going output→source (instead of source→output) guarantees each output pixel
gets exactly one value — no holes, no overlaps. The pixel-index mapping
depends only on the geometry, never on the weather, so it is computed once
(`@lru_cache`) and reused for every frame: after the first frame, rendering
takes under 0.1 s.

Because the warped image is linear in EPSG:3857 — the same CRS Leaflet
displays in — `L.imageOverlay` can place it *exactly* using its lat/lon
corner bounds.

### From pixel values to colors

The GeoTIFF pixels are bytes encoding radar reflectivity:

```
dBZ = pixel × 0.4 − 30        (SMHI's documented gain/offset)
0   = inside coverage, no echo
255 = outside radar coverage
```

dBZ is logarithmic: ~5 dBZ is the precipitation threshold, ~25 dBZ steady
rain, 45+ dBZ a downpour or hail. A precomputed 256-entry RGBA lookup table
maps every byte to a color in one array operation; below-threshold and
no-data values get alpha 0 so the base map shows through.

### The animation

The frontend loads the last 12 frames (one per 5 minutes ≈ the last hour) as
stacked `L.imageOverlay`s, all at opacity 0, and animates by flipping
opacities. Keeping every frame mounted means the browser holds the decoded
images in memory: no flicker, no re-downloads while looping. Rendered PNGs
are also cached server-side keyed by frame timestamp (a past timestamp's
image never changes, hence the long `Cache-Control`).

The frame list refreshes every 5 minutes; if SMHI has published new frames
the overlay stack is rebuilt and the loop continues from the newest frame.

### The nowcast (+30 min forecast)

The timeline extends past "now" with six forecast frames (amber ticks, a
`FORECAST +N min` badge, slightly lighter overlay). They come from
`nowcast.py`, an *extrapolation nowcast*:

1. **Dense motion estimation.** The frame is scanned with overlapping
   32-pixel windows every 16 pixels (~a vector every 32 km), each measured by
   phase correlation against the previous frame, so nearby showers can move
   in different directions. Interpolated to full resolution, every rain
   pixel gets its own vector. A caveat from physics — the *aperture
   problem* — means a pixel can't be tracked in isolation: the featureless
   interior of a rain area looks identical from frame to frame and carries
   no motion information at all. Three gates keep such degenerate cases from
   polluting the field: windows whose content didn't change are treated as
   "no information" (not "zero motion"); a correlation peak must stand well
   above its surface's noise floor to count; and vectors disagreeing with
   their neighbourhood median are rejected as rogue votes. Untracked areas
   then inherit motion from their *nearest* trusted vectors by diffusion —
   never from a global average that could belong to a different weather
   system. Fields from the last two frame pairs are averaged for stability.
2. **Advection.** The latest frame is pushed along that field one 5-minute
   step at a time, sampling *backward* (`next(x) = curr(x − v(x))`) so every
   future pixel gets exactly one value — the same hole-free logic as the map
   reprojection.

The estimated overall drift (speed + compass direction) is exposed at
`/api/radar/motion` and shown in the status corner, so the forecast's
assumption can always be checked against what the animation shows.

This is called **Lagrangian persistence**: rain keeps moving the way it has
been moving. Its blind spots follow directly from the construction — nothing
grows, decays, or forms, and dying cells sail on unchanged — which is why the
forecast is capped at +30 minutes and visually marked everywhere. SMHI's own
nowcasts start from this exact scheme and add growth/decay modelling on top.

## Coverage and limits

- The radar composite covers Sweden and its immediate surroundings; the
  forecast API covers roughly the Nordics. Searching for Tokyo will move the
  map but show no radar and return a "outside forecast area" message.
- Be polite to the free services: Nominatim requires a meaningful
  `User-Agent` (set in `radar.USER_AGENT` — put your contact info there) and
  at most ~1 request/second, which the debounced search respects in practice.

## Files

| File | Role |
|---|---|
| `app.py` | Flask routes: page + 4 JSON/PNG endpoints |
| `radar.py` | Frame listing, download, warp, colorize, caching |
| `nowcast.py` | Motion estimation (phase correlation) + advection forecast |
| `templates/index.html` | Page structure (Swedish) |
| `static/style.css` | Light "glass HUD over the map" styling, with a dark-mode variant |
| `static/app.js` | Map, animation loop, theme toggle, search, geolocation, forecast panel |
