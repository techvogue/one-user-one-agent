"""Step 5 "Verify before acting": the independent verifier must be able to reject (D-040).

Each test hands the verifier a proposal broken in exactly one way and expects it to be caught: reject for goal, page,
holdout and duplicate problems; downgrade (drop the message, keep the page) for message-only problems."""

import ast
import json
from pathlib import Path

import pytest
from conftest import make_event
from test_harness import build_pack, trig
from test_profiles_catalogue import profile_row

from ott_agent.config import Config
from ott_agent.ingest import ingest_file
from ott_agent.store import connect
from ott_agent.timeutil import parse_ts
from ott_agent.verifier import Verifier

AT = parse_ts("2026-10-01T09:00:00.000Z")
PUB = parse_ts("2026-10-01T09:00:01.000Z")


@pytest.fixture
def ver(tmp_path):
    history = [
        make_event("o1", user_id="U-1", occurred="2026-09-10T10:00:00.000Z", received="2026-09-10T10:00:01.000Z"),
        make_event("p7", user_id="U-7", event="play_started", occurred="2026-09-27T10:00:00.000Z",
                   received="2026-09-27T10:00:01.000Z", properties={"title_id": "S3"}),
    ]  # fmt: skip
    profiles = [profile_row("U-1", whatsapp_opt_in="true", notifications_opt_in="false"),
                profile_row("U-2"), profile_row("U-7", status="never")]  # fmt: skip
    pack = build_pack(tmp_path, [], history=history, profiles=profiles,
                      arms={"U-1": "agent", "U-2": "holdout", "U-7": "agent"})  # fmt: skip
    conn = connect(tmp_path / "v.sqlite")
    ingest_file(conn, pack / "data" / "events.jsonl", "history", Config())
    return Verifier(pack, conn, Config())


def page(dialect="d1", hero="S2", rows=None):
    return {"dialect": dialect, "hero_title_id": hero,
            "rows": rows if rows is not None else [{"row_type": "popular_in_dialect", "title_ids": ["M2", "M4", "M6"]}]}  # fmt: skip


def goal(gid="G12", **kw):
    return {"goal_id": gid, "start": "2026-10-01T09:00:00.000Z", "deadline": "2026-10-08T09:00:00.000Z", **kw}


def whatsapp(title="S2", **kw):
    return {"channel": "whatsapp", "send_at": "2026-10-02T13:30:00.000Z", "title_id": title,
            "deeplink": f"ott://title/{title}", "dialect": "d1", "template_id": "wa_recommend_title_v1",
            "variables": {"title_name": "Show-2", "dialect_name": "d1"}, **kw}  # fmt: skip


def proposal(**kw):
    p = {"user": "U-1", "trigger": trig("t1"), "known_at": AT, "publish_at": PUB, "plan_outcome": "NEW",
         "goal": goal(), "page": page(), "message": None, "model": {"outcome": "agreed"}, "model_calls": 1,
         "tool_calls": 4, "latency_s": 0.9, "fallback": None}  # fmt: skip
    p.update(kw)
    return p


def failed(v):
    return {f["check"] for f in v.failed}


# --- the good case ----------------------------------------------------------------------------------------------


def test_a_valid_decision_is_approved(ver):
    v = ver.verify(proposal(message=whatsapp()))
    assert v.verdict == "approve" and not v.failed
    assert {
        "holdout",
        "eligibility",
        "dialect",
        "availability",
        "consent",
        "frequency_cap",
        "template",
    } <= set(v.passed)


# --- reject: goal, holdout, duplicates, page --------------------------------------------------------------------


def test_holdout_viewer_may_not_receive_anything(ver):
    v = ver.verify(proposal(user="U-2", trigger=trig("t2", user="U-2")))
    assert v.verdict == "reject" and "holdout" in failed(v)


def test_unknown_arm_may_not_receive_anything(ver):
    v = ver.verify(proposal(user="U-99", trigger=trig("t9", user="U-99")))
    assert v.verdict == "reject" and "holdout" in failed(v)


def test_second_page_for_the_same_trigger_is_rejected(ver):
    ver.record_published({"trigger_event_id": "t1", "type": "home_page", "user_id": "U-1"})
    assert "duplicate" in failed(ver.verify(proposal()))


