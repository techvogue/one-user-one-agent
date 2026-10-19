"""Intervention planner (Step 3): what to change for the viewer now, or why nothing (D-033, D-034).

The trigger is an app open, so the home page (seen within seconds) is the main lever. A reminder message only serves a
later visit and is used sparingly (G03, G12). Content is chosen from data: only titles that exist, are available for
the page's whole lifetime, are in the dialect the app will show, and are not flagged. No model is involved here.

Platform rules (TTL, row limits, row types, templates, copy limits, quiet hours) are read from
config/interventions.json, so a different file on another day is applied as written.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .catalogue import Catalogue, Title
from .config import Config
from .facts import ViewerFacts
from .timeutil import IST, UTC, iso, parse_ts

HOME_PAGE, NO_ACTION = "HOME_PAGE", "NO_ACTION"
NO_GOAL, STALE_TRIGGER, DIALECT_UNKNOWN = "NO_GOAL", "STALE_TRIGGER", "DIALECT_UNKNOWN"
PAGE_STILL_LIVE, INSUFFICIENT_CONTENT = "PAGE_STILL_LIVE", "INSUFFICIENT_CONTENT"
PLAYBACK_EVENTS = ("play_started", "play_progress", "episode_completed")


# --- platform rules --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlatformRules:
    page_ttl: timedelta
    max_rows: int
    min_titles_per_row: int
    max_titles_per_row: int
    row_types: tuple[str, ...]
    push_title_max: int
    push_body_max: int
    forbidden_phrases: tuple[str, ...]
    quiet_start_h: int  # IST hour quiet hours start (22)
    quiet_end_h: int  # IST hour quiet hours end (8)
    wa_templates: dict  # template_id -> {"variables": [...], "text": ...}

    @classmethod
    def from_file(cls, path: Path) -> PlatformRules:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        rows = d["home_page"]["rows"]
        push = d["push"]
        q_start, q_end = (int(x.split(":")[0]) for x in push["quiet_hours_ist"])
        return cls(
            page_ttl=timedelta(minutes=d["home_page"]["ttl_minutes"]),
            max_rows=rows["max_rows"],
            min_titles_per_row=rows["min_titles_per_row"],
            max_titles_per_row=rows["max_titles_per_row"],
            row_types=tuple(rows["row_types"]),
            push_title_max=push["copy"]["title_max_chars"],
            push_body_max=push["copy"]["body_max_chars"],
            forbidden_phrases=tuple(p.lower() for p in push["copy"]["forbidden_phrases"]),
            quiet_start_h=q_start,
            quiet_end_h=q_end,
            wa_templates={t["template_id"]: t for t in d["whatsapp"]["approved_templates"]},
        )

    def in_quiet_hours(self, at: datetime) -> bool:
        h = at.astimezone(IST).hour
        return h >= self.quiet_start_h or h < self.quiet_end_h


# --- content statistics ----------------------------------------------------------------------------------------


@dataclass
class Popularity:
    """Distinct viewers with playback per slug in the window before a moment, from events received by then."""

    window_days: int
    _cache: dict = field(default_factory=dict)

    def counts(self, conn: sqlite3.Connection, catalogue: Catalogue, at: datetime) -> dict[str, int]:
        key = at.astimezone(UTC).strftime("%Y-%m-%dT%H")  # recomputed at most hourly
        if key not in self._cache:
            since = iso(at - timedelta(days=self.window_days))
            rows = conn.execute(
                "SELECT user_id, json_extract(payload_json, '$.properties.title_id') AS tid FROM events"
                " WHERE event IN (?, ?, ?) AND first_received_at <= ? AND event_time >= ?",
                (*PLAYBACK_EVENTS, iso(at), since),
            )
            viewers: dict[str, set] = {}
            for r in rows:
                t = catalogue.titles.get(r["tid"])
                if t is not None:
                    viewers.setdefault(t.slug, set()).add(r["user_id"])
            self._cache[key] = {slug: len(users) for slug, users in viewers.items()}
        return self._cache[key]


# --- result ----------------------------------------------------------------------------------------------------


@dataclass
class Intervention:
    action: str  # HOME_PAGE | NO_ACTION
    reason: str  # why this action (or the no-action code + explanation)
    no_action_code: str | None = None
    page: dict | None = None  # {dialect, hero_title_id, rows: [{row_type, title_ids}], live_until}
    message: dict | None = None  # {channel, send_at, title_id, template_id/variables or text, deeplink}
    message_note: str | None = None  # why no message (when relevant)
    content: list[str] = field(default_factory=list)  # ["title:T0412", ...]
    evidence: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --- page building ---------------------------------------------------------------------------------------------

HERO_STYLE = {  # D-033 table
    "G01": "deeplink_or_popular",
    "G02": "popular_micro_drama",
    "G03": "popular",
    "G04": "popular_premium",
    "G05": "popular_premium",
    "G06": "popular_premium",
    "G07": "series",
    "G08": "newest_release",
    "G09": "popular_micro_drama",
    "G12": "popular",
}
ROW_PLAN = {
    "G01": ["popular_in_dialect", "new_releases", "genre"],
    "G02": ["popular_in_dialect:micro_drama", "because_you_watched", "new_releases"],
    "G03": ["popular_in_dialect", "new_releases", "genre"],
    "G04": ["popular_in_dialect:premium", "new_releases", "genre"],
    "G05": ["popular_in_dialect:premium", "new_releases", "genre"],
    "G06": ["popular_in_dialect:premium", "new_releases", "genre"],
    "G07": ["next_episode", "because_you_watched", "popular_in_dialect"],
    "G08": ["new_releases", "popular_in_dialect", "genre"],
    "G09": ["because_you_watched", "popular_in_dialect", "genre"],
    "G12": ["popular_in_dialect", "new_releases", "genre"],
}
DEFAULT_ROWS = ["popular_in_dialect", "new_releases", "genre"]


def _pool(vf, catalogue, dialect, at, ttl) -> dict[str, Title]:
    """slug -> the promotable variant in the page dialect, preferring the viewer's app language (D-010, D-011)."""
    lang = vf.value("app_language")
    finished = set(vf.value("finished_slugs"))
    pool: dict[str, Title] = {}
    for t in catalogue.titles.values():
        if t.dialect != dialect or t.slug in finished or not catalogue.is_promotable(t, at, ttl):
            continue
        cur = pool.get(t.slug)
        if (
            cur is None
            or (cur.language != lang and t.language == lang)
            or (cur.language == t.language and t.title_id < cur.title_id)
        ):
            pool[t.slug] = t
    return pool


