# LCP Routing — Design Doc

## Metadata

- **Author:** aunttwister (maintained with homelab-expert L2)
- **Status:** design for review, 2026-11-16 (rev. 4). Nothing in §4 onwards is built.
  Supersedes the earlier draft that proposed a `task -> board` configuration table.
- **Created:** 2026-11-16 · **Revised:** 2026-11-17 — (1) a score is keyed
  `(portfolio, release, quantisation)`; (2) the intent model is described as its real
  two-step process (score, then assign); (3) §7's rank-correlation bootstrap measured
  and found non-discriminating; (4) §15 rewritten — each open question now states what
  it means, why it blocks, the options and the recommendation, with live measurement
  behind Q1 and Q2.
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

| # | Step | Touches request path? |
|---|---|---|
| 1 | Merge the DeepSeek portfolio into `deepseek-flash`, with releases | no |
| 2 | Fix model identity so benchmark keys and arena slugs join on portfolio | no |
| 3 | `Routing definitions` page; move prod's matrix editor into tab 1 | no |
| 4 | CSV import into tab 1 | no |
| 5 | Release-aware matrix (one row per release × quant) + release-history panel | no |
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

Each question below states what it means in plain terms, why it blocks work, the
options, and the recommendation. **Q1 blocks everything else; Q2 and Q3 can be decided
during the build.**

### Q1 — What is the label space for Arena routing? *(blocking)*

**What it means.** The intent judge assigns a label by scoring the message against a
fixed set of names. Which set?

- **our 8 task names** — `agentic_multi_step`, `code_generation`, `debugging`,
  `planning`, `reasoning_chain`, `research_deep`, `unit_tests`, `casual_chat`
- **the 36 LMArena board names** — `coding`, `math`, `multi_turn`,
  `agent_bash_reliability`, `creative_writing`, …

**Why it blocks.** §7's derivation exists *only* to translate between these two
vocabularies. If the judge speaks board names, the translation is unnecessary and §7
collapses to nothing. Every later step (routing type, score source, what gets recorded
per request) changes shape depending on the answer.

**Why board names are the stronger option — measured, not argued.** The 8 task names
literally cannot express the questions being asked of this system. Classified live:

```
"what is the best model for multi turn"
   planning        0.673   ← assigned
   research_deep   0.665
   code_generation 0.643
   unit_tests      0.624
   classify() -> planning
```

There is **no `multi_turn` label**, so a multi-turn question becomes "planning", by a
margin of 0.008. And `agent_bash_reliability` — one of the 36 boards — has no task name
that could ever represent it either. The task vocabulary is the *router's* vocabulary;
it was never designed to answer "which model is best at X".

**Options.**

- **A. Board names as the label space.** Judge assigns a board directly. Requires
  exemplars per board (one-time authoring). §7's derivation deleted. Intent routing
  still uses the 8 task names — the two label spaces coexist and are never mixed.
- **B. Task names as the label space.** Judge assigns one of 8 tasks; a separate step
  maps task → board. That mapping is either a config table (you rejected) or the
  derivation (measured as non-discriminating).
- **C. Both, per routing type.** Arena routing scores against boards; Intent routing
  scores against the operator's own taxonomy. Same engine, different label space.

**Recommendation: C.** It is A for Arena routing and B for Intent routing, which is
also exactly the two routing types already agreed. The classifier accepts its label
space as an argument, so this needs no new machinery. The cost is board exemplars, and
boards you do not care about are simply not authored.

### Q2 — May a profile's pool contain a model that has no portfolio?

**What it means.** A profile's **pool** is the set of models its routing may choose
from. Some provider pairs are registered but not tied to a portfolio, and therefore
have no scores. May such a pair sit in the pool?

**Where the estate stands** (measured):

```
mi_deployments          112 pairs
  active                 23   all portfolio-bound — these have scores
  retired                12   all portfolio-bound
  new                    77   ← 73 have NO portfolio at all
```

So the question is about those **73**. They are names a provider serves that your
registry has not yet decided what they are.

**Options.**

- **A. Allowed in the pool, but never picked by score.** It can only be reached if the
  operator names it explicitly in the static chain. The system never *chooses* it on a
  score it does not have. Honest and usable immediately.
- **B. Barred from the pool until portfolio-bound.** Cleaner data, but a model you are
  actively paying a provider for cannot be routed to at all until you classify it.

**Recommendation: A.** A model with no score can never win a scoring comparison anyway;
letting it sit in the pool costs nothing and prevents "I registered it, why can't I use
it". What must not happen is scoring inventing a number for it.

### Q3 — When the system suggests a label, what evidence does it show?

**What it means.** During taxonomy authoring the system proposes "this new label looks
like `casual_chat`". Does the panel show **just the label**, or **the label plus the
scores behind it**?

**Why it matters.** A suggestion with no evidence is either trusted blindly or ignored.
With evidence it is checkable — and when it is wrong, the numbers show you why.

**Options.**

- **A. Label only.** `scores like: casual_chat`.
- **B. Label plus the ranked vector.** `scores like: casual_chat 0.64, code_generation
  0.61, unit_tests 0.55` — the same `top_scores()` output the router uses.

**Recommendation: B.** The vector already exists (it is one call), it makes a wrong
suggestion debuggable, and the numbers are what make the suggestion credible. This is
cosmetic compared with Q1 and can be decided during the build.
