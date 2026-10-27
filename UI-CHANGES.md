# LCP UI change ledger

Every user-visible change made to the LCP console, one line each, with the commit
that carries it and how to undo it. Staging repo `simple-lcp`, branch `main`.
Prod (`/your/data/docker-apps/lcp`, `:8734`) carried none of this until
2026-10-26, when staging `main` was merged into it and cut over (see
"Wave 7 promotion" at the end of this file). Prod now serves `ui.js` and
`sparkline.js`, and both trees are at the same tree.

Baseline for "before all of this" is `78c9e23^` — the commit before the first UI
wave. Everything below sits on top of it; `git rev-list --count 78c9e23^..HEAD`
is the authoritative number, because this file's own commits count too.

## How to roll back

**One change.** `git revert --no-edit <sha>` reverts exactly that commit. Revert
newest-first: most of these append a block to the end of `dashboard.css`, and two
reverts that touch adjacent hunks can conflict. If one does, the table below names
the selector or element to delete by hand — every change is small enough to undo
with a single edit.

**One wave.** `git revert --no-edit <sha1> <sha2> ...` newest-first, or
`git revert --no-commit <oldest>^..<newest>` and resolve once.

**Everything.**
```
cd /your/data/docker-apps/simple-lcp
git reset --hard 78c9e23^          # nothing here is pushed anywhere
docker compose -f docker-compose.staging.yml --env-file host-paths.env -p lcp-staging up -d --build
```

**Restoring the decommissioned tabs** (see wave 4a) is the one case where a single
revert is not enough: `46cbd17` put includes back into templates that `f6772cd`
deleted. Revert them together, newest-first:
```
git revert --no-edit e2680cc 1d711ae d392204 f6772cd 46cbd17 d675134
```
Then delete the `.pd-scope-note` block if it survives the merge.

## Verifying after a revert

```
cd /your/data/docker-apps/simple-lcp
python3 -m pytest tests/test_m2_page_merge.py tests/test_m2c_profile_surface.py \
                 tests/test_m2d_profile_model.py tests/test_work_conversations.py \
                 tests/test_ui_tables.py -q          # expect 210 passed
docker compose -f docker-compose.staging.yml --env-file host-paths.env -p lcp-staging up -d --build
```
Then load `http://127.0.0.1:8735/profiles`, `/profiles/l2`, `/activity?tab=logs`
and check the specific element the table names.

---

## Wave 1 — `78c9e23` (2026-10-23)

| Commit | Change | Where |
|---|---|---|
| `78c9e23` | Sparklines on usage cards, radial gauges, terminal chrome around the log panel | `static/js/sparkline.js`, `dashboard.css`, `sec_usage.html` |

Revert: `git revert --no-edit 78c9e23` (also removes `sparkline.js`, which nothing
else uses).

## Wave 2 — accessibility and log tables

| Commit | Change | Where |
|---|---|---|
| `2b96d19` | Global `:focus-visible` ring, skip link + real `<main>`, sidebar `aria-expanded`, dialog focus trap and focus return, `aria-busy` + live region, `scope="col"`, sticky log headers, skeleton rows, copy gateway URL, unified dialog close ring | `ui.js`, `modal.js`, `sidebar.js`, `base.html`, `dashboard.css` |
| `9e9bd6c` | Copy button reads `COPY`/`COPIED` instead of the `⧉` glyph (which read as a magnifier) | `sec_profiles.html`, `dashboard.css` |
| `b43958d` | Log table renders its `<thead>` before the first fetch, so skeleton rows have column names | `sec_logs.html` |

Revert: `git revert --no-edit b43958d 9e9bd6c 2b96d19` (newest first).

## Wave 3 — `1452f7f` (2026-10-24): sortable, filterable, honest log

| Commit | Change | Where |
|---|---|---|
| `1452f7f` | Server-side column sorts (`cost`, `tokens`, `latency`) offered only when the server advertises the key; `aria-sort` on the header cell; term-bar label follows the sort; profile filter with options from the payload; filter chips with clear; `sr-only` captions on six tables; error panel with Retry; pager in-flight guard; `aria-current` on nav and profile tab; sticky profile tab strip; freshness stamp ticking every second; structured empty state with Clear filters; `_profiles()` no longer limited to conversation-linked rows | `work_conversations.py`, `logtable.js`, `sec_logs*.html`, `_sidebar.html`, `profile_detail.html`, `dashboard.css`, tests |

Revert: `git revert --no-edit 1452f7f`. Note: this also reverts the arrow-visibility
tweak (0.8em / 0.85 opacity) that made the sort arrows legible.

## Wave 4a — reorganise, and decommission agent infrastructure

The user's direction: this app routes traffic. Skills, memory and cron belong to a
Hermes agent and are managed in the Hermes dashboard.

