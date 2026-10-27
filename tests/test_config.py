"""Tests for config.py — DB-backed gateway config (no YAML, no hot-reload).

The Config object is hydrated from the ``settings`` table
(``gateway_config:<section>`` JSON blobs), seeding missing sections from the
Python ``SEED_CONFIG``. ``save()`` writes every section back to the DB.
"""

import os
from unittest.mock import patch

import pytest

from src.api.config import (
    Config,
    ConfigError,
    SEED_CONFIG,
    ALL_SECTIONS,
    init_config,
    get_config,
)


@pytest.fixture
def db_store(tmp_path):
    """A real SettingsStore backed by a temp DB."""
    from src.api.models import Base, get_engine
    from src.api.cost_cache import SettingsStore
    db_path = str(tmp_path / "test.db")
    e = get_engine(db_path)
    Base.metadata.create_all(e)
    return SettingsStore(e)


@pytest.fixture
def cfg():
    """A Config with no store — falls back to the Python seed."""
    return Config(store=None)


class TestSeedHydration:
    def test_seed_loaded_when_no_store(self, cfg):
        assert cfg.server["port"] == 8734
        assert cfg.server["default_profile"] == "l2"
        assert "l2" in cfg.profiles
        assert "deepseek" in cfg.providers
        assert isinstance(cfg.pricing, list)
        assert cfg.circuit_breaker["failures_dead"] == 6
        assert cfg.retry["max_attempts"] == 3
        assert cfg.database["path"] == "/app/data/costs.db"

    def test_dynamic_routing_defaults_disabled(self, cfg):
        dr = cfg.dynamic_routing
        assert dr["enabled"] is False
        assert dr["cost_bias"] == 0.15

    def test_model_limits_seeded(self, cfg):
        assert cfg.model_limits["deepseek-v4-pro"]["context_window"] == 1000000

    def test_raw_is_live_dict(self, cfg):
        cfg.raw["server"]["port"] = 9999
        assert cfg.server["port"] == 9999

    def test_deep_copy_does_not_alias_seed(self, cfg):
        # Mutating config must not mutate the module-level SEED_CONFIG.
        cfg.raw["profiles"]["l2"]["chain"] = []
        assert SEED_CONFIG["profiles"]["l2"]["chain"] != []

    def test_plugins_seed_has_memory_block(self, cfg):
        # The seed ships a plugins.memory block with auto_recall off by default.
        plugins = cfg.plugins
        assert "memory" in plugins
        assert plugins["memory"]["auto_recall"] is False
        assert plugins["memory"]["top_k"] == 3


class TestDbBacked:
    def test_hydrates_from_db(self, db_store):
        # Pre-seed a couple of sections in the DB.
        db_store.set_config_section("server", {"port": 9000, "default_profile": "l2"})
        db_store.set_config_section("dynamic_routing", {"enabled": True, "cost_bias": 0.4})
        c = Config(store=db_store)
        assert c.server["port"] == 9000
        assert c.dynamic_routing["enabled"] is True
        assert c.dynamic_routing["cost_bias"] == 0.4
        # Sections not in DB fall back to seed.
        assert "l2" in c.profiles

    def test_db_wins_over_seed(self, db_store):
        db_store.set_config_section("dynamic_routing", {"enabled": False, "cost_bias": 0.9})
        c = Config(store=db_store)
        assert c.dynamic_routing["enabled"] is False
        assert c.dynamic_routing["cost_bias"] == 0.9

    def test_corrupt_section_falls_back_to_seed(self, db_store):
        db_store.set("gateway_config:profiles", "{not json")
        c = Config(store=db_store)
        assert "l2" in c.profiles

    def test_env_overrides_port_and_db_path(self, db_store, monkeypatch):
        monkeypatch.setenv("LISTEN_PORT", "7777")
        monkeypatch.setenv("COST_DB", "/tmp/override.db")
        c = Config(store=db_store)
        assert c.server["port"] == 7777
        assert c.database["path"] == "/tmp/override.db"


