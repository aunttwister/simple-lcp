"""Unit tests for the Jinja2 template renderer (src/ui/render.py)
and page-rendering functions (src/ui/pages.py).

Covers:
  - render_page() with valid templates
  - All 4 page renderers (providers, profiles, keys, usage)
  - Template context injection (config, monthly, providers, profiles, active_page)
  - _compute_monthly() edge cases
  - Template inheritance (base.html extends)
  - jinja2.ext.do extension
  - Graceful handling of None engine / missing attributes
"""

import json
import re
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_config():
    """A minimal config mock with providers, profiles, and server attributes."""
    cfg = MagicMock()
    cfg.providers = {
        "deepseek": {
            "api_key_env": "DEEPSEEK_API_KEY",
            "api_base": "https://api.deepseek.com/v1",
            "models": ["deepseek-chat", "deepseek-reasoner"],
        },
        "opencode": {
            "api_key_env": "OPENCODE_API_KEY",
            "api_base": "https://opencode.ai/zen/go/v1",
            "models": ["deepseek-v4-pro"],
        },
    }
    cfg.profiles = {
        "l2": {
            "forbidden_tools": ["write_file"],
            "chain": [
                {"provider": "opencode", "model": "deepseek-v4-pro"},
                {"provider": "deepseek", "model": "deepseek-chat"},
            ],
        },
        "l1": {
            "forbidden_tools": ["terminal"],
            "chain": [
                {"provider": "deepseek", "model": "deepseek-reasoner"},
            ],
        },
    }
    cfg.server = {"port": 8734, "default_profile": "l2"}
    cfg.database = {"path": "/tmp/test.db"}
    cfg._data = {}
    return cfg


@pytest.fixture
def mock_config_minimal():
    """Config with no providers/profiles — edge case."""
    cfg = MagicMock()
    cfg.providers = {}
    cfg.profiles = {}
    cfg.server = {"port": 8734}
    cfg.database = {"path": "/tmp/test.db"}
    cfg._data = {}
    return cfg


@pytest.fixture
def render_env():
    """Return the Jinja2 Environment used by render.py."""
    from src.ui.render import _env
    return _env


# ---------------------------------------------------------------------------
# render_page — core function
# ---------------------------------------------------------------------------


class TestRenderPage:
    """Tests for render_page(template_name, config, engine, **kwargs)."""

    def test_returns_string(self, mock_config):
        from src.ui.render import render_page
        html = render_page("pages/providers.html", mock_config)
        assert isinstance(html, str)
        assert len(html) > 0

    def test_renders_valid_html_doctype(self, mock_config):
        from src.ui.render import render_page
        html = render_page("pages/providers.html", mock_config)
        assert html.strip().startswith("<!DOCTYPE html>")

    def test_injects_config_providers_into_template(self, mock_config):
        from src.ui.render import render_page
        html = render_page("pages/providers.html", mock_config)
        # providers.html iterates over providers dict
        assert "deepseek" in html
        assert "opencode" in html
        assert "https://api.deepseek.com/v1" in html

    def test_injects_profiles_into_sidebar(self, mock_config):
        from src.ui.render import render_page
        html = render_page("pages/profiles.html", mock_config)
        # Profiles moved from sidebar to dashboard filter pills
        # Sidebar should NOT contain profile dashboard links anymore
        assert 'href="/l2/dashboard"' not in html
        # But profiles are still injected via the 'profiles' context var
        # (used by other templates like dashboard filter pills)

    def test_active_page_injected(self, mock_config):
        from src.ui.render import render_page
        html = render_page("pages/keys.html", mock_config, active_page="profiles")
        # M2c: keys are per profile, so the legacy keys page aliases onto the
        # profile directory — the Profiles nav entry is active and its tab strip
        # offers the directory and Config.
        assert 'class="active"' in html
        assert 'href="/profiles?tab=config"' in html

    def test_active_page_defaults_empty(self, mock_config):
        from src.ui.render import render_page
        # No active_page kwarg → sidebar won't have active class
        html = render_page("pages/usage.html", mock_config)
        # Still renders fine, just no class="active"
        assert "<!DOCTYPE html>" in html

    def test_engine_none_ok(self, mock_config):
        from src.ui.render import render_page
        html = render_page("pages/providers.html", mock_config, engine=None)
        assert "<!DOCTYPE html>" in html

    def test_monthly_json_is_valid_json(self, mock_config):
        from src.ui.render import render_page
        html = render_page("pages/providers.html", mock_config)
        # Extract the `var monthly = {...}` line from the output
        match = re.search(r"var monthly = ({.*?});", html, re.DOTALL)
        assert match is not None
        parsed = json.loads(match.group(1))
        assert isinstance(parsed, dict)

    def test_configured_providers_json_is_valid_json(self, mock_config):
        from src.ui.render import render_page
        html = render_page("pages/providers.html", mock_config)
        match = re.search(r"var configuredProviders = (\[.*?\]);", html, re.DOTALL)
        assert match is not None
        parsed = json.loads(match.group(1))
        assert isinstance(parsed, list)
        assert sorted(parsed) == ["deepseek", "opencode"]


