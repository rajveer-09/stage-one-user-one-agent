"""Catalogue: titles, episodes, availability, withdrawals and the instruction scanner.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from ..clock import parse_ts

INSTRUCTION_PATTERNS: dict[str, str] = {
    "system_note": r"\bsystem\s+(note|prompt|message|instruction)s?\b",
    "bracketed_role": r"\[\s*(assistant|system|developer|admin)\b[^\]]*\]",
    "addresses_ai": r"\b(for|to)\s+(ai|llm)\s+(agents?|models?|assistants?)\b|\bas an ai\b",
    "ignore_rules": r"\b(ignore|disregard|override|bypass)\s+(all\s+|any\s+|your\s+|the\s+)*(previous\s+|prior\s+|above\s+)?(rules|instructions|polic(y|ies)|checks)\b",
    "mass_action": r"\b(message|notify|send|email|push|whatsapp)\b[^.]{0,80}\b(every|all)\s+(users?|viewers?|customers?)\b",
    "mark_eligible": r"\bmark\s+(every|all)\s+(users?|viewers?)\b",
    "skip_checks": r"\bskip\s+(all\s+|the\s+|any\s+)?(checks|verification|validation|rules)\b",
    "pre_approved": r"\bpre-?approved\b",
    "role_change": r"\byou\s+(are|must|should)\s+now\b",
}
_COMPILED = {name: re.compile(p, re.I) for name, p in INSTRUCTION_PATTERNS.items()}
SCANNED_FIELDS = ("title", "description", "genres")


@dataclass(frozen=True, slots=True)
class InjectionFlag:
    title_id: str
    field: str
    pattern: str
    excerpt: str


def scan_text(text: str) -> list[tuple[str, str]]:
    """Return (pattern_name, excerpt) for every instruction-like match in text."""
    hits = []
    for name, rx in _COMPILED.items():
        m = rx.search(text)
        if m:
            lo, hi = max(0, m.start() - 40), min(len(text), m.end() + 40)
            hits.append((name, text[lo:hi].strip()))
    return hits


@dataclass(frozen=True, slots=True)
class Title:
    title_id: str
    content_slug: str
    title: str
    dialect: str
    language: str
    content_type: str
    genres: tuple[str, ...]
    release_date: date | None
    available_from: datetime | None
    available_until: datetime | None  # None = no end
    is_premium: bool
    description: str

    @property
    def ref(self) -> str:
        return f"catalogue:{self.title_id}"

    @property
    def is_series(self) -> bool:
        return self.content_type in ("show", "micro_drama")


@dataclass(frozen=True, slots=True)
class Episode:
    episode_id: str
    content_slug: str
    season: int
    episode: int
    minutes: int


# Reasons a title cannot be used; None means usable.
UNKNOWN, QUARANTINED, WITHDRAWN = "unknown_title", "quarantined", "withdrawn"
NOT_YET, ENDED, ENDS_BEFORE = "not_yet_available", "availability_ended", "ends_before_until"


class Catalogue:
    def __init__(self, titles: list[Title], episodes: list[Episode]) -> None:
        self.titles: dict[str, Title] = {t.title_id: t for t in titles}
        self.by_slug: dict[str, list[Title]] = defaultdict(list)
        self.by_dialect: dict[str, list[Title]] = defaultdict(list)
        for t in titles:
            self.by_slug[t.content_slug].append(t)
            self.by_dialect[t.dialect].append(t)
        self.episodes_by_slug: dict[str, list[Episode]] = defaultdict(list)
        for e in sorted(episodes, key=lambda e: (e.content_slug, e.season, e.episode)):
            self.episodes_by_slug[e.content_slug].append(e)
        self.episode_by_id = {e.episode_id: e for e in episodes}
        self.withdrawn: dict[str, tuple[datetime, str]] = {}
        self.flags: list[InjectionFlag] = []
        for t in titles:
            for field_name in SCANNED_FIELDS:
                value = getattr(t, field_name)
                text = "|".join(value) if isinstance(value, tuple) else value
                for pattern, excerpt in scan_text(text):
                    self.flags.append(InjectionFlag(t.title_id, field_name, pattern, excerpt))
        self.quarantined: set[str] = {f.title_id for f in self.flags}

    def dialects(self) -> set[str]:
        return set(self.by_dialect)

    def withdraw(self, title_id: str, at: datetime, reason: str) -> None:
        """Record a withdrawal (control event). Takes effect from `at`; earlier queries are unaffected."""
        self.withdrawn[title_id] = (at, reason)

    def check(self, title_id: str, at: datetime, until: datetime | None = None) -> str | None:
        """Why the title can't be shown/promoted at `at` (and through `until`, if given); None if usable."""
        t = self.titles.get(title_id)
        if t is None:
            return UNKNOWN
        if title_id in self.quarantined:
            return QUARANTINED
        w = self.withdrawn.get(title_id)
        if w is not None and at >= w[0]:
            return WITHDRAWN
        if t.available_from is not None and at < t.available_from:
            return NOT_YET
        if t.available_until is not None:
            if at >= t.available_until:
                return ENDED
            if until is not None and until > t.available_until:
                return ENDS_BEFORE
        return None

    def usable(self, title_id: str, at: datetime, until: datetime | None = None) -> bool:
        return self.check(title_id, at, until) is None

    def episodes(self, content_slug: str) -> list[Episode]:
        return self.episodes_by_slug.get(content_slug, [])


def _bool(text: str) -> bool:
    return text.strip().lower() in ("true", "1", "yes")


def load_catalogue(catalogue_csv: Path, episodes_csv: Path) -> Catalogue:
    with catalogue_csv.open(encoding="utf-8-sig", newline="") as fh:
        titles = [
            Title(
                title_id=r["title_id"],
                content_slug=r["content_slug"],
                title=r["title"],
                dialect=r["dialect"],
                language=r["language"],
                content_type=r["content_type"],
                genres=tuple(g for g in r["genres"].split("|") if g),
                release_date=date.fromisoformat(r["release_date"]) if r["release_date"] else None,
                available_from=parse_ts(r["available_from"]),
                available_until=parse_ts(r["available_until"]),
                is_premium=_bool(r["is_premium"]),
                description=r["description"],
            )
            for r in csv.DictReader(fh)
        ]
    with episodes_csv.open(encoding="utf-8-sig", newline="") as fh:
        episodes = [
            Episode(r["episode_id"], r["content_slug"], int(r["season"]), int(r["episode"]), int(r["minutes"]))
            for r in csv.DictReader(fh)
        ]
    return Catalogue(titles, episodes)
