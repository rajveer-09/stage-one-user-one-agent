"""Tests for IST helpers and quiet hours.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from datetime import timezone

import pytest

from ott_agent.clock import (VirtualClock, in_quiet_hours, ist_day_key, next_send_time, parse_hhmm, parse_ts,
                             to_iso)

Q_START, Q_END = parse_hhmm("22:00"), parse_hhmm("08:00")


def test_parse_and_format_round_trip():
    t = parse_ts("2026-10-01T08:30:00.123Z")
    assert t.tzinfo == timezone.utc
    assert to_iso(t) == "2026-10-01T08:30:00.123Z"
    assert parse_ts("") is None
    with pytest.raises(ValueError):
        parse_ts("2026-10-01T08:30:00")  # no timezone: refuse rather than guess


def test_ist_day_rolls_over_at_1830_utc():
    assert ist_day_key(parse_ts("2026-09-30T18:29:59Z")) == "2026-09-30"
    assert ist_day_key(parse_ts("2026-09-30T18:30:00Z")) == "2026-10-01"


@pytest.mark.parametrize("utc,quiet", [
    ("2026-10-01T16:29:59Z", False),  # 21:59:59 IST
    ("2026-10-01T16:30:00Z", True),   # 22:00 IST
    ("2026-10-01T02:29:59Z", True),   # 07:59:59 IST
    ("2026-10-01T02:30:00Z", False),  # 08:00 IST
])
def test_quiet_hours_boundaries(utc, quiet):
    assert in_quiet_hours(parse_ts(utc), Q_START, Q_END) is quiet


def test_next_send_time_moves_to_0800_ist():
    late = parse_ts("2026-10-01T17:00:00Z")  # 22:30 IST
    assert to_iso(next_send_time(late, Q_START, Q_END)) == "2026-10-02T02:30:00.000Z"  # 08:00 IST next day
    early = parse_ts("2026-10-01T00:00:00Z")  # 05:30 IST
    assert to_iso(next_send_time(early, Q_START, Q_END)) == "2026-10-01T02:30:00.000Z"  # 08:00 IST same day
    day = parse_ts("2026-10-01T06:00:00Z")
    assert next_send_time(day, Q_START, Q_END) == day


def test_virtual_clock_never_goes_backwards():
    clock = VirtualClock(parse_ts("2026-10-01T05:00:00Z"))
    clock.advance_to(parse_ts("2026-10-01T04:00:00Z"))
    assert to_iso(clock.now()) == "2026-10-01T05:00:00.000Z"
    clock.advance_to(parse_ts("2026-10-01T06:00:00Z"))
    assert to_iso(clock.now()) == "2026-10-01T06:00:00.000Z"
