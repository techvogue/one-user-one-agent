# Work Plan and Progress

| Item | Value |
|---|---|
| Assignment received | 2026-10-18, 12:00 |
| Deadline (4 calendar days) | **2026-10-22, 12:00** |
| Effort budget | 10–12 hours |

Follows the step-by-step plan from the assignment guide. Every implementation step needs discussion and approval first
([rules.md](rules.md), Rule 0). Status: ✅ done · 🟡 in progress · ⏳ not started.

## Step 1: Understand

| Task | Status | Where |
|---|---|---|
| Read the brief and the data pack README | ✅ | requirements.md §1–3 |
| Read all config files (goals, interventions, pricing, messaging policy) | ✅ | requirements.md §3.2, §6–7; rules.md Rule 10 |
| Explore the data (duplicates, timestamps, profile duplicates, dialect conflicts, variants) | ✅ | data_traps.md |
| List every data issue | ✅ | data_traps.md (13 traps, D-004 to D-016) |
| Recompute the pilot uplift with the randomized arms | ✅ | data_traps.md Trap 13 |
| Write the docs skeleton (README, ARCHITECTURE, AI_COLLABORATION, KNOWN_LIMITATIONS) | ✅ | repo root (drafts) |
| Agree the tech stack | ✅ | techstack.md, D-017 |
| Answer the remaining open questions | ✅ | requirements.md §14 (all answered, D-021 to D-026) |
| git init | ✅ | D-026 |
| Set up the repo structure | ✅ folders | D-018 (pyproject + package added with the first code step) |

## Replay surprises (guide §9) ✅

All ten checked against the real run (D-048 closed the one gap: purchases are now acted on when they arrive: 31
reactions, 7 goals re-planned at the purchase time; bursts are reported in latency_report.json → load).

## Guide loop step 1: Understand the viewer ✅

Facts collected (signup, subscription, dialect, consent, watch history) with source, time and freshness; conflicts
kept visible; precedence and freshness rules written in ARCHITECTURE.md §5.1 (D-027).

**Verified (review, 2026-10-18):** 48 tests pass (incl. plan-expiry fix, D-028) on Python 3.10; ruff lint + format clean; independent re-implementation
matches the resolver on all 2,679 unique replay triggers × 11 facts (0 mismatches); 7 bugs found and fixed in review.

## Guide loop step 2: Choose a goal ✅

One measurable goal from evidence with deadline and success signal, or NO_GOAL with reasons; one active goal per
viewer; re-planned on situation change or deadline; rule corrections touch only open goals of that type (D-029).
**Verified (review, 2026-10-19):** 86 tests pass; ruff lint + format clean; independent re-implementation of
eligibility (12 goals) and goal choice matches the planner on all 2,679 unique replay triggers (0 mismatches); facts
cross-check re-run (0 mismatches). Dry run on 1,504 replay users: 1,497 goals, 7 NO_GOAL (each checked); G07 672 ·
G02 365 · G05 144 · G12 108 · G08 106 · G09 45 · G06 31 · G01 23 · G04 2 · G03 1; G10/G11 never chosen.

## Guide loop step 3: Decide the intervention ✅

Home page (main lever), one reminder message for G03/G12 only, or no action with a reason; only content that exists,
is available for the page's lifetime and is in the dialect the viewer will see (D-033, D-034).
**Verified:** 112 tests pass; real dry run on all 2,679 unique replay triggers: 2,086 pages, 593 no-actions
(424 stale, 159 dialect unknown, 10 no goal), 124 reminders (G12; 63 WhatsApp, 61 push); every page and message
checked independently against the raw catalogue, profiles, opt-outs and interventions.json: **0 violations**;
3.6 ms per trigger (goal + intervention). Steps 1–2 cross-checks re-run: 0 mismatches.

## Guide loop step 4: Act within budget ✅

One-command replay; simulated clock; budgets 8 model / 20 tool calls counted per decision; 60 s publish limit;
fallbacks for model faults/outage and data-store degradation (D-035–D-039).
**Verified:** 131 tests pass; replay of all 2,769 triggers: 0 decisions over 60 s (pages p50 0.84 s, max 2.2 s);
independent output validator: 0 violations (incl. 11/11 pages with withdrawn T0001 republished); **pack simulator:
0 policy problems, 907/907 pages accepted, 828 shown**; two runs identical except publish timestamps (≤ 8 ms).

## Guide loop step 5: Verify before acting ✅

