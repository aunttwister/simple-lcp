"""M2c — the profile surface: cards, the level-3 profile page, breadcrumbs.

Four things this pins, each of which fails silently rather than loudly:

1. **A profile name from a URL is a path segment, not a file path.** The name
   reaches the filesystem, so traversal, absolute paths and empty names are
   checked here — the failure mode without the guard is a file-read primitive.
2. **Only the profile's own artefacts are readable.** A profile directory also
   holds ``.env`` (provider keys) and a session database. A viewer that exists to
   list skills must never be a way to read those.
3. **An upload is checked against its bytes.** Avatars are served back from the
   directory they are written to, so a mislabelled file would be a way to place
   arbitrary content on a page.
4. **Every page names its location.** Breadcrumbs are rendered from a `crumbs`
   list the renderers supply; a page that stops supplying one would just quietly
   lose its trail.
"""
import base64
import os
import struct
import zlib

import pytest
from unittest.mock import MagicMock

from src.api import profile_data

SEC = os.path.join(os.path.dirname(__file__), "..", "src", "ui", "templates", "jinja", "sections")


# ── fixtures ─────────────────────────────────────────────────────────────────

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 1, 1)
       + b"\x08\x06\x00\x00\x00" + b"\x1f\x15\xc4\x89" + b"IDAT" + b"\x78\x9c"
       + zlib.compress(b"\x00\xff\xff\xff") + b"\x00\x00\x00\x00IEND\xaeB`\x82")
JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + b"\x00" * 40 + b"\xff\xd9"
WEBP = b"RIFF" + struct.pack("<I", 24) + b"WEBP" + b"VP8 " + b"\x00" * 20


@pytest.fixture
def profiles_root(tmp_path, monkeypatch):
    """A miniature profiles root: two profiles, one nested skill layout."""
    root = tmp_path / "profiles"
    (root / "l2").mkdir(parents=True)
    skills = root / "l2" / "skills"
    (skills / "devops" / "alpha-skill").mkdir(parents=True)
    (skills / "devops" / "alpha-skill" / "SKILL.md").write_text(
        "---\nname: alpha-skill\ndescription: Does the alpha thing\n---\n\nbody\n")
    (skills / "flat-skill").mkdir(parents=True)
    (skills / "flat-skill" / "SKILL.md").write_text(
        "---\nname: flat-skill\ndescription: >\n  A folded\n  description here\n---\n\nbody\n")
    (skills / "nodesc").mkdir(parents=True)
    (skills / "nodesc" / "SKILL.md").write_text("# no frontmatter\n")
    memories = root / "l2" / "memories"
    memories.mkdir()
    (memories / "MEMORY.md").write_text("Docker: compose\n" * 40)
    (memories / "USER.md").write_text("Pavle\n")
    # a profile directory that is not a gateway profile, and a secret to protect
    (root / "stray").mkdir()
    (root / "stray" / ".env").write_text("DEEPSEEK_API_KEY=sk-do-not-serve-this\n")
    avatar_dir = tmp_path / "avatars"
    monkeypatch.setenv("LCP_PROFILES_DIR", str(root))
    monkeypatch.setenv("LCP_AVATAR_DIR", str(avatar_dir))
    return root


@pytest.fixture
def engine(temp_db):
    return temp_db[1]


@pytest.fixture
def cfg():
    c = MagicMock()
    c.providers = {"deepseek": {"api_key_env": "DEEPSEEK_API_KEY",
                                "api_base": "https://api.deepseek.com/v1",
                                "models": ["deepseek-chat"]}}
    c.profiles = {
        "l2": {"forbidden_tools": [], "chain": [], "description": "Routes the L2 homelab expert"},
        "l1": {"forbidden_tools": [], "chain": []},
    }
    c.server = {"port": 8734}
    return c


# ── 1. the name is a path segment ────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "..", "../..", "../../etc/passwd", "a/b", "/etc/passwd", "", "   ",
    "x" * 65, "na me", ".", "..%2f..", "l2;rm -rf /",
])
def test_dangerous_profile_names_are_rejected(bad):
    with pytest.raises(profile_data.BadProfileName):
        profile_data.validate_name(bad)


