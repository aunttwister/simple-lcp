"""Quantisation is a property of the served artifact, modelled independently.

Operator decision (2026-11-15): a canonical model is quant-free; the quant lives on
``mi_quants`` and a served artifact references it. These tests pin the properties that
decision buys, and the failure modes a column-on-the-model would reintroduce.
"""
from __future__ import annotations

import pytest

from src.api import quants as q
from tests._model_names_fixtures import (
    add_deployment,
    add_model,
    add_endpoint,
    new_db,
)


@pytest.fixture()
def db(tmp_path):
    """A file-backed DB with the quant entity, so the accessors can open it."""
    import sqlite3
    from tests import _model_names_fixtures as fx

    path = str(tmp_path / "quants.db")
    con = sqlite3.connect(path)
    fx.mi_schema(con)
    con.close()
    return path


def test_ensure_quant_is_idempotent(db):
    """A re-run must not create a second row for the same quantisation."""
    con = q._connect(db)
    try:
        first = q.ensure_quant(con, "q4_k_m", family=q.FAMILY_GGUF)
        again = q.ensure_quant(con, "q4_k_m", family=q.FAMILY_GGUF)
        con.commit()
        assert first == again
        n = con.execute("SELECT COUNT(*) FROM mi_quants").fetchone()[0]
        assert n == 1
    finally:
        con.close()


def test_bits_are_parsed_but_never_invented(db):
    """A quant that states bits per weight records them; one that does not records none.

    ``exl3-eaqv1-2.9bpw`` states 2.9. ``q4_k_m`` does not state a bpw figure, so its
    bits stay NULL rather than being filled in with a remembered approximation — an
    absent measurement must stay visibly absent.
    """
    con = q._connect(db)
    try:
        q.ensure_quant(con, "exl3-eaqv1-2.9bpw")
        q.ensure_quant(con, "q4_k_m")
        con.commit()
        rows = {r["slug"]: r["bits"] for r in con.execute("SELECT slug, bits FROM mi_quants")}
        assert rows["exl3-eaqv1-2.9bpw"] == pytest.approx(2.9)
        assert rows["q4_k_m"] is None
    finally:
        con.close()


def test_a_deployment_carries_the_quant_and_the_model_does_not(db):
    """The served artifact references the quant; the model's identity stays clean."""
    con = q._connect(db)
    try:
        add_endpoint(con, "local-sparks")
        add_model(con, "glm-5.3-flash")
        dep = add_deployment(con, "local-sparks", "GLM-5.3-Flash-EXL3",
                             "glm-5.3-flash", quant="exl3",
                             observed_quant="exl3")
        con.commit()

        got = q.quant_for_deployment(con, dep)
        assert got is not None and got["slug"] == "exl3"

        # the model itself carries no quantisation, and no quant is derivable from it
        quant_col = con.execute(
            "SELECT quantization FROM mi_models WHERE slug='glm-5.3-flash'").fetchone()[0]
        assert quant_col is None

        # the engine's own report survives as evidence, unmodified by resolution
        observed = con.execute(
            "SELECT quantisation FROM mi_deployments WHERE deployment_id=?", (dep,)
        ).fetchone()[0]
        assert observed == "exl3"
    finally:
        con.close()


def test_full_precision_deployment_has_no_quant(db):
    """NULL means full precision — absence is a fact, not a missing value."""
    con = q._connect(db)
    try:
        add_endpoint(con, "commandcode")
        add_model(con, "claude-opus-5")
        dep = add_deployment(con, "commandcode", "claude-opus-5", "claude-opus-5")
        con.commit()
        assert q.quant_for_deployment(con, dep) is None
    finally:
        con.close()


