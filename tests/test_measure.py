"""Tests for outcome judging, impact statistics, learnings and the audit view.

Author: Rajveer Sharma
Date: 19 Oct 2026
"""

import json
from datetime import timedelta

import pytest

from conftest import INTERPRETATIONS, REPO, build_world, event
from ott_agent.agent.goal_planner import GoalPlanner
from ott_agent.agent.messaging import MessageContext
from ott_agent.agent.viewer_agent import ViewerAgent
from ott_agent.audit.explain import explain
from ott_agent.clock import parse_ts, to_iso
from ott_agent.measure.impact import compare, newcombe, pilot_reanalysis, wilson
from ott_agent.measure.judge import GoalCase, apply_corrections, cases_from_decisions, judge, judge_all, learnings
from ott_agent.pack.events import parse_event
from ott_agent.pack.loader import load_pack

START = parse_ts("2026-10-01T05:00:00Z")  # 10:30 IST
HORIZON = START + timedelta(days=15)


def ev(eid, user, name, when, **props):
    return parse_event(event(eid, user, name, to_iso(when), **props), "outcome")


def case(goal_id, start=START, target=None, user="U-3"):
    return GoalCase(user, "agent", "itt", "D-1", goal_id, start, target or {}, True)


@pytest.fixture
def pack(mini_pack):
    return load_pack(mini_pack, INTERPRETATIONS)


# ---------------------------------------------------------------- judging


def test_judge_ignores_a_signal_before_the_goal_started(pack):
    before = ev("E1", "U-3", "episode_completed", START - timedelta(minutes=5), title_id="T1", episode_id="show-a-d1-s1e1")
    out = judge(case("G02"), [before], pack, HORIZON, {})
    assert out["verdict"] == "failed"
    after = ev("E2", "U-3", "episode_completed", START + timedelta(hours=5), title_id="T1", episode_id="show-a-d1-s1e1")
    assert judge(case("G02"), [before, after], pack, HORIZON, {})["signal"] == "event:E2"


def test_judge_applies_a_rule_correction_from_its_effective_time(pack):
    pack.goals.vocabulary.add("play_started")  # the real pack observes it; the tiny fixture history doesn't
    apply_corrections(pack)  # mini pack: G03 app_open -> play_started at 08:30Z
    day2_open = ev("E1", "U-3", "app_open", parse_ts("2026-10-02T06:00:00Z"))
    day2_play = ev("E2", "U-3", "play_started", parse_ts("2026-10-02T07:00:00Z"), title_id="T5")
    # Opened before the correction and still open: from 08:30 on, only play_started counts.
    assert judge(case("G03"), [day2_open], pack, HORIZON, {})["verdict"] == "failed"
    res = judge(case("G03"), [day2_open, day2_play], pack, HORIZON, {})
    assert res["verdict"] == "succeeded" and res["rule_ref"] == "goal_rule:G03@v2"


def test_correction_does_not_reopen_a_goal_that_already_succeeded(pack):
    apply_corrections(pack)  # even if the corrected rule were unmeasurable, a v1 success stands
    early = parse_ts("2026-09-29T20:00:00Z")  # 01:30 IST 30 Sep; the next IST day starts 18:30Z
    open_next_day = ev("E1", "U-3", "app_open", parse_ts("2026-09-30T19:00:00Z"))  # before the 08:30Z correction
    res = judge(case("G03", start=early), [open_next_day], pack, HORIZON, {})
    assert res["verdict"] == "succeeded" and res["rule_ref"] == "goal_rule:G03@v1"


def test_inconclusive_when_the_deadline_is_past_the_outcome_data(pack):
    out = judge(case("G02"), [], pack, START + timedelta(hours=10), {})
    assert out["verdict"] == "inconclusive" and "outcome data ends" in out["reason"]


def test_invalidated_goal_is_inconclusive_not_a_success(pack):
    out = judge(case("G02"), [], pack, HORIZON, {"D-1": "invalidated: decided on stale facts"})
    assert out["verdict"] == "inconclusive"


def test_itt_cases_count_unserved_and_holdout_viewers_and_skip_stale(pack):
    base = {"trigger": {"event_age_s": 3, "deliver_at": to_iso(START)}, "published_at": to_iso(START)}
    decisions = [
        {**base, "decision_id": "D-a", "viewer_id": "U-3", "arm": "agent", "status": "PUBLISHED", "goal": {"id": "G02", "start": to_iso(START)}},
        {**base, "decision_id": "D-b", "viewer_id": "U-4", "arm": "agent", "status": "NO_ACTION", "goal": {"id": "G02"}},
        {**base, "decision_id": "D-c", "viewer_id": "U-2", "arm": "holdout", "status": "HOLDOUT_NO_ACTION", "goal": {"id": "G02"}},
        {**base, "decision_id": "D-d", "viewer_id": "U-5", "arm": "holdout", "status": "HOLDOUT_NO_ACTION", "goal": {"id": "G02"},
         "trigger": {"event_age_s": 99999, "deliver_at": to_iso(START)}},
    ]
    cases = cases_from_decisions(decisions, stale_after_s=900)
    assert [(c.user_id, c.role, c.served) for c in cases] == [("U-3", "itt", True), ("U-4", "itt", False), ("U-2", "itt", False)]