@pytest.mark.parametrize("good", ["l2", "l1", "homelab-expert-l2", "blog_writer", "a.b", "A-1"])
def test_real_profile_names_are_accepted(good):
    assert profile_data.validate_name(good) == good


def test_surrounding_whitespace_is_stripped_not_rejected():
    """A name arrives from a URL and from a form; trailing whitespace is noise,
    and rejecting it would fail a request that names a real profile."""
    assert profile_data.validate_name("  l2\n") == "l2"


def test_traversal_never_reaches_the_filesystem(profiles_root):
    """Even if a caller skips validate_name, profile_dir must refuse."""
    for bad in ("..", "../../etc", "l2/../l2", "/etc"):
        try:
            assert profile_data.profile_dir(bad) is None
        except profile_data.BadProfileName:
            pass  # refusing loudly is also correct


def test_unknown_profile_directory_is_none(profiles_root):
    assert profile_data.profile_dir("no-such-profile") is None


def test_the_secret_file_is_not_reachable(profiles_root):
    """The skills/memory views expose the two artefacts and nothing else."""
    skills = profile_data.skills_view("stray")
    memory = profile_data.memory_view("stray")
    blob = repr(skills) + repr(memory)
    assert "do-not-serve-this" not in blob
    assert ".env" not in blob


# ── 2. skills ────────────────────────────────────────────────────────────────

def test_skills_are_listed_with_their_category(profiles_root):
    view = profile_data.skills_view("l2")
    assert view["available"] and view["count"] == 3
    by_name = {s["name"]: s for s in view["skills"]}
    assert by_name["alpha-skill"]["category"] == "devops"
    assert by_name["alpha-skill"]["description"] == "Does the alpha thing"
    assert by_name["flat-skill"]["category"] == ""      # flat layout has no category
    assert view["categories"] == ["devops"]


def test_a_folded_description_is_read_not_returned_as_its_marker(profiles_root):
    """`description: >` is in use; returning ">" would be a silent wrong answer."""
    by_name = {s["name"]: s for s in profile_data.skills_view("l2")["skills"]}
    assert by_name["flat-skill"]["description"] == "A folded description here"


def test_a_skill_without_frontmatter_is_listed_not_dropped(profiles_root):
    by_name = {s["name"]: s for s in profile_data.skills_view("l2")["skills"]}
    assert "nodesc" in by_name and by_name["nodesc"]["description"] == ""


def test_missing_skills_directory_degrades_to_unavailable(profiles_root):
    view = profile_data.skills_view("stray")
    assert view["available"] is False and view["count"] == 0
    assert "skills directory" in view["reason"]


def test_the_skill_cap_is_reported_not_silent(tmp_path, monkeypatch):
    root = tmp_path / "p"
    for i in range(3):
        d = root / "l2" / "skills" / ("s%02d" % i)
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: s%02d\n---\n" % i)
    monkeypatch.setenv("LCP_PROFILES_DIR", str(root))
    monkeypatch.setattr(profile_data, "SKILL_CAP", 2)
    view = profile_data.skills_view("l2")
    assert view["count"] == 3 and len(view["skills"]) == 2 and view["truncated"] is True


# ── 2b. memory ───────────────────────────────────────────────────────────────

def test_memory_reports_both_files_and_their_sizes(profiles_root):
    view = profile_data.memory_view("l2")
    names = [f["name"] for f in view["files"]]
    assert names == ["MEMORY.md", "USER.md"]
    assert view["files"][0]["chars"] == len(open(profiles_root / "l2" / "memories" / "MEMORY.md").read())
    assert view["total_chars"] == sum(f["chars"] for f in view["files"])
    assert view["files"][0]["lines"] > 1


def test_missing_memories_directory_degrades(profiles_root):
    view = profile_data.memory_view("stray")
    assert view["available"] is False and view["files"] == []


def test_memory_truncation_is_flagged(tmp_path, monkeypatch):
    root = tmp_path / "p"
    (root / "l2" / "memories").mkdir(parents=True)
    (root / "l2" / "memories" / "MEMORY.md").write_text("x" * 500)
    monkeypatch.setenv("LCP_PROFILES_DIR", str(root))
    monkeypatch.setattr(profile_data, "MEMORY_CHAR_CAP", 100)
    f = profile_data.memory_view("l2")["files"][0]
    assert f["chars"] == 500 and len(f["text"]) == 100 and f["truncated"] is True


