from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ensure_utc(parsed)


def to_zone(ts: datetime, tz_name: str) -> datetime:
    return ensure_utc(ts).astimezone(ZoneInfo(tz_name))


def day_key_in_zone(ts: datetime, tz_name: str) -> str:
    return to_zone(ts, tz_name).date().isoformat()