| Commit | Change | Where |
|---|---|---|
| `46cbd17` | Profile page drops Skills / Memory / Cron; tabs become **Routing, Models, API Keys**; default tab `skills` → `routing`; a note under the strip says where that infrastructure lives | `pages.py`, `profile_detail.html`, `dashboard.css` |
| `f6772cd` | Deletes `sec_skills.html`, `sec_memory.html`, `sec_cron.html`, `sec_cron_script.html` | `sections/` |
| `d392204` | Tests retargeted at the three routing tabs; the two cron-tab tests removed with the tab | `test_m2_page_merge.py`, `test_m2c_profile_surface.py` |
| `58b2b61` | Nav order is the routing workflow: Profiles → Models → **Activity → Conversations** → Usage → Alerts | `_sidebar.html`, `test_m2_page_merge.py` |
| `d675134` | Profiles page describes routing subjects; says once, in muted text, that agent infrastructure lives elsewhere | `profiles.html` |
| `04c6fa0` | `lcp_only` profile test asserts the three routing tabs render real controls instead of explaining empty agent tabs | `test_m2d_profile_model.py` |
| `1d711ae` | Tab pills carry real counts: Routing = declared intents, Models = chain steps | `pages.py` |
| `e2680cc` | The Hermes-scope note moves from the bottom of the page (~1,200px below the tabs) to directly under the strip | `profile_detail.html` |

Revert the whole unit newest-first (see the note above). Reverting only
`46cbd17` or only `f6772cd` leaves broken includes.

## Wave 4b — twenty UI changes, one commit each

| # | Commit | Change | Undo anchor |
|---|---|---|---|
| 1 | `6b5f7b7` | **Sign out is a red button** — destructive palette, footer styles moved out of inline attributes | `.sb-signout`, `.sidebar-footer` in `dashboard.css`; markup in `_sidebar.html` |
| 2 | `2b8bb57` | Active nav row: ring-tinted wash + 2px inset accent bar + 600 weight, replacing the near-white pill | `.sidebar-nav a.active` |
| 3 | `e2ea33b` | Alert badge tokenised (was the last raw hex in the sidebar), tabular figures | `.sb-alert-badge` |
| 4 | `528f117` | `color-scheme: dark` — native scrollbars, select popups, date pickers, carets | `:root` |
| 5 | `ecbbd16` | Thin scrollbars on sidebar, log panel, wide tables, dialog bodies | `.sidebar`, `.logs-term`, `.work-scroll`, `.dialog-body` |
| 6 | `f16632f` | Selection highlight matches the focus ring | `::selection` |
| 7 | `f6c45ed` | Link underlines lifted 2px off descenders | `a`, `.main-content a` |
| 8 | `237c2c1` | Buttons dip 1px on press; disabled = 45% opacity + not-allowed cursor | `.btn-sm`, `button:disabled` |
| 9 | `1907f32` | Zebra stripe at 12% on even table rows | `.work-table tbody tr:nth-child(even)` |
| 10 | `e268f43` | Count line reserves its height (no 18px jump when data lands) | `#lg-count`, `.task-count` |
| 11 | `f592491` | Sort/filter controls lay out as one wrapping row, stacked under 640px | `.task-toolbar` |
| 12 | `5312ceb` | Tab hover background; tab counts become pills | `.tab-btn:hover`, `.tab-count` |
| 13 | `6cea338` | Profile card hover no longer *reduces* border alpha (it was making the edge fainter) — now brightens toward the ring | `.profile-card:hover` |
| 14 | `c5a8689` | Breadcrumb current crumb ellipsises at 22rem instead of wrapping | `.breadcrumbs .crumb-current` |
| 15 | `35aa2c4` | Dialog backdrop blurs (3px) at 72% black instead of flat 80% black | `.dialog-overlay`, `.modal-overlay` |
| 16 | `69cbb10` | Mobile drawer overlay blurs; **Escape closes the drawer** and returns focus to the toggle | `.sidebar-overlay`, `sidebar.js` |
| 17 | `8327389` | `scroll-margin-top: 4.5rem` so anchors are not hidden by the sticky tab strip | `[id]` |
| 18 | `be3f85b` | Smooth scrolling, only inside `prefers-reduced-motion: no-preference` | `html` |
| 19 | `72d66d2` | **Jump-to-page field** + "page N of M" in the count line (2,594 pages were unreachable) | `renderPager`/`renderCount` in `logtable.js`, `.task-pager-jump` |
| 20 | `6c6c335` | `scope="col"` on every remaining column header — 40 headers across 13 files | all templates + `*_script.html` header strings |

Revert any single one with `git revert --no-edit <sha>`. Reverting #19 also removes
the jump field's CSS; reverting #20 re-strips scope from every header.

## Measured, after wave 4

- Nav: `Profiles, Models, Activity, Conversations, Usage, Alerts`.
- Profile tabs: `Routing` (active, `aria-current="page"`), `Models 4` (chain steps),
  `API Keys`. Skills/Memory/Cron absent; note sits 17px under the strip.
