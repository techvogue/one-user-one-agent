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
  *(Correction 2026-10-19: 424 unique triggers; 432 counted duplicate deliveries. The decision is unchanged.)*
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
- Date: 2026-10-18 · Status: Accepted (send slot superseded by D-034; re-check at send time still applies) · Decided by: candidate
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

### D-029: Goal planner lifecycle and eligibility refinements (Step 2)
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate
- Context: Building Step 2 ("Choose a goal"); tests exposed two eligibility gaps.
- Decision:
  1. **Measurability** is checked generically at load and after every correction (success event must be an
     observable event type, condition and window defined): G10 and G11 are disabled with their reasons.
  2. **Eligibility uses only usable facts** (D-027); structured fields in goals.json are applied as written;
     text-only rules use the D-014 interpretations (thresholds in config.py, assumption IDs in every decision).
  3. **One open goal per viewer**, enforced by a unique index. Lifecycle: KEEP while valid; close and re-plan when a
     state condition breaks (status changed, the "no prior event" happened, G05 viewer has paid) or the deadline
     passes; entry windows (e.g. "signed up within 2 days") are **not** re-checked for an open goal, so it keeps its
     full success window. Goal start = commit time; deadline = start + window.
  4. Planning and committing are separate (`plan_goal` / `commit_plan`) so the Step 5 verifier can reject first.
  5. **Rule corrections** create a new rule version and update **only open goals of that goal type**; history kept.
  6. `has_played` counts any playback evidence (`play_started`, `play_progress`, `episode_completed`), since a
     `play_started` can be late or missing. G12 absence = no `app_open` **and no other activity** in 14 days
     (new fact `last_activity_at`); also requires signup > 14 days ago. New fact `last_lapsed_at` for G06.
- Why: Goals only from evidence; stable plans; corrections never reprocess every viewer.

### D-030: G09 ranks above G08 (resolves P-004)
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate · Changes D-019 (priority order)
- Context: Every G09-eligible viewer (active subscriber who watched on ≤ 1 day in 14) is also G08-eligible (any active
  subscriber). With G08 above G09, G09 could never be chosen (0 of 1,504 in the dry run).
- Decision: Order is now G04 → G01 → G02 (new) → G03 → G07 → G12 → G02 → **G09 → G08** → G06 → G05. The more
  specific goal (rarely-watching subscriber) comes before the general one.
- Why: A goal that can never be selected cannot be defended; the specific need (build a habit) is served first, and
  subscribers who watch often still get G08.
- Effect (dry run, 1,504 replay users): G09 0 → 46.

### D-031: G07 "series in progress" must have been watched within 14 days (resolves P-005)
- Date: 2026-10-18 · Status: Accepted · Decided by: candidate · Refines D-014 (A-G07-1)
- Context: Any unfinished show/micro-drama left ≥ 72 h qualified, however long ago; G07 took 920 of 1,504 users (61 %),
  including viewers who tried one episode weeks ago and moved on.
- Decision: A-G07-1 is now: unfinished series last played **between 72 h and 14 days** before the trigger
  (`g07_max_age_days = 14`, configurable).
- Why: A series watched two weeks ago is in progress; one tried a month ago is not.
- Effect (dry run): G07 920 → 672; those viewers now get G02 (+54), G05 (+70), G12 (+31), G06 (+18), G08/G09; NO_GOAL
  2 → 7 (all lapsed/uncertain viewers with no provable goal, checked one by one).

### D-032: Step 2 review fixes
- Date: 2026-10-19 · Status: Accepted · Decided by: candidate
- Context: Review of Step 2 against the guide (logic, code quality, tests).
- Decision:
  1. An open goal is also closed and re-planned when: its **G07 series is no longer available** (withdrawn/ended);
     its **rule is no longer measurable**; or its **goal no longer exists** in the goal catalogue.
  2. A rule correction that makes a rule unmeasurable **closes** the open goals of that type (logged); a correction
     for an unknown goal is rejected with a clear error.
  3. G09 play days count **any playback event** (same rule as `has_played`, D-029); the 14-day window is one named
     constant (the unused `g09_lookback_days` setting was removed).
- Why: No goal may stay open when it can no longer be achieved or judged; no setting may silently do nothing.
- Effect (dry run, 1,504 users): G09 46 → 45, G08 105 → 106 (one viewer has playback on 2 days).

