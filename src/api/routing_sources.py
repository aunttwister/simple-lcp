"""Which benchmark sources are ON, and which routing tiers they make available.

Operator, 2026-11-21:

    "I say that we remove manage taxonomy as its unnecessary complicates the process. Just
     keep livebench and lmarena and have it configurable which one to turn on in global
     settings and then if both enabled have them chooseable in the profile"

So there are exactly **two** capability sources and **two** tiers that depend on them:

    tier      needs        what it does
    static    (nothing)    walk the chain in order; only explicit rules act
    intent    livebench    classify the request, reorder by the capability matrix
    arena     lmarena      classify to an LMArena board, walk that board's ranking

`static` needs no source because it consults neither: it is the chain order plus rules, and
it must always be available — it is the honest fallback when a source is switched off.

Why the rule lives in ONE module: the router, `/api/routing/status`, the profile page and the
Settings page all have to agree about whether a tier is usable. Three copies of "intent needs
LiveBench" is three chances to disagree, and the disagreement shows up as a profile that
silently keeps routing on a source the operator switched off.

────────────────────────────────────────────────────────────────────────────────────────
THREE STATES PER SOURCE, AND ONLY ONE OF THEM GATES
────────────────────────────────────────────────────────────────────────────────────────

    True    switched on       polled, and its tier is usable
    False   switched OFF      not polled, and its tier is DISABLED -> profiles fall back
    None    never configured  no switch exists for it -> NO gate

The third state is not decoration. An earlier version of this module treated "no entry" as
"off", so any config that had never had the switches touched — including the repo's own
default `config/gateway.yaml`, and every test that passes `config=None` — read as "every
source is off" and silently turned routing to `static`. 38 router tests failed, which was
the correct verdict: a setting nobody set must not change routing.

The rule that follows is the one worth stating plainly: **only an explicit OFF disables a
tier.** Never-configured is not off, and an unreadable switch (``None`` from ``read_states``)
gates nothing at all — we do not downgrade routing on the strength of a value we failed to
read. A silent downgrade is indistinguishable from a working system, which is the same
reason the arena path already refuses an empty label space instead of quietly falling back.

**Turning a source off is not a no-op, and the UI says so:** it stops that source being
polled AND disables its tier, so any profile pinned to it falls back to `static`. The pin
itself is not deleted — an operator who turns LiveBench back on gets the tiers back exactly
as they were, rather than having to re-pick.
"""
from __future__ import annotations

from typing import Optional

#: Source names, identical to the poll's (`model_intel_poll.SOURCE_*`) so one switch governs
#: both the polling and the tier. Two names for one thing is how a source ends up "off" for
#: polling while a profile still routes on it.
SOURCE_LIVEBENCH = "livebench"
SOURCE_LMARENA = "lmarena"

SOURCE_NAMES = (SOURCE_LIVEBENCH, SOURCE_LMARENA)

#: tier -> the source it cannot work without (None = needs no source).
TIER_REQUIRES: dict[str, Optional[str]] = {
    "static": None,
    "intent": SOURCE_LIVEBENCH,
    "arena": SOURCE_LMARENA,
}

#: What each tier is FOR, for the UI to show beside the choice.
TIER_LABEL: dict[str, str] = {
    "static": "pool order; hard rules only",
    "intent": "classify each request, reorder by the capability matrix",
    "arena": "classify to an LMArena board, walk its ranking",
}

#: The tier used when the chosen one has no source. `static` is the only honest answer: it
#: consults no source, so it cannot be wrong about data that was switched off.
FALLBACK_TIER = "static"

#: Per-source states, for readers that want the distinction spelled out.
ON, OFF, UNSET = True, False, None


