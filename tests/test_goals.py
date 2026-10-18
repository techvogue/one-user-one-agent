"""Step 2 "Choose a goal": measurability, eligibility from evidence, priority, NO_GOAL, one active goal, re-planning,
and rule corrections that touch only the affected open goals (D-013, D-014, D-019)."""

import json
import sqlite3

import pytest
from conftest import make_event, write_goals
from test_facts import AT, setup, trigger
from test_profiles_catalogue import profile_row

from ott_agent.facts import resolve
from ott_agent.goals import GoalCatalogue, check_measurable, evaluate
from ott_agent.planner import (
    KEEP,
    NEW,
    NO_GOAL,
    REPLAN,
    apply_rule_correction,
    commit_plan,
    plan_goal,
    select_goal,
)
from ott_agent.timeutil import parse_ts


@pytest.fixture
def goals(tmp_path):
    write_goals(tmp_path / "goals.json")
    return GoalCatalogue.from_file(tmp_path / "goals.json")


def facts(tmp_path, conn, cfg, events, rows, user="U-1", at=AT):
    profiles, cat = setup(tmp_path, conn, cfg, events, rows)
    return resolve(conn, profiles, cat, user, at, trigger(), cfg), cat


def play(eid, when, title="T1", user="U-1", event="play_started", episode="raloja-d1-s1e1"):
    return make_event(eid, user_id=user, event=event, occurred=when, received=when,
                      properties={"title_id": title, "episode_id": episode})  # fmt: skip


# --- measurability (D-013) --------------------------------------------------------------------------------------


def test_unmeasurable_goals_are_disabled_generically(goals):
    assert not goals.rules["G10"].measurable and "no success event" in goals.rules["G10"].unmeasurable_reason
    assert not goals.rules["G11"].measurable and "tv_app_open" in goals.rules["G11"].unmeasurable_reason
    assert {r.goal_id for r in goals.measurable()} == {f"G0{i}" for i in range(1, 10)} | {"G12"}
    assert check_measurable({"event": "smart_tv_ping", "condition": "any", "window_hours": 24})[0] is False
    assert check_measurable({"event": "play_started", "condition": "", "window_hours": 24})[0] is False


# --- eligibility from evidence (D-014, D-027) -------------------------------------------------------------------


def test_g01_new_user_who_never_played(tmp_path, conn, cfg, goals):
    vf, cat = facts(tmp_path, conn, cfg, [], [profile_row(signup_at="2026-09-28T10:00:00.000Z")])
    e = evaluate(goals.rules["G01"], vf, AT, cat, cfg)
    assert e.eligible and "goal_rule:G01@test-v1" in e.evidence and "fact:has_played=false" in e.evidence


def test_g01_not_eligible_after_a_play_or_without_provable_signup(tmp_path, conn, cfg, goals):
    vf, cat = facts(tmp_path, conn, cfg, [play("p1", "2026-09-29T10:00:00.000Z")],
                    [profile_row(signup_at="2026-09-28T10:00:00.000Z")])  # fmt: skip
    assert evaluate(goals.rules["G01"], vf, AT, cat, cfg).reason == "already has play_started"
    # no profile row and no signup event: first activity only bounds the signup, it does not prove it
    vf2, _ = facts(tmp_path, conn, cfg, [make_event("x", user_id="U-7")], [profile_row()], user="U-7")
    assert "only bounded by first activity" in evaluate(goals.rules["G01"], vf2, AT, cat, cfg).reason


def test_g03_signup_window(tmp_path, conn, cfg, goals):
    vf, cat = facts(tmp_path, conn, cfg, [], [profile_row(signup_at="2026-09-30T10:00:00.000Z")])
    assert evaluate(goals.rules["G03"], vf, AT, cat, cfg).eligible
    vf2, _ = facts(tmp_path, conn, cfg, [], [profile_row(signup_at="2026-09-27T10:00:00.000Z")])
    assert "signed up 4.0 days ago" in evaluate(goals.rules["G03"], vf2, AT, cat, cfg).reason


