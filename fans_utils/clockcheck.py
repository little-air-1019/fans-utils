"""Read-only local clock check via macOS `sntp`.

Measures how far this Mac's clock is from a few public NTP servers and turns the result into
ONE conservative `--adjust-ms` suggestion for `fans-utils schedule`. It never sets or slews the
system clock: the only sntp flags ever passed are `-t` (timeout) and `-n 1` (one DNS record).

SCOPE: this compares the Mac system clock against public NTP sources ONLY. It does not observe
the FANS server clock, iPhone Mirroring input latency, or cellular/network round-trip time, and
it cannot tell you when the Apply button actually becomes live.

Sign convention (same as sntp): offset = remote - local. Positive offset = this Mac is BEHIND
the measured NTP source time, so the scheduler must fire EARLIER (negative adjust_ms) to land on
the source's instant.

Run `python3 -m fans_utils.clockcheck` shortly before use; a reading is only valid for the
next few minutes, not hours later. See docs/clock-check.md for the arithmetic.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Callable

DEFAULT_HOSTS = ("time.apple.com", "time.cloudflare.com", "time.google.com")
MAX_REQUESTS = 6  # hard cap on sntp invocations per run
MAX_SAMPLES_PER_HOST = 2
MIN_TIMEOUT_S, MAX_TIMEOUT_S, DEFAULT_TIMEOUT_S = 1, 5, 2
MAX_UNCERTAINTY_S = 1.0  # a +/- of a second or more is useless for ms-level tuning: reject
# A Mac whose clock is a whole minute off is not something a ms-level nudge can fix, and a huge
# finite value (1e308 s) overflows to inf once multiplied by 1000. Reject both with one bound.
MAX_OFFSET_S = 60.0
MARGIN_MS = 5.0  # safety margin added so the suggestion never fires early
RERUN_NOTE = ("Rerun this check shortly before use (minutes, not hours). The offset drifts and "
              "network conditions change; this is NOT a durable correction.")
SCOPE_NOTE = ("Scope: this measures the Mac system clock against public NTP sources only. It does "
              "NOT measure the FANS server clock, iPhone Mirroring input latency, or cellular/network "
              "round-trip time. It is a local clock correction, not an end-to-end timing guarantee.")

# e.g. "+0.054980 +/- 0.117337 time.apple.com 17.253.114.35"
_LINE_RE = re.compile(r"^\s*([+-]?\S+)\s+\+/-\s+(\S+)\s+(\S+)(?:\s+(\S+))?\s*$")

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class Sample:
    host: str
    ok: bool
    offset_s: float | None = None
    uncertainty_s: float | None = None
    server: str | None = None
    raw: str = ""
    error: str | None = None

    @property
    def interval_ms(self) -> tuple[float, float]:
        assert self.ok and self.offset_s is not None and self.uncertainty_s is not None
        return ((self.offset_s - self.uncertainty_s) * 1000.0, (self.offset_s + self.uncertainty_s) * 1000.0)


@dataclass(frozen=True)
class Report:
    captured_at_utc: str
    hosts: list[str]
    samples: list[Sample]
    failures: int
    status: str  # consensus | single-source | inconclusive | no_data
    interval_ms: tuple[float, float] | None
    offset_ms: float | None  # midpoint of interval_ms
    suggested_adjust_ms: int | None
    margin_ms: float = MARGIN_MS
    sign_convention: str = ("offset = remote - local; positive = this Mac is behind the measured "
                            "NTP source time")
    measures: str = "Mac system clock vs public NTP sources"
    does_not_measure: str = "FANS server clock; iPhone Mirroring input latency; cellular/network RTT"
    scope: str = SCOPE_NOTE
    note: str = RERUN_NOTE
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class ClockCheckError(ValueError):
    """Bad arguments (never raised for network failures; those become failed Samples)."""


def sntp_argv(host: str, timeout_s: int) -> list[str]:
    """The ONLY argv shape this module ever executes. Read-only flags, host last."""
    if not host or host.startswith("-") or any(c.isspace() for c in host):
        raise ClockCheckError(f"invalid host {host!r}")
    if not (MIN_TIMEOUT_S <= timeout_s <= MAX_TIMEOUT_S):
        raise ClockCheckError(f"timeout must be {MIN_TIMEOUT_S}..{MAX_TIMEOUT_S} s")
    return ["sntp", "-t", str(int(timeout_s)), "-n", "1", host]


def parse_sntp_line(host: str, text: str) -> Sample:
    """Parse one sntp stdout line into a Sample; anything odd becomes ok=False with a reason."""
    raw = text.strip()
    lines = [ln for ln in raw.splitlines() if "+/-" in ln]
    if not lines:
        return Sample(host, False, raw=raw, error="no offset line in output")
    m = _LINE_RE.match(lines[0])
    if not m:
        return Sample(host, False, raw=raw, error="unparseable offset line")
    try:
        offset, unc = float(m.group(1)), float(m.group(2))
    except ValueError:
        return Sample(host, False, raw=raw, error="non-numeric offset/uncertainty")
    if not (math.isfinite(offset) and math.isfinite(unc)):
        return Sample(host, False, raw=raw, error="non-finite offset/uncertainty")
    if unc < 0:
        return Sample(host, False, raw=raw, error="negative uncertainty")
    if unc >= MAX_UNCERTAINTY_S:
        return Sample(host, False, raw=raw, error=f"uncertainty {unc:g}s >= {MAX_UNCERTAINTY_S:g}s (unbounded)")
    if abs(offset) > MAX_OFFSET_S:
        return Sample(host, False, raw=raw,
                      error=f"offset {offset:g}s exceeds +/-{MAX_OFFSET_S:g}s; fix the system clock, not --adjust-ms")
    return Sample(host, True, offset, unc, m.group(4) or m.group(3), raw)


def take_sample(host: str, timeout_s: int, runner: Runner | None = None) -> Sample:
    argv = sntp_argv(host, timeout_s)
    try:
        proc = (runner or subprocess.run)(argv, capture_output=True, text=True, timeout=timeout_s + 2, check=False)
    except subprocess.TimeoutExpired:
        return Sample(host, False, error=f"timeout after {timeout_s + 2}s")
    except OSError as exc:  # sntp missing (non-macOS) etc.
        return Sample(host, False, error=f"cannot run sntp: {exc}")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        return Sample(host, False, raw=(proc.stdout or "").strip(), error=err)
    return parse_sntp_line(host, proc.stdout or "")


def intersect(intervals: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Intersection of closed intervals, or None if they do not all overlap or are not finite."""
    if not intervals or not all(math.isfinite(x) for i in intervals for x in i):
        return None
    lo = max(i[0] for i in intervals)
    hi = min(i[1] for i in intervals)
    return (lo, hi) if lo <= hi else None


