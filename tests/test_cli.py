import contextlib
import io
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from fans_utils import cli, driver
from tests.fakes import T0, FakeClock, FakeObserver

REPO = Path(__file__).resolve().parent.parent
PROFILES = str(REPO / "profiles")
NMIXX_PATH = str(REPO / "profiles" / "nmixx-uoo7zw9oie.json")
STALE_T = T0 + timedelta(hours=2)  # 11:00Z, one hour after signup start


def invoke(argv, fake: FakeClock | None = None, observer=None):
    fake = fake or FakeClock()
    out = io.StringIO()
    argv = ["--profiles-dir", PROFILES, *argv]
    with contextlib.redirect_stderr(io.StringIO()):  # argparse usage noise
        if observer is None:
            code = cli.main(argv, clock=fake.clock(), out=out)
        else:
            # dry-run guarded retry: inject the observer the parser does not expose
            real_main = cli.cmd_schedule

            def with_observer(args, path, clock, o):
                args.observer = observer
                return real_main(args, path, clock, o)

            with mock.patch.object(cli, "cmd_schedule", with_observer):
                code = cli.main(argv, clock=fake.clock(), out=out)
    return code, out.getvalue(), fake


class ListShowTests(unittest.TestCase):
    def test_list(self):
        code, out, _ = invoke(["list"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("nmixx-uoo7zw9oie", out)
        self.assertIn("upcoming", out)
        self.assertIn("2026-09-22T18:00:00.000+08:00 Taipei", out)

    def test_list_marks_stale(self):
        _, out, _ = invoke(["list"], FakeClock(STALE_T))
        self.assertIn("stale", out)

    def test_show_by_id_and_by_path(self):
        for ref in ("nmixx-uoo7zw9oie", NMIXX_PATH):
            with self.subTest(ref=ref):
                code, out, _ = invoke(["show", ref])
                self.assertEqual(code, cli.EXIT_OK)
                self.assertIn("https://app.fans/community/nmixx/event/uoo7zw9oie", out)
                self.assertIn("Taipei 2026-09-22T18:00:00.000+08:00", out)
                self.assertIn("UTC 2026-09-22T10:00:00.000+00:00", out)
                self.assertIn("100 (+10 alternates)", out)
                self.assertIn("starts in 3600.0s", out)

    def test_show_stale(self):
        _, out, _ = invoke(["show", "nmixx-uoo7zw9oie"], FakeClock(STALE_T))
        self.assertIn("STALE", out)

    def test_default_profiles_dir_does_not_depend_on_cwd(self):
        with tempfile.TemporaryDirectory() as d, contextlib.chdir(d):
            out = io.StringIO()
            code = cli.main(["list"], clock=FakeClock().clock(), out=out)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("nmixx-uoo7zw9oie", out.getvalue())
        self.assertIn(cli.DEFAULT_PROFILES_DIR, (REPO / "profiles", REPO / "fans_utils" / "profiles"))

    def test_malformed_profile_exits_1(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.json"
            bad.write_text('{"id": "x"}', encoding="utf-8")
            code, out, _ = invoke(["show", str(bad)])
        self.assertEqual(code, cli.EXIT_PROFILE_OR_PLAN_ERROR)
        self.assertIn("error:", out)
        self.assertIn("missing", out)


class ScheduleTests(unittest.TestCase):
    def test_dry_run_waits_and_clicks_once(self):
        code, out, fake = invoke(["schedule", "nmixx-uoo7zw9oie"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("DRY-RUN", out)
        self.assertNotIn("REHEARSAL", out)
        self.assertIn("preflight:        ok", out)
        self.assertIn("waiting:          3600.000s", out)
        self.assertIn("status:           completed", out)
        self.assertIn("clicks attempted: 1  (dry-run via mock)", out)
        self.assertIn("lateness ms:      [0.0]", out)
        self.assertIn("scheduler dispatch", out)
        self.assertIn(cli.ACCEPTANCE_NOTE, out)
        self.assertNotIn(cli.LIVE_NOTE, out)
        self.assertGreater(len(fake.sleeps), 0)
        self.assertEqual(fake.wall, T0 + timedelta(hours=1))

    def test_countdown_is_periodic_and_throttled(self):
        _, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie"])
        lines = [l for l in out.splitlines() if l.startswith("waiting:")]
        # initial line + one per 60s while > 300s remain + one per 10s in the last 300s, roughly
        self.assertGreater(len(lines), 50)
        self.assertLess(len(lines), 3600 / 60 + 300 / 10 + 5)
        self.assertIn("waiting:          3600.000s", lines[0])
        remaining = [float(l.split()[1].rstrip("s")) for l in lines]
        self.assertEqual(remaining, sorted(remaining, reverse=True))
        self.assertLess(remaining[-1], 15.0, "countdown must continue into the last seconds")

    def test_adjust_ms_is_shown_as_effective_target(self):
        code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--adjust-ms", "-100"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("adjust:           -100 ms", out)
        self.assertIn("nominal start:    2026-09-22T19:00:00.000+09:00", out)
        self.assertIn("effective target: 2026-09-22T18:59:59.900+09:00", out)

    def test_bounded_multi_click(self):
        code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--max-clicks", "3", "--interval-ms", "100"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("clicks attempted: 3", out)
        self.assertIn("lateness ms:      [0.0, 100.0, 200.0]", out)

    def test_stale_profile_exits_3_with_hint_and_no_click(self):
        code, out, fake = invoke(["schedule", "nmixx-uoo7zw9oie"], FakeClock(STALE_T))
        self.assertEqual(code, cli.EXIT_STALE)
        self.assertIn("status:           stale", out)
        self.assertIn("nothing clicked", out)
        self.assertIn("--rehearse-in", out)
        self.assertEqual(fake.sleeps, [])

    def test_rehearsal_overrides_stale_start_and_is_marked(self):
        code, out, fake = invoke(["schedule", "nmixx-uoo7zw9oie", "--rehearse-in", "2"], FakeClock(STALE_T))
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("REHEARSAL", out)
        self.assertIn("DRY-RUN", out)
        self.assertIn("waiting:          2.000s", out)
        self.assertIn("clicks attempted: 1", out)
        self.assertEqual(fake.wall, STALE_T + timedelta(seconds=2))

    def test_live_stale_has_no_rehearsal_bypass(self):
        code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--live", "--rehearse-in", "1"], FakeClock(STALE_T))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("dry-run only", out)

    def test_calibration_requires_live(self):
        code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--calibration", "x.json"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("--calibration only applies to --live", out)

    def test_ctrl_c_exits_130_and_reports_cancelled(self):
        fake = FakeClock()

        def hook(_s):
            if len(fake.sleeps) == 2:
                raise KeyboardInterrupt

        fake.on_sleep = hook
        code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie"], fake)
        self.assertEqual(code, cli.EXIT_CANCELLED)
        self.assertIn("status:           cancelled", out)
        self.assertIn("clicks attempted: 0", out)


class StubRealDriver:
    """Stands in for MacOSClickDriver: records lifecycle calls, never touches the OS."""

    name = "macos"
    performs_real_click = True
    instances: list["StubRealDriver"] = []
    init_error: Exception | None = None
    preflight_error: Exception | None = None
    preflight_warnings: list[str] = []
    click_error: Exception | None = None

    def __init__(self, **kwargs):
        if self.init_error:
            raise self.init_error
        self.kwargs = kwargs
        self.preflights = 0
        self.clicks = 0
        self.closes = 0
        StubRealDriver.instances.append(self)

    def preflight(self):
        self.preflights += 1
        if self.preflight_error:
            raise self.preflight_error
        return list(self.preflight_warnings)

    def click(self):
        if self.click_error:
            raise self.click_error
        self.clicks += 1

    def close(self):
        self.closes += 1


class LiveDriverLifecycleTests(unittest.TestCase):
    def setUp(self):
        StubRealDriver.instances = []
        StubRealDriver.init_error = None
        StubRealDriver.preflight_error = None
        StubRealDriver.preflight_warnings = []
        StubRealDriver.click_error = None
        patcher = mock.patch.dict(driver.REAL_DRIVERS, {"macos": StubRealDriver})
        patcher.start()
        self.addCleanup(patcher.stop)
        # never fork a real caffeinate from the test suite
        awake = mock.patch.object(cli, "_keep_awake", lambda out: contextlib.nullcontext())
        awake.start()
        self.addCleanup(awake.stop)

    def only_instance(self) -> StubRealDriver:
        self.assertEqual(len(StubRealDriver.instances), 1)
        return StubRealDriver.instances[0]

    def test_live_success_preflight_then_click_then_close(self):
        StubRealDriver.preflight_warnings = ["frontmost app is 'x', not 'y'"]
        code, out, fake = invoke(["schedule", "nmixx-uoo7zw9oie", "--live", "--driver", "macos"])
        self.assertEqual(code, cli.EXIT_OK)
        d = self.only_instance()
        self.assertEqual((d.preflights, d.clicks, d.closes), (1, 1, 1))
        # target app pinned to iPhone Mirroring; default calibration path is the driver's business
        self.assertEqual(d.kwargs, {"expected_bundle_id": "com.apple.ScreenContinuity"})
        self.assertIn("mode:             LIVE (real click)", out)
        self.assertIn("WARNING:          frontmost app is 'x'", out)
        self.assertIn("preflight:        ok", out)
        self.assertIn("live:             keep the target window frontmost", out)
        self.assertIn("clicks attempted: 1  (REAL via macos)", out)
        self.assertIn(cli.LIVE_NOTE, out)
        self.assertEqual(fake.wall, T0 + timedelta(hours=1))

    def test_explicit_calibration_is_forwarded(self):
        code, _, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--live", "--calibration", "/tmp/c.json"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(self.only_instance().kwargs,
                         {"expected_bundle_id": "com.apple.ScreenContinuity", "calibration_path": Path("/tmp/c.json")})

    def test_live_kwargs_pin_mirroring_so_browser_calibration_is_refused_by_the_real_driver(self):
        # Contract check against the real class signature, without constructing it (no native init):
        # MacOSClickDriver refuses a calibration whose bundle differs from expected_bundle_id.
        import inspect

        from fans_utils import macos
        params = inspect.signature(macos.MacOSClickDriver).parameters
        for key in driver.REAL_DRIVER_LIVE_KWARGS["macos"]:
            self.assertIn(key, params)
        self.assertEqual(driver.REAL_DRIVER_LIVE_KWARGS["macos"]["expected_bundle_id"], macos.DEFAULT_BUNDLE_ID)

    def test_driver_setup_failure_exits_5_before_waiting(self):
        StubRealDriver.init_error = RuntimeError("no calibration at calibration.local.json")
        code, out, fake = invoke(["schedule", "nmixx-uoo7zw9oie", "--live"])
        self.assertEqual(code, cli.EXIT_DRIVER_SETUP)
        self.assertIn("setup failed: no calibration", out)
        self.assertEqual(fake.sleeps, [])
        self.assertEqual(StubRealDriver.instances, [])

    def test_preflight_permission_error_exits_5_closes_and_never_clicks(self):
        StubRealDriver.preflight_error = PermissionError("Accessibility permission is missing")
        code, out, fake = invoke(["schedule", "nmixx-uoo7zw9oie", "--live"])
        self.assertEqual(code, cli.EXIT_DRIVER_SETUP)
        self.assertIn("preflight failed (permission): Accessibility", out)
        d = self.only_instance()
        self.assertEqual((d.clicks, d.closes), (0, 1))
        self.assertEqual(fake.sleeps, [])

    def test_preflight_driver_error_exits_5_and_closes(self):
        StubRealDriver.preflight_error = RuntimeError("window moved or resized. Recalibrate.")
        code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--live"])
        self.assertEqual(code, cli.EXIT_DRIVER_SETUP)
        self.assertIn("preflight failed: window moved", out)
        self.assertEqual(self.only_instance().closes, 1)

    def test_click_runtime_error_exits_1_and_closes(self):
        StubRealDriver.click_error = RuntimeError("refusing to click: frontmost app is 'other'")
        code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--live"])
        self.assertEqual(code, cli.EXIT_PROFILE_OR_PLAN_ERROR)
        self.assertIn("status:           error  (RuntimeError: refusing to click", out)
        self.assertIn("clicks attempted: 0  (REAL via macos)", out)
        self.assertEqual(self.only_instance().closes, 1)

    def test_ctrl_c_during_live_wait_exits_130_and_closes(self):
        fake = FakeClock()

        def hook(_s):
            if len(fake.sleeps) == 3:
                raise KeyboardInterrupt

        fake.on_sleep = hook
        code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--live"], fake)
        self.assertEqual(code, cli.EXIT_CANCELLED)
        self.assertIn("status:           cancelled", out)
        d = self.only_instance()
        self.assertEqual((d.clicks, d.closes), (0, 1))

    def test_dry_run_never_constructs_the_real_driver(self):
        StubRealDriver.init_error = AssertionError("real driver constructed during dry-run")
        code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("DRY-RUN", out)
        self.assertEqual(StubRealDriver.instances, [])

    def test_live_stale_is_rejected_before_constructing_the_driver(self):
        StubRealDriver.init_error = AssertionError("driver constructed for a stale target")
        code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--live"], FakeClock(STALE_T))
        self.assertEqual(code, cli.EXIT_STALE)
        self.assertIn("nothing clicked", out)

    def test_bad_plan_is_rejected_before_constructing_the_driver(self):
        StubRealDriver.init_error = AssertionError("driver constructed for an invalid plan")
        code, _, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--live", "--interval-ms", "-1"])
        self.assertEqual(code, cli.EXIT_PROFILE_OR_PLAN_ERROR)


class GuardedRetryCLITests(unittest.TestCase):
    def test_default_schedule_is_unchanged_single_click_with_no_guard(self):
        code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("max 1 click(s), interval 250 ms, duration cap 5000 ms", out)
        self.assertNotIn("guarded retry", out)
        self.assertNotIn("screen guard", out)
        self.assertNotIn("baseline age", out)

    def test_guarded_retry_dry_run_uses_guarded_defaults_and_reports_the_guard(self):
        fake = FakeClock()
        obs = FakeObserver(fake, [], capture_s=0.01)
        code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--guarded-retry"], fake, observer=obs)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("max 3 click(s), interval 150 ms, duration cap 800 ms", out)
        self.assertIn("[guarded retry]", out)
        self.assertIn("screen guard:     FakeObserver ready", out)
        self.assertIn("clicks attempted: 3", out)
        self.assertIn("screen checks:    3", out)
        self.assertIn("baseline age ms:", out)
        self.assertIn(cli.GUARDED_NOTE, out)
        self.assertEqual((obs.prepares, obs.closes), (1, 1))

    def test_screen_change_exits_6_and_never_claims_success(self):
        fake = FakeClock()
        obs = FakeObserver(fake, [True], capture_s=0.01)
        code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--guarded-retry"], fake, observer=obs)
        self.assertEqual(code, cli.EXIT_SCREEN_CHANGED)
        self.assertIn("status:           screen_changed", out)
        self.assertIn("confirm in app", out)
        self.assertIn("clicks attempted: 1", out)
        self.assertIn(cli.SCREEN_CHANGED_NOTE, out)
        self.assertNotIn("accepted", out.replace(cli.ACCEPTANCE_NOTE, "").replace(cli.SCREEN_CHANGED_NOTE, ""))
        self.assertEqual(obs.closes, 1)

    def test_capture_failure_exits_1_and_closes_the_observer(self):
        fake = FakeClock()
        obs = FakeObserver(fake, [], capture_s=0.01, fail_on_check=1)
        code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--guarded-retry"], fake, observer=obs)
        self.assertEqual(code, cli.EXIT_PROFILE_OR_PLAN_ERROR)
        self.assertIn("screen check failed: OSError", out)
        self.assertIn("clicks attempted: 1", out)
        self.assertEqual(obs.closes, 1)

    def test_guarded_bounds_are_rejected_with_exit_1(self):
        for extra in (["--max-clicks", "6"], ["--interval-ms", "50"], ["--max-duration-ms", "2500"]):
            with self.subTest(extra=extra):
                code, out, fake = invoke(["schedule", "nmixx-uoo7zw9oie", "--guarded-retry", *extra])
                self.assertEqual(code, cli.EXIT_PROFILE_OR_PLAN_ERROR)
                self.assertIn("guarded retry", out)
                self.assertEqual(fake.sleeps, [])

    def test_same_bounds_are_fine_without_guarded_retry(self):
        code, _, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--max-clicks", "6", "--interval-ms", "50",
                             "--max-duration-ms", "2500"])
        self.assertEqual(code, cli.EXIT_OK)

    def test_explicit_bounds_override_the_guarded_defaults(self):
        fake = FakeClock()
        obs = FakeObserver(fake, [], capture_s=0.0)
        code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--guarded-retry", "--max-clicks", "2",
                               "--interval-ms", "200", "--max-duration-ms", "1000"], fake, observer=obs)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("max 2 click(s), interval 200 ms, duration cap 1000 ms", out)


