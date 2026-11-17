# LCP Routing — Design Doc

## Metadata

- **Author:** aunttwister (maintained with homelab-expert L2)
- **Status:** APPROVED 2026-11-17. The Arena tier is IMPLEMENTED, deployed to staging and
  verified live (§17). The `Routing definitions` authoring page is not built (§17.5).
- **Created:** 2026-11-16 · **Revised:** 2026-11-17 — (1) a score is keyed
  `(portfolio, release, quantisation)`; (2) the intent model is described as its real
  two-step process; (3) §7's rank-correlation derivation measured as non-discriminating
  and RETIRED; (4) two label spaces decided; (5) §16 gap and edge-case analysis; (6) §17
  implementation status with live evidence.
- **URL:** `docs/routing-design.md` in github.com/aunttwister/lcp
- **Mirrored:** LeafWiki `okf-homelab-documentation/local-ai/routing-design`

> **Read §1-3 first.** Most of the confusion in this area has been several different
> things sharing one name; the vocabulary is defined before the design.

---

## 1. The question routing answers

**Given this request, which of my models should serve it?**

Everything else is about *how you know* which model is best. There are three ways to
know, and they are the three tiers.

---

## 2. Two kinds of ranking — different objects

| | **LMArena leaderboard** | **Intent taxonomy** |
|---|---|---|
| What is ranked | **models** | **messages** |
| Ranked by | Elo / net improvement, human votes | similarity to example messages |
| Input | nothing of ours | the user's own request text |
| Output | "who is best at board X" | "which label fits this message" |
| Defined by | LMArena, weekly | **the user** |
| Unit | per board, ~419 models, ~11–12 of ours | per label, as coarse as you like |

A leaderboard says **who is good**. A taxonomy says **what this request is**. Routing
needs both: *what is this* → *who is good at that*. Neither replaces the other.

---

## 3. The pool — static routing is more than an ordering

**Static routing is the pool of `model:provider` pairs a profile may use.**

That is its primary job. The order is fallback priority, but the *membership* is what
every other tier draws from:

```
static chain (global default, optionally overridden per profile)
   = the set of model:provider pairs this profile may serve
   = the pool dynamic routing may choose from
```

Two consequences that are easy to miss:

1. **Dynamic routing never leaves the pool.** Arena's ranking walk walks *the pool* in
   rank order — not the whole estate. A model outside the pool is not a candidate,
   however highly LMArena rates it.
2. **Chain order is also provider preference.** If the pool lists
   `opencode → deepseek-v4-flash` above `commandcode → deepseek/deepseek-v4-flash`, then
   for the *same* model, `opencode` wins in dynamic routing too. Preference expressed
   once, honoured everywhere.

---

## 4. The three tiers

### Tier 1 — Static

**Send every request to pool entry #1. If it fails, try #2.**

- No measurement, no classification.
- Fully user-defined. Nothing can be "wrong" — you said what you wanted.
- **The default, and the fallback for every other tier.**

### Tier 2 — Arena (dynamic, zero effort)

**Classify the request to an intent → determine which board judges that intent → walk
that board's ranking, filtered to the pool → the first survivor wins.**

- The user does nothing. No scores, no examples.
- Entry-level dynamic routing: immediate feel for how it behaves.
- Its value is as a **baseline** — "if I configure nothing, what does consensus say?"

```
request → intent (judge) → board (derived, see §7) → ranked list
        → filter to the pool (membership + provider preference + breaker state)
        → first match wins
        → nothing survives? → fall back to the static chain
```

"It's #1 but we can't serve it" must never mean "route to nothing". It means "take #2".

### Tier 3 — Intent (dynamic, fully custom)

**Classify the request into the user's own labels → pick the model with the highest
score for that label, within the pool.**

- The user defines the labels **and** supplies the scores.
- Describes *your* traffic, not the public's.
- With `both` enabled, Arena fills any label the user has not scored.
- If a label has no scores of its own, the system offers a `scores_like` hint derived
  from the base intent — the user is never asked what `refactor_python` "looks like".

---

## 5. What gets measured — and its granularity

Three different measurements, not interchangeable:

| Board kind | Value | Range | Meaning | Comparable? |
|---|---|---|---|---|
| Text | Elo | ~1400–1600 | pairwise win rate vs that board's field | only within the board |
| Agent | net improvement | ~-0.05…+0.15 | beats the baseline agent by how much | only within the board |
| Percentile | 0–100 | rank / n | standing within that board | **yes, across boards** |

A `coding` Elo (1543) is not comparable to a `math` Elo (1525) — different opponent
fields, different scales. **Percentile is the only cross-board number**, and it compares
standing, not skill.

For routing this barely matters: a request maps to one intent, so one board is
consulted and its internal ordering is exact. It matters for the **matrix UI**, where
Elo across boards side by side would be a lie.