def test_model_claim_without_evidence_is_rejected(ver):
    # the decision step was (wrongly) persuaded by the model: goal G04 for a viewer who never had a trial
    model = {"outcome": "unsupported_claim", "claimed_goal": "G04"}
    v = ver.verify(proposal(user="U-7", trigger=trig("t7", user="U-7"), goal=goal("G04"), model=model))
    assert v.verdict == "reject" and {"eligibility", "model_not_evidence"} <= failed(v)


def test_unmeasurable_goal_is_rejected(ver):
    v = ver.verify(proposal(goal=goal("G10")))
    assert v.verdict == "reject" and any("not measurable" in f["detail"] for f in v.failed)


def test_goal_the_evidence_does_not_support_is_rejected(ver):
    # U-7 played two days ago: not absent, so G12 is wrong; and G01 needs a provable recent signup
    assert "eligibility" in failed(
        ver.verify(proposal(user="U-7", trigger=trig("t7", user="U-7"), goal=goal("G12")))
    )
    assert "eligibility" in failed(ver.verify(proposal(goal=goal("G01"))))


def test_kept_goal_is_not_rechecked_on_entry_windows(ver):
    # an open G03 kept after its 2-day entry window: only state conditions are re-checked (D-029)
    assert "eligibility" in ver.verify(proposal(goal=goal("G03"), plan_outcome="KEEP")).passed


@pytest.mark.parametrize(
    "bad_page,check",
    [
        (page(dialect="d2"), "dialect"),  # page not in the app's dialect
        (page(hero="D2A"), "title_dialect"),  # a title from another dialect
        (page(hero="BAD"), "availability"),  # quarantined: instruction-like description
        (page(hero="END"), "availability"),  # ends inside the page's 2-hour life
        (page(hero="NOPE"), "availability"),  # does not exist
        (page(hero="S1", rows=[{"row_type": "popular_in_dialect", "title_ids": ["S1E", "M2", "M4"]}]), "unique_slug"),
        (page(rows=[{"row_type": "popular_in_dialect", "title_ids": ["M2", "M4"]}]), "row_limits"),
        (page(rows=[{"row_type": "continue_watching", "title_ids": ["M2", "M4", "M6"]}]), "row_types"),
        (page(hero=None, rows=[]), "not_empty"),
    ],
)  # fmt: skip
def test_page_problems_are_rejected(ver, bad_page, check):
    v = ver.verify(proposal(page=bad_page))
    assert v.verdict == "reject" and check in failed(v)


def test_withdrawn_title_is_rejected_from_the_withdrawal_on(ver):
    assert ver.verify(proposal(page=page(hero="F1"))).verdict == "approve"
    ver.withdraw("F1", parse_ts("2026-10-01T08:00:00.000Z"))
    assert "availability" in failed(ver.verify(proposal(page=page(hero="F1"))))


def test_budget_and_latency_breaches_are_rejected(ver):
    assert "budget" in failed(ver.verify(proposal(model_calls=9)))
    assert "budget" in failed(ver.verify(proposal(tool_calls=21)))
    assert "latency" in failed(ver.verify(proposal(latency_s=61)))


# --- downgrade: message-only problems drop the message, keep the page --------------------------------------------


@pytest.mark.parametrize(
    "msg,check",
    [
        (
            whatsapp(channel="push", text={"title": "Hi", "body": "Watch Show-2"}),
            "consent",
        ),  # no push consent
        (whatsapp(send_at="2026-10-02T17:00:00.000Z"), "quiet_hours"),  # 22:30 IST
        (whatsapp(template_id="wa_free_offer_v9"), "template"),  # not an approved template
        (
            whatsapp(variables={"title_name": "Best show ever!!", "dialect_name": "d1"}),
            "template",
        ),  # free text
        (whatsapp(deeplink="https://evil.example/S2"), "deeplink"),
        (
            whatsapp(
                title="D2A",
                deeplink="ott://title/D2A",
                variables={"title_name": "Other-1", "dialect_name": "d2"},
            ),
            "message_dialect",
        ),
        (
            whatsapp(
                title="END",
                deeplink="ott://title/END",
                variables={"title_name": "Ending-Soon", "dialect_name": "d1"},
            ),
            "message_title_available",
        ),
    ],
)
def test_message_problems_downgrade(ver, msg, check):
    v = ver.verify(proposal(message=msg))
    assert v.verdict == "downgrade" and check in failed(v)


