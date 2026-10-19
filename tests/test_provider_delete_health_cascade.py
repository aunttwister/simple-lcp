"""A deleted provider must not leave circuit-breaker health behind.

Regression tests for the "llamacpp ghost". ``DELETE /api/providers/<name>``
removed the config entry, every profile chain step referencing it and its
stored credential — but never its ``provider_health`` rows. Nothing else ever
deletes those rows, and ``attach_engine()`` materializes **every** persisted row
at boot, so ``/health`` and ``/api/providers/health`` kept listing a provider
that no longer existed — across restarts, forever. Three ``llamacpp`` rows
survived (including ``degraded`` / ``consecutive_failures=6`` on the ``coder``
profile), which is why a daily report kept naming a provider that had already
been removed from the config.

The load-bearing test here is ``test_ghost_does_not_return_after_restart``: it
re-creates the breaker against the same DB, which is exactly what a container
rebuild does, and asserts the ghost does not come back.
"""
import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from src.server import LCPHandler
from src.api.runtime import resolve_service
from src.api.models import Base, ProviderHealth, get_engine, get_session
from src.api import circuit_breaker as cb_module
from src.api.circuit_breaker import CircuitBreaker, get_circuit_breaker


# ── fixtures / helpers ──────────────────────────────────────────────────────

@pytest.fixture
def temp_db():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
    for ext in ("", "-wal", "-shm"):
        try:
            os.unlink(db_path + ext)
        except FileNotFoundError:
            pass


@pytest.fixture(autouse=True)
def _reset_breaker_singleton():
    """The breaker is a module singleton — isolate it per test."""
    cb_module._circuit_breaker = None
    yield
    cb_module._circuit_breaker = None


def _cfg(providers=("llamacpp", "deepseek"), profiles=("l2",)):
    """Mock config with REAL dicts where the code under test indexes into them.

    ``circuit_breaker`` thresholds must be ints (compared with ``>=``), and
    ``raw`` must be a real dict because the delete handler mutates it.
    """
    cfg = MagicMock()
    cfg.circuit_breaker = {
        "failures_degraded": 3, "failures_dead": 6,
        "degraded_cooldown_seconds": 60, "dead_cooldown_seconds": 300,
    }
    cfg.raw = {
        "providers": {n: {"api_base": f"https://{n}/v1"} for n in providers},
        "profiles": {
            p: {"chain": [{"provider": n, "model": "m"} for n in providers]}
            for p in profiles
        },
    }
    cfg.providers = dict(cfg.raw["providers"])
    cfg.profiles = dict(cfg.raw["profiles"])
    cfg.save = MagicMock()
    return cfg


def _rows(engine):
    """Every persisted provider_health row as sorted (provider, profile) pairs."""
    with get_session(engine) as session:
        return sorted((r.provider, r.profile) for r in session.query(ProviderHealth).all())


def _seed(cb, provider, url, profiles=("l2",), failures=0):
    """Create a real health row per profile (via the breaker, like production)."""
    for profile in profiles:
        if failures:
            for _ in range(failures):
                cb.record_failure(provider, url, profile,
                                  error_type="ProviderTimeoutError",
                                  error_reason="HTTP 504")
        else:
            cb.record_success(provider, url, profile)


class _Handler(LCPHandler):
    """In-process handler that skips socketserver auto-handle."""

    def __init__(self, path="/", method="GET", engine=None):
        self.path = path
        self.command = method
        self.headers = {}
        self.request_version = "HTTP/1.1"
        self.requestline = f"{method} {path} HTTP/1.1"
        self.raw_requestline = self.requestline.encode()
        self.client_address = ("127.0.0.1", 0)
        self.send_response = MagicMock()
        self.send_header = MagicMock()
        self.end_headers = MagicMock()
        self.wfile = MagicMock()
        self.wfile.write = MagicMock()
        self.rfile = MagicMock()
        self.rfile.read = MagicMock(return_value=b"{}")
        self._write_chunk = MagicMock()
        self.engine = engine
        self.log_error = MagicMock()


def _json(handler):
    data = b""
    for call in handler.wfile.write.call_args_list:
        arg = call[0][0]
        data += arg.encode() if isinstance(arg, str) else bytes(arg)
    return json.loads(data)


# ── CircuitBreaker.forget_provider ──────────────────────────────────────────

