"""Tests for the declared-capability seeder, the model registry and the
capability-matrix resolution the router depends on."""

import os
import sqlite3
import tempfile

import pytest

from src.api.seed_capabilities import (
    DEFAULT_MODEL_REGISTRY,
    DERIVED_TASKS,
    load_capability_matrix,
    load_model_registry,
    seed_capabilities,
    seed_model_registry,
)


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from src.api.models import get_engine, Base
    engine = get_engine(path)
    Base.metadata.create_all(engine)
    engine.dispose()
    # The model-name normalisation tables (mi_models / mi_sources / mi_model_names)
    # are owned by alembic 034, not the ORM Base — a migrated production DB has
    # them, so give the freshly-created test DB the same schema before seeding.
    from tests._model_names_fixtures import mi_schema
    mi_schema(sqlite3.connect(path)).close()
    yield path
    for ext in ("", "-wal", "-shm"):
        try:
            os.unlink(path + ext)
        except FileNotFoundError:
            pass


# ── seed_model_registry / cleanup ────────────────────────────────────────────

class TestSeedRegistry:
    def test_seed_and_sync(self, db_path):
        n = seed_model_registry(db_path)
        assert n == len(DEFAULT_MODEL_REGISTRY)
        # Insert-only: a second plain run finds every entry already present, so
        # there is nothing to write and it reports 0.
        n2 = seed_model_registry(db_path)
        assert n2 == 0
        # sync re-applies curated defaults onto the EXISTING mi_models rows +
        # provider names (benchmark_key, release fields, provider spellings) —
        # once per curated entry, so the count matches a fresh insert again.
        n3 = seed_model_registry(db_path, sync=True)
        assert n3 == len(DEFAULT_MODEL_REGISTRY)

        con = sqlite3.connect(db_path)
        try:
            # "re-applies" means UPDATE, not duplicate: still exactly one
            # mi_models row per logical name, and exactly one provider name row
            # per declared (model x provider) spelling — i.e. the same total the
            # curated defaults declare.
            assert con.execute("SELECT COUNT(*) FROM mi_models").fetchone()[0] == \
                len(DEFAULT_MODEL_REGISTRY)
            assert con.execute(
                "SELECT COUNT(*) FROM mi_model_names n JOIN mi_sources s"
                " ON s.source_id = n.source_id WHERE s.kind = 'provider_endpoint'"
            ).fetchone()[0] == sum(
                len(e.get("provider_mappings") or {}) for e in DEFAULT_MODEL_REGISTRY)

            # sync also re-applies the curated release pins. Clobber them, sync,
            # and the curated values are back (deepseek-v4-pro pins a release).
            con.execute("UPDATE mi_models SET active_release = NULL,"
                        " benchmark_release = NULL WHERE slug = 'deepseek-v4-pro'")
            con.commit()
        finally:
            con.close()

        seed_model_registry(db_path, sync=True)
        con = sqlite3.connect(db_path)
        try:
            row = con.execute(
                "SELECT active_release, benchmark_release FROM mi_models"
                " WHERE slug = 'deepseek-v4-pro'").fetchone()
        finally:
            con.close()
        assert row == ("2026-08-13", "2026-06-25")

    def test_load_registry_roundtrip(self, db_path):
        seed_model_registry(db_path)
        reg = load_model_registry(db_path)
        assert "deepseek-v4-pro" in reg
        assert reg["deepseek-v4-pro"]["benchmark_key"] == "deepseek-v4-pro"
        assert reg["deepseek-v4-pro"]["provider_mappings"]["commandcode"] == "deepseek/deepseek-v4-pro"


# ── load_capability_matrix resolution ────────────────────────────────────────

class TestCapabilityMatrix:
    def test_matrix_resolves_active_release(self, db_path):
        seed_model_registry(db_path)
        seed_capabilities(db_path)
        matrix = load_capability_matrix(db_path)
        # deepseek-v4-pro should be present under reasoning_chain.
        assert "reasoning_chain" in matrix
        assert "deepseek-v4-pro" in matrix["reasoning_chain"]
    def test_matrix_release_filter(self, db_path):
        seed_capabilities(db_path)
        matrix = load_capability_matrix(db_path, release="2026-06-25")
        assert "gpt-5.6-luna" in matrix.get("casual_chat", {})

    def test_matrix_includes_derived_coding_subskills(self, db_path):
        seed_model_registry(db_path)
        seed_capabilities(db_path)
        matrix = load_capability_matrix(db_path)
        # debugging + unit_tests are derived from code_generation (coding
        # subskills), so they must be present with the same per-model scores.
        for derived in ("debugging", "unit_tests"):
            assert derived in matrix
            assert matrix[derived]["deepseek-v4-pro"] == matrix["code_generation"]["deepseek-v4-pro"]


# ── derived tasks registry ───────────────────────────────────────────────────

def test_derived_tasks_registry():
    # debugging + unit_tests are derived from code_generation.
    assert DERIVED_TASKS == {
        "debugging": "code_generation",
        "unit_tests": "code_generation",
    }