def test_status_goals_need_usable_status(tmp_path, conn, cfg, goals):
    vf, cat = facts(tmp_path, conn, cfg, [], [profile_row(status="trial_active")])
    assert evaluate(goals.rules["G04"], vf, AT, cat, cfg).eligible
    assert "status is trial_active" in evaluate(goals.rules["G05"], vf, AT, cat, cfg).reason
    # newest row "never", older row "lapsed" -> uncertain -> no status goal at all
    vf2, _ = facts(tmp_path, conn, cfg, [], [profile_row(status="lapsed", updated="2026-09-22T00:00:00.000Z"),
                                              profile_row(status="never")])  # fmt: skip
    for gid in ("G04", "G05", "G06"):
        assert "not usable" in evaluate(goals.rules[gid], vf2, AT, cat, cfg).reason


def test_g05_requires_never_paid(tmp_path, conn, cfg, goals):
    vf, cat = facts(tmp_path, conn, cfg, [], [profile_row(status="never")])
    e = evaluate(goals.rules["G05"], vf, AT, cat, cfg)
    assert e.eligible and any("A-G05-1" in a for a in e.assumptions)


def test_g06_recent_lapse_must_be_provable(tmp_path, conn, cfg, goals):
    lapse = make_event("l1", event="subscription_lapsed", occurred="2026-09-21T10:00:00.000Z",
                       received="2026-09-21T10:00:01.000Z")  # fmt: skip
    vf, cat = facts(
        tmp_path, conn, cfg, [lapse], [profile_row(status="lapsed", updated="2026-09-25T00:00:00.000Z")]
    )
    e = evaluate(goals.rules["G06"], vf, AT, cat, cfg)
    assert e.eligible and "event:l1" in e.evidence
    vf2, _ = facts(tmp_path, conn, cfg, [], [profile_row("U-2", status="lapsed")], user="U-2")
    assert "cannot be proven" in evaluate(goals.rules["G06"], vf2, AT, cat, cfg).reason
    old = make_event("l9", user_id="U-3", event="subscription_lapsed", occurred="2026-08-20T10:00:00.000Z",
                     received="2026-08-20T10:00:01.000Z")  # fmt: skip
    vf3, _ = facts(tmp_path, conn, cfg, [old], [profile_row("U-3", status="lapsed")], user="U-3")
    assert "lapsed 42.0 days ago" in evaluate(goals.rules["G06"], vf3, AT, cat, cfg).reason


def test_g07_unfinished_series_left_for_72h(tmp_path, conn, cfg, goals):
    vf, cat = facts(tmp_path, conn, cfg, [play("p1", "2026-09-27T10:00:00.000Z")], [profile_row()])
    e = evaluate(goals.rules["G07"], vf, AT, cat, cfg)
    assert e.eligible and e.detail["series"]["slug"] == "raloja-d1"
    vf2, _ = facts(tmp_path, conn, cfg, [play("p2", "2026-09-30T12:00:00.000Z", user="U-2")],
                   [profile_row("U-2")], user="U-2")  # fmt: skip
    assert not evaluate(goals.rules["G07"], vf2, AT, cat, cfg).eligible  # played 22 h ago
    cat.withdraw("T1", parse_ts("2026-10-01T07:00:00.000Z"))
    cat.withdraw("T2", parse_ts("2026-10-01T07:00:00.000Z"))
    assert not evaluate(goals.rules["G07"], vf, AT, cat, cfg).eligible  # series no longer available


def test_g07_series_touched_too_long_ago_is_not_in_progress(tmp_path, conn, cfg, goals):
    # watched 1 episode 25 days ago and never came back: tried and left, not "in progress" (D-031)
    vf, cat = facts(tmp_path, conn, cfg, [play("p1", "2026-09-06T10:00:00.000Z")], [profile_row()])
    e = evaluate(goals.rules["G07"], vf, AT, cat, cfg)
    assert not e.eligible and "between 72 h and 14 days ago" in e.reason


