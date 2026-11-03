"""Configurable alert floors for provider credits and allowances.

The provider watch (the L2 profile's `lcp-alert-watch.py` cron) reads these over the
admin API, so the numbers live with the deployment they protect instead of inside
whichever script happens to check them. One vocabulary is shared by this module, the
Alerts page and the watch:

    window -> direction, default floors, unit, label

``direction`` says which side of a floor is bad, and it is a property of the window,
never of the edit: a *balance* is bad when it is **below** warn, a *usage percentage*
is bad when it is **above** it. Editing a floor therefore cannot invert its meaning —
:func:`validate` enforces the ordering the direction implies (``critical <= warn`` for
``below``, ``critical >= warn`` for ``above``) and rejects anything else naming the
window, rather than accepting a floor that can never fire.

Stored shape (under the ``alert_thresholds`` settings key, so it survives a restart
and needs no schema change)::

    {"deepseek": {"balance": {"warn": 3.0, "critical": 1.0, "enabled": true}},
     "commandcode": {"monthly": {"warn": 85.0, "critical": 95.0}}}

Only overrides are stored; :func:`effective` merges them over :data:`WINDOWS` defaults,
so a provider added later inherits sane floors without an edit and removing an override
restores the default.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

# ── the window vocabulary ────────────────────────────────────────────────────
# The keys are the field names the cost plugins emit (`balance`, and `<window>_pct` /
# `<window>_used`+`<window>_cap` triplets), which is what makes a new provider wireable
# by shipping a plugin rather than by editing a check.
WINDOWS: dict[str, dict[str, Any]] = {
    "balance": {
        "direction": "below", "warn": 2.0, "critical": 0.5, "unit": "USD",
        "label": "account balance",
        "hint": "spend stops when this hits zero",
    },
    "available_credits": {
        "direction": "below", "warn": 2.0, "critical": 0.5, "unit": "USD",
        "label": "available credits",
        "hint": "prepaid credits left on the account",
    },
    "daily": {
        "direction": "above", "warn": 80.0, "critical": 90.0, "unit": "%",
        "label": "daily allowance used",
        "hint": "share of the vendor's daily allowance already spent",
    },
    "five_hour": {
        "direction": "above", "warn": 80.0, "critical": 90.0, "unit": "%",
        "label": "5-hour rolling allowance used",
        "hint": "short rolling window; the first thing a burst exhausts",
    },
    "weekly": {
        "direction": "above", "warn": 80.0, "critical": 90.0, "unit": "%",
        "label": "weekly allowance used",
        "hint": "share of the vendor's weekly allowance already spent",
    },
    "monthly": {
        "direction": "above", "warn": 80.0, "critical": 90.0, "unit": "%",
        "label": "monthly allowance used",
        "hint": "share of the vendor's monthly allowance already spent",
    },
}

# Which windows each provider is watched on. A provider absent from this map is wired by
# the watch from whatever its own cost plugin publishes, so adding a provider cannot
# silently leave it unwatched.
PROVIDER_WINDOWS: dict[str, list[str]] = {
    # Balance only: the account is prepaid, so the balance IS the limit.
    "deepseek": ["balance"],
    # Its plugin publishes all three allowance windows (5-hour rolling included).
    "commandcode": ["five_hour", "weekly", "monthly"],
    # The Go plan publishes daily/weekly/monthly (and a rolling window) — readable only
    # by a key from a Go-entitled account; the pay-as-you-go console exposes no ceiling.
    "opencode": ["daily", "five_hour", "weekly", "monthly", "balance"],
}

SETTINGS_KEY = "alert_thresholds"

# A reading the plugin has not refreshed within this window is not a reading: it cannot
# tell a topped-up account from an empty one. Shared with the L2 provider watch so the
# panel and the alert agree on what counts as current.
STALE_AFTER_MIN = float(os.environ.get("CREDIT_MAX_AGE_MIN", "120")) if os else 120.0

DEFAULTS_NOTE = (
    "Floors are per provider and per window. A window the provider's own cost plugin "
    "does not publish is shown as unavailable: no floor can be typed for a number the "
    "vendor never reports."
)


class ThresholdError(ValueError):
    """Raised with a message meant for the operator, naming the offending window."""


def _defaults_for(provider: str) -> dict[str, dict[str, Any]]:
    return {w: {"warn": WINDOWS[w]["warn"], "critical": WINDOWS[w]["critical"],
                "enabled": True}
            for w in PROVIDER_WINDOWS.get(provider, [])}


def defaults() -> dict[str, dict[str, dict[str, Any]]]:
    """Every provider's default floors, keyed by provider then window."""
    return {p: _defaults_for(p) for p in PROVIDER_WINDOWS}


