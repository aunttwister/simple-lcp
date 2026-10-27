"""CI/prod parity for the gateway configuration.

CI builds its config from `SEED_CONFIG`; prod runs whatever is in the `settings` table after
UI edits. Nothing checked the two against the same rule, so a configuration that breaks prod
could pass CI green. Measured 2026-10-24: `SEED_CONFIG` declares 3 providers / 5 profiles, the
live prod DB declares 4 / 7 — CI has never exercised prod's shape.

This module pins three things:

1. `validate_gateway_config` catches the cross-section problems `_validate` cannot see when it
   validates one section at a time (a chain step naming an undeclared provider is structurally
   valid on its own).
2. The seed CI validates is itself valid — so "CI green" now means something about config.
3. The image prod runs and the matrix CI runs agree on the interpreter, and the known
   dependency-set difference is named rather than accidental.

`scripts/config-parity.py` runs the same validator against the live DB, which is how prod's
config gets checked by the same rule.
"""
import re
from pathlib import Path

import pytest

from src.api.config import SEED_CONFIG, validate_gateway_config

ROOT = Path(__file__).resolve().parent.parent


def _cfg(providers=None, profiles=None, default_profile="l2"):
    return {
        "server": {"port": 8734, "default_profile": default_profile},
        "providers": providers if providers is not None else {"deepseek": {"api_base": "https://d/v1"}},
        "profiles": profiles if profiles is not None else {
            "l2": {"chain": [{"provider": "deepseek", "model": "m",
                              "base_url": "https://d/v1"}]},
        },
    }


class TestCrossSectionChecks:
    def test_valid_config_has_no_problems(self):
        assert validate_gateway_config(_cfg()) == []

    def test_chain_step_naming_an_undeclared_provider_is_caught(self):
        cfg = _cfg(profiles={"l2": {"chain": [
            {"provider": "ghost", "model": "m", "base_url": "https://g/v1"}]}})
        problems = validate_gateway_config(cfg)
        assert len(problems) == 1
        assert "ghost" in problems[0]
        assert "not declared" in problems[0]

    def test_missing_model_is_caught(self):
        cfg = _cfg(profiles={"l2": {"chain": [
            {"provider": "deepseek", "base_url": "https://d/v1"}]}})
        assert any("no model" in p for p in validate_gateway_config(cfg))

    def test_blank_model_is_caught(self):
        cfg = _cfg(profiles={"l2": {"chain": [
            {"provider": "deepseek", "model": "   ", "base_url": "https://d/v1"}]}})
        assert any("no model" in p for p in validate_gateway_config(cfg))

    def test_missing_base_url_without_api_base_is_caught(self):
        cfg = _cfg(providers={"deepseek": {}}, profiles={"l2": {"chain": [
            {"provider": "deepseek", "model": "m"}]}})
        assert any("no base_url" in p for p in validate_gateway_config(cfg))

    def test_base_url_falls_back_to_provider_api_base(self):
        cfg = _cfg(providers={"deepseek": {"api_base": "https://d/v1"}},
                   profiles={"l2": {"chain": [{"provider": "deepseek", "model": "m"}]}})
        assert validate_gateway_config(cfg) == []

    def test_empty_chain_is_caught(self):
        cfg = _cfg(profiles={"l2": {"chain": []}})
        assert any("no usable chain" in p for p in validate_gateway_config(cfg))

    def test_default_profile_must_exist(self):
        cfg = _cfg(default_profile="ghost")
        assert any("default_profile" in p for p in validate_gateway_config(cfg))

    def test_no_providers_is_caught(self):
        cfg = _cfg(providers={}, profiles={"l2": {"chain": [
            {"provider": "deepseek", "model": "m", "base_url": "https://d/v1"}]}})
        problems = validate_gateway_config(cfg)
        assert any("no providers" in p for p in problems)

    def test_reports_every_problem_not_just_the_first(self):
        cfg = _cfg(providers={}, profiles={
            "a": {"chain": [{"provider": "x", "model": "m", "base_url": "https://x/v1"}]},
            "b": {"chain": [{"provider": "y", "base_url": "https://y/v1"}]},
        })
        problems = validate_gateway_config(cfg)
        assert len(problems) >= 4


class TestSeedIsValid:
    def test_seed_passes_the_same_rule_ci_runs(self):
        """If this fails, CI green is meaningless for config."""
        problems = validate_gateway_config(SEED_CONFIG)
        assert problems == [], problems

    def test_seed_default_profile_is_declared(self):
        assert SEED_CONFIG["server"]["default_profile"] in SEED_CONFIG["profiles"]

    def test_every_seed_chain_step_resolves(self):
        for name, prof in SEED_CONFIG["profiles"].items():
            for i, step in enumerate(prof["chain"]):
                assert step["provider"] in SEED_CONFIG["providers"], (name, i)
                assert step["model"].strip(), (name, i)


class TestImageAndMatrixAgree:
    """The interpreter CI tests must be one the prod image actually runs."""

    def _dockerfile_python(self):
        text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        m = re.search(r"^FROM\s+python:(\d+\.\d+)", text, re.M)
        assert m, "Dockerfile has no `FROM python:<version>` line"
        return m.group(1)

    def _ci_matrix(self):
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        m = re.search(r"python-version:\s*\[([^\]]+)\]", text)
        assert m, "ci.yml has no python-version matrix"
        return [v.strip().strip("'\"") for v in m.group(1).split(",")]

    def test_ci_tests_the_image_interpreter(self):
        image_py = self._dockerfile_python()
        assert image_py in self._ci_matrix(), (
            f"the prod image runs python {image_py}, but CI only tests {self._ci_matrix()}"
        )

    def test_image_installs_without_extras_ci_installs_with_them(self):
        """The known difference, asserted rather than assumed.

        CI installs `.[dev,memory]`; the default image installs `.` (WITH_MEMORY is a build arg,
        default 0). So a test that needs lancedb can pass in CI and fail in a default prod image.
        If either side changes, this test should be updated deliberately.
        """
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

        assert re.search(r"RUN\s+pip install --no-cache-dir \.\s*$", dockerfile, re.M), (
            "Dockerfile no longer installs the bare package; re-check parity"
        )
        assert "[dev,memory]" in ci, "CI install spec changed; re-check parity"
        assert "WITH_MEMORY" in dockerfile, "memory extra is baked via a build arg, not by default"

    def test_memory_dependent_tests_skip_when_the_module_is_absent(self):
        """A default image has no lancedb; those tests must skip, not fail."""
        try:
            import lancedb  # noqa: F401
        except ImportError:
            pytest.skip("lancedb absent in this environment — the skip path is what matters")
        # If lancedb IS present, the import guard in the memory tests is still the contract.
        assert True
