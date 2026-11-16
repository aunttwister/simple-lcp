"""Tests for the leaderboard store, the poll, and the taxonomy namespace.

The operator's decisions of 2026-11-15, pinned here so a regression cannot ship:

* a model is VISIBLE only if a provider registers it AND it is not retired;
* Elo (text boards) and net improvement (agent boards) carry their own unit and
  are never ranked against each other;
* effort is a DIMENSION (R2=C), not a second model;
* operator-defined taxonomies are `custom:<slug>`, and a poll can never write one
  (nor an import write an arena board) — the separation is structural;
* a poll STAGES: the published baseline changes only when rows are accepted;
* re-polling a model with no effort suffix must REFRESH its row, not duplicate it
  (SQLite treats NULLs as distinct in a unique index — the 038 defect fixed in 039).
"""
from __future__ import annotations

import sqlite3

import pytest

from src.api import arena_poll, model_ratings as mr

SCHEMA = """
CREATE TABLE mi_models (
    model_id INTEGER PRIMARY KEY, slug TEXT, display_name TEXT, family TEXT,
    status TEXT, supersedes_model_id INTEGER, benchmark_key TEXT, created_at TEXT,
    active_release TEXT, benchmark_release TEXT, quantization TEXT);
CREATE TABLE mi_deployments (
    deployment_id INTEGER PRIMARY KEY, endpoint_id INTEGER, model_id INTEGER,
    served_id TEXT, variant TEXT, quantisation TEXT, context_window INTEGER,
    status TEXT, source TEXT, verified_at TEXT, first_seen TEXT, last_seen TEXT,
    notes TEXT, quant_id INTEGER);
CREATE TABLE mi_model_names (
    id INTEGER PRIMARY KEY, model_id INTEGER, source_id INTEGER, name TEXT,
    is_primary INTEGER, confidence TEXT, evidence TEXT, valid_from TEXT,
    valid_to TEXT, first_seen TEXT, last_seen TEXT, verified_at TEXT, note TEXT);
CREATE TABLE model_ratings (
    id INTEGER PRIMARY KEY, model_slug TEXT NOT NULL, taxonomy TEXT NOT NULL,
    value REAL NOT NULL, unit TEXT NOT NULL, percentile REAL, rank INTEGER,
    n INTEGER, source TEXT NOT NULL, board TEXT, lab TEXT, effort TEXT,
    basis TEXT, published TEXT, first_seen TEXT, updated_at TEXT NOT NULL,
    UNIQUE (model_slug, taxonomy, source, board, effort));
CREATE TABLE rating_taxonomies (
    taxonomy TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT, unit TEXT,
    note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE arena_boards (
    board TEXT PRIMARY KEY, family TEXT NOT NULL, unit TEXT NOT NULL,
    rows INTEGER NOT NULL DEFAULT 0, models_matched INTEGER NOT NULL DEFAULT 0,
    published TEXT, first_seen TEXT NOT NULL, last_polled_at TEXT NOT NULL);
CREATE TABLE arena_board_rows (
    id INTEGER PRIMARY KEY, board TEXT NOT NULL, model_slug TEXT NOT NULL,
    value REAL NOT NULL, unit TEXT NOT NULL, rank INTEGER, n INTEGER, lab TEXT,
    effort TEXT NOT NULL DEFAULT '', published TEXT, polled_at TEXT NOT NULL,
    UNIQUE (board, model_slug, effort));
CREATE TABLE taxonomy_map (
    id INTEGER PRIMARY KEY, taxonomy TEXT NOT NULL, board TEXT NOT NULL,
    note TEXT, is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (taxonomy, board));
CREATE TABLE rating_poll_staging (
    id INTEGER PRIMARY KEY, poll_id TEXT NOT NULL, model_slug TEXT NOT NULL,
    taxonomy TEXT NOT NULL, unit TEXT NOT NULL, value REAL NOT NULL,
    old_value REAL, percentile REAL, rank INTEGER, n INTEGER,
    source TEXT NOT NULL, board TEXT NOT NULL DEFAULT '',
    lab TEXT, effort TEXT NOT NULL DEFAULT '', basis TEXT, published TEXT,
    old_published TEXT, old_updated_at TEXT, polled_at TEXT NOT NULL,
    UNIQUE (model_slug, taxonomy, unit, board, effort));
"""

