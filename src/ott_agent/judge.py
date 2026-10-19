"""Outcome judge (Step 6: close the loop; D-042, D-043).

Every goal instance (agent and holdout shadow) is judged by the success-rule version it carries, from attributable
signals only: events of the success type whose corrected event time lies inside the goal window and that were known by
the judging time. Verdicts: SUCCEEDED / FAILED / INCONCLUSIVE, with a stage (provisional / final, D-005), the evidence,
the viewer's exposure to our actions (reported, never used to decide success) and a factual learning note.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .catalogue import Catalogue
from .config import Config
from .goals import check_measurable
from .timeutil import iso, ist_day, parse_ts

SUCCEEDED, FAILED, INCONCLUSIVE = "SUCCEEDED", "FAILED", "INCONCLUSIVE"

SCHEMA = """
CREATE TABLE IF NOT EXISTS goal_outcomes (
    goal_instance_id  TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    goal_id           TEXT NOT NULL,
    shadow            INTEGER NOT NULL,
    verdict           TEXT NOT NULL,
    stage             TEXT NOT NULL,
    judged_at         TEXT NOT NULL,
    outcome_json      TEXT NOT NULL
);
"""


@dataclass
class Outcome:
    goal_instance_id: str
    user_id: str
    goal_id: str
    goal_name: str
    arm: str  # agent | holdout (shadow)
    rule_version: str
    window: dict  # {start, deadline, judged_until}
    verdict: str
    stage: str  # provisional | final | open
    reason: str
    success_events: list = field(default_factory=list)  # [{event_id, event, event_time}]
    exposure: dict = field(default_factory=dict)
    learned: str = ""
    judged_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _events(conn: sqlite3.Connection, user: str, known_by: datetime) -> list[dict]:
    rows = conn.execute(
        "SELECT event_id, event, event_time, first_received_at, payload_json FROM events"
        " WHERE user_id = ? AND first_received_at <= ? ORDER BY event_time",
        (user, iso(known_by)),
    ).fetchall()
    return [{"id": r["event_id"], "event": r["event"], "t": parse_ts(r["event_time"]),
             "props": json.loads(r["payload_json"]).get("properties") or {}} for r in rows]  # fmt: skip


def attributable_signals(
    goal: dict, events: list[dict], start: datetime, end: datetime, catalogue: Catalogue
) -> list:
    """Events that satisfy the goal's success rule inside [start, end] (D-042 point 2)."""
    success = goal.get("success") or {}
    etype, cond, gid = success.get("event"), (success.get("condition") or "").lower(), goal["goal_id"]
    hits = [e for e in events if e["event"] == etype and start <= e["t"] <= end]
    if "later ist calendar day" in cond:  # G03 (both rule versions)
        hits = [e for e in hits if ist_day(e["t"]) > ist_day(start)]
    if etype == "purchase_completed":  # G04/G05/G06: a paid plan, not a trial
        hits = [e for e in hits if not e["props"].get("is_trial")]
    if gid == "G07" or "same content_slug" in cond:
        slug = ((goal.get("detail") or {}).get("series") or {}).get("slug")
        hits = [e for e in hits if (t := catalogue.titles.get(e["props"].get("title_id"))) and t.slug == slug]
    if gid == "G08" or "released in the 30 days" in cond:
        lo = (start - timedelta(days=30)).date()
        hits = [e for e in hits if (t := catalogue.titles.get(e["props"].get("title_id"))) and t.release_date
                and lo <= t.release_date <= start.date()]  # fmt: skip
    if gid == "G09" or "3 distinct ist days" in cond:
        days: dict = {}
        for e in hits:
            days.setdefault(ist_day(e["t"]), e)
        hits = list(days.values()) if len(days) >= 3 else []
    return hits


def judge_goal(gi: sqlite3.Row, events: list[dict], judged_at: datetime, catalogue: Catalogue, cfg: Config,
               our_actions: dict, arm: str) -> Outcome:  # fmt: skip
    goal = json.loads(gi["goal_json"])
    start, deadline = parse_ts(gi["start_at"]), parse_ts(gi["deadline"])
    closed_early = (gi["status"] == "closed" and gi["close_reason"] and gi["close_reason"] != "deadline_passed"
                    and gi["closed_at"] and parse_ts(gi["closed_at"]) < deadline)  # fmt: skip
    end = min(deadline, parse_ts(gi["closed_at"])) if closed_early else deadline
    # A goal closed early (e.g. a G05 goal closed because the viewer paid) is judged up to its closing time: the
    # purchase that closed it was known by then, so it lies inside [start, closed_at] and counts.
    hits = attributable_signals(goal, events, start, min(end, judged_at), catalogue)
    ok, why_not = check_measurable(goal.get("success") or {})
    if hits:
        verdict, reason = SUCCEEDED, f"{len(hits)} attributable signal(s) in the window"
    elif not ok:
        verdict, reason = INCONCLUSIVE, f"rule not measurable: {why_not}"
    elif closed_early:
        verdict, reason = INCONCLUSIVE, f"goal closed early ({gi['close_reason']}) without a success signal"
    elif judged_at >= deadline + timedelta(hours=cfg.grace_provisional_h):
        verdict, reason = (
            FAILED,
            f"no attributable signal by the deadline ({iso(deadline)}) + {cfg.grace_provisional_h} h",
        )
    else:
        verdict, reason = INCONCLUSIVE, f"observed data ends before deadline + {cfg.grace_provisional_h} h"
    if judged_at >= deadline + timedelta(days=cfg.grace_final_days):
        stage = "final"
    elif judged_at >= deadline + timedelta(hours=cfg.grace_provisional_h) or verdict == SUCCEEDED:
        stage = "provisional"
    else:
        stage = "open"

    exposure = _exposure(events, start, end, our_actions)
    out = Outcome(
        goal_instance_id=gi["goal_instance_id"], user_id=gi["user_id"], goal_id=gi["goal_id"], goal_name=gi["goal_name"],
        arm=arm, rule_version=gi["rule_version"],
        window={"start": iso(start), "deadline": iso(deadline), "judged_until": iso(min(end, judged_at))},
        verdict=verdict, stage=stage, reason=reason,
        success_events=[{"event_id": e["id"], "event": e["event"], "event_time": iso(e["t"])} for e in hits[:5]],
        exposure=exposure, judged_at=iso(judged_at),
    )  # fmt: skip
    out.learned = _learning(out, hits, start)
    return out


