"""MacOSClickDriver guard logic with an injected fake Native. Nothing here touches the OS
and nothing asserts that a real UI received anything."""

from __future__ import annotations

import io
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fans_utils.macos import (
    DEFAULT_BUNDLE_ID,
    Calibration,
    Display,
    DriverError,
    MacOSClickDriver,
    Window,
    calibrate,
    diagnose,
)

MIRROR = DEFAULT_BUNDLE_ID
BROWSER = "com.apple.Safari"
MAIN = Display(id=1, x=0.0, y=0.0, width=1512.0, height=982.0, scale=2.0)
SECOND = Display(id=3, x=-1920.0, y=0.0, width=1920.0, height=1080.0, scale=1.0)  # negative x is legal


def win(number=7929, bundle=MIRROR, x=782.0, y=139.0, w=348.0, h=766.0, on_screen=True, layer=0, owner="iPhone Mirroring"):
    return Window(number=number, pid=100 + number, bundle_id=bundle, owner=owner,
                  x=x, y=y, width=w, height=h, layer=layer, on_screen=on_screen)


class FakeNative:
    def __init__(self, *, trusted=True, displays=(MAIN,), frontmost=MIRROR, windows=(win(),), cursor=(900.0, 500.0)):
        self.trusted = trusted
        self._displays = list(displays)
        self.frontmost = frontmost
        self.windows = list(windows)
        self.cursor = cursor
        self.events: list[tuple[str, float, float]] = []
        self.warmed = 0
        self.closed = 0
        self.raise_after: tuple[str, BaseException] | None = None  # (kind, exc) raised after recording kind

    def accessibility_trusted(self):
        return self.trusted

    def displays(self):
        return self._displays

    def frontmost_bundle_id(self):
        return self.frontmost

    def on_screen_windows(self):
        return [w for w in self.windows if w.on_screen and w.layer == 0]

    def window_by_number(self, number):
        return next((w for w in self.windows if w.number == number), None)

    def cursor_position(self):
        return self.cursor

    def warm_up(self):
        self.warmed += 1

    def post_mouse(self, kind, x, y):
        self.events.append((kind, x, y))
        if self.raise_after and self.raise_after[0] == kind:
            raise self.raise_after[1]

    def close(self):
        self.closed += 1


def driver(native: FakeNative, x=900.0, y=500.0, **kw) -> MacOSClickDriver:
    return MacOSClickDriver(x, y, native=native, **kw)


