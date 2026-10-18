# Requirements: One User, One Agent

| Item | Value |
|---|---|
| Source | `assignment_one_user_one_agent.pdf` (brief) + `one_user_one_agent_pack/` (data pack) |
| Status | **Draft: for discussion. Nothing here is approved for implementation yet** (see [rules.md](rules.md)) |
| Effort budget | 10–12 hours, submit within 4 calendar days |
| Last updated | 2026-10-18 |

Requirement IDs (`FR-`, `NFR-`, `DR-`, …) are referenced by change proposals, tests and docs.
Items marked **[PROPOSED]** are my suggested interpretation and must be confirmed before coding.
Items marked **[OPEN]** need an answer (see §14).

---

## 1. Goal of the project

Build an agentic system that gives **every viewer one dedicated agent**. On each trigger (app open / login) the
agent, within a strict time budget:

1. assembles what is known about the viewer, with provenance and freshness;
2. chooses **one measurable goal** (or explains why none);
3. decides an intervention: home page, one message channel, timing, or **nothing**;
4. verifies the decision **independently** before publishing;
5. publishes within the latency and call budgets, degrading safely on failure;
6. later judges the goal as succeeded / failed / inconclusive from attributable signals;
7. proves impact against the **randomized holdout**.

**What is being judged:** goal-directed decisions, evidence, timeliness, safety and honest measurement.
**Not** recommendation accuracy. A clever recommender without a trustworthy decide → act → measure loop scores poorly.

---

## 2. Scoring (from the brief)

| Weight | Area | Strong evidence |
|---|---|---|
| 20% | Goal and decision quality | Goals fit the viewer, actions proportionate, doing nothing chosen when right |
| 15% | Evidence and state | Facts carry source + freshness, conflicts visible, state durable and consistent |
| 15% | Real-time reliability | Latency met, idempotent triggers, safe degradation |
| 15% | Verification and safety | Checks catch ineligible goals, wrong dialect, consent/cap violations, injected instructions |
| 15% | Measurement and learning | Honest attribution, holdout comparison, results fed back into state |
| 10% | Engineering quality | Understandable, testable, reproducible |
| 10% | Use of AI tools | Candidate directs, checks and sometimes rejects AI work |

**Scores poorly:** a single prompt picking titles for everyone; goals/eligibility without evidence; messages in the
wrong dialect, at night or without consent; served-vs-unserved uplift; ignoring late/duplicate events;
**reprocessing every viewer after one rule change**; AI code that was not run and tested.

---

## 3. Inputs: the data pack (verified facts)

All numbers below were measured directly from the pack on 2026-10-18.

### 3.1 History (`data/`)

| File | Content | Measured issues |
|---|---|---|
| `events.jsonl` | 142,906 events, 4,759 users, 2026-09-01 → 09-30 IST | **2,176 duplicate `event_id`s**; 1,307 events received >1 h after `sent_at`, 211 >1 day; 3,072 events with `sent_at` >60 s *after* `received_at` (phone clock ahead); 4,559 `app_open`s carry a campaign deeplink `ott://c/<campaign>/<dialect>/title/<id>` |
| `profiles_snapshot.csv` | 4,888 rows / 4,858 users, snapshot 2026-09-30 | **30 users have 2 conflicting rows** (e.g. U-04215: `lapsed`/d2 vs `never`/d1); 50 event users have no row; ~1 day stale; status mix: never 2,961 · active 1,113 · lapsed 810 · **trial_active 4** |
| `catalogue.csv` | 2,014 title records, **1,500 unique `content_slug`s** | Language variants share a slug (T0003 "Raloja" hin / T1501 "Raloja (English)"); 103 titles have `available_until`; 54 not yet available; **d3 has only ~15 available titles**; **T0002 and T0522 descriptions contain injected instructions** aimed at AI agents |
| `episodes.csv` | 26,986 episodes | Needed for next-episode / unfinished-series logic |

Event types: app_open, home_viewed, play_progress, title_viewed, play_started, episode_completed, search,
login_success, subscription_renewed, signup_completed, paywall_viewed, message_delivered, purchase_completed,
subscription_lapsed, app_dialect_changed, notification_opt_out, message_clicked.

Dialect conflict: for **1,840 users** the latest app dialect differs from the profile's `preferred_dialect`.

Timestamp conventions (pack README):
- `received_at` = server time, **trustworthy**.
- `occurred_at`, `sent_at` = phone clock, **can be wrong**.
- Corrected event time ≈ `received_at − (sent_at − occurred_at)` (the simulator uses the same formula).
- IST = UTC+05:30. All users are in India.