def test_g09_low_activity_subscriber(tmp_path, conn, cfg, goals):
    vf, cat = facts(tmp_path, conn, cfg, [play("p1", "2026-09-25T10:00:00.000Z", title="T3")],
                    [profile_row(status="active")])  # fmt: skip
    assert evaluate(goals.rules["G09"], vf, AT, cat, cfg).eligible
    two = [
        play("a", "2026-09-25T10:00:00.000Z", "T3", "U-2"),
        play("b", "2026-09-27T10:00:00.000Z", "T3", "U-2"),
    ]
    vf2, _ = facts(tmp_path, conn, cfg, two, [profile_row("U-2", status="active")], user="U-2")
    assert "played on 2 IST days" in evaluate(goals.rules["G09"], vf2, AT, cat, cfg).reason


def test_g12_existing_user_who_stopped_visiting(tmp_path, conn, cfg, goals):
    old_open = make_event("o1", occurred="2026-09-10T10:00:00.000Z", received="2026-09-10T10:00:01.000Z")
    vf, cat = facts(tmp_path, conn, cfg, [old_open], [profile_row()])
    assert evaluate(goals.rules["G12"], vf, AT, cat, cfg).eligible
    recent = make_event(
        "o2", user_id="U-2", occurred="2026-09-28T10:00:00.000Z", received="2026-09-28T10:00:01.000Z"
    )
    vf2, _ = facts(tmp_path, conn, cfg, [recent], [profile_row("U-2")], user="U-2")
    assert "opened the app 3.0 days ago" in evaluate(goals.rules["G12"], vf2, AT, cat, cfg).reason
    vf3, _ = facts(
        tmp_path, conn, cfg, [], [profile_row("U-3", signup_at="2026-09-29T10:00:00.000Z")], user="U-3"
    )
    assert "not an existing user" in evaluate(goals.rules["G12"], vf3, AT, cat, cfg).reason


def test_g12_needs_no_activity_of_any_kind(tmp_path, conn, cfg, goals):
    # plays 2 days ago but no app_open event: the viewer has not stopped visiting
    vf, cat = facts(tmp_path, conn, cfg, [play("p", "2026-09-29T10:00:00.000Z", "T3")], [profile_row()])
    assert "was active 2.0 days ago" in evaluate(goals.rules["G12"], vf, AT, cat, cfg).reason


def test_completion_counts_as_having_played(tmp_path, conn, cfg, goals):
    events = [play("c", "2026-09-30T12:00:00.000Z", "T3", event="episode_completed")]  # play_started missing
    vf, cat = facts(tmp_path, conn, cfg, events, [profile_row(signup_at="2026-09-30T10:00:00.000Z")])
    assert vf.value("has_played") is True
    assert evaluate(goals.rules["G01"], vf, AT, cat, cfg).reason == "already has play_started"


