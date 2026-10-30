"""Phase 5b — the guard that stops model identity going back to ad-hoc string handling.

What this protects
------------------
Identity used to be decided in nine places by munging a display string: lowercasing
it, splitting on ``/``, stripping a suffix, comparing it. That is how the estate
ended up with 24 (provider, model) pairs of which 10 resolved to nothing, 19
capability keys of which 5 resolved to nothing, and one model carrying two cost
factors depending on its spelling.

``src/api/model_identity.py`` is now the only place allowed to do it. This test
fails when a NEW place starts.

How it stays honest
-------------------
The pre-phase-5 sites still exist and are listed in ``ALLOWED`` WITH A REASON. A
bare allowlist would just be a list of things nobody dares remove; each entry names
the measurement that blocks its removal (or says it is a delegation, not a
normaliser). When a reason no longer holds, deleting the entry makes the guard
catch the next regression.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
AUTHORITY = "src/api/model_identity.py"

#: Function names that mean "I decide identity from a string".
_NORMALISER_PATTERNS = (
    "normalize_model", "normalise_model",
    "_alias_to_logical", "_logical_model",
    "logical_model", "benchmark_model", "provider_model",
)

#: (file, function) -> why this one is still allowed to munge a model string.
ALLOWED: dict[tuple[str, str], str] = {
    # ── the legacy implementation, with the measurement that blocks its removal ──
    ("src/api/router.py", "normalize_model_id"):
        "the legacy normaliser itself (/models/ prefix, .gguf, lowercase)",
    ("src/api/router.py", "logical_model_name"):
        "legacy alias index; 172/172 capability lookups still fall back to it "
        "because the canonical model has no capability rows of its own",
    ("src/api/router.py", "_alias_to_logical"):
        "legacy index keyed on the provider NAME (renamed in this estate)",
    ("src/api/router.py", "benchmark_model_name"):
        "legacy: reads the old model_registry blob for the benchmark key",
    ("src/api/router.py", "provider_model_name"):
        "legacy: logical -> provider-side id from the blob; still builds chains",
    # ── reads a property off the string rather than deciding identity ────────────
    ("src/api/router.py", "detect_quantization"):
        "reads a quant tag OFF the model string; identity is not changed",
    ("src/api/config.py", "validate_gateway_config"):
        "non-empty validation of a config model field, not identity",
    # ── per-plugin catalog lookups: the plugin owns its own key space ────────────
    ("src/api/cost_plugins/commandcode.py", "_logical_model"):
        "last-path-segment match against the plugin's own catalog",
    ("src/api/cost_plugins/commandcode.py", "_api_model"):
        "the plugin's catalog is keyed by lowercase provider-side ids it owns",
    # ── capability rows: keyed on the model string in the capability table ───────
    ("src/api/seed_capabilities.py", "resolve_active_rows"):
        "case-insensitive capability-row -> release resolution",
    ("src/api/seed_capabilities.py", "effective_releases"):
        "case-insensitive release-label lookup for capability rows",
    # ── pricing rows: matched case-insensitively by design ──────────────────────
    ("src/api/config.py", "_find_pricing_row"):
        "prices match case-insensitively; candidates are registry-augmented",
}

#: String methods that mean "I am re-spelling a model name to look it up".
_MUNGE_METHODS = {"lower", "casefold", "upper", "strip", "lstrip", "rstrip",
                  "split", "rsplit", "replace", "removesuffix", "removeprefix",
                  "partition"}


def _model_ish(node: ast.AST) -> bool:
    """True when the expression is plainly derived from a model name."""
    return any(isinstance(n, ast.Name) and "model" in n.id.lower()
               for n in ast.walk(node))


def _walk_functions(path: Path):
    """Yield (function_name, node) for every def in a file (nested included)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    stack = [(None, tree)]
    while stack:
        encloser, node = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield child.name, child, encloser
                stack.append((child.name, child))
            else:
                stack.append((encloser, child))