---

## 6. Model portfolios, releases, and dimensions

### 6.1 A model is not one thing

`deepseek-v4-flash` is a **base model**. `v4.1-ga` and `1017` are **releases** of it.
Each provider serves some release at some time. The registry already has the shape
(`mi_releases`, `mi_deployment_releases` with `effective_from`/`effective_to`), and the
data exists:

```
deepseek-v4-flash  (base portfolio)
  ├─ 0731     released 2026-07-31   retired upstream 2026-09-10
  ├─ v4.1-ga  released 2026-09-10
  └─ 1017     released 2026-10-17   ← current
```

**The portfolio is a container. The release is the thing that is served and scored.**

### 6.2 The dimensions a model carries

Routing must be able to distinguish models that share a name. Five independent
dimensions, all already present in the schema:

| Dimension | Where | Example values | Why it matters |
|---|---|---|---|
| **portfolio** | `mi_models.slug` | `deepseek-flash` | the identity everything joins on |
| **release** | `mi_releases.label` | `v4.1-ga`, `1017` | scores are per release (with the quant) |
| **variant** | `mi_deployments.variant` | `fast` | a provider's sped-up serving of the same release |
| **quantisation** | `mi_quants` + `mi_deployments.quant_id` | `exl3`, `exl3-eaqv1-2.9bpw`, `q4_k_m` | **a quant is its own score subject** |
| **effort** | `arena_board_rows.effort` | `low`, `medium`, `high`, `xhigh`, `max` | a dial, not a model — 301 arena rows carry it |

**Rules that follow:**

- **A score belongs to `(portfolio, release, quantisation)`.** Quantisation is a real
  score subject, not a footnote: `DeepSeek-V4.1-Flash-1017` at
  `exl3-eaqv1-2.9bpw` on the Sparks *is not* the same model as the unquantised API
  release, and it may well score differently. A variant (`fast`) is a serving mode of
  the same weights and does **not** get its own score.
- **A quant with no score of its own inherits the base score**, and the inheritance is
  *visible* — stored with the row that carries it, so the matrix can render
  `0.62 (inherited from base)` rather than implying a measurement nobody took. This is
  what keeps the system honest on a fresh install: a local build routes immediately
  using the base grade, and earns its own once something measures it.
- Where the catalogue stands today: 3 quants (`exl3`, `exl3-eaqv1-2.9bpw`, `q4_k_m`),
  3 deployments carrying one — a Spark build, a retired Spark build, and a llama.cpp
  GGUF. So this is not hypothetical; it is the current estate.
- **effort is a dial, not a model.** `claude-opus-5-5 (max)` and `(high)` are two rows on
  one board, not two models. The poll keeps the highest effort per model by default.

### 6.3 The identity problem, measured

Benchmark scores and LMArena rows are keyed to **different model names**, and the
overlap is small:

```
LiveBench keys : claude-opus-5, claude-fable-5, claude-sonnet-5, deepseek-v4-flash …
LMArena keys   : claude-opus-5-5, claude-fable-5-2, claude-sonnet-5-5, deepseek-flash …
shared         : 4 of 16
```

And almost every LiveBench-only key resolves to a **retired** portfolio:

```
claude-opus-5  → retired    gpt-5.6-sol      → retired
claude-sonnet-5 → retired   grok-4.5         → retired
deepseek-v4-flash → retired gemini-3.6-flash → retired
```

**Consequence:** routing is currently scored against models that are no longer served,
while the models that *are* served have LMArena rows but no local benchmark scores.
This is the biggest correctness problem in the system, and it is a data-join failure,
not a cosmetic one.

**It is fixed by the portfolio:** a benchmark key and an arena slug must resolve to the
same portfolio, differing only in **release**. `mi_model_names` is the table for that.

---

## 7. How Arena picks a board — superseded by the Q1 decision

**RETIRED 2026-11-17.** The operator decided the judge labels with **board names
directly** (§15 Q1), so there is no need to derive a board from a task. This section is
kept for the record: it documents an approach that was designed, measured, and dropped —
and the measurement is why it was dropped.

The approach was: reject a `task → board` configuration table (a shipped default is a
guess nobody measures; an editable one is a table the user must maintain before routing
works at all), and derive the board from agreement between two independent rankings.

```
request text
   │
   ├─► intent judge: top_scores(text, k)     → ranked labels + probabilities
   │
   ├─► which board judges this intent?       → derived by rank correlation
   │      for each board: compare the intent's score ordering against
   │      the board's ranking ordering, over the models they share
   │      highest correlation wins
   │
   └─► walk that board's ranking (pool-filtered) → highest servable model
```

### Bootstrapping it without traffic — what this section means, and why it does not work yet

**The problem it is trying to solve.** Arena routing needs to answer: *given intent
X, which board judges it?* Two ways to get that answer:

- **from traffic** — record which board we used and whether the request succeeded, then
  count how often each board's pick actually worked. Slow: needs real requests first.
  This is what "without traffic" refers to — the aim was to have *something* before
  any requests have been routed.
- **without traffic** — compare two rankings that already exist on disk. Our intent
  scores put the models in some order; a board puts them in some order. If both order
  them the same way, that board plausibly measures that intent. No requests needed.

**Measured, and it does not discriminate.** Tested live against the only models scored
by both sources (4 of them: `deepseek-v4-pro`, `kimi-k3`, `qwen3.8-max`, `minimax-m3`):

```
code_generation → coding       ρ = +0.80
code_generation → german       ρ = +0.80      ← same score, unrelated board
code_generation → japanese     ρ = +0.80
code_generation → instruction_following  ρ = +0.80
code_generation → math         ρ = +0.40
```

Every board lands on the same handful of values, because **4 models cannot produce a
meaningful rank correlation** — there are only 24 possible orderings, and the ordering
of four models is nearly the same on every board. The method cannot tell `coding` from
`japanese`, so it cannot answer the question it exists to answer.

Two things could rescue it, and neither is guaranteed:

1. **More overlapping models.** Joining the identities (§6.3) takes the overlap from 4
   to ~16, which is enough to discriminate. This is the strongest argument for fixing
   identity first — but it is untested until identity is fixed.
2. **A different question.** Correlating LiveBench with LMArena asks "do these two
   benchmarks agree?" — which is interesting, but not the same as "which board judges
   this intent".

**The earlier claim in this document was also wrong in a worse way.** A first attempt
appeared to confirm `reasoning_chain → math` at ρ=1.00. That was **circular**: those
intent scores came *from* the `math` board, so the correlation compared LMArena with
itself. Any high correlation between an intent and the board it was imported from is a
tautology, not evidence.

### What to do instead — the simpler answer

Since the intent model can score against **any label space** (§8), the label space for
Arena routing can simply **be the board names**. Then:

- the judge assigns a **board** directly, as its most probable label
- no derivation, no correlation, no traffic, no table

That is strictly simpler than §7's derivation and removes the bootstrap problem
entirely. The only cost: the classifier needs exemplars per board, which is a one-time
authoring task (and every board the operator does not care about can be left out).

**This is the recommendation.** Recorded here as the alternative to §7, pending the
operator's read of §7.

### Learning from outcomes — later

Agreement from outcomes is the second, slower signal, and per the operator it is kept
but not used yet:

> for a given intent, how often did the chosen board's pick actually succeed

It needs `arena_board` and a per-decision success link, which do not exist yet (§8).
Unlike the correlation, this one is sound — it measures what actually happened, on our
own traffic, which is exactly what the operator asked to collect and train on later.

---

## 8. What gets recorded per request

Recorded whether or not routing uses it, because the corpus is wanted for revision and
for training the next intent models.

| Field | Example | Status |
|---|---|---|
| `intent_text` | the user message | exists |
| `base_scores` | `[{code_generation:0.62},{unit_tests:0.21},…]` — **step 1** output, the full ranked vector | fits `semantic_json` (empty on 100% of rows today) |
| `base_intent` | `code_generation` — **step 2** output, independent of any taxonomy | **to add** |
| `base_margin` | `0.41` — gap between #1 and #2, i.e. how close step 2 was | same column |
| `taxonomy_id` / `taxonomy_version` | which taxonomy was in force | exists |
| `our_taxonomy_label` | `refactor_python` | **to add** |
| `arena_board` | `coding` — where it landed in LMArena's taxonomy | **to add** |
| `arena_rank_used` | `2` — we picked #2 on that board | **to add** |
| `quant_id` | `exl3-eaqv1-2.9bpw` — which build served it | **to add** |

`quant_id` is here because a score belongs to `(portfolio, release, quantisation)`:
without recording which build actually served the request, the outcome corpus cannot be
attributed to the thing it is supposed to describe.

`top_scores(text, k)` already returns the ranked vector, so the classifier side is
buildable today.

### Why record the full vector and the margin

- **`base_intent`** is the classification *independent of taxonomy* — the stable thing
  that survives a taxonomy change, and the label a future model would be trained against.
- **`base_scores`** lets a decision be re-derived later without replaying the model.
- **`base_margin`** is confidence. A narrow margin means the judge was unsure — those
  messages are candidates for a new label, and the corpus for the next training round.

That is the loop: the system proposes labels from its own uncertainty instead of asking
the user to invent them.

### Two things that have been conflated

- **how precise the classification is** — a property of the classifier, recorded on
  every request regardless of taxonomy
- **where it should end up** — a property of the taxonomy in force

Both recorded; only the second changes when the taxonomy changes.

### The two stages, explicitly

