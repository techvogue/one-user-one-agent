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
| 3 | Intervention planner (`intervention.py`) | Home page builder (dialect, slug dedupe, variants, availability through TTL), message choice, or `NO_ACTION` | D-009, D-010, D-011 |
| 4 | Verifier | Independent re-derivation of eligibility and every policy rule; can reject or downgrade | rules.md Rule 10, D-012 |
| 5 | Publisher | Writes actions (simulator schema) and decisions; `published_at = deliver_at + measured time` | FR-9, FR-13 |
| 6 | Outcome judge | Succeeded / failed / inconclusive from attributable events; versioned rules; provisional + final verdicts | D-005, D-014 |
| 7 | Impact reporter | ITT agent vs holdout with CI, per goal; pilot re-analysis; latency report | D-016 |
| — | Model provider | `ModelProvider` interface: mock/replay (default) + optional local Ollama | D-003 |
| — | Replay harness (`harness.py`, `tools.py`, `model.py`) | Feeds triggers in `deliver_at` order with live and control events; injects failures; enforces budgets; measures latency; writes outputs | D-017, D-035–D-039 |

## 3. Ownership

- Exactly **one agent per viewer** owns the viewer's single active goal, single active plan and history (FR-2.3).
- No other component can set or change a viewer's goal. Control events (rule correction, withdrawal) do not change goals
  directly: they identify affected viewers and ask **their** agent to re-plan or re-judge.
- Holdout viewers have an agent too, which decides a **shadow goal** but never acts (D-016).

## 4. Planning (Step 2: choose a goal)

Implemented in `src/ott_agent/goals.py` and `src/ott_agent/planner.py` (D-013, D-014, D-019, D-029).

1. **Measurable goals only.** At load (and after every correction) each goal's success rule is checked: the success
   event must be an observable event type and the condition and window defined. G10 (no event) and G11
   (`tv_app_open` never occurs) are disabled with their reasons.
2. **Eligibility from evidence.** Each goal is evaluated from *usable* facts only. Structured fields from goals.json
   (`signed_up_within_days`, `requires_no_prior_event`, `subscription_status`) are applied as written; rules given
   only in text use recorded interpretations (A-G05-1, A-G06-1, A-G07-1 incl. the 14-day recency limit of D-031,
   A-G09-1, A-G12-1; thresholds in `config.py`). Every evaluation returns eligible/reason, evidence references and assumptions.
3. **One goal by priority** (D-019, D-030, `cfg.goal_priority`): G04 → new-user funnel (G01 → G02 → G03) → G07 → G12 →
   G02 (existing users) → G09 → G08 → G06 → G05. The decision records why the goal beat the other eligible ones.
4. **Deadline and signal:** start = commit time, deadline = start + window, success signal from the rule version.
5. **NO_GOAL** is a valid outcome, with the reason for every goal.
6. **One active goal per viewer** (unique index on open goals). Event-driven lifecycle: KEEP while valid; close and
   re-plan when a state condition breaks (status changed, e.g. after a purchase; the "no prior event" happened; a G05
   viewer has paid), the G07 series is no longer available, the rule is no longer measurable, the goal no longer
   exists, or the deadline passes (D-029, D-032). Entry windows are not re-checked for an open goal.
7. **Rule corrections** create a new version and update only the open goals of that goal type (`goal_log` records
   each change); closed goals and other goal types are untouched. If the corrected rule is unmeasurable, those goals
   are closed.
8. `plan_goal` decides, `commit_plan` persists, so the verifier (Step 5) can reject a plan before it is written.

## 4b. Intervention (Step 3: decide the intervention)

Implemented in `src/ott_agent/intervention.py` (D-033, D-034). Platform limits, row types, TTL, templates and copy
rules are read from `config/interventions.json`.

1. **No-action reasons, in order:** `STALE_TRIGGER` (true age > 600 s: not a live visit) → `NO_GOAL` →
   `DIALECT_UNKNOWN` (app dialect missing/invalid) → `PAGE_STILL_LIVE` (same goal kept, same dialect, page < 2 h old)
   → `INSUFFICIENT_CONTENT` (no hero and no row of ≥ 3 valid titles). Otherwise **HOME_PAGE**.
2. **Page content from data, no model.** Candidate titles: in the page dialect, available for the whole 2-hour TTL,
   one language variant per slug (by `app_language`), never flagged, never already finished by the viewer.
   "Popular" = distinct viewers with playback in the last 30 days (only events received by decision time).
3. **Per goal** a hero (G01 campaign title if valid, G02/G09 popular micro-drama, G04/05/06 popular premium, G07 the
   viewer's series, G08 newest release, others most popular) and up to 4 rows from: popular in dialect (optionally
   micro-drama or premium only), new releases (30 days), because you watched (genre overlap with the latest watched
   title), the viewer's top genre, next episode. Rows with < 3 titles are not published.
4. **Reminder messages only for G03 and G12** at 19:00 IST the next day, if the goal is still open an hour later:
   WhatsApp (approved template, catalogue variables) if consented, else push (fixed copy checked for length, forbidden
   phrases and prices); no consent → none; at most one per viewer per IST day. Re-checked at send time (Step 5).
5. In-app messages are not used. Holdout viewers: decided as normal, nothing published.

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
- Time model: **virtual clock** with a modelled worker pool; `published_at = deliver_at + measured compute time + simulated dependency delay + queueing delay` (D-017, D-037).
- Measured on the replay (2,769 triggers): published pages p50 0.84 s, p95 0.84 s, max 2.2 s (model call 0.8 s
  simulated); data-store degradation pushes single decisions to 3.4 s; evening burst (×3) had no queueing with
  4 workers; 0 decisions over 60 s. Budgets: max 2 model calls and ≤ 6 tool calls used per decision (limits 8 / 20).

## 8. Failure recovery

Implemented in `src/ott_agent/tools.py` and `src/ott_agent/harness.py` (D-035 to D-039).

| Failure | Response | Seen in the replay |
|---|---|---|
| Model says a viewer is eligible without evidence | Claim re-checked against evidence and rejected (`unsupported_claim` with the failing check); evidence-based goal kept | U-01991 (G04 "confirmed in billing" → rejected: status is never) |
| Model returns broken JSON | One retry within budget, then rules only | U-02563: 2 calls, page published |
| Model hangs / times out | Cut at 3 s, rules only | U-01090 (holdout shadow) |
| Model provider down (503) | Rules only, page still published | 19 decisions 10:35–10:42 |
| Data store slow / down | Last known facts (≤ 2 h, same dialect) or a generic popular page in the app's dialect; no message, no goal change | 13:10–13:17: 12 generic pages, 1 cached |
| Budget exhausted (8 model / 20 tool calls) | No action, recorded `budget_exceeded` | tested |
| Publishing would be > 60 s late | Not published, recorded `FAILED_LATE` | 0 in the replay; tested |
| Duplicate / late (stale) trigger | `DUPLICATE_IGNORED` / `STALE_SKIPPED`, no model call | 90 / 424 |
| Crash mid-replay | Each replay starts from a fresh store; ingestion is idempotent | — |
| Title withdrawn | Live pages containing it republished, pending messages cancelled | T0001: 11 pages, 3 messages |
| Goal rule corrected | New rule version on open goals of that type only | G03: 0 open at 08:30 |

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
