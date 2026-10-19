"""Tests for event parsing, deduplication, skew correction and the per-user index.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

import json

from conftest import event
from ott_agent.clock import parse_ts, to_iso
from ott_agent.pack.events import EventIndex, ReadStats, dedup_events, parse_event, read_events


def ev(raw, source="history"):
    return parse_event(raw, source)


def test_dedup_keeps_first_received_even_if_file_order_differs():
    # A redelivery keeps the phone timestamps; only received_at changes.
    late_copy = ev(event("E1", "U-1", "app_open", "2026-09-10T10:05:00Z", occurred="2026-09-10T09:59:59Z"))
    first_copy = ev(event("E1", "U-1", "app_open", "2026-09-10T10:00:00Z", occurred="2026-09-10T09:59:59Z"))
    other = ev(event("E2", "U-1", "search", "2026-09-10T09:00:00Z", query="x"))
    unique, stats = dedup_events([late_copy, other, first_copy])  # file order: late copy first
    assert [e.event_id for e in unique] == ["E2", "E1"]  # sorted by received_at
    assert to_iso(unique[1].received_at) == "2026-09-10T10:00:00.000Z"
    assert (stats.input, stats.unique, stats.duplicates, stats.payload_conflicts) == (3, 2, 1, 0)


def test_redelivery_is_not_a_payload_conflict_but_changed_content_is():
    a = ev(event("E1", "U-1", "app_open", "2026-09-10T10:00:00Z", occurred="2026-09-10T09:59:59Z"))
    b = ev(event("E1", "U-1", "app_open", "2026-09-10T10:05:00Z", occurred="2026-09-10T09:59:59Z"))  # only received_at differs
    c = ev(event("E1", "U-1", "app_open", "2026-09-10T10:06:00Z", occurred="2026-09-10T09:59:59Z", dialect="d4"))  # content differs
    _, stats = dedup_events([a, b, c])
    assert stats.duplicates == 2
    assert stats.payload_conflicts == 1


def test_skew_correction_with_phone_clock_three_hours_fast():
    # Phone clock is +3 h: occurred/sent look like 13:00, server received at 10:00:02.
    e = ev(event("E1", "U-1", "app_open", received="2026-09-01T10:00:02Z",
                 occurred="2026-09-01T13:00:00Z", sent="2026-09-01T13:00:01Z"))
    assert e.skew_s == -3 * 3600 + 1
    assert e.buffer_s == 1
    assert to_iso(e.corrected_occurred_at) == "2026-09-01T10:00:01.000Z"
    assert e.age_s(parse_ts("2026-09-01T10:00:31Z")) == 30  # fresh, despite the wrong phone clock


def test_offline_buffered_event_is_old_after_correction():
    # Buffered two days on a phone whose clock is also 2 h slow.
    e = ev(event("E1", "U-1", "app_open", received="2026-09-03T10:00:01Z",
                 occurred="2026-09-01T08:00:00Z", sent="2026-09-03T08:00:00Z"))
    assert e.buffer_s == 2 * 86400
    assert to_iso(e.corrected_occurred_at) == "2026-09-01T10:00:01.000Z"
    assert e.age_s(e.received_at) == 2 * 86400


def test_dialect_sentinels_normalise_to_none():
    assert ev(event("E1", "U-1", "app_open", "2026-09-01T10:00:00Z", dialect="NA")).dialect is None
    assert ev(event("E2", "U-1", "app_open", "2026-09-01T10:00:00Z", dialect=None)).dialect is None
    assert ev(event("E3", "U-1", "app_open", "2026-09-01T10:00:00Z", dialect="d3")).dialect == "d3"


def test_malformed_lines_are_counted_not_fatal():
    good = json.dumps(event("E1", "U-1", "app_open", "2026-09-01T10:00:00Z"))
    lines = [good, "{broken", json.dumps({"event_id": "E2"}), ""]
    stats = ReadStats()
    events = list(read_events(lines, "history", stats, "x.jsonl"))
    assert [e.event_id for e in events] == ["E1"]
    assert (stats.lines, stats.parsed, stats.malformed) == (3, 1, 2)


def test_index_answers_what_had_arrived_by_time_t():
    e1 = ev(event("E1", "U-1", "app_open", "2026-09-10T10:00:00Z"))
    e2 = ev(event("E2", "U-1", "search", "2026-09-10T11:00:00Z"))
    live = ev({**event("L1", "U-1", "purchase_completed", "2026-10-01T05:02:00Z"),
               "deliver_at": "2026-10-01T05:02:00Z"}, source="live")
    idx = EventIndex([e2, e1])
    assert [e.event_id for e in idx.for_user("U-1", until=parse_ts("2026-09-10T10:30:00Z"))] == ["E1"]
    assert idx.add(live) is True
    assert idx.add(live) is False  # same event_id again: rejected
    assert [e.event_id for e in idx.for_user("U-1")] == ["E1", "E2", "L1"]
    assert [e.event_id for e in idx.for_user("U-1", since=parse_ts("2026-09-10T10:30:00Z"))] == ["E2", "L1"]
    assert idx.for_user("nobody") == []
