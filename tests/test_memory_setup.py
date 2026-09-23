"""Tests for the memory module Setup integration (src/api/setup memory parts)."""

import contextlib
import os
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


@pytest.fixture(autouse=True)
def _reset_mem_state():
    """Reset the module-level in-flight/terminal install state between tests.

    Includes the router install state too: tests here assign
    ``_router_install``/``_mem_install`` directly and terminal tests leave
    ``_router_last``/``_mem_last`` populated, which leaks into
    other test files (e.g. the setup progress endpoint reads them).
    """
    for attr in ("_mem_install", "_mem_last",
                 "_router_install", "_router_last",
                 ):
        setattr(setup_mod, attr, None)
    yield
    for attr in ("_mem_install", "_mem_last",
                 "_router_install", "_router_last",
                 ):
        setattr(setup_mod, attr, None)


@pytest.fixture
def mock_config():
    cfg = MagicMock()
    cfg.providers = {}
    cfg.raw = {"providers": {}}
    cfg.save = MagicMock()
    return cfg


class TestMemoryPaths:
    def test_memory_site_uses_modules_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LCP_MODULES_DIR", str(tmp_path / "mods"))
        assert setup_mod.memory_site() == str(tmp_path / "mods" / "memory")

    def test_memory_models_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LCP_MODULES_DIR", str(tmp_path / "mods"))
        assert setup_mod.memory_models_dir() == str(tmp_path / "mods" / "models" / "memory")


class TestMemoryStep:
    def test_memory_step_manifest(self, monkeypatch):
        monkeypatch.setenv("LCP_MODULES_DIR", "/tmp/nowhere-memory")
        step = setup_mod.memory_step()
        assert step["kind"] == "module"
        assert step["name"] == "memory"
        # 'installed' depends on the environment (sentence-transformers may or
        # may not be importable), so only assert the shape here.
        assert "installed" in step
        assert step["installing"] is None

    def test_manifest_includes_memory(self, mock_config):
        m = setup_mod.manifest(mock_config)
        names = {mod["name"] for mod in m["modules"]}
        assert {"router", "memory", "runboard", "workspace"} <= names

    def test_router_step_manifest(self, monkeypatch):
        """The semantic-routing module is its own manifest entry."""
        monkeypatch.setenv("LCP_MODULES_DIR", "/tmp/nowhere-router")
        step = setup_mod.router_step()
        assert step["kind"] == "module"
        assert step["name"] == "router"
        assert "installed" in step
        assert step["install_path"].endswith("router")

    def test_router_step_reflects_inflight(self, monkeypatch):
        inflight = {"status": "running", "progress": 40.0}
        with patch.object(setup_mod, "_router_install", inflight), \
             patch("src.api.memory.router_status", return_value={"available": False}):
            step = setup_mod.router_step()
        assert step["installing"] is inflight

    def test_memory_step_reflects_inflight(self, monkeypatch):
        inflight = {"status": "running", "progress": 10.0}
        with patch.object(setup_mod, "_mem_install", inflight), \
             patch("src.api.memory.memory_status", return_value={"available": False}):
            step = setup_mod.memory_step()
        assert step["installing"] is inflight

    def test_memory_step_baked_flag(self, monkeypatch):
        """Baked image: available but not removable -> UI must not offer Remove."""
        with patch("src.api.memory.memory_status",
                   return_value={"available": True, "removable": False}):
            step = setup_mod.memory_step()
        assert step["installed"] is True
        assert step["baked"] is True

    def test_memory_step_runtime_installed_not_baked(self, monkeypatch):
        """Runtime install on a lean image: available AND removable -> Remove OK."""
        with patch("src.api.memory.memory_status",
                   return_value={"available": True, "removable": True}):
            step = setup_mod.memory_step()
        assert step["installed"] is True
        assert step["baked"] is False

    def test_memory_step_not_installed(self, monkeypatch):
        with patch("src.api.memory.memory_status",
                   return_value={"available": False, "removable": False}):
            step = setup_mod.memory_step()
        assert step["installed"] is False
        assert step["baked"] is False

    def test_router_step_baked_flag(self, monkeypatch):
        with patch("src.api.memory.router_status",
                   return_value={"available": True, "removable": False}):
            step = setup_mod.router_step()
        assert step["installed"] is True
        assert step["baked"] is True

    def test_router_step_runtime_installed_not_baked(self, monkeypatch):
        with patch("src.api.memory.router_status",
                   return_value={"available": True, "removable": True}):
            step = setup_mod.router_step()
        assert step["installed"] is True
        assert step["baked"] is False

    def test_router_step_blocked_without_capability_data(self, monkeypatch):
        """Not installed + no matrix readable -> install blocked with a reason."""
        with patch("src.api.memory.router_status",
                   return_value={"available": False, "removable": False}), \
             contextlib.nullcontext():
            step = setup_mod.router_step()
        assert step["installed"] is False
        assert step["blocked_reason"] is not None
        assert "No capability data to route by" in step["blocked_reason"]

    def test_router_step_not_blocked_when_installed(self, monkeypatch):
        """Already installed (baked or runtime) -> never blocked."""
        with patch("src.api.memory.router_status",
                   return_value={"available": True, "removable": True}), \
             contextlib.nullcontext():
            step = setup_mod.router_step()
        assert step["installed"] is True
        assert step["blocked_reason"] is None