MODELS = [
    # model_id, slug, family, status
    (1, "glm-5.3-flash", "glm", "active"),
    (2, "claude-opus-5", "claude-opus", "active"),
    (3, "ox-alpha-free", "ox", "retired"),
    (4, "qwen3.8-27b-heretic", "qwen", "active"),
    (5, "poll-staged-only", "vendored", "active"),      # only a poll 'new' row
    (6, "provider-silent", "vendored", "active"),       # only a 'retired' deployment
]
# (model_id, deployment_id, endpoint_id, served_id, status)
DEPLOYMENTS = [
    (1, 1, 1, "served-1", "active"),
    (2, 2, 1, "served-2", "active"),
    (3, 3, 1, "served-3", "active"),          # a RETIRED model with a live deployment
    (4, 4, 1, "served-4", "active"),
    (None, 5, 1, "staged-unknown", "new"),    # the poll staged a name it cannot place
    (5, 6, 1, "served-5", "new"),             # ... and this model exists ONLY as staging
    (6, 7, 1, "served-6", "retired"),         # the provider stopped serving it
    (2, 8, 1, "served-2-legacy", "retired"),  # model 2 also carries an old retired row
]
NAMES = [
    # model_id, name — an operator-declared alias that must WIN over spelling
    (4, "Qwen3.8-27B"),
]


@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for mid, slug, family, status in MODELS:
        con.execute("INSERT INTO mi_models (model_id, slug, family, status)"
                    " VALUES (?,?,?,?)", (mid, slug, family, status))
    for mid, dep, ep, served, status in DEPLOYMENTS:
        con.execute("INSERT INTO mi_deployments (deployment_id, model_id, endpoint_id,"
                    " served_id, status) VALUES (?,?,?,?,?)", (dep, mid, ep, served, status))
    for mid, name in NAMES:
        con.execute("INSERT INTO mi_model_names (model_id, name) VALUES (?,?)",
                    (mid, name))
    con.commit()
    return con


# ── visibility ──────────────────────────────────────────────────────────────

def test_retired_model_is_not_visible(db):
    """R3: a retired model still carries a deployment row and must stay out."""
    visible = mr.visible_models(db)
    assert "ox-alpha-free" not in visible
    assert {"glm-5.3-flash", "claude-opus-5"} <= visible


def test_a_poll_staged_deployment_is_not_a_registration(db):
    """The defect the operator caught on the rendered page.

    `mi_deployments.status='new'` is the model-intel poll STAGING a served id it
    does not recognise. The registry refuses to read it as a fact (model_identity
    reads only 'active'), and the leaderboard must refuse too — otherwise the poll
    widens its own eligibility window: it stages a name, and the next poll
    publishes that name as one of our models.
    """
    assert "poll-staged-only" not in mr.visible_models(db)


def test_a_deployment_the_provider_retired_is_not_a_registration(db):
    """A model whose only deployment is retired is not served any more."""
    assert "provider-silent" not in mr.visible_models(db)


def test_a_confirmed_deployment_registers_even_with_old_retired_rows(db):
    """A live row wins over the model's own retired history."""
    assert "claude-opus-5" in mr.visible_models(db)


