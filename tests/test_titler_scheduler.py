"""Unit tests for the conversation-titling scheduler's live-failure behaviors.

Every test here maps to a defect found in production on 2026-11-14, when the
titler had been silently dead for days: the candidate scan read one 20-row
page, the step resolver picked an out-of-credits provider and swallowed the
error at debug level, the pass counter counted attempts instead of titles,
and ``last_run`` evaporated on every restart. These tests pin the fixes so a
regression cannot ship silently again.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.api import conv_store, conv_titler as ct


@pytest.fixture(autouse=True)
def _reset_titler():
    with patch.object(ct, "_run_worker", lambda: None):
        ct.reset_for_tests()
        yield
    ct.reset_for_tests()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("LCP_COSTS_DB", str(tmp_path / "costs.db"))
    conv_store.ensure_store()
    return tmp_path / "costs.db"


def _mkconv(cid: str, *, name=None, summary=None, last_seen="2026-11-14T07:00:00"):
    """A conversation past the MIN_MESSAGES gate (two user/assistant turns)."""
    conv_store.capture_request(
        cid, {"messages": [{"role": "user", "content": f"hello {cid}"}]},
        reply="ok", profile="l2", model="m1", ts=last_seen)
    conv_store.capture_request(
        cid, {"messages": [{"role": "user", "content": f"again {cid}"}]},
        reply="sure", profile="l2", model="m1",
        ts=last_seen.replace("07:00", "07:01"))
    conv_store.upsert_conversation(cid, name=name, summary=summary,
                                   profile="l2", model="m1")
    conv_store.recount_conversation(cid)


def _config(model=None, chain=None):
    cfg = MagicMock()
    cfg.conversation_titling = {"enabled": True, "model": model}
    cfg.server = {"default_profile": "l2", "port": 8734}
    cfg.profiles = {"l2": {"chain": chain or [
        {"provider": "prov-a", "model": model or "head-model",
         "base_url": "https://a/v1"}]}}
    cfg.providers = {"prov-a": {"api_base": "https://a/v1"},
                     "prov-b": {"api_base": "https://b/v1"}}
    return cfg


def _reply(name="Titled", summary="A summary"):
    return {"choices": [{"message": {"content": f"Title: {name}\nSummary: {summary}"}}]}


# ── 1. the candidate scan reaches beyond page one ────────────────────────────

def test_candidate_scan_reaches_past_the_first_page(store):
    """The old code fed the shared table layer {'limit': 500} and got the
    default 20-row page back: 1,071 untitled conversations were structurally
    unreachable. The scan must see all of them."""
    for i in range(30):
        _mkconv(f"s{i:03d}", last_seen=f"2026-11-14T07:{i:02d}:00")
    ids = ct._candidate_ids("untitled", limit=500)
    assert len(ids) == 30, (
        f"the scan must reach every untitled row, saw {len(ids)}")


def test_candidate_scan_respects_limit_as_a_ceiling(store):
    for i in range(12):
        _mkconv(f"l{i:03d}")
    assert len(ct._candidate_ids("untitled", limit=5)) == 5


def test_candidate_scan_never_offers_a_named_row(store):
    _mkconv("cx-named", name="has a name", summary="and summary")
    _mkconv("cx-anon")
    ids = ct._candidate_ids("untitled")
    assert "cx-named" not in ids and "cx-anon" in ids


def test_candidate_scan_newest_first(store):
    _mkconv("old", last_seen="2026-11-13T07:00:00")
    _mkconv("new", last_seen="2026-11-14T07:00:00")
    ids = ct._candidate_ids("untitled", limit=1)
    assert ids == ["new"], "drain order must be newest-first"


# ── 2. step resolution prefers a provider that can actually carry the call ──

def test_resolve_step_skips_a_keyless_provider_for_a_keyed_model(store):
    """prov-a is keyed-but-empty, prov-b funded: the first-match rule picked
    prov-a and every title call died with ``No API key found``."""
    cfg = _config(model="shared-model", chain=[
        {"provider": "prov-a", "model": "shared-model", "base_url": "https://a/v1"},
        {"provider": "prov-b", "model": "shared-model", "base_url": "https://b/v1"},
    ])
    with patch.object(ct, "_provider_has_key",
                      side_effect=lambda p: p == "prov-b"):
        steps = ct._resolve_step(cfg)["__alternatives"]
    assert [s["provider"] for s in steps] == ["prov-b", "prov-a"], (
        "the funded provider must be tried first")


def test_resolve_step_falls_through_to_the_next_chain_step(store):
    """The live failure: opencode → out of credits → commandcode → out of
    credits → commandcode2 → SUCCESS. _title_one must walk the steps."""
    cfg = _config(model="shared-model", chain=[
        {"provider": "prov-a", "model": "shared-model", "base_url": "https://a/v1"},
        {"provider": "prov-b", "model": "shared-model", "base_url": "https://b/v1"},
    ])
    cfg.profiles["l2"]["chain"].append(
        {"provider": "prov-b", "model": "shared-model", "base_url": "https://b/v1"})
    calls = []

    def fake_forward(step, body, config):
        calls.append(step["provider"])
        if step["provider"] == "prov-a":
            raise RuntimeError("Provider prov-a out of credits")
        return _reply(), 200

    _mkconv("sess-x")
    with patch("src.api.config.get_config", return_value=cfg), \
         patch("src.api.request_pipeline.forward_request", side_effect=fake_forward):
        assert ct._title_one("sess-x") is True
    assert calls[0] == "prov-a" and "prov-b" in calls, (
        "a failing first step must be retried on the next chain step")


def test_title_one_survives_every_step_failing(store):
    cfg = _config(model="shared-model", chain=[
        {"provider": "prov-a", "model": "shared-model", "base_url": "https://a/v1"},
    ])
    with patch("src.api.config.get_config", return_value=cfg), \
         patch("src.api.request_pipeline.forward_request",
               side_effect=RuntimeError("out of credits")):
        assert ct._title_one("sess-x") is False


# ── 3. failures are visible and the counter tells the truth ─────────────────

def test_failed_title_is_logged_at_warning(store):
    """A debug-level failure hid a dead titler for days. The failure path must
    emit a WARNING-level conv_titler event naming the provider."""
    cfg = _config()
    _mkconv("sess-x")
    fake_logger = MagicMock()
    with patch.object(ct, "logger", fake_logger), \
         patch("src.api.config.get_config", return_value=cfg), \
         patch("src.api.request_pipeline.forward_request",
               side_effect=RuntimeError("No API key found for provider 'prov-a'")):
        ct._title_one("sess-x")
    warning_events = [c.args[0] for c in fake_logger.warning.call_args_list if c.args]
    assert any("conv_titler" in str(e) for e in warning_events), (
        f"a dead upstream call must log a conv_titler warning, got {warning_events}")


def test_pass_counter_counts_titles_not_attempts(store, caplog):
    """The old code incremented ``titled`` per attempt, so a pass over a dead
    provider reported success. The counter must move only for real titles."""
    for i in range(3):
        _mkconv(f"c{i}")
    cfg = _config()
    with patch("src.api.config.get_config", return_value=cfg), \
         patch("src.api.request_pipeline.forward_request",
               side_effect=RuntimeError("out of credits")):
        out = ct.run_titling_pass("untitled")
    assert out["scanned"] == 3 and out["titled"] == 0, (
        "zero upstream successes must read as zero titles")


def test_pass_counter_counts_real_titles(store):
    for i in range(3):
        _mkconv(f"t{i}")
    cfg = _config()
    with patch("src.api.config.get_config", return_value=cfg), \
         patch.object(ct, "_provider_has_key", return_value=True), \
         patch("src.api.request_pipeline.forward_request",
               return_value=(_reply(), 200)):
        out = ct.run_titling_pass("untitled")
    assert out["titled"] == 3


# ── 4. scope=untitled can never overwrite an existing name ──────────────────

def test_pass_never_touches_a_named_conversation(store):
    """The operator's hard rule: scheduled titling only fills blanks. A
    pre-existing name must survive a pass byte-for-byte."""
    _mkconv("keep-me", name="Original title", summary="original summary")
    for i in range(3):
        _mkconv(f"fill{i}")
    cfg = _config()
    with patch("src.api.config.get_config", return_value=cfg), \
         patch("src.api.request_pipeline.forward_request",
               return_value=(_reply(name="REPLACED", summary="REPLACED"), 200)):
        ct.run_titling_pass("untitled")
    row = conv_store.conversation_audit("keep-me")
    assert row["name"] == "Original title", (
        "scope=untitled must never rewrite an existing name")
    assert row["summary"] == "original summary"


# ── 5. last_run survives a restart, and an overdue job catches up ───────────

def test_last_run_persists_through_the_config(store, monkeypatch):
    cfg = _config()
    saved = {}
    cfg.raw = {"conversation_titling": dict(cfg.conversation_titling)}
    cfg.save = lambda: saved.update(cfg.raw["conversation_titling"])
    monkeypatch.setattr("src.api.config.get_config", lambda: cfg)
    for i in range(2):
        _mkconv(f"p{i}")
    with patch("src.api.request_pipeline.forward_request",
               return_value=(_reply(), 200)):
        ct.run_titling_pass("untitled")
    assert saved.get("last_run"), "the pass must persist last_run via config.save()"


class _NeverSet:
    """A stop-event that never fires; every wait duration is recorded."""

    def __init__(self, waits):
        self._waits = waits

    def is_set(self):
        return False

    def wait(self, s):
        self._waits.append(s)
        return False

    def set(self):
        pass

    def clear(self):
        pass


class _PassCalled(BaseException):
    """Sentinel: the first pass fired; stop the loop.

    Derives from BaseException because _scheduler_loop wraps each pass in
    ``except Exception`` — a sentinel Exception would be swallowed and the
    loop would spin forever.
    """


def _run_loop_until_first_pass(monkeypatch, stored_last_run):
    """Drive _scheduler_loop to its first pass; return the simulated wait."""
    waits = []
    monkeypatch.setattr(ct, "_stored_last_run", lambda: stored_last_run)
    monkeypatch.setattr(ct, "_sched_stop", _NeverSet(waits))

    def fake_pass(*a, **k):
        raise _PassCalled()

    monkeypatch.setattr(ct, "run_titling_pass", fake_pass)
    monkeypatch.setattr("src.api.config.get_config", lambda: _config())
    total = 0
    try:
        ct._scheduler_loop()
    except _PassCalled:
        total = sum(waits)
    except Exception:  # noqa: BLE001 — the loop must not escape other errors
        total = sum(waits)
    return total


def test_scheduler_catches_up_when_last_run_is_stale(store, monkeypatch):
    """Every container restart used to leave the titler idle a full interval
    because last_run was lost. An overdue last_run caps the first wait at
    5 minutes so the backlog drain starts promptly after a deploy."""
    stale = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    total = _run_loop_until_first_pass(monkeypatch, stale)
    assert 0 < total <= 300, (
        f"an overdue last_run must cap the first wait at 5 min, got {total}s")


def test_scheduler_waits_the_full_interval_when_fresh(store, monkeypatch):
    """A fresh last_run must not trigger an early pass: the loop waits the
    configured interval before the first scheduled pass."""
    fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    total = _run_loop_until_first_pass(monkeypatch, fresh)
    assert total == 24 * 3600, (
        f"a fresh last_run must not shorten the 24 h interval, got {total}s")
