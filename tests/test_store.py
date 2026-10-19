"""Tests for the durable state store: invariants, idempotency, ledger, outage buffering, export.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

import json

import pytest

from ott_agent.clock import parse_ts
from ott_agent.state.store import ActiveGoal, SingleGoalViolation, StateStore, StoreUnavailable

T0 = parse_ts("2026-10-01T05:00:00Z")


def goal(user="U-1", goal_id="G03", decision="D-1"):
    return ActiveGoal(user, goal_id, 1, decision, T0, parse_ts("2026-10-03T05:00:00Z"), {"action": "home_page"})


def test_idempotency_claim_returns_the_owner_on_repeat():
    store = StateStore()
    assert store.claim("trigger:R1", "D-1", T0) is None
    assert store.claim("trigger:R1", "D-2", T0) == "D-1"


def test_idempotency_survives_reopening_the_database(tmp_path):
    path = tmp_path / "state.db"
    store = StateStore(path)
    store.claim("trigger:R1", "D-1", T0)
    store.close()
    assert StateStore(path).claim("trigger:R1", "D-9", T0) == "D-1"


def test_one_active_goal_per_viewer_is_enforced_by_the_database():
    store = StateStore()
    store.open_goal(goal())
    with pytest.raises(SingleGoalViolation):
        store.open_goal(goal(goal_id="G02", decision="D-2"))
    assert store.close_goal("U-1").goal_id == "G03"
    store.open_goal(goal(goal_id="G02", decision="D-2"))  # allowed once the first is closed
    assert store.active_goal("U-1").goal_id == "G02"


def test_open_goals_for_finds_only_that_goal_id():
    store = StateStore()
    store.open_goal(goal("U-1", "G03"))
    store.open_goal(goal("U-2", "G02"))
    store.open_goal(goal("U-3", "G03"))
    assert [g.user_id for g in store.open_goals_for("G03")] == ["U-1", "U-3"]
    store.update_goal("U-1", rule_version=2)
    assert store.active_goal("U-1").rule_version == 2


def test_message_cap_counts_push_and_whatsapp_per_ist_day_excluding_cancelled():
    store = StateStore()
    store.record_message("A-1", "U-1", "2026-10-01", "push", T0)
    store.record_message("A-2", "U-1", "2026-10-01", "whatsapp", T0, status="cancelled")
    store.record_message("A-3", "U-1", "2026-10-02", "whatsapp", T0)
    store.record_message("A-4", "U-1", "2026-10-01", "in_app_message", T0)
    assert store.messages_on("U-1", "2026-10-01") == 1
    store.set_message_status("A-1", "cancelled")
    assert store.messages_on("U-1", "2026-10-01") == 0
    assert store.messages_on("U-1", "2026-10-02") == 1


def test_outage_blocks_reads_buffers_writes_and_still_catches_duplicates(tmp_path):
    store = StateStore(tmp_path / "state.db", buffer_path=tmp_path / "wal.jsonl")
    store.claim("trigger:R1", "D-1", T0)
    store.set_available(False)

    with pytest.raises(StoreUnavailable):
        store.active_goal("U-1")
    with pytest.raises(StoreUnavailable):
        store.open_goal(goal())
    store.append("U-1", "decision", T0, {"action": "NO_ACTION", "reason": "dependency_unavailable"})
    assert store.claim("trigger:R1", "D-2", T0) == "D-1"  # duplicate caught from memory
    assert store.claim("trigger:R2", "D-3", T0) is None
    assert store.buffered_writes == 2
    assert (tmp_path / "wal.jsonl").exists()

    assert store.set_available(True) == 2
    assert not (tmp_path / "wal.jsonl").exists()
    assert [e["payload"]["reason"] for e in store.journal("U-1")] == ["dependency_unavailable"]


def test_journal_until_reconstructs_what_was_known_and_export_groups_by_kind(tmp_path):
    store = StateStore()
    store.append("U-1", "facts", T0, {"dialect_seen": "d3"})
    store.append("U-1", "decision", parse_ts("2026-10-01T05:00:01Z"), {"action": "HOME_PAGE"}, ref="decision:D-1")
    store.append("U-1", "outcome", parse_ts("2026-10-04T05:00:00Z"), {"verdict": "succeeded"})
    assert [e["kind"] for e in store.journal("U-1", until=parse_ts("2026-10-02T00:00:00Z"))] == ["facts", "decision"]
    assert [e["kind"] for e in store.journal("U-1", kinds=["outcome"])] == ["outcome"]

    store.open_goal(goal())
    assert store.export_all(tmp_path / "viewer_state") == 1
    exported = json.loads((tmp_path / "viewer_state" / "U-1.json").read_text(encoding="utf-8"))
    assert exported["active_goal"]["goal_id"] == "G03"
    assert set(exported["journal"]) == {"facts", "decision", "outcome"}
    with pytest.raises(ValueError):
        store.append("U-1", "gossip", T0, {})
