"""Fact resolver: what the agent knows about one viewer at time `at` (Traps 4–7, 12; D-007–D-010, D-015, D-027).

Every fact carries its source, the time it reflects (as_of), its age at decision time, a freshness label and whether
it is usable for acting, evidence references another person can inspect, conflicting values, and an assumption when
the value is a default rather than an observation. Only events received by `at` are used, so the result answers
"what did the agent know at time T". Precedence and freshness rules per fact: ARCHITECTURE.md §5.1.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .catalogue import VALID_DIALECTS, Catalogue
from .config import Config
from .profiles import Profile
from .timeutil import compute_event_time, iso, ist_day, parse_ts

DEEPLINK = re.compile(r"^ott://c/([A-Za-z0-9_-]+)/([A-Za-z0-9]+)/title/(T\d+)$")
SERIES_TYPES = frozenset({"show", "micro_drama"})
BILLING_EVENTS = ("purchase_completed", "subscription_renewed", "subscription_lapsed")

# Freshness class per fact (D-027):
#   immutable  never goes stale (signup time)
#   live       only valid from the current, fresh trigger (what the app is showing right now)
#   state      can change; events are authoritative, the profile snapshot only while the snapshot is recent
#   history    a record of the past; always usable, age reported
FACT_CLASS = {
    "signup_at": "immutable",
    "page_dialect": "live",
    "deeplink": "live",
    "subscription_status": "state",
    "push_consent": "state",
    "whatsapp_consent": "state",
    "message_dialect": "state",
    "app_language": "state",
    "ever_paid": "history",
    "has_played": "history",
    "has_completed_episode": "history",
    "last_app_open_at": "history",
    "play_ist_days_14d": "history",
    "series_in_progress": "history",
    "top_genre": "history",
}


@dataclass
class Fact:
    name: str
    value: Any
    source: str  # e.g. "event:<id>", "profile_snapshot", "trigger.app_setting", "derived:events", "default"
    as_of: str | None  # time the source reflects (ISO, UTC)
    age_s: float | None = None
    freshness: str = "unknown"  # live | fresh | recent | aging | old | stale | unknown | n/a
    usable: bool = True  # False: the agent must not act on this value (treated as unknown downstream)
    evidence: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    assumption: str | None = None


def _age_label(age_s: float | None) -> str:
    if age_s is None:
        return "unknown"
    if age_s <= 3600:
        return "fresh"
    if age_s <= 48 * 3600:
        return "recent"
    if age_s <= 7 * 86400:
        return "aging"
    return "old"


def apply_freshness(f: Fact, cfg: Config, trigger_true_age_s: float | None) -> None:
    """Set freshness and usable from the fact's class and source (D-027)."""
    cls = FACT_CLASS.get(f.name, "history")
    if cls == "immutable":
        f.freshness = "n/a"
    elif cls == "live":
        if f.source.startswith("trigger") and trigger_true_age_s is not None:
            live = trigger_true_age_s <= cfg.stale_trigger_s
            f.freshness, f.usable = ("live", True) if live else ("stale", False)
        else:
            f.freshness, f.usable = "unknown", False
    elif cls == "state" and f.source == "profile_snapshot":
        if f.age_s is not None and f.age_s > cfg.profile_max_age_h * 3600:
            f.freshness, f.usable = "stale", False
        else:
            f.freshness = _age_label(f.age_s)
    else:
        f.freshness = _age_label(f.age_s)
    if f.name == "subscription_status" and f.value == "uncertain":
        f.usable = False


@dataclass
class ViewerFacts:
    user_id: str
    at: str
    facts: dict[str, Fact] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)  # e.g. malformed deeplink

    def __getitem__(self, name: str) -> Fact:
        return self.facts[name]

    def value(self, name: str) -> Any:
        return self.facts[name].value

    def usable(self, name: str) -> Any:
        """The value if the agent may act on it, else None (unknown)."""
        f = self.facts[name]
        return f.value if f.usable else None

    def assumptions(self) -> list[str]:
        return [f"{f.name}: {f.assumption}" for f in self.facts.values() if f.assumption]

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "at": self.at,
            "flags": self.flags,
            "facts": {k: asdict(v) for k, v in self.facts.items()},
        }