class TestRouterInstallBlockedReason:
    def test_blocked_when_matrix_unavailable(self, monkeypatch):
        with contextlib.nullcontext():
            assert setup_mod.router_install_blocked_reason() is not None

    def test_blocked_without_db_path(self, monkeypatch):
        """No db_path to verify the matrix -> conservative block (declare scores)."""
        with contextlib.nullcontext():
            reason = setup_mod.router_install_blocked_reason()
        assert reason is not None
        assert "No capability data to route by" in reason

    def test_unblocked_when_matrix_has_scores(self, monkeypatch):
        """Capability data exists -> installable, whatever else is missing."""
        monkeypatch.setattr("src.api.seed_capabilities.load_capability_matrix",
                            lambda db: {"code_generation": {"m": 0.9}})
        with contextlib.nullcontext():
            assert setup_mod.router_install_blocked_reason("/tmp/x.db") is None

    def test_blocked_matrix_empty(self, monkeypatch):
        monkeypatch.setattr("src.api.seed_capabilities.load_capability_matrix",
                            lambda db: {})
        with contextlib.nullcontext():
            reason = setup_mod.router_install_blocked_reason("/tmp/x.db")
        assert reason is not None
        assert "No capability data to route by" in reason


class TestCapabilityMatrixStats:
    def test_empty_without_db(self):
        assert setup_mod.capability_matrix_stats(None) == {"models": 0, "tasks": 0}

    def test_counts_models_and_tasks(self, monkeypatch):
        matrix = {
            "code_generation": {"m1": 0.9, "m2": 0.8},
            "debugging": {"m1": 0.7},
        }
        monkeypatch.setattr("src.api.seed_capabilities.load_capability_matrix",
                            lambda db: matrix)
        assert setup_mod.capability_matrix_stats("/tmp/x.db") == {"models": 2, "tasks": 2}

    def test_empty_on_error(self, monkeypatch):
        def _boom(db):
            raise RuntimeError("no db")
        monkeypatch.setattr("src.api.seed_capabilities.load_capability_matrix", _boom)
        assert setup_mod.capability_matrix_stats("/tmp/x.db") == {"models": 0, "tasks": 0}


