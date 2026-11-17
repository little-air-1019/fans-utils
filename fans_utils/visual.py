"""Visual change guard for MacOSClickDriver: did the target window's content change after a click?

Purpose: stop repeated click attempts once the screen moved on. A change means "hand off to the
user"; it never means the application succeeded, was accepted, or even that the phone got the tap.

Capture: CoreGraphics `CGWindowListCreateImage` of the target window by window number (stdlib
ctypes, no subprocess, no compiling). Verified 2026-09-22 on macOS 26.4: returns real BGRA pixels
of the iPhone Mirroring window in 12-38 ms. It is deprecated by Apple; prepare() fails loudly if
it ever returns NULL. Window capture excludes the cursor and ignores occlusion (occlusion is the
click driver's guard). Requires Screen Recording permission for the hosting terminal app; this
module only checks (`CGPreflightScreenCaptureAccess`), it never prompts.

Freshness: every frame is stamped with time.monotonic() right after the capture call returns and
changed() refuses frames older than STALE_MS. That proves the Mac-side capture is fresh; it does
NOT prove the mirrored phone stream is live (a paused mirror is a fresh capture of a frozen image).

ROI: window content below ROI_TOP_PT logical points (skips the Mac title bar and the phone's status
bar / clock), down to the window bottom (Apply lives there). Sampled on a grid, green channel,
quantized to 32 levels.

Detection is per TILE, not over the whole ROI: the ROI is cut into TILE_ROWS x TILE_COLS bands and
changed() is True when ANY tile's differing fraction reaches TILE_CHANGE_FRACTION. A whole-ROI
threshold would miss a small spinner or an Apply -> Cancel label swap, because such a change is a
fraction of a percent of the window; inside its own tile it is large. Thresholds are measured
against the live idle window with `watch`; see docs/macos-driver.md.

Entry point (read-only, posts nothing):
    python3 -m fans_utils.visual watch --path PATH [--seconds 5] [--interval 0.2]
"""

from __future__ import annotations

import argparse
import ctypes
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .macos import (
    _CF,
    _CG,
    DEFAULT_CALIBRATION_PATH,
    DriverError,
    MacOSClickDriver,
    Window,
    _CGPoint,
    _CGRect,
    _CGSize,
)

ROI_TOP_PT = 90.0  # logical points from the window top to skip: Mac title bar + phone status bar/clock
STALE_MS = 250.0
# The mirrored image is a live video stream, so two captures may differ at the byte level even with
# no visible motion. A per-byte MAGNITUDE threshold is what separates codec dither from a real
# repaint; bucket-quantizing is not enough, because a +-1 delta across a bucket edge still flips.
#
# Provenance of these numbers, stated precisely (2026-09-22, macOS 26.4):
#   - An early histogram (66% of bytes identical, 96% within +-4, max delta 40) was taken during a
#     window that was LATER FOUND to include external phone activity. It is not an idle measurement
#     and is not what these constants rest on.
#   - The operative floor, on the confirmed-still page: no sampled byte exceeded PIXEL_DELTA across
#     11 consecutive capture pairs (~300k sample comparisons), i.e. worst-tile fraction 0.00000.
#     Two static non-video windows (CotEditor, Dia) also measured 0.00000 over 8 samples each.
PIXEL_DELTA = 24  # a sample counts as different only when the green channel moves by more than this
TILE_ROWS = 12  # ROI is cut into TILE_ROWS x TILE_COLS bands; a spinner lives inside one of them
TILE_COLS = 4
# On the real geometry (696x1532 capture, step 6) a tile holds ~522 samples, so this threshold needs
# ~16 changed samples: a 24x24 px / 12x12 pt blob. Sized deliberately below a 20 pt iOS activity
# indicator (40 px = ~44 samples = 8.5% of a tile), which at 0.08 would sit ON the threshold and be
# missed outright when it straddles a tile edge and each half counts separately. The measured noise
# floor is 0.00000, so there is no cost to the headroom.
TILE_CHANGE_FRACTION = 0.03
SAMPLE_TARGET = 20_000  # sampled pixels per frame; keeps changed() at C speed via slicing

SCREEN_RECORDING_HELP = (
    "Screen Recording permission is missing for the app that hosts this terminal. Grant it in "
    "System Settings > Privacy & Security > Screen & System Audio Recording, restart that terminal "
    "app, then re-run. This tool never prompts or changes settings."
)


class VisualGuardError(DriverError):
    """The visual guard could not produce a trustworthy answer. Callers must treat it as STOP."""


@dataclass(frozen=True)
class Frame:
    width: int
    height: int
    bytes_per_row: int  # may exceed width*4: rows are padded
    pixels: bytes  # BGRA, 32 bpp
    captured_monotonic: float  # time.monotonic() right after the capture call returned
    window_number: int