def save_snapshot(conn: sqlite3.Connection, vf: ViewerFacts, trigger_event_id: str | None = None) -> None:
    """Persist what the agent knew (durable state, auditability). The caller owns the transaction."""
    conn.execute(
        "INSERT OR REPLACE INTO facts_snapshots (user_id, at, trigger_event_id, facts_json) VALUES (?, ?, ?, ?)",
        (vf.user_id, vf.at, trigger_event_id or "", json.dumps(vf.to_dict(), sort_keys=True)),
    )


def load_snapshots(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    rows = conn.execute("SELECT facts_json FROM facts_snapshots WHERE user_id = ? ORDER BY at", (user_id,))
    return [json.loads(r["facts_json"]) for r in rows]


def parse_deeplink(link: str | None) -> dict | None:
    """Campaign deeplink as data (never instruction). Returns None for anything not matching the strict format."""
    if not link:
        return None
    m = DEEPLINK.match(link)
    if not m:
        return None
    return {"campaign": m.group(1), "dialect": m.group(2), "title_id": m.group(3)}


def _known_events(conn: sqlite3.Connection, user_id: str, at: datetime) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT event_id, event, event_time, first_received_at, app_dialect, app_language, payload_json"
        " FROM events WHERE user_id = ? AND first_received_at <= ? ORDER BY first_received_at",
        (user_id, iso(at)),
    ).fetchall()


def resolve(
    conn: sqlite3.Connection,
    profiles: dict[str, Profile],
    catalogue: Catalogue,
    user_id: str,
    at: datetime,
    trigger: dict | None = None,
    cfg: Config | None = None,
    plan_days: dict[str, int] | None = None,
) -> ViewerFacts:
    cfg = cfg or Config()
    vf = ViewerFacts(user_id=user_id, at=iso(at))
    profile = profiles.get(user_id)
    events = _known_events(conn, user_id, at)
    props = {r["event_id"]: json.loads(r["payload_json"]).get("properties") or {} for r in events}
    trigger_id = trigger.get("event_id") if trigger else None
    trigger_age = None
    if trigger and trigger.get("received_at"):
        et = compute_event_time(
            parse_ts(trigger.get("occurred_at")),
            parse_ts(trigger.get("sent_at")),
            parse_ts(trigger["received_at"]),
            cfg.max_event_age_days,
        )
        trigger_age = (at - et.event_time).total_seconds()

    def age(as_of: str | None) -> float | None:
        return round((at - parse_ts(as_of)).total_seconds(), 1) if as_of else None

    def add(f: Fact) -> None:
        f.age_s = age(f.as_of)
        apply_freshness(f, cfg, trigger_age)
        vf.facts[f.name] = f

    add(_signup(profile, events))
    _subscription(add, profile, events, props, at, plan_days)
    for channel, field_name in (("push", "notifications_opt_in"), ("whatsapp", "whatsapp_opt_in")):
        add(_consent(channel, field_name, profile, events, props))
    _dialects(add, vf, profile, events, trigger)
    _history(add, catalogue, events, props, at, trigger_id)
    return vf


def _signup(profile: Profile | None, events) -> Fact:
    if profile and profile.get("signup_at"):
        return Fact(
            "signup_at",
            profile.get("signup_at"),
            "profile_snapshot",
            profile.snapshot_at,
            evidence=[f"profile:{profile.user_id}@{profile.updated_at}"],
        )
    signups = [r for r in events if r["event"] == "signup_completed"]
    if signups:
        r = min(signups, key=lambda r: r["event_time"])
        return Fact(
            "signup_at",
            r["event_time"],
            f"event:{r['event_id']}",
            r["first_received_at"],
            evidence=[f"event:{r['event_id']}"],
        )
    if events:
        first = min(events, key=lambda r: r["event_time"])
        return Fact(
            "signup_at",
            first["event_time"],
            "derived:first_seen",
            first["first_received_at"],
            evidence=[f"event:{first['event_id']}"],
            assumption="no profile row or signup event; signed up on or before first-seen time",
        )
    return Fact("signup_at", None, "unknown", None, assumption="no profile row and no events")


