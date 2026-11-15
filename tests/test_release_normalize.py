"""Tests for the release normalisation (policy A) — newest release per line.

The operator's report, pinned here: the Models page showed `claude-opus-5` while
the provider served `claude-opus-5-5` and the arena board had moved to "Claude
Opus 5.5". The registry has to carry the release the provider actually serves.

What these tests protect:

* `identity()` — the family / release / variant split, including the vendor
  prefixes (`moonshotai/Kimi-K3`) and lettered releases (`m3.1`, `k3.1`);
* a successor needs the SAME family AND the SAME variant and a HIGHER release —
  `gpt-6.1-sol` is not a successor of `gpt-5.6-luna`, and `MiniMax-M3.1-Flash` is
  not a plain M3.1;
* only the highest release of a line is registered (4.6 does not get registered
  when the provider already serves 4.7);
* the arena's marketing spelling is captured as an alias, WITHOUT the effort
  suffix (effort is a dimension), and is what the poll then resolves;
* `apply()` claims the poll's staged deployment row (it does not insert a second
  row for the same served id), and the whole thing is undone by `undo_sql()`.
"""
from __future__ import annotations

import sqlite3

import pytest

from src.api import arena_poll, release_normalize as rn

SCHEMA = """
CREATE TABLE mi_models (
    model_id INTEGER PRIMARY KEY, slug TEXT UNIQUE, display_name TEXT, family TEXT,
    status TEXT, supersedes_model_id INTEGER, benchmark_key TEXT, created_at TEXT,
    active_release TEXT, benchmark_release TEXT, quantization TEXT);
CREATE TABLE mi_endpoints (
    endpoint_id INTEGER PRIMARY KEY, slug TEXT, display_name TEXT, base_url TEXT,
    status TEXT, poll_enabled INTEGER);
CREATE TABLE mi_deployments (
    deployment_id INTEGER PRIMARY KEY, endpoint_id INTEGER, model_id INTEGER,
    served_id TEXT, status TEXT, source TEXT, verified_at TEXT, first_seen TEXT,
    last_seen TEXT, notes TEXT, UNIQUE (endpoint_id, served_id));
CREATE TABLE mi_sources (
    source_id INTEGER PRIMARY KEY, slug TEXT, kind TEXT, convention TEXT);
CREATE TABLE mi_model_names (
    id INTEGER PRIMARY KEY, model_id INTEGER, source_id INTEGER, name TEXT,
    is_primary INTEGER, confidence TEXT, evidence TEXT, first_seen TEXT,
    UNIQUE (source_id, name));
"""

# What a real CommandCode catalog looks like next to what the registry carries:
# the registered release, the staged newer ones, a lower one, and a variant that
# must NOT be read as a successor.
CATALOG = [
    # (model_id, deployment_id, served_id, status)
    (11, 1, "claude-opus-5", "active"),                  # ours
    (None, 2, "claude-opus-5-5", "new"),                 # successor (Opus 5.5)
    (None, 3, "claude-opus-4-8", "new"),                 # an older release
    (12, 4, "gpt-5.6-luna", "active"),                   # ours
    (None, 5, "gpt-6-luna", "new"),                      # successor, lower of two
    (None, 6, "gpt-6.1-luna", "new"),                    # successor, highest
    (None, 7, "gpt-6.1-sol", "new"),                     # a DIFFERENT variant
    (13, 8, "Google/gemini-3.6-flash", "new"),           # ours, but NEVER confirmed
    (None, 9, "Google/gemini-3.8-flash", "new"),         # successor
    (14, 10, "xai/grok-4.5", "active"),                  # ours
    (None, 11, "xai/grok-4.6", "new"),                   # successor, lower
    (None, 12, "xai/grok-4.7", "new"),                   # successor, highest
    (15, 13, "moonshotai/Kimi-K3", "active"),            # ours
    (None, 14, "moonshotai/Kimi-K3.1-Pro", "new"),       # variant differs -> no
    (16, 15, "minimax/MiniMax-M3", "active"),            # ours
    (None, 16, "minimax/MiniMax-M3.1-Flash", "new"),     # variant differs -> no
    (17, 17, "deepseek/deepseek-v4.1-flash", "active"),  # ours
    (None, 18, "deepseek/deepseek-v4.1-flash-fast", "new"),   # variant differs -> no
    (None, 20, "deepseek/deepseek-v4.1-flash-20261017", "new"),  # a DATE, not a release
    (None, 21, "xai/grok-4.5-0709", "new"),              # a dated snapshot of a model we have
    (18, 19, "retired-line-1", "active"),                # a model with no successor
]
MODELS = [
    (11, "claude-opus-5", "Claude Opus 5", "anthropic", "active"),
    (12, "gpt-5.6-luna", "GPT-5.6 Luna", "openai", "active"),
    (13, "gemini-3.6-flash", "Gemini 3.6 Flash", "google", "active"),
    (14, "grok-4.5", "Grok 4.5", "xai", "active"),
    (15, "kimi-k3", "Kimi K3", "moonshot", "active"),
    (16, "minimax-m3", "MiniMax M3", "minimax", "active"),
    (17, "deepseek-flash", "DeepSeek Flash", "deepseek", "active"),
    (18, "retired-line-1", "Retired Line", "vendor", "active"),
]