def build_page(
    vf, goal, catalogue, popularity, at, rules: PlatformRules, cfg: Config
) -> tuple[dict | None, list, list]:
    """Returns (page or None, evidence, assumptions)."""
    dialect = vf.usable("page_dialect")
    pool = _pool(vf, catalogue, dialect, at, rules.page_ttl)
    pop = lambda slug: popularity.get(slug, 0)
    by_pop = sorted(pool, key=lambda s: (-pop(s), pool[s].title_id))
    gid = goal["goal_id"]
    evidence, assumptions = [], []
    used: set[str] = set()
    recent_cut = (at - timedelta(days=cfg.new_release_days)).date()

    def new_releases():
        rel = [s for s in pool if pool[s].release_date and recent_cut <= pool[s].release_date <= at.date()]
        return sorted(rel, key=lambda s: (pool[s].release_date, pop(s), pool[s].title_id), reverse=True)

    # Hero
    hero_slug, style = None, HERO_STYLE.get(gid, "popular")
    if style == "series":
        series = (goal.get("detail") or {}).get("series") or {}
        if series.get("slug") in pool:
            hero_slug = series["slug"]
        elif series:
            assumptions.append(
                f"series {series.get('slug')} is not available in the app's current dialect {dialect}"
            )
    elif style == "deeplink_or_popular":
        link = vf.usable("deeplink")
        t = catalogue.titles.get(link["title_id"]) if link else None
        if t is not None and t.dialect == dialect and t.slug in pool:
            hero_slug = t.slug
            evidence.append(f"deeplink:{link['campaign']}/{t.title_id}")
    elif style == "newest_release":
        nr = new_releases()
        hero_slug = nr[0] if nr else None
    if hero_slug is None:
        flt = {"popular_micro_drama": lambda s: pool[s].content_type == "micro_drama",
               "popular_premium": lambda s: pool[s].is_premium}.get(style, lambda s: True)  # fmt: skip
        hero_slug = next((s for s in by_pop if flt(s)), None) or (by_pop[0] if by_pop else None)
    if hero_slug:
        used.add(hero_slug)

    # Rows
    watched = vf.value("watched_slugs")
    seed = next((catalogue.pick_variant(s, None) for s in watched if catalogue.by_slug(s)), None)
    top_genre = vf.value("top_genre")

    def candidates(kind: str) -> list[str]:
        base, _, flt = kind.partition(":")
        if base == "popular_in_dialect":
            out = by_pop
            if flt == "micro_drama":
                out = [s for s in out if pool[s].content_type == "micro_drama"]
            elif flt == "premium":
                out = [s for s in out if pool[s].is_premium]
            return out
        if base == "new_releases":
            return new_releases()
        if base == "genre":
            return [s for s in by_pop if top_genre in pool[s].genres] if top_genre else []
        if base == "because_you_watched":
            if seed is None:
                return []
            g = set(seed.genres)
            rel = [s for s in by_pop if s not in watched and g & set(pool[s].genres)]
            return sorted(rel, key=lambda s: (-len(g & set(pool[s].genres)), -pop(s), pool[s].title_id))
        if base == "next_episode":
            return [x["slug"] for x in vf.value("series_in_progress") if x["slug"] in pool]
        return []

    rows = []
    for kind in ROW_PLAN.get(gid, DEFAULT_ROWS):
        row_type = kind.partition(":")[0]
        if row_type not in rules.row_types or len(rows) >= min(rules.max_rows, cfg.page_max_rows):
            continue
        picked = [s for s in candidates(kind) if s not in used][: rules.max_titles_per_row]
        if len(picked) < rules.min_titles_per_row:
            continue  # the platform drops rows with fewer than 3 titles; don't publish them
        used.update(picked)
        rows.append({"row_type": row_type, "title_ids": [pool[s].title_id for s in picked]})

    if hero_slug is None and not rows:
        return None, evidence, assumptions
    page = {
        "dialect": dialect,
        "hero_title_id": pool[hero_slug].title_id if hero_slug else None,
        "rows": rows,
        "live_until": iso(at + rules.page_ttl),
    }
    shown = ([hero_slug] if hero_slug else []) + [s for s in used if s != hero_slug]
    evidence += [
        f"popularity:{s}={pop(s)} viewers/{cfg.popularity_days}d" for s in ([hero_slug] if hero_slug else [])
    ]
    evidence += [f"catalogue:{pool[s].title_id}" for s in sorted(shown)]
    if seed is not None and any(r["row_type"] == "because_you_watched" for r in rows):
        evidence.append(f"because_you_watched:seed={seed.slug}")
    return page, evidence, assumptions


