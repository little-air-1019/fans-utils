"""Click backends. The scheduler only knows the ClickDriver protocol.

Real backends are imported lazily by name so the core imports and tests run on any OS.
"""

from __future__ import annotations

import importlib
from typing import Protocol


class DriverUnavailable(RuntimeError):
    """The requested real driver is unknown or cannot be imported on this machine."""


class ClickDriver(Protocol):
    name: str
    performs_real_click: bool

    def preflight(self) -> list[str]:
        """Verify everything checkable before the countdown. Returns non-fatal warnings.
        Raises (PermissionError or a driver error) when a click would be refused."""

    def click(self) -> None:
        """Perform one click attempt. Raise on failure; the scheduler does not retry."""

    def close(self) -> None:
        """Release native resources. Idempotent; called on every exit path."""


class ScreenObserver(Protocol):
    """Guarded retry feedback: is the watched screen still what it was right before the
    first click? Real implementations capture pixels; nothing here infers acceptance."""

    def prepare(self) -> None:
        """Verify capture works (permission, geometry) before the countdown. Raise to abort;
        nothing has been clicked yet."""

    def arm(self) -> None:
        """Take the baseline capture. Called by the scheduler just before the first click."""

    def changed(self) -> bool:
        """Fresh capture compared with the baseline. True on any visible difference. Raise on
        capture failure; the scheduler then stops clicking."""

    def close(self) -> None:
        """Release native resources. Idempotent; called on every exit path."""


class MockClickDriver:
    """Records click attempts without touching any device. Used for dry-run and tests."""

    name = "mock"
    performs_real_click = False

    def __init__(self) -> None:
        self.clicks = 0

    def preflight(self) -> list[str]:
        return []

    def click(self) -> None:
        self.clicks += 1

    def close(self) -> None:
        pass


class MockScreenObserver:
    """Dry-run observer: no capture. Reports a change on the Nth check (1-based) when
    `changes_at_check` is set, otherwise never. Records every call for tests."""

    name = "mock"

    def __init__(self, changes_at_check: int | None = None) -> None:
        self.changes_at_check = changes_at_check
        self.prepares = 0
        self.arms = 0
        self.checks = 0
        self.closes = 0

    def prepare(self) -> None:
        self.prepares += 1

    def arm(self) -> None:
        self.arms += 1

    def changed(self) -> bool:
        self.checks += 1
        return self.checks == self.changes_at_check

    def close(self) -> None:
        self.closes += 1


# name -> (module, class). Imported only when --live asks for it.
REAL_DRIVER_IMPORTS: dict[str, tuple[str, str]] = {
    "macos": ("fans_utils.macos", "MacOSClickDriver"),
}
# Constructor kwargs the CLI always passes for `schedule --live`. For macos this pins the target
# app to iPhone Mirroring, so a calibration taken on a browser (mock validation) is refused
# instead of silently becoming the signup target. `python3 -m fans_utils.macos fire` keeps its
# own --bundle override for harmless mock rehearsals.
REAL_DRIVER_LIVE_KWARGS: dict[str, dict[str, object]] = {
    "macos": {"expected_bundle_id": "com.apple.ScreenContinuity"},
}
# Resolved classes (and a hook for tests to register stubs without importing native code).
REAL_DRIVERS: dict[str, type] = {}
# Screen observers for --live --guarded-retry, keyed by driver name. Constructed as
# cls(driver) with the already-built real driver; imported only when asked for.
REAL_OBSERVER_IMPORTS: dict[str, tuple[str, str]] = {
    "macos": ("fans_utils.visual", "MacOSVisualGuard"),
}
REAL_OBSERVERS: dict[str, type] = {}


def _resolve(name: str, imports: dict[str, tuple[str, str]], cache: dict[str, type], kind: str) -> type:
    if name in cache:
        return cache[name]
    if name not in imports:
        raise DriverUnavailable(f"unknown {kind} {name!r}; available: {sorted(imports)}")
    module_name, attr = imports[name]
    try:
        cls = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise DriverUnavailable(f"{kind} {name!r} is not usable here ({module_name}: {exc})") from exc
    cache[name] = cls
    return cls


def resolve_real_driver(name: str) -> type:
    return _resolve(name, REAL_DRIVER_IMPORTS, REAL_DRIVERS, "driver")


def resolve_real_observer(name: str) -> type:
    return _resolve(name, REAL_OBSERVER_IMPORTS, REAL_OBSERVERS, "screen observer")