def _subscription(add, profile: Profile | None, events, props, at: datetime, plan_days: dict | None) -> None:
    """D-015: billing events newer than the profile override it; expiries and contradictions -> uncertain."""
    conflicts: list[str] = []
    evidence: list[str] = []
    status, source, as_of, assumption = None, "unknown", None, None

    if profile and profile.get("subscription_status"):
        status, source, as_of = profile.get("subscription_status"), "profile_snapshot", profile.snapshot_at
        evidence.append(f"profile:{profile.user_id}@{profile.updated_at}")
        conflicts += profile.conflicts("subscription_status")
    elif profile and "subscription_status" in profile.uncertain_fields:
        status, source, as_of = "uncertain", "profile_snapshot", profile.snapshot_at
        conflicts += profile.conflicts("subscription_status")

    billing = sorted((r for r in events if r["event"] in BILLING_EVENTS), key=lambda r: r["event_time"])
    paid_events = [
        r
        for r in billing
        if r["event"] == "subscription_renewed"
        or (r["event"] == "purchase_completed" and not props[r["event_id"]].get("is_trial"))
    ]
    newer = [r for r in billing if profile is None or r["event_time"] > profile.updated_at]

    if newer:
        last = newer[-1]
        if last["event"] == "subscription_lapsed":
            new_status = "lapsed"
        elif last["event"] == "purchase_completed" and props[last["event_id"]].get("is_trial"):
            new_status = "trial_active"
        else:
            new_status = "active"
        if status and status != new_status:
            conflicts.append(f"{source}:{status}")
        status, source, as_of = new_status, f"event:{last['event_id']}", last["first_received_at"]
        evidence.append(f"event:{last['event_id']}")
        if status in ("active", "trial_active"):
            # Same rule as the profile's plan_ends_at: a plan bought or renewed by this event runs out after the plan
            # length in pricing.json; with no later renewal seen, "active" can no longer be trusted.
            plan_id = props[last["event_id"]].get("plan_id")
            days = (plan_days or {}).get(plan_id)
            if days is None:
                assumption = f"plan length unknown for {plan_id!r}: expiry not checked"
            else:
                ends = parse_ts(last["event_time"]) + timedelta(days=days)
                if ends <= at:
                    conflicts.append(f"event:{last['event_id']}:plan_{plan_id}_ended:{iso(ends)}")
                    status = "uncertain"
    elif profile and status in ("never", "trial_active"):
        older_paid = [r for r in paid_events if r["event_time"] <= profile.updated_at]
        if status == "never" and older_paid:
            conflicts.append(f"event:{older_paid[-1]['event_id']}:paid_before_snapshot")
            status = "uncertain"
        if status == "never" and profile.get("plan_ends_at"):
            conflicts.append(f"profile.plan_ends_at:{profile.get('plan_ends_at')}")
            status = "uncertain"
        # A subscription cannot go back to "never": an older row showing a paid status contradicts the newer one.
        if status == "never" and any(
            r.get("subscription_status") in ("active", "lapsed") for r in profile.losing_rows
        ):
            status = "uncertain"
    plan_ends = profile.get("plan_ends_at") if profile else None
    if status == "active" and source == "profile_snapshot" and plan_ends and parse_ts(plan_ends) <= at:
        # The plan ended and no renewal has been seen yet.
        conflicts.append(f"profile.plan_ends_at:{plan_ends}:passed")
        status = "uncertain"
    if status is None:
        status, source, assumption = (
            "never",
            "default",
            "no profile row and no billing events: assumed never paid",
        )

    add(
        Fact(
            "subscription_status",
            status,
            source,
            as_of,
            evidence=evidence,
            conflicts=conflicts,
            assumption=assumption,
        )
    )
    # Any row of the profile (including older duplicate rows) showing a paid status counts: status cannot regress.
    paid_rows = [
        r
        for r in ([profile.row, *profile.losing_rows] if profile else [])
        if r.get("subscription_status") in ("active", "lapsed")
    ]
    ever_paid = bool(paid_events) or bool(paid_rows)
    add(
        Fact(
            "ever_paid",
            ever_paid,
            "derived:billing_events+profile",
            as_of,
            evidence=[f"event:{r['event_id']}" for r in paid_events[-3:]]
            + [f"profile_row@{r['updated_at']}:{r['subscription_status']}" for r in paid_rows],
        )
    )


