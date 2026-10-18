"""Fact resolver: precedence, conflicts, new users, consent, dialect, as-of knowledge (D-007–D-010, D-015)."""

from conftest import make_event
from test_profiles_catalogue import PROFILE_COLS, make_catalogue, profile_row, write_csv

from ott_agent.facts import parse_deeplink, resolve
from ott_agent.ingest import ingest_event
from ott_agent.profiles import load_profiles
from ott_agent.timeutil import parse_ts

AT = parse_ts("2026-10-01T10:00:00.000Z")


def setup(tmp_path, conn, cfg, events, profile_rows):
    start = conn.execute("SELECT COUNT(*) FROM event_deliveries").fetchone()[0]  # continue line numbers
    with conn:
        for i, e in enumerate(events, start + 1):
            ingest_event(conn, e, "history", i, cfg)
    write_csv(tmp_path / "profiles.csv", PROFILE_COLS, profile_rows)
    return load_profiles(tmp_path / "profiles.csv"), make_catalogue(tmp_path)


def trigger(dialect="d3", language="hin", deeplink=None, event_id="trig"):
    t = make_event(
        event_id,
        occurred="2026-10-01T09:59:58.000Z",
        received="2026-10-01T09:59:59.000Z",
        app_dialect=dialect,
        app_language=language,
    )
    if deeplink:
        t["properties"] = {"deeplink": deeplink}
    return t


def test_three_dialects_page_uses_app_setting_and_lists_conflicts(tmp_path, conn, cfg):
    profiles, cat = setup(tmp_path, conn, cfg, [], [profile_row(dialect="d1")])
    vf = resolve(conn, profiles, cat, "U-1", AT, trigger("d3", deeplink="ott://c/cmp036/d5/title/T0909"))
    page = vf["page_dialect"]
    assert page.value == "d3" and page.source == "trigger.app_setting"
    assert page.conflicts == ["profile:d1", "deeplink:d5"]


def test_invalid_app_dialect_means_no_page(tmp_path, conn, cfg):
    profiles, cat = setup(
        tmp_path, conn, cfg, [make_event("e1", app_dialect="d2")], [profile_row(dialect="d1")]
    )
    vf = resolve(conn, profiles, cat, "U-1", AT, trigger("d7"))
    assert vf.value("page_dialect") is None
    assert "trigger.app_setting:d7" in vf["page_dialect"].conflicts
    assert "DIALECT_UNKNOWN" in vf["page_dialect"].assumption
    assert vf.value("message_dialect") == "d2"  # latest valid app setting from history


def test_malformed_deeplink_is_flagged_not_used(tmp_path, conn, cfg):
    profiles, cat = setup(tmp_path, conn, cfg, [], [profile_row()])
    vf = resolve(conn, profiles, cat, "U-1", AT, trigger("d1", deeplink="ott://c/cmp059/None/title/T0902"))
    assert vf.value("deeplink") == {"campaign": "cmp059", "dialect": "None", "title_id": "T0902"}
    assert "deeplink_invalid_dialect:None" in vf.flags
    assert parse_deeplink("javascript:alert(1)") is None


def test_newer_purchase_overrides_never_profile(tmp_path, conn, cfg):
    buy = make_event(
        "p1",
        event="purchase_completed",
        occurred="2026-09-30T08:00:00.000Z",
        received="2026-09-30T08:00:01.000Z",
        properties={"plan_id": "P_MONTH", "price_inr": 149, "is_trial": False},
    )
    profiles, cat = setup(tmp_path, conn, cfg, [buy], [profile_row(status="never")])
    vf = resolve(conn, profiles, cat, "U-1", AT, trigger())
    sub = vf["subscription_status"]
    assert sub.value == "active" and sub.source == "event:p1"
    assert "profile_snapshot:never" in sub.conflicts
    assert vf.value("ever_paid") is True


