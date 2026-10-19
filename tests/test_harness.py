"""Step 4 "Act within budget": the replay harness end to end on a tiny synthetic pack (D-035 to D-038).

Covers the brief's required tests for this step: a late/duplicate trigger, a tool failure (model and data store),
plus budgets, the latency limit, holdout, withdrawals, a corrected goal rule and send-time re-checks."""

import json
from dataclasses import replace

from conftest import GOALS, make_event, write_jsonl
from test_intervention import INTERVENTIONS, catalogue_rows
from test_profiles_catalogue import CAT_COLS, PROFILE_COLS, profile_row, write_csv

from ott_agent.config import Config
from ott_agent.harness import (
    DUPLICATE_IGNORED,
    FAILED_LATE,
    HOLDOUT_SHADOW,
    PUBLISHED,
    SAME_SESSION,
    STALE_SKIPPED,
    Replay,
)  # fmt: skip
from ott_agent.timeutil import parse_ts

PRICING = {"currency": "INR", "plans": [{"plan_id": "P_TRIAL", "price_inr": 1, "days": 3},
                                        {"plan_id": "P_MONTH", "price_inr": 149, "days": 30}]}  # fmt: skip


def trig(
    eid, user="U-1", at="2026-10-01T09:00:00.000Z", dialect="d1", session=None, occurred=None, sent=None
):
    t = make_event(eid, user_id=user, occurred=occurred or at, sent=sent, received=at, app_dialect=dialect,
                   session_id=session or f"s-{eid}")  # fmt: skip
    t["deliver_at"] = at
    return t


def build_pack(tmp, triggers, *, history=(), profiles=None, live=(), control=(), faults=(), arms=None):
    pack = tmp / "pack"
    for d in ("data", "config", "replay"):
        (pack / d).mkdir(parents=True, exist_ok=True)
    write_jsonl(pack / "data" / "events.jsonl", list(history) or [make_event("h0", user_id="U-0")])
    write_csv(pack / "data" / "profiles_snapshot.csv", PROFILE_COLS, profiles or [profile_row("U-1")])
    write_csv(pack / "data" / "catalogue.csv", CAT_COLS, catalogue_rows())
    eps = [{"episode_id": f"{r['content_slug']}-s1e{i}", "content_slug": r["content_slug"], "season": 1, "episode": i,
            "minutes": 3} for r in catalogue_rows() for i in (1, 2, 3)]  # fmt: skip
    write_csv(
        pack / "data" / "episodes.csv", ["episode_id", "content_slug", "season", "episode", "minutes"], eps
    )
    (pack / "config" / "goals.json").write_text(json.dumps(GOALS))
    (pack / "config" / "interventions.json").write_text(json.dumps(INTERVENTIONS))
    (pack / "config" / "pricing.json").write_text(json.dumps(PRICING))
    write_jsonl(pack / "replay" / "triggers.jsonl", list(triggers))
    write_jsonl(pack / "replay" / "live_events.jsonl", list(live))
    write_jsonl(pack / "replay" / "control_events.jsonl", list(control))
    write_jsonl(pack / "replay" / "model_faults.jsonl", list(faults))
    arms = arms or {"U-1": "agent"}
    write_csv(
        pack / "replay" / "arms.csv", ["user_id", "arm"], [{"user_id": u, "arm": a} for u, a in arms.items()]
    )
    return pack


def run(tmp, pack, **cfg):
    r = Replay(pack, tmp / "out", replace(Config(), pack_path=pack, **cfg))
    rep = r.run()
    return r, rep


def lines(path):
    return [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]


# --- one decision per trigger, outputs --------------------------------------------------------------------------


def test_outputs_and_one_decision_per_trigger(tmp_path):
    pack = build_pack(tmp_path, [trig("t1")])
    _r, rep = run(tmp_path, pack)
    out = tmp_path / "out"
    for name in ("decisions.jsonl", "actions.jsonl", "control_log.jsonl", "latency_report.json",
                 "data_quality_report.json"):  # fmt: skip
        assert (out / name).exists(), name
    [d] = lines(out / "decisions.jsonl")
    assert d["status"] == PUBLISHED and d["action"] == "HOME_PAGE" and d["goal"]["id"]
    for key in ("decision_id", "viewer_id", "trigger", "goal", "action", "dialect", "content", "confidence",
                "confidence_reason", "evidence", "evidence_detail", "assumptions", "checks", "decided_in_ms",
                "published_at", "status", "model_calls", "tool_calls"):  # fmt: skip
        assert key in d, key
    [a] = [x for x in lines(out / "actions.jsonl") if x["type"] == "home_page"]
    assert a["decision_id"] == d["decision_id"] and a["dialect"] == "d1"
    assert a["published_at"] == d["published_at"] and rep["over_60s"] == 0
    assert (out / "viewer_state" / "U-1.json").exists()


