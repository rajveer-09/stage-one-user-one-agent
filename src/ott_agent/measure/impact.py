"""Impact: the agent arm vs the randomized holdout, intention-to-treat, with uncertainty.

Author: Rajveer Sharma
Date: 19 Oct 2026
"""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Iterable

from ..clock import to_iso
from ..pack.events import Event
from ..pack.loader import Pack

Z = 1.959964



def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + Z * Z / n
    centre = (p + Z * Z / (2 * n)) / denom
    half = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def newcombe(k1: int, n1: int, k0: int, n0: int) -> tuple[float, float]:
    """95% CI for p1 - p0 (Newcombe hybrid score method)."""
    if not n1 or not n0:
        return (float("nan"), float("nan"))
    p1, p0 = k1 / n1, k0 / n0
    l1, u1 = wilson(k1, n1)
    l0, u0 = wilson(k0, n0)
    d = p1 - p0
    return d - math.sqrt((p1 - l1) ** 2 + (u0 - p0) ** 2), d + math.sqrt((u1 - p1) ** 2 + (p0 - l0) ** 2)


def bootstrap(x1: list[int], x0: list[int], reps: int = 2000, seed: int = 7) -> tuple[float, float]:
    if not x1 or not x0:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    diffs = []
    for _ in range(reps):
        s1 = sum(x1[rng.randrange(len(x1))] for _ in x1) / len(x1)
        s0 = sum(x0[rng.randrange(len(x0))] for _ in x0) / len(x0)
        diffs.append(s1 - s0)
    diffs.sort()
    return diffs[int(0.025 * reps)], diffs[int(0.975 * reps) - 1]


def compare(x1: list[int], x0: list[int], with_bootstrap: bool = False) -> dict[str, Any]:
    k1, n1, k0, n0 = sum(x1), len(x1), sum(x0), len(x0)
    p1, p0 = (k1 / n1 if n1 else float("nan")), (k0 / n0 if n0 else float("nan"))
    lo, hi = newcombe(k1, n1, k0, n0)
    pooled = (k1 + k0) / (n1 + n0) if n1 + n0 else 0
    se0 = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n0)) if n1 and n0 else float("nan")
    out = {"agent": {"k": k1, "n": n1, "rate": p1}, "holdout": {"k": k0, "n": n0, "rate": p0},
           "diff": p1 - p0, "ci95": [lo, hi], "mde_80pct_power": 2.8 * se0,
           "significant": not (lo <= 0 <= hi) if n1 and n0 else False}
    if with_bootstrap:
        out["bootstrap_ci95"] = list(bootstrap(x1, x0))
    return out



def first_triggers(pack: Pack, stale_after_s: float) -> dict[str, tuple[Event, bool]]:
    """viewer -> (first trigger, was it fresh). Uses the first fresh trigger if there is one."""
    out: dict[str, tuple[Event, bool]] = {}
    for t in pack.triggers:
        fresh = t.age_s(t.arrival) <= stale_after_s
        cur = out.get(t.user_id)
        if cur is None or (fresh and not cur[1]):
            out[t.user_id] = (t, fresh)
    return out


def _unpaid_at_start(pack: Pack) -> set[str]:
    status = {}
    for row in sorted(pack.profiles, key=lambda r: (r.updated_at is not None, r.updated_at or datetime.min)):
        status[row.user_id] = row.subscription_status
    paid = {e.user_id for e in pack.history if e.event in ("purchase_completed", "subscription_renewed")}
    users = {t.user_id for t in pack.triggers}
    return {u for u in users if u not in paid and status.get(u) in (None, "never", "lapsed")}


def behaviour(pack: Pack, outcome_events: Iterable[Event], firsts: dict[str, tuple[Event, bool]]) -> dict[str, dict[str, int]]:
    by_user: dict[str, list[Event]] = defaultdict(list)
    for e in outcome_events:
        by_user[e.user_id].append(e)
    unpaid = _unpaid_at_start(pack)
    out: dict[str, dict[str, int]] = {}
    for u, (t, _) in firsts.items():
        start = t.arrival
        evs = by_user.get(u, [])

        def within(name: str, days: float, pred=lambda e: True) -> int:
            end = start + timedelta(days=days)
            return int(any(e.event == name and start <= e.received_at <= end and pred(e) for e in evs))

        out[u] = {
            "play_7d": within("play_started", 7),
            "episode_completed_3d": within("episode_completed", 3),
            "app_open_next_ist_day_7d": within("app_open", 7, lambda e: e.received_at.date() > start.date()),
        }
        if u in unpaid:
            out[u]["paid_7d"] = within("purchase_completed", 7, lambda e: e.properties.get("is_trial") is False)
    return out



