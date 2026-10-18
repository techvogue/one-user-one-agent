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

## Guide loop step 1: Understand the viewer ✅

Facts collected (signup, subscription, dialect, consent, watch history) with source, time and freshness; conflicts
kept visible; precedence and freshness rules written in ARCHITECTURE.md §5.1 (D-027).

**Verified (review, 2026-10-18):** 48 tests pass (incl. plan-expiry fix, D-028) on Python 3.10; ruff lint + format clean; independent re-implementation
matches the resolver on all 2,679 unique replay triggers × 11 facts (0 mismatches); 7 bugs found and fixed in review.

## Step 2: Build the core

| Task | Status |
|---|---|
| Data loader + deduplication (D-004, D-005) | ✅ history ingestion + quality report; live/trigger ingestion reuses it in the harness |
| Viewer state store with provenance | 🟡 facts with source, freshness, conflicts stored (`facts_snapshots`) + `inspect`; goal/decision/action tables come with the planner and harness |
| Fact resolver with precedence rules (D-007–D-009, D-015) | ✅ profiles.py, catalogue.py, facts.py |
| Goal eligibility + selection (D-013, D-014) | ⏳ |
| Intervention planner (home page, message, no action) (D-009–D-011) | ⏳ |
| Independent verifier (rules.md Rule 10, D-012) | ⏳ |
| Mock model provider (D-003) | ⏳ |

## Step 3: Replay and measure

| Task | Status |
|---|---|
| Replay harness (deliver_at order, live + control events, model faults, latency) | ⏳ |
| Write decisions.jsonl and actions.jsonl | ⏳ |
| Run the simulator and judge outcomes | ⏳ |
| Impact report (agent vs holdout) and latency report | ⏳ |

## Step 4: Test, document, submit

| Task | Status |
|---|---|
| Required tests (late/duplicate trigger, conflicting source, tool failure, corrected goal rule) | ⏳ |
| README, ARCHITECTURE, AI_COLLABORATION, KNOWN_LIMITATIONS (final versions) | 🟡 drafts |
| final_report.md | ⏳ |
| Fresh clone → setup → replay → end-to-end check | ⏳ |

## Final check before submitting (from the guide)

- [ ] The replay runs with one command on a fresh clone, without an API key
- [ ] Holdout users received zero actions
- [ ] No push or WhatsApp in quiet hours, without consent, or more than once per IST day
- [ ] All titles in the right dialect and available; no withdrawn titles after withdrawal
- [ ] Every decision has evidence, confidence reason, assumptions, checks and timing
- [ ] All decisions published within 60 seconds (latency report included)
- [ ] Impact measured agent arm vs holdout arm, with the pilot report's mistake explained
- [ ] Required tests pass
- [ ] All 4 docs written
- [ ] sample_run folder complete
