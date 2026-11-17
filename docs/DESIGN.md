# LLM Control Plane (LCP) — Design Doc

## Metadata

- **Author:** aunttwister (maintained with homelab-expert L2)
- **Status:** Living design document — reflects the shipped system (v0.5.0, first public `main` release, 2026-08-22) plus approved designs. Items tagged `[live]` are implemented; `[in-progress]` and `[designed]` map to the feature specs in `features/` and to `PLAN.md`.
- **Created:** 2026-09-11
- **URL:** `docs/DESIGN.md` in github.com/aunttwister/lcp

> **Style note:** this document follows the *Little Moments* design-doc structure —
> "Write an Effective Design Doc" (refactoringenglish.com, Michael Lynch):
> https://refactoringenglish.com/excerpts/write-an-effective-design-doc/little-moments-design-doc/
> The companion skill that produced it is `effective-design-docs`.

---

## Objective

Create a self-hosted LLM management gateway that **routes, meters, and controls** every AI
agent in a homelab or small team — one container, one port (`:8734`), no cloud dependency:
control costs, enforce tool permissions, track utilization, and route each request to the
best-fit `(provider, model)` for the task.

---

## Background

I run a production multi-agent homelab: 6 Hermes agent profiles with 15+ custom skills, plus
daily VS Code Copilot usage through the same LLM backend. As the set of agents grew, three
problems appeared:

1. **Spend was a black box** — every agent hit upstream providers directly; nobody knew what
   was being spent, by whom, or on what task type.
2. **Tool policy was unenforceable** — a coding agent could call `cronjob` or `write_file`
   against production, and nothing in the loop could stop it.
3. **Failures were manual** — a dead provider meant hand-editing per-profile configs and
   hoping nobody noticed the outage.

Existing open-source gateways (LiteLLM, llmgateway) are heavy — they presume PostgreSQL +
Redis + a TypeScript ecosystem — and **none of them understand agent tool permissions**. The
name "control plane" is deliberate: like a networking control plane, LCP routes traffic,
enforces policy, and manages state for the data plane (the upstream LLM providers).

LCP started as `llm-gateway-router` (Phases 1–3: proxy + tool stripping + fallback + cost
dashboard), absorbed `llm-cost-tracking`, and was productized through v0.4.0 (modular split)
to v0.5.0 (benchmark-driven dynamic routing, first public release). It runs as a single
Docker container on the bridge LXC, backed by SQLite.

---

## Goals

- **Control costs `[live]`** — per-key spend limits (403 on breach), per-request cost
  estimation via a provider cost-plugin registry, prompt-cache hit/miss breakdown, and
  LiveBench run cost tracking.
- **Track utilization `[live]`** — time-series daily costs per profile/model/provider,
  recent requests and errors, cache stats, Prometheus `/metrics`.
- **Route intelligently `[live]`** — per-profile fallback chains with a persisted circuit
  breaker; benchmark-driven semantic routing that balances capability score, cost bias,
  circuit-breaker health, and UI-defined rules; context-aware model selection.
- **Enforce permissions `[designed]`** — fails-closed tool allow-lists per profile with a
  cross-cutting `blocked_globally` layer; any new Hermes tool is blocked until whitelisted.
- **Keep hosting ~$0 and low-maintenance `[live]`** — SQLite + one container; no Postgres,
  no Redis, no external services; runs on hardware the operator already owns.
- **Be a drop-in OpenAI-compatible endpoint `[live]`** — agents and VS Code Copilot consume
  `/v1/...` and `/{profile}/chat/completions` without client changes.
- **Stay private `[live]`** — LAN/VPN-only exposure, no telemetry, no ads, no third-party
  tracking.

## Non-goals

- **Commercial multi-tenant SaaS** — LCP is explicitly *not* designed to isolate mutually
  untrusted tenants (see `SECURITY.md`). Hosting other orgs' private traffic is a
  catastrophic-risk problem.
- **Native mobile apps** — a responsive web dashboard suffices; app-store maintenance is
  not worth it.
- **PostgreSQL, Redis, or the TypeScript ecosystem** — SQLite handles our scale; one binary,
  one port. This is a hard constraint, not a preference.
- **GPU inference** — LCP is a proxy/control plane, not an inference engine. Local models
  (ZGX vLLM, llama.cpp) are just providers behind it.