def impact(pack: Pack, decisions: list[dict[str, Any]], outcomes: list[dict[str, Any]], outcome_events: list[Event],
           stale_after_s: float, sim_report: dict[str, Any] | None = None) -> dict[str, Any]:
    arms = pack.arms
    firsts = first_triggers(pack, stale_after_s)
    users = sorted(firsts)
    arm_of = {u: arms.get(u, "none") for u in users}
    beh = behaviour(pack, outcome_events, firsts)

    served = {d["viewer_id"] for d in decisions if d["status"] == "PUBLISHED"}
    itt = {o["viewer_id"]: o for o in outcomes if o["role"] == "itt"}

    def split(metric):
        a = [beh[u][metric] for u in users if arm_of[u] == "agent" and metric in beh[u]]
        h = [beh[u][metric] for u in users if arm_of[u] == "holdout" and metric in beh[u]]
        return a, h

    def success(o):
        return int(o["verdict"] == "succeeded")

    decided = [o for o in itt.values() if o["verdict"] != "inconclusive"]
    goal_a = [success(o) for o in decided if o["arm"] == "agent"]
    goal_h = [success(o) for o in decided if o["arm"] == "holdout"]
    result: dict[str, Any] = {
        "population": {"triggered_viewers": len(users), "agent": sum(arm_of[u] == "agent" for u in users),
                       "holdout": sum(arm_of[u] == "holdout" for u in users),
                       "agent_viewers_served": len(served & {u for u in users if arm_of[u] == "agent"})},
        "primary": {
            "goal_success": compare(goal_a, goal_h, with_bootstrap=True),
            "play_7d": compare(*split("play_7d"), with_bootstrap=True),
        },
        "secondary": {m: compare(*split(m)) for m in ("episode_completed_3d", "paid_7d", "app_open_next_ist_day_7d")},
        "verdicts": {arm: dict(Counter(o["verdict"] for o in itt.values() if o["arm"] == arm))
                     for arm in sorted({o["arm"] for o in itt.values()})},
    }

    by_goal = {}
    for gid in sorted({o["goal_id"] for o in decided}):
        a = [success(o) for o in decided if o["goal_id"] == gid and o["arm"] == "agent"]
        h = [success(o) for o in decided if o["goal_id"] == gid and o["arm"] == "holdout"]
        by_goal[gid] = compare(a, h)
    result["by_goal_exploratory"] = by_goal

    # Abstention check: viewers whose first trigger already rules out acting, in both arms.
    dialects = pack.catalogue.dialects()
    groups = {
        "first trigger stale (offline-buffered)": lambda u: not firsts[u][1],
        "app dialect has no content (d7/NA/unknown)": lambda u: firsts[u][1] and firsts[u][0].dialect not in dialects,
    }
    abst = {}
    for name, pred in groups.items():
        a = [beh[u]["play_7d"] for u in users if arm_of[u] == "agent" and pred(u)]
        h = [beh[u]["play_7d"] for u in users if arm_of[u] == "holdout" and pred(u)]
        abst[name] = compare(a, h)
    result["abstention_check_play_7d"] = abst

    agent_served = [u for u in users if arm_of[u] == "agent" and u in served]
    rest = [u for u in users if u not in served]
    agent_unserved = [u for u in users if arm_of[u] == "agent" and u not in served]
    result["biased_served_vs_rest"] = {
        "play_7d_served_vs_everyone_else": compare([beh[u]["play_7d"] for u in agent_served], [beh[u]["play_7d"] for u in rest]),
        "play_7d_served_vs_unserved_in_agent_arm": compare([beh[u]["play_7d"] for u in agent_served],
                                                           [beh[u]["play_7d"] for u in agent_unserved]),
    }
    share = result["population"]["agent_viewers_served"] / max(1, result["population"]["agent"])
    play = result["primary"]["play_7d"]
    result["cace_play_7d"] = {"served_share": share, "estimate": play["diff"] / share if share else None,
                              "assumption": "unserved agent-arm viewers got no exposure, so all of the effect runs through serving"}
    if sim_report is not None:
        result["simulator"] = {"counts": sim_report.get("counts", {}), "policy_problem_count": sim_report.get("policy_problem_count"),
                               "policy_problems_sample": sim_report.get("policy_problems", [])[:10]}
    return result


