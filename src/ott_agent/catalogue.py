"""Content catalogue (Traps 7, 8, 9; D-010, D-011, D-012).

- Content identity is the content_slug; a slug can have several title_ids (language variants).
- A title is usable only if available at publish time AND for the whole page TTL; withdrawals override end times.
- Descriptions are kept for display only. They never reach decision logic or a model. A pattern scan flags
  instruction-like text for visibility, and flagged titles are quarantined (not promoted) pending human review.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .timeutil import parse_ts

log = logging.getLogger(__name__)

VALID_DIALECTS = frozenset({"d1", "d2", "d3", "d4", "d5", "d6"})

# Visibility only (D-012): the real defence is that free text never drives decisions.
INJECTION_PATTERN = re.compile(
    r"ignore (your|all|previous)|disregard|system (note|prompt)|\bassistant instruction|for ai agents?"
    r"|every user|all users|skip (all )?checks|pre-?approved|mark every|override (the )?rules",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Title:
    title_id: str
    slug: str
    name: str
    dialect: str
    language: str
    content_type: str  # film | show | micro_drama
    genres: tuple[str, ...]
    release_date: date | None
    available_from: datetime | None
    available_until: datetime | None
    is_premium: bool


@dataclass
class Catalogue:
    titles: dict[str, Title]
    episodes: dict[str, list[tuple[int, int, str]]]  # slug -> sorted (season, episode, episode_id)
    flagged: dict[str, str] = field(default_factory=dict)  # title_id -> reason (injection-like text)
    withdrawn: dict[str, datetime] = field(default_factory=dict)  # title_id -> withdrawal time

    def by_slug(self, slug: str) -> list[Title]:
        return [t for t in self.titles.values() if t.slug == slug]

    def end_time(self, t: Title) -> datetime | None:
        ends = [x for x in (t.available_until, self.withdrawn.get(t.title_id)) if x is not None]
        return min(ends) if ends else None

    def is_available(self, t: Title, at: datetime, for_duration: timedelta = timedelta(0)) -> bool:
        """Available at `at` and until at least `at + for_duration` (page TTL rule, D-011)."""
        if t.available_from is None or t.available_from > at:
            return False
        end = self.end_time(t)
        return end is None or end > at + for_duration

    def is_promotable(self, t: Title, at: datetime, for_duration: timedelta = timedelta(0)) -> bool:
        return t.title_id not in self.flagged and self.is_available(t, at, for_duration)

    def withdraw(self, title_id: str, at: datetime) -> None:
        self.withdrawn[title_id] = at

    def pick_variant(self, slug: str, language: str | None) -> Title | None:
        """The variant matching the viewer's app language, else the other one (D-010). Deterministic."""
        variants = sorted(self.by_slug(slug), key=lambda t: (t.language != language, t.title_id))
        return variants[0] if variants else None

    def episode_count(self, slug: str) -> int:
        return len(self.episodes.get(slug, []))


def load_catalogue(catalogue_csv: Path, episodes_csv: Path) -> Catalogue:
    titles: dict[str, Title] = {}
    flagged: dict[str, str] = {}
    with open(catalogue_csv, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            titles[r["title_id"]] = Title(
                title_id=r["title_id"],
                slug=r["content_slug"],
                name=r["title"],
                dialect=r["dialect"],
                language=r["language"],
                content_type=r["content_type"],
                genres=tuple(g for g in r["genres"].split("|") if g),
                release_date=date.fromisoformat(r["release_date"]) if r["release_date"] else None,
                available_from=parse_ts(r["available_from"]),
                available_until=parse_ts(r["available_until"]),
                is_premium=r["is_premium"].strip().lower() == "true",
            )
            for fld in ("description", "title"):
                m = INJECTION_PATTERN.search(r.get(fld) or "")
                if m:
                    flagged[r["title_id"]] = f"instruction-like text in {fld}: {m.group(0)!r}"
                    log.warning(
                        "quarantined %s: %s (data is not instruction)", r["title_id"], flagged[r["title_id"]]
                    )
    episodes: dict[str, list[tuple[int, int, str]]] = {}
    with open(episodes_csv, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            episodes.setdefault(r["content_slug"], []).append(
                (int(r["season"]), int(r["episode"]), r["episode_id"])
            )
    for eps in episodes.values():
        eps.sort()
    return Catalogue(titles, episodes, flagged)
