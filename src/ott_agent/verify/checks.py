"""Verification checks, written independently of the planner.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..clock import in_quiet_hours, ist_day_key, parse_ts
from ..pack.catalogue import scan_text
from ..pack.loader import Pack
from ..state.facts import ViewerFacts
from ..state.store import StateStore, StoreUnavailable

PRICE = re.compile(r"(?:₹|Rs\.?\s?|INR\s?)(\d+)")
DECISION, PAGE, HERO, MESSAGE = "decision", "page", "hero", "message"


@dataclass(frozen=True)
class Finding:
    check: str
    component: str
    ok: bool
    reason: str = ""


def ok(check: str, component: str = DECISION) -> Finding:
    return Finding(check, component, True)


def fail(check: str, reason: str, component: str = DECISION) -> Finding:
    return Finding(check, component, False, reason)



def check_holdout(pack: Pack, d: dict) -> list[Finding]:
    arm = pack.arms.get(d["viewer_id"])  # looked up here, not taken from the decision
    if arm != "agent":
        return [fail("holdout", f"viewer arm is {arm!r}: no action of any kind")]
    return [ok("holdout")]


def check_goal(pack: Pack, store: StateStore, facts: ViewerFacts, d: dict, at: datetime) -> list[Finding]:
    goal = d.get("goal") or {}
    goal_id = goal.get("id")
    if not goal_id:
        return [fail("eligibility", "an action needs a goal")]
    status = pack.goals.status.get(goal_id)
    findings = [ok("measurable") if status and status.enabled
                else fail("measurable", f"{goal_id} is disabled: {status.reasons if status else 'unknown goal'}")]
    try:
        active = store.active_goal(d["viewer_id"])
    except StoreUnavailable:
        return findings + [fail("single_goal", "store unavailable: the single-goal invariant can't be checked")]
    if goal.get("continuing"):
        same = active is not None and active.goal_id == goal_id and active.deadline > at
        findings.append(ok("single_goal") if same else fail("single_goal", "continuing goal is not the viewer's open goal"))
        findings.append(ok("eligibility"))  # eligibility was verified when the goal was opened
        return findings
    if active is not None and active.deadline > at:
        findings.append(fail("single_goal", f"viewer already has open goal {active.goal_id}"))
    else:
        findings.append(ok("single_goal"))
    eligible, why = recompute_eligibility(pack, facts, goal_id, goal.get("target") or {}, at)
    findings.append(ok("eligibility") if eligible else fail("eligibility", f"{goal_id} not eligible on re-check: {why}"))
    return findings


def recompute_eligibility(pack: Pack, f: ViewerFacts, goal_id: str, target: dict, at: datetime) -> tuple[bool, str]:
    rule = pack.goals.current(goal_id, at)
    spec = rule.eligibility
    if "signed_up_within_days" in spec:
        signup = f.get("signup_at")
        if signup is None or at - signup > timedelta(days=int(spec["signed_up_within_days"])):
            return False, "sign-up date missing or outside the window"
    if "requires_no_prior_event" in spec:
        flag = {"play_started": "has_played", "episode_completed": "has_completed_episode"}.get(spec["requires_no_prior_event"])
        if flag is None or f.get(flag) is not False:
            return False, f"prior {spec['requires_no_prior_event']} present or not checkable"
    if "subscription_status" in spec and f.get("subscription_status") not in spec["subscription_status"]:
        return False, f"subscription_status {f.get('subscription_status')!r}"
    if goal_id == "G05" and f.get("ever_paid") is not False:
        return False, "has paid before or unknown"
    if goal_id == "G06":
        lapsed = f.get("last_lapsed_at")
        if lapsed is None or at - lapsed > timedelta(days=30) or f.get("repurchased_since_lapse"):
            return False, "no lapse within 30 days, or bought again"
    if goal_id == "G07":
        slug = target.get("content_slug")
        s = (f.get("series_progress") or {}).get(slug)
        if not s or not s.get("next_episode_id") or at - s["last_play_at"] < timedelta(hours=72):
            return False, f"no stalled unfinished series {slug!r} in the viewer's history"
    if goal_id == "G09" and f.get("play_days_14d", 0) > 1:
        return False, "plays on more than 1 day in 14"
    if goal_id == "G12":
        signup, last_open = f.get("signup_at"), f.get("last_app_open_at")
        if signup is None or at - signup <= timedelta(days=14) or (last_open and at - last_open < timedelta(days=14)):
            return False, "recent sign-up or recent app open"
    structured = {k for k in spec if k != "text"}
    if not structured and goal_id not in ("G06", "G07", "G09", "G12"):
        return False, "text-only rule with no verifiable interpretation"
    return True, "ok"


def check_evidence(pack: Pack, resolver_index, d: dict, at: datetime) -> list[Finding]:
    """Every evidence ref must point at something real that existed by decision time."""
    user, trigger_id = d["viewer_id"], d["trigger"]["event_id"]
    titles, plans = pack.catalogue.titles, pack.policy.plans
    profile_refs = {r.ref for r in pack.profiles if r.user_id == user}
    rule_refs = {v.ref for g in pack.goals.goal_ids() for v in pack.goals.versions(g)}
    assumptions = {a.id for a in pack.goals.assumptions}
    bad = []
    for item in d.get("evidence", []):
        ref = item["ref"]
        kind, _, value = ref.partition(":")
        if kind == "event":
            e = resolver_index.get(value)
            good = value == trigger_id or (e is not None and e.user_id == user and e.arrival <= at)
        elif kind == "profile":
            good = ref in profile_refs
        elif kind == "catalogue":
            good = value in titles
        elif kind == "goal_rule":
            good = ref in rule_refs
        elif kind == "assumption":
            good = value in assumptions
        elif kind == "pricing":
            good = value in plans
        elif kind == "fact":
            good = True  # derived from the facts snapshot journaled with the decision
        else:
            good = False
        if not good:
            bad.append(ref)
    for a in d.get("assumptions", []):
        if a.startswith("assumption:") and a.split(":", 1)[1] not in assumptions:
            bad.append(a)
    return [fail("evidence_exists", f"unresolvable refs: {bad[:5]}")] if bad else [ok("evidence_exists")]


def check_model_claims(pack: Pack, facts: ViewerFacts, resolver_index, d: dict, at: datetime) -> list[Finding]:
    """A model statement counts only if every cited ref exists and the facts agree (P6 adds claims)."""
    claims = d.get("model_claims") or []
    if not claims:
        return [ok("model_claim_supported")]
    goal = d.get("goal") or {}
    for c in claims:
        target = (goal.get("target") or {}) if c.get("goal_id") == goal.get("id") else {}
        refs = [r for r in c.get("evidence", []) if r.startswith("event:") or r.startswith("profile:")]
        if not refs:
            return [fail("model_claim_supported", f"model claim about {c.get('goal_id')} cites no event or profile fact")]
        if c.get("eligible") and not recompute_eligibility(pack, facts, c["goal_id"], target, at)[0]:
            return [fail("model_claim_supported", f"model says eligible for {c['goal_id']}; the facts disagree")]
    return [ok("model_claim_supported")]


def check_duplicate(store: StateStore, d: dict) -> list[Finding]:
    owner = store.owner(d["idempotency_key"])
    if owner is not None and owner != d["decision_id"]:
        return [fail("duplicate_action", f"trigger already handled by {owner}")]
    return [ok("duplicate_action")]



def check_page(pack: Pack, facts: ViewerFacts, page: dict, publish_at: datetime) -> list[Finding]:
    rules, cat = pack.policy.home_page, pack.catalogue
    until = publish_at + timedelta(minutes=rules.ttl_minutes)
    findings: list[Finding] = []
    seen_dialect = facts.get("dialect_seen")
    if page["dialect"] != seen_dialect:
        return [fail("dialect", f"page dialect {page['dialect']} but the app is set to {seen_dialect}", PAGE)]
    rows = page.get("rows") or []
    if not rows:
        findings.append(fail("homepage_shape", "no rows", PAGE))
    if len(rows) > rules.max_rows:
        findings.append(fail("homepage_shape", f"{len(rows)} rows > {rules.max_rows}", PAGE))

    slugs_seen: set[str] = set()

    def title_findings(title_id: str, component: str) -> list[Finding]:
        out = []
        t = cat.titles.get(title_id)
        if t is None:
            return [fail("availability", f"{title_id} not in catalogue", component)]
        if t.dialect != page["dialect"]:
            out.append(fail("dialect", f"{title_id} is {t.dialect}, page is {page['dialect']}", component))
        why = cat.check(title_id, publish_at, until)
        if why == "quarantined":
            out.append(fail("quarantine", f"{title_id} has instruction-like text; never used", component))
        elif why is not None:
            out.append(fail("availability", f"{title_id}: {why}", component))
        if t.content_slug in slugs_seen:
            out.append(fail("homepage_shape", f"slug {t.content_slug} appears more than once on the page", component))
        slugs_seen.add(t.content_slug)
        return out

    if page.get("hero_title_id"):
        findings += title_findings(page["hero_title_id"], HERO)
    for i, row in enumerate(rows):
        comp = f"row:{i}"
        ids = row.get("title_ids") or []
        if row.get("row_type") not in rules.row_types or "continue" in str(row.get("row_type")).lower():
            findings.append(fail("homepage_shape", f"row type {row.get('row_type')!r} not allowed", comp))
        if not rules.min_titles_per_row <= len(ids) <= rules.max_titles_per_row:
            findings.append(fail("homepage_shape", f"row has {len(ids)} titles", comp))
        if len(set(ids)) != len(ids):
            findings.append(fail("homepage_shape", "title repeated within a row", comp))
        for title_id in ids:
            findings += title_findings(title_id, comp)
    names = {f.check for f in findings}
    for name in ("dialect", "availability", "quarantine", "homepage_shape"):
        if name not in names:
            findings.append(ok(name, PAGE))
    return findings



def check_message(pack: Pack, store: StateStore, facts: ViewerFacts, msg: dict, page_dialect: str | None,
                  at: datetime, own_action_id: str | None = None) -> list[Finding]:
    """own_action_id: when re-verifying an already scheduled message at send time, don't count it against its own cap."""
    policy, cat = pack.policy, pack.catalogue
    channel = msg["channel"]
    send_at = parse_ts(msg["send_at"])
    out: list[Finding] = []

    if channel in ("push", "whatsapp"):
        consent = facts.get("consent_push" if channel == "push" else "consent_whatsapp")
        out.append(ok("consent", MESSAGE) if consent is True else fail("consent", f"no {channel} consent on record", MESSAGE))
        quiet = in_quiet_hours(send_at, policy.push.quiet_start, policy.push.quiet_end)
        out.append(fail("quiet_hours", f"send_at {msg['send_at']} is inside 22:00-08:00 IST", MESSAGE) if quiet
                   else ok("quiet_hours", MESSAGE))
        try:
            day = ist_day_key(send_at)
            used = store.messages_on(facts.user_id, day, exclude_action_id=own_action_id)
            used += facts.get("messages_delivered_today", 0) if day == ist_day_key(at) else 0
            out.append(fail("frequency_cap", f"{used} push/WhatsApp already on IST day {day}", MESSAGE)
                       if used >= policy.messages_per_ist_day else ok("frequency_cap", MESSAGE))
        except StoreUnavailable:
            out.append(fail("frequency_cap", "store unavailable: cap can't be verified", MESSAGE))
    elif channel == "in_app_message":
        try:
            used = store.messages_on(facts.user_id, ist_day_key(send_at), channels=("in_app_message",),
                                     exclude_action_id=own_action_id)
            out.append(fail("frequency_cap", "in-app message already planned that day", MESSAGE)
                       if used >= policy.in_app.max_per_day else ok("frequency_cap", MESSAGE))
        except StoreUnavailable:
            out.append(fail("frequency_cap", "store unavailable", MESSAGE))
    else:
        return [fail("channel", f"unknown channel {channel!r}", MESSAGE)]

    title_id = msg.get("title_id")
    if title_id:
        t = cat.titles.get(title_id)
        if t is None:
            out.append(fail("availability", f"{title_id} not in catalogue", MESSAGE))
        else:
            dialect = page_dialect or facts.get("dialect_seen")
            out.append(ok("dialect", MESSAGE) if t.dialect == dialect else
                       fail("dialect", f"{title_id} is {t.dialect}, viewer sees {dialect}", MESSAGE))
            for when in (at, send_at):
                why = cat.check(title_id, when)
                if why is not None:
                    out.append(fail("quarantine" if why == "quarantined" else "availability", f"{title_id}: {why}", MESSAGE))
                    break

    texts: list[str] = []
    if channel == "push":
        text = msg.get("text") or {}
        title, body = text.get("title", ""), text.get("body", "")
        texts += [title, body]
        if len(title) > policy.push.title_max_chars or len(body) > policy.push.body_max_chars:
            out.append(fail("copy_rules", f"push copy too long ({len(title)}/{len(body)})", MESSAGE))
        if title_id and msg.get("deeplink") != policy.push.deeplink(title_id):
            out.append(fail("copy_rules", f"deeplink {msg.get('deeplink')!r} does not match the format", MESSAGE))
        if title_id and title_id in cat.titles and cat.titles[title_id].title not in title + body:
            out.append(fail("copy_rules", "copy does not name the promoted title", MESSAGE))
    elif channel == "whatsapp":
        tmpl = policy.whatsapp.templates.get(msg.get("template_id", ""))
        variables = msg.get("variables") or {}
        if tmpl is None:
            out.append(fail("template_rules", f"template {msg.get('template_id')!r} not approved", MESSAGE))
        else:
            if set(variables) != set(tmpl.variables):
                out.append(fail("template_rules", f"variables {sorted(variables)} != {list(tmpl.variables)}", MESSAGE))
            if tmpl.language != facts.get("app_language"):
                out.append(fail("template_rules", f"template is {tmpl.language}, app is {facts.get('app_language')}", MESSAGE))
            out += _template_sources(pack, variables, title_id)
            texts += [str(v) for v in variables.values()]
    else:
        texts.append((msg.get("text") or {}).get("body", ""))

    joined = " ".join(texts)
    lower = joined.lower()
    banned = [p for p in policy.push.forbidden_phrases if p in lower]
    if banned:
        out.append(fail("copy_rules", f"forbidden phrase(s) {banned}", MESSAGE))
    prices = {int(p) for p in PRICE.findall(joined)}
    if prices - policy.allowed_prices_inr:
        out.append(fail("copy_rules", f"price(s) {sorted(prices - policy.allowed_prices_inr)} not in pricing.json", MESSAGE))
    if scan_text(joined):
        out.append(fail("data_not_instruction", "message text contains instruction-like content", MESSAGE))

    names = {f.check for f in out}
    for name in ("copy_rules", "template_rules", "data_not_instruction"):
        if name not in names and (name != "template_rules" or channel == "whatsapp"):
            out.append(ok(name, MESSAGE))
    return out


def _template_sources(pack: Pack, variables: dict[str, Any], title_id: str | None) -> list[Finding]:
    """WhatsApp variables must come from the catalogue or pricing; free text is rejected."""
    cat, out = pack.catalogue, []
    for name, value in variables.items():
        if name == "title_name":
            good = title_id in cat.titles and cat.titles[title_id].title == value
        elif name == "episode_number":
            slug = cat.titles[title_id].content_slug if title_id in cat.titles else None
            good = slug is not None and str(value) in {str(e.episode) for e in cat.episodes(slug)}
        elif name == "plan_price":
            good = value in {f"₹{p}" for p in pack.policy.allowed_prices_inr}
        else:
            good = False  # e.g. dialect_name: the pack has no source for it
        if not good:
            out.append(fail("template_rules", f"variable {name}={value!r} is not from the catalogue or pricing", MESSAGE))
    return out
