"""Event profile loading and validation. Pure data; no network, no clicking."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ProfileError(ValueError):
    """Raised when a profile file is malformed."""


_REQUIRED: dict[str, type] = {
    "id": str,
    "name": str,
    "url": str,
    "timezone": str,
    "signup_start": str,
    "signup_end": str,
    "capacity": int,
    "alternates": int,
    "channel": str,
}
_OPTIONAL: dict[str, type] = {"notes": str}


@dataclass(frozen=True)
class EventProfile:
    id: str
    name: str
    url: str
    timezone: str
    signup_start: datetime  # timezone-aware
    signup_end: datetime  # timezone-aware
    capacity: int
    alternates: int
    channel: str
    notes: str = ""


def _reject_nonfinite(token: str) -> None:
    raise ProfileError(f"non-finite number not allowed: {token}")


def _parse_aware(field: str, value: str, zone: ZoneInfo) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProfileError(f"{field}: not an ISO-8601 datetime: {value!r}") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ProfileError(f"{field}: must carry a UTC offset, got {value!r}")
    if dt.utcoffset() != dt.astimezone(zone).utcoffset():
        raise ProfileError(
            f"{field}: offset {value!r} does not match timezone {zone.key!r}"
        )
    return dt


def parse_profile(data: object, *, source: str = "<data>") -> EventProfile:
    if not isinstance(data, dict):
        raise ProfileError(f"{source}: top level must be a JSON object")
    unknown = set(data) - set(_REQUIRED) - set(_OPTIONAL)
    if unknown:
        raise ProfileError(f"{source}: unknown field(s): {sorted(unknown)}")
    missing = set(_REQUIRED) - set(data)
    if missing:
        raise ProfileError(f"{source}: missing field(s): {sorted(missing)}")
    for key, typ in {**_REQUIRED, **_OPTIONAL}.items():
        if key in data and (not isinstance(data[key], typ) or isinstance(data[key], bool)):
            raise ProfileError(f"{source}: {key} must be {typ.__name__}")
    for key in ("capacity", "alternates"):
        if data[key] < 0:
            raise ProfileError(f"{source}: {key} must be a non-negative integer")
    if not data["url"].startswith("https://"):
        raise ProfileError(f"{source}: url must start with https://")
    try:
        zone = ZoneInfo(data["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ProfileError(f"{source}: unknown timezone {data['timezone']!r}") from exc

    start = _parse_aware("signup_start", data["signup_start"], zone)
    end = _parse_aware("signup_end", data["signup_end"], zone)
    if end <= start:
        raise ProfileError(f"{source}: signup_end must be after signup_start")

    return EventProfile(
        id=data["id"],
        name=data["name"],
        url=data["url"],
        timezone=data["timezone"],
        signup_start=start,
        signup_end=end,
        capacity=data["capacity"],
        alternates=data["alternates"],
        channel=data["channel"],
        notes=data.get("notes", ""),
    )


def load_profile(path: str | Path) -> EventProfile:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileError(f"{path}: cannot read: {exc}") from exc
    try:
        data = json.loads(text, parse_constant=_reject_nonfinite)
    except json.JSONDecodeError as exc:
        raise ProfileError(f"{path}: invalid JSON: {exc}") from exc
    return parse_profile(data, source=str(path))