def test_purchase_received_after_decision_time_is_not_known_yet(tmp_path, conn, cfg):
    buy = make_event(
        "p1",
        event="purchase_completed",
        occurred="2026-10-01T10:02:00.000Z",
        received="2026-10-01T10:02:01.000Z",
        properties={"is_trial": False},
    )
    profiles, cat = setup(tmp_path, conn, cfg, [buy], [profile_row(status="never")])
    assert resolve(conn, profiles, cat, "U-1", AT, trigger()).value("subscription_status") == "never"
    later = parse_ts("2026-10-01T10:03:00.000Z")
    assert resolve(conn, profiles, cat, "U-1", later, trigger()).value("subscription_status") == "active"


def test_contradictions_make_status_uncertain(tmp_path, conn, cfg):
    profiles, cat = setup(
        tmp_path,
        conn,
        cfg,
        [],
        [
            profile_row("U-1", status="never", plan_ends_at="2026-10-20T00:00:00.000Z"),
            profile_row("U-2", status="active", plan_ends_at="2026-10-01T08:08:01.447Z"),
        ],
    )
    assert resolve(conn, profiles, cat, "U-1", AT, trigger()).value("subscription_status") == "uncertain"
    assert resolve(conn, profiles, cat, "U-2", AT, trigger()).value("subscription_status") == "uncertain"


def test_new_user_without_profile(tmp_path, conn, cfg):
    signup = make_event(
        "s1",
        user_id="U-9",
        event="signup_completed",
        occurred="2026-10-01T09:58:00.000Z",
        received="2026-10-01T09:58:01.000Z",
    )
    profiles, cat = setup(tmp_path, conn, cfg, [signup], [profile_row("U-1")])
    t = trigger()
    t["user_id"] = "U-9"
    vf = resolve(conn, profiles, cat, "U-9", AT, t)
    assert vf["signup_at"].source == "event:s1"
    assert vf.value("subscription_status") == "never" and vf["subscription_status"].assumption
    assert vf.value("push_consent") is False and "consent unknown" in vf["push_consent"].assumption
    assert vf.value("whatsapp_consent") is False


def test_opt_out_event_overrides_profile_consent(tmp_path, conn, cfg):
    out = make_event("o1", event="notification_opt_out", properties={"channel": "push"})
    profiles, cat = setup(tmp_path, conn, cfg, [out], [profile_row(notifications_opt_in="true")])
    c = resolve(conn, profiles, cat, "U-1", AT, trigger())["push_consent"]
    assert c.value is False and c.source == "event:o1" and c.conflicts == ["profile_snapshot:true"]


def test_series_progress_counts_both_language_variants(tmp_path, conn, cfg):
    events = [
        make_event(
            "p1",
            event="play_started",
            occurred="2026-09-20T10:00:00.000Z",
            received="2026-09-20T10:00:01.000Z",
            properties={"title_id": "T1", "episode_id": "raloja-d1-s1e1"},
        ),
        make_event(
            "c1",
            event="episode_completed",
            occurred="2026-09-20T10:05:00.000Z",
            received="2026-09-20T10:05:01.000Z",
            properties={"title_id": "T2", "episode_id": "raloja-d1-s1e1"},
        ),
    ]
    profiles, cat = setup(tmp_path, conn, cfg, events, [profile_row()])
    vf = resolve(conn, profiles, cat, "U-1", AT, trigger())
    [s] = vf.value("series_in_progress")
    assert s["slug"] == "raloja-d1" and s["episodes_completed"] == 1 and s["episodes_total"] == 3
    assert vf.value("top_genre") == "Comedy"  # tie broken alphabetically: deterministic
    assert vf.value("play_ist_days_14d") == ["2026-09-20"]


# --- D-027: freshness rules per fact ---------------------------------------------------------------------------


