"""Model identity (CUR-270): resolution, purity, and the historical-normalisation rule.

The test that matters most is `test_retired_name_is_two_models_by_time`: one string,
`deepseek-v4-flash`, on one endpoint, must resolve to **two different logical models**
depending on the date, because DeepSeek retired V4-Flash and left the name as a routing
alias. No name-only normalisation can express that — it is the reason the release
timeline exists.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from src.api import model_identity as mi  # noqa: E402
import seed_model_identity as seeder  # noqa: E402


TRAFFIC_DDL = """
CREATE TABLE requests (
    id INTEGER PRIMARY KEY, timestamp TEXT, profile TEXT, model TEXT, provider TEXT,
    prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
    cache_hit_tokens INTEGER DEFAULT 0, cache_miss_tokens INTEGER DEFAULT 0,
    cost REAL DEFAULT 0, latency_ms INTEGER, success INTEGER DEFAULT 1,
    error_type TEXT, tools_blocked TEXT, error_detail TEXT, conversation_id TEXT);
CREATE TABLE routing_decisions (
    id INTEGER PRIMARY KEY, ts TEXT, profile TEXT, provider TEXT, model TEXT,
    from_provider TEXT, from_model TEXT, path TEXT, l4_json TEXT);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE model_capabilities (
    id INTEGER PRIMARY KEY, model TEXT, task_type TEXT, score REAL, source TEXT,
    release_label TEXT, updated_at TEXT);
