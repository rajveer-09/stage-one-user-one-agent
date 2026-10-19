"""Safety sweep over the real sample run: every hard rule re-checked from the output files.

Author: Rajveer Sharma
Date: 19 Oct 2026
"""

import json
from collections import Counter, defaultdict
from datetime import timedelta

import pytest

from conftest import INTERPRETATIONS, REPO
from ott_agent.clock import in_quiet_hours, ist_day_key, parse_ts
from ott_agent.config import Freshness
from ott_agent.pack.loader import load_pack
from ott_agent.state.resolver import build_resolver

RUN, PACK = REPO / "sample_run", REPO / "pack"
pytestmark = pytest.mark.skipif(not ((RUN / "decisions.jsonl").exists() and (PACK / "data/events.jsonl").exists()),
                                reason="sample run or pack not present")

REQUIRED = ("decision_id", "viewer_id", "trigger", "goal", "action", "dialect", "content", "confidence",
            "confidence_reason", "evidence", "assumptions", "conflicts", "checks", "decided_in_ms", "status")


def rows(name):
    return [json.loads(l) for l in (RUN / name).read_text(encoding="utf-8").splitlines() if l.strip()]


@pytest.fixture(scope="module")
def run():
    pack = load_pack(PACK, INTERPRETATIONS)
    for c in pack.control_events:
        if c["type"] == "title_withdrawn":
            pack.catalogue.withdraw(c["title_id"], parse_ts(c["at"]), c.get("reason", ""))
    return pack, rows("decisions.jsonl"), rows("actions.jsonl")


def titles(action):
    ids = {action.get("hero_title_id"), action.get("title_id")}
    ids |= {t for r in action.get("rows", []) for t in r["title_ids"]}
    return ids - {None}


def test_every_trigger_delivery_has_exactly_one_decision(run):
    pack, decisions, _ = run
    by_trigger = Counter(d["trigger"]["event_id"] for d in decisions if d["trigger"]["event"] in ("app_open", "login_success"))
    deliveries = Counter(t.event_id for t in pack.triggers)
    assert by_trigger == deliveries
    firsts = Counter(d["trigger"]["event_id"] for d in decisions if d["status"] != "DUPLICATE_IGNORED")
    assert all(n == 1 for n in firsts.values())


def test_every_decision_has_the_required_fields(run):
    _, decisions, _ = run
    for d in decisions:
        missing = [k for k in REQUIRED if k not in d]
        assert not missing, (d["decision_id"], missing)
        if d["action"] == "NO_ACTION":
            assert d["no_action_reason"], d["decision_id"]
        else:
            assert d["confidence_reason"] and d["evidence"] and d["goal"]["id"], d["decision_id"]


def test_holdout_and_unassigned_viewers_get_zero_actions(run):
    pack, _, actions = run
    assert [a["action_id"] for a in actions if pack.arms.get(a["user_id"]) != "agent"] == []


def test_titles_are_in_dialect_available_and_never_quarantined(run):
    pack, _, actions = run
    cat = pack.catalogue
    for a in actions:
        when = parse_ts(a.get("send_at") or a["published_at"]) if a["type"] != "home_page" else parse_ts(a["published_at"])
        for t in titles(a):
            assert t not in cat.quarantined, (a["action_id"], t)
            assert cat.check(t, parse_ts(a["published_at"])) is None, (a["action_id"], t, cat.check(t, parse_ts(a["published_at"])))
            if a["type"] == "home_page" or a.get("dialect"):
                assert cat.titles[t].dialect == a["dialect"], (a["action_id"], t)
            if a["type"] != "home_page":
                assert cat.check(t, when) is None, (a["action_id"], t, "unusable at send time")


def test_messages_respect_consent_quiet_hours_and_the_daily_cap(run):
    pack, _, actions = run
    policy = pack.policy
    resolver = build_resolver(pack, Freshness())
    per_day = defaultdict(int)
    for a in actions:
        if a["type"] not in ("push", "whatsapp"):
            continue
        send = parse_ts(a["send_at"])
        assert not in_quiet_hours(send, policy.push.quiet_start, policy.push.quiet_end), a["action_id"]
        facts = resolver.resolve(a["user_id"], parse_ts(a["published_at"]))
        assert facts.get("consent_push" if a["type"] == "push" else "consent_whatsapp") is True, a["action_id"]
        per_day[(a["user_id"], ist_day_key(send))] += 1
        if a["type"] == "push":
            assert len(a["text"]["title"]) <= policy.push.title_max_chars and len(a["text"]["body"]) <= policy.push.body_max_chars
            assert not any(p in (a["text"]["title"] + a["text"]["body"]).lower() for p in policy.push.forbidden_phrases)
        else:
            assert a["template_id"] in policy.whatsapp.templates
    assert max(per_day.values(), default=0) <= policy.messages_per_ist_day


def test_pages_follow_the_shape_rules(run):
    pack, _, actions = run
    rules, cat = pack.policy.home_page, pack.catalogue
    for a in (x for x in actions if x["type"] == "home_page"):
        assert 1 <= len(a["rows"]) <= rules.max_rows
        slugs = [cat.titles[t].content_slug for t in titles(a)]
        assert len(slugs) == len(set(slugs)), a["action_id"]
        for r in a["rows"]:
            assert r["row_type"] in rules.row_types
            assert rules.min_titles_per_row <= len(r["title_ids"]) <= rules.max_titles_per_row


def test_every_decision_published_within_budget(run):
    _, decisions, _ = run
    late = [d["decision_id"] for d in decisions if d.get("published_at")
            and parse_ts(d["published_at"]) - parse_ts(d["trigger"]["deliver_at"]) > timedelta(seconds=60)]
    assert late == []


def test_simulator_found_no_policy_problems():
    report = RUN / "sim" / "simulator_report.json"
    if not report.exists():
        pytest.skip("simulator not run")
    assert json.loads(report.read_text(encoding="utf-8"))["policy_problem_count"] == 0
