"""Reads of provider health must not create health entries.

Regression tests for the phantom ``"/"`` row in ``/health``.

``CircuitBreaker.get_health()`` was get-or-create and ``status_of()`` /
``is_available()`` both went through it, so a *read* materialized state. The
routing tiebreakers evaluate every chain step with
``cb.status_of(step["provider"], base_url, profile or "")``, and a step whose
provider is empty inserted ``("", base_url, "")`` — which the ``/health``
renderer formats as ``f"{provider}/{profile}"`` → ``"/"``. Observed live:
``/health`` returned 11 keys where only 10 providers exist, and the extra one was
``"/"`` with zero successes and zero failures.

The fix is structural, not cosmetic:

* ``peek()`` reads without inserting (returns a detached default view).
* ``status_of()`` / ``is_available()`` use ``peek()`` and answer an empty
  provider without a lookup at all, so no *future* caller can leak a key
  either.
* ``get_health()`` stays the write path, used by ``record_success()``,
  ``record_failure()`` and the manual override.

``test_a_routing_read_leaves_no_phantom_key`` is the load-bearing one: it drives
the exact tiebreaker shapes and then reads both health endpoints.
"""
import json
import os
import tempfile
import time
from unittest.mock import MagicMock

import pytest

from src.server import LCPHandler
from src.api.runtime import resolve_service
from src.api.models import Base, get_engine
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


def _cfg(provider="deepseek"):
    cfg = MagicMock()
    cfg.circuit_breaker = {
        "failures_degraded": 3, "failures_dead": 6,
        "degraded_cooldown_seconds": 60, "dead_cooldown_seconds": 300,
    }
    cfg.raw = {
        "providers": {provider: {"api_base": f"https://{provider}/v1"}},
        "profiles": {"l2": {"chain": [{"provider": provider, "model": "m"}]}},
    }
    cfg.providers = dict(cfg.raw["providers"])
    cfg.profiles = dict(cfg.raw["profiles"])
    cfg.save = MagicMock()
    return cfg


def _breaker(cfg, engine):
    """A breaker on the real resolution path the handlers use."""
    cb = get_circuit_breaker(cfg)
    cb.attach_engine(engine)
    assert resolve_service("circuit_breaker", fallback=get_circuit_breaker) is cb
    return cb


def _rendered_keys(cb):
    """The key format ``/health`` renders: f"{provider}/{profile}"."""
    return {f"{k[0]}/{k[2]}" for k in cb.get_all_health()}


def _routing_reads(cb, provider, base_url, profile):
    """Every read the routing tiebreakers and the request pipeline perform."""
    cb.status_of(provider, base_url, profile)          # router 750/994
    cb.is_available(provider, base_url, profile)       # router 773
    cb.status_of(provider, base_url, profile)          # pipeline 717/850
    cb.is_available(provider, base_url, profile)       # pipeline 839


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


# ── the phantom key ────────────────────────────────────────────────────────

class TestReadsDoNotMaterialize:
    def test_status_of_leaves_an_unknown_provider_untracked(self):
        cb = CircuitBreaker(_cfg())
        assert cb.status_of("deepseek", "https://deepseek/v1", "l2") == "healthy"
        assert cb.get_all_health() == {}

    def test_is_available_leaves_an_unknown_provider_untracked(self):
        cb = CircuitBreaker(_cfg())
        assert cb.is_available("deepseek", "https://deepseek/v1", "l2") is True
        assert cb.get_all_health() == {}

    def test_empty_provider_is_never_tracked(self):
        cb = CircuitBreaker(_cfg())
        # The exact shape the tiebreakers produce: provider="" and profile="".
        _routing_reads(cb, "", "https://deepseek/v1", "")
        _routing_reads(cb, "", "", "")
        assert cb.get_all_health() == {}
        assert "/" not in _rendered_keys(cb)

    def test_a_routing_read_leaves_no_phantom_key(self, temp_db):
        """The load-bearing test: reads must not add a key to either endpoint."""
        cfg = _cfg()
        cb = _breaker(cfg, temp_db)
        # One real provider, tracked the way production tracks it.
        cb.record_success("deepseek", "https://deepseek/v1", "l2")
        before = set(cb.get_all_health())
        assert _rendered_keys(cb) == {"deepseek/l2"}

        # Tiebreakers walk the chain, including steps with no provider.
        for provider in ("deepseek", "", "None", "unknown-provider"):
            _routing_reads(cb, provider, "https://deepseek/v1", "")

        assert set(cb.get_all_health()) == before, "a read created health state"
        assert _rendered_keys(cb) == {"deepseek/l2"}

        LCPHandler.config = cfg
        LCPHandler.engine = temp_db
        for path in ("/health", "/api/providers/health"):
            handler = _Handler(path, engine=temp_db)
            handler.do_GET()
            body = _json(handler)
            keys = set(body["providers"]) if path == "/health" \
                else set(body["providers"])
            assert "/" not in keys, f"{path} still renders the phantom key"
            assert not any(k.startswith("/") for k in keys), path


# ── behaviour that must NOT change ─────────────────────────────────────────

class TestExistingBehaviourIntact:
    def test_tracked_status_is_still_reported(self):
        cb = CircuitBreaker(_cfg())
        for _ in range(3):  # failures_degraded = 3
            cb.record_failure("deepseek", "https://deepseek/v1", "l2",
                              error_type="ProviderTimeoutError",
                              error_reason="HTTP 504")
        assert cb.status_of("deepseek", "https://deepseek/v1", "l2") == "degraded"
        # Degraded with an unexpired cooldown is tripped: the gate still closes.
        assert cb.is_available("deepseek", "https://deepseek/v1", "l2") is False
        # ...and the reads above did not add or drop a key.
        assert len(cb.get_all_health()) == 1

    def test_promotion_still_mutates_and_persists(self, temp_db):
        cfg = _cfg()
        cb = _breaker(cfg, temp_db)
        for _ in range(6):  # failures_dead = 6
            cb.record_failure("deepseek", "https://deepseek/v1", "l2",
                              error_type="ProviderTimeoutError",
                              error_reason="HTTP 504")
        h = cb.get_health("deepseek", "https://deepseek/v1", "l2")
        assert h["status"] == "dead"
        h["tripped_until"] = time.time() - 1  # cooldown expired

        assert cb.is_available("deepseek", "https://deepseek/v1", "l2") is True
        assert cb.status_of("deepseek", "https://deepseek/v1", "l2") == "degraded"

    def test_reads_do_not_persist_rows(self, temp_db):
        """A read must not write a provider_health row either."""
        from src.api.models import ProviderHealth, get_session
        cfg = _cfg()
        cb = _breaker(cfg, temp_db)
        _routing_reads(cb, "", "https://deepseek/v1", "")
        _routing_reads(cb, "phantom", "https://phantom/v1", "l2")
        with get_session(temp_db) as session:
            assert session.query(ProviderHealth).all() == []

    def test_writers_still_materialise(self):
        cb = CircuitBreaker(_cfg())
        cb.record_success("deepseek", "https://deepseek/v1", "l2")
        assert set(cb.get_all_health()) == {("deepseek", "https://deepseek/v1", "l2")}
        assert cb.get_health("fresh", "https://fresh/v1", "l2")["status"] == "healthy"
        assert len(cb.get_all_health()) == 2

    def test_peek_returns_the_live_entry_for_a_tracked_key(self):
        """peek must not hand back a copy, or promotions would be lost."""
        cb = CircuitBreaker(_cfg())
        live = cb.get_health("deepseek", "https://deepseek/v1", "l2")
        assert cb.peek("deepseek", "https://deepseek/v1", "l2") is live