# --- reminder message ------------------------------------------------------------------------------------------


def next_send_time(at: datetime, cfg: Config) -> datetime:
    """19:00 IST on the next IST calendar day (D-034)."""
    day = at.astimezone(IST).date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, cfg.message_hour_ist, tzinfo=IST).astimezone(UTC)


def _copy_ok(text: str, rules: PlatformRules) -> bool:
    low = text.lower()
    return not any(p in low for p in rules.forbidden_phrases) and "₹" not in text and "rs." not in low


PUSH_COPY = {  # fixed, rule-based copy (D-034): no prices, offers, deadlines or invented facts
    "hin": ("Aapke liye chuna gaya", "{title} ab dekhiye."),
    "en": ("Picked for you", "Watch {title} now."),
}


def plan_message(vf, goal, catalogue, popularity, at, rules: PlatformRules, cfg: Config,
                 page_hero: str | None, messages_on_day) -> tuple[dict | None, str | None, list]:  # fmt: skip
    """A reminder for G03/G12 only, or (None, reason). messages_on_day(day) -> count already planned/sent that IST day."""
    if goal["goal_id"] not in cfg.message_goals:
        return (
            None,
            f"no reminder for {goal['goal_id']} (reminders only for {', '.join(cfg.message_goals)}, D-034)",
            [],
        )
    send_at = next_send_time(at, cfg)
    if send_at + timedelta(hours=1) > parse_ts(goal["deadline"]):
        return None, "goal ends before the next reminder slot", []
    if rules.in_quiet_hours(send_at):
        return None, "reminder slot falls in quiet hours", []
    if messages_on_day(send_at.astimezone(IST).date()) >= 1:
        return None, "frequency cap: a push/WhatsApp is already planned for that IST day", []
    if vf.usable("whatsapp_consent") is True:
        channel = "whatsapp"
    elif vf.usable("push_consent") is True:
        channel = "push"
    else:
        return None, "no usable consent for push or WhatsApp", []
    dialect = vf.usable("message_dialect")
    if dialect is None:
        return None, "no valid dialect known for the message", []
    # the page hero if it suits the message dialect and is still available at send time, else the most popular title
    hero = catalogue.titles.get(page_hero) if page_hero else None
    pool = _pool(vf, catalogue, dialect, send_at, timedelta(0))
    if hero is not None and hero.slug in pool:
        title = pool[hero.slug]
    else:
        ranked = sorted(pool, key=lambda s: (-popularity.get(s, 0), pool[s].title_id))
        title = pool[ranked[0]] if ranked else None
    if title is None:
        return None, f"no available title in dialect {dialect} at send time", []
    msg = {"channel": channel, "send_at": iso(send_at), "title_id": title.title_id,
           "deeplink": f"ott://title/{title.title_id}", "dialect": dialect}  # fmt: skip
    if channel == "whatsapp":
        tpl = rules.wa_templates.get("wa_recommend_title_v1")
        if tpl is None:
            return None, "approved WhatsApp template wa_recommend_title_v1 not available", []
        msg.update(
            template_id=tpl["template_id"],
            variables={"title_name": title.name, "dialect_name": title.dialect},
        )
    else:
        t_text, b_text = PUSH_COPY.get(vf.value("app_language"), PUSH_COPY["hin"])
        body = b_text.format(title=title.name)
        if (
            len(t_text) > rules.push_title_max
            or len(body) > rules.push_body_max
            or not _copy_ok(t_text + body, rules)
        ):
            return None, "push copy would break the copy rules", []
        msg["text"] = {"title": t_text, "body": body}
    assumptions = []
    if channel == "whatsapp":
        assumptions.append(
            "dialect_name filled with the catalogue's dialect code (the pack has no dialect names)"
        )
    return msg, None, assumptions


