"""Goal catalogue: versioned rules, measurability and eligibility from evidence (Step 2; D-013, D-014, D-027).

- Rules are loaded from config/goals.json and versioned. A correction creates a new version; nothing is overwritten.
- A goal is selectable only if its success can be measured: the success event must be a known observable event type
  and the condition must be defined. This catches G10 (no event) and G11 (`tv_app_open` never occurs) generically.
- Eligibility uses only *usable* facts (D-027). Structured fields in goals.json are applied as written
  (signed_up_within_days, requires_no_prior_event, subscription_status). Rules stated only in text use our recorded
  interpretations (assumption IDs A-G0x-1, configurable thresholds in config.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from .catalogue import Catalogue
from .config import Config
from .facts import PLAY_DAYS_WINDOW_DAYS, ViewerFacts
from .timeutil import iso, parse_ts


def _days(td: timedelta) -> str:
    return f"{td.total_seconds() / 86400:.1f} days"


# Event types the platform emits (pack README event table) plus the simulator's outcome-only events.
OBSERVABLE_EVENTS = frozenset(
    {
        "signup_completed", "login_success", "app_open", "app_dialect_changed", "home_viewed", "title_viewed",
        "search", "play_started", "play_progress", "episode_completed", "paywall_viewed", "purchase_completed",
        "subscription_renewed", "subscription_lapsed", "message_delivered", "message_clicked",
        "notification_opt_out", "home_page_shown", "message_failed",
    }
)  # fmt: skip

# requires_no_prior_event -> the history fact that proves it
NO_PRIOR_EVENT_FACT = {"play_started": "has_played", "episode_completed": "has_completed_episode"}


@dataclass(frozen=True)
class GoalRule:
    goal_id: str
    name: str
    description: str
    eligibility: dict
    success: dict
    version: str  # e.g. "G03@2026-09-30" or "G03@2026-10-01T08:30:00.000Z" after a correction
    measurable: bool
    unmeasurable_reason: str | None = None

    @property
    def window(self) -> timedelta:
        return timedelta(hours=float(self.success.get("window_hours") or 0))

    def success_signal(self) -> str:
        return f"{self.success.get('event')} ({self.success.get('condition')}) within {self.success.get('window_hours')} h"


def check_measurable(
    success: dict, observable: frozenset[str] = OBSERVABLE_EVENTS
) -> tuple[bool, str | None]:
    event, condition, window = (
        success.get("event"),
        (success.get("condition") or "").strip(),
        success.get("window_hours"),
    )
    if not event:
        return False, "no success event defined"
    if event not in observable:
        return False, f"success event '{event}' is never emitted by the platform or simulator"
    if not condition:
        return False, "success condition is empty"
    if not window or float(window) <= 0:
        return False, "no positive success window"
    return True, None


@dataclass
class GoalCatalogue:
    rules: dict[str, GoalRule]  # current version per goal_id
    history: list[GoalRule] = field(default_factory=list)  # every version ever loaded, oldest first

    @classmethod
    def from_file(cls, path: Path) -> GoalCatalogue:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        base = data.get("version", "v1")
        rules = {}
        for g in data["goals"]:
            ok, why = check_measurable(g.get("success") or {})
            rules[g["goal_id"]] = GoalRule(
                g["goal_id"], g["name"], g.get("description", ""), g.get("eligibility") or {},
                g.get("success") or {}, f"{g['goal_id']}@{base}", ok, why,
            )  # fmt: skip
        return cls(rules, list(rules.values()))

    def measurable(self) -> list[GoalRule]:
        return [r for r in self.rules.values() if r.measurable]

    def version(self, version: str) -> GoalRule | None:
        return next((r for r in self.history if r.version == version), None)

    def correct(self, goal_id: str, new_success: dict, effective_at: datetime) -> GoalRule:
        """Apply a success-rule correction as a new version (the old one stays in history)."""
        old = self.rules[goal_id]
        success = {**old.success, **new_success}
        ok, why = check_measurable(success)
        rule = replace(old, success=success, version=f"{goal_id}@{iso(effective_at)}", measurable=ok,
                       unmeasurable_reason=why)  # fmt: skip
        self.rules[goal_id] = rule
        self.history.append(rule)
        return rule


@dataclass
class Eligibility:
    goal_id: str
    eligible: bool
    reason: str
    evidence: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)  # e.g. the series chosen for G07


def signup_age(vf: ViewerFacts, at: datetime) -> tuple[timedelta | None, str | None]:
    """How long ago the viewer signed up, if that is provable. A first-seen lower bound is not proof."""
    f = vf["signup_at"]
    if not f.usable or f.value is None:
        return None, "signup time unknown"
    if f.source == "derived:first_seen":
        return None, "signup time only bounded by first activity (no profile row or signup event)"
    return at - parse_ts(f.value), None


def evaluate(rule: GoalRule, vf: ViewerFacts, at: datetime, catalogue: Catalogue, cfg: Config) -> Eligibility:
    """Is the viewer eligible for this goal, from usable facts only? Every check that passes adds its evidence."""
    gid = rule.goal_id
    if not rule.measurable:
        return Eligibility(gid, False, f"not measurable: {rule.unmeasurable_reason}")
    el = rule.eligibility
    evidence = [f"goal_rule:{rule.version}"]
    assumptions: list[str] = []
    detail: dict = {}

    def fail(reason: str) -> Eligibility:
        return Eligibility(gid, False, reason, evidence, assumptions)

    if "signed_up_within_days" in el:
        age, why = signup_age(vf, at)
        if age is None:
            return fail(why)
        if age > timedelta(days=el["signed_up_within_days"]):
            return fail(f"signed up {_days(age)} ago (rule: within {el['signed_up_within_days']} days)")
        evidence += vf["signup_at"].evidence or [vf["signup_at"].source]

    if "requires_no_prior_event" in el:
        fact_name = NO_PRIOR_EVENT_FACT.get(el["requires_no_prior_event"])
        if fact_name is None:
            return fail(f"cannot check 'no prior {el['requires_no_prior_event']}'")
        if vf.usable(fact_name) is not False:
            return fail(f"already has {el['requires_no_prior_event']}")
        evidence.append(f"fact:{fact_name}=false")

    status = vf.usable("subscription_status")
    if "subscription_status" in el:
        if status is None:
            return fail("subscription status not usable (uncertain or stale)")
        if status not in el["subscription_status"]:
            return fail(f"subscription status is {status} (rule: {', '.join(el['subscription_status'])})")
        evidence += vf["subscription_status"].evidence or [vf["subscription_status"].source]
        if vf["subscription_status"].assumption:
            assumptions.append(f"subscription_status: {vf['subscription_status'].assumption}")

    # Text-only rules: recorded interpretations (D-014).
    if gid == "G05":
        if vf.usable("ever_paid") is not False:
            return fail("has paid before (billing event or an older profile row)")
        assumptions.append(
            "[A-G05-1] 'never had a paid plan' = status never AND no paid event or paid profile row"
        )
    elif gid == "G06":
        lapsed = vf.usable("last_lapsed_at")
        if lapsed is None:
            return fail("no lapse event seen: 'recently' cannot be proven")
        ago = at - parse_ts(lapsed)
        if ago > timedelta(days=cfg.g06_recent_lapse_days):
            return fail(f"lapsed {_days(ago)} ago (interpretation: within {cfg.g06_recent_lapse_days} days)")
        evidence += vf["last_lapsed_at"].evidence
        assumptions.append(
            f"[A-G06-1] 'recently lapsed' = lapse event within {cfg.g06_recent_lapse_days} days"
        )
    elif gid == "G07":
        # Left alone for at least 72 h, but touched recently enough to still be "in progress" (D-031).
        cutoff = at - timedelta(hours=cfg.g07_min_gap_h)
        oldest = at - timedelta(days=cfg.g07_max_age_days)
        candidates = [
            s for s in vf.value("series_in_progress")
            if oldest <= parse_ts(s["last_play_at"]) <= cutoff
            and any(catalogue.is_available(t, at) for t in catalogue.by_slug(s["slug"]))
        ]  # fmt: skip
        if not candidates:
            return fail(f"no unfinished, available series last played between {cfg.g07_min_gap_h} h and "
                        f"{cfg.g07_max_age_days} days ago")  # fmt: skip
        detail["series"] = candidates[0]  # most recently played qualifying series
        evidence.append(candidates[0]["evidence"])
        assumptions.append(
            f"[A-G07-1] unfinished series = show/micro-drama with episodes not completed, last played between "
            f"{cfg.g07_min_gap_h} h and {cfg.g07_max_age_days} days ago; success = play_started on the same content_slug"
        )
    elif gid == "G09":
        days = vf.value("play_ist_days_14d")
        if len(days) > cfg.g09_max_play_days:
            return fail(f"played on {len(days)} IST days in the last {PLAY_DAYS_WINDOW_DAYS} (rule: at most "
                        f"{cfg.g09_max_play_days})")  # fmt: skip
        evidence += vf["play_ist_days_14d"].evidence
        assumptions.append(f"[A-G09-1] play days = IST days (corrected event time) with any playback event, last "
                           f"{PLAY_DAYS_WINDOW_DAYS} days")  # fmt: skip
    elif gid == "G12":
        age, why = signup_age(vf, at)
        if age is None or age <= timedelta(days=cfg.g12_absence_days):
            return fail("not an existing user who stopped visiting (new or signup unknown)")
        for fact_name, what in (("last_app_open_at", "opened the app"), ("last_activity_at", "was active")):
            last = vf.value(fact_name)
            if last and at - parse_ts(last) <= timedelta(days=cfg.g12_absence_days):
                return fail(f"{what} {_days(at - parse_ts(last))} ago")
            evidence += vf[fact_name].evidence or [f"fact:{fact_name}=none"]
        assumptions.append(f"[A-G12-1] absence = no app_open and no other activity in the {cfg.g12_absence_days} "
                           f"days before the trigger; user signed up more than {cfg.g12_absence_days} days ago")  # fmt: skip

    return Eligibility(gid, True, "eligible", evidence, assumptions, detail)
