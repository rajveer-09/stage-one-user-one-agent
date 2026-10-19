"""Independent verifier: re-derives the facts and checks a decision before anything is published.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..pack.events import Event
from ..pack.loader import Pack
from ..state.resolver import FactResolver
from ..state.store import StateStore
from . import checks
from .checks import Finding


@dataclass
class VerificationResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(f.ok for f in self.findings)

    @property
    def passed(self) -> list[str]:
        failed = {f.check for f in self.findings if not f.ok}
        return sorted({f.check for f in self.findings if f.ok} - failed)

    @property
    def failed(self) -> list[str]:
        return [f"{f.check} [{f.component}]: {f.reason}" for f in self.findings if not f.ok]

    def failed_components(self) -> set[str]:
        return {f.component for f in self.findings if not f.ok}


class Verifier:
    def __init__(self, pack: Pack, resolver: FactResolver, store: StateStore) -> None:
        self.pack = pack
        self.resolver = resolver
        self.store = store

    def verify(self, decision: dict, trigger: Event, at: datetime, publish_at: datetime) -> VerificationResult:
        result = VerificationResult()
        if decision["action"] == "NO_ACTION":
            result.findings.append(checks.ok("no_action"))
            return result
        facts = self.resolver.resolve(decision["viewer_id"], at, trigger)
        index = self.resolver.index
        f = result.findings
        f += checks.check_holdout(self.pack, decision)
        f += checks.check_duplicate(self.store, decision)
        f += checks.check_goal(self.pack, self.store, facts, decision, at)
        f += checks.check_evidence(self.pack, index, decision, at)
        f += checks.check_model_claims(self.pack, facts, index, decision, at)
        page = decision.get("home_page")
        if page:
            f += checks.check_page(self.pack, facts, page, publish_at)
        message = decision.get("message")
        if message:
            f += checks.check_message(self.pack, self.store, facts, message, page["dialect"] if page else None, at)
        return result
