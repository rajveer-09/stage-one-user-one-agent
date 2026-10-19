"""Typed agent configuration loaded from config/agent.toml.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Budget:
    max_model_calls: int = 8
    max_tool_calls: int = 20
    model_timeout_s: float = 10
    store_timeout_s: float = 2
    decision_soft_deadline_ms: int = 20000
    homepage_fast_path_ms: int = 1000


@dataclass(frozen=True)
class Latency:
    publish_budget_s: float = 60
    failure_after_s: float = 300


@dataclass(frozen=True)
class Freshness:
    stale_trigger_after_s: float = 900
    snapshot_max_age_h: float = 48


@dataclass(frozen=True)
class Replay:
    workers: int = 8
    load_shed_queue_wait_s: float = 20


@dataclass(frozen=True)
class Decision:
    min_confidence_to_act: float = 0.4


@dataclass(frozen=True)
class Planning:
    goal_priority: tuple[str, ...] = ("G01", "G03", "G02", "G04", "G07", "G12", "G06", "G09", "G08", "G05")
    requires_activation: tuple[str, ...] = ("G05",)
    rows_per_page: int = 4
    titles_per_row: int = 8
    new_release_days: int = 30
    message_delay_h: float = 24


@dataclass(frozen=True)
class Model:
    provider: str = "mock"
    seed: str = "oua-practice"
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "low"


@dataclass(frozen=True)
class AgentConfig:
    goal_interpretations: Path = Path("config/goal_interpretations.toml")
    budget: Budget = field(default_factory=Budget)
    latency: Latency = field(default_factory=Latency)
    freshness: Freshness = field(default_factory=Freshness)
    replay: Replay = field(default_factory=Replay)
    decision: Decision = field(default_factory=Decision)
    planning: Planning = field(default_factory=Planning)
    model: Model = field(default_factory=Model)


def _section(cls: type, raw: dict[str, Any], name: str) -> Any:
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"config [{name}]: unknown keys {sorted(unknown)}")
    return cls(**{k: tuple(v) if isinstance(v, list) else v for k, v in raw.items()})


def load_config(path: Path | None = None) -> AgentConfig:
    """Load agent.toml; missing file or sections fall back to defaults. Paths resolve against the repo root."""
    if path is None or not path.exists():
        return AgentConfig()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    root = path.resolve().parent.parent  # config/agent.toml -> repo root
    interp = Path(raw.get("paths", {}).get("goal_interpretations", "config/goal_interpretations.toml"))
    return AgentConfig(
        goal_interpretations=interp if interp.is_absolute() else root / interp,
        budget=_section(Budget, raw.get("budget", {}), "budget"),
        latency=_section(Latency, raw.get("latency", {}), "latency"),
        freshness=_section(Freshness, raw.get("freshness", {}), "freshness"),
        replay=_section(Replay, raw.get("replay", {}), "replay"),
        decision=_section(Decision, raw.get("decision", {}), "decision"),
        planning=_section(Planning, raw.get("planning", {}), "planning"),
        model=_section(Model, raw.get("model", {}), "model"),
    )
