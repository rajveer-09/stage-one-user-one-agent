"""Facts with provenance, and the conflicts found while resolving them.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..clock import to_iso

TRIGGER, EVENT, PROFILE, DERIVED, DEFAULT = "trigger", "event", "profile", "derived", "default"


@dataclass(frozen=True, slots=True)
class Fact:
    name: str
    value: Any
    source: str
    refs: tuple[str, ...] = ()
    as_of: datetime | None = None  # when the source observed it
    age_s: float | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"value": _jsonable(self.value), "source": self.source}
        if self.refs:
            out["refs"] = list(self.refs)
        if self.as_of is not None:
            out["as_of"] = to_iso(self.as_of)
        if self.age_s is not None:
            out["age_s"] = round(self.age_s)
        if self.note:
            out["note"] = self.note
        return out


@dataclass(frozen=True, slots=True)
class Conflict:
    fact: str
    chosen: str
    rejected: tuple[str, ...]
    rule: str

    def to_dict(self) -> dict[str, Any]:
        return {"fact": self.fact, "chosen": self.chosen, "rejected": list(self.rejected), "rule": self.rule}


@dataclass
class ViewerFacts:
    user_id: str
    at: datetime
    facts: dict[str, Fact] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)

    def add(self, fact: Fact) -> Fact:
        if fact.as_of is not None and fact.age_s is None:
            fact = Fact(fact.name, fact.value, fact.source, fact.refs, fact.as_of,
                        (self.at - fact.as_of).total_seconds(), fact.note)
        self.facts[fact.name] = fact
        return fact

    def conflict(self, fact: str, chosen: str, rejected: list[str], rule: str) -> None:
        if rejected:
            self.conflicts.append(Conflict(fact, chosen, tuple(rejected), rule))

    def get(self, name: str, default: Any = None) -> Any:
        f = self.facts.get(name)
        return default if f is None else f.value

    def fact(self, name: str) -> Fact | None:
        return self.facts.get(name)

    def conflicts_for(self, name: str) -> list[Conflict]:
        return [c for c in self.conflicts if c.fact == name]

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "at": to_iso(self.at),
            "facts": {k: f.to_dict() for k, f in sorted(self.facts.items())},
            "conflicts": [c.to_dict() for c in self.conflicts],
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return to_iso(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        items = [_jsonable(v) for v in value]
        return sorted(items) if isinstance(value, set) else items
    return value
