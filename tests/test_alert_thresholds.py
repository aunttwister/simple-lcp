"""Tests for configurable credit/allowance thresholds (module + endpoints).

The floors are the numbers an alert fires on, so the checks here are about the two ways
they can be wrong: a stored override that contradicts its window's direction (a floor
that can never fire), and a provider/window whose floor silently stops being applied.
"""

from unittest.mock import MagicMock

import pytest

from src.api import alert_thresholds as at
from src.api.cost_cache import SettingsStore
from src.api.models import Base, get_engine


@pytest.fixture
def settings(tmp_path):
    """A real SettingsStore (so persistence, not just dict plumbing, is exercised)."""
    db_path = str(tmp_path / "thresholds.db")
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    return SettingsStore(engine)


# ── payload shapes captured from the live cost plugins ───────────────────────
DEEPSEEK_BALANCE = {"balance": 9.99, "currency": "USD", "topped_up": 9.99,
                    "total_granted": 0.0, "raw": {"is_available": True}}
COMMANDCODE_SUB = {"monthly_pct": 85.0, "monthly_used": 59.47, "monthly_cap": 70.0,
                   "weekly_pct": 0.3, "five_hour_pct": 10.3}
OPENCODE_SUB = {"source": "console-usage", "limit_available": False,
                "note": "OpenCode exposes no plan limit on this prepaid org"}


class TestDefaults:
    def test_balance_is_bad_when_low_and_allowances_when_high(self):
        assert at.WINDOWS["balance"]["direction"] == "below"
        assert at.WINDOWS["monthly"]["direction"] == "above"
        assert at.WINDOWS["daily"]["direction"] == "above"

    def test_defaults_cover_every_declared_provider(self):
        d = at.defaults()
        assert d["deepseek"]["balance"]["warn"] == 2.0
        assert d["deepseek"]["balance"]["critical"] == 0.5
        assert set(d["commandcode"]) == {"five_hour", "weekly", "monthly"}
        # opencode's Go plan publishes daily/weekly/monthly; the floors exist to be
        # edited the moment the vendor will let a key read them.
        assert {"daily", "weekly", "monthly"} <= set(d["opencode"])

    def test_effective_without_stored_overrides_is_just_defaults(self, settings):
        assert at.effective(settings) == at.defaults()

    def test_effective_merges_a_partial_override_over_the_default(self, settings):
        at.save(settings, {"commandcode": {"monthly": {"warn": 70.0}}})
        merged = at.effective(settings)
        assert merged["commandcode"]["monthly"]["warn"] == 70.0
        assert merged["commandcode"]["monthly"]["critical"] == 90.0  # untouched default
        assert merged["commandcode"]["weekly"]["warn"] == 80.0       # sibling untouched

    def test_disabling_a_window_round_trips(self, settings):
        at.save(settings, {"deepseek": {"balance": {"enabled": False}}})
        assert at.effective(settings)["deepseek"]["balance"]["enabled"] is False

    def test_clear_restores_defaults(self, settings):
        at.save(settings, {"deepseek": {"balance": {"warn": 9.0, "critical": 4.0}}})
        at.clear(settings)
        assert at.effective(settings)["deepseek"]["balance"]["warn"] == 2.0

    def test_unreadable_store_degrades_to_defaults(self):
        broken = MagicMock()
        broken.get.side_effect = RuntimeError("db down")
        assert at.effective(broken) == at.defaults()


