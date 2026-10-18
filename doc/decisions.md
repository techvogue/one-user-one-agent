# Decision Log: One User, One Agent

Every important decision is recorded here **after** it has been discussed and agreed. Proposals that are still
under discussion go in **Pending**. Nothing in Pending may be implemented (see [rules.md](rules.md), Rule 0).

Use this log in the technical discussion: it shows what was decided, when, by whom, and why.

## How to add a decision

```
### D-XXX: <short title>
- Date:         YYYY-MM-DD
- Status:       Accepted | Superseded by D-YYY | Rejected
- Decided by:   <candidate / candidate after AI proposal>
- Context:      <the problem and constraints>
- Options:      <options considered, one line each>
- Decision:     <what was chosen>
- Why:          <reasons, linked to requirement IDs>
- Consequences: <trade-offs accepted, follow-ups>
- AI input:     <what an AI assistant suggested, and whether it was accepted / changed / rejected>
```

Rules:
- IDs are sequential and never reused. A changed decision gets a **new** entry, and the old one is marked
  `Superseded by D-YYY`. Never edit history.
- Link requirement IDs from [requirements.md](requirements.md) where relevant.

---

## Accepted

### D-001: No code without discussion; docs first
- Date: 2026-10-18
- Status: Accepted
- Decided by: candidate
- Context: Take-home assignment where AI tools are allowed but the candidate must direct and check all work (10 % of
  the score is "Use of AI tools").
- Options: (a) let the AI assistant implement freely; (b) discuss first, implement only after explicit approval.
- Decision: (b). No code is written or changed without discussion and explicit approval of that specific change.
  Requirements and design decisions are documented before coding.
- Why: Keeps the candidate in control, makes every line explainable in the technical discussion, and produces an
  honest AI collaboration log.
- Consequences: Slower start; every change needs a short proposal (template in rules.md).
- AI input: none. This was the candidate's own rule.

### D-002: Keep a decision log
- Date: 2026-10-18
- Status: Accepted
- Decided by: candidate
- Context: Many design choices (stack, precedence rules, thresholds, goal interpretations) need to be explained
  later in ARCHITECTURE.md and the 60-minute discussion.
- Decision: Record every important decision in this file, using the template above.
- Why: Traceability and auditability. It also feeds ARCHITECTURE.md and AI_COLLABORATION.md.
- Consequences: Each agreed decision adds an entry here.
- AI input: none.

### D-003: LLM provider: mock by default, local Ollama as the real model
- Date: 2026-10-18
- Status: Accepted
- Decided by: candidate after AI proposal
- Context: The brief requires a mock/replay mode so evaluators need no API key (FR-8.1). Anthropic's API is paid.
  The privacy rule (rules.md Rule 3, messaging_policy.md §7) forbids sending pack data to services that retain or
  train on it. Hardware: 12-core CPU, 14 GB RAM (~4 GB free), integrated AMD GPU, no CUDA → CPU-only inference.
- Options:
  - Anthropic Claude API: paid → rejected.
  - Google Gemini free tier: free-tier inputs may be used to improve Google's products → violates privacy → rejected.
  - Groq / OpenRouter / Hugging Face free tiers: retention/training policy unclear or provider-dependent, rate
    limits → rejected as too risky.
  - **Local model via Ollama**: free, data never leaves the machine → chosen.
  - Mock only, no real LLM: viable, but a real local adapter proves the provider abstraction works.
- Decision:
  1. `ModelProvider` abstraction with a **mock/replay provider as the default** (deterministic, reproduces
     `model_faults.jsonl`, no key needed).
  2. An **optional Ollama adapter** for a real local model. Model **[TO CONFIRM]**: Qwen 2.5 3B Instruct (Q4,
     ~2 GB), configurable. Alternatives if it runs poorly: Llama 3.2 3B, Qwen3 4B.
  3. No hosted LLM APIs.
- Why: Free, private, offline and reproducible; it fits the privacy rule and the "no personal API key" requirement
  (NFR-4, NFR-8).
