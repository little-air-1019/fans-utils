import unittest
from datetime import datetime, timedelta

from fans_utils.scheduler import ARM_LEAD_S, COARSE_TICK_S, ClickPlan, PlanError, run
from tests.fakes import T0, FakeClock, FakeObserver, TimestampDriver

START = T0 + timedelta(seconds=10)  # nominal target 10s after the fake clock's origin
DEADLINE = 1000.0 + 10.0  # fake monotonic instant of START


def _run(fake: FakeClock, *, fail_on=None, latency_s=0.0, metrics=False, on_tick=None, observer=None,
         **plan_kwargs):
    plan = ClickPlan(nominal_start=plan_kwargs.pop("nominal_start", START), **plan_kwargs)
    driver = TimestampDriver(fake, fail_on=fail_on, latency_s=latency_s, metrics=metrics)
    return run(plan, driver, fake.clock(), on_tick=on_tick, observer=observer), driver, plan


def _guarded(fake: FakeClock, results=(), *, capture_s=0.02, observer_kwargs=None, **kwargs):
    observer = FakeObserver(fake, results, capture_s=capture_s, **(observer_kwargs or {}))
    kwargs.setdefault("max_clicks", 3)
    kwargs.setdefault("interval_ms", 150.0)
    kwargs.setdefault("max_duration_ms", 800.0)
    report, driver, plan = _run(fake, observer=observer, guarded_retry=True, **kwargs)
    return report, driver, observer


class ClockCorrectnessTests(unittest.TestCase):
    def test_exact_sleep_fires_at_target_with_zero_lateness(self):
        fake = FakeClock()
        report, driver, plan = _run(fake)
        self.assertEqual(report.status, "completed")
        self.assertEqual(report.clicks, 1)
        self.assertEqual(driver.wall_times, [plan.effective_target])
        self.assertAlmostEqual(report.lateness_ms[0], 0.0, places=6)
        self.assertEqual(report.effective_target, START)
        self.assertTrue(report.dry_run)

    def test_undersleep_never_fires_early(self):
        # First 30 wakeups return at half the requested sleep (spurious wakeups), then exact.
        fake = FakeClock(sleep_factor=0.5, factor_for_first=30)
        report, driver, plan = _run(fake)
        self.assertEqual(report.status, "completed")
        self.assertEqual(report.clicks, 1)
        self.assertGreaterEqual(driver.wall_times[0], plan.effective_target)
        self.assertGreaterEqual(report.lateness_ms[0], 0.0)
        self.assertGreater(len(fake.sleeps), 30, "the scheduler must re-sleep after every early wakeup")
        self.assertTrue(all(s > 0 for s in fake.sleeps), "zero-length sleep = CPU spin")

    def test_undersleep_in_final_phase_converges_and_stays_on_target(self):
        fake = FakeClock(start=START - timedelta(milliseconds=800), sleep_factor=0.5, factor_for_first=10)
        report, driver, plan = _run(fake)
        self.assertEqual(report.clicks, 1)
        self.assertGreaterEqual(driver.mono_times[0], 1000.0 + 0.8)  # strict: never before the deadline
        self.assertLess(len(fake.sleeps), 20)

    def test_asymptotic_undersleep_still_terminates_without_firing_early(self):
        # Every sleep returns at half the requested time, forever. Without a minimum sleep quantum
        # this never reaches the deadline at float precision; with it, progress is guaranteed.
        fake = FakeClock(start=START - timedelta(milliseconds=800), sleep_factor=0.5)
        report, driver, plan = _run(fake)
        deadline = 1000.0 + 0.8
        self.assertEqual(report.clicks, 1)
        self.assertGreaterEqual(driver.mono_times[0], deadline)
        self.assertLessEqual(driver.mono_times[0] - deadline, 1e-6)  # at most one quantum late
        self.assertLess(len(fake.sleeps), 60)
        self.assertTrue(all(s >= 1e-6 for s in fake.sleeps))

    def test_oversleep_is_reported_as_positive_lateness(self):
        fake = FakeClock(sleep_factor=1.3)
        report, driver, plan = _run(fake)
        self.assertEqual(report.clicks, 1)
        self.assertGreater(report.lateness_ms[0], 0.0)
        self.assertGreaterEqual(driver.wall_times[0], plan.effective_target)

    def test_negative_adjust_fires_before_nominal_but_never_before_effective(self):
        fake = FakeClock()
        report, driver, plan = _run(fake, adjust_ms=-50.0)
        self.assertEqual(plan.effective_target, START - timedelta(milliseconds=50))
        self.assertEqual(driver.wall_times[0], plan.effective_target)
        self.assertLess(driver.wall_times[0], START)
        self.assertAlmostEqual(report.lateness_ms[0], 0.0, places=6)

    def test_positive_adjust_delays_target(self):
        fake = FakeClock()
        _, driver, plan = _run(fake, adjust_ms=200.0)
        self.assertEqual(driver.wall_times[0], START + timedelta(milliseconds=200))

    def test_wall_clock_step_forward_during_wait_still_waits_for_wall_target(self):
        fake = FakeClock()
        target = T0 + timedelta(hours=1)
        stepped = []

        def hook(_s):
            if len(fake.sleeps) == 5 and not stepped:
                fake.wall += timedelta(minutes=30)  # NTP step forward, monotonic unchanged
                stepped.append(True)

        fake.on_sleep = hook
        _, driver, plan = _run(fake, nominal_start=target)
        self.assertEqual(driver.wall_times, [target])
        # monotonic elapsed is 30 minutes shorter than the wall gap: the scheduler re-anchored
        self.assertAlmostEqual(driver.mono_times[0] - 1000.0, 3600 - 1800, delta=1.0)

    def test_wall_clock_step_backward_during_wait_does_not_fire_early(self):
        fake = FakeClock()
        target = T0 + timedelta(hours=1)
        stepped = []

        def hook(_s):
            if len(fake.sleeps) == 5 and not stepped:
                fake.wall -= timedelta(minutes=10)
                stepped.append(True)

        fake.on_sleep = hook
        _, driver, _ = _run(fake, nominal_start=target)
        self.assertEqual(driver.wall_times, [target])
        self.assertAlmostEqual(driver.mono_times[0] - 1000.0, 3600 + 600, delta=1.0)

    def test_long_wait_sleeps_coarsely_without_spinning_and_ticks(self):
        fake = FakeClock()
        target = T0 + timedelta(hours=2)
        ticks: list[float] = []
        _run(fake, nominal_start=target, on_tick=ticks.append)
        self.assertLessEqual(len(fake.sleeps), 2 * 3600 / COARSE_TICK_S + 5)
        self.assertTrue(all(s > 0 for s in fake.sleeps))
        self.assertEqual(len(ticks), len(fake.sleeps) - 1)  # one tick per coarse sleep, none in the final phase
        self.assertEqual(ticks, sorted(ticks, reverse=True))
        self.assertAlmostEqual(ticks[0], 7200.0)