# ── 3. avatars ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ctype,payload", [("image/png", PNG), ("image/jpeg", JPEG),
                                           ("image/webp", WEBP)])
def test_avatar_round_trip(profiles_root, ctype, payload):
    assert profile_data.avatar_for("l2") is None
    out = profile_data.save_avatar("l2", ctype, payload)
    assert out["bytes"] == len(payload)
    found = profile_data.avatar_for("l2")
    assert found and found[1] == ctype and open(found[0], "rb").read() == payload
    assert profile_data.delete_avatar("l2")["removed"]
    assert profile_data.avatar_for("l2") is None


def test_a_mislabelled_upload_is_refused(profiles_root):
    """Content type is checked against the bytes: the file gets served back."""
    with pytest.raises(ValueError, match="not a valid"):
        profile_data.save_avatar("l2", "image/png", b"<script>alert(1)</script>")


def test_an_unsupported_type_is_refused(profiles_root):
    with pytest.raises(ValueError, match="unsupported image type"):
        profile_data.save_avatar("l2", "image/svg+xml", b"<svg/>")


def test_an_oversized_upload_is_refused(profiles_root):
    with pytest.raises(ValueError, match="larger than"):
        profile_data.save_avatar("l2", "image/png", PNG + b"\x00" * (256 * 1024))


def test_an_empty_upload_is_refused(profiles_root):
    with pytest.raises(ValueError, match="empty"):
        profile_data.save_avatar("l2", "image/png", b"")


def test_saving_a_new_format_replaces_the_old_one(profiles_root):
    """One picture per profile: a JPEG after a PNG must not leave both."""
    profile_data.save_avatar("l2", "image/png", PNG)
    profile_data.save_avatar("l2", "image/jpeg", JPEG)
    assert profile_data.avatar_for("l2")[1] == "image/jpeg"
    assert sorted(os.listdir(profile_data.avatar_dir())) == ["l2.jpg"]


def test_avatar_helpers_refuse_a_dangerous_name(profiles_root):
    for bad in ("..", "a/b", ""):
        with pytest.raises(profile_data.BadProfileName):
            profile_data.avatar_for(bad)
        with pytest.raises(profile_data.BadProfileName):
            profile_data.save_avatar(bad, "image/png", PNG)
        with pytest.raises(profile_data.BadProfileName):
            profile_data.delete_avatar(bad)


def test_deleting_a_missing_avatar_is_not_an_error(profiles_root):
    assert profile_data.delete_avatar("l2")["removed"] == []


# ── the card ─────────────────────────────────────────────────────────────────

def test_a_card_keeps_the_description_to_ten_words(cfg):
    from src.ui import pages
    long_desc = " ".join("word%d" % i for i in range(18))
    card = pages._profile_card(cfg, "l2", {"description": long_desc}, {})
    assert card["words"] == 18
    assert len(card["short_description"].split()) == 10
    assert card["short_description"].endswith("\u2026")


def test_a_short_description_is_not_ellipsised(cfg):
    from src.ui import pages
    card = pages._profile_card(cfg, "l2", {"description": "Routes the L2 homelab expert"}, {})
    assert card["short_description"] == "Routes the L2 homelab expert"
    assert not card["short_description"].endswith("\u2026")


def test_a_card_without_a_description_is_marked_empty(cfg):
    from src.ui import pages
    card = pages._profile_card(cfg, "l1", {}, {})
    assert card["words"] == 0 and card["description"] == "" and card["avatar"] is False


def test_a_card_carries_the_at_a_glance_facts(cfg):
    from src.ui import pages
    cfg.profiles["l2"]["chain"] = [{"provider": "opencode", "model": "qwen"}]
    card = pages._profile_card(cfg, "l2", cfg.profiles["l2"], {})
    assert card["chain_label"] == "opencode/qwen"
    assert card["auth_label"] == "key required"
    assert card["initials"] == "L2" and card["href"] == "/profiles/l2"