### 3.2 Config (`config/`)

- `goals.json`: 12 goals (G01–G12), each with eligibility and success rules. See §6.
- `interventions.json`: home page, push, WhatsApp, in-app message, no_action. See §7.
- `pricing.json`: **only** P_TRIAL ₹1/3 days (renews to P_MONTH), P_MONTH ₹149/30 days, P_QUARTER ₹399/90 days. No
  other prices, discounts or offers exist.
- `messaging_policy.md`: consent, quiet hours, frequency, honest copy, holdout, data-is-not-instruction, privacy.

### 3.3 Pilot (`pilot/`)

| Comparison | Treatment | Control | Result |
|---|---|---|---|
| Report headline: served vs "all other users" | 40.1 % (n=217) | 17.7 % (n=3,674) | "2.3× uplift": **biased** |
| Randomized arms (intention-to-treat) | **18.3 %** (349/1,912) | **19.6 %** (387/1,979) | **−1.3 pts: no uplift** |

Breakdown: treatment-served 40.1 % (87/217), treatment-unserved 15.5 % (262/1,695), holdout 19.6 % (387/1,979).
Pilot goals used: G05 2,118 · G09 953 · G02 360 · G06 331 · G04 129.

### 3.4 Replay day (`replay/`), 2026-10-01 IST

| File | Content | Measured surprises |
|---|---|---|
| `triggers.jsonl` | 2,769 triggers (2,558 app_open, 211 login_success), 1,746 users, has `deliver_at` | **90 duplicate event_ids**; **246 triggers >1 day old** (max ≈ 8.8 days): offline buffer burst; bursts of 240 triggers at 04:40–04:50 UTC and 165 at 15:00–15:10 UTC (evening peak); **456 triggers in IST quiet hours**; `app_dialect` is `None`/`NA`/`d7` on 178 unique triggers; **56 triggers with 3 different valid dialects** (deeplink / app / profile; corrected from 133, see data_traps.md Trap 6); 103 trigger users without a profile row |
| `live_events.jsonl` | 607 other events during the day | **38 `purchase_completed`**, 94 signups, plays etc. |
| `control_events.jsonl` | 4 operational events | 07:00 T0001 withdrawn · 08:30 G03 success rule corrected · 10:35–10:42 model provider 503 · 13:10–13:17 user data store degraded (p95 8 s, 20 % timeouts) |
| `model_faults.jsonl` | 5 faults the mock model must reproduce | 3× `unsupported_claim` (G04 eligible, 0.97, "confirmed in billing"), 1× malformed JSON, 1× 45 s timeout |
| `arms.csv` | 5,000 users | agent 2,513 · holdout 2,487 |

### 3.5 Simulator (`simulator/simulate_outcomes.py`): behaviour that matters

Read from source. Our system must produce actions this simulator accepts; evaluators rerun it with **a different
seed and an unseen replay day**.

1. **Actions for holdout users** are ignored **and logged as policy problems**.
2. **Actions for users who did not trigger** that day are rejected.
3. Home page is validated per title: must be in the page dialect, available at `published_at`, unique slug per page;
   rows < 3 valid titles dropped; > 6 rows ignored; hero rejected if invalid. Withdrawn titles become unavailable from
   the withdrawal time.
4. A page is **shown only if** `published_at ≤ moment the user reaches home` (3–40 s after app open) **and** the page
   dialect equals the app's current dialect, within a **120-minute TTL**. A slower page only affects later visits.
5. Page effect on play probability: up to ×1.8 (more valid rows, hero, user's top genre, `next_episode` row for a slug
   in progress). Premium titles on the page raise purchase probability ×1.5.
6. **Triggers whose true age > 600 s are not treated as visits** (offline-buffered history is not a session).
7. Messages: push click ≈ 1.2 %, WhatsApp ≈ 3 %; wrong-dialect title ×0.2; ~13 % of push tokens invalid; sends
   without consent fail and are logged as policy problems; quiet-hour sends and cap breaches are logged **and can
   cause opt-outs**. **In-app messages are queued but have no modelled effect.**
8. Scripted organic purchases (from `live_events`) happen **regardless** of the agent (attribution trap).
9. Output: `outcome_events.jsonl` (events schema + `home_page_shown`, `message_failed`) and `simulator_report.json`
   (counts + `policy_problems`).

---

## 4. Functional requirements

