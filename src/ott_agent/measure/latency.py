"""Latency report: how fast decisions were published, overall and in each stress window.

Author: Rajveer Sharma
Date: 19 Oct 2026
"""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from ..clock import parse_ts
from ..pack.loader import Pack


def _pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


def _stats(rows: list[dict[str, Any]], budget_s: float, fail_s: float) -> dict[str, Any]:
    lat = [r["latency_s"] for r in rows]
    dec = [r["decided_ms"] for r in rows]
    return {
        "n": len(rows),
        "trigger_to_publish_s": {"p50": _pct(lat, .5), "p95": _pct(lat, .95), "p99": _pct(lat, .99), "max": max(lat, default=float("nan"))},
        "decided_in_ms": {"p50": _pct(dec, .5), "p95": _pct(dec, .95), "max": max(dec, default=float("nan"))},
        "queue_wait_ms_max": max((r["queue_ms"] for r in rows), default=0.0),
        "within_budget_pct": 100 * sum(l <= budget_s for l in lat) / len(lat) if lat else None,
        "over_failure_threshold": sum(l > fail_s for l in lat),
    }


def latency_report(decisions: list[dict[str, Any]], pack: Pack, budget_s: float, fail_s: float,
                   budgets: dict[str, int]) -> dict[str, Any]:
    rows = []
    for d in decisions:
        if not d.get("published_at"):
            continue
        deliver = parse_ts(d["trigger"]["deliver_at"])
        rows.append({"deliver": deliver, "latency_s": (parse_ts(d["published_at"]) - deliver).total_seconds(),
                     "decided_ms": d["decided_in_ms"] or 0.0, "queue_ms": d.get("queue_wait_ms", 0.0), "d": d})

    windows: dict[str, tuple[datetime, datetime]] = {}
    for c in pack.control_events:
        if c["type"] in ("dependency_unavailable", "dependency_degraded") and c.get("until"):
            windows[f"{c['type']}: {c.get('dependency')}"] = (parse_ts(c["at"]), parse_ts(c["until"]))
    per10 = Counter(t.arrival.replace(minute=t.arrival.minute // 10 * 10, second=0, microsecond=0) for t in pack.triggers)
    if per10:
        median = statistics.median(per10.values())
        for start, n in sorted(per10.items()):
            if n > 2 * median:
                windows[f"traffic peak {start:%H:%M}Z ({n} triggers vs median {median:.0f})"] = (start, start + timedelta(minutes=10))

    by_window = {name: _stats([r for r in rows if lo <= r["deliver"] < hi], budget_s, fail_s) for name, (lo, hi) in windows.items()}
    ds = [r["d"] for r in rows]
    return {
        "budget_s": budget_s,
        "failure_threshold_s": fail_s,
        "overall": _stats(rows, budget_s, fail_s),
        "windows": by_window,
        "calls_per_decision": {
            "model_calls": dict(Counter(d["model_calls"] for d in ds)),
            "tool_calls_max": max((d["tool_calls"] for d in ds), default=0),
            "limits": budgets,
            "budget_exceeded": sum(1 for d in ds if (d.get("no_action_reason") or "").startswith("budget_exceeded")),
        },
        "fallbacks": dict(Counter((d.get("fallback_used") or "").split(" (")[0] for d in ds if d.get("fallback_used"))),
        "degraded_mode": dict(Counter((d.get("degraded_mode") or "").split(";")[0] for d in ds if d.get("degraded_mode"))),
        "status": dict(Counter(d["status"] for d in ds)),
    }
