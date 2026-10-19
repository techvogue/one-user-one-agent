# Known Limitations: One User, One Agent

> **Status: DRAFT (living document).** Updated as scope decisions are made. Each item says what was left out or
> simplified, **why**, and what it would take to do it properly.

## Deliberate scope choices

| # | Limitation | Why | What a full version would do |
|---|---|---|---|
| L-1 | **G10 and G11 are never selected** | No measurable success signal (G10 has none; G11's `tv_app_open` never occurs). Inventing a definition would move the goalposts (D-013) | Agree a definition with the analytics owner, then enable via the same measurability check |
| L-2 | **Interpretations of G06 and G07** are our assumptions (`A-G06-1`: lapse event ≤ 30 days; `A-G07-1`) | The goal catalogue leaves them unclear (D-014) | Confirm with the goal owner; thresholds are configurable |
| L-3 | **Lapsed users without a lapse event are not eligible for G06** | Recency cannot be proven (no `plan_ends_at` for lapsed users) | Use billing-system history if available |
| L-4 | **Users with unknown consent get no push/WhatsApp** | Default-deny is the safe reading of the policy (D-008) | Read consent from the source of truth in real time |
| L-5 | **Flagged catalogue titles are quarantined, not auto-cleared** | A tampered catalogue entry needs human review (D-012) | Review workflow and audit trail |
| L-6 | **Pattern detector for injected instructions is shallow** | It is for visibility only; the real defence is that free text never reaches decision logic or the LLM (D-012) | Not needed for safety; better detection only for reporting |
| L-7 | **Local LLM is optional and off the critical path** | CPU-only inference takes ~3–10 s per call; privacy forbids free hosted APIs (D-003) | Faster hosted model with a no-retention agreement |
| L-8 | **Final verdict at deadline + 7 days covers ~99 % of late events** | Trade-off between completeness and learning speed (D-005) | Longer final window or re-statement process |
| L-9 | **Latency is simulated on a virtual clock** (real compute time is measured; dependency delays, timeouts and queueing are simulated) | Real sleeps would make the replay slow and non-deterministic (D-017) | Run against real services under load tests |
| L-10 | **Single-process SQLite** | Enough for the replay; zero setup for evaluators (D-017) | Postgres + KV store + queue partitioned by viewer |
| L-11 | **Any opt-out event withdraws consent, even one older than the snapshot** | The data has no opt-in event, so re-consent cannot be proven; the conservative reading of policy rule 1 | Consume a consent-change stream with both directions |
| L-12 | **Profile rows without a user id or `updated_at` are ignored** | They cannot be attributed or ordered (D-007); none exist in this pack | Quarantine table + data-owner alert |

## Pending (to be filled in as decisions are made)

- Reactions to live events (D-048) re-plan goals and re-check reminders but never publish a page: without a visit
  there is nothing to show it in. Only purchase, renewal, lapse, opt-out and dialect-change events trigger them.
- With 4 workers the evening peak never queued, so the "skip the model under load" path ran only in its test, not in
  the replay.

- **Impact: no detectable effect** on one replay day (+1.2 pts, 95 % interval −2.5 to +4.9). With about 720 judged
  viewers per arm the study can only detect effects larger than about ±3.7 points; more replay days would be needed.
- Outcomes come from the pack's simulator; the effect size reflects its behaviour model, not real viewers.

- Goals with 14-day windows (G06, G09) end on the last observed day, so most are INCONCLUSIVE: the deadline + 24 h grace
  is never observed. A longer observation period would turn them into results.
- A per-viewer SUCCEEDED does not mean the agent caused it (e.g. scripted organic purchases); the agent's effect is only
  measured agent arm vs holdout (Step 7). Learning notes record observations, never causes.
- Judging happens once after the simulator (with the data known at its end); in production it would run whenever new
  events arrive. The function supports that (pure, as-of time) and re-judging is tested.

- The verifier is separately written but implements the **same documented interpretations** (A-G0x-1, D-0xx) and
  shares the event store and config thresholds; a misreading of the spec itself would be shared. Real-case unit tests
  and the pack simulator are the outside checks.
- Verifier rejections in the final replay are 0 because the one disagreement it found (D-041) was fixed; its ability to
  reject is shown by tests and mutation testing, not by the replay.

- The optional Ollama provider is implemented (HTTP, JSON-schema output) but was not run in this environment; all
  results use the deterministic mock, as the brief requires for evaluators.
- With our 2 s store timeout, the degraded window (p95 8 s) fails most reads, so most decisions in it use the generic
  fallback page (12 of 13 in the replay).
- The send-time re-check can only use events received by the end of the replay day; reminders are sent the next
  evening, so anything that happens overnight is unknown to the replay.
- Same-session re-decisions are triggered by a fixed list of state-changing event types (D-039).

- WhatsApp `dialect_name` is filled with the catalogue's dialect code (e.g. `d3`): the pack has no dialect names and
  template variables may only come from the catalogue or pricing (D-034).
- Push copy is a fixed rule-based text per app language (no model); the optional Ollama copywriter (D-022) is not
  wired in yet.
- A `next_episode` row needs 3+ unfinished series in the page dialect, which is rare, so G07 pages usually carry the
  series as the hero instead. Pages in thin dialects (d3: 13 titles) may have fewer rows (118 of 2,086 pages have 1 row).
- The per-day message cap and "page still live" checks need state across triggers; the replay harness enforces both
  (verified: no viewer gets two messages on one IST day); Step 5's verifier re-checks them independently.

- An open goal whose success already happened (e.g. the G07 series was resumed) stays open until its deadline; detecting
  success early is part of outcome judging (guide Step 6). Status-type goals (G04/G05/G06) and "first" goals (G01/G02)
  are already re-planned as soon as their condition changes.

- G01/G03 need a provable signup time. Brand-new viewers sign up in the replay's live events, so these goals only
  become available once the replay harness ingests live events (Step 4); a history-only dry run picks G01 for 23
  users.

- The "event time before signup" plausibility check (D-005 choice 4) needs the viewer's signup fact, so it runs in the
  fact resolver, not at ingestion.
- History ingestion takes ~17 s for 143k lines (row-by-row SQLite inserts). Acceptable for the replay; batching would
  speed it up if needed.

- Anything cut for the 10–12 hour budget.

---

## Change log

| Date | Change |
|---|---|
| 2026-10-18 | Draft created from decisions D-003 to D-016. |
| 2026-10-18 | L-9, L-10 added after D-017 (tech stack). |
| 2026-10-18 | L-11, L-12 added in the Step 1 review. |