# --- priority (D-019) and NO_GOAL -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "events,row,expected",
    [
        ([], profile_row(status="trial_active", signup_at="2026-09-30T10:00:00.000Z"), "G04"),
        ([], profile_row(signup_at="2026-09-30T10:00:00.000Z"), "G01"),  # new, never played
        ([play("p", "2026-09-30T12:00:00.000Z", title="T3")], profile_row(signup_at="2026-09-30T10:00:00.000Z"),
         "G02"),  # new, played but never completed
        ([play("c", "2026-09-30T12:00:00.000Z", "T3", event="episode_completed")],
         profile_row(signup_at="2026-09-30T10:00:00.000Z"), "G03"),  # new, completed -> come back tomorrow
        ([play("p", "2026-09-26T10:00:00.000Z")], profile_row(status="never"), "G07"),  # series beats G02/G05
        ([play("a", "2026-09-27T10:00:00.000Z", "T3"), play("b", "2026-09-29T10:00:00.000Z", "T3"),
          play("c", "2026-09-29T11:00:00.000Z", "T3", event="episode_completed")],
         profile_row(status="active"), "G08"),  # subscriber watching on 2+ days: try a new release
        ([play("a", "2026-09-29T10:00:00.000Z", "T3"), play("c", "2026-09-29T11:00:00.000Z", "T3",
          event="episode_completed")], profile_row(status="active"),
         "G09"),  # subscriber who watched on 1 day only: build a habit (D-030, above G08)
        ([make_event("o", occurred="2026-09-29T10:00:00.000Z", received="2026-09-29T10:00:01.000Z")],
         profile_row(status="never"), "G02"),  # existing, active, never completed: G02 beats G05
        ([], profile_row(status="never"), "G12"),  # existing, no activity at all for 14+ days: return first
    ],
)  # fmt: skip
def test_priority_order(tmp_path, conn, cfg, goals, events, row, expected):
    vf, cat = facts(tmp_path, conn, cfg, events, [row])
    sel = select_goal(vf, AT, goals, cat, cfg)
    assert sel.rule.goal_id == expected, sel.reason


def test_new_user_funnel_reason_and_alternatives(tmp_path, conn, cfg, goals):
    vf, cat = facts(tmp_path, conn, cfg, [], [profile_row(signup_at="2026-09-30T10:00:00.000Z")])
    sel = select_goal(vf, AT, goals, cat, cfg)
    assert "new-user funnel" in sel.reason and sel.also_eligible == ["G02", "G03", "G05"]


def test_no_goal_with_a_reason_per_goal(tmp_path, conn, cfg, goals):
    events = [play("c", "2026-09-29T10:00:00.000Z", "T3", event="episode_completed")]
    rows = [profile_row(status="lapsed", updated="2026-09-22T00:00:00.000Z"), profile_row(status="never")]
    vf, cat = facts(tmp_path, conn, cfg, events, rows)
    plan = plan_goal(conn, vf, AT, goals, cat, cfg)
    assert plan.outcome == NO_GOAL and plan.goal is None
    assert plan.evaluations["G10"].startswith("not measurable")
    assert plan.evaluations["G02"] == "already has episode_completed"
    assert "no eligible measurable goal" in plan.reason


# --- one active goal, re-planning ------------------------------------------------------------------------------


def test_new_goal_has_deadline_and_signal_then_is_kept(tmp_path, conn, cfg, goals):
    vf, cat = facts(tmp_path, conn, cfg, [], [profile_row(signup_at="2026-09-30T10:00:00.000Z")])
    plan = plan_goal(conn, vf, AT, goals, cat, cfg)
    assert plan.outcome == NEW and plan.goal["goal_id"] == "G01"
    assert (
        plan.goal["start"] == "2026-10-01T10:00:00.000Z"
        and plan.goal["deadline"] == "2026-10-02T10:00:00.000Z"
    )
    assert plan.goal["success_signal"] == "play_started (any title) within 24 h"
    with conn:
        gi = commit_plan(conn, plan)
    assert gi == "GI-U-1-1"
    later = parse_ts("2026-10-01T15:00:00.000Z")
    again = plan_goal(conn, vf, later, goals, cat, cfg)
    assert again.outcome == KEEP and again.goal["goal_id"] == "G01"


def test_database_allows_only_one_open_goal(tmp_path, conn, cfg, goals):
    vf, cat = facts(tmp_path, conn, cfg, [], [profile_row()])
    with conn:
        commit_plan(conn, plan_goal(conn, vf, AT, goals, cat, cfg))
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute("INSERT INTO goal_instances (goal_instance_id, user_id, goal_id, goal_name, rule_version,"
                     " status, start_at, deadline, goal_json) VALUES ('x', 'U-1', 'G05', 'n', 'v', 'open', 'a', 'b', '{}')")  # fmt: skip


