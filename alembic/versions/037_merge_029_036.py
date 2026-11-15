"""Merge the two forked lineages: prod's 029 and staging's 036.

Why this revision exists
------------------------
Prod and staging share history up to revision **028**, then fork:

* ``028 -> 029_provider_types``  — prod (hard provider types: the CHECK-constrained
  ``provider_types`` table, read by the UI, the vendor registry and the alert policy).
* ``028 -> 030_model_intel_polls -> ... -> 036_quant_entity`` — staging
  (the model-intel poll layer, endpoint poll toggle, nullable staged-deployment model,
  identity promotion queue, model-name normalization, the drop of the superseded name
  tables, and quantisation as its own entity).

Two heads means neither side could ever take the other's code:

* a ``lcp`` (prod-tree) build refused to start against staging's DB with
  ``Can't locate revision identified by '036'`` — the build knows only up to 029;
* and prod's DB, stamped ``029``, has no path forward to 035, because ``029`` is not
  an ancestor of ``035`` — ``alembic upgrade head`` cannot linearise it on its own.

An alembic *merge* revision is the supported answer: it declares both heads as its
down-revisions, so a single head exists again and a database sitting on EITHER side
can upgrade to it. Nothing is applied here by design — the two lineages touch
different tables, so the merge carries no DDL of its own; it only re-joins the graph.

Ordering note: prod's DB at ``029`` will, on the next ``upgrade head``, run staging's
``030``-``036`` migrations; staging's DB at ``036`` will run ``029``. Both are the
intended convergence and must be exercised against a COPY of the DB before a cut.

Revision ID: 037
Revises: 029, 036
"""
revision = "037"
down_revision = ("029", "036")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No DDL: this revision only re-joins the two branches of the graph."""
    pass


def downgrade() -> None:
    """No DDL to undo.

    Alembic will refuse to ``downgrade`` across a merge revision without an explicit
    target branch, which is the correct behaviour — un-merging is a deliberate act,
    not something a routine downgrade should do silently.
    """
    pass