# --- duplicate, same session, stale (required: late or duplicate trigger) ---------------------------------------


def test_duplicate_trigger_creates_no_second_decision_or_action(tmp_path):
    first = trig("t1")
    again = {**trig("t1", at="2026-10-01T09:04:00.000Z"), "occurred_at": first["occurred_at"],
             "sent_at": first["sent_at"]}  # fmt: skip
    pack = build_pack(tmp_path, [first, again])
    r, _ = run(tmp_path, pack)
    assert [d["status"] for d in r.decisions] == [PUBLISHED, DUPLICATE_IGNORED]
    assert r.decisions[1]["duplicate_of"] == r.decisions[0]["decision_id"]
    assert len([a for a in r.actions if a["type"] == "home_page"]) == 1


def test_same_session_is_one_visit_unless_something_changed(tmp_path):
    pack = build_pack(
        tmp_path, [trig("t1", session="S"), trig("t2", at="2026-10-01T09:00:02.000Z", session="S")]
    )
    r, _ = run(tmp_path, pack)
    assert [d["status"] for d in r.decisions] == [PUBLISHED, SAME_SESSION]
    buy = make_event(
        "buy",
        event="purchase_completed",
        occurred="2026-10-01T09:00:01.000Z",
        received="2026-10-01T09:00:01.000Z",
        properties={"plan_id": "P_MONTH", "is_trial": False},
    )
    buy["deliver_at"] = buy["received_at"]
    pack2 = build_pack(tmp_path / "b", [trig("t1", session="S"), trig("t2", at="2026-10-01T09:00:02.000Z", session="S")],
                       live=[buy])  # fmt: skip
    r2, _ = run(tmp_path / "b", pack2)
    assert (
        r2.decisions[1]["status"] != SAME_SESSION and "purchase_completed" in r2.decisions[1]["replan_reason"]
    )


def test_stale_trigger_is_skipped_without_model_or_action(tmp_path):
    old = trig("t1", occurred="2026-09-28T09:00:00.000Z", sent="2026-10-01T08:59:59.000Z")  # buffered 3 days
    pack = build_pack(tmp_path, [old])
    r, _ = run(tmp_path, pack)
    [d] = r.decisions
    assert d["status"] == STALE_SKIPPED and d["model"]["outcome"] == "not_called" and r.actions == []


# --- holdout --------------------------------------------------------------------------------------------------


def test_holdout_is_decided_as_shadow_and_nothing_is_published(tmp_path):
    pack = build_pack(tmp_path, [trig("t1")], arms={"U-1": "holdout"})
    r, _ = run(tmp_path, pack)
    [d] = r.decisions
    assert (
        d["status"] == HOLDOUT_SHADOW
        and d["action"] == "NO_ACTION"
        and d["no_action_reason"]["code"] == "HOLDOUT"
    )
    assert d["shadow_action"]["action"] == "HOME_PAGE" and r.actions == []
    assert r.conn.execute("SELECT shadow FROM goal_instances").fetchone()["shadow"] == 1


def test_unknown_arm_is_treated_as_holdout(tmp_path):
    pack = build_pack(tmp_path, [trig("t1", user="U-9")], profiles=[profile_row("U-9")])
    r, _ = run(tmp_path, pack)
    assert r.decisions[0]["status"] == HOLDOUT_SHADOW and r.actions == []


# --- model second opinion and faults (required: tool failure) --------------------------------------------------


def test_unsupported_model_claim_is_rejected_and_goal_kept(tmp_path):
    fault = {
        "trigger_event_id": "t1",
        "user_id": "U-1",
        "fault": "unsupported_claim",
        "model_output": {
            "goal_id": "G04",
            "eligible": True,
            "confidence": 0.97,
            "reason": "confirmed in billing",
        },
    }
    pack = build_pack(tmp_path, [trig("t1")], faults=[fault])
    r, _ = run(tmp_path, pack)
    [d] = r.decisions
    assert d["model"]["outcome"] == "unsupported_claim" and d["model"]["claimed_goal"] == "G04"
    assert "subscription status is never" in d["model"]["failed_check"]
    assert d["goal"]["goal_id"] != "G04" and d["status"] == PUBLISHED
    assert "confirmed in billing" not in json.dumps(d["evidence"])  # model text is never evidence


