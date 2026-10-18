# Tech Stack: One User, One Agent

| Item | Value |
|---|---|
| Status | **Accepted** ([D-017](decisions.md); LLM provider: [D-003](decisions.md)) |
| Date | 2026-10-18 |

## Summary

| Layer | Choice | One-line reason |
|---|---|---|
| Language | **Python ≥ 3.10** | Same language as the given simulator; best for data, stats and LLM work |
| Runtime dependencies | **Standard library only** | Evaluators install nothing; maximally reproducible |
| State store | **SQLite** (+ JSON export per viewer) | Built in, durable, transactional; unique keys give idempotency; indexed queries find affected viewers |
| Time model | **Virtual clock, deterministic event loop** | Simulates 45 s timeouts and outages without waiting; same input → same output |
| Concurrency | **Modelled worker pool on the virtual clock + priority lanes** | Shows burst behaviour (×3 traffic) deterministically |
| Interface | **argparse CLI** (`python -m ott_agent ...`) | One-command replay; no server to run |
| LLM | **`ModelProvider` interface: mock/replay default + optional local Ollama** (D-003) | No API key; free; private |
| Ollama client | **HTTP via `urllib`** (standard library) | No extra package even for the optional real model |
| Agent framework | **None: plain Python with explicit stages** | Auditable, deterministic, testable control flow |
| Config | **JSON files** | Parsed by the standard library on every supported Python version |
| Statistics | **`math` / `statistics`** (standard library) | Two-proportion CI and per-goal tables are ~20 lines |
| Tests | **pytest** (dev only) | Concise tests, fixtures for small synthetic packs |
| Lint / format | **ruff** (dev only, optional) | Fast; one tool |
| Packaging | **`pyproject.toml`**; `uv` locally, `pip` documented | Evaluators can use plain `pip` / `python` |

---

## What drove the choices

1. **Evaluators must run it easily**: fresh clone, no API key, rerun with a new simulator seed and an unseen replay
   day (NFR-4, NFR-5, NFR-6).
2. **Determinism and auditability**: every decision must be reproducible and explainable (NFR-3, NFR-7).
3. **Latency**: the fast path must finish in seconds; the harness must simulate outages and 45 s timeouts
   (NFR-1, FR-9).
4. **Privacy**: the pack never goes to services that retain or train on data (NFR-8, rules.md Rule 3).
5. **Budget**: 10–12 hours. Every extra moving part costs time.

Local environment checked on 2026-10-18: Python 3.14.4, SQLite 3.46.1, uv, Node 24, Docker; 12-core CPU, 14 GB RAM,
integrated AMD GPU (no CUDA).

---

## 1. Language: Python ≥ 3.10

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Python** | Simulator is Python, so evaluators already have it; best data/stats/LLM tooling; pytest | Slower raw speed (irrelevant at this data size: 143 k events, 5 k users) | ✅ |
| TypeScript / Node | Good async, types | Weaker data/stats tooling; evaluators need a second runtime | ❌ |
| Go | Fast, strong concurrency | Verbose; slower to build in 12 h; weaker for analysis | ❌ |

- **Minimum version 3.10** although 3.14 is installed locally: the simulator supports 3.9+, and evaluators may have an
  older Python. We avoid features newer than 3.10 (e.g. `tomllib` from 3.11, which is why config is JSON).
- Type hints and `dataclasses` throughout for readability.

## 2. Runtime dependencies: standard library only

| Option | Verdict |
|---|---|
| **Standard library at runtime; pytest/ruff for development only** | ✅ Nothing to install to run the replay |
| + pydantic (schema validation) | ❌ Dataclasses + a small validator cover the decision schema |
| + pandas / numpy | ❌ Heavy; the 56 MB event file loads fine with `json`; CI maths is trivial |
| + requests / ollama package | ❌ `urllib` is enough for Ollama's HTTP API |

Consequence: slightly more hand-written code (JSON (de)serialisation, a small CI function), in exchange for zero
install friction and fully pinned behaviour.

## 3. State store: SQLite + JSON export

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| JSON file per viewer | Simple, readable | No transactions; "find all open G03 goals" means scanning every file; fragile under concurrency | ❌ (kept only as an export) |
| **SQLite** | Built in; durable; transactional; **unique keys give idempotency** (D-004); **indexed queries find affected viewers** (rule correction, withdrawal) without reprocessing everyone | Single writer (fine for the replay) | ✅ |
| Postgres / Redis | Production-like | Needs Docker or a server: evaluator friction | ❌ (scale-out story only) |

