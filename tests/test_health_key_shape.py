"""`/health` payload shape — the phantom "/" key, and the duplicate-collapse rule.

Phase 1 of the provider-health work flagged a `"/"` key in the `/health` payload. This module
records the answer: it is **not** load-bearing, it is a malformed row, and the structural fix
(reads no longer materialize health entries — see `test_health_read_no_materialize.py`) already
stopped it being created. Measured live 2026-10-24: `/health` returned 17 provider keys, none of
them `/`.

What was still wrong is the collapse rule. The health map is keyed by
`(provider, base_url, profile)`, but the payload key is only `"provider/profile"`, so a provider
configured with more than one base_url over time holds several rows per profile and they collapse
into one key. Which row won depended on dict iteration order — insertion order of a dict seeded
from the DB. Measured on prod: 20 rows collapsed to 17 keys, and `opencode/l2` had a stale
`degraded/6` row alongside a live `dead/117` row. The live one won by luck.

Rule now: the freshest row wins (latest of last_success / last_failure / tripped_until), and ties
go to the worse status so a dead lane is never masked by a healthy one.
"""
import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from src.server import LCPHandler
from src.api.runtime import resolve_service
from src.api.models import Base, get_engine
from src.api import circuit_breaker as cb_module
from src.api.circuit_breaker import CircuitBreaker, get_circuit_breaker


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
    cb_module._circuit_breaker = None
    yield
    cb_module._circuit_breaker = None


def _cfg():
    cfg = MagicMock()
    cfg.circuit_breaker = {
        "failures_degraded": 3, "failures_dead": 6,
        "degraded_cooldown_seconds": 60, "dead_cooldown_seconds": 300,
    }
    cfg.profiles = {"l2": {"chain": [{"provider": "deepseek", "model": "m"}]}}
    cfg.providers = {"deepseek": {"api_base": "https://deepseek/v1"}}
    cfg.raw = {"providers": cfg.providers, "profiles": cfg.profiles}
    cfg.save = MagicMock()
    return cfg


class _Handler(LCPHandler):
    def __init__(self, path="/health", method="GET", engine=None):
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


def _health(cfg, engine):
    cb = get_circuit_breaker(cfg)
    cb.attach_engine(engine)
    assert resolve_service("circuit_breaker", fallback=get_circuit_breaker) is cb
    h = _Handler(engine=engine)
    h.config = cfg
    h._serve_health()
    return _json(h)


# ── key shape ──────────────────────────────────────────────────────────────

class TestKeyShape:
    def test_every_key_is_provider_slash_profile(self, temp_db):
        cb = get_circuit_breaker(_cfg())
        cb.attach_engine(temp_db)
        cb.record_success("deepseek", "https://deepseek/v1", "l2")
        cb.record_success("commandcode", "https://cc/v1", "coder")

        payload = _health(_cfg(), temp_db)
        for key in payload["providers"]:
            provider, _, profile = key.partition("/")
            assert provider, f"empty provider in key {key!r}"
            assert profile, f"empty profile in key {key!r}"
            assert key.count("/") == 1, f"key {key!r} is not exactly provider/profile"

    def test_no_bare_slash_key(self, temp_db):
        """The phantom `"/"` key must never appear."""
        payload = _health(_cfg(), temp_db)
        assert "/" not in payload["providers"]
        assert not any(k.startswith("/") or k.endswith("/") for k in payload["providers"])

    def test_malformed_row_is_skipped_not_rendered(self, temp_db):
        """A row with an empty provider would render as "/l2" — skip it, do not publish it."""
        cb = get_circuit_breaker(_cfg())
        cb.attach_engine(temp_db)
        cb._health[("", "https://x/v1", "l2")] = {
            "status": "healthy", "consecutive_failures": 0,
            "last_success": None, "last_failure": None, "tripped_until": None,
        }
        payload = _health(_cfg(), temp_db)
        assert "/l2" not in payload["providers"]
        assert "" not in payload["providers"]


