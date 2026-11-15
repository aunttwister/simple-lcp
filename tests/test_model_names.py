"""Unit tests for the model-name normalisation tables and their accessors.

This suite pins the design the operator asked for: one canonical model table, one
source ("model data source") table, and one join table that maps *a source's spelling*
onto *a canonical model* — replacing four unlinked places (a JSON blob on
``model_registry``, ``mi_aliases``, ``mi_deployments.served_id``, and the free-text keys
in ``model_capabilities``).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _model_names_fixtures import (  # noqa: E402
    add_deployment,
    add_endpoint,
    add_model,
    add_name,
    add_source,
    mi_schema,
)

from src.api import model_names  # noqa: E402
from src.api import model_identity  # noqa: E402


@pytest.fixture
def db(tmp_path):
    """A file-backed DB, because the accessors open their own connection."""
    path = str(tmp_path / "costs.db")
    mi_schema(sqlite3.connect(path)).close()
    return path


@pytest.fixture
def con(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


# ── the shape: a source registry and one name table ─────────────────────────

def test_migrations_source_kinds_are_present(db, con):
    kinds = {r["slug"]: r["kind"] for r in model_names.sources(db)}
    assert kinds["canonical"] == "canonical"
    assert kinds["benchmark"] == "benchmark_arena"
    assert kinds["lmarena"] == "benchmark_arena"
    assert kinds["operator"] == "operator"


def test_a_provider_source_links_to_the_endpoint_it_names_for(db, con):
    add_endpoint(con, "commandcode")
    s = [x for x in model_names.sources(db) if x["slug"] == "commandcode"][0]
    assert s["kind"] == "provider_endpoint" and s["endpoint_id"] is not None


def test_one_source_cannot_use_a_name_for_two_models(db, con):
    """UNIQUE(source_id, name) — a collision would make resolution ambiguous."""
    add_endpoint(con, "prov-a")
    m1 = add_model(con, "model-one")
    m2 = add_model(con, "model-two")
    add_name(con, m1, "prov-a", "shared-name")
    with pytest.raises(sqlite3.IntegrityError):
        add_name(con, m2, "prov-a", "shared-name")


def test_two_sources_may_each_use_their_own_spelling_for_one_model(db, con):
    a = add_endpoint(con, "prov-a")
    b = add_endpoint(con, "prov-b")
    m = add_model(con, "deepseek-v4-pro")
    add_name(con, m, "prov-a", "deepseek-v4-pro")
    add_name(con, m, "prov-b", "deepseek/deepseek-v4-pro")
    assert model_names.provider_mappings_for(con, m) == {
        "prov-a": "deepseek-v4-pro", "prov-b": "deepseek/deepseek-v4-pro"}


# ── the old shapes still come out, so callers do not change ────────────────

def test_registry_entries_reproduces_the_provider_mappings_object(db, con):
    add_endpoint(con, "deepseek")
    add_endpoint(con, "commandcode")
    m = add_model(con, "deepseek-v4-pro", benchmark_key="deepseek-v4-pro",
                  active_release="2026-08-13", benchmark_release="2026-06-25")
    add_name(con, m, "deepseek", "deepseek-v4-pro")
    add_name(con, m, "commandcode", "deepseek/deepseek-v4-pro")
    entry = [e for e in model_names.registry_entries(db)
             if e["logical_name"] == "deepseek-v4-pro"][0]
    assert entry["provider_mappings"] == {
        "deepseek": "deepseek-v4-pro", "commandcode": "deepseek/deepseek-v4-pro"}
    assert entry["providers"] == ["commandcode", "deepseek"]
    assert entry["benchmark_key"] == "deepseek-v4-pro"
    assert entry["active_release"] == "2026-08-13"
    assert entry["benchmark_release"] == "2026-06-25"


def test_registry_entries_covers_every_canonical_model(db, con):
    """The old table held 16 of 21 models, so five were invisible to routing."""
    add_model(con, "alpha")
    add_model(con, "beta")
    assert {e["logical_name"] for e in model_names.registry_entries(db)} == {"alpha", "beta"}


# ── writes ─────────────────────────────────────────────────────────────────

def test_upsert_creates_then_updates(db, con):
    assert model_names.upsert_entry(
        db, logical_name="new-model", benchmark_key="new-model", provider_mappings={}
    ) == "created"
    assert model_names.upsert_entry(
        db, logical_name="new-model", benchmark_key="new-bench", provider_mappings={}
    ) == "updated"
    row = con.execute("SELECT benchmark_key FROM mi_models WHERE slug='new-model'").fetchone()
    assert row["benchmark_key"] == "new-bench"


def test_upsert_replaces_a_providers_previous_name_for_the_model(db, con):
    """A provider rename through the registry must not leave the old spelling
    resolvable — otherwise traffic keeps resolving to a name that no longer exists."""
    add_endpoint(con, "prov-a")
    model_names.upsert_entry(db, logical_name="m", benchmark_key="b",
                             provider_mappings={"prov-a": "old-name"})
    model_names.upsert_entry(db, logical_name="m", benchmark_key="b",
                             provider_mappings={"prov-a": "new-name"})
    assert model_names.provider_mappings_for(con, 1) == {"prov-a": "new-name"}
    assert con.execute("SELECT COUNT(*) FROM mi_model_names WHERE name='old-name'"
                       ).fetchone()[0] == 0


def test_upsert_records_the_slug_and_benchmark_key_as_names(db, con):
    model_names.upsert_entry(db, logical_name="slug-x", benchmark_key="bench-x",
                             provider_mappings={})
    names = {r["name"] for r in con.execute("SELECT name FROM mi_model_names").fetchall()}
    assert {"slug-x", "bench-x"} <= names


def test_conflicts_catches_a_duplicate_benchmark_key(db, con):
    add_model(con, "taken", benchmark_key="bk-1")
    reasons = model_names.conflicts(db, logical_name="other", benchmark_key="bk-1",
                                    provider_mappings={})
    assert reasons and "bk-1" in reasons[0]


def test_conflicts_catches_a_duplicate_provider_pair(db, con):
    add_endpoint(con, "prov-a")
    m = add_model(con, "taken")
    add_name(con, m, "prov-a", "the-model-id")
    reasons = model_names.conflicts(db, logical_name="other", benchmark_key="other-bk",
                                    provider_mappings={"prov-a": "the-model-id"})
    assert reasons and "prov-a" in reasons[0]


def test_conflicts_allows_updating_the_same_model(db, con):
    add_endpoint(con, "prov-a")
    m = add_model(con, "same", benchmark_key="bk")
    add_name(con, m, "prov-a", "mid")
    assert model_names.conflicts(db, logical_name="same", benchmark_key="bk",
                                 provider_mappings={"prov-a": "mid"}) == []


# ── lifecycle: remove / rename / retire ────────────────────────────────────

def test_remove_provider_keeps_the_model_and_reports_it_unserved(db, con):
    add_endpoint(con, "prov-a")
    m = add_model(con, "kept-model", benchmark_key="bk")
    add_name(con, m, "prov-a", "mid")
    changed, unserved = model_names.remove_provider(db, "prov-a")
    assert changed == 1 and unserved == 1, (
        "the model is kept — its benchmark identity still means something")
    assert con.execute("SELECT COUNT(*) FROM mi_models WHERE slug='kept-model'"
                       ).fetchone()[0] == 1
    assert model_names.provider_mappings_for(con, m) == {}


def test_remove_provider_leaves_other_providers_alone(db, con):
    add_endpoint(con, "prov-a")
    add_endpoint(con, "prov-b")
    m = add_model(con, "m")
    add_name(con, m, "prov-a", "a-name")
    add_name(con, m, "prov-b", "b-name")
    changed, unserved = model_names.remove_provider(db, "prov-a")
    assert changed == 1 and unserved == 0
    assert model_names.provider_mappings_for(con, m) == {"prov-b": "b-name"}


def test_rename_provider_moves_the_names_and_retires_the_old_source(db, con):
    add_endpoint(con, "old-name")
    add_endpoint(con, "new-name")
    m = add_model(con, "m")
    add_name(con, m, "old-name", "the-served-id")
    moved = model_names.rename_provider(db, "old-name", "new-name")
    assert moved == 1
    assert model_names.provider_mappings_for(con, m) == {"new-name": "the-served-id"}
    old = [s for s in model_names.sources(db) if s["slug"] == "old-name"][0]
    assert old["status"] == "retired", "the old spelling must stop being a live authority"
    assert old["names"] == 0


def test_delete_entry_retires_instead_of_destroying_history(db, con):
    m = add_model(con, "doomed", benchmark_key="bk")
    con.execute("INSERT INTO model_capabilities (model, task_type, score, source)"
                " VALUES ('bk','coding',90.0,'livebench')")
    con.commit()
    assert model_names.delete_entry(db, "doomed") is True
    assert con.execute("SELECT status FROM mi_models WHERE slug='doomed'"
                       ).fetchone()["status"] == "retired"
    assert con.execute("SELECT COUNT(*) FROM mi_model_names WHERE model_id=?",
                       (m,)).fetchone()[0] == 0, "no spelling may keep resolving"
    assert con.execute("SELECT COUNT(*) FROM model_capabilities").fetchone()[0] == 1, (
        "the scoring history is kept")


def test_delete_entry_reports_a_missing_model(db):
    assert model_names.delete_entry(db, "never-existed") is False


# ── integration: resolution and capabilities read the new tables ───────────

def test_a_provider_name_resolves_only_on_its_own_endpoint(con):
    """The same string is a different model on different endpoints, so provider
    names are scoped by the source's endpoint."""
    add_endpoint(con, "prov-a")
    add_endpoint(con, "prov-b")
    ma = add_model(con, "model-on-a")
    mb = add_model(con, "model-on-b")
    add_name(con, ma, "prov-a", "shared-spelling")
    add_name(con, mb, "prov-b", "shared-spelling")
    add_deployment(con, "prov-a", "a-served", "model-on-a")
    add_deployment(con, "prov-b", "b-served", "model-on-b")
    ra = model_identity.resolve("prov-a", "shared-spelling", con=con)
    rb = model_identity.resolve("prov-b", "shared-spelling", con=con)
    assert ra.model == "model-on-a" and rb.model == "model-on-b"