def test_malformed_model_output_is_retried_then_rules_only(tmp_path):
    fault = {"trigger_event_id": "t1", "user_id": "U-1", "fault": "malformed_output",
             "model_output": '{"goal_id": "G02", "eligible": tr'}  # fmt: skip
    pack = build_pack(tmp_path, [trig("t1")], faults=[fault])
    r, _ = run(tmp_path, pack)
    [d] = r.decisions
    assert d["model"]["outcome"] == "malformed" and d["model_calls"] == 2 and d["status"] == PUBLISHED


def test_model_timeout_is_cut_at_the_call_timeout(tmp_path):
    fault = {"trigger_event_id": "t1", "user_id": "U-1", "fault": "timeout", "seconds": 45}
    pack = build_pack(tmp_path, [trig("t1")], faults=[fault])
    r, _ = run(tmp_path, pack)
    [d] = r.decisions
    assert d["model"]["outcome"] == "error" and "timed out" in d["model"]["error"]
    assert 3.0 <= d["latency_s"] < 4.0 and d["status"] == PUBLISHED


def test_model_unavailable_window_means_rules_only(tmp_path):
    down = {"at": "2026-10-01T08:55:00.000Z", "type": "dependency_unavailable", "dependency": "model_provider",
            "until": "2026-10-01T09:05:00.000Z"}  # fmt: skip
    pack = build_pack(tmp_path, [trig("t1")], control=[down])
    r, _ = run(tmp_path, pack)
    [d] = r.decisions
    assert d["model"]["outcome"] == "unavailable" and d["status"] == PUBLISHED


def test_data_store_down_falls_back_to_a_generic_page_without_message_or_goal(tmp_path):
    down = {"at": "2026-10-01T08:55:00.000Z", "type": "dependency_unavailable", "dependency": "user_data_store",
            "until": "2026-10-01T09:05:00.000Z"}  # fmt: skip
    pack = build_pack(
        tmp_path, [trig("t1")], control=[down], profiles=[profile_row("U-1", whatsapp_opt_in="true")]
    )
    r, _ = run(tmp_path, pack)
    [d] = r.decisions
    assert d["fallback_used"] == "generic_page" and d["status"] == PUBLISHED and d["message"] is None
    assert d["confidence"] == 0.3 and d["goal"] is None
    assert r.conn.execute("SELECT COUNT(*) FROM goal_instances").fetchone()[0] == 0
    assert [a["type"] for a in r.actions] == ["home_page"]


def test_data_store_down_uses_recent_cached_facts(tmp_path):
    down = {"at": "2026-10-01T09:30:00.000Z", "type": "dependency_unavailable", "dependency": "user_data_store",
            "until": "2026-10-01T10:30:00.000Z"}  # fmt: skip
    pack = build_pack(tmp_path, [trig("t1"), trig("t2", at="2026-10-01T10:00:00.000Z")], control=[down])
    r, _ = run(tmp_path, pack)
    assert (
        r.decisions[1]["fallback_used"] == "cached_facts"
        and "3600 s earlier" in r.decisions[1]["fallback_detail"]
    )


def test_store_unavailable_read_always_fails():
    from ott_agent.tools import Dependencies, DependencyFailed, ToolBox

    deps = Dependencies()
    at = parse_ts("2026-10-01T09:00:00.000Z")
    deps.add("user_data_store", "unavailable", at, parse_ts("2026-10-01T10:00:00.000Z"))
    tb = ToolBox(Config(), deps, at, "t1")
    try:
        tb.store("read", lambda: "value")
        raise AssertionError("the read should fail while the store is unavailable")
    except DependencyFailed as exc:
        assert exc.dependency == "user_data_store" and tb.simulated_s == Config().store_timeout_s


# --- budgets and the latency limit -------------------------------------------------------------------------------


def test_budget_exhausted_means_no_action(tmp_path):
    pack = build_pack(tmp_path, [trig("t1")])
    r, _ = run(tmp_path, pack, max_tool_calls=2)
    [d] = r.decisions
    assert d["fallback_used"] == "budget_exceeded" and d["action"] == "NO_ACTION" and r.actions == []
    assert d["tool_calls"] <= 2