The intent model is a **two-step process**, and it is worth being precise about which
step is which — the current code does both in one call, which is why the second step
was invisible in the first draft of this document.

**Step 1 — score.** The intent model scores the message against **every label in the
label space**, producing a ranked probability per label (`top_scores(text, k)`). This
step does not decide anything; it is a measurement across the whole space.

**Step 2 — assign.** From those scores, the **most appropriate intent is assigned** —
the winner above the gate (`classify()` is exactly `top_scores(text, 1)` plus the
`min_score` gate). This is a decision, and it is the only thing routing consumes.

The distinction matters because the two steps fail differently:

| Step | Fails when | Symptom |
|---|---|---|
| score | the embedder is unavailable, or the label space has no exemplars | no scores at all — falls back to keyword heuristics |
| assign | the top score is below the gate, or two labels are close | scores exist but no intent is assigned |

Recording them separately is what makes the corpus useful: **`base_scores` is step 1's
output, `base_intent` is step 2's output, `base_margin` is how close step 2 was.**
A request with scores but no assignment, or an assignment with a narrow margin, is the
traffic that tells you the label space is wrong — and that is the revision data you
wanted to keep.

`scores_like` follows from this: it is a **cold-start hint the system offers**, never a
mapping the user maintains. When a suggested label is accepted, the base intent from
step 2 already says roughly which existing scores apply to it.

**One consequence for §7 worth flagging.** Because step 1 scores against *whatever label
space it is given*, the label space can be the LMArena board names instead of our 8 task
names. If so, step 2 assigns a **board** directly and no derivation is needed at all —
which would make §7 simpler, not more complex. That is a question for you when you read
§7, not a change to it: both routes can coexist (board labels for Arena routing, task
labels for Intent routing), and which one is authoritative is the open decision.

---

## 9. Registration and the portfolio binding

**Provider registration does not change.** It is already the right shape:

- `mi_endpoints` — a provider (base URL, status, poll enabled)
- `mi_deployments` — one row per **`model:provider` pair** the provider serves
  (112 rows: 23 active, 77 new, 12 retired)

What was missing is the **step after registration**: binding a pair to a *portfolio* and
declaring which *release* it serves. That work already has a queue:

```
mi_deployments.model_id IS NULL   →  73 pairs, all status='new'
mi_identity_queue                 →  431 rows, mostly 'pending'
```

### Column 3 of the matrix resolves it

When the detail panel opens a model with no portfolio:

```
Qwen/Qwen3.8-Max-0902                     commandcode
──────────────────────────────────────────────────────
Not in a portfolio yet.

○ Map to existing portfolio
    [ Qwen3.8-Max ▾ ]   release [ 0902 ▾ ]
    (releases from that portfolio's history; "new release" adds one)

○ Create a new portfolio
    slug [ qwen3.8-max-0902 ]  display [ … ]
    family [ qwen ]  release [ 0902 ]  date [ 2026-09-02 ]

                       [ Leave unmapped ]  [ Save ]
```

Three explicit outcomes:

1. **Map to an existing portfolio + release** — the common case
2. **Create a new portfolio** — genuine new models; the escape hatch that stops the
   registry being a closed set
3. **Leave unmapped** — usable in a static chain by raw `served_id`, but **cannot be
   scored, cannot appear in the matrix, cannot be chosen by Arena/Intent routing**

**The rule that keeps this honest: a portfolio owns releases; a `model:provider` pair
points at a portfolio *and* a release.** That is what makes "we asked for 1017 but this
provider still serves v4.1-ga" expressible.

---

## 10. Pages

### Navbar

```
Overview · Profiles · Routing definitions · Models · Activity · Conversations · Usage · Alerts · Settings
```

### `Routing definitions` — three tabs

**1. Capability matrix** (the intent ranking)

- **column 2** — the matrix: rows = models **by release**, columns = intents, cell =
  score + source + "measured on release X". Pick a **second portfolio to compare** (all
  portfolios estate-wide) and show a **per-cell diff**.
- **column 3 (shared detail panel)** — the selected model's metadata: name, benchmark
  key, quantisation, registered providers, and its **release history** (`from → to`, and
  what traffic went to each release).
- **import** — CSV: `model, task, score, source, benchmark_category, release`.

**2. Arena boards**

The 36 boards, `In use` / `Not used`, `N of ours` / `N stored`, publish date, the poll,
and each board's **derived correlation per intent** — which is what Arena routing
consults. No task→board table.

**3. Unmapped** — the resolution queue of §9

### `Models` — leaderboard only

Rank, model, lab, Elo/net-improvement, percentile, date. **No editor, no poll.** The poll
lives in Routing definitions → Arena boards, because polling is a routing input.

### Per profile → `Routing` tab

