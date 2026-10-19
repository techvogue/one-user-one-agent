"""Replay harness (Step 4: act within budget; D-035 to D-038).

Plays the replay day in delivery order: live events are ingested, control events change the world (withdrawals, rule
corrections, dependency outages), and every trigger yields exactly one decision record. Time is simulated:
published_at = deliver_at + queue wait + measured compute + simulated dependency delays (D-037).

Outputs (in --out): decisions.jsonl, actions.jsonl (simulator schema), control_log.jsonl, latency_report.json,
data_quality_report.json, viewer_state/<user>.json, state.sqlite.
"""

from __future__ import annotations

import csv
import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from .catalogue import VALID_DIALECTS, load_catalogue
from .config import Config
from .facts import resolve, save_snapshot
from .goals import GoalCatalogue, evaluate
from .ingest import ALREADY_INGESTED, DUPLICATE, DUPLICATE_CONFLICT, IngestStats, ingest_event, ingest_file
from .intervention import (
    HOME_PAGE,
    NO_ACTION,
    PlatformRules,
    Popularity,
    build_generic_page,
    build_page,
    decide_intervention,
)
from .model import MockProvider, build_request, parse_review
from .planner import NO_GOAL, apply_rule_correction, commit_plan, plan_goal
from .pricing import load_plan_days
from .profiles import load_profiles
from .quality import build_report, write_report
from .store import connect
from .timeutil import IST, compute_event_time, iso, parse_ts
from .tools import BudgetExceeded, Dependencies, DependencyFailed, ToolBox
from .verifier import Verifier

PUBLISHED, NO_ACTION_STATUS = "PUBLISHED", "NO_ACTION"
DUPLICATE_IGNORED, SAME_SESSION, STALE_SKIPPED = "DUPLICATE_IGNORED", "SAME_SESSION", "STALE_SKIPPED"
HOLDOUT_SHADOW, FAILED_LATE, REJECTED = "HOLDOUT_SHADOW", "FAILED_LATE", "REJECTED_BY_VERIFIER"
# Events that change the viewer's situation enough to re-decide within the same session.
STATE_CHANGE_EVENTS = (
    "purchase_completed",
    "subscription_renewed",
    "subscription_lapsed",
    "notification_opt_out",
    "app_dialect_changed",
    "signup_completed",
)
CACHED_FACTS_MAX_AGE = timedelta(hours=2)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# How each kind of conflicting fact is resolved (ARCHITECTURE.md §5.1).
RESOLUTION = {
    "page_dialect": "the app requests the page for its current setting (D-009)",
    "message_dialect": "latest valid app setting, then events, then profile (D-009)",
    "subscription_status": "newest profile row wins; billing events newer than it override it; contradictions make "
    "it uncertain and unusable (D-007, D-015, D-027, D-028)",
    "ever_paid": "any paid event or any profile row with a paid status counts (D-027)",
    "push_consent": "an opt-out event withdraws consent; unknown means no consent (policy rule 1, D-008)",
    "whatsapp_consent": "an opt-out event withdraws consent; unknown means no consent (policy rule 1, D-008)",
    "signup_at": "profile, then earliest signup event, then first activity as a lower bound (D-008)",
}


def _resolved_conflicts(vf) -> list[dict]:
    """Every fact the agent resolved from conflicting sources, with how (brief: conflicts and their resolution)."""
    if vf is None:
        return []
    return [
        {
            "fact": f.name,
            "used": f.value,
            "source": f.source,
            "usable": f.usable,
            "conflicting": f.conflicts,
            "resolution": RESOLUTION.get(f.name, "precedence rules in ARCHITECTURE.md §5.1"),
        }
        for f in vf.facts.values()
        if f.conflicts
    ]


