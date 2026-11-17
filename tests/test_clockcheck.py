import subprocess
import unittest
from datetime import UTC, datetime

from fans_utils import clockcheck as cc

FIXED_NOW = lambda: datetime(2026, 9, 22, 9, 0, tzinfo=UTC)  # noqa: E731


def fake_runner(lines_by_host, *, exit_by_host=None, timeout_hosts=()):
    """Build a subprocess.run stand-in that also records every argv it was asked to execute."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        host = argv[-1]
        if host in timeout_hosts:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))
        code = (exit_by_host or {}).get(host, 0)
        return subprocess.CompletedProcess(argv, code, stdout=lines_by_host.get(host, ""), stderr="")

    run.calls = calls
    return run


class ParseTests(unittest.TestCase):
    def test_parses_real_macos_line(self):
        s = cc.parse_sntp_line("time.apple.com", "+0.054980 +/- 0.117337 time.apple.com 17.253.114.35\n")
        self.assertTrue(s.ok)
        self.assertAlmostEqual(s.offset_s, 0.054980)
        self.assertAlmostEqual(s.uncertainty_s, 0.117337)
        self.assertEqual(s.server, "17.253.114.35")
        lo, hi = s.interval_ms
        self.assertAlmostEqual(lo, -62.357)
        self.assertAlmostEqual(hi, 172.317)

    def test_negative_offset_keeps_sign(self):
        s = cc.parse_sntp_line("h", "-0.020000 +/- 0.001000 h 1.2.3.4")
        self.assertTrue(s.ok)
        self.assertAlmostEqual(s.offset_s, -0.02)

    def test_rejects_bad_inputs(self):
        bad = {
            "empty": "",
            "garbage": "sntp: no response",
            "nan": "nan +/- 0.001 h 1.2.3.4",
            "inf": "+inf +/- 0.001 h 1.2.3.4",
            "nan_unc": "+0.01 +/- nan h 1.2.3.4",
            "negative_unc": "+0.01 +/- -0.001 h 1.2.3.4",
            "unbounded": "+0.01 +/- 1.5 h 1.2.3.4",
            "non_numeric": "+abc +/- 0.001 h 1.2.3.4",
            "huge_finite_offset": "1e308 +/- 0.001 h 1.2.3.4",
            "huge_negative_offset": "-1e308 +/- 0.001 h 1.2.3.4",
            "offset_just_over_bound": f"{cc.MAX_OFFSET_S + 0.001:.3f} +/- 0.001 h 1.2.3.4",
        }
        for name, text in bad.items():
            with self.subTest(name):
                s = cc.parse_sntp_line("h", text)
                self.assertFalse(s.ok)
                self.assertTrue(s.error)

    def test_offset_at_bound_is_still_accepted(self):
        s = cc.parse_sntp_line("h", f"{cc.MAX_OFFSET_S:.3f} +/- 0.001 h 1.2.3.4")
        self.assertTrue(s.ok)


class FiniteArithmeticTests(unittest.TestCase):
    """Regression: a finite but absurd offset (1e308 s) used to survive parsing, then
    overflow to inf on the *1000 ms conversion and crash suggest_adjust_ms with
    OverflowError. Every accepted sample must yield finite ms arithmetic."""

    def test_huge_offset_is_rejected_before_ms_conversion(self):
        run = fake_runner({"a": "1e308 +/- 0.001 a 1.1.1.1"})
        r = cc.measure(["a"], runner=run)
        self.assertEqual(r.status, "no_data")
        self.assertIsNone(r.suggested_adjust_ms)
        self.assertIsNone(r.interval_ms)
        self.assertIn("exceeds", r.samples[0].error)

    def test_accepted_samples_always_give_finite_interval_and_suggestion(self):
        import math
        for offset in ("-60.000", "-0.500", "0.000", "+0.085", "+60.000"):
            with self.subTest(offset):
                run = fake_runner({"a": f"{offset} +/- 0.001 a 1.1.1.1"})
                r = cc.measure(["a"], runner=run)
                self.assertEqual(r.status, "single-source")
                self.assertTrue(all(math.isfinite(x) for x in r.interval_ms))
                self.assertTrue(math.isfinite(r.offset_ms))
                self.assertIsInstance(r.suggested_adjust_ms, int)
                lo, _ = r.interval_ms
                self.assertGreaterEqual(r.suggested_adjust_ms + lo, 0.0)

    def test_intersect_rejects_non_finite_bounds(self):
        self.assertIsNone(cc.intersect([(float("-inf"), float("inf"))]))
        self.assertIsNone(cc.intersect([(0.0, 10.0), (5.0, float("inf"))]))
        self.assertIsNone(cc.intersect([(float("nan"), 1.0)]))

    def test_suggest_adjust_ms_never_overflows_for_accepted_range(self):
        # Widest interval an accepted sample can produce: +/-60 s offset, <1 s uncertainty.
        for lo in (-61_000.0, 61_000.0, 0.0):
            self.assertIsInstance(cc.suggest_adjust_ms(lo), int)


class ArgvSafetyTests(unittest.TestCase):
    def test_argv_is_read_only_and_bounded(self):
        argv = cc.sntp_argv("time.apple.com", 2)
        self.assertEqual(argv, ["sntp", "-t", "2", "-n", "1", "time.apple.com"])
        for flag in ("-s", "-S", "-r", "-z"):
            self.assertNotIn(flag, argv)

    def test_rejects_flag_like_or_bad_hosts_and_timeouts(self):
        for host in ("-S", "-s", "", "a b"):
            with self.assertRaises(cc.ClockCheckError):
                cc.sntp_argv(host, 2)
        for t in (0, 6, -1):
            with self.assertRaises(cc.ClockCheckError):
                cc.sntp_argv("h", t)

    def test_measure_never_passes_mutating_flags_and_caps_requests(self):
        run = fake_runner({h: f"+0.08 +/- 0.01 {h} 1.1.1.1" for h in cc.DEFAULT_HOSTS})
        cc.measure(cc.DEFAULT_HOSTS, samples=2, runner=run, now=FIXED_NOW)
        self.assertEqual(len(run.calls), 6)
        for argv in run.calls:
            self.assertEqual(argv[:5], ["sntp", "-t", "2", "-n", "1"])
            self.assertFalse(any(a in ("-s", "-S") for a in argv))
        with self.assertRaises(cc.ClockCheckError):
            cc.measure(["a", "b", "c", "d"], samples=2, runner=run)
        with self.assertRaises(cc.ClockCheckError):
            cc.measure(cc.DEFAULT_HOSTS, samples=3, runner=run)
        with self.assertRaises(cc.ClockCheckError):
            cc.measure([], runner=run)
        with self.assertRaises(cc.ClockCheckError):
            cc.measure(["-S"], runner=run)  # validated before anything runs
        self.assertEqual(len(run.calls), 6, "rejected calls must not execute anything")


class ConsensusTests(unittest.TestCase):
    def test_intersection_of_coordinator_readings(self):
        run = fake_runner({
            "time.apple.com": "+0.082431 +/- 0.055221 time.apple.com 1.1.1.1",
            "time.cloudflare.com": "+0.090610 +/- 0.073715 time.cloudflare.com 2.2.2.2",
            "time.google.com": "+0.085381 +/- 0.010001 time.google.com 3.3.3.3",
        })
        r = cc.measure(cc.DEFAULT_HOSTS, runner=run, now=FIXED_NOW)
        self.assertEqual(r.status, "consensus")
        self.assertEqual(r.failures, 0)
        lo, hi = r.interval_ms
        self.assertAlmostEqual(lo, 75.38, places=2)
        self.assertAlmostEqual(hi, 95.382, places=2)
        self.assertAlmostEqual(r.offset_ms, (lo + hi) / 2)
        self.assertEqual(r.suggested_adjust_ms, -70)  # ceil(-75.38 + 5) = -70
        self.assertEqual(r.captured_at_utc, "2026-09-22T09:00:00+00:00")
        self.assertIn("Rerun", r.note)

    def test_disagreement_is_inconclusive_with_no_suggestion(self):
        run = fake_runner({
            "a": "+0.100 +/- 0.005 a 1.1.1.1",
            "b": "-0.100 +/- 0.005 b 2.2.2.2",
        })
        r = cc.measure(["a", "b"], runner=run)
        self.assertEqual(r.status, "inconclusive")
        self.assertIsNone(r.interval_ms)
        self.assertIsNone(r.suggested_adjust_ms)
        self.assertTrue(any("do not overlap" in x for x in r.reasons))

    def test_single_source_is_labelled_not_consensus(self):
        run = fake_runner({"a": "+0.080 +/- 0.010 a 1.1.1.1", "b": "bogus"})
        r = cc.measure(["a", "b"], runner=run)
        self.assertEqual(r.status, "single-source")
        self.assertEqual(r.failures, 1)
        self.assertEqual(r.suggested_adjust_ms, cc.suggest_adjust_ms(70.0))

    def test_timeouts_and_nonzero_exits_are_counted_as_failures(self):
        run = fake_runner({"c": "+0.080 +/- 0.010 c 3.3.3.3"}, exit_by_host={"b": 1}, timeout_hosts=("a",))
        r = cc.measure(["a", "b", "c"], runner=run)
        self.assertEqual(r.failures, 2)
        self.assertEqual(r.status, "single-source")
        errors = {s.host: s.error for s in r.samples if not s.ok}
        self.assertIn("timeout", errors["a"])
        self.assertIn("exit 1", errors["b"])

    def test_all_failed_is_no_data(self):
        run = fake_runner({}, timeout_hosts=("a", "b"))
        r = cc.measure(["a", "b"], runner=run)
        self.assertEqual(r.status, "no_data")
        self.assertIsNone(r.suggested_adjust_ms)
        self.assertEqual(r.failures, 2)

    def test_missing_sntp_binary_is_a_failure_not_a_crash(self):
        def run(argv, **kw):
            raise FileNotFoundError("sntp")
        r = cc.measure(["a"], runner=run)
        self.assertEqual(r.status, "no_data")
        self.assertIn("cannot run sntp", r.samples[0].error)


class AdjustArithmeticTests(unittest.TestCase):
    def test_intersect(self):
        self.assertEqual(cc.intersect([(0, 10), (5, 20)]), (5, 10))
        self.assertEqual(cc.intersect([(0, 10), (10, 20)]), (10, 10))
        self.assertIsNone(cc.intersect([(0, 10), (11, 20)]))
        self.assertIsNone(cc.intersect([]))

    def test_suggestion_never_fires_early_and_rounds_later(self):
        self.assertEqual(cc.suggest_adjust_ms(75.0), -70)
        self.assertEqual(cc.suggest_adjust_ms(75.38), -70)  # ceil(-70.38)
        self.assertEqual(cc.suggest_adjust_ms(74.2), -69)
        self.assertEqual(cc.suggest_adjust_ms(-20.0), 25)  # interval including 0 -> positive
        for lo in (75.0, 75.38, 74.2, -20.0, 0.0, 3.0):
            self.assertGreaterEqual(cc.suggest_adjust_ms(lo) + lo, 0.0, "would fire early")


class ScopeWordingTests(unittest.TestCase):
    """Both outputs must say what is measured (Mac vs public NTP) and what is not
    (FANS server clock, Mirroring latency, network RTT). 'true time' is banned wording."""

    def _report(self):
        run = fake_runner({h: f"+0.085 +/- 0.010 {h} 1.1.1.1" for h in cc.DEFAULT_HOSTS})
        return cc.measure(cc.DEFAULT_HOSTS, runner=run, now=FIXED_NOW)

    def test_text_output_states_scope_and_avoids_true_time(self):
        text = cc.format_text(self._report())
        self.assertIn("Mac system clock vs public NTP sources", text)
        self.assertIn("FANS server clock", text)
        self.assertIn("iPhone Mirroring input latency", text)
        self.assertIn("round-trip time", text)
        self.assertNotIn("true time", text.lower())
        self.assertIn("Rerun this check shortly before use", text)

    def test_json_output_states_scope_and_avoids_true_time(self):
        import json
        blob = json.dumps(self._report().to_dict())
        self.assertIn("Mac system clock vs public NTP sources", blob)
        self.assertIn("FANS server clock", blob)
        self.assertIn("Mirroring", blob)
        self.assertNotIn("true time", blob.lower())
        self.assertIn("Rerun this check shortly before use", blob)

    def test_scope_is_present_even_when_inconclusive(self):
        run = fake_runner({"a": "+0.100 +/- 0.005 a 1.1.1.1", "b": "-0.100 +/- 0.005 b 2.2.2.2"})
        text = cc.format_text(cc.measure(["a", "b"], runner=run))
        self.assertIn("FANS server clock", text)
        self.assertIn("Rerun this check shortly before use", text)
        self.assertNotIn("true time", text.lower())

    def test_sign_convention_refers_to_measured_source(self):
        r = self._report()
        self.assertIn("measured", r.sign_convention)
        self.assertNotIn("true time", r.sign_convention.lower())


class CliTests(unittest.TestCase):
    def test_json_and_text_output_and_exit_codes(self):
        import io
        import json
        from contextlib import redirect_stdout
        from unittest import mock

        run = fake_runner({h: f"+0.080 +/- 0.010 {h} 1.1.1.1" for h in cc.DEFAULT_HOSTS})
        with mock.patch.object(subprocess, "run", run):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cc.main(["--json"])
            self.assertEqual(code, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(data["status"], "consensus")
            self.assertEqual(data["suggested_adjust_ms"], -65)
            self.assertIn("Rerun", data["note"])

            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cc.main(["--host", "x", "--host", "y"])
            self.assertEqual(code, 1, "no data -> exit 1")
            self.assertIn("none (inconclusive or no data)", buf.getvalue())

    def test_bad_args_exit_2(self):
        import io
        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()):
            self.assertEqual(cc.main(["--samples", "9"]), 2)
            self.assertEqual(cc.main(["--timeout", "99"]), 2)


if __name__ == "__main__":
    unittest.main()
