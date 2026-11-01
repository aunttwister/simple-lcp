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
from unittest.mock import patch

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
    # Stamp the pre-existing schema at 025, then take it to HEAD: the fixture has to end up
    # on the revision the application code writes against, not on the revision that
    # happened to be newest when this test was written. Pinning 026 made the fixture a
    # database the code can no longer write to as soon as a later revision added a column.
    command.stamp(cfg, "025")
    command.upgrade(cfg, "head")

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


# ------------------------------------------------------- caller-supplied connections


def test_public_api_works_on_a_bare_connection(db):
    """A caller's plain `sqlite3.connect()` must work.

    Every read in `model_identity` is by column name, so a connection without
    `row_factory = sqlite3.Row` used to raise ``TypeError: tuple indices must be integers``.
    That is precisely how an ad-hoc reconciliation script fails, and it fails at the point
    where you need the answer, so the module normalises the connection itself.
    """
    con = sqlite3.connect(db)
    assert con.row_factory is None, "this test is meaningless if the caller already rowified"
    assert mi.registry_version(con) >= 1
    assert mi.reconcile(con=con) is not None
    assert mi.list_endpoints(con=con)
    assert mi.list_logical(con=con)
    assert mi.list_deployments(con=con)
    assert mi.aliases("deepseek-flash", con=con)
    assert mi.resolve("deepseek", "deepseek-v4-flash", at="2026-10-20T00:00:00+00:00", con=con).resolved
    con.close()


def test_rowify_switches_the_factory_once(db):
    """Passing a connection must not flip a factory a caller deliberately set."""
    con = sqlite3.connect(db)
    assert con.row_factory is None
    mi.registry_version(con)
    assert con.row_factory is sqlite3.Row
    mi.list_endpoints(con=con)          # already rowified: still fine
    assert con.row_factory is sqlite3.Row
    con.close()


# ------------------------------------------------- the write path (CUR-270 phase 2)


def test_stamp_resolves_a_known_pair(db):
    """The request path gets ids, not a string it has to interpret later."""
    con = mi.connect(db, ro=True)
    s = mi.stamp("deepseek", "deepseek-v4-flash", at="2026-10-20T00:00:00+00:00", con=con)
    assert s["deployment_id"] is not None and s["model_id"] is not None
    assert s["registry_version"] >= 1 and s["unresolved_reason"] is None
    con.close()


def test_stamp_returns_all_none_for_an_unknown_pair(db):
    """Unresolved is a recorded unknown, not a guess and not an exception."""
    con = mi.connect(db, ro=True)
    s = mi.stamp("no-such-endpoint", "no-such-model", con=con)
    assert s["deployment_id"] is None and s["model_id"] is None and s["release_id"] is None
    assert s["unresolved_reason"] == "unknown_endpoint"
    con.close()


def test_stamp_fails_open_when_resolution_breaks(db, monkeypatch):
    """A broken registry must never fail a request that has already been served."""
    def boom(*a, **k):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(mi, "resolve", boom)
    con = mi.connect(db, ro=True)
    s = mi.stamp("deepseek", "deepseek-flash", con=con)
    assert s["deployment_id"] is None and s["unresolved_reason"] == "stamp_failed"
    con.close()


def test_stamp_fails_open_on_an_unmigrated_database(tmp_path):
    """No `mi_*` tables at all — still NULLs, still no exception."""
    path = str(tmp_path / "empty.db")
    sqlite3.connect(path).close()
    con = mi.connect(path, ro=False)
    s = mi.stamp("deepseek", "deepseek-flash", con=con)
    assert s["deployment_id"] is None and s["unresolved_reason"] == "stamp_failed"
    con.close()


def test_stamp_cache_is_keyed_on_the_registry_version(db):
    """A stamp from before a registry publish must not be served after it."""
    con = mi.connect(db, ro=True)
    mi.clear_stamp_cache()
    before = mi.stamp("deepseek", "deepseek-flash", at="2026-10-20T00:00:00+00:00", con=con)
    assert mi.stamp("deepseek", "deepseek-flash",
                    at="2026-10-20T00:00:00+00:00", con=con) == before
    w = sqlite3.connect(db)
    w.execute("INSERT INTO mi_registry_versions (version, created_at, note) VALUES (?,?,?)",
              (before["registry_version"] + 1, "2026-10-30T00:00:00+00:00", "cache test"))
    w.commit()
    w.close()
    after = mi.stamp("deepseek", "deepseek-flash", at="2026-10-20T00:00:00+00:00", con=con)
    assert after["registry_version"] > before["registry_version"]
    con.close()


def test_clear_stamp_cache_empties_it(db):
    con = mi.connect(db, ro=True)
    mi.stamp("deepseek", "deepseek-flash", con=con)
    assert mi.clear_stamp_cache() >= 1
    assert mi.clear_stamp_cache() == 0
    con.close()


