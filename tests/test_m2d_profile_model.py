"""M2d — the profile model: declared mapping, intents, per-profile routing, the pool.

What this pins, and why each one is worth a test rather than a look:

1. **A profile is never left without a chain.** The config loader rejects a profile
   with an empty chain, and `_hydrate` responds to any validation error by silently
   falling back to the built-in seed — so a profile written without a chain does not
   fail loudly, it *replaces every profile* with defaults. Both write paths are
   checked here, and so is the loader guard that makes the fallback reachable.
2. **The lane → agent mapping is declared, not guessed.** Deriving it from the agent's
   own `config.yaml` remains available as a *suggestion* for the migration, but the
   page reads the declared field. A suggestion must be labelled as one.
3. **`intents` narrows; declaring none changes nothing.** The negative control is the
   important half: if an unset `intents` altered routing, every profile in service
   would silently start routing differently.
4. **An LCP-only profile is a kind, not a gap.** The copy on every surface it appears
   is checked, because "no agent directory" reads as a deficiency to a human and that
   framing is what the operator asked to have removed.
"""
import json
import os
import re

import pytest
from unittest.mock import MagicMock

from src.api import profile_data
from src.api.config import (Config, ConfigError, _validate,
                            validate_profile_fields)


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def profiles_root(tmp_path, monkeypatch):
    """A profiles root with every relationship the model has to describe.

    homelab-expert-l2 and blog-writer both route through l2; lonely routes nowhere;
    plain is not a profile at all (no config.yaml).
    """
    root = tmp_path / "profiles"
    for name, lane in (("homelab-expert-l2", "l2"), ("blog-writer", "l2"),
                       ("lonely", ""), ("career-agent", "career")):
        (root / name).mkdir(parents=True)
        (root / name / "config.yaml").write_text(
            ("provider:\n  base_url: http://localhost:8734/%s\n" % lane) if lane
            else "provider:\n  base_url: https://api.example.com/v1\n")
        (root / name / "SOUL.md").write_text(
            "# SOUL\n\nYou are the %s agent for the house.\n" % name)
    (root / "plain").mkdir()
    monkeypatch.setenv("LCP_PROFILES_DIR", str(root))
    monkeypatch.setenv("LCP_AVATAR_DIR", str(tmp_path / "avatars"))
    return root


def _cfg(profiles):
    c = MagicMock()
    c.providers = {"deepseek": {"api_key_env": "DEEPSEEK_API_KEY",
                                "api_base": "https://api.deepseek.com/v1",
                                "models": ["deepseek-chat", "deepseek-reasoner"]},
                   "opencode": {"api_key_env": "OPENCODE_API_KEY",
                                "api_base": "https://opencode.example/v1",
                                "models": ["qwen3"]}}
    c.profiles = profiles
    c.server = {"port": 8734}
    c.default_profile = "l2"
    return c


@pytest.fixture
def cfg():
    return _cfg({
        "l2": {"forbidden_tools": [], "auth_required": True,
               "chain": [{"provider": "deepseek", "model": "deepseek-chat"}]},
        "coder": {"forbidden_tools": [], "auth_required": False,
                  "chain": [{"provider": "opencode", "model": "qwen3"}]},
    })


@pytest.fixture
def engine(temp_db):
    return temp_db[1]


# ── 1. the profile field contract ────────────────────────────────────────────

def test_the_new_fields_are_optional_and_default_harmlessly():
    """Absent fields must not change how an existing profile loads.

    The loader validates at boot and `_hydrate` falls back to the seed on error — so a
    field that defaulted to invalid would not raise, it would replace the config.
    """
    p = _cfg({"l2": {"chain": [{"provider": "p", "model": "m"}]}}).profiles["l2"]
    fields = validate_profile_fields("l2", p)
    assert fields["agent_profile"] == ""
    assert fields["routing"] == "static"   # today's behaviour, byte for byte
    assert fields["intents"] == []
    assert fields["description"] == ""


@pytest.mark.parametrize("value", ["homelab-expert-l2", "l2", "a.b", "A-1", "x_1"])
def test_a_valid_agent_profile_name_is_accepted(value):
    assert validate_profile_fields("l2", {"agent_profile": value})["agent_profile"] == value


