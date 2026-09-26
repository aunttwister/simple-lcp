# simple-lcp

**A self-hosted control plane for LLM harnesses.** One container, one port, SQLite — no cloud
dependency, no Postgres, no Redis.

[![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![CI](https://github.com/aunttwister/simple-lcp/actions/workflows/ci.yml/badge.svg)](https://github.com/aunttwister/simple-lcp/actions/workflows/ci.yml)

---

## Contents

- [What it is](#what-it-is)
- [The profile is the unit of configuration](#the-profile-is-the-unit-of-configuration)
- [Pages](#pages)
- [How a request is handled](#how-a-request-is-handled)
- [Routing](#routing)
- [Model capabilities](#model-capabilities)
- [Optional modules](#optional-modules)
- [Clients](#clients)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Tests](#tests)
- [Status](#status)
- [License](#license)

---

## What it is

simple-lcp sits between your clients and your LLM providers. A client calls a **profile-scoped**
endpoint — `POST /<profile>/v1/chat/completions`, `GET /<profile>/v1/models` — and the control plane
decides which provider and model serve the request, applies that profile's tool permissions and
spending rules, and records what happened.

Everything in the app is either **that**, or a view over what happened.

## The profile is the unit of configuration

| field | meaning |
|---|---|
| `pool` | the ordered `(provider, model)` steps this profile may use — the order is the static fallback order |
| `routing` | `static` (follow the pool order) or `dynamic` (classify the request, then rank the pool) |
| `intents` | the task labels this profile can be given; the dynamic router scores only these |
| `breaker` | circuit-breaker thresholds applied to this profile's own pool |
| `permissions` | per-profile tool policy, applied on the request path |
| `budget` / `alerts` | scoped to provider, profile and API key — on the Alerts page, not on the profile |
| `modules` | optional components enabled for this profile |

## Pages

| page | what it shows |
|---|---|
| **Profiles** | the profiles, the API keys scoped to them, their cron jobs, their work-layer paths |
| **Models** | the capability matrix, the model registry, and where models come from |
| **Activity** | the request / cost / token overview and every log realm |
| **Usage** | spend per provider, live balances, the scrape cache |
| **Alerts** | alerts and budgets by subject (provider · profile · API key) |
| **Setup** | install wizard and module configuration |
| **Tasks** / **Fleet** | the work module: task tree and machines |

## How a request is handled

```
auth -> strip tools -> cache check -> provider chain -> calculate cost -> record
```

## Routing

Two modes, chosen per profile.

- **static** — walk the profile's pool in order, falling back to the next step when a provider
  fails.
- **dynamic** — classify the request against the profile's declared intents, score every step of
  the pool by declared capability, cost bias and circuit-breaker health, then apply the profile's
  rules.

A **margin gate** keeps the outcome deterministic: when the top two intents are too close to
separate, the router does not reorder and the static order stands. Circuit-breaker state is applied
throughout — degraded providers are probed, dead ones are skipped.

## Model capabilities

Capability scores are **declared data**: `src/api/data/declared_capabilities.json`, seeded into the
`model_capabilities` table, which is the matrix the router reads. There is no benchmark runner
inside the app.

## Optional modules

The default image is lean. Modules install from the **Setup** page into `LCP_MODULES_DIR`
(default `/opt/lcp-modules`), each in its own directory, where they survive container recreation.

| module | what it adds |
|---|---|
| `router` | in-process semantic task classifier (sentence-transformers) |
| `memory` | per-profile semantic memory backend (LanceDB) |
| `runboard` | work-layer task and decision views |

Build args `WITH_ROUTER=1` / `WITH_MEMORY=1` bake a module into the image instead of installing it
at runtime.

## Clients

- **Agent harnesses** — point the harness's OpenAI-compatible base URL at a profile path; LCP
  controls which tools the agent can use, what it spends and which provider it hits.
- **VS Code / GitHub Copilot Chat** — the
  [GitHub Copilot LLM Gateway](https://marketplace.visualstudio.com/items?itemName=arbs-io.github-copilot-llm-gateway)
  extension fetches `/v1/models` from LCP, so every profile appears in the model picker with its
  context window and capabilities.
- **Scripts and `curl`** — the API is OpenAI-compatible.

## Quick start

```bash
git clone https://github.com/aunttwister/simple-lcp.git
cd simple-lcp

cp .env.example .env
# edit .env: set LCP_SECRET_KEY (encrypts the provider keys stored in the DB)
# provider API keys are added in the dashboard: Providers -> Configuration

docker compose up -d            # http://localhost:8734
```

Send a request — the bare `/v1` paths use the default profile, `/<profile>/...` targets a named one:

```bash
curl http://localhost:8734/l2/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "deepseek-v4-pro",
       "messages": [{"role": "user", "content": "Hello!"}],
       "max_tokens": 50}'
```

## Configuration

Settings are **DB-backed** (`settings` table, `gateway_config:<section>` JSON blobs). There is no
`gateway.yaml` and no hot reload: the DB is seeded on first boot from `SEED_CONFIG`
(`src/api/config.py`), every edit made in the UI is written straight to the DB and persists across
restarts, and `config/gateway.example.yaml` documents the section shape.

Environment variables needed to bootstrap:

| var | default | purpose |
|---|---|---|
| `LCP_SECRET_KEY` | — | encrypts stored provider credentials (required; if unset a key is generated at `data/.lcp_secret_key`) |
| `COST_DB` | `/app/data/costs.db` | SQLite database |
| `LISTEN_PORT` | `8734` | listen port |
| `LCP_MODELS_PATHS` | `/v1/models,/models` | model-listing paths served per profile |
| `LCP_MODULES_DIR` | `/opt/lcp-modules` | where optional modules install |

## Tests

```bash
python3 -m pytest                 # unit suite (the integration marker is excluded by default)
python3 -m pytest -m integration  # tests that require a running server
```

CI runs the suite on Python 3.11 and 3.12 — see `.github/workflows/ci.yml`.

## Status

`main` is the LCP v0.5.0 tree carried over in full, plus the first simplification milestones: the
profile-first UI, per-profile intents with the routing margin gate, the strip of the duplicated
transcript blobs, and the removal of the in-app benchmark runner. Commit history is the record of
what changed and why.

## License

AGPL-3.0 — see [LICENSE](LICENSE). simple-lcp is a derived work of
[LCP](https://github.com/aunttwister/lcp) by the same author.
