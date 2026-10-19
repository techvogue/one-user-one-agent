"""Step 7 "Prove impact": statistics, cohort, intention-to-treat, the misleading comparisons, the pilot re-analysis and an
end-to-end report on a tiny pack (D-044)."""

import csv
import json

import pytest
from conftest import make_event, write_jsonl
from test_harness import build_pack, trig
from test_profiles_catalogue import profile_row

from ott_agent.__main__ import main
from ott_agent.impact import build_cohort, misleading, newcombe, pilot, primary, wilson
from ott_agent.store import connect


def test_wilson_matches_known_values():
    lo, hi = wilson(0, 10)
    assert lo == pytest.approx(0.0, abs=1e-9) and hi == pytest.approx(0.2775, abs=1e-4)
    lo, hi = wilson(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-4) and hi == pytest.approx(0.7634, abs=1e-4)


def test_newcombe_matches_the_published_example():
    # Newcombe (1998), example (a): 56/70 vs 48/80, method 10 -> 0.0524 to 0.3339
    d = newcombe(56, 70, 48, 80)
    assert d["diff"] == pytest.approx(0.2000, abs=1e-4)  # 0.8 - 0.6
    assert d["lo"] == pytest.approx(0.0524, abs=1e-3) and d["hi"] == pytest.approx(0.3339, abs=1e-3)
    same = newcombe(10, 100, 10, 100)
    assert same["diff"] == 0 and same["lo"] == pytest.approx(-same["hi"])


def _store(tmp_path, goals, outcomes):
    conn = connect(tmp_path / "state.sqlite")
    from ott_agent.judge import SCHEMA

    conn.executescript(SCHEMA)
    for gi, user, start in goals:
        conn.execute("INSERT INTO goal_instances (goal_instance_id, user_id, goal_id, goal_name, rule_version, status,"
                     " start_at, deadline, goal_json) VALUES (?, ?, 'G01', 'g', 'v', 'closed', ?, ?, '{}')",
                     (gi, user, start, start))  # fmt: skip
    for gi, user, verdict, seen in outcomes:
        o = {"goal_instance_id": gi, "goal_id": "G01", "verdict": verdict, "exposure": {"pages_shown": seen}}
        conn.execute(
            "INSERT INTO goal_outcomes VALUES (?, ?, 'G01', 0, ?, 'final', 'x', ?)",
            (gi, user, verdict, json.dumps(o)),
        )
    conn.commit()
    return conn


def _decisions(tmp_path, rows):
    with open(tmp_path / "decisions.jsonl", "w") as f:
        f.writelines(json.dumps({"decision_id": f"D-{i}", "viewer_id": user, "arm": arm, "status": status,
                                "trigger": {"deliver_at": f"2026-10-01T0{i % 9}:00:00.000Z"}}) + "\n" for i, (user, arm, status) in enumerate(rows))  # fmt: skip


def test_cohort_is_fixed_before_action_and_counts_everyone(tmp_path):
    _decisions(tmp_path, [("A1", "agent", "STALE_SKIPPED"), ("A1", "agent", "PUBLISHED"), ("A2", "agent", "NO_ACTION"),
                          ("A3", "agent", "PUBLISHED"), ("H1", "holdout", "HOLDOUT_SHADOW"),
                          ("H2", "holdout", "HOLDOUT_SHADOW"), ("S1", "agent", "STALE_SKIPPED"),
                          ("X1", None, "HOLDOUT_SHADOW")])  # fmt: skip
    conn = _store(tmp_path,
                  [("g1", "A1", "2026-10-01T01:00:00.000Z"), ("g1b", "A1", "2026-10-02T01:00:00.000Z"),
                   ("g2", "A2", "2026-10-01T02:00:00.000Z"), ("g4", "H1", "2026-10-01T04:00:00.000Z"),
                   ("g5", "H2", "2026-10-01T05:00:00.000Z")],
                  [("g1", "A1", "SUCCEEDED", 1), ("g1b", "A1", "FAILED", 1), ("g2", "A2", "FAILED", 0),
                   ("g4", "H1", "INCONCLUSIVE", 0), ("g5", "H2", "SUCCEEDED", 0)])  # fmt: skip
    cohort = build_cohort(tmp_path, conn)
    assert set(cohort) == {"A1", "A2", "A3", "H1", "H2", "X1"}  # S1 only had a stale app open
    assert cohort["A1"]["goal_instance_id"] == "g1"  # the first goal, not the later re-plan
    p = primary(cohort)
    assert p["no_goal_at_first_visit"] == {"agent": 1} and p["unknown_arm_excluded"] == 1
    j = p["judged"]
    assert (j["agent"]["k"], j["agent"]["n"]) == (1, 2)  # A2 got no action but still counts (ITT)
    assert (j["holdout"]["k"], j["holdout"]["n"]) == (1, 1)  # inconclusive H1 excluded from judged...
    s = p["sensitivity_all"]
    assert (s["holdout"]["k"], s["holdout"]["n"]) == (1, 2)  # ...and counted as not succeeded in sensitivity


