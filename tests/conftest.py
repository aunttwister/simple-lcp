"""Shared test fixtures and configuration."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def temp_dir():
    """Temporary directory that cleans up after test."""
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def cron_snapshot(tmp_path, monkeypatch):
    """A cron snapshot at a path the test controls.

    The Cron tab reads a snapshot the *host-side* collector writes
    (``<tasks_dir>/.work-layers/cron-jobs.json``). Without one the view reports
    ``available: False`` and the tab renders its "snapshot missing" state, so any
    test asserting what the tab shows was really asserting "this host has cron
    data" — which is true on the dev box and false on a CI runner. That is how
    two tests went red in CI at M2b while passing locally for eleven pushes.

    Tests that care what the Cron tab renders take this fixture; tests that care
    what it renders *without* data should keep it out and assert the degraded
    state instead.
    """
    import json

    from src.api import work_cron

    snap = tmp_path / "cron-jobs.json"
    snap.write_text(json.dumps({
        "generated_at": "2026-09-24T00:00:00+00:00",
        "counts": {"total": 2, "active": 1, "paused": 1, "disabled": 0, "error": 0},
        "profiles": [{
            "profile": "l2",
            "legacy": False,
            "hidden": False,
            "jobs": [
                {"id": "nightly", "name": "nightly digest", "status": "active"},
                {"id": "paused-one", "name": "paused job", "status": "paused"},
            ],
        }],
    }))
    monkeypatch.setattr(work_cron, "_snapshot_path", lambda: str(snap))
    return snap


@pytest.fixture
def config_store(temp_dir):
    """A SettingsStore bound to a temp DB, seeded with a minimal config.

    The DB-backed Config reads sections from here; any section we don't seed
    falls back to the Python SEED_CONFIG defaults.
    """
    from src.api.models import Base, get_engine
    from src.api.cost_cache import SettingsStore
    db_path = str(temp_dir / "test.db")
    e = get_engine(db_path)
    Base.metadata.create_all(e)
    store = SettingsStore(e)
    store.set_config_section("profiles", {
        "l2": {
            "forbidden_tools": ["write_file"],
            "chain": [
                {"provider": "test_prov", "model": "test-model", "base_url": "https://test.api/v1"}
            ],
        },
        "l1": {
            "forbidden_tools": ["terminal"],
            "chain": [
                {"provider": "test_prov", "model": "test-model-flash", "base_url": "https://test.api/v1"}
            ],
        },
    })
    store.set_config_section("providers", {
        "test_prov": {
            "api_key_env": "TEST_API_KEY",
            "api_base": "https://test.api/v1",
            "models": ["test-model", "test-model-flash"],
        }
    })
    store.set_config_section("pricing", [
        {"provider": "test_prov", "model": "test-model", "cache_hit": 0.01, "cache_miss": 0.5, "output": 1.0},
        {"provider": "test_prov", "model": "test-model-flash", "cache_hit": 0.005, "cache_miss": 0.1, "output": 0.2},
    ])
    store.set_config_section("circuit_breaker", {
        "failures_degraded": 3,
        "failures_dead": 6,
        "degraded_cooldown_seconds": 30,
        "dead_cooldown_seconds": 120,
    })
    return store


@pytest.fixture
def mock_config(config_store):
    """Return a DB-backed Config from a temp settings store."""
    from src.api.config import Config
    return Config(store=config_store)


@pytest.fixture
def temp_db(temp_dir):
    """Create a temporary SQLite database and return its path + engine."""
    from src.api.models import get_engine, Base
    db_path = str(temp_dir / "test.db")
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    return db_path, engine


@pytest.fixture
def mock_handler():
    """Create a minimal mock HTTP handler for testing."""
    h = MagicMock()
    h.send_response = MagicMock()
    h.send_header = MagicMock()
    h.end_headers = MagicMock()
    h.wfile = MagicMock()
    h.rfile = MagicMock()
    h.headers = {}
    h.path = "/"
    return h


@pytest.fixture(autouse=True)
def _init_circuit_breaker():
    """Initialize the circuit breaker singleton before each test."""
    from unittest.mock import MagicMock
    from src.api.circuit_breaker import get_circuit_breaker
    cfg = MagicMock()
    cfg.circuit_breaker = {
        "failures_dead": 5,
        "dead_cooldown_seconds": 300,
        "failures_degraded": 3,
        "degraded_cooldown_seconds": 60,
    }
    get_circuit_breaker(cfg)


@pytest.fixture(autouse=True)
def _reset_credential_store():
    """Reset the credential store singleton before each test."""
    import src.api.credential_store as cs
    cs._credential_store = None
    yield
    cs._credential_store = None


@pytest.fixture(autouse=True)
def _reset_active_runtime():
    """Reset the central active-runtime accessor after EVERY test.

    Facades call ``runtime.bind_active_runtime(rt)`` when a runtime is bound;
    without this reset a runtime from one test would leak into the next and
    make the request path (resolve_service) resolve stale runtime-owned
    services instead of the legacy singletons the test seeded.
    """
    import src.api.runtime as runtime
    yield
    runtime._active_runtime = None