- **Public-internet exposure / TLS inside LCP** — raw HTTP is not a supported security
  boundary; LCP sits behind a reverse proxy (Caddy/Traefik) that terminates HTTPS.
- **OAuth / IdP / JWT complexity** — sha256-hashed API keys are the auth primitive. No
  interactive "ask" states — the gateway is headless.
- **Hot-reload YAML config** — config is DB-backed (decided 2026-08-31); there is no
  `gateway.yaml` and no mtime polling anymore.

## User roles

| Privilege | Admin (operator) | Agent profile (l2, l1, career, cron, coder, …) | VS Code Copilot |
|---|---|---|---|
| Make chat requests through the gateway | ✅ | ✅ | ✅ |
| View dashboard (all profiles) | ✅ | ❌ (not exposed) | ❌ |
| CRUD providers / test connections | ✅ | ❌ | ❌ |
| Generate / revoke API keys | ✅ | ❌ | ❌ |
| Edit routing rules / policies | ✅ | ❌ | ❌ |
| Acknowledge alerts | ✅ | ❌ | ❌ |
| Run / inspect benchmarks | ✅ | ❌ | ❌ |

One human operator; every other actor is an agent or an editor client. Profiles are
**tenants**, not users: each profile resolves to its own chain, its own tool policy, and its
own cost row. There is no self-serve signup. *(Dashboard auth + admin/viewer roles is a
designed gap — see Open issues.)*

## User interface

The dashboard at `:8734/` is server-rendered HTML (Python f-strings/Jinja, shadcn dark
theme, Chart.js CDN) — **no build step, no SPA**:

- **Sidebar tree** — Profiles → Providers → Models with per-provider health dots; profile
  folders auto-expand, provider folders collapse; "settings" inline text link per profile.
- **Summary cards** — Total Cost, Total Requests, Cache Hit Ratio, Output Tokens, per-profile
  cards.
- **Time-series charts** — Aggregate / Per-Profile / Per-Model view toggles (Chart.js).
- **Daily Costs / Recent Requests / Recent Errors** tables with status badges
  (`ok`=green, `error`=red, `FB`=amber for fallback) and `X-Estimated-Cost` in the response.
- **Modals over inline sections** — provider config with Test Connection + model discovery,
  profile config with per-profile API keys and a copyable gateway URL, Add Profile.
- **Providers page** (v0.5.0) — tabbed `#health` / `#config` / `#cache` / `#routing`, Setup
  wizard for installable modules (LiveBench, Semantic router, Memory), OpenCode credits
  widget in the global header.
- **Mobile** — overlay drawer sidebar, swipe-to-reveal actions, tab-hash deep links,
  **no hover-only controls** (touch-first).
- **No emoji in buttons or status text** — color carries semantics (operator preference).

## Scenarios

### Hermes agent request (L2 profile)

1. Agent POSTs `/{profile}/chat/completions` (URL-routed today; API-key auth `[designed]`).
2. LCP estimates cost from the request body (cost plugins), applies the permission gate
   (`tool stripping` today, `permission_matrix` fails-closed `[designed]`).
3. Semantic classifier (`[live]` when module installed) labels the task; the dynamic router
   scores chain steps — capability + cost bias × (1 − cost factor) + health + rules.
4. Circuit breaker drops dead/tripped providers; `try_chain` walks the merit-ordered chain.
5. LCP forwards to the winner (preserving `x-opencode-session` for OpenCode), records
   tokens/cost/latency/cache bits, and returns the response with `X-Estimated-Cost` (and
   `X-LCP-Cache: HIT/MISS`).

### Provider dies mid-day

1. Failures accumulate in the circuit breaker; auth failures weigh 3×.
2. Status flips `healthy → degraded → dead`; `/health` shows the tripped window.
3. Requests fall through to the next chain step; the providers page shows the degraded
   state, persisted across restarts (`provider_health` table) `[live]`.

### Admin adds a provider

1. Providers → Config: add form (name, base URL, key env var, models) with preset dropdown;
   Test Connection issues a real `/chat/completions` call.
2. On save, `discover_models` reads `/models` metadata and auto-learns each model's served
   context into `model_limits` (no more wrong 128k defaults) `[live]`.
3. Chain reorder per profile via drag-and-drop; `PUT /api/chains/<profile>` preserves
   `base_url` on matching (provider, model) pairs.

