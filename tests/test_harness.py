"""Step 4 "Act within budget": the replay harness end to end on a tiny synthetic pack (D-035 to D-038).

Covers the brief's required tests for this step: a late/duplicate trigger, a tool failure (model and data store),
plus budgets, the latency limit, holdout, withdrawals, a corrected goal rule and send-time re-checks."""

import json
from dataclasses import replace

import pytest
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


# --- Step 5: the replay obeys the independent verifier ------------------------------------------------------------


def test_verifier_rejection_stops_publishing(tmp_path, monkeypatch):
    import ott_agent.harness as h

    real = h.decide_intervention

    def buggy(*a, **kw):  # simulate a bug in the decision step: page in the wrong dialect
        iv = real(*a, **kw)
        if iv.page:
            iv.page = {**iv.page, "dialect": "d2"}
        return iv

    monkeypatch.setattr(h, "decide_intervention", buggy)
    pack = build_pack(tmp_path, [trig("t1")])
    r, _ = run(tmp_path, pack)
    [d] = r.decisions
    assert d["status"] == "REJECTED_BY_VERIFIER" and "dialect" in d["checks"]["failed"]
    assert r.actions == [] and r.conn.execute("SELECT COUNT(*) FROM goal_instances").fetchone()[0] == 0


def test_verifier_downgrade_drops_only_the_message(tmp_path, monkeypatch):
    import ott_agent.harness as h

    real = h.decide_intervention

    def buggy(*a, **kw):  # simulate a bug: a reminder sent at 23:00 IST (quiet hours)
        iv = real(*a, **kw)
        if iv.message:
            iv.message = {**iv.message, "send_at": "2026-10-02T17:30:00.000Z"}
        return iv

    monkeypatch.setattr(h, "decide_intervention", buggy)
    pack = build_pack(tmp_path, [trig("t1")], profiles=[profile_row("U-1", whatsapp_opt_in="true")])
    r, _ = run(tmp_path, pack)
    [d] = r.decisions
    assert d["status"] == PUBLISHED and d["verifier"]["verdict"] == "downgrade"
    assert "message dropped by the verifier" in d["message_note"] and "quiet_hours" in d["message_note"]
    assert [a["type"] for a in r.actions] == ["home_page"]


# --- every decision record carries what the brief requires (D-045) ----------------------------------------------

REQUIRED = {
    "decision_id": str,
    "viewer_id": str,
    "trigger": dict,
    "action": str,
    "dialect": dict,
    "content": list,
    "confidence": (int, float),
    "confidence_reason": str,
    "evidence": list,
    "evidence_detail": list,
    "assumptions": list,
    "conflicts": list,
    "checks": dict,
    "decided_in_ms": (int, float),
    "status": str,
}


def record_gaps(d: dict) -> list[str]:
    """The brief's "every decision must include" list, checked on one record."""
    gaps = [f"missing {k}" for k, t in REQUIRED.items() if not isinstance(d.get(k), t)]
    gaps += [
        f"trigger.{k}" for k in ("event", "received_at", "event_age_s") if k not in (d.get("trigger") or {})
    ]
    if d.get("goal"):
        gaps += [f"goal.{k}" for k in ("id", "deadline", "success_signal") if not d["goal"].get(k)]
    elif not ((d.get("no_action_reason") or {}).get("detail") or d.get("goal_note")):
        gaps.append("no goal and no reason")
    if d.get("action") == "NO_ACTION" and not (d.get("no_action_reason") or {}).get("detail"):
        gaps.append("no action without a reason")
    if not d.get("confidence_reason"):
        gaps.append("confidence without a reason")
    if "resolution" not in (d.get("dialect") or {}):
        gaps.append("dialect without a resolution")
    if not any("freshness" in e or "age_s" in e for e in d.get("evidence_detail", [])):
        gaps.append("evidence without freshness")
    if not (d["checks"].get("passed") or d["checks"].get("failed")):
        gaps.append("no check results")
    if not d.get("assumptions"):
        gaps.append("no assumptions")
    if not isinstance(d.get("rule_version"), str) or not d.get("config_versions"):
        gaps.append("no rule_version / config_versions")
    return gaps