@pytest.mark.parametrize("value", ["..", "../etc", "a/b", "/etc/passwd", ".", "x" * 65,
                                   "na me", "l2;rm -rf /", "<script>"])
def test_a_dangerous_agent_profile_name_is_rejected(value):
    """The field names a directory, so it is held to a directory name's shape."""
    with pytest.raises(ConfigError):
        validate_profile_fields("l2", {"agent_profile": value})


def test_an_empty_agent_profile_means_lcp_only():
    """Empty is meaningful: it is how a profile says it has no agent behind it."""
    for value in ("", "   ", None):
        assert validate_profile_fields("l2", {"agent_profile": value})["agent_profile"] == ""


@pytest.mark.parametrize("value", ["static", "dynamic"])
def test_routing_accepts_the_two_modes(value):
    assert validate_profile_fields("l2", {"routing": value})["routing"] == value


@pytest.mark.parametrize("value", ["eager", "on", True, 1, "both"])
def test_routing_rejects_anything_else(value):
    with pytest.raises(ConfigError):
        validate_profile_fields("l2", {"routing": value})


def test_intents_are_normalised_and_deduplicated():
    fields = validate_profile_fields("l2", {"intents": ["code_generation", " code_generation ",
                                                        "unit_tests"]})
    assert fields["intents"] == ["code_generation", "unit_tests"]


@pytest.mark.parametrize("value", ["code_generation", {"a": 1}, [1, 2], ["x" * 80],
                                   ["ok"] * 40, [None]])
def test_a_malformed_intents_value_is_rejected(value):
    with pytest.raises(ConfigError):
        validate_profile_fields("l2", {"intents": value})


def test_the_loader_applies_the_same_contract():
    """One contract: the loader and the write API validate through this function."""
    with pytest.raises(ConfigError):
        _validate("profiles", {"l2": {"chain": [{"provider": "p", "model": "m"}],
                                      "routing": "sometimes"}})
    _validate("profiles", {"l2": {"chain": [{"provider": "p", "model": "m"}],
                                  "routing": "dynamic",
                                  "intents": ["planning"],
                                  "agent_profile": "homelab-expert-l2"}})


# ── 2. a profile is never left without a chain ───────────────────────────────

def test_the_loader_rejects_a_chainless_profile():
    """This is the guard that makes the silent-fallback path reachable — so it is
    pinned rather than assumed, because removing it would turn the fallback into the
    everyday path."""
    with pytest.raises(ConfigError):
        _validate("profiles", {"l2": {"chain": []}})
    with pytest.raises(ConfigError):
        _validate("profiles", {"l2": {}})


def test_the_fallback_has_somewhere_to_fall_back_to():
    """Why the two tests above matter: the seed exists, so a validation error does not
    stop the container — it swaps the config for this. Silent, and severe enough to
    deserve a test of its own."""
    from src.api.config import SEED_CONFIG
    assert "profiles" in SEED_CONFIG and SEED_CONFIG["profiles"]


# ── 3. the mapping is declared, and a suggestion is labelled one ─────────────

def test_the_suggestion_is_derived_from_the_agents_own_config(profiles_root):
    """The migration path: read the link off the agent once, then declare it."""
    lanes = ["l2", "coder", "career"]
    sug = profile_data.mapping_suggestions(lanes)
    assert sug["l2"] == "homelab-expert-l2"   # the convention: the -<lane> profile
    assert sug["career"] == "career-agent"
    assert "coder" not in sug                 # nothing routes through coder
    assert "lonely" not in sug.values()       # routing nowhere is not a match


def test_the_obvious_agent_wins_over_the_other_one_sharing_the_lane(profiles_root):
    """Two agents share l2; the suggestion must be the one named for it."""
    agents = profile_data.agent_profiles(["l2"])
    sharing = sorted(n for n, i in agents.items() if i.get("lane") == "l2")
    assert sharing == ["blog-writer", "homelab-expert-l2"]
    # `agent_profiles` also reports profiles that route nowhere — they are Hermes
    # profiles too — so the lane filter above is the assertion, not the key set.
    assert agents["lonely"]["lane"] == ""
    assert profile_data.primary_agent_for_lane("l2", agents) == "homelab-expert-l2"


