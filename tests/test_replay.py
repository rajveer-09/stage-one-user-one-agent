"""Tests for the replay harness: queueing, idempotency, live events and every control-event surprise.

Author: Rajveer Sharma
Date: 19 Oct 2026
"""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from conftest import INTERPRETATIONS, build_pack, event, write_csv, write_jsonl
from ott_agent.clock import parse_ts, to_iso
from ott_agent.config import AgentConfig, Replay
from ott_agent.pack.loader import load_pack
from ott_agent.replay.harness import ReplayHarness, write_outputs
from ott_agent.state.store import ActiveGoal

T0 = parse_ts("2026-10-01T05:00:00Z")  # 10:30 IST


def at(seconds: float) -> str:
    return to_iso(T0 + timedelta(seconds=seconds))


def trig(eid, user, seconds, occurred=None, **props):
    raw = event(eid, user, "app_open", at(seconds), occurred=occurred, **props)
    return {**raw, "deliver_at": at(seconds)}


def live(eid, user, name, seconds, occurred=None, **props):
    return {**event(eid, user, name, at(seconds), occurred=occurred, **props), "deliver_at": at(seconds)}


def make_replay(tmp_path, triggers, live_events=(), controls=(), faults=()):
    root = build_pack(tmp_path / "pack")
    history = [json.loads(l) for l in (root / "data/events.jsonl").read_text().splitlines()]
    history += [event("S3", "U-3", "signup_completed", "2026-09-30T09:00:00.000Z", method="phone_otp"),
                event("S4", "U-4", "signup_completed", "2026-09-30T09:00:00.000Z", method="phone_otp")]
    write_jsonl(root / "data/events.jsonl", history)
    write_csv(root / "replay/arms.csv", ["user_id", "arm"], [["U-1", "agent"], ["U-2", "holdout"], ["U-3", "agent"], ["U-4", "agent"]])
    write_jsonl(root / "replay/triggers.jsonl", list(triggers))
    write_jsonl(root / "replay/live_events.jsonl", list(live_events))
    write_jsonl(root / "replay/control_events.jsonl", list(controls))
    write_jsonl(root / "replay/model_faults.jsonl", list(faults))
    return root


def run(root, tmp_path, config=None):
    pack = load_pack(root, INTERPRETATIONS)
    harness = ReplayHarness(pack, config or AgentConfig(goal_interpretations=INTERPRETATIONS), tmp_path / "work")
    return harness, harness.run()


def decision_for(result, trigger_id):
    return next(d for d in result.decisions if d["trigger"]["event_id"] == trigger_id and d["status"] != "DUPLICATE_IGNORED")


# ---------------------------------------------------------------- end to end, idempotency, holdout


def test_replay_writes_outputs_and_holdout_gets_nothing(tmp_path):
    root = make_replay(tmp_path, [trig("R3", "U-3", 0), trig("R2", "U-2", 1, dialect="d2"), trig("R3", "U-3", 30)])
    harness, result = run(root, tmp_path)
    paths = write_outputs(result, tmp_path / "out", harness.ctx.store)
    assert [d["status"] for d in result.decisions] == ["PUBLISHED", "HOLDOUT_NO_ACTION", "DUPLICATE_IGNORED"]
    assert {a["user_id"] for a in result.actions} == {"U-3"}
    assert len([a for a in result.actions if a["type"] == "home_page"]) == 1  # the duplicate created nothing
    assert (paths["viewer_state"] / "U-3.json").exists()
    assert all(json.loads(l) for l in paths["actions"].read_text().splitlines())


# ---------------------------------------------------------------- queueing


def test_fresh_triggers_jump_ahead_of_stale_ones_under_backlog(tmp_path):
    triggers = [
        trig("A", "U-3", 0),                                                   # slow: model times out (10 s)
        trig("B", "U-1", 1, occurred=at(-3 * 86400)),                          # stale offline open, arrives 2nd
        trig("C", "U-4", 2),                                                   # fresh, arrives 3rd
    ]
    triggers[1]["sent_at"] = at(1)  # buffered 3 days on the phone, sent just now
    root = make_replay(tmp_path, triggers, faults=[{"trigger_event_id": "A", "user_id": "U-3", "fault": "timeout", "seconds": 45}])
    config = AgentConfig(goal_interpretations=INTERPRETATIONS, replay=Replay(workers=1, load_shed_queue_wait_s=60))
    _, result = run(root, tmp_path, config)
    order = [d["trigger"]["event_id"] for d in result.decisions]
    assert order == ["A", "C", "B"]
    assert decision_for(result, "B")["no_action_reason"].startswith("stale_trigger")
    assert decision_for(result, "C")["queue_wait_ms"] > 5000


