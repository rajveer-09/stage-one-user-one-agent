"""Audit: what the agent knew, decided and observed for one viewer at any moment.

Author: Rajveer Sharma
Date: 19 Oct 2026
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..clock import to_iso
from ..state.store import StateStore


def explain(store: StateStore, user_id: str, at: datetime | None = None) -> dict[str, Any]:
    entries = store.journal(user_id, until=at)
    facts = [e for e in entries if e["kind"] == "facts"]
    decisions = [e["payload"] for e in entries if e["kind"] == "decision"]
    goal_events = [{"at": e["at"], **{k: v for k, v in e["payload"].items() if k in ("event", "goal_id", "id", "reason", "from", "to", "rule_ref", "deadline", "signal")}}
                   for e in entries if e["kind"] == "goal"]
    open_goal = None
    for g in goal_events:
        if g.get("event") == "opened":
            open_goal = {"goal_id": g.get("id"), "opened_at": g["at"], "deadline": g.get("deadline"), "rule": g.get("rule_ref")}
        elif g.get("event") == "rule_corrected" and open_goal:
            open_goal["rule"] = g.get("to")
        elif g.get("event") == "closed":
            open_goal = None
    return {
        "viewer_id": user_id,
        "as_of": to_iso(at) if at else "now",
        "open_goal": open_goal,
        "latest_facts": facts[-1]["payload"] if facts else None,
        "decisions": [{
            "decision_id": d["decision_id"], "at": d["trigger"]["deliver_at"], "trigger": d["trigger"]["event"],
            "status": d["status"], "action": d["action"], "goal": (d.get("goal") or {}).get("id"),
            "why_not": d.get("no_action_reason"), "confidence": d.get("confidence"),
            "confidence_reason": d.get("confidence_reason"), "checks_failed": d.get("checks", {}).get("failed"),
            "notes": d.get("notes"), "published_at": d.get("published_at"),
        } for d in decisions],
        "actions": [{"at": e["at"], **e["payload"]} for e in entries if e["kind"] == "action"],
        "goal_history": goal_events,
        "outcomes": [e["payload"] for e in entries if e["kind"] == "outcome"],
        "learnings": [e["payload"] for e in entries if e["kind"] == "learning"],
    }
