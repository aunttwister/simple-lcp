"""CUR-330 — Overview homepage semantics.

The overview moved from Activity's overview tab to ``/``. What changed is not
just the URL: the numbers were rebuilt. This pins the parts that were wrong or
that could silently regress:

* the **period window** — every aggregate sits behind one timestamp cutoff, and
  the cutoff carries the 'T' separator the stored timestamps use (the
  space-separated datetime('now') cutoff matches nothing);
* the **requests card** — a success is not a fallback; a fallback is a success
  served by a provider that is not the profile's primary chain step;
* the **cost split** — metered / subscription / local dollars are reported
  separately, never as one mixed total;
* the **redirects** — old overview URLs land on ``/``, not a dead tab;
* the **detail payload** — the JSON the right-hand panel opens from is present
  and shaped for the four cards plus the day click-through.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.api.models import Request as RequestModel, get_session
from src.ui.dashboard import dashboard_context, _period_cutoff


@pytest.fixture
def config():
    cfg = MagicMock()
    # provider_type: the declared vendor family the split keys on.
    cfg.provider_type = MagicMock(
        side_effect=lambda name: {
            "deepseek": "deepseek", "commandcode": "commandcode",
            "opencode": "opencode", "local-zgx": "local",
        }.get(name, ""))
    cfg.profiles = {
        "l2": {"chain": [{"provider": "opencode", "model": "m"}]},
        "l1": {"chain": [{"provider": "deepseek", "model": "m"}]},
    }
    cfg.providers = {
        "deepseek": {"models": ["deepseek-chat"]},
        "opencode": {"models": ["m"]},
        "commandcode": {"models": ["m"]},
        "local-zgx": {"models": ["m"]},
    }
    cfg.get_pricing = MagicMock(side_effect=KeyError("no pricing"))
    return cfg


@pytest.fixture
def headers():
    return {"Host": "localhost:8734"}


def _row(**kw):
    defaults = dict(
        timestamp=datetime.now(timezone.utc).isoformat(),
        profile="l2", model="m", provider="opencode",
        prompt_tokens=0, completion_tokens=0, cache_hit_tokens=0,
        cache_miss_tokens=0, cost=0.0, latency_ms=10, success=1,
    )
    defaults.update(kw)
    return RequestModel(**defaults)


# ── the period cutoff ────────────────────────────────────────────────────────

def test_period_cutoff_carries_the_T_separator():
    cut = _period_cutoff("14")
    assert cut is not None and "T" in cut
    # cutoff starts at local midnight UTC, 13 days back inclusive
    expected_day = (datetime.now(timezone.utc).date() - timedelta(days=13)).isoformat()
    assert cut.startswith(expected_day)


def test_period_cutoff_all_is_none_and_unknown_falls_back():
    assert _period_cutoff("all") is None
    assert _period_cutoff("banana") is None


def test_all_rows_outside_the_window_are_excluded(config, headers, temp_db):
    _, engine = temp_db
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    with get_session(engine) as s:
        s.add_all([
            _row(cost=10.0, timestamp=old),
            _row(cost=1.0),
        ])
        s.commit()
    ctx14 = dashboard_context(config, engine, headers, period="14")
    assert ctx14["usage_value_fmt"] == "$1.00"
    ctxall = dashboard_context(config, engine, headers, period="all")
    assert ctxall["usage_value_fmt"] == "$11.00"


# ── the requests card: real fallbacks ────────────────────────────────────────

def test_fallback_counts_success_off_the_primary_only(config, headers, temp_db):
    _, engine = temp_db
    with get_session(engine) as s:
        s.add_all([
            # l2 primary = opencode: this is NOT a fallback
            _row(profile="l2", provider="opencode"),
            # l2 served by deepseek: IS a fallback
            _row(profile="l2", provider="deepseek"),
            # l1 primary = deepseek: NOT a fallback even though l2 fell back to it
            _row(profile="l1", provider="deepseek"),
            # an error row is an error, never a fallback
            _row(profile="l2", provider="error", success=0),
        ])
        s.commit()
    ctx = dashboard_context(config, engine, headers, period="14")
    assert ctx["fallback_count_fmt"] == "1"
    assert ctx["fb_pct_fmt"] == "33.3%"   # 1 of 3 ok requests
    assert ctx["error_count_fmt"] == "1"


def test_old_dashboard_bug_does_not_return(config, headers, temp_db):
    """Every success must never be reported as a fallback (the 99.9% bug)."""
    _, engine = temp_db
    with get_session(engine) as s:
        s.add_all([_row(profile="l2", provider="opencode") for _ in range(5)])
        s.commit()
    ctx = dashboard_context(config, engine, headers, period="14")
    assert ctx["fallback_count_fmt"] == "0"
    assert ctx["fb_pct_fmt"] == "0.0%"


# ── the cost split ───────────────────────────────────────────────────────────

def test_cost_splits_by_billing_family(config, headers, temp_db):
    _, engine = temp_db
    with get_session(engine) as s:
        s.add_all([
            _row(provider="deepseek", cost=3.0),      # metered
            _row(provider="commandcode", cost=2.0),   # subs (name fallback)
            _row(provider="opencode", cost=1.5),      # subs (declared type)
            _row(provider="local-zgx", cost=0.1),     # local
        ])
        s.commit()
    ctx = dashboard_context(config, engine, headers, period="14")
    assert ctx["metered_cost_fmt"] == "$3.00"
    assert ctx["subs_cost_fmt"] == "$3.50"
    assert ctx["local_cost_fmt"] == "$0.10"
    assert ctx["usage_value_fmt"] == "$6.60"


def test_error_rows_do_not_pollute_the_split(config, headers, temp_db):
    _, engine = temp_db
    with get_session(engine) as s:
        s.add(_row(provider="error", cost=0.0, success=0))
        s.add(_row(provider="unknown", cost=0.0, success=0))
        s.commit()
    ctx = dashboard_context(config, engine, headers, period="14")
    # non-route families land in neither metered, subs nor local
    assert ctx["metered_cost_fmt"] == "$0.00"
    assert ctx["subs_cost_fmt"] == "$0.00"
    assert ctx["local_cost_fmt"] == "$0.00"


# ── the detail payload ───────────────────────────────────────────────────────

def test_breakdown_payload_is_embedded_and_shaped(config, headers, temp_db):
    import json
    _, engine = temp_db
    with get_session(engine) as s:
        s.add(_row(provider="deepseek", cost=2.0, prompt_tokens=100,
                   completion_tokens=50, cache_hit_tokens=30,
                   cache_miss_tokens=70))
        s.commit()
    ctx = dashboard_context(config, engine, headers, period="14")
    b = json.loads(ctx["breakdown_json"])
    assert set(b) == {"period", "cost", "tokens", "requests", "cache", "days"}
    assert b["cost"]["totals"]["metered"] == 2.0
    assert b["requests"]["totals"]["ok"] == 1
    assert b["cache"]["totals"]["hit"] == 30
    assert b["cache"]["totals"]["miss"] == 70
    assert b["tokens"]["totals"]["completion"] == 50
    # day rows carry the provider/profile drill-down the trend click opens
    assert b["days"] and set(b["days"][0]) >= {
        "date", "cost", "completion", "providers", "profiles"}


# ── the redirects ────────────────────────────────────────────────────────────

def test_activity_overview_urls_redirect_home(temp_db, config):
    _, engine = temp_db
    from src.server.endpoints import DashboardEndpoints

    for path, want in (
        ("/activity?tab=overview", "/"),
        ("/activity", "/"),                      # bare page → home too
        ("/activity?tab=overview&profile=l2", "/?profile=l2"),
    ):
        h = DashboardEndpoints.__new__(DashboardEndpoints)
        h.path = path
        sent, htmls = [], []

        h._redirect = lambda loc, code=302, sent=sent: sent.append(loc)
        h._qs = lambda p=path: dict(
            kv.split("=", 1) for kv in p.split("?", 1)[1].split("&")) \
            if "?" in p else {}
        h._send_html = lambda body, htmls=htmls: htmls.append(body)
        h.config, h.engine = config, engine
        h._host_headers = lambda: {"Host": "t:8734"}
        DashboardEndpoints._serve_activity_page(h)
        assert sent and sent[0] == want, (path, sent)


def test_legacy_dashboard_redirects_home():
    from src.server.endpoints import DashboardEndpoints
    h = DashboardEndpoints.__new__(DashboardEndpoints)
    h.path = "/dashboard"
    sent = []
    h._redirect = lambda loc, code=302, sent=sent: sent.append(loc)
    DashboardEndpoints._serve_dashboard(h)
    assert sent == ["/"]
    DashboardEndpoints._serve_dashboard(h, profile_filter="l2")
    assert sent[-1] == "/?profile=l2"
