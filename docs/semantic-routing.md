# Semantic Dynamic Routing

LCP classifies every request into a **task type** and routes it to the
best-fit `(provider, model)` for that task. The classification is
**semantic** — the prompt's *meaning* decides the task, not exact keywords.
This document describes that embedding-based classifier, how it integrates
with the dynamic router, and how the module is installed.

---

## 1. The two halves of routing

Routing is two stages that meet at the task string:

```
request ──► classify_task ──► task type ──► CapabilityRouter ──► (provider, model)
             (semantic)                        (scores + cost + health + rules)
```

1. **Task classification** — `src/api/task_classifier.py` embeds the user's
   intent and picks the nearest task by meaning. This is the *semantic routing
   module*.
2. **Model selection** — `src/api/router.py::CapabilityRouter` scores each
   `(provider, model)` step by its benchmarked capability for that task, a
   cost bias, circuit-breaker health, and any per-profile rules. This is
   *benchmark-driven routing* (see
   [Benchmarking (LiveBench)](../README.md#benchmarking-livebench)).

The classifier emits the same task strings the router has always consumed, so
the two halves are fully decoupled.

### Context-aware routing

**The router never selects a model whose context window can't hold the
request.** This is a hard guarantee, layered on top of model selection:

1. **Request size** — the classifier already computes the request's token
   count (stripped of client-injected context); `select_step` carries it
   through selection.
2. **Context gate** — a candidate model is excluded when
   `request_tokens + output_reserve` exceeds its served `context_window`
   (read from `model_limits`; 8k reserve for the response). A `prefer` rule
   that names a too-small model is skipped, and fallback steps that can't fit
   are dropped from the chain — so a request routes to the **next-best model
   that fits** instead of failing upstream with `exceed_context_size`.
3. **Honest advertisement** — `/v1/models` reports the profile's **maximum
   routable context** (the largest `context_window` among chain models). The
   router guarantees any request up to that size is servable — if the
   best-scored model is too small, it falls to a larger one. This is the
   honest contract: "N tokens" means "a model with ≥ N context will serve it."
4. **413 fallback** — when a request exceeds *every* model in the chain, the
   gateway returns a clean `413 Request Too Large` (`RequestTooLargeError`)
   instead of forwarding to a provider that will 400.

**Context source.** A model's `context_window` comes from
`config.model_limits`. When a provider is added (or updated) via the UI, the
gateway **auto-learns** each model's served context from the provider's
`/models` metadata (`discover_models` — llama.cpp exposes `meta.n_ctx`, the
served context, e.g. 200192) and upserts it into `model_limits`. Models with
no entry default to a conservative `128000` so an unknown-capacity local model
is never sent an oversized request.

**Why advertise the max, not the min.** With per-request context gating, the
router can always fall to the largest model in the chain, so advertising the
max is truthful and lets clients use the profile's full long-context capacity
— while small requests still route to cheaper/smaller models. (Advertising the
min would be safe but wastefully shrink every client's usable window.)

### Dependency: benchmark capabilities (LiveBench)

**Semantic routing is only as good as the capability scores it routes by.** It
classifies *what* the task is; the router then needs per-task capability
grades to pick the best `(provider, model)`. Those grades come from the
benchmark capability matrix:

- **LiveBench module** — the primary producer. Running benchmarks grades your
  provider models into `model_capabilities` (`source="lcp_benchmark"`).
- **Bundled leaderboard snapshot** — `seed_livebench` imports the shipped
  LiveBench leaderboard (`source="livebench"`) into the same matrix without
  running benchmarks (see
  [Seeding scores without running benchmarks](../README.md#seeding-scores-without-running-benchmarks)).

The **Setup page gates Semantic routing install on graded capability data**
(the matrix having rows), not on the LiveBench module being installed:

| State | Semantic routing install |
|---|---|
| Capability matrix has scores | installable |
| No scores + LiveBench installed | blocked — "run a benchmark (or seed the snapshot)" |
| No scores + no LiveBench | blocked — "install LiveBench and run a benchmark, or seed the snapshot" |

The LiveBench card shows the current matrix state ("N models · M tasks graded"
or "No graded scores yet") and a one-click **"Seed baseline scores"** action
(`POST /api/models/capability/seed`) that imports the bundled snapshot
instantly — the exit from the gate. The install API enforces the same check
server-side, so it can't be bypassed by calling the endpoint directly.

## 2. Task taxonomy

The classifier produces one of eight task types:

| Task | Meaning |
|---|---|
| `agentic_multi_step` | Multi-step agent work using tools |
| `unit_tests` | Writing/running tests |
| `code_generation` | Writing, reviewing, or refactoring code |
| `debugging` | Diagnosing a failure / traceback |
| `research_deep` | Deep explanation, analysis, comparison |
| `reasoning_chain` | Step-by-step logic, math, complexity |
| `planning` | Architecture, roadmaps, "plan this" |
| `casual_chat` | Small talk |

## 3. How classification works

Each task has a small set of **exemplar prompts** (`TASK_EXEMPLARS` in
`task_classifier.py`). When the embedder is available:

1. The exemplars are embedded once and **averaged into a per-task centroid**
   (cached for the process lifetime).
2. The **intent message** is extracted — the newest *genuine* user instruction,
   walking backward over assistant/tool messages, tool-result echoes, client
   context wrappers, system-prompt preambles, and continuation acks.
3. The intent is embedded and compared to every centroid by **cosine
   similarity**.
4. The nearest task wins **only if its score clears the semantic gate**
   (`min_score`, default `0.35`). Below the gate, classification falls through
   to the degraded-mode heuristics.

When the embedder is unavailable (module not installed, or the probe returns
no signal) the classifier is a no-op and routing falls back to degraded
heuristics: an agentic system-prompt check, tool-count/token-count heuristics,
then `casual_chat`, then a `code_generation` default. This keeps routing fully
functional on a lean image before the module is installed.

### Model

- **Embedding model:** `BAAI/bge-small-en-v1.5` (384-dim), the same model the
  memory plugin uses.
- **Framework:** `sentence-transformers`, CPU by default.
- **Availability probe:** the classifier builds an `EmbeddingModel` and probes
  it by embedding a sentinel string, so a broken/partial install reports
  "unavailable" instead of throwing at request time.

### Observability

`classify_task_detail` returns the full rationale — the path taken
(`semantic` vs degraded), the intent text, the top-N `(task, score)` list, and
the `min_score` gate applied. Routing decisions persist this rationale in the
`routing_decisions` table (`path`, `semantic_json`, `min_score` columns), so
every route decision can be audited after the fact. See
[`features/routing-observability.md`](../features/routing-observability.md).

## 4. Configuration

The classifier reads `plugins.router` from the DB-backed config:

```yaml
plugins:
  router:
    enabled: true            # false disables semantic classification
    min_score: 0.35          # cosine-similarity gate for a semantic match
    embedding:
      model: BAAI/bge-small-en-v1.5
      device: cpu
    site: <LCP_MODULES_DIR>/router       # optional override
    models_dir: <LCP_MODULES_DIR>/models/router   # optional override
```

Only `enabled` and `min_score` are commonly tuned; `site`/`models_dir` are
resolved automatically from `LCP_MODULES_DIR`.

## 5. Module installation

Semantic routing is an **installable module**, grouped with LiveBench in the
Setup page (the same "not installed → Install → Remove" lifecycle as memory):

- **Deps:** `sentence-transformers>=3.0` + `tokenizers==0.22.2` (pinned —
  transformers requires `tokenizers` in `[0.22.0, 0.23.0]`, and `0.23.1` is
  the only later release, which is out of range).
- **Install target:** `pip install --target <LCP_MODULES_DIR>/router`, so the
  deps survive container recreation and `remove_router` can delete them
  without touching the memory or LiveBench installs.
- **Model weights:** pre-downloaded to `<LCP_MODULES_DIR>/models/router` during
  install so the first classification never hits the HuggingFace Hub at
  runtime.
- **Availability:** `router_available(site)` probes with a fresh subprocess
  (`importlib.util.find_spec('sentence_transformers')`), so a `--target`
  install is detected correctly.
- **Status:** `router_status()` reports `available` / `removable` / `active`.
  `removable` is true when the deps live *exclusively* in the `--target` dir.
  When baked into the image (`WITH_ROUTER=1`) the Setup page still shows a
  **Remove** button: it uninstalls the baked deps from the running container's
  image layer (runtime-only — a rebuild re-bakes them). Removal is refused
  while the Memory module is also baked, because both share
  `sentence-transformers`/`torch`; rebuild the image lean
  (`WITH_ROUTER=0` / `WITH_MEMORY=0`) to manage both from Setup.

After install/remove, `invalidate_semantic_classifier()` drops the cached
classifier so the next request rebuilds it against the new deps — no restart
needed.

### Bake-in vs runtime install

The **default image is lean** (`WITH_ROUTER=0`) and installs the module from
the Setup page, matching the memory and LiveBench modules. To bake it into the
image instead:

```bash
docker compose build --build-arg WITH_ROUTER=1 lcp
```

Baking installs `sentence-transformers` globally and pre-downloads the model
to `/app/models/embedding` at build time (instant availability, larger image).

## 6. Relationship to the memory plugin

Semantic routing and the memory bank are **independent modules** that happen to
share the same embedder type and model (`bge-small`). They install into
separate directories (`<LCP_MODULES_DIR>/router` and
`<LCP_MODULES_DIR>/memory`) and can be installed/removed independently. The
classifier reads `plugins.router` only — it never depends on `plugins.memory`.

On a **baked** image the two share one pre-downloaded model cache at
`/app/models/embedding`; on a lean image each module downloads its own copy
into its own `models/<module>` dir.

## 7. Verification

- **Boot log:** `semantic_classifier_ready` (with `min_score` and
  `models_dir`) when the module activates; `semantic_classifier_unavailable`
  otherwise.
- **Probe:** `router_available()` / `router_status()` via the Setup manifest.
- **Tests:** `tests/test_task_classifier*`, `tests/test_router.py`,
  `tests/test_router_db_config.py`, `tests/test_memory_runtime.py`,
  `tests/test_memory_setup.py` cover classification, the semantic gate,
  the rationale, and the install/remove lifecycle.

## 8. Pitfall: a host path is invisible inside a container

**This has now cost two debugging cycles. Read it before adding any module that
reads a file from the host.**

A path that exists on the host does **not** exist inside the container unless it
is explicitly mounted. The read fails with `FileNotFoundError` (or SQLite's
`unable to open database file`) — which is **indistinguishable from "there is no
data"**. There is no error that says "you forgot a bind mount".

It bit the Work layer twice:

| case | symptom | fix |
|---|---|---|
| decisions ledger | page rendered, `available: False` | mount the ledger at `/app/work/decisions.db:ro` |
| profile session DBs | `OperationalError: unable to open database file` for every profile | mount `/root/.hermes/profiles:/app/profiles:ro` |

**Rules that follow:**

1. **Every module that reads host state must declare its path as an env var**
   (`LCP_WORK_DECISIONS_DB`, `LCP_WORK_TASKS_DIR`, `LCP_SELF_BASE`) *and* have the
   mount documented next to it. The env var alone is not enough — the path will
   resolve and then fail.
2. **Mount `:ro` by default.** A view of what happened must not be able to change
   what was recorded. Read-only is enforced at the container boundary, not by the
   code remembering to open SQLite read-only.
3. **Prefer a `available: False` sentinel over a blank page.** When the source is
   genuinely absent the view should say so, naming the resolved path — otherwise
   the next person sees an empty table and blames the query.
4. **SQLite in WAL mode is readable `:ro`** provided the `-wal`/`-shm` files are
   visible too, which mounting the *directory* gives you. Mounting a single `.db`
   file out of a WAL database can read stale. Verified against live files:
   `l2` 2,794 sessions / 122,351 messages, `l1` 204 / 29,393, `blog-writer` 59 / 3,007.
5. **A module's model weights and venv are part of the module.** They install
   under `<LCP_MODULES_DIR>/…` and are present **only when the module is
   installed**. Never assume a runtime exists because the code imports it — probe
   it (`*_available()`) and fail soft. This includes every optional judge model:
   its weights and venv belong to the module that installs them, not to LCP core.