class LateTargetTests(unittest.TestCase):
    def test_stale_target_is_rejected_without_clicking_or_sleeping(self):
        fake = FakeClock(start=START + timedelta(seconds=1))
        report, driver, _ = _run(fake)
        self.assertEqual(report.status, "stale")
        self.assertEqual(report.clicks, 0)
        self.assertEqual(driver.clicks, 0)
        self.assertEqual(fake.sleeps, [])
        self.assertIn("passed", report.detail)

    def test_target_exactly_now_fires_immediately(self):
        fake = FakeClock(start=START)
        report, _, _ = _run(fake)
        self.assertEqual(report.status, "completed")
        self.assertEqual(report.clicks, 1)
        self.assertEqual(report.lateness_ms, [0.0])


class BoundedRunTests(unittest.TestCase):
    def test_default_is_a_single_click(self):
        report, driver, _ = _run(FakeClock())
        self.assertEqual((report.clicks, driver.clicks), (1, 1))

    def test_max_clicks_with_interval(self):
        fake = FakeClock()
        report, driver, _ = _run(fake, max_clicks=3, interval_ms=100.0)
        self.assertEqual(report.clicks, 3)
        self.assertEqual([round(x) for x in report.lateness_ms], [0, 100, 200])
        interval_sleeps = [s for s in fake.sleeps if abs(s - 0.1) < 1e-9]
        self.assertEqual(len(interval_sleeps), 2, "no interval sleep after the last click")

    def test_duration_cap_stops_before_count_is_exhausted(self):
        report, driver, _ = _run(FakeClock(), max_clicks=10, interval_ms=100.0, max_duration_ms=250.0)
        self.assertEqual(report.status, "completed")
        self.assertEqual(report.clicks, 3)  # at 0, 100, 200 ms; 300 ms exceeds the cap
        self.assertEqual(driver.clicks, 3)
        self.assertIn("duration cap", report.detail)

    def test_huge_interval_does_not_sleep_past_duration_cap(self):
        fake = FakeClock()
        report, driver, _ = _run(fake, max_clicks=5, interval_ms=3_600_000.0, max_duration_ms=1000.0)
        self.assertEqual(report.clicks, 1)
        self.assertIn("duration cap", report.detail)
        self.assertLessEqual(fake.mono - 1010.0, 1.0, "run ended within the 1s cap, not after 1h")

    def test_slow_driver_gets_fixed_gap_not_catch_up_burst(self):
        fake = FakeClock()
        report, driver, _ = _run(fake, max_clicks=3, interval_ms=100.0, latency_s=0.5)
        self.assertEqual(report.clicks, 3)
        gaps = [b - a for a, b in zip(driver.mono_times, driver.mono_times[1:])]
        self.assertTrue(all(abs(g - 0.6) < 1e-9 for g in gaps), gaps)  # 500 ms click + 100 ms interval

    def test_slow_driver_is_stopped_by_duration_cap(self):
        report, driver, _ = _run(FakeClock(), max_clicks=10, interval_ms=100.0, max_duration_ms=1000.0,
                                 latency_s=0.5)
        self.assertEqual(report.clicks, 2)  # dispatched at 0 and 600 ms; 1200 ms would exceed the cap
        self.assertIn("duration cap", report.detail)

    def test_driver_failure_is_reported_and_not_retried(self):
        report, driver, _ = _run(FakeClock(), max_clicks=5, fail_on=2)
        self.assertEqual(report.status, "error")
        self.assertEqual(report.clicks, 1)  # the failing attempt is not counted as fired
        self.assertEqual(driver.clicks, 2)  # exactly two attempts: one ok, one failed, no retry
        self.assertIn("RuntimeError", report.detail)