@dataclass(frozen=True)
class Signature:
    """Sampled ROI split into tiles. Comparison is per tile so a small localized change wins."""

    tiles: tuple[bytes, ...]
    row_len: int  # samples per sampled row, for locating a tile in the report

    def __len__(self) -> int:
        return sum(len(t) for t in self.tiles)


Capture = Callable[[Window], "Frame | None"]


class _CaptureBackend(Protocol):
    def __call__(self, window: Window) -> Frame | None: ...
    def screen_recording_allowed(self) -> bool: ...


# --------------------------------------------------------------------------- comparison


def roi_signature(frame: Frame, window_height_pt: float) -> Signature:
    """Sampled, quantized green-channel bytes of the ROI, cut into TILE_ROWS x TILE_COLS tiles.
    Pure slicing and bytes.translate: no per-pixel Python loop."""
    if frame.width <= 0 or frame.height <= 0 or window_height_pt <= 0:
        raise VisualGuardError(f"degenerate frame {frame.width}x{frame.height}")
    if len(frame.pixels) < frame.bytes_per_row * frame.height:
        raise VisualGuardError("frame buffer shorter than bytes_per_row * height")
    scale = frame.height / window_height_pt
    top = min(frame.height - 1, int(ROI_TOP_PT * scale))
    rows = frame.height - top
    step = max(1, int(math.sqrt(rows * frame.width / SAMPLE_TARGET)))
    bpr, w4 = frame.bytes_per_row, frame.width * 4
    # Green channel (offset 1 in BGRA), every `step`-th pixel of every `step`-th row.
    lines = [frame.pixels[r * bpr + 1 : r * bpr + w4 : step * 4] for r in range(top, frame.height, step)]
    lines = [ln for ln in lines if ln]
    if not lines:
        raise VisualGuardError("ROI sampled to nothing; window too small or ROI_TOP_PT too large")
    row_len = len(lines[0])
    lines = [ln for ln in lines if len(ln) == row_len]
    band = max(1, len(lines) // TILE_ROWS)
    col = max(1, row_len // TILE_COLS)
    tiles = []
    for r0 in range(0, len(lines), band):
        for c0 in range(0, row_len, col):
            tiles.append(b"".join(ln[c0 : c0 + col] for ln in lines[r0 : r0 + band]))
    return Signature(tuple(t for t in tiles if t), row_len)


def changed_fraction(a: Signature, b: Signature) -> tuple[float, int]:
    """(worst tile's differing fraction, that tile's index). Any tile crossing the threshold is a
    change: a spinner or a button label is a large fraction of its own tile and a tiny one overall.

    A sample counts only when |green_a - green_b| > PIXEL_DELTA, which skips the mirrored stream's
    codec dither. The subtraction runs in C via int.from_bytes on the whole tile."""
    if len(a.tiles) != len(b.tiles) or not a.tiles:
        raise VisualGuardError(f"signature tile count mismatch ({len(a.tiles)} vs {len(b.tiles)})")
    worst, where = 0.0, -1
    for i, (ta, tb) in enumerate(zip(a.tiles, b.tiles)):
        if len(ta) != len(tb) or not ta:
            raise VisualGuardError(f"signature tile {i} size mismatch ({len(ta)} vs {len(tb)})")
        over = sum(1 for d in map(_abs_diff, ta, tb) if d > PIXEL_DELTA)
        frac = over / len(ta)
        if frac > worst:
            worst, where = frac, i
    return worst, where


def _abs_diff(p: int, q: int) -> int:
    return p - q if p > q else q - p


# --------------------------------------------------------------------------- guard


class MacOSVisualGuard:
    """prepare() at preflight, arm() right before the FIRST click, changed() after each click.
    Every method that cannot answer raises VisualGuardError (or PermissionError); none of them
    ever returns "unchanged" on a missing, blank, stale or mismatched capture."""

    def __init__(self, driver: MacOSClickDriver, *, capture: Capture | None = None) -> None:
        self._driver = driver
        self._capture: Capture = capture if capture is not None else CoreGraphicsCapture()
        self._anchor: tuple[Frame, Signature] | None = None  # from prepare(): the scene we agreed to click
        self._baseline: tuple[Frame, Signature] | None = None  # from arm(): what the first click saw
        self._closed = False
        self.last_fraction: float | None = None
        self.last_tile: int | None = None
        self.last_capture_ms: float | None = None
        self.last_compare_ms: float | None = None

    def _window(self) -> Window:
        if self._closed:
            raise VisualGuardError("visual guard is closed")
        w = self._driver.target_window
        if w is None:
            raise VisualGuardError("driver.preflight() has not run; no target window to capture")
        return w

    def _grab(self) -> tuple[Frame, Signature]:
        window = self._window()
        t0 = time.perf_counter()
        frame = self._capture(window)
        self.last_capture_ms = (time.perf_counter() - t0) * 1000.0
        if frame is None:
            raise VisualGuardError("capture returned nothing (window gone, or Screen Recording denied)")
        if frame.window_number != window.number:
            raise VisualGuardError(f"captured window {frame.window_number}, expected {window.number}")
        age_ms = (time.monotonic() - frame.captured_monotonic) * 1000.0
        if not 0 <= age_ms <= STALE_MS:
            raise VisualGuardError(f"capture is {age_ms:.0f} ms old (limit {STALE_MS:g} ms); refusing to compare")
        sig = roi_signature(frame, window.height)
        if sum(len(set(t)) for t in sig.tiles) <= len(sig.tiles):
            raise VisualGuardError("capture ROI is uniform (blank/black frame); Screen Recording denied or window empty")
        return frame, sig

    def _compare(self, reference: tuple[Frame, Signature], frame: Frame, sig: Signature) -> float:
        ref_frame, ref_sig = reference
        if (frame.width, frame.height, frame.bytes_per_row) != (ref_frame.width, ref_frame.height, ref_frame.bytes_per_row):
            raise VisualGuardError(
                f"frame geometry changed {ref_frame.width}x{ref_frame.height} -> {frame.width}x{frame.height}; "
                "window resized or the mirror reconnected"
            )
        t0 = time.perf_counter()
        fraction, tile = changed_fraction(ref_sig, sig)
        self.last_compare_ms = (time.perf_counter() - t0) * 1000.0
        self.last_fraction, self.last_tile = fraction, tile
        return fraction

    def prepare(self) -> None:
        """Read-only: permission check plus the ANCHOR capture of the scene we agreed to click.
        arm() later refuses if the scene drifted from this anchor. Raises; never prompts."""
        allowed = getattr(self._capture, "screen_recording_allowed", None)
        if allowed is not None and not allowed():
            raise PermissionError(SCREEN_RECORDING_HELP)
        self._anchor = self._grab()

    def arm(self) -> None:
        """Capture the baseline for the FIRST click (core may call this ~100 ms early).

        Refuses when the scene drifted from prepare()'s anchor: a paused or disconnected mirror
        overlay, or the user navigating away, must abort before the first click rather than become
        the new baseline (which would silently make the click blind)."""
        if self._anchor is None:
            raise VisualGuardError("prepare() has not run; no anchor to check drift against")
        frame, sig = self._grab()
        drift = self._compare(self._anchor, frame, sig)
        if drift >= TILE_CHANGE_FRACTION:
            raise VisualGuardError(
                f"scene drifted since prepare() (tile {self.last_tile} changed {drift:.1%}, limit "
                f"{TILE_CHANGE_FRACTION:.0%}): the page moved, the mirror paused or reconnected. "
                "Refusing to re-baseline; re-run prepare() once the intended screen is showing."
            )
        self._baseline = (frame, sig)

    def changed(self) -> bool:
        """Fresh capture vs the armed baseline. True = content changed = stop and hand off to the
        user. It is NOT a success signal: it says the screen moved, nothing about the application."""
        if self._closed:
            raise VisualGuardError("visual guard is closed")
        if self._baseline is None:
            raise VisualGuardError("arm() has not run; no baseline")
        frame, sig = self._grab()
        return self._compare(self._baseline, frame, sig) >= TILE_CHANGE_FRACTION

    def close(self) -> None:
        self._closed = True
        self._anchor = None
        self._baseline = None
        close = getattr(self._capture, "close", None)
        if close is not None:
            close()


# --------------------------------------------------------------------------- ctypes capture

_kCGWindowListOptionIncludingWindow = 1 << 3
_kCGWindowImageBoundsIgnoreFraming = 1 << 0


class CoreGraphicsCapture:
    """Window-number capture through CGWindowListCreateImage. One CFData copy per frame."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise DriverError("CoreGraphicsCapture only runs on macOS")
        c = ctypes
        self.cg = c.CDLL(_CG)
        self.cf = c.CDLL(_CF)
        cg, cf = self.cg, self.cf
        cg.CGPreflightScreenCaptureAccess.restype = c.c_bool
        cg.CGPreflightScreenCaptureAccess.argtypes = []
        cg.CGWindowListCreateImage.restype = c.c_void_p
        cg.CGWindowListCreateImage.argtypes = [_CGRect, c.c_uint32, c.c_uint32, c.c_uint32]
        for name in ("CGImageGetWidth", "CGImageGetHeight", "CGImageGetBytesPerRow"):
            getattr(cg, name).restype = c.c_size_t
            getattr(cg, name).argtypes = [c.c_void_p]
        cg.CGImageGetDataProvider.restype = c.c_void_p
        cg.CGImageGetDataProvider.argtypes = [c.c_void_p]
        cg.CGDataProviderCopyData.restype = c.c_void_p
        cg.CGDataProviderCopyData.argtypes = [c.c_void_p]
        cf.CFDataGetLength.restype = c.c_long
        cf.CFDataGetLength.argtypes = [c.c_void_p]
        cf.CFDataGetBytePtr.restype = c.c_void_p
        cf.CFDataGetBytePtr.argtypes = [c.c_void_p]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [c.c_void_p]

    def screen_recording_allowed(self) -> bool:
        return bool(self.cg.CGPreflightScreenCaptureAccess())

    def __call__(self, window: Window) -> Frame | None:
        rect = _CGRect(_CGPoint(window.x, window.y), _CGSize(window.width, window.height))
        img = self.cg.CGWindowListCreateImage(
            rect, _kCGWindowListOptionIncludingWindow, window.number, _kCGWindowImageBoundsIgnoreFraming)
        stamp = time.monotonic()
        if not img:
            return None
        try:
            width, height, bpr = self.cg.CGImageGetWidth(img), self.cg.CGImageGetHeight(img), self.cg.CGImageGetBytesPerRow(img)
            data = self.cg.CGDataProviderCopyData(self.cg.CGImageGetDataProvider(img))
            if not data:
                return None
            try:
                pixels = ctypes.string_at(self.cf.CFDataGetBytePtr(data), self.cf.CFDataGetLength(data))
            finally:
                self.cf.CFRelease(data)
        finally:
            self.cf.CFRelease(img)
        return Frame(width, height, bpr, pixels, stamp, window.number)

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- CLI


def watch(driver: MacOSClickDriver, *, seconds: float, interval: float, capture: Capture | None = None,
          out=None, sleep=time.sleep) -> list[float]:
    """Read-only: arm, then print the changed fraction every `interval` for `seconds`. Posts nothing.
    Use it to see idle noise on the live window and to confirm a page change registers."""
    out = out or sys.stdout
    print("watch: READ-ONLY. Captures the target window; no mouse event is posted.", file=out)
    for w in driver.preflight():
        print(f"WARNING: {w}", file=out)
    guard = MacOSVisualGuard(driver, capture=capture)
    fractions: list[float] = []
    try:
        guard.prepare()
        guard.arm()
        print(f"armed: capture {guard.last_capture_ms:.1f} ms, drift from anchor {guard.last_fraction:.4f}; "
              f"tiles {TILE_ROWS}x{TILE_COLS}, trip at {TILE_CHANGE_FRACTION:.0%} of one tile", file=out)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            sleep(interval)
            flag = guard.changed()
            fractions.append(guard.last_fraction or 0.0)
            print(f"  changed={flag!s:5} worst_tile={guard.last_tile:3d} fraction={guard.last_fraction:.4f} "
                  f"capture={guard.last_capture_ms:.1f} ms compare={guard.last_compare_ms:.2f} ms", file=out)
    finally:
        guard.close()
    if fractions:
        s = sorted(fractions)
        print(f"worst-tile fractions over {len(s)} samples: max={s[-1]:.4f} p95={s[max(0, math.ceil(len(s) * 0.95) - 1)]:.4f} "
              f"median={s[len(s) // 2]:.4f}", file=out)
        print("note: these are IDLE-noise numbers unless you changed the screen during the watch.", file=out)
    return fractions


def main(argv: list[str] | None = None, *, driver: MacOSClickDriver | None = None, capture: Capture | None = None,
         out=None) -> int:
    out = out or sys.stdout
    p = argparse.ArgumentParser(prog="python3 -m fans_utils.visual", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    w = sub.add_parser("watch", help="read-only: arm on the target window and print change fractions")
    w.add_argument("--path", type=Path, default=DEFAULT_CALIBRATION_PATH, help="calibration file")
    w.add_argument("--bundle", default=None, help="target bundle id (mock validation only)")
    w.add_argument("--seconds", type=float, default=5.0)
    w.add_argument("--interval", type=float, default=0.2)
    args = p.parse_args(argv)
    if not (0 < args.interval <= args.seconds <= 3600):
        print("error: need 0 < --interval <= --seconds <= 3600", file=out)
        return 2
    try:
        d = driver or MacOSClickDriver(expected_bundle_id=args.bundle, calibration_path=args.path)
        try:
            watch(d, seconds=args.seconds, interval=args.interval, capture=capture, out=out)
        finally:
            d.close()
        return 0
    except KeyboardInterrupt:
        print("cancelled", file=out)
        return 130
    except PermissionError as exc:
        print(f"error: permission denied. {exc}", file=out)
        return 3
    except DriverError as exc:
        print(f"error: {exc}", file=out)
        return 1


if __name__ == "__main__":
    sys.exit(main())
