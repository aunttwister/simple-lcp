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
def _admin_auth_optout(monkeypatch):
    """Run the existing suite with admin auth explicitly disabled.

    The admin auth work (2026-10-18) put a guard in front of every mutating and
    admin route, which turned 311 previously-passing tests red — not because the
    guard is wrong, but because those tests exercise admin endpoints with no
    credentials. That *is* the vulnerability being fixed, so they cannot simply
    be pointed at a live token.

    Two options were considered:
      * mint a real session cookie in each test — invasive, and it would hide the
        fact that the endpoints are now guarded;
      * set the documented dev opt-out (``LCP_ALLOW_ANONYMOUS_ADMIN=1``) — the
        same switch an operator uses for local development, so the tests run
        through a supported configuration rather than a test-only backdoor.

    This fixture takes the second route. The dedicated auth tests
    (``tests/test_admin_auth*.py``) DELETE the env var for themselves, so the
    guard's real behaviour is still covered rather than masked.
    """
    monkeypatch.setenv("LCP_ALLOW_ANONYMOUS_ADMIN", "1")
    monkeypatch.delenv("LCP_ADMIN_TOKEN", raising=False)
    yield


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


@pytest.fixture(autouse=True)
def _remote_intent_model(monkeypatch):
    """Point the intent decision model at the local-classifier oracle.

    The local bge-small classifier is obsoleted from the decision path
    (CUR-327): classification is a remote HTTP call now. The router tests
    assert the semantic answers, so the oracle serves the SAME classifier
    behind the lcp-v1 wire contract — the tests verify the outcomes through
    the path production uses. Tests that exercise the structural fallback
    still work: the oracle applies the classifier's own gate, so sub-gate
    messages fall through exactly as before.

    Two layers, so boot/identity tests keep holding:
      1. class level — a REAL SettingsStore answers the intent-model queries
         from the oracle (``get_settings() is store`` stays true);
      2. module level — when no store exists at all (pure unit tests), the
         accessor returns the oracle duck-type instead of None.

    A test can still opt out by patching get_settings or the class methods
    inside itself.
    """
    import sys as _sys
    import os as _os
    _sys.path.insert(0, _os.path.dirname(__file__))
    from tests import _intent_oracle  # noqa:PLC0415
    import importlib  # noqa:PLC0415

    oracle_methods = {
        "get_intent_model_enabled": lambda self, profile=None, default=False: True,
        "get_intent_model_url": lambda self, profile=None, default=None: _intent_oracle.oracle_base_url(),
        "get_intent_model_format": lambda self, profile=None: "lcp-v1",
        "get_intent_model_api_key": lambda self, profile=None, default=None: None,
        "get_intent_model_timeout_ms": lambda self, profile=None: 10000,
        "get_intent_model_min_score": lambda self, profile=None: _intent_oracle.oracle_min_score(),
    }

    for _mod_name in ("src.api.cost_cache", "api.cost_cache"):
        try:
            _cc = importlib.import_module(_mod_name)
        except ImportError:
            continue
        store_cls = getattr(_cc, "SettingsStore", None)
        if store_cls is not None:
            for _name, _fn in oracle_methods.items():
                monkeypatch.setattr(store_cls, _name, _fn, raising=False)
        orig = _cc.get_settings

        def _patched(_orig=orig):
            return _orig() or _intent_oracle.OracleSettings()

        monkeypatch.setattr(_cc, "get_settings", _patched, raising=True)