def _consent(channel: str, field_name: str, profile: Profile | None, events, props) -> Fact:
    """Policy rule 1: an opt-out event overrides the profile; unknown consent means no consent.

    Conservative by design: ANY opt-out seen for the channel withdraws consent, even one older than the snapshot,
    because the data has no opt-in event that could prove the viewer consented again.
    """
    opt_outs = [
        r
        for r in events
        if r["event"] == "notification_opt_out" and props[r["event_id"]].get("channel") == channel
    ]
    name = f"{channel}_consent"
    if opt_outs:
        r = opt_outs[-1]
        conflicts = (
            [f"profile_snapshot:{profile.get(field_name)}"]
            if profile and profile.get(field_name) == "true"
            else []
        )
        return Fact(
            name,
            False,
            f"event:{r['event_id']}",
            r["first_received_at"],
            evidence=[f"event:{r['event_id']}"],
            conflicts=conflicts,
        )
    if profile and profile.get(field_name) in ("true", "false"):
        return Fact(
            name,
            profile.get(field_name) == "true",
            "profile_snapshot",
            profile.snapshot_at,
            evidence=[f"profile:{profile.user_id}@{profile.updated_at}"],
        )
    return Fact(name, False, "default", None, assumption="consent unknown: treated as no consent")


def _dialects(add, vf: ViewerFacts, profile: Profile | None, events, trigger: dict | None) -> None:
    """D-009: the page dialect is the trigger's app setting (the only page the app will request)."""
    profile_dialect = profile.get("preferred_dialect") if profile else None
    raw_link = ((trigger or {}).get("properties") or {}).get("deeplink")
    deeplink = parse_deeplink(raw_link)
    if raw_link and deeplink is None:
        vf.flags.append(f"malformed_deeplink:{raw_link}")
    if deeplink and deeplink["dialect"] not in VALID_DIALECTS:
        vf.flags.append(f"deeplink_invalid_dialect:{deeplink['dialect']}")
    latest_app = next((r for r in reversed(events) if r["app_dialect"] in VALID_DIALECTS), None)

    trig_dialect = (trigger or {}).get("app_dialect")
    trig_as_of = (trigger or {}).get("received_at")
    conflicts = []
    if profile_dialect:
        conflicts.append(f"profile:{profile_dialect}")
    conflicts += profile.conflicts("preferred_dialect") if profile else []
    if deeplink:
        conflicts.append(f"deeplink:{deeplink['dialect']}")

    if trigger is not None and trig_dialect in VALID_DIALECTS:
        add(
            Fact(
                "page_dialect",
                trig_dialect,
                "trigger.app_setting",
                trig_as_of,
                evidence=[f"event:{trigger['event_id']}"],
                conflicts=[c for c in conflicts if not c.endswith(":" + trig_dialect)],
            )
        )
    elif trigger is not None:
        conflicts.insert(0, f"trigger.app_setting:{trig_dialect}")
        add(
            Fact(
                "page_dialect",
                None,
                "none",
                trig_as_of,
                conflicts=conflicts,
                assumption="DIALECT_UNKNOWN: the app's current dialect is missing or invalid; no page can be shown",
            )
        )
    else:
        add(
            Fact(
                "page_dialect",
                None,
                "none",
                None,
                conflicts=conflicts,
                assumption="no trigger: the page dialect only comes from a live app open",
            )
        )

    if trigger is not None and trig_dialect in VALID_DIALECTS:
        msg = Fact(
            "message_dialect",
            trig_dialect,
            "trigger.app_setting",
            trig_as_of,
            evidence=[f"event:{trigger['event_id']}"],
        )
    elif latest_app is not None:
        msg = Fact(
            "message_dialect",
            latest_app["app_dialect"],
            f"event:{latest_app['event_id']}",
            latest_app["first_received_at"],
            evidence=[f"event:{latest_app['event_id']}"],
        )
    elif profile_dialect in VALID_DIALECTS:
        msg = Fact(
            "message_dialect",
            profile_dialect,
            "profile_snapshot",
            profile.snapshot_at,
            evidence=[f"profile:{profile.user_id}@{profile.updated_at}"],
        )
    else:
        msg = Fact(
            "message_dialect", None, "none", None, assumption="no valid dialect known: no message possible"
        )
    msg.conflicts = [c for c in conflicts if msg.value is None or not c.endswith(":" + msg.value)]
    add(msg)

    lang = (trigger or {}).get("app_language") or next(
        (r["app_language"] for r in reversed(events) if r["app_language"]), None
    )
    if lang:
        add(
            Fact(
                "app_language",
                lang,
                "trigger.app_setting" if (trigger or {}).get("app_language") else "events",
                trig_as_of if (trigger or {}).get("app_language") else events[-1]["first_received_at"],
            )
        )
    else:
        add(Fact("app_language", "hin", "default", None, assumption="no app language seen: default hin"))
    add(
        Fact(
            "deeplink", deeplink, "trigger.deeplink" if deeplink else "none", trig_as_of if deeplink else None
        )
    )