1. **Routing type** — `Static` · `Arena` · `Intent` (global default, per-profile override)
2. **Pool** — inherit the global chain, or customise it
3. knobs for the chosen type only:
   - Static → nothing (the pool order is the answer)
   - Arena → percentile floor, `both` on/off
   - Intent → score floor, `both` on/off, which taxonomy
4. **Rules** (`prefer` / `block`) — apply to all three, filtered by the chosen type's
   vocabulary

### Naming

| Full name | UI | Subtitle |
|---|---|---|
| Static routing (global) | **Static** | "Chain order. No measurement." |
| Static routing (per-profile) | **Static · custom** | "Overrides the global chain." |
| LMArena dynamic routing | **Arena** | "Intent → board → highest-ranked servable model." |
| User-defined dynamic routing | **Intent** | "Your labels → your highest-scoring model." |
| Both | **Intent · Arena-assisted** | "Your labels, Arena fills the gaps." |

Three words: **Static · Arena · Intent.** Long names live in docs and tooltips.

---

## 11. Two sources for one classification

| Setting | Labels come from | Scores come from |
|---|---|---|
| `intent` | the user's labels | the user's matrix |
| `arena` | LMArena board names | LMArena rankings |
| `both` | the user's labels | **user matrix first, Arena for anything unscored** |

`both` is the useful middle: the user's labels describe their traffic, Arena fills the
gaps. That is how a new taxonomy becomes routable on day one without scoring 15 models ×
8 labels by hand.

---

## 12. "Available" — precisely

A model may serve a request when all hold:

1. `mi_deployments.status = 'active'` — a provider confirms it serves it
2. `mi_endpoints.status = 'active'` — the provider is not retired
3. breaker not tripped (`provider_health.status != 'dead'`)
4. **in the profile's pool** (the static chain)

Item 3 is real today: `commandcode` is `dead` with **3,789 consecutive failures** on one
profile. A walk that ignored breaker state would pick a dead provider every time.

---

## 13. Build order

Revised 2026-11-17 per the operator's Q1/Q2 decisions: the rank-correlation derivation is
gone (the judge speaks board names directly), and portfolio creation joined the identity
queue.

| # | Step | Touches request path? |
|---|---|---|
| 1 | Merge the DeepSeek portfolio into `deepseek-flash`, with releases | no |
| 2 | Fix model identity so benchmark keys and arena slugs join on portfolio | no |
| 3 | `Routing definitions` page; move prod's matrix editor into tab 1 | no |
| 4 | CSV import into tab 1 | no |
| 5 | Release-aware matrix (one row per release × quant), with inherited-quant marking | no |
| 6 | Unmapped resolution in column 3: map / create portfolio / leave unmapped | no |
| 7 | **Board exemplars** — one exemplar set per board, so the judge can label with boards | no | **DONE 2026-11-17** |
| 8 | Arena routing type = the same classifier with the board label space | no | **DONE 2026-11-17** |
| 9 | Routing type selector on the profile Routing tab (+ label-space validation) | no | **selector DONE; label-space validation still open** |
| 10 | Record `base_intent` / `base_scores` / `base_margin` / `arena_board` / `quant_id` | additive | **DONE for `base_intent`/`arena_board`/`base_scores` (alembic 041); `quant_id` derivable via `to_deployment_id`** |
| 11 | Wire Arena as a score source in `get_model_score()` | **yes, additive** | **DONE 2026-11-17** |
| 12 | Read-only view over `routing_decisions.semantic_json` (data already stored) | no |
| 13 | Delete `Manage taxonomy` from Models and the custom taxonomies | no |

Steps 1–10 change no routing behaviour. Step 11 is the only request-path change.

**What left this table:** "derived correlation per intent" (old step 7). With the judge
speaking board names there is nothing to derive — it is replaced by the much smaller
step 7 above, authoring exemplars.

---

## 14. Where this went wrong before

Recorded so the mistake is not repeated: an earlier build put a "Manage taxonomy" editor
on the Models page. It managed `taxonomy_map` — which board feeds which **leaderboard
tab** — a display concern with **zero** connection to routing:

```
grep taxonomy_map|model_ratings|arena_boards|rating_taxonomies src/api/router.py
  → 0 hits, all four
```

Three vocabularies were conflated:

| Vocabulary | Lives in | Who reads it | Editor today |
|---|---|---|---|
| Routing intents | `model_capabilities.task_type` | `router.get_model_score()` | **prod's Models page** (`/api/models/capability*`) |
| Intent taxonomies | `intent_taxonomies` + `intent_taxonomy.py` | `router.classify_task_detail()` | none |
| Board → tab | `taxonomy_map` | the leaderboard only | **to be deleted** |

The routing vocabulary was already there, already had an editor, and was never opened.

---

## 15. Decisions and remaining questions

All three are now decided by the operator (2026-11-17). What follows is what each
decision means concretely, and what is left to build.

### Q1 — DECIDED: two label spaces, one engine

