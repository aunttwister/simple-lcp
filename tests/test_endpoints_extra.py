"""Tests for additional endpoint coverage: budgets CRUD, alerts config/ack,
plugin cookie/workspace endpoints, provider toggle, chain reorder, and the
setup wizard endpoints."""

from unittest.mock import MagicMock, patch

import pytest

from src.server.endpoints import (
    BudgetEndpoints,
    AlertEndpoints,
    PluginEndpoints,
    ProviderEndpoints,
    SetupEndpoints,
    HealthEndpoints,
)


class _EP:
    """Minimal handler-ish object combining the endpoint mixins."""

    path = "/"
    engine = None
    config = None
    headers = {}
    _read_body = MagicMock(return_value={})
    _send_json = MagicMock()


@pytest.fixture
def ep(temp_db):
    e = _EP()
    e.engine = temp_db[1]
    e._send_json = MagicMock()
    e._read_body = MagicMock(return_value={})
    return e


@pytest.fixture
def budget_ep(temp_db):
    e = BudgetEndpoints()
    e.engine = temp_db[1]
    e._send_json = MagicMock()
    e._read_body = MagicMock(return_value={})
    return e


@pytest.fixture
def alert_ep(temp_db):
    e = AlertEndpoints()
    e.engine = temp_db[1]
    e.path = "/api/alerts"
    e._send_json = MagicMock()
    e._read_body = MagicMock(return_value={})
    return e


@pytest.fixture
def plugin_ep(temp_db):
    e = PluginEndpoints()
    e.engine = temp_db[1]
    e.path = "/api/cost-plugins/cookie/deepseek"
    e._send_json = MagicMock()
    e._read_body = MagicMock(return_value={})
    return e


@pytest.fixture
def setup_ep(temp_db):
    e = SetupEndpoints()
    e.engine = temp_db[1]
    e._send_json = MagicMock()
    e._read_body = MagicMock(return_value={})
    e.send_response = MagicMock()
    e.send_header = MagicMock()
    e.end_headers = MagicMock()
    e.wfile = MagicMock()
    e.config = MagicMock()
    e.config.providers = {}
    e.config.raw = {"providers": {}}
    e.config.save = MagicMock()
    return e


# ── Budget CRUD ──────────────────────────────────────────────────────────────

class TestBudgetCrud:
    def test_budget_list_empty(self, budget_ep):
        budget_ep._serve_budgets_list()
        body = budget_ep._send_json.call_args[0][0]
        assert body == {"budgets": []}

    def test_budget_create_and_status(self, budget_ep):
        budget_ep._read_body = MagicMock(return_value={"name": "Cap", "amount": 100.0, "action": "block"})
        budget_ep._serve_budget_create()
        body = budget_ep._send_json.call_args[0][0]
        assert body["ok"] is True
        budget_id = body["budget"]["id"]

        budget_ep._serve_budgets_status()
        status = budget_ep._send_json.call_args[0][0]
        assert len(status["budgets"]) == 1

        budget_ep._serve_budgets_list()
        listed = budget_ep._send_json.call_args[0][0]
        assert len(listed["budgets"]) == 1
        assert listed["budgets"][0]["name"] == "Cap"

        # Delete
        budget_ep.path = f"/api/budgets/{budget_id}"
        budget_ep._serve_budget_delete(str(budget_id))
        assert budget_ep._send_json.call_args[0][0]["deleted"] == budget_id

    def test_budget_update(self, budget_ep):
        from src.api.models import Budget, get_session
        with get_session(budget_ep.engine) as s:
            b = Budget(name="Old", amount=10.0, period="monthly", threshold_pct="80", action="log", status="active")
            s.add(b)
            s.commit()
            bid = b.id
        budget_ep._read_body = MagicMock(return_value={"amount": 50.0, "status": "paused"})
        budget_ep._serve_budget_update(str(bid))
        assert budget_ep._send_json.call_args[0][0]["updated"] == str(bid)

    def test_budget_delete_missing(self, budget_ep):
        budget_ep._serve_budget_delete("999999")
        assert budget_ep._send_json.call_args[0][1] == 404

    def test_budget_update_invalid_id(self, budget_ep):
        budget_ep._serve_budget_update("abc")
        assert budget_ep._send_json.call_args[0][1] == 400


# ── Alert config + acknowledge ───────────────────────────────────────────────

class TestAlertEndpointsExtra:
    def test_alert_config_roundtrip(self, alert_ep):
        am = MagicMock()
        am.config = {"thresholds": {"budget": 80}}
        with patch("src.server.endpoints.get_alert_manager", return_value=am):
            alert_ep._serve_alerts_config()
            body = alert_ep._send_json.call_args[0][0]
            assert body["config"] == {"thresholds": {"budget": 80}}

            alert_ep._read_body = MagicMock(return_value={"thresholds": {"budget": 90}})
            am.update_config.return_value = {"thresholds": {"budget": 90}}
            alert_ep._serve_alerts_config_update()
            body = alert_ep._send_json.call_args[0][0]
            assert body["ok"] is True

    def test_alert_acknowledge_found(self, alert_ep):
        am = MagicMock()
        am.acknowledge.return_value = True
        with patch("src.server.endpoints.get_alert_manager", return_value=am):
            alert_ep._serve_alert_acknowledge("abc")
            assert alert_ep._send_json.call_args[0][0]["ok"] is True

    def test_alert_acknowledge_missing(self, alert_ep):
        am = MagicMock()
        am.acknowledge.return_value = False
        with patch("src.server.endpoints.get_alert_manager", return_value=am):
            alert_ep._serve_alert_acknowledge("abc")
            assert alert_ep._send_json.call_args[0][1] == 404

    def test_alert_test_webhook(self, alert_ep):
        am = MagicMock()
        am.test_webhook.return_value = {"ok": True}
        with patch("src.server.endpoints.get_alert_manager", return_value=am):
            alert_ep._serve_alerts_test_webhook()
            assert alert_ep._send_json.call_args[0][0] == {"ok": True}


