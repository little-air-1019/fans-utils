"""python3 -m fans_utils  —  list / show / schedule (dry-run by default)."""

from __future__ import annotations

import argparse
import contextlib
import math
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import __version__
from .driver import (
    REAL_DRIVER_IMPORTS,
    REAL_DRIVER_LIVE_KWARGS,
    ClickDriver,
    DriverUnavailable,
    MockClickDriver,
    MockScreenObserver,
    ScreenObserver,
    resolve_real_driver,
    resolve_real_observer,
)
from .profile import EventProfile, ProfileError, load_profile
from .scheduler import (
    GUARDED_DEFAULTS,
    PLAIN_DEFAULTS,
    Clock,
    ClickPlan,
    PlanError,
    RunReport,
    run,
    system_clock,
)

EXIT_OK = 0
EXIT_PROFILE_OR_PLAN_ERROR = 1  # also: the driver raised during clicking (report status "error")
EXIT_USAGE = 2
EXIT_STALE = 3
EXIT_NO_REAL_DRIVER = 4  # --live driver unknown or not importable on this machine
EXIT_DRIVER_SETUP = 5  # real driver construction (e.g. calibration) or preflight failed; nothing clicked
EXIT_SCREEN_CHANGED = 6  # guarded retry stopped because the watched screen changed; NOT a success
EXIT_CANCELLED = 130

DEFAULT_LIVE_DRIVER = "macos"
TAIPEI = ZoneInfo("Asia/Taipei")
# Profile lookup order: packaged data (wheel install) -> <repo>/profiles next to the package
# (checkout, independent of cwd) -> ./profiles. --profiles-dir overrides all three.
_PACKAGED_PROFILES = Path(__file__).resolve().parent / "profiles"
_SIBLING_PROFILES = Path(__file__).resolve().parent.parent / "profiles"
DEFAULT_PROFILES_DIR = next((p for p in (_PACKAGED_PROFILES, _SIBLING_PROFILES) if p.is_dir()), Path("profiles"))
COUNTDOWN_FAR_S = 60.0  # print a countdown line this often while > COUNTDOWN_NEAR_THRESHOLD_S remain
COUNTDOWN_NEAR_S = 10.0
COUNTDOWN_NEAR_THRESHOLD_S = 300.0
ACCEPTANCE_NOTE = (
    "note: counts are local click attempts only; FANS application acceptance "
    "and server ranking are not observable from this tool."
)
LIVE_NOTE = (
    "live: a count here means a synthetic mouse event was posted; delivery is unverified. It does not prove "
    "the phone registered a tap or that the application was submitted or accepted. Confirm in the app."
)
SCREEN_CHANGED_NOTE = (
    "screen_changed: the watched pixels differ from the baseline, so clicking stopped. A change is not "
    "acceptance: it can be a loading state, a modal, an error or an unrelated redraw. Confirm in the app."
)
GUARDED_NOTE = (
    "guarded retry: extra attempts are fired only while the watched screen is unchanged, and the baseline "
    "is captured shortly BEFORE the first click. A change that happens in that gap (for example the Apply "
    "button enabling exactly at the target instant) stops the run after attempt 1."
)


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"must be a finite number, got {text!r}")
    return value


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be an integer, got {text!r}") from exc
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {text!r}")
    return value


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m fans_utils", description=__doc__)
    p.add_argument("--version", action="version", version=f"fans-utils {__version__}")
    p.add_argument("--profiles-dir", type=Path, default=DEFAULT_PROFILES_DIR,
                   help=f"directory of profile JSON files (default: {DEFAULT_PROFILES_DIR})")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list profiles in the profiles dir")

    show = sub.add_parser("show", help="show one profile with times in event tz, Taipei and UTC")
    show.add_argument("profile", help="path to a profile JSON, or a profile id in the profiles dir")

    sched = sub.add_parser("schedule", help="wait for the effective target, then click (dry-run by default)")
    sched.add_argument("profile")
    sched.add_argument("--live", action="store_true",
                       help="use a real ClickDriver: preflight, wait, click for real. Default is dry-run.")
    sched.add_argument("--driver", default=DEFAULT_LIVE_DRIVER,
                       help=f"real driver for --live (default {DEFAULT_LIVE_DRIVER}; known: {sorted(REAL_DRIVER_IMPORTS)})")
    sched.add_argument("--calibration", type=Path, default=None, metavar="PATH",
                       help="calibration file for the real driver (--live only; default is the driver's own, "
                            "<repo>/calibration.local.json for macos)")
    sched.add_argument("--adjust-ms", type=_finite_float, default=0.0,
                       help="shift the target by this many ms: negative = earlier than the profile "
                            "start, positive = later. default 0")
    _add_click_bound_args(sched)
    sched.add_argument("--rehearse-in", type=_finite_float, default=None, metavar="SECONDS",
                       help="REHEARSAL ONLY (dry-run): ignore the profile's start and target now+SECONDS. "
                            "For a harmless real-click rehearsal use the 'rehearse' command.")

    reh = sub.add_parser("rehearse", help="harmless real-click rehearsal against a target you calibrated "
                                          "yourself (mock page or a non-submitting screen). No profile.")
    when = reh.add_mutually_exclusive_group(required=True)
    when.add_argument("--in", dest="seconds", type=_finite_float, metavar="SECONDS",
                      help="click this many seconds from now")
    when.add_argument("--at", dest="at", default=None, metavar="ISO",
                      help="click at this instant, e.g. 2026-09-22T18:00:00+08:00 (must carry a UTC offset)")
    reh.add_argument("--calibration", type=Path, required=True, metavar="PATH",
                     help="REQUIRED: calibration of the harmless target. There is deliberately no default, "
                          "so the real signup calibration is never used by accident.")
    reh.add_argument("--driver", default=DEFAULT_LIVE_DRIVER,
                     help=f"real driver (default {DEFAULT_LIVE_DRIVER})")
    reh.add_argument("--bundle", default=None, metavar="ID",
                     help="expected bundle id of the harmless target (default: whatever the calibration "
                          "file recorded). The signup target pin is NOT applied here.")
    reh.add_argument("--adjust-ms", type=_finite_float, default=0.0, help="shift the target by this many ms")
    _add_click_bound_args(reh)
    return p