- Consequences:
  - CPU inference takes ~3–10 s per short JSON answer, so the **LLM must stay off the critical path**: the home-page
    decision is rules-only. The LLM is limited to checkable tasks (e.g. push copy, a second-opinion goal suggestion),
    and its output always goes through the verifier (FR-7.4, FR-8.3) **[scope of use TO CONFIRM]**.
  - Model latency counts against the decision budget; slow or failed calls trigger the deterministic fallback.
  - Ollama installation and the model download are a setup step that needs its own approval (Rule 0).
- AI input: AI recommended a local Ollama model over free hosted tiers on privacy grounds; candidate accepted.

### D-004: Duplicate events and trigger idempotency (Trap 1)
- Date: 2026-10-18
- Status: Accepted
- Decided by: candidate after AI proposal
- Context: 2,176 duplicate event IDs in history, 90 in triggers, 7 in live events; copies differ only in delivery
  timestamps and arrive up to 15 min apart; 207 sessions produce two trigger IDs (login + app_open) for one visit.
  Full analysis: [data_traps.md, Trap 1](data_traps.md#trap-1-duplicate-events-with-the-same-event_id-).
- Options: see data_traps.md §1.3 (five choices, with the alternatives rejected and why).
- Decision:
  1. Keep the **first-received copy** as the truth; store `last_received_at`, `delivery_count`.
  2. **One ingestion function** with a **durable SQLite unique key** on `event_id`, used for history, live events and
     triggers; a raw layer keeps every delivery for audit.
  3. A duplicate trigger creates **no decision or action**, only a `DUPLICATE_IGNORED` record with `duplicate_of`.
  4. **One decision per visit** (`user_id + session_id`, `SAME_SESSION`), re-planning if the state changed in
     between.
  5. Duplicates with different payloads: keep the first, flag `payload_conflict`, log it.
  6. A generated `data_quality_report.json` makes all of this visible.
- Why: idempotent triggers and durable, auditable state (Real-time reliability, Evidence and state); generic for the
  unseen replay day (NFR-6). Covers required test T-1.
- Consequences: needs SQLite (part of pending P-001); ~45–60 min of the budget including tests.
- AI input: AI proposed the options and recommendation; candidate reviewed the alternatives and accepted.
  Rejected alternatives (time window, retry-on-duplicate, silent drop) are recorded in data_traps.md.

### D-005: Late events and wrong phone clocks (Trap 2)
- Date: 2026-10-18
- Status: Accepted
- Decided by: candidate
- Context: 123 users have phone clocks off by up to 29.5 h (2,963 events would land on the wrong IST day); 7,400
  history events arrived > 1 day late (max 9 days); 457 of 600 live replay events actually happened before the
  replay day. Full analysis: [data_traps.md, Trap 2](data_traps.md#trap-2-late-events-and-wrong-phone-clocks-).
- Options: see data_traps.md §2.3 (six choices, with the alternatives rejected and why).
- Decision:
  1. "When it happened" = corrected `event_time = received_at − (sent_at − occurred_at)`.
  2. Process and audit in `received_at` order; every fact stores both times plus the raw phone times.
  3. A late event recomputes only that viewer's affected facts and is tagged `late_arrival`.
  4. Impossible clock values fall back to `received_at`, are flagged `clock_anomaly`, and lower confidence.
  5. Which time each kind of logic uses: as tabled in data_traps.md §2.3, Choice 5.
  6. Two-stage outcome verdict: provisional at deadline + 24 h, final at deadline + 7 days; later events annotated
     as `late_evidence` without changing the final verdict; both values configurable.
- Why: The grace periods come from the measured delay distribution (24 h covers ~95 %, 7 days ~99 %). A single 24 h
  grace period was considered and rejected because it would count ~5 % of real successes as failures.
  Supports Evidence and state, Real-time reliability, Measurement (FR-1.3, FR-4, FR-11, NFR-6).
- Consequences: Each fact carries more fields; outcome judging needs verdict versioning.

### D-006: Offline-buffered app opens (Trap 3)
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Context: 432 triggers are > 10 min old by true age (238 > 1 day), 236 of them in one 04:00 UTC burst.
  [data_traps.md, Trap 3](data_traps.md#trap-3-offline-buffered-app-opens-days-old-)
- Decision: A trigger with true age (`deliver_at − event_time`) > **600 s** (configurable) is ingested as history only:
  no action, `STALE_SKIPPED` / `STALE_TRIGGER` recorded. Fresh triggers get a priority lane during bursts.
- Why: The session is over, so a page cannot be seen and a message would rest on old evidence; matches how the
  simulator defines a visit.
- Rejected: no threshold; 120-min threshold; re-engagement message; naive age.

### D-007: Stale profile snapshot and duplicate rows (Trap 4)
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Context: 30 users with two rows (dialect/status differ); 28 users with billing events newer than their row;
  contradictions (5 `never` + `plan_ends_at`). [Trap 4](data_traps.md#trap-4-stale-profile-snapshot-users-with-two-rows-)
- Decision: Newest `updated_at` wins; the other row is a visible conflict (ties → field marked uncertain). The snapshot
  is a baseline "as of `updated_at`"; newer events override per fact; contradictions → `uncertain`.
- Rejected: first row; field-wise merge; dropping users; trusting or ignoring the snapshot entirely.

### D-008: Brand-new users without a profile row (Trap 5)
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Context: 103 replay trigger users have no profile (87 sign up live that day; 7 have no signup event at all).
  [Trap 5](data_traps.md#trap-5-brand-new-users-without-a-profile-row-)
- Decision: Build facts from events with explicit assumptions: signup from `signup_completed` (else first-seen as a
  lower bound), status `never` (assumption), **consent unknown → no push/WhatsApp**, dialect from the app setting.
  G01/G03 only with a signup event; G02 allowed.
- Rejected: skipping new users; waiting for the next snapshot; asking the LLM to infer facts.

### D-009: Dialect conflicts (Trap 6)
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Context: 178 triggers with invalid app dialect (`None`/`d7`/`NA`); 788 app ≠ profile; 145 deeplink ≠ app; 56 with
  three different valid dialects. [Trap 6](data_traps.md#trap-6-dialect-conflicts-app-setting--profile--deeplink-)
- Decision: Page dialect = **trigger's valid app setting** (the only page the app requests). Invalid app dialect → no
  page, `DIALECT_UNKNOWN`. Message dialect: latest valid app dialect → profile → no message. Deeplink = content hint
  only (same dialect, available), never a dialect source. All conflicts listed with their resolution.
- Rejected: profile; deeplink; majority vote; falling back to an old app dialect for pages.

### D-010: Language variants (Trap 7)
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Context: 514 slugs have Hindi + English variants, otherwise identical. [Trap 7](data_traps.md#trap-7-language-variants-sharing-a-content_slug-)
- Decision: Content identity = slug (history, progress, success). Pages deduplicate by slug and pick the variant
  matching `app_language`, falling back to the other.
- Rejected: always Hindi; random; showing both.

### D-011: Availability windows and withdrawals (Trap 8)
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Context: 21 titles end during the replay day; T0001 withdrawn at 07:00 UTC; d3 has only 13 available slugs.
  [Trap 8](data_traps.md#trap-8-title-availability-windows-and-withdrawals-)
- Decision: A title must be available at `published_at` **and through the page's 120-min TTL**. On withdrawal: stop
  using it, find live pages and scheduled messages containing it, republish or cancel only those, record the
  affected viewers. Rows with < 3 valid titles are not published; nothing valid → `INSUFFICIENT_CONTENT`.
- Rejected: checking only at load; ignoring live pages; padding with other dialects; re-running everything.

### D-012: Injected instructions in data (Trap 9)
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Context: T0002 and T0522 descriptions contain instructions aimed at AI agents.
  [Trap 9](data_traps.md#trap-9-injected-instructions-in-catalogue-descriptions-)
- Decision: **Structural isolation**: free-text fields never reach the decision logic or the LLM. A pattern detector
  logs `INJECTION_FLAGGED` (visibility only). Flagged titles are quarantined pending human review. Title names used
  in copy are validated; deeplinks parsed strictly.
- Rejected: sanitise-then-send to the LLM; an LLM-based detector as the defence.

### D-013: Unmeasurable goals (Trap 10)
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Context: G11's `tv_app_open` never occurs and the simulator never emits it; G10 has no success event.
  [Trap 10](data_traps.md#trap-10-goals-with-no-measurable-success-event-)
- Decision: A generic measurability check at load and after each rule correction; failing goals are disabled with a
  reason. No invented definitions.
- Rejected: hard-coding the IDs; inventing a G10 metric; keeping them as always-inconclusive.

### D-014: Interpretation of unclear goal rules (Trap 11)
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Context: G06 "recently lapsed" undefined (all lapsed users have empty `plan_ends_at`; 221 of 800 have a lapse
  event); G07 eligibility is free text. [Trap 11](data_traps.md#trap-11-two-goals-with-unclear-rules-)
- Decision: `A-G06-1`: a `subscription_lapsed` event within 30 days (configurable) and no later purchase.
  `A-G07-1`: started show/micro-drama slug, episodes not completed remain, no play of that slug for ≥ 72 h; success =
  `play_started` on the same slug. Clarifications for G04, G05, G09, G12 as in data_traps.md §11.3.
- Rejected: all lapsed users; 14 days; `plan_ends_at`; "unwatched = never started".

### D-015: Freshest source for signup and subscription (Trap 12)
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Context: Snapshot outdated for 28 users; 38 live purchases arrive ~2 min after a decision.
  [Trap 12](data_traps.md#trap-12-freshest-source-for-signup-date-and-subscription-status-)
- Decision: Per-fact precedence: billing events after the profile's `updated_at` override it; expired `plan_ends_at` or
  contradictions → `uncertain` (no status-based goals). A live purchase updates state at once, closes the affected
  goal, cancels pending messages that no longer make sense, and triggers a re-plan.
- Rejected: profile always wins; newest source always wins.

### D-016: Impact measurement and pilot re-analysis (Trap 13)
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Context: Pilot headline 40.1 % vs 17.7 % (served vs everyone else); randomized arms give 18.3 % vs 19.6 %,
  −1.3 pts [−3.8, +1.2]. [Trap 13](data_traps.md#trap-13-pilot-report-uses-a-biased-comparison-)
- Decision: Primary metric = **intention-to-treat** (all agent-arm vs all holdout-arm triggered users) with a 95 % CI and
  a per-goal breakdown; secondary = effect on the served (ITT ÷ serve rate), labelled with its assumption. Holdout
  users get shadow goals; the cohort is fixed at the first trigger of the day. Report honestly even if zero or
  negative.
- Rejected: served vs others; served vs holdout; regression adjustment.

### D-017: Tech stack
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Context: Evaluators must run it on a fresh clone with no key; determinism, auditability, latency simulation and a
  10–12 h budget. Full reasoning: [techstack.md](techstack.md).
- Decision: Python ≥ 3.10, standard library only at runtime; SQLite (source of truth) + JSON export per viewer;
  virtual clock with a modelled worker pool and priority lanes; argparse CLI; `ModelProvider` (mock default, optional
  Ollama via `urllib`, D-003); no agent framework; JSON config; pytest + ruff for development; `pyproject.toml`
  with uv locally and pip documented.
- Rejected: Node/Go; pandas/pydantic/requests; JSON-only state; Postgres/Redis; real sleeps / asyncio timeouts;
  FastAPI; LangChain/LangGraph/CrewAI.
- Consequences: More hand-written helpers; latency is simulated on a virtual clock and must be documented as such.
  Resolves P-001 and requirements.md Q1, Q2, Q14.

### D-018: Repository layout follows the submission format
- Date: 2026-10-18 · Status: Accepted (folder name superseded by D-020) · Decided by: candidate
- Context: The brief prescribes a `submission/` layout (README, ARCHITECTURE, AI_COLLABORATION, KNOWN_LIMITATIONS,
  `src/`, `tests/`, `sample_run/` with decisions, viewer_state, actions, outcomes, latency and impact reports, final report).
- Decision: `ott-agent/` is the submission root and mirrors that layout exactly. `sample_run/` files are **generated
  by the replay only** (never hand-written). The extra `doc/` folder (requirements, rules, decisions, data traps,
  tech stack, work plan) is kept as supporting design documentation.
- Consequences: Resolves requirements.md Q3. `pyproject.toml` and code files will be added with the first
  implementation step (after approval).

### D-019: Goal priority order
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Context: About 88 % of replay users with a fresh trigger are eligible for 2+ goals, so the order decides most goals.
  Estimated eligibility (fresh-trigger users): G01 110 · G02 540 · G03 95 · G04 2 · G05 729 · G06 115 · G07 920 ·
  G08 602 · G09 136 · G12 199. G10/G11 excluded (D-013).
- Options: (A) fixed, documented funnel order; (B) scoring formula (expected uplift × value); (C) LLM chooses.
- Decision: (A), configurable:
  1. **G04** trial→paid (3-day trial, time-critical)
  2. **New-user funnel** (signup ≤ 7 days): **G01** if never played → **G02** if played but never completed →
     **G03** if completed and signup ≤ 2 days
  3. **G07** resume series
  4. **G12** return after absence
  5. **G02** for existing users who never completed an episode
  6. **G08** new release
  7. **G09** weekly habit
  8. **G06** reactivate lapsed
  9. **G05** first paid
- Why: Ranked by urgency, lifecycle stage (the brief's stated problems: first episode, trial conversion), whether the
  agent's main lever (an in-session home page) can move the success event, measurability (G05 ~1 % and G09 ~65 % base
  rates in the pilot leave little detectable room; G09 showed a possible harm signal), and specificity of evidence.
  (B) rejected because no trustworthy uplift estimates exist (the pilot showed no effect); (C) rejected as not
  explainable and penalised by the brief.
- Consequences: Each decision records why its goal was chosen over other eligible goals. Reasoning relies on the
  pilot and the brief, not on tuning against simulator runs (rules.md Rule 6).

### D-020: Project folder is named `submission/`
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Context: The submission checklist names the root folder `submission/`.
- Decision: The project folder `ott-agent/` was renamed to `submission/`. The Python package name stays `ott_agent`
  (`python -m ott_agent ...`). Supersedes the folder name in D-018.

### D-021: Quiet hours: schedule for the morning and re-check at send time (Q10)
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Context: 456 replay triggers fall in IST quiet hours (22:00–08:00). The policy forbids push/WhatsApp in that window;
  the assignment guide says "schedule for later instead".
- Options: (a) schedule `send_at` for the next 08:00–09:00 IST and re-verify at send time; (b) skip the message.
- Decision: (a). A message decided during quiet hours is scheduled for 08:00–09:00 IST **only if** the goal window is
  still open well after that time and every check passes. At `send_at` the verifier runs again (consent, opt-outs,
  daily cap, purchase/status change, title availability/withdrawal, goal still open); if anything fails, the message is
  cancelled and the cancellation recorded. Otherwise the message is skipped with a reason.
- Why: Follows the guide while keeping the policy; re-checking avoids sending stale or now-wrong messages (e.g. after a
  purchase or withdrawal).
- Consequences: The harness needs a small scheduler for pending messages on the virtual clock, plus a cancel path.
  Resolves requirements.md Q10.

### D-022: LLM scope (Q5; closes P-003)
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Decision: The LLM (mock by default; optional Ollama, Qwen 2.5 3B Instruct) is used **only to write push copy**.
  Optionally it may give a second-opinion goal suggestion, which the verifier re-derives from evidence and which can
  never add eligibility. It is never on the home-page path. All output passes the verifier (length, forbidden
  phrases, prices, real/available/in-dialect titles); failures fall back to a fixed rule-based copy template or no
  message.
- Why: Uses the model only where rules can't settle something (wording) and the output can be checked; keeps the
  critical path deterministic and fast (D-003).

### D-023: When to send messages (Q11)
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Decision: The home page (or no action) is the default. A push/WhatsApp message is considered only when the goal's
  success needs a **future visit** (e.g. G03, G07, G12) and consent, quiet hours (D-021), the daily cap and copy rules
  all pass. In-app messages are not used by default (no modelled effect).
- Why: Click rates are low (~1–3 %), messages risk opt-outs and share a daily cap; the brief rewards proportionate
  actions.

### D-024: Several fresh triggers while a page is live (Q12)
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Decision: Reuse the live page (TTL 120 min) and record `NO_ACTION` with reason `PAGE_STILL_LIVE`, unless the state
  changed (new goal, dialect change, withdrawal of a title on the page, purchase), in which case re-plan and republish.
- Why: Avoids churn and duplicate actions; still reacts to real changes.

### D-025: `goal.id` format (Q15)
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Decision: `goal.id` holds the goal **name** (e.g. `first_full_episode_3d`), exactly as in the brief's example; an
  extra `goal.goal_id` holds the catalogue ID (`G02`).
- Why: Keeps the brief's output contract unchanged (FR-13.1).

### D-026: Use git for the submission
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Decision: `git init` in `submission/`. Commits only when the candidate asks (rules.md Rule 9).
- Why: The history shows how the work progressed (supports AI_COLLABORATION.md). Resolves requirements.md Q4.

### D-027: Freshness rules per fact, and "status cannot regress to never"
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Context: Step 1 requires each fact to carry its freshness, and the brief asks "how does your agent decide that what it
  knows is current enough to act on?". Inspecting a real viewer (U-04215) showed a newest profile row `never` while an
  older row said `lapsed`; 14 of the 30 duplicate-profile users have this contradiction.
- Decision:
  1. Every fact has a `freshness` label and a `usable` flag, by class: **immutable** (signup: never stale); **live**
     (page dialect, deeplink: only from the current trigger with true age ≤ 600 s); **state** (status, consent,
     message dialect, language: events authoritative at any age; profile values usable only while the snapshot is
     ≤ 48 h old); **history** (always usable, age reported). Not usable → treated as unknown downstream.
  2. Profile facts are dated by `snapshot_at` (true at export); `updated_at` stays in the evidence and is still used
     to decide whether a billing event is newer than the profile.
  3. A subscription cannot regress: `never` while another row of the same user says `active`/`lapsed` → `uncertain`.
- Why: Answers "current enough to act on" explicitly per fact; prevents a paid user being treated as never-paid
  (e.g. wrongly given G05). Precedence and freshness table: ARCHITECTURE.md §5.1.
- Consequences: Downstream code must use `usable` values; thresholds are configurable (`stale_trigger_s`,
  `profile_max_age_h`).

### D-028: Event-derived "active" status expires after the plan length
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Context: Step 1 review: an `active`/`trial_active` status taken from a purchase or renewal event never expired, while
  the same status from the profile already became `uncertain` after `plan_ends_at` (D-027). 0 users affected in this
  pack, but possible on the unseen replay day.
- Decision: Plan lengths are read from `config/pricing.json` (P_TRIAL 3, P_MONTH 30, P_QUARTER 90 days). If the last
  billing event's plan has ended by decision time with no later renewal/purchase seen, the status becomes
  `uncertain` (not usable), with the end time as a visible conflict. Unknown plan or missing pricing → expiry not
  checked, stated as an assumption.
- Why: One consistent expiry rule for both sources; prevents goals chosen on an expired plan.

---

## Pending (under discussion, not approved)

| ID | Topic | Current proposal | Where discussed |
|---|---|---|---|
| — | _(none)_ | | |

---

## Change log

| Date | Change |
|---|---|
| 2026-10-18 | Created; D-001, D-002 accepted; P-001, P-002 pending. |
| 2026-10-18 | D-003 accepted (mock default + local Ollama); P-001 narrowed; P-003 added. |
| 2026-10-18 | D-004 accepted (duplicate events, Trap 1). |
| 2026-10-18 | D-005 accepted (late events and wrong phone clocks, Trap 2). |
| 2026-10-18 | D-006 to D-016 accepted (Traps 3–13). |
| 2026-10-18 | D-017 accepted (tech stack); P-001 closed. |
| 2026-10-18 | D-018 accepted (repository layout). |
| 2026-10-18 | D-019 accepted (goal priority); D-020 accepted (folder renamed to `submission/`). |
| 2026-10-18 | D-028 accepted (plan expiry for event-derived status). |
| 2026-10-18 | D-027 accepted (freshness per fact; status cannot regress). |
| 2026-10-18 | D-021 to D-026 accepted (quiet hours, LLM scope, messaging, page reuse, goal.id, git); P-002, P-003 closed. |