def test_the_poll_cannot_publish_a_model_it_staged_itself(db):
    """End to end: the staging row must not let an arena name through."""
    visible = mr.visible_models(db)
    out = arena_poll.build_rows(db, {"coding": [
        {"model_name": "poll-staged-only", "rating": 1600.0, "rank": 1, "vote_count": 99,
         "published": "2026-11-11"},
        {"model_name": "glm-5.3-flash", "rating": 1500.0, "rank": 2, "vote_count": 90,
         "published": "2026-11-11"}]}, visible=visible)
    stored = {r["model_slug"] for r in out["rows"]}
    assert stored == {"glm-5.3-flash"}
    # the name resolved to a real slug — it is refused because that slug is not
    # REGISTERED, not because the poll could not spell it
    assert out["stats"]["coding"]["not_registered"] == 1
    assert out["stats"]["coding"]["matched"] == 1


def test_unregistered_model_is_rejected(db):
    out = mr.upsert_ratings(db, [dict(model_slug="nobody-home", taxonomy="coding",
                                      value=1500.0, unit="elo")])
    assert out == {"written": 0, "skipped_unregistered": 1, "rejected_namespace": 0}


# ── the display guarantee: registration is enforced on READ too ──────────────

def test_the_leaderboard_stops_displaying_a_model_whose_registration_went_away(db):
    """A write gate is not enough — a stored row outlives the registration."""
    mr.upsert_ratings(db, [dict(model_slug="glm-5.3-flash", taxonomy="code_generation",
                                value=1500.0, unit="elo", board="coding", rank=1, n=1)])
    assert [r["model_slug"] for r in mr.leaderboard(db, "code_generation")] == ["glm-5.3-flash"]
    db.execute("UPDATE mi_deployments SET status='retired' WHERE model_id=1")
    assert mr.leaderboard(db, "code_generation") == []


def test_a_stored_row_for_a_poll_staged_model_is_never_displayed(db):
    """The row can exist (an earlier poll wrote it); the page must not show it."""
    db.execute("INSERT INTO model_ratings (model_slug, taxonomy, value, unit, source,"
               " board, effort, updated_at) VALUES ('poll-staged-only','code_generation',"
               "1700.0,'elo','arena','coding','','2026-11-15T00:00:00Z')")
    assert mr.leaderboard(db, "code_generation") == []
    assert all(t["rows"] == 0 for t in mr.taxonomies(db)
               if t["taxonomy"] == "code_generation")


def test_the_poll_panel_does_not_offer_a_row_for_an_unregistered_model(db):
    """Accepting it would publish a rating for something no provider serves."""
    db.execute("INSERT INTO rating_poll_staging (poll_id, model_slug, taxonomy, unit,"
               " value, source, board, effort, polled_at) VALUES ('p','poll-staged-only',"
               "'code_generation','elo',1700.0,'arena','coding','','2026-11-15T00:00:00Z')")
    assert mr.staged_rows(db) == []
    assert mr.commit_staged(db)["written"] == 0


# ── the uniqueness the schema declares ──────────────────────────────────────

def test_repeated_upsert_refreshes_instead_of_duplicating(db):
    """The 038 defect: a NULL effort never matched its own ON CONFLICT."""
    row = dict(model_slug="glm-5.3-flash", taxonomy="code_generation", value=1500.0,
               unit="elo", board="coding", effort=None)
    mr.upsert_ratings(db, [row])
    mr.upsert_ratings(db, [dict(row, value=1510.0)])
    rows = db.execute("SELECT value FROM model_ratings WHERE model_slug='glm-5.3-flash'"
                      " AND taxonomy='code_generation'").fetchall()
    assert [r[0] for r in rows] == [1510.0]


# ── units never share a ranking ─────────────────────────────────────────────

