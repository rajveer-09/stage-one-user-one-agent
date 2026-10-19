"""Message planning: at most one push / WhatsApp / in-app message per decision, or none.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from ..clock import ist_day_key, next_send_time, to_iso
from ..pack.catalogue import Catalogue
from ..pack.interventions import Policy
from ..state.facts import TRIGGER, ViewerFacts
from .goal_planner import GoalChoice
from .homepage import HomePagePlan

MESSAGE_GOALS = {"G02", "G03", "G04", "G07"}
UNUSABLE_TEMPLATES = {"wa_recommend_title_v1": "needs dialect_name, which the catalogue does not provide (D05)"}

PUSH_COPY = {
    "en": {
        "next_episode": ("{title}", "Episode {episode} of {title} is ready for you."),
        "recommend": ("{title}", "Picked for you: {title}. Tap to watch."),
    },
    "hin": {
        "next_episode": ("{title}", "{title} ka episode {episode} aapke liye taiyaar hai."),
        "recommend": ("{title}", "Aapke liye: {title}. Dekhne ke liye tap kariye."),
    },
}
IN_APP_COPY = {
    "en": "Keep watching after your trial: {days} days for ₹{price}.",
    "hin": "Trial ke baad bhi dekhte rahiye: {days} din ke liye ₹{price}.",
}


@dataclass
class MessageContext:
    store_ok: bool
    messages_on: Callable[[str], int]  # ist_date -> push+WhatsApp already scheduled/sent by the agent
    pending_messages: int
    ignored_channels: frozenset[str] = frozenset()  # learned: recent messages on these were delivered but never opened


@dataclass
class MessagePlan:
    channel: str
    send_at: datetime
    title_id: str | None = None
    text: dict[str, str] | None = None
    template_id: str | None = None
    variables: dict[str, str] | None = None
    deeplink: str | None = None
    condition: str = ""
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out = {"channel": self.channel, "send_at": to_iso(self.send_at), "condition": self.condition}
        for key in ("title_id", "text", "template_id", "variables", "deeplink"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


class MessagePlanner:
    def __init__(self, policy: Policy, catalogue: Catalogue, delay_h: float) -> None:
        self.policy = policy
        self.catalogue = catalogue
        self.delay = timedelta(hours=delay_h)

    def plan(self, facts: ViewerFacts, goal: GoalChoice, page: HomePagePlan | None, at: datetime,
             ctx: MessageContext) -> tuple[MessagePlan | None, str]:
        if goal.goal_id not in MESSAGE_GOALS:
            return None, "home page alone is proportionate for this goal"
        if not ctx.store_ok:
            return None, "user data store unavailable: frequency cap and duplicates cannot be verified"
        dialect = facts.fact("dialect_seen")
        if dialect is None or dialect.value is None or dialect.source != TRIGGER:
            return None, "dialect not confirmed by the current session"
        sources = facts.get("dialect_sources") or {}
        if len({d for d in sources.values() if d}) >= 3:
            return None, "app, profile and campaign name three different dialects: no outbound message"
        if ctx.pending_messages:
            return None, "a message is already scheduled for this viewer"

        send_at = next_send_time(at + self.delay, self.policy.push.quiet_start, self.policy.push.quiet_end)
        if goal.deadline is None or send_at >= goal.deadline - timedelta(hours=1):
            return None, "no send time outside quiet hours before the goal deadline"
        day = ist_day_key(send_at)
        already = ctx.messages_on(day) + (facts.get("messages_delivered_today", 0) if day == ist_day_key(at) else 0)
        if already >= self.policy.messages_per_ist_day:
            return None, f"frequency cap reached for IST day {day}"

        language = facts.get("app_language") or "en"
        condition = f"send only if {goal.goal_id} has no success signal by send_at; re-verified at send time"
        if goal.goal_id == "G04":
            return self._trial(facts, send_at, language, condition)
        return self._title_message(facts, goal, page, at, send_at, language, condition, ctx.ignored_channels)

    def _title_message(self, facts, goal, page, at, send_at, language, condition, ignored=frozenset()):
        title_id, episode = self._subject(goal, page)
        if title_id is None:
            return None, "no title to message about"
        if not (self.catalogue.usable(title_id, at) and self.catalogue.usable(title_id, send_at)):
            return None, f"{title_id} not usable at send time"
        title = self.catalogue.titles[title_id]
        evidence = [title.ref]
        if facts.get("consent_push") and "push" not in ignored:
            kind = "next_episode" if episode else "recommend"
            head, body = PUSH_COPY.get(language, PUSH_COPY["en"])[kind]
            text = {"title": head.format(title=title.title), "body": body.format(title=title.title, episode=episode)}
            if len(text["title"]) > self.policy.push.title_max_chars or len(text["body"]) > self.policy.push.body_max_chars:
                return None, "push copy would exceed the length limits (names are never truncated)"
            return MessagePlan("push", send_at, title_id, text=text, deeplink=self.policy.push.deeplink(title_id),
                               condition=condition, evidence=evidence + list(facts.fact("consent_push").refs)), "ok"
        if facts.get("consent_whatsapp") and episode and "whatsapp" not in ignored:
            template = self._template("wa_new_episode_v2", language)
            if template is not None:
                return MessagePlan("whatsapp", send_at, title_id, template_id=template.template_id,
                                   variables={"title_name": title.title, "episode_number": str(episode)},
                                   condition=condition, evidence=evidence + list(facts.fact("consent_whatsapp").refs)), "ok"
        return None, ("no consented channel with suitable content"
                      + (f" (learned: viewer ignores {', '.join(sorted(ignored))})" if ignored else ""))

    def _trial(self, facts, send_at, language, condition):
        plan = self._renewal_plan()
        if plan is None:
            return None, "no paid plan in pricing.json"
        evidence = [f"pricing:{plan.plan_id}"]
        if facts.get("consent_whatsapp"):
            template = self._template("wa_trial_ending_v1", language)
            if template is not None:
                return MessagePlan("whatsapp", send_at, template_id=template.template_id,
                                   variables={"plan_price": f"₹{plan.price_inr}"}, condition=condition,
                                   evidence=evidence + list(facts.fact("consent_whatsapp").refs)), "ok"
        body = IN_APP_COPY.get(language, IN_APP_COPY["en"]).format(days=plan.days, price=plan.price_inr)
        return MessagePlan("in_app_message", send_at, text={"body": body}, condition=condition, evidence=evidence), "ok"

    def _subject(self, goal: GoalChoice, page: HomePagePlan | None) -> tuple[str | None, int | None]:
        if goal.goal_id == "G07" and goal.eligibility is not None:
            detail = goal.eligibility.detail
            ep = self.catalogue.episode_by_id.get(detail.get("next_episode_id"))
            title_id = page.hero_title_id if page and page.hero_title_id and \
                self.catalogue.titles[page.hero_title_id].content_slug == detail.get("content_slug") else detail.get("title_id")
            return title_id, ep.episode if ep else None
        if page is not None and page.hero_title_id:
            return page.hero_title_id, None
        return None, None

    def _template(self, template_id: str, language: str):
        template = self.policy.whatsapp.templates.get(template_id)
        if template is None or template_id in UNUSABLE_TEMPLATES or template.language != language:
            return None
        return template

    def _renewal_plan(self):
        plans = self.policy.plans
        trial = next((p for p in plans.values() if p.renews_to), None)
        if trial is not None and trial.renews_to in plans:
            return plans[trial.renews_to]
        paid = [p for p in plans.values() if p.price_inr > 1]
        return min(paid, key=lambda p: p.price_inr) if paid else None