def test_a_card_handles_object_style_chain_steps(cfg):
    """The chain is objects in one code path and dicts in another."""
    from src.ui import pages
    step = MagicMock()
    step.provider, step.model = "deepseek", "deepseek-chat"
    card = pages._profile_card(cfg, "l2", {"chain": [step]}, {})
    assert card["chain_label"] == "deepseek/deepseek-chat"


# ── 4. the pages ─────────────────────────────────────────────────────────────

def test_every_level_two_page_has_a_breadcrumb(cfg, engine):
    from src.ui import pages
    for label, html in (
        ("/profiles", pages.render_profiles_page(cfg, engine, {})),
        ("/profiles?tab=config", pages.render_profiles_page(cfg, engine, {"tab": "config"})),
        ("/models", pages.render_models_page(cfg, engine, {})),
        ("/activity", pages.render_activity_page(cfg, engine, {}, {"Host": "t:8734"})),
        ("/usage", pages.render_usage_page(cfg, engine, {})),
        ("/alerts", pages.render_alerts_page(cfg, engine)),
        ("/setup", pages.render_setup_page(cfg, engine)),
        ("/work/tasks", pages.render_work_tasks_page(cfg, engine, {})),
        ("/work/fleet", pages.render_work_fleet_page(cfg, engine)),
    ):
        assert '<nav class="breadcrumbs"' in html, label
        assert 'class="crumb-current"' in html, label


def test_the_profile_page_trail_is_three_deep(cfg, engine):
    """Profiles › <name> › <tab> — the shape the whole change exists for."""
    from src.ui import pages
    html = pages.render_profile_detail_page(cfg, engine, "l2", {"tab": "skills"})
    assert 'href="/profiles"' in html
    assert 'href="/profiles/l2"' in html
    assert 'class="crumb-current" aria-current="page">Skills<' in html


def test_the_directory_crumb_has_no_link(cfg, engine):
    from src.ui import pages
    html = pages.render_profiles_page(cfg, engine, {})
    assert '<span class="crumb-current" aria-current="page">Profiles</span>' in html


@pytest.mark.parametrize("tab", ["skills", "memory", "tasks", "cron", "keys"])
def test_each_profile_tab_renders_only_its_own_section(cfg, engine, tab):
    """Section isolation across the five new tabs."""
    from src.ui import pages
    html = pages.render_profile_detail_page(cfg, engine, "l2", {"tab": tab})
    markers = {}
    for name in ("sec_skills", "sec_memory", "sec_ptasks", "sec_cron", "sec_keys"):
        txt = open(os.path.join(SEC, name + ".html")).read()
        if name == "sec_skills":
            markers["skills"] = "profileSkills"
        elif name == "sec_memory":
            markers["memory"] = "profileMemory"
        elif name == "sec_ptasks":
            markers["tasks"] = "task-count-line"
        elif name == "sec_cron":
            markers["cron"] = "cron-modal"
        else:
            markers["keys"] = "keyStacks"
    for name, marker in markers.items():
        if name == tab:
            assert marker in html, "%s missing from the %s tab" % (name, tab)
        else:
            assert marker not in html, "%s leaked into the %s tab" % (name, tab)


def test_the_profile_page_offers_all_five_tabs(cfg, engine):
    from src.ui import pages
    html = pages.render_profile_detail_page(cfg, engine, "l2", {})
    for href in ("/profiles/l2?tab=skills", "/profiles/l2?tab=memory", "/profiles/l2?tab=tasks",
                 "/profiles/l2?tab=cron", "/profiles/l2?tab=keys"):
        assert 'href="%s"' % href in html, href
    assert 'class="tab-btn active"' in html


def test_an_unknown_profile_renders_a_page_not_a_traceback(cfg, engine):
    from src.ui import pages
    html = pages.render_profile_detail_page(cfg, engine, "ghost", {})
    assert "No such profile" in html and "ghost" in html
    assert '<nav class="breadcrumbs"' in html


@pytest.mark.parametrize("bad", ["..", "../..", "../../etc/passwd", "etc/passwd", ""])
def test_a_traversing_name_renders_the_not_found_page(cfg, engine, bad):
    from src.ui import pages
    html = pages.render_profile_detail_page(cfg, engine, bad, {})
    assert "No such profile" in html
    assert "root:" not in html


