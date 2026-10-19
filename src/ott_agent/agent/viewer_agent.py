"""ViewerAgent: the one component that decides for a viewer.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from collections import Counter

from ..clock import ist_day_key, parse_ts
from ..config import AgentConfig
from ..pack.events import Event
from ..pack.loader import Pack
from ..state.resolver import FactResolver, build_resolver
from ..state.store import ActiveGoal, StateStore, StoreUnavailable
from ..models.claude import ClaudeProvider
from ..models.faults import DependencySchedule, FaultInjectingProvider
from ..models.mock import MockProvider
from ..models.provider import ModelProvider
from ..verify.verifier import Verifier
from . import confidence
from .advisor import GoalAdvisor
from .budget import Budget, BudgetExceeded
from .decision import DRAFT, DUPLICATE, HOLDOUT, NO_ACTION, NOT_PUBLISHED, PUBLISHED, Decision, decision_id_for
from .goal_planner import GoalChoice, GoalPlanner
from .homepage import ContentIndex, HomePageBuilder
from .messaging import MessageContext, MessagePlanner


@dataclass
class AgentContext:
    pack: Pack
    config: AgentConfig
    resolver: FactResolver
    goals: GoalPlanner
    homepage: HomePageBuilder
    messages: MessagePlanner
    store: StateStore
    verifier: Verifier
    advisor: GoalAdvisor
    dependencies: DependencySchedule
    # Replay hooks: a store-read probe (degraded store simulation) and the agent's write-through cache.
    store_probe: Callable[[datetime, str], tuple[bool, float]] | None = None
    cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    def refresh_cache(self, user_id: str, at: datetime) -> None:
        """Mirror what this process has written for a viewer, for use when a store read times out."""
        try:
            self.cache[user_id] = {
                "active": self.store.active_goal(user_id),
                "live_page": ViewerAgent(user_id, self)._live_page(at),
                "pending": len(self.store.pending_messages(user_id)),
            }
        except StoreUnavailable:
            pass

    @classmethod
    def build(cls, pack: Pack, config: AgentConfig, store: StateStore | None = None,
              include_replay_events: bool = True, provider: ModelProvider | None = None) -> "AgentContext":
        history_start = pack.history[0].received_at if pack.history else None
        window = pack.replay_window
        cutoff = window[0] if window else None
        planning = config.planning
        index = ContentIndex(pack.catalogue, pack.history, cutoff)
        store = store or StateStore()
        resolver = build_resolver(pack, config.freshness, include_replay=include_replay_events)
        dependencies = DependencySchedule(pack.control_events)
        if provider is None:
            base = (ClaudeProvider(config.model.claude_model, config.model.claude_effort)
                    if config.model.provider == "claude" else MockProvider(config.model.seed))
            provider = FaultInjectingProvider(base, pack.model_faults, dependencies)
        return cls(
            pack=pack,
            config=config,
            resolver=resolver,
            goals=GoalPlanner(pack.goals, pack.catalogue, planning.goal_priority, planning.requires_activation, history_start),
            homepage=HomePageBuilder(pack.catalogue, index, pack.policy.home_page, planning.rows_per_page,
                                     planning.titles_per_row, planning.new_release_days),
            messages=MessagePlanner(pack.policy, pack.catalogue, planning.message_delay_h),
            store=store,
            verifier=Verifier(pack, resolver, store),
            advisor=GoalAdvisor(provider, config.budget, pack.goals),
            dependencies=dependencies,
        )


MAX_VERIFY_ROUNDS = 5


@dataclass
class HandleResult:
    decision: Decision
    actions: list[dict[str, Any]] = field(default_factory=list)


class ViewerAgent:
    def __init__(self, user_id: str, ctx: AgentContext) -> None:
        self.user_id = user_id
        self.ctx = ctx

    def handle(self, trigger: Event, at: datetime | None = None, allow_model: bool = True,
               queue_wait_ms: float = 0.0) -> HandleResult:
        """Claim, decide, verify, degrade, persist. Exactly one decision record per trigger delivery."""
        started = time.perf_counter()
        at = at or trigger.arrival
        store = self.ctx.store
        decision_id = decision_id_for(trigger.event_id)
        owner = store.claim(f"trigger:{trigger.event_id}", decision_id, at)
        if owner is not None:
            arm = self.ctx.pack.arms.get(self.user_id)
            d = Decision(f"{decision_id}-dup-{int(at.timestamp())}", self.user_id, arm, trigger, at)
            d.no_action_reason = f"duplicate_trigger: already handled by {owner}; no new action"
            d.status = DUPLICATE
            d.queue_wait_ms = queue_wait_ms
            d.decided_in_ms = (time.perf_counter() - started) * 1000
            self._persist(d, at, [])
            return HandleResult(d)

        d = self.decide(trigger, at, allow_model)
        d.queue_wait_ms = queue_wait_ms
        if not allow_model and d.fallback_used is None and d.model_calls == 0:
            d.degraded_mode = d.degraded_mode or f"load shedding: queued {queue_wait_ms / 1000:.1f} s, rules only"
        failures: list[str] = []
        if d.action != NO_ACTION:
            for _ in range(MAX_VERIFY_ROUNDS):
                publish_at = at + timedelta(milliseconds=(time.perf_counter() - started) * 1000 + d.dependency_ms)
                result = self.ctx.verifier.verify(d.to_dict(), trigger, at, publish_at)
                d.checks["passed"] = result.passed
                if result.ok:
                    break
                failures += result.failed
                self._degrade(d, result.failed_components(), result.failed)
                if d.action == NO_ACTION:
                    break
            d.checks["failed"] = failures
        d.processing_ms = (time.perf_counter() - started) * 1000
        d.decided_in_ms = d.processing_ms + d.dependency_ms
        actions = self.publish(d, at + timedelta(milliseconds=d.decided_in_ms))
        return HandleResult(d, actions)

    def _degrade(self, d: Decision, components: set[str], reasons: list[str]) -> None:
        if "decision" in components:
            self._abstain(d, "verification_failed: " + "; ".join(r for r in reasons if "[decision]" in r))
            return
        if d.page is not None:
            if "page" in components:
                d.page = None
            else:
                bad_rows = sorted((int(c.split(":")[1]) for c in components if c.startswith("row:")), reverse=True)
                for i in bad_rows:
                    d.page.rows.pop(i)
                if "hero" in components:
                    d.page.hero_title_id = None
                if not d.page.rows:
                    d.page = None
        if "message" in components:
            d.message = None
        d.notes.append("verifier removed " + ", ".join(sorted(components)) + ": " + "; ".join(reasons))
        if d.page is None and d.message is None:
            self._abstain(d, "verification_failed: nothing left to publish after removing failed parts")

    def publish(self, d: Decision, published_at: datetime) -> list[dict[str, Any]]:
        """Persist the decision; for an action, open the goal (it starts when committed) and log the actions."""
        d.published_at = published_at
        actions: list[dict[str, Any]] = []
        if d.action != NO_ACTION:
            d.status = PUBLISHED
            goal = d.goal
            if goal is not None and goal.rule is not None and not goal.continuing:
                goal.start = published_at
                goal.deadline = published_at + timedelta(hours=goal.rule.success.window_hours)
                self.ctx.store.open_goal(ActiveGoal(
                    self.user_id, goal.rule.goal_id, goal.rule.version, d.decision_id, goal.start, goal.deadline,
                    {"action": d.action, "target": goal.eligibility.detail if goal.eligibility else {}}))
            actions = d.to_actions(published_at)
            for a in actions:
                if a["type"] in ("push", "whatsapp", "in_app_message"):
                    send_at = parse_ts(a["send_at"])
                    self.ctx.store.record_message(a["action_id"], self.user_id, ist_day_key(send_at), a["type"], send_at)
        self._persist(d, published_at, actions)
        return actions

    def _persist(self, d: Decision, at: datetime, actions: list[dict[str, Any]]) -> None:
        store = self.ctx.store
        if d.facts is not None:
            store.append(self.user_id, "facts", at, d.facts.to_dict(), ref=f"decision:{d.decision_id}")
        store.append(self.user_id, "decision", at, d.to_dict(), ref=f"decision:{d.decision_id}")
        if d.status == PUBLISHED and d.goal is not None and d.goal.rule is not None and not d.goal.continuing:
            store.append(self.user_id, "goal", at, {"event": "opened", **d.goal.to_dict()}, ref=d.goal.rule.ref)
        for a in actions:
            store.append(self.user_id, "action", at, a, ref=f"action:{a['action_id']}")
        store.commit()

    def decide(self, trigger: Event, at: datetime | None = None, allow_model: bool = True) -> Decision:
        started = time.perf_counter()
        at = at or trigger.arrival
        arm = self.ctx.pack.arms.get(self.user_id)
        d = Decision(decision_id_for(trigger.event_id), self.user_id, arm, trigger, at)
        budget = Budget(self.ctx.config.budget)
        try:
            self._decide(d, trigger, at, budget, allow_model)
        except BudgetExceeded as exc:
            d.fallback_used = f"budget: {exc}"
            self._abstain(d, f"budget_exceeded: {exc}")
        finally:
            d.model_calls, d.tool_calls = budget.model_calls, budget.tool_calls
            d.processing_ms = (time.perf_counter() - started) * 1000
            d.dependency_ms = budget.simulated_ms
            d.decided_in_ms = d.processing_ms + d.dependency_ms
        return d

    def _decide(self, d: Decision, trigger: Event, at: datetime, budget: Budget, allow_model: bool) -> None:
        ctx = self.ctx
        budget.charge_tool()
        d.facts = ctx.resolver.resolve(self.user_id, at, trigger)

        if d.arm != "agent":
            d.goal = ctx.goals.choose(d.facts, at)  # shadow goal: what the agent would have pursued
            return self._abstain(d, "holdout: randomized holdout arm receives no action" if d.arm == "holdout"
                                 else "no_arm: viewer missing from arms.csv", status=HOLDOUT)

        age = trigger.age_s(at)
        if age > ctx.config.freshness.stale_trigger_after_s:
            return self._abstain(d, f"stale_trigger: app opened {age / 3600:.1f} h ago (offline-buffered); not a live session")

        budget.charge_tool(3)
        read_ok, read_ms = ctx.store_probe(at, trigger.event_id) if ctx.store_probe else (True, 0.0)
        budget.add_latency(read_ms)
        try:
            if not read_ok:
                raise StoreUnavailable("read timed out")
            active = ctx.store.active_goal(self.user_id)
            live_page = self._live_page(at)
            pending = len(ctx.store.pending_messages(self.user_id))
            cooldown, ignored = self._learned(at)
            store_ok = True
        except StoreUnavailable as exc:
            if not ctx.store.available or ctx.store_probe is None:
                return self._abstain(d, "dependency_unavailable: user data store unreadable; no cached state for this viewer")
            cached = ctx.cache.get(self.user_id, {})
            active, live_page, pending = cached.get("active"), cached.get("live_page"), cached.get("pending", 0)
            cooldown, ignored = {}, frozenset()
            store_ok = False  # no outbound messages: the cap and duplicates can't be confirmed
            d.degraded_mode = f"user_data_store: {exc}; used the agent's write-through cache; home page only"

        if active is not None and active.deadline > at:
            rule = ctx.pack.goals.current(active.goal_id, at)
            d.goal = GoalChoice(rule, None, [], start=active.start, deadline=active.deadline, continuing=True)
        else:
            d.goal = ctx.goals.choose(d.facts, at, cooldown)
        if d.goal.rule is None:
            return self._abstain(d, d.goal.no_goal_reason or "no_eligible_goal")

        if not d.goal.continuing:
            advice = ctx.advisor.consult(d.facts, d.goal, trigger, at, budget, allow=allow_model)
            if advice.note:
                d.notes.append(advice.note)
            d.fallback_used = advice.fallback_used
            d.model_claims_rejected = advice.rejected
            if advice.accepted_claim is not None:
                d.model_claims = [advice.accepted_claim]
                if advice.goal_id != d.goal.goal_id:
                    d.goal = ctx.goals.choice_for(advice.goal_id, d.goal, at)

        if live_page is not None and live_page["dialect"] == d.facts.get("dialect_seen") and d.goal.continuing:
            # The exact expiry depends on measured time, so it stays out of the reason (keeps runs reproducible).
            ttl = ctx.pack.policy.home_page.ttl_minutes
            return self._abstain(d, f"active_page_still_live_no_new_facts: the page from {live_page['decision_id']} "
                                    f"for the same goal and dialect is still inside its {ttl}-min TTL")

        budget.charge_tool()
        d.page, why = ctx.homepage.build(d.facts, d.goal, at)
        if d.page is None:
            return self._abstain(d, why)

        msg_ctx = MessageContext(store_ok, lambda day: ctx.store.messages_on(self.user_id, day), pending, ignored)
        budget.charge_tool()
        d.message, why = ctx.messages.plan(d.facts, d.goal, d.page, at, msg_ctx)
        if d.message is None:
            d.notes.append(f"no message: {why}")

        d.confidence, d.confidence_reason = confidence.assess(d.facts, d.goal, d.page, d.fallback_used)
        if d.confidence < ctx.config.decision.min_confidence_to_act:
            d.page = d.message = None
            return self._abstain(d, f"low_confidence: {d.confidence} < {ctx.config.decision.min_confidence_to_act}")
        d.status = DRAFT

    def _learned(self, at: datetime) -> tuple[dict[str, str], frozenset[str]]:
        """From this viewer's journal: goals on cool-down (failed in the last 14 days) and channels they ignore."""
        cooldown: dict[str, str] = {}
        ignored: Counter = Counter()
        for entry in self.ctx.store.journal(self.user_id, until=at, kinds=["learning"]):
            p = entry["payload"]
            if p.get("learning") == "goal_result" and p.get("verdict") == "failed" \
                    and at - parse_ts(p["deadline"]) <= timedelta(days=14):
                cooldown[p["goal_id"]] = f"cool-down: {p['goal_id']} failed for this viewer (deadline {p['deadline']})"
            elif p.get("learning") == "message_outcome" and p.get("delivered") and not p.get("clicked"):
                ignored[p["channel"]] += 1
        return cooldown, frozenset(c for c, n in ignored.items() if n >= 2)

    def _abstain(self, d: Decision, reason: str, status: str = NOT_PUBLISHED) -> None:
        d.no_action_reason = reason
        d.page = d.message = None
        d.status = status

    def _live_page(self, at: datetime) -> dict | None:
        """The viewer's most recent published home page, if its TTL has not expired."""
        ttl = timedelta(minutes=self.ctx.pack.policy.home_page.ttl_minutes)
        for entry in reversed(self.ctx.store.journal(self.user_id, until=at, kinds=["action"])):
            p = entry["payload"]
            if p.get("type") == "home_page":
                published = parse_ts(p["published_at"])
                if published + ttl > at:
                    return {"decision_id": p["decision_id"], "dialect": p.get("dialect"),
                            "live_until": (published + ttl).isoformat()}
                return None
        return None
