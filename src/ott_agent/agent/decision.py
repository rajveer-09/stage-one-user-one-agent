"""The decision record: one per trigger, machine-readable, including "do nothing".

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..clock import to_iso
from ..pack.events import Event
from ..state.facts import ViewerFacts
from .goal_planner import GoalChoice
from .homepage import HomePagePlan
from .messaging import MessagePlan

NO_ACTION = "NO_ACTION"
DRAFT, PUBLISHED, NOT_PUBLISHED, DUPLICATE = "DRAFT", "PUBLISHED", "NO_ACTION", "DUPLICATE_IGNORED"
HOLDOUT = "HOLDOUT_NO_ACTION"


def decision_id_for(trigger_event_id: str) -> str:
    """Deterministic: the same trigger always maps to the same decision id."""
    return "D-" + hashlib.sha1(trigger_event_id.encode()).hexdigest()[:10]


@dataclass
class Decision:
    decision_id: str
    user_id: str
    arm: str | None
    trigger: Event
    at: datetime
    facts: ViewerFacts | None = None
    goal: GoalChoice | None = None
    page: HomePagePlan | None = None
    message: MessagePlan | None = None
    no_action_reason: str | None = None
    notes: list[str] = field(default_factory=list)
    confidence: float | None = None
    confidence_reason: str = ""
    checks: dict[str, list[str]] = field(default_factory=lambda: {"passed": [], "failed": []})
    model_calls: int = 0
    tool_calls: int = 0
    model_claims: list[dict[str, Any]] = field(default_factory=list)
    model_claims_rejected: list[dict[str, Any]] = field(default_factory=list)  # never used; kept for audit
    processing_ms: float = 0.0
    dependency_ms: float = 0.0       # simulated time waiting on the model / degraded store
    fallback_used: str | None = None
    degraded_mode: str | None = None
    decided_in_ms: float | None = None
    queue_wait_ms: float = 0.0
    published_at: datetime | None = None
    status: str = DRAFT

    @property
    def idempotency_key(self) -> str:
        return f"trigger:{self.trigger.event_id}"

    @property
    def action(self) -> str:
        parts = []
        if self.page is not None:
            parts.append("HOME_PAGE")
        if self.message is not None:
            parts.append(self.message.channel.upper())
        return "+".join(parts) if parts and self.no_action_reason is None else NO_ACTION

    def content(self) -> list[str]:
        ids = self.page.title_ids() if self.page else []
        if self.message and self.message.title_id and self.message.title_id not in ids:
            ids.append(self.message.title_id)
        return [f"title:{t}" for t in ids]

    def evidence(self) -> list[dict[str, Any]]:
        """Inspectable refs with their age at decision time."""
        ages: dict[str, float | None] = {self.trigger.ref: round(self.trigger.age_s(self.at))}
        if self.facts is not None:
            for f in self.facts.facts.values():
                for r in f.refs:
                    ages.setdefault(r, round(f.age_s) if f.age_s is not None else None)
        refs: list[str] = [self.trigger.ref]
        if self.goal is not None and self.goal.eligibility is not None:
            refs += self.goal.eligibility.evidence
        if self.facts is not None:
            for name in ("dialect_seen", "subscription_status", "consent_push", "consent_whatsapp"):
                f = self.facts.fact(name)
                if f is not None:
                    refs += list(f.refs)
        refs += [f"catalogue:{t.split(':', 1)[1]}" for t in self.content()]
        if self.message is not None:
            refs += self.message.evidence
        out = []
        for r in dict.fromkeys(refs):
            item: dict[str, Any] = {"ref": r}
            if ages.get(r) is not None:
                item["age_s"] = ages[r]
            out.append(item)
        return out

    def assumptions(self) -> list[str]:
        out = [f"viewer is in a live session: trigger is {round(self.trigger.age_s(self.at))} s old after clock-skew correction"]
        if self.facts is not None and self.facts.get("dialect_seen"):
            out.append(f"the app will request the {self.facts.get('dialect_seen')} home page (its current setting)")
        if self.page is not None:
            out.append("titles the viewer already played (films) or finished (series) are not re-promoted")
        if self.goal is not None and self.goal.eligibility is not None:
            out += [f"assumption:{a}" for a in self.goal.eligibility.assumptions]
        out += ["assumption:ALL-a1", "assumption:ALL-a2"] if self.goal is not None and self.goal.rule is not None else []
        return out

    def dialect(self) -> dict[str, Any]:
        if self.facts is None:
            return {}
        f = self.facts.fact("dialect_seen")
        conflicts = [r for c in self.facts.conflicts_for("dialect_seen") for r in c.rejected]
        out = {"used": f.value if f else None, "source": "app_setting" if f and f.source in ("trigger", "event") else None,
               "observed_via": f.source if f else None, "conflicts": conflicts}
        if conflicts:
            out["resolution"] = self.facts.conflicts_for("dialect_seen")[0].rule
        return out

    def to_dict(self) -> dict[str, Any]:
        t = self.trigger
        out: dict[str, Any] = {
            "decision_id": self.decision_id,
            "viewer_id": self.user_id,
            "arm": self.arm,
            "idempotency_key": self.idempotency_key,
            "trigger": {
                "event": t.event, "event_id": t.event_id, "received_at": to_iso(t.received_at),
                "deliver_at": to_iso(t.arrival), "event_age_s": round(t.age_s(self.at)),
                "age_basis": "received_at minus offline buffering (sent_at - occurred_at); phone clock skew removed",
            },
            "goal": self.goal.to_dict() if self.goal is not None else None,
            "action": self.action,
            "dialect": self.dialect(),
            "content": self.content(),
            "home_page": self.page.to_dict() if self.page else None,
            "message": self.message.to_dict() if self.message else None,
            "confidence": self.confidence,
            "confidence_reason": self.confidence_reason,
            "evidence": self.evidence(),
            "assumptions": self.assumptions(),
            "conflicts": [c.to_dict() for c in self.facts.conflicts] if self.facts else [],
            "no_action_reason": self.no_action_reason,
            "notes": self.notes,
            "checks": self.checks,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "model_claims": self.model_claims,
            "model_claims_rejected": self.model_claims_rejected,
            "fallback_used": self.fallback_used,
            "degraded_mode": self.degraded_mode,
            "decided_in_ms": round(self.decided_in_ms, 2) if self.decided_in_ms is not None else None,
            "latency_breakdown_ms": {"processing": round(self.processing_ms, 2), "dependencies": round(self.dependency_ms, 2)},
            "queue_wait_ms": round(self.queue_wait_ms, 2),
            "published_at": to_iso(self.published_at) if self.published_at else None,
            "status": self.status,
        }
        return out

    def to_actions(self, published_at: datetime) -> list[dict[str, Any]]:
        if self.action == NO_ACTION:
            return []
        base = {"decision_id": self.decision_id, "user_id": self.user_id, "trigger_event_id": self.trigger.event_id,
                "published_at": to_iso(published_at)}
        actions = []
        if self.page is not None:
            actions.append({"action_id": f"{self.decision_id}-A1", **base, "type": "home_page",
                            "dialect": self.page.dialect, "hero_title_id": self.page.hero_title_id,
                            "rows": [{"row_type": r.row_type, "title_ids": r.title_ids} for r in self.page.rows]})
        if self.message is not None:
            m = self.message
            action = {"action_id": f"{self.decision_id}-A{len(actions) + 1}", **base, "type": m.channel,
                      "send_at": to_iso(m.send_at)}
            if self.page is not None:
                action["dialect"] = self.page.dialect
            for key in ("title_id", "text", "deeplink", "template_id", "variables"):
                value = getattr(m, key)
                if value is not None:
                    action[key] = value
            actions.append(action)
        return actions
