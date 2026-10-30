"""One identity for a model, everywhere (CUR-270).

Every other module asks this one. Nothing else resolves, lowercases, splits or
prefix-strips a model string.

Why a module rather than a helper
---------------------------------

Measured in prod on 2026-10-30, "which model ran?" was unanswerable from LCP's own
records: 24 distinct ``(provider, model)`` pairs in traffic with **10 resolving to
nothing**, 19 capability keys with 5 resolving to nothing, 16 pricing rows carrying
11 spellings, and 30 days of routing "changes" that were mostly the same model under
a different spelling (3,070 + 3,156 + 393 rows). A model was identified by a
*display string*, and the lookup was keyed on the **provider name** — which is itself
renamed — so nothing could be reconciled.

The rules this module exists to enforce:

* resolution is **per endpoint**; the same string is a different model on a different
  endpoint (``deepseek-v4-flash`` on api.deepseek.com is V4.1-Flash today, because the
  name was retired and left as a routing alias);
* resolution is **time-aware**; a release can change under a stable served id, and no
  string can express that;
* resolution is **pure and total** — it never guesses, it returns ``unresolved`` with a
  reason, and identical ``(endpoint, raw, at, registry_version)`` always gives the same
  answer;
* every resolution says **how** it matched, so drift is explainable after the fact.

Failure modes are deliberately asymmetric:

* config / chain load should call :func:`resolve_strict` and **fail closed** — a bad
  chain silently routing to the wrong model is an expensive surprise nobody sees;
* request time calls :func:`resolve`, records the ``unresolved`` reason, and continues
  — live traffic is never blocked because a registry is stale.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

__all__ = [
    "Resolution",
    "UnresolvedModel",
    "resolve",
    "resolve_strict",
    "normalise_model_string",
    "registry_version",
    "list_endpoints",
    "list_logical",
    "list_deployments",
    "aliases",
    "price_for",
    "capabilities_for",
    "reconcile",
    "connect",
]

MATCH_SERVED = "served_id"
MATCH_ALIAS = "explicit_alias"
MATCH_NORMALISED = "normalised"
MATCH_SLUG = "logical_slug"
MATCH_NONE = "unresolved"

_CONFIDENCE = {
    MATCH_SERVED: "high",
    MATCH_SLUG: "high",
    MATCH_ALIAS: "high",
    MATCH_NORMALISED: "low",   # provisional: must be promoted or fixed
}

# Vendor prefix and quantisation/file suffixes are stripped ONLY inside the matcher,
# never for storage. Every match made this way is tagged MATCH_NORMALISED so it can be
# counted and surfaced for promotion.
_VENDOR_PREFIX_RE = re.compile(r"^[A-Za-z0-9_.-]+/")
_QUANT_SUFFIX_RE = re.compile(
    r"[-_.](?:gguf|q[2-8]_[k0-9_]+|iq[0-9_]+|exl[23]|awq|gptq|fp8|fp16|bf16|int[48]"
    r"|4bpw|2\.9bpw|3\.05bpw)$",
    re.I,
)


class UnresolvedModel(LookupError):
    """Raised by :func:`resolve_strict` for a model the registry cannot identify."""

    def __init__(self, resolution: "Resolution"):
        self.resolution = resolution
        super().__init__(
            f"unresolved model {resolution.raw!r} on endpoint "
            f"{resolution.endpoint!r}: {resolution.unresolved_reason}"
        )


@dataclass(frozen=True)
class Resolution:
    """The answer to "which model is this?" — with its provenance."""

    raw: str
    endpoint: Optional[str]
    endpoint_id: Optional[int]
    deployment_id: Optional[int]
    model_id: Optional[int]
    model: Optional[str]            # logical slug — the stable identity
    release_id: Optional[int]
    release: Optional[str]          # release label in effect at ``at``
    matched_by: str
    confidence: str
    unresolved_reason: Optional[str]
    registry_version: int
    at: str

    @property
    def resolved(self) -> bool:
        return self.model is not None

    def __bool__(self) -> bool:  # so callers can write `if resolve(...):`
        return self.resolved

    def as_dict(self) -> dict:
        return {
            "raw": self.raw,
            "endpoint": self.endpoint,
            "deployment_id": self.deployment_id,
            "model_id": self.model_id,
            "model": self.model,
            "release": self.release,
            "matched_by": self.matched_by,
            "confidence": self.confidence,
            "unresolved_reason": self.unresolved_reason,
            "registry_version": self.registry_version,
            "at": self.at,
        }


# --------------------------------------------------------------------------- db


def _default_db_path() -> str:
    try:
        from .config import db_path as _p  # type: ignore

        return _p()
    except Exception:
        return os.environ.get("COST_DB", "/app/data/costs.db")


def connect(path: Optional[str] = None, *, ro: bool = False) -> sqlite3.Connection:
    """Open the cost DB. ``ro`` gives a read-only connection (the safe default here)."""
    p = path or _default_db_path()
    if ro:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
    else:
        con = sqlite3.connect(p, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    return bool(
        con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    )


def _ensure_ready(con: sqlite3.Connection) -> None:
    if not _has_table(con, "mi_models"):
        raise RuntimeError(
            "model-identity registry is not migrated (mi_models missing) — "
            "run `alembic upgrade 026`"
        )


# ------------------------------------------------------------------- normalising


def normalise_model_string(raw: str) -> str:
    """Case-fold, drop a vendor prefix and a quantisation suffix.

    Used **only** as the matcher's last resort, and every match it produces is
    reported as ``normalised`` (confidence ``low``) so it is visible. Never use
    this to decide what to store — that discards the provider's real id.
    """
    s = (raw or "").strip().lower()
    s = _VENDOR_PREFIX_RE.sub("", s)
    # strip suffixes repeatedly: "GLM-5.3-Flash-EXL3.gguf" carries a quant *and* a file
    # extension, and one pass would leave "glm-5.3-flash-exl3" — which matches nothing.
    for _ in range(4):
        stripped = _QUANT_SUFFIX_RE.sub("", s)
        if stripped == s:
            break
        s = stripped
    return s


# ------------------------------------------------------------------- resolution


def registry_version(con: sqlite3.Connection) -> int:
    _ensure_ready(con)
    row = con.execute("SELECT MAX(version) AS v FROM mi_registry_versions").fetchone()
    return int(row["v"] or 0)


def _endpoint(con: sqlite3.Connection, slug: Optional[str]):
    """(endpoint_id, slug) for a live or legacy spelling, else (None, None)."""
    if not slug:
        return None, None
    row = con.execute(
        "SELECT endpoint_id, slug FROM mi_endpoints WHERE slug = ?", (slug,)
    ).fetchone()
    if row:
        return int(row["endpoint_id"]), row["slug"]
    row = con.execute(
        "SELECT e.endpoint_id, e.slug FROM mi_endpoint_aliases a"
        " JOIN mi_endpoints e ON e.endpoint_id = a.endpoint_id WHERE a.alias = ?",
        (slug,),
    ).fetchone()
    if row:
        return int(row["endpoint_id"]), row["slug"]
    return None, None


def _release_at(con: sqlite3.Connection, deployment_id: Optional[int], at: str):
    """The release in effect on a deployment at ``at`` — the normalisation mechanism."""
    if deployment_id is None:
        return None, None
    row = con.execute(
        "SELECT r.release_id, r.label FROM mi_deployment_releases dr"
        " JOIN mi_releases r ON r.release_id = dr.release_id"
        " WHERE dr.deployment_id = ? AND dr.effective_from <= ?"
        "   AND (dr.effective_to IS NULL OR dr.effective_to > ?)"
        " ORDER BY dr.effective_from DESC LIMIT 1",
        (deployment_id, at, at),
    ).fetchone()
    if row:
        return int(row["release_id"]), row["label"]
    return None, None


def _in_force(con: sqlite3.Connection, deployment_id: int, at: str) -> bool:
    """Was this deployment actually serving at ``at``?

    A deployment with a release timeline is in force only inside its windows — which is
    how a *retired* deployment is distinguished from a live one whose served id was
    re-pointed elsewhere. A deployment with no timeline is treated as always in force.
    """
    row = con.execute(
        "SELECT COUNT(*) AS n,"
        " SUM(CASE WHEN effective_from <= ? AND (effective_to IS NULL OR effective_to > ?)"
        "     THEN 1 ELSE 0 END) AS k"
        " FROM mi_deployment_releases WHERE deployment_id = ?",
        (at, at, deployment_id),
    ).fetchone()
    if row["n"]:
        return bool(row["k"])
    d = con.execute(
        "SELECT first_seen, last_seen FROM mi_deployments WHERE deployment_id = ?",
        (deployment_id,),
    ).fetchone()
    if d and (d["first_seen"] or d["last_seen"]):
        return ((not d["first_seen"] or d["first_seen"] <= at)
                and (not d["last_seen"] or d["last_seen"] > at))
    return True


def _from_deployment(con: sqlite3.Connection, deployment_id: int, at: str, **kw) -> Resolution:
    row = con.execute(
        "SELECT d.deployment_id, d.served_id, m.model_id, m.slug, e.slug AS endpoint"
        " FROM mi_deployments d JOIN mi_models m ON m.model_id = d.model_id"
        " JOIN mi_endpoints e ON e.endpoint_id = d.endpoint_id"
        " WHERE d.deployment_id = ?",
        (deployment_id,),
    ).fetchone()
    if not row:
        return _unresolved(con, kw["raw"], kw["endpoint"], kw["reason"], kw["at"], kw["rv"])
    release_id, release = _release_at(con, deployment_id, at)
    matched_by = kw["matched_by"]
    return Resolution(
        raw=kw["raw"],
        endpoint=row["endpoint"],
        endpoint_id=kw["endpoint_id"],
        deployment_id=int(row["deployment_id"]),
        model_id=int(row["model_id"]),
        model=row["slug"],
        release_id=release_id,
        release=release,
        matched_by=matched_by,
        confidence=kw.get("confidence") or _CONFIDENCE.get(matched_by, "medium"),
        unresolved_reason=None,
        registry_version=kw["rv"],
        at=at,
    )


def _from_model(con: sqlite3.Connection, model_id: int, at: str, **kw) -> Resolution:
    row = con.execute(
        "SELECT model_id, slug FROM mi_models WHERE model_id = ?", (model_id,)
    ).fetchone()
    if not row:
        return _unresolved(con, kw["raw"], kw["endpoint"], kw["reason"], kw["at"], kw["rv"])
    return Resolution(
        raw=kw["raw"],
        endpoint=kw["endpoint"],
        endpoint_id=kw["endpoint_id"],
        deployment_id=None,
        model_id=int(row["model_id"]),
        model=row["slug"],
        release_id=None,
        release=None,
        matched_by=kw["matched_by"],
        confidence=kw.get("confidence") or _CONFIDENCE.get(kw["matched_by"], "medium"),
        unresolved_reason=None,
        registry_version=kw["rv"],
        at=at,
    )


def _unresolved(con, raw, endpoint, reason, at, rv) -> Resolution:
    ep_id, ep_slug = _endpoint(con, endpoint)
    return Resolution(
        raw=raw,
        endpoint=ep_slug or endpoint,
        endpoint_id=ep_id,
        deployment_id=None,
        model_id=None,
        model=None,
        release_id=None,
        release=None,
        matched_by=MATCH_NONE,
        confidence="none",
        unresolved_reason=reason,
        registry_version=rv,
        at=at,
    )


def resolve(
    endpoint: Optional[str],
    raw: str,
    *,
    at: Optional[str] = None,
    con: Optional[sqlite3.Connection] = None,
) -> Resolution:
    """Identify ``raw`` on ``endpoint`` as of ``at``. Pure; never raises on bad input."""
    own = con is None
    con = con or connect(ro=True)
    try:
        _ensure_ready(con)
        rv = registry_version(con)
        at = at or _now()
        raw = (raw or "").strip()
        endpoint = (endpoint or "").strip() or None
        if not raw:
            return _unresolved(con, raw, endpoint, "empty", at, rv)

        ep_id, ep_slug = _endpoint(con, endpoint)
        if endpoint and ep_id is None:
            endpoint_reason = "unknown_endpoint"
        else:
            endpoint_reason = "no_match"
        ctx = {
            "raw": raw,
            "endpoint": ep_slug or endpoint,
            "endpoint_id": ep_id,
            "rv": rv,
            "reason": endpoint_reason,
        }

        # -- layer 0: the endpoint's own served id, while that deployment was serving --
        # A served id can be retired and re-pointed (DeepSeek left `deepseek-v4-flash` as
        # a routing alias to V4.1-Flash), so an exact id match alone is not enough: the
        # deployment must actually have been serving at `at`.
        served_id_match = None
        if ep_id is not None:
            row = con.execute(
                "SELECT deployment_id FROM mi_deployments"
                " WHERE endpoint_id = ? AND served_id = ?",
                (ep_id, raw),
            ).fetchone()
            if row:
                served_id_match = int(row["deployment_id"])
                if _in_force(con, served_id_match, at):
                    return _from_deployment(con, served_id_match, at,
                                            matched_by=MATCH_SERVED, **ctx)

        # -- layer 1: an explicit alias, in force at `at` ---------------------------
        # Endpoint-specific wins over global; within that, the most recent start.
        row = con.execute(
            "SELECT deployment_id, model_id FROM mi_aliases"
            " WHERE alias = ? AND (endpoint_id IS NULL OR endpoint_id = ?)"
            "   AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
            " ORDER BY (endpoint_id IS NOT NULL) DESC, valid_from DESC LIMIT 1",
            (raw, ep_id, at, at),
        ).fetchone()
        if row:
            if row["deployment_id"] is not None:
                return _from_deployment(con, int(row["deployment_id"]), at,
                                        matched_by=MATCH_ALIAS, **ctx)
            if row["model_id"] is not None:
                return _from_model(con, int(row["model_id"]), at,
                                   matched_by=MATCH_ALIAS, **ctx)

        # -- layer 2: exact served id outside its serving window ---------------------
        # Weaker evidence than an in-force alias (the deployment is retired), so medium.
        if served_id_match is not None:
            return _from_deployment(con, served_id_match, at,
                                    matched_by=MATCH_SERVED, confidence="medium", **ctx)

        # -- layer 3: normalised spelling (provisional, always reported) ------------
        # Scoped by endpoint: a spelling registered for one endpoint must never resolve
        # traffic on another.
        norm = normalise_model_string(raw)
        if ep_id is not None:
            for r in con.execute(
                "SELECT deployment_id, served_id FROM mi_deployments WHERE endpoint_id = ?",
                (ep_id,),
            ):
                if normalise_model_string(r["served_id"]) == norm:
                    return _from_deployment(con, int(r["deployment_id"]), at,
                                            matched_by=MATCH_NORMALISED, **ctx)
        for r in con.execute(
            "SELECT alias, deployment_id, model_id FROM mi_aliases"
            " WHERE endpoint_id IS NULL OR endpoint_id = ?"
            " ORDER BY (endpoint_id IS NOT NULL) DESC",
            (ep_id,),
        ):
            if normalise_model_string(r["alias"]) == norm:
                if r["deployment_id"] is not None:
                    return _from_deployment(con, int(r["deployment_id"]), at,
                                            matched_by=MATCH_NORMALISED, **ctx)
                if r["model_id"] is not None:
                    return _from_model(con, int(r["model_id"]), at,
                                       matched_by=MATCH_NORMALISED, **ctx)

        # -- layer 3: the raw string is already a logical slug ---------------------
        row = con.execute("SELECT model_id FROM mi_models WHERE slug = ?", (raw,)).fetchone()
        if row:
            return _from_model(con, int(row["model_id"]), at, matched_by=MATCH_SLUG, **ctx)
        row = con.execute(
            "SELECT model_id FROM mi_models WHERE slug = ?", (norm,)
        ).fetchone()
        if row:
            return _from_model(con, int(row["model_id"]), at,
                               matched_by=MATCH_NORMALISED, **ctx)

        return _unresolved(con, raw, endpoint, endpoint_reason, at, rv)
    finally:
        if own:
            con.close()


def resolve_strict(endpoint: Optional[str], raw: str, **kw) -> Resolution:
    """Fail closed — for config / chain load, where a wrong model is expensive."""
    r = resolve(endpoint, raw, **kw)
    if not r.resolved:
        raise UnresolvedModel(r)
    return r


# ------------------------------------------------------------------- read views


def list_endpoints(con: Optional[sqlite3.Connection] = None) -> list[dict]:
    own = con is None
    con = con or connect(ro=True)
    try:
        _ensure_ready(con)
        return [dict(r) for r in con.execute(
            "SELECT endpoint_id, slug, display_name, base_url, tier, status, notes"
            " FROM mi_endpoints ORDER BY tier, slug"
        )]
    finally:
        if own:
            con.close()


def list_logical(con: Optional[sqlite3.Connection] = None) -> list[dict]:
    own = con is None
    con = con or connect(ro=True)
    try:
        _ensure_ready(con)
        return [dict(r) for r in con.execute(
            "SELECT m.model_id, m.slug, m.display_name, m.family, m.status, m.benchmark_key,"
            "  (SELECT COUNT(*) FROM mi_deployments d WHERE d.model_id = m.model_id) AS deployments"
            " FROM mi_models m ORDER BY m.status, m.slug"
        )]
    finally:
        if own:
            con.close()


def list_deployments(con: Optional[sqlite3.Connection] = None) -> list[dict]:
    own = con is None
    con = con or connect(ro=True)
    try:
        _ensure_ready(con)
        return [dict(r) for r in con.execute(
            "SELECT d.deployment_id, e.slug AS endpoint, d.served_id, m.slug AS model,"
            "  d.variant, d.quantisation, d.context_window, d.status, d.source, d.verified_at,"
            "  d.notes"
            " FROM mi_deployments d JOIN mi_models m ON m.model_id = d.model_id"
            " JOIN mi_endpoints e ON e.endpoint_id = d.endpoint_id"
            " ORDER BY e.slug, m.slug, d.served_id"
        )]
    finally:
        if own:
            con.close()


def aliases(model_or_deployment: Any, *, con: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Every accepted spelling for a model slug or a deployment id."""
    own = con is None
    con = con or connect(ro=True)
    try:
        _ensure_ready(con)
        if isinstance(model_or_deployment, int):
            sel = ("SELECT a.alias, e.slug AS endpoint, a.source, a.valid_from, a.valid_to"
                   " FROM mi_aliases a"
                   " LEFT JOIN mi_endpoints e ON e.endpoint_id = a.endpoint_id"
                   " WHERE a.deployment_id = ? ORDER BY a.valid_from")
            return [dict(r) for r in con.execute(sel, (model_or_deployment,))]
        sel = ("SELECT a.alias, e.slug AS endpoint, a.source, a.valid_from, a.valid_to"
               " FROM mi_aliases a"
               " LEFT JOIN mi_endpoints e ON e.endpoint_id = a.endpoint_id"
               " LEFT JOIN mi_models m ON m.model_id = a.model_id"
               " LEFT JOIN mi_deployments d ON d.deployment_id = a.deployment_id"
               " LEFT JOIN mi_models dm ON dm.model_id = d.model_id"
               " WHERE m.slug = ? OR dm.slug = ? ORDER BY a.valid_from")
        return [dict(r) for r in con.execute(sel, (model_or_deployment, model_or_deployment))]
    finally:
        if own:
            con.close()


