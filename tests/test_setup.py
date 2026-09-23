"""Tests for the first-run setup wizard (src.api.setup)."""
import os
import subprocess
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from src.api.models import get_engine, Base
from src.api import setup as setup_mod


@pytest.fixture
def temp_db():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
    for ext in ["", "-wal", "-shm"]:
        try:
            os.unlink(db_path + ext)
        except FileNotFoundError:
            pass


@pytest.fixture
def mock_config():
    cfg = MagicMock()
    cfg.providers = {}
    cfg.raw = {"providers": {}}
    cfg.save = MagicMock()
    return cfg


# ── Manifest ────────────────────────────────────────────────────────────────

class TestManifest:
    def test_provider_steps_all_present_and_required_flags(self, mock_config):
        steps = setup_mod.provider_steps(mock_config)
        names = {s["name"] for s in steps}
        assert names == {"deepseek", "opencode", "commandcode", "llamacpp"}
        req = {s["name"]: s["required"] for s in steps}
        assert req == {
            "deepseek": True,
            "opencode": True,
            "commandcode": True,
            "llamacpp": False,
        }
        for s in steps:
            assert s["installed"] is False

    def test_llamacpp_installed_when_configured(self, mock_config):
        mock_config.providers = {"llamacpp": {"api_base": "http://localhost:8080/v1", "models": []}}
        mock_config.raw = {"providers": dict(mock_config.providers)}
        steps = setup_mod.provider_steps(mock_config)
        llamacpp = next(s for s in steps if s["name"] == "llamacpp")
        assert llamacpp["installed"] is True

    def test_manifest_shape(self, mock_config):
        m = setup_mod.manifest(mock_config)
        assert "steps" in m and "modules" in m


# ── State helpers ───────────────────────────────────────────────────────────

class TestState:
    def test_set_and_load_state(self, temp_db):
        setup_mod.set_state(temp_db, "provider:deepseek", "done", "installed")
        state = setup_mod.load_state(temp_db)
        assert state["provider:deepseek"]["status"] == "done"
        assert state["provider:deepseek"]["detail"] == "installed"

    def test_mark_skipped_once(self, temp_db):
        assert setup_mod.mark_skipped(temp_db) is True
        assert setup_mod.mark_skipped(temp_db) is False
        assert setup_mod.load_state(temp_db)["wizard"]["status"] == "skipped"

    def test_is_complete_skipped(self, temp_db, mock_config):
        setup_mod.mark_skipped(temp_db)
        assert setup_mod.is_complete(temp_db, mock_config) is True

    def test_is_complete_requires_all(self, temp_db, mock_config):
        assert setup_mod.is_complete(temp_db, mock_config) is False

    def test_is_complete_no_required(self, temp_db, mock_config, monkeypatch):
        # Patch provider_steps so no steps are required.
        monkeypatch.setattr(
            setup_mod,
            "provider_steps",
            lambda cfg: [
                {"kind": "provider", "name": "llamacpp", "required": False,
                 "installed": False}
            ],
        )
        assert setup_mod.is_complete(temp_db, mock_config) is True


# ── Provider install ────────────────────────────────────────────────────────

class TestInstallProvider:
    def test_unknown_provider_raises(self, temp_db, mock_config):
        with pytest.raises(setup_mod.SetupError, match="unknown provider"):
            setup_mod.install_provider(temp_db, mock_config, "openai", {"api_key": "k"})

    def test_models_not_list_raises(self, temp_db, mock_config):
        with patch("src.api.setup._provider_preset", return_value={"api_base": "https://x/v1", "models": []}):
            with pytest.raises(setup_mod.SetupError, match="'models' must be a list"):
                setup_mod.install_provider(temp_db, mock_config, "deepseek", {"models": "m"})

    def test_missing_api_base_raises(self, temp_db, mock_config):
        with patch("src.api.setup._provider_preset", return_value={"api_base": "", "models": ["m"]}):
            with pytest.raises(setup_mod.SetupError, match="missing api_base"):
                setup_mod.install_provider(temp_db, mock_config, "deepseek", {})

    def test_store_none_raises(self, temp_db, mock_config):
        with patch("src.api.setup._provider_preset", return_value={"api_base": "https://x/v1", "models": ["m"]}):
            with patch("src.api.credential_store.get_credential_store", return_value=None):
                with pytest.raises(setup_mod.SetupError, match="credential store not initialized"):
                    setup_mod.install_provider(temp_db, mock_config, "deepseek", {"api_key": "k"})

    def test_missing_key_raises_for_keyed_provider(self, temp_db, mock_config):
        with patch("src.api.setup._provider_preset", return_value={"api_base": "https://x/v1", "models": ["m"]}):
            with patch("src.api.credential_store.get_credential_store", return_value=MagicMock(has=MagicMock(return_value=False))):
                with pytest.raises(setup_mod.SetupError, match="missing api_key"):
                    setup_mod.install_provider(temp_db, mock_config, "deepseek", {})

    def test_install_deepseek_writes_config_and_credential(self, temp_db, mock_config):
        store = MagicMock()
        store.has = MagicMock(return_value=False)
        with patch("src.api.setup._provider_preset", return_value={"api_base": "https://api.deepseek.com/v1", "models": ["deepseek-v4-pro"]}):
            with patch("src.api.credential_store.get_credential_store", return_value=store):
                result = setup_mod.install_provider(
                    temp_db, mock_config, "deepseek",
                    {"api_key": "sk-test"},
                )
        assert result == {"installed": True, "provider": "deepseek"}
        mock_config.save.assert_called_once()
        assert mock_config.raw["providers"]["deepseek"]["api_base"] == "https://api.deepseek.com/v1"
        store.set.assert_called_once_with("deepseek", "sk-test")
        assert setup_mod.load_state(temp_db)["provider:deepseek"]["status"] == "done"


class TestRemoveProvider:
    def test_unknown_provider_raises(self, temp_db, mock_config):
        with pytest.raises(setup_mod.SetupError, match="unknown provider"):
            setup_mod.remove_provider(temp_db, mock_config, "openai")

    def test_remove_provider_clears_config_and_credentials(self, temp_db, mock_config):
        mock_config.providers = {"deepseek": {"api_base": "https://api.deepseek.com/v1", "models": ["m"]}}
        mock_config.raw = {
            "providers": {"deepseek": {"api_base": "https://api.deepseek.com/v1", "models": ["m"]}},
            "profiles": {"l2": {"chain": [{"provider": "deepseek", "model": "m"}]}},
        }
        store = MagicMock()
        with patch("src.api.credential_store.get_credential_store", return_value=store):
            result = setup_mod.remove_provider(temp_db, mock_config, "deepseek")
        assert result == {"removed": True, "provider": "deepseek"}
        assert "deepseek" not in mock_config.raw["providers"]
        assert mock_config.raw["profiles"]["l2"]["chain"] == []
        store.set.assert_called_once_with("deepseek", "")
        assert setup_mod.load_state(temp_db)["provider:deepseek"]["status"] == "removed"


# ── module install coordinators ──────────────────────────────────────────────


# ── Install progress helpers (previously uncovered) ─────────────────────────