class CancellationTests(unittest.TestCase):
    def test_ctrl_c_during_wait(self):
        fake = FakeClock()

        def hook(_s):
            if len(fake.sleeps) == 3:
                raise KeyboardInterrupt

        fake.on_sleep = hook
        report, driver, _ = _run(fake)
        self.assertEqual(report.status, "cancelled")
        self.assertEqual((report.clicks, driver.clicks), (0, 0))

    def test_ctrl_c_between_clicks_keeps_count_so_far(self):
        fake = FakeClock()

        def hook(s):
            if abs(s - 0.1) < 1e-9:
                raise KeyboardInterrupt

        fake.on_sleep = hook
        report, driver, _ = _run(fake, max_clicks=5, interval_ms=100.0)
        self.assertEqual(report.status, "cancelled")
        self.assertEqual((report.clicks, driver.clicks), (1, 1))


class GuardedRetryTests(unittest.TestCase):
    def test_unchanged_screen_fires_all_attempts_and_says_unchanged(self):
        fake = FakeClock()
        report, driver, obs = _guarded(fake)
        self.assertEqual(report.status, "completed")
        self.assertEqual((report.clicks, driver.clicks), (3, 3))
        self.assertEqual(report.screen_checks, 3, "one fresh capture after every click, including the last")
        self.assertIn("screen unchanged after 3 attempt(s); confirm in app", report.detail)
        self.assertEqual(driver.mono_times[0], DEADLINE, "first click exactly at the target despite arming")
        gaps = [b - a for a, b in zip(driver.mono_times, driver.mono_times[1:])]
        self.assertTrue(all(g >= 0.15 for g in gaps), gaps)  # gap + capture time, never a catch-up burst

    def test_arm_happens_before_the_deadline_and_does_not_delay_the_first_click(self):
        fake = FakeClock()
        report, driver, obs = _guarded(fake, capture_s=0.08)
        self.assertIsNotNone(obs.arm_mono)
        self.assertLessEqual(obs.arm_mono, DEADLINE)
        self.assertGreaterEqual(obs.arm_mono, DEADLINE - ARM_LEAD_S - 1e-9)
        self.assertEqual(report.lateness_ms[0], 0.0)
        self.assertEqual(driver.mono_times[0], DEADLINE)

    def test_one_second_lead_arms_around_t_minus_lead_and_dispatches_at_t(self):
        # Rehearsal-shaped: the run starts inside the final window, arm costs 30 ms.
        fake = FakeClock(start=START - timedelta(seconds=1))
        deadline = 1000.0 + 1.0  # the clock starts 1s before the target
        report, driver, obs = _guarded(fake, capture_s=0.03)
        # armed at the lead point (at most one MIN_SLEEP_S quantum after it), never at the deadline
        self.assertGreaterEqual(obs.arm_mono, deadline - ARM_LEAD_S)
        self.assertLessEqual(obs.arm_mono, deadline - ARM_LEAD_S + 1e-5)
        self.assertAlmostEqual(driver.mono_times[0], deadline)
        self.assertEqual(report.lateness_ms[0], 0.0)
        self.assertAlmostEqual(report.baseline_age_ms, ARM_LEAD_S * 1000.0, places=2)
        self.assertTrue(all(s > 0 for s in fake.sleeps))

    def test_change_during_the_gap_is_seen_before_the_second_click(self):
        # The screen changes 60 ms after the first click, i.e. inside the 150 ms gap. The check
        # immediately before attempt 2 must see it and attempt 2 must not fire.
        fake = FakeClock()
        obs = FakeObserver(fake, capture_s=0.02, change_at_mono=DEADLINE + 0.06)
        report, driver, _ = _run(fake, observer=obs, guarded_retry=True, max_clicks=3, interval_ms=150.0,
                                 max_duration_ms=800.0)
        self.assertEqual(report.status, "screen_changed")
        self.assertEqual(driver.clicks, 1)
        self.assertEqual(len(obs.check_monos), 1)
        self.assertGreaterEqual(obs.check_monos[0], DEADLINE + 0.15)

    def test_target_already_now_arms_then_clicks(self):
        fake = FakeClock(start=START)
        report, driver, obs = _guarded(fake, capture_s=0.03)
        self.assertEqual(obs.arm_mono, 1000.0)
        self.assertEqual(report.clicks, 3)
        self.assertAlmostEqual(report.lateness_ms[0], 30.0)  # arm could not be hidden: the cost is visible
        self.assertAlmostEqual(report.baseline_age_ms, 30.0)

    def test_plain_run_has_no_baseline_age(self):
        report, _, _ = _run(FakeClock())
        self.assertIsNone(report.baseline_age_ms)

    def test_change_after_first_click_stops_exactly_there(self):
        fake = FakeClock()
        report, driver, obs = _guarded(fake, results=[True])
        self.assertEqual(report.status, "screen_changed")
        self.assertEqual((report.clicks, driver.clicks), (1, 1))
        self.assertEqual(report.screen_checks, 1)
        self.assertIn("screen_changed after attempt 1 of 3", report.detail)
        self.assertIn("confirm in app", report.detail)
        self.assertNotIn("success", report.detail.lower().replace("not proof", ""))

    def test_ignored_first_attempt_then_change_stops_after_second(self):
        fake = FakeClock()
        report, driver, obs = _guarded(fake, results=[False, True])
        self.assertEqual(report.status, "screen_changed")
        self.assertEqual(driver.clicks, 2)
        self.assertEqual(report.screen_checks, 2)
        self.assertIn("after attempt 2 of 3", report.detail)

    def test_change_after_last_attempt_is_still_reported_as_screen_changed(self):
        fake = FakeClock()
        report, driver, _ = _guarded(fake, results=[False, False, True])
        self.assertEqual(report.status, "screen_changed")
        self.assertEqual(driver.clicks, 3)
        self.assertIn("after attempt 3 of 3", report.detail)

    def test_capture_failure_aborts_with_no_more_clicks(self):
        fake = FakeClock()
        report, driver, obs = _guarded(fake, observer_kwargs={"fail_on_check": 1})
        self.assertEqual(report.status, "error")
        self.assertEqual(driver.clicks, 1)
        self.assertTrue(report.detail.startswith("screen check failed: OSError"), report.detail)

    def test_baseline_failure_aborts_before_any_click(self):
        fake = FakeClock()
        report, driver, obs = _guarded(fake, observer_kwargs={"fail_on_arm": PermissionError("no screen recording")})
        self.assertEqual(report.status, "error")
        self.assertEqual(driver.clicks, 0)
        self.assertTrue(report.detail.startswith("screen baseline failed: PermissionError"), report.detail)

    def test_click_refusal_is_reported_bare_and_stops_retrying(self):
        fake = FakeClock()
        report, driver, obs = _guarded(fake, fail_on=2)
        self.assertEqual(report.status, "error")
        self.assertEqual(report.detail, "RuntimeError: device gone")
        self.assertEqual(driver.clicks, 2)
        self.assertEqual(report.screen_checks, 1)

    def test_ctrl_c_during_screen_check_is_cancelled_and_keeps_count(self):
        fake = FakeClock()
        obs = FakeObserver(fake, [])
        original = obs.changed

        def interrupted():
            original()
            raise KeyboardInterrupt

        obs.changed = interrupted
        report, driver, _ = _run(fake, observer=obs, guarded_retry=True, max_clicks=3, interval_ms=150.0,
                                 max_duration_ms=800.0)
        self.assertEqual(report.status, "cancelled")
        self.assertEqual(driver.clicks, 1)

    def test_duration_cap_bounds_guarded_attempts(self):
        fake = FakeClock()
        report, driver, _ = _guarded(fake, capture_s=0.1, max_clicks=5, interval_ms=300.0, max_duration_ms=800.0)
        self.assertEqual(report.status, "completed")
        self.assertLessEqual(driver.clicks, 3)
        self.assertIn("duration cap", report.detail)
        self.assertLessEqual(fake.mono - DEADLINE, 0.8 + 0.1)

    def test_guarded_retry_without_observer_is_refused(self):
        plan = ClickPlan(nominal_start=START, guarded_retry=True, max_clicks=3, interval_ms=150.0,
                         max_duration_ms=800.0)
        fake = FakeClock()
        with self.assertRaises(PlanError):
            run(plan, TimestampDriver(fake), fake.clock())
        self.assertEqual(fake.sleeps, [])

    def test_plain_plan_ignores_a_passed_observer(self):
        fake = FakeClock()
        obs = FakeObserver(fake, [True])
        report, driver, _ = _run(fake, observer=obs, max_clicks=2, interval_ms=100.0)
        self.assertEqual(report.clicks, 2)
        self.assertIsNone(obs.arm_mono)
        self.assertEqual(report.screen_checks, 0)

    def test_guarded_bounds(self):
        ok = dict(guarded_retry=True, max_clicks=5, interval_ms=100.0, max_duration_ms=2000.0)
        ClickPlan(nominal_start=START, **ok)
        for bad in (dict(ok, max_clicks=6), dict(ok, interval_ms=99.9), dict(ok, max_duration_ms=2000.1),
                    dict(ok, guarded_retry="yes")):
            with self.subTest(bad=bad), self.assertRaises(PlanError):
                ClickPlan(nominal_start=START, **bad)
        # the same values are legal for a plain plan
        ClickPlan(nominal_start=START, max_clicks=6, interval_ms=50.0, max_duration_ms=3000.0)