def test_live_facts_usable_only_from_a_fresh_trigger(tmp_path, conn, cfg):
    profiles, cat = setup(tmp_path, conn, cfg, [], [profile_row()])
    fresh = resolve(conn, profiles, cat, "U-1", AT, trigger("d3"), cfg)
    assert fresh["page_dialect"].freshness == "live" and fresh["page_dialect"].usable
    old = make_event(
        "old",
        occurred="2026-09-28T10:00:00.000Z",
        sent="2026-10-01T09:59:58.000Z",
        received="2026-10-01T09:59:59.000Z",
        app_dialect="d3",
    )  # buffered 3 days on the phone
    stale = resolve(conn, profiles, cat, "U-1", AT, old, cfg)
    assert stale["page_dialect"].value == "d3"
    assert stale["page_dialect"].freshness == "stale" and not stale["page_dialect"].usable
    assert stale.usable("page_dialect") is None


def test_profile_facts_become_unusable_when_snapshot_too_old(tmp_path, conn, cfg):
    profiles, cat = setup(
        tmp_path,
        conn,
        cfg,
        [],
        [profile_row(status="active", notifications_opt_in="true", snapshot_at="2026-09-25T00:00:00.000Z")],
    )
    vf = resolve(conn, profiles, cat, "U-1", AT, trigger(), cfg)  # snapshot is 6.4 days old
    for name in ("subscription_status", "push_consent"):
        assert vf[name].freshness == "stale" and not vf[name].usable
    assert vf.usable("push_consent") is None  # downstream: unknown -> no message
    assert vf["signup_at"].usable  # immutable facts never go stale


def test_recent_snapshot_is_usable_and_dated_by_snapshot_time(tmp_path, conn, cfg):
    profiles, cat = setup(
        tmp_path, conn, cfg, [], [profile_row(status="never", updated="2026-08-25T07:36:04.179Z")]
    )
    sub = resolve(conn, profiles, cat, "U-1", AT, trigger(), cfg)["subscription_status"]
    assert sub.as_of == "2026-09-30T00:00:00.000Z"  # true as of the export, even if the row changed in August
    assert sub.usable and sub.freshness == "recent"
    assert "profile:U-1@2026-08-25T07:36:04.179Z" in sub.evidence


def test_uncertain_status_is_never_usable(tmp_path, conn, cfg):
    profiles, cat = setup(
        tmp_path, conn, cfg, [], [profile_row(status="never", plan_ends_at="2026-10-20T00:00:00.000Z")]
    )
    sub = resolve(conn, profiles, cat, "U-1", AT, trigger(), cfg)["subscription_status"]
    assert sub.value == "uncertain" and not sub.usable


def test_snapshot_is_stored_and_reloaded(tmp_path, conn, cfg):
    from ott_agent.facts import load_snapshots, save_snapshot

    profiles, cat = setup(tmp_path, conn, cfg, [], [profile_row()])
    vf = resolve(conn, profiles, cat, "U-1", AT, trigger(), cfg)
    with conn:
        save_snapshot(conn, vf, "trig")
        save_snapshot(conn, vf, "trig")  # idempotent per (user, at, trigger)
    [snap] = load_snapshots(conn, "U-1")
    assert snap["facts"]["page_dialect"]["value"] == "d3"
    assert snap["facts"]["page_dialect"]["freshness"] == "live"


def test_status_cannot_regress_from_paid_to_never(tmp_path, conn, cfg):
    # Real case in the pack (U-04215): newest row says "never", an older row says "lapsed".
    profiles, cat = setup(
        tmp_path,
        conn,
        cfg,
        [],
        [
            profile_row(status="lapsed", dialect="d2", updated="2026-09-22T02:49:42.237Z"),
            profile_row(status="never", dialect="d1", updated="2026-09-29T11:24:48.408Z"),
        ],
    )
    sub = resolve(conn, profiles, cat, "U-1", AT, trigger(), cfg)["subscription_status"]
    assert sub.value == "uncertain" and not sub.usable
    assert "profile_row@2026-09-22T02:49:42.237Z:lapsed" in sub.conflicts


# --- review fixes and coverage gaps -----------------------------------------------------------------------------


