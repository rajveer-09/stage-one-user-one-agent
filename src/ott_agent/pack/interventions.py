"""Intervention, messaging and pricing rules, parsed from config/interventions.json and pricing.json.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

from ..clock import parse_hhmm


@dataclass(frozen=True)
class HomePageRules:
    ttl_minutes: int
    max_hero: int
    max_rows: int
    min_titles_per_row: int
    max_titles_per_row: int
    row_types: tuple[str, ...]


@dataclass(frozen=True)
class PushRules:
    consent_field: str
    quiet_start: time
    quiet_end: time
    title_max_chars: int
    body_max_chars: int
    forbidden_phrases: tuple[str, ...]
    deeplink_format: str

    def deeplink(self, title_id: str) -> str:
        return self.deeplink_format.replace("<title_id>", title_id)


@dataclass(frozen=True)
class WhatsAppTemplate:
    template_id: str
    language: str
    variables: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class WhatsAppRules:
    consent_field: str
    quiet_start: time
    quiet_end: time
    templates: dict[str, WhatsAppTemplate]


@dataclass(frozen=True)
class InAppRules:
    max_per_day: int
    consent_required: bool


@dataclass(frozen=True)
class Plan:
    plan_id: str
    price_inr: int
    days: int
    renews_to: str | None = None


@dataclass
class Policy:
    home_page: HomePageRules
    push: PushRules
    whatsapp: WhatsAppRules
    in_app: InAppRules
    messages_per_ist_day: int  # push + WhatsApp combined
    plans: dict[str, Plan]
    notes: list[str] = field(default_factory=list)

    @property
    def allowed_prices_inr(self) -> set[int]:
        return {p.price_inr for p in self.plans.values()}


def _quiet(spec: dict[str, Any]) -> tuple[time, time]:
    start, end = spec.get("quiet_hours_ist", ["22:00", "08:00"])
    return parse_hhmm(start), parse_hhmm(end)


def load_policy(interventions_json: Path, pricing_json: Path) -> Policy:
    iv = json.loads(interventions_json.read_text(encoding="utf-8"))
    notes: list[str] = []

    hp, rows = iv["home_page"], iv["home_page"]["rows"]
    home = HomePageRules(
        ttl_minutes=int(hp["ttl_minutes"]),
        max_hero=int(hp["hero"]["max_titles"]),
        max_rows=int(rows["max_rows"]),
        min_titles_per_row=int(rows["min_titles_per_row"]),
        max_titles_per_row=int(rows["max_titles_per_row"]),
        row_types=tuple(rows["row_types"]),
    )

    p = iv["push"]
    push_quiet = _quiet(p)
    push = PushRules(
        consent_field=p["consent_field"],
        quiet_start=push_quiet[0],
        quiet_end=push_quiet[1],
        title_max_chars=int(p["copy"]["title_max_chars"]),
        body_max_chars=int(p["copy"]["body_max_chars"]),
        forbidden_phrases=tuple(s.lower() for s in p["copy"]["forbidden_phrases"]),
        deeplink_format=p["deeplink_format"],
    )

    m = re.search(r"at most (\d+)", p.get("frequency", ""), re.I)
    per_day = int(m.group(1)) if m else 1
    if not m:
        notes.append("push.frequency not parseable; using the strictest cap of 1 per IST day")

    w = iv["whatsapp"]
    wa_quiet = _quiet(w)
    whatsapp = WhatsAppRules(
        consent_field=w["consent_field"],
        quiet_start=wa_quiet[0],
        quiet_end=wa_quiet[1],
        templates={
            t["template_id"]: WhatsAppTemplate(t["template_id"], t["language"], tuple(t["variables"]), t["text"])
            for t in w["approved_templates"]
        },
    )

    ia = iv.get("in_app_message", {})
    in_app = InAppRules(
        max_per_day=int(ia.get("max_per_day", 1)),
        consent_required=str(ia.get("consent", "")).strip().lower() not in ("not required", "none", "no"),
    )

    pricing = json.loads(pricing_json.read_text(encoding="utf-8"))
    plans = {
        pl["plan_id"]: Plan(pl["plan_id"], int(pl["price_inr"]), int(pl["days"]), pl.get("renews_to"))
        for pl in pricing["plans"]
    }
    return Policy(home, push, whatsapp, in_app, per_day, plans, notes)
