# LCP Routing — Design Doc

## Metadata

- **Author:** aunttwister (maintained with homelab-expert L2)
- **Status:** design for review, 2026-11-16. Nothing in §4 onwards is built.
  Supersedes the earlier draft that proposed a `task → board` configuration table;
  that table is gone (§7), because the board is derived from agreement between
  independent rankings rather than configured.
- **Created:** 2026-11-16
- **URL:** `docs/routing-design.md` in github.com/aunttwister/lcp
- **Mirrored:** LeafWiki `okf-homelab-documentation/local-ai/routing-design`

> **Read §1–3 first.** Most of the confusion in this area has been several different
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
| **release** | `mi_releases.label` | `v4.1-ga`, `1017` | scores are per release |
| **variant** | `mi_deployments.variant` | `fast` | a provider's sped-up serving of the same release |
| **quantisation** | `mi_deployments.quantisation` | `exl3`, `exl3-eaqv1-2.9bpw`, `q4_k_m` | a local build is not the same model |
| **effort** | `arena_board_rows.effort` | `low`, `medium`, `high`, `xhigh`, `max` | a dial, not a model — 301 arena rows carry it |

**Rules that follow:**

- a score belongs to `(portfolio, release)` — not to a variant, not to a quantisation
- a **quantised local build is a different serving of the same release** and must be
  distinguishable in the pool, because it may score differently
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

## 7. How Arena picks a board — derived, not configured

A `task → board` configuration table is rejected: a shipped default is a guess nobody
measures, and an editable one is a table the user must maintain before routing works at
all.

**Instead the board is derived from agreement between two independent rankings.**

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

### Bootstrapping it without traffic

The correlation is computable **today**, from data on disk — **once the identities
join** (§6.3).

A first attempt appeared to confirm `reasoning_chain → math` at ρ=1.00. **That was
circular**: those intent scores came *from* the `math` board, so the correlation
compared LMArena with itself.

The honest version correlates a **genuinely independent** benchmark (LiveBench) against
LMArena boards. That currently returns nothing, because only 4 model keys overlap.

**So the bootstrap order is forced:** portfolio identity → then correlation → then Arena
routing has a defensible board assignment with zero traffic.

### Learning from outcomes — later

Agreement from outcomes is the second, slower signal, and per the operator it is kept
but not used yet:

> for a given intent, how often did the chosen board's pick actually succeed

It needs `arena_board` and a per-decision success link, which do not exist yet (§8).

---

## 8. What gets recorded per request

Recorded whether or not routing uses it, because the corpus is wanted for revision and
for training the next intent models.

| Field | Example | Status |
|---|---|---|
| `intent_text` | the user message | exists |
| `base_intent` | `code_generation` — independent of any taxonomy | **to add** |
| `base_scores` | `[{code_generation:0.62},{unit_tests:0.21},…]` — full ranked vector | fits `semantic_json` (empty on 100% of rows today) |
| `base_margin` | `0.41` — gap between #1 and #2 | same column |
| `taxonomy_id` / `taxonomy_version` | which taxonomy was in force | exists |
| `our_taxonomy_label` | `refactor_python` | **to add** |
| `arena_board` | `coding` — where it landed in LMArena's taxonomy | **to add** |
| `arena_rank_used` | `2` — we picked #2 on that board | **to add** |

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

**Stage 1 — precision.** Score the message against every label; a probability per label.

**Stage 2 — confidence.** How settled is the winner? Is the gap between #1 and #2 wide,
or a coin toss?

Recorded as `base_scores` (the whole vector) and `base_margin` (the gap). The margin is
the useful half: a wide margin is a settled request, a narrow one is where the taxonomy
is missing a distinction. Cluster the narrow-margin messages and they become the
suggested exemplars for a new label — so the system proposes intents from its own
uncertainty rather than asking the user to invent them from nothing.

`scores_like` follows from this: it is a **cold-start hint the system offers**, never a
mapping the user maintains. When a suggested label is accepted, the base intent already
says roughly which existing scores apply to it.

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

| # | Step | Touches request path? |
|---|---|---|
| 1 | Merge the DeepSeek portfolio into `deepseek-flash`, with releases | no |
| 2 | Fix model identity so benchmark keys and arena slugs join on portfolio | no |
| 3 | `Routing definitions` page; move prod's matrix editor into tab 1 | no |
| 4 | CSV import into tab 1 | no |
| 5 | Release-aware matrix (one row per release) + release-history panel | no |
| 6 | Unmapped resolution in column 3 | no |
| 7 | Arena boards tab + derived correlation per intent | no |
| 8 | Routing type selector on the profile Routing tab | no |
| 9 | Record `base_intent` / `base_scores` / `base_margin` | additive |
| 10 | Wire Arena as a score source in `get_model_score()` | **yes, additive** |
| 11 | Two-stage classification + suggestion loop | medium |
| 12 | Delete `Manage taxonomy` from Models and the custom taxonomies | no |

Steps 1–8 change no routing behaviour. Step 10 is the only request-path change.

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

## 15. Open questions

1. **Arena cold start.** With the derived-board approach there is no cold start *after*
   step 2 — correlation works from data on disk. Confirm that ordering is acceptable.
2. **Pool and volume.** 77 of 112 `model:provider` pairs are `new`/unmapped. May a
   profile's pool contain an unmapped pair (usable, unscored), or must a pair be
   portfolio-bound before it can be pooled?
3. **`scores_like` visibility.** When the system offers a hint, does it show the
   correlation it used, or just the label?
