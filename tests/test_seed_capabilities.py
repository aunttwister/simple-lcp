"""Tests for the declared capability matrix: the bundled seed and its resolution.

The router ranks models on ``model_capabilities``; since R13 that table is fed by
the bundled ``data/declared_capabilities.json`` instead of a LiveBench import.
"""

import json
import os
import tempfile

import pytest

from src.api.seed_capabilities import (
    CAPABILITIES_FILE,
    load_capability_matrix,
    load_declared_capabilities,
    seed_capabilities,
    seed_model_registry,
)


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from src.api.models import Base, get_engine
    engine = get_engine(path)
    Base.metadata.create_all(engine)
    engine.dispose()
    yield path
    for ext in ("", "-wal", "-shm"):
        try:
            os.unlink(path + ext)
        except FileNotFoundError:
            pass


def test_bundled_file_is_valid_declared_data():
    payload = json.load(open(CAPABILITIES_FILE))
    assert payload["_why"], "the bundled file must explain itself"
    rows = load_declared_capabilities()
    assert rows, "the bundled matrix must not be empty"
    for row in rows:
        assert set(row) >= {"model", "task_type", "score", "source", "release_label"}
        assert 0.0 <= row["score"] <= 1.0
        assert row["source"] in {"livebench", "lcp_benchmark", "arena", "gateway_yaml", "manual"}


def test_seed_writes_rows_and_loads_as_matrix(db_path):
    n = seed_capabilities(db_path)
    assert n == len(load_declared_capabilities())
    matrix = load_capability_matrix(db_path)
    assert matrix, "a freshly seeded database must yield a usable matrix"
    sample = load_declared_capabilities()[0]
    assert matrix[sample["task_type"]][sample["model"]] == pytest.approx(sample["score"], abs=1e-4)


def test_seed_is_idempotent(db_path):
    first = seed_capabilities(db_path)
    second = seed_capabilities(db_path)
    assert first == second
    from src.api.models import ModelCapability, get_engine, get_session
    with get_session(get_engine(db_path)) as session:
        assert session.query(ModelCapability).count() == first


def test_seed_never_clobbers_a_manual_score(db_path):
    """A hand-set score from another source must survive a re-seed."""
    from src.api.models import ModelCapability, get_engine, get_session

    seed_capabilities(db_path)
    with get_session(get_engine(db_path)) as session:
        session.add(ModelCapability(
            model="deepseek-v4-pro", task_type="code_generation", score=0.111,
            source="manual", release_label="2099-01-01", updated_at="2026-09-23T00:00:00",
        ))
        session.commit()

    seed_capabilities(db_path)
    with get_session(get_engine(db_path)) as session:
        kept = session.query(ModelCapability).filter_by(
            model="deepseek-v4-pro", source="manual").one()
        assert kept.score == pytest.approx(0.111)


def test_registry_seed_then_matrix_resolution(db_path):
    seed_model_registry(db_path)
    seed_capabilities(db_path)
    matrix = load_capability_matrix(db_path)
    assert "reasoning_chain" in matrix
    for derived in ("debugging", "unit_tests"):
        assert derived in matrix