# ---------------------------------------------------------------------------
# Page renderers (from pages.py)
# ---------------------------------------------------------------------------


class TestPageRenderers:
    """Test each render_*_page function delegates to render_page correctly."""

    def test_render_providers_page(self, mock_config):
        from src.ui.pages import render_providers_page
        html = render_providers_page(mock_config)
        assert "<!DOCTYPE html>" in html
        assert "Providers — LCP" in html
        assert "deepseek" in html

    def test_render_profiles_page(self, mock_config):
        from src.ui.pages import render_profiles_page
        html = render_profiles_page(mock_config)
        assert "<!DOCTYPE html>" in html
        assert "Profiles — LCP" in html

    def test_render_profiles_page_with_budget(self, mock_config, temp_db):
        """Profiles page renders profile-budget spend/limit when an engine is bound."""
        from src.ui.pages import render_profiles_page
        from src.api.models import Budget, get_session
        db_path, engine = temp_db
        with get_session(engine) as session:
            session.add(Budget(
                name="L2 Cap", key_id=None, profile="l2",
                amount=200.0, current_spend=50.0, period="monthly",
                threshold_pct="50,80", action="block", status="active",
            ))
            session.commit()
        html = render_profiles_page(mock_config, engine=engine)
        assert "<!DOCTYPE html>" in html
        # Budget column shows $spend/$limit for the l2 profile
        assert "$50.00/$200" in html
        assert "25.0%" in html

    def test_render_profiles_page_budget_query_error_swallowed(self, mock_config, temp_db):
        """A failure querying profile budgets falls back to an empty dict."""
        from src.ui.pages import render_profiles_page
        _db_path, engine = temp_db
        with patch("src.api.models.get_session", side_effect=RuntimeError("db down")):
            html = render_profiles_page(mock_config, engine=engine)
        assert "<!DOCTYPE html>" in html

    def test_render_keys_page(self, mock_config):
        from src.ui.pages import render_keys_page
        html = render_keys_page(mock_config, engine=None)
        assert "<!DOCTYPE html>" in html
        # M2c: the legacy keys page lands on the profile directory, because keys
        # are reviewed per profile now.
        assert "Profiles — LCP" in html

    def test_render_usage_page(self, mock_config):
        from src.ui.pages import render_usage_page
        html = render_usage_page(mock_config)
        assert "<!DOCTYPE html>" in html
        assert "Usage — LCP" in html
        # Usage page loads Chart.js
        assert "chart.js" in html.lower()
        # Command Code credentials card (cookie input) renders in the Usage tab
        assert "ccCookieInput" in html
        assert "saveCcCookie" in html
        assert "toggleCcCreds" in html
        assert "commandcode" in html.lower()
        # Cookie help note (full name=value) + bare-value validation in both cards
        assert "name=value" in html
        assert "bare token value" in html
        assert "__Secure-commandcode_prod_.session_token" in html
        # Cache management (moved from Providers → Cache tab)
        assert 'id="cacheRows"' in html
        assert 'id="ttlRows"' in html
        assert "Cost data refresh" in html

    def test_all_four_pages_return_different_content(self, mock_config):
        from src.ui.pages import (
            render_providers_page,
            render_profiles_page,
            render_keys_page,
            render_usage_page,
        )
        results = {
            "providers": render_providers_page(mock_config),
            "profiles": render_profiles_page(mock_config),
            "keys": render_keys_page(mock_config, engine=None),
            "usage": render_usage_page(mock_config),
        }
        # All should be non-empty and distinct (different titles)
        for name, html in results.items():
            assert len(html) > 500, f"{name} page too small: {len(html)} bytes"
        # Each page has a unique title
        titles = set()
        for html in results.values():
            m = re.search(r"<title>(.*?)</title>", html)
            assert m, "Missing <title> in page"
            titles.add(m.group(1))
        # M2c folded the keys page into the profile directory, so profiles and
        # keys share a title: four pages, three distinct titles.
        assert len(titles) == 3, f"Expected 3 unique titles, got {len(titles)}"