"""


@pytest.fixture()
def db(tmp_path) -> str:
    """A migrated, seeded registry on a throwaway DB."""
    path = str(tmp_path / "costs.db")
    con = sqlite3.connect(path)
    con.executescript(TRAFFIC_DDL)
    con.commit()
    con.close()

    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    # stamp the pre-existing schema at 025 so only 026 runs
    command.stamp(cfg, "025")
    command.upgrade(cfg, "026")

    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    s = seeder.Seeder(con, apply=True, verbose=False)
    ep_ids = {e["slug"]: s.ensure_endpoint(e) for e in seeder.ENDPOINTS}
    for alias, slug in seeder.ENDPOINT_ALIASES:
        s.ensure_endpoint_alias(alias, slug, ep_ids)
    model_ids = {m[0]: s.ensure_model(m) for m in seeder.MODELS}
    rel_ids = {(r[0], r[1]): s.ensure_release(r, model_ids) for r in seeder.RELEASES}
    dep_ids = {(d[0], d[1]): s.ensure_deployment(d, ep_ids, model_ids)
               for d in seeder.DEPLOYMENTS}
    for dr in seeder.DEPLOYMENT_RELEASES:
        s.ensure_deployment_release(dr, ep_ids, model_ids, dep_ids, rel_ids)
    for a in seeder.ALIASES:
        s.ensure_alias(a, ep_ids, model_ids, dep_ids)
    s.bump_version("test fixture seed")
    con.commit()
    con.close()
    return path


def conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


# ------------------------------------------------------------------ the migration


def test_migration_creates_registry_and_adds_columns(db):
    con = conn(db)
    tables = {r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("mi_endpoints", "mi_endpoint_aliases", "mi_models", "mi_releases",
              "mi_deployments", "mi_deployment_releases", "mi_aliases",
              "mi_registry_versions"):
        assert t in tables, f"{t} missing"

    req_cols = {r[1] for r in con.execute("PRAGMA table_info(requests)")}
    assert {"deployment_id", "release_id", "registry_version"} <= req_cols
    rd_cols = {r[1] for r in con.execute("PRAGMA table_info(routing_decisions)")}
    assert {"from_deployment_id", "to_deployment_id", "registry_version"} <= rd_cols
    # additive: the original columns are untouched
    assert {"model", "provider", "cost"} <= req_cols
    con.close()


def test_downgrade_is_clean(db):
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.downgrade(cfg, "025")
    con = conn(db)
    tables = {r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert not any(t.startswith("mi_") for t in tables)
    assert "deployment_id" not in {r[1] for r in con.execute("PRAGMA table_info(requests)")}
    con.close()


# ------------------------------------------------- the historical-normalisation rule


def test_retired_name_is_two_models_by_time(db):
    """`deepseek-v4-flash` on the deepseek endpoint: genuine V4-Flash, then an alias."""
    con = conn(db)

    before = mi.resolve("deepseek", "deepseek-v4-flash", at="2026-08-15T00:00:00+00:00", con=con)
    assert before.resolved
    assert before.model == "deepseek-v4-flash"
    assert before.release == "0731"
    assert before.matched_by in (mi.MATCH_SERVED, mi.MATCH_ALIAS)
    assert before.confidence == "high"

    after = mi.resolve("deepseek", "deepseek-v4-flash", at="2026-10-20T00:00:00+00:00", con=con)
    assert after.resolved
    assert after.model == "deepseek-flash", "the retired name must route to V4.1 after 09-10"
    assert after.release == "1017"
    assert after.model_id != before.model_id
    con.close()


def test_local_lane_and_cloud_are_same_model_different_deployments(db):
    """The Spark box and the cloud endpoint serve one logical model from two deployments."""
    con = conn(db)
    local = mi.resolve("local-sparks", "DeepSeek-V4.1-Flash-1017",
                       at="2026-10-29T00:00:00+00:00", con=con)
    cloud = mi.resolve("deepseek", "deepseek-flash",
                       at="2026-10-29T00:00:00+00:00", con=con)
    assert local.model == cloud.model == "deepseek-flash"
    assert local.deployment_id != cloud.deployment_id
    assert local.release == cloud.release == "1017"
    assert local.matched_by == mi.MATCH_SERVED
    con.close()


def test_v41_ga_window_is_its_own_release(db):
    con = conn(db)
    r = mi.resolve("deepseek", "deepseek-flash", at="2026-09-20T00:00:00+00:00", con=con)
    assert r.release == "v4.1-ga"
    con.close()


# ------------------------------------------------------------------- case / aliases


def test_case_only_variant_resolves_as_explicit_alias(db):
    con = conn(db)
    r = mi.resolve("local-sparks", "glm-5.3-flash-exl3",
                   at="2026-10-29T00:00:00+00:00", con=con)
    assert r.model == "glm-5.3-flash"
    assert r.matched_by == mi.MATCH_ALIAS, "must be an explicit alias, not string munging"
    assert r.confidence == "high"
    con.close()


def test_retired_provider_name_is_an_endpoint_alias(db):
    """The registry's `zgx` key must still resolve after the lane was renamed."""
    con = conn(db)
    r = mi.resolve("zgx", "qwen3.8-flash-next", at="2026-09-01T00:00:00+00:00", con=con)
    assert r.resolved
    assert r.endpoint == "local-zgx"
    assert r.model == "qwen3.8-flash-next"
    con.close()


def test_reselling_endpoints_apply_the_same_retirement_rule(db):
    """commandcode and opencode carry DeepSeek's legacy name: same string, two models.

    Evidence: commandcode's live /provider/v1/models advertises exactly the pair DeepSeek
    documents as routed to V4.1 (-v4-flash and -v4-flash-vision-exp) and resells at
    underlying API rates; opencode stopped carrying the legacy name on 2026-09-29 and
    requires the explicit -v4.1-flash spelling.
    """
    con = conn(db)
    for endpoint, name in (("opencode", "deepseek-v4-flash"),
                           ("commandcode", "deepseek/deepseek-v4-flash")):
        before = mi.resolve(endpoint, name, at="2026-08-20T00:00:00+00:00", con=con)
        assert before.model == "deepseek-v4-flash", (endpoint, before.as_dict())
        assert before.release == "0731"

        ga = mi.resolve(endpoint, name, at="2026-09-20T00:00:00+00:00", con=con)
        assert ga.model == "deepseek-flash", (endpoint, ga.as_dict())
        assert ga.release == "v4.1-ga"

        after = mi.resolve(endpoint, name, at="2026-10-20T00:00:00+00:00", con=con)
        assert after.model == "deepseek-flash", (endpoint, after.as_dict())
        assert after.release == "1017"
        assert after.deployment_id != before.deployment_id
    con.close()


