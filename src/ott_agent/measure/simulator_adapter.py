"""Run the pack's outcome simulator on our published actions and read its output back.

Author: Rajveer Sharma
Date: 19 Oct 2026
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..pack.events import Event, ReadStats, read_events


def run_simulator(pack_root: Path, actions: Path, out_dir: Path, seed: str) -> dict[str, Any]:
    script = (pack_root / "simulator" / "simulate_outcomes.py").resolve()
    if not script.exists():
        raise FileNotFoundError(f"simulator not found at {script}")
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-I", str(script), "--pack", str(pack_root.resolve()), "--actions", str(actions.resolve()),
           "--out", str(out_dir.resolve()), "--seed", seed]
    subprocess.run(cmd, check=True, cwd=out_dir.resolve())
    return json.loads((out_dir / "simulator_report.json").read_text(encoding="utf-8"))


def load_outcome_events(path: Path) -> list[Event]:
    stats = ReadStats()
    with path.open(encoding="utf-8") as fh:
        events = list(read_events(fh, "outcome", stats, path.name))
    if stats.malformed:
        print(f"warning: {stats.malformed} unparseable outcome events skipped; first: {stats.errors[:2]}", file=sys.stderr)
    return sorted(events, key=lambda e: e.received_at)