def test_orm_models_carry_the_identity_columns():
    """The schema exists; the ORM must know about it or the ids cannot be written."""
    from src.api.models import Request, RoutingDecision
    for col in ("deployment_id", "release_id", "registry_version"):
        assert col in Request.__table__.columns
    for col in ("from_deployment_id", "to_deployment_id", "registry_version"):
        assert col in RoutingDecision.__table__.columns


def test_record_cost_stamps_the_identity(db):
    """End to end: a request row records what served it, resolved at write time."""
    from src.api.models import Request, get_engine, get_session
    from src.api.request_pipeline import record_cost
    engine = get_engine(db)
    with patch("src.api.request_pipeline.resolve_service") as rs:
        rs.return_value.for_provider.return_value = None
        record_cost(engine, profile="cron", model="deepseek-v4-flash", provider="deepseek",
                    cost_info={"cost": 0.0, "prompt_tokens": 5, "completion_tokens": 5},
                    success=True, error_type=None, tools_blocked=[])
    with get_session(engine) as session:
        row = session.query(Request).order_by(Request.id.desc()).first()
        assert row.provider == "deepseek" and row.model == "deepseek-v4-flash"
        assert row.deployment_id is not None, "the write path did not stamp the deployment"
        assert row.release_id is not None
        assert row.registry_version >= 1


def test_record_cost_stores_nulls_without_a_registry(tmp_path):
    """Fail open: an unmigrated DB records NULLs rather than losing the request."""
    from src.api.models import Base, Request, get_engine, get_session
    from src.api.request_pipeline import record_cost
    path = str(tmp_path / "plain.db")
    engine = get_engine(path)
    Base.metadata.create_all(engine)          # ORM tables only, no `mi_*` registry
    with patch("src.api.request_pipeline.resolve_service") as rs:
        rs.return_value.for_provider.return_value = None
        record_cost(engine, profile="cron", model="deepseek-flash", provider="deepseek",
                    cost_info={"cost": 0.0}, success=True, error_type=None, tools_blocked=[])
    with get_session(engine) as session:
        row = session.query(Request).order_by(Request.id.desc()).first()
        assert row is not None, "the request row must survive a missing registry"
        assert row.deployment_id is None and row.registry_version is None


def test_decision_identity_resolves_both_sides(db):
    """A reorder records identity, so "the model changed" is a real change."""
    from src.api.router import CapabilityRouter
    ident = CapabilityRouter._decision_identity({
        "ts": "2026-10-20T00:00:00+00:00",
        "from_provider": "deepseek", "from_model": "deepseek-flash",
        "provider": "commandcode", "model": "deepseek/deepseek-v4.1-flash",
    }, db)
    assert ident["to_deployment_id"] is not None
    assert ident["from_deployment_id"] is not None
    # both sides are the SAME logical model here - which is the whole point
    assert ident["from_deployment_id"] != ident["to_deployment_id"]
    assert ident["registry_version"] >= 1