def test_every_status_produces_a_complete_record(tmp_path, monkeypatch):
    users = [f"U-{i}" for i in range(1, 7)]
    old = trig("t-stale", user="U-2", occurred="2026-09-28T09:00:00.000Z", sent="2026-10-01T08:59:59.000Z")
    first = trig("t1", user="U-1", session="S")
    dup = {**trig("t1", user="U-1", at="2026-10-01T09:03:00.000Z", session="S"),
           "occurred_at": first["occurred_at"], "sent_at": first["sent_at"]}  # fmt: skip
    triggers = [first, trig("t1b", user="U-1", at="2026-10-01T09:00:02.000Z", session="S"), dup, old,
                trig("t3", user="U-3"), trig("t4", user="U-4", dialect="d7"),
                trig("t5", user="U-5", at="2026-10-01T09:20:00.000Z"), trig("t6", user="U-6")]  # fmt: skip
    down = {"at": "2026-10-01T09:15:00.000Z", "type": "dependency_unavailable", "dependency": "user_data_store",
            "until": "2026-10-01T09:30:00.000Z"}  # fmt: skip
    pack = build_pack(tmp_path, triggers, profiles=[profile_row(u, status="lapsed", dialect="d1") for u in users]
                      + [profile_row("U-1", dialect="d2")], control=[down],
                      arms={"U-1": "agent", "U-2": "agent", "U-3": "holdout", "U-4": "agent", "U-5": "agent",
                            "U-6": "agent"})  # fmt: skip
    r, _ = run(tmp_path, pack)
    statuses = {d["status"] for d in r.decisions}
    assert {
        "PUBLISHED",
        "SAME_SESSION",
        "DUPLICATE_IGNORED",
        "STALE_SKIPPED",
        "HOLDOUT_SHADOW",
        "NO_ACTION",
    } <= statuses
    assert any(d["fallback_used"] == "generic_page" for d in r.decisions)
    for d in r.decisions:
        assert record_gaps(d) == [], (d["status"], record_gaps(d))
    # budget exhausted and too late also produce complete records
    for cfg in ({"max_tool_calls": 2}, {"publish_deadline_s": 0.5}):
        r2, _ = run(tmp_path / str(cfg), build_pack(tmp_path / str(cfg), [trig("t1")]), **cfg)
        assert record_gaps(r2.decisions[0]) == [], r2.decisions[0]["status"]


def test_resolved_conflicts_are_listed_on_the_decision(tmp_path):
    rows = [
        profile_row("U-1", status="lapsed", dialect="d2", updated="2026-09-22T00:00:00.000Z"),
        profile_row("U-1", status="never", dialect="d1"),
    ]  # the U-04215 case
    pack = build_pack(tmp_path, [trig("t1", dialect="d3")], profiles=rows)
    r, _ = run(tmp_path, pack)
    by_fact = {c["fact"]: c for c in r.decisions[0]["conflicts"]}
    assert (
        by_fact["subscription_status"]["used"] == "uncertain" and not by_fact["subscription_status"]["usable"]
    )
    assert "profile_row@2026-09-22T00:00:00.000Z:lapsed" in by_fact["subscription_status"]["conflicting"]
    assert by_fact["page_dialect"]["used"] == "d3" and "profile:d1" in by_fact["page_dialect"]["conflicting"]
    assert "D-009" in by_fact["page_dialect"]["resolution"]


# --- D-047: every data trap is reported in the run's output -------------------------------------------------------


def test_data_quality_report_lists_every_trap(tmp_path, caplog):
    rows = [profile_row("U-1", status="lapsed", dialect="d2", updated="2026-09-22T00:00:00.000Z"),
            profile_row("U-1", status="never", dialect="d2"), profile_row("U-2", dialect="d1")]  # fmt: skip
    t1 = trig("t1", dialect="d3")
    t1["properties"] = {
        "deeplink": "ott://c/cmp1/d5/title/S2"
    }  # three different dialects: app d3, profile d2, link d5
    triggers = [
        t1,
        trig("t2", user="U-2", dialect="d7"),
        trig("t9", user="U-9"),
    ]  # invalid dialect; no profile row
    pack = build_pack(
        tmp_path, triggers, profiles=rows, arms={"U-1": "agent", "U-2": "agent", "U-9": "agent"}
    )
    with caplog.at_level("WARNING"):
        _r, _ = run(tmp_path, pack)
    assert any("quarantined BAD" in m for m in caplog.messages)  # logged at load
    traps = json.loads((tmp_path / "out" / "data_quality_report.json").read_text())["traps"]
    assert len([k for k in traps if k[0].isdigit()]) == 13
    [inj] = traps["9_injected_instructions"]["flagged_titles"]
    assert inj["title_id"] == "BAD" and inj["times_in_published_actions"] == 0
    goals = {g["goal_id"]: g for g in traps["10_unmeasurable_goals"]["goals"]}
    assert not goals["G10"]["measurable"] and "tv_app_open" in goals["G11"]["reason_not_selectable"]
    assert goals["G01"]["measurable"]
    p = traps["4_profile_snapshot"]
    assert p["users_with_two_rows"] == 1 and p["never_after_paid_contradictions"] == 1
    assert traps["5_new_users_without_profile"]["trigger_viewers_without_profile"] == 1
    d = traps["6_dialect_conflicts"]
    assert d["triggers_with_invalid_app_dialect"] == 1 and d["three_different_dialects"] == 1
    assert traps["7_language_variants"]["slugs_with_several_title_ids"] == 1  # show-1 has hin + en


# --- D-048: react to state changes when they arrive; notice bursts ------------------------------------------------


