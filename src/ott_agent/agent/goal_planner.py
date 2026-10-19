"""Goal planner: evaluate every enabled goal from resolved facts and choose one, or none.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from ..clock import to_iso
from ..pack.catalogue import Catalogue
from ..pack.goals import GoalRegistry, GoalRule
from ..state.facts import ViewerFacts

# Absence facts that requires_no_prior_event can be checked against.
ABSENCE_FACTS = {"play_started": "has_played", "episode_completed": "has_completed_episode"}
PAYING_STATUSES = ("active",)


@dataclass
class Eligibility:
    goal_id: str
    eligible: bool
    reason: str
    evidence: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = {"goal_id": self.goal_id, "eligible": self.eligible, "reason": self.reason}
        if self.assumptions:
            out["assumptions"] = self.assumptions
        return out


@dataclass
class GoalChoice:
    rule: GoalRule | None
    eligibility: Eligibility | None
    candidates: list[Eligibility]
    start: datetime | None = None
    deadline: datetime | None = None
    no_goal_reason: str | None = None
    continuing: bool = False

    @property
    def goal_id(self) -> str | None:
        return self.rule.goal_id if self.rule else None

    def to_dict(self) -> dict[str, Any]:
        if self.rule is None:
            return {"id": None, "no_goal_reason": self.no_goal_reason,
                    "candidates": [c.to_dict() for c in self.candidates]}
        return {
            "id": self.rule.goal_id,
            "name": self.rule.name,
            "rule_ref": self.rule.ref,
            "start": to_iso(self.start) if self.start else None,
            "deadline": to_iso(self.deadline) if self.deadline else None,
            "success_signal": {"event": self.rule.success.event, "condition": self.rule.success.condition},
            "judge_at": to_iso(self.deadline) if self.deadline else None,
            "continuing": self.continuing,
            "target": dict(self.eligibility.detail) if self.eligibility and self.eligibility.detail else None,
            "candidates": [c.to_dict() for c in self.candidates],
        }


def _fact_refs(facts: ViewerFacts, *names: str) -> list[str]:
    refs: list[str] = []
    for name in names:
        f = facts.fact(name)
        if f is None:
            continue
        refs += list(f.refs) if f.refs else [f"fact:{name}={f.value}"]
    return list(dict.fromkeys(refs))


class GoalPlanner:
    def __init__(self, registry: GoalRegistry, catalogue: Catalogue, priority: tuple[str, ...],
                 requires_activation: tuple[str, ...], history_start: datetime | None) -> None:
        self.registry = registry
        self.catalogue = catalogue
        self.priority = list(priority)
        self.requires_activation = set(requires_activation)
        self.history_start = history_start
        self.extra: dict[str, Callable[[ViewerFacts, datetime, Eligibility], None]] = {
            "G02": self._g02, "G05": self._g05, "G06": self._g06, "G07": self._g07,
            "G09": self._g09, "G12": self._g12,
        }

    def ordered_goals(self) -> list[str]:
        known = [g for g in self.priority if g in self.registry.status]
        rest = [g for g in self.registry.goal_ids() if g not in known]
        return known + rest

    def choose(self, facts: ViewerFacts, at: datetime, cooldown: dict[str, str] | None = None) -> GoalChoice:
        """cooldown: goal_id -> reason, from this viewer's learnings (a goal that just failed is not retried at once)."""
        cooldown = cooldown or {}
        candidates = [Eligibility(g, False, cooldown[g]) if g in cooldown else self.evaluate(g, facts, at)
                      for g in self.ordered_goals()]
        chosen = next((c for c in candidates if c.eligible), None)
        if chosen is None:
            disabled = sum(1 for c in candidates if c.reason.startswith("disabled"))
            reason = ("no_eligible_goal: every enabled goal failed its eligibility check"
                      if disabled < len(candidates) else "no_eligible_goal: all goals disabled")
            return GoalChoice(None, None, candidates, no_goal_reason=reason)
        rule = self.registry.current(chosen.goal_id, at)
        return GoalChoice(rule, chosen, candidates, start=at, deadline=at + timedelta(hours=rule.success.window_hours))

    def choice_for(self, goal_id: str, base: GoalChoice, at: datetime) -> GoalChoice:
        """The same candidate list, but committing to another *eligible* goal (used when the model's pick is accepted)."""
        el = next(c for c in base.candidates if c.goal_id == goal_id and c.eligible)
        rule = self.registry.current(goal_id, at)
        return GoalChoice(rule, el, base.candidates, start=at, deadline=at + timedelta(hours=rule.success.window_hours))

    def evaluate(self, goal_id: str, facts: ViewerFacts, at: datetime) -> Eligibility:
        status = self.registry.status[goal_id]
        if not status.enabled:
            return Eligibility(goal_id, False, "disabled: " + status.reasons[0].split(":")[0])
        rule = self.registry.current(goal_id, at)
        el = Eligibility(goal_id, True, "eligible", evidence=[rule.ref])
        spec = rule.eligibility
        structured = {k for k in spec if k != "text"}

        if "signed_up_within_days" in spec:
            signup = facts.get("signup_at")
            days = int(spec["signed_up_within_days"])
            if signup is None:
                return self._no(el, "signup date unknown")
            if at - signup > timedelta(days=days):
                return self._no(el, f"signed up {(at - signup).days} days ago (> {days})")
            el.evidence += _fact_refs(facts, "signup_at")

        if "requires_no_prior_event" in spec:
            event = spec["requires_no_prior_event"]
            fact = ABSENCE_FACTS.get(event)
            if fact is None:
                return self._no(el, f"cannot verify absence of '{event}'")
            if facts.get(fact):
                return self._no(el, f"has a prior {event}")
            el.evidence.append(f"fact:{fact}=false")

        if "subscription_status" in spec:
            status_value = facts.get("subscription_status")
            if status_value not in spec["subscription_status"]:
                return self._no(el, f"subscription_status is {status_value!r}, needs {spec['subscription_status']}")
            el.evidence += _fact_refs(facts, "subscription_status")

        handler = self.extra.get(goal_id)
        if handler is not None:
            handler(facts, at, el)
        elif not structured:
            return self._no(el, "eligibility is text-only and has no evaluable interpretation")

        if el.eligible and goal_id in self.requires_activation and not facts.get("has_completed_episode"):
            return self._no(el, "deferred: not activated yet (no completed episode); activation comes before payment")
        if el.eligible:
            known = {a.id for a in self.registry.assumptions_for(goal_id)}
            el.assumptions += [a for a in _USED.get(goal_id, ()) if a in known and a not in el.assumptions]
        return el

    @staticmethod
    def _no(el: Eligibility, reason: str) -> Eligibility:
        el.eligible, el.reason = False, reason
        return el

    def _g02(self, facts: ViewerFacts, at: datetime, el: Eligibility) -> None:
        signup = facts.get("signup_at")
        if self.history_start is not None and (signup is None or signup < self.history_start):
            el.detail["pre_history_viewer"] = True
            el.assumptions.append("G02-a1")

    def _g05(self, facts: ViewerFacts, at: datetime, el: Eligibility) -> None:
        if facts.get("ever_paid") is not False:
            self._no(el, f"ever_paid is {facts.get('ever_paid')!r} (G05-a1)")
            return
        el.evidence += _fact_refs(facts, "ever_paid")

    def _g06(self, facts: ViewerFacts, at: datetime, el: Eligibility) -> None:
        lapsed_at = facts.get("last_lapsed_at")
        if lapsed_at is None:
            self._no(el, "no lapse date on record, recency cannot be shown (G06-a1)")
        elif at - lapsed_at > timedelta(days=30):
            self._no(el, f"lapsed {(at - lapsed_at).days} days ago (> 30, G06-a1)")
        elif facts.get("repurchased_since_lapse"):
            self._no(el, "bought again after lapsing")
        else:
            el.evidence += _fact_refs(facts, "last_lapsed_at")

    def _g07(self, facts: ViewerFacts, at: datetime, el: Eligibility) -> None:
        series = facts.get("series_progress") or {}
        stalled = [
            (slug, s) for slug, s in series.items()
            if s.get("next_episode_id") and s.get("last_play_at") and at - s["last_play_at"] >= timedelta(hours=72)
        ]
        if not stalled:
            self._no(el, "no started series with unwatched episodes left and 3+ days since last play")
            return
        slug, s = max(stalled, key=lambda kv: kv[1]["last_play_at"])  # most recently played (G07-a1)
        dialect = self.catalogue.titles[s["title_id"]].dialect if s["title_id"] in self.catalogue.titles else None
        seen = facts.get("dialect_seen")
        if dialect != seen:
            self._no(el, f"unfinished series {slug} is in {dialect}, the app shows {seen}: it can't be promoted")
            return
        blocked = {t.title_id: self.catalogue.check(t.title_id, at) for t in self.catalogue.by_slug.get(slug, [])}
        if all(blocked.values()):
            reasons = ", ".join(f"{tid} {why}" for tid, why in blocked.items())
            self._no(el, f"unfinished series {slug} can't be promoted now ({reasons})")
            return
        el.detail.update(content_slug=slug, title_id=s["title_id"], next_episode_id=s["next_episode_id"])
        el.evidence += s.get("refs", [])

    def _g09(self, facts: ViewerFacts, at: datetime, el: Eligibility) -> None:
        days = facts.get("play_days_14d", 0)
        if days > 1:
            self._no(el, f"played on {days} days in the last 14 (> 1)")
        else:
            el.evidence += _fact_refs(facts, "play_days_14d")

    def _g12(self, facts: ViewerFacts, at: datetime, el: Eligibility) -> None:
        signup = facts.get("signup_at")
        if signup is None or at - signup <= timedelta(days=14):
            self._no(el, "signed up within 14 days (or unknown): not a returning viewer (G12-a1)")
            return
        last_open = facts.get("last_app_open_at")
        if last_open is not None and at - last_open < timedelta(days=14):
            self._no(el, f"app_open {(at - last_open).days} days before the trigger (< 14)")
            return
        el.evidence += _fact_refs(facts, "last_app_open_at", "signup_at")


_USED = {
    "G03": ("G03-a1",), "G04": ("G04-a1",), "G05": ("G05-a1",), "G06": ("G06-a1",),
    "G07": ("G07-a1",), "G08": ("G08-a1",), "G09": ("G09-a1",), "G12": ("G12-a1",),
}