ARENA = [
    {"arena_name": "Claude Opus 5.5 (High)"},
    {"arena_name": "Claude Opus 5.5 (Max)"},
    {"arena_name": "Claude Opus 5 (High)"},          # the OLD name, must not alias
    {"arena_name": "GPT-6.1 Luna (Max)"},
    {"arena_name": "Gemini 3.8 Flash (High)"},
    {"arena_name": "Grok 4.7 (xHigh)"},
    {"arena_name": "Kimi K3.1 Pro (Max)"},
    {"arena_name": "Some Other Model"},
]


@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute("INSERT INTO mi_endpoints (endpoint_id, slug, status, poll_enabled)"
                " VALUES (6,'commandcode','active',1)")
    con.execute("INSERT INTO mi_endpoints (endpoint_id, slug, status, poll_enabled)"
                " VALUES (2,'local-zgx','retired',0)")
    for mid, slug, disp, fam, status in MODELS:
        con.execute("INSERT INTO mi_models (model_id, slug, display_name, family, status,"
                    " created_at) VALUES (?,?,?,?,?,'2026-10-30T00:00:00Z')",
                    (mid, slug, disp, fam, status))
    for mid, dep, served, status in CATALOG:
        con.execute("INSERT INTO mi_deployments (deployment_id, endpoint_id, model_id,"
                    " served_id, status, source) VALUES (?,6,?,?,?,'poll')",
                    (dep, mid, served, status))
    con.execute("INSERT INTO mi_sources (source_id, slug, kind) VALUES (3,'lmarena','benchmark_arena')")
    con.execute("INSERT INTO mi_sources (source_id, slug, kind) VALUES (1,'canonical','canonical')")
    con.commit()
    return con


def _plan(db, arena=ARENA):
    return rn.plan(db, arena)


# ── identity ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("served_id,family,release,variant", [
    ("claude-opus-5", ("claude", "opus"), (5,), ()),
    ("claude-opus-5-5", ("claude", "opus"), (5, 5), ()),
    ("claude-fable-5-2", ("claude", "fable"), (5, 2), ()),
    ("gemini-3.8-flash", ("gemini",), (3, 8), ("flash",)),
    ("Google/gemini-3.8-flash", ("gemini",), (3, 8), ("flash",)),
    ("moonshotai/Kimi-K3.1-Pro", ("kimi",), (3, 1), ("pro",)),
    ("minimax/MiniMax-M3", ("minimax",), (3,), ()),
    ("v4.1-flash", (), (4, 1), ("flash",)),
    ("gpt-6.1-sol", ("gpt",), (6, 1), ("sol",)),
    ("deepseek-v4.1-flash-fast", ("deepseek",), (4, 1), ("flash", "fast")),
])
def test_identity_splits_family_release_and_variant(served_id, family, release, variant):
    assert rn.identity(served_id) == (family, release, variant)


