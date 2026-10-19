"""Command-line entry point: `python -m ott_agent <command>`.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .clock import parse_ts
from .config import load_config
from .pack.loader import PackSchemaError, load_pack
from .agent.viewer_agent import AgentContext, ViewerAgent
from .state.resolver import build_resolver
from .state.store import StateStore
from .replay.harness import ReplayHarness, write_outputs

DEFAULT_CONFIG = Path("config/agent.toml")


def cmd_load(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    pack = load_pack(args.pack, config.goal_interpretations)
    summary = pack.summary()
    summary["catalogue"]["injection_flags"] = [
        {"title_id": f.title_id, "field": f.field, "pattern": f.pattern} for f in pack.catalogue.flags
    ]
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def cmd_facts(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    pack = load_pack(args.pack, config.goal_interpretations)
    trigger = None
    if args.trigger:
        trigger = next((t for t in pack.triggers if t.event_id == args.trigger), None)
        if trigger is None:
            print(f"trigger {args.trigger} not found in replay/triggers.jsonl", file=sys.stderr)
            return 2
    at = parse_ts(args.at) if args.at else (trigger.arrival if trigger else None)
    if at is None:
        print("give --at or --trigger", file=sys.stderr)
        return 2
    user = args.viewer or (trigger.user_id if trigger else None)
    if user is None:
        print("give --viewer or --trigger", file=sys.stderr)
        return 2
    facts = build_resolver(pack, config.freshness).resolve(user, at, trigger)
    print(json.dumps(facts.to_dict(), indent=2, ensure_ascii=False))
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    pack = load_pack(args.pack, config.goal_interpretations)
    trigger = next((t for t in pack.triggers if t.event_id == args.trigger), None)
    if trigger is None:
        print(f"trigger {args.trigger} not found in replay/triggers.jsonl", file=sys.stderr)
        return 2
    store = StateStore(args.store)
    agent = ViewerAgent(trigger.user_id, AgentContext.build(pack, config, store))
    if args.draft:
        decision, actions = agent.decide(trigger), []
    else:
        result = agent.handle(trigger)
        decision, actions = result.decision, result.actions
    out = decision.to_dict()
    out["published_actions"] = actions
    if args.export:
        store.export_all(args.export, [trigger.user_id])
        print("viewer state exported to " + str(args.export / f"{trigger.user_id}.json"), file=sys.stderr)
    if args.with_facts:
        out["facts"] = decision.facts.to_dict()["facts"] if decision.facts else None
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    import time
    from dataclasses import replace

    config = load_config(args.config)
    if getattr(args, "model_seed", None):
        config = replace(config, model=replace(config.model, seed=args.model_seed))
    if args.provider:
        config = replace(config, model=replace(config.model, provider=args.provider))
    if args.workers:
        config = replace(config, replay=replace(config.replay, workers=args.workers))
    pack = load_pack(args.pack, config.goal_interpretations)
    started = time.perf_counter()
    harness = ReplayHarness(pack, config, args.out / "tmp")
    result = harness.run()
    paths = write_outputs(result, args.out, harness.ctx.store)
    harness.ctx.store.close()
    summary = dict(result.summary, wall_clock_s=round(time.perf_counter() - started, 1))
    print(json.dumps(summary, indent=2, default=str))
    print("wrote: " + ", ".join(str(p) for p in paths.values()), file=sys.stderr)
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    from .measure.simulator_adapter import run_simulator

    report = run_simulator(args.pack, args.out / "actions.jsonl", args.out / "sim", args.seed)
    print(json.dumps({"seed": report.get("seed"), "events_written": report.get("events_written"),
                      "policy_problem_count": report.get("policy_problem_count")}, indent=2))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from datetime import date

    from .measure import impact as impact_mod
    from .measure.judge import judge_all, learnings
    from .measure.latency import latency_report
    from .measure.simulator_adapter import load_outcome_events

    config = load_config(args.config)
    pack = load_pack(args.pack, config.goal_interpretations)
    run_dir = args.out
    read = lambda name: [json.loads(l) for l in (run_dir / name).read_text(encoding="utf-8").splitlines() if l.strip()]
    decisions, actions, control_log = read("decisions.jsonl"), read("actions.jsonl"), read("control_log.jsonl")
    sim_dir = run_dir / "sim"
    events = load_outcome_events(sim_dir / "outcome_events.jsonl")
    sim_report = json.loads((sim_dir / "simulator_report.json").read_text(encoding="utf-8"))
    stale = config.freshness.stale_trigger_after_s

    outcomes, horizon_end = judge_all(decisions, events, pack, stale, control_log)
    with (run_dir / "outcomes.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for o in outcomes:
            fh.write(json.dumps(o, ensure_ascii=False) + "\n")

    store_path = run_dir / "tmp" / "state.db"
    if store_path.exists():
        store = StateStore(store_path)
        # Idempotent: re-running `report` must not append the same outcome or learning twice.
        for o in outcomes:
            key = f"outcome:{o['decision_id']}:{o['goal_id']}:{o['role']}"
            if store.claim(key, o["outcome_id"], horizon_end) is None:
                store.append(o["viewer_id"], "outcome", horizon_end, o, ref=f"outcome:{o['outcome_id']}")
        for user, items in learnings(outcomes, events, actions).items():
            for item in items:
                key = "learning:" + user + ":" + json.dumps(item, sort_keys=True)
                if store.claim(key, user, horizon_end) is None:
                    store.append(user, "learning", horizon_end, item)
        store.commit()
        store.export_all(run_dir / "viewer_state")
        store.close()

    res = impact_mod.impact(pack, decisions, outcomes, events, stale, sim_report)
    pilot = impact_mod.pilot_reanalysis(args.pack)
    res["pilot"] = pilot
    (run_dir / "impact.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    (run_dir / "impact_report.md").write_text(
        impact_mod.to_markdown(res, pilot, date.today().strftime("%d %b %Y"), sim_report.get("seed", "?")), encoding="utf-8")
    lat = latency_report(decisions, pack, config.latency.publish_budget_s, config.latency.failure_after_s,
                         {"max_model_calls": config.budget.max_model_calls, "max_tool_calls": config.budget.max_tool_calls})
    (run_dir / "latency_report.json").write_text(json.dumps(lat, indent=2, default=str), encoding="utf-8")

    g, p = res["primary"]["goal_success"], res["primary"]["play_7d"]
    print(json.dumps({
        "outcomes": len(outcomes), "outcome_data_ends": horizon_end.isoformat(),
        "goal_success": {"agent": round(g["agent"]["rate"], 4), "holdout": round(g["holdout"]["rate"], 4),
                         "diff": round(g["diff"], 4), "ci95": [round(x, 4) for x in g["ci95"]]},
        "play_7d": {"agent": round(p["agent"]["rate"], 4), "holdout": round(p["holdout"]["rate"], 4),
                    "diff": round(p["diff"], 4), "ci95": [round(x, 4) for x in p["ci95"]]},
        "latency": lat["overall"], "simulator_policy_problems": sim_report.get("policy_problem_count"),
    }, indent=2, default=str))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    for step in (cmd_replay, cmd_simulate, cmd_report):
        code = step(args)
        if code:
            return code
    print(f"done: see {args.out / 'impact_report.md'} and {args.out / 'latency_report.json'}", file=sys.stderr)
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    from .audit.explain import explain

    store = StateStore(args.out / "tmp" / "state.db")
    print(json.dumps(explain(store, args.viewer, parse_ts(args.at) if args.at else None), indent=2,
                     ensure_ascii=False, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ott_agent", description="One User, One Agent")
    sub = parser.add_subparsers(dest="command", required=True)

    p_load = sub.add_parser("load", help="load and validate the data pack, print counts")
    p_load.add_argument("--pack", type=Path, default=Path("pack"))
    p_load.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p_load.set_defaults(func=cmd_load)

    p_facts = sub.add_parser("facts", help="resolve a viewer's facts at a moment (what the agent could know)")
    p_facts.add_argument("--viewer", help="user_id (optional when --trigger is given)")
    p_facts.add_argument("--at", help="ISO-8601 UTC time; defaults to the trigger's deliver_at")
    p_facts.add_argument("--trigger", help="replay trigger event_id to resolve against")
    p_facts.add_argument("--pack", type=Path, default=Path("pack"))
    p_facts.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p_facts.set_defaults(func=cmd_facts)

    p_decide = sub.add_parser("decide", help="decide, verify and persist one replay trigger")
    p_decide.add_argument("--trigger", required=True, help="replay trigger event_id")
    p_decide.add_argument("--draft", action="store_true", help="stop before verification (P4 draft only)")
    p_decide.add_argument("--store", default=":memory:", help="SQLite state file (default: in memory)")
    p_decide.add_argument("--export", type=Path, help="write viewer_state/<user>.json into this folder")
    p_decide.add_argument("--with-facts", action="store_true", help="include the resolved facts")
    p_decide.add_argument("--pack", type=Path, default=Path("pack"))
    p_decide.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p_decide.set_defaults(func=cmd_decide)

    def common(p, seed=False):
        p.add_argument("--pack", type=Path, default=Path("pack"))
        p.add_argument("--out", type=Path, default=Path("sample_run"))
        p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        if seed:
            p.add_argument("--seed", default="oua-practice", help="simulator seed (evaluators use their own)")
        return p

    for name, func, help_text in (("replay", cmd_replay, "replay the whole trigger day and write decisions/actions/state"),
                                  ("run", cmd_run, "replay, simulate and report: the one command")):
        p = common(sub.add_parser(name, help=help_text), seed=True)
        p.add_argument("--provider", choices=["mock", "claude"], help="model provider (default mock)")
        p.add_argument("--workers", type=int, help="replay workers (default from config)")
        p.add_argument("--model-seed", help="mock model latency seed (default from config)")
        p.set_defaults(func=func)
    common(sub.add_parser("simulate", help="run the pack's outcome simulator on <out>/actions.jsonl"), seed=True).set_defaults(func=cmd_simulate)
    common(sub.add_parser("report", help="judge outcomes; impact, latency and pilot reports")).set_defaults(func=cmd_report)
    p_explain = common(sub.add_parser("explain", help="what the agent knew/decided/observed for a viewer at a moment"))
    p_explain.add_argument("--viewer", required=True)
    p_explain.add_argument("--at", help="ISO-8601 UTC time (default: everything)")
    p_explain.set_defaults(func=cmd_explain)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except PackSchemaError as exc:
        print(f"pack error: {exc}", file=sys.stderr)
        return 2
