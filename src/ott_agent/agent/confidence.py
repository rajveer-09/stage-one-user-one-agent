"""Decision confidence: an evidence-quality score with a reason that names every penalty.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from __future__ import annotations

from ..state.facts import DEFAULT, DERIVED, TRIGGER, ViewerFacts
from .goal_planner import GoalChoice
from .homepage import HomePagePlan

BASE = 0.80
FLOOR, CEILING = 0.05, 0.95


def assess(facts: ViewerFacts, goal: GoalChoice, page: HomePagePlan | None,
           fallback_used: str | None = None) -> tuple[float, str]:
    penalties: list[tuple[float, str]] = []
    dialect = facts.fact("dialect_seen")
    if dialect is None or dialect.source != TRIGGER:
        penalties.append((0.15, "dialect taken from an earlier event, not this session"))
    rejected = sum(len(c.rejected) for c in facts.conflicts_for("dialect_seen"))
    if rejected:
        penalties.append((min(0.10, 0.05 * rejected), f"{rejected} other dialect source(s) disagree"))
    if facts.get("is_new_viewer"):
        penalties.append((0.10, "no profile row; facts from events only"))
    if facts.conflicts_for("profile_row"):
        penalties.append((0.05, "duplicate profile rows disagreed"))
    status = facts.fact("subscription_status")
    if status is not None and status.source in (DERIVED, DEFAULT):
        penalties.append((0.05, f"subscription status {status.source}, not observed"))
    if goal.eligibility is not None and "G02-a1" in goal.eligibility.assumptions:
        penalties.append((0.10, "completions before the 30-day history are invisible (G02-a1)"))
    if not facts.get("has_played"):
        penalties.append((0.05, "no watch history to personalise from"))
    if page is not None and len(page.rows) < 2:
        penalties.append((0.10, "thin page: one row in this dialect"))
    if fallback_used:
        penalties.append((0.05, f"fallback used: {fallback_used}"))

    score = max(FLOOR, min(CEILING, BASE - sum(p for p, _ in penalties)))
    basis = f"base {BASE:.2f}: eligibility from {goal.goal_id} rule checks on resolved facts" if goal.goal_id else f"base {BASE:.2f}"
    detail = "; ".join(f"-{p:.2f} {why}" for p, why in penalties) or "no penalties"
    return round(score, 2), f"{basis}; {detail}"