class TestSave:
    def test_save_writes_all_sections(self, db_store):
        c = Config(store=db_store)
        c.raw["server"]["port"] = 8123
        c.raw["dynamic_routing"] = {"enabled": True, "cost_bias": 0.3}
        c.save()
        # Fresh Config from the same store sees the persisted values.
        c2 = Config(store=db_store)
        assert c2.server["port"] == 8123
        assert c2.dynamic_routing["enabled"] is True
        # Every section got written.
        for section in ALL_SECTIONS:
            assert db_store.get_config_section(section, None) is not None

    def test_save_without_store_warns_but_does_not_raise(self, cfg):
        cfg.raw["server"]["port"] = 9999
        cfg.save()  # should not raise

    def test_save_preserves_unrelated_sections(self, db_store):
        c = Config(store=db_store)
        c.save()
        db_store.set_config_section("dynamic_routing", {"enabled": False, "cost_bias": 0.5})
        # Re-load, mutate a different section, save — dynamic_routing must stay.
        c2 = Config(store=db_store)
        c2.raw["retry"] = {"max_attempts": 9}
        c2.save()
        c3 = Config(store=db_store)
        assert c3.dynamic_routing["enabled"] is False


class TestAccessors:
    def test_get_profile(self, cfg):
        p = cfg.get_profile("l2")
        assert p is not None
        assert "chain" in p
        assert cfg.get_profile("nope") is None

    def test_get_provider_key_env(self, cfg):
        with patch.dict(os.environ, {"OPENCODE_API_KEY": "sk-test123"}, clear=False):
            assert cfg.get_provider_key("opencode") == "sk-test123"

    def test_get_provider_key_missing_env(self, cfg):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ConfigError, match="not set"):
                cfg.get_provider_key("opencode")

    def test_get_pricing(self, cfg):
        p = cfg.get_pricing("deepseek", "deepseek-v4-pro")
        assert p["cache_miss"] > 0
        with pytest.raises(ConfigError, match="No pricing found"):
            cfg.get_pricing("unknown", "unknown")

    def test_get_provider_cache_config_default(self, cfg):
        assert cfg.get_provider_cache_config("nonexistent") == {
            "strategy": "none", "savings": "none", "hit_field": None,
        }

    def test_get_model_limits(self, cfg):
        assert cfg.get_model_limits("deepseek-v4-pro")["context_window"] == 1000000
        assert cfg.get_model_limits("nope") is None


class TestValidation:
    def test_corrupt_server_falls_back_to_seed(self, db_store):
        db_store.set_config_section("server", {"no_port": True})
        c = Config(store=db_store)
        assert c.server["port"] == 8734

    def test_corrupt_profile_is_quarantined_not_seeded(self, db_store):
        # CONTRACT CHANGE 2026-10-23: a section whose profiles are all invalid used to
        # be replaced wholesale by SEED_CONFIG. That silently swapped real profiles for
        # defaults when ONE chain came out empty (proven: emptying `l2` made `blog`
        # disappear). Now the stored profiles are kept and quarantined individually so
        # the failure is visible and repairable, and requests to them return
        # 503 profile_invalid instead of being routed to seed defaults nobody chose.
        db_store.set_config_section("profiles", {"bad": {"no_chain": True}})
        c = Config(store=db_store)
        assert "bad" in c.profiles, "the stored profile must not be discarded"
        assert "bad" in c.invalid_profiles
        assert "l2" not in c.profiles, "seed must not be substituted for real profiles"


class TestSingletons:
    def test_init_config_binds_store(self, db_store):
        import src.api.config as cfg_mod
        cfg_mod._config = None
        c = init_config(store=db_store)
        assert c._store is db_store
        assert cfg_mod._config is c

    def test_get_config_fallback(self, db_store):
        import src.api.config as cfg_mod
        from src.api.cost_cache import _settings_store, init_settings
        init_settings(None)  # ensure store exists (may be None)
        cfg_mod._config = None
        c = get_config()
        assert c is not None
