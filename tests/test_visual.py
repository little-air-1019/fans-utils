"""MacOSVisualGuard with an injected capture. Nothing here touches the OS, captures a real screen,
or asserts that a real UI changed. A True from changed() means pixels moved, never success."""

from __future__ import annotations

import io
import sys
import time
import unittest

from fans_utils.macos import DEFAULT_BUNDLE_ID, DriverError, MacOSClickDriver, Window
from fans_utils.visual import (
    PIXEL_DELTA,
    ROI_TOP_PT,
    STALE_MS,
    TILE_CHANGE_FRACTION,
    TILE_COLS,
    TILE_ROWS,
    CoreGraphicsCapture,
    Frame,
    MacOSVisualGuard,
    VisualGuardError,
    changed_fraction,
    roi_signature,
    watch,
)

from .test_macos import FakeNative, win

WIN = win()
SCALE = 2
W_PX, H_PX = int(WIN.width) * SCALE, int(WIN.height) * SCALE
BPR = W_PX * 4 + 32  # padded rows: the real capture had bpr 2816 for width 696 (= 2784 + 32)


def make_pixels(*, seed: int = 0, blank: bool = False, width: int = W_PX, height: int = H_PX,
                bpr: int = BPR) -> bytes:
    """Deterministic non-uniform content. Not an image of anything; just stable varied bytes."""
    if blank:
        return bytes(bpr * height)
    buf = bytearray(bpr * height)
    for r in range(height):
        row = r * bpr
        for c in range(width):
            buf[row + c * 4 + 1] = (r * 7 + c * 13 + seed) % 251  # green channel
    return bytes(buf)


def frame(pixels: bytes | None = None, *, age_ms: float = 0.0, number: int = WIN.number,
          width: int = W_PX, height: int = H_PX, bpr: int = BPR, **kw) -> Frame:
    return Frame(width=width, height=height, bytes_per_row=bpr,
                 pixels=make_pixels(**kw) if pixels is None else pixels,
                 captured_monotonic=time.monotonic() - age_ms / 1000.0, window_number=number)


class FakeCapture:
    """Returns queued frames. Records how many times it was called."""

    def __init__(self, *frames, allowed: bool = True):
        self.queue = list(frames)
        self.calls = 0
        self.allowed = allowed
        self.closed = 0

    def __call__(self, window: Window):
        self.calls += 1
        if not self.queue:
            return frame()  # stable default
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        return item() if callable(item) else item

    def screen_recording_allowed(self) -> bool:
        return self.allowed

    def close(self) -> None:
        self.closed += 1


def ready_driver(native: FakeNative | None = None) -> MacOSClickDriver:
    n = native or FakeNative()
    d = MacOSClickDriver(900.0, 500.0, native=n)
    d.preflight()
    return d


def patch_region(pixels: bytes, *, x0: int, y0: int, x1: int, y1: int, value: int = 200,
                 bpr: int = BPR) -> bytes:
    """Overwrite a rectangle's green channel, simulating a spinner or a button label swap."""
    buf = bytearray(pixels)
    for r in range(y0, y1):
        for c in range(x0, x1):
            buf[r * bpr + c * 4 + 1] = value
    return bytes(buf)


