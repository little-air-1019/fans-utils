"""Monotonic click scheduler. Waits for an effective target instant, then fires a
bounded number of click attempts through a ClickDriver. Never fires early, never
catches up, never spins.

Two click modes, both bounded by ClickPlan:
- plain (default): fixed count, fixed gap, no feedback.
- guarded retry (opt-in): after every click the ScreenObserver takes a fresh capture; the
  next attempt is fired only while the screen is unchanged. Any change stops the run with
  status "screen_changed" (not a success signal). Any observer failure aborts clicking."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Callable

from .driver import ClickDriver, ScreenObserver

COARSE_TICK_S = 0.5  # wall-clock re-anchor cadence during long waits
FINAL_WINDOW_S = 1.0  # last stretch runs on the monotonic clock only
# Smallest sleep requested in the final phase. Sleeping at least this much per iteration
# guarantees forward progress on any clock (a sub-µs remainder is never spun on) while the
# loop still exits only once monotonic >= deadline: strictly never early, at most ~1 µs late.
MIN_SLEEP_S = 1e-6
# Guarded retry: the baseline capture is taken this long before the target so its cost
# (measured 15-46 ms on the native driver) is absorbed by the final wait instead of delaying
# the first click. The baseline is therefore up to this old when the first click lands; a
# change in that gap is attributed to the first click and stops the run after attempt 1.
ARM_LEAD_S = 0.1

# Guarded retry bounds (spec): at most 5 attempts, within 2000 ms, at least 100 ms apart.
GUARDED_MAX_CLICKS = 5
GUARDED_MAX_DURATION_MS = 2000.0
GUARDED_MIN_INTERVAL_MS = 100.0
GUARDED_DEFAULTS = {"max_clicks": 3, "interval_ms": 150.0, "max_duration_ms": 800.0}
PLAIN_DEFAULTS = {"max_clicks": 1, "interval_ms": 250.0, "max_duration_ms": 5000.0}


class PlanError(ValueError):
    """Raised for an invalid ClickPlan."""


@dataclass(frozen=True)
class Clock:
    """Injectable time source. `now` must return an aware datetime."""

    now: Callable[[], datetime]
    monotonic: Callable[[], float]
    sleep: Callable[[float], None]


def system_clock() -> Clock:
    return Clock(now=lambda: datetime.now(UTC), monotonic=time.monotonic, sleep=time.sleep)


@dataclass(frozen=True)
class ClickPlan:
    nominal_start: datetime  # aware
    adjust_ms: float = 0.0  # sign convention: negative = earlier than nominal, positive = later
    max_clicks: int = 1
    interval_ms: float = 250.0
    max_duration_ms: float = 5000.0  # measured from effective target
    guarded_retry: bool = False  # opt-in; run() then requires a ScreenObserver

    def __post_init__(self) -> None:
        if self.nominal_start.tzinfo is None or self.nominal_start.utcoffset() is None:
            raise PlanError("nominal_start must be timezone-aware")
        for name in ("adjust_ms", "interval_ms", "max_duration_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise PlanError(f"{name} must be a finite number, got {value!r}")
        if isinstance(self.max_clicks, bool) or not isinstance(self.max_clicks, int) or self.max_clicks < 1:
            raise PlanError(f"max_clicks must be an integer >= 1, got {self.max_clicks!r}")
        if self.interval_ms < 0:
            raise PlanError("interval_ms must be >= 0")
        if self.interval_ms == 0 and self.max_clicks > 1:
            raise PlanError("interval_ms must be > 0 when max_clicks > 1 (a zero interval would spin)")
        if self.max_duration_ms <= 0:
            raise PlanError("max_duration_ms must be > 0")
        if not isinstance(self.guarded_retry, bool):
            raise PlanError(f"guarded_retry must be a bool, got {self.guarded_retry!r}")
        if self.guarded_retry:
            if self.max_clicks > GUARDED_MAX_CLICKS:
                raise PlanError(f"guarded retry allows at most {GUARDED_MAX_CLICKS} attempts, got {self.max_clicks}")
            if self.max_duration_ms > GUARDED_MAX_DURATION_MS:
                raise PlanError(f"guarded retry window must be <= {GUARDED_MAX_DURATION_MS:g} ms, "
                                f"got {self.max_duration_ms:g}")
            if self.interval_ms < GUARDED_MIN_INTERVAL_MS:
                raise PlanError(f"guarded retry gap must be >= {GUARDED_MIN_INTERVAL_MS:g} ms, "
                                f"got {self.interval_ms:g}")
        try:
            self.nominal_start + timedelta(milliseconds=self.adjust_ms)
        except (OverflowError, ValueError) as exc:
            raise PlanError(f"adjust_ms {self.adjust_ms!r} puts the target out of range") from exc

    @property
    def effective_target(self) -> datetime:
        return self.nominal_start + timedelta(milliseconds=self.adjust_ms)


@dataclass
class RunReport:
    status: str  # completed | screen_changed | cancelled | stale | error
    driver: str
    real_click: bool
    nominal_start: datetime
    effective_target: datetime
    clicks: int = 0
    # Per click: scheduler dispatch instant minus effective target, in ms. This is when the
    # scheduler handed the click to the driver, not when the OS/device/network delivered it.
    lateness_ms: list[float] = field(default_factory=list)
    # Per click, from the driver's optional last_click_metrics (None when not reported):
    # time spent in the driver's pre-click guards, and the instants the mouse-down / mouse-up
    # HID events were posted, minus the effective target. Still not phone or network arrival.
    guard_ms: list[float | None] = field(default_factory=list)
    down_lateness_ms: list[float | None] = field(default_factory=list)
    up_lateness_ms: list[float | None] = field(default_factory=list)
    screen_checks: int = 0  # guarded retry: fresh captures compared against the baseline
    baseline_age_ms: float | None = None  # guarded retry: first-click dispatch minus arm() start
    detail: str = ""

    @property
    def dry_run(self) -> bool:
        return not self.real_click


def _record_metrics(report: RunReport, driver: ClickDriver, deadline: float) -> None:
    metrics = getattr(driver, "last_click_metrics", None)
    metrics = metrics if isinstance(metrics, dict) else {}

    def rel(key: str) -> float | None:
        value = metrics.get(key)
        return (value - deadline) * 1000.0 if isinstance(value, (int, float)) else None

    guard = metrics.get("guard_ms")
    report.guard_ms.append(float(guard) if isinstance(guard, (int, float)) else None)
    report.down_lateness_ms.append(rel("down_posted_monotonic"))
    report.up_lateness_ms.append(rel("up_posted_monotonic"))


def run(
    plan: ClickPlan,
    driver: ClickDriver,
    clock: Clock,
    on_tick: Callable[[float], None] | None = None,
    observer: ScreenObserver | None = None,
) -> RunReport:
    """Wait for plan.effective_target, then click. on_tick(remaining_s) is called once per
    coarse tick while waiting (at most every COARSE_TICK_S); callers throttle their output.
    With plan.guarded_retry an observer is mandatory: baseline before the first click, a fresh
    capture after every click, and no further click once the screen changed."""
    if plan.guarded_retry and observer is None:
        raise PlanError("guarded_retry requires a ScreenObserver; refusing to retry blind")
    guard = observer if plan.guarded_retry else None
    target = plan.effective_target
    report = RunReport(
        status="completed",
        driver=driver.name,
        real_click=driver.performs_real_click,
        nominal_start=plan.nominal_start,
        effective_target=target,
    )
    remaining = (target - clock.now()).total_seconds()
    if remaining < 0:
        report.status = "stale"
        report.detail = f"effective target passed {-remaining:.3f}s before start"
        return report

    stage = ""  # names the observer call that raised; driver errors keep their bare message
    try:
        # Coarse phase: re-anchor to the wall clock each tick so NTP steps during a
        # long wait cannot move the target. Each sleep is <= remaining, so we never
        # overshoot the target here; undersleep just costs another tick.
        while remaining > FINAL_WINDOW_S:
            if on_tick:
                on_tick(remaining)
            clock.sleep(min(remaining, COARSE_TICK_S))
            remaining = (target - clock.now()).total_seconds()
        # Final phase: monotonic deadline, immune to wall-clock steps. Re-check after
        # every sleep so an undersleep can never fire early.
        deadline = clock.monotonic() + remaining
        armed_at: float | None = None

        def arm() -> None:
            nonlocal armed_at, stage
            stage = "screen baseline"
            armed_at = clock.monotonic()
            guard.arm()  # baseline capture; its cost is absorbed by the remaining wait
            stage = ""

        while (left := deadline - clock.monotonic()) > 0:
            if guard and armed_at is None:
                if left > ARM_LEAD_S:
                    # Wake at the arm point, not at the deadline. The MIN_SLEEP_S floor keeps
                    # a sub-µs remainder from spinning (the wait is still never early: the
                    # arm point is ARM_LEAD_S before the deadline).
                    clock.sleep(max(left - ARM_LEAD_S, MIN_SLEEP_S))
                else:
                    arm()
                continue
            clock.sleep(max(left, MIN_SLEEP_S))
        if guard and armed_at is None:
            arm()

        for i in range(plan.max_clicks):
            now = clock.monotonic()
            lateness = (now - deadline) * 1000.0
            if lateness > plan.max_duration_ms:
                report.detail = f"duration cap {plan.max_duration_ms:g} ms reached"
                break
            if i == 0 and armed_at is not None:
                report.baseline_age_ms = (now - armed_at) * 1000.0
            driver.click()
            report.clicks += 1
            report.lateness_ms.append(lateness)
            _record_metrics(report, driver, deadline)
            if guard is None and i == plan.max_clicks - 1:
                break
            # Fixed gap after each click (no catch-up burst if the click itself was slow),
            # never sleeping past the duration cap. Guarded mode also looks after the last
            # click so the report can say whether anything visibly happened.
            elapsed_ms = (clock.monotonic() - deadline) * 1000.0
            if elapsed_ms + plan.interval_ms > plan.max_duration_ms:
                report.detail = f"duration cap {plan.max_duration_ms:g} ms reached"
                break
            clock.sleep(plan.interval_ms / 1000.0)
            if guard is not None:
                stage = "screen check"
                changed = guard.changed()
                stage = ""
                report.screen_checks += 1
                if changed:
                    report.status = "screen_changed"
                    report.detail = (f"screen_changed after attempt {report.clicks} of {plan.max_clicks}; "
                                     "not proof of acceptance, confirm in app")
                    break
        else:
            if guard is not None:
                report.detail = f"screen unchanged after {report.clicks} attempt(s); confirm in app"
    except KeyboardInterrupt:
        report.status = "cancelled"
        report.detail = "interrupted by user"
    except Exception as exc:  # driver or observer failure: report, do not retry
        report.status = "error"
        prefix = f"{stage} failed: " if stage else ""
        report.detail = f"{prefix}{type(exc).__name__}: {exc}"
    return report