### D-033: Intervention design (Step 3)
- Date: 2026-10-19 · Status: Accepted · Decided by: candidate
- Context: The trigger is an app open, so the home page (seen within seconds) is the main lever; a message only helps a
  later visit.
- Decision:
  1. Order of checks: stale trigger → `STALE_TRIGGER` (checked first: not a live visit); no goal → `NO_GOAL`;
     invalid app dialect → `DIALECT_UNKNOWN`;
     a page for the same goal still live (< 2 h, same dialect) → `PAGE_STILL_LIVE` (D-024); no valid hero and no row
     of ≥ 3 valid titles → `INSUFFICIENT_CONTENT`; otherwise **HOME_PAGE**. Every no-action has a reason.
  2. Page content is built from data (no model): titles in the page dialect, available for the whole 2-hour TTL, one
     language variant per slug (by `app_language`), never a flagged title, never a title the viewer already finished.
     "Popular" = distinct viewers with playback of the title in the 30 days before the decision.
  3. Hero and rows per goal: G01 campaign-link title (if valid) else popular; G02 popular **micro-drama** (≈ 3-minute
     episodes, easiest to finish); G07 the viewer's unfinished series; G04/G05/G06 popular **premium** title; G08
     newest popular release; G09 popular micro-drama; G03/G12 popular. Rows: popular in dialect, new releases (last
     30 days), because you watched, the viewer's top genre (exact mix per goal in ARCHITECTURE.md). At most 4 rows of
     3–10 titles; limits, row types, TTL and templates are read from `config/interventions.json`.
  4. In-app messages are not used (D-023). Holdout viewers: decided as normal, nothing published (D-016).
- Why: Proportionate, evidence-based, and only content the viewer can actually see.

### D-034: Reminder messages (Step 3; updates the timing in D-021)
- Date: 2026-10-19 · Status: Accepted · Decided by: candidate
- Context: Up to ~330 messages if every "future visit" goal got one; the brief warns against messaging everyone.
- Decision:
  1. Only goals **G03** (come back on a later day) and **G12** (return after absence) get a reminder (~45 messages at
     most in this replay). G07 relies on its page (the series is the hero).
  2. Sent at **19:00 IST the next day** (evening viewing peak, never in quiet hours), only if the goal is still open at
     least an hour after that. This replaces the 08:00–09:00 slot of D-021; the re-check at send time from D-021
     stays (cancel if consent, cap, status, title or goal changed, or the goal already succeeded).
  3. Channel: **WhatsApp** (approved template `wa_recommend_title_v1`, variables from the catalogue) if consented,
     else **push** (fixed rule-based copy, checked for length, forbidden phrases and prices); no consent → no message.
     At most one push/WhatsApp per viewer per IST day. The title must be available and in the viewer's message
     dialect.
- Why: Messages only where a later visit is the goal, at a time people watch, with copy that cannot go wrong.

### D-035: The model gives a second opinion on the goal; it is never trusted (Step 4)
- Date: 2026-10-19 · Status: Accepted · Decided by: candidate (accepted the recommendation) · Refines D-022
- Context: All five injected model faults are answers to "is this viewer eligible for goal X?" (3 unsupported G04
  claims, 1 malformed JSON, 1 45-second timeout), so the pack expects the agent to consult the model about goals.
  Using the model only for push copy (61 pushes) would leave the faults almost never exercised.
- Options: (a) second opinion on the goal for every fresh decision; (b) push copy only.
- Decision: (a). For every fresh (non-stale) decision, agent and holdout-shadow alike, the model receives **typed facts
  only** (no free text from the data) plus the planner's goal and asks for an opinion. The answer is never evidence:
  - same goal → recorded as agreement (small confidence bonus);
  - another goal the evidence also supports → recorded; the priority order still decides (D-019/D-030);
  - a goal the evidence does **not** support → rejected, logged as `unsupported_claim` with the failing check;
  - malformed output → one retry within budget, then rules only; timeout / 503 → rules only.
  The decision never waits for the model to publish a page. Stale triggers never call the model (cheap lane, D-006).
  Push copy stays rule-based for now (D-034).