class GuardedRetryLiveTests(unittest.TestCase):
    """Live guarded retry must construct the real observer lazily, and never in dry-run."""

    class StubObserver:
        instances: list["GuardedRetryLiveTests.StubObserver"] = []
        prepare_error: Exception | None = None

        def __init__(self, driver):
            self.driver = driver
            self.prepares = self.arms = self.checks = self.closes = 0
            GuardedRetryLiveTests.StubObserver.instances.append(self)

        def prepare(self):
            self.prepares += 1
            if GuardedRetryLiveTests.StubObserver.prepare_error:
                raise GuardedRetryLiveTests.StubObserver.prepare_error

        def arm(self):
            self.arms += 1

        def changed(self):
            self.checks += 1
            return False

        def close(self):
            self.closes += 1

    def setUp(self):
        StubRealDriver.instances = []
        StubRealDriver.init_error = None
        StubRealDriver.preflight_error = None
        StubRealDriver.preflight_warnings = []
        StubRealDriver.click_error = None
        self.StubObserver.instances = []
        self.StubObserver.prepare_error = None
        for patcher in (mock.patch.dict(driver.REAL_DRIVERS, {"macos": StubRealDriver}),
                        mock.patch.dict(driver.REAL_OBSERVERS, {"macos": self.StubObserver}),
                        mock.patch.object(cli, "_keep_awake", lambda out: contextlib.nullcontext())):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_live_guarded_retry_builds_the_observer_from_the_driver(self):
        code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--live", "--guarded-retry"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(len(self.StubObserver.instances), 1)
        obs = self.StubObserver.instances[0]
        self.assertIs(obs.driver, StubRealDriver.instances[0], "observer gets the constructed driver")
        self.assertEqual((obs.prepares, obs.arms, obs.closes), (1, 1, 1))
        self.assertEqual(obs.checks, 3)
        self.assertIn("clicks attempted: 3  (REAL via macos)", out)

    def test_dry_run_guarded_retry_never_constructs_the_real_observer(self):
        self.StubObserver.prepare_error = AssertionError("real observer used during dry-run")
        code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--guarded-retry"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(self.StubObserver.instances, [])
        self.assertIn("MockScreenObserver ready", out)

    def test_observer_setup_failure_exits_5_without_clicking(self):
        self.StubObserver.prepare_error = PermissionError("Screen Recording permission is missing")
        code, out, fake = invoke(["schedule", "nmixx-uoo7zw9oie", "--live", "--guarded-retry"])
        self.assertEqual(code, cli.EXIT_DRIVER_SETUP)
        self.assertIn("screen capture permission missing: Screen Recording", out)
        self.assertEqual(StubRealDriver.instances[0].clicks, 0)
        self.assertEqual(StubRealDriver.instances[0].closes, 1)
        self.assertEqual(fake.sleeps, [])

    def test_live_without_guarded_retry_builds_no_observer(self):
        code, _, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--live"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(self.StubObserver.instances, [])

    def test_failed_observer_setup_still_closes_the_half_built_observer(self):
        self.StubObserver.prepare_error = RuntimeError("capture region is off screen")
        code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--live", "--guarded-retry"])
        self.assertEqual(code, cli.EXIT_DRIVER_SETUP)
        self.assertIn("screen observer setup failed: capture region", out)
        self.assertEqual(self.StubObserver.instances[0].closes, 1)

    def test_ctrl_c_during_observer_setup_exits_130_without_clicking(self):
        self.StubObserver.prepare_error = KeyboardInterrupt()
        code, out, fake = invoke(["schedule", "nmixx-uoo7zw9oie", "--live", "--guarded-retry"])
        self.assertEqual(code, cli.EXIT_CANCELLED)
        self.assertIn("cancelled  (during screen observer setup; nothing clicked)", out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.StubObserver.instances[0].closes, 1)
        self.assertEqual(StubRealDriver.instances[0].clicks, 0)
        self.assertEqual(StubRealDriver.instances[0].closes, 1)
        self.assertEqual(fake.sleeps, [])


