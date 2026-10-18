start # Architecture: One User, One Agent

> **Status: DRAFT (living document).** Sections marked **[PENDING]** depend on decisions not yet taken (see
> [doc/decisions.md](doc/decisions.md), "Pending"). This file is updated as decisions are made and code is written.

The brief asks this note to cover: **ownership, planning, memory, verification, latency, failure recovery and trust
boundaries**. Each has its own section below.

---

## 1. Overview

One agent per viewer. On each trigger (app open / login) the agent decides, within seconds, whether and how to act,
using only evidence it can cite; an independent verifier can veto the decision; outcomes are judged later from
attributable signals; impact is measured against the randomized holdout.

The core is **deterministic and rule-based**. The language model is optional, off the critical path, and never treated
as evidence ([D-003](doc/decisions.md)).

```
                ┌──────────────────────────── REPLAY HARNESS (virtual clock, D-017)  ────────────────────────────┐
 triggers ─────►│ intake: dedupe (D-004) · event_time + true age (D-005) · stale lane (D-006) · priority lane     │
 live events ──►│ ingest: same ingestion function → canonical events → only the affected viewer's facts update    │
 control evts ─►│ apply: title withdrawn · goal rule corrected · dependency down/slow (inject failures)           │
 model faults ─►│ mock provider reproduces unsupported claims / malformed output / timeouts                      │
                └───────────────────────────────────────────┬──────────────────────────────────────────────────────┘
                                                            ▼
            ┌───────────────────────────── VIEWER AGENT (one owner per viewer) ─────────────────────────────┐
            │ 1 Fact resolver ─► 2 Goal planner ─► 3 Intervention planner ─► 4 VERIFIER ─► 5 Publisher        │
            │   (provenance,       (eligibility      (home page / one        (independent    (actions.jsonl, │
            │    precedence,        from evidence,    message / nothing)      checks, can     decisions.jsonl,│
            │    conflicts)         one goal)                                 veto/downgrade) state update)   │
            └───────────────────────────────────────────────┬───────────────────────────────────────────────┘
                                                            ▼
     simulator (given) ──► outcome events ──► 6 Outcome judge (two-stage verdict, D-005) ──► 7 Impact reporter (ITT, D-016)
                                                            │                                   │
                                                            └────────► viewer state (learnings) ◄┘
```

## 2. Components

| # | Component | Job | Key decisions |
|---|---|---|---|
| 0 | Data loader / ingestion (`ingest.py`, `store.py`, `quality.py`) | Raw delivery log + canonical events (one per `event_id`); corrected `event_time`; same function for history, live events and triggers | D-004, D-005 |
| 1 | Fact resolver (`facts.py`, `profiles.py`, `catalogue.py`) | Builds facts with source, `as_of`, freshness; per-fact precedence; conflicts kept visible | D-007, D-008, D-009, D-015 |
| 2 | Goal planner | Measurability check; eligibility from cited evidence; one goal with start, deadline, success signal; or `NO_GOAL` | D-013, D-014, D-015 |
| 3 | Intervention planner | Home page builder (dialect, slug dedupe, variants, availability through TTL), message choice, or `NO_ACTION` | D-009, D-010, D-011 |
| 4 | Verifier | Independent re-derivation of eligibility and every policy rule; can reject or downgrade | rules.md Rule 10, D-012 |
| 5 | Publisher | Writes actions (simulator schema) and decisions; `published_at = deliver_at + measured time` | FR-9, FR-13 |
| 6 | Outcome judge | Succeeded / failed / inconclusive from attributable events; versioned rules; provisional + final verdicts | D-005, D-014 |
| 7 | Impact reporter | ITT agent vs holdout with CI, per goal; pilot re-analysis; latency report | D-016 |
| — | Model provider | `ModelProvider` interface: mock/replay (default) + optional local Ollama | D-003 |
| — | Replay harness | Feeds triggers in `deliver_at` order with live and control events; injects failures; measures latency | D-017 |

## 3. Ownership

- Exactly **one agent per viewer** owns the viewer's single active goal, single active plan and history (FR-2.3).
- No other component can set or change a viewer's goal. Control events (rule correction, withdrawal) do not change goals
  directly: they identify affected viewers and ask **their** agent to re-plan or re-judge.
