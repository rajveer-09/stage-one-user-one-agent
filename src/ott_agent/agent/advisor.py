"""Goal advisor: the only place the agent consults a model, and how it falls back.

Author: Rajveer Sharma
Date: 19 Oct 2026
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..clock import to_iso
from ..config import Budget as BudgetConfig
from ..pack.events import Event
from ..models.provider import GOAL_ASSESSMENT, MALFORMED, SCHEMA, ModelProvider, ModelRequest
from ..pack.goals import GoalRegistry
from ..state.facts import ViewerFacts
from .budget import Budget, BudgetExceeded
from .goal_planner import GoalChoice

MAX_ATTEMPTS = 2  # first try + one retry for malformed/schema-invalid output


@dataclass
class AdviceOutcome:
    consulted: bool = False
    goal_id: str | None = None
    accepted_claim: dict[str, Any] | None = None
    rejected: list[dict[str, Any]] = field(default_factory=list)
    fallback_used: str | None = None
    note: str = ""


class CircuitBreaker:
    """Stops calling a failing provider for a while (virtual time), so outages don't cost latency per decision."""

    def __init__(self, threshold: int = 3, cooldown_s: float = 120) -> None:
        self.threshold = threshold
        self.cooldown = timedelta(seconds=cooldown_s)
        self.failures = 0
        self.open_until: datetime | None = None

    def allows(self, at: datetime) -> bool:
        return self.open_until is None or at >= self.open_until

    def record(self, ok: bool, at: datetime) -> None:
        if ok:
            self.failures, self.open_until = 0, None
            return
        self.failures += 1
        if self.failures >= self.threshold:
            self.open_until = at + self.cooldown


class GoalAdvisor:
    def __init__(self, provider: ModelProvider, cfg: BudgetConfig, registry: GoalRegistry) -> None:
        self.provider = provider
        self.cfg = cfg
        self.registry = registry
        self.breaker = CircuitBreaker()

    def consult(self, facts: ViewerFacts, choice: GoalChoice, trigger: Event, at: datetime, budget: Budget,
                allow: bool = True) -> AdviceOutcome:
        eligible = [c for c in choice.candidates if c.eligible]
        if len(eligible) < 2:
            return AdviceOutcome(note="model not consulted: only one eligible goal, nothing to decide")
        if not allow:
            return AdviceOutcome(note="model skipped: load shedding (rules-only mode)", fallback_used="rules: load shedding")
        if not self.breaker.allows(at):
            return AdviceOutcome(note=f"model skipped: circuit open until {to_iso(self.breaker.open_until)}",
                                 fallback_used="rules: model circuit open")

        payload = self._payload(facts, eligible, trigger, at)
        known_refs = {r for c in payload["candidates"] for r in c["evidence"]}
        out = AdviceOutcome(consulted=True)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                budget.charge_model()
            except BudgetExceeded as exc:
                out.fallback_used = f"rules: {exc}"
                return out
            result = self.provider.complete(ModelRequest(GOAL_ASSESSMENT, payload, trigger.event_id, at, self.cfg.model_timeout_s))
            budget.add_latency(result.latency_ms)
            if not result.ok:
                self.breaker.record(False, at)
                out.rejected.append({"attempt": attempt, "error": result.error, "detail": result.detail,
                                     "raw_excerpt": (result.raw or "")[:120] or None})
                if result.error in (MALFORMED, SCHEMA) and attempt < MAX_ATTEMPTS:
                    continue
                out.fallback_used = f"rules: model {result.error} ({result.detail})"
                return out

            self.breaker.record(True, at)
            claim = dict(result.output or {})
            problem = self._validate(claim, eligible, known_refs)
            if problem:
                out.rejected.append({"attempt": attempt, "error": "unsupported_claim", "detail": problem, "claim": claim})
                out.fallback_used = "rules: model claim rejected (unsupported_claim)"
                return out
            out.goal_id = claim["goal_id"]
            out.accepted_claim = {**claim, "accepted": True, "provider": result.provider}
            out.note = ("model agreed with the rule choice" if claim["goal_id"] == choice.goal_id
                        else f"model preferred {claim['goal_id']} over {choice.goal_id} among eligible goals")
            return out
        return out

    @staticmethod
    def _validate(claim: dict[str, Any], eligible: list, known_refs: set[str]) -> str | None:
        ids = {c.goal_id for c in eligible}
        if not claim.get("eligible"):
            return "model did not mark its chosen goal as eligible"
        if claim.get("goal_id") not in ids:
            return (f"model says the viewer is eligible for {claim.get('goal_id')}, but the rule checks found "
                    f"only {sorted(ids)} eligible; no event or profile fact supports the claim")
        cited = [r for r in claim.get("evidence", []) if r.startswith(("event:", "profile:"))]
        if not cited:
            return "claim cites no event or profile evidence"
        unknown = [r for r in cited if r not in known_refs]
        if unknown:
            return f"claim cites refs that were not provided: {unknown[:3]}"
        return None

    def _payload(self, facts: ViewerFacts, eligible: list, trigger: Event, at: datetime) -> dict[str, Any]:
        """Pseudonymised, derived features only: no user id, no free text from events or the catalogue."""
        base_refs = [trigger.ref] + list((facts.fact("profile_row").refs if facts.fact("profile_row") else ()))
        signup = facts.get("signup_at")
        return {
            "task": "Choose the single goal that best fits this viewer now.",
            "viewer": {
                "subscription_status": facts.get("subscription_status"),
                "days_since_signup": (facts.at - signup).days if signup else None,
                "has_played": facts.get("has_played"),
                "has_completed_episode": facts.get("has_completed_episode"),
                "play_days_last_14": facts.get("play_days_14d"),
                "unfinished_series": sum(1 for s in (facts.get("series_progress") or {}).values() if s.get("next_episode_id")),
                "new_viewer": facts.get("is_new_viewer"),
            },
            "candidates": [
                {"goal_id": c.goal_id,
                 "description": self.registry.current(c.goal_id, at).description,
                 "evidence": list(dict.fromkeys(c.evidence + base_refs))}
                for c in eligible
            ],
        }