def test_long_queue_switches_to_rules_only(tmp_path):
    root = make_replay(tmp_path, [trig("A", "U-3", 0), trig("C", "U-4", 1)],
                       faults=[{"trigger_event_id": "A", "user_id": "U-3", "fault": "timeout", "seconds": 45}])
    config = AgentConfig(goal_interpretations=INTERPRETATIONS, replay=Replay(workers=1, load_shed_queue_wait_s=5))
    _, result = run(root, tmp_path, config)
    c = decision_for(result, "C")
    assert c["model_calls"] == 0 and c["fallback_used"] == "rules: load shedding"
    assert c["status"] == "PUBLISHED"


def test_peak_burst_still_publishes_within_the_budget(tmp_path):
    users = ["U-1", "U-3", "U-4"]
    triggers = [trig(f"P{i}", users[i % 3], i * 0.2) for i in range(300)]  # 300 opens in one minute
    root = make_replay(tmp_path, triggers)
    _, result = run(root, tmp_path)
    worst = max((parse_ts(d["published_at"]) - parse_ts(d["trigger"]["deliver_at"])).total_seconds() for d in result.decisions)
    assert worst < 60


# ---------------------------------------------------------------- live events


def _open_goal(harness, user, goal_id, minutes_ago=10, decision="D-test"):
    start = T0 - timedelta(minutes=minutes_ago)
    harness.ctx.store.open_goal(ActiveGoal(user, goal_id, 1, decision, start, start + timedelta(days=7), {}))


def test_purchase_after_decision_closes_the_paid_goal_cancels_its_message_and_replans(tmp_path):
    from ott_agent.pack.events import parse_event
    root = make_replay(tmp_path, [])
    pack = load_pack(root, INTERPRETATIONS)
    pack.goals._versions["G02"][0] = replace(pack.goals.current("G02"), success=replace(
        pack.goals.current("G02").success, event="purchase_completed", condition="is_trial = false"))
    harness = ReplayHarness(pack, AgentConfig(goal_interpretations=INTERPRETATIONS), tmp_path / "work")
    _open_goal(harness, "U-3", "G02", decision="D-old")
    harness.index.add({"action_id": "D-old-A2", "decision_id": "D-old", "user_id": "U-3", "type": "push",
                       "send_at": at(86400), "published_at": at(-600), "title_id": "T5"})
    harness.ctx.store.record_message("D-old-A2", "U-3", "2026-10-02", "push", T0 + timedelta(days=1))
    buy = parse_event(live("L9", "U-3", "purchase_completed", 120, plan_id="P_MONTH", price_inr=149, is_trial=False), "live")
    harness.ctx.resolver.index.add(buy)
    harness._on_live_event(buy)

    kinds = [r["type"] for r in harness.control_log]
    assert "goal_closed_by_live_event" in kinds and "replanned_after_purchase" in kinds
    assert harness.index.status["D-old-A2"] == "cancelled"
    replan = harness.decisions[-1]
    assert replan["trigger"]["event"] == "purchase_completed"
    facts = harness.ctx.store.journal("U-3", kinds=["facts"])[-1]["payload"]["facts"]
    assert facts["subscription_status"]["value"] == "active"  # the purchase is visible to the re-plan


def test_purchase_that_happened_before_the_goal_started_invalidates_it(tmp_path):
    from ott_agent.pack.events import parse_event
    root = make_replay(tmp_path, [])
    pack = load_pack(root, INTERPRETATIONS)
    pack.goals._versions["G02"][0] = replace(pack.goals.current("G02"), eligibility={"subscription_status": ["never"]})
    harness = ReplayHarness(pack, AgentConfig(goal_interpretations=INTERPRETATIONS), tmp_path / "work")
    _open_goal(harness, "U-3", "G02", minutes_ago=10)
    raw = live("L9", "U-3", "purchase_completed", 120, occurred=at(-3600), plan_id="P_MONTH", price_inr=149, is_trial=False)
    raw["sent_at"] = at(119)  # buffered offline: really happened an hour ago, before the goal began
    buy = parse_event(raw, "live")
    harness.ctx.resolver.index.add(buy)
    harness._on_live_event(buy)
    closed = next(r for r in harness.control_log if r["type"] == "goal_closed_by_live_event")
    assert closed["reason"].startswith("invalidated")


def test_opt_out_cancels_pending_messages_on_that_channel(tmp_path):
    from ott_agent.pack.events import parse_event
    root = make_replay(tmp_path, [])
    harness = ReplayHarness(load_pack(root, INTERPRETATIONS), AgentConfig(goal_interpretations=INTERPRETATIONS), tmp_path / "w")
    harness.index.add({"action_id": "A-p", "decision_id": "D", "user_id": "U-3", "type": "push", "send_at": at(86400),
                       "published_at": at(0)})
    harness.ctx.store.record_message("A-p", "U-3", "2026-10-02", "push", T0)
    harness._on_live_event(parse_event(live("O1", "U-3", "notification_opt_out", 60, channel="push"), "live"))
    assert harness.index.status["A-p"] == "cancelled"