def test_misleading_groups_split_the_agent_arm_by_exposure(tmp_path):
    _decisions(
        tmp_path,
        [("A1", "agent", "PUBLISHED"), ("A2", "agent", "PUBLISHED"), ("H1", "holdout", "HOLDOUT_SHADOW")],
    )
    conn = _store(tmp_path, [("g1", "A1", "2026-10-01T00:00:00.000Z"), ("g2", "A2", "2026-10-01T01:00:00.000Z"),
                             ("g3", "H1", "2026-10-01T02:00:00.000Z")],
                  [("g1", "A1", "SUCCEEDED", 2), ("g2", "A2", "FAILED", 0), ("g3", "H1", "FAILED", 0)])  # fmt: skip
    cohort = build_cohort(tmp_path, conn)
    sec = {u: {"play_7d": u == "A1", "play_days_14d": 0, "paid_14d": False} for u in cohort}
    m = misleading(cohort, sec)
    assert m["agent_page_seen"]["goal"]["rate"] == 1.0 and m["agent_page_not_seen"]["goal"]["rate"] == 0.0
    assert m["holdout"]["goal"]["n"] == 1


def test_pilot_reanalysis_separates_the_biased_headline_from_itt(tmp_path):
    rows = ([("treatment", "true", "G09", "true")] * 8 + [("treatment", "true", "G09", "false")] * 2
            + [("treatment", "false", "G05", "false")] * 40 + [("holdout", "false", "G09", "true")] * 9
            + [("holdout", "false", "G05", "false")] * 41)  # fmt: skip
    (tmp_path / "pilot").mkdir()
    with open(tmp_path / "pilot" / "pilot_outcomes.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["user_id", "arm", "served", "goal_id", "goal_started_at", "succeeded"])
        for i, (arm, served, g, ok) in enumerate(rows):
            w.writerow([f"U{i}", arm, served, g, "2026-09-02T00:00:00.000Z", ok])
    p = pilot(tmp_path)
    head, itt = p["report_headline_served_vs_everyone_else"], p["itt"]
    assert head["agent"]["rate"] == 0.8 and head["holdout"]["rate"] == pytest.approx(9 / 90)  # looks like 8x!
    assert itt["agent"]["rate"] == pytest.approx(8 / 50) and itt["holdout"]["rate"] == pytest.approx(9 / 50)
    assert itt["difference"]["lo"] < 0 < itt["difference"]["hi"]  # randomized: no effect
    assert p["served_goal_mix"] == {"G09": 10}


def test_end_to_end_replay_judge_report_on_a_tiny_pack(tmp_path):
    users = ["U-1", "U-2", "U-3", "U-4"]
    pack = build_pack(tmp_path, [trig(f"t{u}", user=u) for u in users], profiles=[profile_row(u) for u in users],
                      arms={"U-1": "agent", "U-2": "agent", "U-3": "holdout", "U-4": "holdout"})  # fmt: skip
    (pack / "pilot").mkdir()
    with open(pack / "pilot" / "pilot_outcomes.csv", "w", newline="") as f:
        csv.writer(f).writerows([["user_id", "arm", "served", "goal_id", "goal_started_at", "succeeded"],
                                 ["P1", "treatment", "true", "G09", "x", "true"],
                                 ["P2", "holdout", "false", "G09", "x", "false"]])  # fmt: skip
    out = tmp_path / "run"
    assert main(["replay", "--pack", str(pack), "--out", str(out)]) == 0
    (out / "sim").mkdir()
    plays = [
        make_event(
            f"o-{u}",
            user_id=u,
            event="play_started",
            occurred="2026-10-02T10:00:00.000Z",
            received="2026-10-02T10:00:00.000Z",
            properties={"title_id": "F2"},
        )
        for u in ("U-1", "U-3")
    ]
    plays.append(
        make_event(
            "end", user_id="U-9", occurred="2026-10-15T10:00:00.000Z", received="2026-10-15T10:00:00.000Z"
        )
    )
    write_jsonl(out / "sim" / "outcome_events.jsonl", plays)
    assert main(["judge", "--pack", str(pack), "--out", str(out)]) == 0
    assert main(["report", "--pack", str(pack), "--out", str(out), "--seeds", ""]) == 0
    report = (out / "impact_report.md").read_text()
    assert "agent arm vs randomized holdout" in report and "served vs not served" in report
    data = json.loads((out / "impact.json").read_text())
    j = data["primary"]["judged"]
    assert j["agent"]["n"] == 2 and j["holdout"]["n"] == 2 and j["agent"]["k"] == 1 and j["holdout"]["k"] == 1