def test_a_declared_mapping_beats_the_suggestion(cfg, profiles_root):
    """Once declared, the field decides — the derivation is not consulted again."""
    from src.ui import pages
    cfg.profiles["l2"]["agent_profile"] = "blog-writer"
    groups = pages._profile_groups(cfg, {})
    card = [c for c in groups["declared"] if c["name"] == "l2"][0]
    assert card["agent"] == "blog-writer"
    assert card["mapping_source"] == "declared"


def test_an_undeclared_mapping_is_shown_as_a_suggestion(cfg, profiles_root):
    from src.ui import pages
    card = [c for c in pages._profile_groups(cfg, {})["declared"] if c["name"] == "l2"][0]
    assert card["agent"] == "homelab-expert-l2"
    assert card["mapping_source"] == "suggested"
    assert "mapping suggested" in card["kind_label"]


# ── 4. the groups ────────────────────────────────────────────────────────────

def test_the_grid_groups_by_what_a_profile_is(cfg, profiles_root):
    from src.ui import pages
    groups = pages._profile_groups(cfg, {})
    assert sorted(groups) == ["declared", "lcp_only", "uncovered"]
    assert [c["name"] for c in groups["declared"]] == ["l2"]
    assert [c["name"] for c in groups["lcp_only"]] == ["coder"]


def test_an_uncovered_profile_is_listed_with_where_its_traffic_goes(cfg, profiles_root):
    from src.ui import pages
    uncovered = {u["name"]: u for u in pages._profile_groups(cfg, {})["uncovered"]}
    assert uncovered["blog-writer"]["routes_through"] == "l2"
    assert uncovered["lonely"]["routes_through"] == ""
    # its own config names a lane no gateway profile serves — which is the state worth
    # showing, because the gateway answers such a request with 400
    assert uncovered["career-agent"]["routes_through"] == "career"
    assert uncovered["career-agent"]["lane_served"] is False
    assert uncovered["blog-writer"]["lane_served"] is True
    # the agent that owns l2 is not uncovered — l2 claims it
    assert "homelab-expert-l2" not in uncovered


def test_every_hermes_profile_is_either_claimed_or_offered(cfg, profiles_root):
    """No profile may vanish from the page — the M2c invariant, kept."""
    from src.ui import pages
    groups = pages._profile_groups(cfg, {})
    shown = {c["agent"] for c in groups["declared"] if c["agent"]}
    shown |= {c["name"] for c in groups["lcp_only"]}
    shown |= {u["name"] for u in groups["uncovered"]}
    for name in ("homelab-expert-l2", "blog-writer", "lonely", "career-agent"):
        assert name in shown, name


def test_the_create_flow_is_offered_exactly_the_uncovered(cfg, profiles_root):
    from src.ui import pages
    page = pages.render_profiles_page(cfg, engine=None, params={})
    uncovered = [u["name"] for u in pages._profile_groups(cfg, {})["uncovered"]]
    assert len(re.findall(r'openAddProfile\(\'', page)) == len(uncovered)
    for name in uncovered:
        assert "openAddProfile('%s')" % name in page


# ── 5. the copy: an LCP-only profile is a kind, not a gap ────────────────────

