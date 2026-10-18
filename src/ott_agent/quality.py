"""Data quality report: makes the handling of Traps 1 and 2 visible to evaluators (D-004, D-005).

Computed from the store on every run, so it reports the numbers of whatever pack/replay day is loaded.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from pathlib import Path

from .config import Config
from .timeutil import parse_ts

EXAMPLES = 5


def _percentile(sorted_values: list[float], p: float) -> float | None:
    if not sorted_values:
        return None
    k = min(len(sorted_values) - 1, int(len(sorted_values) * p / 100))
    return sorted_values[k]


def duplicates_section(conn: sqlite3.Connection, source: str) -> dict:
    lines = conn.execute("SELECT COUNT(*) FROM event_deliveries WHERE source = ?", (source,)).fetchone()[0]
    unique = conn.execute("SELECT COUNT(*) FROM events WHERE source = ?", (source,)).fetchone()[0]
    rows = conn.execute(
        "SELECT event_id, event, first_received_at, last_received_at, delivery_count, payload_conflict"
        " FROM events WHERE source = ? AND delivery_count > 1 ORDER BY first_received_at",
        (source,),
    ).fetchall()
    gaps = sorted(
        (parse_ts(r["last_received_at"]) - parse_ts(r["first_received_at"])).total_seconds() for r in rows
    )
    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r["event"]] = by_type.get(r["event"], 0) + 1
    return {
        "delivered_lines": lines,
        "unique_events": unique,
        "duplicate_event_ids": len(rows),
        "extra_deliveries": sum(r["delivery_count"] - 1 for r in rows),
        "payload_conflicts": sum(r["payload_conflict"] for r in rows),
        "gap_between_copies_s": {
            "min": gaps[0] if gaps else None,
            "median": statistics.median(gaps) if gaps else None,
            "max": gaps[-1] if gaps else None,
        },
        "duplicates_by_event_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "example_event_ids": [r["event_id"] for r in rows[:EXAMPLES]],
        "handling": "first-received copy kept; later copies counted (D-004)",
    }


def clocks_section(conn: sqlite3.Connection, source: str, cfg: Config) -> dict:
    rows = conn.execute(
        "SELECT user_id, buffer_delay_s, clock_skew_s, clock_anomaly FROM events WHERE source = ?", (source,)
    ).fetchall()
    skewed = [
        r for r in rows if r["clock_skew_s"] is not None and abs(r["clock_skew_s"]) > cfg.clock_skew_report_s
    ]
    buffers = sorted(
        r["buffer_delay_s"] for r in rows if r["buffer_delay_s"] is not None and r["clock_anomaly"] is None
    )
    anomalies: dict[str, int] = {}
    for r in rows:
        if r["clock_anomaly"]:
            anomalies[r["clock_anomaly"]] = anomalies.get(r["clock_anomaly"], 0) + 1
    n = len(buffers)

    def share_within(hours: float) -> float | None:
        return round(100 * sum(b <= hours * 3600 for b in buffers) / n, 2) if n else None

    def hours(v: float | None) -> float | None:
        return round(v / 3600, 2) if v is not None else None

    return {
        "events": len(rows),
        "skewed_clock_events": len(skewed),
        "skewed_clock_users": len({r["user_id"] for r in skewed}),
        "max_abs_clock_skew_h": hours(max((abs(r["clock_skew_s"]) for r in skewed), default=None)),
        "buffered_over_10min": sum(b > 600 for b in buffers),
        "buffered_over_1day": sum(b > 86400 for b in buffers),
        "buffer_delay_h_percentiles": {f"p{p}": hours(_percentile(buffers, p)) for p in (50, 90, 95, 99)}
        | {"max": hours(buffers[-1] if buffers else None)},
        "arrival_share_pct": {
            "within_24h": share_within(24),
            "within_72h": share_within(72),
            "within_7d": share_within(168),
        },
        "clock_anomalies": anomalies,
        "handling": "event_time = received_at - (sent_at - occurred_at); anomalies fall back to received_at (D-005)",
    }


def build_report(conn: sqlite3.Connection, cfg: Config, ingest_stats: list) -> dict:
    sources = [r[0] for r in conn.execute("SELECT DISTINCT source FROM events ORDER BY source")]
    return {
        "config": cfg.to_dict(),
        "ingest": [
            {
                "source": s.source,
                "lines": s.lines,
                "status_counts": s.counts,
                "malformed_lines": s.malformed_lines,
            }
            for s in ingest_stats
        ],
        "duplicates": {s: duplicates_section(conn, s) for s in sources},
        "clocks": {s: clocks_section(conn, s, cfg) for s in sources},
    }


def write_report(report: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "data_quality_report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path
