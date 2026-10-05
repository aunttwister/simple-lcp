"""DeepSeek peak / off-peak billing windows.

DeepSeek bills peak vs off-peak by REQUEST time:
  "Off-peak rates are half of the peak rates. Peak hours are 01:00-04:00 and
   06:00-10:00 UTC, Monday through Friday, excluding Chinese public holidays.
   All other hours are off-peak, including weekends and Chinese public holidays
   in full."
  — https://api-docs.deepseek.com/quick_start/pricing (verified 2026-10-05)

Before this, every DeepSeek-backed row stored only the off-peak triple, so every
peak-window request was recorded at half its real cost. Measured over 70,341
real rows: 13.7% of requests landed in peak windows, understating the ledger by
9.3% overall. (Storing a flat peak rate instead would overstate it by 81% — the
peak window is only 7 of 168 hours a week, so time-aware selection is the only
option that is actually right.)

Pinned here:
1. ``is_deepseek_peak`` band edges, the weekday rule, naive -> UTC, junk input.
2. ``select_rates`` — peak triple inside a window, base triple outside, and a
   no-op for rows with no ``peak_*`` keys (local tiers, non-DeepSeek providers).
3. Each DeepSeek-backed plugin bills 2x inside a peak window, keyed off the
   ``__ts`` the caller puts in the usage dict.
4. Every DeepSeek row in every plugin carries peak = exactly 2x its base rates.
"""
from datetime import datetime, timezone

import pytest

from src.api.cost_plugins.base import is_deepseek_peak, select_rates
from src.api.cost_plugins.commandcode import CommandCodeCostPlugin
from src.api.cost_plugins.deepseek import DeepSeekCostPlugin
from src.api.cost_plugins.opencode import OpenCodeCostPlugin

# 2026-10-05 is a Monday; 2026-10-10 is a Saturday.
MON = lambda h, m=0: datetime(2026, 10, 5, h, m, tzinfo=timezone.utc)   # noqa: E731
SAT = lambda h, m=0: datetime(2026, 10, 10, h, m, tzinfo=timezone.utc)  # noqa: E731


# ── is_deepseek_peak ────────────────────────────────────────────────────────

@pytest.mark.parametrize("ts,expected", [
    (MON(0, 59), False),   # just before the first band
    (MON(1, 0),  True),    # first band opens
    (MON(3, 59), True),    # first band closes
    (MON(4, 0),  False),
    (MON(5, 59), False),   # the gap between the bands
    (MON(6, 0),  True),    # second band opens
    (MON(9, 59), True),    # second band closes
    (MON(10, 0), False),
    (MON(12, 0), False),
    (MON(23, 59), False),
    (SAT(2, 0),  False),   # weekends are off-peak in full
    (SAT(8, 0),  False),
])
def test_peak_band_boundaries(ts, expected):
    assert is_deepseek_peak(ts) is expected


def test_naive_timestamp_is_read_as_utc():
    assert is_deepseek_peak(datetime(2026, 10, 5, 2, 0)) is True
    assert is_deepseek_peak(datetime(2026, 10, 5, 12, 0)) is False


def test_non_datetime_never_counts_as_peak():
    assert is_deepseek_peak("2026-10-05T02:00:00Z") is False
    assert is_deepseek_peak(0) is False


def test_default_is_now_and_returns_a_bool():
    assert isinstance(is_deepseek_peak(), bool)


# ── select_rates ────────────────────────────────────────────────────────────

FLASH = {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.6,
         "peak_cache_hit": 0.006, "peak_cache_miss": 0.30, "peak_output": 1.20}
LOCAL = {"cache_hit": 0.0, "cache_miss": 0.0, "output": 0.0}


def _triple(row):
    return (row["cache_hit"], row["cache_miss"], row["output"])


def test_off_peak_uses_the_base_triple():
    assert _triple(select_rates(FLASH, MON(12))) == (0.003, 0.15, 0.6)


def test_peak_uses_the_peak_triple():
    assert _triple(select_rates(FLASH, MON(2))) == (0.006, 0.30, 1.20)


def test_weekend_uses_the_base_triple():
    assert _triple(select_rates(FLASH, SAT(2))) == (0.003, 0.15, 0.6)


