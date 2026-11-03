"""Per-user intent taxonomies.

Two things must hold, and most of these tests are about the first:
1. DEFAULT BEHAVIOUR IS BYTE-IDENTICAL — with nothing configured, resolution returns the built-in
   label space whose exemplars ARE ``TASK_EXEMPLARS``, so the classifier decides exactly as before.
2. A TAXONOMY CANNOT ACTIVATE UNMEASURED — the grade gate is the whole mitigation for the risk of
   accumulating classifiers nobody can evaluate.
"""
from __future__ import annotations

import json

import pytest

from src.api import intent_taxonomy as it
from src.api import task_classifier as tc


# ---------------------------------------------------------------------------- helpers

def _acts(n=3, exemplars=3):
    return {f"act{i}": [f"example {i} number {j}" for j in range(exemplars)] for i in range(n)}


def _passing_grade():
    return {"kind": "reference", "negative_control": {"passed": True}}


def _tax(**kw):
    base = dict(
        id="op-v1", display_name="Operator", owner="l2", version=1, status="draft",
        acts=_acts(), subjects={}, buckets={}, groups={},
        grade=_passing_grade(),
    )
    base.update(kw)
    return it.IntentTaxonomy(**base)


class FakeEmbed:
    """Deterministic bag-of-words embedder: identical text -> identical vector, so centroids are
    exact and a label-space change is visible without any real model."""

    def __call__(self, texts):
        dim = 16
        out = []
        for t in texts:
            v = [0.0] * dim
            for tok in str(t).lower().split():
                v[hash(tok) % dim] += 1.0
            if not any(v):
                v[0] = 1.0
            out.append(v)
        return out


# ---------------------------------------------------------------------------- 1. the default path

def test_builtin_taxonomy_is_the_shipped_label_space():
    b = it.builtin_v0()
    b.validate()
    assert b.id == it.BUILTIN_ID
    assert set(b.acts) == set(tc.TASK_EXEMPLARS)
    for label, ex in tc.TASK_EXEMPLARS.items():
        assert b.acts[label] == list(ex)


def test_builtin_is_activatable_without_a_new_grade():
    """It is the shipped vocabulary; its 'grade' is that it is the status quo."""
    assert it.builtin_v0().can_activate()


def test_unconfigured_resolution_returns_builtin():
    """THE safety property: nothing configured anywhere => today's label space."""
    assert it.resolve_for_profile(None, config=None).id == it.BUILTIN_ID
    assert it.resolve_for_profile("l2", config=None).id == it.BUILTIN_ID


def test_builtin_classifier_equals_pre_taxonomy_behaviour():
    """With no taxonomy, the classifier must decide the same way it did before taxonomies existed."""
    clf = tc.SemanticClassifier(embed=FakeEmbed())
    assert clf.labels == list(tc.TASK_EXEMPLARS)
    assert clf.taxonomy_id == "builtin-v0"


def test_classifier_uses_supplied_label_space_not_the_constant():
    clf = tc.SemanticClassifier(embed=FakeEmbed(), exemplars=_acts(2),
                               taxonomy_id="op-v1", taxonomy_version=4)
    assert clf.labels == ["act0", "act1"]
    assert clf.taxonomy_id == "op-v1" and clf.taxonomy_version == 4
    assert clf.classify("example 1 number 2") in {"act0", "act1"}


def test_two_profiles_do_not_share_one_cache_key():
    """The bug a single global cache caused: profile B silently classified in profile A's space."""
    a = _tax(id="tax-a", acts={"only_a": ["x y z"] * 3})
    b = _tax(id="tax-b", acts={"only_b": ["x y z"] * 3})
    assert tc._cache_key(a) != tc._cache_key(b)
    assert tc._cache_key(None) == ("builtin-v0", None)


def test_editing_a_taxonomy_changes_its_cache_key():
    """A key without the version would serve a stale label space after an edit."""
    v1 = _tax(id="tax-a", version=1)
    v2 = _tax(id="tax-a", version=2)
    assert tc._cache_key(v1) != tc._cache_key(v2)


def test_invalidate_clears_every_taxonomy_key():
    tc._classifiers[("a", 1)] = object()
    tc._classifiers[("b", 2)] = object()
    tc.invalidate_semantic_classifier()
    assert tc._classifiers == {}


# ---------------------------------------------------------------------------- 2. the grade gate

def test_draft_taxonomy_is_inert(tmp_path):
    store = it.TaxonomyStore(str(tmp_path / "t.db"))
    store.save(_tax(status="draft"))
    resolved = it.resolve_for_profile("l2", config=_Cfg({"l2": {"intent_taxonomy": "op-v1"}}),
                                     store=store)
    assert resolved.id == it.BUILTIN_ID, "a draft taxonomy must never be in force"


def test_activate_refuses_without_a_grade(tmp_path):
    store = it.TaxonomyStore(str(tmp_path / "t.db"))
    store.save(_tax(grade=None))
    with pytest.raises(it.TaxonomyError, match="no grade"):
        store.activate("op-v1")


def test_activate_refuses_when_negative_control_failed(tmp_path):
    store = it.TaxonomyStore(str(tmp_path / "t.db"))
    store.save(_tax(grade={"negative_control": {"passed": False}}))
    with pytest.raises(it.TaxonomyError, match="negative control"):
        store.activate("op-v1")