def _add_click_bound_args(sub: argparse.ArgumentParser) -> None:
    """Click bounds shared by schedule and rehearse. Defaults stay None so the guarded and
    plain defaults can be told apart from an explicit value."""
    sub.add_argument("--guarded-retry", action="store_true",
                     help="OPT-IN: after each click compare a fresh capture of the target window with a "
                          "baseline taken just before the first click, and fire the next attempt only while "
                          "it is unchanged. Any change stops the run (status screen_changed, exit 6) and is "
                          "NOT proof the application was accepted. Off by default.")
    sub.add_argument("--max-clicks", type=_positive_int, default=None,
                     help=f"default {PLAIN_DEFAULTS['max_clicks']}, or {GUARDED_DEFAULTS['max_clicks']} with "
                          "--guarded-retry")
    sub.add_argument("--interval-ms", type=_finite_float, default=None,
                     help=f"between clicks. default {PLAIN_DEFAULTS['interval_ms']:g}, or "
                          f"{GUARDED_DEFAULTS['interval_ms']:g} with --guarded-retry")
    sub.add_argument("--max-duration-ms", type=_finite_float, default=None,
                     help=f"stop clicking this long after the effective target. default "
                          f"{PLAIN_DEFAULTS['max_duration_ms']:g}, or "
                          f"{GUARDED_DEFAULTS['max_duration_ms']:g} with --guarded-retry")


def _resolve(profile_arg: str, profiles_dir: Path) -> Path:
    direct = Path(profile_arg)
    if direct.is_file():
        return direct
    return profiles_dir / f"{profile_arg}.json"


def _fmt(dt: datetime, zone: ZoneInfo | None = None) -> str:
    return (dt.astimezone(zone) if zone else dt).isoformat(timespec="milliseconds")