- Holdout viewers have an agent too, which decides a **shadow goal** but never acts (D-016).

## 4. Planning

- **Event-driven re-planning**, not on a schedule: a fresh trigger, a purchase, a dialect change, a withdrawal or a
  rule correction can cause a re-plan of the affected viewer only (D-005 Choice 3, D-015).
- Goal selection: measurable goals only (D-013) → eligibility from evidence → one goal by priority **[PENDING: Q7
  goal priority]** → deadline = start + window.
- The active goal persists across triggers until it succeeds, fails, expires or the situation changes.

## 5. Memory (durable per-viewer state)

SQLite is the source of truth, with a JSON export per viewer to `sample_run/viewer_state/` ([D-017](doc/decisions.md), [techstack.md](doc/techstack.md)).

| Store | Content |
|---|---|
| Raw deliveries | Every line as received (audit) |
| Canonical events | One row per `event_id`: `first_received_at`, `event_time`, `delivery_count`, flags |
| Facts | Value, source, `as_of`, freshness, conflicts |
| Goals | Active / closed, rule version, start, deadline, verdicts (versioned) |
| Decisions | Every trigger → exactly one record (incl. no action, duplicate, stale) |
| Actions | Published actions in the simulator schema; cancellations |
| Learnings | Short per-viewer notes from judged outcomes |

Auditability: an evaluator can inspect what the agent knew (facts received by time T), decided and observed for any
viewer at any moment: `python -m ott_agent inspect --viewer U-04215 --at 2026-10-01T10:00:00Z` (snapshots are
stored in `facts_snapshots`).

### 5.1 Facts: precedence, freshness and conflicts (Step 1, "Understand the viewer")

Implemented in `src/ott_agent/facts.py`. Every fact carries `value`, `source`, `as_of`, `age_s`, `freshness`,
`usable`, `evidence`, `conflicts` and `assumption`. Only events **received** by the decision time are used.

**Precedence per fact** (first source that applies wins; every losing value stays in `conflicts`):

| Fact | Precedence (highest first) | Conflict handling | Decision |
|---|---|---|---|
| `signup_at` | profile `signup_at` → earliest `signup_completed` event (by event time) → first-seen event (assumption: "on or before") | — | D-008 |
| `subscription_status` | billing event (purchase / renewal / lapse) with event time **after** the profile row's `updated_at` → profile row (newest `updated_at`) → default `never` (assumption, no profile and no billing) | Becomes **`uncertain`** (not usable) when: profile `never` but a paid event before the snapshot; `never` with a `plan_ends_at`; `never` while an older row of the same user says `active`/`lapsed` (a status cannot regress to never); `active` whose `plan_ends_at` has passed with no renewal seen; `active`/`trial_active` from a billing event whose plan length (pricing.json) has run out with no later renewal | D-007, D-015, D-027, D-028 |
| `ever_paid` | any non-trial purchase or renewal event; or **any** profile row of the user (incl. older duplicates) `active`/`lapsed` | — | D-015, D-027 |
| `push_consent`, `whatsapp_consent` | `notification_opt_out` event for that channel (**any** opt-out, even before the snapshot: no opt-in event exists to prove re-consent) → profile opt-in → default **no consent** (assumption) | profile `true` overridden by an opt-out is listed | Policy rule 1, D-008 |
| `page_dialect` | **trigger's valid `app_dialect` only**; invalid / missing → none (`DIALECT_UNKNOWN`, no page) | profile, older profile rows and deeplink dialects listed | D-009 |
| `message_dialect` | trigger's valid `app_dialect` → latest valid `app_dialect` in events → profile `preferred_dialect` → none (no message) | same | D-009 |
| `app_language` | trigger → latest event → default `hin` (assumption) | — | D-010 |
| `deeplink` | trigger deeplink parsed with a strict pattern; anything else ignored and flagged | invalid dialect in the link flagged | D-009, D-012 |
| watch history (`has_played`, `has_completed_episode`, `last_app_open_at`, `play_ist_days_14d`, `series_in_progress`, `top_genre`) | deduplicated events in corrected event time; content identified by **slug** (both language variants count) | — | D-004, D-005, D-010 |