def price_for(deployment_id: int, *, con: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    """The price row for a deployment, read from the existing pricing settings.

    Phase 1 deliberately does **not** create a second price table — the design
    forbids two sources of truth. This resolves the price the app already uses,
    keyed by the deployment's endpoint + served id, so the identity layer can be
    the single reader without moving the data yet (phase 4 moves it).
    """
    import json

    own = con is None
    con = con or connect(ro=True)
    try:
        _ensure_ready(con)
        row = con.execute(
            "SELECT e.slug AS endpoint, d.served_id, d.variant, m.slug AS model"
            " FROM mi_deployments d JOIN mi_endpoints e ON e.endpoint_id = d.endpoint_id"
            " JOIN mi_models m ON m.model_id = d.model_id WHERE d.deployment_id = ?",
            (deployment_id,),
        ).fetchone()
        if not row:
            return None
        raw = con.execute(
            "SELECT value FROM settings WHERE key = 'gateway_config:pricing'"
        ).fetchone()
        if not raw:
            return None
        try:
            rows = json.loads(raw["value"])
        except Exception:
            return None
        wanted = {row["served_id"], row["model"]}
        for r in rows or []:
            if str(r.get("model") or "") in wanted and (
                r.get("provider") in (None, row["endpoint"])
            ):
                return dict(r)
        return None
    finally:
        if own:
            con.close()


def capabilities_for(model: str, *, con: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Capability rows for a logical model, found via every spelling it is known by."""
    own = con is None
    con = con or connect(ro=True)
    try:
        _ensure_ready(con)
        keys = {model, normalise_model_string(model)}
        mrow = con.execute(
            "SELECT model_id, benchmark_key FROM mi_models WHERE slug = ?", (model,)
        ).fetchone()
        if mrow and mrow["benchmark_key"]:
            keys.add(mrow["benchmark_key"])
        for r in con.execute("SELECT alias FROM mi_aliases"):
            if normalise_model_string(r["alias"]) in {normalise_model_string(k) for k in keys}:
                keys.add(r["alias"])
        if mrow:
            for r in con.execute(
                "SELECT d.served_id FROM mi_deployments d WHERE d.model_id = ?",
                (mrow["model_id"],),
            ):
                keys.add(r["served_id"])
        out: list[dict] = []
        for k in sorted(keys):
            for r in con.execute(
                "SELECT * FROM model_capabilities WHERE model = ?", (k,)
            ):
                out.append(dict(r))
        return out
    finally:
        if own:
            con.close()


# ------------------------------------------------------------------ reconciliation


def reconcile(*, con: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Compare every surface against the registry and report disagreement.

    Report-only by design; the caller decides whether to alert. A silent gap here
    is exactly what let the alias noise accumulate.
    """
    own = con is None
    con = con or connect(ro=True)
    try:
        _ensure_ready(con)
        findings: list[dict] = []

        def add(kind: str, severity: str, detail: str, **extra):
            findings.append({"kind": kind, "severity": severity, "detail": detail, **extra})

        # 1. deployments never confirmed against the endpoint's own /v1/models
        for r in con.execute(
            "SELECT e.slug AS endpoint, d.served_id, m.slug AS model FROM mi_deployments d"
            " JOIN mi_endpoints e ON e.endpoint_id = d.endpoint_id"
            " JOIN mi_models m ON m.model_id = d.model_id"
            " WHERE d.verified_at IS NULL AND d.status = 'active'"
        ):
            add("unverified_deployment", "medium",
                f"{r['endpoint']} / {r['served_id']} ({r['model']}) is not confirmed against "
                f"the endpoint's /v1/models", endpoint=r["endpoint"], served_id=r["served_id"])

        # 2. traffic strings that resolve to nothing
        for table, col_prov, col_model in (("requests", "provider", "model"),
                                           ("routing_decisions", "provider", "model")):
            if not _has_table(con, table):
                continue
            rows = list(con.execute(
                f"SELECT {col_prov} AS p, {col_model} AS m, COUNT(*) AS n FROM {table}"
                f" WHERE {col_model} IS NOT NULL GROUP BY p, m ORDER BY n DESC LIMIT 60"
            ))
            for r in rows:
                res = resolve(r["p"], r["m"], con=con)
                if not res.resolved:
                    add("unresolved_traffic", "high",
                        f"{table}: {r['n']} rows of ({r['p']} / {r['m']}) resolve to nothing"
                        f" ({res.unresolved_reason})", table=table, provider=r["p"],
                        served=r["m"], rows=r["n"])

        # 3. the writer defect: rows with no provider/model at all
        for table in ("requests", "routing_decisions"):
            if not _has_table(con, table):
                continue
            n = con.execute(
                f"SELECT COUNT(*) FROM {table} WHERE provider = 'unknown' AND model = 'unknown'"
            ).fetchone()[0]
            if n:
                add("unknown_writer_rows", "high",
                    f"{table}: {n} rows recorded with provider AND model literally 'unknown' — "
                    f"a code path writes a decision without knowing what it routed to",
                    table=table, rows=n)

        # 4. active deployments with no price
        for d in list_deployments(con=con):
            if d["status"] == "active" and price_for(d["deployment_id"], con=con) is None:
                add("deployment_without_price", "medium",
                    f"{d['endpoint']} / {d['served_id']} has no pricing row",
                    endpoint=d["endpoint"], served_id=d["served_id"])

        # 5. capability keys nothing identifies
        if _has_table(con, "model_capabilities"):
            for r in con.execute("SELECT DISTINCT model FROM model_capabilities"):
                if not resolve(None, r["model"], con=con).resolved:
                    add("capability_key_unresolved", "medium",
                        f"model_capabilities holds rows for {r['model']!r}, which identifies "
                        f"no model", key=r["model"], table="model_capabilities")

        # 8. traffic recorded against an endpoint the registry does not know
        # (resolution can still succeed here — the string may be a logical slug — so this
        # cannot be inferred from unresolved traffic alone)
        for table in ("requests", "routing_decisions"):
            if not _has_table(con, table):
                continue
            for r in con.execute(
                f"SELECT provider, COUNT(*) AS n FROM {table}"
                f" WHERE provider IS NOT NULL GROUP BY provider ORDER BY n DESC"
            ):
                if r["provider"] == "unknown":
                    continue
                if _endpoint(con, r["provider"])[0] is None:
                    add("unregistered_endpoint", "medium",
                        f"{table}: provider {r['provider']!r} ({r['n']} rows) is not a registered "
                        f"endpoint and has no endpoint alias",
                        table=table, provider=r["provider"], rows=r["n"])

        # 9. two DIFFERENT served ids on the SAME endpoint that differ only by
        # case/punctuation — the drift class. The same string on two endpoints is not
        # drift: one model is legitimately served from several places.
        seen: dict[tuple, set] = {}
        for r in con.execute("SELECT e.slug AS endpoint, d.served_id FROM mi_deployments d"
                             " JOIN mi_endpoints e ON e.endpoint_id = d.endpoint_id"):
            seen.setdefault((r["endpoint"], normalise_model_string(r["served_id"])), set()).add(
                r["served_id"]
            )
        for (endpoint, norm), members in seen.items():
            if len(members) > 1:
                add("near_duplicate_served_ids", "medium",
                    f"on {endpoint}: {sorted(members)} normalise to the same string "
                    f"({norm!r}) — one of them is a spelling variant",
                    endpoint=endpoint, normalised=norm, members=sorted(members))

        # 7. active models that no chain references (or vice versa)
        chain_models: set = set()
        raw = con.execute(
            "SELECT value FROM settings WHERE key = 'gateway_config:profiles'"
        ).fetchone()
        if raw:
            import json

            try:
                profiles = json.loads(raw["value"])
            except Exception:
                profiles = {}
            for prof in (profiles or {}).values():
                for step in (prof.get("chain") or []):
                    if isinstance(step, dict) and step.get("model"):
                        chain_models.add(step["model"])
        for m in list_logical(con=con):
            if m["status"] != "active":
                continue
            if m["deployments"] == 0:
                add("model_without_deployment", "medium",
                    f"active model {m['slug']!r} has no deployment", model=m["slug"])

        return findings
    finally:
        if own:
            con.close()


def _self_test() -> None:  # pragma: no cover - manual smoke aid
    con = connect(ro=True)
    for ep, raw in (("local-sparks", "DeepSeek-V4.1-Flash-1017"),
                    ("deepseek", "deepseek-v4-flash"),
                    ("deepseek", "deepseek-flash"),
                    ("local-sparks", "glm-5.3-flash-exl3"),
                    ("opencode", "ox-alpha-free")):
        r = resolve(ep, raw, con=con)
        print(f"{ep:14} {raw:30} -> {r.model} [{r.matched_by}] {r.release}")
    for f in reconcile(con=con):
        print(" ", f["severity"], f["kind"], f["detail"])
    con.close()


if __name__ == "__main__":  # pragma: no cover
    _self_test()