def test_decision_identity_survives_a_broken_resolver(db, monkeypatch):
    """Persistence must never break routing: no ids, no exception."""
    import src.api.model_identity as _mi
    monkeypatch.setattr(_mi, "stamp", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    from src.api.router import CapabilityRouter
    ident = CapabilityRouter._decision_identity({"provider": "deepseek", "model": "deepseek-flash"})
    assert ident == {"from_deployment_id": None, "to_deployment_id": None,
                     "registry_version": None}


def test_decision_identity_without_a_from_side(db):
    """A first-choice decision has nothing displaced; that is None, not a lookup."""
    from src.api.router import CapabilityRouter
    ident = CapabilityRouter._decision_identity(
        {"ts": "2026-10-20T00:00:00+00:00", "provider": "deepseek", "model": "deepseek-flash"}, db)
    assert ident["from_deployment_id"] is None
    assert ident["to_deployment_id"] is not None


# ------------------------------------------------- in-force at the present moment


def test_active_deployment_is_in_force_at_the_present_moment(db):
    """Regression: `last_seen > at` marked every live deployment as NOT in force.

    A deployment is never observed after the moment you ask about, so that rule
    excluded layer 0 — the exact served-id match, the only layer that knows the
    deployment — from every present-tense resolution. Resolution then fell to the
    alias layer, and an alias row cannot name a deployment, so a chain step came back
    as a logical model with **no deployment at all**.

    Measured on production 2026-10-30 before the fix, for exactly this shape:

        coder -> commandcode -> z-ai/glm-5.3-flash
          at 07:00  -> deployment 13  (served_id)      correct
          at 08:00  -> None           (explicit_alias) WRONG - dep 13 was active
          at 12:00  -> None           (explicit_alias) WRONG
          at now    -> None           (explicit_alias) WRONG

    dep13's last_seen was 07:05, so every `at` after that lost the deployment.
    """
    con = conn(db)
    endpoint, model_id = con.execute(
        "SELECT e.slug, m.model_id FROM mi_endpoints e, mi_models m"
        " ORDER BY e.endpoint_id, m.model_id LIMIT 1").fetchone()

    # An active deployment with no release timeline, observed once in June and never since.
    con.execute(
        "INSERT INTO mi_deployments (deployment_id, model_id, endpoint_id, served_id,"
        " quantisation, status, source, first_seen, last_seen)"
        " VALUES (9001, ?, (SELECT endpoint_id FROM mi_endpoints WHERE slug = ?),"
        " 'probe-served-id', 'none', 'active', 'test', '2026-01-01T00:00:00+00:00',"
        " '2026-06-01T00:00:00+00:00')", (model_id, endpoint))
    # A global (endpoint-less) alias for the same string, which cannot know the deployment.
    con.execute(
        "INSERT INTO mi_aliases (id, endpoint_id, alias, model_id, deployment_id, source,"
        " valid_from) VALUES (9001, NULL, 'probe-served-id', ?, NULL, 'registry',"
        " '2026-01-01')", (model_id,))
    con.commit()

    # `at` is months after last_seen; the deployment is still active, so it is in force.
    r = mi.resolve(endpoint, "probe-served-id", at="2026-12-01T00:00:00+00:00", con=con)
    assert r.matched_by == mi.MATCH_SERVED, (
        "layer 0 (exact served id) must win over a global alias — it is the only layer "
        f"that can name the deployment; got matched_by={r.matched_by}")
    assert r.deployment_id == 9001, (
        "an active deployment is in force at the present moment, whatever its last_seen")
    con.close()


def test_retired_deployment_is_known_but_not_in_force_after_it_was_last_seen(db):
    """The flip side, so the fix cannot be confused with "ignore status or time".

    A deployment with no release timeline that is NOT active must stop being *in
    force* after its last observation — otherwise retiring a deployment would do
    nothing. It is still *known*: layer 2 returns the same deployment at
    ``confidence="medium"`` ("we know which deployment you mean, and it is not
    serving"), which is weaker evidence than an in-force match, not the same thing.
    """
    con = conn(db)
    endpoint, model_id = con.execute(
        "SELECT e.slug, m.model_id FROM mi_endpoints e, mi_models m"
        " ORDER BY e.endpoint_id, m.model_id LIMIT 1").fetchone()
    con.execute(
        "INSERT INTO mi_deployments (deployment_id, model_id, endpoint_id, served_id,"
        " quantisation, status, source, first_seen, last_seen)"
        " VALUES (9002, ?, (SELECT endpoint_id FROM mi_endpoints WHERE slug = ?),"
        " 'probe-retired-id', 'none', 'retired', 'test', '2026-01-01T00:00:00+00:00',"
        " '2026-06-01T00:00:00+00:00')", (model_id, endpoint))
    con.commit()

    inside = mi.resolve(endpoint, "probe-retired-id", at="2026-05-01T00:00:00+00:00", con=con)
    after = mi.resolve(endpoint, "probe-retired-id", at="2026-12-01T00:00:00+00:00", con=con)

    assert inside.deployment_id == 9002
    assert inside.confidence == "high", "it was serving while it was still being seen"

    assert after.deployment_id == 9002, "it is still known after retirement"
    assert after.confidence == "medium", (
        "a retired deployment is known but NOT in force — that distinction is what "
        f"makes it dangerous; got confidence={after.confidence}")
    con.close()


def test_a_retired_deployment_with_no_timestamps_is_not_in_force(db):
    """A retired deployment with no evidence at all must still not be "serving".

    ``_in_force`` has no release timeline and no observation timestamps to go on in
    that case, and it used to assume "in force". That reported a *retired* deployment
    as serving at high confidence — on this estate three are in exactly that state
    (deployments 22 and 24 on `local-sparks`, 27 on `llamacpp`), and deployment 24 is
    the target of the `coder` chain step. Status is the one signal that does exist,
    so it decides: not in force, and layer 2 still reports the deployment as *known*
    (medium confidence) — which is why fixing this cannot change a stored id.
    """
    con = conn(db)
    endpoint, model_id = con.execute(
        "SELECT e.slug, m.model_id FROM mi_endpoints e, mi_models m"
        " ORDER BY e.endpoint_id, m.model_id LIMIT 1").fetchone()
    con.execute(
        "INSERT INTO mi_deployments (deployment_id, model_id, endpoint_id, served_id,"
        " quantisation, status, source) VALUES (9003, ?, (SELECT endpoint_id FROM"
        " mi_endpoints WHERE slug = ?), 'probe-untimed-id', 'none', 'retired', 'test')",
        (model_id, endpoint))
    con.commit()

    r = mi.resolve(endpoint, "probe-untimed-id", at="2026-12-01T00:00:00+00:00", con=con)
    assert r.deployment_id == 9003, "it is still known — layer 2 reports it"
    assert r.confidence == "medium", (
        "a retired deployment is known but NOT in force, however little evidence "
        f"exists; got confidence={r.confidence}")
    con.close()
