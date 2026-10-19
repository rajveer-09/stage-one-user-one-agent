"""Tests for the model provider layer: fault replay, fallbacks, budgets and the circuit breaker.

Author: Rajveer Sharma
Date: 19 Oct 2026
"""

import json
from datetime import timedelta

import pytest

from conftest import INTERPRETATIONS, build_world
from ott_agent.agent.budget import Budget, BudgetExceeded
from ott_agent.agent.viewer_agent import ViewerAgent
from ott_agent.clock import parse_ts
from ott_agent.config import AgentConfig, Budget as BudgetConfig
from ott_agent.models.faults import DependencySchedule, FaultInjectingProvider
from ott_agent.models.mock import MockProvider
from ott_agent.models.provider import ModelResult, parse_goal_assessment

AT = parse_ts("2026-10-01T05:00:00Z")


class Scripted:
    """A provider that returns canned answers and counts its calls."""

    name = "scripted"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        answer = self.answers[min(self.calls - 1, len(self.answers) - 1)]
        if isinstance(answer, ModelResult):
            return answer
        if callable(answer):
            answer = answer(request)
        raw = answer if isinstance(answer, str) else json.dumps(answer)
        output, error, detail = parse_goal_assessment(raw)
        return ModelResult(error is None, output, raw, 500.0, error, detail, self.name)


def faulty(fault, inner=None, controls=()):
    return FaultInjectingProvider(inner or MockProvider("t"), {"R3": {"trigger_event_id": "R3", **fault}},
                                  DependencySchedule(list(controls)))


def run(mini_pack, provider, config=None):
    pack, ctx, trigger = build_world(mini_pack, provider=provider, config=config)
    return ctx, ViewerAgent("U-3", ctx).handle(trigger).decision


# ---------------------------------------------------------------- happy path


def test_mock_is_consulted_only_with_two_eligible_goals_and_agrees(mini_pack):
    ctx, d = run(mini_pack, None)  # default provider: mock + the pack's faults (none target R3)
    assert d.model_calls == 1
    assert d.model_claims and d.model_claims[0]["goal_id"] == d.goal.goal_id == "G03"
    assert "model agreed" in " ".join(d.notes)
    assert d.status == "PUBLISHED" and "model_claim_supported" in d.checks["passed"]
    assert d.dependency_ms > 0 and d.decided_in_ms >= d.dependency_ms  # model time counts toward latency


def test_accepted_model_choice_switches_to_another_eligible_goal(mini_pack):
    def prefer_g02(request):
        g02 = next(c for c in request.payload["candidates"] if c["goal_id"] == "G02")
        refs = [r for r in g02["evidence"] if r.startswith("event:")]
        return {"goal_id": "G02", "eligible": True, "confidence": 0.8, "reason": "short first episodes", "evidence": refs}

    ctx, d = run(mini_pack, Scripted(prefer_g02))
    assert d.goal.goal_id == "G02"
    assert "preferred G02 over G03" in " ".join(d.notes)
    assert d.status == "PUBLISHED" and not d.checks["failed"]  # the verifier re-checked the claim and agreed


# ---------------------------------------------------------------- failures fall back to rules


def test_model_timeout_falls_back_to_rules_without_retry(mini_pack):
    ctx, d = run(mini_pack, faulty({"fault": "timeout", "seconds": 45}))
    assert d.model_calls == 1                       # no retry: a timeout already cost the latency
    assert d.dependency_ms == pytest.approx(10_000)  # capped at the per-call timeout, not 45 s
    assert d.fallback_used.startswith("rules: model timeout")
    assert d.goal.goal_id == "G03" and d.status == "PUBLISHED"
    assert d.decided_in_ms < 60_000


def test_malformed_model_json_is_retried_once_then_rules(mini_pack):
    provider = faulty({"fault": "malformed_output", "model_output": '{"goal_id": "G02", "eligible": tr'})
    ctx, d = run(mini_pack, provider)
    assert d.model_calls == 2
    assert [r["error"] for r in d.model_claims_rejected] == ["malformed", "malformed"]
    assert d.fallback_used.startswith("rules: model malformed")
    assert d.goal.goal_id == "G03" and d.status == "PUBLISHED"
    assert provider.exercised == {"R3": 2}


def test_malformed_then_valid_answer_is_accepted_on_retry(mini_pack):
    ctx, d = run(mini_pack, Scripted('{"goal_id": "G0', lambda r: {
        "goal_id": "G03", "eligible": True, "confidence": 0.6, "reason": "new viewer",
        "evidence": [e for e in r.payload["candidates"][0]["evidence"] if e.startswith("event:")]}))
    assert d.model_calls == 2 and d.fallback_used is None and d.model_claims


def test_unsupported_model_claim_is_rejected_and_never_used(mini_pack):
    claim = {"goal_id": "G04", "eligible": True, "confidence": 0.97,
             "reason": "The user is on an active trial that ends tomorrow (confirmed in billing)."}
    ctx, d = run(mini_pack, faulty({"fault": "unsupported_claim", "model_output": claim}))
    [rejected] = d.model_claims_rejected
    assert rejected["error"] == "unsupported_claim" and "G04" in rejected["detail"]
    assert d.model_claims == []
    assert d.goal.goal_id == "G03"
    assert d.fallback_used == "rules: model claim rejected (unsupported_claim)"