class TestMemoryInstallCoordinator:
    def test_mem_progress_idle_by_default(self):
        assert setup_mod.mem_progress() is None
        assert setup_mod.mem_last() is None

    def test_start_memory_install_spawns_thread(self, temp_db):
        with patch.object(setup_mod, "_run_memory_install", lambda engine: None):
            state = setup_mod.start_memory_install(temp_db)
        assert state["status"] in ("queued", "running")

    def test_mem_finish_marks_terminal(self, monkeypatch):
        monkeypatch.setattr(setup_mod, "_mem_install", {
            "status": "running", "progress": 42.0, "detail": "x", "log": ["a"],
        })
        setup_mod._mem_finish("done", "Memory installed.")
        assert setup_mod.mem_progress() is None
        last = setup_mod.mem_last()
        assert last is not None
        assert last["status"] == "done"
        assert last["progress"] == 100.0

    def test_mem_update_clamps_progress(self, monkeypatch):
        monkeypatch.setattr(setup_mod, "_mem_install", {
            "status": "running", "progress": 0.0, "detail": "", "log": [],
        })
        setup_mod._mem_update(None, progress=999.0)
        assert setup_mod._mem_install["progress"] == 100.0

    def test_run_memory_install_success(self, temp_db, monkeypatch, tmp_path):
        monkeypatch.setenv("LCP_MODULES_DIR", str(tmp_path / "mods"))
        monkeypatch.setattr(setup_mod.os, "makedirs", lambda *a, **k: None)
        monkeypatch.setattr(setup_mod, "_stream_mem", lambda *a, **k: None)
        monkeypatch.setattr("src.api.memory.memory_available", lambda site=None: True)
        # Skip the model pre-download subprocess.
        def _no_popen(*a, **k):
            raise FileNotFoundError("skip model")
        monkeypatch.setattr(setup_mod.subprocess, "Popen", _no_popen)
        setup_mod._mem_install = {
            "status": "running", "progress": 0.0, "detail": "", "log": [],
        }

        setup_mod._run_memory_install(temp_db)
        assert setup_mod.mem_last()["status"] == "done"
        assert setup_mod.load_state(temp_db)["module:memory"]["status"] == "done"

    def test_run_memory_install_failure(self, temp_db, monkeypatch, tmp_path):
        monkeypatch.setenv("LCP_MODULES_DIR", str(tmp_path / "mods"))
        monkeypatch.setattr(setup_mod.os, "makedirs", lambda *a, **k: None)
        def _boom(*a, **k):
            raise Exception("pip exploded")
        monkeypatch.setattr(setup_mod, "_stream_mem", _boom)
        setup_mod._mem_install = {
            "status": "running", "progress": 0.0, "detail": "", "log": [],
        }

        setup_mod._run_memory_install(temp_db)
        assert setup_mod.mem_last()["status"] == "failed"


class TestRemoveMemory:
    def test_remove_memory_deletes_dirs(self, temp_db, monkeypatch, tmp_path):
        mods = tmp_path / "mods"
        site = mods / "memory"
        models = mods / "models" / "memory"
        site.mkdir(parents=True)
        models.mkdir(parents=True)
        (site / "x").write_text("")
        monkeypatch.setenv("LCP_MODULES_DIR", str(mods))
        # Lean image: deps live in the --target dir only, not global
        # site-packages — so the baked-uninstall branch must not trigger.
        monkeypatch.setattr("src.api.memory.memory_available", lambda site=None: False)
        monkeypatch.setattr("src.api.memory.router_available", lambda site=None: False)

        result = setup_mod.remove_memory(temp_db)
        assert result["removed"] is True
        assert not site.exists()
        assert not models.exists()
        assert setup_mod.load_state(temp_db)["module:memory"]["status"] == "removed"

    def test_remove_memory_noop_when_absent(self, temp_db, monkeypatch, tmp_path):
        monkeypatch.setenv("LCP_MODULES_DIR", str(tmp_path / "mods"))
        monkeypatch.setattr("src.api.memory.memory_available", lambda site=None: False)
        monkeypatch.setattr("src.api.memory.router_available", lambda site=None: False)
        result = setup_mod.remove_memory(temp_db)
        assert result["removed"] is True
        assert result["paths"] == []

    def test_remove_memory_baked_uninstalls_deps(self, temp_db, monkeypatch):
        """Baked image: removing uninstalls the baked deps from site-packages."""
        monkeypatch.setattr("src.api.memory.memory_available", lambda site=None: True)
        monkeypatch.setattr("src.api.memory.router_available", lambda site=None: False)
        uninstalled = []
        monkeypatch.setattr(
            setup_mod, "_uninstall_baked_packages",
            lambda pkgs: uninstalled.extend(pkgs) or pkgs,
        )

        result = setup_mod.remove_memory(temp_db)
        assert uninstalled == setup_mod.BAKED_MEMORY_PACKAGES
        assert result["removed"] is True
        assert setup_mod.load_state(temp_db)["module:memory"]["status"] == "removed"

    def test_remove_memory_baked_blocked_when_router_baked(self, temp_db, monkeypatch):
        """Both baked: removal refused (shared sentence-transformers/torch)."""
        monkeypatch.setattr("src.api.memory.memory_available", lambda site=None: True)
        monkeypatch.setattr("src.api.memory.router_available", lambda site=None: True)
        with pytest.raises(setup_mod.SetupError, match="Semantic routing is also baked"):
            setup_mod.remove_memory(temp_db)


