"""Trap 2 / D-005: corrected event time and IST days."""

from datetime import date

from ott_agent.timeutil import compute_event_time, iso, ist_day, parse_ts


def et(occurred, sent, received, max_days=30):
    return compute_event_time(parse_ts(occurred), parse_ts(sent), parse_ts(received), max_days)


def test_parse_ts_handles_z_and_any_fraction_on_py310():
    assert iso(parse_ts("2026-10-01T07:00:00Z")) == "2026-10-01T07:00:00.000Z"
    assert iso(parse_ts("2026-10-01T07:00:00.5Z")) == "2026-10-01T07:00:00.500Z"
    assert iso(parse_ts("2026-10-01T07:00:00.123456Z")) == "2026-10-01T07:00:00.123Z"
    assert parse_ts("") is None and parse_ts(None) is None


def test_phone_clock_22h_fast_lands_on_true_ist_day():
    # Really happened 2026-09-10 20:00 UTC (= 11 Sep 01:30 IST); the phone clock is 22 h fast.
    r = et("2026-09-11T18:00:00.000Z", "2026-09-11T18:00:00.000Z", "2026-09-10T20:00:01.000Z")
    assert r.anomaly is None
    assert iso(r.event_time) == "2026-09-10T20:00:01.000Z"
    assert ist_day(r.event_time) == date(2026, 9, 11)  # IST day from the true time
    assert ist_day(parse_ts("2026-09-11T18:00:00.000Z")) == date(2026, 9, 11)  # naive would agree here...
    # ...but for a skew that crosses midnight the naive day is wrong:
    r2 = et("2026-09-12T10:00:00.000Z", "2026-09-12T10:00:00.000Z", "2026-09-11T12:00:00.000Z")
    assert ist_day(r2.event_time) == date(2026, 9, 11)
    assert ist_day(parse_ts("2026-09-12T10:00:00.000Z")) == date(2026, 9, 12)
    assert round(r2.clock_skew_s / 3600) == 22


def test_buffered_event_keeps_its_real_time():
    # Happened 5 days before it reached the server; phone clock correct.
    r = et("2026-09-05T10:00:00.000Z", "2026-09-10T10:00:00.000Z", "2026-09-10T10:00:02.000Z")
    assert r.anomaly is None
    assert round(r.buffer_delay_s / 86400) == 5
    assert iso(r.event_time) == "2026-09-05T10:00:02.000Z"


def test_buffered_event_with_skewed_clock():
    # Held 2 h on a phone whose clock is 10 h slow; both errors handled together.
    r = et("2026-09-09T22:00:00.000Z", "2026-09-10T00:00:00.000Z", "2026-09-10T10:00:00.000Z")
    assert iso(r.event_time) == "2026-09-10T08:00:00.000Z"


def test_sent_before_occurred_falls_back_to_received():
    r = et("2026-09-10T10:00:00.000Z", "2026-09-10T09:00:00.000Z", "2026-09-10T10:00:05.000Z")
    assert r.anomaly == "sent_before_occurred"
    assert iso(r.event_time) == "2026-09-10T10:00:05.000Z"


def test_implausibly_old_and_missing_times_fall_back():
    old = et("2026-07-01T10:00:00.000Z", "2026-09-10T10:00:00.000Z", "2026-09-10T10:00:01.000Z")
    assert old.anomaly == "implausibly_old"
    missing = compute_event_time(None, None, parse_ts("2026-09-10T10:00:01.000Z"), 30)
    assert missing.anomaly == "missing_phone_time"