class ClickMetricsOutputTests(unittest.TestCase):
    def setUp(self):
        StubRealDriver.instances = []
        StubRealDriver.init_error = None
        StubRealDriver.preflight_error = None
        StubRealDriver.preflight_warnings = []
        StubRealDriver.click_error = None
        for patcher in (mock.patch.dict(driver.REAL_DRIVERS, {"macos": StubRealDriver}),
                        mock.patch.object(cli, "_keep_awake", lambda out: contextlib.nullcontext())):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_driver_metrics_are_reported_as_separate_down_and_up_lateness(self):
        class MetricDriver(StubRealDriver):
            def click(self):
                super().click()
                # posted 2 ms / 3 ms after this fake monotonic instant (1000.0 + 3600s)
                self.last_click_metrics = {"guard_ms": 2.0, "down_posted_monotonic": 4600.002,
                                           "up_posted_monotonic": 4600.003}

        with mock.patch.dict(driver.REAL_DRIVERS, {"macos": MetricDriver}):
            code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--live"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("guard ms:         [2.000]", out)
        self.assertIn("down posted ms:   [2.000]", out)
        self.assertIn("up posted ms:     [3.000]", out)
        self.assertIn("EXCLUDES phone and network arrival", out)

    def test_driver_without_metrics_prints_no_metric_lines(self):
        code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--live"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertNotIn("down posted ms:", out)
        self.assertIn("lateness ms:", out)