def test_a_directory_that_is_not_a_gateway_profile_is_labelled(cfg, engine, profiles_root):
    """Visible but not routable is a real state and the page says so."""
    from src.ui import pages
    html = pages.render_profile_detail_page(cfg, engine, "stray", {})
    assert "Not a gateway profile" in html


def test_a_profile_name_is_escaped_in_the_page(cfg, engine):
    """The name reaches markup; a name is never HTML."""
    from src.ui import pages
    html = pages.render_profile_detail_page(cfg, engine, "<script>", {})
    assert "<script>alert" not in html


# ── the per-profile task tree ────────────────────────────────────────────────

def test_use_root_scopes_and_restores(profiles_root):
    """The Tasks tab runs the same readers against another tree; the override
    must not survive the block, or the next request inherits the wrong tree."""
    from src.api import work_tasks
    before = work_tasks.tasks_dir()
    with work_tasks.use_root("/tmp/somewhere-else"):
        assert work_tasks.tasks_dir() == "/tmp/somewhere-else"
    assert work_tasks.tasks_dir() == before


def test_use_root_nests(profiles_root):
    from src.api import work_tasks
    outer = work_tasks.tasks_dir()
    with work_tasks.use_root("/a"):
        with work_tasks.use_root("/b"):
            assert work_tasks.tasks_dir() == "/b"
        assert work_tasks.tasks_dir() == "/a"
    assert work_tasks.tasks_dir() == outer


def test_tasks_root_for_returns_empty_for_no_profile(monkeypatch):
    from src.api import work_sources
    assert work_sources.tasks_root_for("") == ""


def test_tasks_root_for_reads_the_profile_override(monkeypatch):
    from src.api import work_sources
    monkeypatch.setattr(work_sources, "resolved_view",
                        lambda: {"profiles": {"l2": {"tasks_root": "/t/l2"}}})
    assert work_sources.tasks_root_for("l2") == "/t/l2"


def test_tasks_root_for_survives_a_broken_sources_file(monkeypatch):
    from src.api import work_sources
    def boom():
        raise ValueError("bad sources")
    monkeypatch.setattr(work_sources, "resolved_view", boom)
    assert work_sources.tasks_root_for("l2") == ""


def test_the_profile_tasks_tab_uses_the_profiles_tree(cfg, engine, monkeypatch, tmp_path):
    """The tab must read the profile's tree, not the default one.

    Built on a real fixture tree rather than a stubbed view: the point is that the
    readers produce a normal view under the override, which a stub could not show.
    """
    from src.api import work_sources
    from src.api import work_tasks as wt
    from src.ui import pages

    tree = tmp_path / "tasks"
    (tree / "new" / "alpha-task").mkdir(parents=True)
    (tree / "new" / "alpha-task" / "PLAN.md").write_text("# Alpha task\n\nDo the alpha thing.\n")
    (tree / "in_progress" / "beta-task").mkdir(parents=True)
    (tree / "in_progress" / "beta-task" / "PLAN.md").write_text("# Beta task\n\nDo the beta thing.\n")

    monkeypatch.setattr(work_sources, "resolved_view",
                        lambda: {"profiles": {"l2": {"tasks_root": str(tree)}}})
    seen = {}
    real = wt.tasks_view

    def spy(params):
        seen["root"] = wt.tasks_dir()
        return real(params)

    monkeypatch.setattr(wt, "tasks_view", spy)
    html = pages.render_profile_detail_page(cfg, engine, "l2", {"tab": "tasks"})
    assert seen["root"] == str(tree)
    # The rows are fetched by the table's own script, so the proof that the right
    # tree was read is the payload the server hands that script.
    assert '"total": 2' in html
    assert 'tasks-init' in html


def test_the_profile_tasks_tab_survives_an_unreadable_tree(cfg, engine, monkeypatch):
    """A viewer that 500s on a bad path is worse than one that says why it is empty."""
    from src.api import work_sources
    from src.ui import pages
    monkeypatch.setattr(work_sources, "resolved_view",
                        lambda: {"profiles": {"l2": {"tasks_root": "/definitely/not/here"}}})
    html = pages.render_profile_detail_page(cfg, engine, "l2", {"tab": "tasks"})
    assert "no task directories found at /definitely/not/here" in html
    # and the init payload is still well formed, so the script does not throw
    assert '"total": 0' in html
