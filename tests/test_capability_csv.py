"""CSV import into the capability matrix — design build step 4.

Pinned here, because an importer that half-works is worse than one that refuses:

* **Registered models only.** A row naming a model the registry does not confirm is
  SKIPPED, never written. Writing it would put a score on a model that every read path
  is forbidden to display — the operator's own rule, and the defect he caught on the
  rendered page on 2026-11-17 ("Can we only have the models that are registered").
* **The eight routing axes only.** An unknown ``task_type`` is an ERROR and the file
  writes NOTHING. The router reads those keys and nothing else, so a typo would create
  a score nobody consults while the matrix looks complete.
* **Imported rows are their own source.** An import must not overwrite benchmark scores
  or hand-entered ones, and neither may overwrite it.
"""
from __future__ import annotations

import sqlite3

import pytest

from src.api import capability_csv as cc

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
CREATE TABLE model_capabilities (
    id INTEGER PRIMARY KEY, model TEXT NOT NULL, task_type TEXT NOT NULL,
    score REAL NOT NULL, source TEXT, benchmark_category TEXT, raw_score REAL,
    updated_at TEXT, release_label TEXT);
"""

MODELS = [
    (1, "glm-5.3-flash", "glm", "active"),
    (2, "claude-opus-5", "claude-opus", "active"),
    (3, "ox-alpha-free", "ox", "retired"),      # retired → not registered
    (5, "poll-staged-only", "vendored", "active"),   # only a poll 'new' row
    (6, "provider-silent", "vendored", "active"),    # only a retired deployment
]
DEPLOYMENTS = [
    (1, 1, 1, "served-1", "active"),
    (2, 2, 1, "served-2", "active"),
    (3, 3, 1, "served-3", "active"),
    (None, 5, 1, "staged-unknown", "new"),      # the poll staged a name it cannot place
    (5, 6, 1, "served-5", "new"),               # ... and this model exists ONLY as staging
    (6, 7, 1, "served-6", "retired"),           # the provider stopped serving it
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
    con.commit()
    return con


def _rows(con, model=None):
    sql = "SELECT model, task_type, score, source FROM model_capabilities"
    args = ()
    if model:
        sql += " WHERE model = ?"
        args = (model,)
    return [tuple(r) for r in con.execute(sql, args)]


# ── the header ───────────────────────────────────────────────────────────────

def test_a_file_with_no_header_is_refused(db):
    with pytest.raises(ValueError):
        cc.import_capabilities(db, "")


def test_the_missing_column_is_NAMED(db):
    """'invalid CSV' sends the operator hunting; the column name does not."""
    with pytest.raises(ValueError) as ei:
        cc.import_capabilities(db, "model,score\nglm-5.3-flash,0.9\n")
    assert "task_type" in str(ei.value)


def test_the_three_column_aliases_are_accepted(db):
    out = cc.import_capabilities(
        db, "model_slug,axis,value\nglm-5.3-flash,code_generation,0.9\n")
    assert out["written"] == 1 and not out["aborted"]
    assert _rows(db) == [("glm-5.3-flash", "code_generation", 0.9, "import")]


def test_column_order_is_free(db):
    out = cc.import_capabilities(
        db, "score,task_type,model\n0.9,planning,glm-5.3-flash\n")
    assert out["written"] == 1
    assert _rows(db) == [("glm-5.3-flash", "planning", 0.9, "import")]


# ── scores ───────────────────────────────────────────────────────────────────

def test_both_score_scales_land_on_the_same_value(db):
    """0-100 and 0-1 are both accepted, as in the manual editor."""
    cc.import_capabilities(db, "model,task_type,score\n"
                               "glm-5.3-flash,planning,87\n"
                               "claude-opus-5,planning,0.87\n")
    got = dict((r[0], r[2]) for r in _rows(db))
    assert got["glm-5.3-flash"] == 0.87
    assert got["claude-opus-5"] == 0.87


def test_a_non_numeric_score_is_an_error_and_writes_nothing(db):
    out = cc.import_capabilities(db, "model,task_type,score\n"
                                     "glm-5.3-flash,planning,high\n")
    assert out["aborted"] and out["written"] == 0
    assert _rows(db) == []
    assert any("not a number" in e for e in out["errors"])


# ── the axes: the router's vocabulary, and only it ───────────────────────────

def test_an_unknown_axis_is_an_error_not_a_skipped_row(db):
    """The router reads the eight axes; a ninth would be a score nobody consults."""
    out = cc.import_capabilities(db, "model,task_type,score\n"
                                     "glm-5.3-flash,code_generation,0.9\n"
                                     "glm-5.3-flash,code_generaton,0.9\n")   # typo
    assert out["aborted"], "a typo'd axis must stop the import, not land silently"
    assert _rows(db) == [], "the good row must not land either — the file is refused whole"


def test_the_eight_routing_axes_are_the_accepted_vocabulary(db):
    axes = cc.routing_axes()
    assert set(axes) == {"agentic_multi_step", "code_generation", "debugging",
                         "planning", "reasoning_chain", "research_deep",
                         "unit_tests", "casual_chat"}


# ── registration: the rule the whole estate reads by ─────────────────────────

def test_a_model_a_poll_only_STAGED_is_skipped(db):
    """`mi_deployments.status='new'` is the poll staging an id it cannot place. It is
    not a registration, so a score must not be written for it."""
    out = cc.import_capabilities(db, "model,task_type,score\n"
                                     "poll-staged-only,planning,0.9\n")
    assert out["written"] == 0
    assert _rows(db) == []
    assert any("poll-staged-only" in s for s in out["skipped"])


def test_a_model_only_a_retired_deployment_covers_is_skipped(db):
    out = cc.import_capabilities(db, "model,task_type,score\n"
                                     "provider-silent,planning,0.9\n")
    assert out["written"] == 0 and _rows(db) == []


def test_a_retired_model_is_skipped(db):
    out = cc.import_capabilities(db, "model,task_type,score\n"
                                     "ox-alpha-free,planning,0.9\n")
    assert out["written"] == 0 and _rows(db) == []


def test_a_registered_model_is_written(db):
    out = cc.import_capabilities(db, "model,task_type,score\n"
                                     "glm-5.3-flash,code_generation,0.9\n")
    assert out["written"] == 1 and out["models"] == ["glm-5.3-flash"]
    assert out["axes"] == ["code_generation"]


def test_a_skip_does_not_stop_the_registered_rows(db):
    """Mixed file: the registered row lands, the unregistered one is reported."""
    out = cc.import_capabilities(db, "model,task_type,score\n"
                                     "glm-5.3-flash,planning,0.9\n"
                                     "poll-staged-only,planning,0.9\n")
    assert out["written"] == 1 and len(out["skipped"]) == 1
    assert _rows(db) == [("glm-5.3-flash", "planning", 0.9, "import")]


# ── sources: an import is its own, and overwrites nothing else ───────────────

def test_an_import_never_overwrites_a_benchmark_or_manual_score(db):
    db.execute("INSERT INTO model_capabilities (model, task_type, score, source)"
               " VALUES ('glm-5.3-flash', 'planning', 0.5, 'livebench')")
    db.execute("INSERT INTO model_capabilities (model, task_type, score, source)"
               " VALUES ('glm-5.3-flash', 'planning', 0.6, 'manual')")
    db.commit()
    cc.import_capabilities(db, "model,task_type,score\nglm-5.3-flash,planning,0.9\n")
    sources = sorted((r[3], r[2]) for r in _rows(db))
    assert sources == [("import", 0.9), ("livebench", 0.5), ("manual", 0.6)]


def test_re_importing_the_same_release_updates_rather_than_duplicates(db):
    csv_text = "model,task_type,score,release\nglm-5.3-flash,planning,0.9,2026-11-17\n"
    cc.import_capabilities(db, csv_text)
    cc.import_capabilities(db, csv_text.replace("0.9", "0.95"))
    assert _rows(db) == [("glm-5.3-flash", "planning", 0.95, "import")]


def test_a_model_cell_is_never_re_spelled(db):
    """The identity guard, applied to the importer.

    `tests/test_model_identity_guard.py` forbids stripping/lowercasing model strings
    outside model_identity.py, and it caught this importer doing exactly that. The guard
    is right: if " glm-5.3-flash" were stripped it would MATCH the registered
    `glm-5.3-flash`, so a spelling difference in the file would be silently reinterpreted
    as an identity. It must instead be skipped, and named in the report.
    """
    out = cc.import_capabilities(db, "model,task_type,score\n"
                                     " glm-5.3-flash,planning,0.9\n")   # leading space
    assert out["written"] == 0, "a whitespace variant must not be accepted as the model"
    assert _rows(db) == []
    assert any("not a registered model" in s for s in out["skipped"])


def test_a_model_cell_is_matched_case_sensitively(db):
    """Same rule, the other direction: GLM-5.3-Flash is a different spelling, not a match."""
    out = cc.import_capabilities(db, "model,task_type,score\n"
                                     "GLM-5.3-Flash,planning,0.9\n")
    assert out["written"] == 0 and _rows(db) == []


# ── the template ─────────────────────────────────────────────────────────────

def test_the_template_has_the_header_and_a_worked_example(db):
    t = cc.template()
    assert t.splitlines()[0] == "model,task_type,score,release"
    assert len(t.strip().splitlines()) == 3


def test_blank_lines_are_not_errors(db):
    out = cc.import_capabilities(db, "model,task_type,score\n"
                                     "\n"
                                     "glm-5.3-flash,planning,0.9\n"
                                     "\n")
    assert out["written"] == 1 and not out["errors"] and not out["aborted"]


# ── provenance: an import can declare a real source, and a bad one aborts ──────

def test_a_file_can_declare_its_real_source(db):
    """Found 2026-11-19 importing published LiveBench numbers: the importer wrote
    `source='import'`, which is NOT in `GRADING_SOURCES`, so the grading view DROPS those
    rows — the operator imports a file and the matrix does not change, with nothing
    saying why. Published-leaderboard values ARE `livebench`, and that is also what makes
    them visible and gradeable, so the file can name it."""
    con = db
    text = ("model,task_type,score,release,source\n"
            "glm-5.3-flash,code_generation,79.0,2026-06-25,livebench\n")
    rep = cc.import_capabilities(con, text)
    assert rep["written"] == 1 and not rep["aborted"], rep
    assert rep["sources"] == ["livebench"]
    src = con.execute("select source from model_capabilities where model='glm-5.3-flash'"
                      " and task_type='code_generation'").fetchone()[0]
    assert src == "livebench", "the declared source was not written"


def test_a_source_the_grading_view_would_drop_aborts_the_file(db):
    """Same rule as an unknown axis: a typo must not produce rows that exist and are
    invisible. Writing them would make the operator believe the import landed."""
    con = db
    text = ("model,task_type,score,source\n"
            "glm-5.3-flash,code_generation,79.0,livebnech\n")
    rep = cc.import_capabilities(con, text)
    assert rep["aborted"] and rep["written"] == 0
    assert any("livebnech" in e and "invisible" in e for e in rep["errors"]), rep["errors"]
    assert con.execute("select count(*) from model_capabilities").fetchone()[0] == 0


def test_without_a_source_column_nothing_changes(db):
    """The default stays `import`: an import must not silently start clobbering the
    benchmark rows or the hand-entered ones just because a column became optional."""
    con = db
    rep = cc.import_capabilities(con, "model,task_type,score\n"
                                      "glm-5.3-flash,code_generation,79.0\n")
    assert rep["written"] == 1 and rep["sources"] == ["import"]
    assert con.execute("select source from model_capabilities").fetchone()[0] == "import"


def test_the_source_column_takes_aliases_and_only_grading_sources():
    """`provenance` is accepted as the header, and the allowlist is the real one — read
    from GRADING_SOURCES rather than a copy that can drift from it."""
    assert "provenance" in cc.SOURCE_COLS
    allowed = cc.allowed_sources()
    assert "livebench" in allowed and "manual" in allowed and "lcp_benchmark" in allowed
    # 'import' is NOT a grading source — that is exactly why the column exists.
    assert "import" not in allowed, (
        "if 'import' ever becomes a grading source this whole dance is unnecessary")
    # ...but it is still accepted as the default, or no source-less file could import.
    assert "import" in tuple(allowed) + (cc.SOURCE,)
