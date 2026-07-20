"""
Extrapolation nowcast ("Lagrangian persistence") with a dense motion field.

The pipeline:

  1. Estimate a GLOBAL displacement of the whole frame (phase correlation,
     centroid fallback) — the coarse "which way is weather going" answer.
  2. Estimate a DENSE grid of local vectors: small windows (32 px ~ 64 km)
     every 16 px, so nearby showers can move differently. Each window must
     pass three tests to be trusted (see _window_shift and the consistency
     filter below); untrusted windows inherit motion from their neighbours.
  3. Interpolate the grid to full resolution: every rain pixel gets its own
     vector.
  4. Advect the latest frame backward-sampling along that field, one
     5-minute step at a time.

Why not literally track each pixel on its own? The APERTURE PROBLEM: a
single pixel value matches countless other pixels, and the featureless
interior of a rain area looks identical from frame to frame — there is no
information there about motion. (Blocks in the interior correlate perfectly
at zero shift: they vote "no motion" when the truth is "can't tell".) All
motion information lives where the field has texture — edges, cells,
gradients — so we measure there and interpolate everywhere else.

Phase correlation, briefly: if curr(x) = prev(x - d) within a window, their
Fourier transforms differ only by a phase ramp, and the inverse FFT of the
normalized cross-spectrum peaks exactly at d. The sharpness of that peak
tells us how much to trust it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image

WINDOW = 32           # px per local window (~64 km at 2 km/px)
STRIDE = 16           # grid spacing: a vector every ~32 km
MAX_LOCAL_SHIFT = 8   # px per 5-min step (~190 km/h ceiling for local vectors)
MAX_GLOBAL_SHIFT = 15
MIN_ECHO_FRACTION = 0.03    # a window needs >=3% rainy pixels to be usable
MIN_CHANGE = 0.75           # mean |curr-prev| below this = "nothing moved here
                            # or nothing to see" -> no information, not zero
MIN_PEAK_QUALITY = 4.5      # peak must stand this many stddevs above surface
CONSISTENCY_TOL = 4.0       # px: max deviation from neighbourhood median
DBZ_THRESHOLD_VALUE = 88    # uint8 for ~5 dBZ ((5+30)/0.4)


@dataclass
class MotionDiagnostics:
    method: str               # "phase" | "centroid" | "none"
    global_dydx: tuple[float, float]
    valid_windows: int
    total_windows: int

    @property
    def speed_kmh(self) -> float:
        dy, dx = self.global_dydx           # px per 5 min, 2 km/px
        return float(np.hypot(dy, dx) * 2.0 * 12.0)

    @property
    def bearing_deg(self) -> float:
        """Compass direction the rain is moving TOWARD (row axis points south)."""
        dy, dx = self.global_dydx
        return float(np.degrees(np.arctan2(dx, -dy)) % 360.0)


# ---------------------------------------------------------------------------
# Correlation primitives
# ---------------------------------------------------------------------------

def _phase_surface(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Centered phase-correlation surface between two equally sized arrays."""
    wy = np.hanning(a.shape[0])[:, None]
    wx = np.hanning(a.shape[1])[None, :]
    fa = np.fft.fft2((a - a.mean()) * wy * wx)   # mean removal: don't let the
    fb = np.fft.fft2((b - b.mean()) * wy * wx)   # DC component drown the peak
    cross = fb * np.conj(fa)
    cross /= np.abs(cross) + 1e-9
    return np.fft.fftshift(np.real(np.fft.ifft2(cross)))


def _peak_shift(surface: np.ndarray, max_shift: int) -> tuple[float, float, float]:
    """(dy, dx, quality) of the strongest plausible peak in the surface."""
    cy, cx = surface.shape[0] // 2, surface.shape[1] // 2
    search = surface[cy - max_shift:cy + max_shift + 1,
                     cx - max_shift:cx + max_shift + 1]
    peak_idx = np.unravel_index(np.argmax(search), search.shape)
    quality = (search.max() - surface.mean()) / (surface.std() + 1e-9)
    return (float(peak_idx[0] - max_shift), float(peak_idx[1] - max_shift),
            float(quality))