Duplicate profile rows: newest `updated_at` wins, the other row is kept as a conflict; on a tie the differing fields
are marked uncertain (D-007).

**Freshness per fact** (D-027): `freshness` labels age (`live`, `fresh` ≤ 1 h, `recent` ≤ 48 h, `aging` ≤ 7 d, `old`,
`stale`); `usable = false` means the agent must treat the value as unknown.

| Class | Facts | Rule |
|---|---|---|
| immutable | `signup_at` | never stale |
| live | `page_dialect`, `deeplink` | usable only from the **current trigger** with true age ≤ 600 s (`stale_trigger_s`); otherwise `stale`, not usable |
| state | `subscription_status`, consents, `message_dialect`, `app_language` | events are authoritative at any age; profile-sourced values usable only while the **snapshot** is ≤ 48 h old (`profile_max_age_h`), dated by `snapshot_at` (the value was true at export, even if the row last changed earlier); `uncertain` status is never usable |
| history | `ever_paid`, watch history | always usable (records of the past); age reported |

## 6. Verification

- The verifier runs **after** the decision step and **re-derives** what it checks from the data, rather than trusting
  the planner's output.
- Checks: holdout, measurable goal, eligibility (with evidence), dialect, availability through TTL, withdrawal,
  quarantine, slug uniqueness, row/hero limits, consent (incl. opt-out events), quiet hours, daily cap, approved
  templates, copy limits and forbidden phrases, prices, duplicate action, latency budget.
- Verdicts: approve / downgrade (e.g. drop the message, keep the page) / reject. Results go to `checks.passed/failed`.
- Model output is never evidence or approval (FR-7.4). Unsupported model claims are recorded as such.

## 7. Latency

- Target: publish **within seconds** (the viewer reaches the home page 3–40 s after opening); the hard budget is 60 s,
  and > 5 min is a failure.
- Fast path is rules-only; the LLM is never needed to publish a page (D-003).
- Budgets per decision: ≤ 8 model calls, ≤ 20 tool calls, per-call timeouts (configurable).
- Bursts: fresh triggers in a priority lane, stale ones in a cheap lane (D-006); optional model calls skipped under
  load.
- Time model: **virtual clock** with a modelled worker pool; `published_at = deliver_at + measured compute time + simulated dependency delay + queueing delay` (D-017).

## 8. Failure recovery

| Failure | Response |
|---|---|
| Model provider down / timeout / malformed output | Safe parse → retry within budget → rules-only path → smaller safe action or `NO_ACTION` |
| Data store slow / timing out | Timeouts; cached state; downgrade or `NO_ACTION`; never block past the budget |
| Duplicate / late trigger | Durable idempotency key; stale lane (D-004, D-006) |
| Crash mid-replay | Durable state + idempotency → rerun never publishes twice |
| Title withdrawn | Affected live pages / pending messages found and fixed (D-011) |
| Goal rule corrected | Versioned rules; only open goals of that type re-judged |

## 9. Trust boundaries

| Source | Trusted for | Never trusted for |
|---|---|---|
| Server `received_at` | Ordering, "known at" | — |
| Phone `occurred_at` / `sent_at` | Delay within the phone (difference) | Absolute time (D-005) |
| Profile snapshot | Baseline facts as of `updated_at` | Current status when newer events exist |
| Catalogue typed fields | IDs, dialect, availability, genres | — |
| Catalogue free text, search queries, deeplink strings | Display content only | **Instructions**; decision input (D-012) |
| Language model output | Suggestions to be verified | Evidence, eligibility, approval |
| Config files in the pack | Rules (versioned; corrections applied) | — |

## 10. Scale-out notes (for the technical discussion) [PENDING]
To be written: 1,000 opens per minute, 10 M viewers / 40 dialects, which decisions should never be automated.

---

## Change log

| Date | Change |
|---|---|
| 2026-10-18 | Draft created from requirements.md, rules.md and decisions D-001–D-016. |
| 2026-10-18 | Storage, time model and harness updated after D-017 (tech stack). |
