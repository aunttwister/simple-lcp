#!/usr/bin/env python3
"""Seed the model-identity registry (CUR-270). Idempotent: safe to re-run.

    python3 scripts/seed_model_identity.py              # dry run (default)
    python3 scripts/seed_model_identity.py --apply
    python3 scripts/seed_model_identity.py --apply --verify-endpoints
    python3 scripts/seed_model_identity.py --db /tmp/costs.db --apply

Design rules this script follows
--------------------------------

* **desired-state, get-or-create**: every object is matched on a stable natural
  key (endpoint slug, model slug, ``(endpoint, served_id)``, ``(endpoint, alias,
  valid_from)``), never on a display name.
* **never invent a value.** Where a fact is not established, the field is left
  NULL and the object carries a note; ``verified_at`` stays NULL until the
  endpoint itself confirms the served id.
* **served ids are observed, not authored**: ``--verify-endpoints`` reads each
  endpoint's ``/v1/models`` (the same paths LCP uses, ``LCP_MODELS_PATHS``) and
  stamps ``verified_at``. An endpoint that cannot be reached is reported, not
  assumed.
* **no deletes.** Re-runs only create or update.
* the data below is the reviewed mapping: `STEP0-PROPOSED-MODEL-MAP.md` plus the
  provider-documented release timeline in `MODEL-IDENTITY-DESIGN.md` §2c.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.environ.get("COST_DB", "/app/data/costs.db")

# --------------------------------------------------------------------------- data

ENDPOINTS = [
    dict(slug="local-sparks", tier="local", status="active",
         display_name="DGX Spark pair (HP ZGX .107 + MSI EdgeXpert .108)",
         base_url="http://192.168.1.107:18300/v1", notes=None),
    dict(slug="local-zgx", tier="local", status="retired",
         display_name="HP ZGX local lane (retired — superseded by local-sparks)",
         base_url=None, notes="Retired local lane; kept for historical resolution."),
    dict(slug="llamacpp", tier="local", status="retired",
         display_name="llama.cpp lane (retired)", base_url=None,
         notes="Retired local lane; kept for historical resolution."),
    dict(slug="deepseek", tier="cloud", status="active",
         display_name="DeepSeek API", base_url="https://api.deepseek.com/v1", notes=None),
    dict(slug="opencode", tier="cloud", status="inactive",
         display_name="opencode.ai inference", base_url="https://opencode.ai/inference/openai/v1",
         notes="Inactive: subscription no longer active (operator, 2026-10-30). Last observed "
               "traffic 2026-09-29; the live catalogue no longer advertises the legacy "
               "deepseek-v4-flash name. Kept for historical resolution."),
    dict(slug="commandcode", tier="cloud", status="active",
         display_name="CommandCode", base_url="https://api.commandcode.ai/provider/v1",
         notes="Active. Its public /provider/v1/models is readable without credentials and is "
               "the observation source for its served ids."),
]

# legacy provider keys found in the registry's provider_mappings_json
ENDPOINT_ALIASES = [("zgx", "local-zgx")]

# slug, display_name, family, status, benchmark_key
MODELS = [
    ("deepseek-flash", "DeepSeek-V4.1-Flash", "deepseek", "active", "deepseek-v4.1-flash"),
    ("deepseek-v4-flash", "DeepSeek-V4-Flash", "deepseek", "retired", "deepseek-v4-flash"),
    ("deepseek-v4-pro", "DeepSeek-V4-Pro", "deepseek", "active", "deepseek-v4-pro"),
    ("glm-5.3-flash", "GLM-5.3-Flash", "glm", "active", None),
    ("qwen3.8-flash-next", "Qwen3.8-Flash-Next", "qwen", "active", "qwen3.8-flash-next"),
    ("qwen3.8-max", "Qwen3.8-Max", "qwen", "active", "qwen-3.8-max"),
    ("qwen3.6-27b-q4_k_m", "Qwen3.6-27B (q4_k_m)", "qwen", "active", "qwen3.6-27b"),
    ("qwen3.8-27b-heretic", "Qwen3.8-27B Heretic (q4_k_m)", "qwen", "active", None),
    ("claude-opus-5", "Claude Opus 5", "anthropic", "active", "claude-opus-5"),
    ("claude-sonnet-5", "Claude Sonnet 5", "anthropic", "active", "claude-sonnet-5"),
    ("claude-fable-5", "Claude Fable 5", "anthropic", "active", "claude-fable-5"),
    ("gpt-5.6-sol", "GPT-5.6 Sol", "openai", "active", "gpt-5.6-sol"),
    ("gpt-5.6-terra", "GPT-5.6 Terra", "openai", "active", "gpt-5.6-terra"),
    ("gpt-5.6-luna", "GPT-5.6 Luna", "openai", "active", "gpt-5.6-luna"),
    ("gpt-5.5", "GPT-5.5", "openai", "active", None),
    ("kimi-k3", "Kimi K3", "moonshot", "active", "kimi-k3"),
    ("minimax-m3", "MiniMax M3", "minimax", "active", "minimax-m3"),
    ("grok-4.5", "Grok 4.5", "xai", "active", "grok-4.5"),
    ("gemini-3.6-flash", "Gemini 3.6 Flash", "google", "active", "gemini-3.6-flash"),
    ("ox-alpha-free", "ox-alpha-free", None, "active", None),
    # Not a stray name: Smaug Agentic is Abacus.AI's agentic SFT of Kimi K3 (2.8T MoE,
    # architectural drop-in for K3). Our six LiveBench rows match its published numbers
    # exactly (coding 82.5, agentic coding 64.6, maths 83.9, data analysis 79.9,
    # language 84.4, instruction following 71.0), so it is a model in its own right,
    # not a task-shaped key. Released 2026-09-10.
    ("smaug-agentic", "Smaug Agentic", "abacus", "active", "smaug-agentic"),
]

# model_slug, label, released_on, notes
RELEASES = [
    ("deepseek-flash", "v4.1-ga", "2026-09-10",
     "DeepSeek-V4.1-Flash GA. Source: provider change log."),
    ("deepseek-flash", "1017", "2026-10-17",
     "Checkpoint upgrade of V4.1-Flash. Source: provider change log; local kit revision "
     "45ee5d0fd106c94a3d77f721e09cbb2f50e5a16b."),
    ("deepseek-v4-flash", "0731", "2026-07-31",
     "DeepSeek-V4-Flash-0731. Retired upstream 2026-09-10."),
    ("deepseek-v4-pro", "2026-08-13", "2026-08-13", "from model_registry.active_release"),
    ("qwen3.8-flash-next", "2026-08-25", "2026-08-25", "from model_registry.active_release"),
    ("smaug-agentic", "2026-09-10", "2026-09-10",
     "Abacus.AI Smaug line launch (Smaug Agentic / Flash / Mini). Agentic SFT of Kimi K3; "
     "open weights on Hugging Face. Not served from any endpoint we own."),
]

# endpoint, served_id, model_slug, variant, quantisation, context_window, status, source, notes
DEPLOYMENTS = [
    # ---- deepseek (cloud) ----------------------------------------------------
    ("deepseek", "deepseek-v4-pro", "deepseek-v4-pro", None, None, 1000000, "active",
     "registry", None),
    ("deepseek", "deepseek-v4-flash", "deepseek-v4-flash", None, None, 1000000, "retired",
     "registry", "Retired upstream 2026-09-10; the name remains a routing alias."),
    ("deepseek", "deepseek-flash", "deepseek-flash", None, None, 1000000, "active",
     "registry", None),
    # ---- opencode (cloud) ----------------------------------------------------
    ("opencode", "deepseek-v4-pro", "deepseek-v4-pro", None, None, 1000000, "active",
     "registry", None),
    ("opencode", "deepseek-v4-flash", "deepseek-v4-flash", None, None, 1000000, "retired",
     "registry", "Genuine V4-Flash until 2026-09-10. opencode mirrored DeepSeek's legacy "
                 "name, then required the explicit deepseek-v4.1-flash; its live catalogue "
                 "no longer lists this name. See the 2026-09-10 alias to V4.1."),
    ("opencode", "deepseek-v4.1-flash", "deepseek-flash", None, None, 1000000, "active",
     "registry", "The explicit V4.1 name, added on a new name rather than a repurposed one."),
    ("opencode", "glm-5.3-flash", "glm-5.3-flash", None, None, None, "active", "traffic", None),
    ("opencode", "ox-alpha-free", "ox-alpha-free", None, None, None, "active", "traffic",
     "PENDING: name only — no capability or price data exists for this model."),
    # ---- commandcode (cloud) -------------------------------------------------
    ("commandcode", "deepseek/deepseek-v4-pro", "deepseek-v4-pro", None, None, 1000000,
     "active", "registry", None),
    ("commandcode", "deepseek/deepseek-v4-flash", "deepseek-v4-flash", None, None, 1000000,
     "active", "observed", "Still advertised in the live /provider/v1/models catalogue today, "
     "alongside -v4.1-flash, so the name is kept. It carries the same pair of legacy names "
     "DeepSeek documents as routed to V4.1 (-v4-flash and -v4-flash-vision-exp), and "
     "commandcode resells at underlying API rates, so the 2026-09-10 rule applies."),
    ("commandcode", "deepseek/deepseek-v4.1-flash", "deepseek-flash", None, None, 1000000,
     "active", "registry", None),
    ("commandcode", "deepseek/deepseek-v4.1-flash-fast", "deepseek-flash", "fast", None,
     1000000, "active", "registry",
     "Priced differently from -v4.1-flash (cache_hit 0.016 vs 0.003) — a distinct offering, "
     "same logical model."),
    ("commandcode", "z-ai/glm-5.3-flash", "glm-5.3-flash", None, None, None, "active",
     "registry", None),
    ("commandcode", "claude-opus-5", "claude-opus-5", None, None, None, "active", "registry", None),
    ("commandcode", "claude-sonnet-5", "claude-sonnet-5", None, None, None, "active",
     "registry", None),
    ("commandcode", "claude-fable-5", "claude-fable-5", None, None, None, "active",
     "registry", None),
    ("commandcode", "gpt-5.6-terra", "gpt-5.6-terra", None, None, None, "active", "registry", None),
    ("commandcode", "gpt-5.6-luna", "gpt-5.6-luna", None, None, None, "active", "registry", None),
    ("commandcode", "moonshotai/Kimi-K3", "kimi-k3", None, None, None, "active", "registry", None),
    ("commandcode", "MiniMaxAI/MiniMax-M3", "minimax-m3", None, None, None, "active",
     "registry", None),
    ("commandcode", "Qwen/Qwen3.8-Max", "qwen3.8-max", None, None, None, "active",
     "registry", None),
    # ---- local-sparks (the live lane) ---------------------------------------
    ("local-sparks", "GLM-5.3-Flash-EXL3", "glm-5.3-flash", None, "exl3", None, "retired",
     "observed", "Served from :18300 historically, but the engine running there now "
                 "advertises only DeepSeek-V4.1-Flash-1017 (observed 2026-10-30), so this "
                 "deployment is retired on this endpoint."),
    ("local-sparks", "DeepSeek-V4.1-Flash-1017", "deepseek-flash", None,
     "exl3-eaqv1-2.9bpw", 1048576, "active", "observed", None),
    ("local-sparks", "qwen3.8-flash-next", "qwen3.8-flash-next", None, None, None, "retired",
     "traffic", "Recorded historically; not advertised by the live engine (2026-10-30)."),
    # ---- retired local lanes (history only) ---------------------------------
    ("local-zgx", "qwen3.8-flash-next", "qwen3.8-flash-next", None, None, None, "retired",
     "traffic", None),
    ("llamacpp", "qwen3.8-flash-next", "qwen3.8-flash-next", None, None, None, "retired",
     "traffic", None),
    ("llamacpp", "qwen3.8-27b-q4_k_m-heretic", "qwen3.8-27b-heretic", None, "q4_k_m", None,
     "retired", "traffic", "Heretic finetune — its own model, not a qwen3.8-flash-next alias."),
]

# endpoint, served_id, model_slug, release_label, effective_from, effective_to, source, confidence
DEPLOYMENT_RELEASES = [
    ("deepseek", "deepseek-v4-flash", "deepseek-v4-flash", "0731",
     "2026-07-27", "2026-09-10", "provider_doc", "high"),
    ("deepseek", "deepseek-flash", "deepseek-flash", "v4.1-ga",
     "2026-09-10", "2026-10-17", "provider_doc", "high"),
    ("deepseek", "deepseek-flash", "deepseek-flash", "1017",
     "2026-10-17", None, "provider_doc", "high"),
    ("deepseek", "deepseek-v4-pro", "deepseek-v4-pro", "2026-08-13",
     "2026-08-13", None, "registry", "medium"),
    ("opencode", "deepseek-v4.1-flash", "deepseek-flash", "v4.1-ga",
     "2026-09-10", "2026-10-17", "provider_doc", "medium"),
    ("opencode", "deepseek-v4.1-flash", "deepseek-flash", "1017",
     "2026-10-17", None, "provider_doc", "medium"),
    ("commandcode", "deepseek/deepseek-v4.1-flash", "deepseek-flash", "v4.1-ga",
     "2026-09-10", "2026-10-17", "provider_doc", "medium"),
    ("commandcode", "deepseek/deepseek-v4.1-flash", "deepseek-flash", "1017",
     "2026-10-17", None, "provider_doc", "medium"),
    ("commandcode", "deepseek/deepseek-v4.1-flash-fast", "deepseek-flash", "v4.1-ga",
     "2026-09-10", "2026-10-17", "provider_doc", "medium"),
    ("commandcode", "deepseek/deepseek-v4.1-flash-fast", "deepseek-flash", "1017",
     "2026-10-17", None, "provider_doc", "medium"),
    # The legacy name on the reselling endpoints. Genuine V4-Flash until 2026-09-10, so the
    # window ends there and layer 0 stops being in force; the alias above takes over.
    ("opencode", "deepseek-v4-flash", "deepseek-v4-flash", "0731",
     "2026-07-27", "2026-09-10", "provider_catalog_inferred", "high"),
    ("commandcode", "deepseek/deepseek-v4-flash", "deepseek-v4-flash", "0731",
     "2026-07-27", "2026-09-10", "provider_catalog_inferred", "medium"),
    ("local-sparks", "DeepSeek-V4.1-Flash-1017", "deepseek-flash", "1017",
     "2026-10-22", None, "operator", "high"),
    ("local-zgx", "qwen3.8-flash-next", "qwen3.8-flash-next", "2026-08-25",
     "2026-08-25", None, "registry", "medium"),
    ("llamacpp", "qwen3.8-flash-next", "qwen3.8-flash-next", "2026-08-25",
     "2026-08-25", None, "registry", "medium"),
]

# endpoint (or None = any), alias, model_slug, (dep_endpoint, dep_served_id) or None,
# source, valid_from, valid_to
ALIASES = [
    # The retirement rule: on api.deepseek.com `deepseek-v4-flash` was a genuine model
    # until 2026-09-10 and a routing alias to V4.1-Flash after it. Same string, two models.
    ("deepseek", "deepseek-v4-flash", None, ("deepseek", "deepseek-v4-flash"),
     "provider_doc", "2026-07-27", "2026-09-10"),
    ("deepseek", "deepseek-v4-flash", None, ("deepseek", "deepseek-flash"),
     "provider_doc", "2026-09-10", None),
    # The reselling endpoints carry DeepSeek's legacy namespace verbatim — commandcode's live
    # catalogue advertises exactly the pair DeepSeek documents as routed to V4.1
    # (-v4-flash and -v4-flash-vision-exp) and resells at underlying API rates, and
    # opencode required the explicit -v4.1-flash name while keeping the legacy one until
    # 2026-09-29. So the same 2026-09-10 rule applies; both rows are marked as inferred
    # from the provider catalogue rather than from a provider statement.
    ("opencode", "deepseek-v4-flash", None, ("opencode", "deepseek-v4.1-flash"),
     "provider_catalog_inferred", "2026-09-10", None),
    ("commandcode", "deepseek/deepseek-v4-flash", None,
     ("commandcode", "deepseek/deepseek-v4.1-flash"), "provider_catalog_inferred",
     "2026-09-10", None),
    ("deepseek", "deepseek-flash", None, ("deepseek", "deepseek-flash"),
     "provider_doc", "2026-09-10", None),
    # local lane: case-only variant of the served id
    ("local-sparks", "glm-5.3-flash-exl3", "glm-5.3-flash", None,
     "operator", "2026-10-22", None),
    ("local-sparks", "deepseek-v4.1-flash-1017", None,
     ("local-sparks", "DeepSeek-V4.1-Flash-1017"), "operator", "2026-10-22", None),
    # spellings unambiguous across endpoints
    (None, "deepseek-v4.1-flash", "deepseek-flash", None, "operator", "2026-09-10", None),
    (None, "deepseek-v4-flash", "deepseek-v4-flash", None, "registry", "2026-07-27", None),
    (None, "deepseek/deepseek-v4-flash", "deepseek-v4-flash", None, "registry",
     "2026-07-27", None),
    # commandcode records this without the vendor prefix; promoted from a provisional
    # normalised match after the run showed 2,302 rows depending on it (see CUR-270).
    ("commandcode", "deepseek-v4-pro", None, ("commandcode", "deepseek/deepseek-v4-pro"),
     "registry", "2026-07-27", None),
    (None, "deepseek/deepseek-v4.1-flash", "deepseek-flash", None, "registry",
     "2026-09-10", None),
    (None, "z-ai/glm-5.3-flash", "glm-5.3-flash", None, "registry", "2026-10-22", None),
    # Found by --verify-endpoints: commandcode advertises these vendor-prefixed spellings
    # and they are the only advertised ids that would have matched only provisionally.
    # Recorded from the provider's own catalogue, dated to the observation.
    ("commandcode", "google/gemini-3.6-flash", "gemini-3.6-flash", None, "observed_catalog",
     "2026-10-30", None),
    ("commandcode", "xai/grok-4.5", "grok-4.5", None, "observed_catalog", "2026-10-30", None),
    (None, "glm-5.3-flash", "glm-5.3-flash", None, "registry", "2026-10-22", None),
    (None, "qwen3.8-27b-q4_k_m-heretic", "qwen3.8-27b-heretic", None, "registry",
     "2026-07-27", None),
    # benchmark/capability import keys that identify nothing on their own
    (None, "qwen-3.8-max", "qwen3.8-max", None, "benchmark_import", "2026-07-27", None),
    (None, "claude-5-opus-thinking", "claude-opus-5", None, "benchmark_import",
     "2026-07-27", None),
    (None, "gpt-5.5-thinking", "gpt-5.5", None, "benchmark_import", "2026-07-27", None),
]

# Deliberately NOT mapped — recorded so the gap is visible instead of silently normalised.
# `smaug-agentic` used to be listed here; it is now a registered logical model, because it
# turned out to be Abacus.AI's agentic SFT of Kimi K3 rather than a task-shaped key.
KNOWN_UNMAPPED: list = []


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------------ plumbing
class Seeder:
    def __init__(self, con: sqlite3.Connection, apply: bool, verbose: bool):
        self.con = con
        self.apply = apply
        self.verbose = verbose
        self.created = 0
        self.updated = 0
        self.existing = 0
        self.errors: list[str] = []
        self._ph = 0

    def _placeholder(self) -> int:
        """A stand-in id for a row that *would* be created, so a dry run still resolves
        its own references instead of cascading 'missing ref' errors."""
        self._ph -= 1
        return self._ph

    def _ins(self, sql: str, params: tuple) -> int:
        cur = self.con.execute(sql, params)
        return int(cur.lastrowid or 0)

    def log(self, kind: str, what: str) -> None:
        if self.verbose or kind != "existing":
            print(f"  {kind:9} {what}")

    # -- endpoints -------------------------------------------------------------
    def ensure_endpoint(self, e: dict) -> Optional[int]:
        row = self.con.execute(
            "SELECT endpoint_id, display_name, base_url, tier, status, notes FROM mi_endpoints"
            " WHERE slug = ?", (e["slug"],)
        ).fetchone()
        if row:
            want = (e["display_name"], e["base_url"], e["tier"], e["status"], e.get("notes"))
            have = (row["display_name"], row["base_url"], row["tier"], row["status"], row["notes"])
            if have != want:
                if self.apply:
                    self.con.execute(
                        "UPDATE mi_endpoints SET display_name=?, base_url=?, tier=?, status=?,"
                        " notes=? WHERE endpoint_id=?", want + (row["endpoint_id"],))
                self.updated += 1
                self.log("updated", f"endpoint {e['slug']}")
            else:
                self.existing += 1
                self.log("existing", f"endpoint {e['slug']}")
            return int(row["endpoint_id"])
        if not self.apply:
            self.created += 1
            self.log("create", f"endpoint {e['slug']}")
            return self._placeholder()
        eid = self._ins(
            "INSERT INTO mi_endpoints (slug, display_name, base_url, tier, status, notes,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (e["slug"], e["display_name"], e["base_url"], e["tier"], e["status"],
             e.get("notes"), now()))
        self.created += 1
        self.log("created", f"endpoint {e['slug']}")
        return eid

    def ensure_endpoint_alias(self, alias: str, slug: str, ep_ids: dict) -> None:
        eid = ep_ids.get(slug)
        if eid is None:
            return
        row = self.con.execute(
            "SELECT id FROM mi_endpoint_aliases WHERE alias = ?", (alias,)).fetchone()
        if row:
            self.existing += 1
            self.log("existing", f"endpoint alias {alias} -> {slug}")
            return
        if not self.apply:
            self.created += 1
            self.log("create", f"endpoint alias {alias} -> {slug}")
            return
        self._ins("INSERT INTO mi_endpoint_aliases (endpoint_id, alias, source) VALUES (?,?,?)",
                  (eid, alias, "legacy_registry"))
        self.created += 1
        self.log("created", f"endpoint alias {alias} -> {slug}")

    # -- models ---------------------------------------------------------------
    def ensure_model(self, m: tuple) -> Optional[int]:
        slug, display, family, status, bench = m
        row = self.con.execute(
            "SELECT model_id, display_name, family, status, benchmark_key FROM mi_models"
            " WHERE slug = ?", (slug,)).fetchone()
        if row:
            want = (display, family, status, bench)
            have = (row["display_name"], row["family"], row["status"], row["benchmark_key"])
            if have != want:
                if self.apply:
                    self.con.execute(
                        "UPDATE mi_models SET display_name=?, family=?, status=?, benchmark_key=?"
                        " WHERE model_id=?", want + (row["model_id"],))
                self.updated += 1
                self.log("updated", f"model {slug}")
            else:
                self.existing += 1
                self.log("existing", f"model {slug}")
            return int(row["model_id"])
        if not self.apply:
            self.created += 1
            self.log("create", f"model {slug}")
            return self._placeholder()
        mid = self._ins(
            "INSERT INTO mi_models (slug, display_name, family, status, benchmark_key,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (slug, display, family, status, bench, now()))
        self.created += 1
        self.log("created", f"model {slug}")
        return mid

    def ensure_release(self, r: tuple, model_ids: dict) -> Optional[int]:
        slug, label, released_on, notes = r
        mid = model_ids.get(slug)
        if mid is None:
            return None
        row = self.con.execute(
            "SELECT release_id, released_on, notes FROM mi_releases WHERE model_id=? AND label=?",
            (mid, label)).fetchone()
        if row:
            if (row["released_on"], row["notes"]) != (released_on, notes):
                if self.apply:
                    self.con.execute(
                        "UPDATE mi_releases SET released_on=?, notes=? WHERE release_id=?",
                        (released_on, notes, row["release_id"]))
                self.updated += 1
                self.log("updated", f"release {slug}/{label}")
            else:
                self.existing += 1
                self.log("existing", f"release {slug}/{label}")
            return int(row["release_id"])
        if not self.apply:
            self.created += 1
            self.log("create", f"release {slug}/{label}")
            return self._placeholder()
        rid = self._ins(
            "INSERT INTO mi_releases (model_id, label, released_on, weights_ref, notes)"
            " VALUES (?,?,?,?,?)", (mid, label, released_on, None, notes))
        self.created += 1
        self.log("created", f"release {slug}/{label}")
        return rid

    # -- deployments -----------------------------------------------------------
    def ensure_deployment(self, d: tuple, ep_ids: dict, model_ids: dict) -> Optional[int]:
        ep, served, model, variant, quant, ctx, status, source, notes = d
        eid, mid = ep_ids.get(ep), model_ids.get(model)
        if eid is None or mid is None:
            self.errors.append(f"deployment {ep}/{served}: missing endpoint or model")
            return None
        row = self.con.execute(
            "SELECT deployment_id, model_id, variant, quantisation, context_window, status,"
            " source, notes FROM mi_deployments WHERE endpoint_id=? AND served_id=?",
            (eid, served)).fetchone()
        if row:
            want = (mid, variant, quant, ctx, status, source, notes)
            have = (row["model_id"], row["variant"], row["quantisation"], row["context_window"],
                    row["status"], row["source"], row["notes"])
            if have != want:
                if self.apply:
                    self.con.execute(
                        "UPDATE mi_deployments SET model_id=?, variant=?, quantisation=?,"
                        " context_window=?, status=?, source=?, notes=? WHERE deployment_id=?",
                        want + (row["deployment_id"],))
                self.updated += 1
                self.log("updated", f"deployment {ep}/{served}")
            else:
                self.existing += 1
                self.log("existing", f"deployment {ep}/{served}")
            return int(row["deployment_id"])
        if not self.apply:
            self.created += 1
            self.log("create", f"deployment {ep}/{served} -> {model}")
            return self._placeholder()
        did = self._ins(
            "INSERT INTO mi_deployments (endpoint_id, model_id, served_id, variant,"
            " quantisation, context_window, status, source, notes) VALUES (?,?,?,?,?,?,?,?,?)",
            (eid, mid, served, variant, quant, ctx, status, source, notes))
        self.created += 1
        self.log("created", f"deployment {ep}/{served} -> {model}")
        return did

    def ensure_deployment_release(self, dr: tuple, ep_ids: dict, model_ids: dict,
                                  dep_ids: dict, rel_ids: dict) -> None:
        ep, served, model, label, start, end, source, conf = dr
        did = dep_ids.get((ep, served))
        rid = rel_ids.get((model, label))
        if did is None or rid is None:
            self.errors.append(f"deployment_release {ep}/{served} {label}: missing ref")
            return
        row = self.con.execute(
            "SELECT id, release_id, effective_to, source, confidence FROM mi_deployment_releases"
            " WHERE deployment_id=? AND effective_from=?", (did, start)).fetchone()
        if row:
            want = (rid, end, source, conf)
            have = (row["release_id"], row["effective_to"], row["source"], row["confidence"])
            if have != want:
                if self.apply:
                    self.con.execute(
                        "UPDATE mi_deployment_releases SET release_id=?, effective_to=?,"
                        " source=?, confidence=? WHERE id=?", want + (row["id"],))
                self.updated += 1
                self.log("updated", f"release-map {model}/{label} from {start}")
            else:
                self.existing += 1
                self.log("existing", f"release-map {model}/{label} from {start}")
            return
        if not self.apply:
            self.created += 1
            self.log("create", f"release-map {ep}/{served} {label} from {start}")
            return
        self._ins(
            "INSERT INTO mi_deployment_releases (deployment_id, release_id, effective_from,"
            " effective_to, source, confidence) VALUES (?,?,?,?,?,?)",
            (did, rid, start, end, source, conf))
        self.created += 1
        self.log("created", f"release-map {ep}/{served} {label} from {start}")

    def ensure_alias(self, a: tuple, ep_ids: dict, model_ids: dict, dep_ids: dict) -> None:
        ep, alias, model, dep_ref, source, start, end = a
        eid = None if ep is None else ep_ids.get(ep)
        mid = None if model is None else model_ids.get(model)
        did = None if dep_ref is None else dep_ids.get(tuple(dep_ref))
        if ep is not None and eid is None:
            self.errors.append(f"alias {alias}: unknown endpoint {ep}")
            return
        if mid is None and did is None:
            self.errors.append(f"alias {alias}: no model or deployment target")
            return
        row = self.con.execute(
            "SELECT id, model_id, deployment_id, valid_to FROM mi_aliases"
            " WHERE endpoint_id IS ? AND alias=? AND valid_from=?",
            (eid, alias, start)).fetchone()
        if row:
            want = (mid, did, end)
            have = (row["model_id"], row["deployment_id"], row["valid_to"])
            if have != want:
                if self.apply:
                    self.con.execute(
                        "UPDATE mi_aliases SET model_id=?, deployment_id=?, valid_to=?"
                        " WHERE id=?", want + (row["id"],))
                self.updated += 1
                self.log("updated", f"alias {alias} ({ep or 'any'}) from {start}")
            else:
                self.existing += 1
                self.log("existing", f"alias {alias} ({ep or 'any'}) from {start}")
            return
        if not self.apply:
            self.created += 1
            self.log("create", f"alias {alias} ({ep or 'any'}) from {start}")
            return
        self._ins(
            "INSERT INTO mi_aliases (endpoint_id, alias, model_id, deployment_id, source,"
            " valid_from, valid_to) VALUES (?,?,?,?,?,?,?)",
            (eid, alias, mid, did, source, start, end))
        self.created += 1
        self.log("created", f"alias {alias} ({ep or 'any'}) from {start}")

    def bump_version(self, note: str) -> None:
        v = self.con.execute("SELECT MAX(version) AS v FROM mi_registry_versions").fetchone()
        v = int(v["v"] or 0)
        if not self.apply:
            print(f"  create    registry version {v + 1} ({note})")
            return
        if v == 0 or self.created or self.updated:
            self._ins("INSERT INTO mi_registry_versions (version, created_at, note) VALUES (?,?,?)",
                      (v + 1, now(), note))
            print(f"  created   registry version {v + 1} ({note})")


# ------------------------------------------------------------------- verification
def verify_endpoints(con: sqlite3.Connection, apply: bool) -> None:
    """Stamp ``verified_at`` from each endpoint's own /v1/models. Observed, not authored."""
    try:
        # one normaliser and one resolver, shared — never a second implementation.
        # Running as `python scripts/...` puts scripts/ on sys.path, not the repo root,
        # so make the package importable explicitly.
        if REPO not in sys.path:
            sys.path.insert(0, REPO)
        from src.api.model_identity import (
            MATCH_NORMALISED,
            normalise_model_string as _norm,
            resolve as _resolve,
        )
    except Exception as exc:  # noqa: BLE001 — the seeder must still run standalone
        # Do NOT degrade silently: a fallback here would misreport every candidate.
        print(f"  NOTE  the resolver is not importable ({exc.__class__.__name__}); the "
              f"advertised-id classification below is disabled")
        MATCH_NORMALISED = "normalised"

        def _norm(raw):
            return (raw or "").strip().lower()

        _resolve = None

    paths = [p.strip() for p in os.environ.get("LCP_MODELS_PATHS", "/v1/models,/models").split(",")]
    keys = {}
    envfile = os.path.join(REPO, ".env")
    if os.path.exists(envfile):
        for line in open(envfile):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                keys[k.strip()] = v.strip().strip('"').strip("'")
    keymap = {"deepseek": "DEEPSEEK_API_KEY", "opencode": "OPENCODE_API_KEY",
              "commandcode": "COMMANDCODE_API_KEY"}

    def tokens_for(slug: str) -> list:
        """Every credential worth trying, and finally no credential at all.

        Sources in order: the process environment, the repo .env, then the app's own
        encrypted credential store (which is what is reachable *inside* the container,
        where the live keys actually live). Values are never printed.
        """
        out = []
        env_name = keymap.get(slug)
        if env_name:
            v = os.environ.get(env_name) or keys.get(env_name)
            if v:
                out.append(v)
        try:
            from src.api.credential_store import get_credential_store
            store = get_credential_store()
            if store is not None:
                v = store.get(slug)
                if v:
                    out.append(v)
        except Exception:  # noqa: BLE001 — not running inside the app, that is fine
            pass
        out.append(None)      # some catalogues (commandcode's) are public
        return out

    print("\n-- endpoint verification (/v1/models) --")
    for ep in con.execute("SELECT endpoint_id, slug, base_url FROM mi_endpoints"
                          " WHERE status='active' ORDER BY slug"):
        base = (ep["base_url"] or "").rstrip("/")
        if not base:
            print(f"  {ep['slug']:14} SKIP  no base_url recorded")
            continue
        root = base
        found, errors = None, []
        for p in paths:
            url = root.rsplit("/v1", 1)[0] + p if "/v1" in root else root + p
            for token in tokens_for(ep["slug"]):
                req = urllib.request.Request(url)
                # a bare urllib User-Agent gets 403'd by at least one of these providers
                req.add_header("User-Agent", "lcp-model-identity/1.0 (+curl-compatible)")
                req.add_header("Accept", "application/json")
                if token:
                    req.add_header("Authorization", "Bearer " + token)
                try:
                    with urllib.request.urlopen(req, timeout=12) as f:
                        body = json.loads(f.read().decode())
                    ids = [str(m.get("id")) for m in (body.get("data") or [])]
                    if ids:
                        found = ids
                        break
                except Exception as ex:  # noqa: BLE001
                    errors.append(f"{'+auth' if token else 'no-auth'}: {str(ex)[:60]}")
            if found:
                break
        if not found:
            print(f"  {ep['slug']:14} UNVERIFIED  {'; '.join(errors) or 'no model ids'}")
            continue
        matched = 0
        for r in con.execute(
            "SELECT deployment_id, served_id FROM mi_deployments WHERE endpoint_id=? AND"
            " status='active'", (ep["endpoint_id"],)):
            if r["served_id"] in found:
                matched += 1
                if apply:
                    con.execute("UPDATE mi_deployments SET verified_at=?, last_seen=? WHERE"
                                " deployment_id=?", (now(), now(), r["deployment_id"]))
        print(f"  {ep['slug']:14} OK  {len(found)} ids advertised, {matched} deployments matched")
        missing = [d["served_id"] for d in con.execute(
            "SELECT served_id FROM mi_deployments WHERE endpoint_id=? AND status='active'",
            (ep["endpoint_id"],)) if d["served_id"] not in found]
        for m in missing:
            print(f"                   DRIFT  {m!r} is registered but not advertised")

        # Advertised but not registered. A large catalogue (commandcode advertises 94
        # models) would otherwise bury the actionable rows, so classify instead of dump:
        # only ids that normalise to a model or alias we already know are worth raising —
        # those are the spelling variants that need registering or an explicit alias.
        known = {_norm(r["slug"])
                 for r in con.execute("SELECT slug FROM mi_models")}
        known |= {_norm(r["alias"])
                  for r in con.execute("SELECT alias FROM mi_aliases")}
        candidates, available, others = [], [], []
        for i in found:
            if con.execute("SELECT 1 FROM mi_deployments WHERE endpoint_id=? AND served_id=?",
                           (ep["endpoint_id"], i)).fetchone():
                continue
            if _norm(i) not in known:
                others.append(i)                  # unrelated to anything we use
                continue
            # It looks like a model we know — so ask the resolver whether traffic under
            # this exact string would resolve cleanly *here*. A name that already resolves
            # by logical slug needs nothing; one that would only match provisionally is
            # exactly the spelling variant that needs an explicit alias.
            res = _resolve(ep["slug"], i, con=con) if _resolve else None
            if res is None or not res.resolved or res.matched_by == MATCH_NORMALISED:
                candidates.append(i)
            else:
                available.append(i)
        for i in candidates:
            print(f"                   ALIAS? {i!r} looks like a model we know but would not "
                  f"resolve cleanly on this endpoint — register it or add an alias")
        if available:
            print(f"                   AVAILABLE: {len(available)} known models are advertised "
                  f"here with no registered deployment (usable, currently unused)")
        if others:
            print(f"                   {len(others)} other advertised ids are unrelated to "
                  f"anything we use")


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Seed the model-identity registry (CUR-270).")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="write (default is dry run)")
    ap.add_argument("--quiet", action="store_true", help="only print the summary")
    ap.add_argument("--verify-endpoints", action="store_true",
                    help="also read each endpoint's /v1/models")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2
    con = sqlite3.connect(args.db, timeout=30)
    con.row_factory = sqlite3.Row
    if not con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mi_models'"
    ).fetchone():
        print("model-identity tables missing — run `alembic upgrade 026` first "
              "(has --db been migrated?)", file=sys.stderr)
        return 2

    print(f"== seeding {'(APPLY)' if args.apply else '(DRY RUN)'} {args.db} ==")
    s = Seeder(con, args.apply, verbose=not args.quiet)

    ep_ids = {e["slug"]: s.ensure_endpoint(e) for e in ENDPOINTS}
    for alias, slug in ENDPOINT_ALIASES:
        s.ensure_endpoint_alias(alias, slug, ep_ids)
    model_ids = {m[0]: s.ensure_model(m) for m in MODELS}
    rel_ids = {(r[0], r[1]): s.ensure_release(r, model_ids) for r in RELEASES}
    dep_ids = {}
    for d in DEPLOYMENTS:
        dep_ids[(d[0], d[1])] = s.ensure_deployment(d, ep_ids, model_ids)
    for dr in DEPLOYMENT_RELEASES:
        s.ensure_deployment_release(dr, ep_ids, model_ids, dep_ids, rel_ids)
    for a in ALIASES:
        s.ensure_alias(a, ep_ids, model_ids, dep_ids)
    s.bump_version("seed: reviewed model map + DeepSeek release timeline (CUR-270)")

    if args.verify_endpoints:
        verify_endpoints(con, args.apply)

    if args.apply:
        con.commit()

    print(f"\ncreated={s.created} updated={s.updated} existing={s.existing}"
          f" errors={len(s.errors)}")
    for e in s.errors:
        print("  ERROR", e)
    for name, why in KNOWN_UNMAPPED:
        print(f"  NOT MAPPED  {name}: {why}")
    if not args.apply:
        print("\n(dry run — nothing written; re-run with --apply)")
    con.close()
    return 1 if s.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
