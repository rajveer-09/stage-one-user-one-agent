"""Load and validate the data pack into typed, ordered structures.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..clock import parse_ts, to_iso
from .catalogue import Catalogue, load_catalogue
from .events import DedupStats, Event, ReadStats, dedup_events, read_events
from .goals import GoalRegistry, load_goals
from .interventions import Policy, load_policy

MAX_MALFORMED_SHARE = 0.01
ARMS = ("agent", "holdout")
FAULT_TYPES = ("unsupported_claim", "malformed_output", "timeout")
CONTROL_TYPES = ("title_withdrawn", "goal_rule_corrected", "dependency_unavailable", "dependency_degraded")

FILES = {
    "events": "data/events.jsonl",
    "profiles": "data/profiles_snapshot.csv",
    "catalogue": "data/catalogue.csv",
    "episodes": "data/episodes.csv",
    "goals": "config/goals.json",
    "interventions": "config/interventions.json",
    "pricing": "config/pricing.json",
    "triggers": "replay/triggers.jsonl",
    "live_events": "replay/live_events.jsonl",
    "control_events": "replay/control_events.jsonl",
    "model_faults": "replay/model_faults.jsonl",
    "arms": "replay/arms.csv",
}
REQUIRED_COLUMNS = {
    "profiles": ("user_id", "signup_at", "subscription_status", "preferred_dialect", "notifications_opt_in",
                 "whatsapp_opt_in", "updated_at"),
    "catalogue": ("title_id", "content_slug", "title", "dialect", "language", "content_type", "genres",
                  "release_date", "available_from", "available_until", "is_premium", "description"),
    "episodes": ("episode_id", "content_slug", "season", "episode", "minutes"),
    "arms": ("user_id", "arm"),
}


class PackSchemaError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProfileRow:
    """One row of the daily warehouse export, typed but not yet resolved (duplicates are kept)."""

    user_id: str
    signup_at: datetime | None
    subscription_status: str | None
    plan_started_at: datetime | None
    plan_ends_at: datetime | None
    preferred_dialect: str | None
    preferred_language: str | None
    notifications_opt_in: bool | None  # None = unknown / unparseable
    whatsapp_opt_in: bool | None
    platform: str | None
    updated_at: datetime | None
    snapshot_at: datetime | None
    row_number: int

    @property
    def ref(self) -> str:
        stamp = to_iso(self.updated_at) if self.updated_at else f"row{self.row_number}"
        return f"profile:{self.user_id}@{stamp}"


@dataclass
class Pack:
    root: Path
    history: list[Event]
    history_dedup: DedupStats
    profiles: list[ProfileRow]
    catalogue: Catalogue
    goals: GoalRegistry
    policy: Policy
    arms: dict[str, str]
    triggers: list[Event]
    live_events: list[Event]
    control_events: list[dict[str, Any]]
    model_faults: dict[str, dict[str, Any]]
    read_stats: dict[str, ReadStats]
    warnings: list[str] = field(default_factory=list)

    @property
    def replay_window(self) -> tuple[datetime, datetime] | None:
        if not self.triggers:
            return None
        times = [t.arrival for t in self.triggers]
        return min(times), max(times)

    def summary(self) -> dict[str, Any]:
        unique_triggers = len({t.event_id for t in self.triggers})
        profile_users = Counter(p.user_id for p in self.profiles)
        window = self.replay_window
        return {
            "history": {
                "lines": self.read_stats["events"].lines,
                "malformed": self.read_stats["events"].malformed,
                "unique": self.history_dedup.unique,
                "duplicates": self.history_dedup.duplicates,
                "payload_conflicts": self.history_dedup.payload_conflicts,
                "users": len({e.user_id for e in self.history}),
                "received_from": to_iso(self.history[0].received_at) if self.history else None,
                "received_to": to_iso(self.history[-1].received_at) if self.history else None,
            },
            "profiles": {
                "rows": len(self.profiles),
                "users": len(profile_users),
                "users_with_duplicate_rows": sum(1 for n in profile_users.values() if n > 1),
            },
            "catalogue": {
                "titles": len(self.catalogue.titles),
                "slugs": len(self.catalogue.by_slug),
                "dialects": sorted(self.catalogue.dialects()),
                "episodes": len(self.catalogue.episode_by_id),
                "quarantined": sorted(self.catalogue.quarantined),
            },
            "goals": {
                "enabled": self.goals.enabled_ids(),
                "disabled": {g: s.reasons for g, s in self.goals.status.items() if not s.enabled},
                "assumptions": [a.id for a in self.goals.assumptions],
            },
            "replay": {
                "triggers": len(self.triggers),
                "unique_triggers": unique_triggers,
                "duplicate_triggers": len(self.triggers) - unique_triggers,
                "window": [to_iso(window[0]), to_iso(window[1])] if window else None,
                "live_events": len(self.live_events),
                "control_events": dict(Counter(c["type"] for c in self.control_events)),
                "model_faults": dict(Counter(f["fault"] for f in self.model_faults.values())),
                "arms": dict(Counter(self.arms.values())),
            },
            "warnings": self.warnings,
        }



def _path(root: Path, key: str) -> Path:
    path = root / FILES[key]
    if not path.exists():
        raise PackSchemaError(f"missing pack file: {FILES[key]} (pack root {root})")
    return path


def _read_csv(path: Path, required: tuple[str, ...]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        columns = reader.fieldnames or []
        missing = [c for c in required if c not in columns]
        if missing:
            raise PackSchemaError(f"{path.name}: missing columns {missing}; found {columns}")
        return list(reader)


def _read_jsonl_events(path: Path, source: str, stats: ReadStats) -> list[Event]:
    with path.open(encoding="utf-8") as fh:
        events = list(read_events(fh, source, stats, path.name))
    if stats.lines and stats.malformed / stats.lines > MAX_MALFORMED_SHARE:
        raise PackSchemaError(
            f"{path.name}: {stats.malformed}/{stats.lines} records malformed (schema drift?); first: {stats.errors[:3]}"
        )
    return events


def _read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    out = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise PackSchemaError(f"{path.name}:{lineno}: {exc.msg}") from exc
    return out


def _bool_or_none(text: str | None) -> bool | None:
    t = (text or "").strip().lower()
    return True if t == "true" else False if t == "false" else None


def _blank_to_none(text: str | None) -> str | None:
    return text.strip() if text and text.strip() else None


def _profiles(path: Path) -> list[ProfileRow]:
    rows = _read_csv(path, REQUIRED_COLUMNS["profiles"])
    return [
        ProfileRow(
            user_id=r["user_id"],
            signup_at=parse_ts(r.get("signup_at")),
            subscription_status=_blank_to_none(r.get("subscription_status")),
            plan_started_at=parse_ts(r.get("plan_started_at")),
            plan_ends_at=parse_ts(r.get("plan_ends_at")),
            preferred_dialect=_blank_to_none(r.get("preferred_dialect")),
            preferred_language=_blank_to_none(r.get("preferred_language")),
            notifications_opt_in=_bool_or_none(r.get("notifications_opt_in")),
            whatsapp_opt_in=_bool_or_none(r.get("whatsapp_opt_in")),
            platform=_blank_to_none(r.get("platform")),
            updated_at=parse_ts(r.get("updated_at")),
            snapshot_at=parse_ts(r.get("snapshot_at")),
            row_number=i,
        )
        for i, r in enumerate(rows, start=2)  # row 1 is the header
    ]



def load_pack(root: Path, interpretations: Path | None = None) -> Pack:
    root = Path(root)
    if not root.is_dir():
        raise PackSchemaError(f"pack folder not found: {root}")
    warnings: list[str] = []
    stats = {k: ReadStats() for k in ("events", "triggers", "live_events")}

    history, dedup = dedup_events(_read_jsonl_events(_path(root, "events"), "history", stats["events"]))
    triggers = _read_jsonl_events(_path(root, "triggers"), "trigger", stats["triggers"])
    live = _read_jsonl_events(_path(root, "live_events"), "live", stats["live_events"])
    for name, events in (("triggers", triggers), ("live_events", live)):
        undelivered = [e.event_id for e in events if e.deliver_at is None]
        if undelivered:
            raise PackSchemaError(f"{FILES[name]}: {len(undelivered)} records without deliver_at")
    triggers.sort(key=lambda e: e.arrival)  # stable: equal deliver_at keeps file order
    live.sort(key=lambda e: e.arrival)

    profiles = _profiles(_path(root, "profiles"))
    for key in ("catalogue", "episodes"):
        _read_csv(_path(root, key), REQUIRED_COLUMNS[key])
    catalogue = load_catalogue(_path(root, "catalogue"), _path(root, "episodes"))

    vocabulary = {e.event for e in history} | {e.event for e in live}
    goals = load_goals(_path(root, "goals"), interpretations, vocabulary)
    policy = load_policy(_path(root, "interventions"), _path(root, "pricing"))
    warnings += policy.notes

    arms: dict[str, str] = {}
    for r in _read_csv(_path(root, "arms"), REQUIRED_COLUMNS["arms"]):
        arm = r["arm"].strip().lower()
        if arm not in ARMS:
            warnings.append(f"arms.csv: unknown arm {arm!r} for {r['user_id']}; user treated as no-action")
            continue
        arms[r["user_id"]] = arm

    controls = _read_jsonl_dicts(_path(root, "control_events"))
    for c in controls:
        if "at" not in c or "type" not in c:
            raise PackSchemaError(f"control_events.jsonl: record without at/type: {c}")
        if c["type"] not in CONTROL_TYPES:
            warnings.append(f"control event type {c['type']!r} is not known; it will be logged and ignored")
    controls.sort(key=lambda c: parse_ts(c["at"]))

    faults: dict[str, dict[str, Any]] = {}
    for f in _read_jsonl_dicts(_path(root, "model_faults")):
        if f.get("fault") not in FAULT_TYPES:
            warnings.append(f"model fault type {f.get('fault')!r} is not known")
        faults[f["trigger_event_id"]] = f

    return Pack(root, history, dedup, profiles, catalogue, goals, policy, arms, triggers, live,
                controls, faults, stats, warnings)
