"""Tests for the declared-capability seeder, the model registry and the
capability-matrix resolution the router depends on."""

import os
import tempfile

import pytest

from src.api.seed_capabilities import (
    DERIVED_TASKS,
    load_capability_matrix,
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
        assert n == len(__import__("src.api.seed_capabilities", fromlist=["DEFAULT_MODEL_REGISTRY"]).DEFAULT_MODEL_REGISTRY)
        # Idempotent on second run.
        n2 = seed_model_registry(db_path)
        assert n2 == 0
        # sync re-applies defaults (still idempotent count-wise on unchanged data).
        n3 = seed_model_registry(db_path, sync=True)
        assert n3 == len(__import__("src.api.seed_capabilities", fromlist=["DEFAULT_MODEL_REGISTRY"]).DEFAULT_MODEL_REGISTRY)

    def test_load_registry_roundtrip(self, db_path):
        seed_model_registry(db_path)
        from src.api.seed_capabilities import load_model_registry
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