def pilot_reanalysis(pack_root) -> dict[str, Any]:
    import csv
    from pathlib import Path

    rows = list(csv.DictReader(Path(pack_root, "pilot/pilot_outcomes.csv").open(encoding="utf-8")))
    ok = lambda r: int(r["succeeded"].strip().lower() == "true")
    served = lambda r: r["served"].strip().lower() == "true"
    t = [ok(r) for r in rows if r["arm"] == "treatment"]
    h = [ok(r) for r in rows if r["arm"] == "holdout"]
    srv = [ok(r) for r in rows if served(r)]
    rest = [ok(r) for r in rows if not served(r)]
    t_srv = [ok(r) for r in rows if r["arm"] == "treatment" and served(r)]
    t_uns = [ok(r) for r in rows if r["arm"] == "treatment" and not served(r)]
    by_goal = {}
    for gid in sorted({r["goal_id"] for r in rows}):
        by_goal[gid] = compare([ok(r) for r in rows if r["goal_id"] == gid and r["arm"] == "treatment"],
                               [ok(r) for r in rows if r["goal_id"] == gid and r["arm"] == "holdout"])
    return {
        "report_headline_served_vs_rest": compare(srv, rest),
        "within_treatment_served_vs_unserved": compare(t_srv, t_uns),
        "itt_treatment_vs_holdout": compare(t, h, with_bootstrap=True),
        "served_share_of_treatment": len(t_srv) / len(t) if t else None,
        "by_goal_exploratory": by_goal,
    }



def _pct(x: float) -> str:
    return "–" if x != x else f"{100 * x:.1f}%"


def _pp(x: float) -> str:
    return "–" if x != x else f"{100 * x:+.1f} pt"


def _row(name: str, c: dict[str, Any]) -> str:
    a, h = c["agent"], c["holdout"]
    lo, hi = c["ci95"]
    sig = "yes" if c["significant"] else "no"
    return (f"| {name} | {_pct(a['rate'])} ({a['k']}/{a['n']}) | {_pct(h['rate'])} ({h['k']}/{h['n']}) | "
            f"**{_pp(c['diff'])}** | [{_pp(lo)}, {_pp(hi)}] | {sig} |")


HEADER = "| Metric | Agent arm | Holdout arm | Difference | 95% CI | Excludes 0? |\n|---|---|---|---|---|---|"
BIASED_HEADER = ("| Comparison (biased) | Served viewers | Comparison group | Difference | 95% CI | Excludes 0? |\n"
                 "|---|---|---|---|---|---|")
LABELS = {
    "episode_completed_3d": "Finished an episode within 3 days",
    "paid_7d": "Paid purchase within 7 days (not paying at start)",
    "app_open_next_ist_day_7d": "Came back on a later IST day within 7 days",
}


