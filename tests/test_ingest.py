"""Trap 1 / D-004: deduplication, raw vs canonical layers, restart safety, and the quality report."""

from conftest import make_event, write_jsonl

from ott_agent.ingest import ALREADY_INGESTED, DUPLICATE, DUPLICATE_CONFLICT, NEW, ingest_event, ingest_file
from ott_agent.quality import build_report


def canonical(conn, event_id):
    return conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()


def test_duplicate_kept_once_with_earliest_received(conn, cfg):
    first = make_event(received="2026-09-10T10:00:01.000Z")
    again = make_event(received="2026-09-10T10:05:00.000Z")
    with conn:
        assert ingest_event(conn, first, "history", 1, cfg) == NEW
        assert ingest_event(conn, again, "history", 2, cfg) == DUPLICATE
    row = canonical(conn, "e1")
    assert row["delivery_count"] == 2
    assert row["first_received_at"] == "2026-09-10T10:00:01.000Z"
    assert row["last_received_at"] == "2026-09-10T10:05:00.000Z"
    assert row["payload_conflict"] == 0
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM event_deliveries").fetchone()[0] == 2  # audit keeps both


def test_out_of_order_copy_becomes_the_truth(conn, cfg):
    with conn:
        ingest_event(conn, make_event(received="2026-09-10T10:05:00.000Z"), "history", 1, cfg)
        ingest_event(conn, make_event(received="2026-09-10T10:00:01.000Z"), "history", 2, cfg)
    row = canonical(conn, "e1")
    assert row["first_received_at"] == "2026-09-10T10:00:01.000Z"
    assert row["event_time"] == "2026-09-10T10:00:01.000Z"


def test_payload_conflict_is_flagged_and_first_copy_kept(conn, cfg):
    first = make_event(app_dialect="d1")
    changed = make_event(app_dialect="d4", received="2026-09-10T10:02:00.000Z")
    with conn:
        ingest_event(conn, first, "history", 1, cfg)
        assert ingest_event(conn, changed, "history", 2, cfg) == DUPLICATE_CONFLICT
    row = canonical(conn, "e1")
    assert row["payload_conflict"] == 1
    assert row["app_dialect"] == "d1"


def test_reingesting_same_file_is_a_noop(tmp_path, conn, cfg):
    path = tmp_path / "events.jsonl"
    write_jsonl(
        path, [make_event("e1"), make_event("e2"), make_event("e1", received="2026-09-10T10:03:00.000Z")]
    )
    s1 = ingest_file(conn, path, "history", cfg)
    s2 = ingest_file(conn, path, "history", cfg)  # e.g. rerun after a crash
    assert s1.counts == {NEW: 2, DUPLICATE: 1}
    assert s2.counts == {ALREADY_INGESTED: 3}
    assert canonical(conn, "e1")["delivery_count"] == 2


def test_malformed_lines_are_counted_not_guessed(tmp_path, conn, cfg):
    path = tmp_path / "events.jsonl"
    write_jsonl(path, [make_event("e1"), "{not json", '{"event_id": "x"}'])
    stats = ingest_file(conn, path, "history", cfg)
    assert stats.counts == {NEW: 1}
    assert [m["line_no"] for m in stats.malformed_lines] == [2, 3]


def test_quality_report_counts(tmp_path, conn, cfg):
    path = tmp_path / "events.jsonl"
    write_jsonl(
        path,
        [
            make_event("e1"),
            make_event("e1", received="2026-09-10T10:10:01.000Z"),
            # skewed phone clock (+22 h) and a 2-day buffered event
            make_event("e2", occurred="2026-09-11T08:00:00.000Z", received="2026-09-10T10:00:00.000Z"),
            make_event(
                "e3",
                occurred="2026-09-08T10:00:00.000Z",
                sent="2026-09-10T10:00:00.000Z",
                received="2026-09-10T10:00:01.000Z",
            ),
        ],
    )
    stats = ingest_file(conn, path, "history", cfg)
    report = build_report(conn, cfg, [stats])
    dup = report["duplicates"]["history"]
    assert dup["duplicate_event_ids"] == 1 and dup["extra_deliveries"] == 1
    assert dup["gap_between_copies_s"]["max"] == 600
    clk = report["clocks"]["history"]
    assert clk["skewed_clock_users"] == 1 and clk["buffered_over_1day"] == 1


# --- review fixes -----------------------------------------------------------------------------------------------


def test_out_of_order_conflicting_copy_replaces_payload_too(conn, cfg):
    late = make_event(app_dialect="d4", received="2026-09-10T10:05:00.000Z")
    early = make_event(app_dialect="d1", received="2026-09-10T10:00:01.000Z")
    with conn:
        ingest_event(conn, late, "history", 1, cfg)
        assert ingest_event(conn, early, "history", 2, cfg) == DUPLICATE_CONFLICT
    row = canonical(conn, "e1")
    assert row["app_dialect"] == "d1"  # the earliest copy is the truth, payload included
    assert row["first_received_at"] == "2026-09-10T10:00:01.000Z"
    assert row["last_received_at"] == "2026-09-10T10:05:00.000Z"
    assert row["delivery_count"] == 2 and row["payload_conflict"] == 1


def test_bad_received_at_is_rejected_without_partial_state(tmp_path, conn, cfg):
    path = tmp_path / "events.jsonl"
    write_jsonl(path, [make_event("bad", received="not-a-time"), "[1, 2]", make_event("ok")])
    stats = ingest_file(conn, path, "history", cfg)
    assert stats.counts == {NEW: 1}
    assert [m["line_no"] for m in stats.malformed_lines] == [1, 2]
    assert (
        conn.execute("SELECT COUNT(*) FROM event_deliveries").fetchone()[0] == 1
    )  # nothing written for bad lines


def test_bad_phone_time_becomes_clock_anomaly(conn, cfg):
    with conn:
        assert ingest_event(conn, make_event(occurred="garbage"), "history", 1, cfg) == NEW
    row = canonical(conn, "e1")
    assert row["clock_anomaly"] == "missing_phone_time"
    assert row["event_time"] == row["first_received_at"]