def _window_shift(prev_w: np.ndarray, curr_w: np.ndarray) -> tuple[float, float] | None:
    """One local vector, or None if this window carries no motion information.

    Three gates:
      echo    — enough rainy pixels in both frames to correlate at all;
      change  — the window content actually changed (an unchanged interior
                is 'no information', NOT 'zero motion');
      quality — the correlation peak stands clearly above the noise floor,
                so a flat surface can't inject an arbitrary direction.
    """
    if (prev_w > DBZ_THRESHOLD_VALUE).mean() < MIN_ECHO_FRACTION:
        return None
    if (curr_w > DBZ_THRESHOLD_VALUE).mean() < MIN_ECHO_FRACTION:
        return None
    if np.abs(curr_w.astype(np.float32) - prev_w.astype(np.float32)).mean() < MIN_CHANGE:
        return None
    dy, dx, quality = _peak_shift(_phase_surface(prev_w, curr_w), MAX_LOCAL_SHIFT)
    if quality < MIN_PEAK_QUALITY:
        return None
    return dy, dx


# ---------------------------------------------------------------------------
# Global motion
# ---------------------------------------------------------------------------

def _global_motion(prev: np.ndarray, curr: np.ndarray) -> tuple[tuple[float, float], str]:
    rainy_prev = prev > DBZ_THRESHOLD_VALUE
    rainy_curr = curr > DBZ_THRESHOLD_VALUE
    if rainy_prev.mean() < 1e-4 or rainy_curr.mean() < 1e-4:
        return (0.0, 0.0), "none"

    dy, dx, quality = _peak_shift(
        _phase_surface(prev.astype(np.float32), curr.astype(np.float32)),
        MAX_GLOBAL_SHIFT)
    if quality >= MIN_PEAK_QUALITY:
        return (dy, dx), "phase"

    # Fallback: the "linear function" idea — mean rain coordinate then vs now.
    # (Biased when rain enters/leaves the domain edge, hence only a fallback.)
    py, px = np.nonzero(rainy_prev)
    cy, cx = np.nonzero(rainy_curr)
    return (float(cy.mean() - py.mean()), float(cx.mean() - px.mean())), "centroid"


# ---------------------------------------------------------------------------
# Dense field
# ---------------------------------------------------------------------------

def motion_field(prev: np.ndarray, curr: np.ndarray
                 ) -> tuple[np.ndarray, np.ndarray, MotionDiagnostics]:
    """Full-resolution (vy, vx) in px per frame step, plus diagnostics."""
    prev = np.where(prev == 255, 0, prev).astype(np.float32)
    curr = np.where(curr == 255, 0, curr).astype(np.float32)
    h, w = curr.shape

    (g_dy, g_dx), method = _global_motion(prev, curr)

    grid_y = range(0, h - WINDOW + 1, STRIDE)
    grid_x = range(0, w - WINDOW + 1, STRIDE)
    vy = np.full((len(list(grid_y)), len(list(grid_x))), np.nan, np.float32)
    vx = np.full_like(vy, np.nan)
    for gi, y0 in enumerate(range(0, h - WINDOW + 1, STRIDE)):
        for gj, x0 in enumerate(range(0, w - WINDOW + 1, STRIDE)):
            shift = _window_shift(prev[y0:y0 + WINDOW, x0:x0 + WINDOW],
                                  curr[y0:y0 + WINDOW, x0:x0 + WINDOW])
            if shift is not None:
                vy[gi, gj], vx[gi, gj] = shift

    total = vy.size
    valid_before = int(np.isfinite(vy).sum())

    # Consistency filter: a vector disagreeing strongly with the median of
    # its neighbourhood is a lone rogue vote, not a regional difference.
    vy, vx = _reject_outliers(vy, vx)
    valid = int(np.isfinite(vy).sum())

    diag = MotionDiagnostics(method=method, global_dydx=(g_dy, g_dx),
                             valid_windows=valid, total_windows=total)

    # Fill no-information cells from their NEAREST trusted vectors (diffusion),
    # not from the global average: with two showers moving differently, the
    # global vector would flood the wrong motion into half the map. The
    # global vector is only the last resort when nothing was trackable.
    vy = _diffusion_fill(vy, g_dy)
    vx = _diffusion_fill(vx, g_dx)
    for _ in range(2):
        vy = _mean3(vy)
        vx = _mean3(vx)

    vy_full = np.asarray(Image.fromarray(vy, "F").resize((w, h), Image.BILINEAR))
    vx_full = np.asarray(Image.fromarray(vx, "F").resize((w, h), Image.BILINEAR))
    return vy_full, vx_full, diag


