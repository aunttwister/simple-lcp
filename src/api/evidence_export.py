"""Evidence-pack exporter — the artifact handed to an auditor or a court.

Builds the pack layout defined in EVIDENCE-EXPORT-SPEC.md from the live
gateway DB, read **read-only** (``file:...?mode=ro``), and signs the
MANIFEST.json with Ed25519 so the pack is tamper-evident offline:

    agent-evidence-<estate>-<YYYYMMDD>/
    ├── MANIFEST.json          # every file, sha256, row count, LCP version, window
    ├── MANIFEST.sig           # Ed25519 signature over MANIFEST.json
    ├── README.md              # offline verification, no tooling of ours required
    ├── policies/              # profiles.json + policy-hashes.txt
    ├── agents.csv             # key_prefix ONLY — never the hash, never the key
    ├── actions.jsonl / refusals.jsonl / decisions.jsonl / failovers.jsonl
    ├── health.jsonl / model-registry.csv / retention.json

Two invariants that make this evidence rather than a log dump:

1. The manifest contains the sha256 of every file and is signed; anyone with
   the public key (shipped separately) can verify offline.
2. The refusals are in the pack — what the agent was NOT allowed to do
   (``requests.tools_blocked``) is what an auditor actually asks about.

No secret ever enters the pack: API keys appear as ``key_prefix`` only.
Message-level content (``conv_messages``) is deliberately NOT exported.
"""

import csv
import hashlib
import io
import json
import os
import socket
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .logging_config import get_logger

logger = get_logger("lcp.evidence_export")

# ── Configuration ────────────────────────────────────────────────────────────

# Env var holding the PATH to the Ed25519 private key (PEM). The public key is
# shipped to the auditor separately — it is deliberately not part of the pack.
SIGNING_KEY_ENV = "LCP_EVIDENCE_SIGNING_KEY"

# Env var naming the estate the pack belongs to (defaults to the hostname).
ESTATE_ENV = "LCP_ESTATE"

# Default fallback location for a generated signing key (per-install).
_FALLBACK_KEY_FILE = "evidence_signing_key.pem"

FORMAT_NAME = "lcp-evidence-pack"
FORMAT_VERSION = 1

