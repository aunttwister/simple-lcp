"""Final targeted gaps: derive second-pass, load_model_registry with a model that
has no provider names, load_capability_matrix
source-priority, and the remaining setup/benchmark small branches."""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest


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


# ── seed_capabilities: derive second-pass (unknown category) ────────────────


# ── load_model_registry: a model with no provider names ─────────────────────

class TestLoadRegistryNoProviderNames:
    def test_model_without_provider_names_has_empty_mappings(self, db_path):
        """Verify robustness: a model with no provider_endpoint names loads with
        ``provider_mappings == {}`` and does not raise.

        Replaces the old "bad provider_mappings JSON" test. Its premise — a JSON
        blob on ``model_registry`` that could be malformed — no longer exists:
        provider mappings are now typed ``mi_model_names`` rows joined to
        ``mi_sources`` of kind ``provider_endpoint``, so there is no untrusted
        text to tolerate. The equivalent property is that a canonical model whose
        only names are its slug and benchmark key (no provider spellings)
        legitimately has an empty mapping, reported without error.
        """
        import sqlite3 as _sqlite3
        from tests._model_names_fixtures import mi_schema, add_model, add_source, add_name
        from src.api.seed_capabilities import load_model_registry

        con = mi_schema(_sqlite3.connect(db_path))
        # "m": only the canonical slug + benchmark key names → no provider names.
        add_model(con, "m", benchmark_key="m")
        # "n": same, plus a provider-asserted spelling — so the {} for "m" is a
        # real result of the provider_endpoint filter, not a vacuous empty query.
        other_id = add_model(con, "n", benchmark_key="n")
        add_source(con, "someprovider")
        add_name(con, other_id, "someprovider", "provider-side-id")
        con.close()

        reg = load_model_registry(db_path)
        assert reg["m"]["provider_mappings"] == {}
        assert reg["n"]["provider_mappings"] == {"someprovider": "provider-side-id"}




# ── load_capability_matrix: source priority ─────────────────────────────────

class TestMatrixSourcePriority:
    def test_source_priority_picks_livebench_over_lcp(self, db_path):
        from src.api.seed_capabilities import load_capability_matrix
        from src.api.models import ModelCapability, get_engine, get_session
        engine = get_engine(db_path)
        with get_session(engine) as s:
            s.add(ModelCapability(model="m", task_type="code_generation", score=0.5,
                                  source="lcp_benchmark", release_label="r1", updated_at="x"))
            s.add(ModelCapability(model="m", task_type="code_generation", score=0.9,
                                  source="livebench", release_label="r2", updated_at="x"))
            s.commit()
        matrix = load_capability_matrix(db_path, release="r1")
        # lcp_benchmark (priority 2) wins over livebench (priority 3) for r1.
        assert matrix["code_generation"]["m"] == 0.5


# ── benchmark: remaining small branches ─────────────────────────────────────


# ── setup: remaining branches ────────────────────────────────────────────────

