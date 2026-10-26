"""Guard: the surfaces cut in the slim-down must stay cut.

Phase 3 of the refactor plan. Without it, the cut is a one-time act and the
next person to add "just a small Fleet page" regrows the exact surface that
made the control plane 26.8k lines.

This pins two things that a human review would otherwise have to remember:

1. the route names — a removed page cannot come back silently;
2. the modules behind them — a route that returns doesn't help if the 845-line
   reader it needs was re-added under a different name.

Deliberately NOT pinned: the exact total route count. That changes whenever a
legitimate feature lands, and a test that fails on every good change gets
deleted rather than fixed.
"""

from __future__ import annotations

import importlib

import pytest

from src.server.handler import _build_routes


def _route_names() -> set[str]:
    return {r.name for r in _build_routes().rules}


# Every route registration removed by the slim-down (C1–C6).
CUT_ROUTES = {
    # C1 — Tasks
    "page.work.tasks",
    "api.work.tasks",
    "api.work.tasks.detail",
    "api.work.status",
    # C2 — Fleet
    "page.work.fleet",
    "api.work.fleet",
    # C3 — Board decisions
    "api.work.decisions",
    # C5 — retired conversations realm
    "api.work.conversations",
    "api.work.conversations.detail",
    # C6 — first-run wizard
    "page.setup",
    "api.setup",
    "api.setup.progress",
    "api.setup.skip",
    "api.setup.install",
    # C7 — agent-infrastructure surface (2026-10-25)
    # Cron and Config left the UI in two waves; these five routes had no caller
    # left. Two of them were writes: POST spooled cron mutation intents, PUT
    # repointed every profile's task-tree root, cron store and op spool.
    "api.work.cron",
    "api.work.cron.ops",
    "api.work.sources",
}

# Modules deleted outright. Re-adding any of them restores the surface even if
# the route names above were changed to slip past this test.
CUT_MODULES = [
    "src.api.work_tasks",
    "src.api.work_fleet",
    "src.api.workspace",
    "src.api.work",
    "src.api.setup",
    # C7 — with the five routes above gone, these had no live root:
    #   work_cron.tasks_dir <- _snapshot_path <- cron_view <- resolved_view
    "src.api.work_cron",
    "src.api.work_sources",
]


class TestCutRoutesAreGone:
    @pytest.mark.parametrize("name", sorted(CUT_ROUTES))
    def test_route_is_not_registered(self, name):
        assert name not in _route_names(), (
            f"{name} was removed in the slim-down and must not be re-registered"
        )

    def test_no_page_route_reaches_the_cut_prefixes(self):
        """A future route can't sneak back under a removed URL space."""
        dead_prefixes = ("page.work.tasks", "page.work.fleet", "page.setup",
                         "api.work.tasks", "api.work.fleet", "api.setup")
        for name in _route_names():
            assert not name.startswith(dead_prefixes), (
                f"{name} lives under a URL space the slim-down removed"
            )


class TestCutModulesAreGone:
    @pytest.mark.parametrize("mod", CUT_MODULES)
    def test_module_does_not_import(self, mod):
        with pytest.raises(ImportError):
            importlib.import_module(mod)


class TestSurvivorsAreIntact:
    """The other half of the guard: the cut must not have taken these with it."""

    KEEP_MODULES = [
        "src.api.router",
        "src.api.request_pipeline",
        "src.api.circuit_breaker",
        "src.api.task_classifier",
        "src.api.conv_store",
        "src.api.key_manager",
        "src.api.alert_manager",
        "src.api.seed_capabilities",
        # src.api.work_cron and src.api.work_sources are NOT here any more: they
        # were kept when the Cron and Config tabs existed, and deleted with them on
        # 2026-10-25. Their names moved into CUT_MODULES instead.
    ]

    @pytest.mark.parametrize("mod", KEEP_MODULES)
    def test_module_still_imports(self, mod):
        assert importlib.import_module(mod) is not None

    def test_work_conversations_keeps_its_live_half(self):
        """Only the dead half was cut; requests_view feeds the Logs tab."""
        from src.api import work_conversations as wc

        for fn in ("requests_view", "provider_decisions_view",
                   "ensure_schema", "stamp_new", "db_path", "_connect"):
            assert hasattr(wc, fn), f"{fn} is load-bearing and must survive"

    def test_work_conversations_dropped_its_dead_half(self):
        from src.api import work_conversations as wc

        for gone in ("conversations_view", "conversation_detail",
                     "sync_conversations", "backfill_conversations"):
            assert not hasattr(wc, gone), f"{gone} has no live caller"