class TestForgetProvider:
    def test_drops_only_the_named_provider(self, temp_db):
        cb = CircuitBreaker(_cfg())
        cb.attach_engine(temp_db)
        _seed(cb, "llamacpp", "https://llamacpp/v1", profiles=("l2", "coder"))
        _seed(cb, "deepseek", "https://deepseek/v1", profiles=("l2",))
        assert _rows(temp_db) == [
            ("deepseek", "l2"), ("llamacpp", "coder"), ("llamacpp", "l2"),
        ]

        removed = cb.forget_provider("llamacpp")

        assert removed == 2
        assert _rows(temp_db) == [("deepseek", "l2")]
        assert [k[0] for k in cb.get_all_health()] == ["deepseek"]

    def test_ghost_does_not_return_after_restart(self, temp_db):
        """The load-bearing test: a rebuild re-attaches the engine and reloads
        every row. If the delete left rows behind, the provider reappears."""
        cb = CircuitBreaker(_cfg())
        cb.attach_engine(temp_db)
        _seed(cb, "llamacpp", "https://llamacpp/v1", profiles=("l2", "coder"), failures=3)
        _seed(cb, "deepseek", "https://deepseek/v1", profiles=("l2",))

        cb.forget_provider("llamacpp")

        fresh = CircuitBreaker(_cfg())          # simulates the rebuilt container
        fresh.attach_engine(temp_db)            # reloads ALL persisted rows
        assert [k[0] for k in fresh.get_all_health()] == ["deepseek"]
        assert _rows(temp_db) == [("deepseek", "l2")]

    def test_removes_every_profile_and_base_url_variant(self, temp_db):
        cb = CircuitBreaker(_cfg())
        cb.attach_engine(temp_db)
        _seed(cb, "llamacpp", "https://a/v1", profiles=("l2", "l1", "coder"))
        _seed(cb, "llamacpp", "https://b/v1", profiles=("l2",))

        assert cb.forget_provider("llamacpp") == 4
        assert _rows(temp_db) == []

    def test_unknown_provider_is_a_noop(self, temp_db):
        cb = CircuitBreaker(_cfg())
        cb.attach_engine(temp_db)
        _seed(cb, "deepseek", "https://deepseek/v1")

        assert cb.forget_provider("never-existed") == 0
        assert _rows(temp_db) == [("deepseek", "l2")]

    def test_without_engine_clears_memory_only(self):
        """Legacy/tests path: no engine attached must not raise."""
        cb = CircuitBreaker(_cfg())
        _seed(cb, "llamacpp", "https://llamacpp/v1", profiles=("l2",))

        assert cb.forget_provider("llamacpp") == 1
        assert cb.get_all_health() == {}


# ── the HTTP delete path ────────────────────────────────────────────────────

class TestProviderDeleteCascade:
    def _wire(self, cfg, temp_db):
        """Bind the real resolution path the handler uses, and prove it binds."""
        cb = get_circuit_breaker(cfg)
        cb.attach_engine(temp_db)
        # Same contract the health endpoints rely on (fallback == our breaker);
        # if a runtime ever gets bound here, fail loudly instead of silently
        # testing a different instance.
        assert resolve_service("circuit_breaker",
                               fallback=get_circuit_breaker) is cb
        LCPHandler.config = cfg
        LCPHandler.engine = temp_db
        return cb

    def test_delete_endpoint_removes_health_rows(self, temp_db):
        cfg = _cfg()
        cb = self._wire(cfg, temp_db)
        _seed(cb, "llamacpp", "https://llamacpp/v1", profiles=("l2", "coder"), failures=3)
        _seed(cb, "deepseek", "https://deepseek/v1", profiles=("l2",))

        h = _Handler("/api/providers/llamacpp", method="DELETE", engine=temp_db)
        h.do_DELETE()

        data = _json(h)
        assert data == {"ok": True, "deleted": "llamacpp", "health_rows_removed": 2}
        assert _rows(temp_db) == [("deepseek", "l2")]
        assert "llamacpp" not in cfg.raw["providers"]

    def test_delete_endpoint_clears_chain_references(self, temp_db):
        cfg = _cfg()
        self._wire(cfg, temp_db)

        h = _Handler("/api/providers/llamacpp", method="DELETE", engine=temp_db)
        h.do_DELETE()

        assert [c["provider"] for c in cfg.raw["profiles"]["l2"]["chain"]] == ["deepseek"]

    def test_health_listing_no_longer_shows_the_deleted_provider(self, temp_db):
        cfg = _cfg()
        cb = self._wire(cfg, temp_db)
        _seed(cb, "llamacpp", "https://llamacpp/v1", profiles=("l2",))
        _seed(cb, "deepseek", "https://deepseek/v1", profiles=("l2",))

        _Handler("/api/providers/llamacpp", method="DELETE", engine=temp_db).do_DELETE()

        h = _Handler("/api/providers/health", engine=temp_db)
        h.do_GET()
        listed = {p["provider"] for p in _json(h)["providers"].values()} \
            if isinstance(_json(h)["providers"], dict) else {
                p["provider"] for p in _json(h)["providers"]
            }
        assert "llamacpp" not in listed
        assert "deepseek" in listed

    def test_delete_unknown_provider_is_404_and_keeps_other_health(self, temp_db):
        cfg = _cfg()
        cb = self._wire(cfg, temp_db)
        _seed(cb, "deepseek", "https://deepseek/v1", profiles=("l2",))

        h = _Handler("/api/providers/ghost", method="DELETE", engine=temp_db)
        h.do_DELETE()

        assert h.send_response.call_args[0][0] == 404
        assert _rows(temp_db) == [("deepseek", "l2")]

    def test_cascade_failure_does_not_break_the_delete(self, temp_db, monkeypatch):
        """Health bookkeeping is best-effort — a broken breaker must not turn a
        working provider delete into a 500."""
        cfg = _cfg()
        self._wire(cfg, temp_db)
        monkeypatch.setattr(CircuitBreaker, "forget_provider",
                            lambda self, name: (_ for _ in ()).throw(RuntimeError("db gone")))

        h = _Handler("/api/providers/llamacpp", method="DELETE", engine=temp_db)
        h.do_DELETE()

        data = _json(h)
        assert data["ok"] is True and data["deleted"] == "llamacpp"
        assert data["health_rows_removed"] == 0


# ── the setup-module delete path ────────────────────────────────────────────