**Board names for Arena routing; task names for Intent routing.**

The judge is the same component (`SemanticClassifier`); only the label space it is
handed differs. Nothing translates between the two vocabularies, because nothing needs
to: a message routed by Arena is labelled with a board, a message routed by Intent is
labelled with the operator's taxonomy.

Consequences:

- §7's derivation (rank correlation, task → board) is **deleted from the build order**.
  It was measured as non-discriminating (§7), and this decision makes it unnecessary
  rather than merely unreliable.
- Each routing type declares its label space, and the label space is validated when the
  routing type is chosen — a profile cannot be in Arena mode with only the 8 task names
  loaded.
- Building Arena routing requires **exemplars per board**. That is the one new input
  this decision creates.

### Q2 — DECIDED: auto-map on registration, create when it fails

**Yes, it works — and more of it already exists than the question assumed.**

Registration stays simple. The behaviour the operator described splits into three cases,
and the first is already implemented:

1. **Name maps to an existing portfolio → do it silently.** This is live today.
   `model_intel_poll` resolves every newly observed served id through
   `resolve_canonical()`, which matches on slug, benchmark key, declared alias and
   served id. A match is staged already-linked. Explicitly **no fuzzy or prefix
   guessing** — a wrong link fabricates capability data, which is worse than a missing
   one.
2. **Name maps to nothing → it lands in the identity queue** as an observation. The
   queue is operator-facing (`GET /api/model-intel/settings` returns
   `pending_identity`; accept/reject via `POST /api/model-intel/identity/{accept,reject}`).
3. **Operator decides it is a new portfolio → create it here.** *This is the gap.*
   `accept()` today raises `KeyError("no canonical model …")` if the target slug does not
   exist — it can only alias to a portfolio that already exists. Creating one is
   possible (`model_names.upsert_entry()`), but only from the registry page, not from
   the point where the operator is looking at the problem.

**What to build:** add "create a new portfolio from this name" to the identity queue's
accept path, so the three options — *map to existing* / *create new* / *leave unmapped* —
appear together on one screen. That is the same three-way choice already agreed for
the matrix's third column, so the two surfaces should share it.

The pool rule stands: a pair with no portfolio may sit in a pool but can never be
chosen by a score. Only the static chain can reach it by name.

### Q3 — ANSWERED: two different surfaces, and only one needs work

The operator asked whether this means the intent classification log. **Partly — and the
measurement splits the answer in two.**

**The log already does it.** `routing_decisions.semantic_json` holds the full ranked
vector per decision. Live counts:

```
routing_decisions        28,668 rows
  with the score vector  25,308   ← 88%
  sem_available = 0       3,128   ← keyword fallback, no vector (correct)
  sem_available = null      232   ← pre-column rows
```

Example stored value:

```
[["casual_chat",0.7484],["code_generation",0.6966],
 ["agentic_multi_step",0.5858],["planning",0.5764],["research_deep",0.5721]]
```

So **label plus evidence is already the behaviour of the log.** Nothing to build. What
is missing is only that no UI renders it — the API returns it, the page does not.

**The suggestion panel is the part that does not exist.** `scores_like` appears
nowhere in the codebase — it is a concept in this document only. When it is built, it
shows label plus evidence, exactly as decided.

So Q3 resolves as: *the log is already correct, keep it; the authoring hint inherits the
same format; the only build item is a view over data that already exists.*

---

## 16. Gap analysis and edge cases — measured 2026-11-17

Written at the operator's request before any build starts. Every number below was
probed against the live staging database, not recalled.

### 16.1 Where the work stands

| Layer | State |
|---|---|
| Leaderboard (36 boards, 438 rows) | **built, live on staging** (:8735, alembic 040) |
| **Arena routing tier** (board label space, walk, hard rules, score source) | **built + verified live on staging, alembic 041** — §17 |
| Persist-then-map taxonomy mapping (14 seeds) | **built, live on staging** |
| Registration fixes (active-only, release normalisation) | **built, live on staging** |
| Design doc (this document) | **approved** — all 3 questions decided |
| Routing types, Arena routing, board exemplars, recording columns | **DONE** (§17); the `Routing definitions` AUTHORING page (steps 3–6, 12) is not started |
| Prod (:8734) | **alembic 029** — none of the above is visible in prod |

The prod gap is deliberate (no deploy GO), but it means the entire leaderboard feature
exists only on staging until steps 1–2 of §13 ship and prod migrates 029 → 040.

### 16.2 Gaps nothing in the design has fitted yet

1. **Board exemplars do not exist and have no home.** The Q1 decision made exemplars
   the critical path for Arena routing — the judge cannot label with board names
   without them — and nothing in the plan says who authors them, where they live, or
   what makes a set acceptable. This is the largest unbuilt piece of the design.
