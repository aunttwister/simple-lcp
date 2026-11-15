"""Provider ↔ model ↔ profile dependency: the delete/rename graph rewrite.

Measured on 2026-10-23 in prod LCP:
  * `provider_health` held `local-zgx` rows (degraded / 6 failures) for a provider that
    is NOT in the config — it had been renamed to `local-sparks` and rename never moved
    the health rows, so the ghost survived every restart.
  * the model registry mapped `qwen3.8-flash-next` to `zgx` and `llamacpp`, neither of
    which exists in the config, and nine other mappings pointed at model IDs the named
    provider does not declare.
  * DELETE /api/providers removed chain steps blindly. If that emptied a profile's
    chain, the config loader rejected the `profiles` section and replaced it with
    SEED_CONFIG — every real profile silently became the default one at next restart.
  * `alerts` had 0 rows: `provider_dead` / `provider_degraded` rules existed but nothing
    called fire() except budget_breach.

These tests pin the corrected contract:
  * one bad profile is quarantined, not the whole section;
  * an emptied chain makes the profile INVALID (kept, flagged, 503 at request time);
  * delete refuses with 409 unless forced, and cascades the registry;
  * rename moves health rows and registry mappings with the name.

The registry itself was re-homed (alembic 034/035): the old `model_registry` JSON blob
`provider_mappings_json` (`{provider: provider_side_name}`) and `mi_aliases` are gone.
A provider spelling is now a `mi_model_names` row owned by a `mi_sources` row whose
`kind = 'provider_endpoint'` — one row per (model × naming authority). `zgx` and
`llamacpp` are therefore source rows, not keys in a blob: a provider that is NOT a live
endpoint is still recorded (as a name row under its own source) and still rewritten when
the provider is renamed or deleted.
"""
import sqlite3
import sys
from pathlib import Path

import pytest
from unittest.mock import MagicMock, patch

from src.api.config import Config, SEED_CONFIG
from src.api.models import Base, ProviderHealth, get_engine, get_session
from src.server.endpoints import (
    ProviderEndpoints,
    _rekey_provider_in_registry,
    _strip_provider_from_registry,
)

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests._model_names_fixtures import (  # noqa: E402
    add_name,
    add_model,
    add_source,
    mi_schema,
    provider_mappings,
)


@pytest.fixture()
def temp_db(tmp_path):
    """A DB carrying BOTH the ORM tables (provider_health lives here) and the
    model-name normalisation tables that the accessors read."""
    path = str(tmp_path / "t.db")
    engine = get_engine(path)
    Base.metadata.create_all(engine)          # provider_health, requests, ...
    con = sqlite3.connect(path)
    mi_schema(con)                            # mi_sources / mi_model_names / ...
    con.close()
    yield engine
    engine.dispose()


def _conn(engine):
    """An open connection onto the same file the engine persists to."""
    con = sqlite3.connect(engine.url.database)
    con.row_factory = sqlite3.Row
    return con


class _Store:
    def __init__(self, sections):
        self.sections = sections

    def get_config_section(self, name, default=None):
        return self.sections.get(name, default)


# ── loader: quarantine, never estate-wide reset ─────────────────────────────

class TestLoaderQuarantine:
    def test_one_empty_chain_does_not_reset_every_profile(self):
        profiles = {
            "l2": {"chain": []},
            "blog": {"chain": [{"provider": "deepseek", "model": "deepseek-flash"}]},
        }
        cfg = Config(store=_Store({"profiles": profiles}))
        assert sorted(cfg.profiles) == ["blog", "l2"], "real profiles were replaced by seed"
        assert cfg.profiles["blog"]["chain"] == profiles["blog"]["chain"]
        assert "l2" in cfg.invalid_profiles
        assert "empty" in cfg.invalid_profiles["l2"]

    def test_healthy_config_has_no_invalid_profiles(self):
        profiles = {
            "l2": {"chain": [{"provider": "deepseek", "model": "deepseek-v4-pro"}]},
        }
        cfg = Config(store=_Store({"profiles": profiles}))
        assert cfg.invalid_profiles == {}

    def test_structurally_broken_section_still_falls_back(self):
        cfg = Config(store=_Store({"profiles": ["not", "a", "dict"]}))
        assert sorted(cfg.profiles) == sorted(SEED_CONFIG["profiles"])


# ── registry cascade ────────────────────────────────────────────────────────