def test_one_quant_serves_many_deployments(db):
    """The quant is shared, not duplicated per model — the point of the entity."""
    con = q._connect(db)
    try:
        add_endpoint(con, "llamacpp")
        add_endpoint(con, "local-sparks")
        add_model(con, "qwen3.8-27b-heretic")
        add_model(con, "glm-5.3-flash")
        add_deployment(con, "llamacpp", "qwen3.8-27b-q4_k_m-heretic",
                       "qwen3.8-27b-heretic", quant="q4_k_m", observed_quant="q4_k_m")
        add_deployment(con, "local-sparks", "glm-5.3-flash-q4_k_m",
                       "glm-5.3-flash", quant="q4_k_m", observed_quant="q4_k_m")
        con.commit()

        rows = q.quants(con)
        q4 = [r for r in rows if r["slug"] == "q4_k_m"][0]
        assert q4["deployments"] == 2

        deps = q.deployments_for_quant(con, "q4_k_m")
        assert len(deps) == 2
        assert {d["endpoint"] for d in deps} == {"llamacpp", "local-sparks"}
    finally:
        con.close()


def test_set_deployment_quant_records_and_clears(db):
    """Re-pointing a served artifact at a different quant, and back to full precision."""
    con = q._connect(db)
    try:
        add_endpoint(con, "local-sparks")
        add_model(con, "glm-5.3-flash")
        dep = add_deployment(con, "local-sparks", "GLM-5.3-Flash-EXL3", "glm-5.3-flash")
        con.commit()

        assert q.set_deployment_quant(con, dep, "exl3") is True
        assert q.quant_for_deployment(con, dep)["slug"] == "exl3"

        assert q.set_deployment_quant(con, dep, None) is True
        assert q.quant_for_deployment(con, dep) is None
    finally:
        con.close()


def test_quant_identity_drift_is_detectable(db):
    """A model whose NAME embeds a quantisation is the drift this design forbids."""
    con = q._connect(db)
    try:
        add_model(con, "qwen3.6-27b-q4_k_m")
        add_model(con, "qwen3.6-27b")
        con.commit()
        hits = q.quant_identity_is_clean(con)
        assert "qwen3.6-27b-q4_k_m" in hits
        assert "qwen3.6-27b" not in hits
    finally:
        con.close()


def test_accessors_are_safe_on_an_unmigrated_db(tmp_path):
    """A DB with no quant entity reads as 'no quants', not an exception.

    The same tolerance the name layer needs: the tables are alembic-owned, so a
    fixture-built database legitimately lacks them.
    """
    import sqlite3

    path = str(tmp_path / "bare.db")
    sqlite3.connect(path).close()
    assert q.quants(path) == []
    assert q.deployments_for_quant(path, "q4_k_m") == []


def test_two_quants_of_one_model_coexist_without_duplicating_the_model(db):
    """The reason the quant is not identity: one model, several quants, ONE model row.

    If a quant were part of the model's identity these deployments would be two
    models, and every capability row would have to be duplicated per quant — even
    though a benchmark measures the model, not the quantisation.
    """
    con = q._connect(db)
    try:
        add_endpoint(con, "llamacpp")
        add_endpoint(con, "local-sparks")
        add_model(con, "glm-5.3-flash")
        add_deployment(con, "llamacpp", "glm-5.3-flash-q4_k_m", "glm-5.3-flash",
                       quant="q4_k_m", observed_quant="q4_k_m")
        add_deployment(con, "local-sparks", "GLM-5.3-Flash-EXL3", "glm-5.3-flash",
                       quant="exl3", observed_quant="exl3")
        con.commit()

        model_rows = con.execute(
            "SELECT COUNT(*) FROM mi_models WHERE slug = 'glm-5.3-flash'").fetchone()[0]
        assert model_rows == 1, "two quants of one model must not become two models"

        dep_rows = con.execute(
            "SELECT COUNT(*) FROM mi_deployments WHERE model_id ="
            " (SELECT model_id FROM mi_models WHERE slug = 'glm-5.3-flash')").fetchone()[0]
        assert dep_rows == 2
        assert len(q.quants(con)) == 2, "each quant is its own entity"
    finally:
        con.close()
