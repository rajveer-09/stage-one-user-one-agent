"""Event model: parsing, deduplication, clock-skew correction and a per-user time index.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Iterator

from ..clock import parse_ts

REQUIRED_FIELDS = ("event_id", "user_id", "event", "received_at")
# Fields that legitimately differ between redeliveries of the same event.
DELIVERY_FIELDS = ("received_at", "deliver_at")
NO_DIALECT = {"", "NA", "N/A", "null", "none"}


class EventSchemaError(ValueError):
    """A record is missing a required field or has an unparseable timestamp."""


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    user_id: str
    event: str
    received_at: datetime
    occurred_at: datetime | None
    sent_at: datetime | None
    session_id: str | None
    app_dialect: str | None
    app_language: str | None
    platform: str | None
    properties: dict[str, Any]
    source: str
    deliver_at: datetime | None = None
    content_hash: int = field(default=0, compare=False, repr=False)

    @property
    def ref(self) -> str:
        return f"event:{self.event_id}"

    @property
    def skew_s(self) -> float:
        """Phone clock error plus network delay (received − sent); 0 when sent_at is missing."""
        if self.sent_at is None:
            return 0.0
        return (self.received_at - self.sent_at).total_seconds()

    @property
    def buffer_s(self) -> float:
        """Offline buffering delay (sent − occurred), independent of phone clock error; never negative."""
        if self.sent_at is None or self.occurred_at is None:
            return 0.0
        return max(0.0, (self.sent_at - self.occurred_at).total_seconds())

    @property
    def corrected_occurred_at(self) -> datetime:
        return self.received_at - timedelta(seconds=self.buffer_s)

    def age_s(self, at: datetime) -> float:
        """How long ago the user actually did this, measured at `at` (skew-corrected)."""
        return (at - self.corrected_occurred_at).total_seconds()

    @property
    def dialect(self) -> str | None:
        """App dialect, with sentinel strings like "NA" normalised to None."""
        if self.app_dialect is None or self.app_dialect.strip() in NO_DIALECT:
            return None
        return self.app_dialect.strip()

    @property
    def arrival(self) -> datetime:
        """When the agent can know about this event: deliver_at in replay files, else received_at."""
        return self.deliver_at or self.received_at


def parse_event(raw: dict[str, Any], source: str) -> Event:
    missing = [f for f in REQUIRED_FIELDS if not raw.get(f)]
    if missing:
        raise EventSchemaError(f"missing {missing}")
    try:
        received = parse_ts(raw["received_at"])
        occurred = parse_ts(raw.get("occurred_at"))
        sent = parse_ts(raw.get("sent_at"))
        deliver = parse_ts(raw.get("deliver_at"))
    except ValueError as exc:
        raise EventSchemaError(str(exc)) from exc
    assert received is not None
    payload = {k: v for k, v in raw.items() if k not in DELIVERY_FIELDS}
    props = raw.get("properties") or {}
    return Event(
        event_id=str(raw["event_id"]),
        user_id=str(raw["user_id"]),
        event=str(raw["event"]),
        received_at=received,
        occurred_at=occurred,
        sent_at=sent,
        session_id=raw.get("session_id"),
        app_dialect=raw.get("app_dialect"),
        app_language=raw.get("app_language"),
        platform=raw.get("platform"),
        properties=dict(props) if isinstance(props, dict) else {},
        source=source,
        deliver_at=deliver,
        content_hash=hash(json.dumps(payload, sort_keys=True, default=str)),
    )


@dataclass
class ReadStats:
    lines: int = 0
    parsed: int = 0
    malformed: int = 0
    errors: list[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        self.malformed += 1
        if len(self.errors) < 5:
            self.errors.append(msg)


def read_events(lines: Iterable[str], source: str, stats: ReadStats, name: str = "") -> Iterator[Event]:
    """Parse JSONL lines, skipping (and counting) malformed records instead of failing the whole load."""
    for lineno, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        stats.lines += 1
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise EventSchemaError("not a JSON object")
            event = parse_event(raw, source)
        except (json.JSONDecodeError, EventSchemaError) as exc:
            stats.error(f"{name}:{lineno}: {exc}")
            continue
        stats.parsed += 1
        yield event


@dataclass
class DedupStats:
    input: int = 0
    unique: int = 0
    duplicates: int = 0
    payload_conflicts: int = 0  # same event_id, different content (beyond delivery fields)


def dedup_events(events: Iterable[Event]) -> tuple[list[Event], DedupStats]:
    """Keep the first *received* copy of each event_id; return unique events sorted by received_at."""
    stats = DedupStats()
    ordered = sorted(events, key=lambda e: e.received_at)  # stable: ties keep file order
    stats.input = len(ordered)
    first: dict[str, Event] = {}
    out: list[Event] = []
    for event in ordered:
        kept = first.get(event.event_id)
        if kept is None:
            first[event.event_id] = event
            out.append(event)
            continue
        stats.duplicates += 1
        if kept.content_hash != event.content_hash:
            stats.payload_conflicts += 1
    stats.unique = len(out)
    return out, stats


class EventIndex:
    """Per-user events ordered by arrival, answering 'what had arrived by time t' with bisect."""

    def __init__(self, events: Iterable[Event] = ()) -> None:
        self._by_user: dict[str, list[Event]] = {}
        self._keys: dict[str, list[datetime]] = {}
        self._ids: set[str] = set()
        self._by_id: dict[str, Event] = {}
        for e in events:
            self.add(e)

    def add(self, event: Event) -> bool:
        """Insert an event; returns False if this event_id is already indexed (a duplicate)."""
        if event.event_id in self._ids:
            return False
        self._ids.add(event.event_id)
        self._by_id[event.event_id] = event
        events = self._by_user.setdefault(event.user_id, [])
        keys = self._keys.setdefault(event.user_id, [])
        pos = bisect.bisect_right(keys, event.arrival)
        events.insert(pos, event)
        keys.insert(pos, event.arrival)
        return True

    def __contains__(self, event_id: str) -> bool:
        return event_id in self._ids

    def get(self, event_id: str) -> Event | None:
        return self._by_id.get(event_id)

    def users(self) -> set[str]:
        return set(self._by_user)

    def for_user(self, user_id: str, until: datetime | None = None, since: datetime | None = None) -> list[Event]:
        """Events for a user that arrived in [since, until] (inclusive), oldest first."""
        events = self._by_user.get(user_id, [])
        keys = self._keys.get(user_id, [])
        hi = len(events) if until is None else bisect.bisect_right(keys, until)
        lo = 0 if since is None else bisect.bisect_left(keys, since)
        return events[lo:hi]

    def __len__(self) -> int:
        return len(self._ids)