# ---------------------------------------------------------------------------
# Template loading & inheritance
# ---------------------------------------------------------------------------


class TestTemplateLoading:
    """Verify all templates compile and extend correctly."""

    def test_all_page_templates_load(self, render_env):
        """Every template in pages/ should compile without error."""
        from pathlib import Path
        pages_dir = Path(__file__).parent.parent / "src" / "ui" / "templates" / "jinja" / "pages"
        for f in sorted(pages_dir.glob("*.html")):
            # get_template will compile and validate
            tmpl = render_env.get_template(f"pages/{f.name}")
            assert tmpl is not None, f"Failed to load {f.name}"

    def test_base_template_loads(self, render_env):
        tmpl = render_env.get_template("base.html")
        assert tmpl is not None

    def test_sidebar_partial_loads(self, render_env):
        tmpl = render_env.get_template("_sidebar.html")
        assert tmpl is not None

    def test_do_extension_enabled(self, render_env):
        """jinja2.ext.do must be enabled for profiles.html {% do %} tag."""
        assert hasattr(render_env, "extensions")
        # The 'do' extension is registered as ExprStmtExtension
        ext_keys = [str(k).lower() for k in render_env.extensions]
        assert any("exprstmt" in k or "do" in k for k in ext_keys), \
            f"do extension not found in: {list(render_env.extensions.keys())}"

    def test_autoescape_enabled(self, render_env):
        """XSS protection: autoescape must be on."""
        assert render_env.autoescape is True