class TestValidation:
    def test_below_window_rejects_critical_above_warn(self):
        with pytest.raises(at.ThresholdError) as exc:
            at.validate({"deepseek": {"balance": {"warn": 1.0, "critical": 5.0}}})
        assert "bad when LOW" in str(exc.value)

    def test_above_window_rejects_critical_below_warn(self):
        with pytest.raises(at.ThresholdError) as exc:
            at.validate({"commandcode": {"monthly": {"warn": 90.0, "critical": 50.0}}})
        assert "bad when HIGH" in str(exc.value)

    def test_equal_floors_are_accepted_on_both_directions(self):
        assert at.validate({"deepseek": {"balance": {"warn": 2.0, "critical": 2.0}}})
        assert at.validate({"commandcode": {"monthly": {"warn": 80.0, "critical": 80.0}}})

    def test_unknown_window_is_named_in_the_error(self):
        with pytest.raises(at.ThresholdError) as exc:
            at.validate({"deepseek": {"hourly": {"warn": 1, "critical": 2}}})
        assert "hourly" in str(exc.value) and "known:" in str(exc.value)

    def test_non_numeric_floor_is_rejected(self):
        with pytest.raises(at.ThresholdError):
            at.validate({"deepseek": {"balance": {"warn": "lots"}}})

    def test_negative_floor_is_rejected(self):
        with pytest.raises(at.ThresholdError):
            at.validate({"deepseek": {"balance": {"warn": -1.0, "critical": -2.0}}})

    def test_numeric_strings_are_normalised_to_floats(self):
        out = at.validate({"deepseek": {"balance": {"warn": "3.5", "critical": "1"}}})
        assert out["deepseek"]["balance"] == {"warn": 3.5, "critical": 1.0}

    def test_empty_provider_object_clears_that_provider(self):
        assert at.validate({"deepseek": {}}) == {}

    def test_non_object_body_is_rejected(self):
        with pytest.raises(at.ThresholdError):
            at.validate([1, 2, 3])

    def test_missing_store_on_save_is_an_operator_error(self):
        with pytest.raises(at.ThresholdError):
            at.save(None, {"deepseek": {"balance": {"warn": 1.0, "critical": 0.5}}})

    def test_save_persists_and_reads_back(self, settings):
        at.save(settings, {"commandcode": {"five_hour": {"warn": 60.0, "critical": 75.0}}})
        assert at.load(settings) == {"commandcode": {"five_hour": {"warn": 60.0, "critical": 75.0}}}


class TestWindowSelection:
    def test_declared_windows_win(self):
        assert at.windows_for("deepseek", {"deepseek": DEEPSEEK_BALANCE}, {}) == ["balance"]
        assert set(at.windows_for("commandcode", {}, {"commandcode": COMMANDCODE_SUB})) == {
            "five_hour", "weekly", "monthly"}

    def test_unknown_provider_is_wired_from_its_own_payload(self):
        assert at.windows_for("newvendor", {}, {"newvendor": {"monthly_pct": 5.0}}) == ["monthly"]
        assert at.windows_for("newvendor2", {"newvendor2": {"balance": 3.0}}, {}) == ["balance"]

    def test_provider_with_no_plugin_gets_no_windows(self):
        assert at.windows_for("local-sparks", {}, {}) == []

    def test_published_reflects_what_the_plugin_actually_reports(self):
        assert at.published_windows("deepseek", {"deepseek": DEEPSEEK_BALANCE}, {}) == {"balance"}
        assert at.published_windows("commandcode", {}, {"commandcode": COMMANDCODE_SUB}) == {
            "five_hour", "weekly", "monthly"}
        # the Go plan is mentioned in the payload's note but reports no window
        assert at.published_windows("opencode", {}, {"opencode": OPENCODE_SUB}) == set()

    def test_reported_value_uses_the_plugins_own_field(self):
        assert at.reported_value("deepseek", "balance",
                                 {"deepseek": DEEPSEEK_BALANCE}, {}) == (9.99, "USD")
        assert at.reported_value("commandcode", "monthly", {},
                                 {"commandcode": COMMANDCODE_SUB}) == (85.0, "%")
        assert at.reported_value("opencode", "monthly", {}, {"opencode": OPENCODE_SUB}) \
            == (None, "%")


