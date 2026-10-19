# Known Limitations: One User, One Agent

What was left out or simplified on purpose, **why**, and what a full version would do. Decision references (D-xxx)
point to [doc/decisions.md](doc/decisions.md), where the options considered are recorded.

## Goals

| # | Limitation | Why | A full version would |
|---|---|---|---|
| L-1 | **G10 and G11 are never selected** | No measurable success signal: G10 has none, G11's `tv_app_open` never occurs. Inventing a definition would move the goalposts (D-013) | Agree a definition with the analytics owner; the measurability check then enables it |
| L-2 | **G06 and G07 use our interpretations**: lapse within 30 days (A-G06-1); a series watched 72 h – 14 days ago with episodes left (A-G07-1) | The goal catalogue leaves them unclear (D-014, D-031) | Confirm with the goal owner; thresholds are configurable |
| L-3 | **Lapsed viewers without a lapse event are not eligible for G06** | Recency cannot be proven: lapsed profiles have no `plan_ends_at` | Use billing-system history |
| L-4 | **G01/G03 need a provable signup time**; viewers with neither a profile row nor a signup event (7 in the replay) never get them | First activity only bounds the signup (D-008) | Read signups from the account system |
| L-5 | **An open goal that already succeeded (e.g. the series was resumed) stays open until its deadline** | Success is judged after the simulator (Step 6); status goals (G04–G06) and "first" goals (G01/G02) are re-planned as soon as their condition changes (D-029, D-048) | Judge continuously as events arrive (the judge is a pure function of the events known by T and supports this) |

## Data handling

| # | Limitation | Why | A full version would |
|---|---|---|---|
| L-6 | **Any opt-out event withdraws consent, even one older than the snapshot** | The data has no opt-in event, so re-consent cannot be proven; conservative reading of policy rule 1 | Consume a consent stream with both directions |
| L-7 | **Profile rows without a user id or `updated_at` are ignored** | They cannot be attributed or ordered (D-007); none exist in this pack | Quarantine table + data-owner alert |
| L-8 | **The "event time before signup" plausibility check listed in D-005 is not implemented**; only "sent before occurred" and "implausibly old" are | Found during the final review; low impact (no such events in this pack) | Flag events dated before the viewer's signup as `clock_anomaly` |
| L-9 | **Flagged catalogue titles are quarantined, not auto-cleared**; the instruction scan is a simple pattern match | A tampered entry needs human review; the real defence is that free text never reaches decisions or a model (D-012) | Review workflow; the scan only improves reporting |
| L-10 | **History ingestion takes ~10 s** (row-by-row SQLite inserts) | Acceptable for the replay | Batched inserts |

## Decisions and content

| # | Limitation | Why | A full version would |
|---|---|---|---|
| L-11 | **Thin dialects get thin pages**: d3 has 13 titles; 55 of 905 published pages have a single row | Rows with fewer than 3 valid titles are never published; other dialects are never used (D-011) | Report content gaps per dialect to the content team |
| L-12 | **A `next_episode` row is rare**: it needs 3+ unfinished series in the page dialect | Platform minimum of 3 titles per row; G07 pages carry the series as the hero instead | — |
| L-13 | **Reactions to live events never publish a page** | Without a visit there is nothing to show it in; only purchase, renewal, lapse, opt-out and dialect-change events trigger them (D-048) | — |
| L-14 | **Same-session re-decisions** use a fixed list of state-changing event types | Simple and auditable (D-039) | Learn which changes matter |

## Messaging

| # | Limitation | Why | A full version would |
|---|---|---|---|
| L-15 | **Unknown consent means no push/WhatsApp** | Default-deny is the safe reading (D-008) | Read consent from the source of truth in real time |
| L-16 | **Reminders only for G03 and G12**, at 19:00 IST the next day | Proportionate messaging (D-034) | Test timing and goals against a holdout |
| L-17 | **Push copy is fixed rule-based text** per app language; the optional model copywriter (D-022) is not wired in | Copy that cannot break the rules; the model stays off the critical path | Model-written copy, always checked by the verifier |
| L-18 | **WhatsApp `dialect_name` holds the dialect code** (e.g. `d3`) | The pack has no dialect names; variables may only come from the catalogue or pricing (D-034) | A dialect-name table in the catalogue |
| L-19 | **The send-time re-check only knows events received by the end of the replay day** | Reminders go out the next evening; the replay has no data for the night | Re-check with live data at send time |

## Model

| # | Limitation | Why | A full version would |
|---|---|---|---|
| L-20 | **The optional local Ollama provider was implemented but not run here**; all results use the deterministic mock | The brief requires a mock for evaluators; CPU-only inference takes 3–10 s per call (D-003) | Run a real model off the critical path, with a no-retention agreement if hosted |
| L-21 | **The model only gives a second opinion on the goal** | It is never evidence; the core stays deterministic (D-035) | Use a model only where it measurably reduces uncertainty |

## Real time and infrastructure

| # | Limitation | Why | A full version would |
|---|---|---|---|
| L-22 | **Latency is simulated** on a virtual clock: real compute time is measured; dependency delays, timeouts and queueing are simulated | Real sleeps would make the replay slow and non-deterministic (D-017, D-037) | Load-test against real services |
| L-23 | **Single-process SQLite** | Zero setup for evaluators | Sharded store + key-value idempotency + stream partitioned by viewer (ARCHITECTURE §10) |
| L-24 | **The "skip the model under load" path ran only in its test**: with 4 workers the evening peak never queued | Capacity was sufficient (D-048) | — |
| L-25 | **With a 2 s store timeout, the degraded window fails most reads**: 12 of 13 decisions in it used the generic page | p95 8 s in the control event | Tune the timeout per dependency |

## Verification

| # | Limitation | Why | A full version would |
|---|---|---|---|
| L-26 | **The verifier implements the same documented interpretations** as the planner and shares the event store and thresholds; a misreading of the spec itself would be shared | Separately written code, not a separate spec (D-040) | Real-case tests and the pack simulator are the outside checks |
| L-27 | **0 verifier rejections in the final replay** | The one real disagreement it found (157 decisions, D-041) was fixed at the source; its ability to reject is shown by tests and mutation testing | — |

## Measurement

| # | Limitation | Why | A full version would |
|---|---|---|---|
| L-28 | **No detectable effect: +1.2 pts [−2.5, +4.9]** on one replay day; the study can only detect effects larger than about ±3.7 points | ~720 judged viewers per arm | More replay days |
| L-29 | **Outcomes come from the pack's simulator** | Its behaviour model, not real viewers, decides the effect size | Real outcomes |
| L-30 | **14-day goals (G06, G09) are mostly INCONCLUSIVE** | The data ends on their deadline, so deadline + 24 h is never observed | A longer observation period |
| L-31 | **A per-viewer SUCCEEDED does not mean the agent caused it** (e.g. scripted organic purchases) | Causation is only measured agent arm vs holdout (D-044); learning notes record observations, never causes | — |
| L-32 | **Final verdicts at deadline + 7 days cover ~99 % of late events** | Completeness vs learning speed (D-005) | Longer final window or re-statements |

## Change log

| Date | Change |
|---|---|
| 2026-10-18 | Draft created from decisions D-003 to D-016. |
| 2026-10-18 | L-9, L-10 added after D-017 (tech stack). |
| 2026-10-18 | L-11, L-12 added in the Step 1 review. |
| 2026-10-19 | Final version: grouped by area, every entry re-checked against the current run; three outdated or false entries corrected (the unimplemented before-signup check, the 1-row page count, the G01 count). |