class TestRegistryCascade:
    def _seed(self, engine):
        """`qwen3.8-flash-next` named by two non-endpoint providers (`zgx`,
        `llamacpp`) and `deepseek-flash` named by `deepseek` + `opencode`.

        In the old table this was one `model_registry` row per model with a
        `provider_mappings_json` blob. Now it is a provider_endpoint source per
        provider plus one `mi_model_names` row per (model, provider).
        """
        con = _conn(engine)
        for slug in ("zgx", "llamacpp", "deepseek", "opencode"):
            add_source(con, slug)                     # kind='provider_endpoint'
        qwen = add_model(con, "qwen3.8-flash-next", benchmark_key="qwen3.8-flash-next")
        add_name(con, qwen, "zgx", "qwen3.8-flash-next")
        add_name(con, qwen, "llamacpp", "qwen3.8-flash-next")
        ds = add_model(con, "deepseek-flash", benchmark_key="deepseek-flash")
        add_name(con, ds, "deepseek", "deepseek-flash")
        add_name(con, ds, "opencode", "deepseek-v4.1-flash")
        con.commit()
        con.close()

    def test_strip_counts_rows_and_unserved_models(self, temp_db):
        self._seed(temp_db)
        # qwen keeps zgx, so removing llamacpp alone leaves it served
        assert _strip_provider_from_registry(temp_db, "llamacpp") == (1, 0)
        con = _conn(temp_db)
        assert provider_mappings(con, "qwen3.8-flash-next") == {
            "zgx": "qwen3.8-flash-next"}
        assert "opencode" in provider_mappings(con, "deepseek-flash")
        con.close()
        # removing the last provider of qwen leaves it unserved, but the model survives
        assert _strip_provider_from_registry(temp_db, "zgx") == (1, 1)
        con = _conn(temp_db)
        assert provider_mappings(con, "qwen3.8-flash-next") == {}
        con.close()

    def test_strip_unknown_provider_is_a_no_op(self, temp_db):
        self._seed(temp_db)
        assert _strip_provider_from_registry(temp_db, "nope") == (0, 0)

    def test_rekey_moves_mappings_and_health(self, temp_db):
        self._seed(temp_db)
        with get_session(temp_db) as s:
            s.add(ProviderHealth(provider="zgx", base_url="http://zgx/v1",
                                 profile="l2", status="degraded",
                                 consecutive_failures=6))
            s.commit()
        changed = _rekey_provider_in_registry(temp_db, "zgx", "local-sparks")
        assert changed == 1
        con = _conn(temp_db)
        mappings = provider_mappings(con, "qwen3.8-flash-next")
        assert "local-sparks" in mappings and "zgx" not in mappings
        con.close()
        with get_session(temp_db) as s:
            ghost = s.query(ProviderHealth).filter_by(provider="zgx").all()
            moved = s.query(ProviderHealth).filter_by(provider="local-sparks").all()
            assert ghost == [] and len(moved) == 1


# ── delete handler ──────────────────────────────────────────────────────────

class _Handler(ProviderEndpoints):
    """Minimal stand-in for the delete/rename handlers."""

    def __init__(self, cfg, engine, path="/api/providers/x"):
        self.config = cfg
        self.engine = engine
        self.path = path
        self.sent = []
        self._audit = MagicMock()

    def _send_json(self, payload, status=200):
        self.sent.append((status, payload))


def _cfg(profiles, providers=("deepseek", "opencode")):
    cfg = MagicMock()
    cfg.raw = {
        "providers": {n: {"api_base": f"https://{n}/v1"} for n in providers},
        "profiles": profiles,
    }
    cfg.providers = dict(cfg.raw["providers"])
    cfg.profiles = dict(cfg.raw["profiles"])
    cfg.save = MagicMock()
    return cfg


def _cred_none():
    return patch("src.server.endpoints._credential_store_for", return_value=None)


class TestProviderDelete:
    def test_refuses_when_it_would_empty_a_chain(self, temp_db):
        cfg = _cfg({"l2": {"chain": [{"provider": "deepseek", "model": "m"}]}})
        h = _Handler(cfg, temp_db)
        with _cred_none():
            h._serve_provider_delete("deepseek")
        status, body = h.sent[-1]
        assert status == 409
        assert body["code"] == "provider_in_use"
        assert body["profiles_emptied"] == ["l2"]
        assert "deepseek" in cfg.raw["providers"], "delete must not have happened"
        cfg.save.assert_not_called()

    def test_force_deletes_and_reports_invalidated_profiles(self, temp_db):
        cfg = _cfg({"l2": {"chain": [{"provider": "deepseek", "model": "m"}]}})
        h = _Handler(cfg, temp_db, path="/api/providers/deepseek?force=1")
        with _cred_none():
            h._serve_provider_delete("deepseek")
        status, body = h.sent[-1]
        assert status == 200
        assert body["profiles_emptied"] == ["l2"]
        assert "deepseek" not in cfg.raw["providers"]
        assert cfg.raw["profiles"]["l2"]["chain"] == []
        cfg.save.assert_called_once()

    def test_delete_allowed_when_another_step_remains(self, temp_db):
        cfg = _cfg({"l2": {"chain": [{"provider": "deepseek", "model": "m"},
                                     {"provider": "opencode", "model": "m"}]}})
        h = _Handler(cfg, temp_db)
        with _cred_none():
            h._serve_provider_delete("deepseek")
        status, body = h.sent[-1]
        assert status == 200
        assert body["profiles_emptied"] == []
        assert [s["provider"] for s in cfg.raw["profiles"]["l2"]["chain"]] == ["opencode"]

    def test_delete_strips_registry_mappings(self, temp_db):
        con = _conn(temp_db)
        add_source(con, "deepseek")
        add_source(con, "opencode")
        x = add_model(con, "x", benchmark_key="x")
        add_name(con, x, "deepseek", "x")
        add_name(con, x, "opencode", "x")
        con.commit()
        con.close()
        cfg = _cfg({"l2": {"chain": [{"provider": "deepseek", "model": "m"},
                                     {"provider": "opencode", "model": "m"}]}})
        h = _Handler(cfg, temp_db)
        with _cred_none():
            h._serve_provider_delete("deepseek")
        status, body = h.sent[-1]
        assert status == 200
        assert body["registry_mappings_updated"] == 1
        assert body["models_unserved"] == 0
        con = _conn(temp_db)
        assert provider_mappings(con, "x") == {"opencode": "x"}
        con.close()
