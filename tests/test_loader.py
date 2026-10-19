"""Tests for pack loading and schema validation, plus a smoke test on the real pack when present.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

import csv
from pathlib import Path

import pytest

from conftest import INTERPRETATIONS, REPO, write_csv
from ott_agent.clock import parse_hhmm
from ott_agent.pack.loader import PackSchemaError, load_pack


def test_mini_pack_loads_with_expected_shape(mini_pack):
    pack = load_pack(mini_pack, INTERPRETATIONS)
    s = pack.summary()
    assert (s["history"]["unique"], s["history"]["duplicates"], s["history"]["payload_conflicts"]) == (3, 1, 0)
    assert s["profiles"]["users"] == 2
    assert s["catalogue"]["quarantined"] == ["T10", "T4"]
    assert s["goals"]["enabled"] == ["G02", "G03"]
    # Triggers keep their duplicate (idempotency is the harness's job) and are in deliver order.
    assert [t.event_id for t in pack.triggers] == ["R1", "R1"]
    assert pack.triggers[0].deliver_at < pack.triggers[1].deliver_at
    assert [c["type"] for c in pack.control_events] == ["title_withdrawn", "goal_rule_corrected"]  # sorted by at
    assert pack.model_faults["R1"]["fault"] == "timeout"
    assert pack.arms == {"U-1": "agent", "U-2": "holdout"}


def test_policy_is_parsed_from_config(mini_pack):
    policy = load_pack(mini_pack, INTERPRETATIONS).policy
    assert policy.messages_per_ist_day == 1
    assert (policy.push.quiet_start, policy.push.quiet_end) == (parse_hhmm("22:00"), parse_hhmm("08:00"))
    assert "hurry" in policy.push.forbidden_phrases
    assert policy.push.deeplink("T1") == "ott://title/T1"
    assert policy.allowed_prices_inr == {1, 149, 399}
    assert policy.in_app.consent_required is False


def test_missing_column_fails_loudly_with_file_name(mini_pack):
    path = mini_pack / "data/profiles_snapshot.csv"
    rows = list(csv.reader(path.open(encoding="utf-8")))
    idx = rows[0].index("whatsapp_opt_in")
    write_csv(path, [c for i, c in enumerate(rows[0]) if i != idx], [[c for i, c in enumerate(r) if i != idx] for r in rows[1:]])
    with pytest.raises(PackSchemaError, match=r"profiles_snapshot.csv: missing columns \['whatsapp_opt_in'\]"):
        load_pack(mini_pack, INTERPRETATIONS)


def test_mostly_malformed_events_are_treated_as_schema_drift(mini_pack):
    (mini_pack / "data/events.jsonl").write_text('{"evt": 1}\n{"evt": 2}\n', encoding="utf-8")
    with pytest.raises(PackSchemaError, match="schema drift"):
        load_pack(mini_pack, INTERPRETATIONS)


def test_unknown_arm_is_warned_and_excluded(mini_pack):
    write_csv(mini_pack / "replay/arms.csv", ["user_id", "arm"], [["U-1", "agent"], ["U-2", "control"]])
    pack = load_pack(mini_pack, INTERPRETATIONS)
    assert "U-2" not in pack.arms
    assert any("unknown arm 'control'" in w for w in pack.warnings)


REAL_PACK = REPO / "pack"


@pytest.mark.skipif(not (REAL_PACK / "data/events.jsonl").exists(), reason="real data pack not present")
def test_real_pack_counts_match_data_findings():
    s = load_pack(REAL_PACK, INTERPRETATIONS).summary()
    assert (s["history"]["unique"], s["history"]["duplicates"], s["history"]["payload_conflicts"]) == (140730, 2176, 0)
    assert (s["profiles"]["rows"], s["profiles"]["users_with_duplicate_rows"]) == (4888, 30)
    assert s["catalogue"]["quarantined"] == ["T0002", "T0522"]
    assert sorted(s["goals"]["disabled"]) == ["G10", "G11"]
    assert (s["replay"]["unique_triggers"], s["replay"]["duplicate_triggers"]) == (2679, 90)
    assert s["warnings"] == []
