"""Data-trap summary for the run output (D-047): what was found in this run's data and how it was handled.

Written into data_quality_report.json under "traps". Every number is computed from the data the run actually saw, so
an unseen replay day reports its own counts. Docs: doc/data_traps.md (one section per trap).
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from .catalogue import VALID_DIALECTS
from .facts import parse_deeplink
from .timeutil import iso


def build_traps(r) -> dict:
    """r: a finished Replay (decisions, actions, control log, catalogue, profiles, goals, store)."""
    D, A = r.decisions, r.actions
    status = Counter(d["status"] for d in D)
    first_trig = {}
    for t in r.trigger_rows:
        first_trig.setdefault(t["event_id"], t)
    trig_users = {t["user_id"] for t in first_trig.values()}

    # Trap 4: profile snapshot
    profiles = r.profiles
    dup_users = [u for u, p in profiles.items() if p.losing_rows]
    regress = [u for u, p in profiles.items() if p.get("subscription_status") == "never"
               and any(x.get("subscription_status") in ("active", "lapsed") for x in p.losing_rows)]  # fmt: skip
    last_billing = r.conn.execute(
        "SELECT user_id, MAX(event_time) AS t FROM events WHERE event IN ('purchase_completed',"
        " 'subscription_renewed', 'subscription_lapsed') GROUP BY user_id"
    ).fetchall()
    newer = sum(
        1
        for row in last_billing
        if row["user_id"] in profiles and row["t"] > profiles[row["user_id"]].updated_at
    )

    # Trap 5: new users without a profile row
    no_profile = sorted(u for u in trig_users if u not in profiles)
    with_signup = {
        row[0]
        for row in r.conn.execute("SELECT DISTINCT user_id FROM events WHERE event = 'signup_completed'")
    }

    # Trap 6: dialect sources
    invalid = app_vs_profile = link_vs_app = three_way = 0
    for t in first_trig.values():
        app = t.get("app_dialect")
        prof = profiles.get(t["user_id"])
        pd = prof.get("preferred_dialect") if prof else None
        link = parse_deeplink((t.get("properties") or {}).get("deeplink"))
        ld = link["dialect"] if link else None
        if app not in VALID_DIALECTS:
            invalid += 1
            continue
        app_vs_profile += bool(pd in VALID_DIALECTS and pd != app)
        link_vs_app += bool(ld in VALID_DIALECTS and ld != app)
        three_way += bool(pd in VALID_DIALECTS and ld in VALID_DIALECTS and len({app, pd, ld}) == 3)

    # Trap 7: language variants
    cat = r.catalogue
    by_slug = Counter(t.slug for t in cat.titles.values())
    page_titles = [x for a in A if a["type"] == "home_page" for x in
                   ([a["hero_title_id"]] if a["hero_title_id"] else []) + [y for row in a["rows"] for y in row["title_ids"]]]  # fmt: skip
    variant_titles = [x for x in page_titles if by_slug[cat.titles[x].slug] > 1]

    # Trap 8: availability
    start, end = r.replay_start, r.replay_end
    ending = [
        t.title_id for t in cat.titles.values() if t.available_until and start <= t.available_until < end
    ]
    withdrawals = [{"title_id": c["event"]["title_id"], "at": c["at"],
                    "pages_republished": len(c.get("pages_republished", [])),
                    "messages_cancelled": len(c.get("messages_cancelled", []))}
                   for c in r.control_log if c["type"] == "title_withdrawn"]  # fmt: skip

    # Trap 9: injected instructions
    in_actions = Counter(x for x in page_titles) + Counter(
        a.get("title_id") for a in A if a["type"] != "home_page"
    )
    injected = [{"title_id": tid, "found": why, "handling": "quarantined: never promoted, text never used for "
                 "decisions or sent to a model (D-012)", "times_in_published_actions": in_actions.get(tid, 0)}
                for tid, why in sorted(cat.flagged.items())]  # fmt: skip

    # Traps 10–12: goals
    goals = [{"goal_id": g.goal_id, "name": g.name, "rule_version": g.version, "measurable": g.measurable,
              "reason_not_selectable": g.unmeasurable_reason} for g in r.goals.rules.values()]  # fmt: skip
    interp = Counter()
    for d in D:
        for a in d.get("assumptions", []):
            if a.startswith("[A-G"):
                interp[a.split("]")[0] + "]"] += 1
    status_conflicts = sum(1 for d in D for c in d.get("conflicts", []) if c["fact"] == "subscription_status")

    return {
        "1_duplicate_events": {
            "duplicate_triggers_ignored": status["DUPLICATE_IGNORED"],
            "second_trigger_same_visit": status["SAME_SESSION"],
            "handling": "first-received copy kept; see 'duplicates' above (D-004)",
        },
        "2_late_events_wrong_clocks": {
            "handling": "event_time = received_at - (sent_at - occurred_at); ordering and "
            "freshness by received_at; see 'clocks' above (D-005)"
        },
        "3_offline_buffered_app_opens": {
            "stale_triggers_skipped": status["STALE_SKIPPED"],
            "limit_s": r.cfg.stale_trigger_s,
            "handling": "history only: no model call, no page, no message (D-006)",
        },
        "4_profile_snapshot": {
            "users_with_two_rows": len(dup_users),
            "never_after_paid_contradictions": len(regress),
            "users_with_billing_newer_than_snapshot": newer,
            "decisions_resolving_a_status_conflict": status_conflicts,
            "handling": "newest updated_at wins; newer billing events override; contradictions -> "
            "uncertain, not usable (D-007, D-015, D-027, D-028)",
        },
        "5_new_users_without_profile": {
            "trigger_viewers_without_profile": len(no_profile),
            "of_which_signup_event_seen": sum(1 for u in no_profile if u in with_signup),
            "handling": "facts from events; unknown consent = no messages (D-008)",
        },
        "6_dialect_conflicts": {
            "triggers_with_invalid_app_dialect": invalid,
            "app_differs_from_profile": app_vs_profile,
            "deeplink_differs_from_app": link_vs_app,
            "three_different_dialects": three_way,
            "handling": "page in the app's current setting; invalid -> no page; conflicts listed on "
            "each decision (D-009)",
        },
        "7_language_variants": {
            "slugs_with_several_title_ids": sum(1 for v in by_slug.values() if v > 1),
            "published_page_titles_from_variant_slugs": len(variant_titles),
            "handling": "content identity = content_slug; one variant per page, by app language (D-010)",
        },
        "8_availability_windows": {
            "titles_ending_during_replay": len(ending),
            "withdrawals": withdrawals,
            "handling": "available at publish and for the whole 2 h page life; withdrawals "
            "republish live pages and cancel messages (D-011)",
        },
        "9_injected_instructions": {
            "flagged_titles": injected,
            "handling": "ignored as instruction and logged here and as a WARNING at load",
        },
        "10_unmeasurable_goals": {
            "goals": goals,
            "corrections_applied": r.config_versions["goal_corrections"],
            "handling": "success event must be observable and the condition defined (D-013)",
        },
        "11_unclear_goal_rules": {
            "interpretations_used_in_decisions": dict(interp),
            "handling": "A-G06-1 (lapse within 30 days), A-G07-1 (series watched 72 h-14 d ago), "
            "recorded as assumptions (D-014, D-031)",
        },
        "12_freshest_source": {
            "handling": "billing events newer than the profile override it; plan expiry from "
            "pricing.json; status/consent freshness labelled on every fact (D-015, D-027, D-028)"
        },
        "13_pilot_report": {
            "handling": "re-analysed with the randomized arms in impact_report.md section 9 (D-044)"
        },
        "window": {"from": iso(start), "to": iso(end + timedelta(0))},
    }