def test_purchase_changes_the_situation_and_triggers_a_replan(tmp_path, conn, cfg, goals):
    profiles, cat = setup(tmp_path, conn, cfg, [play("c", "2026-09-29T10:00:00.000Z", "T3", event="episode_completed")],
                          [profile_row(status="never")])  # fmt: skip
    vf = resolve(conn, profiles, cat, "U-1", AT, trigger(), cfg)
    first = plan_goal(conn, vf, AT, goals, cat, cfg)
    assert first.goal["goal_id"] == "G05"
    with conn:
        commit_plan(conn, first)
    buy = make_event(
        "buy",
        event="purchase_completed",
        occurred="2026-10-01T10:02:00.000Z",
        received="2026-10-01T10:02:01.000Z",
        properties={"plan_id": "P_MONTH", "is_trial": False},
    )
    with conn:
        from ott_agent.ingest import ingest_event

        ingest_event(conn, buy, "live", 1, cfg)
    at2 = parse_ts("2026-10-01T10:05:00.000Z")
    vf2 = resolve(conn, profiles, cat, "U-1", at2, trigger(), cfg)
    plan = plan_goal(conn, vf2, at2, goals, cat, cfg)
    assert plan.outcome == REPLAN and plan.closing["goal_id"] == "G05"
    assert plan.closing["close_reason"].startswith("situation_changed: subscription status is now active")
    assert plan.goal["goal_id"] == "G09"  # new subscriber who has barely watched: build a habit (D-030)
    with conn:
        commit_plan(conn, plan)
    rows = conn.execute(
        "SELECT goal_id, status, close_reason FROM goal_instances ORDER BY goal_instance_id"
    ).fetchall()
    assert [(r["goal_id"], r["status"]) for r in rows] == [("G05", "closed"), ("G09", "open")]
    changes = [r["change"] for r in conn.execute("SELECT change FROM goal_log ORDER BY rowid")]
    assert changes == ["opened", "closed", "opened"]


def test_deadline_passed_closes_and_replans(tmp_path, conn, cfg, goals):
    vf, cat = facts(tmp_path, conn, cfg, [], [profile_row(signup_at="2026-09-30T10:00:00.000Z")])
    with conn:
        commit_plan(conn, plan_goal(conn, vf, AT, goals, cat, cfg))  # G01, deadline +24 h
    later = parse_ts("2026-10-02T11:00:00.000Z")
    plan = plan_goal(conn, vf, later, goals, cat, cfg)
    assert plan.outcome == REPLAN and plan.closing["close_reason"] == "deadline_passed"


def test_entry_window_is_not_rechecked_for_an_open_goal(tmp_path, conn, cfg, goals):
    # G03 started 1.5 days after signup; 1 day later the "signed up within 2 days" entry rule no longer holds,
    # but the goal keeps its full 48 h success window.
    events = [play("c", "2026-09-30T12:00:00.000Z", "T3", event="episode_completed")]
    vf, cat = facts(tmp_path, conn, cfg, events, [profile_row(signup_at="2026-09-29T22:00:00.000Z")])
    plan = plan_goal(conn, vf, AT, goals, cat, cfg)
    assert plan.goal["goal_id"] == "G03"
    with conn:
        commit_plan(conn, plan)
    assert plan_goal(conn, vf, parse_ts("2026-10-02T10:00:00.000Z"), goals, cat, cfg).outcome == KEEP


# --- rule correction touches only the affected open goals ------------------------------------------------------


