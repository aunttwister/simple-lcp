"""Remaining setup.py + seed_capabilities.py branch coverage."""

import contextlib
import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def temp_db():
    import tempfile as _t
    from src.api.models import get_engine, Base
    fd, path = _t.mkstemp(suffix=".db")
    os.close(fd)
    engine = get_engine(path)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
    for ext in ["", "-wal", "-shm"]:
        try:
            os.unlink(path + ext)
        except FileNotFoundError:
            pass


# ── setup: benchmark_step / manifest / load_state branches ──────────────────

class TestSetupManifestBranches:

    def test_manifest_returns_modules(self):
        from src.api import setup as setup_mod
        cfg = MagicMock()
        cfg.providers = {}
        m = setup_mod.manifest(cfg)
        assert "steps" in m and "modules" in m

    def test_load_state_exception_returns_empty(self, temp_db):
        from src.api import setup as setup_mod
        with patch("src.api.models.get_session", side_effect=RuntimeError("db down")):
            assert setup_mod.load_state(temp_db) == {}

    def test_set_state_updates_existing(self, temp_db):
        from src.api import setup as setup_mod
        setup_mod.set_state(temp_db, "k", "done", "detail-1")
        setup_mod.set_state(temp_db, "k", "failed", "detail-2")
        state = setup_mod.load_state(temp_db)
        assert state["k"]["status"] == "failed"
        assert state["k"]["detail"] == "detail-2"

    def test_is_complete_required_installed(self, temp_db):
        from src.api import setup as setup_mod
        with patch.object(setup_mod, "provider_steps", return_value=[
            {"required": True, "installed": True},
            {"required": False, "installed": False},
        ]):
            assert setup_mod.is_complete(temp_db, MagicMock()) is True

    def test_provider_preset_unknown(self):
        from src.api import setup as setup_mod
        reg = MagicMock()
        reg.presets = {"deepseek": {"api_base": "x"}}
        with patch("src.api.cost_plugins.get_registry", return_value=reg):
            assert setup_mod._provider_preset("nope") == {}


# ── setup: install_provider cookie/workspace + remove branches ──────────────

class TestProviderBranches:
    def test_install_provider_with_cookie_and_workspace(self, temp_db):
        from src.api import setup as setup_mod
        cfg = MagicMock()
        cfg.raw = {"providers": {}}
        cfg.save = MagicMock()
        store = MagicMock()
        store.has.return_value = True
        with patch("src.api.setup._provider_preset", return_value={"api_base": "https://x/v1", "models": ["m"]}):
            with patch("src.api.credential_store.get_credential_store", return_value=store):
                result = setup_mod.install_provider(
                    temp_db, cfg, "deepseek",
                    {"api_key": "k", "cookie": "c", "workspace_id": "w"},
                )
        assert result["installed"] is True
        store.set.assert_called_with("deepseek", "k")
        store.set_cookie.assert_called_with("deepseek", "c")
        store.set_workspace_id.assert_called_with("deepseek", "w")

    def test_remove_provider_clears_cookie_and_workspace(self, temp_db):
        from src.api import setup as setup_mod
        cfg = MagicMock()
        cfg.raw = {
            "providers": {"deepseek": {}},
            "profiles": {"l2": {"chain": [{"provider": "deepseek", "model": "m"}]}},
        }
        cfg.save = MagicMock()
        store = MagicMock()
        with patch("src.api.credential_store.get_credential_store", return_value=store):
            result = setup_mod.remove_provider(temp_db, cfg, "deepseek")
        assert result["removed"] is True
        store.set.assert_called_with("deepseek", "")
        store.set_cookie.assert_called_with("deepseek", "")
        store.set_workspace_id.assert_called_with("deepseek", "")


# ── setup: _tail_detail error-line selection ─────────────────────────────────


# ── seed_capabilities: resolution branches ──────────────────────────────────

class TestResolutionBranches:
    def _mkrow(self, model, task, score, release):
        from src.api.models import ModelCapability
        from datetime import datetime, timezone
        return ModelCapability(
            model=model, task_type=task, score=score, source="livebench",
            release_label=release, updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def test_resolve_pinned_missing_release_falls_back(self):
        from src.api.seed_capabilities import resolve_active_rows
        rows = [self._mkrow("m", "t", 0.5, "2026-08-01")]
        registry = {"m": {"benchmark_key": "m", "active_release": "2099-01-01",
                          "provider_mappings": {}}}
        out = resolve_active_rows(rows, registry)
        assert len(out) == 1
        assert out[0].release_label == "2026-08-01"  # falls back to newest

    def test_resolve_legacy_only_rows(self):
        from src.api.seed_capabilities import resolve_active_rows
        rows = [self._mkrow("m", "t", 0.5, None)]
        registry = {"m": {"benchmark_key": "m", "active_release": None,
                          "provider_mappings": {}}}
        out = resolve_active_rows(rows, registry)
        # Legacy rows participate (may be emitted twice when selected==None;
        # deduped downstream in load_capability_matrix).
        assert any(r.model == "m" for r in out)

    def test_resolve_provider_alias_key(self):
        from src.api.seed_capabilities import resolve_active_rows
        # Row keyed by provider-side model ID maps via registry provider_mappings.
        rows = [self._mkrow("deepseek/deepseek-v4-pro", "t", 0.5, "2026-08-13")]
        registry = {"deepseek-v4-pro": {
            "benchmark_key": "deepseek-v4-pro",
            "active_release": "2026-08-13",
            "provider_mappings": {"deepseek": "deepseek/deepseek-v4-pro"},
        }}
        out = resolve_active_rows(rows, registry)
        assert len(out) == 1

    def test_effective_releases_pinned_and_newest(self):
        from src.api.seed_capabilities import effective_releases
        rows = [
            self._mkrow("a", "t", 0.5, "2026-08-01"),
            self._mkrow("b", "t", 0.5, "2026-07-01"),
            self._mkrow("c", "t", 0.5, None),
        ]
        registry = {
            "a": {"benchmark_key": "a", "active_release": "2026-08-01", "provider_mappings": {}},
            "b": {"benchmark_key": "b", "active_release": None, "provider_mappings": {}},
        }
        out = effective_releases(rows, registry)
        assert out["a"] == "2026-08-01"
        assert out["b"] == "2026-07-01"
        assert "c" not in out  # legacy-only omitted