def suggest_adjust_ms(lo_ms: float, margin_ms: float = MARGIN_MS) -> int:
    """Smallest integer adjust that never fires early for any offset >= lo_ms.

    Firing at local nominal + adjust happens, on the measured NTP source's timescale, at
    nominal + adjust + offset. To keep that >= nominal for every offset in the interval we need
    adjust >= -lo. Add the margin and round toward LATER (ceil).
    Example: lo = +75 ms -> ceil(-75 + 5) = -70.
    """
    return math.ceil(-lo_ms + margin_ms)


def measure(hosts: list[str] | tuple[str, ...] = DEFAULT_HOSTS, samples: int = 1,
            timeout_s: int = DEFAULT_TIMEOUT_S, runner: Runner | None = None,
            now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> Report:
    """Take bounded sequential read-only sntp samples and build a Report. Never raises for
    network trouble; raises ClockCheckError for bad arguments."""
    hosts = list(dict.fromkeys(hosts))  # dedupe, keep order
    if not hosts:
        raise ClockCheckError("at least one host is required")
    if not (1 <= samples <= MAX_SAMPLES_PER_HOST):
        raise ClockCheckError(f"samples must be 1..{MAX_SAMPLES_PER_HOST}")
    if len(hosts) * samples > MAX_REQUESTS:
        raise ClockCheckError(f"{len(hosts)} hosts x {samples} samples exceeds {MAX_REQUESTS} requests")
    for h in hosts:
        sntp_argv(h, timeout_s)  # validate up front so nothing runs with a bad argv

    captured = now().isoformat(timespec="seconds")
    results = [take_sample(h, timeout_s, runner) for h in hosts for _ in range(samples)]
    good = [s for s in results if s.ok]
    failures = len(results) - len(good)
    reasons = [f"{s.host}: {s.error}" for s in results if not s.ok]
    distinct_ok_hosts = {s.host for s in good}

    interval = intersect([s.interval_ms for s in good])
    if not good:
        status = "no_data"
    elif interval is None:
        status = "inconclusive"
        reasons.append("source intervals do not overlap; no recommendation")
    elif len(distinct_ok_hosts) >= 2:
        status = "consensus"
    else:
        status = "single-source"
        reasons.append("only one host answered; not a consensus")

    offset_ms = (interval[0] + interval[1]) / 2 if interval else None
    adjust = suggest_adjust_ms(interval[0]) if interval else None
    return Report(captured, hosts, results, failures, status, interval, offset_ms, adjust, reasons=reasons)


def format_text(r: Report) -> str:
    out = [f"measures:         {r.measures}",
           f"does NOT measure: {r.does_not_measure}",
           f"captured (UTC):   {r.captured_at_utc}", f"status:           {r.status}",
           f"samples ok/total: {len(r.samples) - r.failures}/{len(r.samples)}"]
    for s in r.samples:
        if s.ok:
            lo, hi = s.interval_ms
            out.append(f"  {s.host:<22} {s.offset_s * 1000:+.1f} ms +/- {s.uncertainty_s * 1000:.1f}"
                       f"  -> [{lo:+.1f}, {hi:+.1f}]  ({s.server})")
        else:
            out.append(f"  {s.host:<22} FAILED: {s.error}")
    if r.interval_ms:
        lo, hi = r.interval_ms
        out += [f"interval (ms):    [{lo:+.1f}, {hi:+.1f}]  ({r.sign_convention})",
                f"offset_ms:        {r.offset_ms:+.1f}",
                f"suggested:        --adjust-ms {r.suggested_adjust_ms}"
                f"   (= ceil(-{lo:.1f} + {r.margin_ms:g}); never fires early within the interval)"]
    else:
        out.append("suggested:        none (inconclusive or no data)")
    out += [f"note: {x}" for x in r.reasons]
    out.append(f"note: {r.scope}")
    out.append(f"note: {r.note}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m fans_utils.clockcheck",
                                description="Read-only check of the Mac system clock against public "
                                            "NTP sources via macOS sntp. Does NOT measure the FANS "
                                            "server clock, iPhone Mirroring latency, or network RTT.")
    p.add_argument("--host", action="append", help="NTP host (repeatable, max 6 total requests)")
    p.add_argument("--samples", type=int, default=1, help=f"per host, 1..{MAX_SAMPLES_PER_HOST}")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S, help=f"{MIN_TIMEOUT_S}..{MAX_TIMEOUT_S} s")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        report = measure(a.host or DEFAULT_HOSTS, a.samples, a.timeout)
    except ClockCheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_dict(), indent=2) if a.json else format_text(report))
    return 0 if report.suggested_adjust_ms is not None else 1


if __name__ == "__main__":
    sys.exit(main())