- Sign out: text `rgb(244,124,124)`, fill `rgba(127,29,29,0.22)`, border
  `rgba(127,29,29,0.6)`, 207×37, anchored to the sidebar foot.
- Log: `Showing 1–20 of 51,861 requests · page 1 of 2,594 · newest first · refreshed 2s ago`;
  jump to 1200 → `page 1,200 of 2,594`, URL gains `page=1200`; out-of-range input
  snaps back without fetching.
- Zebra rows: `rgba(30,41,59,0.12)` on even rows; row hover still overrides.
- 9/9 tables on `/usage` fully scoped; 9/9 headers on the log scoped.
- Dialog overlay: `rgba(0,0,0,0.72)` + `blur(3px)`.
- Mobile 390px: drawer opens, overlay blurs, Escape closes and focuses
  `sidebarToggle`; no horizontal overflow.
- Zero JS exceptions across 13 surfaces.
- Tests: 210 passed in the five UI-facing files. The wider suite has 82
  pre-existing failures/errors (router, LanceDB memory backend) that reproduce at
  `1452f7f`; the only new one, `test_an_lcp_only_profiles_empty_tabs_say_why`,
  failed on a decommissioned tab name and was retargeted in `04c6fa0`.

## Wave 5 — the Config tab leaves, and its leftovers with it

`/profiles?tab=config` was the work-sources editor: it repointed the task-tree
root, the cron store and the op spool for every profile. That is Hermes agent
infrastructure, not routing — the same reason skills, memory and cron left — so it
went on 2026-10-25.

| Commit | Change | Where |
|---|---|---|
| `30fd6c2` | Removes the Config tab, its section and its script. The Profiles page has **no tabs at all** — a strip whose only entry is the page you are already on is noise. `_work_config_view` deleted with its only caller. Every retired `?tab=` value falls back to the directory. | `pages.py`, `profiles.html`, `sections/sec_config.html`, `sections/sec_config_script.html` |
| `134af86` | `/work/config` redirects to `/profiles` instead of the removed tab | `endpoints.py` |
| `c68fcfe` | Tests follow: config markers and its MERGED row removed; the strip test split into "Models must show its tabs" and "Profiles must NOT have a strip"; every retired tab value asserted to land on the directory | `test_m2_page_merge.py` |
| `25e0f7b` | Deletes `_work_cron_view` — dead since the Cron tab went, still building a cron snapshot, op list and per-profile filter that nothing rendered | `pages.py` |
| `0cce42a` | Comments cleared: the sidebar no longer promises cron jobs and work-layer paths on Profiles, or lists Setup/Tasks/Fleet as nav entries; the Agent Profile field says "the Hermes profile this route serves" rather than "whose skills, memory and tasks" | `_sidebar.html`, `sec_profiles.html` |
| `d43c7b1` | `/work/cron` redirects to `/profiles` directly — it was pointing at `?tab=cron`, a tab that no longer exists, which fell back to the directory anyway (two hops, and a docstring claiming a Cron tab was there) | `endpoints.py`, `test_m2_page_merge.py` |
| `f2f2d9d` | Three tests that pinned the old redirect targets and the Profiles tab strip now assert what ships: both legacy routes land on `/profiles`, `?profile=` is dropped rather than carried to a page that cannot honour it, and the Profiles page carries no `?tab=` links | `test_server.py`, `test_jinja_renderer.py` |

**Revert:** `git revert --no-edit f2f2d9d d43c7b1 0cce42a 25e0f7b c68fcfe 134af86 30fd6c2`
(newest first). Restoring the Config tab means restoring two deleted templates and
the `_work_config_view` builder, so revert the whole unit — `30fd6c2` alone leaves
`profiles.html` including files that do not exist.

**What stayed, deliberately:** `src/api/work_sources.py` and the `/api/work/sources`
API. It is the documented contract the host-side collector reads, and the paths stay
repointable without a UI. Its `PUT` is now UI-orphaned — see "Still open".

## Measured, after wave 5

- `/profiles`, `/profiles?tab=config`, `/profiles?tab=cron`, `/profiles?tab=skills`
  all render the directory: title `Profiles — LCP`, no `.page-tabs`, no
  `#sources-form`, 6 cards, first card at y=274.
- `/work/config` → `302 /profiles`; `/work/cron` → `302 /profiles`.
- `GET /api/work/sources` still answers with `configured: true` and the resolved paths.
- Zero JS exceptions across 12 surfaces.
- 256 tests pass in the five UI-facing files.

## Wave 6 — the dead work-layer backend

The UI lost Cron, Config and the task/fleet pages over three waves. What was left
was an HTTP surface with no caller, and two modules whose only purpose was to feed
it. Five routes:

| Route | Called | Wrote |
|---|---|---|
| `GET /api/work/cron` | the Cron tab (gone) | no |
| `GET /api/work/cron/ops` | the Cron tab (gone) | no |
| `POST /api/work/cron/ops` | the Cron tab (gone) | **yes** — spooled cron mutation intents for a host executor |
| `GET /api/work/sources` | the Config tab (gone) | no |
| `PUT /api/work/sources` | the Config tab (gone) | **yes** — repointed the task-tree root, cron store and op spool for every profile |

The two writes were reachable by anything holding the admin token, and nothing in
the app called them. That is not dead code, it is unused attack surface.

With the routes gone, the modules had no live root:

```
work_cron.tasks_dir <- _snapshot_path <- cron_view <- work_sources.resolved_view
                                                   <- profile_data.profile_paths  (no caller)
```

so both modules went, with `profile_paths`, `_tasks_root_for` (its only caller passed
it a `?profile=` from the cron page) and the `cron_snapshot` test fixture.

| Commit | Change |
|---|---|
| `f6f0f29` | Removes the five routes, their handler methods, the `do_PUT` dispatch branch for sources, and `_tasks_root_for` |
| `3cb41c3` | Deletes `src/api/work_cron.py` (346 lines) and `src/api/work_sources.py` (233 lines), `profile_data.profile_paths`, and the `cron_snapshot` fixture |
| `2e86e9d` | Deletes `tests/test_work_cron.py` and `tests/test_work_sources.py`; drops the seven `tasks_root_for` cases; the admin-auth route-table test now asserts **zero** table-level PUTs |
| `eb10a5e` | The slim-surface guard (C7) pins the five route names and both module names, so a later refactor cannot restore a cron write endpoint by renaming it |
| `ed9f22a` | QA harness regenerated: the cron+sources group deleted, three API rows and four sources cases removed, the op-validation matrix and its JSON-error case gone. 197 cases, zero references left |

**20 files, −1,630 lines, +31.**

**Revert:** `git revert --no-edit ed9f22a eb10a5e 2e86e9d 3cb41c3 f6f0f29` (newest
first). The two deleted test files and the two deleted modules come back with
`3cb41c3` and `2e86e9d`; the routes come back with `f6f0f29`. Reverting only part of
this leaves the endpoints pointing at modules that do not exist, so revert the unit.

**What stayed, and why:** `work-sources.json` on disk, and the host-side collector
and executor that read and write it through the shared ops mount. That was always the
design — LCP's reader was a convenience for a panel that no longer exists. Deleting the
file would break the host cron system; deleting LCP's reader does not.

**Verified:** all five routes return `404` (GET, and the POST/PUT writes); the two
survivors `/api/work/requests` and `/api/work/provider-decisions` return `200`;
`/work/cron` and `/work/config` still `302 → /profiles`; eleven UI surfaces render
with zero JS exceptions; the full suite's failure list is identical to the
`1452f7f` baseline (82 pre-existing failures in router and the LanceDB memory
backend, none new).

**Deliberately not touched:** `cases.workload.json` keeps its committed contents.
Running `gen_workload.py` produces 34 cases where the committed fragment has 52 —
that is filesystem drift in the generator's task-directory scan, not a consequence of
this change, and accepting an 18-case coverage drop to make a generator run cleanly
would be worse than leaving it.

## Wave 7 — QA made truthful, conversations audited, charts coloured by name

Three things at once, because they turned out to be one thing: the QA sweep was
84% red against routes that no longer exist, the Conversations page was presenting
"unknown" as "zero", and the charts drew the biggest spender in the same grey as a
profile that spent nothing.

### 7a. The QA sweep is generated from the live route table again

`0a70e59` — `test(qa): rebuild the sweep against the live route table`

The suite had drifted to 166 of 197 failing, and almost none of it was real: the
cases targeted pages and endpoints decommissioned in waves 4–6, and every console
route returned 302 because the runner never authenticated. A suite that is 84% red
teaches the operator to ignore it, which is worse than no suite.

Grounded in reality first: probed every registered GET route (`_build_routes()`),
43 of 54 exact paths answer 200 behind the admin cookie; the rest are deliberate
redirects or `cid required` 400s. Regenerated the cases from that probe.

- `runner.py` logs in once, writes the cookie to a throwaway `HOME/.curlrc`, runs
  each case with that HOME. Anonymous cases use `curl -q` (curl ignores the config
  file), so the auth boundary is still tested rather than assumed.
- Deleted the generators for surfaces that no longer exist: `gen_tasks`,
  `gen_workload`, `gen_misc`, `gen_extra`, `gen_lifecycle`, `gen_gateway`,
  `gen_cron`.
- 182 cases: api 35, auth 9, conv 26, logs 26, pages 35, proxy 11, resilience 21,
  routing 7, usage 12.

Two expectations I got wrong and corrected rather than working around:

- An anonymous `POST /nope/chat/completions` returns **302**, not 400. That is
  correct — a 400 naming the unknown profile would be an unauthenticated oracle for
  enumerating profile names. The 400 is asserted with the cookie, the 302 without.