Separately written verifier (imports only config + date parsing) re-derives every check from raw data; runs before
every publish, for holdout shadows, before republishing, and at send time; verdicts approve / downgrade / reject.
**Verified:** 167 tests pass (33 verifier tests: one broken rule each; 2 harness tests: rejection stops publishing,
downgrade drops only the message); mutation test: 6/6 disabled checks caught. First real replay: the verifier rejected
157 G07 goals built on quarantined titles: a real planner bug, fixed (D-041) → 0 rejections. Replay: 0 decisions
over 60 s; independent validators 0 violations; pack simulator 0 policy problems, 905/905 pages accepted.

## Guide loop step 6: Close the loop ✅

Every goal (agent + holdout shadow) judged SUCCEEDED / FAILED / INCONCLUSIVE from attributable signals in its window,
by its rule version; stages provisional/final; exposure recorded; learning notes saved in the viewer's state.
**Verified:** 178 tests pass (11 judge tests incl. the required corrected-goal-rule test); mutation test with bytecode
caching disabled: 7/7 broken judge rules caught (and the verifier's 6/6 re-confirmed); independent re-judging of all
1,487 goals from the raw files: 0 mismatches. Replay outcomes: agent 111 succeeded / 608 failed / 30 inconclusive;
holdout 101 / 610 / 27 (comparison is Step 7).

## Guide loop step 7: Prove impact ✅

Agent arm vs randomized holdout, intention-to-treat on a cohort fixed before any action; Newcombe intervals; per goal;
goal-independent outcomes; three extra simulator seeds; served-vs-not shown as misleading with evidence of selection;
pilot re-analysed (D-044).
**Verified:** 184 tests pass (6 impact tests incl. Newcombe's published example and an end-to-end replay → judge →
report run); mutation test 4/4; headline recomputed independently from the output files: identical.
**Result:** agent 111/719 vs holdout 101/711, +1.2 pts [-2.5, +4.9]: no detectable effect; consistent small positive estimates across 4 seeds.

## Step 2: Build the core

| Task | Status |
|---|---|
| Data loader + deduplication (D-004, D-005) | ✅ history ingestion + quality report; live/trigger ingestion reuses it in the harness |
| Viewer state store with provenance | 🟡 facts with source, freshness, conflicts stored (`facts_snapshots`) + `inspect`; goal/decision/action tables come with the planner and harness |
| Fact resolver with precedence rules (D-007–D-009, D-015) | ✅ profiles.py, catalogue.py, facts.py |
| Goal eligibility + selection (D-013, D-014) | ✅ goals.py, planner.py (D-029, D-030, D-031) |
| Intervention planner (home page, message, no action) (D-009–D-011) | ✅ intervention.py (D-033, D-034) |
| Independent verifier (rules.md Rule 10, D-012) | ✅ verifier.py (D-040, D-041) |
| Mock model provider (D-003) | ✅ model.py: mock reproduces model_faults.jsonl; optional Ollama (D-035) |

## Step 3: Replay and measure

| Task | Status |
|---|---|
| Replay harness (deliver_at order, live + control events, model faults, latency) | ✅ harness.py (D-035–D-039, D-048) |
| Write decisions.jsonl and actions.jsonl | ✅ (+ control_log, latency_report, viewer_state) |
| Run the simulator and judge outcomes | ✅ judge.py (D-042, D-043) |
| Impact report (agent vs holdout) and latency report | ✅ impact.py, harness latency report (D-044) |

## Step 4: Test, document, submit

| Task | Status |
|---|---|
| Required tests (late/duplicate trigger, conflicting source, tool failure, corrected goal rule) | ✅ 202 tests (README "Required tests") |
| README, ARCHITECTURE, AI_COLLABORATION, KNOWN_LIMITATIONS (final versions) | ✅ final (D-050); one-command `run` added |
| final_report.md | ✅ generated by `run` (D-052) |
| Fresh clone → setup → replay → end-to-end check | ✅ identical outputs (see final check) |

## Final check before submitting (from the guide)

- [x] The replay runs with one command on a fresh clone, without an API key (2026-10-19: clone → uv setup → `run` in 54 s, 202 tests pass; outputs identical to sample_run except measured compute time)
- [x] Holdout users received zero actions (simulator: 0 holdout actions)
- [x] No push or WhatsApp in quiet hours, without consent, or more than once per IST day (simulator: 0 policy problems)
- [x] All titles in the right dialect and available; no withdrawn titles after withdrawal (validator + verifier)
- [x] Every decision has evidence, confidence reason, assumptions, checks and timing (D-045: 0 gaps in 2,769 records)
- [x] All decisions published within 60 seconds (latency report included; max 2.2 s)
- [x] Impact measured agent arm vs holdout arm, with the pilot report's mistake explained
- [x] Required tests pass (202)
- [x] All 4 docs written (D-050; every claim re-checked against the current run)
- [x] sample_run folder complete (decisions, actions, no-action reasons, viewer_state, outcomes, latency, impact, final report; generated by `run`)
