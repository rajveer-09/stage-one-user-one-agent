"""Deterministic mock model: the default provider, so evaluators need no API key.

Author: Rajveer Sharma
Date: 19 Oct 2026
"""

from __future__ import annotations

import hashlib
import json

from .provider import GOAL_ASSESSMENT, ModelRequest, ModelResult, parse_goal_assessment


class MockProvider:
    name = "mock"

    def __init__(self, seed: str = "oua-practice", min_latency_ms: int = 300, max_latency_ms: int = 1500) -> None:
        self.seed = seed
        self.min_latency_ms = min_latency_ms
        self.max_latency_ms = max_latency_ms

    def latency_ms(self, trigger_event_id: str) -> float:
        h = int(hashlib.sha256(f"{self.seed}:{trigger_event_id}".encode()).hexdigest()[:8], 16)
        return float(self.min_latency_ms + h % (self.max_latency_ms - self.min_latency_ms + 1))

    def complete(self, request: ModelRequest) -> ModelResult:
        latency = self.latency_ms(request.trigger_event_id)
        if request.task != GOAL_ASSESSMENT:
            return ModelResult(False, error="schema", detail=f"mock does not handle task {request.task!r}",
                               latency_ms=latency, provider=self.name)
        candidates = request.payload.get("candidates") or []
        if not candidates:
            return ModelResult(False, error="schema", detail="no candidates", latency_ms=latency, provider=self.name)
        top = candidates[0]
        refs = list(top.get("evidence", []))
        cited = [r for r in refs if r.startswith(("event:", "profile:"))][:3] + [r for r in refs if r.startswith("goal_rule:")][:1]
        raw = json.dumps({
            "goal_id": top["goal_id"],
            "eligible": True,
            "confidence": 0.7,
            "reason": f"mock: agrees with the highest-priority eligible goal {top['goal_id']}",
            "evidence": cited,
        })
        output, error, detail = parse_goal_assessment(raw)
        return ModelResult(error is None, output, raw, latency, error, detail, self.name)