class KeepAwakeTests(unittest.TestCase):
    def setUp(self):
        StubRealDriver.instances = []
        StubRealDriver.init_error = None
        StubRealDriver.preflight_error = None
        StubRealDriver.preflight_warnings = []
        StubRealDriver.click_error = None
        patcher = mock.patch.dict(driver.REAL_DRIVERS, {"macos": StubRealDriver})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_live_run_starts_and_terminates_caffeinate(self):
        proc = mock.Mock()
        with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/caffeinate"), \
                mock.patch.object(cli.subprocess, "Popen", return_value=proc) as popen:
            code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--live"])
        self.assertEqual(code, cli.EXIT_OK)
        popen.assert_called_once()
        self.assertEqual(popen.call_args[0][0][:3], ["/usr/bin/caffeinate", "-di", "-w"])
        proc.terminate.assert_called_once()
        self.assertIn("keep-awake:       on", out)

    def test_dry_run_never_starts_caffeinate(self):
        with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/caffeinate"), \
                mock.patch.object(cli.subprocess, "Popen") as popen:
            code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie"])
        self.assertEqual(code, cli.EXIT_OK)
        popen.assert_not_called()
        self.assertNotIn("keep-awake", out)

    def test_missing_caffeinate_is_not_fatal(self):
        with mock.patch.object(cli.shutil, "which", return_value=None):
            code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--live"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertNotIn("keep-awake", out)