# ---------------------------------------------------------------------------
# Edge cases & robustness
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Graceful handling of edge cases."""

    def test_empty_providers_and_profiles(self, mock_config_minimal):
        from src.ui.render import render_page
        html = render_page("pages/providers.html", mock_config_minimal)
        assert "<!DOCTYPE html>" in html
        # Should not crash — just no rows
        assert "No providers" in html or "providersBody" in html

    def test_config_without_providers_attribute(self):
        """Config object that lacks .providers entirely."""
        cfg = MagicMock(spec=[])  # No attributes at all
        from src.ui.render import render_page
        html = render_page("pages/providers.html", cfg)
        assert "<!DOCTYPE html>" in html

    def test_config_without_profiles_attribute(self):
        """Config object that lacks .profiles entirely."""
        cfg = MagicMock()
        cfg.providers = {"test": {"api_base": "http://x", "models": []}}
        # No .profiles attribute
        from src.ui.render import render_page
        with patch.object(type(cfg), "profiles", create=True, side_effect=AttributeError):
            html = render_page("pages/providers.html", cfg)
            assert "<!DOCTYPE html>" in html

    def test_config_is_none(self):
        """render_page should handle config=None gracefully."""
        from src.ui.render import render_page
        html = render_page("pages/providers.html", None)
        assert "<!DOCTYPE html>" in html

    def test_provider_names_sorted(self, mock_config):
        """Provider names in configured_providers_json should be sorted."""
        from src.ui.render import render_page
        # Add providers in non-alphabetical order
        mock_config.providers = {
            "zulu": {"api_base": "x", "models": []},
            "alpha": {"api_base": "x", "models": []},
            "mike": {"api_base": "x", "models": []},
        }
        html = render_page("pages/providers.html", mock_config)
        match = re.search(r"var configuredProviders = (\[.*?\]);", html, re.DOTALL)
        names = json.loads(match.group(1))
        assert names == sorted(names), f"Providers not sorted: {names}"

    def test_special_characters_in_provider_name(self, mock_config):
        """Provider/config names with special chars should be HTML-escaped."""
        mock_config.providers = {
            "test<script>": {"api_base": "http://x&y", "models": []},
        }
        from src.ui.render import render_page
        html = render_page("pages/providers.html", mock_config)
        # Jinja2 autoescape should escape < and &
        assert "<script>" not in html or "&lt;script&gt;" in html
        assert "&amp;" in html or "http://x&y" not in html


# ---------------------------------------------------------------------------
# _compute_monthly
# ---------------------------------------------------------------------------


class TestComputeMonthly:
    """Tests for _compute_monthly(engine)."""

    def test_returns_empty_dict_when_engine_is_none(self):
        from src.ui.render import _compute_monthly
        result = _compute_monthly(None)
        assert result == {}

    def test_returns_dict_with_expected_keys(self, temp_db):
        """Insert a request row and verify _compute_monthly picks it up."""
        from src.ui.render import _compute_monthly
        from src.api.models import get_session, Request

        _db_path, engine = temp_db

        # Insert a test request for this month
        from datetime import date
        today = date.today().isoformat()
        with get_session(engine) as s:
            s.add(Request(
                timestamp=today,
                provider="test_prov",
                model="test-model",
                profile="l2",
                prompt_tokens=100,
                completion_tokens=50,
                cost=0.015,
                success=1,
                latency_ms=200,
            ))
            s.commit()

        result = _compute_monthly(engine)
        assert "test_prov" in result
        assert result["test_prov"]["reqs"] == 1
        assert result["test_prov"]["tokens"] == 150
        assert result["test_prov"]["cost"] == 0.015

    def test_filters_out_unsuccessful_requests(self, temp_db):
        """Only success=1 rows should be counted."""
        from src.ui.render import _compute_monthly
        from src.api.models import get_session, Request
        from datetime import date

        _db_path, engine = temp_db
        today = date.today().isoformat()
        with get_session(engine) as s:
            s.add(Request(
                timestamp=today, provider="test_prov", model="m",
                profile="l2",
                prompt_tokens=100, completion_tokens=50,
                cost=1.0, success=0, latency_ms=200,
            ))
            s.add(Request(
                timestamp=today, provider="test_prov", model="m",
                profile="l2",
                prompt_tokens=10, completion_tokens=5,
                cost=0.1, success=1, latency_ms=100,
            ))
            s.commit()

        result = _compute_monthly(engine)
        assert result["test_prov"]["reqs"] == 1
        assert result["test_prov"]["cost"] == 0.1

    def test_filters_out_old_months(self, temp_db):
        """Requests from previous months should not appear."""
        from src.ui.render import _compute_monthly
        from src.api.models import get_session, Request
        from datetime import date, timedelta

        _db_path, engine = temp_db
        last_month = (date.today().replace(day=1) - timedelta(days=1)).isoformat()
        with get_session(engine) as s:
            s.add(Request(
                timestamp=last_month, provider="test_prov", model="m",
                profile="l2",
                prompt_tokens=1, completion_tokens=1,
                cost=999.0, success=1, latency_ms=1,
            ))
            s.commit()

        result = _compute_monthly(engine)
        assert "test_prov" not in result


# ---------------------------------------------------------------------------
# _fmt_num / _fmt_cost filters
# ---------------------------------------------------------------------------


class TestFormatFilters:
    """Direct tests for the Jinja2 format filters."""

    def test_fmt_num_none(self):
        from src.ui.render import _fmt_num
        assert _fmt_num(None) == "0"

    def test_fmt_num_millions(self):
        from src.ui.render import _fmt_num
        assert _fmt_num(1_500_000) == "1.5M"

    def test_fmt_num_thousands(self):
        from src.ui.render import _fmt_num
        assert _fmt_num(42_300) == "42.3K"

    def test_fmt_num_small(self):
        from src.ui.render import _fmt_num
        assert _fmt_num(7) == "7"

    def test_fmt_cost_none(self):
        from src.ui.render import _fmt_cost
        assert _fmt_cost(None) == "$0.000000"

    def test_fmt_cost_value(self):
        from src.ui.render import _fmt_cost
        assert _fmt_cost(1.5) == "$1.500000"


# ---------------------------------------------------------------------------
# _compute_monthly exception path
# ---------------------------------------------------------------------------


class TestComputeMonthlyExceptions:
    def test_db_error_returns_empty_dict(self, temp_db):
        from src.ui.render import _compute_monthly
        _db_path, engine = temp_db
        # _compute_monthly imports get_session from models at call time
        with patch("src.api.models.get_session", side_effect=RuntimeError("db down")):
            assert _compute_monthly(engine) == {}


# ---------------------------------------------------------------------------
# _sidebar.html partial — standalone rendering
# ---------------------------------------------------------------------------


class TestSidebarPartial:
    """Verify _sidebar.html renders correctly as a partial."""

    def test_active_page_highlight(self, render_env):
        tmpl = render_env.get_template("_sidebar.html")
        html = tmpl.render(
            config=MagicMock(profiles={"l2": {}, "l1": {}}),
            active_page="profiles",
            profiles=["l2", "l1"],
        )
        # Profiles owns the profile-scoped surface (M2), so it is the active entry
        assert 'href="/profiles" class="active"' in html
        # Others should not be
        assert 'href="/models" class="active"' not in html

    def test_profile_links_not_in_sidebar(self, render_env):
        """Profile links moved from sidebar to dashboard filter pills."""
        tmpl = render_env.get_template("_sidebar.html")
        html = tmpl.render(
            config=MagicMock(profiles={"l2": {}, "l1": {}, "coder": {}}),
            active_page="",
            profiles=["l2", "l1", "coder"],
        )
        # No profile links in sidebar anymore
        assert 'href="/l2/dashboard"' not in html
        assert "L2" not in html

    def test_uppercase_profile_names_not_in_sidebar(self, render_env):
        """Profile names no longer rendered in sidebar."""
        tmpl = render_env.get_template("_sidebar.html")
        html = tmpl.render(
            config=MagicMock(profiles={"myProfile": {}}),
            active_page="",
            profiles=["myProfile"],
        )
        assert "MYPROFILE" not in html


# ---------------------------------------------------------------------------
# pages/dashboard.html — Jinja2 dashboard template
# ---------------------------------------------------------------------------


def _dashboard_kwargs(**overrides):
    """Build the full context dict the dashboard template expects.

    Mirrors the kwargs that src.ui.dashboard.render_dashboard() passes to
    render_page("pages/dashboard.html", ...).
    """
    base = {
        "profile_filter": None,
        "filter_title": "",
        "now_utc": "2026-08-01 00:00:00 UTC",
        "version": "0.5.0",
        "host_url": "http://localhost:8734",
        "total_cost_fmt": "$1.234567",
        "cache_savings_fmt": "$0.1234",
        "cache_hit_rate_fmt": "50.0%",
        "fb_pct_fmt": "12.3%",
        "summary_total_requests_fmt": "1,234",
        "fallback_count_fmt": "152",
        "cache_hit_tokens_fmt": "1.2K",
        "cache_miss_tokens_fmt": "3.4K",
        "output_tokens_fmt": "5.6K",
        "prompt_tokens_fmt": "7.8K",
        "active_days": 15,
        "profile_cards": [
            {"profile": "l2", "count": 10, "total_cost": 0.5},
        ],
        "daily_rows": [
            {"date": "2026-07-20", "profile": "l2", "model": "deepseek-chat",
             "provider": "deepseek", "reqs": 3, "fb_count": 1,
             "cache_hit": "10", "cache_miss": "20", "output": "30",
             "cost": 0.001234, "saved": 0.0001},
        ],
        "recent_rows": [
            {"time": "10:00:00", "profile": "l2", "model": "deepseek-chat",
             "provider": "deepseek",
             "badges": '<span class="badge badge-success">ok</span>',
             "latency_s": 1.2, "cost": 0.001234, "saved": 0.0001},
        ],
        "error_rows": [
            {"time": "2026-07-21T11:00:00", "profile": "l1", "provider": "deepseek",
             "error_type": "timeout"},
        ],
        "sidebar_profiles": [
            {"name": "l2", "providers_str": "opencode, deepseek"},
            {"name": "l1", "providers_str": "deepseek"},
        ],
        "token_mismatches": 0,
        "routing_threshold": 4096,
        "cache_entries": 0,
        "cache_max_entries": 1000,
        "ts_dates_json": json.dumps(["2026-07-20"]),
        "ts_costs_json": json.dumps([0.001234]),
        "ts_lats_json": json.dumps([1234.0]),
        "pp_data_json": json.dumps({"dates": [], "profiles": {}}),
        "pm_data_json": json.dumps({"dates": [], "models": {}}),
        "monthly_json": json.dumps({"deepseek": {"reqs": 1, "tokens": 100, "cost": 0.01}}),
        "configured_providers_json": json.dumps(["deepseek", "opencode"]),
    }
    base.update(overrides)
    return base


class TestDashboardTemplate:
    """Unit tests for pages/dashboard.html (the migrated dashboard)."""

    def _render(self, mock_config, **overrides):
        from src.ui.render import render_page
        return render_page("pages/dashboard.html", mock_config, **_dashboard_kwargs(**overrides))

    def test_dashboard_renders_valid_html(self, mock_config):
        html = self._render(mock_config)
        assert isinstance(html, str)
        assert html.strip().startswith("<!DOCTYPE html>")
        assert "<title>Activity — LCP</title>" in html
        assert "Daily Cost Trend (14-day)" in html
        assert 'id="costChart"' in html
        assert 'id="provModal"' in html
        assert "Generated" in html

    def test_dashboard_summary_cards_render(self, mock_config):
        html = self._render(mock_config)
        assert "$1.234567" in html       # Total Cost
        assert "1,234" in html           # Total Requests
        assert "50.0%" in html           # Cache Hit Ratio
        assert "$0.1234" in html         # Cache Savings
        assert "5.6K" in html            # Output Tokens
        assert "15 active days" in html

    def test_dashboard_table_rows_render(self, mock_config):
        html = self._render(mock_config)
        # profile summary cards
        assert "L2" in html
        assert "$0.5000" in html         # l2 cost in profile mini card
        assert "10 reqs" in html
        # profile filter dropdown
        assert "lcp-filter-dropdown" in html
        assert "lcp-filter-menu" in html
        # Chart canvas still rendered
        assert 'id="costChart"' in html
        # metrics row
        assert "metric-card" in html

    def test_dashboard_profile_filter_active(self, mock_config):
        html = self._render(mock_config, profile_filter="l2", filter_title=" — L2")
        assert "<title>Activity (L2) — LCP</title>" in html
        # Filter dropdown shows active profile
        assert "L2" in html
        assert "lcp-filter-item active" in html
        # host URL injected into the page JS
        assert "http://localhost:8734" in html