def test_claim_for_an_eligible_goal_without_real_evidence_is_rejected(mini_pack):
    ctx, d = run(mini_pack, Scripted({"goal_id": "G02", "eligible": True, "confidence": 0.9,
                                      "reason": "trust me", "evidence": ["event:made-up"]}))
    assert d.model_claims_rejected[0]["error"] == "unsupported_claim"
    assert d.goal.goal_id == "G03"


def test_provider_outage_window_fails_fast_and_opens_the_circuit(mini_pack):
    outage = {"at": "2026-10-01T04:55:00Z", "type": "dependency_unavailable", "dependency": "model_provider",
              "until": "2026-10-01T05:10:00Z", "detail": "every model call fails with HTTP 503"}
    provider = FaultInjectingProvider(MockProvider("t"), {}, DependencySchedule([outage]))
    pack, ctx, trigger = build_world(mini_pack, provider=provider)
    breaker = ctx.advisor.breaker
    for i in range(3):  # three failing decisions open the breaker
        d = ctx.advisor.consult(*_advice_args(ctx, trigger, AT + timedelta(seconds=i)))
        assert d.fallback_used.startswith("rules: model unavailable")
    assert not breaker.allows(AT + timedelta(seconds=3))
    skipped = ctx.advisor.consult(*_advice_args(ctx, trigger, AT + timedelta(seconds=4)))
    assert skipped.fallback_used == "rules: model circuit open" and not skipped.consulted
    assert breaker.allows(AT + timedelta(minutes=3))  # half-open after the cooldown


def _advice_args(ctx, trigger, at):
    facts = ctx.resolver.resolve("U-3", at, trigger)
    choice = ctx.goals.choose(facts, at)
    return facts, choice, trigger, at, Budget(ctx.config.budget)


def test_degraded_provider_adds_latency_from_the_control_event():
    sched = DependencySchedule([{"at": "2026-10-01T13:10:00Z", "type": "dependency_degraded", "dependency": "user_data_store",
                                 "until": "2026-10-01T13:17:00Z", "detail": "p95 read latency 8 s and 20% of reads time out"}])
    state, window = sched.state("user_data_store", parse_ts("2026-10-01T13:12:00Z"))
    assert state == "degraded" and sched.degraded_params(window) == (8.0, 0.2)
    assert sched.state("user_data_store", parse_ts("2026-10-01T13:17:00Z"))[0] == "ok"


# ---------------------------------------------------------------- budgets and switches


def test_model_budget_of_one_means_no_retry(mini_pack):
    config = AgentConfig(goal_interpretations=INTERPRETATIONS, budget=BudgetConfig(max_model_calls=1))
    ctx, d = run(mini_pack, faulty({"fault": "malformed_output", "model_output": "{oops"}), config)
    assert d.model_calls == 1
    assert "budget" in d.fallback_used and d.status == "PUBLISHED"


def test_tool_budget_exhaustion_is_a_recorded_no_action(mini_pack):
    config = AgentConfig(goal_interpretations=INTERPRETATIONS, budget=BudgetConfig(max_tool_calls=2))
    ctx, d = run(mini_pack, None, config)
    assert d.action == "NO_ACTION" and d.no_action_reason.startswith("budget_exceeded")


def test_load_shedding_skips_the_model(mini_pack):
    provider = Scripted({"goal_id": "G03"})
    pack, ctx, trigger = build_world(mini_pack, provider=provider)
    d = ViewerAgent("U-3", ctx).handle(trigger, allow_model=False).decision
    assert provider.calls == 0 and d.model_calls == 0
    assert d.fallback_used == "rules: load shedding" and d.status == "PUBLISHED"


def test_budget_object_enforces_limits():
    b = Budget(BudgetConfig(max_model_calls=8, max_tool_calls=20))
    for _ in range(8):
        b.charge_model()
    with pytest.raises(BudgetExceeded):
        b.charge_model()
    b.charge_tool(20)
    with pytest.raises(BudgetExceeded):
        b.charge_tool()


@pytest.mark.parametrize("raw,error", [
    ('{"goal_id": "G02", "eligible": tr', "malformed"),
    ('["G02"]', "schema"),
    ('{"goal_id": "G02", "eligible": "yes", "confidence": 0.5}', "schema"),
    ('{"goal_id": "G02", "eligible": true, "confidence": 3}', "schema"),
    ('{"goal_id": "G02", "eligible": true, "confidence": 0.5, "reason": "", "evidence": "event:x"}', "schema"),
])
def test_parse_goal_assessment_rejects_bad_output(raw, error):
    assert parse_goal_assessment(raw)[1] == error


def test_payload_is_pseudonymised(mini_pack):
    seen = []
    provider = Scripted(lambda r: seen.append(r.payload) or {"goal_id": "x", "eligible": False, "confidence": 0, "reason": "", "evidence": []})
    run(mini_pack, provider)
    text = json.dumps(seen[0])
    assert "U-3" not in text and "description" in text
    assert "SYSTEM NOTE" not in text and "pre-approved" not in text  # no catalogue text reaches the model