- Why: Shows the trust boundary the brief asks for ("a confident model output is not evidence") on real injected faults.

### D-036: Fallback when the data store is slow or down (Step 4)
- Date: 2026-10-19 · Status: Accepted · Decided by: candidate (accepted the recommendation)
- Options: (a) smaller safe action; (b) no action during the outage.
- Decision: (a). If reading the viewer's state fails or times out: use the **last known facts** for the viewer from this
  run if they exist (marked as cached, with their age); otherwise publish a **generic page** of popular titles in the
  dialect named by the app open itself (no store needed), with **no message and no goal change**, marked
  `fallback_used: generic_page`. Invalid dialect or no titles → no action. Everything is recorded on the decision.
- Why: The brief: "fall back to a smaller safe action or no action". A generic page in the right dialect cannot
  break any rule; messages and goal changes need facts we do not have.

### D-037: Time model, budgets and timing limits (Step 4)
- Date: 2026-10-19 · Status: Accepted · Decided by: candidate (accepted the recommendation) · Details D-017
- Decision (all configurable):
  - Simulated clock: `published_at = deliver_at + measured compute time + simulated dependency delays + queue wait`.
  - Normal simulated speed: data-store read 20 ms, mock model call 800 ms.
  - Timeouts: data store 2 s, model 3 s (the injected 45 s hang costs 3 s).
  - Budgets per decision: at most **8 model calls** and **20 tool calls**; every call is counted and recorded.
  - 4 parallel workers; fresh triggers are processed before stale ones; stale triggers use the cheap lane.
  - The model is skipped when the decision is already more than 10 s behind its trigger.
  - Publishing more than **60 s** after the trigger is not done: the decision is recorded as `FAILED_LATE` (the brief
    counts > 5 min as a failure; we stay well inside 60 s).
  - Degraded data store (control event): deterministic per trigger, 20 % of reads time out, the rest take up to 8 s.
- Why: Reproducible latency without waiting in real time, with explicit budgets and limits.

### D-038: Replay harness design (Step 4)
- Date: 2026-10-19 · Status: Accepted · Decided by: candidate (accepted the recommendation)
- Decision:
  1. One timeline in delivery order of triggers, live events and control events; live events are ingested with the same
     path as history (D-004/D-005); control events withdraw titles, correct goal rules (only open goals of that type,
     D-029) and switch dependencies down/slow for their window.
  2. Per trigger: duplicate → `DUPLICATE_IGNORED`; same session with no relevant change → `SAME_SESSION`; stale →
     `STALE_SKIPPED` (cheap lane); holdout → full decision, nothing published (`HOLDOUT_SHADOW`, shadow goal);
     otherwise facts → goal → model second opinion → intervention → checks → publish.
  3. Every trigger yields exactly one line in `decisions.jsonl` (brief's format, extended); published actions go to
     `actions.jsonl` in the simulator's schema; `latency_report.json` summarises timing.
  4. Reminder messages are held as pending until their send time and re-checked then (D-021, D-034); only messages that
     pass are written as actions. A title withdrawal finds the live pages and pending messages that contain it: pages
     are republished without it, messages cancelled; affected viewers are recorded.
  5. Step 5's independent verifier plugs into the publish path; Step 4 runs basic checks only.
- Why: Covers the "act within budget" step end to end with every decision auditable.

### D-039: Step 4 implementation details
- Date: 2026-10-19 · Status: Accepted · Decided by: candidate · Details D-036, D-038
- Decision:
  1. **Cached facts** (D-036) are used only if they are from this run, at most **2 hours** old, and the app open names
     the **same dialect**; with cached facts no message is planned and no goal is committed.
  2. An **unavailable** data store always fails the read (after the timeout); a **degraded** store fails 20 % of reads
     outright and any read slower than the 2 s timeout.
  3. **Holdout records** say `action: NO_ACTION` with reason `HOLDOUT`; the would-be decision is kept under
     `shadow_action` (so nobody can read a holdout record as "we acted"). A viewer with **no arm** in arms.csv is treated
     as holdout (never acted on).
  4. **Same session:** a second trigger re-decides only if, since the first decision, a purchase/renewal/lapse,
     opt-out, dialect change or signup event arrived, or the app dialect differs; otherwise `SAME_SESSION`.
  5. **Send-time re-check** (basic, Step 5 completes it): the goal must still be open, the viewer must not have played
     since the goal started, consent still usable, the title still available, outside quiet hours, one per IST day.
     Knowledge at send time = events received by the earlier of the send time and the end of the replay.
  6. **Withdrawal republishing** rebuilds the page from the facts and goal that produced it, at the withdrawal time;
     if no personal page is possible, a generic page in the same dialect is published.
  7. **Reproducibility:** every replay starts from a fresh state store; decisions and actions are identical across
     runs except publish timestamps (measured compute time, a few ms).
- Why: Make the fallback and audit behaviour explicit and testable.

### D-040: Independent verifier (Step 5)
- Date: 2026-10-19 · Status: Accepted · Decided by: candidate (accepted the recommendation)
- Context: The brief: "Run independent checks, separate from the decision step … The checker must be able to reject the
  decision. Model output is never proof." A checker that reuses the decision code (facts resolver, eligibility
  function, page builder) cannot disagree with it: a bug there would pass its own check.
