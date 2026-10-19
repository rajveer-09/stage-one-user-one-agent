"""Home page builder: one hero and up to N rows, in the dialect the app will request.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from ..clock import to_iso
from ..pack.catalogue import Catalogue
from ..pack.events import Event
from ..pack.interventions import HomePageRules
from ..state.facts import ViewerFacts
from .goal_planner import GoalChoice

SHORT_EPISODE_MINUTES = 20  # "finish a whole episode" is easier when the first episode is short

ROW_RECIPES: dict[str, tuple[str, ...]] = {
    "G01": ("popular_in_dialect", "genre", "new_releases", "because_you_watched"),
    "G02": ("popular_in_dialect", "genre", "new_releases", "because_you_watched"),
    "G03": ("popular_in_dialect", "because_you_watched", "new_releases", "genre"),
    "G07": ("next_episode", "because_you_watched", "popular_in_dialect", "new_releases"),
    "G08": ("new_releases", "popular_in_dialect", "because_you_watched", "genre"),
    "G09": ("because_you_watched", "popular_in_dialect", "new_releases", "genre"),
    "G12": ("because_you_watched", "popular_in_dialect", "new_releases", "genre"),
}
DEFAULT_RECIPE = ("popular_in_dialect", "new_releases", "because_you_watched", "genre")


@dataclass
class Row:
    row_type: str
    title_ids: list[str]


@dataclass
class HomePagePlan:
    dialect: str
    hero_title_id: str | None
    rows: list[Row]
    publish_at: datetime
    until: datetime
    dropped_rows: list[dict[str, Any]] = field(default_factory=list)

    def title_ids(self) -> list[str]:
        ids = [self.hero_title_id] if self.hero_title_id else []
        return ids + [t for r in self.rows for t in r.title_ids]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dialect": self.dialect,
            "hero_title_id": self.hero_title_id,
            "rows": [{"row_type": r.row_type, "title_ids": r.title_ids} for r in self.rows],
            "publish_at": to_iso(self.publish_at),
            "live_until": to_iso(self.until),
            "dropped_rows": self.dropped_rows,
        }


class ContentIndex:
    """Popularity and lookup helpers over the catalogue, computed once from pre-replay history."""

    def __init__(self, catalogue: Catalogue, history: Iterable[Event], cutoff: datetime | None) -> None:
        self.catalogue = catalogue
        viewers: dict[str, set[str]] = defaultdict(set)
        for e in history:
            if e.event != "play_started" or (cutoff is not None and e.received_at >= cutoff):
                continue
            title = catalogue.titles.get(str(e.properties.get("title_id")))
            if title is not None:
                viewers[title.content_slug].add(e.user_id)
        self.popularity = Counter({slug: len(users) for slug, users in viewers.items()})
        self._by_dialect: dict[str, list[str]] = {}
        for dialect, titles in catalogue.by_dialect.items():
            slugs = sorted({t.content_slug for t in titles})
            self._by_dialect[dialect] = sorted(slugs, key=lambda s: (-self.popularity[s], s))

    def popular_slugs(self, dialect: str) -> list[str]:
        return list(self._by_dialect.get(dialect, []))

    def slug_genres(self, slug: str) -> set[str]:
        return {g for t in self.catalogue.by_slug.get(slug, []) for g in t.genres}

    def slug_release(self, slug: str):
        dates = [t.release_date for t in self.catalogue.by_slug.get(slug, []) if t.release_date]
        return max(dates) if dates else None

    def first_episode_minutes(self, slug: str) -> int | None:
        eps = self.catalogue.episodes(slug)
        return eps[0].minutes if eps else None

    def best_variant(self, slug: str, language: str | None, at: datetime, until: datetime) -> str | None:
        usable = [t for t in self.catalogue.by_slug.get(slug, []) if self.catalogue.usable(t.title_id, at, until)]
        if not usable:
            return None
        usable.sort(key=lambda t: (t.language != language, t.title_id))
        return usable[0].title_id


class HomePageBuilder:
    def __init__(self, catalogue: Catalogue, index: ContentIndex, rules: HomePageRules, rows_per_page: int,
                 titles_per_row: int, new_release_days: int) -> None:
        self.catalogue = catalogue
        self.index = index
        self.rules = rules
        self.rows_per_page = min(rows_per_page, rules.max_rows)
        self.titles_per_row = max(rules.min_titles_per_row, min(titles_per_row, rules.max_titles_per_row))
        self.new_release_days = new_release_days

    def build(self, facts: ViewerFacts, goal: GoalChoice, at: datetime) -> tuple[HomePagePlan | None, str]:
        dialect = facts.get("dialect_seen")
        if not dialect or dialect not in self.catalogue.dialects():
            return None, f"insufficient_content_in_dialect: no catalogue titles in {dialect or 'an unknown dialect'}"
        until = at + timedelta(minutes=self.rules.ttl_minutes)
        language = facts.get("app_language")
        candidates = self._candidates(facts, goal, dialect, at)
        used: set[str] = set()

        hero = None
        for slug in self._hero_order(goal, candidates, dialect):
            title_id = self.index.best_variant(slug, language, at, until)
            if title_id:
                hero = title_id
                used.add(slug)
                break

        rows: list[Row] = []
        dropped: list[dict[str, Any]] = []
        for row_type in ROW_RECIPES.get(goal.goal_id or "", DEFAULT_RECIPE):
            if row_type not in self.rules.row_types:
                dropped.append({"row_type": row_type, "reason": "row type not allowed by interventions.json"})
                continue
            if len(rows) >= self.rows_per_page:
                break
            picked, picked_slugs = [], []
            for slug in candidates.get(row_type, []):
                if slug in used or slug in picked_slugs:
                    continue
                title_id = self.index.best_variant(slug, language, at, until)
                if title_id:
                    picked.append(title_id)
                    picked_slugs.append(slug)
                if len(picked) >= self.titles_per_row:
                    break
            if len(picked) < self.rules.min_titles_per_row:
                dropped.append({"row_type": row_type, "valid_titles": len(picked),
                                "reason": f"fewer than {self.rules.min_titles_per_row} valid titles"})
                continue
            used.update(picked_slugs)
            rows.append(Row(row_type, picked))

        if not rows:
            return None, "insufficient_content_in_dialect: no row reached the minimum of valid titles"
        return HomePagePlan(dialect, hero, rows, at, until, dropped), "ok"

    def _candidates(self, facts: ViewerFacts, goal: GoalChoice, dialect: str, at: datetime) -> dict[str, list[str]]:
        popular = self.index.popular_slugs(dialect)
        series = facts.get("series_progress") or {}
        finished_series = {slug for slug, s in series.items() if not s.get("next_episode_id")}
        played_films = {slug for slug in facts.get("played_slugs") or []
                        if slug in self.catalogue.by_slug and not self.catalogue.by_slug[slug][0].is_series}
        exclude = finished_series | played_films  # nothing already watched is re-promoted
        discover = [s for s in popular if s not in exclude and s not in series]

        in_progress = [slug for slug, s in sorted(series.items(), key=lambda kv: kv[1].get("last_play_at") or at, reverse=True)
                       if s.get("next_episode_id") and self.catalogue.by_slug[slug][0].dialect == dialect]
        top_genres = list(facts.get("top_genres") or [])
        cutoff = (at - timedelta(days=self.new_release_days)).date()
        new = sorted((s for s in discover if (self.index.slug_release(s) or cutoff) > cutoff),
                     key=lambda s: (self.index.slug_release(s), self.index.popularity[s]), reverse=True)
        genre = top_genres[0] if top_genres else self._top_genre(dialect)
        if goal.goal_id == "G02":
            discover.sort(key=lambda s: (self.index.first_episode_minutes(s) or 999) > SHORT_EPISODE_MINUTES)
        return {
            "next_episode": in_progress,
            "because_you_watched": [s for s in discover if self.index.slug_genres(s) & set(top_genres)] if top_genres else [],
            "popular_in_dialect": discover,
            "new_releases": new,
            "genre": [s for s in discover if genre in self.index.slug_genres(s)] if genre else [],
        }

    def _hero_order(self, goal: GoalChoice, candidates: dict[str, list[str]], dialect: str) -> list[str]:
        first: list[str] = []
        if goal.goal_id == "G07" and goal.eligibility is not None:
            first.append(goal.eligibility.detail.get("content_slug"))
        if goal.goal_id == "G08":
            first += candidates["new_releases"][:5]
        if goal.goal_id in ("G04", "G05", "G06"):
            first += [s for s in candidates["popular_in_dialect"] if any(t.is_premium for t in self.catalogue.by_slug[s])][:5]
        return [s for s in first if s] + candidates["popular_in_dialect"]

    def _top_genre(self, dialect: str) -> str | None:
        counts: Counter = Counter()
        for slug in self.index.popular_slugs(dialect)[:50]:
            counts.update({g: self.index.popularity[slug] for g in self.index.slug_genres(slug)})
        return counts.most_common(1)[0][0] if counts else None
