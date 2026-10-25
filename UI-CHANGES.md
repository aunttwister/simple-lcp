# LCP UI change ledger

Every user-visible change made to the LCP console, one line each, with the commit
that carries it and how to undo it. Staging repo `simple-lcp`, branch `main`.
Prod (`/your/data/docker-apps/lcp`, `:8734`) has **none** of this — it is still at
`3be4362` and serves neither `ui.js` nor `sparkline.js` (both 404 there).

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

**QA harness** — the cron+sources group is regenerated, but the rest of the suite
still exercises routes cut in earlier slims (`/api/setup`, `/api/work/tasks`,
`/api/work/fleet`, `/api/work/status`, `/api/work/conversations`). Those cases could
only ever fail; they need their own regeneration pass against the current route table.

**UI and design gaps**

- Dark-only by design; no high-contrast light theme.
- The motion phase (Odometer / AutoAnimate) is unimplemented — only the
  `prefers-reduced-motion` gate exists.
- `sec_dashboard_script.html` hardcodes chart colours for four profile names
  (`l2`, `l1`, `career`, `cron`) and falls back to grey for everything else. A
  six-profile fleet therefore draws several identical grey series. It needs a
  generated palette, not a lookup table.
- No print stylesheet; the log tables print with dark backgrounds.

**Known, accepted**

- The wider test suite has 82 failures/errors (router, LanceDB memory backend) that
  reproduce at `1452f7f`; the five UI-facing files are clean.
- `vision_analyze` times out on full-page screenshots in this environment; crops and
  CDP geometry are the reliable read-back.
