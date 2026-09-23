> **This is the product README carried over from upstream LCP.** The plan for this repo is in
> [`README.md`](README.md). Kept for reference until the simplification lands; delete it then if you want.

# LLM Control Plane (LCP)

**A self-hosted LLM gateway. Route, meter, control — one container, one port, no cloud dependency.**

[![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-ready-brightgreen.svg)](https://hub.docker.com/)
[![CI](https://github.com/aunttwister/lcp/actions/workflows/ci.yml/badge.svg)](https://github.com/aunttwister/lcp/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-2259%20passed-brightgreen.svg)](https://github.com/aunttwister/lcp/actions/workflows/ci.yml)

---

## Contents

- [What is LCP?](#what-is-lcp)
- [Features](#features)
- [Quick Start](#quick-start)
- [VS Code Integration](#vs-code-integration)
- [Configuration](#configuration)
- [Benchmarking (LiveBench)](#benchmarking-livebench)
- [Semantic Dynamic Routing](#semantic-dynamic-routing)
- [Component Runtime](#component-runtime)
- [Why LCP over alternatives?](#why-lcp-over-alternatives)
- [Architecture](#architecture)
- [Dependencies](#dependencies)
- [Test Coverage](#test-coverage)
- [API](#api)
- [Roadmap](#roadmap)
- [Status](#status)

---

## What is LCP?

LCP (LLM Control Plane) is a self-hosted LLM gateway that sits between your clients and LLM
providers. It routes requests, tracks costs, enforces spending limits, and manages API keys —
all from a single Docker container backed by SQLite. No PostgreSQL. No Redis. No external
services.

**Two primary use cases:**

- **AI agents** — Route Hermes, Claude Code, or custom agents through LCP to control which
  tools they can use, how much they can spend, and which providers they hit
- **VS Code / GitHub Copilot** — Point the [GitHub Copilot LLM Gateway extension](#vs-code-integration)
  at LCP to make all your profiles appear as model providers in Copilot Chat with full context
  windows and capabilities

It was built for a production multi-agent homelab setup managing 6 Hermes profiles with 15+
custom skills, where knowing exactly what is being spent, by whom, and with what tools is
critical. The same instance also serves as the LLM backend for daily VS Code Copilot usage.

> ✨ **Headline feature: benchmark-driven routing.** Grade your models with LiveBench (or
> your own benchmark), and LCP classifies every request by task type and routes it to the
> best-fit (provider, model) — balancing capability, cost, circuit-breaker health, and your
> own policy/rules — all tunable from the UI, no restarts. See [Benchmarking](#benchmarking-livebench).

<!--
  Add a real demo here: 2–3 screenshots or a GIF (dashboard, Providers → Routing tab, Usage).
  e.g. ![Dashboard](docs/screenshots/dashboard.png)
-->

```
Clients (agents, VS Code, scripts, curl)
          |
          v
+--------------------------------------------+
|             LCP (:8734)                   |
|                                            |
|  Auth -> Estimate Cost -> Route            |
|       -> Circuit Breaker -> Track Cost     |
|                                            |
|  Dashboard     API Keys     Budgets        |
|  (:8734/)      Alerts      Export          |
+-------------------+------------------------+
                    |
        +-----------+-----------+
        v           v           v
    DeepSeek    OpenCode    llama.cpp
```

## Features

### Intelligent routing
- Provider chains with automatic fallback — if one provider fails, the next in chain takes over
- Circuit breaker with configurable thresholds — degraded providers are probed, dead providers are skipped
- Profile-based routing by URL path: `/l2`, `/l1`, `/career`, `/coder`, `/cron`
- SSE streaming passthrough — real-time token delivery, no buffering
- **Semantic task classification** — every request is classified by *meaning* using an embedding model (bge-small), not exact keywords ([semantic dynamic routing](#semantic-dynamic-routing))
- Benchmark-driven capability routing — grade each model with LiveBench, then route each request by the model's measured task scores ([benchmarking](#benchmarking-livebench))

### Tool permission control
- Strip dangerous tools per profile — an L1 triage agent cannot `terminal`, a cron profile strips everything
- *(Being replaced)* Tool stripping will be decommissioned in favor of a fails-closed permission matrix
- *(Planned)* Permission matrix — declarative `allow` / `block` / `blocked_globally` rules per profile, with audit trail

### Cost tracking
- Per-request cost with DeepSeek cache hit/miss breakdown
- Daily cost summaries per profile, per model, per provider
- Prompt prefix caching — normalizes messages so repeated system prompts hit the provider cache
- Cache hits are 120x cheaper than cache misses; LCP tracks both accurately

### API key management
- Create, rotate, and revoke virtual API keys through the dashboard
- Per-key spend limits with hard-stop enforcement
- Per-profile access control — each key can be scoped to specific profiles
- Usage breakdown per key

### Provider health & credentials
- **Encrypted provider keys** — paste an upstream API key (DeepSeek, OpenCode, etc.) directly in the Providers → Configuration tab; it is encrypted with Fernet using the `LCP_SECRET_KEY` master key and stored in SQLite, never in the git-tracked `gateway.yaml`
- No env vars needed — keys come exclusively from the encrypted credential store (UI-managed)
- **Circuit breaker health** — Providers → Health tab shows live status per provider/profile with failure counts, last error reason, and cooldown timers
- **Reset cooldown** — force a provider back to healthy with one click instead of waiting out the cooldown
- Dashboard shows provider health mini-badges (healthy / degraded / dead counts)

### Dashboard
- Server-rendered HTML — no SPA, no build step, loads instantly
- Daily cost charts with Chart.js — stacked bars, per-profile views
- Provider health monitoring with live status
- Drag-and-drop chain editing — reorder fallback providers in the UI
- Request log with error inspection
- Budget alerts with configurable thresholds

### Plugin architecture
- Provider cost extraction plugins — DeepSeek, OpenCode, Command Code, Local LLM (llama.cpp), OpenAI
- **Command Code plugin** — subscription usage tracking (rolling 5-hour / weekly / monthly usage windows, monthly credits remaining, plan + status, plus billing-period totals: total tokens, total runs, and monthly usage) via a browser session cookie from the credential store, plus cost history from the gateway `requests` table
- Memory module — installable from the Setup page: per-profile semantic memory with an embedded [LanceDB](https://github.com/lancedb/lancedb) backend (columnar vector storage, ANN indexing, no separate service)
- See [features/memory.md](features/memory.md) for the unified memory specification

## Quick Start

```bash
# Clone
git clone https://github.com/aunttwister/lcp.git
cd lcp

# Configure providers — add your API keys
cp config/.env.example config/.env
# Edit config/.env: set LCP_SECRET_KEY (used to encrypt provider keys)
# Provider keys are managed via the dashboard (Providers → Configuration tab)
# Only LCP_SECRET_KEY needs to be set (used to encrypt stored keys).

# Run
docker compose up -d
```

LCP is now running at `http://localhost:8734`. Open the dashboard, send a request:

```bash
curl http://localhost:8734/l2/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-pro",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 50
  }'
```

## VS Code Integration

Use the [**GitHub Copilot LLM Gateway**](https://marketplace.visualstudio.com/items?itemName=arbs-io.github-copilot-llm-gateway)
extension by Andrew Butson to make your LCP profiles appear directly in GitHub Copilot Chat as
model providers. **All your profiles (coder, l2, career, etc.) show up in the Copilot model
picker with their full context windows and capabilities.**

### Setup

1. Install the extension: `arbs-io.github-copilot-llm-gateway`
2. In VS Code settings, search for **"Copilot Llm Gateway"** and set:
   - **Server URL**: `https://lcp.example.com/v1` (or your LCP instance)
   - **API Key**: your LCP API key (from the Keys dashboard)
3. (Optional) **Model Context Windows**: if you need to override server-reported context sizes
4. **Disable "Enable Image Input"** — your LCP profiles use text-only models (deepseek-v4 etc.).
   The gateway already blocks image requests with a clear error as a safety net.

> **Why this extension?** Unlike OAI Copilot (which requires manually configuring each model
> and doesn't support automatic discovery), the LLM Gateway extension fetches `/v1/models`
> from your LCP instance and populates the model picker automatically. Profile entries like
> `coder`, `l2`, and `career` appear with their correct 1M context windows, tool-calling
> support, and the `supports_vision: false` flag that the gateway reports.

### Troubleshooting

- **Models don't appear?** Run "GitHub Copilot LLM Gateway: Refresh Models" from the command palette.
- **128k context instead of 1M?** Ensure your LCP instance is running the latest version that
  serves `max_model_len` and `context_length` in `/v1/models`.
- **"model does not support vision" errors?** Make sure "Enable Image Input" is turned **off**
  in the extension settings — your DeepSeek models are text-only.
- **Chat errors with "received 0 chars / 0 text parts / 0 tool calls"?** This happens on long
  agentic tasks when a reasoning model (`deepseek-v4-*`) spends its entire output budget on
  thinking and produces no answer before hitting the extension's output-token cap. The gateway
  caps output at **Default Max Output Tokens** (default `4096`) even though LCP reports the full
  1M context. Raise `github.copilot.llm-gateway.defaultMaxOutputTokens` in VS Code settings
  (e.g. `8192`–`16384`) so the model has room to finish reasoning and emit a real response.
- **DeepSeek 400 "reasoning_content must be passed back"?** This happens when an agent / Copilot
  strips the `reasoning_content` field from thinking-mode assistant turns in multi-turn history.
  LCP automatically recovers the real reasoning content it saw in earlier responses and
  re-attaches it. See [Thinking-Mode Reasoning Recovery](features/thinking-mode-recovery.md)
  for the architecture.
- **Command Code "This operation was aborted"?** Command Code models (especially `deepseek-v4-pro`
  via `commandcode`) can take over 60s on complex agentic tasks, exceeding the extension's default
  HTTP timeout. Set **Request Timeout** to `120000` (2 minutes) and raise **Default Max Output
  Tokens** to `32000` so the model has room for both reasoning and a full response:
  - `github.copilot.llm-gateway.requestTimeout`: `120000`
  - `github.copilot.llm-gateway.defaultMaxOutputTokens`: `32768`

## Configuration

Configuration is **DB-backed** (stored in the SQLite `settings` table as JSON
blobs under `gateway_config:<section>`). There is no `gateway.yaml` and no
hot-reload: on first boot the gateway seeds the DB from a built-in Python
default (`src/api/config.py` → `SEED_CONFIG`), and all edits via the UI
(Providers, Profiles, Routing, Cache) are written straight to the DB and
persist across restarts.

The only env vars needed to bootstrap are the DB path and listen port
(`COST_DB`, `LISTEN_PORT`) — everything else is configurable at runtime.

The shape of each section (for reference, matching `SEED_CONFIG` and the
tracked `config/gateway.example.yaml`):

```yaml
server:
  port: 8734
  default_profile: l2
profiles:
  l2:
    forbidden_tools: [write_file, patch, cronjob]
    chain:
      - provider: opencode
        model: deepseek-v4-pro
      - provider: deepseek                       # fallback
        model: deepseek-v4-pro
  cron:
    forbidden_tools: null                        # null = strip ALL tools
    chain:
      - provider: deepseek
        model: deepseek-v4-flash
providers:
  deepseek:
    # API key is entered via the dashboard (Providers → Configuration tab),
    # encrypted with LCP_SECRET_KEY and stored in SQLite — no env vars here.
    cache:
      strategy: prefix
      savings: cost
      hit_field: prompt_cache_hit_tokens
pricing:
  - provider: deepseek
    model: deepseek-v4-pro
    cache_hit: 0.003625
    cache_miss: 0.435
    output: 0.87
```

## Benchmarking (LiveBench)

LCP's dynamic router is driven by **benchmark grades, not vibes**. Each model's
capability scores (`model_capabilities`) are produced exclusively by running
[LiveBench](https://livebench.ai/) against the **raw provider model** — never
through LCP's own routing, which would contaminate the very scores the router
relies on.

Benchmarking is an **opt-in plugin**: the base image stays lean, and the runner
simply reports "not installed" until you enable it.

### How it works

1. You queue a run for a provider model (e.g. `deepseek` / `deepseek-v4-pro`) from
   the Models page or the API. LCP resolves the provider's `api_base` and
   credential-store API key, then runs LiveBench as a subprocess **directly
   against the provider**.
2. LiveBench generates answers and ground-truth judgments per category, and LCP
   parses `all_groups.csv` into per-category 0–100 scores.
3. Scores are upserted into `model_capabilities` with `source="lcp_benchmark"`,
   keyed to the model's registry `benchmark_key`. The router's task classifier
   then uses those graded scores (plus a cost bias) to pick the best model for
   each request.

Only the six non-Docker categories are run; `agentic_coding` is deliberately
excluded because it requires Docker.

| LiveBench category | LCP task type |
|---|---|
| `reasoning` | `reasoning_chain` |
| `coding` | `code_generation` |
| `math` | `reasoning_chain` |
| `data_analysis` | `research_deep` |
| `language` | `casual_chat` |
| `instruction_following` | `planning` |

### Installation

**Option A — bake it into the image (recommended for Docker):**

```bash
docker compose build --build-arg WITH_BENCH=1 lcp
```

This clones LiveBench into `${LCP_MODULES_DIR}/livebench` (default
`/opt/lcp-modules/livebench`) and installs the core package plus the
`code_runner/requirements_eval.txt` extras. Those extras (TensorFlow, scipy,
etc., ~GBs) are only needed to **grade the `coding` category**, which executes
generated code. Core-only covers the other five categories.

**Option B — point LCP at a local checkout:**

```bash
export LCP_MODULES_DIR=/opt/lcp-modules
git clone --depth 1 https://github.com/LiveBench/LiveBench.git "$LCP_MODULES_DIR/livebench"
cd "$LCP_MODULES_DIR/livebench" && pip install -e .
# optional, only for the `coding` category:
pip install -r code_runner/requirements_eval.txt
```

LCP also falls back to `run_livebench.py` on `PATH`.

### Seeding scores without running benchmarks

Running full 150-question LiveBench on every model is expensive. LCP therefore
supports **three tiers** of model data:

1. **Bulk seed (free, baseline).** A snapshot of the public LiveBench
   leaderboard (`2026-06-25`) ships in
   `src/api/data/table_2026_06_25.csv` (the raw LiveBench task table). The
   import pipeline (`src/api/benchmark_import.py`) reads it into the
   `capability_metrics` table and materializes the typed `model_capabilities`
   + `model_capability_subtasks` rows the router and Models page query.
   Top-level category scores are derived from the per-subtask rows (same
   aggregation livebench.ai uses), so models that only have subtask-level
   data get their top-level scores derived automatically. Seed it in
   milliseconds for a zero-cost baseline per model:

   ```bash
   # in the container: seed registry + LiveBench snapshot
   python -m src.api.seed_capabilities --db /app/data/costs.db
   # registry only / LiveBench only
   python -m src.api.seed_capabilities --db /app/data/costs.db --registry-only
   python -m src.api.seed_capabilities --db /app/data/costs.db --livebench-only --release 2026-06-25
   # import a LiveBench CSV dataset directly
   python -m src.api.benchmark_import --db /app/data/costs.db --file path/to/table_2026_06_25.csv
   ```

   **Modular datasets.** The importer reads LiveBench CSV task tables from two
   places: bundled files under `src/api/data/*.csv`, and any installable
   module under `LCP_MODULES_DIR` (default `/opt/lcp-modules`) that ships a
   `data/*.csv` file. A module dataset overrides the bundled one, so
   benchmark plugins can drop in their own leaderboard data without forking
   LCP.

   **Upload a dataset from the UI.** On the Models page, the **Import**
   button opens a file picker — choose a LiveBench CSV (`table_*.csv`). LCP
   uploads it via `multipart/form-data` to
   `POST /api/models/capability/import`, writes `capability_metrics`, and
   materializes the typed rows.

2. **Incremental benchmark (accurate, opt-in).** Run LiveBench for a single
   model + release when a new model appears or a new release ships (e.g.
   `deepseek-v4-pro` 2026-08-13). This is the "Run benchmark" button — it
   targets one provider/model, never all models at once.

3. **Manual override (your own numbers).** The "+ Add score" button on the
   Models page stores `source="manual"` scores that always outrank the other
   tiers.

### Model identity, version & provider mapping

A model is identified by its **logical name** (e.g. `deepseek-v4-pro`), which
is what routing and pricing use. It has:

- **benchmark_key** — the stable, release-independent key used in
  `model_capabilities`;
- **provider_mappings** — the exact provider-side model ID per provider, so
  `deepseek-v4-pro` served by `deepseek`, `opencode`, and `commandcode`
  (`deepseek/deepseek-v4-pro`) resolves to ONE identity and ONE scoring. The
  provider keys are also the model's "providers" list in the UI;
- **active_release** — the CURRENT model version (e.g. `2026-08-13` for
  DeepSeek V4 Pro 0813) whose scores feed the router;
- **benchmark_release** — the LiveBench leaderboard snapshot date the scores
  came from (e.g. `2026-06-25`).

The router resolves `provider-side model ID → logical → benchmark_key`, applies
the active version's scores, and routes the single resulting identity.

### Module install path

All runtime-installed modules live under the **module root** controlled by
`LCP_MODULES_DIR` (default `/opt/lcp-modules`). The in-UI runtime installer
(Setup → LiveBench) clones to `$LCP_MODULES_DIR/livebench`. Set this to a
Docker volume mount so installs survive container recreation:

```bash
export LCP_MODULES_DIR=/app/data/modules
```

### Runtime status

The runner degrades gracefully. `GET /api/models/benchmark/status` reports:

- `available` — whether a LiveBench checkout is reachable
- `coding_supported` — whether the `coding` category can be graded
  (probes for the heavyweight `code_runner` deps)
- `reason` — a human-readable explanation when unavailable

The Models page uses this status to show a clear "not installed" notice instead
of a Run button, and to flag when `coding` is unsupported — while still listing
past runs.

## Semantic Dynamic Routing

Every request is classified into a **task type by meaning, not keywords**. The
embedding-based classifier (`BAAI/bge-small-en-v1.5`, 384-dim) embeds the
user's intent and matches it against per-task exemplar centroids, so
"why does this throw a KeyError?" routes as `debugging` while "write a pytest
for this" routes as `unit_tests` — regardless of the exact words used.

The classifier is an **installable module** (Setup → Semantic routing), like
memory and LiveBench — the default image is lean and installs it at runtime;
`WITH_ROUTER=1` bakes it in instead. When unavailable, routing degrades
gracefully to heuristic classification.

**It depends on benchmark capabilities.** Semantic routing classifies *what*
the task is; the router then routes by the model's per-task capability grades.
The Setup page therefore blocks installing Semantic routing until **graded
capability data exists** — produced either by a LiveBench run or by the
one-click **"Seed baseline scores"** action (imports the bundled LiveBench
leaderboard snapshot, no benchmarks run).

**The router is context-aware.** A model whose context window can't hold the
request is excluded and routing falls to the next-best model that fits — so a
205k-token request never 400s against a 200k-context model. `/v1/models`
advertises the profile's *maximum* routable context (honest, because the
router guarantees a fitting model for any request up to that size), and a
request larger than every chain model's context returns a clean `413`.

See [Semantic Dynamic Routing](docs/semantic-routing.md) for the full
classifier, config, and module-lifecycle details.

## Component Runtime

LCP wires itself through a **declarative component runtime** instead of a
hand-sequenced bootstrap: 13 components (circuit breaker, key manager, cost
cache, dynamic router, memory, cost plugins, …) each declare what they need
(`requires`) and publish (`provides`), and return their own cleanup. The
runtime topologically sorts them, starts them, and tears them down in reverse
(LIFO) — so startup order bugs and teardown leaks are structurally impossible,
and a failed optional module degrades instead of crashing boot.

See [Component Runtime](docs/component-runtime.md) for the contract, the full
component graph, and the request-path resolution model.

## Why LCP over alternatives?

| | LCP | LiteLLM | OpenRouter | one-api |
|---|---|---|---|---|
| **Benchmark-driven routing** | ✅ per-request, by task type | ⚠️ model weights only | ⚠️ provider-only | ❌ |
| **Circuit breaker + health** | ✅ per (provider, profile) | ⚠️ basic retries | ⚠️ partial | ❌ |
| **Agent tool permission enforcement** | ✅ per profile | ❌ | ❌ | ❌ |
| **Per-agent budgets & virtual keys** | ✅ | ✅ keys only | ❌ | ✅ |
| **Deployment** | One container, SQLite | Container + PG + Redis | SaaS | Container + DB |
| **Credentials** | Encrypted at rest (UI-managed) | Env vars | SaaS-managed | Env vars |
| **Dashboard / UI** | Server-rendered, no build | React SPA | SaaS | Web UI |
| **License** | AGPL-3.0, everything included | MIT core + enterprise | SaaS | MIT |

What makes LCP different isn't just proxying — it's **decisions**:

- **Every request is classified and routed to the best (provider, model) for that task**, using
  LiveBench/benchmark capability scores plus cost bias, circuit-breaker health, and your own
  rules — all editable live in the UI.
- **Agent-native control**: per-profile tool permissions, budgets, and keys, so you can run
  many agents (Hermes, Claude Code, Copilot, cron) behind one gateway and know exactly who
  spent what, with what tools.
- **Zero external services**: one container, SQLite, DB-backed config. No Postgres, no
  Redis, no cloud.

## Architecture

```
LCP (:8734) — single process, single port
|
+-- Python stdlib (http.server) — no framework overhead
+-- SQLite (costs.db) — zero-infrastructure persistence
+-- DB-backed config — editable from the UI, no hot-reload files
+-- Server-rendered dashboard — Chart.js, no SPA
+-- Plugin architecture — provider costs, memory backends
+-- Component runtime — declarative startup + LIFO teardown
+-- Docker — python:3.11-slim
```

LCP deliberately avoids PostgreSQL, Redis, Next.js, pnpm, Kubernetes, and the broader
TypeScript ecosystem. SQLite handles millions of cost rows at homelab scale. One container,
one port, one `docker compose up`.

## Dependencies

Every runtime dependency and what it does in LCP:

| Package | Role in LCP |
|---|---|
| **`structlog`** | Structured JSON logging to stdout. Every request, error, budget breach, and startup step is a machine-readable log line — `docker logs lcp` is grep-friendly. |
| **`sqlalchemy`** | SQLite ORM for the `requests`, `budgets`, `alerts`, and `api_keys` tables. All cost history, spend limits, and alert state lives here. No external database. |
| **`alembic`** | Database schema migrations. Every schema change (alerts table, API keys, error_detail column) gets a numbered migration in `alembic/versions/`. Run automatically on container startup via `alembic upgrade head`. |
| **`pyyaml`** | Reads the seed config and `config/gateway.example.yaml`. The live config is DB-backed (SQLite `settings` table), editable from the UI — no file hot-reload needed. |
| **`tiktoken`** | Exact BPE token counts using the `cl100k_base` encoding (same tokenizer used by DeepSeek and OpenAI models). Powers the pre-request `X-Estimated-Cost` header and the dynamic flash/pro router. The ~1 MB vocabulary file is pre-downloaded at Docker build time and persisted to a volume — zero CDN dependency at runtime. |
| **`jinja2`** | Server-rendered HTML templates for the dashboard, profiles page, providers, API keys, alerts, and logs. No SPA, no build step, no npm — pages load instantly from the server. Shared partials for sidebar, modals, and JS utilities. |

Optional modules (installed at runtime from the Setup page, **not baked into the
lean image by default**):

| Package | Role in LCP |
|---|---|
| **`sentence-transformers` + `torch`** | The embedding model powering semantic task classification (router module) and semantic memory recall (memory module). Installed per-module into `<LCP_MODULES_DIR>/<module>`; bake in with `WITH_ROUTER=1` / `WITH_MEMORY=1`. |
| **`lancedb`** | Embedded vector store (memory module only) — columnar storage + ANN indexing, no separate service. |
| **LiveBench** (`git clone` + pip) | The benchmark runner (benchmark module) — a checkout under `<LCP_MODULES_DIR>/livebench`; bake in with `WITH_BENCH=1` (optionally with the `coding`-grading extras). See [Benchmarking (LiveBench)](#benchmarking-livebench). |

The three modules map to one **build flag** each. The flags are Docker build
args (image build time, not runtime env vars) that decide whether a module's
deps ship in the image — all default to `0` (lean, runtime-install):

| Flag | Bakes into the image | Powers |
|---|---|---|
| `WITH_ROUTER=1` | `sentence-transformers`/`tokenizers`/`torch` + pre-downloaded `bge-small` model | Semantic task classification |
| `WITH_MEMORY=1` | `lancedb` (+ shared `sentence-transformers`/`torch`) | Memory plugin (LanceDB vector bank) |
| `WITH_BENCH=1` | A LiveBench checkout + its eval deps | Benchmark-driven routing grades |

Baking makes a module available instantly (larger image); with the flag unset,
the same module installs on demand from the Setup page.

Dev-only dependencies (`pip install .[dev]`):

| Package | Role |
|---|---|
| `pytest` | Test runner — 2259 tests covering routing (incl. benchmark-driven capability routing, semantic task classification, the runtime enable toggle, per-profile routing overrides, `unit_tests` taxonomy), budgets, alerts, cost estimation, auth enforcement, circuit breaker, encrypted credentials, provider plugins (DeepSeek, OpenCode, Command Code, Local LLM), the benchmark/import pipeline, the memory plugin, the component runtime, and the plugin system |
| `pytest-cov` | Coverage reports — `pytest --cov=src --cov-report=term-missing` |
| `pytest-mock` | Mocking utilities for the `unittest.mock` patch system |

## Test Coverage

**99% overall** — 9,489 of 9,505 statements covered (2259 tests, 15 deselected integration tests).

Run: `.venv/bin/python -m pytest --cov=src --cov-report=term-missing -q`

| Module | Coverage |
|---|---|
| `src/__init__.py` | 100% |
| `src/api/__init__.py` | 100% |
| `src/api/alert_manager.py` | 100% |
| `src/api/benchmark.py` | 99% |
| `src/api/benchmark_import.py` | 100% |
| `src/api/circuit_breaker.py` | 100% |
| `src/api/component.py` | 100% |
| `src/api/config.py` | 100% |
| `src/api/cost_cache.py` | 100% |
| `src/api/cost_estimator.py` | 100% |
| `src/api/cost_plugins/__init__.py` | 100% |
| `src/api/cost_plugins/base.py` | 100% |
| `src/api/cost_plugins/commandcode.py` | 99% |
| `src/api/cost_plugins/commandcode_api.py` | 100% |
| `src/api/cost_plugins/deepseek.py` | 97% |
| `src/api/cost_plugins/llamacpp.py` | 100% |
| `src/api/cost_plugins/opencode.py` | 97% |
| `src/api/cost_plugins/opencode_api.py` | 100% |
| `src/api/credential_store.py` | 100% |
| `src/api/crypto.py` | 100% |
| `src/api/exceptions.py` | 100% |
| `src/api/key_manager.py` | 100% |
| `src/api/livebench_tasks.py` | 100% |
| `src/api/logging_config.py` | 100% |
| `src/api/memory/__init__.py` | 100% |
| `src/api/memory/base.py` | 100% |
| `src/api/memory/embeddings.py` | 100% |
| `src/api/memory/harness.py` | 100% |
| `src/api/memory/lancedb_backend.py` | 100% |
| `src/api/models.py` | 100% |
| `src/api/prompt_cache.py` | 100% |
| `src/api/reasoning_store.py` | 100% |
| `src/api/request_pipeline.py` | 100% |
| `src/api/router.py` | 99% |
| `src/api/runtime.py` | 100% |
| `src/api/seed_capabilities.py` | 100% |
| `src/api/setup.py` | 100% |
| `src/api/task_classifier.py` | 100% |
| `src/api/token_verifier.py` | 100% |
| `src/main.py` | 100% |
| `src/server/__init__.py` | 100% |
| `src/server/endpoints.py` | 100% |
| `src/server/handler.py` | 100% |
| `src/server/server.py` | 100% |
| `src/server/sse_helpers.py` | 100% |
| `src/ui/__init__.py` | 100% |
| `src/ui/dashboard.py` | 100% |
| `src/ui/pages.py` | 100% |
| `src/ui/render.py` | 100% |

## API

| Endpoint | Description |
|---|---|
| `POST /{profile}/v1/chat/completions` | OpenAI-compatible chat completions |
| `GET /health` | Provider health and circuit breaker status |
| `GET /v1/models` | Available models across all providers |
| `GET /` | Dashboard |
| `GET /api/daily-costs` | JSON cost data |
| `POST /api/keys` | Create API key |
| `GET /api/keys` | List API keys |
| `GET /api/models/benchmark/status` | Whether the benchmark runner is installed |
| `GET /api/models/benchmark` | List benchmark runs (paginated: `?limit=&offset=&model=`) |
| `POST /api/models/benchmark` | Queue a LiveBench run (direct-to-provider) |
| `GET /api/models/benchmark/{id}` | Benchmark run detail |

See [PLAN.md](PLAN.md) for the full API reference.

## Roadmap

### Shipped
- OpenAI-compatible chat completions API (`/{profile}/v1/chat/completions`)
- Profile-based routing with provider chains and automatic fallback
- Circuit breaker — degraded (probed) and dead (skipped) provider states
- Per-request cost tracking with DeepSeek cache hit/miss breakdown
- Prompt prefix caching — deterministic message ordering for maximum cache-hit rate
- Server-rendered dashboard — Chart.js, budget cards, provider health, alert badge
- Budget system — per-profile and per-key budgets, unified spend tracking
- Budget enforcement — pre-LLM block (HTTP 429), post-request spend increment + threshold alerts
- Alerting — DB-persisted alerts, webhook dispatch, acknowledge/resolve, alert history page
- API key management — create, rotate, revoke; per-key spend limits; profile access scoping
- SSE streaming passthrough — real-time token delivery, no buffering
- Provider plugins — DeepSeek (balance API), OpenCode (web API), Command Code (subscription usage API), llama.cpp (local /models)
- Provider model discovery — auto-detect models from `/v1/models` with metadata
- tiktoken integration — exact BPE token counts, pre-downloaded at build time
- Startup observability — per-step timing logs in `docker logs`
- Benchmark runner — opt-in LiveBench integration; queue runs against provider models and parse per-category scores into `model_capabilities`
- Capability router — task classifier routes by benchmark-graded scores with a cost bias
- Dynamic routing controls — runtime enable toggle (Providers → Routing), policies (`eager` / `cost_first` / `explore` + min-score floor), UI rules (`prefer` / `block` / `policy`), and a `unit_tests` task taxonomy derived from `code_generation`

### Scope boundary
- **Credit limits** — already covered by per-key budgets (spend caps with hard-stop)
- **Alerting** — already covered by the alert system (threshold breaches, webhooks)
- **Multi-tenant teams/users** — out of scope. LCP stays a single-instance, single-org
  gateway; we will not build org/team membership, per-user billing, or role management.

### Planned
- Permission matrix — declarative `allow` / `block` / `blocked_globally` rules,
  replaces tool stripping ([spec](features/permission-plugin.md))
- Logging enhancements — structured request telemetry, token-usage logging
  ([spec](features/logging-enhancements.md))
- Provider health dashboard — richer uptime/health surfacing
  ([spec](features/provider-health.md))
- Memory module hardening — index management, hybrid FTS, time-decay, consolidation, tag auto-suggest, and a unified `/v1/memories` endpoint
  ([spec](features/memory.md))
- Dashboard enhancements — backlog from the original dashboard plan
  ([spec](features/enhancements.md))
- Rate limiting — *only if needed*: budgets already cap spend, but they do not
  cap request frequency. A per-key/per-profile requests-per-minute limiter is a
  separate feature, only worth building if throughput (not cost) becomes a concern.

Full details in [PLAN.md](PLAN.md).

## Status

LCP runs continuously in production, routing all LLM traffic for a multi-agent homelab
(6 Hermes profiles, 15+ custom skills, daily cron jobs) and serving as the LLM backend
for daily VS Code Copilot usage. It handles approximately 200 requests per day across 5
profiles with real cost tracking, budget enforcement, and alerting.

## AI Attribution

This project was built with significant assistance from AI coding agents (Hermes Agent with
DeepSeek V4). Architecture decisions, code, tests, and documentation were produced in
collaboration between a human operator and AI. The bugs are mine; the architecture is ours.

## License

[GNU Affero General Public License v3.0](LICENSE) — you can use, modify, and
distribute LCP freely. If you modify it and offer it as a network service
(e.g. a hosted LLM gateway SaaS), you must make your modified source code
available to users under the same license. This protects the project from
being wrapped into proprietary services without contributing improvements
back to the community.