def test_rule_correction_updates_only_open_goals_of_that_type(tmp_path, conn, cfg, goals):
    completed = lambda u: play(f"c-{u}", "2026-09-30T12:00:00.000Z", "T3", u, "episode_completed")
    rows = [profile_row(u, signup_at="2026-09-30T10:00:00.000Z") for u in ("U-1", "U-2", "U-3", "U-4")]
    profiles, cat = setup(tmp_path, conn, cfg, [completed("U-1"), completed("U-2"), completed("U-4")], rows)
    with conn:
        for u in ("U-1", "U-2", "U-3", "U-4"):  # U-1, U-2, U-4 -> G03; U-3 (never played) -> G01
            commit_plan(
                conn,
                plan_goal(conn, resolve(conn, profiles, cat, u, AT, trigger(), cfg), AT, goals, cat, cfg),
            )
        conn.execute(
            "UPDATE goal_instances SET status = 'closed', close_reason = 'test' WHERE user_id = 'U-4'"
        )
    at = parse_ts("2026-10-01T08:30:00.000Z")
    with conn:
        rule, affected = apply_rule_correction(
            conn, goals, "G03", {"event": "play_started", "condition": "on a later IST calendar day"}, at
        )
    assert rule.version == "G03@2026-10-01T08:30:00.000Z" and rule.measurable
    assert sorted(affected) == ["GI-U-1-1", "GI-U-2-1"]  # not U-3 (G01) and not U-4 (closed)
    by_user = {r["user_id"]: r for r in conn.execute("SELECT * FROM goal_instances")}
    assert by_user["U-1"]["rule_version"] == rule.version and by_user["U-4"]["rule_version"] == "G03@test-v1"
    assert json.loads(by_user["U-2"]["goal_json"])["success"]["event"] == "play_started"
    assert by_user["U-3"]["rule_version"] == "G01@test-v1"
    assert goals.version("G03@test-v1").success["event"] == "app_open"  # history kept
    logged = conn.execute("SELECT COUNT(*) FROM goal_log WHERE change = 'rule_corrected'").fetchone()[0]
    assert logged == 2


# --- Step 2 review: gaps and fixes ------------------------------------------------------------------------------


def test_open_g07_goal_is_replanned_when_its_series_is_withdrawn(tmp_path, conn, cfg, goals):
    vf, cat = facts(
        tmp_path, conn, cfg, [play("p1", "2026-09-27T10:00:00.000Z")], [profile_row(status="never")]
    )
    plan = plan_goal(conn, vf, AT, goals, cat, cfg)
    assert plan.goal["goal_id"] == "G07" and plan.goal["detail"]["series"]["slug"] == "raloja-d1"
    with conn:
        commit_plan(conn, plan)
    for tid in ("T1", "T2"):  # both language variants of the series withdrawn at 11:00
        cat.withdraw(tid, parse_ts("2026-10-01T11:00:00.000Z"))
    later = parse_ts("2026-10-01T12:00:00.000Z")
    again = plan_goal(conn, vf, later, goals, cat, cfg)
    assert again.outcome == REPLAN
    assert (
        again.closing["close_reason"]
        == "situation_changed: series raloja-d1 is no longer available (withdrawn or ended)"
    )
    assert again.goal["goal_id"] != "G07"


def test_correction_that_makes_a_rule_unmeasurable_closes_its_open_goals(tmp_path, conn, cfg, goals):
    vf, cat = facts(tmp_path, conn, cfg, [play("c", "2026-09-30T12:00:00.000Z", "T3", event="episode_completed")],
                    [profile_row(signup_at="2026-09-30T10:00:00.000Z")])  # fmt: skip
    with conn:
        commit_plan(conn, plan_goal(conn, vf, AT, goals, cat, cfg))  # G03
    with conn:
        rule, affected = apply_rule_correction(conn, goals, "G03", {"event": "smart_tv_ping"}, AT)
    assert not rule.measurable and affected == ["GI-U-1-1"]
    row = conn.execute("SELECT status, close_reason FROM goal_instances").fetchone()
    assert row["status"] == "closed" and row["close_reason"].startswith("rule_corrected_unmeasurable")
    later = plan_goal(conn, vf, parse_ts("2026-10-01T11:00:00.000Z"), goals, cat, cfg)
    assert later.outcome == NEW and later.goal["goal_id"] != "G03"  # never re-chosen while unmeasurable


