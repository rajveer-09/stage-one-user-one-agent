"""Reverse indexes over published actions, so a change touches only what it affects.

Author: Rajveer Sharma
Date: 19 Oct 2026
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from ..clock import parse_ts

MESSAGE_TYPES = ("push", "whatsapp", "in_app_message")
LIVE, SUPERSEDED, SCHEDULED, SENT, CANCELLED = "live", "superseded", "scheduled", "sent", "cancelled"


class ActionIndex:
    def __init__(self, page_ttl_minutes: int) -> None:
        self.ttl = timedelta(minutes=page_ttl_minutes)
        self.actions: dict[str, dict[str, Any]] = {}
        self.status: dict[str, str] = {}
        self.latest_page: dict[str, str] = {}
        self.pages_by_title: dict[str, set[str]] = defaultdict(set)
        self.messages_by_title: dict[str, set[str]] = defaultdict(set)
        self.messages_by_user: dict[str, set[str]] = defaultdict(set)
        self.messages_by_decision: dict[str, set[str]] = defaultdict(set)

    def add(self, action: dict[str, Any]) -> None:
        aid, user = action["action_id"], action["user_id"]
        self.actions[aid] = action
        if action["type"] == "home_page":
            previous = self.latest_page.get(user)
            if previous is not None and self.status.get(previous) == LIVE:
                self.status[previous] = SUPERSEDED
            self.latest_page[user] = aid
            self.status[aid] = LIVE
            ids = [action.get("hero_title_id")] + [t for r in action.get("rows", []) for t in r["title_ids"]]
            for t in filter(None, ids):
                self.pages_by_title[t].add(aid)
        elif action["type"] in MESSAGE_TYPES:
            self.status[aid] = SCHEDULED
            self.messages_by_user[user].add(aid)
            self.messages_by_decision[action["decision_id"]].add(aid)
            if action.get("title_id"):
                self.messages_by_title[action["title_id"]].add(aid)

    def live_pages_with(self, title_id: str, at: datetime) -> list[dict[str, Any]]:
        out = []
        for aid in sorted(self.pages_by_title.get(title_id, ())):
            a = self.actions[aid]
            if self.status.get(aid) == LIVE and parse_ts(a["published_at"]) + self.ttl > at:
                out.append(a)
        return out

    def pending(self, ids) -> list[dict[str, Any]]:
        return [self.actions[a] for a in sorted(ids) if self.status.get(a) == SCHEDULED]

    def pending_for_user(self, user_id: str) -> list[dict[str, Any]]:
        return self.pending(self.messages_by_user.get(user_id, ()))

    def pending_for_title(self, title_id: str) -> list[dict[str, Any]]:
        return self.pending(self.messages_by_title.get(title_id, ()))

    def pending_for_decision(self, decision_id: str) -> list[dict[str, Any]]:
        return self.pending(self.messages_by_decision.get(decision_id, ()))

    def all_pending(self) -> list[dict[str, Any]]:
        return [self.actions[a] for a, s in sorted(self.status.items()) if s == SCHEDULED]

    def set_status(self, action_id: str, status: str) -> None:
        self.status[action_id] = status

    def effective_actions(self) -> list[dict[str, Any]]:
        """What actually reached viewers (or will): every page that went live, and messages not cancelled."""
        out = [a for aid, a in self.actions.items() if self.status.get(aid) != CANCELLED]
        return sorted(out, key=lambda a: (a["published_at"], a["action_id"]))