def _reject_outliers(vy: np.ndarray, vx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """NaN-out vectors deviating > CONSISTENCY_TOL px from their 5x5 median."""
    out_y, out_x = vy.copy(), vx.copy()
    rows, cols = vy.shape
    for i in range(rows):
        for j in range(cols):
            if not np.isfinite(vy[i, j]):
                continue
            ny = vy[max(0, i - 2):i + 3, max(0, j - 2):j + 3]
            nx = vx[max(0, i - 2):i + 3, max(0, j - 2):j + 3]
            finite = np.isfinite(ny)
            if finite.sum() < 3:          # too lonely to judge; keep it
                continue
            med_y, med_x = np.nanmedian(ny), np.nanmedian(nx)
            if np.hypot(vy[i, j] - med_y, vx[i, j] - med_x) > CONSISTENCY_TOL:
                out_y[i, j] = np.nan
                out_x[i, j] = np.nan
    return out_y, out_x


def _diffusion_fill(v: np.ndarray, fallback: float) -> np.ndarray:
    """Grow known vectors into NaN cells: each pass, an unknown cell takes the
    mean of its finite 3x3 neighbours, until the grid is full. Cells far from
    any measurement thus inherit their *nearest* motion, not a global blend."""
    if not np.isfinite(v).any():
        return np.full_like(v, fallback, dtype=np.float32)
    filled = v.astype(np.float32).copy()
    for _ in range(filled.size):                     # generous upper bound
        nan_mask = ~np.isfinite(filled)
        if not nan_mask.any():
            break
        padded = np.pad(filled, 1, mode="edge")
        neighbours = np.stack([padded[dy:dy + v.shape[0], dx:dx + v.shape[1]]
                               for dy in range(3) for dx in range(3)])
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN cells are expected early on
            grown = np.nanmean(neighbours, axis=0)
        update = nan_mask & np.isfinite(grown)
        filled[update] = grown[update]
    return filled


def _mean3(a: np.ndarray) -> np.ndarray:
    """3x3 mean filter with edge padding (tiny arrays, loops are fine)."""
    padded = np.pad(a, 1, mode="edge")
    return (sum(padded[dy:dy + a.shape[0], dx:dx + a.shape[1]]
                for dy in range(3) for dx in range(3)) / 9.0).astype(np.float32)


# ---------------------------------------------------------------------------
# Advection (unchanged): backward semi-Lagrangian sampling
# ---------------------------------------------------------------------------

def advect(frame: np.ndarray, vy: np.ndarray, vx: np.ndarray, steps: int) -> list[np.ndarray]:
    """Push `frame` along the motion field; returns one array per future step."""
    frame = np.where(frame == 255, 0, frame)   # treat outside-coverage as no echo
    h, w = frame.shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)

    sy = np.clip(np.rint(yy - vy).astype(np.int32), 0, h - 1)
    sx = np.clip(np.rint(xx - vx).astype(np.int32), 0, w - 1)
    inside = (yy - vy >= 0) & (yy - vy < h) & (xx - vx >= 0) & (xx - vx < w)

    out, current = [], frame
    for _ in range(steps):
        current = np.where(inside, current[sy, sx], 0).astype(np.uint8)
        out.append(current)
    return out
