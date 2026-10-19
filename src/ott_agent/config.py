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
    # Goal interpretations for rules that goals.json states only in text (D-014). Assumption IDs in brackets.
    g06_recent_lapse_days: int = 30  # [A-G06-1] "recently lapsed" = a lapse event within this many days
    g07_min_gap_h: int = 72  # [A-G07-1] "not played it for 3 or more days"
    g07_max_age_days: int = (
        14  # [A-G07-1] still "in progress" only if last played within this many days (D-031)
    )
    g09_max_play_days: int = 1  # [A-G09-1] "plays on at most 1 day in the last 14 days"
    g12_absence_days: int = 14  # [A-G12-1] "no app_open in the 14 days before the trigger"
    # Who counts as a new user for the activation funnel (D-019): signed up within this many days.
    new_user_days: int = 7
    # Goal priority, highest first (D-019, G09 moved above G08 by D-030). "G02:new_user" = G02 only for new users
    # (activation funnel); the plain "G02" later in the list is for existing users.
    goal_priority: tuple[str, ...] = (
        "G04", "G01", "G02:new_user", "G03", "G07", "G12", "G02", "G09", "G08", "G06", "G05",
    )  # fmt: skip
    # Intervention (D-033, D-034). Platform limits themselves come from config/interventions.json.
    page_max_rows: int = 4  # our own cap, below the platform's 6: a focused page
    popularity_days: int = 30  # "popular" = distinct viewers with playback in this many days
    new_release_days: int = 30  # "new release" = released in the last 30 days (same as G08's success rule)
    message_goals: tuple[str, ...] = ("G03", "G12")  # only goals that need a later visit get a reminder
    message_hour_ist: int = 19  # reminders at 19:00 IST the next day
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
        for f in fields(cls):  # JSON has lists; tuple settings (goal_priority, message_goals) stay tuples
            if f.name in data and isinstance(getattr(cls, f.name, None), tuple):
                data[f.name] = tuple(data[f.name])
        return cls(**data)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pack_path"] = str(self.pack_path)
        return d