class SignatureTests(unittest.TestCase):
    def test_padded_rows_are_respected(self):
        # A signature built with width*4 instead of bytes_per_row would drift one row per line and
        # report a change between two identical frames.
        f = frame()
        self.assertEqual(changed_fraction(roi_signature(f, WIN.height), roi_signature(f, WIN.height))[0], 0.0)

    def test_identical_content_in_two_captures_is_zero(self):
        a, b = frame(seed=0), frame(seed=0)
        self.assertEqual(changed_fraction(roi_signature(a, WIN.height), roi_signature(b, WIN.height))[0], 0.0)

    def test_dither_below_pixel_delta_is_not_a_change(self):
        base = make_pixels()
        noisy = bytearray(base)
        for i in range(1, len(noisy), 4):  # nudge every green byte by less than the threshold
            noisy[i] = (noisy[i] + PIXEL_DELTA - 1) % 256 if noisy[i] < 200 else noisy[i]
        fraction, _ = changed_fraction(roi_signature(frame(base), WIN.height),
                                       roi_signature(frame(bytes(noisy)), WIN.height))
        self.assertLess(fraction, TILE_CHANGE_FRACTION,
                        "sub-threshold dither must not read as a change")

    def test_roi_excludes_the_top_of_the_window(self):
        # Repainting only above ROI_TOP_PT (mac title bar + phone clock) must not register.
        base = make_pixels()
        top_px = int(ROI_TOP_PT * SCALE)
        changed_top = patch_region(base, x0=0, y0=0, x1=W_PX, y1=top_px)
        fraction, _ = changed_fraction(roi_signature(frame(base), WIN.height),
                                       roi_signature(frame(changed_top), WIN.height))
        self.assertEqual(fraction, 0.0, "the clock strip must be outside the ROI")

    def test_spinner_sized_change_at_a_tile_boundary_trips(self):
        """The coordinator's acceptance case: a small element, not a whole tile, and deliberately
        NOT aligned to the tile grid so it splits across tiles and each half counts separately."""
        base = make_pixels()
        top_px = int(ROI_TOP_PT * SCALE)
        tile_h = (H_PX - top_px) // TILE_ROWS
        tile_w = W_PX // TILE_COLS
        # 40x40 px ~= a 20 pt iOS activity indicator, centred on a tile corner.
        cx, cy = tile_w * 2, top_px + tile_h * 5
        spinner = patch_region(base, x0=cx - 20, y0=cy - 20, x1=cx + 20, y1=cy + 20)
        fraction, _ = changed_fraction(roi_signature(frame(base), WIN.height),
                                       roi_signature(frame(spinner), WIN.height))
        self.assertGreaterEqual(fraction, TILE_CHANGE_FRACTION,
                                "a spinner straddling a tile boundary must still stop the retry loop")

    def test_button_label_sized_change_trips(self):
        """Apply -> Cancel: a text swap inside the button, far smaller than a tile."""
        base = make_pixels()
        top_px = int(ROI_TOP_PT * SCALE)
        y0 = top_px + ((H_PX - top_px) * 9) // 12  # near the bottom, where Apply lives
        label = patch_region(base, x0=W_PX // 3, y0=y0, x1=W_PX // 3 + 60, y1=y0 + 26)
        fraction, _ = changed_fraction(roi_signature(frame(base), WIN.height),
                                       roi_signature(frame(label), WIN.height))
        self.assertGreaterEqual(fraction, TILE_CHANGE_FRACTION, "a button label swap must trip")

    def test_small_localized_change_trips_a_tile(self):
        # A spinner-sized repaint: one tile of the 12x4 grid, far smaller than 8% of the whole ROI.
        base = make_pixels()
        top_px = int(ROI_TOP_PT * SCALE)
        tile_h = (H_PX - top_px) // TILE_ROWS
        tile_w = W_PX // TILE_COLS
        y0 = top_px + tile_h * 6
        spinner = patch_region(base, x0=tile_w, y0=y0, x1=tile_w * 2, y1=y0 + tile_h)
        fraction, tile = changed_fraction(roi_signature(frame(base), WIN.height),
                                          roi_signature(frame(spinner), WIN.height))
        self.assertGreaterEqual(fraction, TILE_CHANGE_FRACTION, "one repainted tile must trip")
        self.assertGreaterEqual(tile, 0)
        whole_roi = (tile_w * tile_h) / (W_PX * (H_PX - top_px))
        self.assertLess(whole_roi, TILE_CHANGE_FRACTION,
                        "sanity: this change is below a whole-ROI threshold, which is why tiles exist")


class GuardTests(unittest.TestCase):
    def test_prepare_requires_screen_recording_permission(self):
        g = MacOSVisualGuard(ready_driver(), capture=FakeCapture(allowed=False))
        with self.assertRaisesRegex(PermissionError, "Screen Recording"):
            g.prepare()

    def test_prepare_before_preflight_raises(self):
        d = MacOSClickDriver(900.0, 500.0, native=FakeNative())  # no preflight
        with self.assertRaisesRegex(VisualGuardError, "preflight"):
            MacOSVisualGuard(d, capture=FakeCapture()).prepare()

    def test_missing_capture_raises_and_never_returns_unchanged(self):
        g = MacOSVisualGuard(ready_driver(), capture=FakeCapture(None))
        with self.assertRaisesRegex(VisualGuardError, "capture returned nothing"):
            g.prepare()

    def test_blank_capture_raises(self):
        g = MacOSVisualGuard(ready_driver(), capture=FakeCapture(frame(blank=True)))
        with self.assertRaisesRegex(VisualGuardError, "uniform"):
            g.prepare()

    def test_stale_capture_raises(self):
        g = MacOSVisualGuard(ready_driver(), capture=FakeCapture(frame(age_ms=STALE_MS + 50)))
        with self.assertRaisesRegex(VisualGuardError, "old"):
            g.prepare()

    def test_capture_of_the_wrong_window_raises(self):
        g = MacOSVisualGuard(ready_driver(), capture=FakeCapture(frame(number=WIN.number + 1)))
        with self.assertRaisesRegex(VisualGuardError, "expected"):
            g.prepare()

    def test_changed_before_arm_raises(self):
        g = MacOSVisualGuard(ready_driver(), capture=FakeCapture())
        g.prepare()
        with self.assertRaisesRegex(VisualGuardError, "arm"):
            g.changed()

    def test_arm_before_prepare_raises(self):
        g = MacOSVisualGuard(ready_driver(), capture=FakeCapture())
        with self.assertRaisesRegex(VisualGuardError, "prepare"):
            g.arm()

    def test_arm_refuses_when_the_scene_drifted_from_the_anchor(self):
        # The coordinator's requirement: a paused/disconnected mirror or a page change between
        # prepare() and arm() must ABORT, not silently become the new baseline.
        # A different page, not a flat fill: still varied content, so the blank check is not what fires.
        cap = FakeCapture(frame(seed=0), frame(seed=101))
        g = MacOSVisualGuard(ready_driver(), capture=cap)
        g.prepare()
        with self.assertRaisesRegex(VisualGuardError, "drifted"):
            g.arm()

    def test_arm_accepts_a_still_scene_and_changed_is_false_when_nothing_moves(self):
        cap = FakeCapture(frame(seed=5))
        g = MacOSVisualGuard(ready_driver(), capture=cap)
        g.prepare()
        g.arm()
        self.assertFalse(g.changed())
        self.assertEqual(g.last_fraction, 0.0)

    def test_changed_is_true_after_a_small_localized_repaint(self):
        base = make_pixels()
        top_px = int(ROI_TOP_PT * SCALE)
        tile_h = (H_PX - top_px) // TILE_ROWS
        tile_w = W_PX // TILE_COLS
        y0 = top_px + tile_h * 6
        spinner = patch_region(base, x0=tile_w, y0=y0, x1=tile_w * 2, y1=y0 + tile_h)
        cap = FakeCapture(frame(base), frame(base), frame(spinner))
        g = MacOSVisualGuard(ready_driver(), capture=cap)
        g.prepare()
        g.arm()
        self.assertTrue(g.changed(), "a spinner-sized change must stop the retry loop")

    def test_changed_captures_fresh_every_call(self):
        cap = FakeCapture(frame(seed=1))
        g = MacOSVisualGuard(ready_driver(), capture=cap)
        g.prepare()
        g.arm()
        before = cap.calls
        g.changed()
        g.changed()
        self.assertEqual(cap.calls - before, 2, "changed() must re-capture, never reuse a frame")

    def test_stale_frame_after_arm_raises_rather_than_reporting_unchanged(self):
        cap = FakeCapture(frame(), frame(), frame(age_ms=STALE_MS + 100))
        g = MacOSVisualGuard(ready_driver(), capture=cap)
        g.prepare()
        g.arm()
        with self.assertRaisesRegex(VisualGuardError, "old"):
            g.changed()

    def test_geometry_change_after_arm_raises(self):
        cap = FakeCapture(frame(), frame(), frame(width=W_PX // 2, height=H_PX // 2, bpr=BPR // 2))
        g = MacOSVisualGuard(ready_driver(), capture=cap)
        g.prepare()
        g.arm()
        with self.assertRaisesRegex(VisualGuardError, "geometry changed"):
            g.changed()

    def test_close_is_idempotent_and_refuses_further_use(self):
        cap = FakeCapture()
        g = MacOSVisualGuard(ready_driver(), capture=cap)
        g.prepare()
        g.close()
        g.close()
        self.assertEqual(cap.closed, 2)
        with self.assertRaisesRegex(VisualGuardError, "closed"):
            g.changed()

    def test_guard_never_posts_an_event(self):
        n = FakeNative()
        d = ready_driver(n)
        g = MacOSVisualGuard(d, capture=FakeCapture())
        g.prepare()
        g.arm()
        g.changed()
        g.close()
        self.assertEqual(n.events, [], "the visual guard is read-only")


class WatchTests(unittest.TestCase):
    def test_watch_is_read_only_and_reports_fractions(self):
        n = FakeNative()
        d = MacOSClickDriver(900.0, 500.0, native=n)
        out = io.StringIO()
        fractions = watch(d, seconds=0.05, interval=0.01, capture=FakeCapture(), out=out, sleep=lambda _s: None)
        self.assertEqual(n.events, [])
        self.assertIn("READ-ONLY", out.getvalue())
        self.assertTrue(all(f == 0.0 for f in fractions))

    def test_cli_rejects_bad_intervals(self):
        from fans_utils.visual import main

        out = io.StringIO()
        self.assertEqual(main(["watch", "--seconds", "1", "--interval", "0"],
                              driver=ready_driver(), capture=FakeCapture(), out=out), 2)
        self.assertIn("--interval", out.getvalue())


@unittest.skipUnless(sys.platform == "darwin", "real capture bindings only exist on macOS")
class RealCaptureSmokeTest(unittest.TestCase):
    """Loads the real capture bindings read-only. Posts nothing and asserts nothing about content."""

    def test_bindings_load_and_permission_is_reported(self):
        try:
            cap = CoreGraphicsCapture()
        except DriverError as exc:
            self.skipTest(str(exc))
        self.assertIsInstance(cap.screen_recording_allowed(), bool)
        cap.close()

    def test_capture_of_a_live_window_has_padded_row_shape(self):
        from fans_utils.macos import CoreGraphicsNative

        try:
            native = CoreGraphicsNative()
            windows = native.on_screen_windows()
        except DriverError as exc:
            self.skipTest(str(exc))
        cap = CoreGraphicsCapture()
        if not cap.screen_recording_allowed():
            self.skipTest("Screen Recording not granted to this terminal app")
        target = next((w for w in windows if w.width > 200 and w.height > 200), None)
        if target is None:
            self.skipTest("no window large enough to sample")
        f = cap(target)
        if f is None:
            self.skipTest("window vanished between listing and capture")
        self.assertGreaterEqual(f.bytes_per_row, f.width * 4, "rows may be padded, never shorter")
        self.assertGreaterEqual(len(f.pixels), f.bytes_per_row * f.height)
        native.close()
        cap.close()


if __name__ == "__main__":
    unittest.main()