- Options: (a) re-run the planner's functions as "checks"; (b) a separate verifier that re-derives what it checks
  from the stored events, profile rows, raw catalogue fields and pack config, with its own code.
- Decision: (b). `verifier.py` shares only file parsing and the event store; it does **not** import the facts resolver,
  the goal evaluator, the planner or the intervention builder. It runs:
  1. **before every publish** (agent arm) and for every holdout shadow decision, and
  2. **at send time** for every pending reminder (D-021, D-034).
  Checks: holdout (nothing published for holdout or unknown arm); duplicates (no second action for the same trigger,
  no second page for one visit unless re-planned); goal measurable; **goal eligibility re-derived from evidence**
  (never from model text: a goal the model claimed is rejected unless the evidence supports it); page dialect equals
  the app's valid setting; every title in that dialect, available through the page TTL, not withdrawn, not flagged
  (own scan of descriptions); page structure (hero ≤ 1, rows within limits, allowed row types, unique slugs); message
  consent (latest profile row + any opt-out event; unknown = no), quiet hours, one push/WhatsApp per IST day, approved
  template with catalogue-filled variables, push copy limits / forbidden phrases / no prices, deeplink format, title
  dialect = the viewer's message dialect; budgets and the 60 s limit.
- Verdicts: **approve**; **downgrade** (only message checks failed → the message is dropped, the page goes out);
  **reject** (any goal, page, holdout or duplicate check failed → nothing published, goal not committed, status
  `REJECTED_BY_VERIFIER`). Every check is listed as passed or failed with a reason on the decision record.
- Why: Gives the brief's "checks that can disagree with the decision step" a concrete meaning: two separately written
  implementations must agree before anything reaches a viewer.

### D-041: G07 only for series that may be promoted (found by the verifier)
- Date: 2026-10-19 · Status: Accepted · Decided by: candidate · Fixes D-014 / D-031 (A-G07-1)
- Context: On its first real replay the independent verifier rejected **157 decisions**: G07 "resume series" goals set
  on `hama-chudibha-d1` (T0002) and `guka-kosusa-d2` (T0522), the two titles quarantined for injected instructions
  (D-012). The planner checked only availability, so it set goals around titles the page may never show.
- Decision: A series qualifies for G07 only if a variant is **promotable** (available and not quarantined); an open G07
  goal is re-planned if its series stops being promotable (withdrawn, ended or quarantined).
- Effect: verifier rejections 157 → 0; those viewers get another goal.
- Why: The decision step must not build goals on content it is not allowed to promote. This is the verifier working as
  intended: a separately written check disagreed and was right.

### D-042: Judging goals (Step 6)
- Date: 2026-10-19 · Status: Accepted · Decided by: candidate (accepted the recommendation) · Uses D-005, D-029
- Context: The brief: judge each goal as succeeded, failed or inconclusive "from attributable signals only", when the
  deadline passes or new events arrive. Outcome events come from the pack's simulator (14 days after the replay day);
  they are ingested with the same path as all other events (dedupe, corrected time).