def test_leaderboard_keeps_units_apart(db):
    mr.upsert_ratings(db, [
        dict(model_slug="glm-5.3-flash", taxonomy="debugging", value=1509.0,
             unit="elo", board="coding", rank=1, n=2),
        dict(model_slug="glm-5.3-flash", taxonomy="debugging", value=0.0134,
             unit="net_improvement", board="agent_bash_reliability", rank=30, n=59),
        dict(model_slug="claude-opus-5", taxonomy="debugging", value=1520.0,
             unit="elo", board="coding", rank=2, n=2),
    ])
    rows = mr.leaderboard(db, "debugging")
    elo = [r for r in rows if r["unit"] == "elo"]
    net = [r for r in rows if r["unit"] == "net_improvement"]
    assert [r["model_slug"] for r in elo] == ["claude-opus-5", "glm-5.3-flash"]
    assert len(net) == 1                     # never ranked beside the Elo rows
    assert elo[0]["percentile"] == 100.0
    # the group order is stable across identical calls (it used to come from a set)
    assert [r["unit"] for r in mr.leaderboard(db, "debugging")] == \
           [r["unit"] for r in mr.leaderboard(db, "debugging")]


def test_diff_reports_both_dates(db):
    mr.upsert_ratings(db, [dict(model_slug="glm-5.3-flash", taxonomy="code_generation",
                                value=1500.0, unit="elo", board="coding",
                                published="2026-11-01")])
    changes = mr.diff(db, "code_generation", [
        dict(model_slug="glm-5.3-flash", taxonomy="code_generation", value=1512.0,
             unit="elo", board="coding", published="2026-11-11"),
        dict(model_slug="claude-opus-5", taxonomy="code_generation", value=1532.0,
             unit="elo", board="coding", published="2026-11-11"),
    ])
    first = [c for c in changes if c["model_slug"] == "glm-5.3-flash"][0]
    assert (first["old"], first["new"], first["changed"]) == (1500.0, 1512.0, True)
    assert first["old_date"] and first["new_date"] == "2026-11-11"
    newrow = [c for c in changes if c["model_slug"] == "claude-opus-5"][0]
    assert newrow["old"] is None and newrow["changed"] is True


# ── the namespace ───────────────────────────────────────────────────────────

def test_custom_taxonomy_is_namespaced():
    assert mr.custom_taxonomy("My Coding") == "custom:my-coding"
    assert mr.custom_taxonomy("custom:Coding") == "custom:coding"
    assert mr.custom_taxonomy("coding") == "custom:coding"    # arena axis stays separate


def test_poll_cannot_write_a_custom_board(db):
    out = mr.upsert_ratings(db, [dict(model_slug="glm-5.3-flash",
                                      taxonomy="custom:mine", value=1600.0,
                                      unit="elo", source="arena", board="coding")])
    assert out["rejected_namespace"] == 1 and out["written"] == 0


def test_custom_row_cannot_claim_to_be_arena(db):
    out = mr.upsert_ratings(db, [dict(model_slug="glm-5.3-flash", taxonomy="coding",
                                      value=1600.0, unit="elo", source="custom")])
    assert out["rejected_namespace"] == 1 and out["written"] == 0


def test_define_taxonomy_makes_an_empty_tab(db):
    mr.define_taxonomy(db, "Pavle's axis", label="Pavle's axis")
    tabs = mr.taxonomies(db)
    assert [t["taxonomy"] for t in tabs] == ["custom:pavles-axis"]
    assert tabs[0]["kind"] == "custom" and tabs[0]["rows"] == 0


def test_import_csv_writes_custom_rows_and_reports_bad_lines(db):
    csv_text = ("model_slug,value,taxonomy,lab\n"
                "glm-5.3-flash,1520,Mine,zai\n"
                "claude-opus-5,1540,Mine,anthropic\n"
                "not-registered,1500,Mine,someone\n"
                "glm-5.3-flash,not-a-number,Mine,zai\n")
    out = mr.import_csv(db, csv_text)
    assert out["written"] == 2
    assert out["skipped_unregistered"] == 1
    assert len(out["errors"]) == 1 and "not numeric" in out["errors"][0]["error"]
    rows = mr.leaderboard(db, "custom:mine")
    assert [r["model_slug"] for r in rows] == ["claude-opus-5", "glm-5.3-flash"]
    assert all(r["source"] == "custom" for r in rows)