### Benchmark-driven routing

1. Admin runs LiveBench via `POST /api/models/benchmark` (parallel workers, watchdog cron).
2. Scores upsert into `model_capabilities` (`source="lcp_benchmark"`) — the same matrix the
   router consumes; bundled leaderboard snapshot can seed instantly.
3. Every route decision persists rationale (`routing_decisions`), auditable and replayable
   via `scripts/judge_routing.py` `[live]`.

### Budget breach `[designed]`

1. Key spend limit is enforced pre-flight (403) `[live]`; profile/team budgets add a 429
   hard-stop `[designed]`.
2. `AlertManager.fire(rule="budget_breach", ...)` → webhook POST with `X-LCP-Webhook-Secret`
   → 5-minute cooldown per dedup key `[live]`; alerts persist to SQLite + dashboard badge
   `[designed]`.

## Missing features

Deliberately omitted from v1/v0.5 scope — each with a reason (see also `features/todo.md`):

- **Real-time streaming viewer** — SSE passthrough exists; a live-tail UI is deferred
  (`features/logging-enhancements.md` Phase 5).
- **Full request/response body viewer** — bodies are not persisted in v1 by design
  (privacy + disk); opt-in storage is designed (`features/logging-enhancements.md`).
- **LLM Council / mixture-of-agents routing** — Tier 2 of `features/intelligent-routing.md`;
  the capability router is Tier 1 and ships first.
- **Declarative/Terraform-style config** — DB-backed settings are the source of truth; a
  config-as-code import is not planned.
- **Per-item chat history or conversation UI** — LCP is a control plane, not a chat app.

## Users

Assumptions about LCP's users (operator + clients):

- One technical operator who is comfortable with Docker, YAML, and a JSON API.
- Agents run **modern Python (3.11+) / OpenAI-compatible clients**; editors (VS Code)
  target ≥ 2025 browser engines on desktop.
- Clients are on the **LAN or WireGuard VPN** — never the public WAN (`SECURITY.md` scope).
- Dashboard browsed on desktop and phone (modern Chrome/Safari/Firefox); some users may use
  OS-level text-size/zoom settings.
- English-only; no localization.

## Notifications

- **`[live]` AlertManager** — 6 rule types: `budget_breach`, `provider_dead`,
  `provider_degraded`, `error_spike`, `circuit_breaker_trip`, `circuit_breaker_recovery`.
  Webhook dispatch in a background thread (POST with `X-LCP-Webhook-Secret`), 5-minute
  cooldown per dedup key, rolling-window error-spike tracking. Config via
  `GET/PUT /api/alerts/config`; `POST /api/alerts/{id}/acknowledge`;
  `POST /api/alerts/webhook/test`.
- **`[designed]` persistence + UI** — alerts currently live in memory (lost on restart);
  `features/alerting-budgeting.md` moves them to SQLite with a dashboard page, sidebar
  badge, and budget status cards at 50/80/90% thresholds.

## Service level objectives (design targets)

- **Uptime: 99.5% (≈44 h/yr)** — LCP is the critical path: outage = every agent blocked.
  Rebuilds must be atomic per operator directive: stop/rm/up in one command, never an
  intermediate outage. Restarts are fast (single container, SQLite warm).
- **Latency: gateway-added overhead p95 < 100 ms** for non-cached proxied requests
  (loopback, excluding provider response time). To be baselined per release; prompt-cache
  hits should be ~0-added-cost (served entirely from LCP).
- **Scale: 10 profiles · ~50 clients · 10⁴–10⁵ requests/day** — SQLite is comfortable past
  10⁶ rows; retention + CSV export (`/export?limit=N`) bound growth. Target is deliberately
  modest: the audience is one homelab, not the internet.

## Architecture

Every concern below is a *decision with a reason* — chosen for simplicity and durability,
and swappable where a standard interface exists (SMTP-style, S3-style, OpenAI-compatible).