def _fmt_instant(dt: datetime, profile: EventProfile) -> str:
    ev = ZoneInfo(profile.timezone)
    return f"{_fmt(dt, ev)}  |  Taipei {_fmt(dt, TAIPEI)}  |  UTC {_fmt(dt, ZoneInfo('UTC'))}"


def cmd_list(profiles_dir: Path, clock: Clock, out) -> int:
    paths = sorted(profiles_dir.glob("*.json"))
    if not paths:
        print(f"no profiles in {profiles_dir}", file=out)
        return EXIT_OK
    now = clock.now()
    for path in paths:
        try:
            prof = load_profile(path)
        except ProfileError as exc:
            print(f"INVALID  {path.name}: {exc}", file=out)
            continue
        state = "stale" if prof.signup_start < now else "upcoming"
        print(f"{prof.id:24} {state:9} start {_fmt(prof.signup_start, TAIPEI)} Taipei  {prof.url}", file=out)
    return EXIT_OK


def cmd_show(path: Path, clock: Clock, out) -> int:
    prof = load_profile(path)
    now = clock.now()
    delta = (prof.signup_start - now).total_seconds()
    when = f"starts in {delta:.1f}s" if delta >= 0 else f"STALE: started {-delta:.1f}s ago"
    print(f"id:           {prof.id}", file=out)
    print(f"name:         {prof.name}", file=out)
    print(f"url:          {prof.url}", file=out)
    print(f"timezone:     {prof.timezone}", file=out)
    print(f"signup_start: {_fmt_instant(prof.signup_start, prof)}", file=out)
    print(f"signup_end:   {_fmt_instant(prof.signup_end, prof)}", file=out)
    print(f"capacity:     {prof.capacity} (+{prof.alternates} alternates)", file=out)
    print(f"channel:      {prof.channel}", file=out)
    if prof.notes:
        print(f"notes:        {prof.notes}", file=out)
    print(f"now:          {_fmt(now, TAIPEI)} Taipei  ->  {when}", file=out)
    return EXIT_OK


def _build_plan(args: argparse.Namespace, nominal: datetime) -> ClickPlan:
    """Resolve the click bounds: guarded and plain modes have different defaults, but an
    explicit flag always wins (and is then validated by ClickPlan)."""
    defaults = GUARDED_DEFAULTS if args.guarded_retry else PLAIN_DEFAULTS
    chosen = {name: getattr(args, name) if getattr(args, name) is not None else defaults[name]
              for name in ("max_clicks", "interval_ms", "max_duration_ms")}
    return ClickPlan(nominal_start=nominal, adjust_ms=args.adjust_ms, guarded_retry=args.guarded_retry, **chosen)


