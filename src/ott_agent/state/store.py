"""Durable per-viewer state in SQLite: journal, active goal, idempotency keys and message ledger.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from ..clock import parse_ts, to_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    at TEXT NOT NULL,
    ref TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_user ON journal(user_id, seq);
CREATE TABLE IF NOT EXISTS active_goal (
    user_id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    decision_id TEXT NOT NULL,
    start TEXT NOT NULL,
    deadline TEXT NOT NULL,
    plan TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS active_goal_by_goal ON active_goal(goal_id);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_ledger (
    action_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    ist_date TEXT NOT NULL,
    channel TEXT NOT NULL,
    send_at TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_user_day ON message_ledger(user_id, ist_date);
"""

JOURNAL_KINDS = ("facts", "decision", "action", "outcome", "learning", "conflict", "goal", "event", "control")
COUNTED_MESSAGE_STATUSES = ("scheduled", "sent", "delivered")


class StoreUnavailable(RuntimeError):
    """The user-data store cannot be read right now (outage or timeout)."""


class SingleGoalViolation(RuntimeError):
    """Attempt to open a second active goal for a viewer."""


@dataclass(frozen=True)
class ActiveGoal:
    user_id: str
    goal_id: str
    rule_version: int
    decision_id: str
    start: datetime
    deadline: datetime
    plan: dict[str, Any]


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=_default)


def _default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return to_iso(obj)
    if isinstance(obj, (set, tuple)):
        return list(obj)
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    raise TypeError(f"not JSON serialisable: {type(obj).__name__}")


