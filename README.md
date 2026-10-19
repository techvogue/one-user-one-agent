# One User, One Agent

An agentic system that gives every viewer of a regional-dialect OTT platform one dedicated agent. On each app open the
agent works out what it knows about the viewer (with sources and freshness), chooses one measurable goal from evidence,
decides what to change (home page, one reminder, or nothing, always with a reason), has the decision checked by an
independent verifier, publishes within the latency budget, later judges whether the goal was reached, and measures its
own effect against the randomized holdout.

## Quick start

Requires **Python ≥ 3.10** (tested on 3.10) and **no runtime dependencies**. No API key is needed: the model is a
deterministic mock by default. The data pack must sit next to this folder at `../one_user_one_agent_pack/`.

```bash
# 1. set up (uv or plain pip)
uv venv --python 3.10 .venv && uv pip install --python .venv/bin/python -e '.[dev]'
#   or: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'

# 2. run everything: replay -> the pack's simulator -> judge -> impact report -> final report (about 1 minute)
.venv/bin/python -m ott_agent run --pack ../one_user_one_agent_pack --out sample_run

# 3. run the tests (221)
.venv/bin/python -m pytest -q
```

`run` does five steps; each also exists on its own:

| Command | What it does | Writes |
|---|---|---|
| `replay` | **Replays the trigger script**: loads the 30-day history, plays the replay day in delivery order (triggers, live events, control events, injected model faults), decides and publishes | `decisions.jsonl`, `actions.jsonl`, `control_log.jsonl`, `latency_report.json`, `data_quality_report.json`, `viewer_state/` |
| *(simulator)* | The pack's `simulator/simulate_outcomes.py` on our `actions.jsonl` | `sim/outcome_events.jsonl`, `sim/simulator_report.json` |
| `judge` | Judges every goal from the outcome events; stores outcomes and learning notes | `outcomes.jsonl`, adds to `viewer_state/` |
| `report` | Agent arm vs randomized holdout (+ 3 extra simulator seeds, pilot re-analysis) | `impact_report.md`, `impact.json` |
| `final` | One-page summary built only from the files above, incl. every no-action reason | `final_report.md` |

Other commands: `load` (history only + data quality report) and `inspect` (what the agent knows about one viewer at a
moment, which goal it would choose and why, and the page/reminder it would publish for a given app open):

```bash
.venv/bin/python -m ott_agent inspect --pack ../one_user_one_agent_pack --viewer U-04215 --at 2026-10-01T10:00:00Z
.venv/bin/python -m ott_agent inspect --pack ../one_user_one_agent_pack --viewer U-01991 \
    --trigger 202cccb8-8127-465f-b2d0-038cf533197e        # add --json for the full record
```

Options: `--provider ollama --ollama-model qwen2.5:3b-instruct` uses a local model instead of the mock
([D-003](doc/decisions.md)); `--seed` sets the simulator seed; `--config file.json` overrides any threshold in
`src/ott_agent/config.py`.

## Results at a glance (sample run)

| | |
|---|---|
| Triggers decided | 2,769: 810 pages published, 1,016 holdout shadows (nothing published), 271 no action with a reason, 424 stale app opens skipped, 90 duplicates ignored, 158 second triggers of the same visit |
| Latency | published pages p50 0.84 s, max 2.2 s; **0 decisions over 60 s**; max 2 model / 6 tool calls per decision (limits 8 / 20). Load test, 1,000 viewers in one minute: all decided safely, 0 over 60 s, pages after ~10 s with default capacity (ARCHITECTURE §7) |
| Safety | pack simulator: **0 policy problems**, 822/822 pages accepted (incl. 12 republished after a withdrawal); independent rules audit of every published action: 0 violations; 0 actions for holdout viewers |
| Injected surprises | false model eligibility claim rejected; broken JSON retried then rules only; 45 s model hang cut at 3 s; model and data-store outages handled with fallbacks; withdrawn title removed from 12 live pages; G03 rule correction applied to open goals only; purchases re-plan the buyer's goal when they arrive |
| Learning | results feed back: a goal that recently failed for the viewer (pilot or our own judge) is not repeated (D-051): 26 agent-arm decisions got another goal, 98 got "do nothing" (holdout shadows: 24 / 88) |
| Pages and messages | pages carry up to 5 real rows (avg 4.3; D-053); 129 reminders (17 % of agent viewers): 92 "continue your series" for G07 by WhatsApp only (D-054), 37 for G12 |
| **Impact** | first goal succeeded: agent **16.9 %** vs holdout **14.8 %**, **+2.1 pts [−1.9, +6.0]**: **no detectable effect**; all 3 other simulator seeds positive, none significant; any play within 7 days +3.7 pts [−1.0, +8.3]. The biased "served vs everyone else" comparison would claim a significant +4.9 pts [+0.9, +8.9] on the same data. |

