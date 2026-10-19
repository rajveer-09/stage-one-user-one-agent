"""Replay harness: runs the replay day on a virtual clock, one agent per viewer.

Author: Rajveer Sharma
Date: 19 Oct 2026
"""

from __future__ import annotations

import hashlib
import heapq
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..agent.viewer_agent import AgentContext, ViewerAgent
from ..clock import parse_ts, to_iso
from ..config import AgentConfig
from ..models.faults import DEGRADED, DOWN
from ..models.provider import ModelProvider
from ..pack.catalogue import scan_text
from ..pack.events import Event
from ..pack.loader import Pack
from ..state.store import StateStore, StoreUnavailable
from ..measure.success import signal_matches
from ..verify import checks
from .indexes import SUPERSEDED, ActionIndex
from .scheduler import MessageScheduler

FRESH, STALE = 0, 1
TRIGGER, LIVE, CONTROL, RECOVER = 0, 1, 2, 3  # tie-break order at the same instant: arrivals before control
PURCHASE_EVENTS = ("purchase_completed", "subscription_renewed")


def _unit(key: str, salt: str) -> float:
    """Deterministic pseudo-random number in [0, 1) for a key."""
    return int(hashlib.sha256(f"{salt}:{key}".encode()).hexdigest()[:8], 16) / 0x100000000


@dataclass(order=True)
class _Ready:
    priority: int
    arrival: datetime
    seq: int
    trigger: Event = field(compare=False)


@dataclass
class ReplayResult:
    decisions: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    control_log: list[dict[str, Any]]
    security_log: list[dict[str, Any]]
    summary: dict[str, Any]