| Concern | Choice | Why | Lock-in note |
|---|---|---|---|
| Backend language | Python 3.11+ (stdlib `http.server`, `ThreadingHTTPServer`) | Already working; fast enough for a proxy; zero deps | None |
| Frontend | Server-rendered HTML + vanilla JS/CSS + Chart.js CDN | No build step, no package manager, simple to test | Chart.js via CDN only |
| Database | SQLite + Alembic | Single-node scale, zero infra, millions of rows fine | Hard cap: "no Postgres/Redis" |
| Bootstrap | Component runtime — 13 components declare `requires`/`provides`, LIFO disposers | Order bugs impossible; teardown can't leak; failed module degrades, boot never breaks | See `docs/component-runtime.md` |
| Config | DB-backed `settings` rows (`gateway_config:<section>`); hydrate prefers DB over seed | Live CRUD edits; no hot-reload file watches | Decided 2026-08-31 — no `gateway.yaml` |
| Provider cost plugins | Registry (`cost_plugins/`): deepseek, opencode, opencode_api, llama.cpp/"Local LLM", commandcode, commandcode_api; auto-register + `set_engine` | One choke point for pricing; billing-page scrape for OpenCode/CommandCode credits | Plugins are swappable |
| Circuit breaker | 3 states (`healthy→degraded→dead`), half-open ladder, error weights (auth 3×), **persisted** `provider_health` | Failures survive restarts; no resurrected dead providers | See `features/provider-health.md` |
| Prompt cache | Hash-based TTL cache; normalization **preserves tool_calls** | Saves cost on identical prompts without breaking tool loops | Cache stats via `/cache/stats` |
| Router | Semantic classifier (`bge-small-en-v1.5`, 8 task types, `min_score=0.35`) + `CapabilityRouter` scoring (capability + cost bias, 5% hysteresis), policies `eager`/`cost_first`/`explore`, context gate (8k output reserve, 413 fallback), **chain-as-source-of-truth** merit order | Best-fit routing that degrades gracefully when the embedder is missing | See `docs/semantic-routing.md`, `features/merit-order.md`, `features/deterministic-routing.md` |
| Routing observability | `routing_decisions` rationale + `conversation_json`; replay/judgment scripts | Every route is auditable and improvable | See `features/routing-observability.md`, `features/routing-judgment.md` |
| Reasoning store | DeepSeek `reasoning_content` capture/re-attachment | Thinking-mode tool loops no longer 400 (clients strip it) | `features/thinking-mode-recovery.md` |
| Credentials | `credential_store` + `crypto` — upstream API keys encrypted at rest | Secrets never in plaintext or logs | `SECURITY.md` scope |
| API keys | `key_manager` — sha256 hashing, raw shown once, per-key spend limits | Simple, effective; no JWT | — |
| Token verifier | `token_verifier` — provider usage vs local estimate comparison | Detects estimation drift | — |
| Memory `[in-progress]` | LanceDB embedded plugin: `/{profile}/memory/{retain,recall,forget,count}` | One memory bank for all clients, zero new infra | See `features/memory.md` |
| Permission `[designed]` | `PermissionPlugin` ABC + registry; fails-closed; `blocked_globally`; `X-LCP-Capabilities` header | The only gateway that understands agent tool permissions — the differentiator | See `features/permission-plugin.md` |
| Alerts/budgets `[partially live]` | `AlertManager` + `Budget` model + key spend-limit 403 | Budgets before the request hits the LLM | See `features/alerting-budgeting.md` |
| Benchmarking | LiveBench worker via `POST /api/models/benchmark`, answer-tree on shared checkout, capability matrix upsert | Grades models → feeds the router | See README "Benchmarking (LiveBench)" |
| Hosting | Docker container `lcp` on bridge LXC, `:8734`, SQLite volume; image bakes code (rebuild required) | One box, one port; LAN-only | Volume: `llm-cost-tracker_cost_data` |
| Monitoring | `/health` (breaker state), `/metrics` (Prometheus), `/cache/stats`; external Uptime Kuma | Watchdog-friendly | — |
| Job scheduling | In-process background threads (cost-cache refresher, benchmark worker) + watchdog cron pattern | No external scheduler; minimal moving parts | — |
| CI | GitHub Actions (test suite, 2259+ tests, ~99% coverage) | Quality gate before release | — |

Request path (hot loop): `handler.py → request_pipeline.py → try_chain → forward_request`
with components resolved through `resolve_service(key, fallback)` from the active runtime —
no module globals. Streaming requests pass raw SSE bytes through with usage extracted from
the last chunk (`sse_helpers.py`).

## Privacy

