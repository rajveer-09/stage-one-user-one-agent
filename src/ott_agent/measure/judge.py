"""Outcome judge: succeeded / failed / inconclusive, from attributable signals only.

Author: Rajveer Sharma
Date: 19 Oct 2026
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from ..clock import ist_date, parse_ts, to_iso
from ..pack.events import Event
from ..pack.loader import Pack
from .success import AGGREGATE, distinct_days_needed, signal_matches

SUCCEEDED, FAILED, INCONCLUSIVE = "succeeded", "failed", "inconclusive"


@dataclass
class GoalCase:
    user_id: str
    arm: str
    role: str                     # itt (intended goal per viewer) | opened (any goal the agent opened)
    decision_id: str
    goal_id: str
    start: datetime
    target: dict[str, Any]
    served: bool


def apply_corrections(pack: Pack) -> None:
    """Rebuild the rule history the replay saw (idempotent: skips corrections already applied)."""
    for c in pack.control_events:
        if c["type"] == "goal_rule_corrected":
            versions = pack.goals.versions(c["goal_id"])
            if not any(v.effective_at == parse_ts(c["at"]) for v in versions):
                pack.goals.apply_correction(c)


def cases_from_decisions(decisions: list[dict[str, Any]], stale_after_s: float) -> list[GoalCase]:
    cases: list[GoalCase] = []
    seen_itt: set[str] = set()
    for d in decisions:
        if d["status"] == "DUPLICATE_IGNORED" or not d.get("goal") or not d["goal"].get("id"):
            continue
        g = d["goal"]
        start = parse_ts(g.get("start") or d["published_at"] or d["trigger"]["deliver_at"])
        fresh = d["trigger"]["event_age_s"] <= stale_after_s
        served = d["status"] == "PUBLISHED"
        if fresh and d["viewer_id"] not in seen_itt:
            seen_itt.add(d["viewer_id"])
            cases.append(GoalCase(d["viewer_id"], d["arm"] or "none", "itt", d["decision_id"], g["id"], start,
                                  g.get("target") or {}, served))
        elif served and not g.get("continuing"):
            cases.append(GoalCase(d["viewer_id"], d["arm"] or "none", "opened", d["decision_id"], g["id"], start,
                                  g.get("target") or {}, served))
    return cases


def judge(case: GoalCase, events: list[Event], pack: Pack, horizon_end: datetime,
          invalidated: dict[str, str]) -> dict[str, Any]:
    rule0 = pack.goals.current(case.goal_id, case.start)
    deadline = case.start + timedelta(hours=rule0.success.window_hours)
    out = {"viewer_id": case.user_id, "arm": case.arm, "role": case.role, "decision_id": case.decision_id,
           "goal_id": case.goal_id, "served": case.served, "start": to_iso(case.start), "deadline": to_iso(deadline)}
    if case.decision_id in invalidated:
        return {**out, "verdict": INCONCLUSIVE, "reason": invalidated[case.decision_id], "rule_ref": rule0.ref}
    status = pack.goals.status.get(case.goal_id)
    never_measurable = status is not None and not status.enabled and len(pack.goals.versions(case.goal_id)) == 1
    if never_measurable:
        return {**out, "verdict": INCONCLUSIVE, "reason": "unmeasurable success signal", "rule_ref": rule0.ref}

    days: set = set()
    day_refs: list[str] = []
    for e in events:
        if e.received_at < case.start:
            continue
        if e.received_at > deadline:
            break
        rule = pack.goals.current(case.goal_id, e.received_at)  # the rule in force when this happened
        match = signal_matches(rule, case.start, case.target, e, pack.catalogue)
        if match:
            return {**out, "verdict": SUCCEEDED, "reason": f"{e.event} at {to_iso(e.received_at)}",
                    "signal": e.ref, "signal_at": to_iso(e.received_at), "rule_ref": rule.ref}
        need = distinct_days_needed(rule) if match is AGGREGATE else None
        if need:
            days.add(ist_date(e.received_at))
            day_refs.append(e.ref)
            if len(days) >= need:
                return {**out, "verdict": SUCCEEDED, "reason": f"{e.event} on {need} distinct IST days",
                        "signal": day_refs[-need:], "signal_at": to_iso(e.received_at), "rule_ref": rule.ref}
    final = pack.goals.current(case.goal_id, min(deadline, horizon_end))
    if final.success.event is None or final.success.event not in pack.goals.vocabulary:
        return {**out, "verdict": INCONCLUSIVE, "rule_ref": final.ref,
                "reason": f"the rule in force ({final.ref}) has a success signal that is never observed"}
    if deadline > horizon_end:
        return {**out, "verdict": INCONCLUSIVE, "rule_ref": final.ref,
                "reason": f"no success yet and the outcome data ends at {to_iso(horizon_end)}, before the deadline"}
    if days:
        return {**out, "verdict": FAILED, "rule_ref": final.ref,
                "reason": f"only {len(days)} of {distinct_days_needed(final)} distinct days by the deadline"}
    return {**out, "verdict": FAILED, "rule_ref": final.ref,
            "reason": f"no {final.success.event} matching '{final.success.condition}' by the deadline"}


def judge_all(decisions: list[dict[str, Any]], outcome_events: Iterable[Event], pack: Pack, stale_after_s: float,
              control_log: list[dict[str, Any]] = ()) -> tuple[list[dict[str, Any]], datetime]:
    apply_corrections(pack)
    by_user: dict[str, list[Event]] = defaultdict(list)
    seen: set[str] = set()
    horizon_end = None
    for e in outcome_events:
        if e.event_id in seen:
            continue
        seen.add(e.event_id)
        by_user[e.user_id].append(e)
        horizon_end = e.received_at if horizon_end is None or e.received_at > horizon_end else horizon_end
    for evs in by_user.values():
        evs.sort(key=lambda e: e.received_at)
    invalidated = _invalidated(decisions, control_log)
    cases = cases_from_decisions(decisions, stale_after_s)
    horizon_end = horizon_end or datetime.max
    results = []
    for i, case in enumerate(cases, 1):
        results.append({"outcome_id": f"O-{i:05d}", **judge(case, by_user.get(case.user_id, []), pack, horizon_end, invalidated)})
    return results, horizon_end


def _invalidated(decisions: list[dict[str, Any]], control_log: Iterable[dict[str, Any]]) -> dict[str, str]:
    """decision_id -> reason, for goals the replay closed as invalidated (decided on facts that were already stale)."""
    open_by_user = {}
    for d in decisions:
        if d["status"] == "PUBLISHED" and d.get("goal") and not d["goal"].get("continuing"):
            open_by_user.setdefault(d["viewer_id"], []).append(d)
    out = {}
    for r in control_log:
        if r.get("type") == "goal_closed_by_live_event" and str(r.get("reason", "")).startswith("invalidated"):
            candidates = [d for d in open_by_user.get(r["user_id"], []) if d["goal"]["id"] == r["goal_id"]
                          and d["published_at"] <= r["at"]]
            if candidates:
                out[candidates[-1]["decision_id"]] = r["reason"]
    return out


def learnings(outcomes: list[dict[str, Any]], outcome_events: Iterable[Event],
              actions: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Per viewer: what happened to each message and goal, written back into viewer state for later decisions."""
    by_action = {a["action_id"]: a for a in actions if a["type"] in ("push", "whatsapp", "in_app_message")}
    seen: dict[str, dict[str, Any]] = {}
    for e in outcome_events:
        aid = e.properties.get("action_id")
        if aid in by_action and e.event in ("message_delivered", "message_clicked", "message_failed"):
            rec = seen.setdefault(aid, {"learning": "message_outcome", "action_id": aid,
                                        "channel": by_action[aid]["type"], "delivered": False, "clicked": False})
            if e.event == "message_delivered":
                rec["delivered"] = True
            elif e.event == "message_clicked":
                rec["clicked"] = True
            else:
                rec["failed_reason"] = e.properties.get("reason")
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for aid, rec in seen.items():
        out[by_action[aid]["user_id"]].append(rec)
    for o in outcomes:
        if o["arm"] == "agent" and o["served"]:
            out[o["viewer_id"]].append({"learning": "goal_result", "goal_id": o["goal_id"], "verdict": o["verdict"],
                                        "decision_id": o["decision_id"], "deadline": o["deadline"]})
    return out
