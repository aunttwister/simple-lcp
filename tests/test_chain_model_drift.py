"""Tests for `scripts/check_chain_model_drift.py` (CUR-272).

The checker exists because a chain step can point at a model its provider no longer
serves and *nothing notices* — the request still succeeds, because the local engine
answers whatever name you ask for. So the tests care about exactly one thing: that a
step the provider does not serve is reported as DRIFT, and that the cases which only
*look* like drift (a legacy spelling the provider block forgot to list, a deployment
reachable through our own registry alias) are not.

The last test reproduces the production case this was built from, as it was measured
on 2026-10-30.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
for p in (str(REPO), str(REPO / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import check_chain_model_drift as ccd  # noqa: E402

from test_model_identity import conn, db  # noqa: E402,F401  (reuse the seeded registry fixture)


# --------------------------------------------------------------------- pure helpers


def test_chain_steps_reads_every_provider_and_model():
    profiles = {
        "l2": {"chain": [{"provider": "a", "model": "m1"},
                         {"provider": "b", "model": "m2", "role": "tool"}]},
        "l1": {"chain": [{"provider": "b", "model": "m2"}]},
        "empty": {"chain": []},
    }
    steps = ccd.chain_steps(profiles)
    assert [(s["profile"], s["provider"], s["model"]) for s in steps] == [
        ("l1", "b", "m2"), ("l2", "a", "m1"), ("l2", "b", "m2")]


def test_chain_steps_skips_malformed_steps_instead_of_crashing():
    """Config is data; a half-written step must not take the checker down."""
    profiles = {"p": {"chain": [{"provider": "a"}, {"model": "m"}, "not-a-dict", None,
                                {"provider": "a", "model": ""}]}}
    assert ccd.chain_steps(profiles) == [{"profile": "p", "index": 1, "provider": "",
                                          "model": "m", "role": None}]


def test_declared_models_handles_every_shape_the_config_uses():
    providers = {
        "list": {"models": ["a", "b"]},
        "wrapped": {"models": {"id": "c"}},
        "single": {"models": "d"},
        "dictlist": {"models": [{"id": "e"}, {"model": "f"}, {"name": "g"},
                                {"served_id": "h"}]},
        "none": {},
    }
    assert ccd.declared_models(providers, "list") == ["a", "b"]
    assert ccd.declared_models(providers, "wrapped") == ["c"]
    assert ccd.declared_models(providers, "single") == ["d"]
    assert ccd.declared_models(providers, "dictlist") == ["e", "f", "g", "h"]
    # A block with no `models` key declares nothing, which is not the same as "" —
    # the checker must not treat an unknown declaration as an empty one.
    assert ccd.declared_models(providers, "none") is None
    assert ccd.declared_models(providers, "absent") is None


def test_group_findings_collapses_repeats_and_keeps_the_worst_verdict():
    rows = [
        {"provider": "p", "model": "m", "profile": "l1", "verdict": ccd.OK,
         "evidence": [], "slug": "s", "deployment_status": "active", "traffic_30d": 0},
        {"provider": "p", "model": "m", "profile": "l2", "verdict": ccd.ERROR,
         "evidence": ["bad"], "slug": "s", "deployment_status": "retired", "traffic_30d": 5},
        {"provider": "q", "model": "n", "profile": "l1", "verdict": ccd.OK,
         "evidence": [], "slug": "t", "deployment_status": "active", "traffic_30d": 0},
    ]
    groups = ccd.group_findings(rows)
    assert len(groups) == 2
    worst = groups[0]
    assert (worst["provider"], worst["model"]) == ("p", "m")
    assert worst["verdict"] == ccd.ERROR          # the worst verdict wins
    assert worst["evidence"] == ["bad"]
    assert sorted(worst["profiles"]) == ["l1", "l2"]
    assert worst["traffic_30d"] == 5              # the largest traffic wins


# ------------------------------------------------------------------ verdicts on a DB


def _seed_config(con, profiles: dict, providers: dict) -> None:
    con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("gateway_config:profiles", json.dumps(profiles)))
    con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("gateway_config:providers", json.dumps(providers)))
    con.commit()


def _add_deployment(con, dep_id, endpoint, model_id, served_id, status):
    con.execute(
        "INSERT INTO mi_deployments (deployment_id, model_id, endpoint_id, served_id,"
        " quantisation, status, source) VALUES (?, ?, (SELECT endpoint_id FROM"
        " mi_endpoints WHERE slug = ?), ?, 'none', ?, 'test')",
        (dep_id, model_id, endpoint, served_id, status))
    con.commit()


def test_a_retired_deployment_on_a_live_chain_step_is_drift(db):
    """The defect CUR-272 exists for, in its simplest form."""
    from src.api import model_identity as mi
    con = conn(db)
    endpoint, model_id = con.execute(
        "SELECT e.slug, m.model_id FROM mi_endpoints e, mi_models m"
        " ORDER BY e.endpoint_id, m.model_id LIMIT 1").fetchone()
    _add_deployment(con, 9101, endpoint, model_id, "gone-model", "retired")

    step = {"profile": "p", "index": 0, "provider": endpoint, "model": "gone-model",
            "role": None}
    f = ccd.assess(con, mi, step, {endpoint: {"models": ["gone-model"]}}, {})
    assert f["verdict"] == ccd.ERROR
    assert "not serving" in " ".join(f["evidence"])
    assert "retired" in " ".join(f["evidence"])
    con.close()


def test_an_active_deployment_is_not_drift_even_behind_a_global_alias(db):
    """Regression: the `coder -> commandcode -> z-ai/glm-5.3-flash` case.

    A global (endpoint-less) alias maps the string to a logical model with no
    deployment. If the served-id match is lost — which is what the `_in_force`
    `last_seen > at` bug did — the step reports "no deployment" and looks like drift
    when the provider is serving it perfectly well.
    """
    from src.api import model_identity as mi
    con = conn(db)
    endpoint, model_id = con.execute(
        "SELECT e.slug, m.model_id FROM mi_endpoints e, mi_models m"
        " ORDER BY e.endpoint_id, m.model_id LIMIT 1").fetchone()
    _add_deployment(con, 9102, endpoint, model_id, "inner/served-id", "active")
    con.execute(
        "INSERT INTO mi_aliases (id, endpoint_id, alias, model_id, deployment_id,"
        " source, valid_from) VALUES (9102, NULL, 'inner/served-id', ?, NULL, 'registry',"
        " '2026-01-01')", (model_id,))
    con.commit()

    step = {"profile": "p", "index": 0, "provider": endpoint, "model": "inner/served-id",
            "role": None}
    f = ccd.assess(con, mi, step, {endpoint: {"models": ["inner/served-id"]}}, {})
    assert f["verdict"] == ccd.OK, f["evidence"]
    assert f["deployment_id"] == 9102
    con.close()


def test_a_declared_mismatch_on_a_served_model_is_a_note_not_an_error(db):
    """`deepseek -> deepseek-v4-flash` five times over is one note, not five warnings.

    The provider demonstrably serves it (active deployment, real traffic); the
    provider block simply does not list the legacy spelling. That is config lagging
    reality, which is worth saying once — not a serving defect.
    """
    from src.api import model_identity as mi
    con = conn(db)
    endpoint, model_id = con.execute(
        "SELECT e.slug, m.model_id FROM mi_endpoints e, mi_models m"
        " ORDER BY e.endpoint_id, m.model_id LIMIT 1").fetchone()
    _add_deployment(con, 9103, endpoint, model_id, "legacy-spelling", "active")

    step = {"profile": "p", "index": 0, "provider": endpoint, "model": "legacy-spelling",
            "role": None}
    f = ccd.assess(con, mi, step, {endpoint: {"models": ["new-spelling"]}}, {})
    assert f["verdict"] == ccd.INFO, f["evidence"]
    assert "does not list it" in " ".join(f["evidence"])
    con.close()


def test_an_undeclared_and_unknown_model_is_drift_even_with_a_catalogue(db):
    """Nothing knows it and the provider does not advertise it: drift."""
    from src.api import model_identity as mi
    con = conn(db)
    endpoint = con.execute("SELECT slug FROM mi_endpoints ORDER BY endpoint_id LIMIT 1"
                           ).fetchone()[0]
    step = {"profile": "p", "index": 0, "provider": endpoint, "model": "never-heard-of-it",
            "role": None}
    live = {endpoint: (["something-else"], None)}
    f = ccd.assess(con, mi, step, {endpoint: {"models": ["something-else"]}}, live)
    assert f["verdict"] == ccd.ERROR
    assert "cannot resolve" in " ".join(f["evidence"])
    con.close()


def test_the_production_case_is_detected(db):
    """The exact shape measured on 2026-10-30, reproduced.

    `coder` -> `local-sparks` -> `qwen3.8-flash-next`, while `local-sparks` declares and
    advertises exactly one model (`DeepSeek-V4.1-Flash-1017`) and the registry records
    the qwen deployment as retired. The engine answers for *any* requested name, so the
    step never errors — it quietly serves DeepSeek V4.1 Flash to a chain that asked for
    Qwen 3.8. That is why this has to be detected from the registry and the catalogue
    rather than from failures.

    The registry fixture already carries this deployment (same seeder as production), so
    the test asserts against it rather than inventing one; only its status is pinned, so
    the test does not depend on how the seed happens to classify it.
    """
    from src.api import model_identity as mi
    con = conn(db)
    row = con.execute(
        "SELECT d.deployment_id, d.model_id, e.slug FROM mi_deployments d"
        " JOIN mi_endpoints e ON e.endpoint_id = d.endpoint_id"
        " WHERE d.served_id = 'qwen3.8-flash-next' ORDER BY d.deployment_id LIMIT 1"
    ).fetchone()
    assert row is not None, "the fixture seeder must register the qwen deployment"
    dep_id, model_id, endpoint = row
    con.execute("UPDATE mi_deployments SET status = 'retired' WHERE deployment_id = ?",
                (dep_id,))
    _seed_config(con,
                 {"coder": {"chain": [{"provider": endpoint,
                                       "model": "qwen3.8-flash-next"}]}},
                 {endpoint: {"api_base": "http://127.0.0.1:18300/v1",
                             "models": ["DeepSeek-V4.1-Flash-1017"]}})

    steps = ccd.chain_steps(json.loads(con.execute(
        "SELECT value FROM settings WHERE key='gateway_config:profiles'").fetchone()[0]))
    providers = json.loads(con.execute(
        "SELECT value FROM settings WHERE key='gateway_config:providers'").fetchone()[0])
    # The engine advertises one model, and it is not the one the chain asks for.
    live = {endpoint: (["DeepSeek-V4.1-Flash-1017"], None)}

    findings = [ccd.assess(con, mi, s, providers, live) for s in steps]
    groups = ccd.group_findings(findings)

    assert len(groups) == 1
    g = groups[0]
    assert g["verdict"] == ccd.ERROR
    assert g["model"] == "qwen3.8-flash-next"
    assert g["profiles"] == ["coder"]
    evidence = " ".join(g["evidence"])
    assert "retired" in evidence            # the registry says so
    assert "declared=NO" in evidence        # the provider block does not list it
    assert "advertised=NO" in evidence      # and the live engine does not advertise it
    con.close()


def test_unreferenced_reports_only_active_deployments_nothing_uses(db):
    from src.api import model_identity as mi
    con = conn(db)
    endpoint, model_id = con.execute(
        "SELECT e.slug, m.model_id FROM mi_endpoints e, mi_models m"
        " ORDER BY e.endpoint_id, m.model_id LIMIT 1").fetchone()
    _add_deployment(con, 9105, endpoint, model_id, "used-model", "active")
    _add_deployment(con, 9106, endpoint, model_id, "unused-model", "active")
    _add_deployment(con, 9107, endpoint, model_id, "retired-unused", "retired")

    steps = [{"profile": "p", "index": 0, "provider": endpoint, "model": "used-model",
              "role": None}]
    got = {(e["endpoint"], e["served_id"]) for e in ccd.unreferenced(con, mi, steps)}
    assert (endpoint, "unused-model") in got
    assert (endpoint, "used-model") not in got, "a referenced deployment is not unreferenced"
    assert (endpoint, "retired-unused") not in got, "only ACTIVE deployments count"
    con.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
