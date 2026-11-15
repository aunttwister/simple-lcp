"""Quantisation modelled as its own entity, referenced by the served artifact.

Operator decision (2026-11-15): a quant is a property of the SERVED ARTIFACT, and it
is modelled INDEPENDENTLY of the model. So a canonical model is quant-free — it names
the model — and ``mi_quants`` names the quantisation, with ``mi_deployments.quant_id``
pointing at it. A deployment that serves full precision simply has no quant.

This mirrors the name layer deliberately: ``mi_deployments.quantisation`` keeps the
*observed* string as evidence (what the engine reported), and ``quant_id`` is the
*resolved* entity. Raw observation plus resolved reference, the same pattern as
``mi_model_names``.

Why the quant cannot live on the model: capability data is measured per MODEL, not per
quantisation. If a quant were part of the model's identity, one evaluation could not
cover two quants of the same model and every quant would need its own duplicated
capability rows. Routing selects on capability; the quant is how the model happens to
be served — an ops choice about VRAM and speed, not a quality tier.

Backfill:
  * every distinct non-empty ``mi_deployments.quantisation`` becomes a ``mi_quants``
    row, and its deployments are linked to it;
  * ``qwen3.6-27b-q4_k_m`` -> ``qwen3.6-27b``: the quant leaves the model's identity
    (its ``mi_models.quantization`` is cleared, and the quant lives on the deployment
    that serves it).

``bits`` is only populated where the observed string states it, never inferred.
"""

from alembic import op
import re
import sqlalchemy as sa
from datetime import datetime, timezone

revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


# slug -> (label, family). Only what the estate actually reports; bits is parsed
# from the string when stated, never guessed.
KNOWN = {
    "q4_k_m": ("Q4_K_M", "gguf"),
    "exl3": ("EXL3", "exllama"),
    "exl3-eaqv1-2.9bpw": ("EXL3 EAQV1 2.9bpw", "exllama"),
}

# Models whose NAME carried a quant; the quant belongs to the deployment instead.
MODEL_RENAMES = [("qwen3.6-27b-q4_k_m", "qwen3.6-27b")]

# Tokens that mark a trailing parenthetical in a display name as a quantisation.
# A parenthetical without one of these is left alone — "Qwen3.6-27B (Preview)" is
# part of the model's name, not a quant.
_QUANT_MARKERS = ("q4_k_m", "q5_k_m", "q6_k", "q8_0", "q3_k", "gguf", "awq",
                  "gptq", "exl3", "fp8", "bpw")


def _strip_quant_label(name):
    """``Qwen3.6-27B (q4_k_m)`` -> ``Qwen3.6-27B``. None when nothing is stripped."""
    if not name:
        return None
    m = re.search(r"\s*\(([^)]*)\)\s*$", name)
    if not m:
        return None
    if not any(t in m.group(1).lower() for t in _QUANT_MARKERS):
        return None
    return name[:m.start()].strip()


def _bits_from(slug: str):
    """``exl3-eaqv1-2.9bpw`` -> 2.9. Returns None when the string states no bits."""
    if "bpw" not in slug:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*bpw", slug)
    return float(m.group(1)) if m else None


