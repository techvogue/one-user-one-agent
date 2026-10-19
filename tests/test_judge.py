"""Step 6 "Close the loop": verdicts from attributable signals only, rule versions, stages, exposure and learning
notes stored in the viewer's state (D-042, D-043)."""

import json

import pytest
from conftest import make_event
from test_intervention import catalogue_rows
from test_profiles_catalogue import CAT_COLS, write_csv

from ott_agent.catalogue import load_catalogue
from ott_agent.config import Config
from ott_agent.ingest import ingest_event
from ott_agent.judge import FAILED, INCONCLUSIVE, SUCCEEDED, judge_all, update_viewer_state
from ott_agent.timeutil import parse_ts

START = "2026-10-01T09:00:00.000Z"  # 14:30 IST


@pytest.fixture
def cat(tmp_path):
    write_csv(tmp_path / "catalogue.csv", CAT_COLS, catalogue_rows())
    eps = [{"episode_id": f"{r['content_slug']}-s1e{i}", "content_slug": r["content_slug"], "season": 1, "episode": i,
            "minutes": 3} for r in catalogue_rows() for i in (1, 2, 3)]  # fmt: skip
    write_csv(tmp_path / "episodes.csv", ["episode_id", "content_slug", "season", "episode", "minutes"], eps)
    return load_catalogue(tmp_path / "catalogue.csv", tmp_path / "episodes.csv")


SUCCESS = {
    "G01": {"event": "play_started", "condition": "any title", "window_hours": 24},
    "G02": {"event": "episode_completed", "condition": "any episode", "window_hours": 72},
    "G03": {"event": "app_open", "condition": "on a later IST calendar day than the goal start", "window_hours": 48},
    "G05": {"event": "purchase_completed", "condition": "is_trial = false", "window_hours": 168},
    "G07": {"event": "play_started", "condition": "same content_slug as the unfinished series", "window_hours": 72},
    "G08": {"event": "play_started", "condition": "a title released in the 30 days before the goal start",
            "window_hours": 168},
    "G09": {"event": "play_started", "condition": "on 3 distinct IST days", "window_hours": 336},
    "G12": {"event": "play_started", "condition": "any title", "window_hours": 168},
}  # fmt: skip


def add_goal(
    conn, gid, user="U-1", start=START, shadow=0, status="open", close_reason=None, closed_at=None, **detail
):
    hours = SUCCESS[gid]["window_hours"]
    deadline = parse_ts(start).timestamp() + hours * 3600
    from datetime import datetime, timezone

    dl = datetime.fromtimestamp(deadline, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    n = conn.execute("SELECT COUNT(*) FROM goal_instances").fetchone()[0] + 1
    goal = {"goal_id": gid, "name": gid.lower(), "success": SUCCESS[gid], "detail": detail}
    conn.execute(
        "INSERT INTO goal_instances (goal_instance_id, user_id, goal_id, goal_name, rule_version, status, close_reason,"
        " start_at, deadline, closed_at, shadow, goal_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"GI-{n}", user, gid, gid.lower(), f"{gid}@v1", status, close_reason, start, dl, closed_at, shadow,
         json.dumps(goal)),
    )  # fmt: skip
    return f"GI-{n}"


def ev(conn, eid, event, when, user="U-1", received=None, sent=None, **props):
    e = make_event(
        eid, user_id=user, event=event, occurred=when, sent=sent, received=received or when, properties=props
    )
    n = conn.execute("SELECT COUNT(*) FROM event_deliveries").fetchone()[0] + 1
    with conn:
        ingest_event(conn, e, "outcome", n, Config())


def judge(conn, cat, at="2026-10-20T00:00:00.000Z", actions=frozenset(), arms=None):
    with conn:
        return {o.goal_instance_id: o for o in judge_all(conn, cat, Config(), parse_ts(at), arms or {"U-1": "agent"},
                                                           set(actions))}  # fmt: skip


def test_success_and_failure_from_signals_in_the_window(conn, cat):
    g1 = add_goal(conn, "G01")
    g2 = add_goal(conn, "G02", user="U-2")
    ev(conn, "p", "play_started", "2026-10-01T15:00:00.000Z", title_id="F2")
    out = judge(conn, cat, arms={"U-1": "agent", "U-2": "agent"})
    assert out[g1].verdict == SUCCEEDED and out[g1].success_events[0]["event_id"] == "p"
    assert out[g2].verdict == FAILED and out[g2].stage == "final"