def test_open_goal_whose_goal_was_removed_is_closed(tmp_path, conn, cfg, goals):
    vf, cat = facts(tmp_path, conn, cfg, [], [profile_row(signup_at="2026-09-30T10:00:00.000Z")])
    with conn:
        commit_plan(conn, plan_goal(conn, vf, AT, goals, cat, cfg))  # G01
    del goals.rules["G01"]  # e.g. a different goals.json on another day
    plan = plan_goal(conn, vf, parse_ts("2026-10-01T11:00:00.000Z"), goals, cat, cfg)
    assert plan.outcome == REPLAN and "no longer exists" in plan.closing["close_reason"]


def test_correction_for_unknown_goal_is_rejected(conn, goals):
    with pytest.raises(ValueError, match="unknown goal 'G99'"):
        apply_rule_correction(conn, goals, "G99", {"event": "play_started"}, AT)


def test_shadow_goal_for_holdout_is_stored_and_still_one_per_viewer(tmp_path, conn, cfg, goals):
    vf, cat = facts(tmp_path, conn, cfg, [], [profile_row(signup_at="2026-09-30T10:00:00.000Z")])
    with conn:
        gi = commit_plan(conn, plan_goal(conn, vf, AT, goals, cat, cfg), shadow=True)
    row = conn.execute(
        "SELECT shadow, status FROM goal_instances WHERE goal_instance_id = ?", (gi,)
    ).fetchone()
    assert row["shadow"] == 1 and row["status"] == "open"
    assert plan_goal(conn, vf, parse_ts("2026-10-01T11:00:00.000Z"), goals, cat, cfg).outcome == KEEP


def test_goal_priority_can_be_overridden_from_config(tmp_path, conn, cfg, goals):
    from ott_agent.config import Config

    (tmp_path / "cfg.json").write_text(json.dumps({"goal_priority": ["G05", "G02"]}))
    custom = Config.from_json(tmp_path / "cfg.json")
    assert custom.goal_priority == ("G05", "G02")
    vf, cat = facts(tmp_path, conn, cfg, [], [profile_row(status="never")])
    assert select_goal(vf, AT, goals, cat, cfg).rule.goal_id == "G12"  # default order
    assert select_goal(vf, AT, goals, cat, custom).rule.goal_id == "G05"


def test_goal_missing_from_priority_ranks_last(tmp_path, conn, cfg):
    data = json.loads(json.dumps(__import__("conftest").GOALS))
    data["goals"].append({"goal_id": "G13", "name": "new_goal", "eligibility": {"subscription_status": ["never"]},
                          "success": {"event": "play_started", "condition": "any", "window_hours": 24}})  # fmt: skip
    (tmp_path / "g.json").write_text(json.dumps(data))
    goals = GoalCatalogue.from_file(tmp_path / "g.json")
    vf, cat = facts(tmp_path, conn, cfg, [], [profile_row(status="never")])
    sel = select_goal(vf, AT, goals, cat, cfg)
    assert sel.rule.goal_id == "G12" and sel.also_eligible[-1] == "G13"


def test_g09_play_days_count_any_playback(tmp_path, conn, cfg, goals):
    # play_started missing on one day, but progress/completion events prove playback on 2 IST days
    events = [play("a", "2026-09-27T10:00:00.000Z", "T3"),
              play("b", "2026-09-29T10:00:00.000Z", "T3", event="play_progress")]  # fmt: skip
    vf, cat = facts(tmp_path, conn, cfg, events, [profile_row(status="active")])
    assert vf.value("play_ist_days_14d") == ["2026-09-27", "2026-09-29"]
    assert "played on 2 IST days" in evaluate(goals.rules["G09"], vf, AT, cat, cfg).reason