# ── Plugin cookie / workspace endpoints ──────────────────────────────────────

class TestPluginCookieWorkspace:
    def test_cookie_get_no_store(self, plugin_ep):
        with patch("src.server.endpoints.get_credential_store", return_value=None):
            plugin_ep._serve_plugin_cookie_get("deepseek")
            assert plugin_ep._send_json.call_args[0][0]["has_cookie"] is False

    def test_cookie_set_clears(self, plugin_ep):
        store = MagicMock()
        plugin_ep._read_body = MagicMock(return_value={"cookie": ""})
        with patch("src.server.endpoints.get_credential_store", return_value=store):
            plugin_ep._serve_plugin_cookie_set("deepseek")
            body = plugin_ep._send_json.call_args[0][0]
            assert body["ok"] is True
            assert body["has_cookie"] is False
            store.set_cookie.assert_called_once_with("deepseek", "")

    def test_workspace_id_set(self, plugin_ep):
        store = MagicMock()
        plugin_ep._read_body = MagicMock(return_value={"workspace_id": "wrk_123"})
        with patch("src.server.endpoints.get_credential_store", return_value=store):
            plugin_ep._serve_plugin_workspace_id_set("opencode")
            body = plugin_ep._send_json.call_args[0][0]
            assert body["has_workspace_id"] is True
            store.set_workspace_id.assert_called_once_with("opencode", "wrk_123")

    def test_plugin_cookie_store_none_error(self, plugin_ep):
        plugin_ep._read_body = MagicMock(return_value={"cookie": "x"})
        with patch("src.server.endpoints.get_credential_store", return_value=None):
            plugin_ep._serve_plugin_cookie_set("deepseek")
            assert plugin_ep._send_json.call_args[0][1] == 500


# ── Provider toggle + chain reorder ──────────────────────────────────────────

class TestProviderToggle:
    def test_toggle_missing_fields(self):
        e = HealthEndpoints()
        e.config = MagicMock()
        e._send_json = MagicMock()
        e._read_body = MagicMock(return_value={})
        e._serve_provider_toggle("deepseek")
        assert e._send_json.call_args[0][1] == 400

    def test_toggle_bad_action(self):
        e = HealthEndpoints()
        e.config = MagicMock()
        e._send_json = MagicMock()
        e._read_body = MagicMock(return_value={"profile": "l2", "action": "banana"})
        e._serve_provider_toggle("deepseek")
        assert e._send_json.call_args[0][1] == 400


class TestChainReorder:
    def test_reorder_preserves_base_url(self):
        e = ProviderEndpoints()
        e.config = MagicMock()
        e.config.profiles = {"l2": {}}
        e.config.raw = {
            "profiles": {"l2": {"chain": [
                {"provider": "deepseek", "model": "m", "base_url": "https://old/v1"},
            ]}},
        }
        e.config.save = MagicMock()
        e._send_json = MagicMock()
        e._read_body = MagicMock(return_value={
            "chain": [{"provider": "deepseek", "model": "m"}],
        })
        e._serve_chain_reorder("l2")
        body = e._send_json.call_args[0][0]
        assert body["ok"] is True
        assert body["chain"][0]["base_url"] == "https://old/v1"


# ── Setup endpoints ──────────────────────────────────────────────────────────

class TestSetupEndpoints:
    def test_setup_skip(self, setup_ep):
        with patch("src.api.setup.mark_skipped", return_value=True):
            setup_ep._serve_setup_skip_api()
            assert setup_ep._send_json.call_args[0][0]["ok"] is True

    def test_setup_install_unknown_target(self, setup_ep):
        setup_ep._serve_setup_install_api("bogus", "nope")
        assert setup_ep._send_json.call_args[0][1] == 404

    def test_setup_install_router_blocked_without_capability_data(self, setup_ep):
        """Router install refused server-side when there is no capability data."""
        with patch("src.api.setup.router_install_blocked_reason",
                   return_value="No capability data to route by"), \
             patch("src.api.setup.start_router_install") as mock_start:
            setup_ep._serve_setup_install_api("module", "router")
        body = setup_ep._send_json.call_args[0][0]
        assert setup_ep._send_json.call_args[0][1] == 400
        assert "capability data" in body["error"]
        mock_start.assert_not_called()

    def test_setup_install_router_allowed_with_capability_data(self, setup_ep):
        """Router install proceeds once capability data exists."""
        with patch("src.api.setup.router_install_blocked_reason", return_value=None), \
             patch("src.api.setup.start_router_install",
                   return_value={"status": "queued"}) as mock_start:
            setup_ep._serve_setup_install_api("module", "router")
        body = setup_ep._send_json.call_args[0][0]
        # Single positional arg, no status code → 200; ok is True.
        assert len(setup_ep._send_json.call_args[0]) == 1
        assert body.get("ok") is True
        mock_start.assert_called_once_with(setup_ep.engine)

    def test_setup_remove_provider(self, setup_ep):
        with patch("src.api.setup.remove_provider", return_value={"removed": True, "provider": "deepseek"}):
            setup_ep._serve_setup_remove_api("provider", "deepseek")
            body = setup_ep._send_json.call_args[0][0]
            assert body["ok"] is True
            assert body["removed"] is True