def _keep_awake(out) -> contextlib.AbstractContextManager:
    """Ask macOS not to idle-sleep while a live run waits. `caffeinate -di -w <pid>` exits on
    its own if we die, so a crash cannot leave sleep disabled. Tests patch this whole hook."""
    caffeinate = shutil.which("caffeinate")
    if caffeinate is None:
        return contextlib.nullcontext()

    @contextlib.contextmanager
    def assertion():
        try:
            proc = subprocess.Popen([caffeinate, "-di", "-w", str(os.getpid())],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:  # never fail a run over keep-awake
            print(f"WARNING:          could not keep the Mac awake ({exc}); disable sleep yourself", file=out)
            yield
            return
        print("keep-awake:       on (idle sleep held off for this run only)", file=out)
        try:
            yield
        finally:
            proc.terminate()

    return assertion()


def _make_real_observer(args: argparse.Namespace, driver: ClickDriver, out) -> tuple[ScreenObserver | None, int]:
    """Construct the real screen observer for --guarded-retry. Only reached in live mode."""
    try:
        cls = resolve_real_observer(args.driver)
    except DriverUnavailable as exc:
        print(f"error: {exc}", file=out)
        return None, EXIT_NO_REAL_DRIVER
    observer = None
    try:
        observer = cls(driver)
        observer.prepare()
    except KeyboardInterrupt:
        _close_quietly(observer)
        print("status:           cancelled  (during screen observer setup; nothing clicked)", file=out)
        return None, EXIT_CANCELLED
    except PermissionError as exc:
        _close_quietly(observer)
        print(f"error: screen capture permission missing: {exc}", file=out)
        return None, EXIT_DRIVER_SETUP
    except Exception as exc:
        _close_quietly(observer)
        print(f"error: screen observer setup failed: {exc}", file=out)
        return None, EXIT_DRIVER_SETUP
    return observer, EXIT_OK


def _close_quietly(observer: ScreenObserver | None) -> None:
    """Release a half-built observer. A failure here must not mask the original error."""
    if observer is None:
        return
    try:
        observer.close()
    except Exception:
        pass


def _make_real_driver(args: argparse.Namespace, out, *, pin_target: bool = True) -> tuple[ClickDriver | None, int]:
    """Construct the real driver (may load calibration / native libs). On failure prints the
    reason and returns (None, exit_code).

    pin_target=True (schedule --live) forces the signup app, so a calibration taken on a
    browser is refused. rehearse passes False and its own --bundle: the whole point there is
    a harmless target the user picked."""
    try:
        cls = resolve_real_driver(args.driver)
    except DriverUnavailable as exc:
        print(f"error: {exc}", file=out)
        return None, EXIT_NO_REAL_DRIVER
    kwargs: dict[str, object] = dict(REAL_DRIVER_LIVE_KWARGS.get(args.driver, {})) if pin_target else {}
    if not pin_target and getattr(args, "bundle", None):
        kwargs["expected_bundle_id"] = args.bundle
    if args.calibration is not None:
        kwargs["calibration_path"] = args.calibration
    try:
        return cls(**kwargs), EXIT_OK
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # calibration missing/stale, native init failure, ...
        print(f"error: driver {args.driver!r} setup failed: {exc}", file=out)
        return None, EXIT_DRIVER_SETUP


def cmd_schedule(args: argparse.Namespace, path: Path, clock: Clock, out) -> int:
    # 1. Validate everything that does not need the device, before touching native code.
    prof = load_profile(path)
    if args.rehearse_in is not None and args.live:
        print("error: --rehearse-in is dry-run only; it cannot be combined with --live", file=out)
        return EXIT_USAGE
    if args.calibration is not None and not args.live:
        print("error: --calibration only applies to --live", file=out)
        return EXIT_USAGE
    nominal = prof.signup_start
    if args.rehearse_in is not None:
        try:
            nominal = clock.now() + timedelta(seconds=args.rehearse_in)
        except (OverflowError, ValueError):
            print(f"error: --rehearse-in {args.rehearse_in!r} is out of range", file=out)
            return EXIT_PROFILE_OR_PLAN_ERROR
    plan = _build_plan(args, nominal)
    wait_s = (plan.effective_target - clock.now()).total_seconds()
    if wait_s < 0:
        print(f"status:           stale  (effective target passed {-wait_s:.3f}s ago; nothing clicked)", file=out)
        print("hint: the target is in the past. For a local rehearsal use --rehearse-in SECONDS "
              "(dry-run only). Live mode has no stale override.", file=out)
        return EXIT_STALE

    # 2. Only now construct the driver (dry-run never touches a real backend).
    if args.live:
        driver, code = _make_real_driver(args, out)
        if driver is None:
            return code
    else:
        driver = MockClickDriver()

    mode = "LIVE (real click)" if driver.performs_real_click else "DRY-RUN (mock driver, no real click)"
    if args.rehearse_in is not None:
        mode += f"  ** REHEARSAL: profile start ignored, target = now + {args.rehearse_in:g}s **"
    header = [
        f"mode:             {mode}",
        f"profile:          {prof.id}  {prof.url}",
        f"nominal start:    {_fmt_instant(plan.nominal_start, prof)}",
        f"adjust:           {plan.adjust_ms:+g} ms  (negative = earlier than nominal, positive = later)",
        f"effective target: {_fmt_instant(plan.effective_target, prof)}",
    ]
    return _wait_and_click(args, plan, driver, clock, out, header=header, wait_s=wait_s)


def _wait_and_click(args: argparse.Namespace, plan: ClickPlan, driver: ClickDriver, clock: Clock, out, *,
                    header: list[str], wait_s: float) -> int:
    """Shared tail of schedule and rehearse: print the plan, preflight, build the observer for
    guarded retry, keep the Mac awake, wait, click, always close."""
    observer: ScreenObserver | None = None
    try:
        for line in header:
            print(line, file=out)
        print(f"plan:             max {plan.max_clicks} click(s), interval {plan.interval_ms:g} ms, "
              f"duration cap {plan.max_duration_ms:g} ms, driver {driver.name}"
              + ("  [guarded retry]" if plan.guarded_retry else ""), file=out)

        # 3. Preflight: permission, window, coordinates. Nothing is clicked here.
        try:
            warnings = driver.preflight()
        except PermissionError as exc:
            print(f"error: preflight failed (permission): {exc}", file=out)
            return EXIT_DRIVER_SETUP
        except KeyboardInterrupt:
            print("status:           cancelled  (during preflight; nothing clicked)", file=out)
            return EXIT_CANCELLED
        except Exception as exc:
            print(f"error: preflight failed: {exc}", file=out)
            return EXIT_DRIVER_SETUP
        for w in warnings:
            print(f"WARNING:          {w}", file=out)
        print("preflight:        ok", file=out)

        # 4. Guarded retry needs an observer. Real capture only in live mode; dry-run is injectable.
        if plan.guarded_retry:
            if driver.performs_real_click:
                observer, code = _make_real_observer(args, driver, out)
                if observer is None:
                    return code
            else:
                observer = getattr(args, "observer", None) or MockScreenObserver()
                observer.prepare()
            print(f"screen guard:     {type(observer).__name__} ready; baseline is captured shortly "
                  "before the first click", file=out)
            print(f"note: {GUARDED_NOTE}", file=out)

        if driver.performs_real_click:
            print("live:             keep the target window frontmost and still, do not move the mouse, "
                  "do not let the Mac sleep. Ctrl-C stops; switching apps makes the click refuse itself.",
                  file=out)
        print(f"waiting:          {wait_s:.3f}s  (Ctrl-C to cancel)", file=out)
        out.flush()

        last_print = clock.monotonic()

        def countdown(remaining_s: float) -> None:
            nonlocal last_print
            every = COUNTDOWN_FAR_S if remaining_s > COUNTDOWN_NEAR_THRESHOLD_S else COUNTDOWN_NEAR_S
            if clock.monotonic() - last_print >= every:
                last_print = clock.monotonic()
                print(f"waiting:          {remaining_s:.1f}s", file=out)
                out.flush()

        keep_awake = _keep_awake(out) if driver.performs_real_click else contextlib.nullcontext()
        with keep_awake:
            report = run(plan, driver, clock, on_tick=countdown, observer=observer)
    finally:
        if observer is not None:
            observer.close()
        driver.close()

    _print_report(report, out)
    if report.status == "stale":  # clock moved during setup; nothing clicked
        return EXIT_STALE
    if report.status == "cancelled":
        return EXIT_CANCELLED
    if report.status == "screen_changed":
        return EXIT_SCREEN_CHANGED
    if report.status == "error":
        return EXIT_PROFILE_OR_PLAN_ERROR
    return EXIT_OK


def cmd_rehearse(args: argparse.Namespace, clock: Clock, out) -> int:
    """Harmless real-click rehearsal: no profile, no signup target pin, calibration required.
    Same scheduler path as schedule --live, so timing evidence carries over."""
    if args.at is not None:
        try:
            nominal = datetime.fromisoformat(args.at)
        except ValueError as exc:
            print(f"error: --at {args.at!r} is not an ISO instant ({exc})", file=out)
            return EXIT_USAGE
        if nominal.tzinfo is None or nominal.utcoffset() is None:
            print(f"error: --at {args.at!r} has no UTC offset; use e.g. 2026-09-22T18:00:00+08:00", file=out)
            return EXIT_USAGE
    else:
        try:
            nominal = clock.now() + timedelta(seconds=args.seconds)
        except (OverflowError, ValueError):
            print(f"error: --in {args.seconds!r} is out of range", file=out)
            return EXIT_PROFILE_OR_PLAN_ERROR

    plan = _build_plan(args, nominal)
    wait_s = (plan.effective_target - clock.now()).total_seconds()
    if wait_s < 0:
        print(f"status:           stale  (effective target passed {-wait_s:.3f}s ago; nothing clicked)", file=out)
        return EXIT_STALE

    driver, code = _make_real_driver(args, out, pin_target=False)
    if driver is None:
        return code
    target = getattr(driver, "expected_bundle_id", "?")
    point = (getattr(driver, "x", None), getattr(driver, "y", None))
    header = [
        "mode:             REHEARSAL (REAL click on a target YOU calibrated; no profile, no signup pin)",
        f"calibration:      {args.calibration}",
        f"target app:       {target}   point: {point[0]}, {point[1]}",
        "check this is the harmless target you meant. This command never reads an event profile.",
        f"effective target: {_fmt(plan.effective_target, TAIPEI)} Taipei  |  UTC {_fmt(plan.effective_target)}",
    ]
    return _wait_and_click(args, plan, driver, clock, out, header=header, wait_s=wait_s)


def _fmt_optional(values: list[float | None]) -> str:
    return "[" + ", ".join("n/a" if v is None else f"{v:.3f}" for v in values) + "]"


def _print_report(report: RunReport, out) -> None:
    print(f"status:           {report.status}" + (f"  ({report.detail})" if report.detail else ""), file=out)
    print(f"clicks attempted: {report.clicks}  ({'dry-run' if report.dry_run else 'REAL'} via {report.driver})",
          file=out)
    print(f"lateness ms:      {[round(x, 3) for x in report.lateness_ms]}  "
          "(scheduler dispatch minus effective target; OS/device/network delivery not measured)", file=out)
    if any(v is not None for v in report.down_lateness_ms + report.up_lateness_ms + report.guard_ms):
        print(f"guard ms:         {_fmt_optional(report.guard_ms)}  "
              "(driver's pre-click checks, included in the down lateness below)", file=out)
        print(f"down posted ms:   {_fmt_optional(report.down_lateness_ms)}  "
              "(mouse-down handed to the OS minus effective target; EXCLUDES phone and network arrival)",
              file=out)
        print(f"up posted ms:     {_fmt_optional(report.up_lateness_ms)}  "
              "(mouse-up handed to the OS minus effective target; EXCLUDES phone and network arrival)", file=out)
    if report.baseline_age_ms is not None:
        print(f"baseline age ms:  {report.baseline_age_ms:.3f}  "
              "(how old the screen baseline was when the first click was dispatched)", file=out)
    if report.screen_checks:
        print(f"screen checks:    {report.screen_checks}  (fresh captures compared against the baseline)", file=out)
    print(ACCEPTANCE_NOTE, file=out)
    if report.status == "screen_changed":
        print(SCREEN_CHANGED_NOTE, file=out)
    if report.real_click:
        print(LIVE_NOTE, file=out)


def main(argv: list[str] | None = None, *, clock: Clock | None = None, out=None) -> int:
    out = out or sys.stdout
    clock = clock or system_clock()
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse already printed usage/version
        return int(exc.code or 0)
    try:
        if args.command == "list":
            return cmd_list(args.profiles_dir, clock, out)
        if args.command == "rehearse":
            return cmd_rehearse(args, clock, out)
        path = _resolve(args.profile, args.profiles_dir)
        if args.command == "show":
            return cmd_show(path, clock, out)
        return cmd_schedule(args, path, clock, out)
    except (ProfileError, PlanError) as exc:
        print(f"error: {exc}", file=out)
        return EXIT_PROFILE_OR_PLAN_ERROR