class TestRouterInstallCoordinator:
    def test_router_progress_idle_by_default(self):
        assert setup_mod.router_progress() is None
        assert setup_mod.router_last() is None

    def test_start_router_install_spawns_thread(self, temp_db):
        with patch.object(setup_mod, "_run_router_install", lambda engine: None):
            state = setup_mod.start_router_install(temp_db)
        assert state["status"] in ("queued", "running")

    def test_router_finish_marks_terminal(self, monkeypatch):
        monkeypatch.setattr(setup_mod, "_router_install", {
            "status": "running", "progress": 42.0, "detail": "x", "log": ["a"],
        })
        setup_mod._router_finish("done", "Semantic routing installed.")
        assert setup_mod.router_progress() is None
        last = setup_mod.router_last()
        assert last is not None
        assert last["status"] == "done"
        assert last["progress"] == 100.0

    def test_run_router_install_success(self, temp_db, monkeypatch, tmp_path):
        monkeypatch.setenv("LCP_MODULES_DIR", str(tmp_path / "mods"))
        monkeypatch.setattr(setup_mod.os, "makedirs", lambda *a, **k: None)
        monkeypatch.setattr(setup_mod, "_stream_router", lambda *a, **k: None)
        monkeypatch.setattr("src.api.memory.router_available", lambda site=None: True)
        def _no_popen(*a, **k):
            raise FileNotFoundError("skip model")
        monkeypatch.setattr(setup_mod.subprocess, "Popen", _no_popen)
        setup_mod._router_install = {
            "status": "running", "progress": 0.0, "detail": "", "log": [],
        }

        setup_mod._run_router_install(temp_db)
        assert setup_mod.router_last()["status"] == "done"
        assert setup_mod.load_state(temp_db)["module:router"]["status"] == "done"

    def test_run_router_install_failure(self, temp_db, monkeypatch, tmp_path):
        monkeypatch.setenv("LCP_MODULES_DIR", str(tmp_path / "mods"))
        monkeypatch.setattr(setup_mod.os, "makedirs", lambda *a, **k: None)
        def _boom(*a, **k):
            raise Exception("pip exploded")
        monkeypatch.setattr(setup_mod, "_stream_router", _boom)
        setup_mod._router_install = {
            "status": "running", "progress": 0.0, "detail": "", "log": [],
        }

        setup_mod._run_router_install(temp_db)
        assert setup_mod.router_last()["status"] == "failed"


