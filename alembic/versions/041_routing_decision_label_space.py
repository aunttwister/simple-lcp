"""routing_decisions: the label space that decided the request (alembic 041)

Arena routing assigns a BOARD as the intent (design doc §15 Q1, decided 2026-11-17).
`task` alone cannot carry that afterwards: the task taxonomy and the board label space
are different vocabularies, and a request routed under one is not explainable by the
other. `base_intent` is the assigned label regardless of space; `arena_board` names the
LCP board whose ranking was walked, only when the Arena tier produced the decision.

Additive and nullable on purpose: every existing row keeps meaning, and the column is
absent-valued rather than defaulted for rows that predate Arena routing. No default is
set — '' would read as "classified as the empty label", which is a different fact.

Revision ID: 041
Revises: 040
"""
from alembic import op
import sqlalchemy as sa

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None

_TABLE = "routing_decisions"
_COLUMNS = ("base_intent", "arena_board")


def _raw_sqlite(conn) -> bool:
    """True when *conn* is a bare sqlite3 connection rather than an alembic one.

    Both are real: alembic runs this at container startup with a SQLAlchemy Connection
    (which REQUIRES ``sa.text(...)`` for raw SQL and raises
    ObjectNotExecutableError on a plain string), and the test suite runs it against a
    ``sqlite3.Connection`` (which requires a plain string and cannot take a TextClause).
    Supporting only one of them is a crash loop on startup, which is exactly what the
    first version of this file did. Measured 2026-11-17.
    """
    try:
        import sqlite3
        return isinstance(conn, sqlite3.Connection)
    except Exception:  # noqa: BLE001
        return False


def _execute(conn, sql, params=()):
    """Execute raw SQL on either connection type."""
    if _raw_sqlite(conn):
        return conn.execute(sql, params)
    return conn.execute(sa.text(sql), params)


def _existing(conn) -> set:
    """Columns already on the table."""
    rows = _execute(conn, f"PRAGMA table_info({_TABLE})").fetchall()
    return {r[1] for r in rows}


def upgrade(conn=None):
    """Add the two columns. Idempotent: an upgrade may be retried after a crash."""
    conn = conn or op.get_bind()
    have = _existing(conn)
    for col in _COLUMNS:
        if col not in have:
            _execute(conn, f"ALTER TABLE {_TABLE} ADD COLUMN {col} VARCHAR")


def downgrade(conn=None):
    conn = conn or op.get_bind()
    have = _existing(conn)
    for col in _COLUMNS:
        if col in have:
            _execute(conn, f"ALTER TABLE {_TABLE} DROP COLUMN {col}")