def test_activate_refuses_thin_exemplars(tmp_path):
    store = it.TaxonomyStore(str(tmp_path / "t.db"))
    store.save(_tax(acts={"only": ["one exemplar"]}))
    with pytest.raises(it.TaxonomyError, match="too thin"):
        store.activate("op-v1")


def test_activate_then_resolves(tmp_path):
    store = it.TaxonomyStore(str(tmp_path / "t.db"))
    store.save(_tax())
    store.activate("op-v1")
    got = it.resolve_for_profile("l2", config=_Cfg({"l2": {"intent_taxonomy": "op-v1"}}), store=store)
    assert got.id == "op-v1" and got.status == "active"


def test_profile_assignment_beats_the_estate_default(tmp_path):
    store = it.TaxonomyStore(str(tmp_path / "t.db"))
    store.save(_tax(id="mine", owner="l2"))
    store.save(_tax(id="estate"))
    store.activate("mine")
    store.activate("estate")
    cfg = _Cfg({"l2": {"intent_taxonomy": "mine"}}, router_default="estate")
    assert it.resolve_for_profile("l2", config=cfg, store=store).id == "mine"
    assert it.resolve_for_profile("l1", config=cfg, store=store).id == "estate"


def test_unknown_assignment_falls_back_rather_than_raising(tmp_path):
    store = it.TaxonomyStore(str(tmp_path / "t.db"))
    got = it.resolve_for_profile("l2", config=_Cfg({"l2": {"intent_taxonomy": "nope"}}), store=store)
    assert got.id == it.BUILTIN_ID


def test_unreadable_store_falls_back_to_builtin(tmp_path):
    store = it.TaxonomyStore(str(tmp_path / "missing.db"))
    got = it.resolve_for_profile("l2", config=_Cfg({"l2": {"intent_taxonomy": "op-v1"}}),
                                 store=_BrokenStore())
    assert got.id == it.BUILTIN_ID


# ---------------------------------------------------------------------------- 3. structural validation

def test_validate_rejects_empty_axis():
    with pytest.raises(it.TaxonomyError, match="no labels"):
        _tax(acts={}, axes=["act"]).validate()


def test_validate_rejects_a_taxonomy_with_nothing_at_all():
    with pytest.raises(it.TaxonomyError, match="axes is empty"):
        _tax(acts={}).validate()


def test_validate_rejects_axis_without_exemplars():
    with pytest.raises(it.TaxonomyError, match="no usable exemplars"):
        _tax(acts={"a": []}).validate()


def test_validate_rejects_bucket_naming_an_unknown_label():
    """A typo in a projection would otherwise become an empty class at decode time."""
    with pytest.raises(it.TaxonomyError, match="unknown act"):
        _tax(buckets={"change": ["act0", "typo"]}).validate()


def test_validate_rejects_group_naming_an_unknown_subject():
    t = _tax(subjects={"s0": ["x", "y", "z"]}, groups={"g": ["not-a-subject"]})
    with pytest.raises(it.TaxonomyError, match="unknown subject"):
        t.validate()


def test_validate_rejects_unknown_status():
    with pytest.raises(it.TaxonomyError, match="status"):
        _tax(status="live").validate()


def test_validate_rejects_unknown_axis():
    with pytest.raises(it.TaxonomyError, match="unknown axis"):
        _tax(axes=["act", "mood"]).validate()


# ---------------------------------------------------------------------------- 4. versioning + scoping

def test_saving_a_change_bumps_the_version(tmp_path):
    store = it.TaxonomyStore(str(tmp_path / "t.db"))
    store.save(_tax())
    again = store.get("op-v1")
    again.acts["act0"] = ["changed one", "changed two", "changed three"]
    store.save(again)
    assert store.get("op-v1").version == 2, "v1 on insert, 2 after one edit"


def test_roundtrip_preserves_the_spec(tmp_path):
    store = it.TaxonomyStore(str(tmp_path / "t.db"))
    t = _tax(buckets={"change": ["act0"]})
    store.save(t)
    got = store.get("op-v1")
    assert got.acts == t.acts and got.buckets == t.buckets and got.owner == "l2"


def test_rules_scoping_risk_is_reported_not_hidden():
    """Routing rules are keyed on the shipped task vocabulary; a taxonomy bringing new labels needs
    its own rule scope. That must be REPORTS, never silently assumed."""
    shipped = it.builtin_v0()
    operator = _tax(acts={"build": ["a b c"], "status": ["d e f"], "research": ["g h i"]})
    # a taxonomy bringing labels the shipped vocabulary does not have -> needs its own rule scope
    assert operator.logical_labels_differ(shipped) is True
    # the shipped vocabulary is closed against itself
    assert shipped.logical_labels_differ(shipped) is False
    # a taxonomy whose labels ARE a subset of the shipped set needs no new rule scope
    subset = _tax(acts={"code_generation": shipped.acts["code_generation"],
                        "planning": shipped.acts["planning"]})
    assert subset.logical_labels_differ(shipped) is False


class _Cfg:
    """Duck-typed config: what resolve_for_profile actually touches."""

    def __init__(self, profiles, router_default=None):
        self.profiles = profiles
        self.plugins = {"router": {"default_taxonomy": router_default}}


class _BrokenStore:
    def get(self, taxonomy_id):
        raise it.TaxonomyError("store unreadable")
