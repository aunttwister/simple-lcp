# simple-lcp

**The profile-first control plane.** This is the simplification line of
[LCP](https://github.com/aunttwister/lcp) — the same idea, cut down to the six things it is actually
for, with a deterministic router and human-first UI.

| | |
|---|---|
| Status | **PLANNING** — this repo currently holds the plan and the measurements behind it. No simplification code written yet. |
| Code state | `main` here is the exact LCP tree at `8016792` (v0.5.0, AGPL-3.0) plus this README. Branches `main`, `dev`, `bugfix`, `harness` and tag `v0.5.0` carried over. |
| Relationship to LCP | `upstream` = `github.com/aunttwister/lcp`. Nothing is deleted upstream; this is where the cut happens first. |
| Runtime | This repo **is the staging line**. From 2026-09-23 the homelab staging instance (`lcp-staging`, port `8735`) builds and runs from this tree. Production (`lcp`, port `8734`) still builds from `aunttwister/lcp` until the cut is real. |
| Source of requirements | The operator's own voice memos of 2026-09-22 — reproduced verbatim in [Appendix A](#appendix-a--memo-1-verbatim) and [Appendix B](#appendix-b--memo-2-verbatim). |

**Why a separate repo instead of a branch.** Production serves 57,223 recorded requests across 6
profiles. The simplification is structural — tables dropped, 15 pages collapsed to 5, one module
removed — so the work needs a tree that is allowed to break. Staging gets that tree; prod keeps the
known-good one. The two no longer share a working directory (they did until now).

**There is no code fork here.** GitHub will not fork a repository into the account that already owns
it, so this repo was created and then filled with a full mirror push of LCP — same history, same
branches, same tag. The only thing missing is GitHub's fork-network relation.

---

## Contents

- [North Star](#north-star)
- [Requirements (R1–R14)](#requirements-r1r14)
- [What is measured today](#what-is-measured-today)
- [What is already right](#what-is-already-right)
- [What is wrong](#what-is-wrong)
- [The plan](#the-plan)
- [The deterministic router (R6)](#the-deterministic-router-r6)
- [The gate (R11)](#the-gate-r11)
- [Alerts and budgets by subject (R10)](#alerts-and-budgets-by-subject-r10)
- [A profile document (R2/R3/R4)](#a-profile-document-r2r3r4)
- [UI target (R8)](#ui-target-r8)
- [Removals (R1/R9/R13)](#removals-r1r9r13)
- [Parked decisions](#parked-decisions)
- [Not established](#not-established)
- [Running this repo as staging](#running-this-repo-as-staging)
- [Provenance](#provenance)
- [Appendix A — memo 1, verbatim](#appendix-a--memo-1-verbatim)
- [Appendix B — memo 2, verbatim](#appendix-b--memo-2-verbatim)

---

## North Star

**LCP is the admin panel for an LLM harness.** Not Hermes-specific — Hermes is the first harness it
governs, not the thing it is. One environment where the operator:

1. defines **profiles** (what a lane of work is for),
2. points them at a **pool of providers and models**,
3. declares **what each model can do**,
4. sets **routing** (static or dynamic) and **circuit-breaker** behaviour,
5. sets **gates and budgets** so nothing is spent before it is approved,
6. watches **what happened** — as diagrams, not log dumps.

Everything that is not one of those six is either an optional module or is removed.

> "the goal of this is to have the control plane of Hermes or other LLM, whatever harness inside of
> this … environment. It's like an admin panel. It's a control plane over it."

---

## Requirements (R1–R14)

Every row is the operator's own framing, not an inference. Quoted fragments are verbatim.

| # | requirement | source |
|---|---|---|
| R1 | Drop proof-of-concept features — *"it's quite cheap to do that"* | memo 1 |
| R2 | Profile-based configuration: a profile defines a **pool** of models/providers | memo 1 |
| R3 | Circuit-breaker pattern defined **per profile**, over that pool | memo 1 |
| R4 | Per profile: routing mode = static chain **or** dynamic | memo 1 |
| R5 | **Intents declared per profile** (l2 → code planning; an architect profile → architecture planning); the semantic router scores only that profile's intents | memo 1 |
| R6 | Dynamic routing *"world class"*: a **very deterministic algorithm** — *"merit order … sortation … this list needs to be very deterministic"* | memo 2 |
| R7 | Observability as **VSM-style diagrams** — *"attractive to the eye. We should put effort there"* | memo 2 |
| R8 | Human-first UI — *"the nav bar is huge amounts of different configuration pages, complicated and messed up"* | memo 1 |
| R9 | Drop per-profile API keys — *"is that necessary? in my opinion no"* | memo 1 |
| R10 | **Alerts stay.** Alerts and budgets are defined **per subject**: provider · profile · API key — on their own **alerts page and budgets page**, *"not on profile or API key. That doesn't work like that"* | memo 2 |
| R11 | A **gate before committing** — *"some sort of gate before committing"*; LCP is the control plane over the harness | memo 2 |
| R12 | **Balance-aware routing** — *"we're not taking into account the available balances. We're only reacting if we have insufficient balance"* — rank across subscriptions (opencode, commandcode, …) by remaining balance | memo 2 |
| R13 | **Obsolete LiveBench inside LCP** — *"testing a model on LiveBench is insane via this app"* — declare model capabilities by hand, then select | memo 2 |
| R14 | This work is the **`simplify-lcp`** task, on the L2 profile; it retires the in-progress `llm-control-plane` task | memo 2 |

---

## What is measured today

All figures below were read live from `costs.db` (read-only) on 2026-09-22/23 against LCP `main`
`8016792`. They are measurements, not estimates — they are what makes the cut list safe to act on.

### Tables, with verdicts

| table | rows | verdict |
|---|---|---|
| `requests` | 57,223 | **KEEP** — the spine |
| `routing_decisions` | 34,133 | **KEEP** — but 124.4 MB of duplicated transcripts inside it → strip |
| `conversations` | 1,409 | **KEEP** — the R7 spine |
| `failover_events` | 287 | **KEEP** |
| `model_registry` | 15 | **KEEP — the pool primitive** |
| `provider_health` / `provider_credentials` | 11 / 7 | **KEEP** |
| `daily_summary` | 45 | keep (or recompute on read) |
| `capability_metrics` / `model_capability_subtasks` / `model_capabilities` | 496 / 434 / 116 | **CUT** — all three are `source=livebench` (R13) |
| `alerts` | **0** | **KEEP** — R10 wants it; implemented, never used, needs restructuring |
| `api_keys` | 11 | **CUT** — no enforcement path exists (`auth_required: false` on 4 of 6 profiles) |
| `audit_logs` | **0** | **CUT** — the gate records into `requests` + `routing_decisions` instead |
| `budgets` | **0** | **CUT as shaped** — user/team-shaped; R10 re-forms it per subject |
| `users` / `teams` | **0 / 0** | **CUT** — Phase 5 multi-tenant |
| `routing_judgments` | **0** | **CUT** — verify the 02:00 routing-assessment cron's write target first |

### Profiles by real traffic

| profile | requests | first | last | verdict |
|---|---|---|---|---|
| `l2` | 38,925 | 2026-06-15 | live | keep |
| `coder` | 12,347 | 2026-07-27 | 2026-09-20 | keep |
| `l1` | 5,932 | 2026-09-04 | live | keep |
| `naptune-admin` | 11 | 2026-08-29 | 2026-08-30 | dead (one day) |
| `career` | 8 | 2026-08-04 | 2026-09-18 | effectively dead |

### Providers actually serving (14 days) — why R12 matters

| provider / model | n | avg TPS | cost | $/request |
|---|---|---|---|---|
| commandcode / deepseek-v4-flash | 11,980 | 688.6 | $213.28 | **$0.0178** |
| opencode / deepseek-v4-flash | 4,445 | 313.9 | $147.44 | **$0.0332** |
| llamacpp / qwen3.8-flash-next | 4,454 | 1,320.2 | $0.00 | $0 |
| opencode / deepseek-v4-pro | 8 | — | — | — |
| deepseek / deepseek-v4-flash | 2 | — | — | — |
| **unknown / unknown** | **800** | — | — | defect to fix while in here |

### Bloat found while measuring

`routing_decisions.conversation_json` holds **124,430,043 bytes** across 33,916 rows — 73% of the
169 MB database — as a second copy of the Hermes transcripts. Separately, `requests.conversation_id`
is NULL on 4,084 of 57,236 rows (93% link correctly).

---

## What is already right

### The pool primitive already exists: `model_registry`

`model_registry` maps **one logical model → many provider IDs**:

```json
{"logical_name": "deepseek-v4-pro",
 "provider_mappings_json": {"deepseek": "deepseek-v4-pro",
                            "opencode": "deepseek-v4-pro",
                            "commandcode": "deepseek/deepseek-v4-pro"}}
```

That is exactly the *"pool of models/providers"* R2 asks for — already in the schema, already
populated for 15 models. Today `gateway_config:profiles` re-declares a chain per profile with
`api_base` pasted inline. simple-lcp points profiles at the registry instead of restating it.

### Dynamic routing's headline move is worth keeping

24,417 semantic decisions in 14 days. The dominant effect is **one** move:

| from | to | times |
|---|---|---|
| l2: opencode/deepseek-v4-flash | commandcode/deepseek/deepseek-v4-flash | **12,036** |
| l1: (none) | local-zgx/qwen3.8-flash-next | 7,922 |
| l2: (none) | opencode/deepseek-v4-flash | 3,896 |

And it earns its keep: **2.2× faster, 1.9× cheaper** per request (see the provider table above).

---

## What is wrong

**The router's justification is noise.** The outcome is right, the reasoning behind it is not:

| metric | value |
|---|---|
| top-1 vs top-2 intent-score margin | **median 0.0236** (p10 0.005, p90 0.046, max 0.281) |
| decisions with margin < 0.05 | **91.7%** |
| decisions with margin < 0.02 | **39.4%** |
| `routing_min_score` gate | 0.35 — never fires; cosine sims sit ≥ 0.5 |

Mechanism: `bge-small-en-v1.5` cosine similarity against **8 global exemplar centroids**
(`src/api/task_classifier.py`, `TASK_EXEMPLARS`). The labels are global, so an infrastructure-ops
profile gets labelled `code_generation` 9,842 times, and the winner on the two-character message
"Soo?" was `casual_chat`, whose exemplars are literally "hello" / "thanks!". The label is *stable*,
not *separating*: the top five sit in a 0.11 band.

**The rules engine is nearly unused.** `routing_rules` = 3 entries, all `prefer deepseek-v4-flash`
**for `coder` only**; `rules_json` is `[]` on every l2 decision. `action=prefer` fired 157× against
11,822 `reorder`s.

**Balances are collected and never used for routing (R12).** `fetch_balance()` exists in the cost
plugins (`deepseek` → `/user/balance`; `opencode` → scraped; `commandcode` → always `None`, no public
API) and `cost_cache.py` stores the payloads. The **only** place balance affects routing is
`benchmark.py:770`, which treats `"insufficient balance"` as a fallback trigger. Balance data is
fetched, cached, and ignored when choosing a lane.

**Config lives in two namespaces.** 11 `gateway_config:*` sections *plus* 6 bare `routing_*` keys.

---

## The plan

| # | milestone | gate |
|---|---|---|
| M1 | **Confirm the cut list** and the removals-vs-module split | operator |
| M2 | Profile-first UI — 15 pages → 5, one profile page as the entry point (R8/R2/R3/R4) | M1 |
| M3 | Per-profile intents + the margin gate (R5, L2 below) | M1 |
| M4 | Deterministic L0–L4 + the 5 invariants as tests (R6) | M3 |
| M5 | Balance-aware ranking (R12) — needs a `commandcode` balance source (no public API today) | M4 |
| M6 | Gate (R11) + alerts/budgets by subject (R10) | M4 |
| M7 | Diagram observability (R7) + strip `conversation_json`, backfill `conversation_id` | M1 |
| M8 | Obsolete LiveBench (R13); declared capabilities become the only source | M1 |

M1 is the only gate. M2–M8 are independent of each other and of any repo/naming decision.

---

## The deterministic router (R6)

Determinism means: **same request + same state ⇒ same decision**, reason recorded, replayable.

```
L0  HARD FILTER    drop candidates on capability mismatch, dead/degraded breaker,
                   missing credential, G2/G3 floors.   (no scoring, no randomness)
L1  EXPLICIT RULES priority-ordered, first non-empty layer wins:
                   {priority, match:{profile, task, intent, tool, prompt_regex},
                    action: pin | prefer | deny}
                   ordered by (priority, rule index)
L2  PER-PROFILE INTENT   score ONLY against this profile's declared intents (R5)
                   gate: (top1 - top2) < intent_margin_gate  -> DO NOT REORDER
L3  RANK           total order over survivors:
                   (role_rank, -declared_quality, balance_headroom, cost_bias*cost, chain_index)
                   chain_index is the FINAL tiebreak -> total order, no ties, no RNG
L4  RECORD         {layer, rule_id, intent, margin, scores, balances, from, to, reason}
                   one replayable row
```

Invariants — property tests, not examples:

1. permuting the input pool never changes the winner unless `chain_index` decides it;
2. replaying L4 on a recorded row reproduces the same winner;
3. margin < gate ⇒ output == static chain head, always;
4. no `random`, no wall-clock, no dict-iteration-order dependence anywhere in L0–L3;
5. with all balances equal, the result equals the pre-balance-aware result (R12 is additive).

The single highest-value change is L2's gate: it deletes the 39.4% of decisions that are currently
coin flips, and costs nothing measurable — the move in [What is already
right](#what-is-already-right) came from L1→L3 in the first place.

---

## The gate (R11)

A preflight gate evaluated **before** the request is released to a provider:

```
G0 capability   model supports vision / context / thinking required by this request
G1 breaker      target not dead/degraded
G2 budget       profile + subject budgets have headroom        -> else deny or downgrade
G3 balance      provider balance above its floor               -> else re-rank, not fail
G4 intent gate  intent margin >= gate                          -> else keep static order
-> PASS (carry the chosen lane) | DOWNGRADE (a named cheaper lane) | DENY (explicit code)
```

Every verdict is written to `routing_decisions` with its layer id, so the R7 diagram can show the
gate outcome. It fails **loud** — an explicit code, never a silent pass, matching the existing
`LCP-4001` style already in the codebase.

---

## Alerts and budgets by subject (R10)

`alerts` today is subject-agnostic (`rule, severity, dedup_key, metadata_json, status,
acknowledged`). Extend with `subject_type ∈ {provider, profile, api_key}` + `subject_id`, and give
each subject its own page.

| subject | what is watched | example |
|---|---|---|
| **provider** | balance, error rate, breaker state, spend rate | "commandcode balance < $5" |
| **profile** | spend vs budget, tokens, blocked-tool attempts | "l2 over $40/day" |
| **api_key** | spend, request rate, last used | "key X unused 30 days" |

Budgets reuse the same three subjects. The existing `budgets` table is user/team-shaped (0 rows) and
is replaced by this, not dropped as a capability.

---

## A profile document (R2/R3/R4)

| field | today | simple-lcp |
|---|---|---|
| `intents` | 8 global labels, one shared taxonomy | **per-profile list**, each with exemplars |
| `pool` | `chain` re-declaring `api_base` inline | ordered pool of **`model_registry` logical names**, each tagged `role`: workhorse / escalation / free-local |
| `routing` | `routing_enabled`, `routing_policy`, `routing_min_score`, plus `routing_enabled:<profile>` keys in a *second* settings namespace | `mode: static \| dynamic` + `intent_margin_gate` + `cost_bias` + balance weights |
| `breaker` | global thresholds only | per-profile override over its own pool |
| `permissions` | `forbidden_tools` (fails open) | allow-list, fails closed |
| `budget`/`alerts` | n/a | **not here** — R10 puts them on their own pages |
| `surfaces` | separate top-level product (`work_*`, 3,645 LOC) | **module**, enabled per profile |

---

## UI target (R8)

Measured today: **9 sidebar entries, 15 pages**, configuration spread across 5 of them.

| today (page LOC) | after |
|---|---|
| models 1,257 · dashboard 1,079 · usage 999 · providers 958 · work_tasks 550 · setup 404 · profiles 357 · logs 314 · work_cron 312 · alerts 174 · work_conversations 144 · keys 139 · work_fleet 135 · work_decisions 135 · work_config 131 | **Profiles · Providers & Models · Routing · Alerts & Budgets · Logs** (+ per-profile page as the entry point) |

Per conversation, the R7 render target is one timeline — the shape to draw, not a log table:

```
message "Soo?"
  gate      : PASS  (budget 82% headroom, provider healthy, capability match)
  routing   : ON — profile l2, mode dynamic
  intent    : casual_chat 0.7202 · margin 0.0236 · gate 0.05 → BELOW GATE → static order kept
  pool      : opencode/flash (workhorse) · commandcode/flash (fast) · local-zgx (free)
  balance   : opencode $12.40 · commandcode $3.05 · local-zgx n/a
  choice    : commandcode  reason: L3 balance_term (most headroom of the two paid lanes)
  result    : 4,120 in / 812 out · $0.021 · 3.4 s · tools 12 allowed / 0 blocked
```

Every field already exists in `routing_decisions` (20 columns), `requests` (16) and `conversations`.
The work is **join + render + the two data fixes**, not new instrumentation.

---

## Removals (R1/R9/R13)

| remove | measured footprint |
|---|---|
| LiveBench inside LCP (R13) | `benchmark.py` 906 + `benchmark_import.py` 466 + `livebench_tasks.py` 601 = **1,973 LOC** src, **1,314 LOC** tests, 2 migrations, 3 tables |
| Phase 5 multi-tenant | `users`, `teams`, `budgets` schema + migration 002 |
| `api_keys` (R9) | 11 rows, `key_manager.py` 306 LOC, `keys.html` 139 LOC |
| `audit_logs` | schema + migration; replaced by the gate record |
| `routing_judgments` | table (0 rows) — verify the 02:00 assessment's write target first |
| `conversation_json` dual storage | **124 MB** of the 169 MB DB |
| the `/work*` surface as a top-level product | 3,645 LOC python + ~1.5k LOC of Jinja pages → **module** |
| `naptune-admin`, `career` profiles | 19 requests ever, between them |

---

## Parked decisions

| # | decision | default |
|---|---|---|
| D1 | fork to a new repo vs branch in place | **done** — separate repo (`this one`), staging builds from it; prod stays on `aunttwister/lcp` |
| D2 | per-profile API keys | **cut** (R9; 11 rows, no enforcement) |
| D3 | where the work surface goes | **module** (a `plugins` block and the `/opt/lcp-modules` mount already exist) |
| D4 | editing skills from LCP | browse + review only for now — `profiles` mounts read-only; the only writable work path is the cron-ops spool |
| D5 | `commandcode` balance | no public API; needs a scraper or a manual override before M5 can rank it |
| D6 | repo name | **`simple-lcp`** (the operator's words: *"we'll call it simple LCP"*) |

---

## Not established

- Whether anyone outside the homelab uses the public repo. Contributors from git history: aunttwister
  ×465 + 2 agent commits.
- Whether the 800 `unknown/unknown` requests are a routing defect or a logging gap.
- Whether `routing_judgments` is truly unused (0 rows) or written by the 02:00 assessment outside this DB.
- What "VSM" means to the operator — flow/sequence diagram of the request pipeline, or value-stream
  map (queue time, wait states, handoff cost per stage). It changes the renderer, not the data.

---

## Running this repo as staging

The homelab staging instance serves **this tree** on port `8735`. Production is a different compose
project on `8734` and must not be touched.

```bash
cd /your/data/docker-apps/simple-lcp

# refresh from this repo, then rebuild
git pull
docker compose -f docker-compose.staging.yml -p lcp-staging up -d --build   # never `down`/`stop`

# verify
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8735/health
```

| property | value |
|---|---|
| compose project | `lcp-staging` |
| container | `lcp-staging` |
| host port | `8735` → container `8734` |
| data volume | `/your/data/app/lcp-staging/data` (its own DB; not prod's) |
| modules | `/your/data/app/lcp/modules` — shared with prod on purpose (~12 GB of installed deps) |
| `.env` | required, git-ignored: `LCP_SECRET_KEY`, `LCP_MODELS_PATHS` |

Read-only mounts (`board ledger`, task tree, profiles) and the writable cron-ops spool are documented
inline in `docker-compose.staging.yml`.

---

## Provenance

Written from the `simplify-lcp` task plan (`PLAN.md`, 2026-09-22, homelab-expert-l2 profile). Every
number above was measured live against `costs.db` and the repo at `8016792`; nothing here is
estimated. The two memos below are the operator's requirements in his own words and outrank any
summary of them — including this one.


---

## Appendix A — memo 1, verbatim

_Operator voice memo, 2026-09-22. Transcription, unedited apart from removing the speech-to-text prefix marker. Source: `homelab-expert-l2` session `20260922_140751_49233eb9`._

> So, I'm trying to summarize the, you know, I'm reviewing LCP and I'm trying to summarize like the good parts and the bad parts. The parts that I want to focus on and the parts that were, you know, maybe a proof of concept or something like that. You know, certain things have been developed up until now, but I think it's time to, you know, drop them off. It's quite cheap to do that, you know. So, I was thinking, you know, to just say here the number of things or features I like about LCP and we're going to make it like, you know, compact or something, LCP, not compact, but it's going to be a true LCP or something like that, which is going to have only a subset of features, right? So first things first, like the, the, because I'm using Hermes so much, but this is not, I believe, necessarily, you know, related to Hermes. I would like to have a, you know, profile-based, you know, configuration setup, right now what we have in the nav bar is huge amounts of different configuration pages, types of configurations. It's all complicated and messed up. I feel like least human, you know, and UI for humans. So again, only the main, mainly making it for humans, but the UI is like, okay, you want to make a profile and profile, you want to define a pool of, you know, models slash providers, you want to define circuit breaker pattern, you know, with the models, you want to say whether this profile will be dynamically routed or statically routed via circuit breaker, you know, approach. Then, you know, promote profile, you're going to have tasks, you're going to have API keys. I think, you know, having API keys for multiple profiles is like, what the fuck? Is that necessary in my opinion? You know, stuff like that, cron jobs, tasks and et cetera, you know, so it's going to be, you know, and also we're going to pull skills, we're going to review skills. You know, edit them maybe and stuff like that. The idea is to give, you know, a user this clear experience into what profile is, what is profile consistent of, what is the intent to profile and purpose of the profile, et cetera. We're also going to define the intents for a certain profile, so for example, you know, for L2, we're going to define intents, you know, for code planning for this, but for some architect profile, we're going to define architecture planning and et cetera. So based on the profile, the dynamic router or this, you know, semantic embedded decision making for what's the intent of a message is going to also take into the account the available intents of a certain profile, so it can be more precise, right? Then the dynamic router is something that I really want to focus on. I feel like it's, you know, we're getting there, but it's not, you know, complete at all. There needs to be a certain, so to say, algorithm and like very deterministic way of how the rules are applied and how the things are, you know, how the merit to order to order lists or the sortation of, you know, different parameters that's going to decide in how and where the message is going to be routed, this list needs to be very deterministic, you know, and we are somewhere there, but not really, you know, it's not very clear yet, so we need to put more focus on that, observability, logs, I want, you know, decent logs, I want conversations, I want a conversation that starts from, you know, a message, you know, decision to make an intent, you know, see, I mean, I want to diagram literally that's going to be like message, dynamic routing is on or off if it's on, you know, decide was the intent, this is the intent is routed to this profile based on this, and then, you know, the message tool calls everything that, you know, Hermes already shows.

---

## Appendix B — memo 2, verbatim

_Operator voice memo, 2026-09-22 (follow-up in the same thread). Transcription, unedited apart from removing the speech-to-text prefix marker._

> I think alerts are okay. I do want to make some sort of, how to say, like, I forgot the word. Like some sort of gate, you know, before committing. I mean, Hermes already has a gate. But I want to, the goal of this is to have the control plane of Hermes or other LLM, whatever harness inside of this, you know, environment that's somehow my goal. It's like an admin panel. It's a control plane over it, you know. So, overall, if you can go on and create a task that's going to be like, hey, you should, you know, state all of these things. And I know that routing, dynamic routing is working, but I want to make it word class. I want to have a very deterministic, like, algorithm, you know. For example, in our dynamic routing, we're not taking into the account the available balances. You know, we're only reacting if, you know, we have insufficient balance. So, stuff like that, you know, we can, you know, if we have both open code and command code and blah blah blah, and other, you know, subscriptions, we can, you know, react differently. And, you know, aim at the, you know, one that has less balance or more balance and etcetera. So, make a task that's going to simplify LCP, call it like that. And then we're going to retire this LCP task, which is in progress. And this simplify LCP is going to be on L2, you know, expert profile task. And it's going to, yeah, it's going to have a description of the things that I just said or we just said are important. And then also the, you know, what we shall remove. Also, I don't know if I mentioned, so logs for observability and should be displayed in the manner that I said, like VSM diagrams, like it should be nice. It should be attractive to the eye. Like we should put effort there. Second, we need to have, I want to have alerts. I want to have budgets, but for a profile, for example, or provider or stuff like that. And then these budgets and alerts need to be set up on alerts page and budgets page. Not on, you know, profile or API key. That doesn't work like that. So, we need to have a different ways how to set alerts and budgets. So, we should say provider alert, not model, but provider alert. We need to say the, oh my God. The API key alert, profile alert. So, you know, for example, three different layers of alerts or ways or subjects of measurement, something like that. And then what else? Yeah, providers and models. We need to define the, you know, capability of a model. Livebench is good, but you know, testing a model on a livebench is insane via this app. Like, what the fuck? We should not do that. We should, you know, obsolete that in this new version. You know, we'll call it simple LCP. And yeah, we're just going to define, you know, the capabilities of different models that we decide. Then we select and that's all, you know. So yeah, make a task.