# ---------------------------------------------------------------- control events


def test_rule_correction_targets_only_affected_open_goals(tmp_path):
    correction = {"at": at(60), "type": "goal_rule_corrected", "goal_id": "G03",
                  "old_success": {"event": "app_open"}, "new_success": {"event": "play_started"},
                  "applies_to": "every G03 goal that is still open"}
    root = make_replay(tmp_path, [], controls=[correction])
    pack = load_pack(root, INTERPRETATIONS)
    harness = ReplayHarness(pack, AgentConfig(goal_interpretations=INTERPRETATIONS), tmp_path / "work")
    _open_goal(harness, "U-3", "G03", decision="D-a")
    _open_goal(harness, "U-4", "G03", decision="D-b")
    _open_goal(harness, "U-1", "G02", decision="D-c")
    result = harness.run()

    [log] = [r for r in result.control_log if r["type"] == "goal_rule_corrected"]
    assert log["affected_open_goals"] == 2 and sorted(log["affected_viewers"]) == ["U-3", "U-4"]
    store = harness.ctx.store
    assert store.active_goal("U-3").rule_version == 2 and store.active_goal("U-4").rule_version == 2
    assert store.active_goal("U-1").rule_version == 1                 # other goals untouched
    assert store.journal("U-1", kinds=["goal"]) == []                 # and not reprocessed at all
    assert pack.goals.current("G03").success.event == "play_started"


def test_withdrawn_title_is_removed_from_live_pages_and_never_used_again(tmp_path):
    root = make_replay(tmp_path, [trig("R3", "U-3", 0), trig("R4", "U-4", 600)])
    pack = load_pack(root, INTERPRETATIONS)
    harness = ReplayHarness(pack, AgentConfig(goal_interpretations=INTERPRETATIONS), tmp_path / "w")
    first = harness.run()
    page = next(a for a in first.actions if a["user_id"] == "U-3")
    victim = page["rows"][0]["title_ids"][0]

    withdrawal = {"at": at(300), "type": "title_withdrawn", "title_id": victim, "reason": "licence ended"}
    root2 = make_replay(tmp_path / "second", [trig("R3", "U-3", 0), trig("R4", "U-4", 600)], controls=[withdrawal])
    _, result = run(root2, tmp_path / "second")
    log = next(r for r in result.control_log if r["type"] == "title_withdrawn")
    assert log["pages_rebuilt"] == ["U-3"]
    for a in result.actions:
        ids = {a.get("hero_title_id")} | {t for r in a.get("rows", []) for t in r["title_ids"]}
        if a["published_at"] >= at(300):
            assert victim not in ids


def test_store_outage_means_no_action_and_buffered_writes_until_recovery(tmp_path):
    outage = {"at": at(-1), "type": "dependency_unavailable", "dependency": "user_data_store", "until": at(120),
              "detail": "store down"}
    root = make_replay(tmp_path, [trig("R3", "U-3", 0), trig("R4", "U-4", 300)], controls=[outage])
    harness, result = run(root, tmp_path)
    assert decision_for(result, "R3")["no_action_reason"].startswith("dependency_unavailable")
    assert decision_for(result, "R4")["status"] == "PUBLISHED"  # after recovery
    recovered = next(r for r in result.control_log if r["type"] == "dependency_recovered")
    assert recovered["buffered_writes_replayed"] > 0


def test_degraded_store_read_timeout_uses_cache_home_page_only(tmp_path):
    root = make_replay(tmp_path, [trig("R3", "U-3", 0)])
    pack = load_pack(root, INTERPRETATIONS)
    harness = ReplayHarness(pack, AgentConfig(goal_interpretations=INTERPRETATIONS), tmp_path / "w")
    harness.ctx.store_probe = lambda when, key: (False, 2000.0)  # every read times out after 2 s
    result = harness.run()
    d = decision_for(result, "R3")
    assert d["status"] == "PUBLISHED" and d["action"] == "HOME_PAGE"
    assert "write-through cache" in d["degraded_mode"]
    assert d["latency_breakdown_ms"]["dependencies"] >= 2000


def test_unknown_control_event_is_logged_and_ignored(tmp_path):
    root = make_replay(tmp_path, [trig("R3", "U-3", 0)])
    pack = load_pack(root, INTERPRETATIONS)
    pack.control_events.append({"at": at(30), "type": "solar_flare"})
    harness = ReplayHarness(pack, AgentConfig(goal_interpretations=INTERPRETATIONS), tmp_path / "w")
    result = harness.run()
    assert any(r["type"] == "control_event_ignored" for r in result.control_log)
