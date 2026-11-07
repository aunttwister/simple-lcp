"""Tests for src.ui.dashboard.render_dashboard().

Covers the dashboard query/aggregation paths that only execute with seeded
Request rows (formatters, profile cards, badges, budget cards, monthly
aggregation, latest-provider query) plus the empty-DB and exception paths.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.api.models import Request as RequestModel, Budget, get_session


@pytest.fixture
def dash_config():
    cfg = MagicMock()
    cfg.profiles = {
        "l2": {"chain": [{"provider": "opencode", "model": "deepseek-v4-pro"}]},
        "l1": {"chain": [{"provider": "deepseek", "model": "deepseek-v4-flash"}]},
        # 3-provider chain -> sidebar "+N" suffix path
        "coder": {"chain": [
            {"provider": "opencode", "model": "deepseek-v4-pro"},
            {"provider": "deepseek", "model": "deepseek-v4-flash"},
            {"provider": "llamacpp", "model": "local-model"},
        ]},
    }
    cfg.providers = {
        "opencode": {"models": ["deepseek-v4-pro"]},
        "deepseek": {"models": ["deepseek-v4-flash"]},
        "llamacpp": {"models": ["local-model"]},
    }
    def _pricing_lookup(p_name, model):
        # Raise for flash model -> triggers the except path in _savings_for_model
        if model == "deepseek-v4-flash":
            raise KeyError("no pricing")
        return {"cache_hit": 0.01, "cache_miss": 0.5, "output": 1.0}

    cfg.get_pricing = MagicMock(side_effect=_pricing_lookup)
    # Circuit breaker needs config to initialize
    from src.api.circuit_breaker import get_circuit_breaker
    cfg.circuit_breaker = {
        "failures_dead": 5, "dead_cooldown_seconds": 300,
        "failures_degraded": 3, "degraded_cooldown_seconds": 60,
    }
    get_circuit_breaker(cfg)
    return cfg


@pytest.fixture
def dash_headers():
    return {"Host": "localhost:8734"}


def _seed_requests(engine):
    """Insert a mix of success/error/fallback request rows across profiles."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        # Success: big token counts (triggers _fmt_num K/M paths), provider = opencode
        RequestModel(
            timestamp=now, profile="l2", model="deepseek-v4-pro", provider="opencode",
            prompt_tokens=2_000_000, completion_tokens=1_500_000,
            cache_hit_tokens=1_000_000, cache_miss_tokens=1_000_000,
            cost=5.5, latency_ms=120, success=1,
        ),
        # Success on a different profile/provider
        RequestModel(
            timestamp=now, profile="l1", model="deepseek-v4-flash", provider="deepseek",
            prompt_tokens=1000, completion_tokens=500,
            cache_hit_tokens=500, cache_miss_tokens=500,
            cost=0.01, latency_ms=80, success=1,
        ),
        # Error row (provider='error' keeps it out of fallback count)
        RequestModel(
            timestamp=now, profile="l2", model="deepseek-v4-pro", provider="error",
            prompt_tokens=0, completion_tokens=0, cache_hit_tokens=0,
            cache_miss_tokens=0, cost=0, latency_ms=0, success=0, error_type="timeout",
        ),
        # Zero-cost success on l2
        RequestModel(
            timestamp=now, profile="l2", model="deepseek-v4-pro", provider="opencode",
            prompt_tokens=100, completion_tokens=100, cache_hit_tokens=0,
            cache_miss_tokens=100, cost=0.0, latency_ms=10, success=1,
        ),
        # Fallback: successful request whose provider != profile's first chain provider
        RequestModel(
            timestamp=now, profile="l2", model="deepseek-v4-flash", provider="deepseek",
            prompt_tokens=200, completion_tokens=50, cache_hit_tokens=0,
            cache_miss_tokens=200, cost=0.005, latency_ms=40, success=1,
        ),
        # Model not in any provider -> _savings_for_model returns 0.0 (no match)
        RequestModel(
            timestamp=now, profile="l2", model="gpt-4", provider="unknown",
            prompt_tokens=100, completion_tokens=100, cache_hit_tokens=100,
            cache_miss_tokens=100, cost=0.001, latency_ms=5, success=1,
        ),
        # Mid-size token counts -> _fmt_num K-branch (1K - 1M)
        RequestModel(
            timestamp=now, profile="l2", model="mid-model", provider="opencode",
            prompt_tokens=5000, completion_tokens=3000, cache_hit_tokens=5000,
            cache_miss_tokens=2000, cost=0.01, latency_ms=15, success=1,
        ),
    ]
    with get_session(engine) as session:
        session.add_all(rows)
        session.commit()


