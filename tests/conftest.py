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