def test_a_trailing_bare_number_continues_the_release():
    """`claude-opus-5-5` is 5.5, not 'release 5, variant 5'."""
    assert rn.identity("claude-opus-5-5")[1] == (5, 5)
    assert rn.identity("claude-opus-5-5")[2] == ()


def test_version_comparison_pads_the_shorter_tuple():
    assert rn._gt((5, 5), (5,))            # 5.5 > 5
    assert rn._gt((6,), (5, 6))            # 6 > 5.6
    assert not rn._gt((5,), (5, 5))
    assert not rn._gt((5, 6), (6,))
    assert not rn._gt((), (5,))            # unversioned is never 'newer'


def test_slug_and_display_name_for_a_served_id():
    assert rn.slug_for_served_id("Google/gemini-3.8-flash") == "gemini-3.8-flash"
    assert rn.slug_for_served_id("claude-opus-5-5") == "claude-opus-5-5"
    assert rn.display_name_for("claude-opus-5-5") == "Claude Opus 5 5"
    assert rn.display_name_for("Google/gemini-3.8-flash") == "Gemini 3.8 Flash"


# ── what counts as a successor ──────────────────────────────────────────────

def test_only_a_higher_release_of_the_same_variant_is_a_successor(db):
    slugs = {r["slug"] for r in rn.catalog_successors(db)["register"]}
    assert slugs == {"claude-opus-5-5", "gpt-6.1-luna", "gemini-3.8-flash", "grok-4.7"}


def test_a_different_variant_is_never_a_successor(db):
    """`gpt-6.1-sol` must not be registered as the successor of `gpt-5.6-luna`."""
    assert "gpt-6.1-sol" not in {r["slug"] for r in rn.catalog_successors(db)["register"]}


def test_only_the_highest_release_of_a_line_is_registered(db):
    reg = {r["slug"]: r for r in rn.catalog_successors(db)["register"]}
    assert "grok-4.6" not in reg and "grok-4.7" in reg
    assert "gpt-6-luna" not in reg and "gpt-6.1-luna" in reg
    skipped = {s["slug"] for s in rn.catalog_successors(db)["skipped"]}
    assert {"grok-4.6", "gpt-6-luna"} <= skipped


def test_an_older_release_is_not_a_successor(db):
    assert "claude-opus-4-8" not in {r["slug"] for r in rn.catalog_successors(db)["register"]}


@pytest.mark.parametrize("served_id,expect", [
    ("qwen3.8-max-0902", (("qwen",), (3, 8), ("max", "0902"))),
    ("Qwen/Qwen3.8-Max", (("qwen",), (3, 8), ("max",))),
    ("deepseek-v4.1-flash-20261017", (("deepseek",), (4, 1), ("flash", "20261017"))),
    ("xai/grok-4.5-0709", (("grok",), (4, 5), ("0709",))),
])
def test_a_dated_run_is_part_of_the_variant_not_the_release(served_id, expect):
    """A build stamp must not read as a later release — it would register a snapshot."""
    assert rn.identity(served_id) == expect


def test_a_dated_snapshot_is_never_registered_as_a_successor(db):
    """`grok-4.5-0709` is grok-4.5, not grok-4.6."""
    reg = {r["slug"] for r in rn.catalog_successors(db)["register"]}
    assert "grok-4.5-0709" not in reg
    assert "deepseek-v4.1-flash-20261017" not in reg
    # ...while the real successor on that line is still found
    assert "grok-4.7" in reg


@pytest.mark.parametrize("served_id,family", [
    ("qwen3.8-max", ("qwen",)),
    ("mistral3.9-max", ("mistral",)),
    ("claude-opus-5", ("claude", "opus")),
    ("m3.1-flash", ()),          # a one-letter version marker stays a release
    ("k3.1-pro", ()),
])
def test_a_vendor_word_is_family_a_version_marker_is_not(served_id, family):
    assert rn.identity(served_id)[0] == family