- Decision:
  1. **Every goal instance is judged, agent and holdout-shadow alike**, by the success rule **version it was opened or
     corrected to** (G03 opened before the 08:30 correction is judged by the corrected rule, D-029).
  2. **Attributable signal** = an event of the rule's success type whose corrected event time lies in
     **[goal start, deadline]**, known by the judging time, matching the condition: G01/G12 any `play_started`; G02 any
     `episode_completed`; G03 success event on a later IST day than the start; G04/G05/G06 `purchase_completed` with
     `is_trial = false`; G07 `play_started` on the goal's series slug (any language variant); G08 `play_started` on a
     title released in the 30 days before the start; G09 `play_started` on 3 distinct IST days.
  3. Verdicts:
     - **SUCCEEDED** as soon as an attributable signal exists (cannot be revoked);
     - **FAILED** when the deadline + 24 h grace has passed within the observed data without a signal;
     - **INCONCLUSIVE** when the outcome cannot be known: the observed data ends before deadline + 24 h; the goal was
       closed early for a reason other than success (e.g. series withdrawn); the rule is not measurable.
  4. **Stage** (D-005): `provisional` from deadline + 24 h, `final` from deadline + 7 days; early successes are
     `provisional` until then. Judging is a pure function of (goal, events known by T), so it can be re-run whenever
     new events arrive; a later run may move a verdict from INCONCLUSIVE to SUCCEEDED/FAILED, never the other way.
  5. Each verdict records the success events (ids, times), the window, the rule version, the reason, and **exposure**:
     our pages shown to the viewer in the window (`home_page_shown` with our `page_action_id`) and messages
     delivered/clicked. Exposure is reported, not used to decide success (attribution to the agent is measured
     against the holdout in Step 7, never per viewer).
- Why: Honest verdicts that only count what can be tied to the goal window, and say "unknown" when it is unknown.

### D-043: What is learned per viewer (Step 6)
- Date: 2026-10-19 · Status: Accepted · Decided by: candidate (accepted the recommendation)
- Decision: For each judged goal a short, factual **learning note** is stored in the viewer's state (`goal_outcomes`
  table, `goal_log` entry `judged`, `viewer_state/<user>.json`), e.g. "G07 succeeded 1.2 days after start; resumed the
  series after our page was shown" or "G12 failed: no play within 7 days; reminder delivered, not clicked". Notes state
  observations only, never causes. `sample_run/outcomes.jsonl` holds one line per judged goal.
- Why: The brief asks to "save what was learned in the user's state"; per-viewer notes cannot prove causation, so they
  only record what happened.

### D-044: How impact is measured (Step 7)
- Date: 2026-10-19 · Status: Accepted · Decided by: candidate (accepted the recommendation) · Details D-016
- Context: The brief: compare the agent arm with the randomized holdout using simulator outcomes, and explain why
  "served vs not served" misleads. The pilot report made exactly that mistake (D-016, Trap 13).
- Decision:
  1. **Unit and cohort:** a viewer. The cohort is every viewer with at least one fresh (live) trigger on the replay
     day, in either arm, fixed **before** any action: the same rule decides who is in the cohort in both arms, so the
     arm cannot change who is counted.
  2. **Primary metric (intention-to-treat):** share of cohort viewers whose **first goal** (agent goal or holdout
     shadow goal, decided by the same planner) SUCCEEDED, agent arm vs holdout arm, including viewers the agent then
     chose not to act on or whose page was never seen. Later goals are excluded (a re-plan can depend on the
     treatment). Viewers with no goal at their first visit are reported per arm, not dropped silently.
  3. **Inconclusive goals:** primary = succeeded / judged (succeeded + failed); sensitivity = succeeded / all (an
     inconclusive goal counted as not succeeded). Both are shown.
  4. **Uncertainty:** difference in proportions with a **95 % Newcombe–Wilson interval**; per-goal breakdown with the
     same intervals and an explicit small-sample warning; no per-goal claim without its interval.
  5. **Secondary, goal-independent outcomes** (same cohort, ITT): any play in the 7 days after the first visit, play
     days in 14 days, any paid purchase in 14 days.
  6. **Robustness:** the simulator is re-run with additional seeds on the same actions; the effect is reported for
     each seed (evaluators use their own seed).
  7. **What is shown as misleading, labelled as such:** served vs not served within the agent arm, and served vs
     everyone else (the pilot's comparison), with the reason (selection: who gets served/sees a page depends on their
     own behaviour). An "effect on the served" estimate (ITT ÷ exposure rate) is secondary, labelled with its
     assumption (an unexposed viewer is unaffected).
  8. **Pilot re-analysis** reproduced in the same report.
  9. Results are reported as they come out, including zero or negative effects (rules.md Rule 6).