def visible_text(html: str) -> str:
    """What a person reads on the page: no scripts, no styles, no comments, no tags.

    Asserting on raw HTML makes the test fail on a *comment* whose prose happens to
    contain the word — which says nothing about the page the operator sees.
    """
    html = re.sub(r"<script\b.*?</script>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<style\b.*?</style>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


@pytest.mark.parametrize("bad", ["no agent directory", "missing", "incomplete",
                                 "not a gateway lane", "no skills directory",
                                 "no task tree of its own yet"])
def test_no_surface_calls_an_lcp_only_profile_deficient(cfg, profiles_root, engine, bad):
    from src.ui import pages
    blob = visible_text(pages.render_profiles_page(cfg, engine, {}))
    for tab in ("skills", "memory", "tasks", "routing", "models"):
        blob += visible_text(pages.render_profile_detail_page(cfg, engine, "coder", {"tab": tab}))
    # The message carries the context, so a failure says where the word was read
    # rather than only that it was.
    where = blob.lower().find(bad.lower())
    assert where < 0, "%r read on the page: ...%s..." % (bad, blob[max(0, where - 140):where + 80])


def test_an_lcp_only_profiles_empty_tabs_say_why(cfg, profiles_root, engine):
    from src.ui import pages
    for tab in ("skills", "memory", "tasks"):
        html = pages.render_profile_detail_page(cfg, engine, "coder", {"tab": tab})
        assert "no agent behind it" in html, tab


# ── 6. the two new level-3 tabs ──────────────────────────────────────────────

def test_the_routing_tab_carries_this_profiles_settings_and_scope(cfg, profiles_root, engine):
    from src.ui import pages
    html = pages.render_profile_detail_page(cfg, engine, "l2", {"tab": "routing"})
    assert 'data-profile="l2"' in html
    assert "prouEnabled" in html and "prouRules" in html
    # the write calls are scoped to this profile, so a rule cannot land on another
    assert "profile: PROFILE" in html
    assert 'var PROFILE = ROUTING.profile' in html


def test_an_agent_profile_cannot_edit_routing_it_does_not_own(cfg, profiles_root, engine):
    """The Routing tab must not offer controls that would write a rule onto a name
    that is not a profile."""
    from src.ui import pages
    html = pages.render_profile_detail_page(cfg, engine, "blog-writer", {"tab": "routing"})
    assert "No routing settings here" in html
    # the controls themselves, not the identifiers inside the included script
    assert 'id="prouEnabled"' not in html
    assert 'id="prouRules"' not in html
    assert "prouToggle(this)" not in html


def test_the_models_tab_offers_every_registered_model(cfg, profiles_root, engine):
    from src.ui import pages
    html = pages.render_profile_detail_page(cfg, engine, "l2", {"tab": "models"})
    assert "ppChain" in html and "ppProvider" in html
    assert "deepseek-chat" in html and "deepseek-reasoner" in html and "qwen3" in html
    assert "opencode" in html


def test_a_provider_with_no_key_is_shown_and_marked(cfg, profiles_root, engine, monkeypatch):
    """A model you cannot call is a fact about the pool, not something to hide."""
    from src.ui import pages
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "present")
    view = pages._profile_pool_view(cfg, "l2")
    by_name = {c["provider"]: c for c in view["catalogue"]}
    assert by_name["deepseek"]["has_key"] is True
    assert by_name["opencode"]["has_key"] is False
    assert by_name["opencode"]["key_env"] == "OPENCODE_API_KEY"


def test_the_models_tab_is_read_only_for_an_agent_profile(cfg, profiles_root, engine):
    from src.ui import pages
    html = pages.render_profile_detail_page(cfg, engine, "blog-writer", {"tab": "models"})
    assert "No chain of its own" in html
    assert 'id="ppProvider"' not in html
    assert 'id="ppChain"' not in html
    # and the script that would have driven them must not run there
    assert "if (!document.getElementById('ppChain')) return;" in html


def test_the_chain_is_the_pool_no_second_list(cfg, profiles_root, engine):
    """There is no `pool` field: the router scores the chain, so the pool IS it."""
    from src.ui import pages
    view = pages._profile_pool_view(cfg, "l2")
    assert view["chain"] == [{"provider": "deepseek", "model": "deepseek-chat"}]
    assert view["model_count"] == 3     # every registered (provider, model)


# ── 7. config on init ────────────────────────────────────────────────────────

def test_the_profile_config_travels_with_every_tab(cfg, profiles_root, engine):
    from src.ui import pages
    cfg.profiles["l2"].update({"agent_profile": "homelab-expert-l2",
                               "routing": "dynamic", "intents": ["planning"],
                               "description": "Routes the L2 work"})
    for tab in ("skills", "memory", "cron", "routing", "models"):
        html = pages.render_profile_detail_page(cfg, engine, "l2", {"tab": tab})
        assert '"agent_profile": "homelab-expert-l2"' in html, tab
        assert '"routing": "dynamic"' in html, tab
        assert '"intents": ["planning"]' in html, tab


def test_an_agent_profile_page_sends_an_empty_config(cfg, profiles_root, engine):
    """An agent directory is not a routing entry, so there is no config to send."""
    from src.ui import pages
    html = pages.render_profile_detail_page(cfg, engine, "blog-writer", {"tab": "skills"})
    assert '<script id="profile-config" type="application/json">{}</script>' in html


# ── 8. per-profile intents narrow the classifier ─────────────────────────────

