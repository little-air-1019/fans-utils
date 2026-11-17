"""macOS click backend for the iPhone Mirroring window (bundle com.apple.ScreenContinuity).

Standard library only: ctypes over CoreGraphics / CoreFoundation / AppKit (objc runtime).
No helper process, no threads, no queue. A click is one synchronous mouseMoved +
leftMouseDown + leftMouseUp posted to the HID event tap at a calibrated point, guarded
right before posting by: Accessibility permission, frontmost app == expected bundle,
target window still on screen at the calibrated bounds, point inside that window.

Coordinate space: CoreGraphics global *logical points* (origin = top-left of the main
display, y grows downward, Retina scale already divided out). Window bounds from the
window server and the cursor position from CGEvent share this space. Secondary displays
may have negative coordinates. Never mix in NSEvent/NSScreen values (their y is flipped).

CGEventPost returns void: a "posted" click only means the event was handed to the HID event
tap. It proves nothing about the phone receiving a tap or the FANS server accepting an
application.

Entry points:
    python3 -m fans_utils.macos diagnose
    python3 -m fans_utils.macos calibrate --in 10 [--bundle com.apple.ScreenContinuity]
    python3 -m fans_utils.macos check
    python3 -m fans_utils.macos fire --in 5        # preflight, countdown, exactly ONE guarded click
    python3 -m fans_utils.macos benchmark --path PATH --samples 30   # time the click guards; posts nothing
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

DEFAULT_BUNDLE_ID = "com.apple.ScreenContinuity"
DEFAULT_CALIBRATION_PATH = Path(__file__).resolve().parent.parent / "calibration.local.json"
BOUNDS_TOLERANCE_PT = 0.5  # window-server bounds are integral points; anything larger is a move/resize
CALIBRATION_VERSION = 1
MAX_COUNTDOWN_S = 24 * 3600.0  # --in cap for calibrate/fire; the real event uses `schedule`

ACCESSIBILITY_HELP = (
    "Accessibility permission is missing for the app that hosts this terminal (Terminal, iTerm, "
    "Orca, VS Code, ...). Grant it in System Settings > Privacy & Security > Accessibility, then "
    "re-run from the SAME terminal app you will use at signup time. This tool never changes "
    "security settings or prompts on your behalf."
)


class DriverError(RuntimeError):
    """A guard failed. Nothing was clicked."""


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Display:
    id: int
    x: float
    y: float
    width: float
    height: float
    scale: float  # backing pixels per logical point (2.0 on Retina)

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.x + self.width and self.y <= py < self.y + self.height


@dataclass(frozen=True)
class Window:
    number: int
    pid: int
    bundle_id: str | None
    owner: str
    x: float
    y: float
    width: float
    height: float
    layer: int
    on_screen: bool

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.width, self.height)

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.x + self.width and self.y <= py < self.y + self.height


@dataclass(frozen=True)
class Calibration:
    x: float
    y: float
    bundle_id: str
    window_owner: str
    window_bounds: tuple[float, float, float, float]
    display_id: int
    captured_at: str
    window_number: int = 0  # identity at calibration time; may change after a reconnect, so bounds are the guard
    pid: int = 0
    version: int = CALIBRATION_VERSION

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")

    @classmethod
    def load(cls, path: Path) -> "Calibration":
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError as exc:
            raise DriverError(
                f"no calibration at {path}. Run: python3 -m fans_utils.macos calibrate --in 10"
            ) from exc
        except (OSError, ValueError) as exc:
            raise DriverError(f"unreadable calibration {path}: {exc}") from exc
        try:
            if not isinstance(raw, dict):
                raise TypeError("top level must be an object")
            if int(raw.get("version", -1)) != CALIBRATION_VERSION:
                raise ValueError(f"version {raw.get('version')!r} != {CALIBRATION_VERSION}; recalibrate")
            bounds = tuple(float(v) for v in raw["window_bounds"])
            if len(bounds) != 4:
                raise ValueError("window_bounds must have 4 numbers")
            cal = cls(
                x=float(raw["x"]),
                y=float(raw["y"]),
                bundle_id=str(raw["bundle_id"]),
                window_owner=str(raw.get("window_owner", "")),
                window_bounds=bounds,  # type: ignore[arg-type]
                display_id=int(raw["display_id"]),
                captured_at=str(raw.get("captured_at", "")),
                window_number=int(raw.get("window_number", 0)),
                pid=int(raw.get("pid", 0)),
                version=CALIBRATION_VERSION,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DriverError(f"malformed calibration {path}: {exc}") from exc
        if not all(math.isfinite(v) for v in (cal.x, cal.y, *cal.window_bounds)) or not cal.bundle_id:
            raise DriverError(f"malformed calibration {path}: non-finite coordinates or empty bundle_id")
        return cal


def load_calibration(path: Path = DEFAULT_CALIBRATION_PATH) -> Calibration:
    return Calibration.load(Path(path))


def save_calibration(calibration: Calibration, path: Path = DEFAULT_CALIBRATION_PATH) -> None:
    calibration.save(Path(path))


def _bounds_equal(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return len(a) == len(b) == 4 and all(abs(p - q) <= BOUNDS_TOLERANCE_PT for p, q in zip(a, b))


# --------------------------------------------------------------------------- native protocol


class Native(Protocol):
    """Everything that touches the OS. Injected so the driver logic is testable without a Mac."""

    def accessibility_trusted(self) -> bool: ...
    def displays(self) -> list[Display]: ...
    def frontmost_bundle_id(self) -> str | None: ...
    def on_screen_windows(self) -> list[Window]: ...  # front-to-back, layer-0 only
    def window_by_number(self, number: int) -> Window | None: ...
    def cursor_position(self) -> tuple[float, float]: ...
    def warm_up(self) -> None: ...
    def post_mouse(self, kind: str, x: float, y: float) -> float | None: ...
    """kind: moved | down | up. Returns time.monotonic() taken right after CGEventPost returned,
    or None when the backend cannot stamp it (the driver then stamps after the call)."""
    def close(self) -> None: ...


# --------------------------------------------------------------------------- driver


class MacOSClickDriver:
    """ClickDriver for the scheduler. Zero-arg construction (what cli.py does) loads
    calibration.local.json; explicit (x, y) skips the file."""

    name = "macos"
    performs_real_click = True

    def __init__(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        expected_bundle_id: str | None = None,
        calibration_path: Path | None = None,
        native: Native | None = None,
    ) -> None:
        self.calibration_path = calibration_path or DEFAULT_CALIBRATION_PATH
        self.calibration: Calibration | None = None
        if x is None or y is None:
            cal = Calibration.load(self.calibration_path)
            if expected_bundle_id and expected_bundle_id != cal.bundle_id:
                raise DriverError(
                    f"calibration was taken on {cal.bundle_id!r} but the target is {expected_bundle_id!r}; "
                    "recalibrate on the intended window"
                )
            self.calibration = cal
            x, y = cal.x, cal.y
            expected_bundle_id = cal.bundle_id
        for label, value in (("x", x), ("y", y)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise DriverError(f"{label} must be a finite number in logical points, got {value!r}")
        self.x = float(x)
        self.y = float(y)
        self.expected_bundle_id = expected_bundle_id or DEFAULT_BUNDLE_ID
        self._native: Native = native if native is not None else CoreGraphicsNative()
        self._window: Window | None = None  # captured by preflight; guards every click
        self._closed = False
        # Set after every click() that posted; None after a refusal so nobody reads stale numbers.
        # Keys: guard_ms (validate_target wall time), down_posted_monotonic, up_posted_monotonic
        # (time.monotonic seconds right after the corresponding CGEventPost returned).
        self.last_click_metrics: dict[str, float] | None = None

    @property
    def target_window(self) -> Window | None:
        """The window preflight() locked onto; None before preflight."""
        return self._window

    # -- preflight -------------------------------------------------------------

    def preflight(self) -> list[str]:
        """Verify everything checkable before the countdown. Returns warnings (non-fatal).
        Raises PermissionError or DriverError; nothing is clicked in any case."""
        if self._closed:
            raise DriverError("driver is closed")
        n = self._native
        if not n.accessibility_trusted():
            raise PermissionError(ACCESSIBILITY_HELP)
        displays = n.displays()
        if not any(d.contains(self.x, self.y) for d in displays):
            raise DriverError(
                f"point ({self.x:g}, {self.y:g}) is not on any active display: "
                + ", ".join(f"display {d.id} [{d.x:g},{d.y:g} {d.width:g}x{d.height:g} @{d.scale:g}x]" for d in displays)
            )
        windows = [w for w in n.on_screen_windows() if w.bundle_id == self.expected_bundle_id]
        if not windows:
            raise DriverError(
                f"no on-screen window of {self.expected_bundle_id!r}. For iPhone Mirroring: open it, "
                "connect the phone, keep the window visible (not minimized)."
            )
        hit = [w for w in windows if w.contains(self.x, self.y)]
        if not hit:
            raise DriverError(
                f"point ({self.x:g}, {self.y:g}) is outside every {self.expected_bundle_id!r} window "
                f"({[w.bounds for w in windows]}); the window moved/resized or the calibration is stale. Recalibrate."
            )
        window = hit[0]
        if self.calibration and not _bounds_equal(window.bounds, self.calibration.window_bounds):
            raise DriverError(
                f"{self.expected_bundle_id!r} window is at {window.bounds} but calibration recorded "
                f"{self.calibration.window_bounds}: it moved or resized. Recalibrate."
            )
        n.warm_up()
        self._window = window
        warnings: list[str] = []
        # Focus is a warning here (the user has the countdown to fix it) and a hard guard in click().
        # A failed AX read (observed: -25212 NoValue while an Electron app is frontmost) is the same
        # kind of not-yet-ready state, so it is reported, not fatal, at preflight time.
        try:
            front = n.frontmost_bundle_id()
        except DriverError as exc:
            front = None
            warnings.append(f"cannot read the frontmost app yet ({exc}); the click will be refused until it can.")
        if front != self.expected_bundle_id:
            warnings.append(
                f"frontmost app is {front!r}, not {self.expected_bundle_id!r}. Bring the target window to the "
                "front before the countdown ends or the click will be refused."
            )
        return warnings

    # -- click -----------------------------------------------------------------

    def validate_target(self) -> Window:
        """Read-only hot-path check, the exact guards click() runs before posting. Returns the
        live target window; raises DriverError / PermissionError. Posts nothing, so it is safe to
        call any number of times (benchmark, visual guard)."""
        if self._closed:
            raise DriverError("driver is closed")
        if self._window is None:
            raise DriverError("preflight() has not run; refusing to click blind")
        n = self._native
        if not n.accessibility_trusted():
            raise PermissionError("refusing to click: " + ACCESSIBILITY_HELP)
        # One window-server query, front-to-back: the target must still be on screen, and the
        # topmost window at the click point must be the target itself (no occlusion, not even
        # by another window of the same app).
        on_screen = n.on_screen_windows()
        live = next((w for w in on_screen if w.number == self._window.number), None)
        if live is None:
            raise DriverError(
                f"refusing to click: {self.expected_bundle_id!r} window {self._window.number} is gone or "
                "no longer on screen (phone disconnected, window closed, hidden or minimized)"
            )
        top = next((w for w in on_screen if w.contains(self.x, self.y)), None)
        if top is None or top.number != live.number:
            who = f"{top.owner!r} ({top.bundle_id}) window {top.number}" if top else "nothing"
            raise DriverError(
                f"refusing to click: point ({self.x:g}, {self.y:g}) is covered by {who}, not by the target window"
            )
        if not _bounds_equal(live.bounds, self._window.bounds):
            raise DriverError(
                f"refusing to click: target window moved/resized from {self._window.bounds} to {live.bounds}"
            )
        if not live.contains(self.x, self.y):
            raise DriverError(f"refusing to click: point ({self.x:g}, {self.y:g}) left the target window")
        # Focus last: every guard must pass anyway, and this order lets `benchmark` measure the
        # full read path (AX, window list, geometry) from a terminal that is itself frontmost.
        front = n.frontmost_bundle_id()
        if front != self.expected_bundle_id:
            raise DriverError(
                f"refusing to click: frontmost app is {front!r}, expected {self.expected_bundle_id!r}"
            )
        return live

    def click(self) -> None:
        """One guarded press+release. Synchronous: when this returns nothing is pending."""
        self.last_click_metrics = None
        t0 = time.perf_counter()
        self.validate_target()
        guard_ms = (time.perf_counter() - t0) * 1000.0
        n = self._native
        n.post_mouse("moved", self.x, self.y)
        down = up = None
        try:
            down = n.post_mouse("down", self.x, self.y)
            if down is None:
                down = time.monotonic()
        finally:
            # Always release: a stuck button is worse than a lone mouse-up, which is not a click.
            up = n.post_mouse("up", self.x, self.y)
            if up is None:
                up = time.monotonic()
        self.last_click_metrics = {
            "guard_ms": guard_ms,
            "down_posted_monotonic": down,
            "up_posted_monotonic": up,
        }

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._native.close()


# --------------------------------------------------------------------------- ctypes native

_CG = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
_CF = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_APPKIT = "/System/Library/Frameworks/AppKit.framework/AppKit"
_APPSERVICES = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
_OBJC = "/usr/lib/libobjc.A.dylib"

_kCFStringEncodingUTF8 = 0x08000100
_kCFNumberSInt64Type = 4
_kCGEventLeftMouseDown = 1
_kCGEventLeftMouseUp = 2
_kCGEventMouseMoved = 5
_kCGMouseButtonLeft = 0
_kCGHIDEventTap = 0
_kCGEventSourceStateCombinedSessionState = 0
_kCGMouseEventClickState = 1
_kCGWindowListOptionOnScreenOnly = 1 << 0
_kCGWindowListOptionIncludingWindow = 1 << 3
_kCGWindowListExcludeDesktopElements = 1 << 4
_kCGNullWindowID = 0
_kAXErrorAPIDisabled = -25211  # Accessibility not granted to the hosting app
AX_READ_ATTEMPTS = 5  # first AXFocusedApplication read in a fresh process can fail transiently
AX_READ_RETRY_S = 0.05
_MOUSE_TYPES = {"moved": _kCGEventMouseMoved, "down": _kCGEventLeftMouseDown, "up": _kCGEventLeftMouseUp}


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _CGRect(ctypes.Structure):
    _fields_ = [("origin", _CGPoint), ("size", _CGSize)]


class CoreGraphicsNative:
    """Real OS bindings. Only the driver and the CLI construct this."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise DriverError("MacOSClickDriver only runs on macOS")
        c = ctypes
        self.cg = c.CDLL(_CG)
        self.cf = c.CDLL(_CF)
        self.appkit = c.CDLL(_APPKIT)  # loads NSWorkspace/NSRunningApplication classes
        self.apps = c.CDLL(_APPSERVICES)
        self.objc = c.CDLL(_OBJC)
        self._source: int | None = None
        self._pid_bundle: dict[int, str | None] = {}

        cf, cg, apps, objc = self.cf, self.cg, self.apps, self.objc
        cf.CFStringCreateWithCString.restype = c.c_void_p
        cf.CFStringCreateWithCString.argtypes = [c.c_void_p, c.c_char_p, c.c_uint32]
        cf.CFStringGetCString.restype = c.c_bool
        cf.CFStringGetCString.argtypes = [c.c_void_p, c.c_char_p, c.c_long, c.c_uint32]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [c.c_void_p]
        cf.CFArrayGetCount.restype = c.c_long
        cf.CFArrayGetCount.argtypes = [c.c_void_p]
        cf.CFArrayGetValueAtIndex.restype = c.c_void_p
        cf.CFArrayGetValueAtIndex.argtypes = [c.c_void_p, c.c_long]
        cf.CFDictionaryGetValue.restype = c.c_void_p
        cf.CFDictionaryGetValue.argtypes = [c.c_void_p, c.c_void_p]
        cf.CFNumberGetValue.restype = c.c_bool
        cf.CFNumberGetValue.argtypes = [c.c_void_p, c.c_int, c.c_void_p]
        cf.CFBooleanGetValue.restype = c.c_bool
        cf.CFBooleanGetValue.argtypes = [c.c_void_p]
        cg.CGRectMakeWithDictionaryRepresentation.restype = c.c_bool
        cg.CGRectMakeWithDictionaryRepresentation.argtypes = [c.c_void_p, c.POINTER(_CGRect)]
        cg.CGGetActiveDisplayList.restype = c.c_int32
        cg.CGGetActiveDisplayList.argtypes = [c.c_uint32, c.POINTER(c.c_uint32), c.POINTER(c.c_uint32)]
        cg.CGDisplayBounds.restype = _CGRect
        cg.CGDisplayBounds.argtypes = [c.c_uint32]
        cg.CGDisplayPixelsWide.restype = c.c_size_t
        cg.CGDisplayPixelsWide.argtypes = [c.c_uint32]
        cg.CGEventCreate.restype = c.c_void_p
        cg.CGEventCreate.argtypes = [c.c_void_p]
        cg.CGEventGetLocation.restype = _CGPoint
        cg.CGEventGetLocation.argtypes = [c.c_void_p]
        cg.CGEventSourceCreate.restype = c.c_void_p
        cg.CGEventSourceCreate.argtypes = [c.c_int32]
        cg.CGEventCreateMouseEvent.restype = c.c_void_p
        cg.CGEventCreateMouseEvent.argtypes = [c.c_void_p, c.c_uint32, _CGPoint, c.c_uint32]
        cg.CGEventSetIntegerValueField.restype = None
        cg.CGEventSetIntegerValueField.argtypes = [c.c_void_p, c.c_uint32, c.c_int64]
        cg.CGEventPost.restype = None
        cg.CGEventPost.argtypes = [c.c_uint32, c.c_void_p]
        cg.CGWindowListCopyWindowInfo.restype = c.c_void_p
        cg.CGWindowListCopyWindowInfo.argtypes = [c.c_uint32, c.c_uint32]
        apps.AXIsProcessTrusted.restype = c.c_bool
        apps.AXIsProcessTrusted.argtypes = []
        apps.AXUIElementCreateSystemWide.restype = c.c_void_p
        apps.AXUIElementCreateSystemWide.argtypes = []
        apps.AXUIElementCopyAttributeValue.restype = c.c_int32  # AXError, 0 = success
        apps.AXUIElementCopyAttributeValue.argtypes = [c.c_void_p, c.c_void_p, c.POINTER(c.c_void_p)]
        apps.AXUIElementGetPid.restype = c.c_int32
        apps.AXUIElementGetPid.argtypes = [c.c_void_p, c.POINTER(c.c_int32)]
        self._ax_system: int | None = None
        self._ax_focused_app_key = self._cfstr("AXFocusedApplication")
        objc.objc_getClass.restype = c.c_void_p
        objc.objc_getClass.argtypes = [c.c_char_p]
        objc.sel_registerName.restype = c.c_void_p
        objc.sel_registerName.argtypes = [c.c_char_p]
        self._msgsend_addr = c.cast(objc.objc_msgSend, c.c_void_p).value

        self._keys = {name: self._cfstr(name) for name in (
            "kCGWindowNumber", "kCGWindowOwnerPID", "kCGWindowOwnerName", "kCGWindowBounds",
            "kCGWindowLayer", "kCGWindowIsOnscreen",
        )}

    # -- CF / objc helpers -----------------------------------------------------

    def _cfstr(self, text: str) -> int:
        return self.cf.CFStringCreateWithCString(None, text.encode(), _kCFStringEncodingUTF8)

    def _py_str(self, cfstring: int | None) -> str | None:
        if not cfstring:
            return None
        buf = ctypes.create_string_buffer(1024)
        if not self.cf.CFStringGetCString(cfstring, buf, len(buf), _kCFStringEncodingUTF8):
            return None
        return buf.value.decode(errors="replace")

    def _int(self, cfnumber: int | None) -> int | None:
        if not cfnumber:
            return None
        out = ctypes.c_int64()
        return int(out.value) if self.cf.CFNumberGetValue(cfnumber, _kCFNumberSInt64Type, ctypes.byref(out)) else None

    def _msg(self, receiver: int | None, selector: str, *args, restype=ctypes.c_void_p, argtypes=()):
        if not receiver:
            return None
        proto = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)
        return proto(self._msgsend_addr)(receiver, self.objc.sel_registerName(selector.encode()), *args)

    def _bundle_for_pid(self, pid: int) -> str | None:
        if pid not in self._pid_bundle:
            cls = self.objc.objc_getClass(b"NSRunningApplication")
            app = self._msg(cls, "runningApplicationWithProcessIdentifier:", pid, argtypes=(ctypes.c_int,))
            ident = self._msg(app, "bundleIdentifier")
            utf8 = self._msg(ident, "UTF8String", restype=ctypes.c_char_p)
            self._pid_bundle[pid] = utf8.decode() if utf8 else None
        return self._pid_bundle[pid]

    # -- Native protocol -------------------------------------------------------

    def accessibility_trusted(self) -> bool:
        return bool(self.apps.AXIsProcessTrusted())

    def displays(self) -> list[Display]:
        ids = (ctypes.c_uint32 * 16)()
        count = ctypes.c_uint32(0)
        if self.cg.CGGetActiveDisplayList(16, ids, ctypes.byref(count)) != 0:
            raise DriverError("CGGetActiveDisplayList failed")
        out = []
        for i in range(count.value):
            r = self.cg.CGDisplayBounds(ids[i])
            px = self.cg.CGDisplayPixelsWide(ids[i])
            scale = px / r.size.width if r.size.width else 1.0
            out.append(Display(int(ids[i]), r.origin.x, r.origin.y, r.size.width, r.size.height, scale))
        return out

    def frontmost_bundle_id(self) -> str | None:
        """Live read through the Accessibility server: system-wide AXFocusedApplication -> pid.
        NSWorkspace.frontmostApplication is NOT used here: it is refreshed by run-loop
        notifications, and this process has no main run loop, so it returns the value from
        the first call forever (observed 2026-09-22: preflight saw Orca, the user switched to
        iPhone Mirroring, click still saw Orca)."""
        if self._ax_system is None:
            self._ax_system = self.apps.AXUIElementCreateSystemWide()
            if not self._ax_system:
                raise DriverError("AXUIElementCreateSystemWide failed")
        value = ctypes.c_void_p()
        err = 0
        for attempt in range(AX_READ_ATTEMPTS):
            err = self.apps.AXUIElementCopyAttributeValue(self._ax_system, self._ax_focused_app_key, ctypes.byref(value))
            if err == 0 and value.value:
                break
            if err == _kAXErrorAPIDisabled:
                break
            # Observed: the very first read in a fresh process returns -25204 (CannotComplete) while
            # the AX connection comes up; the next read succeeds. Bounded retry, then give up.
            if attempt < AX_READ_ATTEMPTS - 1:
                time.sleep(AX_READ_RETRY_S)
        if err != 0 or not value.value:
            raise DriverError(
                f"cannot read the focused application via Accessibility (AXError {err}). "
                + (ACCESSIBILITY_HELP if err == _kAXErrorAPIDisabled else "Retry; if it persists, re-run diagnose.")
            )
        pid = ctypes.c_int32(0)
        try:
            if self.apps.AXUIElementGetPid(value, ctypes.byref(pid)) != 0 or pid.value <= 0:
                raise DriverError("AXUIElementGetPid failed for the focused application")
        finally:
            self.cf.CFRelease(value)
        return self._bundle_for_pid(pid.value)

    def _parse_windows(self, array: int | None, *, layer0_only: bool) -> list[Window]:
        if not array:
            return []
        out = []
        try:
            for i in range(self.cf.CFArrayGetCount(array)):
                d = self.cf.CFArrayGetValueAtIndex(array, i)
                get = lambda k: self.cf.CFDictionaryGetValue(d, self._keys[k])  # noqa: E731
                layer = self._int(get("kCGWindowLayer")) or 0
                if layer0_only and layer != 0:
                    continue
                rect = _CGRect()
                if not self.cg.CGRectMakeWithDictionaryRepresentation(get("kCGWindowBounds"), ctypes.byref(rect)):
                    continue
                pid = self._int(get("kCGWindowOwnerPID")) or 0
                onscreen_ref = get("kCGWindowIsOnscreen")
                out.append(Window(
                    number=self._int(get("kCGWindowNumber")) or 0,
                    pid=pid,
                    bundle_id=self._bundle_for_pid(pid) if pid else None,
                    owner=self._py_str(get("kCGWindowOwnerName")) or "",
                    x=rect.origin.x, y=rect.origin.y, width=rect.size.width, height=rect.size.height,
                    layer=layer,
                    on_screen=bool(self.cf.CFBooleanGetValue(onscreen_ref)) if onscreen_ref else False,
                ))
        finally:
            self.cf.CFRelease(array)
        return out

    def on_screen_windows(self) -> list[Window]:
        arr = self.cg.CGWindowListCopyWindowInfo(
            _kCGWindowListOptionOnScreenOnly | _kCGWindowListExcludeDesktopElements, _kCGNullWindowID)
        return self._parse_windows(arr, layer0_only=True)

    def window_by_number(self, number: int) -> Window | None:
        arr = self.cg.CGWindowListCopyWindowInfo(_kCGWindowListOptionIncludingWindow, number)
        found = self._parse_windows(arr, layer0_only=False)
        return found[0] if found else None

    def cursor_position(self) -> tuple[float, float]:
        ev = self.cg.CGEventCreate(None)
        if not ev:
            raise DriverError("CGEventCreate failed (no window server session?)")
        try:
            p = self.cg.CGEventGetLocation(ev)
        finally:
            self.cf.CFRelease(ev)
        return (p.x, p.y)

    def warm_up(self) -> None:
        if self._source is None:
            self._source = self.cg.CGEventSourceCreate(_kCGEventSourceStateCombinedSessionState)
            if not self._source:
                raise DriverError("CGEventSourceCreate failed")
        # Fault in the event machinery without posting anything.
        ev = self.cg.CGEventCreateMouseEvent(self._source, _kCGEventMouseMoved, _CGPoint(0.0, 0.0), _kCGMouseButtonLeft)
        if ev:
            self.cf.CFRelease(ev)
        # Bring up the Accessibility connection now so the per-click focus read is warm.
        # Observed 2026-09-22 (macOS 26.4): the system-wide AX read returns -25204 on EVERY attempt
        # until this process has made one window-server call (CGWindowListCopyWindowInfo).
        # preflight() reads the window list before warm_up(), which satisfies that; keep that order.
        try:
            self.frontmost_bundle_id()
        except DriverError:
            pass  # warm-up only; preflight reports the live read as a warning, click() refuses on it

    def post_mouse(self, kind: str, x: float, y: float) -> float | None:
        if self._source is None:
            raise DriverError("warm_up() not called")
        ev = self.cg.CGEventCreateMouseEvent(self._source, _MOUSE_TYPES[kind], _CGPoint(x, y), _kCGMouseButtonLeft)
        if not ev:
            raise DriverError(f"CGEventCreateMouseEvent({kind}) failed")
        try:
            if kind != "moved":
                self.cg.CGEventSetIntegerValueField(ev, _kCGMouseEventClickState, 1)
            self.cg.CGEventPost(_kCGHIDEventTap, ev)
            return time.monotonic()
        finally:
            self.cf.CFRelease(ev)

    def close(self) -> None:
        if self._source:
            self.cf.CFRelease(self._source)
            self._source = None
        if self._ax_system:
            self.cf.CFRelease(self._ax_system)
            self._ax_system = None


