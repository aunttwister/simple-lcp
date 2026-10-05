"""Tests for the OpenCode cost tracking plugin.

Uses a temporary SQLite database with the gateway ``requests`` table
(single source of truth).  Verifies the plugin reads and aggregates
correctly via SQLAlchemy engine.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from src.api.cost_plugins.opencode import OpenCodeCostPlugin, _OPENCODE_PRICING, _FREE_MODELS


# ── Helpers ─────────────────────────────────────────────────────────────────

def _create_engine_and_table():
    """Create a SQLAlchemy engine + ``requests`` table in a temp in-memory DB."""
    from src.api.models import Base, get_engine
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine, tables=[Base.metadata.tables["requests"]])
    return engine


def _insert_request(engine, **kwargs):
    """Insert a row into the ``requests`` table."""
    from src.api.models import Request, get_session
    defaults = {
        "timestamp": "2025-10-10T12:00:00",
        "profile": "default",
        "model": "deepseek-v4-pro",
        "provider": "opencode",
        "prompt_tokens": 500,
        "completion_tokens": 200,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "cost": 0.0,
        "latency_ms": 100,
        "success": 1,
        "error_type": None,
        "tools_blocked": None,
    }
    defaults.update(kwargs)
    with get_session(engine) as session:
        req = Request(**defaults)
        session.add(req)
        session.commit()


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture
def engine():
    """Create an in-memory SQLAlchemy engine with the requests table."""
    return _create_engine_and_table()


@pytest.fixture
def plugin(engine):
    """Return an OpenCode plugin bound to the test engine."""
    return OpenCodeCostPlugin(engine=engine)


# ═══════════════════════════════════════════════════════════════════════
# Plugin identity & pricing
# ═══════════════════════════════════════════════════════════════════════

class TestOpenCodeIdentity:
    def test_provider_name(self, plugin):
        assert plugin.provider_name == "opencode"

    def test_supported_models_includes_all(self, plugin):
        models = plugin.get_supported_models()
        assert "deepseek-v4-pro" in models
        assert "deepseek-v4-flash" in models
        for free in _FREE_MODELS:
            assert free in models

    def test_get_pricing_pro(self, plugin):
        p = plugin.get_pricing("deepseek-v4-pro")
        assert p == _OPENCODE_PRICING["deepseek-v4-pro"]

    def test_get_pricing_flash(self, plugin):
        p = plugin.get_pricing("deepseek-v4-flash")
        assert p == _OPENCODE_PRICING["deepseek-v4-flash"]

    def test_get_pricing_free_models(self, plugin):
        for free in _FREE_MODELS:
            p = plugin.get_pricing(free)
            assert p == {"cache_hit": 0.0, "cache_miss": 0.0, "output": 0.0}

    def test_get_pricing_unknown(self, plugin):
        assert plugin.get_pricing("nonexistent") is None


# ═══════════════════════════════════════════════════════════════════════
# calculate_cost
# ═══════════════════════════════════════════════════════════════════════

class TestOpenCodeCalculateCost:
    def test_v4_pro_cost(self, plugin):
        cost = plugin.calculate_cost("deepseek-v4-pro", {
            "prompt_cache_hit_tokens": 500_000,
            "prompt_cache_miss_tokens": 1_000_000,
            "completion_tokens": 200_000,
        })
        expected = (
            (500_000 / 1_000_000) * 0.003625
            + (1_000_000 / 1_000_000) * 0.435
            + (200_000 / 1_000_000) * 0.87
        )
        assert cost == pytest.approx(expected)

    def test_free_model_zero_cost(self, plugin):
        cost = plugin.calculate_cost("qwen3-coder", {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 500_000,
        })
        assert cost == 0.0

    def test_unknown_model_returns_none(self, plugin):
        cost = plugin.calculate_cost("unknown", {"prompt_tokens": 100})
        assert cost is None

    def test_cache_miss_fallback(self, plugin):
        """When cache_hit and cache_miss are both 0/absent, fall back to prompt_tokens."""
        cost = plugin.calculate_cost("deepseek-v4-pro", {
            "completion_tokens": 200_000,
        })
        assert cost == pytest.approx(0.174)


# ═══════════════════════════════════════════════════════════════════════
# fetch_usage (reading from gateway requests table)
# ═══════════════════════════════════════════════════════════════════════

class TestOpenCodeFetchUsage:
    def test_empty_db_returns_empty(self, plugin):
        assert plugin.fetch_usage() == []

    def test_single_request(self, plugin, engine):
        _insert_request(engine,
            timestamp="2025-10-10T12:00:00",
            model="deepseek-v4-pro",
            prompt_tokens=1000, completion_tokens=500,
        )
        result = plugin.fetch_usage()
        assert len(result) == 1
        row = result[0]
        assert row["date"] == "2025-10-10"
        assert row["model"] == "deepseek-v4-pro"
        assert row["provider"] == "opencode"
        assert row["prompt_tokens"] == 1000
        assert row["completion_tokens"] == 500
        assert row["request_count"] == 1

    def test_multiple_requests_same_day(self, plugin, engine):
        _insert_request(engine,
            timestamp="2025-10-10T12:00:00",
            prompt_tokens=1000, completion_tokens=200,
        )
        _insert_request(engine,
            timestamp="2025-10-10T12:01:00",
            prompt_tokens=500, completion_tokens=100,
        )
        result = plugin.fetch_usage()
        assert len(result) == 1
        row = result[0]
        assert row["prompt_tokens"] == 1500  # 1000 + 500
        assert row["completion_tokens"] == 300  # 200 + 100
        assert row["request_count"] == 2

    def test_only_opencode_provider(self, plugin, engine):
        """Non-opencode requests should be excluded."""
        _insert_request(engine,
            timestamp="2025-10-10T12:00:00",
            provider="openai", prompt_tokens=999,
        )
        result = plugin.fetch_usage()
        assert result == []

    def test_only_successful_requests(self, plugin, engine):
        """Failed requests should be excluded."""
        _insert_request(engine,
            timestamp="2025-10-10T12:00:00",
            success=0, error_type="timeout",
            prompt_tokens=100, completion_tokens=50,
        )
        result = plugin.fetch_usage()
        assert result == []

    def test_date_filtering(self, plugin, engine):
        _insert_request(engine, timestamp="2025-10-09T12:00:00",
                        prompt_tokens=100, completion_tokens=10)
        _insert_request(engine, timestamp="2025-10-10T12:00:00",
                        prompt_tokens=200, completion_tokens=20)
        result = plugin.fetch_usage(start_date="2025-10-10")
        assert len(result) == 1
        assert result[0]["date"] == "2025-10-10"

        result2 = plugin.fetch_usage(end_date="2025-10-09")
        assert len(result2) == 1
        assert result2[0]["date"] == "2025-10-09"

    def test_no_engine_returns_empty(self):
        p = OpenCodeCostPlugin(engine=None)
        assert p.fetch_usage() == []

    def test_db_error_returns_empty(self, engine):
        """Session error should return empty list gracefully."""
        with patch("src.api.models.get_session",
                   side_effect=RuntimeError("boom")):
            p = OpenCodeCostPlugin(engine=engine)
            result = p.fetch_usage()
            assert result == []


# ═══════════════════════════════════════════════════════════════════════
# fetch_balance
# ═══════════════════════════════════════════════════════════════════════

class TestOpenCodeFetchBalance:
    """Credits come from the console session token, not a scraped page.

    ``GET /api/billing/account`` rejects a service API key with 403 even at
    ``all`` permissions, and the page the old implementation scraped no longer
    exists — it returns the same 1565-byte client shell as every console route.
    The happy path therefore needs a session minted by the console OAuth flow.
    """

    def test_no_credential_returns_none(self, plugin):
        """Nothing configured at all → plugin stays quiet (returns None)."""
        with patch.object(plugin, "_token", return_value=""), \
             patch("src.api.cost_plugins.console_oauth.current_access_token",
                   return_value=""):
            assert plugin.fetch_balance() is None

    def test_service_key_without_session_reports_the_missing_step(self, plugin):
        """A key but no session is a precise, actionable state — not a dead cookie."""
        with patch.object(plugin, "_token", return_value="oc_sk_all"), \
             patch("src.api.cost_plugins.console_oauth.current_access_token",
                   return_value=""):
            result = plugin.fetch_balance()
        assert result["_error"] == "auth_failed"
        assert "console_oauth start" in result["detail"]

    def test_returns_credits_from_the_console_account_route(self, plugin):
        """Happy path: micro-cents from /api/billing/account → USD credits."""
        with patch.object(plugin, "_token", return_value="oc_sk_all"), \
             patch("src.api.cost_plugins.console_oauth.current_access_token",
                   return_value="sess"), \
             patch("src.api.cost_plugins.opencode_api.fetch_account_credits",
                   return_value={"availableMicroCents": "1234000000",
                                 "currency": "USD", "plan": "go"}) as mock_fetch:
            result = plugin.fetch_balance()
        assert result["available_credits"] == 12.34
        assert result["balance"] == 12.34
        assert result["plan"] == "go"
        assert mock_fetch.call_args[0][0] == "sess"

    def test_unrecognised_payload_is_flagged_not_fabricated(self, plugin):
        """A shape change upstream must never yield an invented balance."""
        with patch.object(plugin, "_token", return_value="oc_sk_all"), \
             patch("src.api.cost_plugins.console_oauth.current_access_token",
                   return_value="sess"), \
             patch("src.api.cost_plugins.opencode_api.fetch_account_credits",
                   return_value={"surprise": True}):
            result = plugin.fetch_balance()
        assert result["_error"] == "api_error"
        assert "unrecognised" in result["detail"]

    def test_session_token_round_trips_through_the_credential_store(self, plugin, tmp_path):
        """The console session is read back out of the encrypted store."""
        import json as _json
        import os as _os
        import time as _time

        import src.api.credential_store as cs_module
        from src.api.credential_store import CredentialStore
        from src.api.models import Base, get_engine

        engine = get_engine(":memory:")
        Base.metadata.create_all(engine)
        cs_module._credential_store = CredentialStore(engine, data_dir=str(tmp_path))
        with patch.dict(_os.environ, {"LCP_SECRET_KEY": "test-master"}, clear=False):
            cs_module._credential_store.set("opencode_console", _json.dumps({
                "client_id": "oac_1", "access_token": "sess-from-store",
                "refresh_token": "r", "expires_at": _time.time() + 3600,
            }))
            with patch("src.api.cost_plugins.opencode_api.fetch_account_credits",
                       return_value={"availableMicroCents": 500000000}) as mock_fetch:
                result = plugin.fetch_balance()
        assert result["available_credits"] == 5.0
        assert mock_fetch.call_args[0][0] == "sess-from-store"


# ═══════════════════════════════════════════════════════════════════════
# fetch_summary
# ═══════════════════════════════════════════════════════════════════════

class TestOpenCodeFetchSummary:
    def test_summary_empty_db(self, plugin):
        """Empty DB returns zeros for all periods."""
        result = plugin.fetch_summary()
        assert result is not None
        for period in ("daily", "weekly", "monthly"):
            assert result[period]["tokens"] == 0
            assert result[period]["cost"] == 0.0
            assert result[period]["requests"] == 0

    def test_summary_with_data(self, engine):
        """Recent requests should appear in daily/weekly/monthly aggregates."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        recent = now.strftime("%Y-%m-%dT%H:%M:%S")

        _insert_request(engine, timestamp=recent,
                        prompt_tokens=1000, completion_tokens=500,
                        cost=0.00087)
        _insert_request(engine, timestamp=recent,
                        prompt_tokens=2000, completion_tokens=1000,
                        cost=0.005)
        plugin = OpenCodeCostPlugin(engine=engine)
        result = plugin.fetch_summary()
        assert result is not None

        assert result["daily"]["tokens"] == 4500  # 1000+500+2000+1000
        assert result["daily"]["cost"] == pytest.approx(0.00587, rel=1e-5)
        assert result["daily"]["requests"] == 2

        # Weekly/monthly should be same (both within current window)
        assert result["weekly"]["tokens"] >= 4500
        assert result["weekly"]["requests"] >= 2
        assert result["monthly"]["tokens"] >= 4500

    def test_summary_none_when_no_engine(self):
        """Should return None when engine is None."""
        plugin = OpenCodeCostPlugin(engine=None)
        assert plugin.fetch_summary() is None

    def test_summary_none_on_db_error(self, engine):
        """DB error should return None gracefully."""
        with patch("src.api.models.get_session",
                   side_effect=RuntimeError("boom")):
            plugin = OpenCodeCostPlugin(engine=engine)
            result = plugin.fetch_summary()
            assert result is None


