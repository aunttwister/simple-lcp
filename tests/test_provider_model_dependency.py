"""Provider ↔ model ↔ profile dependency: the delete/rename graph rewrite.

Measured on 2026-10-23 in prod LCP:
  * `provider_health` held `local-zgx` rows (degraded / 6 failures) for a provider that
    is NOT in the config — it had been renamed to `local-sparks` and rename never moved
    the health rows, so the ghost survived every restart.
  * `model_registry` mapped `qwen3.8-flash-next` to `zgx` and `llamacpp`, neither of
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
"""
import json

import pytest
from unittest.mock import MagicMock, patch

from src.api.config import Config, SEED_CONFIG
from src.api.models import Base, ModelRegistryEntry, ProviderHealth, get_engine, get_session
from src.server.endpoints import (
    ProviderEndpoints,
    _rekey_provider_in_registry,
    _strip_provider_from_registry,
)


@pytest.fixture()
def temp_db(tmp_path):
    engine = get_engine(str(tmp_path / "t.db"))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


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
        with get_session(engine) as s:
            s.add(ModelRegistryEntry(logical_name="qwen3.8-flash-next",
                                     benchmark_key="qwen3.8-flash-next",
                                     provider_mappings_json=json.dumps(
                                         {"zgx": "qwen3.8-flash-next",
                                          "llamacpp": "qwen3.8-flash-next"})))
            s.add(ModelRegistryEntry(logical_name="deepseek-flash",
                                     benchmark_key="deepseek-flash",
                                     provider_mappings_json=json.dumps(
                                         {"deepseek": "deepseek-flash",
                                          "opencode": "deepseek-v4.1-flash"})))
            s.commit()

    def test_strip_counts_rows_and_unserved_models(self, temp_db):
        self._seed(temp_db)
        # qwen keeps zgx, so removing llamacpp alone leaves it served
        assert _strip_provider_from_registry(temp_db, "llamacpp") == (1, 0)
        with get_session(temp_db) as s:
            qwen = s.query(ModelRegistryEntry).filter_by(
                logical_name="qwen3.8-flash-next").one()
            assert json.loads(qwen.provider_mappings_json) == {"zgx": "qwen3.8-flash-next"}
            ds = s.query(ModelRegistryEntry).filter_by(logical_name="deepseek-flash").one()
            assert "opencode" in json.loads(ds.provider_mappings_json)
        # removing the last provider of qwen leaves it unserved, but the row survives
        assert _strip_provider_from_registry(temp_db, "zgx") == (1, 1)
        with get_session(temp_db) as s:
            qwen = s.query(ModelRegistryEntry).filter_by(
                logical_name="qwen3.8-flash-next").one()
            assert json.loads(qwen.provider_mappings_json) == {}

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
        with get_session(temp_db) as s:
            qwen = s.query(ModelRegistryEntry).filter_by(
                logical_name="qwen3.8-flash-next").one()
            mappings = json.loads(qwen.provider_mappings_json)
            assert "local-sparks" in mappings and "zgx" not in mappings
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
        with get_session(temp_db) as s:
            s.add(ModelRegistryEntry(logical_name="x", benchmark_key="x",
                                     provider_mappings_json=json.dumps(
                                         {"deepseek": "x", "opencode": "x"})))
            s.commit()
        cfg = _cfg({"l2": {"chain": [{"provider": "deepseek", "model": "m"},
                                     {"provider": "opencode", "model": "m"}]}})
        h = _Handler(cfg, temp_db)
        with _cred_none():
            h._serve_provider_delete("deepseek")
        status, body = h.sent[-1]
        assert status == 200
        assert body["registry_mappings_updated"] == 1
        assert body["models_unserved"] == 0
        with get_session(temp_db) as s:
            entry = s.query(ModelRegistryEntry).filter_by(logical_name="x").one()
            assert json.loads(entry.provider_mappings_json) == {"opencode": "x"}
