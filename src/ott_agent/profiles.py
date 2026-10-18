"""Profile snapshot loading (Trap 4, D-007).

The snapshot is a baseline "as of updated_at", not the current truth. For users with several rows the row with the
newest updated_at wins and the other rows stay visible as conflicts. If two rows tie on updated_at, every field on
which they disagree is marked uncertain instead of picking one.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from .timeutil import iso, parse_ts

FIELDS = (
    "signup_at",
    "subscription_status",
    "plan_started_at",
    "plan_ends_at",
    "preferred_dialect",
    "preferred_language",
    "notifications_opt_in",
    "whatsapp_opt_in",
    "platform",
)
TIME_FIELDS = ("signup_at", "plan_started_at", "plan_ends_at", "updated_at", "snapshot_at")


def _normalise(row: dict) -> dict:
    """Timestamps to the canonical text form (iso()), so comparisons with event times are exact on any input
    format. Unparseable values become empty (unknown) rather than being compared as text."""
    out = {k: (v or "").strip() for k, v in row.items()}
    for k in TIME_FIELDS:
        if out.get(k):
            try:
                out[k] = iso(parse_ts(out[k]))
            except ValueError:
                out[k] = ""
    for k in ("notifications_opt_in", "whatsapp_opt_in"):
        out[k] = out.get(k, "").lower()
    return out


@dataclass
class Profile:
    user_id: str
    row: dict  # the winning row (raw strings)
    updated_at: str
    snapshot_at: str
    losing_rows: list[dict] = field(default_factory=list)
    uncertain_fields: set[str] = field(default_factory=set)

    def get(self, name: str) -> str | None:
        """Field value, or None when empty or uncertain (tie between rows)."""
        if name in self.uncertain_fields:
            return None
        v = self.row.get(name)
        return v if v not in ("", None) else None

    def conflicts(self, name: str) -> list[str]:
        """Values of `name` in losing rows that differ from the winner, e.g. ['profile_row@2026-09-22...:d2']."""
        out = []
        for r in self.losing_rows:
            if r.get(name) != self.row.get(name):
                out.append(f"profile_row@{r['updated_at']}:{r.get(name) or '∅'}")
        return out


def load_profiles(path: Path) -> dict[str, Profile]:
    rows_by_user: dict[str, list[dict]] = {}
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            r = _normalise(r)
            if not r.get("user_id") or not r.get("updated_at"):
                continue  # cannot be ordered or attributed: ignored rather than guessed
            rows_by_user.setdefault(r["user_id"], []).append(r)

    profiles = {}
    for user_id, rows in rows_by_user.items():
        rows.sort(key=lambda r: r["updated_at"], reverse=True)
        winner, losers = rows[0], rows[1:]
        uncertain = set()
        for r in losers:
            if r["updated_at"] == winner["updated_at"]:
                uncertain |= {k for k in FIELDS if r.get(k) != winner.get(k)}
        # Without a snapshot time the row can only be dated by its last change (older, so more conservative).
        snapshot_at = winner.get("snapshot_at") or winner["updated_at"]
        profiles[user_id] = Profile(user_id, winner, winner["updated_at"], snapshot_at, losers, uncertain)
    return profiles
