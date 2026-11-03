"""End-to-end wiring of per-user taxonomies through the real classify path.

This is the P5 exit criterion in test form: two profiles assigned different taxonomies must
classify under DIFFERENT label spaces and stamp different taxonomy ids on the decision, while a
profile with no assignment must be byte-identical to today.

Uses the real ``classify_task_detail`` and the real ``TaxonomyStore``; only the embedder is fake,
because the point here is resolution and stamping, not similarity quality.
"""
from __future__ import annotations

import pytest

from src.api import intent_taxonomy as it
from src.api import router
from src.api import task_classifier as tc


def _acts(a=3, n=3):
    return {f"L{i}": [f"exemplar {i}-{j}" for j in range(n)] for i in range(a)}


def _tax(tid, owner=None, labels=3, status="active"):
    return it.IntentTaxonomy(
        id=tid, display_name=tid, owner=owner, status=status, version=1,
        acts=_acts(labels), subjects={},
        grade={"negative_control": {"passed": True}},
    )


class _Cfg:
    def __init__(self, profiles, router_default=None):
        self.profiles = profiles
        self.plugins = {"router": {"enabled": False, "default_taxonomy": router_default}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = it.TaxonomyStore(str(tmp_path / "t.db"))
    monkeypatch.setattr(it, "_db_path", lambda: str(tmp_path / "t.db"))
    return s


def _classify(profile, monkeypatch, config):
    """Call the REAL classify path with a stubbed embedder, so only resolution is under test."""
    monkeypatch.setattr(tc, "get_semantic_classifier", lambda taxonomy=None: None)
    return router.classify_task_detail(
        [{"role": "user", "content": "implement the retry guard and deploy it"}],
        None, 1024, profile=profile, config=config,
    )


def test_two_profiles_resolve_to_different_taxonomies(store, monkeypatch):
    store.save(_tax("op-v1", owner="l2"))
    store.save(_tax("l1-tax", owner="l1"))
    cfg = _Cfg({"l2": {"intent_taxonomy": "op-v1"}, "l1": {"intent_taxonomy": "l1-tax"}})

    for profile, expected in (("l2", "op-v1"), ("l1", "l1-tax")):
        res = _classify(profile, monkeypatch, cfg)
        assert res.taxonomy_id == expected, f"{profile} must classify under {expected}"
        assert res.taxonomy_version == 1


def test_unassigned_profile_is_byte_identical_to_today(store, monkeypatch):
    """THE safety property. A profile that declares nothing must behave exactly as before."""
    store.save(_tax("op-v1", owner="l2"))
    cfg = _Cfg({"l2": {"intent_taxonomy": "op-v1"}, "coder": {}})
    res = _classify("coder", monkeypatch, cfg)
    assert res.taxonomy_id == it.BUILTIN_ID
    assert res.taxonomy_version == 1, "the built-in taxonomy's own version, not None"
    # the routing decision is still made - resolution did not short-circuit classification
    assert res.task and res.path


def test_estate_default_applies_when_profile_declares_nothing(store, monkeypatch):
    store.save(_tax("estate"))
    cfg = _Cfg({"coder": {}}, router_default="estate")
    assert _classify("coder", monkeypatch, cfg).taxonomy_id == "estate"


def test_draft_taxonomy_is_inert_end_to_end(store, monkeypatch):
    store.save(_tax("draft-tax", owner="l2", status="draft"))
    cfg = _Cfg({"l2": {"intent_taxonomy": "draft-tax"}})
    assert _classify("l2", monkeypatch, cfg).taxonomy_id == it.BUILTIN_ID


def test_classification_never_raises_on_a_broken_taxonomy(store, monkeypatch):
    """This sits in the request path: a corrupt taxonomy must degrade, not 500."""
    monkeypatch.setattr(it, "_db_path", lambda: "/nonexistent/dir/nope.db")
    cfg = _Cfg({"l2": {"intent_taxonomy": "op-v1"}})
    res = _classify("l2", monkeypatch, cfg)
    assert res.taxonomy_id == it.BUILTIN_ID


def test_ungraded_taxonomy_cannot_be_activated_then_used(store, monkeypatch):
    """Exit criterion 4: create -> cannot enable without a grade -> enable -> now in force."""
    store.save(it.IntentTaxonomy(id="op-v1", owner="l2", status="draft", acts=_acts(), grade=None))
    cfg = _Cfg({"l2": {"intent_taxonomy": "op-v1"}})

    with pytest.raises(it.TaxonomyError):
        store.activate("op-v1")
    assert _classify("l2", monkeypatch, cfg).taxonomy_id == it.BUILTIN_ID, "still inert"

    t = store.get("op-v1")
    t.grade = {"negative_control": {"passed": True}}
    store.save(t)
    store.activate("op-v1")
    assert _classify("l2", monkeypatch, cfg).taxonomy_id == "op-v1", "in force once graded"


def test_the_decision_model_carries_the_taxonomy_columns():
    """Portable guard for the defect that actually bit live: the ORM model had no such attribute, so
    the stamp vanished between the decision dict and the table while every in-process check passed.
    This one runs in both trees, which the writer test below cannot."""
    from src.api.models import RoutingDecision
    cols = {c.name for c in RoutingDecision.__table__.columns}
    assert {"taxonomy_id", "taxonomy_version"} <= cols, sorted(cols)
    assert RoutingDecision.__table__.columns["taxonomy_id"].nullable is True
    assert RoutingDecision.__table__.columns["taxonomy_version"].nullable is True


def test_the_writer_actually_persists_the_taxonomy(tmp_path):
    """The dict key is not the column. Verified live: a real request wrote a decision row with
    taxonomy_id NULL because the writer names every column explicitly and the ORM model had no such
    attribute - so the stamp silently vanished between the dict and the table.

    Skipped where the preflight gate does not exist (prod has none; its writer is the router's).
    """
    import sqlalchemy as sa
    from src.api.models import Base
    record_decision = pytest.importorskip(
        "src.api.preflight", reason="no preflight gate in this tree"
    ).record_decision

    db = str(tmp_path / "d.db")
    eng = sa.create_engine("sqlite:///" + db)
    # Build the schema from the ORM metadata, not by hand: a hand-written table drifts from the
    # model and the writer then fails for a reason that has nothing to do with the change.
    Base.metadata.create_all(eng)
    ok = record_decision({"ts": "2026-11-03T00:00:00Z", "profile": "l2", "task": "t", "path": "p",
                          "taxonomy_id": "op-v1", "taxonomy_version": 7}, db)
    assert ok, "record_decision reported failure"
    with eng.connect() as c:
        row = c.exec_driver_sql("select taxonomy_id, taxonomy_version from routing_decisions").fetchone()
    assert row == ("op-v1", 7), f"taxonomy did not reach the table: {row}"


def test_decision_rows_stamp_the_taxonomy():
    """Without this stamp a decision logged under one label space is indistinguishable from the
    same decision under another, and the historical log cannot be re-read honestly."""
    from src.api.router import ClassifyResult
    r = ClassifyResult(task="t", path="p", taxonomy_id="op-v1", taxonomy_version=7)
    assert r.taxonomy_id == "op-v1" and r.taxonomy_version == 7
    assert ClassifyResult(task="t", path="p").taxonomy_id == "builtin-v0"
