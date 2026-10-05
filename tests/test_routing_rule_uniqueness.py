"""Routing rules: one rule per (profile, intent), enforced server-side.

The operator reported being able to set multiple rules for the same intent.
Two rules for one intent in one scope make the outcome depend on list ORDER,
which is precisely what a prefer/block rule exists to remove — so the API
refuses the second rule and says which intent collided.

The uniqueness key mirrors ``Router._rule_matches`` (which compares a rule's
``profile`` and ``task``), so the API cannot accept a pair the resolver would
treat as ambiguous. Profile-scoped rules REPLACE the global list rather than
merging with it, so a global rule and a profile rule for the same intent are
legitimately different scopes and both allowed.
"""

import pytest

from src.server.endpoints import validate_routing_rules


def _rule(task="code_generation", profile="l2", **kw):
    rule = {"task": task, "profile": profile, "action": "prefer", "model": "deepseek-v4.1-flash"}
    rule.update(kw)
    return rule


class TestUniqueness:
    def test_two_rules_for_one_intent_are_rejected(self):
        err = validate_routing_rules([_rule(), _rule(model="deepseek-v4-pro")])
        assert err is not None
        assert "code_generation" in err and "l2" in err
        assert "One rule per intent" in err

    def test_the_error_names_the_offending_rule(self):
        err = validate_routing_rules([_rule(), _rule(action="block")])
        assert "rule 1" in err

    def test_distinct_intents_are_accepted(self):
        assert validate_routing_rules([_rule(task="code_generation"),
                                       _rule(task="debugging")]) is None

    def test_the_same_intent_in_two_scopes_is_accepted(self):
        """Global vs profile-scoped are different scopes (profile list replaces
        the global one), so this is not the reported bug."""
        assert validate_routing_rules([_rule(profile="*"), _rule(profile="l2")]) is None

    def test_missing_profile_defaults_to_the_global_scope(self):
        a = {"task": "planning", "action": "prefer", "model": "m"}
        b = {"task": "planning", "profile": "*", "action": "prefer", "model": "m2"}
        err = validate_routing_rules([a, b])
        assert err is not None and "planning" in err

    def test_wildcard_intent_is_its_own_key(self):
        assert validate_routing_rules([
            _rule(task="*"), _rule(task="code_generation")]) is None
        err = validate_routing_rules([_rule(task="*"), _rule(task="*")])
        assert err is not None

    def test_three_rules_two_duplicates_reports_the_first_collision(self):
        err = validate_routing_rules([_rule(task="a"), _rule(task="b"), _rule(task="a")])
        assert "rule 2" in err and "'a'" in err

    def test_list_valued_task_is_keyed_as_a_tuple(self):
        """A rule matching many intents must not collide with its own members."""
        assert validate_routing_rules([
            _rule(task=["code_generation", "debugging"]),
            _rule(task="planning")]) is None
        err = validate_routing_rules([
            _rule(task=["code_generation", "debugging"]),
            _rule(task=["code_generation", "debugging"])])
        assert err is not None

    def test_whitespace_is_normalised_so_it_cannot_smuggle_a_duplicate(self):
        err = validate_routing_rules([{"task": "planning", "profile": "l2",
                                       "action": "prefer", "model": "m"},
                                      {"task": " planning ", "profile": " l2 ",
                                       "action": "block", "model": "m2"}])
        assert err is not None


class TestPreExistingValidationStillHolds:
    def test_rules_must_be_a_list(self):
        assert validate_routing_rules({"task": "x"}) is not None
        assert validate_routing_rules(None) is not None

    def test_rule_must_be_an_object(self):
        assert validate_routing_rules(["nope"]) is not None

    def test_action_must_be_valid(self):
        assert validate_routing_rules([_rule(action="maybe")]) is not None

    def test_prefer_block_need_a_target(self):
        err = validate_routing_rules([{"task": "t", "action": "prefer"}])
        assert err is not None
        assert validate_routing_rules([{"task": "t", "action": "prefer",
                                        "provider": "deepseek"}]) is None
        assert validate_routing_rules([{"task": "t", "action": "block",
                                        "model": "deepseek-v4.1-flash"}]) is None

    def test_policy_rule_needs_a_valid_policy(self):
        assert validate_routing_rules([{"task": "t", "action": "policy",
                                        "policy": "cost_first"}]) is None
        assert validate_routing_rules([{"task": "t", "action": "policy",
                                        "policy": "nonsense"}]) is not None
        assert validate_routing_rules([{"task": "t", "action": "policy"}]) is not None

    def test_min_score_must_be_numeric(self):
        assert validate_routing_rules([_rule(min_score=0.5)]) is None
        assert validate_routing_rules([_rule(min_score="0.5")]) is None
        assert validate_routing_rules([_rule(min_score="high")]) is not None
        assert validate_routing_rules([_rule(min_score=None)]) is None

    def test_empty_list_is_valid(self):
        assert validate_routing_rules([]) is None

    def test_the_live_l2_ruleset_passes(self):
        """The operator's real 8-intent l2 rules must still be accepted."""
        rules = [
            {"task": t, "action": "prefer", "provider": "*",
             "model": "deepseek-v4.1-flash", "profile": "l2", "enabled": True}
            for t in ("agentic_multi_step", "casual_chat", "code_generation",
                      "debugging", "planning", "reasoning_chain", "research_deep",
                      "unit_tests")
        ]
        assert validate_routing_rules(rules) is None
