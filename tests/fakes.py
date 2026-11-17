"""Deterministic clock and driver fakes shared by the tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Callable

from fans_utils.driver import MockClickDriver
from fans_utils.scheduler import Clock

T0 = datetime(2026, 9, 22, 9, 0, 0, tzinfo=UTC)  # one hour before the NMIXX signup start


class FakeClock:
    """Wall + monotonic clock that only moves when sleep() is called.

    sleep_factor < 1 simulates undersleep (spurious wakeups), > 1 oversleep. With
    factor_for_first=N only the first N sleeps are distorted; later ones are exact, so an
    undersleep scenario is finite (an asymptotic 0.5x undersleep would never reach the
    deadline at float precision, which no real clock does).
    on_sleep(seconds) is a hook that may mutate the clock or raise (e.g. KeyboardInterrupt).
    """

    def __init__(
        self,
        start: datetime = T0,
        *,
        mono: float = 1000.0,
        sleep_factor: float = 1.0,
        factor_for_first: int | None = None,
    ) -> None:
        self.wall = start
        self.mono = mono
        self.sleep_factor = sleep_factor
        self.factor_for_first = factor_for_first
        self.sleeps: list[float] = []
        self.on_sleep: Callable[[float], None] | None = None

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0, f"negative sleep {seconds}"
        self.sleeps.append(seconds)
        if self.on_sleep:
            self.on_sleep(seconds)
        distorted = self.factor_for_first is None or len(self.sleeps) <= self.factor_for_first
        self.advance(seconds * (self.sleep_factor if distorted else 1.0))

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)

    def clock(self) -> Clock:
        return Clock(now=self.now, monotonic=self.monotonic, sleep=self.sleep)


class TimestampDriver(MockClickDriver):
    """Records the fake monotonic and wall time of every click; optionally slow or failing.

    With metrics=True it behaves like the native driver's optional contract: `latency_s` is
    spent in pre-click guards, then mouse-down and mouse-up are "posted" 1 ms apart and
    `last_click_metrics` is set (guard_ms, down_posted_monotonic, up_posted_monotonic)."""

    def __init__(self, fake: FakeClock, *, fail_on: int | None = None, latency_s: float = 0.0,
                 metrics: bool = False) -> None:
        super().__init__()
        self.fake = fake
        self.fail_on = fail_on
        self.latency_s = latency_s
        self.metrics = metrics
        self.mono_times: list[float] = []
        self.wall_times: list[datetime] = []

    def click(self) -> None:
        super().click()
        self.mono_times.append(self.fake.mono)
        self.wall_times.append(self.fake.wall)
        if self.metrics:
            self.fake.advance(self.latency_s)
            down = self.fake.mono
            self.fake.advance(0.001)
            self.last_click_metrics = {"guard_ms": self.latency_s * 1000.0, "down_posted_monotonic": down,
                                       "up_posted_monotonic": self.fake.mono}
        else:
            self.fake.advance(self.latency_s)
        if self.fail_on is not None and self.clicks == self.fail_on:
            raise RuntimeError("device gone")


class FakeObserver:
    """Scripted ScreenObserver. `results[i]` is what the (i+1)th changed() returns; past the
    end it returns False. Each capture (arm or changed) costs `capture_s` of fake time.
    `fail_on_check` / `fail_on_arm` raise instead (an exception instance)."""

    def __init__(self, fake: FakeClock, results: list[bool] = (), *, capture_s: float = 0.0,
                 fail_on_check: int | None = None, fail_on_arm: Exception | None = None,
                 change_at_mono: float | None = None) -> None:
        self.fake = fake
        self.results = list(results)
        self.capture_s = capture_s
        self.fail_on_check = fail_on_check
        self.fail_on_arm = fail_on_arm
        self.change_at_mono = change_at_mono  # the screen "changes" at this fake instant
        self.prepares = 0
        self.closes = 0
        self.arm_mono: float | None = None
        self.check_monos: list[float] = []

    def prepare(self) -> None:
        self.prepares += 1

    def arm(self) -> None:
        assert self.arm_mono is None, "arm() must be called exactly once"
        if self.fail_on_arm:
            raise self.fail_on_arm
        self.arm_mono = self.fake.mono
        self.fake.advance(self.capture_s)

    def changed(self) -> bool:
        assert self.arm_mono is not None, "changed() before arm()"
        captured = self.fake.mono
        self.check_monos.append(captured)
        self.fake.advance(self.capture_s)
        n = len(self.check_monos)
        if self.fail_on_check == n:
            raise OSError("screen capture failed")
        if self.change_at_mono is not None:
            return captured >= self.change_at_mono
        return self.results[n - 1] if n <= len(self.results) else False

    def close(self) -> None:
        self.closes += 1