# ── collapse rule ──────────────────────────────────────────────────────────

def _row(status, failures, last_success=None, last_failure=None, tripped=None):
    return {
        "status": status,
        "consecutive_failures": failures,
        "last_success": last_success,
        "last_failure": last_failure,
        "tripped_until": tripped,
    }


class TestDuplicateCollapse:
    def test_freshest_row_wins_regardless_of_insertion_order(self, temp_db):
        """The measured prod case: a stale `degraded` row and a live `dead` row."""
        cb = get_circuit_breaker(_cfg())
        cb.attach_engine(temp_db)
        # insert the LIVE row first, then the stale one — insertion order must not decide
        cb._health[("opencode", "https://new/v1", "l2")] = _row(
            "dead", 117, last_failure="2026-10-24T11:12:24+00:00", tripped=1792840464.0)
        cb._health[("opencode", "https://old/v1", "l2")] = _row(
            "degraded", 6, last_failure="2026-09-20T09:06:56+00:00")

        payload = _health(_cfg(), temp_db)
        assert payload["providers"]["opencode/l2"]["status"] == "dead"
        assert payload["providers"]["opencode/l2"]["failures"] == 117

    def test_freshest_row_wins_when_stale_is_inserted_last(self, temp_db):
        cb = get_circuit_breaker(_cfg())
        cb.attach_engine(temp_db)
        cb._health[("opencode", "https://old/v1", "l2")] = _row(
            "degraded", 6, last_failure="2026-09-20T09:06:56+00:00")
        cb._health[("opencode", "https://new/v1", "l2")] = _row(
            "dead", 117, last_failure="2026-10-24T11:12:24+00:00", tripped=1792840464.0)

        payload = _health(_cfg(), temp_db)
        assert payload["providers"]["opencode/l2"]["status"] == "dead"

    def test_tie_goes_to_the_worse_status(self, temp_db):
        cb = get_circuit_breaker(_cfg())
        cb.attach_engine(temp_db)
        same = "2026-10-24T11:00:00+00:00"
        cb._health[("p", "https://a/v1", "l2")] = _row("healthy", 0, last_success=same)
        cb._health[("p", "https://b/v1", "l2")] = _row("dead", 6, last_failure=same)

        payload = _health(_cfg(), temp_db)
        assert payload["providers"]["p/l2"]["status"] == "dead"

    def test_row_count_collapses(self, temp_db):
        cb = get_circuit_breaker(_cfg())
        cb.attach_engine(temp_db)
        for i in range(3):
            cb._health[("opencode", f"https://v{i}/v1", "l2")] = _row(
                "healthy", 0, last_success=f"2026-10-2{i}T00:00:00+00:00")
        payload = _health(_cfg(), temp_db)
        assert list(payload["providers"]) == ["opencode/l2"]


# ── disclosure guard ───────────────────────────────────────────────────────

class TestNoDisclosure:
    def test_base_url_and_failure_reason_are_not_exposed(self, temp_db):
        cb = get_circuit_breaker(_cfg())
        cb.attach_engine(temp_db)
        cb._health[("p", "http://10.0.0.5:18300/v1", "l2")] = {
            "status": "dead", "consecutive_failures": 9,
            "last_success": None, "last_failure": "2026-10-24T00:00:00+00:00",
            "tripped_until": None, "base_url": "http://10.0.0.5:18300/v1",
            "last_failure_reason": "Provider p out of credits",
        }
        payload = _health(_cfg(), temp_db)
        entry = payload["providers"]["p/l2"]
        blob = json.dumps(entry)
        assert "10.0.0.5" not in blob
        assert "out of credits" not in blob
        assert set(entry) == {"status", "failures", "last_success", "last_failure", "tripped_until"}

    def test_top_level_shape_is_unchanged(self, temp_db):
        payload = _health(_cfg(), temp_db)
        assert set(payload) == {"status", "profiles", "providers"}
        assert payload["status"] == "ok"
        assert payload["profiles"] == ["l2"]
