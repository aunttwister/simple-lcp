"""Preflight gate — refuse a request that cannot succeed, before any provider is billed.

PLAN §8 (R11) defines the gate as four checks plus an intent veto, evaluated
*before* the request is released to a provider:

```
G0 capability   model supports vision / context / thinking required by this request
G1 breaker      target not dead/degraded
G2 budget       profile + subject budgets have headroom        -> else deny or downgrade
G3 balance      provider balance above its floor               -> else re-rank, not fail
G4 intent gate  intent margin >= gate                          -> else keep static order
-> PASS (carry the chosen lane) | DOWNGRADE (a named cheaper lane) | DENY (explicit code)
```

and: *"Every verdict is written to ``routing_decisions`` with its layer id"*.

What this module is: ONE function — :func:`preflight_gate` — that answers a
single question, cheaply and without touching a provider:

    "Can this request succeed, and on which lane?"

It answers with a :class:`PreflightResult`, whose ``verdict`` is PASS /
DOWNGRADE / DENY. On DENY the result carries a typed :class:`PreflightRefusal`
naming the failed gate (G0–G4); the caller (``try_chain``) turns that into a
:class:`~src.api.exceptions.PreflightRefusalError` **before** the provider loop
runs, so a doomed request costs nothing. On G3 the gate may instead return a
DOWNGRADE — the same request, re-ranked onto a funded lane (a *named* lane, not
a silent edit).

Design rules this module holds to:

* **One choke point.** The gate runs inside ``try_chain`` immediately before the
  provider loop. Every caller of ``try_chain`` therefore gets the gate for free
  — no caller can forget it, and no caller can route around it.
* **The gate itself never raises.** It *returns* a refusal; the caller decides
  whether to raise. Decisions stay testable and composable.
* **Observation must not break the request.** Every gate is individually
  guarded: a broken budget table or an unreadable config downgrades that gate to
  "not evaluated" (recorded as such) instead of failing the request. The ONE
  exception is a genuine DENY, which is the whole point of the gate.
* **No secrets.** Messages and details carry gate ids, provider names, model
  names and counts — never credentials, keys or request bodies.

Where the verdict is recorded: the ``routing_decisions`` table (model
``RoutingDecision``), the row LCP already writes per routing decision. The gate
row uses the same columns as the dynamic router's rows, with ``path`` set to the
failed gate id (the §8 "layer id") and ``action`` one of ``refuse`` /
``gate_downgrade`` / ``gate_pass``. No new table is introduced.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .logging_config import get_logger

logger = get_logger("lcp.preflight")

# ── Gate ids (PLAN §8 / features/preflight-gate.md) ─────────────────────────
GATE_CAPABILITY = "G0"
GATE_BREAKER = "G1"
GATE_BUDGET = "G2"
GATE_BALANCE = "G3"
GATE_INTENT = "G4"
GATES = (GATE_CAPABILITY, GATE_BREAKER, GATE_BUDGET, GATE_BALANCE, GATE_INTENT)

# ── Verdicts (PLAN §8) ──────────────────────────────────────────────────────
PASS = "PASS"
DOWNGRADE = "DOWNGRADE"
DENY = "DENY"

# Stable machine-readable code for every gate refusal (LCP-4001 style).
REFUSAL_CODE = "LCP-4004"

# Per-gate HTTP status for the refusal. The reasons are not the same kind of
# failure, so one blanket 400 would be wrong:
#   G0 the caller asked for something no chain member can do        -> 400
#   G1 no chain member is usable right now (all dead/degraded)      -> 502
#   G2 a blocking budget for this profile/key is exhausted          -> 429
#   G3 every chain member's balance is at/below its floor           -> 402
#   G4 is never a refusal (it keeps the static order)               -> 400 (unused)
_GATE_STATUS = {
    GATE_CAPABILITY: 400,
    GATE_BREAKER: 502,
    GATE_BUDGET: 429,
    GATE_BALANCE: 402,
    GATE_INTENT: 400,
}

# routing_decisions.action for each verdict (the column is a plain String and
# already carries router actions like prefer/reorder/keep_default).
_GATE_ACTION = {PASS: "gate_pass", DOWNGRADE: "gate_downgrade", DENY: "refuse"}

# A relative/default db_path is never written to (see `_resolve_db_path`).
DEFAULT_ROUTER_DB_PATH = "data/costs.db"


# ── Types ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PreflightRefusal:
    """The typed refusal the gate returns when a request cannot succeed.

    ``gate`` is the failed gate id (G0–G4), ``code`` the stable LCP code, and
    ``status_code`` the HTTP status the caller should surface. ``details`` is
    always safe to send to a client: ids, counts and names only.
    """

    gate: str
    message: str
    code: str = REFUSAL_CODE
    status_code: int = 400
    details: dict = field(default_factory=dict)

    def to_error(self):
        """Build the exception ``try_chain`` raises for this refusal."""
        from .exceptions import PreflightRefusalError
        return PreflightRefusalError(
            self.message,
            gate=self.gate,
            status_code=self.status_code,
            details=self.details,
        )


@dataclass(frozen=True)
class PreflightResult:
    """The gate's answer.

    ``chain`` is the lane the request should use: unchanged on PASS, re-ranked
    on a G3 DOWNGRADE, and irrelevant on DENY (nothing is sent). ``decision`` is
    the ``routing_decisions`` row this verdict describes.
    """

    verdict: str
    chain: list
    gate: Optional[str] = None
    refusal: Optional[PreflightRefusal] = None
    notes: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    decision: dict = field(default_factory=dict)
    recorded: bool = False

    @property
    def is_refusal(self) -> bool:
        return self.refusal is not None


# ── Small helpers (duck-typed, never raise) ─────────────────────────────────

def _resolve_router(router: Any = None) -> Any:
    if router is not None:
        return router
    from .router import get_dynamic_router
    return get_dynamic_router()


def _model_limits(model: str, config: Any) -> dict:
    """Best-effort ``model_limits[model]`` for *model*, tolerant of stubs.

    Mirrors ``CapabilityRouter._context_window_for``: the registry-aware
    ``get_model_limits`` is consulted first, the plain ``model_limits`` dict
    second. Never raises; an unreadable config yields ``{}``.
    """
    for source in ("get_model_limits", "model_limits"):
        try:
            if config is None:
                break
            if source == "get_model_limits":
                getter = getattr(config, "get_model_limits", None)
                if not callable(getter):
                    continue
                limits = getter(model)
            else:
                limits = (getattr(config, "model_limits", None) or {}).get(model)
            if isinstance(limits, dict):
                return limits
        except Exception:  # noqa: BLE001 — duck-typed configs, never fatal
            continue
    return {}


def _step_base_url(step: dict, config: Any) -> str:
    base = step.get("base_url")
    if base:
        return base
    try:
        return (getattr(config, "providers", None) or {}).get(
            step.get("provider"), {}).get("api_base", "") or ""
    except Exception:  # noqa: BLE001
        return ""


def _chain_head(chain: list) -> dict:
    return chain[0] if chain else {}


# ── G0 — capability ─────────────────────────────────────────────────────────

def _gate_capability(chain: list, body: dict, config: Any, router: Any,
                     request_tokens: Optional[int] = None):
    """Can any chain member serve this request's shape *at all*?

    Three checks, each of which the provider would otherwise answer with a 4xx
    after being billed a round-trip:

    * **vision** — the body carries an image and no chain model declares
      ``supports_vision``;
    * **context** — the request *alone* is larger than the largest context
      window among the chain's models, so no member can hold it. The narrower
      "request + output reserve over the limit" case stays a warning
      (``X-LCP-Context-Warning``), not a refusal: the history can still be
      served and the caller has deliberately chosen to be warned rather than
      hard-failed;
    * **contract** — every chain member's declared capability map rejects the
      request's ``response_format`` (a contract, not a hint: it is never
      silently dropped).

    Returns ``(refusal_or_None, note_or_None)``.
    """
    if not chain:
        return None, None

    messages = body.get("messages") or []

    # ── vision ──
    try:
        from .request_pipeline import has_image_content
        if has_image_content(messages):
            if not any(_model_limits(s.get("model", ""), config)
                       .get("supports_vision", False) for s in chain):
                return PreflightRefusal(
                    gate=GATE_CAPABILITY,
                    message=(
                        "Request contains image content but no model in this "
                        "profile's chain declares vision support."
                    ),
                    status_code=_GATE_STATUS[GATE_CAPABILITY],
                    details={
                        "gate": GATE_CAPABILITY,
                        "reason": "vision_unsupported",
                        "models": [s.get("model") for s in chain],
                    },
                ), None
    except Exception:  # noqa: BLE001 — a capability probe must not fail the request
        logger.warning("preflight_g0_vision_check_failed", error="vision probe failed")

    # ── context ──
    try:
        tokens = request_tokens
        if tokens is None:
            from .cost_estimator import count_tokens
            from .router import _strip_client_context_from_messages
            tokens = count_tokens(
                _strip_client_context_from_messages(messages), body.get("tools"))
        if tokens and tokens > 0:
            max_ctx = router._max_routable_context(chain, config)
            if tokens > max_ctx:
                return PreflightRefusal(
                    gate=GATE_CAPABILITY,
                    message=(
                        f"Request is {tokens} tokens, which exceeds the largest "
                        f"context window ({max_ctx}) among this profile's chain — "
                        "no model can hold it."
                    ),
                    status_code=413,
                    details={
                        "gate": GATE_CAPABILITY,
                        "reason": "context_exceeded",
                        "request_tokens": tokens,
                        "max_context": max_ctx,
                    },
                ), None
    except Exception:  # noqa: BLE001
        logger.warning("preflight_g0_context_check_failed", error="context probe failed")

    # ── response_format contract ──
    try:
        from .capabilities import prepare_body
        if body.get("response_format"):
            reasons = []
            for s in chain:
                _out, _notes, skip = prepare_body(s.get("provider", ""), body, config)
                if not skip:
                    reasons = None
                    break
                reasons.append(f"{s.get('provider')}: {skip}")
            if reasons:
                return PreflightRefusal(
                    gate=GATE_CAPABILITY,
                    message=(
                        "Every provider in this profile's chain rejects the "
                        "request's response_format — the request cannot be served."
                    ),
                    status_code=_GATE_STATUS[GATE_CAPABILITY],
                    details={
                        "gate": GATE_CAPABILITY,
                        "reason": "response_format_unsupported",
                        "providers": reasons,
                    },
                ), None
    except Exception:  # noqa: BLE001
        logger.warning("preflight_g0_contract_check_failed", error="contract probe failed")

    return None, None


# ── G1 — breaker ────────────────────────────────────────────────────────────

def _gate_breaker(chain: list, config: Any, profile: str):
    """Is at least one chain member usable right now (not dead/degraded)?

    A chain where every member is breaker-unavailable cannot succeed, so the
    request is refused here rather than walking a chain that will skip every
    step and end as an opaque 502.
    """
    if not chain:
        return None, None
    try:
        from .circuit_breaker import get_circuit_breaker
        cb = get_circuit_breaker()
    except Exception:  # noqa: BLE001 — no breaker available → do not gate
        return None, "G1 not evaluated (circuit breaker unavailable)"

    available = []
    unavailable = []
    for step in chain:
        provider = step.get("provider", "")
        base_url = _step_base_url(step, config)
        try:
            ok = bool(cb.is_available(provider, base_url, profile))
        except Exception:  # noqa: BLE001 — an unknown state is not a proven outage
            ok = True
        (available if ok else unavailable).append(provider)

    if available:
        return None, None
    return PreflightRefusal(
        gate=GATE_BREAKER,
        message=(
            "Every provider in this profile's chain is currently unavailable "
            "(circuit breaker dead/degraded) — the request cannot succeed."
        ),
        status_code=_GATE_STATUS[GATE_BREAKER],
        details={"gate": GATE_BREAKER, "reason": "all_providers_unavailable",
                 "providers": unavailable},
    ), None


# ── G2 — budget ─────────────────────────────────────────────────────────────

def _gate_budget(profile: str, key_id: Optional[int], engine: Any):
    """Is any blocking budget for this profile/key already exhausted?

    Mirrors ``LCPHandler._check_budget_block`` (same ``budgets`` table, same
    ``action == 'block'`` + ``status in (active, exceeded)`` + spend >= amount
    rule) so the gate and the handler agree on what "blocked" means. Returns
    ``(refusal_or_None, note_or_None)``; a missing/unreadable store yields a
    note, never a denial — the gate must not refuse a request because its own
    bookkeeping is broken.
    """
    if engine is None:
        return None, "G2 not evaluated (no budget store)"
    try:
        from sqlalchemy import or_
        from .models import Budget, get_session
        with get_session(engine) as session:
            query = session.query(Budget).filter(
                Budget.action == "block",
                Budget.status.in_(["active", "exceeded"]),
                or_(Budget.profile == profile, Budget.profile.is_(None)),
            )
            if key_id is not None:
                query = query.filter(
                    or_(Budget.key_id == key_id, Budget.key_id.is_(None)))
            else:
                query = query.filter(Budget.key_id.is_(None))
            for budget in query.all():
                amount = budget.amount or 0.0
                spend = budget.current_spend or 0.0
                if amount > 0 and spend >= amount:
                    return PreflightRefusal(
                        gate=GATE_BUDGET,
                        message=(
                            f"Budget '{budget.name}' has been exhausted "
                            f"(${spend:.2f} of ${amount:.2f}) — requests are blocked."
                        ),
                        status_code=_GATE_STATUS[GATE_BUDGET],
                        details={"gate": GATE_BUDGET, "reason": "budget_exceeded",
                                 "budget": budget.name},
                    ), None
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not refuse a request
        logger.warning("preflight_g2_check_failed", error=type(exc).__name__)
        return None, "G2 not evaluated (budget store unreadable)"
    return None, None


# ── G3 — balance ────────────────────────────────────────────────────────────

def _gate_balance(chain: list, router: Any):
    """Is any chain member funded? If not, refuse; if the head is drained,
    re-rank so the request lands on a funded lane.

    PLAN §8 is explicit that G3 "re-ranks, does not fail" — so the normal
    outcome is a DOWNGRADE that moves a funded member to the front. The only
    refusal is the case re-ranking cannot fix: *every* member is at/below its
    floor, where any attempt is a guaranteed billed round-trip to a drained
    account.

    Returns ``(refusal_or_None, new_chain_or_None, note_or_None)``.
    """
    if not chain or router is None:
        return None, None, None
    try:
        headroom = {s.get("provider"): router._balance_headroom(s.get("provider"))
                    for s in chain}
    except Exception:  # noqa: BLE001 — an unreadable balance is "unknown", not "drained"
        return None, None, "G3 not evaluated (balance source unavailable)"

    funded = [s for s in chain if headroom.get(s.get("provider"))]
    if not funded:
        return PreflightRefusal(
            gate=GATE_BALANCE,
            message=(
                "Every provider in this profile's chain is at or below its "
                "balance floor — the request would be billed on a drained account."
            ),
            status_code=_GATE_STATUS[GATE_BALANCE],
            details={"gate": GATE_BALANCE, "reason": "all_providers_drained",
                     "providers": sorted({s.get("provider") for s in chain})},
        ), None, None

    drained = [s for s in chain if not headroom.get(s.get("provider"))]
    if drained:
        return None, funded + drained, (
            "G3 re-ranked the chain: drained provider(s) "
            f"{sorted({s.get('provider') for s in drained})} moved behind funded ones"
        )
    return None, None, None


# ── G4 — intent gate ────────────────────────────────────────────────────────

def _gate_intent(body: dict, config: Any, profile: str, router: Any,
                 margin: Optional[float], intent_gate: Optional[float]):
    """PLAN §8 G4: ``intent margin >= gate`` — otherwise keep the static order.

    This gate never refuses. It vetoes a *reorder*: when the classifier's top-1
    vs top-2 margin is below the profile's ``intent_margin_gate`` the first pick
    is a coin flip, so the static chain order stands. Both inputs are optional;
    the gate only runs when the profile declares a gate (``> 0``), which is the
    same off-switch M3 uses, so a profile that declares nothing pays nothing.

    Returns ``(static_order: bool, note: str | None, margin, evaluated: bool)``.
    """
    if router is None:
        return False, None, margin, False
    gate = intent_gate
    try:
        if gate is None:
            gate = float(router._intent_margin_gate(config, profile) or 0.0)
    except Exception:  # noqa: BLE001 — a bad gate must not stop a request
        gate = 0.0
    if not gate or gate <= 0.0:
        return False, "G4 not evaluated (profile declares no intent_margin_gate)", margin, False

    top1 = top2 = None
    try:
        if margin is None:
            from .router import classify_task_detail
            detail = classify_task_detail(
                body.get("messages") or [], body.get("tools"))
            margin, top1, top2 = router._intent_margin(detail)
    except Exception:  # noqa: BLE001
        return False, "G4 not evaluated (intent margin unavailable)", margin, False

    if margin is None:
        return False, "G4 not evaluated (no two scored intents to compare)", None, False
    if margin < float(gate):
        return True, (
            f"G4 intent gate fired: margin {margin:.3f} < gate {gate:g}"
            + (f" ({top1} vs {top2})" if top1 and top2 else "")
            + " — static chain order kept"
        ), margin, True
    return False, f"G4 passed: margin {margin:.3f} >= gate {gate:g}", margin, True


# ── The decision row + recorder ─────────────────────────────────────────────

def _decision_row(profile: str, task: str, verdict: str, gate: Optional[str],
                  chain: list, notes: list, message: Optional[str],
                  margin: Optional[float] = None,
                  balances: Optional[dict] = None) -> dict:
    """The ``routing_decisions`` row describing this verdict.

    Same shape the dynamic router writes (``_decision_base`` + ``l4``), with
    ``path`` carrying the §8 layer id and ``action`` the verdict. Persisting it
    is the "every verdict is written to ``routing_decisions``" requirement.
    """
    head = _chain_head(chain)
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "profile": profile or "",
        "task": task or "",
        "policy": "preflight",
        "action": _GATE_ACTION.get(verdict, verdict.lower()),
        "provider": head.get("provider"),
        "model": head.get("model"),
        "score": None,
        "rules": list(notes or []),
        "note": message,
        "path": gate,          # the §8 layer id (G0..G4) or None on a clean pass
        "min_score": None,
        "l4": {
            "layer": gate or "preflight",
            "rule_id": gate or "preflight",
            "intent": task or "",
            "margin": margin,
            "scores": {},
            "balances": balances or {},
            "from": {"provider": head.get("provider"), "model": head.get("model")},
            "to": {"provider": head.get("provider"), "model": head.get("model")},
            "reason": message or verdict,
        },
    }


def record_decision(decision: dict, db_path: str) -> bool:
    """Persist *decision* to ``routing_decisions``. Best-effort → bool.

    Uses the same table and column mapping as ``CapabilityRouter._record_decision``
    (a DB failure must never break routing), so the gate's verdicts sit in the
    one decision log the UI already reads.
    """
    try:
        from .models import RoutingDecision, get_engine, get_session
        with get_session(get_engine(db_path)) as session:
            session.add(RoutingDecision(
                ts=decision.get("ts"),
                profile=decision.get("profile") or "",
                task=decision.get("task") or "",
                policy=decision.get("policy") or "",
                action=decision.get("action") or "",
                provider=decision.get("provider"),
                model=decision.get("model"),
                score=decision.get("score"),
                rules_json=json.dumps(decision.get("rules") or []),
                note=decision.get("note"),
                path=decision.get("path"),
                min_score=decision.get("min_score"),
                l4_json=(json.dumps(decision["l4"]) if decision.get("l4") else None),
            ))
            session.commit()
        return True
    except Exception as exc:  # noqa: BLE001 — persistence must never break routing
        logger.warning("preflight_decision_record_failed", error=type(exc).__name__)
        return False


def _resolve_db_path(config: Any, router: Any, explicit: Optional[str]) -> Optional[str]:
    """Resolve the decision-log DB path, or ``None`` to skip recording.

    Order: an explicit path, then the config's ``database.path``, then the
    router's ``db_path``. The router fallback is accepted only when it is
    ABSOLUTE: the legacy singleton defaults to the relative ``data/costs.db``,
    and a stray test or dev process must not be able to append gate rows to a
    repo-local database. Production always resolves an absolute path
    (``/app/data/costs.db``).
    """
    if explicit:
        return explicit
    try:
        data = getattr(config, "_data", None)
        if isinstance(data, dict):
            path = (data.get("database") or {}).get("path")
            if isinstance(path, str) and path:
                return path
    except Exception:  # noqa: BLE001
        pass
    for attr in ("database_path", "db_path"):
        val = getattr(config, attr, None)
        if isinstance(val, str) and val:
            return val
    router_path = getattr(router, "db_path", None)
    if isinstance(router_path, str) and os.path.isabs(router_path):
        return router_path
    return None


# ── The gate ────────────────────────────────────────────────────────────────

def preflight_gate(profile_name: str, profile_cfg: dict, body: dict, config: Any,
                   *,
                   chain: Optional[list] = None,
                   static_chain: Optional[list] = None,
                   key_id: Optional[int] = None,
                   engine: Any = None,
                   db_path: Optional[str] = None,
                   task: str = "",
                   margin: Optional[float] = None,
                   intent_gate: Optional[float] = None,
                   request_tokens: Optional[int] = None,
                   router: Any = None,
                   recorder: Optional[Callable[[dict], bool]] = None,
                   record: bool = True,
                   record_pass: bool = False) -> PreflightResult:
    """Evaluate G0–G4 for this request and return the verdict.

    Call this **before** any provider is contacted (``try_chain`` calls it
    immediately before its provider loop). The returned ``chain`` is the lane to
    use; a returned ``refusal`` means the request cannot succeed and must not be
    attempted.

    ``record`` persists the verdict to ``routing_decisions`` (a refusal always
    records; a clean PASS records only when ``record_pass=True``, so the log is
    not doubled by every healthy request). ``recorder`` overrides the writer for
    tests. Nothing here raises.
    """
    chain = list(chain if chain is not None else (profile_cfg or {}).get("chain") or [])
    static_chain = list(static_chain if static_chain is not None else chain)
    router = _resolve_router(router)

    checks: dict = {}
    notes: list = []

    def _skip(gate_id: str, note: Optional[str]):
        checks[gate_id] = "skip"
        if note:
            notes.append(note)

    def _pass(gate_id: str):
        checks[gate_id] = "pass"

    def _settle(gate_id: str, note: Optional[str]):
        """Record a non-refusing gate outcome: a note means 'not evaluated'."""
        if note:
            _skip(gate_id, note)
        else:
            _pass(gate_id)

    refusal = None

    # ── G0 capability ──
    note = None
    try:
        refusal, note = _gate_capability(chain, body, config, router,
                                         request_tokens)
    except Exception as exc:  # noqa: BLE001
        logger.warning("preflight_gate_error", gate=GATE_CAPABILITY,
                       error=type(exc).__name__)
        refusal, note = None, f"G0 not evaluated ({type(exc).__name__})"
    if refusal is None:
        _settle(GATE_CAPABILITY, note)

    # ── G1 breaker ──
    if refusal is None:
        try:
            refusal, note = _gate_breaker(chain, config, profile_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("preflight_gate_error", gate=GATE_BREAKER,
                           error=type(exc).__name__)
            refusal, note = None, f"G1 not evaluated ({type(exc).__name__})"
        if refusal is None:
            _settle(GATE_BREAKER, note)

    # ── G2 budget ──
    check_engine = engine
    if check_engine is None and refusal is None:
        path = _resolve_db_path(config, router, db_path)
        if path and os.path.exists(path):
            try:
                from .models import get_engine as _get_engine
                check_engine = _get_engine(path)
            except Exception:  # noqa: BLE001
                check_engine = None
    if refusal is None:
        try:
            refusal, note = _gate_budget(profile_name, key_id, check_engine)
        except Exception as exc:  # noqa: BLE001
            logger.warning("preflight_gate_error", gate=GATE_BUDGET,
                           error=type(exc).__name__)
            refusal, note = None, f"G2 not evaluated ({type(exc).__name__})"
        if refusal is None:
            _pass(GATE_BUDGET) if note is None else _skip(GATE_BUDGET, note)

    # ── G3 balance ──
    balances = None
    if refusal is None:
        try:
            balances = {s.get("provider"): router._balance_headroom(s.get("provider"))
                        for s in chain} if chain else {}
        except Exception:  # noqa: BLE001
            balances = None
        try:
            refusal, new_chain, note = _gate_balance(chain, router)
        except Exception as exc:  # noqa: BLE001
            logger.warning("preflight_gate_error", gate=GATE_BALANCE,
                           error=type(exc).__name__)
            refusal, new_chain, note = None, None, f"G3 not evaluated ({type(exc).__name__})"
        if refusal is None:
            if new_chain is not None:
                chain = new_chain
                checks[GATE_BALANCE] = "downgrade"
                notes.append(note)
            elif note:
                _skip(GATE_BALANCE, note)
            else:
                _pass(GATE_BALANCE)

    # ── G4 intent gate (never refuses) ──
    static_order = False
    if refusal is None:
        evaluated = False
        try:
            static_order, note, margin, evaluated = _gate_intent(
                body, config, profile_name, router, margin, intent_gate)
        except Exception as exc:  # noqa: BLE001
            logger.warning("preflight_gate_error", gate=GATE_INTENT,
                           error=type(exc).__name__)
            static_order, note, evaluated = False, f"G4 not evaluated ({type(exc).__name__})", False
        if static_order:
            chain = static_chain
            checks[GATE_INTENT] = "static"
            if note:
                notes.append(note)
        elif evaluated:
            checks[GATE_INTENT] = "pass"
            if note:
                notes.append(note)
        else:
            _skip(GATE_INTENT, note)

    # ── Verdict ──
    if refusal is not None:
        verdict, gate = DENY, refusal.gate
        message = refusal.message
    elif checks.get(GATE_BALANCE) == "downgrade":
        verdict, gate = DOWNGRADE, GATE_BALANCE
        message = next((n for n in notes if n.startswith("G3")), None)
    elif checks.get(GATE_INTENT) == "static":
        verdict, gate = PASS, GATE_INTENT
        message = next((n for n in notes if n.startswith("G4")), None)
    else:
        verdict, gate, message = PASS, None, None

    decision = _decision_row(profile_name, task, verdict, gate, chain, notes,
                             message, margin=margin, balances=balances)

    result = PreflightResult(
        verdict=verdict, chain=chain, gate=gate, refusal=refusal,
        notes=notes, checks=checks, decision=decision)

    # ── Record ──
    if record and (refusal is not None or verdict == DOWNGRADE or record_pass):
        if recorder is not None:
            try:
                recorded = bool(recorder(decision))
            except Exception:  # noqa: BLE001
                recorded = False
        else:
            path = _resolve_db_path(config, router, db_path)
            recorded = bool(path) and record_decision(decision, path)
        result = PreflightResult(
            verdict=result.verdict, chain=result.chain, gate=result.gate,
            refusal=result.refusal, notes=result.notes, checks=result.checks,
            decision=result.decision, recorded=recorded)

    if refusal is not None:
        logger.warning("preflight_refused", profile=profile_name,
                       gate=refusal.gate, code=refusal.code,
                       reason=refusal.details.get("reason"))
    elif verdict == DOWNGRADE:
        logger.info("preflight_downgrade", profile=profile_name, gate=gate)

    return result
