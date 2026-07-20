"""
Extrapolation nowcast ("Lagrangian persistence").

Idea: rain fields mostly *move* over 5-minute timescales; growth and decay
are slower. So:

  1. Estimate a motion field from the most recent observed frames.
  2. Push (advect) the latest frame along that field, one 5-minute step at
     a time, to produce short-range forecast frames.

Motion estimation — blockwise phase correlation
-----------------------------------------------
If a block of the current frame is just a shifted copy of the same block in
the previous frame, curr(x) = prev(x - d), then their Fourier transforms
differ only by a phase ramp:  F_curr = F_prev * exp(-2*pi*i*k.d/N).
The inverse FFT of the *normalized* cross-spectrum,

    R = F_curr * conj(F_prev) / |F_curr * conj(F_prev)|,

is therefore (ideally) a delta function peaking exactly at d. Finding the
peak per 64-pixel block gives a grid of displacement vectors. Blocks with
too little echo can't be correlated and are filled with the median motion
of the blocks that could; the grid is then smoothed and upsampled.

Advection — backward semi-Lagrangian
------------------------------------
To build the next frame we sample *backward*: next(x) = curr(x - v(x)).
Asking "where did this pixel's rain come from?" (instead of pushing source
pixels forward) guarantees every output pixel receives exactly one value:
no holes, no double-writes — the same reasoning as the map reprojection.

Limits (by construction)
------------------------
Nothing intensifies, dissipates or forms; cells that die keep sailing on.
Useful out to roughly 30-60 minutes; we default to 6 steps = +30 min.
Professional nowcasts start from exactly this scheme and add stochastic
growth/decay models on top.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

BLOCK = 64          # px per correlation block (~128 km at 2 km/px)
MAX_SHIFT = 12      # px per 5-min step (~29 km => ~350 km/h ceiling, generous)
MIN_ECHO_FRACTION = 0.02   # a block needs >=2% rainy pixels to be trusted
DBZ_THRESHOLD_VALUE = 88   # uint8 value for ~5 dBZ ((5+30)/0.4)


def _block_shift(prev_block: np.ndarray, curr_block: np.ndarray) -> tuple[float, float] | None:
    """Phase-correlation displacement (dy, dx) of one block, or None if untrustable."""
    rainy_prev = (prev_block > DBZ_THRESHOLD_VALUE).mean()
    rainy_curr = (curr_block > DBZ_THRESHOLD_VALUE).mean()
    if rainy_prev < MIN_ECHO_FRACTION or rainy_curr < MIN_ECHO_FRACTION:
        return None

    window = np.hanning(BLOCK)          # taper edges so the FFT doesn't see
    window = np.outer(window, window)   # the block border as a fake feature
    f_prev = np.fft.fft2(prev_block.astype(np.float32) * window)
    f_curr = np.fft.fft2(curr_block.astype(np.float32) * window)

    cross = f_curr * np.conj(f_prev)
    cross /= np.abs(cross) + 1e-9
    corr = np.fft.fftshift(np.real(np.fft.ifft2(cross)))

    # Only consider physically plausible shifts around the center.
    c = BLOCK // 2
    search = corr[c - MAX_SHIFT:c + MAX_SHIFT + 1, c - MAX_SHIFT:c + MAX_SHIFT + 1]
    peak = np.unravel_index(np.argmax(search), search.shape)
    return float(peak[0] - MAX_SHIFT), float(peak[1] - MAX_SHIFT)


def motion_field(prev: np.ndarray, curr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Full-resolution (vy, vx) in pixels per frame step, curr(x) ~ prev(x - v)."""
    # Nodata (255) must not correlate as if it were extreme rain.
    prev = np.where(prev == 255, 0, prev)
    curr = np.where(curr == 255, 0, curr)
    h, w = curr.shape
    rows, cols = h // BLOCK, w // BLOCK

    vy = np.full((rows, cols), np.nan)
    vx = np.full((rows, cols), np.nan)
    for r in range(rows):
        for c in range(cols):
            sl = np.s_[r * BLOCK:(r + 1) * BLOCK, c * BLOCK:(c + 1) * BLOCK]
            shift = _block_shift(prev[sl], curr[sl])
            if shift is not None:
                vy[r, c], vx[r, c] = shift

    valid = ~np.isnan(vy)
    if not valid.any():                       # no rain anywhere -> persistence
        return np.zeros((h, w), np.float32), np.zeros((h, w), np.float32)

    # Gap-fill with the median of trusted blocks, then smooth twice (3x3 mean)
    # so neighbouring blocks can't disagree wildly.
    vy = np.where(valid, vy, np.nanmedian(vy)).astype(np.float32)
    vx = np.where(valid, vx, np.nanmedian(vx)).astype(np.float32)
    for _ in range(2):
        vy = _mean3(vy)
        vx = _mean3(vx)

    # Upsample the block grid to full resolution (bilinear).
    vy_full = np.asarray(Image.fromarray(vy, "F").resize((w, h), Image.BILINEAR))
    vx_full = np.asarray(Image.fromarray(vx, "F").resize((w, h), Image.BILINEAR))
    return vy_full, vx_full


def _mean3(a: np.ndarray) -> np.ndarray:
    """3x3 mean filter with edge padding (tiny arrays, loops are fine)."""
    padded = np.pad(a, 1, mode="edge")
    return sum(padded[dy:dy + a.shape[0], dx:dx + a.shape[1]]
               for dy in range(3) for dx in range(3)) / 9.0


def advect(frame: np.ndarray, vy: np.ndarray, vx: np.ndarray, steps: int) -> list[np.ndarray]:
    """Push `frame` along the motion field; returns one array per future step."""
    frame = np.where(frame == 255, 0, frame)   # treat outside-coverage as no echo
    h, w = frame.shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)

    # Backward sampling coordinates for ONE step, rounded to nearest pixel
    # (nearest keeps dBZ values intact instead of blurring them each step).
    sy = np.clip(np.rint(yy - vy).astype(np.int32), 0, h - 1)
    sx = np.clip(np.rint(xx - vx).astype(np.int32), 0, w - 1)
    inside = (yy - vy >= 0) & (yy - vy < h) & (xx - vx >= 0) & (xx - vx < w)

    out, current = [], frame
    for _ in range(steps):
        current = np.where(inside, current[sy, sx], 0).astype(np.uint8)
        out.append(current)
    return out
