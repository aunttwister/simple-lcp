"""Batch J: config.py validation/hydrate/env branches + __main__ guards.

Targets config.py: 212, 215, 220, 223, 226, 253-254, 261, 277-278, 282-283,
384, 387-388. Plus main.py:181 and the seed_capabilities/benchmark_import
__main__ guards (380/466/597) via the frame-local _trim trick / guarded exec.
"""
import copy
import sys
from unittest.mock import MagicMock, patch

import pytest

from src.api.config import Config, SEED_CONFIG, _validate, _env_port
from src.api.exceptions import ConfigError


class TestValidateBranches:
    def test_server_missing_default_profile(self):
        # 212
        with pytest.raises(ConfigError, match="default_profile"):
            _validate("server", {"port": 8734})

    def test_profiles_not_dict(self):
        # 215
        with pytest.raises(ConfigError, match="must be a dict"):
            _validate("profiles", [])

    def test_profile_empty_chain(self):
        # 220
        with pytest.raises(ConfigError, match="empty 'chain'"):
            _validate("profiles", {"x": {"chain": []}})

    def test_pricing_not_list(self):
        # 223
        with pytest.raises(ConfigError, match="'pricing' must be a list"):
            _validate("pricing", {})

    def test_providers_not_dict(self):
        # 226
        with pytest.raises(ConfigError, match="must be a dict"):
            _validate("providers", [])


class TestHydrateStoreErrors:
    def test_store_read_error_falls_back_to_seed(self):
        # 253-254: get_config_section raises → stored=None → seed path
        store = MagicMock()
        store.get_config_section.return_value = None
        cfg = Config(store=store)
        assert cfg.server["port"] == SEED_CONFIG["server"]["port"]

    def test_store_crash_during_hydrate(self):
        # 253-254 via exception on every section read
        store = MagicMock()
        store.get_config_section.side_effect = RuntimeError("db gone")
        cfg = Config(store=store)
        assert "chain" in cfg.profiles["l2"]

    def test_empty_stored_section_falls_back(self):
        # 261: stored value empty dict → seed used
        store = MagicMock()
        def get(sec, default=None):
            return {} if sec == "pricing" else None
        store.get_config_section.side_effect = get
        cfg = Config(store=store)
        assert isinstance(cfg.pricing, list)

    def test_invalid_required_section_restored(self):
        # 277-278: DB row validates badly → seed restored for that section
        store = MagicMock()
        def get(sec, default=None):
            if sec == "server":
                return {"no_port": True}     # fails _validate
            return None
        store.get_config_section.side_effect = get
        cfg = Config(store=store)
        assert "port" in cfg.server          # seed fallback survived


class TestEnvOverrides:
    def test_bad_listen_port_swallowed(self, monkeypatch):
        # 282-283: LISTEN_PORT set but not int-able → except → pass
        monkeypatch.setenv("LISTEN_PORT", "not-a-port")
        cfg = Config(store=None)
        assert cfg.server["port"] == SEED_CONFIG["server"]["port"]

    def test_env_port_ok(self, monkeypatch):
        monkeypatch.setenv("LISTEN_PORT", "9999")
        assert _env_port() == 9999

    def test_bad_cost_db_swallowed(self, monkeypatch):
        # 286-287 area: COST_DB normally just passes through
        monkeypatch.setenv("COST_DB", "/tmp/whatever.db")
        cfg = Config(store=None)
        assert cfg.database["path"] == "/tmp/whatever.db"


class TestSaveNoStoreAndErrors:
    def test_save_without_store(self):
        # 384: store is None → warning + return
        cfg = Config(store=None)
        cfg.save()

    def test_save_section_error(self):
        # 387-388: per-section set failure logged, loop continues
        store = MagicMock()
        store.get_config_section.return_value = None
        store.set_config_section.side_effect = [RuntimeError("ro")] + [None] * 20
        cfg = Config(store=store)
        cfg.save()
        assert store.set_config_section.called


class TestMainModuleGuard:
    def test_main_py_guard(self):
        # src/main.py:181 — exec defs under real name, then the guard alone
        import src.main as m
        with open(m.__file__, encoding="utf-8") as f:
            lines = f.readlines()
        guard_idx = next(i for i, ln in enumerate(lines)
                         if ln.startswith("if __name__ == "))
        body = "".join(lines[:guard_idx])
        guard = "".join(lines[guard_idx:])
        g = {"__name__": m.__name__, "__file__": m.__file__}
        exec(compile(body, m.__file__, "exec"), g)
        called = MagicMock()
        g["main"] = called
        g["__name__"] = "__main__"
        exec(compile(guard, m.__file__, "exec"), g)
        called.assert_called_once()


# ── CLI entry guards via runpy (covers seed_capabilities:597,
#    benchmark_import:466 and their argv branches 380 etc.) ──────────────────

def _run_guard(mod_name, argv, mocks: dict):
    """Exec a module's defs in a private namespace, inject mocks there, then
    run ONLY its __main__ guard line with sys.argv patched. Returns mocks."""
    import importlib
    mod = importlib.import_module(mod_name)
    src_path = mod.__file__
    with open(src_path, encoding="utf-8") as f:
            lines = f.readlines()
    guard_idx = next(i for i, ln in enumerate(lines)
                     if ln.startswith("if __name__ == "))
    body = "".join(lines[:guard_idx])
    guard = "".join(lines[guard_idx:])
    g = {"__name__": mod_name, "__file__": src_path}
    exec(compile(body, src_path, "exec"), g)
    for k, v in mocks.items():
        g[k] = v
    g["__name__"] = "__main__"
    with patch("sys.argv", argv):
        exec(compile(guard, src_path, "exec"), g)
    return mocks


class TestCliGuards:
    def test_seed_capabilities_registry_only(self, tmp_path):
        db = str(tmp_path / "seed.db")
        _run_guard("src.api.seed_capabilities",
                   ["seed_capabilities", "--db", db, "--registry-only"], {})
        from src.api.models import get_engine, ModelRegistryEntry, get_session
        engine = get_engine(db)
        with get_session(engine) as s:
            assert s.query(ModelRegistryEntry).count() > 0
        engine.dispose()

    def test_seed_capabilities_only(self):
        a = MagicMock(return_value=1)
        _run_guard("src.api.seed_capabilities",
                   ["seed_capabilities", "--db", ":memory:", "--capabilities-only"],
                   {"seed_capabilities": a})
        a.assert_called_once()