2. **The model-key join is still broken.** LiveBench scores key to retired releases
   (`claude-opus-5` vs `claude-opus-5-5`); overlap with arena slugs is 4 of 16. The
   matrix compare and any independent-source scoring inherit this. It is §13 step 2
   and must land before step 5's matrix means anything across sources.
3. **The DeepSeek merge (§13 step 1) has not started**, and steps 4–6 of the build
   order sit on top of it.
4. **The identity queue has no UI.** Q2's create-new-portfolio option lands on an
   API-only surface (`/api/model-intel/settings` returns `pending_identity`; no
   template renders it). Building the decision without building the screen leaves the
   operator unable to reach it.
5. **The `custom:` rows problem.** 3 hand-entered rows are not derivable and are read
   by nothing; deletion is decided, but `qwen3.8-27b-heretic` has **0** arena-board
   rows — deleting its 3 custom rows leaves it with no scores at all. It must be
   re-entered through whatever replaces custom entry (a matrix cell), not silently
   dropped.

### 16.3 Edge cases — each one probed, not assumed

**E1 — The 36 boards are not 36 label spaces.** The set includes meta boards
(`overall`, `exclude_ties`, `longer_query`, `non_english` — artefacts of the poll
methodology, not intents) and 9 language boards (`english` … `spanish`). As a label
space, a language board means "route by the language of the message", which is a
different product decision nobody has made. **Required:** the Arena label space is a
curated subset of boards (roughly 20), declared per install, not "all boards the poll
found".

**E2 — The effort dimension makes "walk the board" ambiguous.** The `coding` board
carries four effort groups (`''`×4, `max`×3, `xhigh`×2, `high`×2). Walking "the"
ranking requires an effort policy. **Required:** default = the highest effort the
deployment supports, else the unlabelled rows; the policy must be explicit in the
walker, not accidental in the sort.

**E3 — Ties.** No ties on `coding` today, but the walk needs a deterministic tiebreak
(value desc, then slug) so routing never flips on equal Elos.

**E4 — "No servable model on the board" cannot happen today — but must be coded.**
Measured: every board has ≥1 servable model (11 of 11 on text boards, 18 of 18 on
agent boards). Today. The estate changes; the walker needs a defined fallback to the
static chain when a board's slice of the pool is empty, not an exception.

**E5 — Arena routing can only pick the best of what we serve, and sometimes that is
rank 9.** Measured worst cases: `multi_turn` our best rank = 9, `french` = 9,
`polish` = 7. That is correct behaviour — best *available* — but the UI must show the
absolute rank alongside the pick, or "Arena picked rank 9" reads as a bug when it is
the honest answer.

**E6 — Conversation stickiness is undecided and it matters.** Measured:
conversations run 300–800 messages, contain up to **6 distinct tasks**, yet almost
always 1 model. Per-message Arena decisions could bounce a long conversation between
models as the text drifts between boards. **Open question for the operator:** does
Arena routing re-decide per message, or per conversation? Recommendation: re-decide
per message with hysteresis — switch only when the new pick beats the incumbent by a
configured margin. Hysteresis keeps stability without freezing a conversation onto a
wrong model.

**E7 — Keyword fallback cannot speak board names.** 3,128 of 28,668 decisions were
classified by keyword fallback (`sem_available=0`), which only knows task names. If
Arena mode silently fell back to keywords, it would guess a board mapping — exactly
the fabrication this design forbids. **Required:** in Arena mode, embedder
unavailable → static chain, never keyword→board.

**E8 — Quant inheritance has an end.** One active quant deployment exists
(`DeepSeek-V4.1-Flash-1017 @ exl3-eaqv1-2.9bpw`); it routes on its base grade. But if
a new release has neither a quant score nor a base score, inheritance has nothing to
inherit. **Required:** the chain terminates in the static chain, visibly marked
"unscored", not a zero.

**E9 — A re-poll can reorder a board mid-conversation.** With per-message decisions
this is harmless (each decision is recorded with the ranking it used). With
stickiness (E6), the conversation must keep its incumbent until the hysteresis
threshold says otherwise — a poll must never re-rank a live conversation.

**E10 — The prod migration is the riskiest single step left.** Alembic 040 on prod
needs the same WAL-safe backup ritual staging got
(`integrity_check ok` pre-040), and prod's Models page is the capability-matrix
editor users see today — the §13 step 3 swap must move, not replace, it.

### 16.4 What this changes in the build order

Nothing moves. Three things are added to the steps they belong to:

- step 7 (board exemplars) gains: curated board subset, authoring surface, and a
  defined "acceptable set" check;
- step 8 (Arena routing type) gains: E2 effort policy, E3 tiebreak, E4 fallback,
  E7 keyword guard, E6/E9 hysteresis;
- step 5 (matrix) gains: E8 visible "unscored" terminal state.

---

## 17. Implementation status — 2026-11-17