def test_import_csv_requires_a_taxonomy(db):
    out = mr.import_csv(db, "model_slug,value\nglm-5.3-flash,1500\n")
    assert out["written"] == 0 and out["errors"]


# ── the poll: resolution + staging ──────────────────────────────────────────

BOARDS = {
    "coding": [
        {"model_name": "GLM 5.3 Flash", "organization": "zai", "rating": 1509.4,
         "rank": 87, "vote_count": 100, "published": "2026-11-11"},
        {"model_name": "Claude Opus 5 (Max)", "organization": "anthropic",
         "rating": 1532.0, "rank": 1, "vote_count": 200, "published": "2026-11-11"},
        {"model_name": "Nobody Known", "organization": "x", "rating": 1400.0,
         "rank": 300, "vote_count": 3, "published": "2026-11-11"},
    ],
    "agent_tool_hallucination": [
        {"model_name": "GLM-5.3-Flash", "organization": "zai", "score": 0.0099,
         "rank": 2, "session_count": 61020, "published": "2026-11-11"},
    ],
}


def test_poll_resolves_by_alias_and_effort_and_ignores_the_rest(db):
    built = arena_poll.build_rows(db, BOARDS)
    keys = sorted((r["model_slug"], r["taxonomy"], r["unit"]) for r in built["rows"])
    assert ("claude-opus-5", "code_generation", "elo") in keys
    assert ("glm-5.3-flash", "code_generation", "elo") in keys
    # R2=C: the effort suffix is a dimension, not another model
    opus = [r for r in built["rows"] if r["model_slug"] == "claude-opus-5"][0]
    assert opus["effort"] == "max"
    # R3: an unknown name is ignored, and reported rather than guessed at
    assert built["unmatched"]["coding"] == ["Nobody Known"]
    assert all(r["model_slug"] != "Nobody Known" for r in built["rows"])


def test_poll_uses_operator_alias_over_slug_spelling(db):
    boards = {"coding": [{"model_name": "Qwen3.8-27B", "organization": "alibaba",
                          "rating": 1479.12, "rank": 2, "vote_count": 10,
                          "published": "2026-11-11"}]}
    built = arena_poll.build_rows(db, boards)
    assert {r["model_slug"] for r in built["rows"]} == {"qwen3.8-27b-heretic"}


def test_polled_rows_carry_the_board_publish_date(db):
    """A value without its date is not reviewable — the diff shows both."""
    built = arena_poll.build_rows(db, BOARDS)
    assert {r["published"] for r in built["rows"]} == {"2026-11-11"}
    mr.stage_poll(db, fetch=lambda: BOARDS)
    rows = mr.staged_rows(db)
    assert all(r["new_date"] == "2026-11-11" for r in rows)


def test_stage_then_commit_changes_the_baseline_only_on_accept(db):
    staged = mr.stage_poll(db, fetch=lambda: BOARDS)
    assert staged["staged"] > 0
    assert mr.leaderboard(db, "code_generation") == []      # nothing published yet
    assert len(mr.staged_rows(db)) == staged["staged"]

    first = mr.staged_rows(db)[0]
    out = mr.commit_staged(db, [{"model_slug": first["model_slug"],
                                 "taxonomy": first["taxonomy"],
                                 "unit": first["unit"], "board": first["board"],
                                 "effort": first["effort"]}])
    assert out["written"] == 1
    assert out["remaining"] == staged["staged"] - 1
    assert len(mr.leaderboard(db, first["taxonomy"])) == 1


def test_clear_staged_discards_without_touching_the_baseline(db):
    staged = mr.stage_poll(db, fetch=lambda: BOARDS)
    before = db.execute("SELECT count(*) FROM model_ratings").fetchone()[0]
    assert mr.clear_staged(db)["cleared"] == staged["staged"]
    assert db.execute("SELECT count(*) FROM model_ratings").fetchone()[0] == before
    assert mr.staged_rows(db) == []