def test_two_vendors_with_the_same_release_shape_are_not_one_line(db):
    """`mistral3.9-max` must not be read as a successor of `qwen3.8-max`."""
    db.execute("INSERT INTO mi_models (model_id, slug, display_name, family, status,"
               " created_at) VALUES (31,'qwen3.8-max','Qwen 3.8 Max','qwen','active',"
               "'2026-10-30T00:00:00Z')")
    db.execute("INSERT INTO mi_deployments (deployment_id, endpoint_id, model_id, served_id,"
               " status, source) VALUES (30,6,31,'Qwen/Qwen3.8-Max','active','registry')")
    db.execute("INSERT INTO mi_deployments (deployment_id, endpoint_id, model_id, served_id,"
               " status, source) VALUES (31,6,NULL,'mistral3.9-max','new','poll')")
    reg = {r["slug"] for r in rn.catalog_successors(db)["register"]}
    assert "mistral3.9-max" not in reg


def test_a_staged_row_at_a_retired_endpoint_is_ignored(db):
    """Only an endpoint that is live can register anything."""
    db.execute("INSERT INTO mi_deployments (deployment_id, endpoint_id, model_id,"
               " served_id, status, source) VALUES (99,2,NULL,'claude-opus-9-9','new','poll')")
    reg = {r["slug"] for r in rn.catalog_successors(db)["register"]}
    assert "claude-opus-9-9" not in reg


def test_an_already_claimed_deployment_is_not_a_candidate(db):
    db.execute("UPDATE mi_deployments SET model_id=11 WHERE deployment_id=2")
    assert "claude-opus-5-5" not in {r["slug"] for r in rn.catalog_successors(db)["register"]}


def test_a_retired_model_does_not_anchor_a_successor(db):
    db.execute("UPDATE mi_models SET status='retired' WHERE slug='claude-opus-5'")
    assert "claude-opus-5-5" not in {r["slug"] for r in rn.catalog_successors(db)["register"]}


# ── the arena's spelling ────────────────────────────────────────────────────

def test_the_arena_name_is_captured_as_an_alias_without_the_effort_suffix(db):
    """(High) and (Max) are one name: the effort is a dimension."""
    aliases = rn.arena_aliases([{"slug": "claude-opus-5-5", "served_id": "claude-opus-5-5"}],
                               ARENA)
    assert aliases["claude-opus-5-5"] == ["Claude Opus 5.5"]


def test_the_old_arena_name_is_not_captured_for_the_new_release(db):
    aliases = rn.arena_aliases([{"slug": "claude-opus-5-5", "served_id": "claude-opus-5-5"}],
                               ARENA)
    assert "Claude Opus 5" not in aliases["claude-opus-5-5"]


def test_an_alias_is_found_by_normalised_spelling_too(db):
    """A served id that IS the arena spelling needs no version math."""
    aliases = rn.arena_aliases([{"slug": "kimi-k3.1-pro", "served_id": "moonshotai/Kimi-K3.1-Pro"}],
                               ARENA)
    assert "Kimi K3.1 Pro" in aliases["kimi-k3.1-pro"]


def test_a_release_with_no_arena_name_is_reported_not_silently_registered(db):
    p = rn.plan(db, [{"arena_name": "Some Other Model"}])
    assert "claude-opus-5-5" in p["unaliased"]
    assert "warning" in rn.describe(p)


def test_the_poll_resolves_the_new_alias_to_the_new_model(db):
    """The point of the alias: the board's own spelling lands on our slug."""
    rn.apply(db, _plan(db))
    alias, known = arena_poll.load_resolvers(db)
    slug, effort = arena_poll.resolve("Claude Opus 5.5 (Max)", alias, known)
    assert (slug, effort) == ("claude-opus-5-5", "max")


# ── the plan ────────────────────────────────────────────────────────────────

def test_the_plan_retires_the_superseded_release(db):
    p = _plan(db)
    assert {r["slug"] for r in p["retire"]} == {"claude-opus-5", "gpt-5.6-luna",
                                               "gemini-3.6-flash", "grok-4.5"}
    assert {r["replaced_by"] for r in p["retire"]} == {"claude-opus-5-5", "gpt-6.1-luna",
                                                       "gemini-3.8-flash", "grok-4.7"}


def test_a_model_with_no_newer_release_is_left_alone(db):
    p = _plan(db)
    touched = {r["slug"] for r in p["retire"]} | {r["slug"] for r in p["register"]}
    assert "kimi-k3" not in touched and "minimax-m3" not in touched
    assert "deepseek-flash" not in touched and "retired-line-1" not in touched