def upgrade() -> None:
    conn = op.get_bind()
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS mi_quants (
            quant_id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL,
            label TEXT NOT NULL,
            family TEXT,
            bits REAL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            UNIQUE (slug)
        )"""))

    cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(mi_deployments)"))}
    if "quant_id" not in cols:
        conn.execute(sa.text(
            "ALTER TABLE mi_deployments ADD COLUMN quant_id INTEGER "
            "REFERENCES mi_quants(quant_id)"))

    # ── every observed quantisation string becomes an entity ────────────────
    observed = [r[0] for r in conn.execute(sa.text(
        "SELECT DISTINCT quantisation FROM mi_deployments "
        "WHERE quantisation IS NOT NULL AND TRIM(quantisation) <> ''"))]
    for raw in observed:
        slug = raw.strip().lower()
        label, family = KNOWN.get(slug, (raw.strip().upper(), None))
        conn.execute(sa.text(
            "INSERT OR IGNORE INTO mi_quants (slug, label, family, bits, status, created_at)"
            " VALUES (:s, :l, :f, :b, 'active', :c)"),
            {"s": slug, "l": label, "f": family, "b": _bits_from(slug), "c": now})
        conn.execute(sa.text(
            "UPDATE mi_deployments SET quant_id ="
            " (SELECT quant_id FROM mi_quants WHERE slug = :s)"
            " WHERE LOWER(TRIM(quantisation)) = :s"), {"s": slug})

    # ── the quant leaves the model's identity ───────────────────────────────
    for old, new in MODEL_RENAMES:
        conn.execute(sa.text("UPDATE mi_models SET slug = :n WHERE slug = :o"),
                     {"n": new, "o": old})
        conn.execute(sa.text("UPDATE mi_model_names SET name = :n WHERE name = :o"),
                     {"n": new, "o": old})
        conn.execute(sa.text("UPDATE model_capabilities SET model = :n WHERE model = :o"),
                     {"n": new, "o": old})
        conn.execute(sa.text("UPDATE mi_models SET quantization = NULL WHERE slug = :n"),
                     {"n": new})

    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_mi_deploy_quant ON mi_deployments (quant_id)"))

    # ── the display name carried the quant too ──────────────────────────────
    # "Qwen3.6-27B (q4_k_m)" attaches the quant to the MODEL, which is exactly what
    # this migration removes. Only a trailing parenthetical naming a quant is
    # stripped; "(Preview)" and friends are part of a model's name and stay.
    for mid, name in [(r[0], r[1]) for r in conn.execute(sa.text(
            "SELECT model_id, display_name FROM mi_models WHERE display_name IS NOT NULL"))]:
        cleaned = _strip_quant_label(name)
        if cleaned:
            conn.execute(sa.text("UPDATE mi_models SET display_name = :d WHERE model_id = :i"),
                         {"d": cleaned, "i": mid})


def downgrade() -> None:
    conn = op.get_bind()

    for old, new in MODEL_RENAMES:
        # restore the name, and put the quant back on the model it was taken from
        quant = conn.execute(sa.text(
            "SELECT q.slug FROM mi_deployments d JOIN mi_quants q ON q.quant_id = d.quant_id"
            " WHERE d.served_id LIKE '%q4_k_m%' LIMIT 1")).fetchone()
        conn.execute(sa.text("UPDATE mi_models SET slug = :o WHERE slug = :n"),
                     {"n": new, "o": old})
        conn.execute(sa.text("UPDATE mi_model_names SET name = :o WHERE name = :n"),
                     {"n": new, "o": old})
        conn.execute(sa.text("UPDATE model_capabilities SET model = :o WHERE model = :n"),
                     {"n": new, "o": old})
        if quant:
            conn.execute(sa.text("UPDATE mi_models SET quantization = :q WHERE slug = :o"),
                         {"q": quant[0], "o": old})

    # restore the quant parenthetical where a deployment still records a quant.
    #
    # This derives the label from the deployment rather than remembering the previous
    # wording, so a model that was previously unlabelled but IS served quantised gets a
    # label back too. That label is true (the deployment record says so); what is not
    # preserved is the exact original text. Recording the originals would need a
    # persisted side table for a cosmetic label, which is not worth the schema.
    for mid, name, slug in [(r[0], r[1], r[2]) for r in conn.execute(sa.text(
            "SELECT m.model_id, m.display_name, q.slug FROM mi_models m"
            " JOIN mi_deployments d ON d.model_id = m.model_id"
            " JOIN mi_quants q ON q.quant_id = d.quant_id"
            " WHERE m.display_name IS NOT NULL GROUP BY m.model_id"))]:
        if name and "(" not in name:
            conn.execute(sa.text("UPDATE mi_models SET display_name = :d WHERE model_id = :i"),
                         {"d": f"{name} ({slug})", "i": mid})

    conn.execute(sa.text("DROP INDEX IF EXISTS ix_mi_deploy_quant"))
    cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(mi_deployments)"))}
    if "quant_id" in cols:
        conn.execute(sa.text("ALTER TABLE mi_deployments DROP COLUMN quant_id"))
    conn.execute(sa.text("DROP TABLE IF EXISTS mi_quants"))
