"""Impact measurement (Step 7: prove impact; D-016, D-044).

Agent arm vs randomized holdout, intention-to-treat, on a cohort fixed before any action. Also: goal-independent
secondary outcomes, robustness across simulator seeds, the misleading comparisons (labelled as such) and the pilot
re-analysis. Writes impact_report.md and impact.json.
"""

from __future__ import annotations

import csv
import json
import math
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from .timeutil import iso, ist_day, parse_ts

FRESH_EXCLUDED = {"STALE_SKIPPED", "DUPLICATE_IGNORED", "SAME_SESSION"}
Z = 1.96


# --- statistics ------------------------------------------------------------------------------------------------


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    den = 1 + Z * Z / n
    centre = (p + Z * Z / (2 * n)) / den
    half = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / den
    return (centre - half, centre + half)


def newcombe(k1: int, n1: int, k2: int, n2: int) -> dict:
    """Difference p1 - p2 with the Newcombe hybrid score 95 % interval (method 10)."""
    if not n1 or not n2:
        return {"diff": None, "lo": None, "hi": None}
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson(k1, n1)
    l2, u2 = wilson(k2, n2)
    d = p1 - p2
    return {
        "diff": d,
        "lo": d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2),
        "hi": d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2),
    }


def mean_diff(a: list[float], b: list[float]) -> dict:
    """Difference of means with a Welch normal-approximation 95 % interval."""
    if len(a) < 2 or len(b) < 2:
        return {"diff": None, "lo": None, "hi": None}
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    return {"diff": ma - mb, "lo": ma - mb - Z * se, "hi": ma - mb + Z * se, "mean_a": ma, "mean_b": mb}


def compare(k1, n1, k2, n2) -> dict:
    return {
        "agent": {"k": k1, "n": n1, "rate": k1 / n1 if n1 else None, "ci": wilson(k1, n1)},
        "holdout": {"k": k2, "n": n2, "rate": k2 / n2 if n2 else None, "ci": wilson(k2, n2)},
        "difference": newcombe(k1, n1, k2, n2),
    }


# --- cohort and outcomes ---------------------------------------------------------------------------------------


def build_cohort(out: Path, conn: sqlite3.Connection) -> dict:
    """Cohort fixed before any action (D-044 point 1): viewers with a fresh trigger; first goal per viewer."""
    with open(out / "decisions.jsonl", encoding="utf-8") as f:
        decisions = [json.loads(x) for x in f if x.strip()]
    first: dict[str, dict] = {}
    for d in decisions:
        if d["status"] not in FRESH_EXCLUDED and d["viewer_id"] not in first:
            first[d["viewer_id"]] = d
    first_goal = {}
    for r in conn.execute(
        "SELECT goal_instance_id, user_id FROM goal_instances ORDER BY user_id, start_at, rowid"
    ):
        first_goal.setdefault(r["user_id"], r["goal_instance_id"])
    outcomes = {
        r["goal_instance_id"]: json.loads(r["outcome_json"])
        for r in conn.execute("SELECT * FROM goal_outcomes")
    }
    cohort = {}
    for user, d in first.items():
        gi = first_goal.get(user)
        cohort[user] = {
            "arm": d.get("arm"),
            "first_decision": d,
            "t0": parse_ts(d["trigger"]["deliver_at"]),
            "goal_instance_id": gi,
            "outcome": outcomes.get(gi) if gi else None,
        }
    return cohort


def secondary(conn: sqlite3.Connection, cohort: dict) -> dict[str, dict]:
    """Goal-independent outcomes per viewer after their first visit (D-044 point 5)."""
    res = {}
    for user, c in cohort.items():
        t0 = c["t0"]
        rows = conn.execute(
            "SELECT event, event_time, payload_json FROM events WHERE user_id = ? AND event_time > ? AND event_time <= ?"
            " AND event IN ('play_started', 'purchase_completed')",
            (user, iso(t0), iso(t0 + timedelta(days=14))),
        ).fetchall()
        plays = [parse_ts(r["event_time"]) for r in rows if r["event"] == "play_started"]
        paid = [
            r
            for r in rows
            if r["event"] == "purchase_completed"
            and not (json.loads(r["payload_json"]).get("properties") or {}).get("is_trial")
        ]
        res[user] = {
            "play_7d": any(t <= t0 + timedelta(days=7) for t in plays),
            "play_days_14d": len({ist_day(t) for t in plays}),
            "paid_14d": bool(paid),
        }
    return res


