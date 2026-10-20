"""Evidence-pack exporter — src/api/evidence_export.py + its endpoints.

Spec: EVIDENCE-EXPORT-SPEC.md. The two properties that make the pack
*evidence* rather than a log dump are both asserted here:

1. Hash-chained and signed — every file's sha256 is in MANIFEST.json and
   MANIFEST.sig is a real Ed25519 signature over MANIFEST.json bytes,
   verifiable with an independent verifier (and by ``verify_pack``).
2. The refusals are in it — refusals.jsonl carries exactly the rows that
   carried ``tools_blocked``, nothing else.

Plus the invariant that must never break: no key material in the pack —
agents.csv carries key_prefix ONLY (never the hash, never the key).
"""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from src.api.evidence_export import (
    generate_evidence_pack,
    verify_pack,
)

# ── Fixture: a raw SQLite gateway DB with rows in every exported table ───────

SCHEMA = """
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
    profile TEXT NOT NULL DEFAULT 'unknown', model TEXT NOT NULL,
    provider TEXT NOT NULL, prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0, cache_hit_tokens INTEGER DEFAULT 0,
    cache_miss_tokens INTEGER DEFAULT 0, cost REAL DEFAULT 0.0,
    latency_ms INTEGER DEFAULT 0, success INTEGER DEFAULT 1,
    error_type TEXT, tools_blocked TEXT, error_detail TEXT,
    conversation_id TEXT);
CREATE TABLE routing_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts VARCHAR NOT NULL,
    profile VARCHAR NOT NULL, task VARCHAR NOT NULL, policy VARCHAR NOT NULL,
    action VARCHAR NOT NULL, provider VARCHAR, model VARCHAR, score FLOAT,
    rules_json TEXT, from_provider VARCHAR, from_model VARCHAR, note TEXT,
    path VARCHAR, keyword VARCHAR, intent_text TEXT, semantic_json TEXT,
    min_score FLOAT, sem_available BOOLEAN, conversation_id TEXT,
    l4_json TEXT);
CREATE TABLE failover_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp VARCHAR NOT NULL,
    profile VARCHAR NOT NULL, from_provider VARCHAR NOT NULL,
    to_provider VARCHAR NOT NULL, reason VARCHAR NOT NULL,
    error_message TEXT, request_id INTEGER);
CREATE TABLE provider_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT, provider VARCHAR NOT NULL,
    base_url VARCHAR NOT NULL, profile VARCHAR NOT NULL,
    status VARCHAR NOT NULL, consecutive_failures INTEGER NOT NULL,
    last_failure VARCHAR, last_failure_reason TEXT, last_success VARCHAR,
    tripped_until FLOAT, manual_override VARCHAR, updated_at VARCHAR NOT NULL);
CREATE TABLE model_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT, logical_name VARCHAR NOT NULL,
    benchmark_key VARCHAR NOT NULL, updated_at VARCHAR NOT NULL,
    active_release VARCHAR, provider_mappings_json TEXT DEFAULT '{}',
    benchmark_release VARCHAR, quantization VARCHAR, UNIQUE (logical_name));
CREATE TABLE model_capabilities (
    id INTEGER PRIMARY KEY AUTOINCREMENT, model VARCHAR NOT NULL,
    task_type VARCHAR NOT NULL, score FLOAT NOT NULL,
    source VARCHAR DEFAULT 'livebench' NOT NULL, benchmark_category VARCHAR,
    raw_score FLOAT, updated_at VARCHAR NOT NULL, release_label VARCHAR);
CREATE TABLE api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key_hash TEXT UNIQUE NOT NULL,
    key_prefix TEXT NOT NULL, name TEXT NOT NULL, allowed_profiles TEXT,
    spend_limit REAL DEFAULT 0.0, total_spend REAL DEFAULT 0.0,
    status TEXT DEFAULT 'active', created_at TEXT NOT NULL,
    last_used_at TEXT, expires_at TEXT, revoked_at TEXT, metadata_tags TEXT);
CREATE TABLE settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, "key" VARCHAR NOT NULL,
    value TEXT NOT NULL, updated_at VARCHAR NOT NULL);
"""


