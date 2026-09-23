"""Small-module coverage gaps: crypto fallback-key error path, reasoning_store
singleton and setup install
progress/failure paths."""

import os
from unittest.mock import patch

import pytest


# ── crypto: fallback-key error path ──────────────────────────────────────────

class TestCryptoExtra:
    def test_fallback_key_io_error_returns_none(self, tmp_path, monkeypatch):
        from src.api import crypto
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.open", side_effect=OSError("denied")):
                key = crypto._load_or_create_fallback_key(str(tmp_path))
        assert key is None

    def test_get_secret_key_raises_when_fallback_fails(self, tmp_path, monkeypatch):
        from src.api import crypto
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(crypto, "_load_or_create_fallback_key", return_value=None):
                with pytest.raises(RuntimeError, match="Unable to obtain a secret key"):
                    crypto.get_secret_key(str(tmp_path))


# ── reasoning_store: singleton lifecycle ─────────────────────────────────────

class TestReasoningStoreSingleton:
    def test_singleton_reset(self):
        from src.api import reasoning_store
        reasoning_store._reasoning_store = None
        a = reasoning_store.get_reasoning_store()
        b = reasoning_store.get_reasoning_store()
        assert a is b
        reasoning_store._reasoning_store = None
        c = reasoning_store.get_reasoning_store()
        assert c is not a
        reasoning_store._reasoning_store = None



@pytest.fixture
def db_path():
    import tempfile
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


# ── setup: install failure paths ─────────────────────────────────────────────


# ── commandcode: _load_catalog TTL/cooldown branches ─────────────────────────

class TestCommandCodeCatalog:
    def test_catalog_ttl_hit_returns_cache(self):
        import src.api.cost_plugins.commandcode as cc
        import time as _t
        cc._catalog_cache["by_last_seg"] = {"cached": "cached-model"}
        cc._catalog_cache["loaded_ts"] = _t.time()  # fresh → TTL hit
        cc._catalog_cache["failed_ts"] = 0.0
        with patch("urllib.request.urlopen") as mock_open:
            idx = cc._load_catalog()
            mock_open.assert_not_called()
        assert idx == {"cached": "cached-model"}

    def test_catalog_failure_cooldown_returns_cache(self):
        import src.api.cost_plugins.commandcode as cc
        import time as _t
        cc._catalog_cache["by_last_seg"] = {"cached": "cached-model"}
        cc._catalog_cache["loaded_ts"] = 0.0
        cc._catalog_cache["failed_ts"] = _t.time()  # recent failure → cooldown
        with patch("urllib.request.urlopen") as mock_open:
            idx = cc._load_catalog()
            mock_open.assert_not_called()
        assert idx == {"cached": "cached-model"}