def test_push_copy_rules(ver, tmp_path):
    ver.profile_rows["U-1"][0]["notifications_opt_in"] = "true"
    for text in ({"title": "Hurry! last chance", "body": "Watch now"}, {"title": "Offer", "body": "Only ₹49 today"},
                 {"title": "x" * 41, "body": "Watch"}):  # fmt: skip
        v = ver.verify(proposal(message=whatsapp(channel="push", text=text)))
        assert "push_copy" in failed(v), text


def test_frequency_cap_counts_scheduled_and_sent_messages(ver):
    ver.schedule("D-OTHER", "U-1", parse_ts("2026-10-02T13:30:00.000Z"))
    assert "frequency_cap" in failed(ver.verify(proposal(message=whatsapp())))
    ver.unschedule("D-OTHER")
    ver.record_published({"user_id": "U-1", "type": "push", "send_at": "2026-10-02T05:00:00.000Z",
                          "trigger_event_id": "x"})  # fmt: skip
    assert "frequency_cap" in failed(ver.verify(proposal(message=whatsapp())))


def test_reminder_for_a_goal_without_reminders_downgrades(ver):
    v = ver.verify(proposal(goal=goal("G05"), message=whatsapp(), user="U-7", trigger=trig("t7", user="U-7")))
    assert "message_goal" in failed(v)


def test_opt_out_event_overrides_profile_consent(ver, tmp_path):
    out = make_event("oo", user_id="U-1", event="notification_opt_out", occurred="2026-09-30T10:00:00.000Z",
                     received="2026-09-30T10:00:01.000Z", properties={"channel": "whatsapp"})  # fmt: skip
    from ott_agent.ingest import ingest_event

    with ver.conn:
        ingest_event(ver.conn, out, "live", 1, Config())
    assert "consent" in failed(ver.verify(proposal(message=whatsapp())))


# --- send-time re-check -------------------------------------------------------------------------------------------


def test_send_time_check_cancels_when_goal_closed_or_reached(ver):
    pending = {"decision_id": "D-1", "user_id": "U-1", "goal": goal(), "message": whatsapp()}
    known = parse_ts("2026-10-01T18:00:00.000Z")
    assert ver.verify_send(pending, known, goal_open=True).verdict == "approve"
    assert "goal_open" in failed(ver.verify_send(pending, known, goal_open=False))
    played = make_event("pl", user_id="U-1", event="play_started", occurred="2026-10-01T12:00:00.000Z",
                        received="2026-10-01T12:00:01.000Z", properties={"title_id": "S2"})  # fmt: skip
    from ott_agent.ingest import ingest_event

    with ver.conn:
        ingest_event(ver.conn, played, "live", 1, Config())
    v = ver.verify_send(pending, known, goal_open=True)
    assert v.verdict == "reject" and "not_already_reached" in failed(v)


# --- independence ---------------------------------------------------------------------------------------------


def test_verifier_does_not_import_the_decision_code():
    src = Path(__file__).parent.parent / "src" / "ott_agent" / "verifier.py"
    imported = {n.module for n in ast.walk(ast.parse(src.read_text())) if isinstance(n, ast.ImportFrom)}
    assert imported <= {"__future__", "config", "timeutil", "dataclasses", "datetime", "pathlib"}, imported
    assert not imported & {"facts", "goals", "planner", "intervention", "catalogue", "profiles"}


def test_verifier_scans_for_instructions_independently(ver):
    assert "BAD" in ver.flagged and len(ver.flagged) == 1
    assert json.dumps(sorted(ver.flagged)) == '["BAD"]'


def test_goal_that_recently_failed_for_the_viewer_is_rejected(tmp_path, ver):
    (tmp_path / "pack" / "pilot").mkdir(exist_ok=True)
    (tmp_path / "pack" / "pilot" / "pilot_outcomes.csv").write_text(
        "user_id,arm,served,goal_id,goal_started_at,succeeded\nU-1,treatment,true,G12,2026-09-15T10:00:00.000Z,false\n"
    )
    v = Verifier(tmp_path / "pack", ver.conn, Config())
    r = v.verify(proposal())
    assert r.verdict == "reject" and "eligibility" in failed(r)
    assert "already failed" in next(f["detail"] for f in r.failed if f["check"] == "eligibility")
    assert ver.verify(proposal()).verdict == "approve"  # without the pilot row the same goal is fine