def test_ever_paid_true_when_an_older_row_was_paid(tmp_path, conn, cfg):
    profiles, cat = setup(
        tmp_path,
        conn,
        cfg,
        [],
        [
            profile_row(status="lapsed", updated="2026-09-22T02:49:42.237Z"),
            profile_row(status="never", updated="2026-09-29T11:24:48.408Z"),
        ],
    )
    paid = resolve(conn, profiles, cat, "U-1", AT, trigger(), cfg)["ever_paid"]
    assert paid.value is True and "profile_row@2026-09-22T02:49:42.237Z:lapsed" in paid.evidence


def test_signup_from_earliest_event_time_not_first_received(tmp_path, conn, cfg):
    later_sent = make_event(
        "s2",
        user_id="U-9",
        event="signup_completed",
        occurred="2026-09-28T08:00:00.000Z",
        sent="2026-09-30T08:00:00.000Z",
        received="2026-09-30T08:00:01.000Z",
    )  # buffered
    first_rcv = make_event(
        "s1",
        user_id="U-9",
        event="signup_completed",
        occurred="2026-09-29T08:00:00.000Z",
        received="2026-09-29T08:00:01.000Z",
    )
    profiles, cat = setup(tmp_path, conn, cfg, [first_rcv, later_sent], [profile_row("U-1")])
    assert resolve(conn, profiles, cat, "U-9", AT, None, cfg)["signup_at"].source == "event:s2"


def test_trigger_with_null_properties(tmp_path, conn, cfg):
    profiles, cat = setup(tmp_path, conn, cfg, [], [profile_row()])
    t = trigger("d2")
    t["properties"] = None
    vf = resolve(conn, profiles, cat, "U-1", AT, t, cfg)
    assert vf.value("page_dialect") == "d2" and vf.value("deeplink") is None


def test_old_opt_out_still_withdraws_consent(tmp_path, conn, cfg):
    out = make_event(
        "o1",
        event="notification_opt_out",
        occurred="2026-09-01T10:00:00.000Z",
        received="2026-09-01T10:00:01.000Z",
        properties={"channel": "whatsapp"},
    )
    profiles, cat = setup(tmp_path, conn, cfg, [out], [profile_row(whatsapp_opt_in="true")])
    vf = resolve(conn, profiles, cat, "U-1", AT, trigger(), cfg)
    assert vf.value("whatsapp_consent") is False  # conservative: no opt-in event exists to prove re-consent
    assert vf.value("push_consent") is True  # other channel unaffected


def test_message_dialect_fallback_chain(tmp_path, conn, cfg):
    profiles, cat = setup(tmp_path, conn, cfg, [], [profile_row(dialect="d5")])
    vf = resolve(conn, profiles, cat, "U-1", AT, trigger("NA"), cfg)  # no valid app dialect anywhere
    assert vf["message_dialect"].value == "d5" and vf["message_dialect"].source == "profile_snapshot"
    profiles2, _ = setup(tmp_path, conn, cfg, [], [profile_row(dialect="")])
    vf2 = resolve(conn, profiles2, cat, "U-1", AT, trigger(None), cfg)
    assert vf2.value("message_dialect") is None and vf2["message_dialect"].assumption


def test_play_days_window_and_buffered_app_open(tmp_path, conn, cfg):
    events = [
        make_event(
            "p_old",
            event="play_started",
            occurred="2026-09-16T10:00:00.000Z",
            received="2026-09-16T10:00:01.000Z",
            properties={"title_id": "T3"},
        ),  # 15 days before AT
        make_event(
            "p_in",
            event="play_started",
            occurred="2026-09-20T20:00:00.000Z",
            received="2026-09-20T20:00:01.000Z",
            properties={"title_id": "T3"},
        ),  # 21 Sep IST
        # app open from 25 Sep, buffered on the phone and received after a newer open
        make_event("o_new", occurred="2026-09-27T10:00:00.000Z", received="2026-09-27T10:00:01.000Z"),
        make_event(
            "o_buf",
            occurred="2026-09-25T10:00:00.000Z",
            sent="2026-09-30T10:00:00.000Z",
            received="2026-09-30T10:00:01.000Z",
        ),
    ]
    profiles, cat = setup(tmp_path, conn, cfg, events, [profile_row()])
    vf = resolve(conn, profiles, cat, "U-1", AT, trigger(), cfg)
    assert vf.value("play_ist_days_14d") == ["2026-09-21"]
    assert vf.value("last_app_open_at") == "2026-09-27T10:00:01.000Z"  # latest by event time, not by arrival
    assert vf.value("series_in_progress") == []  # T3 is a film


