"""Shared fixtures. Tests use small synthetic events, never copies of the data pack (rules.md Rule 2)."""

from __future__ import annotations

import json

import pytest

from ott_agent.config import Config
from ott_agent.store import connect


@pytest.fixture
def cfg() -> Config:
    return Config()


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    yield c
    c.close()


def make_event(
    event_id="e1",
    user_id="U-1",
    event="app_open",
    occurred="2026-09-10T10:00:00.000Z",
    sent=None,
    received="2026-09-10T10:00:01.000Z",
    **extra,
) -> dict:
    e = {
        "event_id": event_id,
        "user_id": user_id,
        "event": event,
        "occurred_at": occurred,
        "sent_at": sent or occurred,
        "received_at": received,
        "session_id": "s1",
        "app_dialect": "d1",
        "app_language": "hin",
        "platform": "android",
        "app_version": "6.1.4",
        "properties": {},
    }
    e.update(extra)
    return e


def write_jsonl(path, events) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.writelines((e if isinstance(e, str) else json.dumps(e)) + "\n" for e in events)


# Synthetic goal rules in the pack's goals.json format (same structure, written for tests).
GOALS = {
    "version": "test-v1",
    "goals": [
        {
            "goal_id": "G01",
            "name": "first_play_24h",
            "eligibility": {"signed_up_within_days": 7, "requires_no_prior_event": "play_started"},
            "success": {"event": "play_started", "condition": "any title", "window_hours": 24},
        },
        {
            "goal_id": "G02",
            "name": "first_full_episode_3d",
            "eligibility": {"requires_no_prior_event": "episode_completed"},
            "success": {"event": "episode_completed", "condition": "any episode", "window_hours": 72},
        },
        {
            "goal_id": "G03",
            "name": "second_day_return_2d",
            "eligibility": {"signed_up_within_days": 2},
            "success": {
                "event": "app_open",
                "condition": "on a later IST calendar day than the goal start",
                "window_hours": 48,
            },
        },
        {
            "goal_id": "G04",
            "name": "trial_to_paid_7d",
            "eligibility": {"subscription_status": ["trial_active"]},
            "success": {"event": "purchase_completed", "condition": "is_trial = false", "window_hours": 168},
        },
        {
            "goal_id": "G05",
            "name": "first_paid_7d",
            "eligibility": {"subscription_status": ["never"]},
            "success": {"event": "purchase_completed", "condition": "is_trial = false", "window_hours": 168},
        },
        {
            "goal_id": "G06",
            "name": "reactivate_lapsed_14d",
            "eligibility": {"subscription_status": ["lapsed"]},
            "success": {"event": "purchase_completed", "condition": "any paid plan", "window_hours": 336},
        },
        {
            "goal_id": "G07",
            "name": "resume_series_3d",
            "eligibility": {"text": "unfinished series"},
            "success": {"event": "play_started", "condition": "same content_slug", "window_hours": 72},
        },
        {
            "goal_id": "G08",
            "name": "try_new_release_7d",
            "eligibility": {"subscription_status": ["active"]},
            "success": {"event": "play_started", "condition": "recent release", "window_hours": 168},
        },
        {
            "goal_id": "G09",
            "name": "weekly_habit_14d",
            "eligibility": {"subscription_status": ["active"]},
            "success": {"event": "play_started", "condition": "on 3 distinct IST days", "window_hours": 336},
        },
        {
            "goal_id": "G10",
            "name": "meaningful_engagement_7d",
            "eligibility": {"text": "Any user."},
            "success": {"event": None, "condition": "engages meaningfully", "window_hours": 168},
        },
        {
            "goal_id": "G11",
            "name": "tv_activation_14d",
            "eligibility": {"subscription_status": ["active"]},
            "success": {"event": "tv_app_open", "condition": "any", "window_hours": 336},
        },
        {
            "goal_id": "G12",
            "name": "return_after_absence_7d",
            "eligibility": {"text": "no app_open 14 days"},
            "success": {"event": "play_started", "condition": "any title", "window_hours": 168},
        },
    ],
}


def write_goals(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(GOALS), encoding="utf-8")
