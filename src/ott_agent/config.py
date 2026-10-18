"""Run configuration. Every threshold that encodes a decision lives here, with the decision it comes from."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Config:
    pack_path: Path = Path("../one_user_one_agent_pack")

    # D-006: a trigger older than this (true age, deliver_at - event_time) is not a live visit.
    stale_trigger_s: int = 600
    # D-005 choice 4: a corrected event time further back than this is implausible -> clock_anomaly.
    max_event_age_days: int = 30
    # D-005 choice 6: two-stage outcome verdict.
    grace_provisional_h: int = 24
    grace_final_days: int = 7
    # D-027: profile-snapshot facts (status, consent, preferred dialect) are usable only while the snapshot is
    # younger than this. The snapshot is a daily export, so 48 h allows one missed export.
    profile_max_age_h: int = 48
    # Phone clock is reported as skewed when |sent_at - received_at| exceeds this (network delay is seconds).
    clock_skew_report_s: int = 60

    @classmethod
    def from_json(cls, path: Path) -> Config:
        """Load overrides from a JSON file; unknown keys are rejected so typos don't pass silently."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        if "pack_path" in data:
            data["pack_path"] = Path(data["pack_path"])
        return cls(**data)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pack_path"] = str(self.pack_path)
        return d
