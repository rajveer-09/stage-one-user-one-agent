"""Versioned goal registry with a measurability check and written interpretations.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ..clock import parse_ts


@dataclass(frozen=True, slots=True)
class SuccessRule:
    event: str | None
    condition: str
    window_hours: int


@dataclass(frozen=True, slots=True)
class GoalRule:
    goal_id: str
    name: str
    description: str
    eligibility: dict[str, Any]
    success: SuccessRule
    version: int = 1
    effective_at: datetime | None = None  # None = from the start
    source: str = "goals.json"
    note: str = ""

    @property
    def ref(self) -> str:
        return f"goal_rule:{self.goal_id}@v{self.version}"


@dataclass(frozen=True, slots=True)
class Assumption:
    id: str
    goal: str
    rule: str
    interpretation: str
    why: str

    @property
    def ref(self) -> str:
        return f"assumption:{self.id}"


@dataclass
class GoalStatus:
    goal_id: str
    enabled: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class CorrectionResult:
    goal_id: str
    old_rule: GoalRule
    new_rule: GoalRule
    applies_to: str
    warnings: list[str]


class GoalRegistry:
    def __init__(
        self,
        rules: list[GoalRule],
        vocabulary: set[str],
        explicit_disabled: dict[str, str] | None = None,
        assumptions: list[Assumption] | None = None,
    ) -> None:
        self.vocabulary = set(vocabulary)
        self.explicit_disabled = dict(explicit_disabled or {})
        self.assumptions = list(assumptions or [])
        self._versions: dict[str, list[GoalRule]] = {r.goal_id: [r] for r in rules}
        self.status: dict[str, GoalStatus] = {}
        for goal_id in self._versions:
            self._refresh_status(goal_id)

    def goal_ids(self) -> list[str]:
        return sorted(self._versions)

    def current(self, goal_id: str, at: datetime | None = None) -> GoalRule:
        """The rule version in force at `at` (latest version if at is None)."""
        versions = self._versions[goal_id]
        if at is None:
            return versions[-1]
        in_force = [v for v in versions if v.effective_at is None or v.effective_at <= at]
        return in_force[-1]

    def versions(self, goal_id: str) -> list[GoalRule]:
        return list(self._versions[goal_id])

    def is_enabled(self, goal_id: str) -> bool:
        return self.status[goal_id].enabled

    def enabled_ids(self) -> list[str]:
        return [g for g in self.goal_ids() if self.is_enabled(g)]

    def assumptions_for(self, goal_id: str) -> list[Assumption]:
        return [a for a in self.assumptions if a.goal in (goal_id, "*")]

    def apply_correction(self, control: dict[str, Any]) -> CorrectionResult:
        """Add a new rule version from a goal_rule_corrected control event."""
        goal_id = control["goal_id"]
        at = parse_ts(control["at"])
        old = self._versions[goal_id][-1]
        warnings = []
        old_spec = control.get("old_success") or {}
        if old_spec.get("event") != old.success.event:
            warnings.append(f"old_success.event {old_spec.get('event')!r} != current {old.success.event!r}")
        if old_spec.get("condition") and old_spec.get("condition") != old.success.condition:
            warnings.append(f"old_success.condition wording differs from goals.json: {old_spec.get('condition')!r}")
        new_spec = control.get("new_success") or {}
        new = replace(
            old,
            success=SuccessRule(
                event=new_spec.get("event", old.success.event),
                condition=new_spec.get("condition", old.success.condition),
                window_hours=int(new_spec.get("window_hours", old.success.window_hours)),
            ),
            version=old.version + 1,
            effective_at=at,
            source=f"control:goal_rule_corrected@{control['at']}",
            note=control.get("note", ""),
        )
        self._versions[goal_id].append(new)
        self._refresh_status(goal_id)
        return CorrectionResult(goal_id, old, new, control.get("applies_to", ""), warnings)

    def _refresh_status(self, goal_id: str) -> None:
        rule = self._versions[goal_id][-1]
        reasons = []
        if goal_id in self.explicit_disabled:
            reasons.append(self.explicit_disabled[goal_id])
        if rule.success.event is None:
            reasons.append("unmeasurable_success_signal: success.event is null")
        elif rule.success.event not in self.vocabulary:
            reasons.append(f"unmeasurable_success_signal: '{rule.success.event}' never observed in the event stream")
        self.status[goal_id] = GoalStatus(goal_id, enabled=not reasons, reasons=reasons)


def load_goals(goals_json: Path, interpretations_toml: Path | None, vocabulary: set[str]) -> GoalRegistry:
    data = json.loads(goals_json.read_text(encoding="utf-8"))
    version_label = data.get("version", "")
    rules = []
    for g in data["goals"]:
        s = g.get("success") or {}
        rules.append(
            GoalRule(
                goal_id=g["goal_id"],
                name=g.get("name", g["goal_id"]),
                description=g.get("description", ""),
                eligibility=dict(g.get("eligibility") or {}),
                success=SuccessRule(s.get("event"), s.get("condition", ""), int(s.get("window_hours", 0))),
                source=f"goals.json@{version_label}" if version_label else "goals.json",
            )
        )
    disabled: dict[str, str] = {}
    assumptions: list[Assumption] = []
    if interpretations_toml is not None and interpretations_toml.exists():
        interp = tomllib.loads(interpretations_toml.read_text(encoding="utf-8"))
        for goal_id, spec in (interp.get("disabled") or {}).items():
            disabled[goal_id] = f"{spec.get('reason', 'disabled')}: {spec.get('detail', '')}".strip(": ")
        for a in interp.get("assumption") or []:
            assumptions.append(Assumption(a["id"], a["goal"], a.get("rule", ""), a["interpretation"], a.get("why", "")))
    return GoalRegistry(rules, vocabulary, disabled, assumptions)