def _decode(raw: Any) -> dict:
    """The stored value as a dict.

    ``SettingsStore.set`` writes ``str(value)``, so a dict round-trips as its repr and a
    JSON string round-trips as JSON — this accepts either (and a dict, for a store that
    keeps objects) rather than assuming, because guessing wrong here silently means
    "no floors configured" and every alert falls back to the defaults.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def load(settings: Any) -> dict:
    """Stored overrides only (empty when nothing has been customised)."""
    if settings is None:
        return {}
    try:
        stored = _decode(settings.get(SETTINGS_KEY, None))
    except Exception:  # noqa: BLE001 — a broken store must not break the page
        return {}
    return {str(p): dict(w) for p, w in stored.items() if isinstance(w, dict)}


def effective(settings: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """Defaults merged with stored overrides — what the watch should apply."""
    merged = defaults()
    for provider, windows in load(settings).items():
        target = merged.setdefault(provider, {})
        for window, floors in windows.items():
            if not isinstance(floors, dict):
                continue
            row = dict(WINDOWS.get(window, {}))
            row.setdefault("direction", "above")
            row.update(target.get(window, {}))
            row.update({k: v for k, v in floors.items() if v is not None})
            target[window] = row
    return merged


def validate(payload: Any) -> dict:
    """Normalise and check an incoming thresholds payload.

    Accepts ``{"<provider>": {"<window>": {"warn": n, "critical": n, "enabled": bool}}}``
    and returns the normalised form. Raises :class:`ThresholdError` with an
    operator-readable reason for an unknown window, a non-numeric floor, or a pair whose
    ordering contradicts the window's direction.
    """
    if not isinstance(payload, dict):
        raise ThresholdError("body must be an object keyed by provider")
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for provider, windows in payload.items():
        name = str(provider).strip()
        if not name:
            raise ThresholdError("a provider key is empty")
        if not isinstance(windows, dict):
            raise ThresholdError(f"{name}: expected an object of windows")
        for window, floors in windows.items():
            win = str(window).strip()
            if win not in WINDOWS:
                raise ThresholdError(
                    f"{name}: unknown window {win!r} (known: {', '.join(sorted(WINDOWS))})")
            if floors is None:
                continue
            if not isinstance(floors, dict):
                raise ThresholdError(f"{name}/{win}: expected an object of floors")
            row: dict[str, Any] = {}
            for side in ("warn", "critical"):
                if side not in floors or floors[side] is None:
                    continue
                raw = floors[side]
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    try:
                        raw = float(raw)
                    except (TypeError, ValueError):
                        raise ThresholdError(
                            f"{name}/{win}: {side} must be a number, got {raw!r}") from None
                value = float(raw)
                if value < 0:
                    raise ThresholdError(f"{name}/{win}: {side} cannot be negative")
                row[side] = value
            if "enabled" in floors:
                row["enabled"] = bool(floors["enabled"])
            if "warn" in row and "critical" in row:
                direction = WINDOWS[win]["direction"]
                if direction == "below" and row["critical"] > row["warn"]:
                    raise ThresholdError(
                        f"{name}/{win}: critical ({row['critical']:g}) must be at or below "
                        f"warn ({row['warn']:g}) — this window is bad when LOW")
                if direction == "above" and row["critical"] < row["warn"]:
                    raise ThresholdError(
                        f"{name}/{win}: critical ({row['critical']:g}) must be at or above "
                        f"warn ({row['warn']:g}) — this window is bad when HIGH")
            if row:
                out.setdefault(name, {})[win] = row
    return out


def persist(settings: Any, normalised: dict) -> dict:
    """Write already-validated thresholds, JSON-encoded (see :func:`_decode`)."""
    if settings is None:
        raise ThresholdError("no settings store is available to persist into")
    settings.set(SETTINGS_KEY, json.dumps(normalised))
    return normalised


def save(settings: Any, payload: Any) -> dict:
    """Validate then persist; returns the normalised thresholds written."""
    return persist(settings, validate(payload))


def clear(settings: Any) -> None:
    """Drop every override, restoring the built-in defaults."""
    if settings is not None:
        settings.set(SETTINGS_KEY, json.dumps({}))


def windows_for(provider: str, balances: dict, subs: dict) -> list[str]:
    """Windows to watch for a provider: its declared list, else what its plugin emits.

    Mirrors the watch script's rule so the Alerts page and the watch agree on what is
    being checked. A provider with neither lands here with an empty list, which the
    caller reports as "no cost plugin" rather than as a pass.
    """
    declared = PROVIDER_WINDOWS.get(provider)
    if declared is not None:
        return list(declared)
    payloads = (balances.get(provider) or {}, subs.get(provider) or {})
    return [w for w in WINDOWS
            if w in payloads[0] or any(k.startswith(w + "_") for k in payloads[1])]


def published_windows(provider: str, balances: dict, subs: dict) -> set[str]:
    """Which of the provider's windows its plugin actually reports right now."""
    bal = balances.get(provider) or {}
    sub = subs.get(provider) or {}
    found: set[str] = set()
    for window in PROVIDER_WINDOWS.get(provider, list(WINDOWS)):
        if window in ("balance", "available_credits"):
            if window in bal:
                found.add(window)
        elif any(k.startswith(window + "_") for k in sub) or any(
                k.startswith(window + "_") for k in bal):
            found.add(window)
    return found


def staleness(payload: dict) -> tuple[float | None, bool]:
    """``(age_minutes, is_stale)`` for one plugin payload.

    The plugin's own ``fetched_at`` is the only honest clock here: a payload that was
    read six days ago and says ``0.00`` must not be presented as a current zero. An
    absent timestamp counts as stale — not knowing when a number was read is not the
    same as knowing it is fresh.
    """
    raw = (payload or {}).get("fetched_at")
    if not raw:
        return None, True
    try:
        text = str(raw).replace("Z", "+00:00")
        when = datetime.fromisoformat(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None, True
    age = (datetime.now(timezone.utc) - when).total_seconds() / 60.0
    return age, age > STALE_AFTER_MIN or bool((payload or {}).get("_stale"))


def reported_value(provider: str, window: str, balances: dict, subs: dict):
    """The vendor's own reported figure for a window, or ``None``.

    Only ever the plugin's published field — the display does not re-derive a
    percentage, so the page can never show a number the watch would disagree with.
    """
    if window in ("balance", "available_credits"):
        return (balances.get(provider) or {}).get(window), WINDOWS[window]["unit"]
    row = subs.get(provider) or {}
    for key, unit in ((f"{window}_pct", "%"), (f"{window}_used", WINDOWS[window]["unit"])):
        value = row.get(key)
        if isinstance(value, (int, float)):
            return float(value), unit
    return None, WINDOWS[window]["unit"]
