"""Dependency calls with budgets, simulated latency, timeouts and outage windows (Step 4; D-036, D-037).

Every call a decision makes goes through a ToolBox, which
  - counts it against the per-decision budgets (model calls, tool calls),
  - adds its (simulated) latency to the decision's elapsed time,
  - applies the dependency's state at that moment (normal / degraded / unavailable) from control events,
  - enforces the call timeout.
Degraded behaviour is deterministic per trigger (hash), so a replay always gives the same result.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from .config import Config

USER_DATA_STORE, MODEL_PROVIDER = "user_data_store", "model_provider"


class BudgetExceeded(Exception):
    pass


class DependencyFailed(Exception):
    def __init__(self, dependency: str, reason: str):
        super().__init__(f"{dependency}: {reason}")
        self.dependency, self.reason = dependency, reason


@dataclass
class Dependencies:
    """Outage / slowdown windows from control events: (dependency, state, start, until)."""

    windows: list[tuple[str, str, datetime, datetime]] = field(default_factory=list)

    def add(self, dependency: str, state: str, start: datetime, until: datetime) -> None:
        self.windows.append((dependency, state, start, until))

    def state(self, dependency: str, at: datetime) -> str:
        for dep, state, start, until in self.windows:
            if dep == dependency and start <= at < until:
                return state  # "unavailable" | "degraded"
        return "ok"


def _h01(*parts) -> float:
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:12], 16) / 16**12


@dataclass
class ToolBox:
    cfg: Config
    deps: Dependencies
    at: datetime  # when the decision runs (dependency state is taken at this moment)
    trigger_id: str
    model_calls: int = 0
    tool_calls: int = 0
    simulated_s: float = 0.0  # simulated dependency time spent so far
    log: list[dict] = field(default_factory=list)

    def _count(self, model: bool = False) -> None:
        if self.tool_calls + 1 > self.cfg.max_tool_calls:
            raise BudgetExceeded(f"tool-call budget of {self.cfg.max_tool_calls} reached")
        if model and self.model_calls + 1 > self.cfg.max_model_calls:
            raise BudgetExceeded(f"model-call budget of {self.cfg.max_model_calls} reached")
        self.tool_calls += 1
        self.model_calls += int(model)

    def store(self, name: str, fn):
        """A read from the user data store."""
        self._count()
        state = self.deps.state(USER_DATA_STORE, self.at)
        latency = self.cfg.store_latency_s
        if state == "unavailable":
            latency = self.cfg.store_timeout_s + 1  # no answer: the read always times out
        elif state == "degraded":
            if _h01(self.trigger_id, name, self.tool_calls, "timeout") < self.cfg.degraded_timeout_share:
                latency = self.cfg.store_timeout_s + 1  # this read times out
            else:
                latency += (
                    _h01(self.trigger_id, name, self.tool_calls, "lat") * self.cfg.degraded_max_latency_s
                )
        if latency > self.cfg.store_timeout_s:
            self.simulated_s += self.cfg.store_timeout_s
            self.log.append(
                {"call": name, "dependency": USER_DATA_STORE, "state": state, "result": "timeout"}
            )
            raise DependencyFailed(
                USER_DATA_STORE, f"{state}: read timed out after {self.cfg.store_timeout_s} s"
            )
        self.simulated_s += latency
        self.log.append(
            {"call": name, "dependency": USER_DATA_STORE, "state": state, "latency_s": round(latency, 3)}
        )
        return fn()

    def local(self, name: str, fn):
        """An in-process lookup (catalogue, popularity cache): counted, no simulated latency."""
        self._count()
        self.log.append({"call": name, "dependency": "local"})
        return fn()

    def model(self, provider, request: dict):
        """A model call. Returns RawReply-like (kind, payload, error) after applying availability and timeout."""
        from .model import RawReply

        self._count(model=True)
        if self.deps.state(MODEL_PROVIDER, self.at) == "unavailable":
            self.simulated_s += self.cfg.model_fail_fast_s
            self.log.append({"call": "model.review_goal", "dependency": MODEL_PROVIDER, "result": "503"})
            return RawReply(
                "error", None, self.cfg.model_fail_fast_s, "model provider unavailable (HTTP 503)"
            )
        reply = provider.review_goal(request, self.trigger_id)
        if reply.latency_s > self.cfg.model_timeout_s:
            self.simulated_s += self.cfg.model_timeout_s
            self.log.append({"call": "model.review_goal", "dependency": MODEL_PROVIDER, "result": "timeout"})
            return RawReply(
                "error", None, self.cfg.model_timeout_s, f"timed out after {self.cfg.model_timeout_s} s"
            )
        self.simulated_s += reply.latency_s
        self.log.append({"call": "model.review_goal", "dependency": MODEL_PROVIDER, "result": reply.kind,
                         "latency_s": round(reply.latency_s, 3)})  # fmt: skip
        return reply
