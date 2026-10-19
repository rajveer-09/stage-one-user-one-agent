"""Tests for the goal registry: measurability, interpretations and rule versioning.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

import json

from conftest import INTERPRETATIONS
from ott_agent.clock import parse_ts
from ott_agent.pack.goals import load_goals

VOCAB = {"app_open", "episode_completed", "play_started"}


def registry(mini_pack, interpretations=None, vocab=VOCAB):
    return load_goals(mini_pack / "config/goals.json", interpretations, vocab)


def test_unmeasurable_goals_disabled_automatically(mini_pack):
    reg = registry(mini_pack)  # no interpretations file: automatic check alone
    assert reg.enabled_ids() == ["G02", "G03"]
    assert "success.event is null" in reg.status["G10"].reasons[0]
    assert "'tv_app_open' never observed" in reg.status["G11"].reasons[0]


def test_explicit_interpretations_add_reasons_and_assumptions(mini_pack):
    reg = registry(mini_pack, INTERPRETATIONS)
    assert len(reg.status["G11"].reasons) == 2  # explicit + automatic agree
    ids = {a.id for a in reg.assumptions_for("G03")}
    assert "G03-a1" in ids and "ALL-a1" in ids  # goal-specific plus the global ones
    assert reg.assumptions_for("G03")[0].ref.startswith("assumption:")


def test_rule_correction_adds_a_version_effective_from_its_time(mini_pack):
    reg = registry(mini_pack)
    control = json.loads((mini_pack / "replay/control_events.jsonl").read_text().splitlines()[0])
    result = reg.apply_correction(control)

    assert result.old_rule.ref == "goal_rule:G03@v1"
    assert result.new_rule.ref == "goal_rule:G03@v2"
    assert result.warnings == []
    assert reg.current("G03", parse_ts("2026-10-01T08:29:59Z")).success.event == "app_open"
    assert reg.current("G03", parse_ts("2026-10-01T08:30:00Z")).success.event == "play_started"
    assert reg.current("G03").version == 2
    assert reg.current("G02").version == 1  # other goals untouched


def test_correction_with_mismatched_old_rule_is_applied_with_a_warning(mini_pack):
    reg = registry(mini_pack)
    result = reg.apply_correction({
        "at": "2026-10-01T08:30:00Z", "type": "goal_rule_corrected", "goal_id": "G03",
        "old_success": {"event": "login_success"}, "new_success": {"event": "play_started"}})
    assert reg.current("G03").success.event == "play_started"
    assert any("old_success.event" in w for w in result.warnings)


def test_correction_to_an_unobserved_event_disables_the_goal(mini_pack):
    reg = registry(mini_pack)
    reg.apply_correction({"at": "2026-10-01T08:30:00Z", "type": "goal_rule_corrected", "goal_id": "G02",
                          "old_success": {"event": "episode_completed"}, "new_success": {"event": "never_seen"}})
    assert not reg.is_enabled("G02")