class ClickMetricsTests(unittest.TestCase):
    def test_driver_without_metrics_reports_none_per_click(self):
        report, _, _ = _run(FakeClock(), max_clicks=2, interval_ms=100.0)
        self.assertEqual(report.guard_ms, [None, None])
        self.assertEqual(report.down_lateness_ms, [None, None])
        self.assertEqual(report.up_lateness_ms, [None, None])

    def test_down_and_up_lateness_are_relative_to_the_effective_target(self):
        fake = FakeClock()
        report, driver, _ = _run(fake, metrics=True, latency_s=0.004, max_clicks=2, interval_ms=100.0)
        self.assertEqual(report.lateness_ms[0], 0.0)  # dispatch: scheduler handed off exactly at target
        self.assertAlmostEqual(report.guard_ms[0], 4.0)
        self.assertAlmostEqual(report.down_lateness_ms[0], 4.0)  # posted after 4 ms of guards
        self.assertAlmostEqual(report.up_lateness_ms[0], 5.0)
        # second click: dispatched at 5 ms (first click) + 100 ms gap
        self.assertAlmostEqual(report.lateness_ms[1], 105.0)
        self.assertAlmostEqual(report.down_lateness_ms[1], 109.0)

    def test_partial_metrics_dict_is_tolerated(self):
        fake = FakeClock()
        driver = TimestampDriver(fake)
        driver.last_click_metrics = {"guard_ms": "bogus", "down_posted_monotonic": DEADLINE + 0.002}
        report = run(ClickPlan(nominal_start=START), driver, fake.clock())
        self.assertEqual(report.guard_ms, [None])
        self.assertAlmostEqual(report.down_lateness_ms[0], 2.0)
        self.assertEqual(report.up_lateness_ms, [None])


class PlanValidationTests(unittest.TestCase):
    def test_rejects_bad_values(self):
        bad = [
            dict(adjust_ms=float("nan")),
            dict(adjust_ms=float("inf")),
            dict(adjust_ms=1e300),  # finite but overflows datetime arithmetic
            dict(interval_ms=float("-inf")),
            dict(interval_ms=-1.0),
            dict(interval_ms=0.0, max_clicks=2),  # zero gap between clicks would spin
            dict(max_duration_ms=0.0),
            dict(max_duration_ms=float("nan")),
            dict(max_clicks=0),
            dict(max_clicks=True),
            dict(max_clicks=2.0),
            dict(adjust_ms="5"),
        ]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(PlanError):
                ClickPlan(nominal_start=START, **kwargs)

    def test_zero_interval_is_fine_for_a_single_click(self):
        ClickPlan(nominal_start=START, interval_ms=0.0, max_clicks=1)

    def test_rejects_naive_start(self):
        with self.assertRaises(PlanError):
            ClickPlan(nominal_start=datetime(2026, 9, 22, 19, 0))


if __name__ == "__main__":
    unittest.main()
