"""Past goal outcomes per viewer (D-051): the earlier pilot and the goals our judge has already decided.

A goal that failed for a viewer recently is not chosen for them again (facts.recent_failed_goals, goals.evaluate).
This records an observation about the viewer ("did not reach this goal last time"), never a cause.
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .timeutil import iso, parse_ts

PILOT_SOURCE = "pilot_outcomes.csv"


def load_pilot_outcomes(conn: sqlite3.Connection, path: Path, windows: dict[str, timedelta]) -> int:
    """Store the pilot's per-viewer goal results. Re-loading the same file is a no-op. Returns rows stored."""
    n = 0
    with open(path, encoding="utf-8", newline="") as f:
        for line_no, r in enumerate(csv.DictReader(f), start=2):
            window = windows.get(r.get("goal_id") or "")
            try:
                start = parse_ts(r.get("goal_started_at"))
            except ValueError:
                start = None  # unreadable row: skipped, never guessed
            if not r.get("user_id") or window is None or start is None:
                continue
            verdict = "SUCCEEDED" if (r.get("succeeded") or "").strip().lower() == "true" else "FAILED"
            conn.execute(
                "INSERT OR IGNORE INTO past_outcomes (source, ref, user_id, goal_id, started_at, deadline, verdict)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    PILOT_SOURCE,
                    str(line_no),
                    r["user_id"],
                    r["goal_id"],
                    iso(start),
                    iso(start + window),
                    verdict,
                ),
            )
            n += 1
    conn.commit()
    return n


def recent_failures(conn: sqlite3.Connection, user_id: str, at: datetime, cooldown_days: int) -> list[dict]:
    """Failed goals of this viewer known by `at` whose deadline is within the cooldown. Oldest first."""
    since = iso(at - timedelta(days=cooldown_days))
    out = [
        {"goal_id": r["goal_id"], "source": r["source"], "ref": f"{r['source']}:line {r['ref']}",
         "deadline": r["deadline"]}
        for r in conn.execute(
            "SELECT * FROM past_outcomes WHERE user_id = ? AND verdict = 'FAILED' AND deadline <= ? AND deadline >= ?",
            (user_id, iso(at), since),
        )
    ]  # fmt: skip
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'goal_outcomes'").fetchone():
        out += [
            {"goal_id": r["goal_id"], "source": "judge", "ref": f"goal_outcome:{r['goal_instance_id']}",
             "deadline": r["deadline"]}
            for r in conn.execute(
                "SELECT o.goal_id, o.goal_instance_id, g.deadline FROM goal_outcomes o"
                " JOIN goal_instances g ON g.goal_instance_id = o.goal_instance_id"
                " WHERE o.user_id = ? AND o.verdict = 'FAILED' AND o.judged_at <= ? AND g.deadline >= ?",
                (user_id, iso(at), since),
            )
        ]  # fmt: skip
    return sorted(out, key=lambda x: x["deadline"])