# --------------------------------------------------------------------------- module functions


def cursor_position(native: Native | None = None) -> tuple[float, float]:
    return (native or CoreGraphicsNative()).cursor_position()


def diagnose(native: Native | None = None, bundle_id: str = DEFAULT_BUNDLE_ID) -> dict:
    """Read-only snapshot. Posts nothing."""
    n = native or CoreGraphicsNative()
    windows = n.on_screen_windows()
    cx, cy = n.cursor_position()
    under = next((w for w in windows if w.contains(cx, cy)), None)
    trusted = n.accessibility_trusted()
    try:
        frontmost = n.frontmost_bundle_id() if trusted else None
    except DriverError as exc:
        frontmost = f"error: {exc}"
    return {
        "accessibility_trusted": trusted,
        "displays": [asdict(d) for d in n.displays()],
        "frontmost_bundle_id": frontmost,  # live, via AX system-wide focused application
        "cursor": {"x": cx, "y": cy, "window_under_cursor": asdict(under) if under else None},
        "target_bundle_id": bundle_id,
        "target_windows": [asdict(w) for w in windows if w.bundle_id == bundle_id],
    }


def calibrate(
    native: Native,
    *,
    seconds: float = 0.0,
    bundle_id: str = DEFAULT_BUNDLE_ID,
    path: Path = DEFAULT_CALIBRATION_PATH,
    point: tuple[float, float] | None = None,
    sleep=None,  # resolved at call time so tests can patch time.sleep
    out=None,
) -> Calibration:
    """Record a calibration without posting any event. Default: count down, then read the
    cursor. With `point`: validate that explicit global point immediately (no countdown, no
    cursor read) so a screenshot-derived coordinate can be captured read-only."""
    out = out or sys.stdout
    sleep = sleep or time.sleep
    if not native.accessibility_trusted():
        raise PermissionError(ACCESSIBILITY_HELP)
    if point is not None:
        x, y = point
        for label, value in (("x", x), ("y", y)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise DriverError(f"--{label} must be a finite number in global logical points, got {value!r}")
        x, y = float(x), float(y)
        print(f"calibrate: validating explicit point ({x:g}, {y:g}) for {bundle_id!r} (no cursor read, no click)",
              file=out)
    else:
        remaining = float(seconds)
        if not math.isfinite(remaining) or not 0 <= remaining <= MAX_COUNTDOWN_S:
            raise DriverError(f"countdown must be between 0 and {MAX_COUNTDOWN_S:g} seconds, got {seconds!r}")
        print(f"calibrate: move the mouse onto the target button in {bundle_id!r}; reading in {remaining:g}s ...",
              file=out)
        while remaining > 0:
            step = min(1.0, remaining)
            sleep(step)
            remaining -= step
            if remaining > 0:
                print(f"  {remaining:g}s", file=out)
        x, y = native.cursor_position()
    display = next((d for d in native.displays() if d.contains(x, y)), None)
    if display is None:
        raise DriverError(f"point ({x:g}, {y:g}) is not on an active display")
    under = next((w for w in native.on_screen_windows() if w.contains(x, y)), None)
    if under is None:
        raise DriverError(f"no window at point ({x:g}, {y:g})")
    print(f"point: ({x:g}, {y:g}) on display {display.id} @{display.scale:g}x; "
          f"topmost window there: {under.owner!r} ({under.bundle_id}) bounds {under.bounds}", file=out)
    if under.bundle_id != bundle_id:
        raise DriverError(
            f"window at point belongs to {under.bundle_id!r}, expected {bundle_id!r}. "
            f"For local mock validation only, pass --bundle {under.bundle_id}"
        )
    if bundle_id != DEFAULT_BUNDLE_ID:
        print(f"WARNING: calibrating on {bundle_id!r}, not iPhone Mirroring. Mock validation only.", file=out)
    cal = Calibration(
        x=x, y=y, bundle_id=bundle_id, window_owner=under.owner, window_bounds=under.bounds,
        display_id=display.id, captured_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
        window_number=under.number, pid=under.pid,
    )
    cal.save(path)
    print(f"saved {path}", file=out)
    return cal


BENCHMARK_MAX_SAMPLES = 100


def benchmark(driver: MacOSClickDriver, *, samples: int = 30, out=None) -> dict:
    """Time validate_target() `samples` times. Read-only: nothing is posted, ever. A refused
    sample still paid for the whole read path (focus is checked last), so its timing counts."""
    out = out or sys.stdout
    if not 1 <= samples <= BENCHMARK_MAX_SAMPLES:
        raise DriverError(f"--samples must be between 1 and {BENCHMARK_MAX_SAMPLES}, got {samples}")
    print("benchmark: READ-ONLY. Measures the click guards only; no mouse event is posted.", file=out)
    for w in driver.preflight():
        print(f"WARNING: {w}", file=out)
    times: list[float] = []
    outcomes: dict[str, int] = {}
    for _ in range(samples):
        t0 = time.perf_counter()
        try:
            driver.validate_target()
            key = "ok"
        except (DriverError, PermissionError) as exc:
            key = f"refused: {exc}"
        times.append((time.perf_counter() - t0) * 1000.0)
        outcomes[key] = outcomes.get(key, 0) + 1
    s = sorted(times)
    report = {
        "samples": samples,
        "guard_ms": {"p50": s[len(s) // 2], "p95": s[max(0, math.ceil(len(s) * 0.95) - 1)], "max": s[-1], "min": s[0]},
        "outcomes": outcomes,
        "events_posted": 0,
    }
    g = report["guard_ms"]
    print(f"guard ms over {samples} samples: p50={g['p50']:.3f} p95={g['p95']:.3f} max={g['max']:.3f} min={g['min']:.3f}",
          file=out)
    for key, count in outcomes.items():
        print(f"  {count:3d}x {key}", file=out)
    print("events posted: 0", file=out)
    return report


# --------------------------------------------------------------------------- CLI


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m fans_utils.macos", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("diagnose", help="read-only: permission, displays, frontmost app, target windows, cursor")
    d.add_argument("--bundle", default=DEFAULT_BUNDLE_ID)
    for name, help_ in (("calibrate", "countdown, then record the cursor position (no click)"),
                        ("check", "run preflight only"),
                        ("benchmark", "read-only: time the click guards p50/p95/max; posts nothing"),
                        ("fire", "preflight, countdown, then exactly ONE guarded real click")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--bundle", default=None, help=f"target bundle id (default {DEFAULT_BUNDLE_ID}; "
                                                      "other values are for local mock validation only)")
        s.add_argument("--path", type=Path, default=DEFAULT_CALIBRATION_PATH, help="calibration file")
        if name == "benchmark":
            s.add_argument("--samples", type=int, default=30, help=f"1..{BENCHMARK_MAX_SAMPLES} (default 30)")
        elif name != "check":
            s.add_argument("--in", dest="seconds", type=float, default=10.0, metavar="SECONDS",
                           help="countdown before reading the cursor / clicking (default 10)")
        if name == "calibrate":
            s.add_argument("--x", type=float, default=None, metavar="POINT",
                           help="explicit global logical x; with --y skips the countdown and cursor read")
            s.add_argument("--y", type=float, default=None, metavar="POINT", help="explicit global logical y")
    return p


def main(argv: list[str] | None = None, *, native: Native | None = None, out=None) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)
    try:
        if args.command == "diagnose":
            print(json.dumps(diagnose(native, args.bundle), indent=2), file=out)
            return 0
        if args.command == "benchmark":
            driver = MacOSClickDriver(expected_bundle_id=args.bundle, calibration_path=args.path, native=native)
            try:
                benchmark(driver, samples=args.samples, out=out)
            finally:
                driver.close()
            return 0
        if args.command != "check" and not (math.isfinite(args.seconds) and 0 <= args.seconds <= MAX_COUNTDOWN_S):
            print(f"error: --in must be between 0 and {MAX_COUNTDOWN_S:g} seconds", file=out)
            return 2
        if args.command == "calibrate":
            if (args.x is None) != (args.y is None):
                print("error: --x and --y must be given together", file=out)
                return 2
            point = (args.x, args.y) if args.x is not None else None
            calibrate(native or CoreGraphicsNative(), seconds=args.seconds, point=point,
                      bundle_id=args.bundle or DEFAULT_BUNDLE_ID, path=args.path, out=out)
            return 0
        driver = MacOSClickDriver(expected_bundle_id=args.bundle, calibration_path=args.path, native=native)
        try:
            for w in driver.preflight():
                print(f"WARNING: {w}", file=out)
            print(f"preflight ok: ({driver.x:g}, {driver.y:g}) in {driver.expected_bundle_id!r} window "
                  f"{driver._window.bounds if driver._window else None}", file=out)
            if args.command == "check":
                return 0
            from datetime import timedelta
            from .scheduler import ClickPlan, run, system_clock

            clock = system_clock()
            plan = ClickPlan(nominal_start=clock.now() + timedelta(seconds=args.seconds))
            print(f"fire: ONE real click in {args.seconds:g}s at ({driver.x:g}, {driver.y:g}); "
                  f"bring {driver.expected_bundle_id!r} to the front. Ctrl-C to cancel.", file=out)
            out.flush()
            report = run(plan, driver, clock)
            print(f"status: {report.status}" + (f"  ({report.detail})" if report.detail else ""), file=out)
            print(f"clicks posted: {report.clicks}  lateness ms: {[round(v, 3) for v in report.lateness_ms]}", file=out)
            print("note: a posted HID event proves neither phone tap receipt nor FANS application acceptance.",
                  file=out)
            return 0 if report.status == "completed" else 1
        finally:
            driver.close()
    except KeyboardInterrupt:
        print("cancelled by user; nothing posted after this point", file=out)
        return 130
    except PermissionError as exc:
        print(f"error: permission denied. {exc}", file=out)
        return 3
    except DriverError as exc:
        print(f"error: {exc}", file=out)
        return 1


if __name__ == "__main__":
    sys.exit(main())
