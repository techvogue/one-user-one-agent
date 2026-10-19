"""Goal planner (Step 2): one measurable goal per viewer, or an explicit NO_GOAL (D-019, D-013, D-014).

plan_goal() decides; commit_plan() persists. They are separate so an independent verifier (Step 5) can reject a
plan before anything is written.

Lifecycle of a viewer's single active goal:
  - no open goal                          -> select by priority -> NEW, or NO_GOAL with a reason per goal
  - open goal, deadline passed            -> close (deadline_passed), select again -> REPLAN / NO_GOAL
  - open goal, situation changed          -> close (situation_changed: ...), select again -> REPLAN / NO_GOAL
  - open goal, still valid                -> KEEP (no churn)

"Situation changed" re-checks the goal's state conditions only (subscription status, "no prior event", "never paid").
Entry windows such as "signed up within 2 days" are not re-checked: they decide whether a goal can start, and a
goal that started inside its window keeps its full success window.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from .catalogue import Catalogue
from .config import Config
from .facts import ViewerFacts
from .goals import NO_PRIOR_EVENT_FACT, Eligibility, GoalCatalogue, GoalRule, evaluate, signup_age
from .timeutil import iso, parse_ts

NEW, KEEP, REPLAN, NO_GOAL = "NEW", "KEEP", "REPLAN", "NO_GOAL"


@dataclass
class Selection:
    rule: GoalRule | None
    eligibility: Eligibility | None
    reason: str
    evaluations: dict[str, Eligibility]
    also_eligible: list[str] = field(default_factory=list)


def _priority(goals: GoalCatalogue, cfg: Config) -> list[tuple[str, str | None]]:
    order = []
    for entry in cfg.goal_priority:
        gid, _, cond = entry.partition(":")
        order.append((gid, cond or None))
    listed = {gid for gid, _ in order}
    order += [(gid, None) for gid in sorted(goals.rules) if gid not in listed]  # unknown goals rank last
    return order


def select_goal(
    vf: ViewerFacts, at: datetime, goals: GoalCatalogue, catalogue: Catalogue, cfg: Config
) -> Selection:
    evals = {gid: evaluate(rule, vf, at, catalogue, cfg) for gid, rule in goals.rules.items()}
    age, _ = signup_age(vf, at)
    is_new = age is not None and age <= timedelta(days=cfg.new_user_days)
    ranked = []
    for gid, cond in _priority(goals, cfg):
        if gid not in evals or (cond == "new_user" and not is_new):
            continue
        if evals[gid].eligible and gid not in ranked:
            ranked.append(gid)
    if not ranked:
        reasons = "; ".join(f"{gid}: {e.reason}" for gid, e in sorted(evals.items()))
        return Selection(None, None, f"no eligible measurable goal ({reasons})", evals)
    chosen = ranked[0]
    why = f"{chosen} is the highest-priority eligible goal (goal priority D-019, D-030)"
    if is_new and chosen in ("G01", "G02", "G03"):
        why += "; new-user funnel: next step not yet reached"
    if len(ranked) > 1:
        why += f"; also eligible, lower priority: {', '.join(ranked[1:])}"
    return Selection(goals.rules[chosen], evals[chosen], why, evals, ranked[1:])


@dataclass
class GoalPlan:
    user_id: str
    at: str
    outcome: str  # NEW | KEEP | REPLAN | NO_GOAL
    goal: dict | None  # goal_id, name, rule_version, start, deadline, success, success_signal, evidence, ...
    reason: str
    closing: dict | None = None  # previous goal being closed: {goal_instance_id, goal_id, close_reason}
    evaluations: dict[str, str] = field(default_factory=dict)  # goal_id -> eligible / reason (audit)

    def to_dict(self) -> dict:
        return asdict(self)


def get_open_goal(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM goal_instances WHERE user_id = ? AND status = 'open'", (user_id,)
    ).fetchone()


def _situation_changed(
    rule: GoalRule | None, vf: ViewerFacts, goal: dict, catalogue: Catalogue, at: datetime
) -> str | None:
    """Has a state condition of the open goal stopped holding? Returns the reason, or None."""
    if rule is None:
        return "goal no longer exists in the goal catalogue"
    if not rule.measurable:
        return f"goal rule {rule.version} is no longer measurable: {rule.unmeasurable_reason}"
    series = (goal.get("detail") or {}).get("series")
    if series and not any(catalogue.is_promotable(t, at) for t in catalogue.by_slug(series["slug"])):
        return f"series {series['slug']} is no longer available (withdrawn, ended or quarantined)"
    el = rule.eligibility
    status = vf.usable("subscription_status")
    if "subscription_status" in el and status is not None and status not in el["subscription_status"]:
        return f"subscription status is now {status} ({vf['subscription_status'].source})"
    fact_name = NO_PRIOR_EVENT_FACT.get(el.get("requires_no_prior_event", ""))
    if fact_name and vf.usable(fact_name) is True:
        return f"{el['requires_no_prior_event']} has now happened"
    if rule.goal_id == "G05" and vf.usable("ever_paid") is True:
        return "viewer has now paid"
    return None


def plan_goal(
    conn: sqlite3.Connection,
    vf: ViewerFacts,
    at: datetime,
    goals: GoalCatalogue,
    catalogue: Catalogue,
    cfg: Config,
) -> GoalPlan:
    active = get_open_goal(conn, vf.user_id)
    closing = None
    if active is not None:
        if parse_ts(active["deadline"]) <= at:
            closing = {"goal_instance_id": active["goal_instance_id"], "goal_id": active["goal_id"],
                       "close_reason": "deadline_passed"}  # fmt: skip
        else:
            goal = json.loads(active["goal_json"])
            changed = _situation_changed(goals.rules.get(active["goal_id"]), vf, goal, catalogue, at)
            if changed is None:
                return GoalPlan(vf.user_id, iso(at), KEEP, goal,
                                f"open goal {active['goal_id']} still valid until {active['deadline']}")  # fmt: skip
            closing = {"goal_instance_id": active["goal_instance_id"], "goal_id": active["goal_id"],
                       "close_reason": f"situation_changed: {changed}"}  # fmt: skip

    sel = select_goal(vf, at, goals, catalogue, cfg)
    evals = {gid: ("eligible" if e.eligible else e.reason) for gid, e in sorted(sel.evaluations.items())}
    if sel.rule is None:
        return GoalPlan(vf.user_id, iso(at), NO_GOAL, None, sel.reason, closing, evals)
    rule, el = sel.rule, sel.eligibility
    goal = {
        "goal_id": rule.goal_id,
        "name": rule.name,
        "rule_version": rule.version,
        "start": iso(at),  # the window starts when the agent commits to the goal (goals.json notes)
        "deadline": iso(at + rule.window),
        "success": rule.success,
        "success_signal": rule.success_signal(),
        "evidence": el.evidence,
        "assumptions": el.assumptions,
        "detail": el.detail,
    }
    return GoalPlan(vf.user_id, iso(at), REPLAN if closing else NEW, goal, sel.reason, closing, evals)


def commit_plan(conn: sqlite3.Connection, plan: GoalPlan, shadow: bool = False) -> str | None:
    """Persist a plan (caller owns the transaction). Returns the open goal_instance_id, or None for NO_GOAL."""
    if plan.closing:
        conn.execute(
            "UPDATE goal_instances SET status = 'closed', close_reason = ?, closed_at = ? WHERE goal_instance_id = ?",
            (plan.closing["close_reason"], plan.at, plan.closing["goal_instance_id"]),
        )
        _log(conn, plan.user_id, plan.at, plan.closing["goal_instance_id"], "closed", plan.closing)
    if plan.outcome == KEEP:
        active = get_open_goal(conn, plan.user_id)
        _log(conn, plan.user_id, plan.at, active["goal_instance_id"], "kept", {"reason": plan.reason})
        return active["goal_instance_id"]
    if plan.outcome == NO_GOAL:
        _log(conn, plan.user_id, plan.at, None, "no_goal", {"reason": plan.reason})
        return None
    n = conn.execute("SELECT COUNT(*) FROM goal_instances WHERE user_id = ?", (plan.user_id,)).fetchone()[0]
    gi = f"GI-{plan.user_id}-{n + 1}"
    g = plan.goal
    conn.execute(
        "INSERT INTO goal_instances (goal_instance_id, user_id, goal_id, goal_name, rule_version, status, start_at,"
        " deadline, shadow, goal_json) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
        (gi, plan.user_id, g["goal_id"], g["name"], g["rule_version"], g["start"], g["deadline"], int(shadow),
         json.dumps(g, sort_keys=True)),
    )  # fmt: skip
    _log(conn, plan.user_id, plan.at, gi, "opened", {"reason": plan.reason, "goal_id": g["goal_id"]})
    return gi


def apply_rule_correction(
    conn: sqlite3.Connection, goals: GoalCatalogue, goal_id: str, new_success: dict, effective_at: datetime
) -> tuple[GoalRule, list[str]]:
    """A success rule was corrected: new version, applied only to the OPEN goals of that type (never to every viewer).

    If the corrected rule is no longer measurable, those goals are closed. Returns the new rule and the affected
    goal_instance_ids.
    """
    if goal_id not in goals.rules:
        raise ValueError(f"rule correction for unknown goal {goal_id!r}")
    rule = goals.correct(goal_id, new_success, effective_at)
    affected = [r["goal_instance_id"] for r in conn.execute(
        "SELECT goal_instance_id FROM goal_instances WHERE goal_id = ? AND status = 'open'", (goal_id,))]  # fmt: skip
    for gi in affected:
        row = conn.execute("SELECT user_id, goal_json, rule_version FROM goal_instances WHERE goal_instance_id = ?",
                           (gi,)).fetchone()  # fmt: skip
        goal = json.loads(row["goal_json"])
        goal.update(rule_version=rule.version, success=rule.success, success_signal=rule.success_signal())
        conn.execute("UPDATE goal_instances SET rule_version = ?, goal_json = ? WHERE goal_instance_id = ?",
                     (rule.version, json.dumps(goal, sort_keys=True), gi))  # fmt: skip
        _log(conn, row["user_id"], iso(effective_at), gi, "rule_corrected",
             {"from": row["rule_version"], "to": rule.version})  # fmt: skip
        if not rule.measurable:
            # The corrected rule can no longer be judged: close the goal now instead of keeping an unjudgeable goal.
            reason = f"rule_corrected_unmeasurable: {rule.unmeasurable_reason}"
            conn.execute("UPDATE goal_instances SET status = 'closed', close_reason = ?, closed_at = ?"
                         " WHERE goal_instance_id = ?", (reason, iso(effective_at), gi))  # fmt: skip
            _log(conn, row["user_id"], iso(effective_at), gi, "closed", {"close_reason": reason})
    return rule, affected


def _log(conn, user_id, at, gi, change, detail) -> None:
    conn.execute("INSERT INTO goal_log (user_id, at, goal_instance_id, change, detail_json) VALUES (?, ?, ?, ?, ?)",
                 (user_id, at, gi, change, json.dumps(detail, sort_keys=True)))  # fmt: skip