def test_a_line_with_no_confirmed_deployment_still_anchors_its_newest_release(db):
    """The operator's case: we carry `gemini-3.6-flash` but never confirmed it,
    and CommandCode serves `gemini-3.8-flash`. Hiding the line would leave the
    board without the model; its newest SERVED release is what gets registered."""
    reg = {r["slug"]: r for r in rn.catalog_successors(db)["register"]}
    assert "gemini-3.8-flash" in reg
    assert reg["gemini-3.8-flash"]["anchor_confirmed"] is False
    assert "gemini-3.7-flash" not in reg             # the lower release is skipped


def test_a_confirmed_line_is_marked_as_such(db):
    reg = {r["slug"]: r for r in rn.catalog_successors(db)["register"]}
    assert reg["claude-opus-5-5"]["anchor_confirmed"] is True


def test_describe_flags_a_line_that_was_never_confirmed(db):
    text = rn.describe(_plan(db))
    assert "NO confirmed deployment" in text


def test_a_model_row_with_no_deployment_never_creates_a_registration(db):
    """The anchor is a provider's catalog row, not a name in mi_models."""
    db.execute("INSERT INTO mi_models (model_id, slug, display_name, family, status,"
               " created_at) VALUES (40,'orphan-model','Orphan','orphan','active',"
               "'2026-10-30T00:00:00Z')")
    reg = {r["slug"] for r in rn.catalog_successors(db)["register"]}
    assert not any("orphan" in s for s in reg)


def test_a_second_run_has_nothing_left_to_do(db):
    """`apply` claims the staged row, so it stops being a candidate at all."""
    rn.apply(db, _plan(db))
    again = rn.plan(db, ARENA)
    assert again["register"] == [] and again["retire"] == [] and again["skipped"] == []


def test_an_already_registered_successor_is_not_retired_again(db):
    """The other side of idempotence: a slug that already exists is left alone."""
    entry = {"slug": "claude-opus-5-5", "served_id": "claude-opus-5-5", "endpoint_id": 6,
             "deployment_id": 2, "endpoint_slug": "commandcode", "display_name": "Claude Opus 5 5",
             "family": "anthropic", "release": "5.5", "version": (5, 5), "variant": (),
             "supersedes_slug": "claude-opus-5", "supersedes_model_id": 11,
             "supersedes_version": (5,)}
    rn.apply(db, rn.plan(db, ARENA, register=[entry]))
    again = rn.plan(db, ARENA, register=[entry])
    assert again["register"][0]["already_registered"] is True
    assert again["retire"] == []


def test_describe_names_the_old_and_new_release(db):
    text = rn.describe(_plan(db))
    assert "claude-opus-5 -> claude-opus-5-5" in text
    assert "arena names: 'Claude Opus 5.5'" in text


# ── the write ───────────────────────────────────────────────────────────────

def test_dry_run_writes_nothing(db):
    receipt = rn.apply(db, _plan(db), dry_run=True)
    assert receipt["registered"] and receipt["dry_run"]
    assert db.execute("SELECT COUNT(*) FROM mi_models WHERE slug='claude-opus-5-5'").fetchone()[0] == 0
    assert db.execute("SELECT status FROM mi_models WHERE slug='claude-opus-5'").fetchone()[0] == "active"


def test_apply_registers_the_model_aliases_it_and_retires_the_old_release(db):
    receipt = rn.apply(db, _plan(db))
    assert {r["slug"] for r in receipt["registered"]} == {"claude-opus-5-5", "gpt-6.1-luna",
                                                          "gemini-3.8-flash", "grok-4.7"}
    new = db.execute("SELECT model_id, family, status, supersedes_model_id, active_release"
                     " FROM mi_models WHERE slug='claude-opus-5-5'").fetchone()
    assert (new["family"], new["status"], new["active_release"]) == ("anthropic", "active", "5.5")
    assert new["supersedes_model_id"] == 11
    old = db.execute("SELECT status FROM mi_models WHERE slug='claude-opus-5'").fetchone()
    assert old["status"] == "retired"
    assert receipt["aliased"] == 4