class ReplayHarness:
    def __init__(self, pack: Pack, config: AgentConfig, work_dir: Path, provider: ModelProvider | None = None) -> None:
        self.pack, self.config = pack, config
        work_dir.mkdir(parents=True, exist_ok=True)
        for stale in work_dir.glob("state.db*"):
            stale.unlink()
        store = StateStore(work_dir / "state.db", buffer_path=work_dir / "store_buffer.jsonl")
        self.ctx = AgentContext.build(pack, config, store, include_replay_events=False, provider=provider)
        self.ctx.store_probe = self._store_probe
        self.index = ActionIndex(pack.policy.home_page.ttl_minutes)
        self.decisions: list[dict[str, Any]] = []
        self.control_log: list[dict[str, Any]] = []
        self.security_log: list[dict[str, Any]] = []
        self.scheduler = MessageScheduler(self.ctx, self.index, self.control_log.append)
        self.busy_until: dict[str, datetime] = {}
        self.max_queue_wait_ms = 0.0
        self.shed = 0

    def _store_probe(self, at: datetime, key: str) -> tuple[bool, float]:
        """Simulated user-data-store read: ok normally; slow or timing out inside a degraded window."""
        state, window = self.ctx.dependencies.state("user_data_store", at)
        timeout_ms = self.config.budget.store_timeout_s * 1000
        if state == DOWN:
            return False, 0.0
        if state == DEGRADED and window is not None:
            p95_s, fail_share = self.ctx.dependencies.degraded_params(window)
            if _unit(key, "store-timeout") < fail_share:
                return False, timeout_ms  # we waited the full read timeout, then gave up
            return True, min(timeout_ms * 0.95, p95_s * 1000 * _unit(key, "store-latency"))
        return True, 0.0

    def run(self) -> ReplayResult:
        pack = self.pack
        self._security_scan()
        items: list[tuple] = []
        seq = 0
        for t in pack.triggers:
            items.append((t.arrival, TRIGGER, seq, t)); seq += 1
        for e in pack.live_events:
            items.append((e.arrival, LIVE, seq, e)); seq += 1
        for c in pack.control_events:
            items.append((parse_ts(c["at"]), CONTROL, seq, c)); seq += 1
        heapq.heapify(items)
        self._seq = seq
        start_clock = items[0][0] if items else datetime.min
        workers = [start_clock] * self.config.replay.workers
        heapq.heapify(workers)
        ready: list[_Ready] = []
        last = start_clock

        while items or ready:
            if ready:
                best = ready[0]
                start = max(workers[0], best.arrival, self.busy_until.get(best.trigger.user_id, best.arrival))
                if not items or start < items[0][0]:
                    heapq.heappop(ready)
                    free = heapq.heappop(workers)
                    start = max(free, best.arrival, self.busy_until.get(best.trigger.user_id, best.arrival))
                    done = self._dispatch(best.trigger, start)
                    heapq.heappush(workers, done)
                    last = max(last, done)
                    continue
            when, kind, _, payload = heapq.heappop(items)
            last = max(last, when)
            self.scheduler.send_due(when)
            if kind == TRIGGER:
                self.ctx.resolver.index.add(payload)
                stale = payload.age_s(payload.arrival) > self.config.freshness.stale_trigger_after_s
                heapq.heappush(ready, _Ready(STALE if stale else FRESH, payload.arrival, self._next(), payload))
            elif kind == LIVE:
                if self.ctx.resolver.index.add(payload):
                    self._on_live_event(payload)
                else:
                    self._log(when, "duplicate_live_event_ignored", event=payload.ref, user_id=payload.user_id)
            elif kind == CONTROL:
                self._on_control(payload, when, items)
            elif kind == RECOVER:
                replayed = self.ctx.store.set_available(True)
                self._log(when, "dependency_recovered", dependency="user_data_store", buffered_writes_replayed=replayed)

        end = max(last, pack.replay_window[1] if pack.replay_window else last)
        finalize = self.scheduler.finalize(end)
        actions = self.index.effective_actions()
        held = [a for a in actions if pack.arms.get(a["user_id"]) != "agent"]
        if held:  # defence in depth: the verifier should make this impossible
            raise RuntimeError(f"{len(held)} actions for non-agent-arm viewers: {[a['action_id'] for a in held[:5]]}")
        summary = {
            "replay_end": to_iso(end),
            "decisions": len(self.decisions),
            "actions": len(actions),
            "messages": finalize,
            "max_queue_wait_ms": round(self.max_queue_wait_ms, 1),
            "decisions_load_shed": self.shed,
            "faults_exercised": self.ctx.advisor.provider.__dict__.get("exercised", {}),
            "faults_scheduled": sorted(pack.model_faults),
        }
        return ReplayResult(self.decisions, actions, self.control_log, self.security_log, summary)

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def _dispatch(self, trigger: Event, start: datetime) -> datetime:
        wait_ms = (start - trigger.arrival).total_seconds() * 1000
        self.max_queue_wait_ms = max(self.max_queue_wait_ms, wait_ms)
        allow_model = wait_ms <= self.config.replay.load_shed_queue_wait_s * 1000
        self.shed += not allow_model
        result = ViewerAgent(trigger.user_id, self.ctx).handle(trigger, at=start, allow_model=allow_model,
                                                                queue_wait_ms=wait_ms)
        self._record(result, start)
        done = result.decision.published_at or start
        self.busy_until[trigger.user_id] = done
        return done

    def _record(self, result, at: datetime) -> None:
        d = result.decision
        for a in result.actions:
            self.index.add(a)
        self.ctx.refresh_cache(d.user_id, at)
        self.decisions.append(d.to_dict())
        for r in d.model_claims_rejected:
            if r.get("error") == "unsupported_claim":
                self.security_log.append({"at": to_iso(at), "type": "model_claim_rejected", "user_id": d.user_id,
                                          "decision_id": d.decision_id, "detail": r["detail"], "claim": r.get("claim")})

    def _on_live_event(self, e: Event) -> None:
        if self.pack.arms.get(e.user_id) != "agent":
            return  # holdout: no agent action of any kind, including re-planning
        at, store = e.arrival, self.ctx.store
        if e.event == "notification_opt_out":
            channel = e.properties.get("channel")
            for a in self.index.pending_for_user(e.user_id):
                if channel in (None, "", a["type"]):
                    self.scheduler.cancel(a, f"viewer opted out of {channel or 'notifications'} ({e.ref})", at)
            self._log(at, "opt_out_handled", user_id=e.user_id, event=e.ref)
            return
        try:
            active = store.active_goal(e.user_id)
        except StoreUnavailable:
            self._log(at, "live_event_deferred", user_id=e.user_id, event=e.ref, reason="store unavailable")
            return

        closed = None
        if active is not None:
            rule = self.pack.goals.current(active.goal_id, at)
            if signal_matches(rule, active.start, active.plan.get("target"), e, self.pack.catalogue):
                closed = "success_signal_observed"
            elif e.event in PURCHASE_EVENTS and "subscription_status" in rule.eligibility \
                    and e.corrected_occurred_at < active.start:
                closed = "invalidated: the purchase happened before the goal started (decided on stale facts)"
            if closed:
                store.close_goal(e.user_id)
                store.append(e.user_id, "goal", at, {"event": "closed", "goal_id": active.goal_id, "reason": closed,
                                                     "signal": e.ref, "rule_ref": rule.ref}, ref=rule.ref)
                for a in self.index.pending_for_decision(active.decision_id):
                    self.scheduler.cancel(a, f"goal {active.goal_id} closed: {closed}", at)
                store.commit()
                self._log(at, "goal_closed_by_live_event", user_id=e.user_id, goal_id=active.goal_id, reason=closed,
                          event=e.ref)

        if e.event in PURCHASE_EVENTS and (active is None or closed):
            result = ViewerAgent(e.user_id, self.ctx).handle(e, at=at)
            self._record(result, at)
            self._log(at, "replanned_after_purchase", user_id=e.user_id, event=e.ref,
                      decision_id=result.decision.decision_id, action=result.decision.action,
                      new_goal=result.decision.goal.goal_id if result.decision.goal else None)
        self.ctx.refresh_cache(e.user_id, at)

    def _on_control(self, c: dict[str, Any], at: datetime, items: list) -> None:
        kind = c["type"]
        if kind == "title_withdrawn":
            self._withdraw(c, at)
        elif kind == "goal_rule_corrected":
            self._correct_rule(c, at)
        elif kind in ("dependency_unavailable", "dependency_degraded"):
            dep, until = c.get("dependency"), parse_ts(c.get("until"))
            response = {
                "model_provider": "model calls fail or slow down in this window; the advisor falls back to rules "
                                  "and a circuit breaker stops repeated calls",
                "user_data_store": ("reads time out or slow down in this window; decisions use the agent's "
                                    "write-through cache, publish the home page only and plan no messages"),
            }.get(dep, "unknown dependency: logged, no special handling")
            if dep == "user_data_store" and kind == "dependency_unavailable":
                self.ctx.store.set_available(False)
                heapq.heappush(items, (until, RECOVER, self._next(), {}))
                response = "store marked unavailable: reads fail (decisions abstain), writes are buffered until recovery"
            self._log(at, kind, dependency=dep, until=c.get("until"), detail=c.get("detail"), response=response)
        else:
            self._log(at, "control_event_ignored", event=c, reason=f"unknown control event type {kind!r}")

    def _correct_rule(self, c: dict[str, Any], at: datetime) -> None:
        result = self.pack.goals.apply_correction(c)
        affected = self.ctx.store.open_goals_for(result.goal_id)
        for g in affected:
            self.ctx.store.update_goal(g.user_id, rule_version=result.new_rule.version)
            self.ctx.store.append(g.user_id, "goal", at, {"event": "rule_corrected", "goal_id": g.goal_id,
                                                          "from": result.old_rule.ref, "to": result.new_rule.ref,
                                                          "new_success": c.get("new_success")}, ref=result.new_rule.ref)
            self.ctx.refresh_cache(g.user_id, at)
        self.ctx.store.commit()
        self._log(at, "goal_rule_corrected", goal_id=result.goal_id, from_rule=result.old_rule.ref,
                  to_rule=result.new_rule.ref, applies_to=result.applies_to, affected_open_goals=len(affected),
                  affected_viewers=[g.user_id for g in affected], warnings=result.warnings,
                  response="only open goals of this goal_id were re-pointed; no other viewer was reprocessed")

    def _withdraw(self, c: dict[str, Any], at: datetime) -> None:
        title_id = c["title_id"]
        self.pack.catalogue.withdraw(title_id, at, c.get("reason", ""))
        rebuilt, failed = [], []
        for page in self.index.live_pages_with(title_id, at):
            new = self._rebuild_page(page, title_id, at)
            (rebuilt if new else failed).append(page["user_id"])
        cancelled = []
        for a in self.index.pending_for_title(title_id):
            self.scheduler.cancel(a, f"title {title_id} withdrawn: {c.get('reason', '')}", at)
            cancelled.append(a["action_id"])
        self.ctx.store.commit()
        self._log(at, "title_withdrawn", title_id=title_id, reason=c.get("reason"), pages_rebuilt=rebuilt,
                  pages_not_rebuilt=failed, messages_cancelled=cancelled,
                  response="title unusable from this moment; only pages and messages containing it were touched")

    def _rebuild_page(self, page: dict[str, Any], title_id: str, at: datetime) -> dict[str, Any] | None:
        rules = self.pack.policy.home_page
        rows = [{"row_type": r["row_type"], "title_ids": [t for t in r["title_ids"] if t != title_id]} for r in page["rows"]]
        rows = [r for r in rows if len(r["title_ids"]) >= rules.min_titles_per_row]
        hero = None if page.get("hero_title_id") == title_id else page.get("hero_title_id")
        candidate = {"dialect": page["dialect"], "hero_title_id": hero, "rows": rows}
        facts = self.ctx.resolver.resolve(page["user_id"], at)
        findings = checks.check_page(self.pack, facts, candidate, at) if rows else []
        if not rows or not all(f.ok for f in findings):
            return None
        new = {**page, **candidate, "action_id": f"{page['action_id']}-R1", "published_at": to_iso(at),
               "supersedes": page["action_id"], "reason": f"{title_id} withdrawn"}
        self.index.add(new)
        self.index.set_status(page["action_id"], SUPERSEDED)
        self.ctx.store.append(page["user_id"], "action", at, new, ref=f"action:{new['action_id']}")
        self.ctx.refresh_cache(page["user_id"], at)
        return new

    def _log(self, at: datetime, kind: str, **fields: Any) -> None:
        self.control_log.append({"at": to_iso(at), "type": kind, **fields})

    def _security_scan(self) -> None:
        for f in self.pack.catalogue.flags:
            self.security_log.append({"type": "catalogue_instruction_quarantined", "title_id": f.title_id,
                                      "field": f.field, "pattern": f.pattern, "excerpt": f.excerpt,
                                      "response": "title never used in any action; text treated as content"})
        for e in list(self.pack.triggers) + list(self.pack.live_events):
            for key, value in e.properties.items():
                if isinstance(value, str) and scan_text(value):
                    self.security_log.append({"type": "event_text_flagged", "event": e.ref, "field": key,
                                              "excerpt": value[:120], "response": "content only; never followed"})


def write_outputs(result: ReplayResult, out_dir: Path, store: StateStore) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, rows in (("decisions", result.decisions), ("actions", result.actions),
                       ("control_log", result.control_log), ("security_log", result.security_log)):
        path = out_dir / f"{name}.jsonl"
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        paths[name] = path
    state_dir = out_dir / "viewer_state"
    if state_dir.exists():
        shutil.rmtree(state_dir)
    store.export_all(state_dir)
    paths["viewer_state"] = state_dir
    (out_dir / "replay_summary.json").write_text(json.dumps(result.summary, indent=2, default=str), encoding="utf-8")
    paths["replay_summary"] = out_dir / "replay_summary.json"
    return paths