class RehearseCommandTests(unittest.TestCase):
    def setUp(self):
        StubRealDriver.instances = []
        StubRealDriver.init_error = None
        StubRealDriver.preflight_error = None
        StubRealDriver.preflight_warnings = []
        StubRealDriver.click_error = None
        for patcher in (mock.patch.dict(driver.REAL_DRIVERS, {"macos": StubRealDriver}),
                        mock.patch.object(cli, "_keep_awake", lambda out: contextlib.nullcontext())):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_rehearse_in_clicks_once_without_the_signup_pin(self):
        code, out, fake = invoke(["rehearse", "--in", "5", "--calibration", "/tmp/mock.json"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("REHEARSAL (REAL click", out)
        self.assertIn("no profile, no signup pin", out)
        self.assertIn("clicks attempted: 1  (REAL via macos)", out)
        kwargs = StubRealDriver.instances[0].kwargs
        self.assertNotIn("expected_bundle_id", kwargs, "the Mirroring pin must not leak into a rehearsal")
        self.assertEqual(kwargs["calibration_path"], Path("/tmp/mock.json"))
        self.assertEqual(fake.wall, T0 + timedelta(seconds=5))

    def test_bundle_is_forwarded_for_a_browser_mock_target(self):
        code, _, _ = invoke(["rehearse", "--in", "1", "--calibration", "/tmp/mock.json",
                             "--bundle", "com.google.Chrome"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(StubRealDriver.instances[0].kwargs["expected_bundle_id"], "com.google.Chrome")

    def test_at_takes_an_absolute_instant(self):
        target = (T0 + timedelta(seconds=30)).isoformat()
        code, out, fake = invoke(["rehearse", "--at", target, "--calibration", "/tmp/mock.json"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("waiting:          30.000s", out)
        self.assertEqual(fake.wall, T0 + timedelta(seconds=30))

    def test_at_without_offset_is_a_usage_error(self):
        code, out, fake = invoke(["rehearse", "--at", "2026-09-22T18:00:00", "--calibration", "/tmp/m.json"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("no UTC offset", out)
        self.assertEqual(fake.sleeps, [])

    def test_at_and_in_are_mutually_exclusive(self):
        code, _, fake = invoke(["rehearse", "--in", "5", "--at", "2026-09-22T18:00:00+08:00",
                                "--calibration", "/tmp/m.json"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(fake.sleeps, [])

    def test_calibration_is_required_so_the_signup_default_cannot_be_hit(self):
        code, _, fake = invoke(["rehearse", "--in", "5"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(StubRealDriver.instances, [])
        self.assertEqual(fake.sleeps, [])

    def test_rehearse_never_reads_a_profile(self):
        with mock.patch.object(cli, "load_profile", side_effect=AssertionError("profile read in rehearse")):
            code, _, _ = invoke(["rehearse", "--in", "1", "--calibration", "/tmp/mock.json"])
        self.assertEqual(code, cli.EXIT_OK)

    def test_rehearse_supports_guarded_retry(self):
        code, out, _ = invoke(["rehearse", "--in", "1", "--calibration", "/tmp/m.json", "--guarded-retry",
                               "--max-clicks", "6"])
        self.assertEqual(code, cli.EXIT_PROFILE_OR_PLAN_ERROR)
        self.assertIn("guarded retry allows at most 5", out)

    def test_stale_at_is_rejected_without_clicking(self):
        code, out, _ = invoke(["rehearse", "--at", (T0 - timedelta(seconds=5)).isoformat(),
                               "--calibration", "/tmp/m.json"])
        self.assertEqual(code, cli.EXIT_STALE)
        self.assertEqual(StubRealDriver.instances[0].clicks if StubRealDriver.instances else 0, 0)


class RealDriverResolutionTests(unittest.TestCase):
    def test_unknown_driver_exits_4(self):
        code, out, fake = invoke(["schedule", "nmixx-uoo7zw9oie", "--live", "--driver", "nope"])
        self.assertEqual(code, cli.EXIT_NO_REAL_DRIVER)
        self.assertIn("unknown driver 'nope'", out)
        self.assertEqual(fake.sleeps, [])

    def test_unimportable_backend_exits_4_with_reason(self):
        with mock.patch.dict(driver.REAL_DRIVER_IMPORTS, {"macos": ("fans_utils.no_such_backend", "X")}), \
                mock.patch.dict(driver.REAL_DRIVERS, {}, clear=True):
            code, out, _ = invoke(["schedule", "nmixx-uoo7zw9oie", "--live"])
        self.assertEqual(code, cli.EXIT_NO_REAL_DRIVER)
        self.assertIn("driver 'macos' is not usable here", out)

    def test_macos_backend_resolves_lazily_without_native_init(self):
        with mock.patch.dict(driver.REAL_DRIVERS, {}, clear=True):
            cls = driver.resolve_real_driver("macos")
        self.assertEqual((cls.__module__, cls.__name__), ("fans_utils.macos", "MacOSClickDriver"))
        self.assertTrue(cls.performs_real_click)

    def test_macos_screen_observer_resolves_to_the_shape_the_cli_calls(self):
        # Drift check against the native worker's module: the CLI does cls(driver) then
        # prepare/arm/changed/close. Resolving imports the class but constructs nothing.
        import inspect

        with mock.patch.dict(driver.REAL_OBSERVERS, {}, clear=True):
            cls = driver.resolve_real_observer("macos")
        self.assertEqual((cls.__module__, cls.__name__), ("fans_utils.visual", "MacOSVisualGuard"))
        for method in ("prepare", "arm", "changed", "close"):
            self.assertTrue(callable(getattr(cls, method, None)), f"observer must have {method}()")
        params = list(inspect.signature(cls).parameters.values())
        self.assertTrue(params, "MacOSVisualGuard must take the driver as its first argument")
        self.assertIn(params[0].kind, (inspect.Parameter.POSITIONAL_ONLY,
                                       inspect.Parameter.POSITIONAL_OR_KEYWORD))

    def test_unknown_observer_is_reported_like_an_unknown_driver(self):
        with self.assertRaises(driver.DriverUnavailable) as caught:
            driver.resolve_real_observer("nope")
        self.assertIn("unknown screen observer 'nope'", str(caught.exception))


class NumericArgumentTests(unittest.TestCase):
    def test_argparse_rejects_nonfinite_and_nonpositive(self):
        for extra in (["--adjust-ms", "nan"], ["--adjust-ms", "inf"], ["--interval-ms", "-inf"],
                      ["--max-duration-ms", "NaN"], ["--rehearse-in", "inf"],
                      ["--max-clicks", "0"], ["--max-clicks", "1.5"], ["--max-clicks", "x"]):
            with self.subTest(extra=extra):
                code, _, fake = invoke(["schedule", "nmixx-uoo7zw9oie", *extra])
                self.assertEqual(code, cli.EXIT_USAGE)
                self.assertEqual(fake.sleeps, [])

    def test_plan_range_errors_exit_1(self):
        for extra in (["--interval-ms", "-1"], ["--max-duration-ms", "0"]):
            with self.subTest(extra=extra):
                code, out, fake = invoke(["schedule", "nmixx-uoo7zw9oie", *extra])
                self.assertEqual(code, cli.EXIT_PROFILE_OR_PLAN_ERROR)
                self.assertIn("error:", out)
                self.assertEqual(fake.sleeps, [])

    def test_zero_interval_with_multiple_clicks_exits_1(self):
        code, out, fake = invoke(["schedule", "nmixx-uoo7zw9oie", "--max-clicks", "2", "--interval-ms", "0"])
        self.assertEqual(code, cli.EXIT_PROFILE_OR_PLAN_ERROR)
        self.assertIn("spin", out)
        self.assertEqual(fake.sleeps, [])

    def test_huge_adjust_and_rehearse_are_concise_errors(self):
        for extra in (["--adjust-ms", "1e300"], ["--rehearse-in", "1e300"]):
            with self.subTest(extra=extra):
                code, out, fake = invoke(["schedule", "nmixx-uoo7zw9oie", *extra])
                self.assertEqual(code, cli.EXIT_PROFILE_OR_PLAN_ERROR)
                self.assertIn("error:", out)
                self.assertNotIn("Traceback", out)
                self.assertEqual(fake.sleeps, [])


if __name__ == "__main__":
    unittest.main()
