"""Change-history audit for the LCP control plane (Phase 5, CUR-219).

The gap this closes, measured 2026-10-18: traffic history is excellent
(107k requests, 1.4M messages, full tool-call accounting) but CHANGE history
did not exist — ``config_saved`` (config.py) fired without section, actor, or
diff, ``audit_logs`` had 0 rows, and the ``AuditLog`` model had been deleted.

Design:

1. **No migration.** The ``audit_logs`` table already exists on both lines
   (prod alembic 024, converged staging). Its ``user_id`` is an INTEGER FK to
   the still-empty ``users`` table, so the actor string rides in ``detail``
   JSON instead — compatible everywhere, and Phase 3 (users/teams) can later
   start writing real FKs without a read path change.

2. **Best-effort, never blocking.** An audit write failure logs a warning and
   returns False; it must never 500 a mutation that already succeeded.

3. **Redaction at the door.** Any dict key matching
   key/secret/token/password/credential/authorization (case-insensitive) is
   dropped from ``detail`` before it reaches disk. Handlers pass object names
   and sections, never credentials — this is the safety net if one slips.

4. **Actor from the verified session.** ``actor_from_handler`` re-verifies the
   ``lcp_admin`` cookie (cheap HMAC) and returns its ``sub`` — ``token`` for
   the break-glass login, ``oidc:<username>`` for SSO, ``bearer`` for script
   access. The guard has already run by the time any mutating handler
   executes, so an unauditable request cannot reach these lines.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .logging_config import get_logger

logger = get_logger("lcp.audit")

_SECRET_KEY_PARTS = ("key", "secret", "token", "password", "credential",
                     "authorization")


def _redact(value: Any) -> Any:
    """Drop anything that smells like a credential from a detail structure."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and any(p in k.lower() for p in _SECRET_KEY_PARTS):
                out[k] = "[REDACTED]"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def record(action: str, actor: str = "anonymous", detail: Any = None,
           ip: str | None = None, data_dir: str = "data") -> bool:
    """Append one change-history row. Returns True on success.

    Best-effort by design: a failed audit write is a logged warning, never an
    exception into a handler whose mutation already succeeded.
    """
    body = {
        "actor": actor,
        "detail": _redact(detail) if detail is not None else {},
    }
    try:
        conn = sqlite3.connect(f"file:{data_dir}/costs.db", timeout=5)
        try:
            conn.execute(
                "INSERT INTO audit_logs (timestamp, user_id, team_id, action,"
                " detail, ip_address) VALUES (?, NULL, NULL, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 action, json.dumps(body, separators=(",", ":")), ip),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:  # noqa: BLE001 — see docstring, this must not raise
        logger.warning("audit_write_failed", action=action, actor=actor)
        return False


def count(data_dir: str = "data") -> int:
    """Total rows in audit_logs — drives the change-history pager."""
    conn = sqlite3.connect(f"file:{data_dir}/costs.db?mode=ro", uri=True,
                           timeout=5)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0])
    finally:
        conn.close()


def recent(limit: int = 200, data_dir: str = "data",
           offset: int = 0) -> list[dict[str, Any]]:
    """Newest-first audit trail for GET /api/audit (guarded)."""
    try:
        conn = sqlite3.connect(f"file:{data_dir}/costs.db?mode=ro", uri=True,
                               timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, timestamp, action, detail, ip_address"
                " FROM audit_logs ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, max(0, int(offset))),
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d["detail"] or "{}")
            except (ValueError, TypeError):
                pass
            out.append(d)
        return out
    except Exception:  # noqa: BLE001
        logger.warning("audit_read_failed")
        return []


def actor_from_handler(handler: Any, data_dir: str = "data") -> str:
    """Best-effort actor string from the request that mutated something.

    The admin guard has already admitted this request; this only attributes
    it. Anything undecidable becomes "authenticated" rather than a crash.
    """
    from . import admin_auth
    cookie_header = handler.headers.get("Cookie", "") or ""
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == admin_auth.COOKIE_NAME and value:
            payload = admin_auth.verify_cookie(value, data_dir)
            if payload and payload.get("sub"):
                return str(payload["sub"])
    auth_header = handler.headers.get("Authorization", "") or ""
    if auth_header.lower().startswith("bearer "):
        if admin_auth.token_matches(auth_header[7:].strip()):
            return "bearer"
    return "authenticated"


class AuditEndpoints:
    """Mixin providing ``self._audit(action, **detail)`` to all handlers."""

    def _audit(self, action: str, **detail: Any) -> None:
        """One-line change attribution from any mutating handler."""
        try:
            record(
                action,
                actor=actor_from_handler(self, data_dir="data"),
                detail=detail or None,
                ip=self.client_address[0] if getattr(self, "client_address", None) else None,
            )
        except Exception:  # noqa: BLE001 — same doctrine as record()
            logger.warning("audit_call_failed", action=action)