def _narrow(task, intents, semantic=None, path="semantic"):
    from src.api.router import CapabilityRouter, ClassifyResult
    r = CapabilityRouter.__new__(CapabilityRouter)
    detail = ClassifyResult(task=task, path=path, semantic=semantic)
    return r._narrow_to_intents(detail, "l2", _cfg({"l2": {"chain": [], "intents": intents}}))


def test_declaring_no_intents_leaves_the_classifier_alone():
    """The negative control. If this ever fails, every profile in service changes
    behaviour without anyone having asked for it."""
    from src.api.router import CapabilityRouter, ClassifyResult
    r = CapabilityRouter.__new__(CapabilityRouter)
    for intents in ([], None, ["", "   "]):
        detail = ClassifyResult(task="agentic_multi_step", path="semantic",
                                semantic=[("agentic_multi_step", 0.9)])
        out = r._narrow_to_intents(detail, "l2", _cfg({"l2": {"chain": [], "intents": intents}}))
        assert out is detail


def test_an_already_declared_task_is_not_rewritten():
    from src.api.router import CapabilityRouter, ClassifyResult
    r = CapabilityRouter.__new__(CapabilityRouter)
    detail = ClassifyResult(task="planning", path="semantic", semantic=[("planning", 0.8)])
    out = r._narrow_to_intents(detail, "l2", _cfg({"l2": {"chain": [], "intents": ["planning"]}}))
    assert out is detail


def test_narrowing_picks_the_best_declared_scorer():
    out = _narrow("casual_chat", ["unit_tests", "code_generation"],
                  [("casual_chat", 0.95), ("code_generation", 0.8), ("unit_tests", 0.55)])
    assert out.task == "code_generation"      # best *declared*, not best overall
    assert out.path == "intent_narrowed"


def test_narrowing_never_invents_a_task_that_did_not_score():
    out = _narrow("casual_chat", ["research_deep"], [("casual_chat", 0.9)])
    assert out.task == "research_deep"
    assert out.path == "intent_declared"      # marked: no signal backed this choice


def test_narrowing_survives_a_broken_config():
    from src.api.router import CapabilityRouter, ClassifyResult
    r = CapabilityRouter.__new__(CapabilityRouter)

    class Boom:
        @property
        def profiles(self):
            raise RuntimeError("bad config")

    detail = ClassifyResult(task="casual_chat", path="casual")
    assert r._narrow_to_intents(detail, "l2", Boom()) is detail
    assert r._narrow_to_intents(detail, "l2", None) is detail


def test_intents_do_not_change_the_classifier_itself():
    from src.api.router import classify_task
    assert classify_task([{"role": "user", "content": "hi"}]) == "casual_chat"


def test_the_profile_intents_reader_uses_the_same_contract():
    from src.api.router import _profile_intents
    assert _profile_intents(_cfg({"l2": {"chain": [], "intents": ["planning", "planning"]}}),
                            "l2") == ["planning"]
    assert _profile_intents(_cfg({"l2": {"chain": []}}), "l2") == []
    assert _profile_intents(_cfg({"l2": {}}), "unknown") == []


# ── 9. the JSON the pickers read ─────────────────────────────────────────────

def test_the_pool_payload_is_json_serialisable(cfg, profiles_root, engine):
    """The init payload is embedded in a <script type=application/json>; anything
    unserialisable would break the page, not a test."""
    from src.ui import pages
    view = pages._profile_pool_view(cfg, "l2")
    json.dumps(view)


def test_the_routing_payload_is_json_serialisable(cfg, profiles_root, engine):
    from src.ui import pages
    json.dumps(pages._profile_routing_view(cfg, "l2"))


# ── 10. the write paths ──────────────────────────────────────────────────────
#
# These run against a *real* Config backed by an in-memory store, because the thing
# worth testing is the whole path — validate, mutate, save — and a MagicMock config
# would skip exactly the part that can fail silently (a write that stores something
# the loader later refuses, which swaps the config for the seed).

class _Store:
    """The settings-DB surface the config needs, in memory."""

    def __init__(self, sections):
        self.sections = dict(sections)

    def get_config_section(self, section, default=None):
        return self.sections.get(section, default)

    def set_config_section(self, section, value):
        self.sections[section] = value