def primary(cohort: dict) -> dict:
    arms = {"agent": [], "holdout": []}
    no_goal, unknown = Counter(), 0
    for c in cohort.values():
        if c["arm"] not in arms:
            unknown += 1
            continue
        if c["outcome"] is None:
            no_goal[c["arm"]] += 1
            continue
        arms[c["arm"]].append(c["outcome"])

    def count(arm, judged_only):
        rows = [o for o in arms[arm] if not judged_only or o["verdict"] != "INCONCLUSIVE"]
        return sum(o["verdict"] == "SUCCEEDED" for o in rows), len(rows)

    by_goal = {}
    for gid in sorted({o["goal_id"] for a in arms.values() for o in a}):
        ka = sum(1 for o in arms["agent"] if o["goal_id"] == gid and o["verdict"] == "SUCCEEDED")
        na = sum(1 for o in arms["agent"] if o["goal_id"] == gid and o["verdict"] != "INCONCLUSIVE")
        kh = sum(1 for o in arms["holdout"] if o["goal_id"] == gid and o["verdict"] == "SUCCEEDED")
        nh = sum(1 for o in arms["holdout"] if o["goal_id"] == gid and o["verdict"] != "INCONCLUSIVE")
        by_goal[gid] = compare(ka, na, kh, nh)
    return {
        "cohort_size": {a: sum(1 for c in cohort.values() if c["arm"] == a) for a in ("agent", "holdout")},
        "with_goal": {a: len(arms[a]) for a in arms},
        "no_goal_at_first_visit": dict(no_goal),
        "unknown_arm_excluded": unknown,
        "inconclusive": {a: sum(o["verdict"] == "INCONCLUSIVE" for o in arms[a]) for a in arms},
        "judged": compare(*count("agent", True), *count("holdout", True)),
        "sensitivity_all": compare(*count("agent", False), *count("holdout", False)),
        "by_goal": by_goal,
    }


def misleading(cohort: dict, sec: dict) -> dict:
    """Served vs not served (labelled misleading, D-044 point 7)."""
    groups = defaultdict(list)
    for user, c in cohort.items():
        o = c["outcome"]
        if c["arm"] == "holdout":
            groups["holdout"].append((user, o))
        elif c["arm"] == "agent":
            seen = bool(o and o["exposure"]["pages_shown"])
            groups["agent_page_seen" if seen else "agent_page_not_seen"].append((user, o))

    def rate(rows, key):
        if key == "goal":
            judged = [o for _, o in rows if o and o["verdict"] != "INCONCLUSIVE"]
            k = sum(o["verdict"] == "SUCCEEDED" for o in judged)
            return {"k": k, "n": len(judged), "rate": k / len(judged) if judged else None}
        k = sum(sec[u][key] for u, _ in rows)
        return {"k": k, "n": len(rows), "rate": k / len(rows) if rows else None}

    return {g: {"goal": rate(rows, "goal"), "play_7d": rate(rows, "play_7d")} for g, rows in groups.items()}


def prior_activity(conn: sqlite3.Connection, cohort: dict) -> dict[str, int]:
    res = {}
    for user, c in cohort.items():
        n = conn.execute(
            "SELECT COUNT(DISTINCT substr(event_time, 1, 10)) FROM events WHERE user_id = ?"
            " AND event = 'play_started' AND event_time < ? AND event_time >= ?",
            (user, iso(c["t0"]), iso(c["t0"] - timedelta(days=14))),
        ).fetchone()[0]
        res[user] = n
    return res