def test_smaug_agentic_is_its_own_model(db):
    """Not a task-shaped key: Abacus.AI's agentic SFT of Kimi K3, with its own identity."""
    con = conn(db)
    r = mi.resolve(None, "smaug-agentic", con=con)
    assert r.model == "smaug-agentic" and r.model_id is not None
    row = [m for m in mi.list_logical(con=con) if m["slug"] == "smaug-agentic"][0]
    assert row["family"] == "abacus"
    assert row["benchmark_key"] == "smaug-agentic"
    con.close()


def test_inactive_endpoint_is_recorded_as_such(db):
    """opencode's subscription ended — the registry must say so, not silently stay 'active'."""
    con = conn(db)
    eps = {e["slug"]: e for e in mi.list_endpoints(con=con)}
    assert eps["opencode"]["status"] == "inactive"
    assert "2026-09-29" in (eps["opencode"]["notes"] or "")
    assert eps["commandcode"]["status"] == "active"
    con.close()


def test_benchmark_import_key_resolves_to_its_model(db):
    con = conn(db)
    r = mi.resolve(None, "claude-5-opus-thinking", con=con)
    assert r.model == "claude-opus-5"
    con.close()


# ------------------------------------------------------------------- purity / safety


def test_resolution_is_pure(db):
    con = conn(db)
    args = ("deepseek", "deepseek-v4-flash")
    at = "2026-10-20T00:00:00+00:00"
    a = mi.resolve(*args, at=at, con=con).as_dict()
    b = mi.resolve(*args, at=at, con=con).as_dict()
    assert a == b
    con.close()


def test_unknown_input_is_reported_not_guessed(db):
    con = conn(db)
    r = mi.resolve("opencode", "totally-made-up-model", con=con)
    assert not r.resolved and not r
    assert r.unresolved_reason == "no_match"
    assert r.model is None
    assert r.confidence == "none"

    unknown_ep = mi.resolve("does-not-exist", "deepseek-flash", con=con)
    # a string that *is* a logical slug still identifies its model — the model is known
    # even when the endpoint is not — so this resolves by slug...
    assert unknown_ep.model == "deepseek-flash"
    assert unknown_ep.matched_by == mi.MATCH_SLUG
    # ...and the unregistered endpoint is surfaced by reconcile instead (see below)
    assert mi.resolve("does-not-exist", "nothing-like-this", con=con).unresolved_reason == (
        "unknown_endpoint")

    blank = mi.resolve("deepseek", "   ", con=con)
    assert blank.unresolved_reason == "empty"
    con.close()


def test_reconcile_flags_an_unregistered_endpoint(db):
    """A new provider must be reported, not silently tolerated."""
    con = conn(db)
    con.execute("INSERT INTO requests (timestamp, provider, model) VALUES (?,?,?)",
                ("2026-10-30T00:00:00+00:00", "local-newbox", "glm-5.3-flash"))
    con.commit()
    findings = mi.reconcile(con=con)
    hit = [f for f in findings if f["kind"] == "unregistered_endpoint"]
    assert hit and hit[0]["provider"] == "local-newbox"
    con.close()


def test_strict_fails_closed_and_lenient_fails_open(db):
    con = conn(db)
    with pytest.raises(mi.UnresolvedModel) as e:
        mi.resolve_strict("deepseek", "nope", con=con)
    assert e.value.resolution.unresolved_reason == "no_match"
    # the same input through resolve() must NOT raise — live traffic is never blocked
    assert not mi.resolve("deepseek", "nope", con=con).resolved
    con.close()


def test_normalisation_is_low_confidence_and_never_stores(db):
    con = conn(db)
    # a spelling nobody registered: only layer 2 can match it, and it must say so
    r = mi.resolve("local-sparks", "GLM-5.3-Flash-EXL3.gguf", con=con)
    assert r.resolved and r.matched_by == mi.MATCH_NORMALISED
    assert r.confidence == "low"
    con.close()


# ------------------------------------------------------------------- reconciliation


