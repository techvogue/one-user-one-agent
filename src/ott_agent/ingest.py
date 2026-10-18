"""One ingestion path for history, live events and triggers (D-004, D-005).

- Every delivery is logged in the raw layer.
- The canonical layer keeps one row per event_id. The earliest-received copy is the truth; later copies only update
  last_received_at and delivery_count. A later copy whose payload differs (beyond delivery fields) is flagged as a
  payload_conflict and never silently replaces the first.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .timeutil import compute_event_time, iso, parse_ts

log = logging.getLogger(__name__)

# Fields that legitimately differ between deliveries of the same event.
DELIVERY_FIELDS = frozenset({"received_at", "deliver_at"})

NEW = "new"
DUPLICATE = "duplicate"
DUPLICATE_CONFLICT = "duplicate_conflict"
ALREADY_INGESTED = "already_ingested"

REQUIRED_FIELDS = ("event_id", "user_id", "event", "received_at")


def payload_hash(event: dict) -> str:
    content = {k: v for k, v in event.items() if k not in DELIVERY_FIELDS}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode("utf-8")).hexdigest()


def ingest_event(conn: sqlite3.Connection, event: dict, source: str, line_no: int, cfg: Config) -> str:
    """Ingest one delivered event. Returns NEW, DUPLICATE, DUPLICATE_CONFLICT or ALREADY_INGESTED.

    The caller owns the transaction.
    """
    # Validate before touching the store, so a bad line leaves no partial state.
    missing = [k for k in REQUIRED_FIELDS if not event.get(k)]
    if missing:
        raise ValueError(f"missing {missing}")
    received = parse_ts(event["received_at"])  # the trusted clock: unparseable -> reject the line

    raw = json.dumps(event, sort_keys=True)
    cur = conn.execute(
        "INSERT OR IGNORE INTO event_deliveries (source, line_no, event_id, received_at, deliver_at, raw_json)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (source, line_no, event.get("event_id"), event.get("received_at"), event.get("deliver_at"), raw),
    )
    if cur.rowcount == 0:
        return ALREADY_INGESTED  # same (source, line) seen before: a rerun after a restart

    event_id = event["event_id"]
    h = payload_hash(event)
    row = conn.execute(
        "SELECT first_received_at, last_received_at, payload_hash, delivery_count, payload_conflict"
        " FROM events WHERE event_id = ?",
        (event_id,),
    ).fetchone()

    if row is None:
        _insert_canonical(conn, event, source, received, h, cfg)
        return NEW

    conflict = row["payload_hash"] != h
    received_iso = iso(received)
    if received_iso < row["first_received_at"]:
        # This copy actually arrived first (input out of order): it becomes the truth, payload included.
        conn.execute("DELETE FROM events WHERE event_id = ?", (event_id,))
        _insert_canonical(conn, event, source, received, h, cfg)
        conn.execute(
            "UPDATE events SET delivery_count = ?, last_received_at = ?, payload_conflict = ? WHERE event_id = ?",
            (
                row["delivery_count"] + 1,
                max(row["last_received_at"], received_iso),
                max(row["payload_conflict"], int(conflict)),
                event_id,
            ),
        )
        if conflict:
            log.warning(
                "payload conflict for event_id %s (source=%s line=%d); earliest copy kept",
                event_id,
                source,
                line_no,
            )
        return DUPLICATE_CONFLICT if conflict else DUPLICATE
    conn.execute(
        "UPDATE events SET delivery_count = delivery_count + 1,"
        " last_received_at = MAX(last_received_at, ?),"
        " payload_conflict = MAX(payload_conflict, ?) WHERE event_id = ?",
        (received_iso, int(conflict), event_id),
    )
    if conflict:
        log.warning(
            "payload conflict for event_id %s (source=%s line=%d); first copy kept", event_id, source, line_no
        )
        return DUPLICATE_CONFLICT
    return DUPLICATE


def _phone_ts(value):
    """Phone timestamps are untrusted: unparseable values become None (-> clock_anomaly), never a crash."""
    try:
        return parse_ts(value)
    except (TypeError, ValueError):
        return None


def _insert_canonical(conn, event, source, received, h, cfg):
    et = compute_event_time(
        _phone_ts(event.get("occurred_at")), _phone_ts(event.get("sent_at")), received, cfg.max_event_age_days
    )
    received_iso = iso(received)
    conn.execute(
        "INSERT INTO events (event_id, user_id, event, source, session_id, app_dialect, app_language,"
        " first_received_at, last_received_at, delivery_count, payload_conflict, payload_hash,"
        " occurred_at_raw, sent_at_raw, event_time, buffer_delay_s, clock_skew_s, clock_anomaly, payload_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event["event_id"],
            event["user_id"],
            event["event"],
            source,
            event.get("session_id"),
            event.get("app_dialect"),
            event.get("app_language"),
            received_iso,
            received_iso,
            h,
            event.get("occurred_at"),
            event.get("sent_at"),
            iso(et.event_time),
            et.buffer_delay_s,
            et.clock_skew_s,
            et.anomaly,
            json.dumps(event, sort_keys=True),
        ),
    )


@dataclass
class IngestStats:
    source: str
    lines: int = 0
    counts: dict = field(default_factory=dict)
    malformed_lines: list = field(default_factory=list)

    def add(self, status: str) -> None:
        self.counts[status] = self.counts.get(status, 0) + 1


def ingest_file(conn: sqlite3.Connection, path: Path, source: str, cfg: Config) -> IngestStats:
    """Ingest a JSONL file in one transaction. Malformed lines are counted and skipped, never guessed at."""
    stats = IngestStats(source=source)
    with conn, open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            stats.lines += 1
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise TypeError("not a JSON object")
                status = ingest_event(conn, event, source, line_no, cfg)  # validates before writing
            except (ValueError, TypeError) as exc:  # includes json.JSONDecodeError and bad received_at
                stats.malformed_lines.append({"line_no": line_no, "error": str(exc)})
                log.warning("skipping malformed line %d in %s: %s", line_no, path, exc)
                continue
            stats.add(status)
    return stats