def test_an_operator_name_resolves_on_any_endpoint(con):
    add_endpoint(con, "prov-a")
    m = add_model(con, "global-model")
    add_name(con, m, "operator", "a-global-alias", verified=True)
    res = model_identity.resolve("prov-a", "a-global-alias", con=con)
    assert res.model == "global-model"


def test_a_provider_name_resolves_to_its_deployment(con):
    add_endpoint(con, "prov-a")
    m = add_model(con, "m")
    dep = add_deployment(con, "prov-a", "served-id", "m")
    add_name(con, m, "prov-a", "served-id")
    res = model_identity.resolve("prov-a", "served-id", con=con)
    assert res.deployment_id == dep and res.model == "m"


def test_capabilities_are_found_through_every_registered_name(con):
    """The whole point: a capability row keyed by a provider's spelling is reachable
    because that spelling is registered against the model."""
    add_endpoint(con, "prov-a")
    m = add_model(con, "m", benchmark_key="bench-key")
    add_name(con, m, "prov-a", "provider/spelling")
    con.execute("INSERT INTO model_capabilities (model, task_type, score, source)"
                " VALUES ('provider/spelling','coding',91.0,'lmarena')")
    con.execute("INSERT INTO model_capabilities (model, task_type, score, source)"
                " VALUES ('bench-key','coding',88.0,'livebench')")
    con.commit()
    rows = model_identity.capabilities_for("m", con=con)
    assert {r["source"] for r in rows} == {"lmarena", "livebench"}


def test_an_unregistered_capability_key_stays_visible_by_exact_match(con):
    """A row keyed by a name nobody registered must not silently vanish — it is
    visible to an exact-string lookup so it can be promoted."""
    m = add_model(con, "m")
    con.execute("INSERT INTO model_capabilities (model, task_type, score, source)"
                " VALUES ('m','coding',70.0,'manual')")
    con.commit()
    assert len(model_identity.capabilities_for("m", con=con)) == 1


def test_index_lookup_path_is_used_not_a_full_scan(con):
    """Resolution joins on name, so the name table must be indexed for it to scale
    past the ~430 observed spellings it already holds."""
    idx = {r[1] for r in con.execute("PRAGMA index_list(mi_model_names)").fetchall()}
    cols = {tuple(r[2] for r in con.execute(f"PRAGMA index_info('{i}')").fetchall())
            for i in idx}
    assert ("name",) in cols or ("name", "model_id") in cols, (
        "mi_model_names.name must be indexed — it is the resolution join key")