- `grep -L` exits 0 when every file **matches**, so `grep -L … && exit 1` failed a
  passing run. The parallel cases now check each file explicitly.

The one genuine defect the suite found is `d0253a7`.

`d0253a7` — `fix(api): /api/providers/health silently dropped a colliding provider`

`providers_out` was keyed by `provider/profile`, and two configs can share both
while pointing at different base URLs: opencode's zen gateway
(`https://opencode.ai/zen/go/v1`) and its OpenAI-compatible endpoint
(`https://opencode.ai/inference/openai/v1`) both serve `l2`. The second write
overwrote the first, so the payload carried 10 entries while `summary.total`
counted 11 from the loop.

Measured before the fix: `summary {total: 11, healthy: 10, degraded: 1}`,
`len(providers) = 10`. After: 11 and 11. The entry that was lost was the **healthy**
zen gateway; the survivor was the degraded "out of credits" endpoint. The page
showed 10 rows under a heading claiming 11, and the provider that was actually fine
was the invisible one.

The key carries a discriminator derived from the base URL only on collision, so the
common `provider/profile` key is unchanged (existing tests pin it). The providers
page no longer parses the key to recover provider and profile — it reads them from
the entry, so the discriminator cannot leak into a profile heading.

### 7b. Conversations: audited against the database, then fixed

The audit read the live store directly (`data/costs.db`) and probed the endpoints.
Staging's copy is 1.0 GB against prod's 7.8 GB, so the prod file was audited
read-only for anything that had to be true of the product rather than of the copy.

**What the audit found**

1. **The transcript capture re-appends history.** `capture_request` appends the
   client's full `messages` array, and dedup was a longest-common-**prefix** match
   against the stored hashes. A prefix match only holds while the incoming list is
   an append-only continuation. The agent compacts, so the list diverges early, the
   prefix stops at the first divergence, and the entire remainder — history already
   stored — is appended again. Every compaction re-appends everything after it, so
   a conversation grows as O(n²).

   Measured: conversation `20261023_155414_d4409841` held **193,930 rows for 57
   distinct user messages**, with one `tool_call_id` stored **222 times**. Prod:
   **3,550,889 rows** for 629 live conversations, one conversation holding 494,944
   messages across 48 hours.

2. **`msg_hash` cannot identify a message.** It covers role + content + tool_name +
   tool_call_id, but not `tool_calls` — and a tool-calling assistant turn has no
   content. So **1,044,282 empty assistant rows share exactly one hash** (distinct
   hashes among them: 1). Any uniqueness or dedupe keyed on it treats all of them as
   the same message.

3. **Cost and transcript are disjoint.** On staging, the 1,383 conversations that
   carry cost ($1,121.69, 5.1B tokens) have no transcript, and the 3,666 that carry
   a transcript have `calls`/`tokens`/`cost` all zero. On prod, 1,427 of 2,056
   conversations are rollup-only (requests, no transcript) and 629 are transcript-
   only-ish; **$245.31 of $1,481.14 spend (16.6%) is unattributed** because 38,100
   requests (31.5%) carry no `x-opencode-session`. Almost all of it is `l2`
   ($243.12). `conversations.cost` sums to $1,210.71, so the Conversations page's
   Cost column can never reconcile with the Usage page's total and nothing on
   screen explains the gap.

   The comment in `handler.py` says "Hermes attaches its agent session id to every
   LCP request". Measured: 31.5% of requests have none, and they are concentrated in
   the main agent profile. That comment is not true of the deployed system.

4. **Summary generation is inverted.** Prod: 1,428 conversations carry an
   LLM-written summary and **exactly one** of them has a transcript; 628 transcript
   conversations have none. Staging: 1,383 with summary, **zero** with a transcript.
   So the summaries describe conversations you cannot read, and the conversations
   you can read have no summary — which means a summary can never be checked against
   its source.

5. **Timestamps are not monotonic along `seq`.** 218 staging conversations have a
   `ts` that goes backwards as `seq` advances; 164 have a first-by-`seq` message
   whose `ts` is not the earliest. `seq` is the authoritative order (and the
   transcript renders by it), but the per-message times shown can appear to run
   backwards. The QA case now asserts `seq` order, not `ts` order.

6. **Naming.** 1,663 of 5,051 staging conversations have no name: 461 `api-<hex>`,
   806 bare timestamps, 392 `cron_<id>_<ts>`, 2 `E2E`. The table rendered a bare id
   with nothing marking it as untitled.

7. **Compaction dominates.** In the largest conversations 78.9–97.4% of messages are
   inactive. The default transcript view is therefore mostly compacted history.

**What shipped**

`39a10be` — `fix(conversations): the history table stops presenting "unknown" as "zero"`

`message_count`, `active_message_count` and `tool_call_count` are NULL on a
rollup-only conversation, and `r["message_count"] or 0` turned that into 0. A row
read "0 messages, 0 tool calls" while its own summary said "16 tool calls recorded
over a four-minute span", and the audit modal correctly rendered "—" for the same
fields — the table and the modal contradicted each other about the same conversation.