@pytest.fixture
def gw_db(tmp_path):
    """A gateway DB with known rows; returns (path, connection)."""
    db = tmp_path / "costs.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO requests (timestamp, profile, model, provider, "
        "prompt_tokens, completion_tokens, cache_hit_tokens, "
        "cache_miss_tokens, cost, latency_ms, success, error_type, "
        "tools_blocked, conversation_id) VALUES "
        "('2026-10-01T10:00:00Z','l2','m1','p1',10,5,2,1,0.01,120,1,NULL,"
        "NULL,'conv-1'),"
        "('2026-10-02T11:30:00Z','l1','m2','p1',20,8,0,3,0.02,300,0,"
        "'rate_limit_error','write_file,patch','conv-2')"
    )
    conn.execute(
        "INSERT INTO routing_decisions (ts, profile, task, policy, action, "
        "provider, model, score, rules_json, from_provider, from_model, note, "
        "path, keyword, semantic_json, min_score, sem_available, "
        "conversation_id) VALUES "
        "('2026-10-01T10:00:01Z','l2','code','keyword','allow','p1','m1',"
        "0.9,'[]','p0','m0','ok','chain','fix','',0.5,1,'conv-1')"
    )
    conn.execute(
        "INSERT INTO failover_events (timestamp, profile, from_provider, "
        "to_provider, reason, error_message) VALUES "
        "('2026-10-01T10:00:02Z','l2','p1','p2','timeout','boom')"
    )
    conn.execute(
        "INSERT INTO provider_health (provider, base_url, profile, status, "
        "consecutive_failures, last_failure, last_failure_reason, "
        "last_success, tripped_until, manual_override, updated_at) VALUES "
        "('p1','https://p1/v1','l2','healthy',0,NULL,NULL,"
        "'2026-10-01T10:00:00Z',NULL,NULL,'2026-10-01T10:00:00Z')"
    )
    conn.execute(
        "INSERT INTO model_registry (logical_name, benchmark_key, updated_at, "
        "active_release, provider_mappings_json) VALUES "
        "('m1','bench-m1','2026-10-01T00:00:00Z','v9','{\"p1\": [\"m1\"]}'),"
        "('m-orphan','bench-orphan','2026-10-01T00:00:00Z','v1','{}')"
    )
    conn.execute(
        "INSERT INTO model_capabilities (model, task_type, score, source, "
        "benchmark_category, updated_at, release_label) VALUES "
        "('bench-m1','code',0.91,'livebench','coding','2026-10-01T00:00:00Z',"
        "'r-label')"
    )
    conn.execute(
        "INSERT INTO api_keys (key_hash, key_prefix, name, allowed_profiles, "
        "spend_limit, total_spend, status, created_at, last_used_at, "
        "expires_at) VALUES "
        "('sha256hex-DO-NOT-EXPORT','lcp_sk_abc','Research agent','l2,l1',"
        "10.0,1.5,'active','2026-09-01T00:00:00Z','2026-10-01T10:00:00Z',NULL)"
    )
    conn.execute(
        "INSERT INTO settings (\"key\", value, updated_at) VALUES "
        "('retention_policy','{\"days\": 90}','2026-10-01T00:00:00Z')"
    )
    conn.commit()
    yield str(db), conn
    conn.close()


def _export_pack(tmp_path, db_path, config=None, **kw):
    return generate_evidence_pack(
        db_path=db_path, out_dir=str(tmp_path / "packs"), config=config,
        data_dir=str(tmp_path / "data"), estate="testestate",
        now="2026-10-20T12:00:00Z", **kw)


def _find_pack(tmp_path) -> Path:
    packs = list((tmp_path / "packs").glob("agent-evidence-*"))
    assert len(packs) == 1
    return packs[0]


# ── Layout ───────────────────────────────────────────────────────────────────