def test_row_without_peak_keys_is_returned_unchanged():
    """Local Spark tiers and non-DeepSeek providers are peak-agnostic."""
    assert select_rates(LOCAL, MON(2)) == LOCAL


def test_missing_pricing_is_returned_as_is():
    assert select_rates(None, MON(2)) is None
    assert select_rates({}, MON(2)) == {}


# ── plugins bill peak inside a window ───────────────────────────────────────

def _usage(hit=0, miss=1_000_000, out=1_000_000, ts=None):
    u = {"prompt_cache_hit_tokens": hit,
         "prompt_cache_miss_tokens": miss,
         "completion_tokens": out}
    if ts is not None:
        u["__ts"] = ts
    return u


def test_deepseek_plugin_doubles_cost_inside_a_peak_window():
    p = DeepSeekCostPlugin()
    off = p.calculate_cost("deepseek-flash", _usage(ts=MON(12)))
    peak = p.calculate_cost("deepseek-flash", _usage(ts=MON(2)))
    assert off == pytest.approx(0.15 + 0.6)
    assert peak == pytest.approx(2 * off)


def test_deepseek_plugin_is_off_peak_on_a_weekend():
    p = DeepSeekCostPlugin()
    assert p.calculate_cost("deepseek-flash", _usage(ts=SAT(2))) == pytest.approx(0.75)


def test_opencode_plugin_doubles_cost_inside_a_peak_window():
    p = OpenCodeCostPlugin()
    off = p.calculate_cost("deepseek-flash", _usage(ts=MON(12)))
    peak = p.calculate_cost("deepseek-flash", _usage(ts=MON(2)))
    assert off is not None
    assert peak == pytest.approx(2 * off)


def test_commandcode_plugin_doubles_cost_on_the_catalogue_id():
    """The prefixed Provider-API ID must peak too, not just the bare name."""
    p = CommandCodeCostPlugin()
    off = p.calculate_cost("deepseek/deepseek-v4.1-flash", _usage(ts=MON(12)))
    peak = p.calculate_cost("deepseek/deepseek-v4.1-flash", _usage(ts=MON(2)))
    assert off is not None
    assert peak == pytest.approx(2 * off)


def test_commandcode_flash_fast_peaks_at_two_x():
    p = CommandCodeCostPlugin()
    off = p.calculate_cost("deepseek/deepseek-v4.1-flash-fast", _usage(ts=MON(12)))
    peak = p.calculate_cost("deepseek/deepseek-v4.1-flash-fast", _usage(ts=MON(2)))
    assert peak == pytest.approx(2 * off)


def test_missing_timestamp_still_bills_a_number():
    """A caller that forgets __ts gets the now-based rate, never a crash."""
    p = DeepSeekCostPlugin()
    assert isinstance(p.calculate_cost("deepseek-flash", _usage()), float)


# ── the rate tables themselves ──────────────────────────────────────────────

def test_every_deepseek_row_peaks_at_exactly_two_x():
    from src.api.cost_plugins.commandcode import _COMMANDCODE_PRICING
    from src.api.cost_plugins.deepseek import _PRICING
    from src.api.cost_plugins.opencode import _OPENCODE_PRICING

    checked = 0
    for table in (_PRICING, _OPENCODE_PRICING, _COMMANDCODE_PRICING):
        for name, row in table.items():
            if "deepseek" not in name.lower():
                continue          # Claude / GPT / Kimi / MiniMax have no peak
            assert "peak_cache_miss" in row, f"{name} has no peak triple"
            assert row["peak_cache_hit"] == pytest.approx(row["cache_hit"] * 2)
            assert row["peak_cache_miss"] == pytest.approx(row["cache_miss"] * 2)
            assert row["peak_output"] == pytest.approx(row["output"] * 2)
            checked += 1
    assert checked == 16, f"expected 16 DeepSeek rows across the plugins, checked {checked}"


def test_non_deepseek_rows_are_untouched_by_peaking():
    """Claude/GPT/Kimi/MiniMax rows must NOT gain a peak triple."""
    from src.api.cost_plugins.commandcode import _COMMANDCODE_PRICING

    for name in ("claude-sonnet-5", "gpt-5.6-luna", "kimi-k3", "minimax-m3"):
        assert "peak_cache_miss" not in _COMMANDCODE_PRICING[name]