def test_reconcile_flags_the_unknown_writer_defect(db):
    con = conn(db)
    con.execute("INSERT INTO requests (timestamp, provider, model) VALUES (?,?,?)",
                ("2026-10-30T00:00:00+00:00", "unknown", "unknown"))
    con.commit()
    findings = mi.reconcile(con=con)
    kinds = {f["kind"] for f in findings}
    assert "unknown_writer_rows" in kinds
    hit = [f for f in findings if f["kind"] == "unknown_writer_rows"][0]
    assert hit["rows"] == 1 and hit["severity"] == "high"
    con.close()


def test_reconcile_reports_unresolvable_traffic(db):
    con = conn(db)
    con.execute("INSERT INTO routing_decisions (ts, provider, model) VALUES (?,?,?)",
                ("2026-10-30T00:00:00+00:00", "opencode", "mystery-model-9000"))
    con.commit()
    findings = mi.reconcile(con=con)
    hit = [f for f in findings if f["kind"] == "unresolved_traffic"]
    assert hit and hit[0]["served"] == "mystery-model-9000"
    con.close()


def test_reconcile_reports_unverified_deployments(db):
    con = conn(db)
    findings = mi.reconcile(con=con)
    assert any(f["kind"] == "unverified_deployment" for f in findings)
    # the seeded notes carry the open questions rather than a guessed answer
    pend = [d for d in mi.list_deployments(con=con) if (d["notes"] or "").startswith("PENDING")]
    assert {d["served_id"] for d in pend} == {"ox-alpha-free"}
    # the DeepSeek legacy name is no longer an open question: it is resolved by a release
    # window plus a time-windowed alias, so it must NOT still be flagged PENDING
    assert not any("deepseek-v4-flash" in d["served_id"] for d in pend)
    con.close()


# ------------------------------------------------------------------------ seeding


def test_seed_is_idempotent(tmp_path):
    """Re-running the seeder must create nothing (rule 2 of the seeding standard)."""
    path = str(tmp_path / "costs.db")
    c = sqlite3.connect(path)
    c.executescript(TRAFFIC_DDL)
    c.commit()
    c.close()
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.stamp(cfg, "025")
    command.upgrade(cfg, "026")

    def run():
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        s = seeder.Seeder(con, apply=True, verbose=False)
        ep = {e["slug"]: s.ensure_endpoint(e) for e in seeder.ENDPOINTS}
        for alias, slug in seeder.ENDPOINT_ALIASES:
            s.ensure_endpoint_alias(alias, slug, ep)
        md = {m[0]: s.ensure_model(m) for m in seeder.MODELS}
        rl = {(r[0], r[1]): s.ensure_release(r, md) for r in seeder.RELEASES}
        dp = {(d[0], d[1]): s.ensure_deployment(d, ep, md) for d in seeder.DEPLOYMENTS}
        for dr in seeder.DEPLOYMENT_RELEASES:
            s.ensure_deployment_release(dr, ep, md, dp, rl)
        for a in seeder.ALIASES:
            s.ensure_alias(a, ep, md, dp)
        con.commit()
        con.close()
        return s

    first = run()
    assert first.created > 0 and not first.errors
    second = run()
    assert second.created == 0, f"second run created {second.created} rows"
    assert not second.errors

    con = conn(db_path := path)
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("mi_endpoints", "mi_models", "mi_deployments", "mi_aliases",
                        "mi_releases", "mi_deployment_releases")}
    assert counts["mi_aliases"] == len(seeder.ALIASES)
    assert counts["mi_deployments"] == len(seeder.DEPLOYMENTS)
    assert counts["mi_models"] == len(seeder.MODELS)
    con.close()


def test_no_alias_ambiguity_on_one_endpoint(db):
    """Invariant: no two in-force aliases on one endpoint map to different models."""
    con = conn(db)
    rows = con.execute(
        "SELECT endpoint_id, alias, COUNT(DISTINCT COALESCE(model_id, deployment_id)) n"
        " FROM mi_aliases WHERE valid_to IS NULL GROUP BY endpoint_id, alias HAVING n > 1"
    ).fetchall()
    assert not rows, f"ambiguous aliases: {[dict(r) for r in rows]}"
    con.close()


def test_registry_version_advances_with_the_seed(db):
    con = conn(db)
    assert mi.registry_version(con) >= 1
    con.close()