class TestRenderDashboard:
    def test_renders_with_seeded_data(self, temp_db, dash_config, dash_headers):
        _, engine = temp_db
        _seed_requests(engine)
        # Add budgets so budget cards render (one active, one exceeded)
        with get_session(engine) as session:
            session.add(Budget(
                name="L2 Cap", key_id=None, profile="l2",
                amount=100.0, current_spend=20.0, period="monthly",
                threshold_pct="50,80", action="log", status="active",
            ))
            session.add(Budget(
                name="Blown Cap", key_id=None, profile=None,
                amount=50.0, current_spend=60.0, period="monthly",
                threshold_pct="80", action="block", status="exceeded",
            ))
            session.commit()

        from src.ui.dashboard import render_dashboard
        html = render_dashboard(dash_config, engine, dash_headers)

        assert "<!DOCTYPE html>" in html
        # Summary totals (2 decimals, CUR-330 item 2)
        assert "$5.51" in html
        # Formatter paths: 2M prompt tokens -> "2.0M"
        assert "2.0M" in html
        # Real fallback counting (CUR-330 item 1): the l1 row (provider=deepseek)
        # is l1's primary so NOT a fallback; the l2 row on deepseek IS (l2's
        # primary is opencode); the 'unknown'-provider row is not a real route.
        assert "1 fallbacks" in html
        assert "99.9%" not in html
        # Budget cards rendered (active + exceeded)
        assert "L2 Cap" in html
        assert "Blown Cap" in html

    def test_render_with_profile_filter(self, temp_db, dash_config, dash_headers):
        _, engine = temp_db
        _seed_requests(engine)
        from src.ui.dashboard import render_dashboard
        html = render_dashboard(dash_config, engine, dash_headers, profile_filter="l2")
        assert "<!DOCTYPE html>" in html
        assert "L2" in html

    def test_render_empty_db(self, temp_db, dash_config, dash_headers):
        _, engine = temp_db
        from src.ui.dashboard import render_dashboard
        html = render_dashboard(dash_config, engine, dash_headers)
        assert "<!DOCTYPE html>" in html
        assert "$0.00" in html  # usage value renders at 2 decimals (CUR-330 item 2)
        # The global header widget renders (empty state is now client-side JS).
        assert 'id="pluginHeaderBadge"' in html
        assert 'header-status.js' in html

    def test_budget_query_failure_swallowed(self, temp_db, dash_config, dash_headers):
        """A failure in the budget-cards query is caught and skipped."""
        _, engine = temp_db
        _seed_requests(engine)
        from src.ui.dashboard import render_dashboard
        import inspect
        import src.api.models as models_mod
        real_get_session = models_mod.get_session

        def flaky(*args, **kwargs):
            # The budget block re-imports get_session from models and calls it
            # at line 521; only fail that call so the except path runs.
            for fr in inspect.stack():
                if fr.filename.endswith("dashboard.py") and fr.lineno == 521:
                    raise RuntimeError("budget db down")
            return real_get_session(*args, **kwargs)

        with patch("src.api.models.get_session", side_effect=flaky):
            html = render_dashboard(dash_config, engine, dash_headers)
        assert "<!DOCTYPE html>" in html

    def test_exception_falls_back_to_empty_summary(self, temp_db, dash_config, dash_headers):
        """A DB failure in the main query still returns a rendered page."""
        _, engine = temp_db
        from src.ui.dashboard import render_dashboard
        import src.api.models as models_mod
        real_get_session = models_mod.get_session
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db down")
            return real_get_session(*args, **kwargs)

        with patch("src.ui.dashboard.get_session", side_effect=flaky):
            html = render_dashboard(dash_config, engine, dash_headers)
        assert "<!DOCTYPE html>" in html
