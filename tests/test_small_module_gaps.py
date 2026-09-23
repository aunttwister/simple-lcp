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

class TestSetupFailurePaths:
    def test_run_install_clone_failure(self, tmp_path, monkeypatch):
        import subprocess
        from src.api import setup as setup_mod
        from src.api.models import get_engine, Base

        engine = get_engine(str(tmp_path / "s.db"))
        Base.metadata.create_all(engine)

        # Save pristine globals so _bench_finish writes don't leak across tests.
        orig_install = setup_mod._bench_install
        orig_last = setup_mod._bench_last
        try:
            monkeypatch.setattr(setup_mod.os, "environ", {"LCP_MODULES_DIR": str(tmp_path / "mods")})
            monkeypatch.setattr("os.makedirs", lambda *a, **k: None)
            monkeypatch.setattr("os.path.isdir", lambda _: False)
            monkeypatch.setattr("shutil.rmtree", lambda *a, **k: None)
            monkeypatch.setattr(setup_mod, "_bench_install", {
                "status": "running", "progress": 0.0, "detail": "", "log": ["cloning..."],
            })

            def fake_stream(cmd, cwd=None, start=0, end=0, status_msg=""):
                raise subprocess.CalledProcessError(128, cmd)

            monkeypatch.setattr(setup_mod, "_stream", fake_stream)
            setup_mod._run_livebench_install(engine)
            last = setup_mod.bench_last()
            assert last is not None and last["status"] == "failed"
        finally:
            setup_mod._bench_install = orig_install
            setup_mod._bench_last = orig_last

    def test_run_install_core_not_importable(self, tmp_path, monkeypatch):
        from src.api import setup as setup_mod
        from src.api.models import get_engine, Base

        engine = get_engine(str(tmp_path / "s.db"))
        Base.metadata.create_all(engine)

        orig_install = setup_mod._bench_install
        orig_last = setup_mod._bench_last
        try:
            monkeypatch.setattr(setup_mod.os, "environ", {"LCP_MODULES_DIR": str(tmp_path / "mods")})
            monkeypatch.setattr("os.makedirs", lambda *a, **k: None)
            monkeypatch.setattr("os.path.isdir", lambda _: False)
            monkeypatch.setattr("os.path.isfile", lambda p: p.endswith("run_livebench.py") or p.endswith("pyproject.toml"))
            monkeypatch.setattr("shutil.rmtree", lambda *a, **k: None)
            monkeypatch.setattr(setup_mod, "_bench_install", {
                "status": "running", "progress": 0.0, "detail": "", "log": ["cloning..."],
            })

            def fake_stream(cmd, cwd=None, start=0, end=0, status_msg=""):
                pass

            monkeypatch.setattr(setup_mod, "_stream", fake_stream)
            monkeypatch.setattr("src.api.benchmark.core_deps_available", lambda site=None: False)
            setup_mod._run_livebench_install(engine)
            last = setup_mod.bench_last()
            assert last is not None and last["status"] == "failed"
            combined = (last.get("detail") or "") + " " + " ".join(last.get("log") or [])
            assert "LiveBench core install did not take effect" in combined
        finally:
            setup_mod._bench_install = orig_install
            setup_mod._bench_last = orig_last


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