def _raw_section(config=None, cfg_section: Optional[dict] = None) -> Optional[dict]:
    """The `model_intel` section **as stored** — the poller's DEFAULTS NOT merged in.

    This is the crux of the whole module. `model_intel_poll.load_config()` merges
    `DEFAULTS` (all sources `accepted: False`, deliberately, so a config failure can never
    start polling) underneath the stored section. Reading THAT through the gate makes
    "never configured" indistinguishable from "switched off": every estate whose switches
    had never been touched — including every test that passes `config=None` — would read as
    *intent disabled*, and routing would silently drop to `static`. 38 router tests failed
    on exactly that, which was the correct verdict.

    So the gate reads what the OPERATOR WROTE: an absent entry means never configured (no
    gate), and only an entry they actually set to false switches a tier off.

    Callers hold three shapes — an explicit dict (raw by definition), a config whose
    `.raw` is the stored mapping, or an object carrying `.model_intel` directly.
    """
    if isinstance(cfg_section, dict):
        return cfg_section
    if isinstance(config, dict):
        return config
    raw = getattr(config, "raw", None)
    if isinstance(raw, dict):
        section = raw.get("model_intel")
        return section if isinstance(section, dict) else {}
    mi = getattr(config, "model_intel", None)
    if isinstance(mi, dict):
        return mi
    return None


def read_states(config=None, cfg_section: Optional[dict] = None) -> Optional[dict[str, Optional[bool]]]:
    """{source: True | False | None} — on, explicitly off, or never configured.

    ``None`` as the WHOLE return value means the section could not be read at all, which is
    a third thing again: unknown. Callers must not collapse it into "everything is off".
    """
    section = _raw_section(config, cfg_section)
    if section is None:
        return None
    sources = section.get("sources")
    states: dict[str, Optional[bool]] = {}
    for name in SOURCE_NAMES:
        entry = sources.get(name) if isinstance(sources, dict) else None
        if isinstance(entry, dict) and "accepted" in entry:
            states[name] = bool(entry.get("accepted"))
        else:
            states[name] = UNSET
    return states


def enabled_sources(config=None, cfg_section: Optional[dict] = None) -> Optional[set[str]]:
    """The names switched ON. ``None`` when the switches could not be read at all."""
    states = read_states(config, cfg_section)
    if states is None:
        return None
    return {n for n, v in states.items() if v is ON}


def gating_sources(config=None, cfg_section: Optional[dict] = None) -> Optional[set[str]]:
    """The names switched OFF *deliberately* — the only ones that disable a tier.

    ``None`` when the switches could not be read at all, which gates nothing.
    """
    states = read_states(config, cfg_section)
    if states is None:
        return None
    return {n for n, v in states.items() if v is OFF}


def tier_availability(off: Optional[set[str]]) -> dict[str, dict]:
    """{tier: {available, reason, needs}} given the *deliberately disabled* sources.

    ``off is None`` (unreadable) leaves every tier available, matching what the router does.
    """
    out: dict[str, dict] = {}
    for tier, needs in TIER_REQUIRES.items():
        available = off is None or needs is None or needs not in off
        reason = ""
        if not available and needs is not None:
            reason = (f"{needs.capitalize()} is switched off in Settings, so this tier has "
                      f"no scores to use")
        out[tier] = {"available": available, "needs": needs, "reason": reason}
    return out


def effective_tier(chosen: Optional[str], off: Optional[set[str]]) -> tuple[str, str]:
    """(tier in force, why it differs from *chosen*).

    A chosen tier whose source is switched off does NOT quietly run on nothing: it falls
    back to `static`, which needs no source, and the reason is returned so every surface can
    say it. Only an explicit OFF fires this; never-configured does not, and an unreadable
    switch (``off is None``) returns the choice UNCHANGED.

    An unknown/missing choice resolves to the FALLBACK tier rather than defaulting to
    `intent` — defaulting to a tier that needs a source is how switching a source off turns
    into unexpected routing.
    """
    norm = (chosen or "").strip().lower()
    if norm == "rules":            # the old spelling of static
        norm = "static"
    if norm not in TIER_REQUIRES:
        return FALLBACK_TIER, ("" if not norm else f"{chosen!r} is not a routing tier")
    if off is None:
        return norm, ""
    needs = TIER_REQUIRES[norm]
    if needs is None or needs not in off:
        return norm, ""
    return FALLBACK_TIER, (f"set to {norm}, but {needs.capitalize()} is switched off — "
                           f"routing as {FALLBACK_TIER}")