- `[live]` **Metadata only** — requests persist tokens, cost, latency, error type, and
  cache stats; **request/response bodies are not stored**.
- `[designed]` **Opt-in body logging** — `features/logging-enhancements.md` adds body
  storage behind explicit config, not by default.
- Provider API keys are **encrypted at rest** (`crypto`); LCP API keys are **hashed**
  (raw value shown exactly once).
- No telemetry, no advertising, no third-party data collection; OpenCode credits are
  scraped from the billing page without exposing the cookie/log.
- LAN + WireGuard VPN only; nothing routes through public DNS except the reverse proxy
  in front.

## Data retention

- `costs.db` (SQLite volume) accumulates `requests`, `benchmark_runs`,
  `routing_decisions`, `provider_health`; `audit_logs` schema exists, runtime wiring is
  `[designed]` (Phase 7).
- CSV export (`/export?limit=N`) gives the operator an escape hatch at any time.
- `[designed]` retention/rollup policy and alert persistence (`features/alerting-budgeting.md`,
  `features/logging-enhancements.md`).
- Backups are Docker-volume snapshots; DB-level replication (Litestream-style) is a
  non-goal for a single-host homelab service.

## Security

### Attack surface

- **LAN `:8734`** — dashboard and management API are unauthenticated today on the LAN
  (`[designed]` admin/viewer roles; operator mitigates with LAN/VPN isolation now).
- **Chat endpoints** `/{profile}/chat/completions` — authenticated by API key
  `[in-progress]` / URL-path routing today.
- **Provider credentials at rest** — `credential_store` + `crypto`.
- **Benchmark worker / webhooks** — outbound network to providers, HF datasets, and webhook
  endpoints.

### Authentication

API keys (`lcp_XXXX`, sha256-hashed at rest, raw shown once at creation) are the primitive;
per-key `spend_limit` is enforced pre-flight. URL-path profile routing remains for
backward-compatible LAN clients. No dashboard auth today (see Open issues). No MFA — the
audience is one operator on a trusted network.

### Threats (scenario → mitigations)

| Threat | Mitigations |
|---|---|
| LAN attacker hits unauthenticated dashboard/API | LAN/VPN-only exposure; `[designed]` dashboard auth + read-only viewer roles; parameterized SQLAlchemy queries; server-rendered templates escape output |
| API key theft | Keys hashed at rest; raw shown once; per-key spend limits; rotate/revoke via UI; `X-LCP-Webhook-Secret` on outbound webhooks |
| Provider credential exfiltration | Encrypted credential store (`crypto`); secrets never written to logs or error responses (in-scope per `SECURITY.md`) |
| Budget bypass | Key spend-limit → 403 `[live]`; profile/team budgets → 429 `[designed]`, enforced pre-flight before forwarding |
| Abusive traffic / DoS through chat endpoint | Rate limiting `[designed]` (Phase 7); circuit breaker + degraded gating; minimal public surface |
| SSRF / injection via provider config or dashboard | Test-connection gate before save; URL validation; `SECURITY.md` explicitly scopes SSRF/injection; no anonymous state mutation |
| Tool-call/reasoning corruption in multi-turn loops | Cache normalization preserves `tool_calls`; sanitizer removes orphaned tool responses; reasoning store re-attaches stripped `reasoning_content` |
| OpenCode rejects missing `x-opencode-session` | LCP threads a stable per-conversation header through every outbound request (commit `65896a6`) |

Per `SECURITY.md`: in scope = auth bypass, credential decryption, budget bypass, SSRF /
injection / unauthenticated state mutation, secrets leaking into logs. Out of scope by
design = TLS termination (reverse proxy owns that), multi-tenant isolation.

## Licensing

- **`[live]` AGPL-3.0** (`LICENSE`). Considerations: prevents enterprise upsell
  (the llmgateway failure mode); keeps the codebase usable by self-hosters while requiring
  derivative network services to share source. Contributions are guided by
  `CONTRIBUTING.md`; security reports via GitHub private vulnerability reporting
  (`SECURITY.md`), acknowledgment within 48 h.
- Positioning: **"the only LLM gateway that understands agent tool permissions"** — lean
  (no PG/Redis), agent-native, truly open source.

## Considerations

- I don't want the license or architecture to make LCP unreachable for self-hosters; every
  external dependency is behind a standard interface or swappable plugin.