def _live(eid, event, when, user="U-1", **props):
    e = make_event(eid, user_id=user, event=event, occurred=when, received=when, properties=props)
    e["deliver_at"] = when
    return e


def _completed_recently(user="U-1"):  # a completed episode 2 days ago: not G02, not G12
    return make_event(
        f"c-{user}",
        user_id=user,
        event="episode_completed",
        occurred="2026-09-29T09:00:00.000Z",
        received="2026-09-29T09:00:01.000Z",
        properties={"title_id": "F3", "episode_id": "x"},
    )


@pytest.mark.parametrize("arm", ["agent", "holdout"])
def test_purchase_after_decision_replans_the_goal_when_it_arrives(tmp_path, arm):
    buy = _live("buy", "purchase_completed", "2026-10-01T09:02:00.000Z", plan_id="P_MONTH", is_trial=False)
    pack = build_pack(tmp_path, [trig("t1")], history=[_completed_recently()],
                      profiles=[profile_row("U-1", status="never")], live=[buy], arms={"U-1": arm})  # fmt: skip
    r, _ = run(tmp_path, pack)
    assert r.decisions[0]["goal"]["goal_id"] == "G05"  # first paid plan
    [react] = [c for c in r.control_log if c["type"] == "reaction_to_live_event"]
    assert react["at"] == "2026-10-01T09:02:00.000Z" and react["goal_before"]["goal_id"] == "G05"
    assert "subscription status is now active" in react["goal_after"]["reason"]
    rows = r.conn.execute(
        "SELECT goal_id, status, closed_at, shadow FROM goal_instances ORDER BY rowid"
    ).fetchall()
    assert rows[0]["goal_id"] == "G05" and rows[0]["status"] == "closed"
    assert rows[0]["closed_at"] == "2026-10-01T09:02:00.000Z"  # at the purchase, not at a later visit
    assert rows[1]["status"] == "open" and rows[1]["goal_id"] in ("G08", "G09")
    assert {x["shadow"] for x in rows} == {int(arm == "holdout")}  # both arms handled the same way
    assert len([a for a in r.actions if a["type"] == "home_page"]) == (
        1 if arm == "agent" else 0
    )  # no extra page


def test_opt_out_cancels_a_pending_reminder_when_it_arrives(tmp_path):
    out = _live("out", "notification_opt_out", "2026-10-01T12:00:00.000Z", channel="whatsapp")
    pack = build_pack(
        tmp_path, [trig("t1")], profiles=[profile_row("U-1", whatsapp_opt_in="true")], live=[out]
    )
    r, _ = run(tmp_path, pack)
    assert r.decisions[0]["message"]["channel"] == "whatsapp"  # a G12 reminder was planned at 09:00
    [react] = [c for c in r.control_log if c["type"] == "reaction_to_live_event"]
    assert react["goal_after"].startswith("kept") and react["messages_cancelled"]
    assert "consent" in react["messages_cancelled"][0]["reason"]
    assert r.decisions[0]["message"]["status"] == "cancelled"
    assert not [a for a in r.actions if a["type"] in ("whatsapp", "push")]


def test_burst_is_noticed_and_the_model_is_skipped_under_load(tmp_path):
    users = [f"U-{i}" for i in range(1, 31)]
    quiet = [
        trig(f"q{i}", user="U-0", at=f"2026-10-01T0{i}:00:00.000Z") for i in range(1, 9)
    ]  # an average to compare
    burst = [trig(f"b{u}", user=u, at="2026-10-01T09:30:00.000Z") for u in users]
    pack = build_pack(tmp_path, quiet + burst, profiles=[profile_row(u) for u in [*users, "U-0"]],
                      arms={u: "agent" for u in [*users, "U-0"]})  # fmt: skip
    r, rep = run(tmp_path, pack, workers=1)
    [b] = rep["load"]["bursts"]
    assert b["window_start"] == "2026-10-01T09:30:00.000Z" and b["triggers"] == 30
    assert b["model_skipped_under_load"] > 0  # later decisions skip the optional model call
    late = [d for d in r.decisions if d["trigger"]["event_id"].startswith("b")][-1]
    assert late["model"]["outcome"] == "skipped" and late["status"] == "PUBLISHED"
    assert all(d["latency_s"] <= 60 for d in r.decisions if "latency_s" in d)


def test_trap_map_names_real_tests():
    """Every trap names the tests that prove its handling, and every named test exists (no stale names)."""
    import ast
    from pathlib import Path

    from ott_agent.final import TRAP_NAMES
    from ott_agent.traps import PROVED_BY

    here = Path(__file__).parent
    names = {n.name for f in here.glob("test_*.py") for n in ast.walk(ast.parse(f.read_text()))
             if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}  # fmt: skip
    assert set(PROVED_BY) == set(TRAP_NAMES) and len(PROVED_BY) == 13
    for trap, tests in PROVED_BY.items():
        assert tests, trap
        assert not set(tests) - names, (trap, set(tests) - names)
