"""Tests for src/api/cost_plugins/opencode_api.py — console budget routes.

The SSR-scrape suites (subscription/billing parsers and their cookie transport)
were removed with the code they covered: those pages return a client shell
now, so the only reachable non-JSON path was already dead.  What remains is the
budget surface, which is live on the console.
"""
import pytest
from unittest.mock import patch


from src.api.cost_plugins.opencode_api import (
    BUDGET_MEMBERS_PATH_V1,
    BUDGET_MEMBERS_PATH_V2,
    ConsoleApiError,
    budget_rules,
    fetch_budget_members,
)


# ═══════════════════════════════════════════════════════════════════════
# fetch_budget_members — v2 with v1 fallback
# ═══════════════════════════════════════════════════════════════════════

class TestFetchBudgetMembers:
    """The vendor's policy-backed v2 line, with v1 kept as the fallback.

    Contract recovered from the console's own SPA bundle (2026-10-11) and
    confirmed live: v2 returns the v1 keys plus ``additional_budget_rules``,
    with the *same* money units, and documents HTTP 409 for budget rows the
    vendor has not yet converted to policies.
    """

    def test_reads_v2_by_default(self):
        rows = [{"user_id": "acc_1", "limit_micro_cents": None,
                 "spent_micro_cents": "0", "additional_budget_rules": []}]
        with patch("src.api.cost_plugins.opencode_api._console_get",
                   return_value=rows) as get:
            assert fetch_budget_members("key") == rows
        assert get.call_args[0][0] == BUDGET_MEMBERS_PATH_V2

    def test_unconverted_409_falls_back_to_v1(self):
        """v2 409 ("unconverted legacy budget") must not blank the card."""
        rows = [{"user_id": "acc_1", "limit_micro_cents": 500_000_000}]
        err = ConsoleApiError(409, "u", "Conflict", "unconverted legacy budget")
        with patch("src.api.cost_plugins.opencode_api._console_get",
                   side_effect=[err, rows]) as get:
            assert fetch_budget_members("key") == rows
        paths = [c[0][0] for c in get.call_args_list]
        assert paths == [BUDGET_MEMBERS_PATH_V2, BUDGET_MEMBERS_PATH_V1]

    def test_other_errors_propagate(self):
        """A 401 is a credential problem, not a version problem."""
        err = ConsoleApiError(401, "u", "Unauthorized", "")
        with patch("src.api.cost_plugins.opencode_api._console_get", side_effect=err):
            with pytest.raises(ConsoleApiError):
                fetch_budget_members("key")

    def test_v1_request_never_falls_back(self):
        err = ConsoleApiError(409, "u", "Conflict", "")
        with patch("src.api.cost_plugins.opencode_api._console_get",
                   side_effect=err) as get:
            with pytest.raises(ConsoleApiError):
                fetch_budget_members("key", version="v1")
        assert get.call_args[0][0] == BUDGET_MEMBERS_PATH_V1

    def test_unfamiliar_shape_degrades_to_empty(self):
        with patch("src.api.cost_plugins.opencode_api._console_get",
                   return_value={"unexpected": True}):
            assert fetch_budget_members("key") == []


# ═══════════════════════════════════════════════════════════════════════
# budget_rules — v2-only configuration, never a bar
# ═══════════════════════════════════════════════════════════════════════

class TestBudgetRules:
    def test_maps_rules_to_usd_descriptors(self):
        members = [{
            "user_id": "acc_1",
            "additional_budget_rules": [{
                "policy_id": "pol_1", "rule_id": "rul_1",
                "limit_micro_cents": 1_000_000_000,          # $10
                "audience": "shared", "scope": "combined",
                "per": "set", "selector": "glm-5.3-flash",
            }],
        }]
        rules = budget_rules(members)
        assert len(rules) == 1
        assert rules[0]["limit_usd"] == 10.0
        assert rules[0]["scope"] == "combined"
        assert rules[0]["per"] == "set"
        assert rules[0]["selector"] == "glm-5.3-flash"

    def test_v1_rows_have_no_rules(self):
        assert budget_rules([{"user_id": "acc_1", "limit_micro_cents": 1}]) == []
        assert budget_rules([]) == []
        assert budget_rules(None) == []

    def test_malformed_rules_are_skipped(self):
        members = [{"additional_budget_rules": [None, "nope", {"rule_id": "ok"}]},
                   "not-a-row"]
        assert [r["rule_id"] for r in budget_rules(members)] == ["ok"]
