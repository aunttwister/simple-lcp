"""Strip ``routing_decisions.conversation_json`` (simplify-lcp M7).

``conversation_json`` held a shape-preserving, content-trimmed copy of the
request messages, written on every routing decision. Measured 2026-09-23 on
the staging DB: **101,309,852 bytes across 27,969 rows — 73% of a 139 MB
``costs.db``**, all of it duplicating what the harness's own session stores
already hold.

What replaces it, and why nothing is lost:

* ``intent_text`` (10 MB, kept) already carries the text that actually drove
  the classification, capped at 500 chars.
* ``conversation_id`` (kept, backfilled since the conversation-view work) is
  the correlation key joining a decision to its requests.
* ``conversations`` (kept) holds the derived per-conversation name/summary, so
  the conversation log view no longer needs to re-derive a head from raw
  messages.

Dropping the column in SQLite rewrites the table and releases the pages to the
freelist; the file itself only shrinks on ``VACUUM`` (run once, manually, after
the upgrade).

Revision ID: 022
Revises: 021
Create Date: 2026-09-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "routing_decisions"
_COLUMN = "conversation_json"


def _has_column(table: str, column: str) -> bool:
    """Return True when *column* exists on *table*.

    ``src.main`` runs ``Base.metadata.create_all(engine)`` at boot, so a fresh
    DB built from the new models never has this column — dropping it
    unguarded would abort ``alembic upgrade head`` and stop the container.
    """
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if not _has_column(_TABLE, _COLUMN):
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)


def downgrade() -> None:
    if _has_column(_TABLE, _COLUMN):
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column(_COLUMN, sa.Text(), nullable=True))
