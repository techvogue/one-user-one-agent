"""Independent verifier (Step 5, D-040): checks that can disagree with the decision step.

Separately written on purpose. It shares only the event store and timestamp parsing with the decision code; it does
NOT import the facts resolver, goal evaluator, planner or intervention builder. Everything it checks is re-derived
from stored events, the raw profile/catalogue files and the pack config. Model output is never used as evidence.

Verdicts: approve | downgrade (only message checks failed: drop the message, keep the page) | reject (anything else).
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .timeutil import IST, iso, parse_ts

VALID_DIALECTS = {f"d{i}" for i in range(1, 7)}
PLAYBACK = ("play_started", "play_progress", "episode_completed")
BILLING = ("purchase_completed", "subscription_renewed", "subscription_lapsed")
# Own copy of the platform's event types (pack README) plus simulator outcome events: a goal must be measurable.
OBSERVABLE = {
    "signup_completed", "login_success", "app_open", "app_dialect_changed", "home_viewed", "title_viewed", "search",
    "play_started", "play_progress", "episode_completed", "paywall_viewed", "purchase_completed",
    "subscription_renewed", "subscription_lapsed", "message_delivered", "message_clicked", "notification_opt_out",
    "home_page_shown", "message_failed",
}  # fmt: skip
# Own scan for instruction-like text in catalogue data (D-012). Different wording from the catalogue loader on purpose.
INSTRUCTION = re.compile(
    r"(ignore|disregard|forget)\b.{0,40}\b(rules|instructions)|system\s+note|assistant\s+instruction|\bai\s+agents?\b"
    r"|(message|notify|mark)\s+(every|all)\s+users?|skip\s+(all\s+)?checks|pre-?approved",
    re.IGNORECASE,
)
MESSAGE_CHECKS = {"consent", "quiet_hours", "frequency_cap", "template", "push_copy", "deeplink", "message_dialect",
                  "message_title_available", "message_goal", "message_before_deadline", "message_series",
                  "message_channel"}  # fmt: skip


@dataclass
class Verdict:
    verdict: str  # approve | downgrade | reject
    passed: list[str] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)  # [{"check": ..., "detail": ...}]

    def to_dict(self) -> dict:
        return asdict(self)


def _true_time(row) -> datetime:
    """Own derivation of when an event happened: server receipt minus the phone's holding time (D-005)."""
    rec = parse_ts(row["first_received_at"])
    try:
        occ, sent = parse_ts(row["occurred_at_raw"]), parse_ts(row["sent_at_raw"])
    except (TypeError, ValueError):
        return rec
    if occ is None or sent is None or sent < occ or (sent - occ) > timedelta(days=30):
        return rec
    return rec - (sent - occ)


