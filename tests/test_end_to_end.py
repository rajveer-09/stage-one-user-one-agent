"""End-to-end tests: the whole loop on the mini pack, determinism, and no hard-coded replay facts.

Author: Rajveer Sharma
Date: 19 Oct 2026
"""

import json
import re
from datetime import timedelta

from conftest import INTERPRETATIONS, REPO, event
from ott_agent.clock import parse_ts, to_iso
from ott_agent.config import AgentConfig
from ott_agent.measure.impact import impact
from ott_agent.measure.judge import judge_all
from ott_agent.pack.events import parse_event
from ott_agent.pack.loader import load_pack
from ott_agent.replay.harness import ReplayHarness, write_outputs
from test_replay import make_replay, trig

TIMING = {"decided_in_ms", "latency_breakdown_ms", "queue_wait_ms", "published_at"}


def strip_timing(d):
    out = {k: v for k, v in d.items() if k not in TIMING}
    if isinstance(out.get("goal"), dict):
        out["goal"] = {k: v for k, v in out["goal"].items() if k not in ("start", "deadline", "judge_at")}
    if isinstance(out.get("home_page"), dict):
        out["home_page"] = {k: v for k, v in out["home_page"].items() if k not in ("publish_at", "live_until")}
    return out


def replay(root, work):
    pack = load_pack(root, INTERPRETATIONS)
    harness = ReplayHarness(pack, AgentConfig(goal_interpretations=INTERPRETATIONS), work)
    return pack, harness, harness.run()


def test_whole_loop_on_the_mini_pack(tmp_path):
    root = make_replay(tmp_path, [trig("R3", "U-3", 0), trig("R4", "U-4", 60), trig("R2", "U-2", 90, dialect="d2"),
                                  trig("R3", "U-3", 120)])
    pack, harness, result = replay(root, tmp_path / "work")
    paths = write_outputs(result, tmp_path / "out", harness.ctx.store)

    # Simulated outcomes: U-3 plays on the next IST day (G03 success); nothing for U-4 or the holdout viewer.
    t0 = parse_ts("2026-10-01T05:00:00Z")
    outcome_events = [parse_event(event("O1", "U-3", "app_open", to_iso(t0 + timedelta(days=1))), "outcome"),
                      parse_event(event("O2", "U-3", "play_started", to_iso(t0 + timedelta(days=1, minutes=1)), title_id="T5"), "outcome"),
                      parse_event(event("O3", "U-9", "app_open", to_iso(t0 + timedelta(days=15))), "outcome")]
    pack.goals.vocabulary.add("play_started")
    outcomes, horizon = judge_all(result.decisions, outcome_events, pack, 900, result.control_log)
    verdicts = {(o["viewer_id"], o["role"]): o["verdict"] for o in outcomes}
    assert verdicts[("U-3", "itt")] == "succeeded"
    assert verdicts[("U-4", "itt")] == "failed"
    assert ("U-2", "itt") in verdicts  # the holdout viewer is judged on their shadow goal too

    res = impact(pack, result.decisions, outcomes, outcome_events, 900)
    assert res["population"] == {"triggered_viewers": 3, "agent": 2, "holdout": 1, "agent_viewers_served": 2}
    assert all(json.loads(line) for line in paths["decisions"].read_text().splitlines())


def test_same_inputs_give_the_same_decisions(tmp_path):
    triggers = [trig("R3", "U-3", 0), trig("R4", "U-4", 30), trig("R1", "U-1", 45), trig("R3", "U-3", 50)]
    root = make_replay(tmp_path, triggers)
    _, _, first = replay(root, tmp_path / "a")
    _, _, second = replay(root, tmp_path / "b")
    assert [strip_timing(d) for d in first.decisions] == [strip_timing(d) for d in second.decisions]
    key = lambda acts: [{k: v for k, v in a.items() if k != "published_at"} for a in acts]
    assert key(first.actions) == key(second.actions)


def test_source_has_no_replay_specific_ids_or_dates():
    """Generalisation: nothing in src/ names a viewer, title, trigger or date from this replay day."""
    pattern = re.compile(r"\b(U-\d{5}|T\d{4}|20\d\d-\d\d-\d\dT)|cmp\d{3}")
    offenders = []
    for path in (REPO / "src").rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert offenders == []