Built test-first, deployed to staging, and verified against the live staging estate.
Prod is untouched (alembic 029, container up 8 h, restarts 0).

### 17.1 What shipped

| Piece | File | What it does |
|---|---|---|
| The label space | `src/api/board_labels.py` | 23 curated boards; the 4 meta and 9 language boards are excluded (§16 E1) |
| Board exemplars | `src/api/board_exemplars.py` | 4 authored exemplars per curated board — the judge cannot assign a label it has no text for |
| The engine | `src/api/arena_routing.py` | `curated_boards`, `pick_board`, `walk`, `score`, `effort_group` |
| The third tier | `src/api/router.py` `_select_step_arena` | breaker gate → per-message classification → hard rules → board walk → build the chain; every fallback records why |
| The vocabulary | `src/api/cost_cache.py`, `src/server/endpoints.py` | `routing_route_by` = static \| intent \| arena; `rules` stays the legacy spelling of `static` |
| The selector | `sec_prouting.html` | three tiers by name: Static / Intent / Arena |
| The recording | `alembic/versions/041_…`, `models.py`, `router.py` | `base_intent` + `arena_board`, named in the constructor AND projected in `recent_decisions()` |
| The score source | `router.get_model_score` → `_arena_score_for` | a board answers when the matrix has nothing for that task; never a fabricated value |

### 17.2 Verified live on staging, not asserted

Real config, real database, real embedder (`bge-small-en-v1.5`), profile `l2`, pool of
four `model:provider` pairs:

```
"refactor this python function …"   → board coding                            → glm-5.3-flash (rank 16)
"explain the tradeoffs between …"   → board expert                            → glm-5.3-flash (rank 20)
"summarise this incident postmortem…" → board industry_software_and_it_services → glm-5.3-flash (rank 18)

hard rule: block glm-5.3-flash → the walk moved on to deepseek-v4-pro (rank 72)
```

Each row persisted with `path='arena'`, `arena_board`, `base_intent`, and a note naming
the board and the rank used. Three messages, three boards, one conversation — the
per-message decision the operator asked for.

### 17.3 The defect the deploy found (and the test that missed it)

The first version of migration 041 executed raw SQL with a plain string, because that is
what a `sqlite3.Connection` needs — and that is what my test passed. Alembic runs
migrations at container startup with a **SQLAlchemy** Connection, which raises
`ObjectNotExecutableError` on a plain string. Staging went into a crash loop on the
first deploy attempt. Nothing was corrupted and the rollback was a rebuild, but the
lesson is recorded: **a migration must be tested through the connection type it actually
runs on.** `_execute()` now dispatches on the connection, and
`test_migration_041_works_through_an_alembic_style_connection` drives the SQLAlchemy
path so it cannot come back.

### 17.4 A tag was contaminated, and repaired

While deploying, a `docker compose build` run from the staging directory used that
directory's prod-shaped `docker-compose.yml`, and rebuilt the image tag
`simple-lcp-lcp:latest` from **staging** code. The subsequent `up -d` was refused by the
container-name conflict (`lcp` already in use), so the prod container never restarted —
verified after the fact: `started=08:39:37Z restarts=0`, health 200. The tag itself was
repaired by rebuilding from the prod repo and re-tagging, then verified by inspecting
the image: neither prod tag contains `src/api/arena_routing.py`.

**Standing hazard, worth a decision:** the staging repo carries a prod-shaped
`docker-compose.yml` beside its `docker-compose.staging.yml`, so a bare
`docker compose up -d` in that directory targets prod. Every staging deploy from now on
must pass `-f docker-compose.staging.yml`.

### 17.5 Not built (deliberately listed, not implied)

- The `Routing definitions` authoring page and its tabs (steps 3–6): the matrix editor,
  CSV import, the release-aware matrix, the three-way unmapped resolution.
- Label-space validation on the tier selector (§15 Q1's last consequence): selecting
  Arena with no boards stored is currently a silent fallback to the static chain rather
  than a refusal.
- The read-only view over `routing_decisions.semantic_json` (step 12).
- `quant_id` on decisions: derivable through `to_deployment_id` today, so it was not
  duplicated into a column.
- Deleting the `custom:` taxonomies (step 13) — it would leave `qwen3.8-27b-heretic` with
  no scores at all (§16.2 gap 5), so it waits for the matrix cell that replaces them.

### 17.6 Rollback

- **Code:** staging is at `c2e9d3b`; the previous staging build was `f499b59`.
- **Schema:** 041 adds two nullable columns. Reverting the code leaves them in place
  and harmless; `alembic downgrade 040` drops them.
- **Database:** full pre-migration dump at
  `/your/data/app/lcp-staging/db-backups/costs.db.pre-041-20261117T163704Z`
  (1.02 GB, `integrity_check ok`, alembic 040, 28,668 decisions).