Now NULL survives to the template, which renders an em dash plus a muted "no
transcript"; `(N live)` became `(N active)` with a tooltip (`active_message_count` is
what remains in the context window after compaction, not a call in flight); a new
`fmt_iso` filter renders `2026-09-20 04:49:03 UTC` instead of the 32-character ISO
string that wrapped mid-token; an unnamed row renders "Untitled · <id>".

Verified: `?sort=calls&per=all` returns 5,051 rows, 1,383 of them rendering
"— — no transcript".

`9feab61` — `fix(conversations): mark spend as unmeasured instead of showing $0.0000`

Same class of bug on the new columns: 3,666 conversations have a transcript and no
request rows, so `calls`/`tokens`/`cost` are 0 — one of them has 5,874 messages and
read "0 tokens, $0.0000". The store exposes `measured` (true only when the
conversation has at least one request row) and the columns render an em dash when it
is false. Measured: 1,385 of 5,051 are measurable; the other 3,666 render "—".

`68f6a1d` — `feat(conversations): offer the sorts the API already supports`

`/api/conversations` advertises four sorts and the filter form offered none; only
the default was ever sent. The options render from `f.sorts`, the same list the
server resolves its ORDER BY against, so the control cannot offer a sort the endpoint
would reject. Every pager link now carries `sort=` — without it, paging through
oldest-first silently reverted to newest-first on page 2.

`1c9f269` — `feat(conversations): show tokens and cost per conversation`

This is a router; spend per conversation is the first number an operator asks for and
it was reachable only by opening the modal. Both columns are right-aligned with
tabular figures. Tokens go through `fmt_num` (6.25B renders as "6250.7M") because at
that magnitude the exact count is noise in a table; the precise figure stays in the
modal.

`a5335bb` — `feat(conversations): surface the stored summary in the history row`

1,383 conversations carry an LLM-written summary and the table showed none of it —
the only way to read one was to open the modal and expand a collapsed `<details>`.
It now sits under the name, clamped to two lines, in muted type; the full text is
still in the audit panel. Measured: 2 lines at 15.55px line-height, 31px tall.

`7ed257a` — `feat(conversations): jump to a page, not just the five in the window`

5,051 conversations at 20/page is 253 pages and the pager windows five of them, so
reaching page 180 meant clicking Next 170 times. The log tables got a jump field in
wave 3; this pager did not. It is a GET form carrying profile, query, per and sort,
so the jump stays a URL. Verified: page 17 → `activePage 17`, 20 rows; page 9999 →
clamped to 21, 8 rows (the last page), not an empty table.

`80f08c7` — `fix(conv): stop re-appending history when the client compacts`

Dedup is now by **multiset**: count each incoming hash, count each stored hash,
append only the surplus copies in incoming order. Counting is order-independent, so
compaction cannot defeat it, and multiplicity is preserved — an identical "ok" sent
twice is still stored twice, which both positional and set dedup got wrong.

Eight unit tests in `tests/test_conv_capture_dedup.py` pin it, including the two
failure modes: a compacted list must add nothing, and a compacted list plus one new
turn must add exactly one row.

**Not retroactive.** Existing rows stay inflated; this stops the growth, it does not
shrink what is already there. A cleanup would be a separate, destructive migration.

**Not fixed.** `content_hash` ignores `tool_calls`, so all empty assistant turns
share one hash. Multiset dedup copes with that (it compares counts) but a hash that
identified a message rather than its text would be better — that is a schema change
and needs approval.

### 7c. Charts: colour by name, not by position

`e0b9713` — `fix(charts): colour series by name, not by position`

`ppData` carries six profiles — `l2`, `l1`, `career`, `cron`, `coder`,
`naptune-admin` — and `profileColors` had entries for four. `coder` ($533.13, the
largest spender on the page) and `naptune-admin` both fell through to the same grey:
two series in an identical colour, and the biggest one was the hardest to find.

The per-model chart had the mirror bug: a six-hue ring indexed by array position, so
inserting one model shifted every colour and the same model was a different colour on
different days.

Both now derive the hue from the series **name** — FNV-1a into a twelve-hue ring,
with the four original colours pinned and `coder`/`naptune-admin` given fixed slots.
A name colliding with a hue already used on the chart steps to the next free one.
Verified: per-profile 6 datasets / 6 distinct colours / 67 points each; per-model 6
datasets / 6 distinct / 99 points each.

`8672e58` — `fix(charts): take axis and legend colour from the theme token`

Axis and legend text was hardcoded `#a1a1aa` in two places — a hex that merely
happens to match `--muted-foreground` today. It now reads the token off
`document.documentElement` and falls back to the old hex only if it is missing.
Gridlines were Chart.js's default mid-grey (chosen for a light canvas); they now use
the card-border token at 35%.

`79e9730` — `fix(charts): put the grid option inside each scale`