# --- pilot -----------------------------------------------------------------------------------------------------


def pilot(pack: Path) -> dict:
    with open(pack / "pilot" / "pilot_outcomes.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    def k(rs):
        return sum(r["succeeded"] == "true" for r in rs), len(rs)

    t = [r for r in rows if r["arm"] == "treatment"]
    h = [r for r in rows if r["arm"] == "holdout"]
    served = [r for r in t if r["served"] == "true"]
    others = [r for r in rows if not (r["arm"] == "treatment" and r["served"] == "true")]
    goals = sorted({r["goal_id"] for r in rows})
    return {
        "itt": compare(*k(t), *k(h)),
        "report_headline_served_vs_everyone_else": compare(*k(served), *k(others)),
        "served_vs_holdout": compare(*k(served), *k(h)),
        "unserved_treatment": dict(zip(("k", "n"), k([r for r in t if r["served"] != "true"]))),
        "served_goal_mix": dict(Counter(r["goal_id"] for r in served)),
        "by_goal": {
            g: compare(*k([r for r in t if r["goal_id"] == g]), *k([r for r in h if r["goal_id"] == g]))
            for g in goals
        },
        "served_within_goal": {
            g: {
                "served": dict(zip(("k", "n"), k([r for r in served if r["goal_id"] == g]))),
                "unserved": dict(
                    zip(("k", "n"), k([r for r in t if r["goal_id"] == g and r["served"] != "true"]))
                ),
                "holdout": dict(zip(("k", "n"), k([r for r in h if r["goal_id"] == g]))),
            }
            for g in goals
        },
    }


# --- robustness across simulator seeds ---------------------------------------------------------------------------


def seed_run(pack: Path, out: Path, seed: str, cfg, catalogue, arms: dict) -> dict:
    """Re-simulate the same actions with another seed on a copy of the store, re-judge, return the ITT result."""
    from .ingest import ingest_file
    from .judge import judge_all
    from .store import connect

    d = out / "seeds" / seed
    d.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            str(pack / "simulator" / "simulate_outcomes.py"),
            "--pack",
            str(pack),
            "--actions",
            str(out / "actions.jsonl"),
            "--out",
            str(d),
            "--seed",
            seed,
        ],
        check=True,
        capture_output=True,
    )
    db = d / "state.sqlite"
    shutil.copy(out / "state.sqlite", db)
    conn = connect(db)
    with conn:
        conn.execute("DELETE FROM events WHERE source = 'outcome'")
        conn.execute("DELETE FROM event_deliveries WHERE source = 'outcome'")
        conn.execute("DROP TABLE IF EXISTS goal_outcomes")
    ingest_file(conn, d / "outcome_events.jsonl", "outcome", cfg)
    judged_at = parse_ts(conn.execute("SELECT MAX(first_received_at) FROM events").fetchone()[0])
    with open(out / "actions.jsonl", encoding="utf-8") as f:
        ours = {json.loads(x)["action_id"] for x in f if x.strip()}
    judge_all(conn, catalogue, cfg, judged_at, arms, ours)
    cohort = build_cohort(out, conn)
    res = primary(cohort)["judged"]
    sec = secondary(conn, cohort)
    pa = [u for u, c in cohort.items() if c["arm"] == "agent"]
    ph = [u for u, c in cohort.items() if c["arm"] == "holdout"]
    res["play_7d"] = compare(
        sum(sec[u]["play_7d"] for u in pa), len(pa), sum(sec[u]["play_7d"] for u in ph), len(ph)
    )
    conn.close()
    shutil.rmtree(d)  # keep only the numbers
    return res


# --- report ----------------------------------------------------------------------------------------------------


def _pct(x) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.1f} %"


def _pts(d: dict) -> str:
    if d.get("diff") is None:
        return "n/a"
    return f"{100 * d['diff']:+.1f} pts [{100 * d['lo']:+.1f}, {100 * d['hi']:+.1f}]"