class TestRemoveRouter:
    def test_remove_router_deletes_dirs(self, temp_db, monkeypatch, tmp_path):
        mods = tmp_path / "mods"
        site = mods / "router"
        models = mods / "models" / "router"
        site.mkdir(parents=True)
        models.mkdir(parents=True)
        (site / "x").write_text("")
        monkeypatch.setenv("LCP_MODULES_DIR", str(mods))
        # Lean image: deps live in the --target dir only — no baked uninstall.
        monkeypatch.setattr("src.api.memory.router_available", lambda site=None: False)
        monkeypatch.setattr("src.api.memory.memory_available", lambda site=None: False)

        result = setup_mod.remove_router(temp_db)
        assert result["removed"] is True
        assert not site.exists()
        assert not models.exists()
        assert setup_mod.load_state(temp_db)["module:router"]["status"] == "removed"

    def test_remove_router_noop_when_absent(self, temp_db, monkeypatch, tmp_path):
        monkeypatch.setenv("LCP_MODULES_DIR", str(tmp_path / "mods"))
        monkeypatch.setattr("src.api.memory.router_available", lambda site=None: False)
        monkeypatch.setattr("src.api.memory.memory_available", lambda site=None: False)
        result = setup_mod.remove_router(temp_db)
        assert result["removed"] is True
        assert result["paths"] == []

    def test_remove_router_baked_uninstalls_deps(self, temp_db, monkeypatch):
        """Baked image: removing uninstalls the baked deps from site-packages."""
        monkeypatch.setattr("src.api.memory.router_available", lambda site=None: True)
        monkeypatch.setattr("src.api.memory.memory_available", lambda site=None: False)
        uninstalled = []
        monkeypatch.setattr(
            setup_mod, "_uninstall_baked_packages",
            lambda pkgs: uninstalled.extend(pkgs) or pkgs,
        )

        result = setup_mod.remove_router(temp_db)
        assert uninstalled == setup_mod.BAKED_ROUTER_PACKAGES
        assert result["removed"] is True
        assert setup_mod.load_state(temp_db)["module:router"]["status"] == "removed"

    def test_remove_router_baked_blocked_when_memory_baked(self, temp_db, monkeypatch):
        """Both baked: removal refused (shared sentence-transformers/torch)."""
        monkeypatch.setattr("src.api.memory.router_available", lambda site=None: True)
        monkeypatch.setattr("src.api.memory.memory_available", lambda site=None: True)
        with pytest.raises(setup_mod.SetupError, match="Memory module is also baked"):
            setup_mod.remove_router(temp_db)


def test_uninstall_baked_packages_parses_success(monkeypatch):
    """The pip-uninstall helper reports the packages pip actually removed."""
    import subprocess as sp
    fake = type("R", (), {
        "stdout": (
            "Successfully uninstalled sentence-transformers-6.0.0\n"
            "Successfully uninstalled torch-2.13.0\n"
        ),
    })()
    monkeypatch.setattr(setup_mod.subprocess, "run", lambda *a, **k: fake)
    out = setup_mod._uninstall_baked_packages(["sentence-transformers", "torch"])
    assert out == ["sentence-transformers-6.0.0", "torch-2.13.0"]


def test_uninstall_baked_packages_never_raises(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("pip exploded")
    monkeypatch.setattr(setup_mod.subprocess, "run", _boom)
    assert setup_mod._uninstall_baked_packages(["torch"]) == []


class TestMemoryAvailableProbe:
    def test_memory_available_false_when_missing(self, monkeypatch):
        monkeypatch.setattr(setup_mod.subprocess, "run",
                            lambda *a, **k: type("R", (), {"returncode": 1})())
        from src.api import memory as mem_mod
        assert mem_mod.memory_available() is False

    def test_memory_available_true_when_importable(self, monkeypatch):
        monkeypatch.setattr(setup_mod.subprocess, "run",
                            lambda *a, **k: type("R", (), {"returncode": 0})())
        from src.api import memory as mem_mod
        assert mem_mod.memory_available() is True

    def test_router_available_probe(self, monkeypatch):
        monkeypatch.setattr(setup_mod.subprocess, "run",
                            lambda *a, **k: type("R", (), {"returncode": 0})())
        from src.api import memory as mem_mod
        assert mem_mod.router_available() is True
        monkeypatch.setattr(setup_mod.subprocess, "run",
                            lambda *a, **k: type("R", (), {"returncode": 1})())
        assert mem_mod.router_available() is False