- I probably won't turn LCP into a commercial product, but AGPL-3.0 keeps that door closed
  for third parties without negotiation — the tradeoff I want.
- Durability-first: self-healing (circuit breaker, health persistence, watchdog crons),
  low-maintenance, minimal moving parts. Each new feature spec starts by asking "what breaks
  at 3 a.m. and how does it recover?"
- The operator prefers **speed-to-value and ROI-ordered milestones** (Mitchell Hashimoto's
  "My Approach to Building Large Technical Projects") — every milestone leaves a working,
  useful gateway, never a schema-only step.

## Implementation timeline

The ordering strategy serves two goals: **maximize ROI** (each step's user-visible value)
and **minimize waste** (no dev-only throwaway). Status reflects the shipped repo as of
2026-09-11.

| # | Milestone | User-visible value | Status |
|---|---|---|---|
| 1 | URL-path gateway + tool stripping + fallback | Agents routed per profile; dangerous tools blocked; opencode→deepseek chain | ✅ LIVE (Jun 15–16) |
| 2 | Circuit breaker + provider health | Dead providers skipped; `/health` shows state | ✅ LIVE (Jun 16) |
| 3 | SQLite cost dashboard | Spend visible per profile with cache hit/miss | ✅ LIVE (Jun 16–17) |
| 4 | Config-driven chains + modular split | Providers editable without code; hot-reload (file-based, superseded later) | ✅ LIVE (Jun 19) |
| 5 | v0.4.0 — keys, budgets schema, UI rework, cost plugins, component-runtime bootstrap | Key management, sidebar tree, modals, declarative boot | ✅ LIVE (Jul–Aug) |
| 6 | v0.5.0 — benchmark-driven routing, persisted health, semantic classifier, Setup wizard, OpenCode credits | Models graded and routed by task; health survives restarts; installable modules | ✅ LIVE (Aug 22, first public `main`) |
| 7 | Multi-tenant — users/teams/credits enforcement | API-key auth on chat endpoints; user credit + team budget 403/429 | 🔶 IN PROGRESS (schema live; enforcement pending — PLAN.md Phase 5) |
| 8 | Permission matrix (fails-closed) + rate limiting + audit log + dashboard roles | New tools blocked until whitelisted; per-profile allow lists; audit trail | 🔷 DESIGNED (`features/permission-plugin.md`, PLAN.md Phase 7) |
| 9 | Alert/budget persistence + UI | Alerts survive restarts; budget cards at 50/80/90% | 🔷 DESIGNED (`features/alerting-budgeting.md`) |
| 10 | Memory plugin (LanceDB) | One memory API for all clients (retain/recall/forget/count) | 🔶 IN PROGRESS (`features/memory.md`) |
| 11 | Logging enhancements (body viewer, search, waterfall) | Production debugging without `docker exec` archaeology | 🔷 DESIGNED (`features/logging-enhancements.md`) |
| 12 | Daemon-socket OS sandbox integration | Gateway provides tool policy; OS enforces cgroups | ⚪ OPEN (PLAN.md Phase 8; adjacent `homelab-daemon-socket` task) |

## Open issues

- Dashboard authentication + admin/viewer roles (PLAN.md Phase 7 item 9).
- Budget enforcement beyond key spend limit (profile/team budgets; 429 semantics).
- Alert persistence + dashboard UI (`features/alerting-budgeting.md`).
- Opt-in request/response body logging + search (`features/logging-enhancements.md`).
- Memory plugin Phases 2–3 (index management, advanced features — `features/memory.md`).
- LLM Council Tier 2 (`features/intelligent-routing.md`).
- Semantic-classifier probe API + UI decision drill-down (`features/routing-observability.md`).
- Rate limiting (token bucket per user + global cap — PLAN.md Phase 7 item 6).

## Closed issues (decision records)

| Decision | Outcome | Rationale |
|---|---|---|
| Database | **SQLite** (not Postgres) | Scale fits; zero infra; "we do NOT add PostgreSQL, Redis, pnpm, Next.js" |
| Config mechanism | **DB-backed settings** (2026-08-31); hydrate prefers DB row over seed | Live CRUD edits; no mtime hot-reload file watches; `gateway.yaml` retired |
| Language | **Python stdlib** (not Go/TS) | Already working; fast enough; no build toolchain |
| Frontend | **Server-rendered + Chart.js** (not SPA) | No build step; matches operator preference (server-rendered, shadcn dark theme) |
| License | **AGPL-3.0** (not MIT/Apache) | Blocks enterprise upsell; keeps self-hosters free |
| Routing | **Static chain + optional dynamic router** | Static chain is the fallback; when routing is on, the router reorders a copy of the chain (merit order), never mutates source config |
| Auth | **API keys (sha256)** (not OAuth) | Headless gateway; no IdP; simplicity |
| Memory backend | **LanceDB** (not sqlite-vec) | `features/memory.md` — ANN maturity + disk layout for the plugin target |
| External gateways | **Build over adopt** (rejected LiteLLM/llmgateway) | Heavy stacks; no agent tool permissions; lock-in |

## Alternatives considered

- **LiteLLM** — standard, popular, but presumes Postgres/Redis, is a huge dependency, and
  has no concept of agent tool permissions. Rejected (informs features, not adopted).
- **llmgateway (theopenco, TypeScript)** — 1.3K stars; evaluated as a possible replacement;
  different stack, still no tool-permission model, same multi-service heft. See
  `PLAN.md` "Active consideration" note; resolved: keep LCP.
- **PostgreSQL** — unnecessary operational complexity at our scale; SQLite handles millions
  of rows and a single-node workload.
- **SPA frameworks (React/Vue)** — build step, package-manager churn, routing/state
  complexity for what is a dashboard; server-rendered HTML is easier to reason about and
  test (same reasoning as the Little Moments frontend choice).
- **OPA / server-side permission services** — overkill for v1, but the
  `PermissionPlugin` architecture leaves the door open for a future plugin
  (`features/permission-plugin.md`).
- **S3-style signed URLs / proxy media access** — N/A to LCP (it has no media), but the
  *principle* transfers: prefer direct provider access with gateway-enforced policy at the
  choke point rather than re-architecting the data path.
- **Cloud-hosted gateways** — violates privacy and the ~$0 self-hosted goal.

---

## Appendix: documentation map

Every `.md` file in this repo and what it contributes to the design:

| File | Role |
|---|---|
| `README.md` | North star, features, quick start, benchmarking, why-LCP, API, roadmap, status |
| `PLAN.md` | Canonical roadmap / phase machine (this design doc's timeline is derived from it) |
| `CHANGELOG.md` | Release history; v0.4.0 → v0.5.0 delta |
| `docs/component-runtime.md` | Component runtime contract, graph, boot, request path (reference for the shipped code) |
| `docs/semantic-routing.md` | Semantic classifier + context-aware routing (module install, taxonomy, config) |
| `docs/routing-design.md` | **Routing definitions design (2026-11-16)**: the three tiers (Static/Arena/Intent), pools, capability matrix, model portfolios with releases and dimensions, derived board assignment, and the fields recorded for future classifier training |
| `docs/release-notes-v0.5.0.md` | First public release notes |
| `SECURITY.md` | Security policy, in/out of scope, supported versions |
| `features/intelligent-routing.md` | Dynamic router, Council (Tier 2), Harness (Tier 3) — Tier 1 shipped |
| `features/merit-order.md` | Chain-as-source-of-truth decision pipeline, credit-status abstraction |
| `features/deterministic-routing.md` | Unified rule/scoring provider resolution (no split decisions) |
| `features/routing-observability.md` | Classification rationale + decision log backend |
| `features/routing-judgment.md` | Replay + human judgment loop for routing decisions |
| `features/permission-plugin.md` | Phase 7 fails-closed permission layer as a plugin |
| `features/alerting-budgeting.md` | Alert persistence + budget enforcement design |
| `features/memory.md` | LanceDB memory layer plugin |
| `features/provider-health.md` | Provider health dashboard + persistence phases |
| `features/thinking-mode-recovery.md` | DeepSeek reasoning-content capture/re-attachment (shipped 0.5.x) |
| `features/system-prompt-handling.md` | Structural detection of harness-injected system prompts |
| `features/logging-enhancements.md` | Request/response logging, search, waterfall, live tail |
| `features/component-runtime.md` | Design rationale for the component runtime (paper-grounded) |
| `features/enhancements.md` | LiteLLM-comparison-driven dashboard enhancement plan |
| `features/todo.md` | Small fixes & improvements ledger |