class Verifier:
    def __init__(self, pack: Path, conn: sqlite3.Connection, cfg: Config):
        pack = Path(pack)
        self.conn, self.cfg = conn, cfg
        with open(pack / "data" / "catalogue.csv", encoding="utf-8", newline="") as f:
            self.titles = {r["title_id"]: r for r in csv.DictReader(f)}
        self.flagged = {tid for tid, r in self.titles.items()
                        if INSTRUCTION.search(r.get("description") or "") or INSTRUCTION.search(r.get("title") or "")}  # fmt: skip
        self.episodes: dict[str, int] = {}
        with open(pack / "data" / "episodes.csv", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                self.episodes[r["content_slug"]] = self.episodes.get(r["content_slug"], 0) + 1
        self.profile_rows: dict[str, list[dict]] = {}
        with open(pack / "data" / "profiles_snapshot.csv", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                self.profile_rows.setdefault(r["user_id"], []).append(r)
        for rows in self.profile_rows.values():
            rows.sort(
                key=lambda r: parse_ts(r["updated_at"]) if r.get("updated_at") else datetime.min, reverse=True
            )
        goals = json.loads((pack / "config" / "goals.json").read_text(encoding="utf-8"))
        self.goal_rules = {g["goal_id"]: g for g in goals["goals"]}
        iv = json.loads((pack / "config" / "interventions.json").read_text(encoding="utf-8"))
        self.ttl = timedelta(minutes=iv["home_page"]["ttl_minutes"])
        self.rows_cfg = iv["home_page"]["rows"]
        self.push_copy = iv["push"]["copy"]
        self.quiet = tuple(int(x.split(":")[0]) for x in iv["push"]["quiet_hours_ist"])
        self.templates = {t["template_id"]: t for t in iv["whatsapp"]["approved_templates"]}
        pricing = json.loads((pack / "config" / "pricing.json").read_text(encoding="utf-8"))
        self.plan_days = {p["plan_id"]: p["days"] for p in pricing["plans"]}
        with open(pack / "replay" / "arms.csv", encoding="utf-8", newline="") as f:
            self.arms = {r["user_id"]: r["arm"] for r in csv.DictReader(f)}
        # Own read of the pilot's per-viewer results (D-051): user -> [(goal_id, deadline)] of failed goals.
        self.pilot_failed: dict[str, list[tuple[str, datetime]]] = {}
        pilot = pack / "pilot" / "pilot_outcomes.csv"
        if pilot.exists():
            with open(pilot, encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    hours = ((self.goal_rules.get(r["goal_id"]) or {}).get("success") or {}).get(
                        "window_hours"
                    )
                    try:
                        start = parse_ts(r.get("goal_started_at"))
                    except ValueError:
                        continue
                    if (r.get("succeeded") or "").strip().lower() != "true" and hours and start:
                        end = start + timedelta(hours=float(hours))
                        self.pilot_failed.setdefault(r["user_id"], []).append((r["goal_id"], end))
        self.withdrawn: dict[str, datetime] = {}
        self.published: list[dict] = []  # own ledger of what has gone out (duplicates, frequency cap)
        self.scheduled: dict[str, tuple[str, datetime]] = {}  # reminder key -> (user, send_at), not yet sent

    # --- world changes -------------------------------------------------------------------------------------------

    def withdraw(self, title_id: str, at: datetime) -> None:
        self.withdrawn[title_id] = at

    def correct(self, goal_id: str, new_success: dict) -> None:
        if goal_id in self.goal_rules:
            self.goal_rules[goal_id] = {**self.goal_rules[goal_id],
                                        "success": {**self.goal_rules[goal_id]["success"], **new_success}}  # fmt: skip

    def record_published(self, action: dict) -> None:
        self.published.append(action)

    def schedule(self, key: str, user: str, send_at: datetime) -> None:
        self.scheduled[key] = (user, send_at)

    def unschedule(self, key: str) -> None:
        self.scheduled.pop(key, None)

    def verify_goal(self, user: str, goal: dict, plan_outcome: str, at: datetime) -> str | None:
        """Goal-only check (used when a goal is re-planned without a visit, D-048). None = supported by evidence."""
        return self._goal_ok(user, goal, plan_outcome, at, None)

    def verify_page(self, page: dict, trigger: dict, pub: datetime) -> Verdict:
        """Page checks only (used when a live page is republished after a withdrawal)."""
        v = Verdict("approve")

        def check(name, ok, detail=""):
            (v.passed.append(name) if ok else v.failed.append({"check": name, "detail": detail}))

        check(
            "holdout", self.arms.get(trigger["user_id"]) == "agent", "nothing may be published for this arm"
        )
        self._check_page(page, trigger, pub, check)
        v.verdict = "reject" if v.failed else "approve"
        return v

    # --- independent derivations -----------------------------------------------------------------------------------

    def _events(self, user: str, known_at: datetime) -> list[dict]:
        rows = self.conn.execute(
            "SELECT event_id, event, first_received_at, occurred_at_raw, sent_at_raw, app_dialect, payload_json"
            " FROM events WHERE user_id = ? AND first_received_at <= ?", (user, iso(known_at))).fetchall()  # fmt: skip
        out = []
        for r in rows:
            out.append({"id": r["event_id"], "event": r["event"], "t": _true_time(r), "dialect": r["app_dialect"],
                        "props": json.loads(r["payload_json"]).get("properties") or {},
                        "received": parse_ts(r["first_received_at"])})  # fmt: skip
        return sorted(out, key=lambda e: e["received"])

    def _failed_recently(self, user: str, gid: str, at: datetime) -> str | None:
        """Own check of D-051: did this goal fail for the viewer with a deadline inside the cooldown?"""
        since = at - timedelta(days=self.cfg.failed_goal_cooldown_days)
        for g, end in self.pilot_failed.get(user, []):
            if g == gid and since <= end <= at:
                return f"pilot, deadline {iso(end)}"
        if self.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'goal_outcomes'").fetchone():
            for r in self.conn.execute(
                "SELECT o.judged_at, g.deadline FROM goal_outcomes o JOIN goal_instances g"
                " ON g.goal_instance_id = o.goal_instance_id WHERE o.user_id = ? AND o.goal_id = ?"
                " AND o.verdict = 'FAILED'", (user, gid)):  # fmt: skip
                if parse_ts(r["judged_at"]) <= at and parse_ts(r["deadline"]) >= since:
                    return f"judged, deadline {r['deadline']}"
        return None

    def _status(self, user: str, ev: list[dict], at: datetime) -> str | None:
        """Subscription status, or None when it cannot be trusted (D-007, D-015, D-027, D-028)."""
        rows = self.profile_rows.get(user, [])
        top = rows[0] if rows else None
        updated = parse_ts(top["updated_at"]) if top else None
        if top and len(rows) > 1 and rows[1]["updated_at"] == top["updated_at"] and \
                rows[1]["subscription_status"] != top["subscription_status"]:  # fmt: skip
            status = None
        else:
            status = top["subscription_status"] if top else None
        bill = sorted((e for e in ev if e["event"] in BILLING), key=lambda e: e["t"])
        newer = [e for e in bill if updated is None or e["t"] > updated]
        if newer:
            last = newer[-1]
            if last["event"] == "subscription_lapsed":
                return "lapsed"
            status = (
                "trial_active"
                if last["event"] == "purchase_completed" and last["props"].get("is_trial")
                else "active"
            )
            days = self.plan_days.get(last["props"].get("plan_id"))
            return None if days is not None and last["t"] + timedelta(days=days) <= at else status
        if top is None:
            return "never"
        paid_before = any(e["event"] == "subscription_renewed" or
                          (e["event"] == "purchase_completed" and not e["props"].get("is_trial")) for e in bill)  # fmt: skip
        if status == "never" and (paid_before or top.get("plan_ends_at") or
                                  any(r["subscription_status"] in ("active", "lapsed") for r in rows[1:])):  # fmt: skip
            return None
        if status == "active" and top.get("plan_ends_at") and parse_ts(top["plan_ends_at"]) <= at:
            return None
        return status

    def _signup(self, user: str, ev: list[dict]) -> datetime | None:
        rows = self.profile_rows.get(user, [])
        if rows and rows[0].get("signup_at"):
            return parse_ts(rows[0]["signup_at"])
        signups = [e["t"] for e in ev if e["event"] == "signup_completed"]
        return min(signups) if signups else None  # first-seen is not proof of signup

    def _consent(self, user: str, channel: str, ev: list[dict]) -> bool:
        if any(e["event"] == "notification_opt_out" and e["props"].get("channel") == channel for e in ev):
            return False
        rows = self.profile_rows.get(user, [])
        field_name = "whatsapp_opt_in" if channel == "whatsapp" else "notifications_opt_in"
        return bool(rows) and (rows[0].get(field_name) or "").strip().lower() == "true"

    def _message_dialect(self, user: str, ev: list[dict], trigger_dialect: str | None) -> str | None:
        if trigger_dialect in VALID_DIALECTS:
            return trigger_dialect
        latest = next((e["dialect"] for e in reversed(ev) if e["dialect"] in VALID_DIALECTS), None)
        if latest:
            return latest
        rows = self.profile_rows.get(user, [])
        d = rows[0].get("preferred_dialect") if rows else None
        return d if d in VALID_DIALECTS else None

    def _available(self, tid: str, start: datetime, end: datetime) -> tuple[bool, str]:
        r = self.titles.get(tid)
        if r is None:
            return False, f"{tid} does not exist in the catalogue"
        if tid in self.flagged:
            return False, f"{tid} has instruction-like text (quarantined)"
        frm = parse_ts(r["available_from"]) if r.get("available_from") else None
        if frm is None or frm > start:
            return False, f"{tid} not yet available at {iso(start)}"
        ends = [x for x in (parse_ts(r["available_until"]) if r.get("available_until") else None,
                            self.withdrawn.get(tid) if self.withdrawn.get(tid) and self.withdrawn[tid] <= start else None) if x]  # fmt: skip
        if ends and min(ends) <= end:
            return False, f"{tid} not available until {iso(end)} (ends {iso(min(ends))})"
        return True, ""

    # --- goal eligibility, re-derived (D-014, D-029, D-031) ----------------------------------------------------------

    def _goal_ok(
        self, user: str, goal: dict, plan_outcome: str, at: datetime, trigger_id: str | None
    ) -> str | None:
        """Returns None if the goal is supported by evidence, else the reason it is not."""
        gid = goal["goal_id"]
        rule = self.goal_rules.get(gid)
        if rule is None:
            return f"{gid} is not in the goal catalogue"
        succ = rule.get("success") or {}
        if (
            not succ.get("event")
            or succ["event"] not in OBSERVABLE
            or not (succ.get("condition") or "").strip()
        ):
            return f"{gid} is not measurable"
        ev = self._events(user, at)
        el = rule.get("eligibility") or {}
        status = self._status(user, ev, at)
        playback = [e for e in ev if e["event"] in PLAYBACK]
        completed = [e for e in ev if e["event"] == "episode_completed"]
        if "subscription_status" in el and status not in el["subscription_status"]:
            return f"status is {status or 'not trustworthy'}, rule needs {el['subscription_status']}"
        no_prior = el.get("requires_no_prior_event")
        if no_prior == "play_started" and playback:
            return "the viewer has already played"
        if no_prior == "episode_completed" and completed:
            return "the viewer has already completed an episode"
        if gid == "G05" and (any(e["event"] in ("purchase_completed", "subscription_renewed") and
                                 not e["props"].get("is_trial") for e in ev) or
                             any(r["subscription_status"] in ("active", "lapsed") for r in self.profile_rows.get(user, []))):  # fmt: skip
            return "the viewer has paid before"
        if plan_outcome == "KEEP":
            return None  # entry windows are only checked when a goal starts (D-029)
        failed = self._failed_recently(user, gid, at)
        if failed:
            return (
                f"{gid} already failed for this viewer ({failed}); must not be repeated within the cooldown"
            )
        signup = self._signup(user, ev)
        if "signed_up_within_days" in el:
            if signup is None:
                return "signup time not provable"
            if at - signup > timedelta(days=el["signed_up_within_days"]):
                return f"signed up more than {el['signed_up_within_days']} days ago"
        if gid == "G06":
            lapses = [e["t"] for e in ev if e["event"] == "subscription_lapsed"]
            if not lapses or at - max(lapses) > timedelta(days=self.cfg.g06_recent_lapse_days):
                return "no lapse within the recent-lapse window"
        if gid == "G07":
            # A-G07-1: "last played" = latest play_started or episode_completed on the series (any language variant).
            slug = ((goal.get("detail") or {}).get("series") or {}).get("slug")
            last, done, is_series = None, set(), False
            for e in ev:
                if e["event"] not in ("play_started", "episode_completed"):
                    continue
                t = self.titles.get(e["props"].get("title_id"))
                if t and t["content_slug"] == slug:
                    is_series = t["content_type"] in ("show", "micro_drama")
                    last = max(last, e["t"]) if last else e["t"]
                    if e["event"] == "episode_completed":
                        done.add(e["props"].get("episode_id"))
            if slug is None or last is None or not is_series:
                return "the goal names no series the viewer has watched"
            if not (
                at - timedelta(days=self.cfg.g07_max_age_days)
                <= last
                <= at - timedelta(hours=self.cfg.g07_min_gap_h)
            ):
                return f"series {slug} last watched outside the 72 h – 14 day window"
            if self.episodes.get(slug, 0) and len(done) >= self.episodes[slug]:
                return f"series {slug} is already finished"
            if not any(
                self._available(x, at, at)[0] for x, r in self.titles.items() if r["content_slug"] == slug
            ):
                return f"series {slug} is no longer available"
        if gid == "G09":
            days = {
                e["t"].astimezone(IST).date() for e in playback if at - timedelta(days=14) <= e["t"] <= at
            }
            if len(days) > self.cfg.g09_max_play_days:
                return f"played on {len(days)} days in the last 14"
        if gid == "G12":
            if signup is None or at - signup <= timedelta(days=self.cfg.g12_absence_days):
                return "not an existing user"
            recent = [
                e
                for e in ev
                if e["id"] != trigger_id and at - e["t"] <= timedelta(days=self.cfg.g12_absence_days)
            ]
            if recent:
                return "the viewer was active in the last 14 days"
        return None

    # --- the checks --------------------------------------------------------------------------------------------------

    def verify(self, p: dict) -> Verdict:
        """p: user, arm, trigger (dict), known_at, publish_at, plan_outcome, goal (dict|None), page (dict|None),
        message (dict|None), model (dict), model_calls, tool_calls, latency_s, fallback."""
        v = Verdict("approve")

        def check(name, ok, detail=""):
            (v.passed.append(name) if ok else v.failed.append({"check": name, "detail": detail}))

        user, t, at, pub = p["user"], p["trigger"], p["known_at"], p["publish_at"]
        acts = p["page"] is not None or p["message"] is not None
        arm = self.arms.get(user)
        check("holdout", arm == "agent" or not acts, f"arm is {arm or 'unknown'}: nothing may be published")
        check("duplicate", not any(a["trigger_event_id"] == t["event_id"] and a.get("type") == "home_page"
                                   for a in self.published) or p["page"] is None,
              f"an action was already published for trigger {t['event_id']}")  # fmt: skip
        check("budget", p["model_calls"] <= self.cfg.max_model_calls and p["tool_calls"] <= self.cfg.max_tool_calls,
              f"{p['model_calls']} model / {p['tool_calls']} tool calls")  # fmt: skip
        check(
            "latency", p["latency_s"] <= self.cfg.publish_deadline_s, f"{p['latency_s']} s after the trigger"
        )
        if p["goal"] is not None:
            why = self._goal_ok(user, p["goal"], p["plan_outcome"], at, t["event_id"])
            check("eligibility", why is None, why or "")
            claimed = (p.get("model") or {}).get("claimed_goal")
            if claimed and (p.get("model") or {}).get("outcome") == "unsupported_claim":
                check("model_not_evidence", p["goal"]["goal_id"] != claimed,
                      f"the goal is the model's unsupported claim {claimed}")  # fmt: skip
        if p["page"] is not None:
            self._check_page(p["page"], t, pub, check)
        if p["message"] is not None:
            self._check_message(p["message"], user, t, at, p["goal"], check)
        hard = [f for f in v.failed if f["check"] not in MESSAGE_CHECKS]
        v.verdict = "reject" if hard else ("downgrade" if v.failed else "approve")
        return v

    def _check_page(self, page: dict, t: dict, pub: datetime, check) -> None:
        trig_dialect = t.get("app_dialect")
        check("dialect", trig_dialect in VALID_DIALECTS and page["dialect"] == trig_dialect,
              f"page in {page['dialect']}, app is set to {trig_dialect}")  # fmt: skip
        ids = ([page["hero_title_id"]] if page.get("hero_title_id") else []) + [
            x for r in page.get("rows", []) for x in r["title_ids"]]  # fmt: skip
        wrong = [x for x in ids if (self.titles.get(x) or {}).get("dialect") != page["dialect"]]
        check("title_dialect", not wrong, f"titles not in {page['dialect']}: {wrong}")
        unavailable = [why for x in ids for ok, why in [self._available(x, pub, pub + self.ttl)] if not ok]
        check("availability", not unavailable, "; ".join(unavailable))
        slugs = [(self.titles.get(x) or {}).get("content_slug") for x in ids]
        check("unique_slug", len(slugs) == len(set(slugs)), "a content_slug appears twice")
        rows = page.get("rows", [])
        check("row_limits", len(rows) <= self.rows_cfg["max_rows"] and all(
            self.rows_cfg["min_titles_per_row"] <= len(r["title_ids"]) <= self.rows_cfg["max_titles_per_row"] for r in rows),
            "row count or row size outside the platform limits")  # fmt: skip
        check(
            "row_types",
            all(r["row_type"] in self.rows_cfg["row_types"] for r in rows),
            "row type not allowed",
        )
        bad = self._row_content(page, t.get("user_id"), pub)
        check("row_content", not bad, "; ".join(bad))
        check("not_empty", bool(ids), "page has no titles")

    def _row_content(self, page: dict, user: str | None, at: datetime) -> list[str]:
        """Own check that each row holds what its type says (D-053). Returns the problems found."""
        genres = lambda x: set(((self.titles.get(x) or {}).get("genres") or "").split("|")) - {""}
        slug = lambda x: (self.titles.get(x) or {}).get("content_slug")
        ev = self._events(user, at) if user else []
        played = [e for e in ev if e["event"] in PLAYBACK and e["props"].get("title_id") in self.titles]
        played_slugs = {slug(e["props"]["title_id"]) for e in played}
        done: dict[str, set] = {}
        for e in played:
            if e["event"] == "episode_completed":
                done.setdefault(slug(e["props"]["title_id"]), set()).add(e["props"].get("episode_id"))
        played_genres = set().union(*(genres(e["props"]["title_id"]) for e in played)) if played else set()
        cut = (at - timedelta(days=self.cfg.new_release_days)).date().isoformat()
        out = []
        for i, r in enumerate(page.get("rows", []), start=1):
            ids, kind = r["title_ids"], r["row_type"]
            if kind == "new_releases":
                old = [
                    x
                    for x in ids
                    if not (
                        cut <= (self.titles.get(x) or {}).get("release_date", "") <= at.date().isoformat()
                    )
                ]
                if old:
                    out.append(
                        f"row {i} new_releases has titles not released in the last {self.cfg.new_release_days} days: {old}"
                    )
            elif kind == "genre" and ids and not set.intersection(*(genres(x) for x in ids)):
                out.append(f"row {i} genre: its titles share no genre")
            elif kind == "next_episode":
                wrong = [x for x in ids if (self.titles.get(x) or {}).get("content_type") not in ("show", "micro_drama")
                         or slug(x) not in played_slugs
                         or len(done.get(slug(x), ())) >= self.episodes.get(slug(x), 0)]  # fmt: skip
                if wrong:
                    out.append(f"row {i} next_episode has titles that are not a series in progress: {wrong}")
            elif kind == "because_you_watched":
                wrong = [x for x in ids if slug(x) in played_slugs or not genres(x) & played_genres]
                if wrong:
                    out.append(
                        f"row {i} because_you_watched has titles unrelated to or already in the viewer's history: {wrong}"
                    )
        return out

    def _check_message(
        self, m: dict, user: str, t: dict, at: datetime, goal: dict | None, check, key: str | None = None
    ) -> None:
        ev = self._events(user, at)
        send = parse_ts(m["send_at"])
        check("consent", self._consent(user, m["channel"], ev), f"no usable {m['channel']} consent")
        h = send.astimezone(IST).hour
        check(
            "quiet_hours",
            not (h >= self.quiet[0] or h < self.quiet[1]),
            f"send time {h}:00 IST is in quiet hours",
        )
        day = send.astimezone(IST).date()
        same_day = [a for a in self.published if a["user_id"] == user and a.get("type") in ("push", "whatsapp")
                    and parse_ts(a["send_at"]).astimezone(IST).date() == day]  # fmt: skip
        same_day += [k for k, (u, s_at) in self.scheduled.items()
                     if u == user and k != key and s_at.astimezone(IST).date() == day]  # fmt: skip
        check("frequency_cap", not same_day, f"a push/WhatsApp is already scheduled for {day}")
        title = self.titles.get(m["title_id"])
        ok, why = self._available(m["title_id"], send, send)
        check("message_title_available", ok, why)
        want = self._message_dialect(user, ev, t.get("app_dialect") if t else None)
        check("message_dialect", title is not None and title["dialect"] == want,
              f"title in {(title or {}).get('dialect')}, viewer sees {want}")  # fmt: skip
        check(
            "deeplink",
            m.get("deeplink") == f"ott://title/{m['title_id']}",
            "deeplink does not match the title",
        )
        if m["channel"] == "whatsapp":
            tpl = self.templates.get(m.get("template_id"))
            vars_ok = (
                tpl is not None and set(m.get("variables", {})) == set(tpl["variables"]) and title is not None
            )
            if vars_ok and "title_name" in tpl["variables"]:
                vars_ok = m["variables"]["title_name"] == title["title"]
            if vars_ok and "dialect_name" in tpl["variables"]:
                vars_ok = m["variables"]["dialect_name"] == title["dialect"]
            check("template", vars_ok, "template not approved or variables not taken from the catalogue")
        else:
            text = m.get("text") or {}
            body, head = text.get("body", ""), text.get("title", "")
            low = f"{head} {body}".lower()
            bad = [p for p in self.push_copy["forbidden_phrases"] if p in low]
            ok = (
                len(head) <= self.push_copy["title_max_chars"]
                and len(body) <= self.push_copy["body_max_chars"]
            )
            check("push_copy", ok and not bad and "₹" not in low and "rs." not in low,
                  f"copy too long, forbidden phrase {bad} or a price")  # fmt: skip
        if goal is not None:
            check(
                "message_goal",
                goal["goal_id"] in self.cfg.message_goals,
                f"no reminders for {goal['goal_id']}",
            )
            check("message_before_deadline", send + timedelta(hours=1) <= parse_ts(goal["deadline"]),
                  "the goal ends before the reminder")  # fmt: skip
            check("message_channel", goal["goal_id"] not in self.cfg.whatsapp_only_goals or m["channel"] == "whatsapp",
                  f"{goal['goal_id']} reminders go by WhatsApp only")  # fmt: skip
            if goal["goal_id"] == "G07":  # a "continue your series" reminder must name that series (D-054)
                want = ((goal.get("detail") or {}).get("series") or {}).get("slug")
                check("message_series", want is not None and (title or {}).get("content_slug") == want,
                      f"message title is not the goal's series {want}")  # fmt: skip

    def verify_send(self, pending: dict, known_at: datetime, goal_open: bool) -> Verdict:
        """Send-time re-check of a reminder (D-021, D-034)."""
        v = Verdict("approve")

        def check(name, ok, detail=""):
            (v.passed.append(name) if ok else v.failed.append({"check": name, "detail": detail}))

        user, m, goal = pending["user_id"], pending["message"], pending["goal"]
        check("goal_open", goal_open, "the goal is closed")
        ev = self._events(user, known_at)
        start = parse_ts(goal["start"])
        reached = [e for e in ev if e["event"] == "play_started" and e["t"] >= start]
        series = (
            ((goal.get("detail") or {}).get("series") or {}).get("slug") if goal["goal_id"] == "G07" else None
        )
        if series:  # G07 is reached only by playing that series (any language variant), D-054
            reached = [e for e in reached
                       if (self.titles.get(e["props"].get("title_id")) or {}).get("content_slug") == series]  # fmt: skip
        check("not_already_reached", not reached,
              f"viewer already played after the goal started ({reached[0]['id'] if reached else ''})")  # fmt: skip
        self._check_message(m, user, {"app_dialect": None}, known_at, goal, check, key=pending["decision_id"])
        v.verdict = "reject" if v.failed else "approve"
        return v