def _history(add, catalogue: Catalogue, events, props, at: datetime, trigger_id: str | None) -> None:
    """Watch and visit history in corrected event time (D-005), content identified by slug (D-010)."""
    plays = [r for r in events if r["event"] == "play_started"]
    completes = [r for r in events if r["event"] == "episode_completed"]
    opens = [r for r in events if r["event"] == "app_open" and r["event_id"] != trigger_id]
    last_seen = events[-1]["first_received_at"] if events else None

    def refs(rows, n=3):
        return [f"event:{r['event_id']}" for r in rows[-n:]]

    add(Fact("has_played", bool(plays), "derived:events", last_seen, evidence=refs(plays)))
    add(Fact("has_completed_episode", bool(completes), "derived:events", last_seen, evidence=refs(completes)))
    last_open = max(opens, key=lambda r: r["event_time"]) if opens else None
    add(
        Fact(
            "last_app_open_at",
            last_open["event_time"] if last_open else None,
            "derived:events",
            last_seen,
            evidence=[f"event:{last_open['event_id']}"] if last_open else [],
        )
    )

    since = at - timedelta(days=14)
    recent_play_days = sorted(
        {
            ist_day(parse_ts(r["event_time"])).isoformat()
            for r in plays
            if since <= parse_ts(r["event_time"]) <= at
        }
    )
    add(Fact("play_ist_days_14d", recent_play_days, "derived:events", last_seen, evidence=refs(plays)))

    genres: dict[str, int] = {}
    series: dict[str, dict] = {}
    for r in plays + completes:
        title = catalogue.titles.get(props[r["event_id"]].get("title_id"))
        if title is None:
            continue
        if r["event"] == "play_started":
            for g in title.genres:
                genres[g] = genres.get(g, 0) + 1
        if title.content_type not in SERIES_TYPES:
            continue
        s = series.setdefault(
            title.slug,
            {"slug": title.slug, "last_play_at": None, "completed_episodes": set(), "last_event": None},
        )
        if s["last_play_at"] is None or r["event_time"] > s["last_play_at"]:
            s["last_play_at"], s["last_event"] = r["event_time"], f"event:{r['event_id']}"
        if r["event"] == "episode_completed":
            s["completed_episodes"].add(props[r["event_id"]].get("episode_id"))
    in_progress = []
    for s in series.values():
        total = catalogue.episode_count(s["slug"])
        done = len(s["completed_episodes"])
        if done < total:
            in_progress.append(
                {
                    "slug": s["slug"],
                    "last_play_at": s["last_play_at"],
                    "episodes_completed": done,
                    "episodes_total": total,
                    "evidence": s["last_event"],
                }
            )
    in_progress.sort(key=lambda s: s["last_play_at"], reverse=True)
    add(
        Fact(
            "series_in_progress",
            in_progress,
            "derived:events+episodes",
            last_seen,
            evidence=[s["evidence"] for s in in_progress[:3]],
        )
    )
    top = max(sorted(genres), key=lambda g: genres[g]) if genres else None
    add(Fact("top_genre", top, "derived:events+catalogue", last_seen, evidence=refs(plays)))
