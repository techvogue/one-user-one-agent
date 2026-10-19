# AI Collaboration Log: One User, One Agent

> **Status: living document.** Records how AI tools were used, how their output was checked, and where it was
> corrected or rejected. Required by the brief and scored ("the candidate directs, checks and sometimes rejects
> AI-generated work").

## Tools used

| Tool | Used for |
|---|---|
| Claude Code (Claude Opus) | Reading the brief and data pack, data profiling scripts, drafting docs, proposing options for each decision |

## How AI output is controlled

- **No code without discussion** ([rules.md](doc/rules.md), Rule 0; [D-001](doc/decisions.md)). The assistant proposes
  options with trade-offs; the candidate decides. Every agreed decision is logged in decisions.md.
- **Claims are checked against the data**: every number in the docs comes from a script run on the pack, not from
  the model's memory or assumptions.
- **Data is never instruction**: injected text found in the catalogue was reported as a finding, not acted on.

## Log

| # | Date | Task | AI output | Check | Result |
|---|---|---|---|---|---|
| 1 | 2026-10-18 | Understand the brief and pack | Summary of the brief; data profiling (duplicates, clock skew, profile conflicts, catalogue, replay surprises) | Numbers produced by scripts run on the pack | Accepted |
| 2 | 2026-10-18 | Pilot re-analysis | Showed the report's served-vs-everyone 2.3× uplift is biased; randomized arms give −1.3 pts | Recomputed from `pilot_outcomes.csv` | Accepted; becomes the impact-report method (D-016) |
| 3 | 2026-10-18 | Tech stack | Suggested an agent framework would be unnecessary; proposed plain Python with explicit stages | Discussed trade-offs (auditability, determinism, latency) | Framework **rejected**; stack still pending (P-001) |
| 4 | 2026-10-18 | LLM provider | Initially proposed Anthropic's API as the optional real model | Candidate: it is paid | **Rejected** → free options compared; free hosted tiers rejected on privacy grounds; local Ollama chosen (D-003) |
| 5 | 2026-10-18 | Trap 2: outcome grace period | First proposal: a single 24 h grace period | Checked against the measured buffering-delay distribution: 24 h covers only ~95 % of events, so ~5 % of real successes would be counted as failures | **Corrected** → two-stage verdict, +24 h provisional and +7 days final (D-005) |
| 6 | 2026-10-18 | Trap 6: dialect conflicts | First analysis reported 133 triggers with three different dialects | Re-check found the deeplink parser read the campaign ID instead of the dialect segment | **Corrected** to 56; requirements.md fixed |
| 7 | 2026-10-18 | Traps 1–13 | Options and recommendations for each trap | Each grounded in measured data; candidate reviewed and decided | Accepted (D-004 to D-016) |
| 8 | 2026-10-18 | First code step: skeleton + ingestion (D-004, D-005) | Change proposal, then code: `config`, `timeutil`, `store`, `ingest`, `quality`, CLI `load`, 12 tests | Approved before writing (Rule 0). Tests run on **Python 3.10** (the minimum). A real run on the pack reproduced every number from the independent analysis in data_traps.md (2,176 duplicates, 123 skewed-clock users, 4,319 skewed events, 7,400 events > 1 day late, delay percentiles). Ruff found 5 style issues; fixed and re-tested | Accepted |
| 9 | 2026-10-18 | Fact resolver: profiles, catalogue, per-viewer facts (D-007–D-010, D-012, D-015) | Short change proposal, then `profiles.py`, `catalogue.py`, `facts.py` and 14 tests | Opt-out channel values checked in the data before coding (`push`, `whatsapp`). Real-pack run over all 2,679 unique triggers reproduced the analysis: 30 duplicate profiles, only T0002/T0522 flagged, 178 triggers with no usable page dialect, 56 with three different dialects, 7 malformed deeplinks; ~0.7 ms per resolve. Ruff flagged a nested `if`; simplified and re-tested | Accepted |
| 10 | 2026-10-18 | Step 1 "Understand the viewer": close gaps (freshness rules, stored snapshots, `inspect`, written precedence table) | Freshness classes per fact, `facts_snapshots` table, `inspect` CLI, ARCHITECTURE §5.1 | Running `inspect` on a real viewer (U-04215) exposed a flaw in the agreed "newest row wins" rule: the newest row said `never`, an older row `lapsed`. Rule added (status cannot regress → `uncertain`); affects 14 real users. Also changed profile facts to be dated by `snapshot_at` rather than `updated_at`. 33 tests pass | **Corrected** own earlier rule (D-027) |
| 11 | 2026-10-18 | Full review of Step 1 (logic, code quality, tests) at the candidate's request | Line-by-line review found 7 issues in AI-written code: out-of-order duplicate kept the wrong payload; a bad `received_at` crashed the load and left a partial row; a bad phone time crashed instead of becoming an anomaly; profile times compared as text (format-fragile); `ever_paid` ignored older profile rows; signup chose first-received instead of earliest; `properties: null` crashed. All fixed with a regression test each, plus 7 coverage tests | 45 tests pass on Python 3.10; ruff lint + format clean. **Independent re-implementation** of the facts from the raw files (separate script, no package imports) compared with the resolver for all 2,679 unique triggers × 11 facts: **0 mismatches**. Caveat: both written with AI help, so shared spec misreadings are covered by hand-written real-case tests | **Corrected** (7 bugs) |
| 12 | 2026-10-18 | Manual test run with expected vs actual output | Running `inspect` on real viewers surfaced a rule gap: event-derived `active` never expired. First impact estimate said 7 users; checking each showed all 7 were already handled through the profile (**0 affected**), so the estimate was corrected before reporting. Fix added (D-028) with 3 tests | 48 tests pass; real-pack run: 0 status changes; cross-check still 0 mismatches | Gap fixed; own estimate **corrected** |
| 13 | 2026-10-18 | Step 2: goal planner (`goals.py`, `planner.py`, 28 tests) | First test run: 11 failures. Diagnosed instead of loosening tests: (a) a **test-helper bug** reused line numbers, so restart-safety silently skipped events; (b) `has_played` ignored completions (a viewer with `episode_completed` counted as never played); (c) G12 treated users with recent plays as absent (only `app_open` checked); (d) day counts truncated (41.99 → "41 days"). (b) and (c) fixed in the logic (D-029), (a) in the helper, (d) by one-decimal wording | 76 tests pass; facts cross-check re-run with the new definitions: 0 mismatches on 2,679 × 12 facts; planner dry run on all 1,504 replay users (0.6 ms each) surfaced two design questions (G09 unreachable, G07 dominant) raised to the candidate rather than changed silently | **Corrected** (2 logic gaps, 1 test bug) |
| 14 | 2026-10-18 | Resolve P-004 / P-005 | Explained both in plain terms with examples; candidate chose "1b 2b": G09 above G08 (D-030), G07 limited to series watched within 14 days (D-031). While updating tests, two new test cases of mine were wrong (viewers who never finished an episode correctly get G02 first); fixed the tests, not the code | 78 tests pass; before/after dry run on 1,504 users: G09 0 → 46, G07 920 → 672, NO_GOAL 2 → 7 (all 7 checked individually and legitimate) | Accepted |
| 15 | 2026-10-19 | Step 2 review at the candidate's request ("is it implemented and tested completely?") | Answered honestly "mostly, not completely" with 6 gaps; then a code read found 4 more: a config setting that did nothing (`g09_lookback_days`), G09 counting only `play_started` (inconsistent with D-029), a KeyError if an open goal's ID disappears, an unclear crash on corrections for unknown goals. All fixed (D-032) with 8 tests | 86 tests pass; **independent re-implementation** of eligibility + priority from the raw files matches the planner on 2,679 triggers × 12 goals + chosen goal: 0 mismatches; facts cross-check 0 mismatches | **Corrected** (2 logic gaps, 4 code issues, 3 untested paths) |
| 16 | 2026-10-19 | Step 3: intervention planner (`intervention.py`, 26 tests) | Proposed the design with data facts (micro-drama episodes ≈ 3 min, 640/758 agent viewers consented, evening peak) and three choices; candidate chose "1b 2b 3a" (D-033, D-034). First test run: 3 failures: two **wrong tests** (a "buffered" trigger whose sent_at equalled occurred_at; a wrong "newest release" expectation) fixed in the tests; one **real over-strict rule** (deeplink ids had to look like T1234) relaxed, since safety comes from requiring the title to exist in the catalogue. Real dry run then showed stale triggers mislabelled as no-goal/dialect-unknown (36 cases) → staleness now checked first. Independent recount exposed that the documented **432** stale triggers counted duplicate deliveries: corrected to **424** in the docs (decision log annotated, not rewritten) | 112 tests pass; independent validation of all 2,086 pages and 124 messages from the raw files: **0 violations**; facts and goal cross-checks still 0 mismatches | **Corrected** (1 rule, 1 ordering, 1 doc count, 2 tests) |

## Suggestions rejected (summary for the technical discussion)

1. **Agent framework (LangChain / LangGraph / CrewAI)**: hides control flow, adds latency; auditability matters more.
2. **Paid / free hosted LLM APIs**: cost (paid) and privacy (free tiers may train on inputs).
3. **Single 24 h grace period**: contradicted by the measured delay distribution.
4. **Retry on duplicate trigger**: the duplicate arrives after the viewer has already seen the home page.
5. **Time-window deduplication**: the data already has duplicates 15 min apart.

---

## Change log

| Date | Change |
|---|---|
| 2026-10-18 | Created with entries 1–7. |
| 2026-10-18 | Entry 8 (first code step). |
| 2026-10-18 | Entry 9 (fact resolver). |
| 2026-10-18 | Entry 10 (Step 1 completed). |
| 2026-10-18 | Entry 11 (Step 1 review). |
| 2026-10-18 | Entry 12 (plan expiry gap). |
| 2026-10-18 | Entry 13 (Step 2 goal planner). |
| 2026-10-18 | Entry 14 (P-004, P-005). |
| 2026-10-19 | Entry 15 (Step 2 review). |
| 2026-10-19 | Entry 16 (Step 3 intervention). |