def _real_config(profiles):
    sections = {"profiles": profiles,
                "server": {"port": 8734, "default_profile": "l2"},
                "providers": {"deepseek": {"api_key_env": "DEEPSEEK_API_KEY",
                                           "api_base": "https://x/v1",
                                           "models": ["deepseek-chat"]}}}
    return Config(store=_Store(sections)), sections


def _handler(path, method="PUT", body=None, config=None):
    from src.server.handler import LCPHandler

    class H(LCPHandler):
        def __init__(self):
            self.path = path
            self.command = method
            self.headers = {}
            self.request_version = "HTTP/1.1"
            self.client_address = ("127.0.0.1", 0)
            self.send_response = MagicMock()
            self.send_header = MagicMock()
            self.end_headers = MagicMock()
            self.wfile = MagicMock()
            payload = json.dumps(body or {}).encode()
            self.rfile = MagicMock()
            self.rfile.read = MagicMock(return_value=payload)
            self.headers["Content-Length"] = str(len(payload))
            self.engine = None

    H.config = config
    return H()


def _sent(handler):
    """(status, body) from a handler that was driven directly."""
    status = handler.send_response.call_args[0][0] if handler.send_response.call_args else None
    for call in handler.wfile.write.call_args_list:
        try:
            return status, json.loads(call[0][0])
        except Exception:
            continue
    return status, None


def test_the_seed_proposes_without_writing(profiles_root):
    from src.server.handler import LCPHandler
    cfg, sections = _real_config({
        "l2": {"chain": [{"provider": "deepseek", "model": "deepseek-chat"}]},
        "coder": {"chain": [{"provider": "deepseek", "model": "deepseek-chat"}]},
    })
    h = _handler("/api/profiles/mapping/seed", "POST", {"apply": False}, cfg)
    LCPHandler.config = cfg
    h._serve_profile_mapping_seed()
    status, body = _sent(h)
    assert status == 200
    assert body["applied"] == {"l2": "homelab-expert-l2"}
    assert body["no_agent_found"] == ["coder"]
    assert body["applied_to_disk"] is False
    # and nothing was written
    assert "agent_profile" not in sections["profiles"]["l2"]


def test_the_seed_writes_when_asked_and_is_then_idempotent(profiles_root):
    from src.server.handler import LCPHandler
    cfg, sections = _real_config({
        "l2": {"chain": [{"provider": "deepseek", "model": "deepseek-chat"}]},
        "coder": {"chain": [{"provider": "deepseek", "model": "deepseek-chat"}]},
    })
    LCPHandler.config = cfg
    h = _handler("/api/profiles/mapping/seed", "POST", {"apply": True}, cfg)
    h._serve_profile_mapping_seed()
    assert sections["profiles"]["l2"]["agent_profile"] == "homelab-expert-l2"
    assert cfg.profiles["l2"]["agent_profile"] == "homelab-expert-l2"   # live in memory too
    # the seeded value is loadable — the point of validating on the write path
    _validate("profiles", sections["profiles"])

    h2 = _handler("/api/profiles/mapping/seed", "POST", {"apply": True}, cfg)
    h2._serve_profile_mapping_seed()
    _, body = _sent(h2)
    assert body["applied"] == {}                      # nothing left to do
    assert body["already_declared"] == {"l2": "homelab-expert-l2"}


def test_the_seed_never_overwrites_a_declaration(profiles_root):
    from src.server.handler import LCPHandler
    cfg, sections = _real_config({
        "l2": {"chain": [{"provider": "deepseek", "model": "deepseek-chat"}],
               "agent_profile": "blog-writer"},
    })
    LCPHandler.config = cfg
    h = _handler("/api/profiles/mapping/seed", "POST", {"apply": True}, cfg)
    h._serve_profile_mapping_seed()
    _, body = _sent(h)
    assert body["applied"] == {}
    assert body["already_declared"] == {"l2": "blog-writer"}
    assert sections["profiles"]["l2"]["agent_profile"] == "blog-writer"


def test_a_profile_update_accepts_the_new_fields(profiles_root):
    from src.server.handler import LCPHandler
    cfg, sections = _real_config({
        "l2": {"chain": [{"provider": "deepseek", "model": "deepseek-chat"}]}})
    LCPHandler.config = cfg
    h = _handler("/api/profiles/l2", "PUT",
                 {"agent_profile": "homelab-expert-l2", "routing": "dynamic",
                  "intents": ["planning", "planning"]}, cfg)
    h._serve_profile_update("l2")
    status, _ = _sent(h)
    assert status == 200
    stored = sections["profiles"]["l2"]
    assert stored["agent_profile"] == "homelab-expert-l2"
    assert stored["routing"] == "dynamic"
    assert stored["intents"] == ["planning"]
    _validate("profiles", sections["profiles"])       # still loadable