def test_apply_claims_the_staged_deployment_instead_of_inserting_a_second_row(db):
    rn.apply(db, _plan(db))
    rows = db.execute("SELECT model_id, status, verified_at FROM mi_deployments"
                      " WHERE served_id='claude-opus-5-5'").fetchall()
    assert len(rows) == 1                      # uq_mi_deployment would have thrown anyway
    assert rows[0]["status"] == "active" and rows[0]["verified_at"]
    assert rows[0]["model_id"] == db.execute(
        "SELECT model_id FROM mi_models WHERE slug='claude-opus-5-5'").fetchone()[0]


def test_apply_retires_the_old_deployments(db):
    rn.apply(db, _plan(db))
    assert db.execute("SELECT status FROM mi_deployments WHERE served_id='claude-opus-5'"
                      ).fetchone()[0] == "retired"


def test_the_new_model_is_what_the_leaderboard_makes_visible(db):
    """The operator-facing outcome: the registry now offers 5.5, not 5."""
    from src.api.model_ratings import visible_models
    rn.apply(db, _plan(db))
    visible = visible_models(db)
    assert "claude-opus-5-5" in visible and "claude-opus-5" not in visible
    assert "grok-4.7" in visible and "grok-4.5" not in visible
    assert "kimi-k3" in visible                # untouched line stays visible


def test_apply_is_idempotent(db):
    rn.apply(db, _plan(db))
    before = db.execute("SELECT COUNT(*) FROM mi_models").fetchone()[0]
    receipt = rn.apply(db, rn.plan(db, ARENA))
    assert receipt["registered"] == [] and receipt["retired"] == []
    assert db.execute("SELECT COUNT(*) FROM mi_models").fetchone()[0] == before


def test_an_alias_that_already_exists_is_repointed_not_duplicated(db):
    db.execute("INSERT INTO mi_model_names (model_id, source_id, name) VALUES (11,3,'Claude Opus 5.5')")
    rn.apply(db, _plan(db))
    rows = db.execute("SELECT model_id FROM mi_model_names WHERE source_id=3"
                      " AND name='Claude Opus 5.5'").fetchall()
    assert len(rows) == 1
    assert rows[0]["model_id"] == db.execute(
        "SELECT model_id FROM mi_models WHERE slug='claude-opus-5-5'").fetchone()[0]


def test_a_missing_lmarena_source_is_an_error_not_a_silent_skip(db):
    db.execute("DELETE FROM mi_sources WHERE slug='lmarena'")
    with pytest.raises(RuntimeError):
        rn.apply(db, _plan(db))


def test_undo_sql_restores_the_previous_state(db):
    p = _plan(db)
    undo = rn.undo_sql(p)
    rn.apply(db, p)
    for stmt in [s for s in undo.splitlines() if s.strip() and not s.startswith("--")]:
        db.execute(stmt)
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM mi_models WHERE slug='claude-opus-5-5'"
                      ).fetchone()[0] == 0
    assert db.execute("SELECT status FROM mi_models WHERE slug='claude-opus-5'"
                      ).fetchone()[0] == "active"
    assert db.execute("SELECT status, model_id FROM mi_deployments WHERE"
                      " served_id='claude-opus-5-5'").fetchone()[:] == ("new", None)
    assert db.execute("SELECT COUNT(*) FROM mi_model_names WHERE source_id=3").fetchone()[0] == 0
    from src.api.model_ratings import visible_models
    assert "claude-opus-5" in visible_models(db)


def test_undo_restores_the_alias_table_and_the_deployments(db):
    """A second look at the undo: every table it touched is back."""
    p = _plan(db)
    undo = rn.undo_sql(p)
    rn.apply(db, p)
    for stmt in [s for s in undo.splitlines() if s.strip() and not s.startswith("--")]:
        db.execute(stmt)
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM mi_deployments WHERE status='active'"
                      ).fetchone()[0] == len([c for c in CATALOG if c[3] == "active"])
    assert db.execute("SELECT COUNT(*) FROM mi_deployments WHERE status='new'"
                      ).fetchone()[0] == len([c for c in CATALOG if c[3] == "new"])
