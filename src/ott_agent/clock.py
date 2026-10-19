"""Time helpers: strict timestamp parsing, IST calendar days, quiet hours and the virtual clock.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp to aware UTC. Empty/None -> None; malformed -> ValueError."""
    if value is None or value == "":
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {value!r}")
    return dt.astimezone(UTC)


def to_iso(dt: datetime) -> str:
    """Format as the pack does: UTC with milliseconds and a trailing Z."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def ist_date(dt: datetime) -> date:
    return dt.astimezone(IST).date()


def ist_day_key(dt: datetime) -> str:
    """Key for 'per IST calendar day' rules, e.g. the message frequency cap."""
    return ist_date(dt).isoformat()


def parse_hhmm(text: str) -> time:
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


def in_quiet_hours(dt: datetime, start: time, end: time) -> bool:
    """True if dt falls in [start, end) IST; handles windows that cross midnight (22:00–08:00)."""
    local = dt.astimezone(IST).time()
    if start <= end:
        return start <= local < end
    return local >= start or local < end


def next_send_time(dt: datetime, start: time, end: time) -> datetime:
    """dt itself if outside quiet hours, else the next moment quiet hours end (as UTC)."""
    if not in_quiet_hours(dt, start, end):
        return dt
    local = dt.astimezone(IST)
    candidate = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


class VirtualClock:
    """Replay time. Only moves forward; set by the harness from deliver_at order."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance_to(self, t: datetime) -> None:
        if t > self._now:
            self._now = t