def test_staging_records_the_baseline_it_replaces(db):
    mr.upsert_ratings(db, [dict(model_slug="glm-5.3-flash", taxonomy="code_generation",
                                value=1500.0, unit="elo", board="coding",
                                published="2026-11-01")])
    mr.stage_poll(db, fetch=lambda: BOARDS)
    row = [r for r in mr.staged_rows(db)
           if r["model_slug"] == "glm-5.3-flash"
           and r["unit"] == "elo" and r["taxonomy"] == "code_generation"][0]
    assert row["old_value"] == 1500.0
    assert row["old_date"] == "2026-11-01"
    assert row["new_date"] != row["old_date"] or row["new_date"] is None
    assert row["changed"] is True


# ── release watch ───────────────────────────────────────────────────────────

def test_release_candidates_group_by_line(db):
    boards = {"coding": [
        {"model_name": "Claude Opus 5.5 (High)", "organization": "anthropic",
         "rating": 1540.0, "rank": 1},
        {"model_name": "Claude Opus 4.6 (High)", "organization": "anthropic",
         "rating": 1500.0, "rank": 9},
        {"model_name": "Some Unrelated Model", "organization": "x", "rating": 1400.0,
         "rank": 200},
    ]}
    report = arena_poll.release_candidates(db, boards)
    names = [c["arena_name"] for c in report["candidates"]]
    assert set(names) == {"Claude Opus 5.5 (High)", "Claude Opus 4.6 (High)"}
    assert names[0] == "Claude Opus 5.5 (High)"        # newer first
    assert report["candidates"][0]["newer"] is True
    assert report["candidates"][1]["newer"] is False   # an older release is not an add
    assert report["newer_count"] == 1
    assert report["candidates"][0]["registered_same_line"] == ["claude-opus-5"]
    assert "ox-alpha-free" not in report["not_on_any_board"]   # retired, not "missing"
    assert "glm-5.3-flash" in report["not_on_any_board"]


def test_is_newer_reads_the_release_number():
    assert arena_poll.is_newer("Claude Opus 5.5 (High)", "claude-opus-5") is True
    assert arena_poll.is_newer("Claude Opus 4.6", "claude-opus-5") is False
    assert arena_poll.is_newer("Gemini 3.8 Flash (High)", "gemini-3.6-flash") is True
    assert arena_poll.is_newer("GLM 5.3 Flash", "glm-5.3-flash") is False
    assert arena_poll.is_newer("Mystery Model", "claude-opus-5") is None
    # a dated snapshot is not a later release of the model it snapshots
    assert arena_poll.is_newer("Grok 4 (0709)", "grok-4.5") is False
    assert arena_poll.is_newer("Grok 4 (0709)", "grok-4") is False
    assert arena_poll.is_newer("DeepSeek-V4.1-Flash-20261017 (Max)", "deepseek-flash") is None


def test_leaderboard_lists_one_row_per_model_per_unit(db):
    """Two boards feeding one axis must not list the same model twice."""
    mr.upsert_ratings(db, [
        dict(model_slug="glm-5.3-flash", taxonomy="agentic_multi_step", value=0.0134,
             unit="net_improvement", board="agent", rank=30, n=59),
        dict(model_slug="glm-5.3-flash", taxonomy="agentic_multi_step", value=0.0050,
             unit="net_improvement", board="agent_task_outcome_explicit", rank=45,
             n=59),
    ])
    rows = mr.leaderboard(db, "agentic_multi_step")
    assert len(rows) == 1
    assert rows[0]["board"] == "agent"                     # the winning row
    assert rows[0]["boards"] == ["agent", "agent_task_outcome_explicit"]


def test_line_key_drops_the_release_and_the_effort():
    assert arena_poll.line_key("Claude Opus 5.5 (High)") == "claude opus"
    assert arena_poll.line_key("claude-opus-5") == "claude opus"
    assert arena_poll.line_key("GLM-5.3-Flash") == "glm flash"