# --- decision --------------------------------------------------------------------------------------------------


def decide_intervention(
    vf: ViewerFacts,
    goal: dict | None,
    goal_outcome: str,
    catalogue: Catalogue,
    popularity: dict[str, int],
    at: datetime,
    rules: PlatformRules,
    cfg: Config,
    live_page: dict | None = None,
    messages_on_day=lambda day: 0,
) -> Intervention:
    """Decide the intervention for this trigger. live_page: the viewer's latest published page
    ({goal_instance_id, dialect, live_until}) if any; messages_on_day: planned/sent messages count per IST day."""
    # Not a live visit comes first: whatever else is true, nothing can be shown to a session that has ended (D-006).
    page_dialect = vf["page_dialect"]
    if (
        vf.trigger_age_s is not None and vf.trigger_age_s > cfg.stale_trigger_s
    ) or page_dialect.freshness == "stale":
        return Intervention(
            NO_ACTION,
            f"the app open is not a live visit: it happened {vf.trigger_age_s} s ago (offline-buffered or late)",
            STALE_TRIGGER,
        )
    if goal is None:
        return Intervention(NO_ACTION, "no eligible measurable goal", NO_GOAL)
    if page_dialect.value is None:
        return Intervention(
            NO_ACTION, page_dialect.assumption or "app dialect missing or invalid", DIALECT_UNKNOWN
        )
    if (
        live_page
        and goal_outcome == "KEEP"
        and live_page.get("dialect") == page_dialect.value
        and parse_ts(live_page["live_until"]) > at
    ):
        return Intervention(
            NO_ACTION, f"page for this goal is live until {live_page['live_until']}", PAGE_STILL_LIVE
        )

    page, evidence, assumptions = build_page(vf, goal, catalogue, popularity, at, rules, cfg)
    if page is None:
        return Intervention(
            NO_ACTION,
            f"not enough available titles in dialect {page_dialect.value} for a hero or a row of "
            f"{rules.min_titles_per_row}",
            INSUFFICIENT_CONTENT,
            evidence=evidence,
            assumptions=assumptions,
        )
    content = ([f"title:{page['hero_title_id']}"] if page["hero_title_id"] else []) + [
        f"title:{t}" for r in page["rows"] for t in r["title_ids"]
    ]
    msg, note, m_assumptions = plan_message(vf, goal, catalogue, popularity, at, rules, cfg,
                                            page["hero_title_id"], messages_on_day)  # fmt: skip
    reason = f"home page for {goal['goal_id']}: hero + {len(page['rows'])} row(s) in {page['dialect']}"
    if msg:
        reason += f"; {msg['channel']} reminder at {msg['send_at']}"
        evidence.append(f"catalogue:{msg['title_id']}")
    return Intervention(
        HOME_PAGE, reason, None, page, msg, note, content, evidence, assumptions + m_assumptions
    )


def build_generic_page(
    catalogue: Catalogue,
    popularity: dict[str, int],
    dialect: str,
    language: str | None,
    at: datetime,
    rules: PlatformRules,
) -> dict | None:
    """Fallback page when the viewer's state cannot be read (D-036): popular titles in the app's dialect only.
    Uses nothing about the viewer except what the app open itself says (dialect, language)."""
    pool: dict[str, Title] = {}
    for t in catalogue.titles.values():
        if t.dialect != dialect or not catalogue.is_promotable(t, at, rules.page_ttl):
            continue
        cur = pool.get(t.slug)
        if cur is None or (cur.language != language and t.language == language):
            pool[t.slug] = t
    ranked = sorted(pool, key=lambda s: (-popularity.get(s, 0), pool[s].title_id))
    if not ranked:
        return None
    hero, rest = ranked[0], ranked[1 : 1 + rules.max_titles_per_row]
    rows = [{"row_type": "popular_in_dialect", "title_ids": [pool[s].title_id for s in rest]}]
    if len(rest) < rules.min_titles_per_row:
        rows = []
    return {
        "dialect": dialect,
        "hero_title_id": pool[hero].title_id,
        "rows": rows,
        "live_until": iso(at + rules.page_ttl),
    }
