"""Fault injection: replay model faults and dependency outages from the pack.

Author: Rajveer Sharma
Date: 19 Oct 2026
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..clock import parse_ts
from .provider import MALFORMED, TIMEOUT, UNAVAILABLE, ModelProvider, ModelRequest, ModelResult, parse_goal_assessment

OK, DOWN, DEGRADED = "ok", "unavailable", "degraded"


@dataclass(frozen=True)
class Window:
    dependency: str
    state: str
    start: datetime
    end: datetime
    detail: str

    def covers(self, at: datetime) -> bool:
        return self.start <= at < self.end


class DependencySchedule:
    def __init__(self, control_events: list[dict[str, Any]]) -> None:
        self.windows: list[Window] = []
        for c in control_events:
            if c.get("type") in ("dependency_unavailable", "dependency_degraded") and c.get("until"):
                state = DOWN if c["type"] == "dependency_unavailable" else DEGRADED
                self.windows.append(Window(c.get("dependency", "?"), state, parse_ts(c["at"]), parse_ts(c["until"]),
                                           c.get("detail", "")))

    def state(self, dependency: str, at: datetime) -> tuple[str, Window | None]:
        hits = [w for w in self.windows if w.dependency == dependency and w.covers(at)]
        if not hits:
            return OK, None
        worst = next((w for w in hits if w.state == DOWN), hits[0])
        return worst.state, worst

    def degraded_params(self, window: Window) -> tuple[float, float]:
        """(p95 latency seconds, failure share) parsed from a detail like 'p95 read latency 8 s and 20% of reads time out'."""
        p95 = re.search(r"p95[^0-9]*([\d.]+)\s*s", window.detail)
        share = re.search(r"([\d.]+)\s*%", window.detail)
        return (float(p95.group(1)) if p95 else 5.0, float(share.group(1)) / 100 if share else 0.2)


class FaultInjectingProvider:
    def __init__(self, inner: ModelProvider, faults: dict[str, dict[str, Any]], schedule: DependencySchedule) -> None:
        self.inner = inner
        self.name = f"{inner.name}+faults"
        self.faults = faults
        self.schedule = schedule
        self.exercised: dict[str, int] = {}

    def complete(self, request: ModelRequest) -> ModelResult:
        state, window = self.schedule.state("model_provider", request.at)
        if state == DOWN:
            return ModelResult(False, error=UNAVAILABLE, latency_ms=50.0, provider=self.name,
                               detail=f"model_provider unavailable until {window.end.isoformat()}: {window.detail}")
        fault = self.faults.get(request.trigger_event_id)
        if fault is None:
            result = self.inner.complete(request)
            if state == DEGRADED and window is not None:
                result.latency_ms += self.schedule.degraded_params(window)[0] * 1000
            return result

        self.exercised[request.trigger_event_id] = self.exercised.get(request.trigger_event_id, 0) + 1
        kind = fault.get("fault")
        if kind == "timeout":
            took = min(float(fault.get("seconds", request.timeout_s)), request.timeout_s) * 1000
            return ModelResult(False, error=TIMEOUT, latency_ms=took, provider=self.name,
                               detail=f"no answer within {request.timeout_s:g} s (injected: {fault.get('seconds')} s)")
        output = fault.get("model_output")
        raw = output if isinstance(output, str) else json.dumps(output)
        latency = self.inner.latency_ms(request.trigger_event_id) if hasattr(self.inner, "latency_ms") else 800.0
        parsed, error, detail = parse_goal_assessment(raw)
        if kind == "malformed_output" and error is None:  # the fault must reproduce as malformed
            error, detail, parsed = MALFORMED, "injected malformed output", None
        return ModelResult(error is None, parsed, raw, latency, error, detail, self.name, {"injected": kind})
