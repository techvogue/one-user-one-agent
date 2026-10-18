"""Time handling: parsing, IST calendar days and the corrected event time (D-005).

Every event carries three timestamps:
  occurred_at, sent_at  -- phone clock (can be wrong by hours)
  received_at           -- server clock (trusted)

event_time = received_at - (sent_at - occurred_at)

(sent_at - occurred_at) is how long the phone held the event; both values come from the same phone clock, so the
phone's clock error cancels out. What remains is network delay (seconds).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

_FRACTION = re.compile(r"\.(\d+)")


def parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime. Works on Python 3.10 (no 'Z' support there)."""
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Python 3.10 fromisoformat accepts only 3 or 6 fractional digits; normalise to 6.
    s = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso(dt: datetime) -> str:
    """Format as the pack does: 2026-10-01T07:00:00.000Z (sortable as text)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ist_day(dt: datetime) -> date:
    return dt.astimezone(IST).date()


@dataclass(frozen=True)
class EventTime:
    event_time: datetime
    buffer_delay_s: float | None  # sent_at - occurred_at: time held on the phone
    clock_skew_s: float | None  # sent_at - received_at: phone clock error (+ network delay)
    anomaly: str | None  # set when the phone times are unusable and event_time fell back to received_at


def compute_event_time(
    occurred_at: datetime | None,
    sent_at: datetime | None,
    received_at: datetime,
    max_event_age_days: int,
) -> EventTime:
    """Corrected event time with plausibility checks (D-005 choices 1 and 4)."""
    if occurred_at is None or sent_at is None:
        return EventTime(received_at, None, None, "missing_phone_time")

    buffer_s = (sent_at - occurred_at).total_seconds()
    skew_s = (sent_at - received_at).total_seconds()

    if buffer_s < 0:
        # The phone claims it sent the event before it happened.
        return EventTime(received_at, buffer_s, skew_s, "sent_before_occurred")

    event_time = received_at - timedelta(seconds=buffer_s)
    if received_at - event_time > timedelta(days=max_event_age_days):
        return EventTime(received_at, buffer_s, skew_s, "implausibly_old")

    return EventTime(event_time, buffer_s, skew_s, None)