def test_a_bad_agent_profile_is_refused_without_a_partial_write(profiles_root):
    from src.server.handler import LCPHandler
    cfg, sections = _real_config({
        "l2": {"chain": [{"provider": "deepseek", "model": "deepseek-chat"}]}})
    LCPHandler.config = cfg
    h = _handler("/api/profiles/l2", "PUT",
                 {"agent_profile": "../../etc/passwd", "routing": "dynamic"}, cfg)
    h._serve_profile_update("l2")
    status, body = _sent(h)
    assert status == 400
    assert "agent_profile" in body["error"]
    # neither field landed: a refused write must not half-apply
    assert "agent_profile" not in sections["profiles"]["l2"]
    assert "routing" not in sections["profiles"]["l2"]


def test_create_seeds_a_chain_from_the_default_profile(profiles_root):
    """A profile created with no chain would make the whole section unloadable."""
    from src.server.handler import LCPHandler
    cfg, sections = _real_config({
        "l2": {"chain": [{"provider": "deepseek", "model": "deepseek-chat"}]}})
    LCPHandler.config = cfg
    h = _handler("/api/profiles", "POST",
                 {"name": "newbie", "agent_profile": "blog-writer"}, cfg)
    h._serve_profile_create()
    status, body = _sent(h)
    assert status == 200, body
    assert sections["profiles"]["newbie"]["chain"] == [
        {"provider": "deepseek", "model": "deepseek-chat"}]
    assert sections["profiles"]["newbie"]["agent_profile"] == "blog-writer"
    # the whole section still loads — which is the failure this prevents
    _validate("profiles", sections["profiles"])
    assert sorted(sections["profiles"]) == ["l2", "newbie"]


def test_the_chain_endpoint_refuses_to_empty_a_profile(profiles_root):
    from src.server.handler import LCPHandler
    cfg, sections = _real_config({
        "l2": {"chain": [{"provider": "deepseek", "model": "deepseek-chat"}]}})
    LCPHandler.config = cfg
    h = _handler("/api/chains/l2", "PUT", {"chain": []}, cfg)
    h._serve_chain_reorder("l2")
    status, body = _sent(h)
    assert status == 400
    assert "at least one model" in body["error"].lower()
    assert sections["profiles"]["l2"]["chain"], "the chain must survive a refused write"


def test_the_profiles_list_offers_a_suggestion_when_nothing_is_declared(profiles_root):
    from src.server.handler import LCPHandler
    cfg, _ = _real_config({
        "l2": {"chain": [{"provider": "deepseek", "model": "deepseek-chat"}]}})
    LCPHandler.config = cfg
    h = _handler("/api/profiles", "GET", None, cfg)
    h.headers = {"Host": "localhost:8735"}
    h._serve_profiles_list()
    _, body = _sent(h)
    entry = body["profiles"]["l2"]
    assert entry["agent_profile"] == ""
    assert entry["suggested_agent_profile"] == "homelab-expert-l2"
    assert entry["routing"] == "static" and entry["intents"] == []


def test_the_profiles_list_carries_the_new_fields(profiles_root):
    from src.server.handler import LCPHandler
    cfg, _ = _real_config({
        "l2": {"chain": [{"provider": "deepseek", "model": "deepseek-chat"}],
               "agent_profile": "homelab-expert-l2", "routing": "dynamic",
               "intents": ["planning"]}})
    LCPHandler.config = cfg
    h = _handler("/api/profiles", "GET", None, cfg)
    h.headers = {"Host": "localhost:8735"}
    h._serve_profiles_list()
    _, body = _sent(h)
    entry = body["profiles"]["l2"]
    assert entry["agent_profile"] == "homelab-expert-l2"
    assert entry["routing"] == "dynamic"
    assert entry["intents"] == ["planning"]
    assert "blog-writer" in entry["routed_by"]     # read from its own config
    assert entry["suggested_agent_profile"] == ""  # declared, so nothing to suggest