def test_judge_all_dedups_outcome_events_and_reports_horizon(pack):
    decisions = [{"decision_id": "D-a", "viewer_id": "U-3", "arm": "agent", "status": "PUBLISHED",
                  "goal": {"id": "G02", "start": to_iso(START)}, "published_at": to_iso(START),
                  "trigger": {"event_age_s": 1, "deliver_at": to_iso(START)}}]
    done = ev("E1", "U-3", "episode_completed", START + timedelta(hours=1), title_id="T1")
    outcomes, horizon = judge_all(decisions, [done, done, ev("E9", "U-9", "app_open", HORIZON)], pack, 900)
    assert [o["verdict"] for o in outcomes] == ["succeeded"] and horizon == HORIZON


# ---------------------------------------------------------------- statistics


def test_wilson_and_newcombe_match_known_values():
    lo, hi = wilson(10, 100)
    assert (round(lo, 4), round(hi, 4)) == (0.0552, 0.1744)
    lo, hi = newcombe(56, 70, 48, 80)  # Newcombe (1998) example 1: diff 0.20, CI 0.0524 to 0.3339
    assert (round(lo, 3), round(hi, 3)) == (0.052, 0.334)


def test_compare_flags_significance_and_includes_everyone():
    c = compare([1] * 30 + [0] * 70, [1] * 10 + [0] * 90, with_bootstrap=True)
    assert c["agent"]["n"] == 100 and round(c["diff"], 2) == 0.20 and c["significant"]
    assert c["bootstrap_ci95"][0] > 0


@pytest.mark.skipif(not (REPO / "pack/pilot/pilot_outcomes.csv").exists(), reason="real pack not present")
def test_pilot_headline_is_selection_not_uplift():
    p = pilot_reanalysis(REPO / "pack")
    assert round(p["report_headline_served_vs_rest"]["diff"], 3) == 0.224
    itt = p["itt_treatment_vs_holdout"]
    assert round(itt["diff"], 3) == -0.013 and not itt["significant"]


# ---------------------------------------------------------------- learnings feed back


def test_learnings_record_message_outcomes_and_goal_results():
    actions = [{"action_id": "A1", "user_id": "U-3", "type": "push", "decision_id": "D-1"}]
    events = [ev("M1", "U-3", "message_delivered", START, channel="push", action_id="A1")]
    outcomes = [{"viewer_id": "U-3", "arm": "agent", "served": True, "goal_id": "G02", "verdict": "failed",
                 "decision_id": "D-1", "deadline": to_iso(START + timedelta(days=3))}]
    out = learnings(outcomes, events, actions)["U-3"]
    assert {"learning": "message_outcome", "action_id": "A1", "channel": "push", "delivered": True, "clicked": False} in out
    assert any(l["learning"] == "goal_result" and l["verdict"] == "failed" for l in out)


def test_learnings_put_a_failed_goal_on_cooldown_and_skip_an_ignored_channel(mini_pack):
    pack, ctx, trigger = build_world(mini_pack)
    store = ctx.store
    later = trigger.arrival
    store.append("U-3", "learning", later - timedelta(days=1),
                 {"learning": "goal_result", "goal_id": "G03", "verdict": "failed", "deadline": to_iso(later - timedelta(days=1))})
    for i in range(2):
        store.append("U-3", "learning", later - timedelta(days=1),
                     {"learning": "message_outcome", "action_id": f"A{i}", "channel": "push", "delivered": True, "clicked": False})
    agent = ViewerAgent("U-3", ctx)
    cooldown, ignored = agent._learned(later)
    assert "G03" in cooldown and ignored == frozenset({"push"})
    d = agent.decide(trigger)
    assert d.goal.goal_id == "G02"  # G03 (higher priority) is cooling down after failing
    assert any(c.goal_id == "G03" and c.reason.startswith("cool-down") for c in d.goal.candidates)


# ---------------------------------------------------------------- audit


def test_explain_reconstructs_state_at_a_moment(mini_pack):
    pack, ctx, trigger = build_world(mini_pack)
    ViewerAgent("U-3", ctx).handle(trigger)
    before = explain(ctx.store, "U-3", trigger.arrival - timedelta(seconds=1))
    after = explain(ctx.store, "U-3", trigger.arrival + timedelta(minutes=1))
    assert before["decisions"] == [] and before["open_goal"] is None
    assert after["open_goal"]["goal_id"] == "G03"
    assert after["decisions"][0]["status"] == "PUBLISHED"
    assert after["latest_facts"]["facts"]["is_new_viewer"]["value"] is True
