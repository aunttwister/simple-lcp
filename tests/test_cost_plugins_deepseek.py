"""Tests for the DeepSeek cost tracking plugin."""

import json
from unittest.mock import patch, MagicMock
from urllib.error import URLError

import pytest
from src.api.cost_plugins.deepseek import DeepSeekCostPlugin, _PRICING, _BALANCE_URL


# ═══════════════════════════════════════════════════════════════════════
# Pricing
# ═══════════════════════════════════════════════════════════════════════

class TestDeepSeekPricing:
    def setup_method(self):
        self.plugin = DeepSeekCostPlugin()

    def test_provider_name(self):
        assert self.plugin.provider_name == "deepseek"

    def test_supported_models(self):
        models = self.plugin.get_supported_models()
        assert "deepseek-v4-pro" in models
        assert "deepseek-v4-flash" in models

    def test_get_pricing_known(self):
        p = self.plugin.get_pricing("deepseek-v4-pro")
        assert p == _PRICING["deepseek-v4-pro"]
        assert p["cache_hit"] == 0.003625

    def test_get_pricing_flash(self):
        p = self.plugin.get_pricing("deepseek-v4-flash")
        assert p == _PRICING["deepseek-v4-flash"]
        assert p["cache_hit"] == 0.0028

    def test_get_pricing_unknown(self):
        assert self.plugin.get_pricing("nonexistent-model") is None


# ═══════════════════════════════════════════════════════════════════════
# calculate_cost
# ═══════════════════════════════════════════════════════════════════════

class TestDeepSeekCalculateCost:
    def setup_method(self):
        self.plugin = DeepSeekCostPlugin()

    def test_v4_pro_mixed_tokens(self):
        """v4-pro: 1M cache_hit + 1M cache_miss + 500K output"""
        cost = self.plugin.calculate_cost("deepseek-v4-pro", {
            "prompt_cache_hit_tokens": 1_000_000,
            "prompt_cache_miss_tokens": 1_000_000,
            "completion_tokens": 500_000,
        })
        expected = (1_000_000 / 1_000_000) * 0.003625 \
                   + (1_000_000 / 1_000_000) * 0.435 \
                   + (500_000 / 1_000_000) * 0.87
        assert cost == pytest.approx(expected)

    def test_v4_flash_only_output(self):
        cost = self.plugin.calculate_cost("deepseek-v4-flash", {
            "prompt_tokens": 0,
            "completion_tokens": 100_000,
        })
        expected = (100_000 / 1_000_000) * 0.28
        assert cost == pytest.approx(expected)

    def test_cache_only_auto_miss(self):
        """When only prompt_tokens is given and no cache_hit/miss, all treated as miss."""
        cost = self.plugin.calculate_cost("deepseek-v4-pro", {
            "prompt_tokens": 500_000,
            "completion_tokens": 0,
        })
        expected = (500_000 / 1_000_000) * 0.435
        assert cost == pytest.approx(expected)

    def test_cache_hit_auto_miss_derived(self):
        """When cache_hit is set but cache_miss is not, derive from prompt - hit."""
        cost = self.plugin.calculate_cost("deepseek-v4-pro", {
            "prompt_tokens": 1000,
            "prompt_cache_hit_tokens": 300,
            "completion_tokens": 0,
        })
        # cache_miss = 1000 - 300 = 700
        expected = (700 / 1_000_000) * 0.435 + (300 / 1_000_000) * 0.003625
        assert cost == pytest.approx(expected, rel=1e-5)

    def test_unknown_model_returns_none(self):
        cost = self.plugin.calculate_cost("unknown-model", {"prompt_tokens": 100})
        assert cost is None


# ═══════════════════════════════════════════════════════════════════════
# fetch_balance
# ═══════════════════════════════════════════════════════════════════════

class TestDeepSeekFetchBalance:
    def setup_method(self):
        self.plugin = DeepSeekCostPlugin()

    def test_no_api_key_returns_none(self):
        with patch.dict("os.environ", {}, clear=True):
            assert self.plugin.fetch_balance() is None

    def test_api_key_from_credential_store(self):
        """Balance query uses the UI-managed key from the credential store."""
        from unittest.mock import patch as _patch
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps({
            "is_available": True,
            "balance_infos": [{
                "currency": "USD",
                "total_balance": "9.99",
                "granted_balance": "10.00",
                "topped_up_balance": "0.00",
            }],
        }).encode("utf-8")
        fake_resp.__enter__.return_value = fake_resp

        store = MagicMock()
        store.get.return_value = "sk-stored"
        with _patch.dict("os.environ", {}, clear=True):  # no env var — must use store
            with _patch("src.api.credential_store.get_credential_store", return_value=store):
                with _patch("src.api.cost_plugins.deepseek.urlopen", return_value=fake_resp) as mock_req:
                    result = self.plugin.fetch_balance()

        assert result is not None
        assert result["balance"] == 9.99
        store.get.assert_called_with("deepseek")
        # Verify the stored key was used in the Authorization header
        args, _ = mock_req.call_args
        assert args[0].headers["Authorization"] == "Bearer sk-stored"

    def test_successful_balance_query(self):
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps({
            "is_available": True,
            "balance_infos": [{
                "currency": "USD",
                "total_balance": "42.50",
                "granted_balance": "100.00",
                "topped_up_balance": "0.00",
            }],
        }).encode("utf-8")
        fake_resp.__enter__.return_value = fake_resp

        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}):
            with patch("src.api.cost_plugins.deepseek.urlopen", return_value=fake_resp) as mock_req:
                result = self.plugin.fetch_balance()

        assert result is not None
        assert result["balance"] == 42.50
        assert result["currency"] == "USD"
        assert result["total_granted"] == 100.0
        assert result["topped_up"] == 0.0
        # Verify the correct URL was called
        args, _ = mock_req.call_args
        assert args[0].full_url == _BALANCE_URL

    def test_balance_is_cached(self):
        """Within cache TTL, the API should not be called again."""
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps({
            "is_available": True,
            "balance_infos": [{"currency": "USD", "total_balance": "10.00",
                                "granted_balance": "0.00", "topped_up_balance": "10.00"}],
        }).encode("utf-8")
        fake_resp.__enter__.return_value = fake_resp

        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}):
            with patch("src.api.cost_plugins.deepseek.urlopen", return_value=fake_resp) as mock_req:
                # First call hits API
                r1 = self.plugin.fetch_balance()
                assert r1["balance"] == 10.0
                assert mock_req.call_count == 1

                # Second call should be cached
                r2 = self.plugin.fetch_balance()
                assert r2["balance"] == 10.0
                assert mock_req.call_count == 1  # still 1 — no API call

    def test_cache_expires(self):
        """After cache TTL elapses, the API should be called again."""
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps({
            "is_available": True,
            "balance_infos": [{"currency": "USD", "total_balance": "10.00",
                                "granted_balance": "0.00", "topped_up_balance": "10.00"}],
        }).encode("utf-8")
        fake_resp.__enter__.return_value = fake_resp

        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}):
            with patch("src.api.cost_plugins.deepseek.urlopen", return_value=fake_resp):
                self.plugin.fetch_balance()
                # Manually expire cache
                self.plugin._balance_cached_at = 0
                self.plugin.fetch_balance()
                # We can verify cache was bypassed by checking the state
                assert self.plugin._balance_cached_at > 0

    def test_api_error_returns_none_and_logs(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}):
            with patch("src.api.cost_plugins.deepseek.urlopen", side_effect=URLError("timeout")):
                result = self.plugin.fetch_balance()
        assert result is None

    def test_malformed_json_returns_none(self):
        fake_resp = MagicMock()
        fake_resp.read.return_value = b"not json"
        fake_resp.__enter__.return_value = fake_resp

        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}):
            with patch("src.api.cost_plugins.deepseek.urlopen", return_value=fake_resp):
                result = self.plugin.fetch_balance()
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# credit_status
# ═══════════════════════════════════════════════════════════════════════

class TestDeepSeekCreditStatus:
    def setup_method(self):
        self.plugin = DeepSeekCostPlugin()

    def test_funded_when_balance_above_threshold(self):
        assert self.plugin.credit_status(None, {"balance": 42.50}) == "funded"

    def test_drained_when_balance_low(self):
        assert self.plugin.credit_status(None, {"balance": 0.30}) == "drained"

    def test_drained_at_threshold(self):
        assert self.plugin.credit_status(None, {"balance": 1.0}) == "drained"

    def test_unknown_without_balance(self):
        assert self.plugin.credit_status(None, None) == "unknown"
        assert self.plugin.credit_status({}, {}) == "unknown"


# ═══════════════════════════════════════════════════════════════════════
# fetch_summary
# ═══════════════════════════════════════════════════════════════════════

class TestDeepSeekFetchSummary:
    def setup_method(self):
        self.plugin = DeepSeekCostPlugin()

    def test_summary_with_balance(self):
        """Real DeepSeek v2 API format: balance_infos[0].total_balance."""
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps({
            "is_available": True,
            "balance_infos": [{
                "currency": "USD",
                "total_balance": "1.19",
                "granted_balance": "0.00",
                "topped_up_balance": "1.19",
            }],
        }).encode("utf-8")
        fake_resp.__enter__.return_value = fake_resp

        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}):
            with patch("src.api.cost_plugins.deepseek.urlopen", return_value=fake_resp):
                result = self.plugin.fetch_summary()
        assert result is not None
        bal = result["balance"]
        assert bal["available"] == 1.19
        assert bal["spent"] == 0.0      # granted 0 + topped_up 1.19 - available 1.19
        assert bal["total_granted"] == 0.0
        assert bal["topped_up"] == 1.19
        assert bal["currency"] == "USD"

    def test_summary_spent_positive(self):
        """When some credits have been spent, spent > 0."""
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps({
            "is_available": True,
            "balance_infos": [{
                "currency": "USD",
                "total_balance": "5.23",
                "granted_balance": "5.00",
                "topped_up_balance": "5.00",
            }],
        }).encode("utf-8")
        fake_resp.__enter__.return_value = fake_resp

        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}):
            with patch("src.api.cost_plugins.deepseek.urlopen", return_value=fake_resp):
                result = self.plugin.fetch_summary()
        bal = result["balance"]
        assert bal["available"] == 5.23
        assert bal["spent"] == pytest.approx(4.77, rel=1e-5)  # 10.0 - 5.23

    def test_summary_no_total_granted(self):
        """When total_granted is 0, spent should be 0 (not None — we know the total)."""
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps({
            "is_available": True,
            "balance_infos": [{
                "currency": "CNY",
                "total_balance": "3.14",
                "granted_balance": "0.00",
                "topped_up_balance": "3.14",
            }],
        }).encode("utf-8")
        fake_resp.__enter__.return_value = fake_resp

        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}):
            with patch("src.api.cost_plugins.deepseek.urlopen", return_value=fake_resp):
                result = self.plugin.fetch_summary()
        assert result is not None
        bal = result["balance"]
        assert bal["available"] == 3.14
        assert bal["spent"] == 0.0
        assert bal["total_granted"] == 0.0
        assert bal["currency"] == "CNY"

    def test_summary_no_api_key(self):
        """Without API key, summary should be None.

        ``clear=True`` is the point of the test, not decoration. This host exports a
        real DEEPSEEK_API_KEY, so without clearing it `fetch_summary()` reaches the
        live API (it returned a real balance) and the test asserts the opposite of
        what it means to. That makes it pass only where CI runs and fail on the host
        it was written on — the same class of defect as the two host-data-coupled
        tests that kept CI red for eleven pushes.
        """
        with patch.dict("os.environ", {}, clear=True):
            assert self.plugin.fetch_summary() is None

    def test_summary_api_error(self):
        """API error should propagate as None."""
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}):
            with patch("src.api.cost_plugins.deepseek.urlopen",
                       side_effect=URLError("timeout")):
                result = self.plugin.fetch_summary()
        assert result is None
