"""Scheduled messages: re-verified when they fall due, and once more when the replay ends.

Author: Rajveer Sharma
Date: 19 Oct 2026
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from ..clock import parse_ts, to_iso
from ..verify import checks
from .indexes import CANCELLED, SENT, ActionIndex


class MessageScheduler:
    def __init__(self, ctx, index: ActionIndex, log: Callable[[dict[str, Any]], None]) -> None:
        self.ctx = ctx
        self.index = index
        self.log = log

    def problems(self, action: dict[str, Any], now: datetime) -> list[str]:
        store, user = self.ctx.store, action["user_id"]
        goal = store.active_goal(user)
        if goal is None or goal.decision_id != action["decision_id"]:
            return [f"goal from {action['decision_id']} is no longer open (achieved, invalidated or replaced)"]
        facts = self.ctx.resolver.resolve(user, now)
        msg = {"channel": action["type"], "send_at": action["send_at"]}
        for key in ("title_id", "text", "template_id", "variables", "deeplink"):
            if key in action:
                msg[key] = action[key]
        findings = checks.check_message(self.ctx.pack, store, facts, msg, action.get("dialect"), now,
                                        own_action_id=action["action_id"])
        return [f"{f.check}: {f.reason}" for f in findings if not f.ok]

    def cancel(self, action: dict[str, Any], reason: str, at: datetime) -> None:
        self.index.set_status(action["action_id"], CANCELLED)
        self.ctx.store.set_message_status(action["action_id"], "cancelled")
        self.ctx.store.append(action["user_id"], "action", at, {"event": "cancelled", "action_id": action["action_id"],
                                                                 "reason": reason}, ref=f"action:{action['action_id']}")
        self.log({"at": to_iso(at), "type": "message_cancelled", "user_id": action["user_id"],
                  "action_id": action["action_id"], "channel": action["type"], "reason": reason})

    def send_due(self, now: datetime) -> None:
        for action in self.index.all_pending():
            if parse_ts(action["send_at"]) <= now:
                issues = self.problems(action, parse_ts(action["send_at"]))
                if issues:
                    self.cancel(action, "; ".join(issues), now)
                else:
                    self.index.set_status(action["action_id"], SENT)
                    self.ctx.store.set_message_status(action["action_id"], "sent")
                    self.log({"at": action["send_at"], "type": "message_sent", "user_id": action["user_id"],
                              "action_id": action["action_id"], "channel": action["type"]})

    def finalize(self, end: datetime) -> dict[str, int]:
        """Messages due after the replay: re-check with everything known at the end; keep or cancel."""
        kept = cancelled = 0
        for action in self.index.all_pending():
            issues = self.problems(action, end)
            if issues:
                self.cancel(action, "re-check at end of replay: " + "; ".join(issues), end)
                cancelled += 1
            else:
                kept += 1
        self.ctx.store.commit()
        return {"kept_for_delivery": kept, "cancelled": cancelled}