def _row(label: str, c: dict) -> str:
    a, h = c["agent"], c["holdout"]
    return (
        f"| {label} | {a['k']}/{a['n']} = {_pct(a['rate'])} | {h['k']}/{h['n']} = {_pct(h['rate'])} |"
        f" {_pts(c['difference'])} |"
    )


def _verdict(d: dict) -> str:
    if d.get("diff") is None:
        return "cannot be estimated"
    if d["lo"] > 0:
        return "a positive effect (the 95 % interval excludes zero)"
    if d["hi"] < 0:
        return "a negative effect (the 95 % interval excludes zero)"
    return "no detectable effect (the 95 % interval includes zero)"


def run_report(pack: Path, out: Path, cfg, seeds: list[str]) -> dict:
    from .catalogue import load_catalogue
    from .store import connect

    pack, out = Path(pack), Path(out)
    conn = connect(out / "state.sqlite")
    if not conn.execute("SELECT name FROM sqlite_master WHERE name = 'goal_outcomes'").fetchone():
        raise SystemExit("error: run `judge` first (no goal outcomes in the store)")
    cohort = build_cohort(out, conn)
    sec = secondary(conn, cohort)
    prim = primary(cohort)
    mis = misleading(cohort, sec)
    prior = prior_activity(conn, cohort)
    agent = [u for u, c in cohort.items() if c["arm"] == "agent"]
    hold = [u for u, c in cohort.items() if c["arm"] == "holdout"]

    def comp(key):
        return compare(sum(sec[u][key] for u in agent), len(agent), sum(sec[u][key] for u in hold), len(hold))

    secondary_res = {
        "play_7d": comp("play_7d"),
        "paid_14d": comp("paid_14d"),
        "play_days_14d": mean_diff(
            [sec[u]["play_days_14d"] for u in agent], [sec[u]["play_days_14d"] for u in hold]
        ),
    }
    with open(pack / "replay" / "arms.csv", encoding="utf-8", newline="") as f:
        arms = {r["user_id"]: r["arm"] for r in csv.DictReader(f)}
    # D-049 (1): who is counted and why
    with open(out / "decisions.jsonl", encoding="utf-8") as f:
        first_any: dict[str, dict] = {}
        for x in f:
            if x.strip():
                d = json.loads(x)
                first_any.setdefault(d["viewer_id"], d)
    counted = {
        a: {
            "in_arms_csv": sum(v == a for v in arms.values()),
            "any_trigger": sum(arms.get(u) == a for u in first_any),
            "fresh_trigger_cohort": sum(c["arm"] == a for c in cohort.values()),
        }
        for a in ("agent", "holdout")
    }
    # D-049 (2): goal-independent outcome over ALL triggered viewers (incl. those with only stale app opens)
    everyone = {u: {"t0": parse_ts(d["trigger"]["deliver_at"])} for u, d in first_any.items()}
    sec_all = secondary(conn, everyone)
    ta = [u for u in everyone if arms.get(u) == "agent"]
    th = [u for u in everyone if arms.get(u) == "holdout"]
    all_triggered_play_7d = compare(
        sum(sec_all[u]["play_7d"] for u in ta), len(ta), sum(sec_all[u]["play_7d"] for u in th), len(th)
    )
    # D-049 (3): the biased comparison on our own data: served (page published at the first visit) vs everyone else
    served = {u for u in agent if cohort[u]["first_decision"]["status"] == "PUBLISHED"}

    def judged(users):
        rows = [
            cohort[u]["outcome"]
            for u in users
            if cohort[u]["outcome"] and cohort[u]["outcome"]["verdict"] != "INCONCLUSIVE"
        ]
        return sum(o["verdict"] == "SUCCEEDED" for o in rows), len(rows)

    served_vs_else = compare(*judged(served), *judged([u for u in cohort if u not in served]))
    agent_goal = [u for u in agent if cohort[u]["outcome"]]
    published = sum(cohort[u]["first_decision"]["status"] == "PUBLISHED" for u in agent)
    seen = sum(bool(cohort[u]["outcome"]["exposure"]["pages_shown"]) for u in agent_goal)
    exposure = {
        "agent_cohort": len(agent),
        "page_published_at_first_visit": published,
        "agent_with_goal": len(agent_goal),
        "page_seen_in_goal_window": seen,
        "seen_rate": seen / len(agent_goal) if agent_goal else None,
    }
    itt = prim["judged"]["difference"]
    on_served = None
    if itt["diff"] is not None and exposure["seen_rate"]:
        r = exposure["seen_rate"]
        on_served = {"diff": itt["diff"] / r, "lo": itt["lo"] / r, "hi": itt["hi"] / r}
    groups = {"agent_page_seen": [], "agent_page_not_seen": [], "holdout": []}
    for u, c in cohort.items():
        if c["arm"] == "holdout":
            groups["holdout"].append(prior[u])
        elif c["arm"] == "agent":
            o = c["outcome"]
            groups["agent_page_seen" if o and o["exposure"]["pages_shown"] else "agent_page_not_seen"].append(
                prior[u]
            )
    prior_means = {g: (sum(v) / len(v) if v else None) for g, v in groups.items()}

    catalogue = load_catalogue(pack / "data" / "catalogue.csv", pack / "data" / "episodes.csv")
    seed_results = {}
    for s in seeds:
        seed_results[s] = seed_run(pack, out, s, cfg, catalogue, arms)
    pil = pilot(pack)
    data = {
        "primary": prim,
        "secondary": secondary_res,
        "exposure": exposure,
        "effect_on_seen": on_served,
        "misleading_comparisons": mis,
        "counted": counted,
        "all_triggered_play_7d": all_triggered_play_7d,
        "served_vs_everyone_else": served_vs_else,
        "prior_play_days_14d_mean": prior_means,
        "seeds": seed_results,
        "pilot": pil,
    }
    (out / "impact.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    (out / "impact_report.md").write_text(_markdown(data, cfg), encoding="utf-8")
    return data


def _markdown(d: dict, cfg) -> str:
    p, s, e = d["primary"], d["secondary"], d["exposure"]
    j = p["judged"]
    L = []
    L += [
        "# Impact report: agent arm vs randomized holdout",
        "",
        (
            "_Generated by `python -m ott_agent report` from the replay, the pack's simulator and the outcome judge. "
            "Method: [doc/decisions.md D-044](../doc/decisions.md)._"
        ),
        "",
    ]
    L += [
        "## 1. Result",
        "",
        (
            f"On the primary metric (share of viewers whose first goal succeeded, intention-to-treat), the agent arm "
            f"reached **{_pct(j['agent']['rate'])}** and the holdout **{_pct(j['holdout']['rate'])}**: a difference of "
            f"**{_pts(j['difference'])}** (95 % interval). That is **{_verdict(j['difference'])}**."
        ),
        "",
        (
            f"Goal-independent check: any play in the 7 days after the first visit, agent "
            f"{_pct(s['play_7d']['agent']['rate'])} vs holdout {_pct(s['play_7d']['holdout']['rate'])}, "
            f"{_pts(s['play_7d']['difference'])}: {_verdict(s['play_7d']['difference'])}."
        ),
        "",
        (
            f"What this study can detect: with {j['agent']['n']} and {j['holdout']['n']} judged viewers, the 95 % "
            f"interval is about ±{100 * (j['difference']['hi'] - j['difference']['lo']) / 2:.1f} points wide, so a "
            f"true effect smaller than that cannot be told apart from zero with one replay day."
            if j["difference"].get("diff") is not None
            else ""
        ),
        "",
    ]
    L += [
        "## 2. Cohort (fixed before any action)",
        "",
        (
            "Every viewer with at least one live (fresh) app open on the replay day, in either arm. The same rule decides "
            "who is counted in both arms, so the agent cannot influence the comparison by choosing whom to serve. The "
            "goal is the **first** goal decided for the viewer (holdout viewers get a shadow goal from the same "
            "planner, never acted on)."
        ),
        "",
        "| | Agent | Holdout |",
        "|---|---|---|",
        *(
            [
                (
                    f"| In `arms.csv` (randomized) | {d['counted']['agent']['in_arms_csv']} | "
                    f"{d['counted']['holdout']['in_arms_csv']} |"
                ),
                (
                    f"| Opened the app on the replay day (any trigger) | {d['counted']['agent']['any_trigger']} | "
                    f"{d['counted']['holdout']['any_trigger']} |"
                ),
            ]
            if d.get("counted")
            else []
        ),
        f"| Viewers in the cohort (a live, fresh app open) | {p['cohort_size']['agent']} | {p['cohort_size']['holdout']} |",
        *(
            [
                "| Cohort as a share of the arm (balance check) | "
                + " | ".join(
                    _pct(p["cohort_size"][a] / d["counted"][a]["in_arms_csv"])
                    if d["counted"][a]["in_arms_csv"]
                    else "n/a"
                    for a in ("agent", "holdout")
                )
                + " |"
            ]
            if d.get("counted")
            else []
        ),
        f"| With a goal at the first visit | {p['with_goal']['agent']} | {p['with_goal']['holdout']} |",
        (
            f"| No goal at the first visit | {p['no_goal_at_first_visit'].get('agent', 0)} | "
            f"{p['no_goal_at_first_visit'].get('holdout', 0)} |"
        ),
        (
            f"| Goal outcome inconclusive (data ends before deadline + 24 h) | {p['inconclusive']['agent']} | "
            f"{p['inconclusive']['holdout']} |"
        ),
        "",
        (
            "**Why not every viewer in `arms.csv`:** the simulator only produces outcomes for viewers who opened the app "
            "on the replay day; for the others there is nothing to measure and nothing the agent could have done. The "
            "cohort filter (a live app open) is decided before any action and is the same rule in both arms, so it "
            "keeps the comparison randomized; the shares above show the arms stay balanced. Viewers the agent decided "
            "not to act on, or whose page was never seen, are counted (intention-to-treat). Section 5 repeats the "
            "goal-independent outcome over **all** viewers who opened the app, including those whose app opens were "
            "all stale."
        )
        if d.get("counted")
        else "",
        "",
    ]
    L += [
        "## 3. Primary metric and sensitivity",
        "",
        "| Metric | Agent | Holdout | Difference (95 % interval) |",
        "|---|---|---|---|",
        _row("First goal succeeded, judged goals only (primary)", j),
        _row("First goal succeeded, inconclusive counted as not succeeded", p["sensitivity_all"]),
        "",
        "Intervals: Wilson per arm, Newcombe hybrid score for the difference.",
        "",
    ]
    L += [
        "## 4. By goal (small samples: read the intervals, not the point estimates)",
        "",
        "| Goal | Agent | Holdout | Difference (95 % interval) |",
        "|---|---|---|---|",
    ]
    for gid, c in p["by_goal"].items():
        L.append(_row(gid, c))
    L += [
        "",
        "## 5. Goal-independent outcomes (same cohort, intention-to-treat)",
        "",
        "| Outcome | Agent | Holdout | Difference (95 % interval) |",
        "|---|---|---|---|",
        _row("Any play within 7 days of the first visit", s["play_7d"]),
        _row("Any paid purchase within 14 days", s["paid_14d"]),
        *(
            [
                _row(
                    "Any play within 7 days, **all** viewers who opened the app (sensitivity)",
                    d["all_triggered_play_7d"],
                )
            ]
            if d.get("all_triggered_play_7d")
            else []
        ),
    ]
    md = s["play_days_14d"]
    if md.get("diff") is not None:
        L.append(
            f"| Mean play days within 14 days | {md['mean_a']:.2f} | {md['mean_b']:.2f} | "
            f"{md['diff']:+.2f} days [{md['lo']:+.2f}, {md['hi']:+.2f}] |"
        )
    L += [
        "",
        "## 6. Exposure",
        "",
        (
            f"- Agent-arm viewers in the cohort: {e['agent_cohort']}; a page was published at their first visit for "
            f"{e['page_published_at_first_visit']}."
        ),
        (
            f"- Of {e['agent_with_goal']} agent viewers with a goal, **{e['page_seen_in_goal_window']}** "
            f"({_pct(e['seen_rate'])}) actually saw one of our pages in the goal window."
        ),
        "",
    ]
    if d["effect_on_seen"]:
        es = d["effect_on_seen"]
        L += [
            (
                f"- Secondary estimate, *effect on viewers who saw a page* = ITT ÷ seen rate = "
                f"{_pts(es)}. This assumes a viewer who saw no page was not affected at all; it is shown for "
                f"completeness and is not the headline."
            ),
            "",
        ]
    m, pr = d["misleading_comparisons"], d["prior_play_days_14d_mean"]

    def mrow(label, g):
        x = m.get(g, {}).get("goal", {})
        y = m.get(g, {}).get("play_7d", {})
        return (
            f"| {label} | {x.get('k', 0)}/{x.get('n', 0)} = {_pct(x.get('rate'))} | "
            f"{_pct(y.get('rate'))} | {pr.get(g) if pr.get(g) is None else f'{pr[g]:.2f}'} |"
        )

    L += [
        '## 7. Why "served vs not served" is misleading (shown for illustration only)',
        "",
        "| Group | First goal succeeded | Any play within 7 days | Play days in the 14 days **before** the visit |",
        "|---|---|---|---|",
        mrow("Agent arm, saw our page", "agent_page_seen"),
        mrow("Agent arm, did not see our page", "agent_page_not_seen"),
        mrow("Holdout (randomized comparison group)", "holdout"),
        "",
        (
            "Whether a viewer *sees* our page depends on the viewer: they must come back while the page is live, in the "
            "same dialect. Viewers who come back are, by definition, the more active ones; the last column shows they "
            'were already more active **before** the agent did anything. Comparing "saw a page" with "did not" (or '
            "with everyone else) therefore mixes the agent's effect with who those viewers already were: selection "
            "bias. Only the randomized comparison in sections 1–5 is free of it, because the arm was assigned by coin "
            "flip before the agent acted and every viewer counts in the arm they were assigned to."
        ),
        "",
    ]
    if d.get("served_vs_everyone_else"):
        sv = d["served_vs_everyone_else"]
        L += [
            (
                "**The same trap on our own replay.** The pilot report's comparison (served vs everyone else) computed on "
                "our data, next to the randomized result:"
            ),
            "",
            "| Comparison | Group A | Group B | Difference (95 % interval) |",
            "|---|---|---|---|",
            (
                f"| Served (page published at the first visit) vs everyone else (**biased**) | "
                f"{sv['agent']['k']}/{sv['agent']['n']} = {_pct(sv['agent']['rate'])} | "
                f"{sv['holdout']['k']}/{sv['holdout']['n']} = {_pct(sv['holdout']['rate'])} | {_pts(sv['difference'])} |"
            ),
            _row("Agent arm vs holdout arm, randomized (**correct**)", j),
            "",
            (
                f"The biased comparison shows {_verdict(sv['difference'])}; the randomized one shows "
                f"{_verdict(j['difference'])}. The gap is who gets served, not what serving does."
            ),
            "",
        ]
    L += [
        "## 8. Robustness across simulator seeds",
        "",
        "Same published actions, re-simulated with other seeds and re-judged (the evaluators use their own seed).",
        "",
        "| Seed | First goal succeeded: agent vs holdout | Difference (95 %) | Any play in 7 days: difference |",
        "|---|---|---|---|",
        (
            f"| default (this sample run) | {_pct(j['agent']['rate'])} vs {_pct(j['holdout']['rate'])} | "
            f"{_pts(j['difference'])} | {_pts(s['play_7d']['difference'])} |"
        ),
    ]
    for seed, r in d["seeds"].items():
        L.append(
            f"| {seed} | {_pct(r['agent']['rate'])} vs {_pct(r['holdout']['rate'])} | {_pts(r['difference'])}"
            f" | {_pts(r['play_7d']['difference'])} |"
        )
    runs = [j["difference"], s["play_7d"]["difference"]]
    for r in d["seeds"].values():
        runs += [r["difference"], r["play_7d"]["difference"]]
    runs = [x for x in runs if x.get("diff") is not None]
    excl = sum(1 for x in runs if x["lo"] > 0 or x["hi"] < 0)
    pos = sum(1 for x in runs if x["diff"] > 0)
    L += [
        "",
        (
            f"Across {len(runs)} intervals (primary and goal-independent metric, {len(d['seeds']) + 1} seeds), "
            f"{pos} point estimates are positive and {excl} interval(s) exclude zero. At 95 % confidence about one "
            f"interval in twenty excludes zero by chance alone, so a single such interval is not evidence of an effect; "
            f"consistent small positive estimates suggest at most a small effect that one replay day cannot confirm."
        ),
    ]
    pil = d["pilot"]
    it, hl = pil["itt"], pil["report_headline_served_vs_everyone_else"]
    L += [
        "",
        "## 9. The earlier pilot, re-analysed",
        "",
        "| Comparison | Treatment / served | Control | Difference (95 % interval) |",
        "|---|---|---|---|",
        (
            f"| Report headline: served vs everyone else (**biased**) | {hl['agent']['k']}/{hl['agent']['n']} = "
            f"{_pct(hl['agent']['rate'])} | {hl['holdout']['k']}/{hl['holdout']['n']} = {_pct(hl['holdout']['rate'])} | "
            f"{_pts(hl['difference'])} |"
        ),
        (
            f"| Randomized arms, intention-to-treat (**correct**) | {it['agent']['k']}/{it['agent']['n']} = "
            f"{_pct(it['agent']['rate'])} | {it['holdout']['k']}/{it['holdout']['n']} = {_pct(it['holdout']['rate'])} | "
            f"{_pts(it['difference'])} |"
        ),
        "",
        (
            f"The headline compares {hl['agent']['n']} hand-picked served viewers with everyone else. The served group "
            f"is dominated by {max(pil['served_goal_mix'], key=pil['served_goal_mix'].get)} (served goal mix: "
            f"{pil['served_goal_mix']}), whose holdout success rate is "
            f"{_pct(pil['by_goal'][max(pil['served_goal_mix'], key=pil['served_goal_mix'].get)]['holdout']['rate'])}: "
            f'it succeeds for most viewers anyway; and "everyone else" includes the treatment viewers the agent chose '
            f"*not* to serve. The randomized comparison shows {_verdict(it['difference'])}."
        ),
        "",
        "| Goal | Treatment | Holdout | Difference (95 %) |",
        "|---|---|---|---|",
    ]
    for gid, c in pil["by_goal"].items():
        L.append(_row(gid, c))
    harm = [
        g
        for g, c in pil["by_goal"].items()
        if c["difference"].get("hi") is not None and c["difference"]["hi"] < 0
    ]
    if harm:
        L += [
            "",
            (
                f"Possible harm signal in the pilot: {', '.join(harm)} did worse in the treatment arm with an interval "
                "below zero. One subgroup out of several can do this by chance; it is a reason to look, not a proof."
            ),
        ]
    L += [
        "",
        "## 10. Limitations of this measurement",
        "",
        "- One replay day; per-goal samples are small and their intervals wide.",
        "- 14-day goals (G06, G09) are mostly inconclusive: the data ends on their deadline.",
        "- Outcomes come from the pack's simulator; its behaviour model, not real viewers, decides the effect size.",
        "- The cohort excludes viewers whose only app opens were stale or duplicates (same rule in both arms).",
        "",
    ]
    return "\n".join(L)
