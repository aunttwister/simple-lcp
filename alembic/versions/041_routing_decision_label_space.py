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


def _existing(conn) -> set:
    """Columns already on the table.

    The SQL is a plain string, not ``sa.text(...)``: this function is exercised both
    through alembic's Connection and, in tests, through a bare ``sqlite3.Connection``,
    and only the former understands a TextClause.
    """
    rows = conn.execute(f"PRAGMA table_info({_TABLE})").fetchall()
    return {r[1] for r in rows}


def upgrade(conn=None):
    """Add the two columns. Idempotent: an upgrade may be retried after a crash."""
    conn = conn or op.get_bind()
    have = _existing(conn)
    for col in _COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {col} VARCHAR")


def downgrade(conn=None):
    conn = conn or op.get_bind()
    have = _existing(conn)
    for col in _COLUMNS:
        if col in have:
            conn.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {col}")
