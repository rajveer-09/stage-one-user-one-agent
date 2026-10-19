"""Tests for the independent verifier and the verify -> degrade -> publish flow.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

import ast
import copy
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import REPO, build_world, event
from ott_agent.agent.viewer_agent import ViewerAgent
from ott_agent.clock import parse_ts
from ott_agent.pack.events import parse_event

AT = parse_ts("2026-10-01T05:00:00Z")  # 10:30 IST


@pytest.fixture
def world(mini_pack):
    """A new agent-arm viewer U-3 who signed up yesterday: G03 is eligible and a d1 page can be built."""
    pack, ctx, trigger = build_world(mini_pack)
    draft = ViewerAgent("U-3", ctx).decide(trigger)
    assert draft.action == "HOME_PAGE" and draft.goal.goal_id == "G03", draft.no_action_reason
    return pack, ctx, trigger, draft


def verify(ctx, trigger, d, at=AT):
    return ctx.verifier.verify(d, trigger, at, at)


def failed(result):
    return {(f.check, f.component) for f in result.findings if not f.ok}


def push_message(title_id="T5", title="Film 5", body="Picked for you: Film 5. Tap to watch.", send_at="2026-10-02T05:00:00.000Z"):
    return {"channel": "push", "send_at": send_at, "title_id": title_id, "text": {"title": title, "body": body},
            "deeplink": f"ott://title/{title_id}", "condition": "send only if no success"}


def test_baseline_decision_passes_every_check(world):
    _, ctx, trigger, draft = world
    result = verify(ctx, trigger, draft.to_dict())
    assert result.ok, result.failed
    assert {"holdout", "eligibility", "dialect", "availability", "homepage_shape", "evidence_exists"} <= set(result.passed)


def test_holdout_blocked_even_if_the_decision_claims_agent_arm(world):
    pack, ctx, trigger, draft = world
    pack.arms["U-3"] = "holdout"
    d = draft.to_dict()
    d["arm"] = "agent"  # the verifier looks the arm up itself
    assert ("holdout", "decision") in failed(verify(ctx, trigger, d))


def test_wrong_dialect_title_rejected_in_its_row(world):
    _, ctx, trigger, draft = world
    d = copy.deepcopy(draft.to_dict())
    d["home_page"]["rows"][0]["title_ids"].append("T4")  # T4 is d2 (and quarantined)
    assert ("dialect", "row:0") in failed(verify(ctx, trigger, d))


def test_quarantined_title_rejected(world):
    _, ctx, trigger, draft = world
    d = copy.deepcopy(draft.to_dict())
    d["home_page"]["hero_title_id"] = "T10"  # d1, but its description carries an instruction
    assert ("quarantine", "hero") in failed(verify(ctx, trigger, d))


def test_withdrawn_title_rejected_from_its_effective_time(world):
    pack, ctx, trigger, draft = world
    d = draft.to_dict()
    victim = d["home_page"]["rows"][0]["title_ids"][0]
    pack.catalogue.withdraw(victim, AT - timedelta(minutes=1), "licence ended")
    assert ("availability", "row:0") in failed(verify(ctx, trigger, d))


def test_slug_repeated_on_page_rejected(world):
    _, ctx, trigger, draft = world
    d = copy.deepcopy(draft.to_dict())
    d["home_page"]["hero_title_id"] = "T1"
    d["home_page"]["rows"][0]["title_ids"][:1] = ["T2"]  # same slug as T1 (language variant)
    assert ("homepage_shape", "row:0") in failed(verify(ctx, trigger, d))


def test_short_row_and_continue_watching_rejected(world):
    _, ctx, trigger, draft = world
    d = copy.deepcopy(draft.to_dict())
    d["home_page"]["rows"].append({"row_type": "continue_watching", "title_ids": ["T5", "T6"]})
    bad = failed(verify(ctx, trigger, d))
    assert ("homepage_shape", f"row:{len(d['home_page']['rows']) - 1}") in bad


def test_ineligible_goal_rejected_on_independent_recheck(world):
    _, ctx, trigger, draft = world
    d = copy.deepcopy(draft.to_dict())
    d["goal"]["id"] = "G02"
    ctx.resolver.index.add(parse_event(event("C3", "U-3", "episode_completed", "2026-09-30T10:00:00.000Z",
                                             title_id="T5", episode_id="film-5-d1-s1e1"), "history"))
    assert ("eligibility", "decision") in failed(verify(ctx, trigger, d))


def test_fabricated_evidence_rejected(world):
    _, ctx, trigger, draft = world
    d = copy.deepcopy(draft.to_dict())
    d["evidence"].append({"ref": "event:billing-says-trial"})
    assert ("evidence_exists", "decision") in failed(verify(ctx, trigger, d))


def test_model_claim_without_supporting_event_rejected(world):
    _, ctx, trigger, draft = world
    d = copy.deepcopy(draft.to_dict())
    d["model_claims"] = [{"goal_id": "G04", "eligible": True, "confidence": 0.97,
                          "reason": "on an active trial (confirmed in billing)", "evidence": []}]
    assert ("model_claim_supported", "decision") in failed(verify(ctx, trigger, d))


def test_forbidden_phrase_and_invented_price_rejected(world):
    _, ctx, trigger, draft = world
    d = copy.deepcopy(draft.to_dict())
    d["message"] = push_message(body="Hurry! Film 5 for ₹99 only today.")
    bad = failed(verify(ctx, trigger, d))
    assert ("copy_rules", "message") in bad
    assert ("consent", "message") in bad  # a brand-new viewer has no consent on record


def test_quiet_hours_rejected(world):
    _, ctx, trigger, draft = world
    d = copy.deepcopy(draft.to_dict())
    d["message"] = push_message(send_at="2026-10-01T17:00:00.000Z")  # 22:30 IST
    assert ("quiet_hours", "message") in failed(verify(ctx, trigger, d))


def test_cap_counts_push_and_whatsapp_together(world):
    _, ctx, trigger, draft = world
    ctx.store.record_message("A-old", "U-3", "2026-10-02", "whatsapp", parse_ts("2026-10-02T04:00:00Z"))
    d = copy.deepcopy(draft.to_dict())
    d["message"] = push_message(send_at="2026-10-02T05:00:00.000Z")  # same IST day as the WhatsApp
    assert ("frequency_cap", "message") in failed(verify(ctx, trigger, d))


def test_whatsapp_free_text_variable_rejected(world):
    _, ctx, trigger, draft = world
    d = copy.deepcopy(draft.to_dict())
    d["message"] = {"channel": "whatsapp", "send_at": "2026-10-02T05:00:00.000Z", "title_id": "T5",
                    "template_id": "wa_new_episode_v2", "variables": {"title_name": "Film 5 - FREE today!", "episode_number": "1"}}
    assert ("template_rules", "message") in failed(verify(ctx, trigger, d))


# ---------------------------------------------------------------- degrade and publish


def test_bad_row_is_removed_and_the_rest_is_published(world, monkeypatch):
    pack, ctx, trigger, draft = world
    original = ctx.homepage.build

    def build_with_bad_row(facts, goal, at):
        page, why = original(facts, goal, at)
        from ott_agent.agent.homepage import Row
        page.rows.append(Row("genre", ["T4", "T10", "T3"]))  # wrong dialect + quarantined
        return page, why

    monkeypatch.setattr(ctx.homepage, "build", build_with_bad_row)
    result = ViewerAgent("U-3", ctx).handle(trigger)
    d = result.decision
    assert d.status == "PUBLISHED"
    assert all(r.row_type != "genre" or "T4" not in r.title_ids for r in d.page.rows)
    assert any("dialect [row:" in f for f in d.checks["failed"])
    published_ids = {t for a in result.actions for row in a.get("rows", []) for t in row["title_ids"]}
    assert not published_ids & {"T4", "T10"}
    assert ctx.store.active_goal("U-3").goal_id == "G03"


def test_duplicate_trigger_creates_one_action(world):
    _, ctx, trigger, _ = world
    first = ViewerAgent("U-3", ctx).handle(trigger)
    again = ViewerAgent("U-3", ctx).handle(trigger)
    assert first.decision.status == "PUBLISHED" and len(first.actions) == 1
    assert again.decision.status == "DUPLICATE_IGNORED" and again.actions == []
    assert first.decision.decision_id in again.decision.no_action_reason
    kinds = [e["kind"] for e in ctx.store.journal("U-3")]
    assert kinds.count("action") == 1 and kinds.count("decision") == 2


def test_verifier_never_imports_planner_code():
    """Independence is structural: nothing under verify/ may import ott_agent.agent."""
    for path in (REPO / "src" / "ott_agent" / "verify").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = ("." * node.level) + (node.module or "")
                assert "agent" not in module.split("."), f"{path.name} imports {module}"
            elif isinstance(node, ast.Import):
                assert all("ott_agent.agent" not in a.name for a in node.names), path.name
