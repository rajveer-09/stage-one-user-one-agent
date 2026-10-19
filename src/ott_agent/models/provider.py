"""Model provider interface and the one task the agent asks a model to do.

Author: Rajveer Sharma
Date: 19 Oct 2026
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

GOAL_ASSESSMENT = "goal_assessment"

# Output contract for goal_assessment (also sent to real models as a JSON schema).
GOAL_ASSESSMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal_id": {"type": "string"},
        "eligible": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["goal_id", "eligible", "confidence", "reason", "evidence"],
    "additionalProperties": False,
}

TIMEOUT, UNAVAILABLE, MALFORMED, SCHEMA, REFUSAL = "timeout", "unavailable", "malformed", "schema", "refusal"


@dataclass(frozen=True)
class ModelRequest:
    task: str
    payload: dict[str, Any]
    trigger_event_id: str
    at: datetime          # decision time (virtual); used for fault and outage windows
    timeout_s: float


@dataclass
class ModelResult:
    ok: bool
    output: dict[str, Any] | None = None
    raw: str | None = None
    latency_ms: float = 0.0
    error: str | None = None
    detail: str = ""
    provider: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


class ModelProvider(Protocol):
    name: str

    def complete(self, request: ModelRequest) -> ModelResult: ...


def parse_goal_assessment(raw: str | dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None, str]:
    """Safely parse and schema-check a goal assessment. Returns (output, error_code, detail)."""
    if raw is None:
        return None, MALFORMED, "empty output"
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, MALFORMED, f"invalid JSON ({exc.msg} at char {exc.pos})"
    else:
        data = raw
    if not isinstance(data, dict):
        return None, SCHEMA, "output is not a JSON object"
    if not isinstance(data.get("goal_id"), str) or not data["goal_id"]:
        return None, SCHEMA, "goal_id missing or not a string"
    if not isinstance(data.get("eligible"), bool):
        return None, SCHEMA, "eligible missing or not a boolean"
    confidence = data.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        return None, SCHEMA, "confidence missing or outside 0..1"
    evidence = data.get("evidence", [])
    if not isinstance(evidence, list) or not all(isinstance(r, str) for r in evidence):
        return None, SCHEMA, "evidence must be a list of strings"
    return {"goal_id": data["goal_id"], "eligible": data["eligible"], "confidence": float(confidence),
            "reason": str(data.get("reason", "")), "evidence": list(evidence)}, None, ""