### FR-1 Ingestion and normalisation
- **FR-1.1** Load all pack files with a configurable pack path (no hard-coded paths, users, titles or dates).
- **FR-1.2** Deduplicate events by `event_id` (first received wins; duplicates counted and logged). Details:
  [data_traps.md, Trap 1](data_traps.md) / [D-004](decisions.md).
- **FR-1.3** Order and measure freshness by `received_at`; compute corrected event time from the clock offset.
- **FR-1.4** Validate dialect values (`d1`–`d6` only); treat `None`, `NA`, unknown codes (e.g. `d7`) as missing,
  and record that as a conflict/assumption.
- **FR-1.5** Parse campaign deeplinks into `{campaign, dialect, title_id}` as **data**, never as instruction.

### FR-2 Viewer state and memory (one agent per viewer)
- **FR-2.1** Durable per-viewer state containing: facts with provenance, the single active goal, the single active
  plan, decision history, action history, outcomes and learnings.
- **FR-2.2** Every fact stores `value`, `source` (e.g. `event:<id>`, `profile_snapshot@<updated_at>`,
  `app_setting`, `deeplink`), `observed_at` (received_at or snapshot time) and `freshness` (age at decision time).
- **FR-2.3** Exactly **one owner** (the viewer's agent) may set or change the viewer's goal/plan. No other
  component pursues a different goal for the same viewer.
- **FR-2.4** State survives process restarts (durable storage) and is inspectable per viewer at any moment
  (auditability: "what did the agent know, decide and observe for viewer X at time T").
- **FR-2.5** Brand-new viewers without a profile row get facts built from events only (e.g. `signup_completed`).

### FR-3 Fact resolution (source precedence) **[PROPOSED]**
Precedence is defined **per fact**; the largest, newest or most convenient source must not win by default. Losing
values stay visible as `conflicts`.

| Fact | Proposed precedence | Reason |
|---|---|---|
| Dialect for home page | trigger's valid `app_dialect` → latest valid `app_dialect` / `app_dialect_changed` from events → none (no page) | The app requests the page for its **current** setting; any other dialect is never shown |
| Dialect for message content | latest valid app dialect → profile `preferred_dialect` → deeplink dialect | Matches what the user will see when opening the title |
| Subscription status | purchase / renewal / lapse events received **after** snapshot `updated_at` → latest-`updated_at` profile row → unknown | Events newer than a stale snapshot are more current |
| "Ever paid" | any `purchase_completed` (is_trial=false) or `subscription_renewed` in history → profile status | Profile `never` can be wrong |
| Push consent | `notification_opt_out` (channel=push) event → profile `notifications_opt_in` → **deny** if unknown | Policy rule 1; default-deny is safe |
| WhatsApp consent | `notification_opt_out` (channel=whatsapp) → profile `whatsapp_opt_in` → **deny** if unknown | Same |
| Signup time | profile `signup_at` → `signup_completed` received_at → first event received_at (marked as assumption/lower bound) | Missing rows for new users |
| Duplicate profile rows | row with latest `updated_at` wins; the other row is recorded as a conflict | Same rule the simulator uses |

### FR-4 Trigger handling and freshness
- **FR-4.1** Process triggers in `deliver_at` order, interleaved with live and control events by time.
- **FR-4.2** **Idempotency:** the same `event_id` never produces a second decision or action (idempotency key =
  trigger `event_id`); duplicates are recorded as `DUPLICATE_IGNORED`.
- **FR-4.3** **Stale triggers** (true age above a threshold, **[PROPOSED] 600 s**, configurable) do not create a home
  page or message; they update state and are recorded as a no-action decision with reason `STALE_TRIGGER`.
- **FR-4.4** Several fresh triggers from the same viewer within a page TTL must not create redundant pages.
  Reuse the live page unless the state changed ([D-024](decisions.md)).

### FR-5 Goal selection
- **FR-5.1** Evaluate eligibility for each goal **from cited evidence only** (event IDs, profile row, rule version).
- **FR-5.2** Select **one** goal, set `goal_start` (= commit time), `deadline` (= start + `window_hours`) and the
  success signal; or record `NO_GOAL` with a reason.
- **FR-5.3** **Never select a goal whose success signal cannot be measured** (G10, G11), and document why.
- **FR-5.4** Keep the active goal across triggers until it succeeds, fails, expires or the situation changes
  (e.g. purchase → close `first_paid`/`trial_to_paid` goals and re-plan). Re-planning is **event-driven**, not on a
  fixed schedule.
- **FR-5.5** Goal priority when several are eligible: see §6.3 / [D-019](decisions.md).
- **FR-5.6** Model output may *suggest* a goal but is never evidence of eligibility (see FR-8).

### FR-6 Intervention decision
- **FR-6.1** Options: `home_page`, `push`, `whatsapp`, `in_app_message`, or `NO_ACTION` (always allowed, always
  recorded with a reason).
- **FR-6.2** Content must exist, be available at publish time, be in the dialect the user will see, and not be
  withdrawn.
- **FR-6.3** Home page builder respects: ≤ 1 hero, ≤ 6 rows, 3–10 titles per row, unique `content_slug` per page,
  allowed row types (`next_episode`, `because_you_watched`, `popular_in_dialect`, `new_releases`, `genre`), no
  "Continue Watching".
- **FR-6.4** If not enough valid titles exist for a row (e.g. low-content dialects such as d3), the row is not
  published; if no valid row/hero remains, the decision becomes `NO_ACTION` with reason `INSUFFICIENT_CONTENT`.
- **FR-6.5** Messages: at most one channel per decision; respect consent, quiet hours (schedule `send_at` for 08:00–09:00 IST and re-verify at send time, see [D-021](decisions.md)), the shared daily cap and copy rules.
- **FR-6.6** Push copy: title ≤ 40 chars, body ≤ 120 chars, no forbidden phrases, no price other than
  `pricing.json`, deeplink `ott://title/<title_id>`.
- **FR-6.7** WhatsApp: only the 3 approved templates; variables filled from catalogue/pricing only, never free text.
- **FR-6.8** Proportionality: do not message by default; prefer the least intrusive action that serves the goal.

### FR-7 Independent verification
- **FR-7.1** A verifier, separate from the decision step and with its own data reads, runs before every publish.
- **FR-7.2** Checks: holdout arm, goal eligibility (re-derived from evidence), dialect, availability/withdrawal,
  slug uniqueness, row/hero limits, consent, quiet hours, daily frequency cap, template/copy/price rules,
  duplicate action, latency budget.
- **FR-7.3** The verifier can **reject** the whole decision or **downgrade** it (e.g. drop the message, keep the
  page); the result is recorded in `checks.passed` / `checks.failed`.
- **FR-7.4** Model text is never treated as evidence or approval.
- **FR-7.5** Instruction-like text in data (catalogue descriptions, search queries, deeplinks) is ignored as
  instruction and **flagged** in logs.

### FR-8 Model use (LLM)
- **FR-8.1** A provider abstraction with a **mock/replay provider as the default** (no personal API key needed).
- **FR-8.2** The mock reproduces `model_faults.jsonl` at the named triggers: unsupported claim, malformed output,
  timeout.
- **FR-8.3** The model is called only when it reduces meaningful uncertainty and fits the budget. Scope: push copy only (optional second-opinion goal), see [D-022](decisions.md).
- **FR-8.4** Model output is parsed defensively; malformed output → retry within budget → deterministic fallback.
- **FR-8.5** The decision path must work fully **without** the model (rules-only fallback).

### FR-9 Real-time behaviour, budgets and failure handling
- **FR-9.1** Measure processing time per decision; `published_at = deliver_at + measured processing time`.
- **FR-9.2** Configurable budgets: max **8 model calls**, max **20 tool calls**, per-call timeouts.
- **FR-9.3** The replay harness applies `control_events.jsonl`: makes the model provider fail (503) or the user data
  store slow/time out during the stated windows.
- **FR-9.4** On dependency failure: fall back to cached state/rules → smaller safe action → `NO_ACTION`.
- **FR-9.5** Handle bursts (3× traffic) without missing the latency budget: concurrency and/or prioritising fresh
  triggers, skipping optional model calls under load.

### FR-10 Change handling during the replay
- **FR-10.1** **Title withdrawn:** stop using the title from the withdrawal time; identify live pages and pending
  messages that include it and handle them (re-publish without it / cancel), and record which viewers were affected.
- **FR-10.2** **Goal rule corrected:** version the goal rules; apply the new rule **only to affected open goals**
  (for G03: every open G03 goal, including ones opened before the correction); **do not reprocess every viewer**.
- **FR-10.3** **Purchase after a decision:** update state, close or replace now-invalid goals and cancel pending
  messages that no longer make sense (e.g. a "trial ending" message to someone who just paid).
- **FR-10.4** Every such change is recorded with: what changed, which viewers/decisions were affected, what was done.

### FR-11 Outcome judgement (closing the loop)
- **FR-11.1** After the simulator run, judge every committed goal as `SUCCEEDED`, `FAILED` or `INCONCLUSIVE` from
  attributable events inside the goal window, using the rule version in force.
- **FR-11.2** `INCONCLUSIVE` when the signal cannot be observed or attribution is unclear (e.g. scripted organic
  purchase before exposure; message never delivered).
- **FR-11.3** Write the outcome and a short "learned" note back into the viewer's state.

### FR-12 Impact measurement
- **FR-12.1** Primary metric: success rate of **all agent-arm triggered users vs all holdout-arm triggered users**
  (intention-to-treat), including users the agent chose not to act on.
- **FR-12.2** Holdout users need a **shadow goal** (the goal the agent *would* have chosen, decided but not acted on)
  so the same success definition can be measured in both arms. **[PROPOSED]**
- **FR-12.3** Report uplift with uncertainty (e.g. 95 % CI) and a per-goal breakdown.
- **FR-12.4** Re-analyse the pilot (§3.3) and explain why served-vs-unserved misleads (selection bias).
- **FR-12.5** Report honestly even if uplift is zero or negative.

### FR-13 Decision output

**FR-13.1 Baseline contract (verbatim from the brief, §4).** Every decision must be machine-readable and contain at
least these fields. The brief says "you may extend the structure", so our schema is a **superset**: every field below
must stay present with the same name and meaning.

```json
{
  "decision_id": "D-0417",
  "viewer_id": "V-2291",
  "trigger": {"event": "app_open", "received_at": "...Z", "event_age_s": 9},
  "goal": {"id": "first_full_episode_3d", "deadline": "...Z"},
  "action": "HOME_PAGE",
  "dialect": {"used": "d3", "source": "app_setting", "conflicts": ["profile:d1"]},
  "content": ["title:T0412", "title:T0977", "title:T1203"],
  "confidence": 0.61,
  "evidence": ["event:E88123", "catalogue:T0412", "goal_rule:G04-r2"],
  "assumptions": ["viewer has not started any episode of T0412"],
  "checks": {"passed": ["eligibility", "dialect", "frequency_cap"], "failed": []},
  "decided_in_ms": 6400,
  "status": "PUBLISHED"
}
```

**FR-13.2 What each decision must express (brief, §4 bullets):**
- a goal and an action, **or** an explicit statement of why the agent did nothing;
- evidence references another person can inspect, **with their freshness**;
- confidence **with a short reason** (a number alone is not enough);
- assumptions about the viewer, their history and their context;
- conflicting evidence that could change the decision, and **how it was resolved**;
- check results, decision time, and the **deadline and signal** by which the decision will be judged.

**FR-13.3 Field mapping: baseline → extension [PROPOSED]**

| Baseline field | Kept as | Extension (added, never replacing) | Why |
|---|---|---|---|
| `decision_id` | same | — | — |
| `viewer_id` | same (value = pack `user_id`, e.g. `U-02291`) | — | Pack uses `U-…` IDs |
| `trigger.event`, `.received_at`, `.event_age_s` | same | `event_id`, `deliver_at`, `occurred_at_corrected`, `is_duplicate`, `is_stale` | Idempotency and freshness must be inspectable |
| `goal.id`, `goal.deadline` | same (`id` = goal name, as in the brief; [D-025](decisions.md)) | `goal_id`, `name`, `start`, `success_signal`, `rule_version` | Bullet 6 requires the success signal; rule versioning needed for the G03 correction |
| `action` | same, upper case (`HOME_PAGE`, `PUSH`, `WHATSAPP`, `IN_APP_MESSAGE`, `NO_ACTION`) | `no_action_reason` | Bullet 1: explicit reason for doing nothing |
| `dialect.used`, `.source`, `.conflicts` | same | `resolution` | Bullet 5: how the conflict was resolved |
| `content` | same (`title:<id>` refs) | — | — |
| `confidence` | same | `confidence_reason` | Bullet 3 |
| `evidence` | same (string refs, e.g. `event:<id>`) | `evidence_detail: [{ref, observed_at, age_s}]` | Bullet 2: freshness per evidence item; keeps the baseline list unchanged |
| `assumptions` | same | — | — |
| `checks.passed`, `.failed` | same | `verifier_verdict` (`approve` / `downgrade` / `reject`) | Shows the independent verifier's decision |
| `decided_in_ms` | same | `published_at`, `model_calls`, `tool_calls`, `fallback_used` | Latency and budget auditability |
| `status` | same | — | — |

**FR-13.4 Full extended example [PROPOSED]:**

```json
{
  "decision_id": "D-0417",
  "viewer_id": "U-02291",
  "trigger": {"event_id": "...", "event": "app_open", "received_at": "...Z", "deliver_at": "...Z", "event_age_s": 9},
  "goal": {"id": "first_full_episode_3d", "goal_id": "G02", "start": "...Z", "deadline": "...Z",
           "success_signal": "episode_completed any episode", "rule_version": "2026-09-30"},
  "action": "HOME_PAGE",
  "no_action_reason": null,
  "dialect": {"used": "d3", "source": "app_setting", "conflicts": ["profile:d1", "deeplink:d5"], "resolution": "..."},
  "content": ["title:T0412", "title:T0977", "title:T1203"],
  "confidence": 0.61,
  "confidence_reason": "...",
  "evidence": ["event:E88123", "catalogue:T0412", "goal_rule:G02@2026-09-30"],
  "evidence_detail": [{"ref": "event:E88123", "observed_at": "...Z", "age_s": 3600}],
  "assumptions": ["..."],
  "checks": {"passed": ["eligibility", "dialect", "frequency_cap"], "failed": []},
  "verifier_verdict": "approve",
  "fallback_used": null,
  "model_calls": 0,
  "tool_calls": 7,
  "decided_in_ms": 640,
  "published_at": "...Z",
  "status": "PUBLISHED"
}
```

Status values **[PROPOSED]**: `PUBLISHED`, `NO_ACTION`, `REJECTED_BY_VERIFIER`, `DUPLICATE_IGNORED`, `STALE_SKIPPED`,
`HOLDOUT_SHADOW`, `FAILED_LATE`.

Actions go to `actions.jsonl` in the **exact simulator schema** from the pack README (`action_id`, `decision_id`,
`user_id`, `trigger_event_id`, `type`, `published_at`, `dialect`, `hero_title_id`, `rows` / `send_at`, `title_id`,
`text`, `deeplink` / `template_id`, `variables`).

---

## 5. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-1 | Publish each decision **≤ 60 s** after its trigger in replay; > 5 min = failure. **Target: a few seconds**, because the user reaches home 3–40 s after opening (§3.5.4). |
| NFR-2 | Budget: ≤ 8 model calls and ≤ 20 tool calls per decision, configurable. |
| NFR-3 | Reproducible: same inputs + config → same decisions (deterministic mock, fixed ordering). |
| NFR-4 | Runs without any personal API key (mock provider default). |
| NFR-5 | One command replays the full trigger script end to end. |
| NFR-6 | Generalises: no hard-coded users, titles, times or seeds; works on an unseen replay day. |
| NFR-7 | Auditability: decisions, evidence and state are human-readable and inspectable per viewer. |
| NFR-8 | Privacy: the pack never leaves the machine except to approved no-retention services. |
| NFR-9 | Safety invariants (holdout, consent, quiet hours, cap, dialect, availability) are enforced by the verifier **and** covered by tests. |

---

## 6. Goals: interpretation

### 6.1 Measurability

| Goal | Name | Measurable? | Notes **[PROPOSED]** |
|---|---|---|---|
| G01 | first_play_24h | Yes | Signup within 7 days and no prior `play_started` |
| G02 | first_full_episode_3d | Yes | No prior `episode_completed` |
| G03 | second_day_return_2d | Yes, **rule corrected at 08:30 UTC** | Success becomes `play_started` on a later IST day (applies to open goals too) |
| G04 | trial_to_paid_7d | Yes | Only 4 trial users in the snapshot; must check against fresher events |
| G05 | first_paid_7d | Yes | "Never had a paid plan": verify with purchase/renewal history, not only profile status |
| G06 | reactivate_lapsed_14d | Yes, **eligibility underspecified** | Lapse event within 30 days ([D-014](decisions.md)) |
| G07 | resume_series_3d | Yes, **eligibility text-only** | Derive: show/micro_drama started, unwatched episodes left (episodes.csv), no play of that slug ≥ 3 days |
| G08 | try_new_release_7d | Yes | "Released in the 30 days before goal start" uses catalogue `release_date` |
| G09 | weekly_habit_14d | Yes | Active subscriber with plays on ≤ 1 IST day in last 14 days |
| G10 | meaningful_engagement_7d | **No**: `success.event = null`, "engages meaningfully" undefined | **Never selected** |
| G11 | tv_activation_14d | **No**: `tv_app_open` does not exist in the event stream | **Never selected** |
| G12 | return_after_absence_7d | Yes | No `app_open` in the 14 days before the trigger; offline-buffered opens must be handled carefully |

### 6.2 Notes
- Brief: "two rules are underspecified and one success signal does not exist". My reading: underspecified =
  **G06** ("recently") and **G07** (free text) or **G10** ("engages meaningfully"); missing signal = **G11**. G10 is
  also unmeasurable. Decided: [D-013](decisions.md), [D-014](decisions.md).
- The goal window starts when the agent **commits** to the goal (`goals.json` notes).
- One active goal per user at a time.

### 6.3 Goal priority when several apply
**Decided → [D-019](decisions.md)**: G04 → new-user funnel (G01 → G02 → G03) → G07 → G12 → G02 (existing users) → G08 → G09 → G06 → G05.

---

## 7. Interventions: rules summary

| Channel | Key rules |
|---|---|
| home_page | One dialect per page; app shows it only for its current dialect; TTL 120 min; hero ≤ 1; rows ≤ 6; 3–10 titles/row; unique slug; titles in dialect + available at publish; no Continue Watching |
| push | Consent `notifications_opt_in` (+ opt-out events); quiet hours 22:00–08:00 IST; shared cap 1/IST day; title ≤ 40, body ≤ 120; forbidden phrases; real prices only; deeplink `ott://title/<id>`; delivery only confirmed by `message_delivered` |
| whatsapp | Consent `whatsapp_opt_in`; same quiet hours and shared cap; only `wa_new_episode_v2`, `wa_recommend_title_v1`, `wa_trial_ending_v1`; variables from catalogue/pricing |
| in_app_message | Shown at next app open; max 1/day; no consent needed; **no modelled effect in the simulator** |
| no_action | Always allowed; must be recorded with a reason |

---

## 8. Surprise events: required behaviour

| Surprise | In this pack | Required behaviour |
|---|---|---|
| Offline-buffered old app opens | 246 triggers > 1 day old | Detect by true age; no page/message; record `STALE_TRIGGER` |
| Purchase right after a decision | 38 live purchases | Update state, close/replace goal, cancel stale pending messages |
| 3 different dialects | 56 triggers | Use app setting for the page; list conflicts and resolution |
| Goal rule corrected | G03 at 08:30 UTC | Version rules; re-judge only open G03 goals |
| LLM unsupported eligibility claim | 3 G04 faults | Verifier rejects (no supporting event); log as unsupported claim |
| Malformed model output / timeout | 1 + 1 faults | Safe parse, retry within budget, deterministic fallback |
| Catalogue injection | T0002, T0522 | Treat as content; flag; never act on it |
| Model provider down | 10:35–10:42 UTC | Rules-only path; keep latency |
| Data store degraded | 13:10–13:17 UTC | Timeouts + cached state; downgrade or no action |
| Title withdrawn | T0001 at 07:00 UTC | Stop using; handle live pages / pending messages containing it |
| Traffic ×3 at evening peak | 15:00–15:10 UTC | Concurrency, prioritise fresh triggers, skip optional model calls |
| Invalid dialect values | 178 unique triggers (`None`/`NA`/`d7`) | Treat as missing; fall back per FR-3 or no page |

The evaluators check that the system **notices** changed conditions, finds **which viewers and decisions are
affected** and responds **safely and on time**, not that it predicts every surprise.

---

## 9. Deliverables

```
submission/
  README.md               setup + ONE command that replays the trigger script
  ARCHITECTURE.md         ownership, planning, memory, verification, latency, failure recovery, trust boundaries
  AI_COLLABORATION.md     how AI tools helped, how output was checked, what was rejected
  KNOWN_LIMITATIONS.md    what was intentionally left incomplete and why
  src/                    runnable CLI or API, provider abstraction + mock mode
  tests/                  automated tests (§10)
  sample_run/
    decisions.jsonl       every decision incl. no-action reasons
    viewer_state/         durable per-viewer state
    actions.jsonl         simulator input
    outcomes.jsonl        goal judgements
    latency_report.json
    impact_report.md      agent arm vs holdout + pilot re-analysis
    final_report.md
```

---

## 10. Tests

**Required by the brief (minimum):**

| ID | Test |
|---|---|
| T-1 | Late or duplicate trigger → no duplicate action; stale trigger handled |
| T-2 | Conflicting source → resolved by the documented precedence; conflict visible in output |
| T-3 | Tool failure (model or data store) → safe fallback within budget |
| T-4 | Corrected goal rule → only affected open goals re-judged |

**Recommended extras:** holdout gets nothing · quiet hours · daily cap across channels · consent + opt-out override ·
injected catalogue instruction ignored · unsupported model claim rejected · malformed model output · row < 3 titles
dropped · slug uniqueness · withdrawn title not used after withdrawal · purchase after decision cancels pending
message · unmeasurable goals never selected · price/forbidden-phrase copy checks.

---

## 11. Acceptance criteria (definition of done)

- [ ] Replay runs with **one command** on a fresh clone, without a personal API key.
- [ ] Simulator runs on our `actions.jsonl` with **0 policy problems**.
- [ ] Holdout users received zero actions.
- [ ] No push/WhatsApp in quiet hours, without consent, or > 1 per IST day.
- [ ] All titles in the right dialect, available, never withdrawn at publish time.
- [ ] Every decision has evidence (with freshness), confidence + reason, assumptions, conflicts, checks, timing,
      deadline and success signal.
- [ ] All decisions within 60 s; latency report included.
- [ ] Impact measured agent arm vs holdout arm with uncertainty; pilot mistake explained.
- [ ] Required tests (T-1…T-4) pass.
- [ ] All four docs written; `sample_run/` complete.

---

## 12. Out of scope (unless discussed)

- UI / dashboard.
- Training ML models or a learned recommender.
- Real message sending or real platform integration.
- Running on real (non-pack) data.

---

## 13. Technical discussion preparation (60 min)

Be ready to walk **one viewer from app open to judged outcome** and answer:
- How does the agent decide what it knows is current enough to act on?
- When should it do nothing, and how do you know that was right?
- How do you show causation rather than selection of already-likely converters?
- What happens when 1,000 viewers open the app in the same minute?
- Which decisions should never be automated?
- How would it work for 10 M viewers and 40 dialects?
- What did an AI assistant suggest that you rejected?

---

## 14. Open questions (to discuss before any implementation)

| # | Question | My proposal |
|---|---|---|
| Q1 | Language / stack? | **Answered → [D-017](decisions.md)**: Python ≥ 3.10, stdlib runtime ([techstack.md](techstack.md)) |
| Q2 | State storage? | **Answered → [D-017](decisions.md)**: SQLite + JSON export |
| Q3 | Is the project folder the submission root? | **Answered → [D-018](decisions.md), [D-020](decisions.md)**: folder is `submission/` |
| Q4 | Use git for this repo? | **Answered → [D-026](decisions.md)** |
| Q5 | What is the LLM used for, if anything? | **Answered → [D-022](decisions.md)** |
| Q6 | Real model provider at all, or mock only? | **Answered → [D-003](decisions.md)**: mock default + optional local Ollama |
| Q7 | Goal priority order (§6.3)? | **Answered → [D-019](decisions.md)** |
| Q8 | Threshold for "recently lapsed" (G06)? | **Answered → [D-014](decisions.md)** |
| Q9 | Stale-trigger threshold? | **Answered → [D-006](decisions.md)** |
| Q10 | Quiet hours: schedule `send_at` for 08:00 IST or skip the message? | **Answered → [D-021](decisions.md)** |
| Q11 | Use messages at all, given their tiny effect and opt-out risk? | **Answered → [D-023](decisions.md)** |
| Q12 | Same viewer, several fresh triggers within the 120-min TTL: re-publish or reuse? | **Answered → [D-024](decisions.md)** |
| Q13 | Holdout shadow decisions for ITT measurement? | **Answered → [D-016](decisions.md)** |
| Q14 | Concurrency model for bursts? | **Answered → [D-017](decisions.md)**: modelled worker pool on a virtual clock + priority lanes |
| Q15 | `goal.id`: goal name (`first_full_episode_3d`, as in the brief) or catalogue ID (`G02`)? | **Answered → [D-025](decisions.md)** |
| Q16 | Do no-action, duplicate and stale decisions also get a full decision record? | **Answered → [D-004](decisions.md)** |

---

## Change log

| Date | Change |
|---|---|
| 2026-10-18 | Initial draft from the brief, the pack README, the configs, data profiling and simulator source. |
| 2026-10-18 | All open questions answered (Q4, Q5, Q10, Q11, Q12, Q15 → D-021 to D-026). |
| 2026-10-18 | Q3, Q7 answered (D-018–D-020); project folder renamed to `submission/`. |
| 2026-10-18 | Q1, Q2, Q8, Q9, Q13, Q14, Q16 marked answered (D-004, D-006, D-014, D-016, D-017). |
| 2026-10-18 | FR-13: added the brief's decision output verbatim as the baseline contract, a field mapping and Q15–Q16. |