def _source_files():
    for p in sorted(SRC.rglob("*.py")):
        rel = p.relative_to(REPO).as_posix()
        if rel == AUTHORITY:
            continue
        yield p, rel


def _find_normaliser_defs():
    out = []
    for path, rel in _source_files():
        for name, node, _ in _walk_functions(path):
            if any(pat in name.lower() for pat in _NORMALISER_PATTERNS):
                out.append((rel, name, node.lineno))
    return out


def _find_munging():
    """Model-string munging: a string method applied to a model-ish expression."""
    out = []
    for path, rel in _source_files():
        for fn, node, _ in _walk_functions(path):
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                f = sub.func
                if not isinstance(f, ast.Attribute) or f.attr not in _MUNGE_METHODS:
                    continue
                if not _model_ish(f.value):
                    continue
                # `.split(",")` on a model list is not identity handling; only
                # path/case re-spelling is. Keep the check narrow and honest.
                if f.attr in {"split", "rsplit"} and sub.args:
                    a = sub.args[0]
                    if isinstance(a, ast.Constant) and a.value != "/":
                        continue
                out.append((rel, fn, sub.lineno, f.attr))
    return out


def test_no_new_model_normaliser_outside_the_authority():
    offenders = [x for x in _find_normaliser_defs() if (x[0], x[1]) not in ALLOWED]
    assert not offenders, (
        "New model normaliser(s) outside model_identity.py. Identity must be "
        "decided in one place:\n"
        + "\n".join(f"  {rel}:{ln}  def {name}()" for rel, name, ln in offenders)
        + "\nIf this is deliberate, add it to ALLOWED with the measurement that "
          "justifies it."
    )


def test_no_new_model_string_munging_outside_the_authority():
    offenders = [x for x in _find_munging() if (x[0], x[1]) not in ALLOWED]
    assert not offenders, (
        "Model string re-spelled outside model_identity.py (lowercase / split('/') "
        "/ strip / replace). A spelling is not an identity:\n"
        + "\n".join(f"  {rel}:{ln}  in {fn}(): .{meth}()" for rel, fn, ln, meth in offenders)
        + "\nIf this is deliberate, add it to ALLOWED with the measurement that "
          "justifies it."
    )


def test_the_authority_is_exempt_and_actually_does_the_work():
    """The exemption must be real: model_identity.py has to contain the normaliser."""
    src = (REPO / AUTHORITY).read_text()
    assert "def normalise_model_string(" in src
    assert "def resolve(" in src
    assert "def cost_factor_for(" in src


def test_every_allowlist_entry_still_exists():
    """A stale allowlist entry would silently widen the hole.

    If a listed function has been deleted, its entry must go too — otherwise the
    guard no longer covers a name that could be reintroduced freely.
    """
    found = {(rel, name) for rel, name, _ in _find_normaliser_defs()}
    found |= {(rel, fn) for rel, fn, _, _ in _find_munging()}
    stale = sorted(k for k in ALLOWED if k not in found)
    assert not stale, (
        "ALLOWED entries whose function no longer exists — delete them so the "
        "guard covers the name again:\n"
        + "\n".join(f"  {rel}::{name}  ({ALLOWED[(rel, name)]})" for rel, name in stale)
    )


if __name__ == "__main__":  # pragma: no cover - manual inspection aid
    print("normaliser defs outside the authority:")
    for rel, name, ln in sorted(_find_normaliser_defs()):
        mark = "allowed" if (rel, name) in ALLOWED else "NEW!"
        print(f"  {mark:8} {rel}:{ln}  {name}()")
    print("\nmodel-string munging:")
    for rel, fn, ln, meth in sorted(_find_munging()):
        mark = "allowed" if (rel, fn) in ALLOWED else "NEW!"
        print(f"  {mark:8} {rel}:{ln}  {fn}(): .{meth}()")
    pytest.main([__file__, "-q"])
