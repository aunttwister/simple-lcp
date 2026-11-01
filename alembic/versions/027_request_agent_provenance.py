"""Agent provenance: record WHO drove a request, not just which profile it used.

Additive, nullable and reversible. No existing column is read differently, no row is
rewritten, and every new column is NULL until the write path stamps it — so a row from
before this migration stays honestly "unknown", never "not an agent".

Why
---
``/l1``, ``/l2`` and the other profile routes are shared: the operator chatting through a
client and an autonomous agent turn look identical to the meter, because the only thing
distinguishing them was which profile name was in the path — and a person can use the same
profile an agent does. The caller says who it is with two headers
(``X-Agent-Generated: true`` / ``X-Agent-Identity: hermes/<profile>``); this migration gives
that claim somewhere to land.

Shape
-----
* ``requests.agent_generated``  — 1 when the caller declared itself an agent, 0 when it
  explicitly declared it is not, NULL when it said nothing. Three states on purpose:
  "unknown" and "human" are different facts, and collapsing NULL into 0 would let an
  unlabelled request be counted as a person's.
* ``requests.agent_identity``   — the caller's own name for itself, as sent. Free text, not
  a foreign key: it arrives from the wire and must never fail a request.
* ``conversations.*``           — the same pair on the rollup, so the Conversations view can
  show the actor without joining ``requests``.

``conversations`` is created by ``work_conversations.ensure_schema()`` rather than by an
earlier revision, so the columns are added with a guard: a database built without that table
must still upgrade cleanly.

Revision ID: 027
Revises: 026
Create Date: 2026-11-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "027"
down_revision: Union[str, None] = "026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_REQUESTS_COLUMNS = (
    ("agent_generated", sa.Integer()),
    ("agent_identity", sa.String()),
)

NEW_CONVERSATIONS_COLUMNS = (
    ("agent_generated", sa.Integer()),
    ("agent_identity", sa.String()),
)


def _columns(table: str) -> set:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return {r[1] for r in rows}


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table},
    ).fetchone()
    return row is not None


def upgrade() -> None:
    if _table_exists("requests"):
        have = _columns("requests")
        for name, type_ in NEW_REQUESTS_COLUMNS:
            if name not in have:
                op.add_column("requests", sa.Column(name, type_, nullable=True))
    if _table_exists("conversations"):
        have = _columns("conversations")
        for name, type_ in NEW_CONVERSATIONS_COLUMNS:
            if name not in have:
                op.add_column("conversations", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    # SQLite cannot drop a column without a table rebuild, and these are additive with no
    # reader that requires them, so the downgrade deliberately leaves them in place rather
    # than risk a rebuild of the live cost table. Recorded here so it is a decision, not a
    # surprise: rolling back the code leaves two unread nullable columns behind.
    pass