def test_publishing_too_late_is_a_recorded_failure(tmp_path):
    pack = build_pack(tmp_path, [trig("t1")])
    r, _rep = run(tmp_path, pack, publish_deadline_s=0.5)  # the mock model alone takes 0.8 s
    [d] = r.decisions
    assert d["status"] == FAILED_LATE and d["published_at"] is None and r.actions == []


def test_queue_wait_counts_in_latency(tmp_path):
    users = [f"U-{i}" for i in range(1, 4)]
    pack = build_pack(tmp_path, [trig(f"t{u}", user=u) for u in users], profiles=[profile_row(u) for u in users],
                      arms={u: "agent" for u in users})  # fmt: skip
    r, _ = run(tmp_path, pack, workers=1)
    q = [d["queue_s"] for d in r.decisions]
    assert q[0] == 0 and q[1] > 0.8 and q[2] > q[1]
    for d in r.decisions:
        assert abs(d["latency_s"] - (d["queue_s"] + d["decided_in_ms"] / 1000)) < 0.01


# --- control events --------------------------------------------------------------------------------------------


def test_withdrawal_republishes_live_pages_and_cancels_pending_messages(tmp_path):
    prof = [profile_row("U-1", whatsapp_opt_in="true")]  # G12 viewer: page + WhatsApp reminder
    withdraw = {
        "at": "2026-10-01T10:00:00.000Z",
        "type": "title_withdrawn",
        "title_id": "F1",
        "reason": "licence",
    }
    pack = build_pack(tmp_path, [trig("t1")], profiles=prof, control=[withdraw])
    r, _ = run(tmp_path, pack)
    pages = [a for a in r.actions if a["type"] == "home_page"]
    assert pages[0]["hero_title_id"] == "F1" and len(pages) == 2
    ids = [pages[1]["hero_title_id"]] + [t for row in pages[1]["rows"] for t in row["title_ids"]]
    assert "F1" not in ids and pages[1]["published_at"].startswith("2026-10-01T10:00")
    log = next(c for c in r.control_log if c["type"] == "title_withdrawn")
    assert log["messages_cancelled"] == [{"user_id": "U-1", "decision_id": r.decisions[0]["decision_id"]}]
    assert not [a for a in r.actions if a["type"] in ("whatsapp", "push")]


def test_corrected_goal_rule_updates_only_open_goals(tmp_path):
    new_user = profile_row("U-1", signup_at="2026-09-30T10:00:00.000Z")
    done = make_event(
        "c",
        event="episode_completed",
        occurred="2026-09-30T12:00:00.000Z",
        received="2026-09-30T12:00:01.000Z",
        properties={"title_id": "F2", "episode_id": "x"},
    )
    fix = {
        "at": "2026-10-01T09:30:00.000Z",
        "type": "goal_rule_corrected",
        "goal_id": "G03",
        "new_success": {
            "event": "play_started",
            "condition": "on a later IST calendar day within the window",
        },
    }
    pack = build_pack(tmp_path, [trig("t1")], history=[done], profiles=[new_user], control=[fix])
    r, _ = run(tmp_path, pack)
    assert r.decisions[0]["goal"]["goal_id"] == "G03"
    log = next(c for c in r.control_log if c["type"] == "goal_rule_corrected")
    assert log["affected_goal_instances"] == ["GI-U-1-1"]
    row = r.conn.execute("SELECT rule_version FROM goal_instances").fetchone()
    assert row["rule_version"] == "G03@2026-10-01T09:30:00.000Z"


def test_reminder_is_sent_unless_the_viewer_already_came_back(tmp_path):
    prof = [profile_row("U-1", whatsapp_opt_in="true")]
    pack = build_pack(tmp_path, [trig("t1")], profiles=prof)
    r, _ = run(tmp_path, pack)
    [m] = [a for a in r.actions if a["type"] == "whatsapp"]
    assert m["send_at"] == "2026-10-02T13:30:00.000Z" and m["template_id"] == "wa_recommend_title_v1"
    played = make_event(
        "p",
        event="play_started",
        occurred="2026-10-01T11:00:00.000Z",
        received="2026-10-01T11:00:01.000Z",
        properties={"title_id": "F2"},
    )
    played["deliver_at"] = played["received_at"]
    pack2 = build_pack(tmp_path / "b", [trig("t1")], profiles=prof, live=[played])
    r2, _ = run(tmp_path / "b", pack2)
    assert not [a for a in r2.actions if a["type"] == "whatsapp"]
    assert "already played" in r2.decisions[0]["message"]["cancel_reason"]
    assert parse_ts(r2.decisions[0]["message"]["send_at"]).hour == 13
