"""Tests for the Command Code billing API client (commandcode_api.py)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.api.cost_plugins.commandcode_api import (
    CommandCodeSubscriptionSnapshot,
    fetch_subscription_snapshot,
    fetch_subscription_snapshot_dict,
    _parse_credits,
    _parse_subscription,
    _parse_usage_summary,
    _parse_usage_list,
)


def _credits_payload(**overrides):
    """A realistic /internal/billing/credits response (root windowLimits)."""
    payload = {
        "credits": {
            "belowThreshold": False,
            "creditThreshold": 0,
            "monthlyCredits": 8.7784,
            "purchasedCredits": 0.0,
            "premiumMonthlyCredits": 0.0,
            "opensourceMonthlyCredits": 8.7784,
        },
        "windowLimits": {
            "limited": True,
            "exceeded": None,
            "fiveHour": {"used": 4.48, "cap": 14, "exceeded": False, "resetAt": 1787084876441},
            "weekly": {"used": 21.57, "cap": 35, "exceeded": False, "resetAt": 1787424086179},
        },
    }
    payload.update(overrides)
    return payload


def _subscription_payload(**overrides):
    payload = {
        "success": True,
        "data": {
            "id": "sub_1TTzt3DSZgxV3MJKG4ClCWpn",
            "status": "active",
            "planId": "individual-goat",
            "currentPeriodEnd": "2026-09-08T00:00:00Z",
        },
    }
    payload["data"].update(overrides)
    return payload


def _usage_summary_payload(**overrides):
    payload = {
        "totalCount": 2204,
        "totalCost": 29.809112054199996,
        "averageCost": 0.013525005469237747,
        "successRate": 100,
        "completedCount": 2204,
        "failedCount": 0,
        "totalTokensIn": 622439445,
        "totalTokensOut": 1588190,
        "totalTokens": 624027635,
        "totalCredits": 29.809112054199996,
        "totalFreeCredits": 0,
        "totalMonthlyCredits": 29.809112054199996,
        "totalPurchasedCredits": 0,
        "periodBasis": "billing-period",
    }
    payload.update(overrides)
    return payload


def _usage_list_payload():
    return {
        "usages": [
            {
                "id": "b36423a0",
                "createdAt": "2026-08-18T16:28:49.295Z",
                "tokensIn": "137815",
                "tokensOut": "4716",
                "tokensTotal": "142531",
                "creditsTotal": "0.01250678",
                "durationTotal": "82422",
                "status": "completed",
                "meta": {"model": "deepseek/deepseek-v4-pro"},
                "type": "api",
                "mode": "api",
            },
        ],
        "nextCursor": "...",
        "limit": 10,
    }


class TestParseCredits:
    def test_full_credits(self):
        result = _parse_credits(_credits_payload())
        assert result["monthly_credits_remaining"] == pytest.approx(8.7784)
        assert result["purchased_credits"] == 0.0
        assert result["premium_monthly_credits"] == 0.0
        assert result["opensource_monthly_credits"] == pytest.approx(8.7784)
        # 4.48 / 14 * 100 == 32.0 ; 21.57 / 35 * 100 == 61.6
        assert result["five_hour_pct"] == 32.0
        assert result["weekly_pct"] == 61.6
        assert result["five_hour_used"] == pytest.approx(4.48)
        assert result["five_hour_cap"] == 14.0
        assert result["weekly_used"] == pytest.approx(21.57)
        assert result["weekly_cap"] == 35.0
        # resetAt is an absolute epoch-ms; the countdown must be non-negative.
        assert result["five_hour_reset_sec"] >= 0
        assert result["weekly_reset_sec"] >= 0

    def test_window_limits_nested_in_credits(self):
        payload = _credits_payload()
        wl = payload.pop("windowLimits")
        payload["credits"]["windowLimits"] = wl
        result = _parse_credits(payload)
        assert result["five_hour_pct"] == 32.0
        assert result["weekly_pct"] == 61.6

    def test_zero_credits(self):
        payload = {"credits": {
            "monthlyCredits": 0, "purchasedCredits": 0,
            "premiumMonthlyCredits": 0, "opensourceMonthlyCredits": 0,
        }}
        result = _parse_credits(payload)
        assert result["monthly_credits_remaining"] == 0.0
        assert result["five_hour_pct"] == 0.0
        assert result["weekly_pct"] == 0.0

    def test_missing_credits_key(self):
        result = _parse_credits({})
        assert result["monthly_credits_remaining"] == 0.0
        assert result["five_hour_pct"] == 0.0

    def test_partial_windows(self):
        payload = _credits_payload()
        payload["windowLimits"] = {
            "fiveHour": {"used": "7", "cap": "14", "resetAt": "1787084876441"},
        }
        result = _parse_credits(payload)
        assert result["five_hour_pct"] == 50.0
        assert result["weekly_pct"] == 0.0

    def test_non_numeric_values(self):
        payload = {"credits": {
            "monthlyCredits": "abc",  # bad
        }, "windowLimits": {
            "fiveHour": {"used": "x", "cap": "y", "resetAt": "z"},
        }}
        result = _parse_credits(payload)
        assert result["monthly_credits_remaining"] == 0.0
        assert result["five_hour_pct"] == 0.0
        assert result["five_hour_reset_sec"] == 0

    def test_reset_at_seconds(self):
        """resetAt as absolute seconds (not ms) should still parse."""
        import time
        now_sec = int(time.time())
        payload = _credits_payload()
        payload["windowLimits"]["fiveHour"] = {"used": 7, "cap": 14, "resetAt": now_sec + 3600}
        result = _parse_credits(payload)
        assert 0 < result["five_hour_reset_sec"] <= 3600


class TestParseUsageSummary:
    def test_full(self):
        result = _parse_usage_summary(_usage_summary_payload())
        assert result["total_runs"] == 2204
        assert result["total_tokens"] == 624027635
        assert result["total_tokens_in"] == 622439445
        assert result["total_tokens_out"] == 1588190
        assert result["total_monthly_credits"] == pytest.approx(29.809112054199996)
        assert result["period_basis"] == "billing-period"

    def test_string_tokens(self):
        payload = _usage_summary_payload(totalTokens="12345", totalCount="99")
        result = _parse_usage_summary(payload)
        assert result["total_tokens"] == 12345
        assert result["total_runs"] == 99


class TestParseUsageList:
    def test_full(self):
        result = _parse_usage_list(_usage_list_payload())
        assert len(result) == 1
        row = result[0]
        assert row["model"] == "deepseek/deepseek-v4-pro"
        assert row["tokens_total"] == 142531
        assert row["credits_total"] == pytest.approx(0.01250678)
        assert row["status"] == "completed"

    def test_empty(self):
        assert _parse_usage_list({}) == []


class TestParseSubscription:
    def test_full_subscription(self):
        plan_id, status, period = _parse_subscription(_subscription_payload())
        assert plan_id == "individual-goat"
        assert status == "active"
        assert period == "2026-09-08T00:00:00Z"

    def test_null_data(self):
        plan_id, status, period = _parse_subscription({"success": True, "data": None})
        assert plan_id is None
        assert status is None
        assert period is None

    def test_missing_data_key(self):
        plan_id, status, period = _parse_subscription({"success": True})
        assert plan_id is None
        assert status is None
        assert period is None

    def test_plan_id_variants(self):
        for key in ("planID", "planId", "plan"):
            payload = _subscription_payload()
            # Drop the default planId so only the variant key carries the value
            payload["data"].pop("planId", None)
            payload["data"][key] = "individual-pro"
            plan_id, _, _ = _parse_subscription(payload)
            assert plan_id == "individual-pro"


class TestFetchSubscriptionSnapshot:
    def _mock_json(self, payload):
        """A fake urlopen response returning payload as JSON.

        Must support the ``with urlopen(...) as resp:`` context-manager
        protocol — MagicMock's default ``__enter__`` returns a *different*
        mock, so pin it to return self.
        """
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_cookie_missing_returns_none(self):
        assert fetch_subscription_snapshot(None) is None
        assert fetch_subscription_snapshot("") is None
        assert fetch_subscription_snapshot("   ") is None

    def test_full_fetch(self):
        responses = [
            self._mock_json(_credits_payload()),
            self._mock_json(_subscription_payload()),
            self._mock_json(_usage_summary_payload()),
            self._mock_json(_usage_list_payload()),
        ]
        with patch("src.api.cost_plugins.commandcode_api.urlopen", side_effect=responses):
            snap = fetch_subscription_snapshot("session=valid")
        assert isinstance(snap, CommandCodeSubscriptionSnapshot)
        assert snap.monthly_credits_remaining == pytest.approx(8.7784)
        assert snap.five_hour_pct == 32.0
        assert snap.weekly_pct == 61.6
        assert snap.five_hour_reset_sec >= 0
        assert snap.weekly_reset_sec >= 0
        assert snap.plan_id == "individual-goat"
        assert snap.plan_status == "active"
        assert snap.billing_period_end == "2026-09-08T00:00:00Z"
        # Monthly derived: goat cap 70 - 8.7784 remaining ≈ 61.22 used → 87.5%
        assert snap.monthly_cap == 70.0
        assert snap.monthly_pct == pytest.approx(87.5, abs=0.1)
        assert snap.usage_summary["total_runs"] == 2204
        assert len(snap.recent_runs) == 1

    def test_subscription_endpoint_down_keeps_credits(self):
        """Subscription/usage failures should not break the credits fetch."""
        from urllib.error import HTTPError
        responses = [
            self._mock_json(_credits_payload()),
            HTTPError("url", 503, "Service Unavailable", {}, None),
        ]
        # The HTTPError will be raised from urlopen — patch to raise for calls
        # after the first (subscription + summary + list are best-effort).
        def _fake_urlopen(req, timeout=15):
            call = _fake_urlopen.calls
            _fake_urlopen.calls += 1
            if call == 0:
                return responses[0]
            raise HTTPError("url", 503, "Service Unavailable", {}, None)
        _fake_urlopen.calls = 0

        with patch("src.api.cost_plugins.commandcode_api.urlopen", side_effect=_fake_urlopen):
            snap = fetch_subscription_snapshot("session=valid")
        assert snap is not None
        assert snap.monthly_credits_remaining == pytest.approx(8.7784)
        assert snap.plan_id is None  # enrichment failed but credits survived
        assert snap.usage_summary == {}

    def test_credits_endpoint_auth_failure(self):
        from urllib.error import HTTPError
        err = HTTPError("url", 401, "Unauthorized", {}, None)
        err.read = MagicMock(return_value=b"{}")
        with patch("src.api.cost_plugins.commandcode_api.urlopen", side_effect=err):
            assert fetch_subscription_snapshot("session=bad") is None

    def test_credits_endpoint_http_error(self):
        from urllib.error import HTTPError
        err = HTTPError("url", 500, "Server Error", {}, None)
        err.read = MagicMock(return_value=b"{}")
        with patch("src.api.cost_plugins.commandcode_api.urlopen", side_effect=err):
            assert fetch_subscription_snapshot("session=valid") is None

    def test_network_error(self):
        from urllib.error import URLError
        with patch("src.api.cost_plugins.commandcode_api.urlopen", side_effect=URLError("no route")):
            assert fetch_subscription_snapshot("session=valid") is None

    def test_parse_failure(self):
        resp = MagicMock()
        resp.read.return_value = b"not json at all"
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        with patch("src.api.cost_plugins.commandcode_api.urlopen", return_value=resp):
            assert fetch_subscription_snapshot("session=valid") is None


class TestFetchSubscriptionSnapshotDict:
    def _mock_json(self, payload):
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_returns_dict(self):
        responses = [
            self._mock_json(_credits_payload()),
            self._mock_json(_subscription_payload()),
            self._mock_json(_usage_summary_payload()),
            self._mock_json(_usage_list_payload()),
        ]
        with patch("src.api.cost_plugins.commandcode_api.urlopen", side_effect=responses):
            result = fetch_subscription_snapshot_dict("session=valid")
        assert isinstance(result, dict)
        assert result["monthly_credits_remaining"] == pytest.approx(8.7784)
        assert "five_hour_pct" in result
        assert "monthly_pct" in result
        assert "plan_id" in result
        assert result["usage_summary"]["total_runs"] == 2204

    def test_none_when_fetch_fails(self):
        from urllib.error import URLError
        with patch("src.api.cost_plugins.commandcode_api.urlopen", side_effect=URLError("boom")):
            assert fetch_subscription_snapshot_dict("session=valid") is None

    def test_none_when_cookie_missing(self):
        assert fetch_subscription_snapshot_dict(None) is None


# ═══════════════════════════════════════════════════════════════════════
# The key-authenticated /alpha/* surface (Command Code CLI's own routes)
# ═══════════════════════════════════════════════════════════════════════

# Payloads below are VERBATIM live responses from 2026-10-31 — the point of
# these tests is that the real shapes parse, not that a hand-written fixture does.
_ALPHA_CREDITS = {
    "credits": {
        "belowThreshold": False,
        "creditThreshold": 0,
        "monthlyCredits": 14.8625855977,
        "purchasedCredits": 0,
        "freeCredits": 0,
    },
    "windowLimits": {
        "limited": True,
        "exceeded": None,
        "fiveHour": {"used": 0.556136168, "cap": 14, "exceeded": False,
                     "resetAt": 1793469048756},
        "weekly": {"used": 22.4357623748, "cap": 35, "exceeded": False,
                   "resetAt": 1793502592400},
    },
    "sandboxAccess": True,
}

_ALPHA_SUBSCRIPTIONS = {
    "success": True,
    "data": {
        "id": "sub_1U2DaBDSZgxV3MJKxtix74Xi",
        "status": "active",
        "planId": "individual-goat",
        "quantity": 1,
        "cancelAtPeriodEnd": True,
        "currentPeriodStart": "2026-10-08T17:57:15.000Z",
        "currentPeriodEnd": "2026-11-08T17:57:15.000Z",
    },
}

_ALPHA_USAGE_SUMMARY = {
    "totalCount": 16836,
    "totalCost": 55.184895469999994,
    "averageCost": 0.003277791367902114,
    "successRate": 100,
    "completedCount": 16836,
    "failedCount": 0,
    "totalTokensIn": 2073654148,
    "totalTokensOut": 17434503,
    "totalTokens": 2091088651,
    "totalCredits": 55.184895469999994,
    "periodBasis": "billing-period",
    "periodStart": "2026-10-08T17:57:15.000Z",
    "daily": [],
}


def _routing_urlopen(routes):
    """Fake urlopen dispatching on URL path (query ignored); records URL + headers."""
    calls = []

    def _fake(req, timeout=15):
        url = getattr(req, "full_url", str(req))
        path = url.split("?", 1)[0]
        headers = dict(getattr(req, "headers", {}) or {})
        calls.append((url, headers))
        for suffix, payload in routes.items():
            if path.endswith(suffix):
                if isinstance(payload, Exception):
                    raise payload
                resp = MagicMock()
                resp.read.return_value = json.dumps(payload).encode()
                resp.__enter__ = MagicMock(return_value=resp)
                resp.__exit__ = MagicMock(return_value=False)
                return resp
        raise AssertionError(f"unrouted URL: {url}")

    return _fake, calls


def _auth_header(headers):
    for key, value in headers.items():
        if key.lower() == "authorization":
            return value
    return None


class TestAlphaKeySurface:
    """The API key must be used against /alpha/* and the cookie NOT used."""

    def test_alpha_snapshot_from_live_shapes(self):
        fake, calls = _routing_urlopen({
            "/alpha/billing/credits": _ALPHA_CREDITS,
            "/alpha/billing/subscriptions": _ALPHA_SUBSCRIPTIONS,
            "/alpha/usage/summary": _ALPHA_USAGE_SUMMARY,
        })
        with patch("src.api.cost_plugins.commandcode_api.urlopen", side_effect=fake):
            snap = fetch_subscription_snapshot("session=valid", api_key="cmd_key")

        assert snap is not None
        assert snap.source == "alpha"
        # Credits
        assert snap.monthly_credits_remaining == pytest.approx(14.8625855977)
        assert snap.purchased_credits == 0.0
        assert snap.free_credits == 0.0
        # Windows: 0.556136168/14 = 3.97% -> 4.0 ; 22.4357623748/35 = 64.10%
        assert snap.five_hour_pct == pytest.approx(4.0, abs=0.05)
        assert snap.five_hour_cap == 14.0
        assert snap.weekly_pct == pytest.approx(64.1, abs=0.05)
        assert snap.weekly_cap == 35.0
        assert snap.five_hour_reset_sec >= 0
        assert snap.weekly_reset_sec >= 0
        # Monthly derived from the plan cap (goat = $70): 70 - 14.8626 used = 78.8%
        assert snap.monthly_cap == 70.0
        assert snap.monthly_pct == pytest.approx(78.8, abs=0.1)
        # Subscription
        assert snap.plan_id == "individual-goat"
        assert snap.plan_status == "active"
        assert snap.billing_period_end == "2026-11-08T17:57:15.000Z"
        assert snap.monthly_reset_sec > 0
        # Usage totals
        assert snap.usage_summary["total_runs"] == 16836
        assert snap.usage_summary["total_cost"] == pytest.approx(55.184895469999994)
        # /alpha/usage is not a route, so there is no per-run list on this surface
        assert snap.recent_runs == []
        # Only the three alpha routes were called — the cookie path was NOT used.
        assert len(calls) == 3
        assert all("/alpha/" in url for url, _ in calls)
        assert not any("/internal/" in url for url, _ in calls)
        # Auth is the API key, as a Bearer token.
        assert _auth_header(calls[0][1]) == "Bearer cmd_key"

    def test_alpha_rejection_falls_back_to_cookie(self):
        from urllib.error import HTTPError
        err = HTTPError("url", 401, "Unauthorized", {}, None)
        fake, calls = _routing_urlopen({
            "/alpha/billing/credits": err,
            "/internal/billing/credits": _credits_payload(),
            "/internal/billing/subscriptions": _subscription_payload(),
            "/internal/usage/summary": _usage_summary_payload(),
            "/internal/usage": _usage_list_payload(),
        })
        with patch("src.api.cost_plugins.commandcode_api.urlopen", side_effect=fake):
            snap = fetch_subscription_snapshot("session=valid", api_key="stale_key")

        assert snap is not None
        assert snap.source == "internal"
        assert snap.monthly_credits_remaining == pytest.approx(8.7784)
        assert len(snap.recent_runs) == 1
        assert any("/internal/billing/credits" in url for url, _ in calls)

    def test_alpha_rejection_without_cookie_returns_none(self):
        from urllib.error import HTTPError
        err = HTTPError("url", 401, "Unauthorized", {}, None)
        fake, _ = _routing_urlopen({"/alpha/billing/credits": err})
        with patch("src.api.cost_plugins.commandcode_api.urlopen", side_effect=fake):
            assert fetch_subscription_snapshot(None, api_key="stale_key") is None

    def test_no_key_means_no_alpha_call(self):
        fake, calls = _routing_urlopen({
            "/internal/billing/credits": _credits_payload(),
            "/internal/billing/subscriptions": _subscription_payload(),
            "/internal/usage/summary": _usage_summary_payload(),
            "/internal/usage": _usage_list_payload(),
        })
        with patch("src.api.cost_plugins.commandcode_api.urlopen", side_effect=fake):
            snap = fetch_subscription_snapshot("session=valid")

        assert snap.source == "internal"
        assert not any("/alpha/" in url for url, _ in calls)

    def test_alpha_usage_summary_failure_keeps_credits(self):
        from urllib.error import HTTPError
        err = HTTPError("url", 503, "Service Unavailable", {}, None)
        fake, _ = _routing_urlopen({
            "/alpha/billing/credits": _ALPHA_CREDITS,
            "/alpha/billing/subscriptions": _ALPHA_SUBSCRIPTIONS,
            "/alpha/usage/summary": err,
        })
        with patch("src.api.cost_plugins.commandcode_api.urlopen", side_effect=fake):
            snap = fetch_subscription_snapshot(None, api_key="cmd_key")

        assert snap is not None
        assert snap.source == "alpha"
        assert snap.monthly_credits_remaining == pytest.approx(14.8625855977)
        assert snap.plan_id == "individual-goat"
        assert snap.usage_summary == {}

    def test_dict_reports_credits_source(self):
        fake, _ = _routing_urlopen({
            "/alpha/billing/credits": _ALPHA_CREDITS,
            "/alpha/billing/subscriptions": _ALPHA_SUBSCRIPTIONS,
            "/alpha/usage/summary": _ALPHA_USAGE_SUMMARY,
        })
        with patch("src.api.cost_plugins.commandcode_api.urlopen", side_effect=fake):
            result = fetch_subscription_snapshot_dict(None, api_key="cmd_key")

        assert result["credits_source"] == "alpha"
        assert result["free_credits"] == 0.0
        assert result["monthly_credits_remaining"] == pytest.approx(14.8625855977)
        assert result["plan_id"] == "individual-goat"