class TestPackLayout:
    def test_spec_files_all_present(self, tmp_path, gw_db):
        db_path, _ = gw_db
        _export_pack(tmp_path, db_path)
        pack = _find_pack(tmp_path)
        for rel in (
            "MANIFEST.json", "MANIFEST.sig", "README.md",
            "policies/profiles.json", "policies/policy-hashes.txt",
            "agents.csv", "actions.jsonl", "refusals.jsonl",
            "decisions.jsonl", "failovers.jsonl", "health.jsonl",
            "model-registry.csv", "retention.json",
        ):
            assert (pack / rel).exists(), rel
        # Deliberate absences: no message content, no key material files.
        assert not (pack / "conv_messages.jsonl").exists()

    def test_manifest_hashes_match_files(self, tmp_path, gw_db):
        db_path, _ = gw_db
        manifest = _export_pack(tmp_path, db_path)
        pack = _find_pack(tmp_path)
        assert manifest["format"] == "lcp-evidence-pack"
        assert manifest["estate"] == "testestate"
        for entry in manifest["files"]:
            data = (pack / entry["path"]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == entry["sha256"], \
                entry["path"]
            assert len(data) == entry["bytes"]

    def test_signature_is_a_real_ed25519_signature(self, tmp_path, gw_db):
        """Verified independently of verify_pack: load the private key from
        the data dir and check the signature over the exact manifest bytes."""
        db_path, _ = gw_db
        _export_pack(tmp_path, db_path)
        pack = _find_pack(tmp_path)
        from cryptography.hazmat.primitives import serialization
        key = serialization.load_pem_private_key(
            (tmp_path / "data" / "evidence_signing_key.pem").read_bytes(),
            password=None)
        manifest_bytes = (pack / "MANIFEST.json").read_bytes()
        sig = bytes.fromhex((pack / "MANIFEST.sig").read_text().strip())
        key.public_key().verify(sig, manifest_bytes)  # raises on any edit
        # The manifest fingerprint matches the shipped private key's public.
        manifest = json.loads(manifest_bytes)
        pub_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        assert manifest["signing"]["public_key_fingerprint"] == \
            "sha256:" + hashlib.sha256(pub_pem).hexdigest()

    def test_tampered_file_fails_verification(self, tmp_path, gw_db):
        db_path, _ = gw_db
        _export_pack(tmp_path, db_path)
        pack = _find_pack(tmp_path)
        pub_pem = None
        from cryptography.hazmat.primitives import serialization
        key = serialization.load_pem_private_key(
            (tmp_path / "data" / "evidence_signing_key.pem").read_bytes(),
            password=None)
        pub_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        pub_path = tmp_path / "public_key.pem"
        pub_path.write_bytes(pub_pem)

        assert verify_pack(str(pack), str(pub_path)) is True

        # Edit one byte of one file → verification must fail.
        actions = pack / "actions.jsonl"
        actions.write_bytes(actions.read_bytes() + b'{"forged": true}\n')
        assert verify_pack(str(pack), str(pub_path)) is False

    def test_window_and_lcp_version_in_manifest(self, tmp_path, gw_db):
        db_path, _ = gw_db
        manifest = _export_pack(tmp_path, db_path)
        assert manifest["window"]["from"] == "2026-10-01T10:00:00Z"
        assert manifest["window"]["to"] == "2026-10-02T11:30:00Z"
        import src
        assert manifest["lcp_version"] == src.__version__

    def test_row_counts_measured_not_hardcoded(self, tmp_path, gw_db):
        db_path, _ = gw_db
        manifest = _export_pack(tmp_path, db_path)
        assert manifest["row_counts"]["actions.jsonl"] == 2
        assert manifest["row_counts"]["refusals.jsonl"] == 1
        assert manifest["row_counts"]["decisions.jsonl"] == 1
        assert manifest["row_counts"]["failovers.jsonl"] == 1
        assert manifest["row_counts"]["health.jsonl"] == 1
        assert manifest["row_counts"]["agents.csv"] == 1


# ── The refusal evidence ─────────────────────────────────────────────────────


class TestRefusalsAreInThePack:
    def test_only_rows_that_carried_blocks(self, tmp_path, gw_db):
        db_path, _ = gw_db
        _export_pack(tmp_path, db_path)
        pack = _find_pack(tmp_path)
        lines = [json.loads(l) for l in
                 (pack / "refusals.jsonl").read_text().splitlines()]
        assert len(lines) == 1
        assert lines[0] == {
            "timestamp": "2026-10-02T11:30:00Z",
            "profile": "l1",
            "conversation_id": "conv-2",
            "tools_blocked": "write_file,patch",
        }

    def test_null_and_empty_blocks_excluded(self, tmp_path, gw_db):
        db_path, conn = gw_db
        conn.execute(
            "INSERT INTO requests (timestamp, profile, model, provider, "
            "tools_blocked) VALUES ('2026-10-03T00:00:00Z','l2','m','p',"
            "'' )"
        )
        conn.commit()
        _export_pack(tmp_path, db_path)
        pack = _find_pack(tmp_path)
        lines = (pack / "refusals.jsonl").read_text().splitlines()
        assert len(lines) == 1  # the '' row is NOT a refusal


# ── Field fidelity ───────────────────────────────────────────────────────────


class TestFieldFidelity:
    def test_actions_fields_match_spec(self, tmp_path, gw_db):
        db_path, _ = gw_db
        _export_pack(tmp_path, db_path)
        pack = _find_pack(tmp_path)
        lines = [json.loads(l) for l in
                 (pack / "actions.jsonl").read_text().splitlines()]
        assert len(lines) == 2
        assert set(lines[0]) == {
            "timestamp", "profile", "model", "provider", "prompt_tokens",
            "completion_tokens", "cache_hit_tokens", "cache_miss_tokens",
            "cost", "latency_ms", "success", "error_type", "tools_blocked",
            "conversation_id"}
        assert lines[1]["success"] is False
        assert lines[1]["error_type"] == "rate_limit_error"

    def test_decisions_fields_match_spec(self, tmp_path, gw_db):
        db_path, _ = gw_db
        _export_pack(tmp_path, db_path)
        pack = _find_pack(tmp_path)
        line = json.loads((pack / "decisions.jsonl").read_text().splitlines()[0])
        assert set(line) == {
            "ts", "profile", "task", "policy", "action", "provider", "model",
            "score", "rules_json", "from_provider", "from_model", "note",
            "path", "keyword", "semantic_json", "min_score", "sem_available",
            "conversation_id"}
        assert line["sem_available"] is True

    def test_failovers_and_health_fields(self, tmp_path, gw_db):
        db_path, _ = gw_db
        _export_pack(tmp_path, db_path)
        pack = _find_pack(tmp_path)
        fo = json.loads((pack / "failovers.jsonl").read_text().splitlines()[0])
        assert set(fo) == {"timestamp", "profile", "from_provider",
                           "to_provider", "reason", "error_message"}
        h = json.loads((pack / "health.jsonl").read_text().splitlines()[0])
        assert set(h) == {"provider", "base_url", "profile", "status",
                          "consecutive_failures", "last_failure",
                          "last_failure_reason", "last_success",
                          "tripped_until", "manual_override"}

    def test_model_registry_joins_capabilities_and_keeps_orphans(
            self, tmp_path, gw_db):
        db_path, _ = gw_db
        _export_pack(tmp_path, db_path)
        pack = _find_pack(tmp_path)
        rows = list(csv_rows(pack / "model-registry.csv"))
        # 2 registry rows; the orphan survives the LEFT JOIN with empty caps.
        assert len(rows) == 2
        m1 = [r for r in rows if r["logical_name"] == "m1"][0]
        assert m1["provider_mappings"] == '{"p1": ["m1"]}'
        assert m1["active_release"] == "v9"
        assert m1["task_type"] == "code"
        assert m1["score"] == "0.91"
        assert m1["release_label"] == "r-label"
        orphan = [r for r in rows if r["logical_name"] == "m-orphan"][0]
        assert orphan["task_type"] == ""


def csv_rows(path: Path):
    import csv
    with open(path, newline="") as fh:
        yield from csv.DictReader(fh)


# ── The invariant: no key material in the pack ───────────────────────────────


class TestNoKeyMaterial:
    def test_agents_csv_has_prefix_not_hash(self, tmp_path, gw_db):
        db_path, _ = gw_db
        _export_pack(tmp_path, db_path)
        pack = _find_pack(tmp_path)
        text = (pack / "agents.csv").read_text()
        assert "lcp_sk_abc" in text  # key_prefix IS exported
        assert "sha256hex-DO-NOT-EXPORT" not in text  # the hash is NOT
        rows = list(csv_rows(pack / "agents.csv"))
        assert rows[0]["name"] == "Research agent"
        assert rows[0]["allowed_profiles"] == "l2,l1"
        assert rows[0]["spend_limit"] == "10.0"
        assert rows[0]["status"] == "active"

    def test_no_hash_column_anywhere(self, tmp_path, gw_db):
        db_path, _ = gw_db
        manifest = _export_pack(tmp_path, db_path)
        pack = _find_pack(tmp_path)
        for entry in manifest["files"]:
            data = (pack / entry["path"]).read_text(errors="ignore")
            assert "sha256hex-DO-NOT-EXPORT" not in data, entry["path"]


# ── Policies ─────────────────────────────────────────────────────────────────


class TestPolicies:
    def test_profiles_content_and_hashes(self, tmp_path, gw_db):
        db_path, _ = gw_db
        cfg = {"profiles": {
            "l2": {
                "forbidden_tools": ["write_file", "patch"],
                "chain": [{"provider": "p1", "model": "m1",
                           "base_url": "https://p1/v1"}],
                "auth_required": True,
                "intents": ["code planning"],
            },
        }}
        _export_pack(tmp_path, db_path, config=cfg)
        pack = _find_pack(tmp_path)
        profiles = json.loads((pack / "policies" / "profiles.json").read_text())
        assert profiles["l2"]["forbidden_tools"] == ["write_file", "patch"]
        assert profiles["l2"]["auth_required"] is True
        assert profiles["l2"]["intents"] == ["code planning"]
        assert profiles["l2"]["chain"][0]["provider"] == "p1"

        # policy-hashes.txt is re-derivable from profiles.json.
        for line in (pack / "policies" / "policy-hashes.txt").read_text() \
                .splitlines():
            if line.startswith("#"):
                continue
            digest, name = line.split()
            canonical = json.dumps(profiles[name], sort_keys=True,
                                   ensure_ascii=False, separators=(",", ":"))
            assert digest == hashlib.sha256(canonical.encode()).hexdigest()

    def test_config_object_with_profiles_property(self, tmp_path, gw_db):
        db_path, _ = gw_db

        class Cfg:
            profiles = {"l1": {"forbidden_tools": ["terminal"]}}
            dynamic_routing = {"enabled": True, "cost_bias": 0.2}

        _export_pack(tmp_path, db_path, config=Cfg())
        pack = _find_pack(tmp_path)
        profiles = json.loads((pack / "policies" / "profiles.json").read_text())
        assert profiles["l1"]["routing"] == {"enabled": True, "cost_bias": 0.2}
        assert profiles["l1"]["forbidden_tools"] == ["terminal"]


# ── Retention ────────────────────────────────────────────────────────────────


class TestRetention:
    def test_reports_policy_and_no_deletions_yet(self, tmp_path, gw_db):
        db_path, _ = gw_db
        _export_pack(tmp_path, db_path)
        pack = _find_pack(tmp_path)
        doc = json.loads((pack / "retention.json").read_text())
        assert doc["policy"] == {"days": 90}
        assert doc["deleted"] == []
        assert doc["note"]

    def test_missing_policy_table_is_not_fatal(self, tmp_path):
        db = tmp_path / "costs.db"
        conn = sqlite3.connect(db)
        conn.executescript(SCHEMA.replace(
            'CREATE TABLE settings (\n    id INTEGER PRIMARY KEY AUTOINCREMENT, "key" VARCHAR NOT NULL,\n    value TEXT NOT NULL, updated_at VARCHAR NOT NULL);',
            "CREATE TABLE settings (id INTEGER PRIMARY KEY, \"key\" VARCHAR, value TEXT, updated_at VARCHAR);"))
        conn.commit()
        conn.close()
        manifest = _export_pack(tmp_path, str(db))
        assert manifest["row_counts"]["actions.jsonl"] == 0

    def test_missing_activity_tables_export_empty(self, tmp_path):
        """A fresh install without all tables exports an empty pack, not an
        error — row counts are re-measured per export (spec line 30)."""
        db = tmp_path / "costs.db"
        conn = sqlite3.connect(db)
        conn.executescript("CREATE TABLE requests (id INTEGER PRIMARY KEY, "
                           "timestamp TEXT);")
        conn.commit()
        conn.close()
        manifest = _export_pack(tmp_path, str(db))
        assert manifest["row_counts"]["actions.jsonl"] == 0
        assert manifest["row_counts"]["agents.csv"] == 0


# ── Endpoints ────────────────────────────────────────────────────────────────


class TestEvidenceEndpoints:
    @pytest.fixture
    def handler_env(self, tmp_path, monkeypatch, gw_db):
        """A TestHandler bound to the gw_db engine, with the evidence output
        dir pointed at tmp and anonymous admin allowed for the request."""
        db_path, conn = gw_db
        conn.close()
        monkeypatch.setenv("LCP_ALLOW_ANONYMOUS_ADMIN", "1")
        monkeypatch.setenv("LCP_EVIDENCE_OUTPUT_DIR", str(tmp_path / "packs"))
        monkeypatch.setenv("LCP_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.chdir(tmp_path)

        from src.api.models import get_engine, Base
        from src.server import LCPHandler
        import src.api.admin_auth as admin_auth
        engine = get_engine(db_path)
        Base.metadata.create_all(engine)

        cfg = type("Cfg", (), {})()
        cfg.profiles = {"l2": {"forbidden_tools": ["write_file"]}}
        old_cfg = LCPHandler.config
        LCPHandler.config = cfg
        yield db_path
        LCPHandler.config = old_cfg
        engine.dispose()

    def test_export_endpoint_returns_manifest(self, handler_env, tmp_path):
        from src.server.handler import LCPHandler
        from tests.test_server import TestHandler

        class BoundHandler(TestHandler):
            pass

        BoundHandler.engine = None
        # Bind the raw sqlite path through the engine URL the mixin resolves.
        h = BoundHandler(path="/api/evidence/export", method="GET")
        import src.api.models as models
        eng = models.get_engine(handler_env)
        h.engine = eng
        LCPHandler._serve_evidence_export(h)
        assert h.send_response.call_args[0][0] == 200
        body = json.loads(h.wfile.write.call_args[0][0])
        assert body["status"] == "ok"
        assert body["manifest"]["format"] == "lcp-evidence-pack"
        assert body["manifest"]["estate"]

    def test_public_key_endpoint(self, handler_env):
        from src.server.handler import LCPHandler
        from tests.test_server import TestHandler

        class BoundHandler(TestHandler):
            pass

        h = BoundHandler(path="/api/evidence/public-key", method="GET")
        LCPHandler._serve_evidence_public_key(h)
        assert h.send_response.call_args[0][0] == 200
        body = json.loads(h.wfile.write.call_args[0][0])
        assert body["algorithm"] == "Ed25519"
        assert "BEGIN PUBLIC KEY" in body["public_key_pem"]
        assert body["fingerprint"].startswith("sha256:")

    def test_evidence_routes_are_registered(self):
        from src.server.handler import LCPHandler
        LCPHandler._routes = None
        table = LCPHandler._route_table()
        names = {r.name for r in table.rules}
        assert {"api.evidence.export", "api.evidence.public_key"} <= names
