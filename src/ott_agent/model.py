"""Model provider: a second opinion on the goal, never evidence (D-003, D-022, D-035).

Two providers share one interface:
  MockProvider   default; deterministic; reproduces replay/model_faults.jsonl at the named triggers
  OllamaProvider optional local model over HTTP (urllib, no extra dependency)

The request carries typed facts only. Free text from the data (titles, descriptions, search queries, deeplinks)
never reaches the model (D-012).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

QUESTION = (
    "You review a goal proposed for one viewer of a streaming app. Reply with JSON only: "
    '{"goal_id": <one of eligible_goals or the proposed goal>, "eligible": <true|false>, '
    '"confidence": <0..1>, "reason": <short text>}.'
)
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "goal_id": {"type": ["string", "null"]},
        "eligible": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["goal_id", "eligible", "confidence"],
}


@dataclass
class RawReply:
    """What the provider returned. `payload` may be anything (dict, broken string, None)."""

    kind: str  # "ok" | "error"
    payload: Any
    latency_s: float  # simulated (mock) or measured (Ollama)
    error: str | None = None


def build_request(vf, proposed_goal: str | None, eligible_goals: list[str], at) -> dict:
    """Typed facts only (D-012, D-035)."""
    from .timeutil import parse_ts

    def days_since(name):
        v = vf.usable(name) if name in vf.facts else None
        return round((at - parse_ts(v)).total_seconds() / 86400, 1) if v else None

    return {
        "proposed_goal": proposed_goal,
        "eligible_goals": eligible_goals,
        "facts": {
            "subscription_status": vf.usable("subscription_status"),
            "ever_paid": vf.usable("ever_paid"),
            "days_since_signup": days_since("signup_at"),
            "has_played": vf.usable("has_played"),
            "has_completed_episode": vf.usable("has_completed_episode"),
            "series_in_progress": len(vf.value("series_in_progress") or []),
            "play_days_last_14d": len(vf.value("play_ist_days_14d") or []),
            "days_since_last_app_open": days_since("last_app_open_at"),
        },
        "question": QUESTION,
    }


def parse_review(payload: Any) -> tuple[dict | None, str | None]:
    """Strictly parse a model answer. Returns (review, None) or (None, error)."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            return None, f"malformed JSON: {exc.msg}"
    if not isinstance(payload, dict):
        return None, "answer is not a JSON object"
    gid, eligible, conf = payload.get("goal_id"), payload.get("eligible"), payload.get("confidence")
    if gid is not None and not isinstance(gid, str):
        return None, "goal_id is not a string"
    if not isinstance(eligible, bool):
        return None, "eligible is not a boolean"
    if not isinstance(conf, (int, float)) or isinstance(conf, bool) or not 0 <= conf <= 1:
        return None, "confidence is not a number between 0 and 1"
    reason = payload.get("reason")
    return {"goal_id": gid, "eligible": eligible, "confidence": float(conf),
            "reason": reason if isinstance(reason, str) else None}, None  # fmt: skip


class MockProvider:
    """Deterministic stand-in for a model. Agrees with the proposed goal unless a fault is configured."""

    name = "mock"

    def __init__(self, latency_s: float = 0.8, faults: dict[str, dict] | None = None):
        self.latency_s = latency_s
        self.faults = faults or {}  # trigger_event_id -> fault row from model_faults.jsonl

    @classmethod
    def from_pack(cls, pack: Path, latency_s: float) -> MockProvider:
        path = Path(pack) / "replay" / "model_faults.jsonl"
        faults = {}
        if path.exists():
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        row = json.loads(line)
                        faults[row["trigger_event_id"]] = row
        return cls(latency_s, faults)

    def review_goal(self, request: dict, trigger_event_id: str) -> RawReply:
        fault = self.faults.get(trigger_event_id)
        if fault is not None:
            if fault["fault"] == "timeout":
                return RawReply("ok", None, float(fault.get("seconds", 60)))  # never answers in time
            return RawReply("ok", fault.get("model_output"), self.latency_s)  # unsupported claim / malformed
        proposed = request.get("proposed_goal")
        return RawReply("ok", {"goal_id": proposed, "eligible": proposed is not None, "confidence": 0.7,
                               "reason": "consistent with the facts provided"}, self.latency_s)  # fmt: skip


class OllamaProvider:
    """Optional real local model (D-003). Latency is measured, not simulated."""

    name = "ollama"

    def __init__(
        self, model: str = "qwen2.5:3b-instruct", url: str = "http://localhost:11434", timeout_s: float = 3.0
    ):
        self.model, self.url, self.timeout_s = model, url.rstrip("/"), timeout_s

    def review_goal(self, request: dict, trigger_event_id: str) -> RawReply:
        body = json.dumps({
            "model": self.model, "stream": False, "format": REVIEW_SCHEMA, "options": {"temperature": 0},
            "messages": [{"role": "system", "content": request["question"]},
                         {"role": "user", "content": json.dumps({k: v for k, v in request.items() if k != "question"})}],
        }).encode("utf-8")  # fmt: skip
        req = urllib.request.Request(
            self.url + "/api/chat", data=body, headers={"Content-Type": "application/json"}
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return RawReply("ok", data.get("message", {}).get("content"), time.perf_counter() - t0)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            return RawReply("error", None, time.perf_counter() - t0, f"ollama: {exc}")