The grid colour above was written as a sibling of `x`/`y` inside `scales`. Chart.js
reads every key of `scales` as a scale definition, so it created a scale named
"grid" with no axis and threw
`TypeError: Cannot read properties of undefined (reading 'axis')` from
`buildCharts`. Both `switchView('per-profile')` and `switchView('per-model')`
aborted, so clicking either tab left the previous view on screen and the palette fix
was invisible. Caught by calling `switchView` in the browser and reading the
exception — the unit tests could not see it.

`f16736a` — `fix(dashboard): a profile dropped from config no longer blanks the whole page`

`pp_data["profiles"]` was pre-seeded from `config.profiles` only, then indexed by
`r.profile` for every request row in the window. History outlives config: dropping a
profile leaves its requests in the database and the lookup raises KeyError. The
failure mode is worse than a 500 — the whole query block sits under a blanket
`except Exception` that logs `dashboard_query_failed` and substitutes zeros, so the
page renders successfully reporting $0.00 and no traffic. The series is now created
on demand, so a retired profile shows its historical spend instead of erasing the
page. (The per-model block already guarded this way.)

### Rollback

`git revert --no-edit 79e9730 f16736a 8672e58 e0b9713 80f08c7 7ed257a a5335bb 1c9f269 68f6a1d 9feab61 0a70e59 d0253a7`

newest-first. `80f08c7` reverts the dedup but leaves `tests/test_conv_capture_dedup.py`
in place — revert that file by hand, or revert `80f08c7` and delete it.

### Still open after wave 7

- **Attribution.** 16.6% of spend cannot be tied to a conversation because 31.5% of
  requests carry no session header. The fix is client-side (attach the id) plus a
  backfill heuristic, and the heuristic is ambiguous — one 36-hour window contained
  1,410 `l2` requests for a single conversation. Needs a decision.
- **Summary coverage.** Summaries exist only for conversations with no transcript.
  Either generate them for transcripts (then they can be checked) or stop generating
  them where there is nothing to summarise.
- **Inflated history.** ~3.5M prod rows for what is probably ~250k real messages.
  A dedupe migration would be destructive and needs approval.
- **`msg_hash`.** Should cover `tool_calls`; schema change.
- **Profile vocabulary.** Staging carries `homelab-expert-l2` (3,016),
  `homelab-expert-l1` (516), `hermes-career-advisor` (49), `blog-writer` (80) — agent
  profile names written by a session importer that prod does not run. Prod is clean
  (`l2`, `l1`, `coder`, `career`, `naptune-admin`, `blog`). Two importers, two
  vocabularies; the Conversations filter can offer values `/api/profiles` does not
  know.
- **Default transcript view.** 78.9–97.4% of the messages in the largest
  conversations are compacted. An "active only" default would be honest about that.
- **No print stylesheet**; the log tables print with dark backgrounds.
- **Motion phase** still unimplemented — only the `prefers-reduced-motion` gate
  exists.

## Still open

**Not shipped**

- Prod cutover. Staging is ahead of `origin/main` by `git rev-list --count
  origin/main..HEAD` commits (66 as of this line) and nothing is pushed
  anywhere. Prod is at `2e1e326` (the ring fix `14efbdc` is still an ancestor) and
  serves neither `ui.js` nor `sparkline.js` — both 404 there. Prod also carries 4
  pre-existing dirty files unrelated to this work.

**Agent-infrastructure plumbing** — cleared on 2026-10-25 (wave 6). The five routes,
both modules and `profile_paths` are gone. What remains outside this repo, by design:
`work-sources.json` and the host-side collector/executor that own it.

**QA harness** — regenerated in wave 7 from a probe of the live route table. 182
cases, 182 passing, with the runner authenticating through a throwaway `HOME/.curlrc`.

**UI and design gaps**

- Dark-only by design; no high-contrast light theme.
- The motion phase (Odometer / AutoAnimate) is unimplemented — only the
  `prefers-reduced-motion` gate exists.
- No print stylesheet; the log tables print with dark backgrounds.

**Known, accepted**

- The wider test suite has 82 failures/errors (router, LanceDB memory backend) that
  reproduce at `1452f7f`; the five UI-facing files are clean.
- `vision_analyze` times out on full-page screenshots in this environment; crops and
  CDP geometry are the reliable read-back.


## Wave 7 promotion — staging merged into prod, 2026-10-26

Staging had 89 unpushed commits and prod was five commits ahead of the merge base
(`fd53302`, the deepseek `json_object` capability fix, prod-only). The promotion is
a real merge, not a reset: prod keeps its own pipeline commits, staging brings the
UI, QA harness and the backend removals.

**Commits**

| What | Commit | Where |
|---|---|---|
| Merge staging `main` into prod `main` (`--no-ff`) | `6a2bc4c` | prod, and `main` was fast-forwarded to it |
| QA sort cases assert the field the `ORDER BY` uses | `71abec8` | prod |
| Same QA fix, cherry-picked onto the staging line | `11cd62f` | staging |

