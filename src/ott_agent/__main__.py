"""Command-line entry point: python -m ott_agent <command>.

Commands so far:
  load     ingest the pack's 30-day history into the state store and write data_quality_report.json
  inspect  show what the agent knows about one viewer at a moment (facts with source, freshness, conflicts)
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
from .ingest import ingest_file
from .pricing import load_plan_days
from .profiles import load_profiles
from .quality import build_report, write_report
from .store import connect
from .timeutil import parse_ts


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
    if args.at:
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
    vf = resolve(conn, profiles, catalogue, args.viewer, at, None, cfg, plan_days)
    if args.save:
        with conn:
            save_snapshot(conn, vf)
    if args.json:
        print(json.dumps(vf.to_dict(), indent=2, ensure_ascii=False))
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

    p = sub.add_parser("inspect", help="show what the agent knows about a viewer at a moment")
    common(p)
    p.add_argument("--viewer", required=True, help="user id, e.g. U-00042")
    p.add_argument("--at", help="ISO time (UTC); default: latest event received in the store")
    p.add_argument("--json", action="store_true", help="print the full facts as JSON")
    p.add_argument("--save", action="store_true", help="also store this snapshot in facts_snapshots")
    p.set_defaults(func=cmd_inspect)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s"
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
