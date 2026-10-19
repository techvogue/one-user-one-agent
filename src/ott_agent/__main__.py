"""Command-line entry point: python -m ott_agent <command>.

Commands so far:
  load     ingest the pack's 30-day history into the state store and write data_quality_report.json
  replay   run the whole replay day (one command): decisions, actions, latency and quality reports
  judge    after the simulator: ingest its outcome events, judge every goal, store outcomes and learnings
  report   impact: agent arm vs holdout (+ seeds, pilot re-analysis) -> impact_report.md, impact.json
  inspect  show what the agent knows about one viewer at a moment (facts with source, freshness, conflicts)
           and which goal it would choose (dry run)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import replace
from pathlib import Path

from .catalogue import load_catalogue
from .config import Config
from .facts import resolve, save_snapshot
from .goals import GoalCatalogue
from .ingest import ingest_file
from .intervention import PlatformRules, Popularity, decide_intervention
from .planner import plan_goal
from .pricing import load_plan_days
from .profiles import load_profiles
from .quality import build_report, write_report
from .store import connect
from .timeutil import iso, parse_ts


def _config(args: argparse.Namespace) -> Config:
    cfg = Config.from_json(args.config) if args.config else Config()
    return replace(cfg, pack_path=Path(args.pack)) if args.pack else cfg


def cmd_load(args: argparse.Namespace) -> int:
    cfg = _config(args)
    history = cfg.pack_path / "data" / "events.jsonl"
    if not history.exists():
        print(f"error: {history} not found (use --pack)", file=sys.stderr)
        return 2

    out = Path(args.out)
    conn = connect(args.db or out / "state.sqlite")
    t0 = time.perf_counter()
    stats = ingest_file(conn, history, "history", cfg)
    elapsed = time.perf_counter() - t0

    report = build_report(conn, cfg, [stats])
    path = write_report(report, out)
    dup = report["duplicates"]["history"]
    clk = report["clocks"]["history"]
    print(f"ingested {stats.lines} lines in {elapsed:.1f}s: {stats.counts}")
    print(f"  duplicates: {dup['duplicate_event_ids']} ids, payload conflicts: {dup['payload_conflicts']}")
    print(
        f"  skewed clocks: {clk['skewed_clock_users']} users / {clk['skewed_clock_events']} events;"
        f" buffered >1 day: {clk['buffered_over_1day']}; anomalies: {clk['clock_anomalies']}"
    )
    print(f"  report: {path}")
    return 0


def _short(v, width=46) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s if len(s) <= width else s[: width - 1] + "…"


def cmd_inspect(args: argparse.Namespace) -> int:
    cfg = _config(args)
    conn = connect(args.db or Path(args.out) / "state.sqlite")
    trigger = None
    if args.trigger:  # decide as of a replay app open: its delivery time, dialect and deeplink
        path = cfg.pack_path / "replay" / "triggers.jsonl"
        with open(path, encoding="utf-8") as f:
            trigger = next((t for t in map(json.loads, f) if t.get("event_id") == args.trigger), None)
        if trigger is None or trigger.get("user_id") != args.viewer:
            print(
                f"error: trigger {args.trigger} not found for viewer {args.viewer} in {path}", file=sys.stderr
            )
            return 2
    if trigger is not None and not args.at:
        at = parse_ts(trigger["deliver_at"])
    elif args.at:
        at = parse_ts(args.at)
    else:
        latest = conn.execute("SELECT MAX(first_received_at) FROM events").fetchone()[0]
        if latest is None:
            print("error: the store is empty; run `load` first", file=sys.stderr)
            return 2
        at = parse_ts(latest)
    profiles = load_profiles(cfg.pack_path / "data" / "profiles_snapshot.csv")
    catalogue = load_catalogue(
        cfg.pack_path / "data" / "catalogue.csv", cfg.pack_path / "data" / "episodes.csv"
    )
    pricing = cfg.pack_path / "config" / "pricing.json"
    plan_days = load_plan_days(pricing) if pricing.exists() else None  # None: expiry not checked (stated)
    vf = resolve(conn, profiles, catalogue, args.viewer, at, trigger, cfg, plan_days)
    if args.save:
        with conn:
            save_snapshot(conn, vf)
    goals = GoalCatalogue.from_file(cfg.pack_path / "config" / "goals.json")
    plan = plan_goal(conn, vf, at, goals, catalogue, cfg)  # dry run: nothing is committed
    iv_rules = cfg.pack_path / "config" / "interventions.json"
    iv = None
    if iv_rules.exists():
        rules = PlatformRules.from_file(iv_rules)
        popularity = Popularity(cfg.popularity_days).counts(conn, catalogue, at)
        iv = decide_intervention(vf, plan.goal, plan.outcome, catalogue, popularity, at, rules, cfg)
    if args.json:
        out = {**vf.to_dict(), "goal_plan": plan.to_dict(), "intervention": iv.to_dict() if iv else None}
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    print(f"viewer {vf.user_id} as known at {vf.at}  (only events received by then)")
    print(f"{'fact':22} {'value':46} {'source':28} {'freshness':9} use  conflicts / assumption")
    for f in vf.facts.values():
        note = "; ".join(f.conflicts) or (f.assumption or "")
        print(
            f"{f.name:22} {_short(f.value):46} {_short(f.source, 28):28} {f.freshness:9}"
            f" {'yes' if f.usable else 'NO ':3}  {_short(note, 70)}"
        )
    if vf.flags:
        print("flags:", ", ".join(vf.flags))
    print("\ngoal evaluation (dry run, nothing committed)")
    for gid, why in plan.evaluations.items():
        print(
            f"  {gid}  {'ELIGIBLE' if why == 'eligible' else '-':8}  {'' if why == 'eligible' else _short(why, 90)}"
        )
    if plan.goal:
        g = plan.goal
        print(
            f"\n=> {plan.outcome}: {g['goal_id']} {g['name']}  start {g['start']}  deadline {g['deadline']}"
        )
        print(f"   success signal: {g['success_signal']}")
        print(f"   why: {plan.reason}")
        for a in g["assumptions"]:
            print(f"   assumption: {a}")
    else:
        print(f"\n=> {plan.outcome}: {_short(plan.reason, 160)}")
    if iv is not None:
        print(
            "\nintervention (dry run)"
            + ("" if trigger else ": no --trigger, so there is no app open to show a page to")
        )
        print(f"=> {iv.action}: {iv.reason}" + (f"  [{iv.no_action_code}]" if iv.no_action_code else ""))
        if iv.page:
            print(f"   hero: {iv.page['hero_title_id']}")
            for r in iv.page["rows"]:
                print(f"   row {r['row_type']}: {', '.join(r['title_ids'])}")
        if iv.message:
            print(
                f"   message: {iv.message['channel']} at {iv.message['send_at']} -> {iv.message['title_id']}"
            )
        elif iv.message_note:
            print(f"   no message: {iv.message_note}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from .harness import Replay, latency_summary
    from .model import MockProvider, OllamaProvider

    cfg = _config(args)
    if not (cfg.pack_path / "replay" / "triggers.jsonl").exists():
        print(f"error: no replay/triggers.jsonl under {cfg.pack_path} (use --pack)", file=sys.stderr)
        return 2
    if args.provider == "ollama":
        provider = OllamaProvider(args.ollama_model, args.ollama_url, cfg.model_timeout_s)
    else:
        provider = MockProvider.from_pack(cfg.pack_path, cfg.model_latency_s)
    rep = Replay(cfg.pack_path, Path(args.out), cfg, provider).run()
    print(latency_summary(rep))
    print(
        f"outputs in {args.out}/: decisions.jsonl, actions.jsonl, control_log.jsonl, latency_report.json,"
        " data_quality_report.json, viewer_state/"
    )
    return 0


def cmd_judge(args: argparse.Namespace) -> int:
    import csv as _csv
    from collections import Counter

    from .judge import judge_all, update_viewer_state

    cfg = _config(args)
    out = Path(args.out)
    sim = Path(args.sim) if args.sim else out / "sim" / "outcome_events.jsonl"
    if not (out / "state.sqlite").exists() or not sim.exists():
        print(f"error: need {out}/state.sqlite (run `replay`) and {sim} (run the simulator)", file=sys.stderr)
        return 2
    conn = connect(out / "state.sqlite")
    stats = ingest_file(conn, sim, "outcome", cfg)  # same path as all events: dedupe, corrected time
    judged_at = parse_ts(conn.execute("SELECT MAX(first_received_at) FROM events").fetchone()[0])
    catalogue = load_catalogue(
        cfg.pack_path / "data" / "catalogue.csv", cfg.pack_path / "data" / "episodes.csv"
    )
    with open(cfg.pack_path / "replay" / "arms.csv", encoding="utf-8", newline="") as f:
        arms = {r["user_id"]: r["arm"] for r in _csv.DictReader(f)}
    with open(out / "actions.jsonl", encoding="utf-8") as f:
        ours = {json.loads(line)["action_id"] for line in f if line.strip()}
    outcomes = judge_all(conn, catalogue, cfg, judged_at, arms, ours)
    with open(out / "outcomes.jsonl", "w", encoding="utf-8") as f:
        f.writelines(json.dumps(o.to_dict(), ensure_ascii=False) + "\n" for o in outcomes)
    update_viewer_state(out, outcomes)
    print(
        f"ingested {stats.lines} outcome events: {stats.counts}; judged {len(outcomes)} goals as of {iso(judged_at)}"
    )
    for arm in ("agent", "holdout"):
        c = Counter(o.verdict for o in outcomes if o.arm == arm)
        print(f"  {arm:8} {dict(c)}")
    print(f"  stages: {dict(Counter(o.stage for o in outcomes))}")
    print(f"outputs: {out}/outcomes.jsonl, viewer_state/ (outcomes + learned)")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .impact import _pts, _verdict, run_report

    cfg = _config(args)
    seeds = [x for x in (args.seeds or "").split(",") if x]
    data = run_report(cfg.pack_path, Path(args.out), cfg, seeds)
    j = data["primary"]["judged"]
    print(
        f"first goal succeeded: agent {j['agent']['k']}/{j['agent']['n']}, holdout {j['holdout']['k']}/{j['holdout']['n']};"
        f" difference {_pts(j['difference'])} -> {_verdict(j['difference'])}"
    )
    for seed, r in data["seeds"].items():
        print(f"  seed {seed}: {_pts(r['difference'])}")
    print(f"outputs: {args.out}/impact_report.md, impact.json")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ott_agent", description="One User, One Agent")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--pack", help="path to the data pack (default from config)")
        p.add_argument("--out", default="sample_run", help="output directory")
        p.add_argument("--db", help="SQLite path (default <out>/state.sqlite)")
        p.add_argument("--config", help="JSON config overrides")

    p = sub.add_parser("load", help="ingest history and write the data quality report")
    common(p)
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("replay", help="run the whole replay day: decisions, actions, reports")
    common(p)
    p.add_argument(
        "--provider", choices=["mock", "ollama"], default="mock", help="model provider (default: mock)"
    )
    p.add_argument("--ollama-model", default="qwen2.5:3b-instruct")
    p.add_argument("--ollama-url", default="http://localhost:11434")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("judge", help="judge every goal from the simulator's outcome events")
    common(p)
    p.add_argument("--sim", help="outcome_events.jsonl (default <out>/sim/outcome_events.jsonl)")
    p.set_defaults(func=cmd_judge)

    p = sub.add_parser("report", help="impact: agent arm vs holdout, seeds, pilot re-analysis")
    common(p)
    p.add_argument(
        "--seeds", default="seed-a,seed-b,seed-c", help="extra simulator seeds, comma-separated ('' = none)"
    )
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("inspect", help="show what the agent knows about a viewer at a moment")
    common(p)
    p.add_argument("--viewer", required=True, help="user id, e.g. U-00042")
    p.add_argument("--at", help="ISO time (UTC); default: latest event received in the store")
    p.add_argument("--json", action="store_true", help="print the full facts as JSON")
    p.add_argument("--save", action="store_true", help="also store this snapshot in facts_snapshots")
    p.add_argument(
        "--trigger", help="replay trigger event_id: decide as of that app open (dialect, deeplink, time)"
    )
    p.set_defaults(func=cmd_inspect)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s"
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
