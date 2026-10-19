"""SQLite state store (D-017). Source of truth for everything the agent knows.

Layers (D-004):
  event_deliveries  every line exactly as received, including duplicates (audit; never modified)
  events            canonical: one row per event_id
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_deliveries (
    source        TEXT    NOT NULL,   -- history | live | trigger
    line_no       INTEGER NOT NULL,   -- 1-based line in the source file
    event_id      TEXT,
    received_at   TEXT,
    deliver_at    TEXT,
    raw_json      TEXT    NOT NULL,
    PRIMARY KEY (source, line_no)     -- re-ingesting the same file is a no-op (restart safety)
);

CREATE TABLE IF NOT EXISTS events (
    event_id          TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    event             TEXT NOT NULL,
    source            TEXT NOT NULL,
    session_id        TEXT,
    app_dialect       TEXT,
    app_language      TEXT,
    first_received_at TEXT NOT NULL,  -- the copy we treat as the truth (D-004 choice 1)
    last_received_at  TEXT NOT NULL,
    delivery_count    INTEGER NOT NULL,
    payload_conflict  INTEGER NOT NULL DEFAULT 0,
    payload_hash      TEXT NOT NULL,
    occurred_at_raw   TEXT,
    sent_at_raw       TEXT,
    event_time        TEXT NOT NULL,  -- corrected (D-005)
    buffer_delay_s    REAL,
    clock_skew_s      REAL,
    clock_anomaly     TEXT,
    payload_json      TEXT NOT NULL
);

-- What the agent knew about a viewer at a moment (auditability). One row per resolution.
CREATE TABLE IF NOT EXISTS facts_snapshots (
    user_id           TEXT NOT NULL,
    at                TEXT NOT NULL,
    trigger_event_id  TEXT NOT NULL DEFAULT '',  -- '' when resolved without a trigger (e.g. inspect)
    facts_json        TEXT NOT NULL,
    PRIMARY KEY (user_id, at, trigger_event_id)
);

-- Goals owned by each viewer's agent (Step 2). At most one open goal per viewer, enforced by the database.
CREATE TABLE IF NOT EXISTS goal_instances (
    goal_instance_id  TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    goal_id           TEXT NOT NULL,
    goal_name         TEXT NOT NULL,
    rule_version      TEXT NOT NULL,   -- the success rule this goal is judged by (updated by corrections)
    status            TEXT NOT NULL,   -- open | closed
    close_reason      TEXT,
    start_at          TEXT NOT NULL,
    deadline          TEXT NOT NULL,
    closed_at         TEXT,
    shadow            INTEGER NOT NULL DEFAULT 0,  -- holdout: decided but never acted on (D-016)
    goal_json         TEXT NOT NULL    -- evidence, assumptions, success signal, detail
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_goal_per_user ON goal_instances (user_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_goals_open_by_goal ON goal_instances (goal_id, status);

-- Every change to a viewer's goal: opened, kept, closed, rule corrected (auditability).
CREATE TABLE IF NOT EXISTS goal_log (
    user_id           TEXT NOT NULL,
    at                TEXT NOT NULL,
    goal_instance_id  TEXT,
    change            TEXT NOT NULL,
    detail_json       TEXT NOT NULL
);

-- Past goal outcomes from outside the replay, e.g. the earlier pilot (D-051). Our own verdicts live in goal_outcomes.
CREATE TABLE IF NOT EXISTS past_outcomes (
    source      TEXT NOT NULL,   -- e.g. pilot_outcomes.csv
    ref         TEXT NOT NULL,   -- line in the source file
    user_id     TEXT NOT NULL,
    goal_id     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    deadline    TEXT NOT NULL,   -- start + the goal's success window; the result is known from then on
    verdict     TEXT NOT NULL,   -- SUCCEEDED | FAILED
    PRIMARY KEY (source, ref)
);

CREATE INDEX IF NOT EXISTS idx_events_user_time ON events (user_id, event_time);
CREATE INDEX IF NOT EXISTS idx_events_user_received ON events (user_id, first_received_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (event);
CREATE INDEX IF NOT EXISTS idx_past_outcomes_user ON past_outcomes (user_id);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open (and create if needed) the state database."""
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn
