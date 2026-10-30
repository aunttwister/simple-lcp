"""Tests for main.py core functions: record_cost, forward_request, try_chain."""
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock

from src.api.request_pipeline import record_cost
from src.api.models import get_engine, Base


def _cred_patch(fallback="test", **keys):
    """Context manager: a credential store that returns keys[provider] or fallback."""
    store = MagicMock()
    store.get.side_effect = lambda name: keys.get(name, fallback)
    return patch("src.api.credential_store.get_credential_store", return_value=store)


@pytest.fixture
def temp_db():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
    for ext in ["", "-wal", "-shm"]:
        try:
            os.unlink(db_path + ext)
        except FileNotFoundError:
            pass


@pytest.fixture
def mock_config():
    cfg = MagicMock()
    cfg.circuit_breaker = {
        "failures_dead": 5, "dead_cooldown_seconds": 300,
        "failures_degraded": 3, "degraded_cooldown_seconds": 60,
    }
    cfg.pricing = []
    return cfg


class TestRecordCost:
    def test_records_success(self, temp_db):
        cost_info = {
            "prompt_tokens": 100, "completion_tokens": 50,
            "cache_hit_tokens": 0, "cache_miss_tokens": 100,
            "cost": 0.001, "latency_ms": 500,
        }
        record_cost(temp_db, "l2", "deepseek-v4-pro", "opencode",
                    cost_info, True, None, [])
        from sqlalchemy import text
        with temp_db.connect() as conn:
            row = conn.execute(text("SELECT * FROM requests")).fetchone()
            assert row is not None
            assert row[3] == "deepseek-v4-pro"  # model
            assert row[4] == "opencode"  # provider
            assert row[11] == 1  # success

    def test_records_failure(self, temp_db):
        cost_info = {"prompt_tokens": 0, "completion_tokens": 0,
                     "cache_hit_tokens": 0, "cache_miss_tokens": 0,
                     "cost": 0, "latency_ms": 0}
        record_cost(temp_db, "l2", "deepseek-v4-flash", "deepseek",
                    cost_info, False, "timeout", ["forbidden_tool"])
        from sqlalchemy import text
        with temp_db.connect() as conn:
            row = conn.execute(text("SELECT * FROM requests")).fetchone()
            assert row is not None
            assert row[11] == 0  # success
            assert "forbidden_tool" in str(row[14])  # tools_blocked (shifted by error_detail)

    def test_empty_tools_blocked(self, temp_db):
        cost_info = {"prompt_tokens": 10, "completion_tokens": 5,
                     "cache_hit_tokens": 0, "cache_miss_tokens": 10,
                     "cost": 0.0, "latency_ms": 100}
        record_cost(temp_db, "l1", "gpt-4", "openai", cost_info, True, None, [])
        from sqlalchemy import text
        with temp_db.connect() as conn:
            row = conn.execute(text("SELECT * FROM requests")).fetchone()
            assert row is not None
            assert row[13] is None


# ═══════════════════════════════════════════════════════════════════════
# forward_request tests
# ═══════════════════════════════════════════════════════════════════════

class TestForwardRequest:
    def test_forwards_with_auth(self, mock_config):
        from src.api.request_pipeline import forward_request
        provider_cfg = {
            "provider": "testco",
            "api_key_env": "TEST_KEY",
            "base_url": "https://api.example.com/v1",
        }
        body = {"messages": [{"role": "user", "content": "hi"}], "model": "test-model"}

        with _cred_patch(testco="sk-test123"),              patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = b'{"choices":[{"message":{"content":"hello"}}]}'
            mock_urlopen.return_value = mock_resp
            mock_config.get_provider_key.return_value = None

            result_body, status = forward_request(provider_cfg, body, mock_config)
            assert status == 200
            assert result_body["choices"][0]["message"]["content"] == "hello"

    def test_handles_urlerror(self, mock_config):
        from src.api.request_pipeline import ProviderTimeoutError, forward_request
        provider_cfg = {
            "provider": "badco",
            "api_key_env": "TEST_KEY",
            "base_url": "https://api.dead.com/v1",
        }
        body = {"messages": [{"role": "user", "content": "hi"}]}

        import urllib.error
        with _cred_patch(),              patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("connection refused")
            mock_config.get_provider_key.return_value = None

            with pytest.raises(ProviderTimeoutError):
                forward_request(provider_cfg, body, mock_config)

    def test_handles_http_429(self, mock_config):
        from src.api.request_pipeline import ProviderRateLimitError, forward_request
        provider_cfg = {
            "provider": "ratelimited",
            "api_key_env": "TEST_KEY",
            "base_url": "https://api.example.com/v1",
        }
        body = {"messages": [{"role": "user", "content": "hi"}]}

        import urllib.error
        with _cred_patch(),              patch("urllib.request.urlopen") as mock_urlopen:
            # HTTPError mock must return bytes from read()
            err = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)
            err.read = MagicMock(return_value=b'{"error":"rate limited"}')
            mock_urlopen.side_effect = err
            mock_config.get_provider_key.return_value = None

            with pytest.raises(ProviderRateLimitError):
                forward_request(provider_cfg, body, mock_config)


# ═══════════════════════════════════════════════════════════════════════
# try_chain tests
# ═══════════════════════════════════════════════════════════════════════

class TestTryChain:
    def test_no_providers_in_chain(self, mock_config):
        from src.api.request_pipeline import try_chain, AllProvidersFailedError
        profile_cfg = {"chain": [], "forbidden_tools": []}
        body = {"messages": [{"role": "user", "content": "hi"}]}
        mock_config.providers = {}
        mock_config.profiles = {"l2": profile_cfg}
        with pytest.raises(AllProvidersFailedError):
            try_chain("l2", profile_cfg, body, mock_config)

    def test_all_providers_fail_in_chain(self, mock_config):
        """When all providers fail, AllProvidersFailedError is raised."""
        from src.api.request_pipeline import try_chain, AllProvidersFailedError
        profile_cfg = {
            "chain": [
                {"provider": "bad1", "base_url": "https://bad1.com/v1", "model": "m1"},
                {"provider": "bad2", "base_url": "https://bad2.com/v1", "model": "m2"},
            ],
            "forbidden_tools": [],
        }
        body = {"messages": [{"role": "user", "content": "hi"}]}
        mock_config.profiles = {"l2": profile_cfg}
        mock_config.providers = {
            "bad1": {"api_key_env": "KEY", "base_url": "https://bad1.com/v1"},
            "bad2": {"api_key_env": "KEY", "base_url": "https://bad2.com/v1"},
        }
        mock_config.get_provider_key.return_value = None
        import urllib.error
        with _cred_patch():
            def urlopen_side(req, timeout=120):
                raise urllib.error.URLError("dead")
            with patch("urllib.request.urlopen", side_effect=urlopen_side):
                with pytest.raises(AllProvidersFailedError):
                    try_chain("l2", profile_cfg, body, mock_config)

    def test_chain_with_fallback(self, mock_config):
        """Chain falls through when first provider fails, succeeds on second."""
        from src.api.request_pipeline import try_chain

        # Each chain step must have provider+base_url+model inline
        profile_cfg = {
            "chain": [
                {"provider": "badco", "base_url": "https://bad.com/v1", "model": "bad-model"},
                {"provider": "goodco", "base_url": "https://good.com/v1", "model": "good-model"},
            ],
            "forbidden_tools": [],
        }
        body = {"messages": [{"role": "user", "content": "hi"}]}
        mock_config.profiles = {"l2": profile_cfg}
        mock_config.providers = {
            "badco": {"api_key_env": "KEY", "base_url": "https://bad.com/v1"},
            "goodco": {"api_key_env": "KEY", "base_url": "https://good.com/v1"},
        }
        mock_config.get_provider_key.return_value = None

        import urllib.error
        with _cred_patch():
            # badco fails with URLError, goodco succeeds
            def urlopen_side(req, timeout=120):
                url = req.full_url if isinstance(req.full_url, str) else req.full_url.decode()
                if "bad.com" in url:
                    raise urllib.error.URLError("dead")
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_resp.read.return_value = b'{"choices":[{"message":{"content":"ok"}}]}'
                return mock_resp

            with patch("urllib.request.urlopen", side_effect=urlopen_side):
                result_body, status, provider, model = try_chain(
                    "l2", profile_cfg, body, mock_config)
                assert status == 200
                assert provider == "goodco"
                assert model == "good-model"

    def test_image_content_rejected_for_non_vision_model(self, mock_config):
        """An image request with no vision-capable model is refused by the gate.

        CONTRACT CHANGE (preflight gate, PLAN §8 / R11, CUR-103): this used to
        walk the chain, skip every provider and end as AllProvidersFailedError
        (an opaque 502). The gate's G0 answers "can ANY model in this chain take
        an image?" before any provider is contacted, so the caller now gets a
        400 that names the real problem — and nothing is billed.
        """
        from src.api.exceptions import PreflightRefusalError
        from src.api.request_pipeline import try_chain
        mock_config.model_limits = {
            "blind-model": {"context_window": 100000, "supports_vision": False},
        }

        profile_cfg = {
            "chain": [
                {"provider": "testco", "base_url": "https://api.example.com/v1", "model": "blind-model"},
            ],
            "forbidden_tools": [],
        }
        body = {
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "Describe this image"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ]},
            ]
        }
        mock_config.profiles = {"l2": profile_cfg}
        mock_config.providers = {
            "testco": {"api_key_env": "KEY", "base_url": "https://api.example.com/v1"},
        }
        mock_config.get_provider_key.return_value = None

        with _cred_patch():
            with pytest.raises(PreflightRefusalError) as exc:
                try_chain("l2", profile_cfg, body, mock_config)
            assert exc.value.gate == "G0"
            assert "vision" in str(exc.value).lower()

    def test_image_content_allowed_for_vision_model(self, mock_config):
        """Chain proceeds when model supports vision and body has images."""
        from src.api.request_pipeline import try_chain
        mock_config.model_limits = {
            "vision-model": {"context_window": 100000, "supports_vision": True},
        }

        profile_cfg = {
            "chain": [
                {"provider": "testco", "base_url": "https://api.example.com/v1", "model": "vision-model"},
            ],
            "forbidden_tools": [],
        }
        body = {
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "Describe this image"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ]},
            ]
        }
        mock_config.profiles = {"l2": profile_cfg}
        mock_config.providers = {
            "testco": {"api_key_env": "KEY", "base_url": "https://api.example.com/v1"},
        }
        mock_config.get_provider_key.return_value = None

        with _cred_patch():
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_resp.read.return_value = b'{"choices":[{"message":{"content":"an image"}}]}'
                mock_urlopen.return_value = mock_resp

                result_body, status, provider, model = try_chain(
                    "l2", profile_cfg, body, mock_config)
                assert status == 200
                assert model == "vision-model"