- SQLite is the **source of truth**; `sample_run/viewer_state/<user_id>.json` is a readable export for evaluators.
- WAL mode, one connection per process, explicit transactions per decision.
- Scale-out story for the discussion: Postgres (state) + Redis/KV with TTL (idempotency keys) + a partitioned queue
  by `user_id` (one owner per viewer).

## 4. Time model: virtual clock

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| Real sleeps with threads | Realistic | A 45 s timeout really waits; outages take minutes; slow, flaky, non-deterministic | ❌ |
| asyncio with real timeouts | Real concurrency | Extra complexity; still slow and non-deterministic in replay | ❌ |
| **Virtual clock** | Harness advances simulated time; injected delays (timeouts, slow store) are added to the measured compute time; fast and reproducible | Must be clearly documented as a simulation | ✅ |

- `published_at = deliver_at + measured compute time + simulated dependency delay + queueing delay`, as the pack README
  asks ("record each action's `published_at` as `deliver_at + your measured processing time`").
- Real compute time is still measured with `time.perf_counter()`, so slow code shows up in the latency report.
- Control events (`dependency_unavailable`, `dependency_degraded`) and model faults (`timeout`) are applied on the
  virtual clock.

## 5. Concurrency: modelled worker pool + priority lanes

- A configurable number of workers is modelled on the virtual clock, so a burst (240 triggers in 10 min; ×3 traffic
  at the evening peak) produces **queueing delay** that appears in the latency report.
- **Priority lane** for fresh triggers; **cheap lane** for stale triggers (fact update only, no model calls)
  ([D-006](decisions.md)).
- Under load, optional model calls are skipped first.
- Deterministic: the same input always gives the same schedule and the same latencies (except measured compute time,
  which is reported).

## 6. Interface: argparse CLI

| Option | Verdict |
|---|---|
| **argparse CLI** | ✅ The brief accepts a CLI or API; one command replays everything |
| FastAPI service | ❌ Server lifecycle and extra dependencies for no benefit in a replay |

Planned commands (names may change during implementation):

```
python -m ott_agent replay  --pack ../one_user_one_agent_pack --out sample_run [--provider mock|ollama] [--seed ...]
python -m ott_agent judge   --out sample_run          # outcome verdicts from simulator events
python -m ott_agent report  --out sample_run          # impact, latency, data quality, final report
python -m ott_agent inspect --out sample_run --viewer U-00042 [--at 2026-10-01T10:00:00Z]
```

## 7. LLM: mock by default, optional local Ollama (D-003)

- `ModelProvider` interface with two implementations:
  - **Mock/replay (default)**: deterministic; reproduces `model_faults.jsonl` (unsupported claim, malformed JSON,
    45 s timeout) at the named triggers; obeys `dependency_unavailable` windows.
  - **Ollama (optional)**: local HTTP API via `urllib`; JSON-schema constrained output; default model
    **Qwen 2.5 3B Instruct** (configurable; P-003 still to confirm scope).
- The LLM is **off the critical path**: the page decision never needs it. Its output always goes through the
  verifier and is never treated as evidence.
- Rejected: Anthropic API (paid); free hosted tiers (privacy: may retain or train on inputs).

## 8. No agent framework

| Option | Verdict |
|---|---|
| LangChain / LangGraph / CrewAI | ❌ Hide control flow; add latency and dependencies; harder to audit and make deterministic; the brief penalises "a single prompt that picks titles" |
| **Plain Python, explicit stages** | ✅ resolve facts → plan goal → plan intervention → verify → publish → judge; each stage visible, timed and testable |

## 9. Tooling

| Tool | Use |
|---|---|
| `pyproject.toml` | Package metadata; dev extras (`pytest`, `ruff`) |
| `uv` | Local environment and running (fast) |
| `pip install -e .[dev]` | Documented for evaluators who don't use uv |
| pytest | Unit and scenario tests (the four required tests plus extras) |
| ruff | Lint and format (optional) |
| Makefile (optional) | `make replay`, `make test` shortcuts |

---

## Consequences and risks

| Risk | Mitigation |
|---|---|
| Virtual clock could be seen as "faking" latency | Documented clearly; real compute time measured and reported separately |
| SQLite single writer | Single-process replay; scale-out story documented |
| Standard-library-only means more hand-written code | Small, well-tested helpers; budget accounted for |
| Python 3.10 compatibility while developing on 3.14 | Avoid newer features; run the tests on Python 3.10 via `uv` (approved 2026-10-18) |
| Ollama slow on CPU | Optional and off the critical path; timeouts fall back to rules |

---

## Change log

| Date | Change |
|---|---|
| 2026-10-18 | Created; stack accepted (D-017). |