# The export files, in manifest order. Row counts are measured per export,
# never hardcoded (the spec says this explicitly).
DATA_FILES = (
    "actions.jsonl",
    "refusals.jsonl",
    "decisions.jsonl",
    "failovers.jsonl",
    "health.jsonl",
    "model-registry.csv",
    "agents.csv",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect_ro(db_path: str) -> sqlite3.Connection:
    """Open the gateway DB strictly read-only. A missing file raises."""
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _jsonl(lines: list[dict]) -> bytes:
    """One JSON object per line, keys in a stable (insertion) order."""
    if not lines:
        return b""
    return "".join(
        json.dumps(line, ensure_ascii=False, sort_keys=False) + "\n" for line in lines
    ).encode("utf-8")


def _csv_bytes(header: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _default_estate() -> str:
    estate = os.environ.get(ESTATE_ENV)
    if estate:
        return estate
    try:
        return socket.gethostname() or "lcp"
    except Exception:  # noqa: BLE001 — a name must never break an export
        return "lcp"


def _profiles_from_config(config: Any) -> dict:
    """Accept a Config object, a dict, or None; return the profiles section."""
    if config is None:
        return {}
    try:
        profiles = config.profiles  # Config property / MagicMock
    except AttributeError:
        profiles = config.get("profiles", {}) if isinstance(config, dict) else {}
    return profiles if isinstance(profiles, dict) else {}


def _profile_routing(profile_cfg: dict, config: Any) -> Any:
    """Per-profile dynamic-routing setting, falling back to the global one."""
    if "dynamic_routing" in profile_cfg:
        return profile_cfg["dynamic_routing"]
    try:
        return config.dynamic_routing
    except AttributeError:
        return None


# ── Extraction (each returns (bytes, row_count, window_hint)) ───────────────


def _extract_actions(conn: sqlite3.Connection) -> tuple[bytes, int]:
    rows = conn.execute(
        "SELECT timestamp, profile, model, provider, prompt_tokens, "
        "completion_tokens, cache_hit_tokens, cache_miss_tokens, cost, "
        "latency_ms, success, error_type, tools_blocked, conversation_id "
        "FROM requests ORDER BY id"
    ).fetchall()
    lines = [
        {
            "timestamp": r[0], "profile": r[1], "model": r[2], "provider": r[3],
            "prompt_tokens": r[4], "completion_tokens": r[5],
            "cache_hit_tokens": r[6], "cache_miss_tokens": r[7],
            "cost": r[8], "latency_ms": r[9], "success": bool(r[10]),
            "error_type": r[11], "tools_blocked": r[12],
            "conversation_id": r[13],
        }
        for r in rows
    ]
    return _jsonl(lines), len(lines)


def _extract_refusals(conn: sqlite3.Connection) -> tuple[bytes, int]:
    """What the agent was NOT allowed to do — the auditor's first question.

    One line per request that carried blocked tools. Rows with an empty or
    NULL tools_blocked are the bulk of the log and are deliberately excluded.
    """
    rows = conn.execute(
        "SELECT timestamp, profile, conversation_id, tools_blocked "
        "FROM requests WHERE tools_blocked IS NOT NULL AND tools_blocked != '' "
        "ORDER BY id"
    ).fetchall()
    lines = [
        {"timestamp": r[0], "profile": r[1], "conversation_id": r[2],
         "tools_blocked": r[3]}
        for r in rows
    ]
    return _jsonl(lines), len(lines)


def _extract_decisions(conn: sqlite3.Connection) -> tuple[bytes, int]:
    rows = conn.execute(
        "SELECT ts, profile, task, policy, action, provider, model, score, "
        "rules_json, from_provider, from_model, note, path, keyword, "
        "semantic_json, min_score, sem_available, conversation_id "
        "FROM routing_decisions ORDER BY id"
    ).fetchall()
    lines = [
        {
            "ts": r[0], "profile": r[1], "task": r[2], "policy": r[3],
            "action": r[4], "provider": r[5], "model": r[6], "score": r[7],
            "rules_json": r[8], "from_provider": r[9], "from_model": r[10],
            "note": r[11], "path": r[12], "keyword": r[13],
            "semantic_json": r[14], "min_score": r[15],
            "sem_available": bool(r[16]) if r[16] is not None else None,
            "conversation_id": r[17],
        }
        for r in rows
    ]
    return _jsonl(lines), len(lines)


def _extract_failovers(conn: sqlite3.Connection) -> tuple[bytes, int]:
    rows = conn.execute(
        "SELECT timestamp, profile, from_provider, to_provider, reason, "
        "error_message FROM failover_events ORDER BY id"
    ).fetchall()
    lines = [
        {"timestamp": r[0], "profile": r[1], "from_provider": r[2],
         "to_provider": r[3], "reason": r[4], "error_message": r[5]}
        for r in rows
    ]
    return _jsonl(lines), len(lines)


def _extract_health(conn: sqlite3.Connection) -> tuple[bytes, int]:
    rows = conn.execute(
        "SELECT provider, base_url, profile, status, consecutive_failures, "
        "last_failure, last_failure_reason, last_success, tripped_until, "
        "manual_override FROM provider_health ORDER BY id"
    ).fetchall()
    lines = [
        {"provider": r[0], "base_url": r[1], "profile": r[2], "status": r[3],
         "consecutive_failures": r[4], "last_failure": r[5],
         "last_failure_reason": r[6], "last_success": r[7],
         "tripped_until": r[8], "manual_override": r[9]}
        for r in rows
    ]
    return _jsonl(lines), len(lines)


def _extract_model_registry(conn: sqlite3.Connection) -> tuple[bytes, int]:
    """Logical model → provider mapping + capability scores + release label.

    Left join: a registry row with no capability rows still appears (with
    empty capability columns) so the pack is complete against the registry.
    """
    header = ["logical_name", "provider_mappings", "active_release",
              "task_type", "score", "source", "benchmark_category",
              "release_label"]
    rows_out: list[list] = []
    if _table_exists(conn, "model_registry"):
        cap = ("model_capabilities" if _table_exists(conn, "model_capabilities")
               else None)
        if cap:
            rows = conn.execute(
                "SELECT r.logical_name, r.provider_mappings_json, "
                "r.active_release, c.task_type, c.score, c.source, "
                "c.benchmark_category, c.release_label "
                "FROM model_registry r LEFT JOIN model_capabilities c "
                "ON c.model = r.benchmark_key ORDER BY r.logical_name, c.id"
            ).fetchall()
        else:
            rows = [(r[0], r[1], r[2], None, None, None, None, None)
                    for r in conn.execute(
                        "SELECT logical_name, provider_mappings_json, "
                        "active_release FROM model_registry ORDER BY "
                        "logical_name")]
        rows_out = [list(r) for r in rows]
    return _csv_bytes(header, rows_out), len(rows_out)


def _extract_agents(conn: sqlite3.Connection) -> tuple[bytes, int]:
    """Agent identities — key_prefix only. NEVER the hash, NEVER the key."""
    header = ["key_prefix", "name", "allowed_profiles", "spend_limit",
              "total_spend", "status", "created_at", "last_used_at",
              "expires_at"]
    rows = conn.execute(
        "SELECT key_prefix, name, allowed_profiles, spend_limit, total_spend, "
        "status, created_at, last_used_at, expires_at FROM api_keys ORDER BY id"
    ).fetchall()
    return _csv_bytes(header, [list(r) for r in rows]), len(rows)


def _extract_retention(conn: sqlite3.Connection) -> bytes:
    """Retention report.

    The live DB has no retention producer yet (no expiry engine — see the
    spec's gap table), so the pack ships an explicit, signed record that
    nothing has been deleted, rather than a file an auditor could mistake for
    a real deletion log.
    """
    policy = None
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'retention_policy'"
        ).fetchone()
        if row:
            policy = json.loads(row[0])
    except Exception:  # noqa: BLE001 — a missing table/row is the expected case
        policy = None
    return json.dumps({
        "policy": policy,
        "deleted": [],
        "note": "No retention engine is deployed; nothing has been "
                "expired under policy. This file will carry real deletion "
                "records once the retention engine exists.",
        "generated_at": _utcnow(),
    }, indent=2).encode("utf-8") + b"\n"


def _extract_policies(config: Any) -> tuple[dict, bytes]:
    """Profile → forbidden_tools, chain, routing, intents, auth_required.

    Returns (profiles_dict, policy-hashes.txt bytes).
    """
    profiles = _profiles_from_config(config)
    out: dict = {}
    for name, pcfg in profiles.items():
        pcfg = pcfg if isinstance(pcfg, dict) else {}
        out[name] = {
            "forbidden_tools": pcfg.get("forbidden_tools") or [],
            "chain": pcfg.get("chain") or [],
            "routing": _profile_routing(pcfg, config),
            "intents": pcfg.get("intents") or [],
            "auth_required": bool(pcfg.get("auth_required", False)),
        }
    profiles_json = json.dumps(out, indent=2, ensure_ascii=False,
                               sort_keys=True).encode("utf-8") + b"\n"
    # Hash each profile's policy canonically (sorted-key compact JSON) and
    # name the config row it came from, so an auditor can re-derive it.
    lines = [
        "# sha256 per profile policy — canonical JSON (sorted keys, compact)",
        "# source: gateway_config:profiles (settings table, DB-backed config)",
    ]
    for name in sorted(out):
        canonical = json.dumps(out[name], sort_keys=True,
                               ensure_ascii=False, separators=(",", ":"))
        lines.append(f"{_sha256(canonical.encode('utf-8'))}  {name}")
    return out, "\n".join(lines).encode("utf-8") + b"\n"


# ── Signing ──────────────────────────────────────────────────────────────────


def _resolve_signing_key(signing_key_path: Optional[str],
                         data_dir: str) -> Any:
    """Return the Ed25519 private key object.

    Priority: explicit argument → ``LCP_EVIDENCE_SIGNING_KEY`` (PEM path) →
    a per-install generated key persisted in ``data_dir`` (0600). The private
    key never enters the pack; the public key is shipped separately.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    path = signing_key_path or os.environ.get(SIGNING_KEY_ENV)
    if path:
        with open(path, "rb") as fh:
            return serialization.load_pem_private_key(fh.read(), password=None)

    # No configured key: generate once per install and persist it.
    fallback = Path(data_dir) / _FALLBACK_KEY_FILE
    if fallback.exists():
        with open(fallback, "rb") as fh:
            return serialization.load_pem_private_key(fh.read(), password=None)
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(fallback, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, pem)
    finally:
        os.close(fd)
    logger.warning("evidence_signing_key_generated", path=str(fallback),
                   hint=f"Set {SIGNING_KEY_ENV} to control the signing key; "
                        "ship the .pub to the auditor separately.")
    return key


def _public_key_pem(private_key: Any) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _public_key_fingerprint(private_key: Any) -> str:
    pub = _public_key_pem(private_key)
    return "sha256:" + _sha256(pub)


def verify_pack(pack_dir: str, public_key_path: str) -> bool:
    """Offline verification, mirroring README.md: hash every file, then check
    the Ed25519 signature over MANIFEST.json."""
    pack = Path(pack_dir)
    manifest_bytes = (pack / "MANIFEST.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    for entry in manifest["files"]:
        rel = entry["path"]
        digest = _sha256((pack / rel).read_bytes())
        if digest != entry["sha256"]:
            return False
        if entry.get("bytes") is not None and \
                (pack / rel).stat().st_size != entry["bytes"]:
            return False
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    with open(public_key_path, "rb") as fh:
        pub = serialization.load_pem_public_key(fh.read())
    sig = bytes.fromhex((pack / "MANIFEST.sig").read_text().strip())
    if not isinstance(pub, ed25519.Ed25519PublicKey):
        return False
    try:
        pub.verify(sig, manifest_bytes)
    except Exception:  # noqa: BLE001 — invalid signature is a False, not a raise
        return False
    return True


# ── README (offline verification, no tooling of ours required) ───────────────

_README = """# Evidence pack — {estate} — {generated_at}

Verify offline. You need `sha256sum`, `jq`, and any Ed25519 verifier
(python3 + the `cryptography` package is used below; openssl >= 1.1.1 also
works). No LCP tooling required.

1. Unpack nothing — verify in place.

2. Check every file hash against the manifest:

       jq -r '.files[] | "\\(.sha256)  \\(.path)"' MANIFEST.json | sha256sum -c -

3. Check the manifest signature (Ed25519, over MANIFEST.json exactly):

       python3 -c "import sys; from cryptography.hazmat.primitives import serialization; \\
       from cryptography.hazmat.primitives.asymmetric import ed25519; \\
       pub = serialization.load_pem_public_key(open('public_key.pem','rb').read()); \\
       sig = bytes.fromhex(open('MANIFEST.sig').read().strip()); \\
       pub.verify(sig, open('MANIFEST.json','rb').read()); print('signature OK')"

4. The public key (`public_key.pem`) is NOT in this pack — it was shipped
   separately. Match its fingerprint against the manifest:

       jq -r .signing.public_key_fingerprint MANIFEST.json
       python3 -c "import hashlib; print('sha256:' + hashlib.sha256(open('public_key.pem','rb').read()).hexdigest())"

5. If any hash or the signature fails, the pack was edited after signing.

Contents: actions.jsonl (every completed call), refusals.jsonl (every blocked
tool call), decisions.jsonl (routing), failovers.jsonl, health.jsonl,
model-registry.csv, agents.csv (key PREFIXES only — never full keys),
policies/ (the policy that governed the agents and its per-profile hashes),
retention.json (what was deleted, when, under which policy).
"""


# ── The pack ─────────────────────────────────────────────────────────────────


def generate_evidence_pack(db_path: str,
                           out_dir: Optional[str] = None,
                           config: Any = None,
                           data_dir: str = "data",
                           estate: Optional[str] = None,
                           signing_key_path: Optional[str] = None,
                           now: Optional[str] = None) -> dict:
    """Build and sign an evidence pack. Returns the manifest as a dict.

    ``now`` (ISO timestamp) is injectable for deterministic tests.
    """
    generated_at = now or _utcnow()
    day = generated_at[:10].replace("-", "")
    estate_name = estate or _default_estate()
    pack_dir = Path(out_dir or os.environ.get(
        "LCP_EVIDENCE_OUTPUT_DIR", "data/evidence-packs")) / \
        f"agent-evidence-{estate_name}-{day}"
    pack_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []
    window: list[str] = []
    row_counts: dict[str, int] = {}

    def _write(rel: str, data: bytes, rows: int,
              timestamps: Optional[list] = None) -> None:
        path = pack_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        entries.append({
            "path": rel,
            "sha256": _sha256(data),
            "bytes": len(data),
            "rows": rows,
        })
        if timestamps:
            window.extend(t for t in timestamps if t)

    conn = _connect_ro(db_path)
    try:
        for rel, extractor in (
            ("actions.jsonl", _extract_actions),
            ("refusals.jsonl", _extract_refusals),
            ("decisions.jsonl", _extract_decisions),
            ("failovers.jsonl", _extract_failovers),
            ("health.jsonl", _extract_health),
            ("model-registry.csv", _extract_model_registry),
            ("agents.csv", _extract_agents),
        ):
            table_missing = False
            try:
                data, n = extractor(conn)
            except sqlite3.OperationalError:
                # Table absent in this install: export an empty file rather
                # than fail the whole pack (row counts are re-measured anyway).
                table_missing = True
                data, n = (b"" if rel.endswith(".jsonl") else
                           _csv_bytes([], [])), 0
            _write(rel, data, n)
            row_counts[rel] = n
            if table_missing:
                logger.warning("evidence_table_missing", table=rel)

        # Timestamp window over the activity tables (spec: "window").
        for ts_col, table in (("timestamp", "requests"),
                              ("ts", "routing_decisions"),
                              ("timestamp", "failover_events")):
            if _table_exists(conn, table):
                lo, hi = conn.execute(
                    f"SELECT MIN({ts_col}), MAX({ts_col}) FROM {table}"
                ).fetchone()
                if lo:
                    window.append(lo)
                if hi:
                    window.append(hi)

        retention_bytes = _extract_retention(conn)
    finally:
        conn.close()

    retention_doc = json.loads(retention_bytes.decode("utf-8"))
    _write("retention.json", retention_bytes, len(retention_doc["deleted"]))

    profiles, policy_hashes = _extract_policies(config)
    profiles_bytes = json.dumps(profiles, indent=2, ensure_ascii=False,
                                sort_keys=True).encode("utf-8") + b"\n"
    _write("policies/profiles.json", profiles_bytes, len(profiles))
    _write("policies/policy-hashes.txt", policy_hashes, len(profiles))

    readme = _README.format(estate=estate_name,
                            generated_at=generated_at).encode("utf-8")
    _write("README.md", readme, 0)

    # ── Manifest + signature ────────────────────────────────────────────────
    import src
    manifest = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "lcp_version": getattr(src, "__version__", "unknown"),
        "estate": estate_name,
        "generated_at": generated_at,
        "source_db": Path(db_path).name,
        "window": {"from": min(window) if window else None,
                   "to": max(window) if window else None},
        "row_counts": row_counts,
        "signing": {
            "algorithm": "Ed25519",
            "public_key_fingerprint": None,  # set below
        },
        "files": entries,
    }

    private_key = _resolve_signing_key(signing_key_path, data_dir)
    manifest["signing"]["public_key_fingerprint"] = \
        _public_key_fingerprint(private_key)
    manifest_bytes = json.dumps(manifest, indent=2, ensure_ascii=False,
                                sort_keys=True).encode("utf-8") + b"\n"
    (pack_dir / "MANIFEST.json").write_bytes(manifest_bytes)
    signature = private_key.sign(manifest_bytes)
    (pack_dir / "MANIFEST.sig").write_text(signature.hex() + "\n")

    logger.info("evidence_pack_built", pack=str(pack_dir),
                files=len(entries),
                actions=row_counts.get("actions.jsonl", 0),
                refusals=row_counts.get("refusals.jsonl", 0))
    return manifest