def test_fully_watched_series_is_not_in_progress(tmp_path, conn, cfg):
    events = [
        make_event(
            f"c{i}",
            event="episode_completed",
            occurred=f"2026-09-2{i}T10:00:00.000Z",
            received=f"2026-09-2{i}T10:00:01.000Z",
            properties={"title_id": "T1", "episode_id": f"raloja-d1-s1e{i}"},
        )
        for i in (1, 2, 3)
    ]
    profiles, cat = setup(tmp_path, conn, cfg, events, [profile_row()])
    assert resolve(conn, profiles, cat, "U-1", AT, trigger(), cfg).value("series_in_progress") == []


# --- plan expiry for event-derived status (same rule as the profile's plan_ends_at) ---------------------------

PLAN_DAYS = {"P_TRIAL": 3, "P_MONTH": 30, "P_QUARTER": 90}


def test_event_derived_active_expires_after_plan_length(tmp_path, conn, cfg):
    buy = make_event(
        "p1",
        user_id="U-9",
        event="purchase_completed",
        occurred="2026-08-20T10:00:00.000Z",
        received="2026-08-20T10:00:01.000Z",
        properties={"plan_id": "P_MONTH", "price_inr": 149, "is_trial": False},
    )
    profiles, cat = setup(tmp_path, conn, cfg, [buy], [profile_row("U-1")])  # U-9 has no profile row
    sub = resolve(conn, profiles, cat, "U-9", AT, None, cfg, PLAN_DAYS)["subscription_status"]
    assert sub.value == "uncertain" and not sub.usable  # bought 20 Aug, 30 days -> ended 19 Sep
    assert sub.conflicts == ["event:p1:plan_P_MONTH_ended:2026-09-19T10:00:01.000Z"]
    still = resolve(conn, profiles, cat, "U-9", parse_ts("2026-09-15T00:00:00.000Z"), None, cfg, PLAN_DAYS)
    assert still.value("subscription_status") == "active"


def test_renewal_extends_and_trial_expires(tmp_path, conn, cfg):
    events = [
        make_event(
            "p1",
            user_id="U-9",
            event="purchase_completed",
            occurred="2026-08-20T10:00:00.000Z",
            received="2026-08-20T10:00:01.000Z",
            properties={"plan_id": "P_MONTH", "is_trial": False},
        ),
        make_event(
            "r1",
            user_id="U-9",
            event="subscription_renewed",
            occurred="2026-09-19T10:00:00.000Z",
            received="2026-09-19T10:00:01.000Z",
            properties={"plan_id": "P_MONTH", "price_inr": 149},
        ),
        make_event(
            "t1",
            user_id="U-8",
            event="purchase_completed",
            occurred="2026-09-26T10:00:00.000Z",
            received="2026-09-26T10:00:01.000Z",
            properties={"plan_id": "P_TRIAL", "is_trial": True},
        ),
    ]
    profiles, cat = setup(tmp_path, conn, cfg, events, [profile_row("U-1")])
    assert (
        resolve(conn, profiles, cat, "U-9", AT, None, cfg, PLAN_DAYS).value("subscription_status") == "active"
    )
    trial = resolve(conn, profiles, cat, "U-8", AT, None, cfg, PLAN_DAYS)["subscription_status"]
    assert trial.value == "uncertain"  # 3-day trial from 26 Sep ended 29 Sep; no purchase seen after it
    no_pricing = resolve(conn, profiles, cat, "U-8", AT, None, cfg, None)["subscription_status"]
    assert no_pricing.value == "trial_active" and "expiry not checked" in no_pricing.assumption