# ═══════════════════════════════════════════════════════════════════════
# fetch_subscription
# ═══════════════════════════════════════════════════════════════════════


class TestOpenCodeFetchSubscription:
    """Plan windows are a console-session read — the page scrape is dead.

    Re-pointed 2026-09-26: the old contract (an ``auth`` cookie → the scraped
    workspace billing page) cannot work any more.  That page now returns a
    1565-byte client-rendered shell byte-identical to ``/console``, and the
    plugin no longer sends a cookie at all.
    """

    def _with_session(self, token="sess-token"):
        return patch("src.api.cost_plugins.console_oauth.current_access_token",
                     return_value=token)

    def _with_org(self, org_id="wrk_test"):
        """Pin the org id so tests never reach the console via /api/orgs."""
        return patch("src.api.cost_plugins.console_oauth.resolve_org_id",
                     return_value=org_id)

    def test_returns_error_without_a_console_session(self, plugin):
        """No session → the actionable one-time-approval reason, not a cookie."""
        with patch("src.api.cost_plugins.console_oauth.current_access_token",
                   return_value=None):
            result = plugin.fetch_subscription()
        assert result is not None
        assert result["_error"] == "auth_failed"
        assert "console session" in result["detail"]
        assert "cookie" not in result["detail"].lower()

    def test_absent_plan_limit_falls_back_to_vendor_numbers(self, plugin):
        """The live console reports limit_micro_cents: null for this org.

        A percentage without a denominator must never be invented, so the
        payload carries the vendor's real totals instead of a bar.
        """
        members = [{"user_id": "acc_1", "limit_micro_cents": None,
                    "spent_micro_cents": "0", "exceeded": False,
                    "resets_at": "2026-10-01T00:00:00.000Z"}]
        summary = {"totalRequests": "9655", "totalInputTokens": "106384603",
                   "totalOutputTokens": "11708909",
                   "totalCacheReadTokens": "1549768128",
                   "totalCostMicroCents": "4097884716"}
        with self._with_session(), self._with_org():
            with patch("src.api.cost_plugins.opencode_api.fetch_budget_members",
                       return_value=members):
                with patch("src.api.cost_plugins.opencode_api.fetch_usage_summary",
                           return_value=summary):
                    result = plugin.fetch_subscription()
        assert "rolling_pct" not in result
        assert "monthly_pct" not in result
        assert result["limit_available"] is False
        assert result["total_requests"] == 9655
        assert result["total_cache_read_tokens"] == 1549768128
        assert result["total_cost_usd"] == 40.97884716

    def test_limit_is_mapped_to_a_monthly_window(self, plugin):
        """A real limit yields spent/limit, with the month-boundary reset."""
        from datetime import datetime, timedelta, timezone
        resets = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        members = [{"limit_micro_cents": 1_000_000_000,      # $10
                    "spent_micro_cents": 250_000_000,        # $2.50
                    "resets_at": resets}]
        with self._with_session():
            with patch("src.api.cost_plugins.opencode_api.fetch_budget_members",
                       return_value=members):
                result = plugin.fetch_subscription()
        assert result["monthly_pct"] == 25.0
        assert result["monthly_reset_sec"] > 0

    def test_budget_route_403_falls_through_to_vendor_numbers(self, plugin):
        """``/api/v1/*`` is service-key only, so a session 403 there is expected.

        It must never be reported as a dead credential: the session is fine,
        that route simply is not open to it.
        """
        from src.api.cost_plugins.opencode_api import ConsoleApiError
        summary = {"totalRequests": "11", "totalCostMicroCents": "2195760"}
        with self._with_session(), self._with_org():
            with patch("src.api.cost_plugins.opencode_api.fetch_budget_members",
                       side_effect=ConsoleApiError(403, "u", "Forbidden", "")):
                with patch("src.api.cost_plugins.opencode_api.fetch_usage_summary",
                           return_value=summary):
                    result = plugin.fetch_subscription()
        assert "_error" not in result
        assert result["total_requests"] == 11
        assert result["total_cost_usd"] == 0.0219576

    def test_usage_route_error_names_the_route(self, plugin):
        """A real failure of the route we *do* use must name the route."""
        from src.api.cost_plugins.opencode_api import ConsoleApiError
        with self._with_session(), self._with_org():
            with patch("src.api.cost_plugins.opencode_api.fetch_budget_members",
                       side_effect=ConsoleApiError(401, "u", "Unauthorized", "")):
                with patch("src.api.cost_plugins.opencode_api.fetch_usage_summary",
                           side_effect=ConsoleApiError(401, "u", "Unauthorized", "")):
                    result = plugin.fetch_subscription()
        assert result["_error"] == "auth_failed"
        assert "401" in result["detail"]
        assert "usage/summary" in result["detail"]

    def test_unexpected_budget_failure_still_reports_usage(self, plugin):
        """A broken budget route must not hide the numbers we *can* read."""
        summary = {"totalRequests": "3", "totalCostMicroCents": "1000000"}
        with self._with_session(), self._with_org():
            with patch("src.api.cost_plugins.opencode_api.fetch_budget_members",
                       side_effect=RuntimeError("network down")):
                with patch("src.api.cost_plugins.opencode_api.fetch_usage_summary",
                           return_value=summary):
                    result = plugin.fetch_subscription()
        assert result["total_requests"] == 3
        assert result["total_cost_usd"] == 0.01

    def test_mock_env_short_circuits_without_a_session(self, plugin, monkeypatch):
        monkeypatch.setenv("LCP_MOCK_PLUGIN_DATA", "1")
        with patch("src.api.cost_plugins.console_oauth.current_access_token",
                   return_value=None):
            result = plugin.fetch_subscription()
        assert result["rolling_pct"] == 17.0


    # ── credit_status ─────────────────────────────────────────────────────

    def test_credit_status_balance_first_funded(self, plugin):
        """Explicit available balance (> $1) → funded even at 100% monthly."""
        assert plugin.credit_status(
            {"monthly_pct": 100},
            {"available_credits": 7.81, "balance": 7.81}) == "funded"

    def test_credit_status_monthly_drained_without_balance(self, plugin):
        """No balance payload; monthly_pct >= 95 → drained."""
        assert plugin.credit_status({"monthly_pct": 98}, None) == "drained"

    def test_credit_status_balance_drained(self, plugin):
        """Balance <= $1 → drained."""
        assert plugin.credit_status(None, {"available_credits": 0.30, "balance": 0.30}) == "drained"

    def test_credit_status_drained_on_error_payload(self, plugin):
        assert plugin.credit_status({"_error": "auth_failed"}, None) == "drained"

    def test_credit_status_unknown(self, plugin):
        assert plugin.credit_status(None, None) == "unknown"
        assert plugin.credit_status({}, {}) == "unknown"


class TestOpenCodeMonthToDate:
    """The monthly dimension: there is no plan quota to fill.

    ``/api/usage/{limits,quota,windows,current}`` all 404 and
    ``creditLimitMicroCents`` is null, so "how much is left this month" has no
    vendor answer.  "How much has been used this month" does — summed from the
    daily rows, because the range enum has no month-aligned window.
    """

    def _month(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def test_sums_only_the_current_month(self, plugin):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        this_month = now.strftime("%Y-%m")
        prev_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        rows = [
            {"date": f"{this_month}-01", "totalCostMicroCents": "100000000",
             "totalRequests": "10", "totalTokens": "1000"},
            {"date": f"{this_month}-02", "totalCostMicroCents": "50000000",
             "totalRequests": "5", "totalTokens": "500"},
            {"date": f"{prev_month}-30", "totalCostMicroCents": "900000000",
             "totalRequests": "90", "totalTokens": "9000"},
        ]
        with patch("src.api.cost_plugins.opencode_api.fetch_usage_cost_by_day",
                   return_value=rows):
            out = plugin._month_to_date("tok", "wrk_test")
        assert out["month_to_date_usd"] == 1.5      # 150,000,000 micro-cents
        assert out["month_requests"] == 15
        assert out["month_tokens"] == 1500
        assert out["month_days"] == 2
        assert out["month_label"].endswith(str(now.year))

    def test_all_and_30d_are_unioned_without_double_counting(self, plugin):
        """Neither window reaches day 1 of a 31-day month on its own."""
        month = self._month()
        day1 = {"date": f"{month}-01", "totalCostMicroCents": "10000000",
                "totalRequests": "1", "totalTokens": "10"}
        day2 = {"date": f"{month}-02", "totalCostMicroCents": "20000000",
                "totalRequests": "2", "totalTokens": "20"}
        seen = []

        def fake(token, range_, org_id=None):
            seen.append(range_)
            return [day1, day2] if range_ == "all" else [day2]

        with patch("src.api.cost_plugins.opencode_api.fetch_usage_cost_by_day",
                   side_effect=fake):
            out = plugin._month_to_date("tok")
        assert seen == ["all", "30d"]
        assert out["month_to_date_usd"] == 0.3     # day2 counted once
        assert out["month_requests"] == 3

    def test_a_console_error_yields_no_month_fields(self, plugin):
        """A failed month read must not fabricate zeros or break the 7d card."""
        from src.api.cost_plugins.opencode_api import ConsoleApiError
        with patch("src.api.cost_plugins.opencode_api.fetch_usage_cost_by_day",
                   side_effect=ConsoleApiError(400, "u", "BadRequest", "")):
            assert plugin._month_to_date("tok") == {}

    def test_usage_numbers_carry_the_month_block(self, plugin):
        month = self._month()
        summary = {"totalRequests": "9", "totalCostMicroCents": "30000000",
                   "totalCacheReadTokens": "700"}
        daily = [{"date": f"{month}-05", "totalCostMicroCents": "10000000",
                  "totalRequests": "4", "totalTokens": "40"}]
        with patch("src.api.cost_plugins.console_oauth.resolve_org_id",
                   return_value="wrk_t"), \
             patch("src.api.cost_plugins.opencode_api.fetch_usage_summary",
                   return_value=summary), \
             patch("src.api.cost_plugins.opencode_api.fetch_usage_cost_by_day",
                   return_value=daily):
            out = plugin._console_usage_numbers("tok")
        assert out["total_requests"] == 9
        assert out["total_cost_usd"] == 0.30
        assert out["month_to_date_usd"] == 0.10
        assert out["month_requests"] == 4
        assert out["limit_available"] is False


# Reset timestamps are RELATIVE to now. They used to be hardcoded absolutes
# (2026-09-26 / 2026-10-03), which silently turned
# ``test_maps_every_window_with_reset_and_status`` red the moment the wall clock
# passed them — a permanently-failing test that hides real regressions.
from datetime import datetime, timedelta, timezone as _tz


def _resets(delta: timedelta) -> str:
    return (datetime.now(_tz.utc) + delta).strftime("%Y-%m-%dT%H:%M:%S.000Z")


GO_USAGE_PAYLOAD = {
    "rolling": {"status": "ok", "percent": 0, "resetsAt": _resets(timedelta(hours=2))},
    "weekly": {"status": "ok", "percent": 0, "resetsAt": _resets(timedelta(days=3))},
    "monthly": {"status": "rate-limited", "percent": 100,
                "resetsAt": _resets(timedelta(days=12))},
}


class TestOpenCodeGoPlanWindows:
    """``GET /inference/go/v1/usage`` is the plan this plugin tracks.

    Measured 2026-09-26: the Go endpoint reports rolling/weekly/monthly with
    ``status`` + ``percent`` + ``resetsAt`` and needs only the provider key —
    no console session and no ``x-org-id``.  The console API has no quota route
    at all, so before this the card could not show a single real window.
    """

    def test_maps_every_window_with_reset_and_status(self):
        from src.api.cost_plugins.opencode_api import plan_windows_from_go

        out = plan_windows_from_go(GO_USAGE_PAYLOAD)
        assert out["rolling_pct"] == 0.0
        assert out["weekly_pct"] == 0.0
        assert out["monthly_pct"] == 100.0
        assert out["rolling_status"] == "ok"
        assert out["monthly_status"] == "rate-limited"
        assert isinstance(out["monthly_reset_sec"], int)
        assert out["monthly_reset_sec"] > 0
        assert out["rolling_reset_sec"] < out["monthly_reset_sec"]

    def test_tolerates_a_partial_payload(self):
        from src.api.cost_plugins.opencode_api import plan_windows_from_go

        out = plan_windows_from_go({"monthly": {"status": "ok", "percent": "42.5"}})
        assert out["monthly_pct"] == 42.5
        assert "rolling_pct" not in out
        assert out == {"monthly_pct": 42.5, "monthly_status": "ok"}

    def test_junk_in_is_nothing_out(self):
        from src.api.cost_plugins.opencode_api import plan_windows_from_go

        assert plan_windows_from_go({}) == {}
        assert plan_windows_from_go(None) == {}
        assert plan_windows_from_go({"monthly": "nonsense"}) == {}
        assert plan_windows_from_go({"monthly": {"percent": "abc"}}) == {}

    def test_go_usage_needs_only_the_provider_key(self, plugin):
        """No console session: the Go endpoint is the provider-key surface."""
        with patch.object(plugin, "_token", lambda **kw: "oc_sk_all"), \
             patch("src.api.cost_plugins.opencode_api.fetch_go_usage",
                   return_value=GO_USAGE_PAYLOAD), \
             patch("src.api.cost_plugins.console_oauth.current_access_token",
                   return_value=None):
            out = plugin.fetch_subscription()
        assert out["source"] == "go-usage"
        assert out["monthly_pct"] == 100.0
        assert out["limit_available"] is True
        assert "_error" not in out

    def test_go_windows_win_over_the_console_path(self, plugin):
        with patch.object(plugin, "_token", lambda **kw: "oc_sk_all"), \
             patch("src.api.cost_plugins.opencode_api.fetch_go_usage",
                   return_value=GO_USAGE_PAYLOAD), \
             patch("src.api.cost_plugins.opencode_api.fetch_budget_members",
                   side_effect=AssertionError("console path must not be reached")), \
             patch("src.api.cost_plugins.console_oauth.current_access_token",
                   return_value="sess"):
            out = plugin.fetch_subscription()
        assert out["monthly_pct"] == 100.0

    def test_go_failure_falls_back_to_the_console_usage_numbers(self, plugin):
        """A key without Go access must not blank the card."""
        from src.api.cost_plugins.opencode_api import ConsoleApiError
        summary = {"totalRequests": "7", "totalCostMicroCents": "1000000"}
        with patch.object(plugin, "_token", lambda **kw: "oc_sk_nogo"), \
             patch("src.api.cost_plugins.opencode_api.fetch_go_usage",
                   side_effect=ConsoleApiError(403, "u", "AuthError", "Forbidden")), \
             patch("src.api.cost_plugins.opencode_api.fetch_budget_members",
                   side_effect=ConsoleApiError(401, "u", "Unauthorized", "")), \
             patch("src.api.cost_plugins.opencode_api.fetch_usage_summary",
                   return_value=summary), \
             patch("src.api.cost_plugins.console_oauth.current_access_token",
                   return_value="sess"), \
             patch("src.api.cost_plugins.console_oauth.resolve_org_id",
                   return_value="wrk_t"), \
             patch("src.api.cost_plugins.opencode_api.fetch_usage_cost_by_day",
                   return_value=[]):
            out = plugin.fetch_subscription()
        assert out["total_requests"] == 7
        assert out["total_cost_usd"] == 0.01
        assert out["limit_available"] is False

    def test_go_windows_are_enriched_with_console_numbers(self, plugin):
        """Both sources land in one payload: bars from Go, dollars from console."""
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        summary = {"totalRequests": "9", "totalCostMicroCents": "30000000"}
        daily = [{"date": f"{month}-05", "totalCostMicroCents": "10000000",
                  "totalRequests": "4", "totalTokens": "40"}]
        with patch.object(plugin, "_token", lambda **kw: "oc_sk_all"), \
             patch("src.api.cost_plugins.opencode_api.fetch_go_usage",
                   return_value=GO_USAGE_PAYLOAD), \
             patch("src.api.cost_plugins.opencode_api.fetch_usage_summary",
                   return_value=summary), \
             patch("src.api.cost_plugins.opencode_api.fetch_usage_cost_by_day",
                   return_value=daily), \
             patch("src.api.cost_plugins.console_oauth.current_access_token",
                   return_value="sess"), \
             patch("src.api.cost_plugins.console_oauth.resolve_org_id",
                   return_value="wrk_t"):
            out = plugin.fetch_subscription()
        assert out["source"] == "go-usage+console"  # both sources named
        assert out["monthly_pct"] == 100.0          # bars: from the Go endpoint
        assert out["month_to_date_usd"] == 0.10     # numbers: from the console
        assert out["total_cost_usd"] == 0.30
        assert out["limit_available"] is True       # bars exist, so not "missing"
        assert "_error" not in out