## Where to find what

| You want to see | Look at |
|---|---|
| Every decision in the brief's format (goal, action, dialect + conflicts, content, confidence + reason, evidence + freshness, assumptions, checks, timing, status) | `sample_run/decisions.jsonl` |
| Published actions (simulator schema) | `sample_run/actions.jsonl` |
| What one viewer's agent knew, decided, observed and learned | `sample_run/viewer_state/<user_id>.json`, or `inspect` |
| Data issues found, how each was handled and the tests that prove it (13 traps) | `sample_run/final_report.md` §7; `sample_run/data_quality_report.json` → `traps`; [doc/data_traps.md](doc/data_traps.md) |
| Withdrawals, rule corrections, outages, reactions to live events | `sample_run/control_log.jsonl` |
| Latency, budgets, traffic bursts, model outcomes, fallbacks | `sample_run/latency_report.json` |
| Goal verdicts and learning notes | `sample_run/outcomes.jsonl` |
| Impact vs holdout, why served-vs-not misleads, pilot re-analysis | `sample_run/impact_report.md` |
| Overall summary, incl. every no-action reason with counts | `sample_run/final_report.md` |
| Design | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Every design decision, with options and reasons (D-001 … D-056) | [doc/decisions.md](doc/decisions.md) |
| How AI tools were used, checked, corrected and rejected | [AI_COLLABORATION.md](AI_COLLABORATION.md) |
| What was left out on purpose | [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) |

## Required tests

| Required by the brief | Tests (in `tests/`) |
|---|---|
| A late or duplicate trigger | `test_duplicate_trigger_creates_no_second_decision_or_action`, `test_stale_trigger_is_skipped_without_model_or_action`, `test_same_session_is_one_visit_unless_something_changed`, `test_reingesting_same_file_is_a_noop` |
| A conflicting source | `test_three_dialects_page_uses_app_setting_and_lists_conflicts`, `test_newer_purchase_overrides_never_profile`, `test_status_cannot_regress_from_paid_to_never`, `test_opt_out_event_overrides_profile_consent` |
| A tool failure | `test_model_timeout_is_cut_at_the_call_timeout`, `test_malformed_model_output_is_retried_then_rules_only`, `test_model_unavailable_window_means_rules_only`, `test_data_store_down_falls_back_to_a_generic_page_without_message_or_goal` |
| A corrected goal rule | `test_rule_correction_updates_only_open_goals_of_that_type`, `test_corrected_goal_rule_is_used_for_judging` |

Also covered: quiet hours, frequency cap, holdout gets nothing, injected instruction ignored, unsupported model claim
rejected, rows with fewer than 3 titles never published, every row holds what its type says, withdrawn titles not used, every decision record complete,
verifier rejections (mutation-tested), a recently failed goal not repeated (pilot and own judge), and an end-to-end
`run` that checks the final report.

## Repository layout

```
submission/
  README.md  ARCHITECTURE.md  AI_COLLABORATION.md  KNOWN_LIMITATIONS.md
  doc/            requirements.md, rules.md, decisions.md, data_traps.md, techstack.md, work_plan.md
  pyproject.toml  package metadata (no runtime dependencies; pytest + ruff for development)
  src/ott_agent/  ingest, store, facts, goals, planner, intervention, model, tools, verifier,
                  harness, judge, past, impact, final, traps, quality, … (see ARCHITECTURE.md §1b)
  tests/          221 tests
  sample_run/     generated by `run`, never hand-written
```

## Data and privacy

The data pack is **read-only** and is the only evidence about viewers and titles. It is treated as personal data:
nothing is sent to any external service (the optional Ollama model runs locally). Text in the data (titles,
descriptions, search queries, deeplinks) is content, never instruction.