class ConstructionTests(unittest.TestCase):
    def test_rejects_nonfinite_or_non_numeric_coordinates_before_touching_native(self):
        for x, y in [(float("nan"), 1.0), (1.0, float("inf")), ("900", 500.0), (True, 1.0), (None, 5.0)]:
            with self.subTest(x=x, y=y):
                n = FakeNative()
                if x is None:  # (None, y) means "load calibration" -> missing file error, still no native use
                    with self.assertRaises(DriverError):
                        MacOSClickDriver(x, y, native=n, calibration_path=Path("/nonexistent/cal.json"))
                else:
                    with self.assertRaises(DriverError):
                        MacOSClickDriver(x, y, native=n)
                self.assertEqual(n.events, [])

    def test_default_bundle_is_iphone_mirroring(self):
        self.assertEqual(driver(FakeNative()).expected_bundle_id, MIRROR)

    def test_explicit_alternate_bundle_is_accepted(self):
        d = driver(FakeNative(frontmost=BROWSER, windows=[win(bundle=BROWSER)]), expected_bundle_id=BROWSER)
        d.preflight()
        d.click()

    def test_loads_calibration_file_when_coordinates_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.json"
            Calibration(x=900.0, y=500.0, bundle_id=MIRROR, window_owner="iPhone Mirroring",
                        window_bounds=(782.0, 139.0, 348.0, 766.0), display_id=1, captured_at="t").save(path)
            d = MacOSClickDriver(native=FakeNative(), calibration_path=path)
            self.assertEqual((d.x, d.y, d.expected_bundle_id), (900.0, 500.0, MIRROR))
            self.assertEqual(d.preflight(), [])

    def test_calibration_bundle_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.json"
            Calibration(x=1.0, y=1.0, bundle_id=BROWSER, window_owner="Safari",
                        window_bounds=(0.0, 0.0, 10.0, 10.0), display_id=1, captured_at="t").save(path)
            with self.assertRaisesRegex(DriverError, "recalibrate"):
                MacOSClickDriver(native=FakeNative(), calibration_path=path, expected_bundle_id=MIRROR)

    def test_malformed_calibration_is_rejected(self):
        good = ('{"x": 900, "y": 500, "bundle_id": "%s", "window_bounds": [782, 139, 348, 766], '
                '"display_id": 1, "version": 1}' % MIRROR)
        bad = [
            '{"x": 1}',
            '[1, 2]',
            good.replace('"version": 1', '"version": 0'),
            good.replace('"version": 1', '"version": 2'),
            good.replace("900", "NaN"),
            good.replace("[782, 139, 348, 766]", "[782, 139]"),
            good.replace("[782, 139, 348, 766]", '["a", 1, 2, 3]'),
            good.replace(MIRROR, ""),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.json"
            path.write_text(good)
            MacOSClickDriver(native=FakeNative(), calibration_path=path)  # sanity: the good one loads
            for text in bad:
                with self.subTest(text=text):
                    path.write_text(text)
                    with self.assertRaisesRegex(DriverError, "malformed"):
                        MacOSClickDriver(native=FakeNative(), calibration_path=path)


class PreflightTests(unittest.TestCase):
    def test_permission_denied_is_clear_and_posts_nothing(self):
        n = FakeNative(trusted=False)
        with self.assertRaisesRegex(PermissionError, "Accessibility"):
            driver(n).preflight()
        self.assertEqual(n.events, [])
        self.assertEqual(n.warmed, 0)

    def test_off_screen_point_is_rejected(self):
        n = FakeNative(displays=[MAIN, SECOND])
        with self.assertRaisesRegex(DriverError, "not on any active display"):
            driver(n, x=5000.0, y=5.0).preflight()
        self.assertEqual(n.events, [])

    def test_point_on_negative_secondary_display_is_on_screen(self):
        n = FakeNative(displays=[MAIN, SECOND], windows=[win(x=-1000.0, y=100.0)])
        driver(n, x=-900.0, y=200.0).preflight()

    def test_target_not_running_is_rejected(self):
        n = FakeNative(windows=[win(bundle=BROWSER)])
        with self.assertRaisesRegex(DriverError, "no on-screen window"):
            driver(n).preflight()

    def test_target_window_minimized_is_rejected(self):
        n = FakeNative(windows=[win(on_screen=False)])
        with self.assertRaisesRegex(DriverError, "no on-screen window"):
            driver(n).preflight()

    def test_point_outside_target_window_is_rejected(self):
        n = FakeNative()
        with self.assertRaisesRegex(DriverError, "outside every"):
            driver(n, x=10.0, y=10.0).preflight()

    def test_window_moved_since_calibration_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.json"
            Calibration(x=900.0, y=500.0, bundle_id=MIRROR, window_owner="iPhone Mirroring",
                        window_bounds=(700.0, 139.0, 348.0, 766.0), display_id=1, captured_at="t").save(path)
            n = FakeNative()  # live window is at x=782, calibration says 700
            with self.assertRaisesRegex(DriverError, "moved or resized"):
                MacOSClickDriver(native=n, calibration_path=path).preflight()
            self.assertEqual(n.events, [])

    def test_wrong_frontmost_is_a_warning_not_a_failure(self):
        n = FakeNative(frontmost="com.apple.Terminal")
        warnings = driver(n).preflight()
        self.assertEqual(len(warnings), 1)
        self.assertIn("com.apple.Terminal", warnings[0])
        self.assertEqual(n.warmed, 1)

    def test_preflight_warms_backend_without_posting(self):
        n = FakeNative()
        driver(n).preflight()
        self.assertEqual(n.warmed, 1)
        self.assertEqual(n.events, [])


class ClickGuardTests(unittest.TestCase):
    def test_click_before_preflight_is_refused(self):
        n = FakeNative()
        with self.assertRaisesRegex(DriverError, "preflight"):
            driver(n).click()
        self.assertEqual(n.events, [])

    def test_happy_path_is_exactly_moved_down_up_once(self):
        n = FakeNative()
        d = driver(n)
        d.preflight()
        d.click()
        self.assertEqual(n.events, [("moved", 900.0, 500.0), ("down", 900.0, 500.0), ("up", 900.0, 500.0)])

    def test_focus_changed_between_preflight_and_click(self):
        n = FakeNative()
        d = driver(n)
        d.preflight()
        n.frontmost = "com.apple.Terminal"
        with self.assertRaisesRegex(DriverError, "frontmost app is 'com.apple.Terminal'"):
            d.click()
        self.assertEqual(n.events, [])

    def test_window_gone_between_preflight_and_click(self):
        n = FakeNative()
        d = driver(n)
        d.preflight()
        n.windows = []  # phone disconnected
        with self.assertRaisesRegex(DriverError, "gone or no longer on screen"):
            d.click()
        self.assertEqual(n.events, [])

    def test_window_hidden_between_preflight_and_click(self):
        n = FakeNative()
        d = driver(n)
        d.preflight()
        n.windows = [win(on_screen=False)]
        with self.assertRaisesRegex(DriverError, "gone or no longer on screen"):
            d.click()
        self.assertEqual(n.events, [])

    def test_accessibility_revoked_between_preflight_and_click(self):
        n = FakeNative()
        d = driver(n)
        d.preflight()
        n.trusted = False
        with self.assertRaisesRegex(PermissionError, "Accessibility"):
            d.click()
        self.assertEqual(n.events, [])

    def test_point_occluded_by_another_app_window_is_refused(self):
        n = FakeNative()
        d = driver(n)
        d.preflight()
        n.windows = [win(number=42, bundle="com.apple.Terminal", owner="Terminal", x=850.0, y=450.0, w=200.0, h=200.0),
                     win()]  # front-to-back: Terminal panel now covers the click point
        with self.assertRaisesRegex(DriverError, "covered by 'Terminal'"):
            d.click()
        self.assertEqual(n.events, [])

    def test_point_occluded_by_same_app_other_window_is_refused(self):
        n = FakeNative()
        d = driver(n)
        d.preflight()
        n.windows = [win(number=43), win()]  # a second Mirroring window at the same bounds, in front
        with self.assertRaisesRegex(DriverError, "window 43"):
            d.click()
        self.assertEqual(n.events, [])

    def test_other_window_elsewhere_does_not_block(self):
        n = FakeNative()
        d = driver(n)
        d.preflight()
        n.windows = [win(number=42, bundle="com.apple.Terminal", owner="Terminal", x=0.0, y=0.0, w=100.0, h=100.0),
                     win()]
        d.click()
        self.assertEqual(len(n.events), 3)

    def test_window_moved_between_preflight_and_click(self):
        n = FakeNative()
        d = driver(n)
        d.preflight()
        n.windows = [win(x=790.0)]
        with self.assertRaisesRegex(DriverError, "moved/resized"):
            d.click()
        self.assertEqual(n.events, [])

    def test_window_resized_so_point_falls_outside(self):
        n = FakeNative()
        d = driver(n, x=1100.0, y=500.0)
        d.preflight()
        n.windows = [win(w=300.0)]  # window shrank; the point is now over nothing
        with self.assertRaisesRegex(DriverError, "covered by nothing"):
            d.click()
        self.assertEqual(n.events, [])

    def test_window_resized_but_point_still_inside_is_refused_as_resize(self):
        n = FakeNative()
        d = driver(n)
        d.preflight()
        n.windows = [win(h=700.0)]
        with self.assertRaisesRegex(DriverError, "moved/resized"):
            d.click()
        self.assertEqual(n.events, [])

    def test_sub_point_jitter_is_tolerated(self):
        n = FakeNative()
        d = driver(n)
        d.preflight()
        n.windows = [win(x=782.4)]
        d.click()
        self.assertEqual(len(n.events), 3)

    def test_interrupt_after_mouse_down_still_releases_and_never_double_clicks(self):
        n = FakeNative()
        n.raise_after = ("down", KeyboardInterrupt())
        d = driver(n)
        d.preflight()
        with self.assertRaises(KeyboardInterrupt):
            d.click()
        self.assertEqual([e[0] for e in n.events], ["moved", "down", "up"])

    def test_frontmost_is_read_live_on_every_click_not_cached_from_preflight(self):
        # Regression for the 2026-09-22 field failure: preflight saw the terminal, the user
        # then focused iPhone Mirroring, and the click must see the NEW frontmost app.
        n = FakeNative(frontmost="com.apple.Terminal")
        reads: list[str | None] = []
        real = n.frontmost_bundle_id
        n.frontmost_bundle_id = lambda: reads.append(real()) or reads[-1]
        d = driver(n)
        warnings = d.preflight()
        self.assertEqual(len(warnings), 1)
        n.frontmost = MIRROR  # user switched apps during the countdown
        d.click()
        self.assertEqual([e[0] for e in n.events], ["moved", "down", "up"])
        self.assertEqual(reads, ["com.apple.Terminal", MIRROR], "click must re-read, not reuse preflight's value")
        n.frontmost = "com.apple.Terminal"
        with self.assertRaises(DriverError):
            d.click()
        self.assertEqual(len(reads), 3)

    def test_frontmost_read_failure_refuses_click_without_posting(self):
        n = FakeNative()
        d = driver(n)
        d.preflight()
        n.frontmost_bundle_id = lambda: (_ for _ in ()).throw(DriverError("AXError -25204"))
        with self.assertRaisesRegex(DriverError, "AXError"):
            d.click()
        self.assertEqual(n.events, [])

    def test_second_click_is_guarded_again(self):
        n = FakeNative()
        d = driver(n)
        d.preflight()
        d.click()
        n.frontmost = "com.apple.finder"
        with self.assertRaises(DriverError):
            d.click()
        self.assertEqual(len(n.events), 3)

    def test_close_releases_and_refuses_further_clicks(self):
        n = FakeNative()
        d = driver(n)
        d.preflight()
        d.close()
        d.close()
        self.assertEqual(n.closed, 1)
        with self.assertRaisesRegex(DriverError, "closed"):
            d.click()


class ClickMetricsTests(unittest.TestCase):
    """last_click_metrics must be truthful: timestamps come from the moment each post returned,
    and a refused click leaves no numbers behind for anyone to misread."""

    def test_metrics_are_none_before_any_click(self):
        self.assertIsNone(driver(FakeNative()).last_click_metrics)

    def test_metrics_after_a_posted_click_are_ordered_and_finite(self):
        n = FakeNative()
        d = driver(n)
        d.preflight()
        before = time.monotonic()
        d.click()
        after = time.monotonic()
        m = d.last_click_metrics
        self.assertIsNotNone(m)
        self.assertLessEqual(before, m["down_posted_monotonic"])
        self.assertLessEqual(m["down_posted_monotonic"], m["up_posted_monotonic"])
        self.assertLessEqual(m["up_posted_monotonic"], after)
        self.assertGreaterEqual(m["guard_ms"], 0.0)

    def test_metrics_use_the_backends_post_timestamp_when_it_supplies_one(self):
        n = FakeNative()
        stamps = iter([1000.5, 1000.75])

        def timed_post(kind, x, y):
            n.events.append((kind, x, y))
            return next(stamps) if kind in ("down", "up") else None

        n.post_mouse = timed_post
        d = driver(n)
        d.preflight()
        d.click()
        self.assertEqual(d.last_click_metrics["down_posted_monotonic"], 1000.5)
        self.assertEqual(d.last_click_metrics["up_posted_monotonic"], 1000.75)

    def test_refused_click_leaves_no_stale_metrics(self):
        n = FakeNative()
        d = driver(n)
        d.preflight()
        d.click()
        self.assertIsNotNone(d.last_click_metrics)
        n.frontmost = "com.apple.Terminal"
        with self.assertRaises(DriverError):
            d.click()
        self.assertIsNone(d.last_click_metrics, "a refusal must not leave the previous click's numbers")


class ValidateTargetTests(unittest.TestCase):
    def test_validate_target_posts_nothing_and_returns_the_live_window(self):
        n = FakeNative()
        d = driver(n)
        d.preflight()
        live = d.validate_target()
        self.assertEqual(live.number, 7929)
        self.assertEqual(n.events, [], "the read-only check must never post")

    def test_validate_target_applies_every_click_guard(self):
        cases = {
            "frontmost": lambda n: setattr(n, "frontmost", "com.apple.Terminal"),
            "gone": lambda n: setattr(n, "windows", []),
            "moved": lambda n: setattr(n, "windows", [win(x=790.0)]),
        }
        for label, mutate in cases.items():
            with self.subTest(guard=label):
                n = FakeNative()
                d = driver(n)
                d.preflight()
                mutate(n)
                with self.assertRaises(DriverError):
                    d.validate_target()
                self.assertEqual(n.events, [])

    def test_validate_target_before_preflight_refuses(self):
        with self.assertRaisesRegex(DriverError, "preflight"):
            driver(FakeNative()).validate_target()

    def test_target_window_is_exposed_only_after_preflight(self):
        d = driver(FakeNative())
        self.assertIsNone(d.target_window)
        d.preflight()
        self.assertEqual(d.target_window.number, 7929)


class BenchmarkTests(unittest.TestCase):
    def test_benchmark_posts_nothing_and_reports_percentiles(self):
        from fans_utils.macos import benchmark

        n = FakeNative()
        out = io.StringIO()
        report = benchmark(driver(n), samples=5, out=out)
        self.assertEqual(n.events, [], "benchmark must never post an event")
        self.assertEqual(report["events_posted"], 0)
        self.assertEqual(report["samples"], 5)
        self.assertEqual(report["outcomes"], {"ok": 5})
        self.assertLessEqual(report["guard_ms"]["p50"], report["guard_ms"]["max"])
        self.assertIn("READ-ONLY", out.getvalue())

    def test_benchmark_counts_refusals_instead_of_crashing(self):
        from fans_utils.macos import benchmark

        n = FakeNative(frontmost="com.apple.Terminal")
        report = benchmark(driver(n), samples=4, out=io.StringIO())
        self.assertEqual(sum(report["outcomes"].values()), 4)
        self.assertNotIn("ok", report["outcomes"])
        self.assertEqual(n.events, [])

    def test_benchmark_rejects_out_of_range_samples(self):
        from fans_utils.macos import BENCHMARK_MAX_SAMPLES, benchmark

        for samples in (0, -1, BENCHMARK_MAX_SAMPLES + 1):
            with self.subTest(samples=samples), self.assertRaisesRegex(DriverError, "samples"):
                benchmark(driver(FakeNative()), samples=samples, out=io.StringIO())

    def test_cli_benchmark_is_read_only(self):
        from fans_utils.macos import main

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.json"
            Calibration(x=900.0, y=500.0, bundle_id=MIRROR, window_owner="iPhone Mirroring",
                        window_bounds=(782.0, 139.0, 348.0, 766.0), display_id=1, captured_at="t").save(path)
            n = FakeNative()
            out = io.StringIO()
            self.assertEqual(main(["benchmark", "--path", str(path), "--samples", "3"], native=n, out=out), 0)
            self.assertEqual(n.events, [])
            self.assertIn("events posted: 0", out.getvalue())


class PreflightFocusReadFailureTests(unittest.TestCase):
    """Observed on macOS 26.4: the AX focused-app read returns NoValue (-25212) while some apps are
    frontmost. That must warn at preflight (recoverable during the countdown) but still refuse a click."""

    def test_unreadable_focus_is_a_preflight_warning(self):
        n = FakeNative()
        n.frontmost_bundle_id = lambda: (_ for _ in ()).throw(DriverError("AXError -25212"))
        warnings = driver(n).preflight()
        self.assertTrue(any("-25212" in w for w in warnings))
        self.assertEqual(n.events, [])

    def test_unreadable_focus_still_refuses_the_click(self):
        n = FakeNative()
        d = driver(n)
        d.preflight()
        n.frontmost_bundle_id = lambda: (_ for _ in ()).throw(DriverError("AXError -25212"))
        with self.assertRaisesRegex(DriverError, "-25212"):
            d.click()
        self.assertEqual(n.events, [], "no fallback focus source; unreadable focus means no click")


class CalibrateTests(unittest.TestCase):
    def test_calibrate_reads_cursor_writes_file_and_never_posts(self):
        n = FakeNative(cursor=(900.0, 500.0))
        sleeps: list[float] = []
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.json"
            cal = calibrate(n, seconds=2.5, path=path, sleep=sleeps.append, out=out)
            self.assertEqual((cal.x, cal.y, cal.bundle_id, cal.display_id), (900.0, 500.0, MIRROR, 1))
            self.assertEqual(cal.window_bounds, (782.0, 139.0, 348.0, 766.0))
            self.assertEqual(Calibration.load(path), cal)
            d = MacOSClickDriver(native=n, calibration_path=path)
            self.assertEqual((d.x, d.y), (cal.x, cal.y))
            self.assertEqual(d.preflight(), [])
        self.assertEqual(sleeps, [1.0, 1.0, 0.5])
        self.assertEqual(n.events, [])
        self.assertIn("topmost window there", out.getvalue())

    def test_calibrate_refuses_wrong_window_and_names_it(self):
        n = FakeNative(windows=[win(bundle=BROWSER, owner="Safari")])
        with self.assertRaisesRegex(DriverError, "--bundle com.apple.Safari"):
            calibrate(n, seconds=0, out=io.StringIO())

    def test_calibrate_alternate_bundle_warns(self):
        n = FakeNative(windows=[win(bundle=BROWSER, owner="Safari")])
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            calibrate(n, seconds=0, bundle_id=BROWSER, path=Path(tmp) / "c.json", out=out)
        self.assertIn("Mock validation only", out.getvalue())

    def test_calibrate_requires_permission(self):
        with self.assertRaises(PermissionError):
            calibrate(FakeNative(trusted=False), seconds=0, out=io.StringIO())

    def test_calibrate_explicit_point_skips_countdown_and_cursor(self):
        n = FakeNative(cursor=(1.0, 1.0))  # cursor is somewhere else entirely
        sleeps: list[float] = []
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.json"
            cal = calibrate(n, seconds=10, point=(956.0, 841.0), path=path, sleep=sleeps.append, out=out)
            self.assertEqual((cal.x, cal.y, cal.window_number), (956.0, 841.0, 7929))
            self.assertEqual(Calibration.load(path), cal)
        self.assertEqual(sleeps, [])
        self.assertEqual(n.events, [])
        self.assertIn("explicit point", out.getvalue())

    def test_calibrate_explicit_point_rejects_bad_values_and_wrong_window(self):
        for point in [(float("nan"), 1.0), (1.0, float("inf")), (True, 2.0)]:
            with self.subTest(point=point), self.assertRaisesRegex(DriverError, "finite"):
                calibrate(FakeNative(), point=point, out=io.StringIO())
        with self.assertRaisesRegex(DriverError, "no window at point"):
            calibrate(FakeNative(), point=(10.0, 10.0), out=io.StringIO())
        with self.assertRaisesRegex(DriverError, "not on an active display"):
            calibrate(FakeNative(), point=(9999.0, 10.0), out=io.StringIO())

    def test_cli_rejects_partial_explicit_pair(self):
        from fans_utils.macos import main

        for argv in (["calibrate", "--x", "956"], ["calibrate", "--y", "841"]):
            out = io.StringIO()
            n = FakeNative()
            self.assertEqual(main(argv, native=n, out=out), 2)
            self.assertIn("--x and --y must be given together", out.getvalue())
            self.assertEqual(n.events, [])

    def test_cli_explicit_pair_writes_calibration(self):
        from fans_utils.macos import main

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            out = io.StringIO()
            code = main(["calibrate", "--x", "956", "--y", "841", "--path", str(path)], native=FakeNative(), out=out)
            self.assertEqual(code, 0)
            self.assertEqual(Calibration.load(path).x, 956.0)

    def test_calibrate_rejects_absurd_or_nonfinite_countdown(self):
        for seconds in (-1, float("nan"), float("inf"), 10 ** 9):
            with self.subTest(seconds=seconds), self.assertRaisesRegex(DriverError, "countdown"):
                calibrate(FakeNative(), seconds=seconds, out=io.StringIO())

    def test_cli_interrupt_during_calibrate_exits_130_without_writing(self):
        from fans_utils.macos import main

        def interrupt(_s):
            raise KeyboardInterrupt

        n = FakeNative()
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            import fans_utils.macos as m
            original = m.time.sleep
            m.time.sleep = interrupt  # calibrate() defaults to time.sleep
            try:
                code = main(["calibrate", "--in", "5", "--path", str(path)], native=n, out=out)
            finally:
                m.time.sleep = original
            self.assertEqual(code, 130)
            self.assertFalse(path.exists())
        self.assertIn("cancelled", out.getvalue())
        self.assertEqual(n.events, [])

    def test_cli_rejects_huge_countdown(self):
        from fans_utils.macos import main

        out = io.StringIO()
        self.assertEqual(main(["fire", "--in", "1e9"], native=FakeNative(), out=out), 2)
        self.assertIn("--in must be", out.getvalue())

    def test_diagnose_is_read_only(self):
        n = FakeNative()
        report = diagnose(n)
        self.assertTrue(report["accessibility_trusted"])
        self.assertEqual(report["cursor"]["window_under_cursor"]["bundle_id"], MIRROR)
        self.assertEqual(n.events, [])


@unittest.skipUnless(sys.platform == "darwin", "real bindings only exist on macOS")
class RealBindingsSmokeTest(unittest.TestCase):
    """Loads the real ctypes bindings and calls only read-only functions. Posts nothing."""

    def test_diagnose_runs_and_reports_shapes(self):
        from fans_utils.macos import CoreGraphicsNative

        try:
            n = CoreGraphicsNative()
            report = diagnose(n)
        except DriverError as exc:  # e.g. no window server session on CI
            self.skipTest(str(exc))
        self.assertIsInstance(report["accessibility_trusted"], bool)
        self.assertGreaterEqual(len(report["displays"]), 1)
        self.assertIn("x", report["cursor"])
        if report["accessibility_trusted"]:
            # Live AX read must yield a bundle id (or a clear error string), never silently None.
            self.assertTrue(report["frontmost_bundle_id"], report)
            try:
                self.assertIsInstance(n.frontmost_bundle_id(), str)
            except DriverError as exc:
                # Environmental, not a logic defect: observed 2026-09-22 on macOS 26.4 that the
                # system-wide AXFocusedApplication read returns NoValue (-25212) while some apps
                # (an Electron host) are frontmost. The guard logic itself is covered by the fakes
                # in PreflightFocusReadFailureTests; do not fail the suite on the environment.
                n.close()
                self.skipTest(f"AX focused-app read unavailable in this environment: {exc}")
        n.close()
        n.close()  # idempotent: system-wide AX element and event source released once


if __name__ == "__main__":
    unittest.main()