def _exposure(events, start, end, our_actions) -> dict:
    """Our actions the viewer actually saw in the window (reported only, D-042 point 5)."""
    pages = [e for e in events if e["event"] == "home_page_shown" and start <= e["t"] <= end
             and e["props"].get("personalised") and e["props"].get("page_action_id") in our_actions]  # fmt: skip
    delivered = [e for e in events if e["event"] == "message_delivered" and start <= e["t"] <= end
                 and e["props"].get("action_id") in our_actions]  # fmt: skip
    clicked = [e for e in events if e["event"] == "message_clicked" and start <= e["t"] <= end
               and e["props"].get("action_id") in our_actions]  # fmt: skip
    return {"pages_shown": len(pages), "messages_delivered": len(delivered), "messages_clicked": len(clicked),
            "first_page_shown_at": iso(pages[0]["t"]) if pages else None}  # fmt: skip


def _learning(o: Outcome, hits, start) -> str:
    """A factual note (D-043): what happened, never why."""
    ex = o.exposure
    seen = ("our page was shown" if ex["pages_shown"] else "no page of ours was shown") if o.arm == "agent" else \
        "holdout: nothing was shown"  # fmt: skip
    msg = ""
    if ex["messages_delivered"]:
        msg = f"; reminder delivered{', clicked' if ex['messages_clicked'] else ', not clicked'}"
    if o.verdict == SUCCEEDED:
        days = (hits[0]["t"] - start).total_seconds() / 86400
        return f"{o.goal_id} succeeded {days:.1f} days after the goal started; {seen}{msg}"
    if o.verdict == FAILED:
        return f"{o.goal_id} failed: no {o.goal_name} signal within the window; {seen}{msg}"
    return f"{o.goal_id} inconclusive: {o.reason}; {seen}{msg}"


def judge_all(conn: sqlite3.Connection, catalogue: Catalogue, cfg: Config, judged_at: datetime,
              arms: dict, our_actions: set) -> list[Outcome]:  # fmt: skip
    """Judge every goal instance and store the outcome and learning note in the viewer's state."""
    conn.executescript(SCHEMA)
    outcomes = []
    goals = conn.execute("SELECT * FROM goal_instances ORDER BY user_id, start_at").fetchall()
    cache: dict = {}
    with conn:
        for gi in goals:
            user = gi["user_id"]
            if user not in cache:
                cache[user] = _events(conn, user, judged_at)
            arm = "holdout" if gi["shadow"] else arms.get(user, "agent")
            o = judge_goal(gi, cache[user], judged_at, catalogue, cfg, our_actions, arm)
            conn.execute(
                "INSERT OR REPLACE INTO goal_outcomes (goal_instance_id, user_id, goal_id, shadow, verdict, stage,"
                " judged_at, outcome_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (o.goal_instance_id, user, o.goal_id, gi["shadow"], o.verdict, o.stage, o.judged_at,
                 json.dumps(o.to_dict(), sort_keys=True)),
            )  # fmt: skip
            conn.execute("INSERT INTO goal_log (user_id, at, goal_instance_id, change, detail_json) VALUES (?, ?, ?, ?, ?)",
                         (user, o.judged_at, o.goal_instance_id, "judged",
                          json.dumps({"verdict": o.verdict, "stage": o.stage, "learned": o.learned})))  # fmt: skip
            outcomes.append(o)
    return outcomes


def update_viewer_state(out_dir: Path, outcomes: list[Outcome]) -> None:
    """Add outcomes and learning notes to viewer_state/<user>.json (D-043)."""
    by_user: dict = {}
    for o in outcomes:
        by_user.setdefault(o.user_id, []).append(o)
    d = Path(out_dir) / "viewer_state"
    for user, items in by_user.items():
        path = d / f"{user}.json"
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"user_id": user}
        state["outcomes"] = [o.to_dict() for o in items]
        state["learned"] = [o.learned for o in items]
        path.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
