"""Per-decision budget: model calls, tool calls and (simulated) time.

Author: Rajveer Sharma
Date: 19 Oct 2026
"""

from __future__ import annotations

from ..config import Budget as BudgetConfig


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, cfg: BudgetConfig) -> None:
        self.cfg = cfg
        self.model_calls = 0
        self.tool_calls = 0
        self.simulated_ms = 0.0  # time spent waiting on dependencies (model, degraded store)

    def charge_model(self) -> None:
        if self.model_calls >= self.cfg.max_model_calls:
            raise BudgetExceeded(f"model call budget of {self.cfg.max_model_calls} used up")
        self.model_calls += 1

    def charge_tool(self, n: int = 1) -> None:
        if self.tool_calls + n > self.cfg.max_tool_calls:
            raise BudgetExceeded(f"tool call budget of {self.cfg.max_tool_calls} used up")
        self.tool_calls += n

    def add_latency(self, ms: float) -> None:
        self.simulated_ms += ms

    def time_left_ms(self, real_elapsed_ms: float) -> float:
        return self.cfg.decision_soft_deadline_ms - self.simulated_ms - real_elapsed_ms
