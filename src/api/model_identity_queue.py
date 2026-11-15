"""Identity promotion queue: observed spellings that reach no canonical model.

Why
---
The registry holds 21 canonical slugs. The observed universe is 424 LMArena names plus
85 polled served ids. ``capabilities_for()`` only searches spellings that resolve to a
canonical model (29 normalised forms today), so capability rows keyed by any OTHER
spelling are unreachable — they exist in the table and no read path can ever find them.

That is what the first LMArena ingest did: it wrote unmatched arena display names
("Claude Opus 4.6") straight into ``model_capabilities``. Those rows are a dead
namespace, and the polled models appear to have no capabilities precisely because
nothing links them.

The rule this table enforces
----------------------------
Writing a capability row requires a spelling that ``capabilities_for()`` searches:
a canonical slug, a declared alias, a served id, or a benchmark key. Nothing else is
stored as capability data. An observation that resolves to none of those lands HERE,
where it is visible, deduplicated, and cheap to promote.

Promotion is explicit and is the only way an observed name reaches routing: accepting
an entry writes an ``mi_aliases`` row (the existing, authoritative bridge), after which
``capabilities_for()`` finds the capability rows and the alias is available to
``resolve()``. Rejecting records the decision so the same name is not offered again.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .model_identity import connect as mi_connect
from .model_identity import normalise_model_string

#: Where an observation came from.
SOURCE_LMARENA = "lmarena"
SOURCE_PROVIDER_CATALOG = "provider_catalog"

#: What kind of thing was observed.
KIND_CAPABILITY_NAME = "capability_name"
KIND_SERVED_ID = "served_id"

PENDING = "pending"
ACCEPTED = "accepted"
REJECTED = "rejected"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loose(raw: str) -> str:
    """A lookup key loose enough to compare spellings, strict enough to stay honest.

    Strips a variant/parenthesis suffix ("(High)", "(20250514)"), a vendor prefix and a
    quantisation suffix, then collapses all separators to single spaces — so
    ``xai/gpt-5.5``, ``GPT 5.5`` and ``gpt-5.5`` share a key, while ``claude opus 4.6``
    and ``claude opus 5`` stay distinct. Used for LOOKUP only; nothing is stored in this
    form, because the provider's real id is the thing worth keeping.
    """
    s = (raw or "").strip()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = s.split("/")[-1]
    s = normalise_model_string(s)
    s = re.sub(r"[^a-z0-9.]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def resolve_canonical(con: sqlite3.Connection, raw: str) -> Optional[str]:
    """The canonical slug a spelling reaches, using the registry's own routes.

    Mirrors ``capabilities_for``'s key set: slug, benchmark_key, declared alias, served
    id — compared on the loose form. Returns None when nothing matches, which is the
    queue's signal. Deliberately no fuzzy/prefix guessing: a wrong link fabricates
    capability data, which is worse than a missing one.
    """
    n = _loose(raw)
    if not n:
        return None
    for r in con.execute("""
        SELECT m.slug AS slug, m.slug AS k FROM mi_models m
        UNION SELECT m.slug, m.benchmark_key FROM mi_models m WHERE m.benchmark_key IS NOT NULL
        UNION SELECT m.slug, a.alias FROM mi_aliases a JOIN mi_models m ON m.model_id = a.model_id
        UNION SELECT m.slug, d.served_id FROM mi_deployments d JOIN mi_models m ON m.model_id = d.model_id
    """):
        if _loose(r["k"] or "") == n:
            return r["slug"]
    return None


def suggest(con: sqlite3.Connection, raw: str) -> Optional[dict]:
    """A best-effort SUGGESTION for the operator to confirm — never applied by itself.

    Token-prefix containment only, and the caller must present it as a guess: the
    prefix rule maps "Claude Opus 4.6" onto "claude-opus-5", which is a different model.
    Returned with its method so a review surface can label it as unverified.
    """
    n = _loose(raw)
    toks = n.split()
    if len(toks) < 2:
        return None
    for r in con.execute("SELECT slug FROM mi_models"):
        kt = _loose(r["slug"]).split()
        if len(kt) >= 2 and kt[:2] == toks[:2]:
            return {"slug": r["slug"], "method": "prefix2",
                    "warning": "unverified prefix guess — confirm the generation"}
    return None


def observe(con: sqlite3.Connection, raw: str, *, source: str, kind: str,
            evidence: Optional[dict] = None) -> dict:
    """Record one observation. Returns {status, canonical?, queued?}.

    A spelling that reaches a canonical model is returned as resolved and NOT queued.
    Otherwise it is upserted into the queue, where repeats bump ``occurrences`` and
    merge evidence rather than creating duplicate rows.
    """
    canonical = resolve_canonical(con, raw)
    if canonical:
        return {"raw": raw, "status": "resolved", "canonical": canonical}
    n = _loose(raw)
    if not n:
        return {"raw": raw, "status": "ignored", "reason": "empty spelling"}
    now = _now()
    existing = con.execute(
        "SELECT id, evidence, occurrences FROM mi_identity_queue WHERE norm=? AND source=?",
        (n, source)).fetchone()
    if existing:
        merged = _merge_evidence(existing["evidence"], evidence)
        con.execute(
            "UPDATE mi_identity_queue SET last_seen=?, occurrences=occurrences+1,"
            " evidence=? WHERE id=?",
            (now, json.dumps(merged), existing["id"]))
        return {"raw": raw, "status": "queued", "queue_id": existing["id"], "repeat": True}
    sug = suggest(con, raw)
    cur = con.execute(
        "INSERT INTO mi_identity_queue (raw, norm, source, kind, evidence, suggested_slug,"
        " suggested_by, status, first_seen, last_seen, occurrences)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,1)",
        (raw, n, source, kind, json.dumps(evidence or {}),
         (sug or {}).get("slug"), (sug or {}).get("method"), PENDING, now, now))
    return {"raw": raw, "status": "queued", "queue_id": cur.lastrowid,
            "suggested": sug}


def _merge_evidence(old: Optional[str], new: Optional[dict]) -> dict:
    try:
        base = json.loads(old) if old else {}
    except Exception:  # noqa: BLE001
        base = {}
    for k, v in (new or {}).items():
        if isinstance(v, (int, float)) and isinstance(base.get(k), (int, float)):
            base[k] = base[k] + v
        elif isinstance(v, list) and isinstance(base.get(k), list):
            base[k] = sorted(set(base[k]) | set(v))
        else:
            base[k] = v
    return base


def list_pending(con: Optional[sqlite3.Connection] = None, *, status: str = PENDING,
                 limit: int = 200) -> list:
    own = con is None
    c = mi_connect(None) if own else con  # type: ignore[assignment]
    try:
        return [dict(r) for r in c.execute(
            "SELECT * FROM mi_identity_queue WHERE status=?"
            " ORDER BY occurrences DESC, raw LIMIT ?", (status, limit))]
    finally:
        if own:
            c.close()


def accept(con: sqlite3.Connection, queue_id: int, *, slug: str, note: str = "") -> dict:
    """Promote an observation to a canonical model by writing the alias.

    The alias is the authoritative bridge the read path already honours, so this is
    what makes previously-unreachable capability rows findable. Also re-keys any
    capability rows already stored under the raw spelling, so nothing is left behind.
    """
    q = con.execute("SELECT * FROM mi_identity_queue WHERE id=?", (queue_id,)).fetchone()
    if q is None:
        raise KeyError(f"no queue entry {queue_id}")
    m = con.execute("SELECT model_id FROM mi_models WHERE slug=?", (slug,)).fetchone()
    if m is None:
        raise KeyError(f"no canonical model {slug!r}")
    now = _now()
    cur = con.cursor()
    # Idempotent: a retry (double-click, network replay) must not stack duplicate
    # aliases onto the same model — the alias is what resolve() reads, so duplicates
    # are noise that makes the identity look more heavily attested than it is.
    dupe = cur.execute(
        "SELECT 1 FROM mi_aliases WHERE alias=? AND model_id=?",
        (q["raw"], m["model_id"])).fetchone()
    if not dupe:
        cur.execute(
            "INSERT INTO mi_aliases (endpoint_id, alias, model_id, source, valid_from, notes)"
            " VALUES (NULL, ?, ?, 'identity_promotion', ?, ?)",
            (q["raw"], m["model_id"], now, note or f"promoted from {q['source']}"))
    moved = 0
    if q["kind"] == KIND_CAPABILITY_NAME:
        moved = cur.execute(
            "UPDATE OR REPLACE model_capabilities SET model=? WHERE model=?",
            (slug, q["raw"])).rowcount
    cur.execute(
        "UPDATE mi_identity_queue SET status=?, decided_at=?, note=? WHERE id=?",
        (ACCEPTED, now, note, queue_id))
    con.commit()
    return {"queue_id": queue_id, "raw": q["raw"], "slug": slug,
            "alias_created": not dupe, "capability_rows_rekeyed": moved}


def reject(con: sqlite3.Connection, queue_id: int, *, note: str = "") -> dict:
    """Record 'this spelling should not be linked'. It is not offered again."""
    now = _now()
    cur = con.execute(
        "UPDATE mi_identity_queue SET status=?, decided_at=?, note=? WHERE id=?",
        (REJECTED, now, note, queue_id))
    if cur.rowcount == 0:
        raise KeyError(f"no queue entry {queue_id}")
    con.commit()
    return {"queue_id": queue_id, "status": REJECTED}


def purge_raw_capability_rows(con: sqlite3.Connection) -> dict:
    """Move capability rows keyed by an unreachable spelling out of the table.

    Every row whose ``model`` key no read path can produce is a dead row: it inflates
    counts, invite false confidence, and cannot be found by ``capabilities_for``. Rows
    whose spelling resolves are re-keyed onto the canonical slug; the rest are removed
    (their spellings are already in the queue). Reversible: the removed set is returned.
    """
    rows = con.execute("SELECT DISTINCT model FROM model_capabilities").fetchall()
    rekeyed, removed, removed_keys = 0, 0, []
    cur = con.cursor()
    for r in rows:
        key = r["model"]
        canon = resolve_canonical(con, key)
        if canon and canon != key:
            rekeyed += cur.execute(
                "UPDATE OR REPLACE model_capabilities SET model=? WHERE model=?",
                (canon, key)).rowcount
        elif not canon:
            removed += cur.execute(
                "DELETE FROM model_capabilities WHERE model=?", (key,)).rowcount
            removed_keys.append(key)
    con.commit()
    return {"rekeyed_rows": rekeyed, "removed_rows": removed,
            "removed_keys": sorted(removed_keys)}