class Replay:
    def __init__(self, pack: Path, out: Path, cfg: Config, provider=None):
        self.pack, self.out, self.cfg = Path(pack), Path(out), cfg
        self.provider = provider or MockProvider.from_pack(self.pack, cfg.model_latency_s)
        self.deps = Dependencies()
        self.decisions: list[dict] = []
        self.actions: list[dict] = []
        self.control_log: list[dict] = []
        self.by_trigger: dict[str, str] = {}  # trigger event_id -> decision_id
        self.sessions: dict[tuple, dict] = {}  # (user, session) -> {decision_id, at, dialect}
        self.live_pages: dict[str, dict] = {}  # user -> latest published page (+ context for republishing)
        self.pending_messages: list[dict] = []
        self.msg_days: Counter = Counter()  # (user, IST date) -> planned/sent push+WhatsApp
        self.facts_cache: dict[str, tuple] = {}  # user -> (vf, plan, at) from the last successful read
        self.worker_free: list[datetime] = []
        self.ingest_stats = {"trigger": IngestStats("trigger"), "live": IngestStats("live")}

    # --- setup ---------------------------------------------------------------------------------------------------

    def load(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):  # every replay starts from a clean store: reproducible
            p = self.out / f"state.sqlite{suffix}"
            if p.exists():
                p.unlink()
        self.conn = connect(self.out / "state.sqlite")
        data, cfgdir = self.pack / "data", self.pack / "config"
        self.history_stats = ingest_file(self.conn, data / "events.jsonl", "history", self.cfg)
        self.profiles = load_profiles(data / "profiles_snapshot.csv")
        self.catalogue = load_catalogue(data / "catalogue.csv", data / "episodes.csv")
        self.goals = GoalCatalogue.from_file(cfgdir / "goals.json")
        self.rules = PlatformRules.from_file(cfgdir / "interventions.json")
        self.plan_days = load_plan_days(cfgdir / "pricing.json")
        self.popularity = Popularity(self.cfg.popularity_days)
        self.verifier = Verifier(self.pack, self.conn, self.cfg)  # Step 5: independent checks (D-040)
        with open(self.pack / "replay" / "arms.csv", encoding="utf-8", newline="") as f:
            self.arms = {r["user_id"]: r["arm"] for r in csv.DictReader(f)}

    def timeline(self) -> list[tuple]:
        items = []
        replay = self.pack / "replay"
        for i, c in enumerate(_read_jsonl(replay / "control_events.jsonl")):
            items.append((parse_ts(c["at"]), 0, i, "control", c))
        for i, e in enumerate(_read_jsonl(replay / "live_events.jsonl"), 1):
            items.append((parse_ts(e.get("deliver_at") or e["received_at"]), 1, i, "live", e))
        for i, t in enumerate(_read_jsonl(replay / "triggers.jsonl"), 1):
            items.append((parse_ts(t["deliver_at"]), 2, i, "trigger", t))
        items.sort(key=lambda x: (x[0], x[1], x[2]))
        return items

    # --- run -----------------------------------------------------------------------------------------------------

    def run(self) -> dict:
        self.load()
        t0 = time.perf_counter()
        items = self.timeline()
        self.worker_free = [items[0][0] if items else datetime.now()] * self.cfg.workers
        self.replay_end = items[-1][0] if items else None
        for at, _, line_no, kind, obj in items:
            if kind == "control":
                self.on_control(obj, at)
            elif kind == "live":
                with self.conn:
                    try:
                        self.ingest_stats["live"].add(ingest_event(self.conn, obj, "live", line_no, self.cfg))
                    except (ValueError, TypeError) as exc:
                        self.ingest_stats["live"].malformed_lines.append(
                            {"line_no": line_no, "error": str(exc)}
                        )
            else:
                self.on_trigger(obj, line_no)
        self.flush_messages()
        self.wall_s = time.perf_counter() - t0
        return self.write_outputs()

    # --- control events ------------------------------------------------------------------------------------------

    def on_control(self, c: dict, at: datetime) -> None:
        kind = c.get("type")
        entry = {"at": iso(at), "type": kind, "event": c}
        if kind == "dependency_unavailable" or kind == "dependency_degraded":
            state = "unavailable" if kind == "dependency_unavailable" else "degraded"
            self.deps.add(c["dependency"], state, at, parse_ts(c["until"]))
            entry["effect"] = f"{c['dependency']} {state} until {c['until']}"
        elif kind == "goal_rule_corrected":
            with self.conn:
                rule, affected = apply_rule_correction(
                    self.conn, self.goals, c["goal_id"], c["new_success"], at
                )
            self.verifier.correct(c["goal_id"], c["new_success"])
            entry["effect"] = f"{c['goal_id']} now {rule.version}; open goals updated: {len(affected)}"
            entry["affected_goal_instances"] = affected
        elif kind == "title_withdrawn":
            entry.update(self.withdraw(c["title_id"], at))
        else:
            entry["effect"] = "unknown control event: ignored"
        self.control_log.append(entry)

    def withdraw(self, title_id: str, at: datetime) -> dict:
        """Stop using the title from `at`; fix only the live pages and pending messages that contain it (D-011)."""
        self.catalogue.withdraw(title_id, at)
        self.verifier.withdraw(title_id, at)
        republished, cancelled = [], []
        pop = self.popularity.counts(self.conn, self.catalogue, at)
        for user, lp in list(self.live_pages.items()):
            if parse_ts(lp["live_until"]) <= at or title_id not in lp["title_ids"]:
                continue
            page, _, _ = build_page(lp["vf"], lp["goal"], self.catalogue, pop, at, self.rules, self.cfg)
            if page is None:
                page = build_generic_page(
                    self.catalogue, pop, lp["dialect"], lp["vf"].value("app_language"), at, self.rules
                )
            if page is None:
                continue
            check = self.verifier.verify_page(page, lp["trigger"], at)
            if check.verdict != "approve":
                republished.append({"user_id": user, "rejected_by_verifier": check.failed})
                continue
            action = self._page_action(lp["decision_id"], user, lp["trigger_event_id"], at, page)
            self.verifier.record_published(action)
            self.live_pages[user] = {**lp, **self._live(page, action)}
            republished.append(
                {"user_id": user, "action_id": action["action_id"], "replaces": lp["action_id"]}
            )
        for m in self.pending_messages:
            if m["message"]["title_id"] == title_id and m.get("status") == "pending":
                m["status"], m["cancel_reason"] = "cancelled", f"title {title_id} withdrawn at {iso(at)}"
                self.verifier.unschedule(m["decision_id"])
                self.msg_days[(m["user_id"], parse_ts(m["message"]["send_at"]).astimezone(IST).date())] -= 1
                cancelled.append({"user_id": m["user_id"], "decision_id": m["decision_id"]})
        return {
            "effect": f"{title_id} withdrawn",
            "pages_republished": republished,
            "messages_cancelled": cancelled,
        }

    # --- triggers ------------------------------------------------------------------------------------------------

    def on_trigger(self, t: dict, line_no: int) -> None:
        with self.conn:
            try:
                status = ingest_event(self.conn, t, "trigger", line_no, self.cfg)
            except (ValueError, TypeError) as exc:
                self.ingest_stats["trigger"].malformed_lines.append({"line_no": line_no, "error": str(exc)})
                return
        self.ingest_stats["trigger"].add(status)
        if status == ALREADY_INGESTED:
            return
        deliver = parse_ts(t["deliver_at"])
        user, eid = t["user_id"], t["event_id"]
        et = compute_event_time(
            parse_ts(t.get("occurred_at")),
            parse_ts(t.get("sent_at")),
            parse_ts(t["received_at"]),
            self.cfg.max_event_age_days,
        ).event_time
        age = round((deliver - et).total_seconds(), 1)
        base = {"trigger": t, "deliver": deliver, "age": age, "arm": self.arms.get(user), "event_time": et}
        if status in (DUPLICATE, DUPLICATE_CONFLICT):
            self._record(
                base,
                DUPLICATE_IGNORED,
                reason=f"duplicate delivery of trigger {eid}",
                extra={"duplicate_of": self.by_trigger.get(eid)},
                confidence=(0.99, "same event_id already decided"),
                checks={"passed": [], "failed": ["duplicate"]},
            )
            return
        if age > self.cfg.stale_trigger_s:
            self._record(
                base,
                STALE_SKIPPED,
                reason=f"not a live visit: happened {age} s before delivery",
                code="STALE_TRIGGER",
                confidence=(0.95, "true age from server receipt time and phone delay"),
                checks={"passed": [], "failed": ["freshness"]},
            )
            return
        key = (user, t.get("session_id"))
        replan_reason = None
        if key in self.sessions:
            prev = self.sessions[key]
            changed = self.conn.execute(
                "SELECT event, event_id FROM events WHERE user_id = ? AND first_received_at > ? AND event IN (?, ?, ?, ?, ?, ?)"
                " ORDER BY first_received_at LIMIT 1",
                (user, prev["at"], *STATE_CHANGE_EVENTS),
            ).fetchone()
            if changed is None and t.get("app_dialect") == prev["dialect"]:
                self._record(
                    base,
                    SAME_SESSION,
                    reason="second trigger of the same visit; nothing relevant changed",
                    extra={"covered_by": prev["decision_id"]},
                    confidence=(0.9, "same session_id"),
                    checks={"passed": ["freshness", "same_session_no_change"], "failed": []},
                )
                return
            replan_reason = (
                f"same visit, but {changed['event']} ({changed['event_id']})"
                if changed
                else f"same visit, app dialect changed to {t.get('app_dialect')}"
            )
        self.decide(base, replan_reason)

    def decide(self, base: dict, replan_reason: str | None) -> None:
        t, deliver, user = base["trigger"], base["deliver"], base["trigger"]["user_id"]
        holdout = base["arm"] != "agent"  # holdout, or unknown arm: never act
        t0 = time.perf_counter()
        w = min(range(len(self.worker_free)), key=lambda i: self.worker_free[i])
        start = max(deliver, self.worker_free[w])
        queue_s = (start - deliver).total_seconds()
        at = deliver  # what the agent knows: everything received by delivery
        tb = ToolBox(self.cfg, self.deps, start, t["event_id"])
        vf = plan = iv = None
        fallback, model_info, notes = None, {"outcome": "not_called"}, []
        try:
            try:
                vf = tb.store(
                    "read_viewer_state",
                    lambda: resolve(
                        self.conn, self.profiles, self.catalogue, user, at, t, self.cfg, self.plan_days
                    ),
                )
                plan = tb.store(
                    "read_open_goal",
                    lambda: plan_goal(self.conn, vf, at, self.goals, self.catalogue, self.cfg),
                )
                self.facts_cache[user] = (vf, plan, at)
            except DependencyFailed as exc:
                vf, plan, fallback = self._fallback_state(user, t, at, exc)
                notes.append(f"data store: {exc.reason}")
            if vf is not None and fallback is None:
                model_info = self._second_opinion(tb, vf, plan, at, queue_s + (time.perf_counter() - t0))
            pop = tb.local("popularity", lambda: self.popularity.counts(self.conn, self.catalogue, at))
            if fallback == "generic_page":
                iv = self._generic_intervention(t, pop, at)
            else:
                no_msgs = fallback is not None  # cached facts: no messages (D-036)
                iv = tb.local(
                    "decide_intervention",
                    lambda: decide_intervention(
                        vf,
                        plan.goal,
                        plan.outcome,
                        self.catalogue,
                        pop,
                        at,
                        self.rules,
                        self.cfg,
                        live_page=self.live_pages.get(user),
                        messages_on_day=(lambda d: 99) if no_msgs else (lambda d: self.msg_days[(user, d)]),
                    ),
                )
        except BudgetExceeded as exc:
            iv, fallback = None, "budget_exceeded"
            notes.append(str(exc))
        compute_s = time.perf_counter() - t0
        busy_s = compute_s + tb.simulated_s
        published = start + timedelta(seconds=busy_s)
        self.worker_free[w] = published
        latency_s = (published - deliver).total_seconds()
        timing = {
            "queue_s": round(queue_s, 3),
            "compute_ms": round(compute_s * 1000, 2),
            "dependency_s": round(tb.simulated_s, 3),
            "latency_s": round(latency_s, 3),
            "published": published,
        }
        # Step 5: the independent verifier (D-040). Holdout: nothing will be published, but the goal is still checked.
        goal = plan.goal if (plan is not None and fallback is None) else None
        verdict = self.verifier.verify(
            {
                "user": user,
                "trigger": t,
                "known_at": at,
                "publish_at": published,
                "plan_outcome": plan.outcome if plan else None,
                "goal": goal,
                "page": iv.page if (iv is not None and not holdout) else None,
                "message": iv.message if (iv is not None and not holdout) else None,
                "model": model_info,
                "model_calls": tb.model_calls,
                "tool_calls": tb.tool_calls,
                "latency_s": latency_s,
                "fallback": fallback,
            }
        )
        if verdict.verdict == "downgrade" and iv is not None:
            dropped = "; ".join(f"{f['check']}: {f['detail']}" for f in verdict.failed)
            iv.message, iv.message_note = None, f"message dropped by the verifier ({dropped})"
        checks = {"passed": verdict.passed, "failed": [f["check"] for f in verdict.failed]}
        ctx = {
            **base,
            "vf": vf,
            "plan": plan,
            "iv": iv,
            "tb": tb,
            "fallback": fallback,
            "model": model_info,
            "timing": timing,
            "checks": checks,
            "verifier": verdict.to_dict(),
            "notes": notes,
            "replan_reason": replan_reason,
        }
        if latency_s > self.cfg.publish_deadline_s:
            self._record_decision(ctx, FAILED_LATE)
        elif verdict.verdict == "reject":
            self._record_decision(ctx, REJECTED)
        elif holdout:
            if plan is not None and fallback is None:
                with self.conn:
                    commit_plan(self.conn, plan, shadow=True)
            self._record_decision(ctx, HOLDOUT_SHADOW)
        else:
            if plan is not None and fallback is None:
                with self.conn:
                    ctx["goal_instance_id"] = commit_plan(self.conn, plan)
            self._record_decision(ctx, PUBLISHED if iv and iv.action == HOME_PAGE else NO_ACTION_STATUS)
        if vf is not None and fallback is None:
            with self.conn:
                save_snapshot(self.conn, vf, t["event_id"])
        self.sessions.setdefault(
            (user, t.get("session_id")),
            {
                "decision_id": self.decisions[-1]["decision_id"],
                "at": t["received_at"],
                "dialect": t.get("app_dialect"),
            },
        )

    # --- decision helpers ----------------------------------------------------------------------------------------

    def _fallback_state(self, user, t, at, exc):
        """D-036: last known facts (recent, same dialect) or a generic page."""
        cached = self.facts_cache.get(user)
        if (
            cached
            and at - cached[2] <= CACHED_FACTS_MAX_AGE
            and cached[0].value("page_dialect") == t.get("app_dialect")
        ):
            self._cached_age_s = int((at - cached[2]).total_seconds())
            return cached[0], cached[1], "cached_facts"
        return None, None, "generic_page"

    def _generic_intervention(self, t, pop, at):
        from .intervention import Intervention

        dialect = t.get("app_dialect")
        if dialect not in VALID_DIALECTS:
            return Intervention(
                NO_ACTION, "data store unavailable and app dialect invalid", "DEPENDENCY_DOWN"
            )
        page = build_generic_page(self.catalogue, pop, dialect, t.get("app_language"), at, self.rules)
        if page is None:
            return Intervention(
                NO_ACTION, "data store unavailable and no titles in the app dialect", "DEPENDENCY_DOWN"
            )
        content = [f"title:{page['hero_title_id']}"] + [
            f"title:{x}" for r in page["rows"] for x in r["title_ids"]
        ]
        return Intervention(
            HOME_PAGE,
            f"fallback: generic popular page in {dialect} (viewer state unreadable)",
            None,
            page,
            None,
            "no message in fallback",
            content,
            [f"event:{t['event_id']}"] + [f"catalogue:{c.split(':')[1]}" for c in content],
        )

    def _second_opinion(self, tb, vf, plan, at, behind_s) -> dict:
        """D-035: ask the model, never trust it."""
        if behind_s > self.cfg.model_skip_after_s:
            return {"outcome": "skipped", "reason": f"decision already {behind_s:.1f} s behind its trigger"}
        proposed = plan.goal["goal_id"] if plan.goal else None
        eligible = [g for g, why in plan.evaluations.items() if why == "eligible"]
        if plan.outcome == "KEEP":
            eligible = [proposed]
        request = build_request(vf, proposed, eligible, at)
        info = {"provider": getattr(self.provider, "name", "model"), "proposed": proposed, "attempts": 0}
        for _ in range(1 + self.cfg.model_retries):
            info["attempts"] += 1
            reply = tb.model(self.provider, request)
            if reply.kind == "error":
                info.update(
                    outcome="unavailable" if "503" in (reply.error or "") else "error", error=reply.error
                )
                return info
            review, err = parse_review(reply.payload)
            if err:
                info.update(outcome="malformed", error=err)
                continue  # retry once within budget
            info["review"] = review
            gid = review["goal_id"]
            if gid == proposed:
                info["outcome"] = (
                    "agreed" if review["eligible"] or proposed is None else "disagreed_without_evidence"
                )
            elif gid not in self.goals.rules:
                info.update(outcome="unsupported_claim", failed_check=f"unknown goal {gid!r}")
            else:
                ev = evaluate(self.goals.rules[gid], vf, at, self.catalogue, self.cfg)
                if review["eligible"] and not ev.eligible:
                    info.update(outcome="unsupported_claim", claimed_goal=gid, failed_check=ev.reason)
                else:
                    info.update(
                        outcome="alternative_noted",
                        claimed_goal=gid,
                        note="evidence also supports it; the priority order decides (D-019/D-030)",
                    )
            return info
        return info

    # --- records and actions -------------------------------------------------------------------------------------

    def _next_id(self, prefix: str, items: list) -> str:
        return f"{prefix}-{len(items) + 1:05d}"

    def _trigger_timing(self, base) -> dict:
        """Freshness evidence of the trigger itself (D-005, D-006): when it happened vs when it reached us."""
        t = base["trigger"]
        return {
            "ref": f"event:{t['event_id']}",
            "received_at": t["received_at"],
            "event_time": iso(base["event_time"]),
            "deliver_at": t["deliver_at"],
            "age_s": base["age"],
            "freshness": "live" if base["age"] <= self.cfg.stale_trigger_s else "stale",
        }

    def _record(self, base, status, reason, code=None, extra=None, confidence=(0.9, ""), checks=None) -> dict:
        t = base["trigger"]
        rec = {
            "decision_id": self._next_id("D", self.decisions),
            "viewer_id": t["user_id"],
            "arm": base["arm"],
            "trigger": {
                "event": t["event"],
                "event_id": t["event_id"],
                "received_at": t["received_at"],
                "deliver_at": t["deliver_at"],
                "event_age_s": base["age"],
            },
            "goal": None,
            "action": NO_ACTION,
            "no_action_reason": {"code": code or status, "detail": reason},
            "dialect": {
                "used": None,
                "source": "trigger.app_setting",
                "app_setting": t.get("app_dialect"),
                "conflicts": [],
                "resolution": f"not decided ({status}): nothing was published, so no dialect was chosen",
            },
            "content": [],
            "confidence": confidence[0],
            "confidence_reason": confidence[1],
            "evidence": [f"event:{t['event_id']}"],
            "evidence_detail": [self._trigger_timing(base)],
            "assumptions": [],
            "conflicts": [],
            "checks": checks or {"passed": [], "failed": []},
            "model": {"outcome": "not_called"},
            "model_calls": 0,
            "tool_calls": 0,
            "fallback_used": None,
            "decided_in_ms": 0,
            "published_at": None,
            "status": status,
        }
        rec.update(extra or {})
        self.decisions.append(rec)
        self.by_trigger.setdefault(t["event_id"], rec["decision_id"])
        return rec

    def _record_decision(self, ctx: dict, status: str) -> None:
        t, vf, plan, iv, tb, timing = (
            ctx["trigger"],
            ctx["vf"],
            ctx["plan"],
            ctx["iv"],
            ctx["tb"],
            ctx["timing"],
        )
        goal = plan.goal if plan else None
        pd = vf["page_dialect"] if vf else None
        conf, conf_reason = self._confidence(ctx, status)
        evidence = (
            [f"event:{t['event_id']}"] + (goal["evidence"] if goal else []) + (iv.evidence if iv else [])
        )
        evidence_detail = [self._trigger_timing(ctx)]
        evidence_detail += [
            {"ref": "dependency:user_data_store", "detail": n}
            for n in ctx["notes"]
            if n.startswith("data store")
        ]
        if vf is not None:
            for name in (
                "page_dialect",
                "subscription_status",
                "signup_at",
                "has_played",
                "has_completed_episode",
            ):
                f = vf[name]
                evidence_detail.append(
                    {
                        "ref": f"fact:{name}",
                        "value": f.value,
                        "source": f.source,
                        "as_of": f.as_of,
                        "age_s": f.age_s,
                        "freshness": f.freshness,
                        "usable": f.usable,
                    }
                )
        assumptions = (
            (goal["assumptions"] if goal else [])
            + (iv.assumptions if iv else [])
            + (vf.assumptions() if vf else [])
        )
        published = status == PUBLISHED and iv is not None and iv.action == HOME_PAGE
        rec = {
            "decision_id": self._next_id("D", self.decisions),
            "viewer_id": t["user_id"],
            "arm": ctx["arm"],
            "trigger": {
                "event": t["event"],
                "event_id": t["event_id"],
                "received_at": t["received_at"],
                "deliver_at": t["deliver_at"],
                "event_age_s": ctx["age"],
            },
            "goal": (
                {
                    "id": goal["name"],
                    "goal_id": goal["goal_id"],
                    "start": goal["start"],
                    "deadline": goal["deadline"],
                    "success_signal": goal["success_signal"],
                    "rule_version": goal["rule_version"],
                    "plan": plan.outcome,
                    "why": plan.reason,
                }
                if goal
                else None
            ),
            "goal_note": (
                plan.reason
                if plan and plan.outcome == NO_GOAL
                else "viewer state unreadable: goal not evaluated, generic page only (D-036)"
                if ctx["fallback"] == "generic_page"
                else None
            ),
            "replan_reason": ctx["replan_reason"],
            "action": iv.action if iv else NO_ACTION,
            "no_action_reason": (
                {"code": iv.no_action_code, "detail": iv.reason}
                if iv and iv.no_action_code
                else {"code": ctx["fallback"].upper(), "detail": "; ".join(ctx["notes"])}
                if iv is None
                else None
            ),
            "dialect": (
                {
                    "used": pd.value,
                    "source": pd.source,
                    "conflicts": pd.conflicts,
                    "resolution": "the app requests the page for its current setting (D-009)",
                }
                if pd
                else {
                    "used": t.get("app_dialect"),
                    "source": "trigger.app_setting (fallback)",
                    "conflicts": [],
                    "resolution": "viewer state unreadable; dialect from the app open itself (D-036)",
                }
            ),
            "content": iv.content if iv else [],
            "page": iv.page if iv else None,
            "message": (
                {**iv.message, "status": "pending_send_time_check"}
                if iv and iv.message and published
                else None
            ),
            "message_note": iv.message_note if iv else None,
            "confidence": conf,
            "confidence_reason": conf_reason,
            "evidence": list(dict.fromkeys(evidence)),
            "evidence_detail": evidence_detail,
            "conflicts": _resolved_conflicts(vf),
            "assumptions": list(dict.fromkeys(assumptions)),
            "checks": ctx["checks"],
            "verifier": ctx.get("verifier"),
            "model": ctx["model"],
            "model_calls": tb.model_calls,
            "tool_calls": tb.tool_calls,
            "tool_log": tb.log,
            "fallback_used": ctx["fallback"],
            "notes": ctx["notes"],
            "queue_s": timing["queue_s"],
            "decided_in_ms": round(timing["compute_ms"] + timing["dependency_s"] * 1000, 1),
            "latency_s": timing["latency_s"],
            "published_at": iso(timing["published"])
            if status in (PUBLISHED, HOLDOUT_SHADOW, NO_ACTION_STATUS)
            else None,
            "status": status,
        }
        if ctx["fallback"] == "cached_facts":
            rec["fallback_detail"] = (
                f"last known facts from {self._cached_age_s} s earlier (data store unreadable)"
            )
        if status == HOLDOUT_SHADOW:
            # Nothing is published for holdout viewers: the record says so, the would-be action is kept for analysis.
            rec["shadow_action"] = {
                "action": rec["action"],
                "page": rec.pop("page"),
                "content": rec["content"],
            }
            rec.update(
                action=NO_ACTION,
                content=[],
                message=None,
                no_action_reason={
                    "code": "HOLDOUT",
                    "detail": "holdout arm: decided as a shadow, nothing published",
                },
            )
        self.decisions.append(rec)
        self.by_trigger.setdefault(t["event_id"], rec["decision_id"])
        if published:
            action = self._page_action(
                rec["decision_id"], t["user_id"], t["event_id"], timing["published"], iv.page
            )
            self.verifier.record_published(action)
            self.live_pages[t["user_id"]] = {
                "decision_id": rec["decision_id"],
                "trigger_event_id": t["event_id"],
                "trigger": t,
                "vf": vf,
                "goal": goal,
                **self._live(iv.page, action),
            }
            if iv.message:
                day = parse_ts(iv.message["send_at"]).astimezone(IST).date()
                self.msg_days[(t["user_id"], day)] += 1
                self.verifier.schedule(rec["decision_id"], t["user_id"], parse_ts(iv.message["send_at"]))
                self.pending_messages.append(
                    {
                        "decision_id": rec["decision_id"],
                        "user_id": t["user_id"],
                        "trigger_event_id": t["event_id"],
                        "published_at": timing["published"],
                        "goal_instance_id": ctx.get("goal_instance_id"),
                        "goal": goal,
                        "message": iv.message,
                        "status": "pending",
                    }
                )

    def _confidence(self, ctx, status) -> tuple[float, str]:
        """Transparent rule: start 0.6; +0.1 model agreed; -0.05 per assumption (max -0.2); -0.05 dialect conflicts;
        fallback capped at 0.3. No-action outcomes that follow from rules alone: 0.9."""
        iv, vf, model = ctx["iv"], ctx["vf"], ctx["model"]
        if ctx["fallback"] in ("generic_page", "budget_exceeded"):
            return 0.3, f"fallback ({ctx['fallback']}): no personal facts used"
        if iv is not None and iv.action == NO_ACTION:
            return 0.9, f"rule-based no-action ({iv.no_action_code})"
        score, why = 0.6, ["base 0.6"]
        if model.get("outcome") == "agreed":
            score += 0.1
            why.append("+0.1 model second opinion agreed (not evidence, only consistency)")
        n_assume = len((ctx["plan"].goal or {}).get("assumptions", [])) + len(iv.assumptions if iv else [])
        if n_assume:
            score -= min(0.2, 0.05 * n_assume)
            why.append(f"-{min(0.2, 0.05 * n_assume):.2f} for {n_assume} assumption(s)")
        if vf is not None and vf["page_dialect"].conflicts:
            score -= 0.05
            why.append("-0.05 dialect sources disagree")
        if ctx["fallback"]:
            score = min(score, 0.4)
            why.append(f"capped at 0.4: {ctx['fallback']}")
        return round(max(0.05, min(0.95, score)), 2), "; ".join(why)

    def _page_action(self, decision_id, user, trigger_id, at, page) -> dict:
        action = {
            "action_id": self._next_id("A", self.actions),
            "decision_id": decision_id,
            "user_id": user,
            "trigger_event_id": trigger_id,
            "type": "home_page",
            "published_at": iso(at),
            "dialect": page["dialect"],
            "hero_title_id": page["hero_title_id"],
            "rows": page["rows"],
        }
        self.actions.append(action)
        return action

    @staticmethod
    def _live(page, action) -> dict:
        ids = ([page["hero_title_id"]] if page["hero_title_id"] else []) + [
            x for r in page["rows"] for x in r["title_ids"]
        ]
        return {
            "action_id": action["action_id"],
            "dialect": page["dialect"],
            "live_until": page["live_until"],
            "title_ids": ids,
        }

    # --- pending messages: re-check at send time (D-021, D-034) ---------------------------------------------------

    def flush_messages(self) -> None:
        """Send-time re-check of every pending reminder by the independent verifier (D-021, D-034, D-040).
        Knowledge = events received by the earlier of the send time and the end of the replay."""
        for m in sorted(self.pending_messages, key=lambda x: x["message"]["send_at"]):
            if m["status"] != "pending":
                continue
            msg, user = m["message"], m["user_id"]
            send_at = parse_ts(msg["send_at"])
            know = min(send_at, self.replay_end) if self.replay_end else send_at
            gi = self.conn.execute(
                "SELECT status FROM goal_instances WHERE goal_instance_id = ?", (m["goal_instance_id"],)
            ).fetchone()
            verdict = self.verifier.verify_send(m, know, goal_open=gi is not None and gi["status"] == "open")
            m["send_check"] = verdict.to_dict()
            self.verifier.unschedule(m["decision_id"])
            if verdict.verdict != "approve":
                m["status"] = "cancelled"
                m["cancel_reason"] = "; ".join(f"{f['check']}: {f['detail']}" for f in verdict.failed)
                continue
            m["status"] = "sent"
            action = {
                "action_id": self._next_id("A", self.actions),
                "decision_id": m["decision_id"],
                "user_id": user,
                "trigger_event_id": m["trigger_event_id"],
                "type": msg["channel"],
                "published_at": iso(m["published_at"]),
                "send_at": msg["send_at"],
                "title_id": msg["title_id"],
                "deeplink": msg["deeplink"],
            }
            if msg["channel"] == "whatsapp":
                action.update(template_id=msg["template_id"], variables=msg["variables"])
            else:
                action["text"] = msg["text"]
            self.actions.append(action)
            self.verifier.record_published(action)
        by_id = {d["decision_id"]: d for d in self.decisions}
        for m in self.pending_messages:
            rec = by_id[m["decision_id"]]
            if rec.get("message"):
                rec["message"]["status"] = m["status"]
                rec["message"]["send_time_check"] = m.get("send_check")
                if m.get("cancel_reason"):
                    rec["message"]["cancel_reason"] = m["cancel_reason"]

    # --- outputs -------------------------------------------------------------------------------------------------

    def write_outputs(self) -> dict:
        def dump(name, rows):
            with open(self.out / name, "w", encoding="utf-8") as f:
                f.writelines(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows)

        dump("decisions.jsonl", self.decisions)
        dump("actions.jsonl", self.actions)
        dump("control_log.jsonl", self.control_log)
        report = build_report(self.conn, self.cfg, [self.history_stats, *self.ingest_stats.values()])
        write_report(report, self.out)
        latency = self.latency_report()
        (self.out / "latency_report.json").write_text(json.dumps(latency, indent=2), encoding="utf-8")
        self.export_viewer_state()
        return latency

    def latency_report(self) -> dict:
        timed = [d for d in self.decisions if "latency_s" in d]
        lat = sorted(d["latency_s"] for d in timed)
        pub = sorted(d["latency_s"] for d in timed if d["status"] == PUBLISHED)

        def pct(v, p):
            return round(v[min(len(v) - 1, int(len(v) * p / 100))], 3) if v else None

        model = Counter(d["model"].get("outcome") for d in self.decisions)
        return {
            "triggers": len(self.decisions),
            "status": dict(Counter(d["status"] for d in self.decisions)),
            "no_action_reasons": dict(
                Counter(
                    (d["no_action_reason"] or {}).get("code")
                    for d in self.decisions
                    if d.get("no_action_reason")
                )
            ),
            "actions": dict(Counter(a["type"] for a in self.actions)),
            "messages": dict(Counter(m["status"] for m in self.pending_messages)),
            "latency_s_all_decided": {
                "p50": pct(lat, 50),
                "p95": pct(lat, 95),
                "p99": pct(lat, 99),
                "max": lat[-1] if lat else None,
            },
            "latency_s_published_pages": {
                "p50": pct(pub, 50),
                "p95": pct(pub, 95),
                "p99": pct(pub, 99),
                "max": pub[-1] if pub else None,
            },
            "over_60s": sum(x > 60 for x in lat),
            "over_5min": sum(x > 300 for x in lat),
            "queue_s_max": max((d["queue_s"] for d in timed), default=None),
            "max_model_calls_per_decision": max((d["model_calls"] for d in self.decisions), default=0),
            "max_tool_calls_per_decision": max((d["tool_calls"] for d in self.decisions), default=0),
            "model_outcomes": dict(model),
            "fallbacks": dict(Counter(d["fallback_used"] for d in self.decisions if d.get("fallback_used"))),
            "control_events": [
                {"at": c["at"], "type": c["type"], "effect": c.get("effect")} for c in self.control_log
            ],
            "wall_clock_s": round(self.wall_s, 1),
            "budgets": {
                "max_model_calls": self.cfg.max_model_calls,
                "max_tool_calls": self.cfg.max_tool_calls,
                "publish_deadline_s": self.cfg.publish_deadline_s,
            },
            "note": "simulated clock (D-037): latency = queue wait + measured compute + simulated dependency time",
        }

    def export_viewer_state(self) -> None:
        d = self.out / "viewer_state"
        d.mkdir(exist_ok=True)
        for old in d.glob("*.json"):
            old.unlink()
        per_user = defaultdict(lambda: {"decisions": [], "actions": []})
        for r in self.decisions:
            per_user[r["viewer_id"]]["decisions"].append(
                {
                    "decision_id": r["decision_id"],
                    "status": r["status"],
                    "action": r["action"],
                    "goal": (r.get("goal") or {}).get("goal_id"),
                    "at": r["trigger"]["deliver_at"],
                }
            )
        for a in self.actions:
            per_user[a["user_id"]]["actions"].append(a["action_id"])
        for user, v in per_user.items():
            goals = [
                dict(r)
                for r in self.conn.execute(
                    "SELECT goal_instance_id, goal_id, rule_version, status,"
                    " close_reason, start_at, deadline, shadow FROM goal_instances WHERE user_id = ?",
                    (user,),
                )
            ]
            log = [
                dict(r)
                for r in self.conn.execute(
                    "SELECT at, goal_instance_id, change, detail_json FROM goal_log"
                    " WHERE user_id = ? ORDER BY rowid",
                    (user,),
                )
            ]
            snap = self.conn.execute(
                "SELECT facts_json FROM facts_snapshots WHERE user_id = ? ORDER BY at DESC LIMIT 1", (user,)
            ).fetchone()
            state = {
                "user_id": user,
                "arm": self.arms.get(user),
                "goals": goals,
                "goal_log": log,
                **v,
                "last_known_facts": json.loads(snap["facts_json"]) if snap else None,
            }
            (d / f"{user}.json").write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


def latency_summary(rep: dict) -> str:
    p = rep["latency_s_published_pages"]
    return (
        f"{rep['triggers']} triggers | status {rep['status']} | pages p50 {p['p50']} s, p95 {p['p95']} s, "
        f"max {p['max']} s | >60 s: {rep['over_60s']} | model {rep['model_outcomes']} | "
        f"fallbacks {rep['fallbacks']} | messages {rep['messages']}"
    )