class StateStore:
    def __init__(self, path: Path | str = ":memory:", buffer_path: Path | None = None) -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.available = True
        self.buffer_path = buffer_path
        self._buffer: list[tuple[str, tuple]] = []
        self._claimed: dict[str, str] = {r["key"]: r["decision_id"] for r in self.conn.execute("SELECT key, decision_id FROM idempotency")}

    def set_available(self, available: bool) -> int:
        """Toggle the outage state; on recovery, replay buffered writes. Returns the number replayed."""
        self.available = available
        if not available:
            return 0
        replayed = 0
        for sql, params in self._buffer:
            self.conn.execute(sql, params)
            replayed += 1
        self._buffer.clear()
        self.conn.commit()
        if self.buffer_path and self.buffer_path.exists():
            self.buffer_path.unlink()
        return replayed

    def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        if not self.available:
            raise StoreUnavailable("user data store unavailable")
        return self.conn.execute(sql, params).fetchall()

    def _write(self, sql: str, params: tuple) -> None:
        if self.available:
            self.conn.execute(sql, params)
            return
        self._buffer.append((sql, params))
        if self.buffer_path:
            with self.buffer_path.open("a", encoding="utf-8") as fh:
                fh.write(_dumps({"sql": sql, "params": list(params)}) + "\n")

    def commit(self) -> None:
        if self.available:
            self.conn.commit()

    @property
    def buffered_writes(self) -> int:
        return len(self._buffer)

    def append(self, user_id: str, kind: str, at: datetime, payload: Any, ref: str | None = None) -> None:
        if kind not in JOURNAL_KINDS:
            raise ValueError(f"unknown journal kind {kind!r}")
        self._write("INSERT INTO journal(user_id, kind, at, ref, payload) VALUES (?, ?, ?, ?, ?)",
                    (user_id, kind, to_iso(at), ref, _dumps(payload)))

    def journal(self, user_id: str, until: datetime | None = None, kinds: Iterable[str] | None = None) -> list[dict]:
        rows = self._read("SELECT seq, kind, at, ref, payload FROM journal WHERE user_id = ? ORDER BY seq", (user_id,))
        wanted = set(kinds) if kinds else None
        out = []
        for r in rows:
            if wanted and r["kind"] not in wanted:
                continue
            if until is not None and parse_ts(r["at"]) > until:
                continue
            out.append({"seq": r["seq"], "kind": r["kind"], "at": r["at"], "ref": r["ref"], "payload": json.loads(r["payload"])})
        return out

    def users(self) -> list[str]:
        return [r["user_id"] for r in self._read("SELECT DISTINCT user_id FROM journal ORDER BY user_id")]

    def claim(self, key: str, decision_id: str, at: datetime) -> str | None:
        """Claim an idempotency key. Returns None if newly claimed, else the decision that already owns it."""
        existing = self._claimed.get(key)
        if existing is not None:
            return existing
        self._claimed[key] = decision_id
        self._write("INSERT OR IGNORE INTO idempotency(key, decision_id, at) VALUES (?, ?, ?)", (key, decision_id, to_iso(at)))
        return None

    def owner(self, key: str) -> str | None:
        """Which decision owns an idempotency key (read-only; does not claim)."""
        return self._claimed.get(key)

    def open_goal(self, goal: ActiveGoal) -> None:
        if self.available:
            try:
                self.conn.execute(
                    "INSERT INTO active_goal(user_id, goal_id, rule_version, decision_id, start, deadline, plan) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (goal.user_id, goal.goal_id, goal.rule_version, goal.decision_id, to_iso(goal.start),
                     to_iso(goal.deadline), _dumps(goal.plan)))
            except sqlite3.IntegrityError as exc:
                raise SingleGoalViolation(f"{goal.user_id} already has an active goal") from exc
        else:
            raise StoreUnavailable("cannot open a goal while the store is unavailable")

    def close_goal(self, user_id: str) -> ActiveGoal | None:
        goal = self.active_goal(user_id)
        if goal is not None:
            self._write("DELETE FROM active_goal WHERE user_id = ?", (user_id,))
        return goal

    def active_goal(self, user_id: str) -> ActiveGoal | None:
        rows = self._read("SELECT * FROM active_goal WHERE user_id = ?", (user_id,))
        return _goal(rows[0]) if rows else None

    def open_goals_for(self, goal_id: str) -> list[ActiveGoal]:
        """Open goals of one goal_id (index lookup), e.g. to apply a rule correction to them only."""
        return [_goal(r) for r in self._read("SELECT * FROM active_goal WHERE goal_id = ? ORDER BY user_id", (goal_id,))]

    def update_goal(self, user_id: str, **changes: Any) -> None:
        allowed = {"rule_version", "deadline", "plan"}
        if set(changes) - allowed:
            raise ValueError(f"cannot update {set(changes) - allowed}")
        cols = ", ".join(f"{k} = ?" for k in changes)
        values = tuple(to_iso(v) if isinstance(v, datetime) else _dumps(v) if k == "plan" else v for k, v in changes.items())
        self._write(f"UPDATE active_goal SET {cols} WHERE user_id = ?", values + (user_id,))

    def record_message(self, action_id: str, user_id: str, ist_date: str, channel: str, send_at: datetime,
                       status: str = "scheduled") -> None:
        self._write("INSERT INTO message_ledger(action_id, user_id, ist_date, channel, send_at, status) VALUES (?, ?, ?, ?, ?, ?)",
                    (action_id, user_id, ist_date, channel, to_iso(send_at), status))

    def set_message_status(self, action_id: str, status: str) -> None:
        self._write("UPDATE message_ledger SET status = ? WHERE action_id = ?", (status, action_id))

    def messages_on(self, user_id: str, ist_date: str, channels: Iterable[str] = ("push", "whatsapp"),
                    exclude_action_id: str | None = None) -> int:
        channels = tuple(channels)
        marks = ",".join("?" * len(channels))
        statuses = ",".join("?" * len(COUNTED_MESSAGE_STATUSES))
        rows = self._read(
            f"SELECT COUNT(*) AS n FROM message_ledger WHERE user_id = ? AND ist_date = ? "
            f"AND channel IN ({marks}) AND status IN ({statuses}) AND action_id != ?",
            (user_id, ist_date, *channels, *COUNTED_MESSAGE_STATUSES, exclude_action_id or ""))
        return int(rows[0]["n"])

    def pending_messages(self, user_id: str) -> list[dict]:
        return [dict(r) for r in self._read(
            "SELECT * FROM message_ledger WHERE user_id = ? AND status = 'scheduled' ORDER BY send_at", (user_id,))]

    def export_viewer(self, user_id: str) -> dict[str, Any]:
        """Everything known about one viewer, grouped for reading (sample_run/viewer_state/<id>.json)."""
        entries = self.journal(user_id)
        grouped: dict[str, list] = {}
        for e in entries:
            grouped.setdefault(e["kind"], []).append({k: e[k] for k in ("seq", "at", "ref", "payload")})
        goal = self.active_goal(user_id)
        return {
            "user_id": user_id,
            "active_goal": None if goal is None else {
                "goal_id": goal.goal_id, "rule_version": goal.rule_version, "decision_id": goal.decision_id,
                "start": to_iso(goal.start), "deadline": to_iso(goal.deadline), "plan": goal.plan},
            "messages": [dict(r) for r in self._read("SELECT * FROM message_ledger WHERE user_id = ? ORDER BY send_at", (user_id,))],
            "journal": grouped,
        }

    def export_all(self, out_dir: Path, users: Iterable[str] | None = None) -> int:
        out_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        for user_id in users if users is not None else self.users():
            (out_dir / f"{user_id}.json").write_text(
                json.dumps(self.export_viewer(user_id), ensure_ascii=False, separators=(",", ":"), default=_default), encoding="utf-8")
            n += 1
        return n

    def close(self) -> None:
        self.commit()
        self.conn.close()


def _goal(r: sqlite3.Row) -> ActiveGoal:
    return ActiveGoal(r["user_id"], r["goal_id"], r["rule_version"], r["decision_id"], parse_ts(r["start"]),
                      parse_ts(r["deadline"]), json.loads(r["plan"]))