def test_signals_outside_the_window_do_not_count(conn, cat):
    g = add_goal(conn, "G01")
    ev(conn, "before", "play_started", "2026-10-01T08:59:00.000Z", title_id="F2")  # before the goal started
    ev(conn, "after", "play_started", "2026-10-02T09:01:00.000Z", title_id="F2")  # after the 24 h deadline
    assert judge(conn, cat)[g].verdict == FAILED


def test_buffered_signal_counts_by_when_it_happened(conn, cat):
    g = add_goal(conn, "G01")
    # happened inside the window, held on the phone, arrived two days later
    ev(conn, "buf", "play_started", "2026-10-01T12:00:00.000Z", sent="2026-10-03T12:00:00.000Z",
       received="2026-10-03T12:00:01.000Z", title_id="F2")  # fmt: skip
    assert judge(conn, cat)[g].verdict == SUCCEEDED


def test_signal_not_yet_received_is_not_known(conn, cat):
    g = add_goal(conn, "G01")
    ev(conn, "late", "play_started", "2026-10-01T12:00:00.000Z", sent="2026-10-05T12:00:00.000Z",
       received="2026-10-05T12:00:01.000Z", title_id="F2")  # fmt: skip
    assert judge(conn, cat, at="2026-10-04T00:00:00.000Z")[g].verdict == FAILED
    assert judge(conn, cat, at="2026-10-06T00:00:00.000Z")[g].verdict == SUCCEEDED  # new events: re-judged


def test_goal_specific_conditions(conn, cat):
    users = {"G05": "U-5", "G07": "U-7", "G08": "U-8", "G09": "U-9"}
    g5 = add_goal(conn, "G05", user="U-5")
    g7 = add_goal(conn, "G07", user="U-7", series={"slug": "show-1"})
    g8 = add_goal(conn, "G08", user="U-8")
    g9 = add_goal(conn, "G09", user="U-9")
    ev(
        conn, "trial", "purchase_completed", "2026-10-02T10:00:00.000Z", user="U-5", is_trial=True
    )  # trial: no
    ev(
        conn, "other", "play_started", "2026-10-02T10:00:00.000Z", user="U-7", title_id="S2"
    )  # other series: no
    ev(
        conn, "variant", "play_started", "2026-10-02T11:00:00.000Z", user="U-7", title_id="S1E"
    )  # English variant: yes
    ev(conn, "old", "play_started", "2026-10-02T10:00:00.000Z", user="U-8", title_id="M1")  # released Jan: no
    for i, d in enumerate(("02", "02", "05")):  # only 2 distinct IST days
        ev(conn, f"d{i}", "play_started", f"2026-10-{d}T10:00:00.000Z", user="U-9", title_id="M1")
    out = judge(conn, cat, at="2026-10-30T00:00:00.000Z", arms=dict.fromkeys(users.values(), "agent"))
    assert out[g5].verdict == FAILED and out[g8].verdict == FAILED and out[g9].verdict == FAILED
    assert out[g7].verdict == SUCCEEDED and out[g7].success_events[0]["event_id"] == "variant"
    ev(
        conn, "new", "play_started", "2026-10-03T10:00:00.000Z", user="U-8", title_id="F5"
    )  # released 15 Sep: yes
    ev(conn, "d3", "play_started", "2026-10-07T10:00:00.000Z", user="U-9", title_id="M1")  # third IST day
    out = judge(conn, cat, at="2026-10-30T00:00:00.000Z", arms=dict.fromkeys(users.values(), "agent"))
    assert out[g8].verdict == SUCCEEDED and out[g9].verdict == SUCCEEDED


def test_g03_needs_a_later_ist_day(conn, cat):
    g = add_goal(conn, "G03")  # starts 14:30 IST on 1 Oct
    ev(conn, "same-day", "app_open", "2026-10-01T15:00:00.000Z")  # 20:30 IST, same IST day: does not count
    assert judge(conn, cat)[g].verdict == FAILED
    ev(conn, "next-day", "app_open", "2026-10-01T19:00:00.000Z")  # 00:30 IST on 2 Oct: a later IST day
    assert judge(conn, cat)[g].verdict == SUCCEEDED