The merge had exactly one conflict, in `dashboard.css`: both sides had independently
declared the same `--ring` value with different comments. Resolved by keeping the
staging comment; the value `217.2 91.2% 59.8%` is identical on both sides, so the
resolution cannot change rendering.

**Safety net taken before the rebuild**

- Running image pinned: `lcp-lcp:rollback-20261025-pre-wave7` → `43d1e765de7c`
  (asserted equal to the container's live `{{.Image}}` at pin time, not assumed).
- Prod HEAD preserved on branch `backup-prod-before-wave7` (`fd53302`).
- No DB migration: both trees carry alembic `025`, and the live DB reports `025`.
  Nothing was written to `costs.db`; all prod DB reads in this section were
  `mode=ro`.

**Rollback (whole promotion)**

```
cd /your/data/docker-apps/lcp
git checkout main && git reset --hard fd53302        # backup-prod-before-wave7
docker tag lcp-lcp:rollback-20261025-pre-wave7 lcp-lcp:latest
docker compose --env-file host-paths.env -p lcp -f docker-compose.yml up -d lcp
```

**Rollback (the QA fix only)** — `git revert --no-edit 71abec8`; it changes three
files under `qa/` and touches no runtime code.

**Cutover mechanics.** `docker compose … up -d lcp` (never `down`), so the recreate
is the only interruption. Measured: container recreated 13:37:38Z → started
13:37:48Z, `/health` 200 two seconds later, `RestartCount=0` afterwards. Traffic
resumed immediately — the request log shows a completed `l2` request at 13:39:26Z,
164 ms, attributed to a conversation id.

**What was verified on prod after the rebuild**

| Check | Result |
|---|---|
| `/health` | 200, 1.3 ms |
| Console routes (authed) | `/profiles`, `/models`, `/usage`, `/alerts`, `/conversations`, `/activity` all 200 |
| Legacy routes | `/providers`→`/models?tab=providers`, `/logs`→`/activity?tab=logs`, `/in-progress`→`/conversations`, `/keys`→`/profiles?tab=keys`, `/work/cron` and `/work/config`→`/profiles`, `/dashboard`→`/activity`; `/setup` and `/work/tasks` 404 |
| Static assets that were 404 before | `/static/js/ui.js` 200 (3,528 B), `/static/js/sparkline.js` 200 (7,430 B) |
| `/api/providers/health` | `summary.total` 19, providers listed 19 — the two colliding `opencode/l2` entries both survive (`opencode/l2` degraded, `opencode/l2 (opencode.ai/inference/openai)` dead) |
| Conversations history table | columns Conversation / Profile / Model / Source / Messages / Tool calls / Tokens / Cost / Last activity / View; sort control in the filter bar; both tables carry an `sr-only` `<caption>`; unmeasured rows render `—` for Tokens and Cost |
| Chart views | aggregate 1 dataset × 14 points; per-profile 7 datasets × 74 points, 7 distinct colours; per-model 10 datasets × 129 points, 10 distinct colours; `switchView` throws nothing, zero `window.onerror` events |
| QA sweep against prod | **182 / 182**, groups api 35, auth 9, conv 26, logs 26, pages 35, proxy 11, resilience 21, routing 7, usage 12 |
| QA sweep against staging | 182 / 182 (re-run with the fixed sort cases) |
| Full pytest suite | 38 failed + 44 errors = **82**, identical to the `1452f7f` baseline; 2,733 passed |
| Transcript growth | **362 rows/min before the cutover → 0.8 rows/min after** — measured with two `conv_messages` counts a minute apart on either side of the rebuild. The multiset dedupe (`80f08c7`) is now the code path prod runs |

**One QA case was wrong, and prod data proved it.** `cv-sort-calls` asserted that
`calls` was monotonic under `sort=calls`, but the server orders by `tool_call_count`;
`calls` is the request-derived column and is not sorted at all. On staging every
`calls` value is 0, so the assertion passed vacuously and stayed green for a whole
wave. Against prod it returned `[0, 3, 282, 0, 17, 35, 36, 32, 0, 0]`. Both sort
cases now assert the field the `ORDER BY` names, page at `per=100`, check the
non-NULL prefix is monotonic and require the NULL tail to stay last instead of being
coerced to 0 (`71abec8`).

**Found while verifying, not fixed — the `measured` flag hides $8.76.** Prod has 230
live conversations whose `calls`/`tokens`/`cost` rollups are NULL, so `measured` is
false and the table renders `—`. All 230 of them *do* have attributed request rows:
10,111 requests worth $8.76 (0.59% of the $1,484.60 request total). The honest fix is
for `measured` to fall back to an `EXISTS` on `requests` when the rollup is NULL, but
that puts a correlated subquery on a 7.8 GB database in the pager's hot path, so it
needs a decision before it ships.