- Why: The only comparison that answers "did the agent cause it" is between randomized arms, counted the same way.

### D-045: Every decision record carries everything the brief requires
- Date: 2026-10-19 · Status: Accepted · Decided by: candidate (approved the proposal) · Completes FR-13
- Context: A check of all 2,769 records against the brief's "every decision must include" list found 688 gaps: the
  short records for skipped triggers (stale, duplicate, same session) had no dialect block, no check results and no
  freshness evidence; fallback decisions had no freshness evidence and 4 had neither a goal nor a stated reason. Only
  dialect conflicts were visible on the record; other resolved conflicts lived only in the facts snapshot.
- Decision:
  1. Skipped triggers get a full record: dialect block from the app open ("not decided: nothing published"), the check
     that decided them (stale → `freshness` failed; duplicate → `duplicate` failed; same session → passed), and the
     trigger's own timing as freshness evidence.
  2. Every decided record starts `evidence_detail` with the trigger's timing (received, corrected event time, true
     age, live/stale); data-store failures are recorded as evidence; fallback pages carry a `goal_note`.
  3. New top-level `conflicts`: every fact resolved from conflicting sources, with the value used, its source, the
     conflicting values, whether it is usable, and how it was resolved (with the decision reference).
  4. A test (`record_gaps`) fails if any record of any status lacks a required item.
- Effect: 688 → 0 gaps; actions, outcomes and impact unchanged (records only).

### D-046: Top-level rule_version and specific assumptions on every record
- Date: 2026-10-19 · Status: Accepted · Decided by: candidate (approved the proposal) · Extends D-045
- Context: The brief lists `rule_version` as a good extra field and requires "assumptions about the user, their history
  and their context". `rule_version` existed only inside `goal`; 1,279 of 2,769 records had no assumptions because
  only defaults and goal interpretations were recorded.
- Decision:
  1. Top-level `rule_version` = the goal rule version used (e.g. `G03@2026-10-01T08:30:00.000Z`), or the goal
     catalogue version when there is no goal (`goals@2026-09-30`); plus `config_versions` (goals, corrections applied
     so far, interventions, agent version).
  2. Assumptions built from the decision's own data, never boilerplate: history = events received by the delivery
     time; the viewer stays on the page dialect for this visit; profile-sourced status unchanged since the snapshot;
     message consent still valid at send time (re-checked then); fallback = no personal facts assumed; skipped triggers
     state why (session ended with its true age / resend of an already-decided event / same visit with no change).
  3. `record_gaps` requires both on every record.
- Effect: 0 records without assumptions or rule_version; actions, outcomes and impact unchanged (records only).

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
| 2026-10-19 | D-046 accepted (rule_version + specific assumptions). |
| 2026-10-19 | D-045 accepted (complete decision records). |
| 2026-10-19 | D-044 accepted (impact methodology). |
| 2026-10-19 | D-042, D-043 accepted (judging goals; learning notes). |
| 2026-10-19 | D-041 accepted (G07 needs a promotable series; found by the verifier). |
| 2026-10-19 | D-040 accepted (independent verifier). |
| 2026-10-19 | D-039 accepted (Step 4 implementation details). |
| 2026-10-19 | D-035 to D-038 accepted (Step 4: model role, fallback, timing, harness). |
| 2026-10-19 | D-033, D-034 accepted (Step 3 intervention design; message timing replaces D-021's slot). |
| 2026-10-19 | D-032 accepted (Step 2 review fixes). |
| 2026-10-18 | D-030, D-031 accepted; P-004, P-005 closed. |
| 2026-10-18 | D-029 accepted (goal planner); P-004, P-005 opened. |
| 2026-10-18 | D-028 accepted (plan expiry for event-derived status). |
| 2026-10-18 | D-027 accepted (freshness per fact; status cannot regress). |
| 2026-10-18 | D-021 to D-026 accepted (quiet hours, LLM scope, messaging, page reuse, goal.id, git); P-002, P-003 closed. |