def test_corrected_goal_rule_is_used_for_judging(conn, cat):
    """Required test: an open G03 goal corrected mid-replay is judged by the corrected rule (D-029, D-042)."""
    from ott_agent.goals import GoalCatalogue
    from ott_agent.planner import apply_rule_correction

    g = add_goal(conn, "G03")
    other = add_goal(conn, "G01", user="U-2")
    ev(conn, "open", "app_open", "2026-10-02T10:00:00.000Z")  # later IST day, but only an app open
    assert judge(conn, cat)[g].verdict == SUCCEEDED  # old rule: an app open on a later day counts

    goals = GoalCatalogue({}, [])
    from ott_agent.goals import GoalRule

    for gid in ("G03", "G01"):
        goals.rules[gid] = GoalRule(gid, gid, "", {}, SUCCESS[gid], f"{gid}@v1", True)
    with conn:
        _, affected = apply_rule_correction(conn, goals, "G03", {"event": "play_started",
                                            "condition": "on a later IST calendar day within the window"},
                                            parse_ts("2026-10-01T10:00:00.000Z"))  # fmt: skip
    assert affected == [g]  # only the open G03 goal, not the G01 goal
    out = judge(conn, cat, arms={"U-1": "agent", "U-2": "agent"})
    assert out[g].verdict == FAILED and out[g].rule_version.startswith("G03@2026-10-01")
    assert out[other].rule_version == "G01@v1"
    ev(conn, "play", "play_started", "2026-10-02T11:00:00.000Z", title_id="F2")
    assert judge(conn, cat, arms={"U-1": "agent", "U-2": "agent"})[g].verdict == SUCCEEDED


def test_closed_early_is_inconclusive_unless_it_succeeded_first(conn, cat):
    withdrawn = add_goal(conn, "G07", status="closed", close_reason="situation_changed: series show-1 is no longer "
                         "available", closed_at="2026-10-01T12:00:00.000Z", series={"slug": "show-1"})  # fmt: skip
    paid = add_goal(conn, "G05", user="U-2", status="closed", close_reason="situation_changed: subscription status is "
                    "now active (event:buy)", closed_at="2026-10-02T12:00:00.000Z")  # fmt: skip
    ev(
        conn, "late-play", "play_started", "2026-10-02T10:00:00.000Z", title_id="S1"
    )  # after the goal was closed
    ev(conn, "buy", "purchase_completed", "2026-10-02T11:00:00.000Z", user="U-2", is_trial=False)
    out = judge(conn, cat, arms={"U-1": "agent", "U-2": "agent"})
    assert out[withdrawn].verdict == INCONCLUSIVE and "closed early" in out[withdrawn].reason
    assert out[paid].verdict == SUCCEEDED


def test_unknown_outcome_when_data_ends_before_the_deadline(conn, cat):
    g = add_goal(conn, "G09")  # 14-day window
    o = judge(conn, cat, at="2026-10-14T00:00:00.000Z")[g]
    assert o.verdict == INCONCLUSIVE and o.stage == "open" and "observed data ends" in o.reason


def test_stages(conn, cat):
    g = add_goal(conn, "G01")  # deadline 2 Oct 09:00
    assert judge(conn, cat, at="2026-10-03T10:00:00.000Z")[g].stage == "provisional"
    assert judge(conn, cat, at="2026-10-09T10:00:00.000Z")[g].stage == "final"


def test_exposure_counts_only_our_actions_and_learning_is_saved(conn, cat, tmp_path):
    g = add_goal(conn, "G12")
    h = add_goal(conn, "G12", user="U-2", shadow=1)
    ev(conn, "shown", "home_page_shown", "2026-10-01T09:00:10.000Z", personalised=True, page_action_id="A-1")
    ev(conn, "other", "home_page_shown", "2026-10-01T09:05:00.000Z", personalised=True, page_action_id="X-9")
    ev(conn, "p", "play_started", "2026-10-02T09:00:00.000Z", title_id="F2")
    out = judge(conn, cat, actions={"A-1"}, arms={"U-1": "agent", "U-2": "holdout"})
    o = out[g]
    assert o.exposure["pages_shown"] == 1 and o.arm == "agent"
    assert o.learned == "G12 succeeded 1.0 days after the goal started; our page was shown"
    assert out[h].arm == "holdout" and "holdout: nothing was shown" in out[h].learned
    row = conn.execute(
        "SELECT verdict, outcome_json FROM goal_outcomes WHERE goal_instance_id = ?", (g,)
    ).fetchone()
    assert row["verdict"] == SUCCEEDED and json.loads(row["outcome_json"])["learned"] == o.learned
    assert conn.execute("SELECT COUNT(*) FROM goal_log WHERE change = 'judged'").fetchone()[0] == 2
    (tmp_path / "viewer_state").mkdir()
    (tmp_path / "viewer_state" / "U-1.json").write_text(json.dumps({"user_id": "U-1", "decisions": []}))
    update_viewer_state(tmp_path, list(out.values()))
    state = json.loads((tmp_path / "viewer_state" / "U-1.json").read_text())
    assert state["learned"] == [o.learned] and state["decisions"] == []  # existing state kept
