"""Hard provider-type store (alembic 029).

The provider `type` must be a hard, DB-constrained value — NOT a free-text
string in the gateway config blob. This pins:

  * the `provider_types` table exists with a CHECK that only the four known
    types pass (the DB itself refuses bad data),
  * the store resolves declared → inferred (row wins; no row = name inference),
  * backfill turns an inferred answer into a hard row on first read,
  * writes reject anything outside the four types and empty clears the row,
  * a bad value cannot be inserted even if the store were bypassed (CHECK).
"""

import os
import shutil
import sqlite3
import tempfile

import pytest

from src.api.models import PROVIDER_TYPES, ProviderType, get_engine, get_session
from src.api.provider_type_store import ProviderTypeStore


@pytest.fixture
def engine(tmp_path):
    eng = get_engine(str(tmp_path / "costs.db"))
    from src.api.models import Base
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def store(engine):
    return ProviderTypeStore(engine)


class TestCheckConstraint:
    def test_the_table_holds_only_the_four_known_types(self, engine):
        db = str(engine.url.database)
        con = sqlite3.connect(db)
        cols = [c[1] for c in con.execute("PRAGMA table_info(provider_types)")]
        assert "provider" in cols and "type" in cols
        # DB-level refusal: insert outside the four types fails.
        with pytest.raises(sqlite3.IntegrityError, match="ck_provider_types_type"):
            con.execute(
                "INSERT INTO provider_types (provider,type,created_at,updated_at) "
                "VALUES ('x','nonsense','2026','2026')"
            )
        con.close()

    def test_each_known_type_is_accepted(self, engine):
        db = str(engine.url.database)
        con = sqlite3.connect(db)
        for i, t in enumerate(PROVIDER_TYPES):
            con.execute(
                "INSERT INTO provider_types (provider,type,created_at,updated_at) "
                f"VALUES ('p{i}','{t}','2026','2026')"
            )
        con.commit()
        got = [r[0] for r in con.execute("SELECT type FROM provider_types ORDER BY provider")]
        con.close()
        assert sorted(got) == sorted(PROVIDER_TYPES)


class TestStoreResolution:
    def test_no_row_resolves_to_name_inference(self, store):
        assert store.type_for("local-sparks") == "local"
        assert store.type_for("commandcode2") == "commandcode"
        assert store.type_for("deepseek") == "deepseek"

    def test_row_wins_over_name(self, store):
        store.set("local-sparks", "deepseek")  # deliberately wrong vs the name
        assert store.type_for("local-sparks") == "deepseek"
        assert store.get("local-sparks") == "deepseek"

    def test_clean_row_is_read(self, store):
        store.set("opencode", "opencode")
        assert store.get("opencode") == "opencode"
        assert store.type_for("opencode") == "opencode"

    def test_all_returns_the_rows(self, store):
        store.set("deepseek", "deepseek")
        store.set("local-sparks", "local")
        assert store.all() == {"deepseek": "deepseek", "local-sparks": "local"}


class TestWrites:
    def test_set_rejects_an_unknown_type(self, store):
        with pytest.raises(ValueError):
            store.set("x", "nope")

    def test_set_normalises_case_and_whitespace(self, store, engine):
        store.set("x", "  LOCAL ")
        assert store.get("x") == "local"
        with get_session(engine) as s:
            row = s.query(ProviderType).filter_by(provider="x").first()
            assert row.type == "local"

    def test_empty_value_clears_the_row(self, storage):
        store, engine = storage
        store.set("opencode", "opencode")
        store.set("opencode", "")
        assert store.get("opencode") is None
        with get_session(engine) as s:
            assert s.query(ProviderType).filter_by(provider="opencode").first() is None

    def test_set_upserts_by_provider(self, store, engine):
        store.set("deepseek", "deepseek")
        store.set("deepseek", "opencode")  # same provider, new type
        with get_session(engine) as s:
            rows = s.query(ProviderType).filter_by(provider="deepseek").all()
            assert len(rows) == 1
            assert rows[0].type == "opencode"

    def test_drop_removes_the_row(self, store, engine):
        store.set("deepseek", "deepseek")
        store.drop("deepseek")
        with get_session(engine) as s:
            assert s.query(ProviderType).filter_by(provider="deepseek").first() is None

    def test_drop_with_no_row_is_a_noop(self, store):
        store.drop("does-not-exist")  # must not raise


@pytest.fixture
def storage(engine):
    return ProviderTypeStore(engine), engine


class FakeProviderHolder:
    def __init__(self, providers):
        self.providers = providers


class TestBackfill:
    def test_backfills_inferred_types_for_unseeded_providers(self, store):
        cfg = FakeProviderHolder({
            "opencode": {"api_base": "x"},
            "depth-first": {"api_base": "x"},  # unknown name → no row
            "local-sparks": {"api_base": "x"},
        })
        written = store.backfill_config(cfg)
        assert written == 2
        assert store.get("opencode") == "opencode"
        assert store.get("local-sparks") == "local"
        assert store.get("depth-first") is None  # nothing to infer

    def test_backfill_prefers_a_declared_type_over_inference(self, store):
        cfg = FakeProviderHolder({
            "commandcode2": {"api_base": "x", "type": "commandcode"},
        })
        store.backfill_config(cfg)
        assert store.get("commandcode2") == "commandcode"

    def test_backfill_never_clobbers_an_existing_row(self, store):
        store.set("local-sparks", "deepseek")  # operator set it intentionally
        cfg = FakeProviderHolder({"local-sparks": {"api_base": "x"}})
        store.backfill_config(cfg)
        assert store.get("local-sparks") == "deepseek"

    def test_backfill_is_idempotent(self, store):
        cfg = FakeProviderHolder({"opencode": {"api_base": "x"}})
        assert store.backfill_config(cfg) == 1
        assert store.backfill_config(cfg) == 0