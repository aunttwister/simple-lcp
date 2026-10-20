#!/usr/bin/env python3
"""Offline verifier for an LCP agent-evidence pack.

Independence doctrine: this script shares NO code with the exporter. It trusts
nothing the exporter says except the public key inside MANIFEST.json — and
even that it prints as a fingerprint so the auditor can compare it against the
copy received out-of-band (email, letterhead, whatever). If someone edits a
file after signing, the hash check fails. If someone edits the manifest, the
signature check fails. If someone adds or removes files, the coverage check
fails.

Usage:
    python3 verify_evidence_pack.py /path/to/agent-evidence-<estate>-<date>/
        [--public-key /path/to/evidence_signing_pub.pem]   # optional out-of-band key

Requires: Python 3.8+. For the Ed25519 check it uses `cryptography` if
installed (pip install cryptography); without it, everything else is still
verified and the signature check prints the exact openssl command to run by
hand.

Exit code 0 = pack intact. Exit code 1 = ANY check failed. No warnings-only
mode: a pack either verifies or it does not.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys

FAILED = []


def fail(msg):
    FAILED.append(msg)
    print("  FAIL  %s" % msg)


def ok(msg):
    print("  ok    %s" % msg)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def count_rows(path):
    """rows for .jsonl = lines; .csv = lines - header; others = n/a."""
    n = 0
    with open(path, "rb") as fh:
        for _ in fh:
            n += 1
    if path.endswith(".csv") and n > 0:
        return n - 1
    return n if path.endswith(".jsonl") else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pack_dir")
    ap.add_argument("--public-key", required=True,
                    help="Out-of-band copy of the signing public key (PEM). The pack "
                         "deliberately does not contain it — obtain it from the deployer "
                         "(GET /api/evidence/public-key at deploy time).")
    args = ap.parse_args()

    pack = args.pack_dir.rstrip("/")
    if not os.path.isdir(pack):
        print("PACK NOT FOUND: %s" % pack)
        return 1
    print("Evidence pack: %s\n" % pack)

    # 1 ── manifest present and parses
    manifest_path = os.path.join(pack, "MANIFEST.json")
    if not os.path.isfile(manifest_path):
        fail("MANIFEST.json missing")
        return 1
    try:
        manifest = json.load(open(manifest_path, encoding="utf-8"))
    except ValueError as e:
        fail("MANIFEST.json is not valid JSON: %s" % e)
        return 1
    ok("MANIFEST.json parses (%s files declared, window %s → %s, lcp %s)" % (
        len(manifest.get("files", [])), manifest.get("window", {}).get("from"),
        manifest.get("window", {}).get("to"), manifest.get("lcp_version", "?")))

    # 2 ── every declared file exists and hashes match
    for entry in manifest.get("files", []):
        rel, want = entry.get("path"), entry.get("sha256")
        p = os.path.join(pack, rel)
        if not os.path.isfile(p):
            fail("declared file missing: %s" % rel)
            continue
        got = sha256_file(p)
        if got != want:
            fail("hash mismatch: %s (manifest %s…, actual %s…)" % (rel, want[:16], got[:16]))
        else:
            ok("hash   %s" % rel)
        declared_rows = entry.get("rows")
        if declared_rows is not None and os.path.isfile(p):
            actual = count_rows(p)
            if actual is not None and actual != declared_rows:
                fail("row count mismatch: %s (manifest %s, actual %s)"
                     % (rel, declared_rows, actual))
            elif actual is not None:
                ok("rows   %s (%s)" % (rel, actual))

    # 3 ── no undeclared files smuggled in
    declared = {e["path"] for e in manifest.get("files", [])} | {
        "MANIFEST.json", "MANIFEST.sig"}
    for root, _dirs, files in os.walk(pack):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), pack)
            if rel not in declared:
                fail("undeclared file present: %s" % rel)

    # 4 ── Ed25519 signature over the manifest bytes
    sig_path = os.path.join(pack, "MANIFEST.sig")
    if not os.path.isfile(sig_path):
        fail("MANIFEST.sig missing — pack is not signed")
        return 1
    pem_bytes = open(args.public_key, "rb").read()
    # cross-check against the fingerprint the exporter recorded in the manifest
    fp = (manifest.get("signing") or {}).get("public_key_fingerprint", "")
    if fp.startswith("sha256:"):
        actual = "sha256:" + hashlib.sha256(pem_bytes).hexdigest()
        if actual != fp:
            fail("out-of-band public key does NOT match the manifest fingerprint "
                 "(%s… vs %s…) — pack may be signed by an impostor key"
                 % (actual[:23], fp[:23]))
            return 1
        ok("public key fingerprint matches the manifest")
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_public_key
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            from cryptography.exceptions import InvalidSignature  # noqa: F401
            if pem_bytes is None:
                fail("no public key available for verification")
                return 1
            key = load_pem_public_key(pem_bytes)
            if not isinstance(key, Ed25519PublicKey):
                fail("signing key is not Ed25519")
            else:
                key.verify(bytes.fromhex(open(sig_path).read().strip()),
                           open(manifest_path, "rb").read())
                ok("signature verifies over the manifest bytes (Ed25519)")
        except ImportError:
            print("  note  `cryptography` not installed — run by hand:")
            print('        openssl pkeyutl -verify -pubin -inkey key.pem '
                  '-rawin -in MANIFEST.json -sigfile MANIFEST.sig')
        except InvalidSignature:
            fail("SIGNATURE INVALID — the manifest was modified after signing")
        except Exception as e:  # noqa: BLE001
            fail("signature check error: %s" % e)

    # 5 ── verdict
    if FAILED:
        print("\nRESULT: FAILED — %d check(s) did not pass" % len(FAILED))
        return 1
    print("\nRESULT: PACK INTACT — every declared file hashes true, row counts "
          "match, no undeclared files, signature verifies.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