def to_markdown(res: dict[str, Any], pilot: dict[str, Any], run_date: str, seed: str) -> str:
    pop = res["population"]
    g, p = res["primary"]["goal_success"], res["primary"]["play_7d"]
    lines = [
        "# Impact Report", "",
        "**Author:** Rajveer Sharma",
        f"**Date:** {run_date} (generated by `python -m ott_agent report`; simulator seed `{seed}`)", "",
        "## Headline", "",
        f"Intention-to-treat over all {pop['triggered_viewers']:,} viewers with a replay trigger "
        f"({pop['agent']:,} agent arm, {pop['holdout']:,} holdout; randomized by `replay/arms.csv`). "
        f"The agent published something for {pop['agent_viewers_served']:,} agent-arm viewers; everyone is counted, served or not.", "",
        f"- **Goal success:** {_pct(g['agent']['rate'])} vs {_pct(g['holdout']['rate'])}, difference {_pp(g['diff'])} "
        f"(95% CI [{_pp(g['ci95'][0])}, {_pp(g['ci95'][1])}]; bootstrap [{_pp(g['bootstrap_ci95'][0])}, {_pp(g['bootstrap_ci95'][1])}]).",
        f"- **Any play within 7 days:** {_pct(p['agent']['rate'])} vs {_pct(p['holdout']['rate'])}, difference {_pp(p['diff'])} "
        f"(95% CI [{_pp(p['ci95'][0])}, {_pp(p['ci95'][1])}]).",
        f"- Minimum detectable effect at 80% power: about {_pp(g['mde_80pct_power'])[1:]} for goal success, "
        f"{_pp(p['mde_80pct_power'])[1:]} for play within 7 days.", "",
        "An interval that includes 0 means this run cannot tell the agent's effect apart from no effect. "
        "That is reported as is; it is not rounded up into a claim.", "",
        "## Agent arm vs holdout (intention-to-treat)", "", HEADER,
        _row("Intended-goal success (primary)", g), _row("Any play within 7 days (primary)", p),
        *[_row(LABELS.get(name, name), c) for name, c in res["secondary"].items()], "",
        "The first two rows are the pre-declared primary metrics. The rest are secondary: with several metrics, "
        "one interval excluding 0 can happen by chance, so a secondary result is a lead to re-test, not a finding.", "",
        "Goal success counts only decided goals: inconclusive verdicts (deadline past the end of the outcome data, "
        "invalidated goals) are excluded from both arms alike. Verdict counts: "
        + "; ".join(f"{arm}: " + ", ".join(f"{n} {v}" for v, n in sorted(c.items())) for arm, c in res["verdicts"].items())
        + ".", "",
        "## By intended goal (exploratory)", "",
        "Holdout viewers are grouped by the shadow goal the agent would have pursued, so each row compares like with like. "
        "Many small groups and no multiple-comparison correction: read these as hints, not findings.", "", HEADER,
        *[_row(gid, c) for gid, c in res["by_goal_exploratory"].items()], "",
        "## Was doing nothing the right call?", "",
        "Viewers whose first trigger already ruled out acting, compared across arms (play within 7 days). "
        "Similar rates mean the agent's abstention cost nothing measurable.", "", HEADER,
        *[_row(name, c) for name, c in res["abstention_check_play_7d"].items()], "",
        "## Why \"served vs everyone else\" misleads", "",
        "These comparisons are biased and are shown only to make the point (play within 7 days):", "", BIASED_HEADER,
        _row("Served (agent arm) vs everyone else", res["biased_served_vs_rest"]["play_7d_served_vs_everyone_else"]),
        _row("Served vs unserved, agent arm only", res["biased_served_vs_rest"]["play_7d_served_vs_unserved_in_agent_arm"]), "",
        "The agent serves a selected group: viewers in a live session, in a dialect with content, with an eligible goal "
        "and no live page. Those traits predict watching on their own. Comparing them with everyone else measures the "
        "selection, not the agent. Only the randomized comparison above isolates the agent's effect.", "",
        f"If all of the effect runs through serving, the effect per served viewer (CACE) is about "
        f"{_pp(res['cace_play_7d']['estimate']) if res['cace_play_7d']['estimate'] is not None else '–'} on play within 7 days "
        f"(ITT ÷ served share {res['cace_play_7d']['served_share']:.0%}). This inherits the ITT uncertainty.", "",
        "## Pilot re-analysis", "",
        "| Comparison | Group A | Group B | Difference | 95% CI |", "|---|---|---|---|---|",
    ]
    for name, key in (("Internal report: served vs everyone else", "report_headline_served_vs_rest"),
                      ("Within treatment: served vs unserved", "within_treatment_served_vs_unserved"),
                      ("**Randomized arms (ITT)**", "itt_treatment_vs_holdout")):
        c = pilot[key]
        lines.append(f"| {name} | {_pct(c['agent']['rate'])} (n={c['agent']['n']:,}) | {_pct(c['holdout']['rate'])} "
                     f"(n={c['holdout']['n']:,}) | {_pp(c['diff'])} | [{_pp(c['ci95'][0])}, {_pp(c['ci95'][1])}] |")
    lines += [
        "", f"The pilot report's +22.4 pt \"uplift\" compares the {pilot['served_share_of_treatment']:.0%} of treatment users the "
        "agent chose to serve with everyone else (unserved treatment users plus the holdout). The randomized comparison "
        "shows no detectable effect. The pilot did not show that the agent helped.", "",
    ]
    if "simulator" in res:
        s = res["simulator"]
        counts = s["counts"]
        lines += ["## Simulator checks", "",
                  f"- Policy problems flagged by the simulator: **{s['policy_problem_count']}** "
                  "(actions on holdout viewers, messages without consent, in quiet hours or over the cap).",
                  f"- Home pages accepted: {counts.get('home_page_accepted', 0):,}; shown as personalised: "
                  f"{counts.get('home_page_shown_personalised', 0):,}; holdout actions ignored: {counts.get('holdout_actions_ignored', 0)}.",
                  f"- Push requested {counts.get('push_requested', 0)}, delivered {counts.get('push_delivered', 0)}, "
                  f"undeliverable token {counts.get('push_undeliverable_token', 0)}, clicked {counts.get('push_clicked', 0)}; "
                  f"WhatsApp requested {counts.get('whatsapp_requested', 0)}, delivered {counts.get('whatsapp_delivered', 0)}.", ""]
    return "\n".join(lines) + "\n"