class TestEndpoints:
    """The panel and the watch both read this payload — the route must answer even when
    a service is missing, and a bad floor must come back as 400, not 500."""

    @pytest.fixture
    def ep(self, settings):
        from src.server.endpoints import AlertEndpoints
        e = AlertEndpoints()
        e._audit = MagicMock()
        e.config = MagicMock()
        e.config.providers = {"deepseek": {}, "opencode": {}, "local-sparks": {}}
        e.path = "/api/alerts/thresholds"
        e._read_body = lambda: {}
        e._send_json = MagicMock()
        return e

    def _body(self, ep):
        return ep._send_json.call_args[0][0]

    def test_get_lists_configured_providers_with_their_windows(self, ep, monkeypatch):
        """The panel must show what the plugin reports, and must not hide a provider it
        cannot read — local-sparks has no plugin, and llamacpp has a payload but is no
        longer configured. Both are facts worth showing, neither is a pass."""
        def fake_payloads(cache, kind, reg):
            if kind == "balance":
                return {"deepseek": DEEPSEEK_BALANCE, "llamacpp": {"_error": "cache_pending"}}
            return {"commandcode": COMMANDCODE_SUB, "opencode": OPENCODE_SUB}

        monkeypatch.setattr("src.api.cost_cache.cached_plugin_payloads", fake_payloads)
        monkeypatch.setattr("src.server.endpoints.resolve_service",
                            lambda name, fallback=None: object())
        ep._serve_alert_thresholds()
        body = self._body(ep)

        assert set(body["providers"]) >= {"deepseek", "opencode", "local-sparks", "llamacpp"}
        deepseek = body["providers"]["deepseek"]
        assert [w["window"] for w in deepseek["windows"]] == ["balance"]
        assert (deepseek["windows"][0]["warn"], deepseek["windows"][0]["critical"]) == (2.0, 0.5)
        assert deepseek["windows"][0]["published"] is True
        assert deepseek["windows"][0]["reported"] == 9.99
        assert deepseek["configured"] is True

        commandcode = body["providers"]["commandcode"]
        assert {w["window"] for w in commandcode["windows"]} == {"five_hour", "weekly", "monthly"}
        assert all(w["published"] for w in commandcode["windows"])
        assert [w["reported"] for w in commandcode["windows"] if w["window"] == "monthly"] == [85.0]

        # configured, but nothing to read
        assert body["providers"]["local-sparks"]["plugin"] is False
        assert body["providers"]["local-sparks"]["windows"] == []
        # plugin payload, not a configured provider
        assert body["providers"]["llamacpp"]["configured"] is False
        # the windows exist as floors but the vendor reports none of them
        assert all(w["published"] is False for w in body["providers"]["opencode"]["windows"])
        assert body["note"] and body["settings_key"] == "alert_thresholds"

    def test_put_rejects_a_floor_that_could_never_fire(self, ep, monkeypatch):
        ep._read_body = lambda: {"thresholds": {"deepseek": {"balance": {"warn": 1.0, "critical": 9.0}}}}
        monkeypatch.setattr("src.api.cost_cache.get_settings", lambda: None, raising=False)
        monkeypatch.setattr("src.server.endpoints.resolve_service",
                            lambda name, fallback=None: None)
        ep._serve_alert_thresholds_update()
        body = self._body(ep)
        assert "bad when LOW" in body["error"]
        assert ep._audit.call_count == 0  # nothing was written, nothing was audited

    def test_put_persists_and_audits(self, ep, settings, monkeypatch):
        ep._read_body = lambda: {"thresholds": {"commandcode": {"monthly": {"warn": 75.0, "critical": 88.0}}}}
        monkeypatch.setattr("src.server.endpoints.resolve_service",
                            lambda name, fallback=None: settings)
        ep._serve_alert_thresholds_update()
        body = self._body(ep)
        assert body["ok"] is True
        assert body["effective"]["commandcode"]["monthly"] == {
            "warn": 75.0, "critical": 88.0, "enabled": True, "direction": "above",
            "unit": "%", "label": "monthly allowance used",
            "hint": at.WINDOWS["monthly"]["hint"]}
        assert ep._audit.call_args[0][0] == "alerts.thresholds_update"
        assert at.load(settings)["commandcode"]["monthly"]["warn"] == 75.0
