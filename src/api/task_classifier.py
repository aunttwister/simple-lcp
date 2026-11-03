"""Semantic task classification for the dynamic router.

The router's ``classify_task`` is keyword/heuristic. This module adds an
optional **embedding-based** classifier that uses its OWN embedder
(bge-small-en-v1.5, 384-dim) so a prompt is classified by its *meaning* rather
than exact keywords.

This is the SEMANTIC ROUTING module — it is independent of the memory plugin.
Its embedder comes from the ``plugins.router`` config block (installed as the
``router`` module, grouped with LiveBench), not ``plugins.memory``.

Design
------
* Each task type has a small set of **exemplar prompts** (canonical examples).
  When the embedder is available, the exemplars are embedded once (cached) and
  averaged into a per-task centroid. A prompt is embedded and classified to the
  nearest centroid by cosine similarity (threshold-gated).
* When no embedder is available (sentence-transformers not installed), the
  module is a no-op and the caller falls back to the existing keyword path.
* It returns the SAME task strings as the keyword classifier, so the rest of
  the router is unchanged: ``agentic_multi_step``, ``unit_tests``,
  ``code_generation``, ``debugging``, ``research_deep``, ``reasoning_chain``,
  ``planning``, ``casual_chat``.
"""

from __future__ import annotations

import math
from typing import Optional

from .logging_config import get_logger
from .memory.embeddings import DEFAULT_MODEL

logger = get_logger("lcp.router.classifier")

# Task type -> canonical exemplar prompts (short, representative).
TASK_EXEMPLARS: dict[str, list[str]] = {
    "agentic_multi_step": [
        "You are an AI agent. Use the available tools to complete this multi-step task.",
        "Use your tools to browse the web and gather information, then act on it.",
        "Call the functions provided to fulfill this request step by step.",
        "You have access to tools — delegate and execute the plan autonomously.",
    ],
    "unit_tests": [
        "Write a pytest suite covering this function's edge cases.",
        "Add unit tests for the new module, including mocking the database.",
        "Create test cases for the API endpoint and assert the responses.",
        "Write a test for this bug to prevent regression.",
        "Run the test suite and report which tests fail.",
    ],
    "code_generation": [
        "Write a Python function that parses this CSV file.",
        "Implement a REST endpoint in FastAPI that returns JSON.",
        "Create a bash script to deploy this service.",
        "Write a React component that renders a list of items.",
        "Review this existing file and make the changes we discussed.",
        "Refactor this function to be cleaner and update the callers.",
        "Look at this code and fix the issue in this file.",
        # "algorithm" in isolation reads like reasoning; pin the implement-a-
        # function/algorithm-in-code sense to code_generation, not reasoning.
        "Implement a sorting algorithm in Python.",
        "Write a function that implements this algorithm.",
        "Implement the binary search algorithm in code.",
        # Plan-mode → implementation-mode transition. The user approved the
        # plan and is now telling the agent to WRITE the code — this is
        # code generation, not planning. These short imperatives exist so
        # "start implementation" / "go ahead and implement it" stop landing on
        # the planning centroid (which must NOT contain implement/build verbs).
        "Start the implementation now.",
        "Go ahead and implement it.",
        "Implement this change.",
        "Let's implement this feature.",
        "Proceed with writing the code.",
    ],
    "debugging": [
        "Why does this code throw a KeyError? Here is the traceback.",
        "Debug this failing test — it works locally but not in CI.",
        "This endpoint returns 500. Help me find the bug.",
        "The build is failing with an import error. What's wrong?",
    ],
    "research_deep": [
        "Explain how attention mechanisms work in transformers, in detail.",
        "Analyze the trade-offs between SQL and NoSQL for this workload.",
        "Compare and contrast microservices and monoliths thoroughly.",
        "Research the latest approaches to vector databases and summarize.",
    ],
    "reasoning_chain": [
        "Solve this logic puzzle step by step.",
        "Prove that this algorithm has O(n log n) complexity.",
        "Calculate the time complexity of this recurrence relation.",
        "Work through this mathematical problem carefully.",
    ],
    "planning": [
        "Design the architecture for a multi-tenant SaaS product.",
        "How should I structure this microservice codebase?",
        "Create a roadmap and data model for this new feature.",
        "Plan the tech stack and schema for this application.",
        # Planning = produce a PLAN / design / roadmap. Deliberately NO
        # "implement"/"build"/"write the code" verbs: the plan-mode →
        # implementation-mode handoff ("start implementation", "go ahead and
        # implement it") must classify as code_generation, not stay pinned to
        # planning through the word "implement".
        "Create a step-by-step plan for this feature.",
        "Plan out the approach for this component.",
        "Let's plan the strategy for this change.",
    ],
    "casual_chat": [
        "hello",
        "hi, how are you?",
        "thanks!",
        "good morning",
        "what's up?",
    ],
}


def _normalize(text: str) -> str:
    return (text or "").strip().lower()


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors (0..1)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


class SemanticClassifier:
    """Embedding-based task classifier with a keyword-free fallback.

    Lazily embeds the task exemplars on first use. When no embedder is
    available, ``classify()`` returns None and the caller should use the
    keyword path.
    """

    # Cosine-similarity floor for a semantic match to be trusted. A low value
    # lets a generic coding message land on a broad centroid (e.g. unit_tests)
    # even when the user's intent is clearly something else. High-confidence
    # semantic matches only.
    DEFAULT_MIN_SCORE = 0.35

    def __init__(
        self,
        embed=None,
        min_score: float = DEFAULT_MIN_SCORE,
        exemplars: Optional[dict[str, list[str]]] = None,
        taxonomy_id: Optional[str] = None,
        taxonomy_version: Optional[int] = None,
    ):
        # The label space is an ARGUMENT, not a module constant. `exemplars=None` preserves the
        # shipped behaviour exactly (the module constant), so every existing caller is unaffected;
        # a taxonomy supplies its own set instead.
        self._embed = embed  # callable(texts: list[str]) -> list[list[float]]
        self._min_score = min_score
        self._exemplars: dict[str, list[str]] = (
            {k: list(v) for k, v in (exemplars or TASK_EXEMPLARS).items()}
        )
        self.taxonomy_id = taxonomy_id or "builtin-v0"
        self.taxonomy_version = taxonomy_version
        self._centroids: Optional[dict[str, list[float]]] = None
        self._task_order: list[str] = list(self._exemplars.keys())

    @property
    def labels(self) -> list[str]:
        """The label space this classifier decides in — visible, because a taxonomy that is not
        queryable is a taxonomy nobody can check."""
        return list(self._exemplars.keys())

    @property
    def available(self) -> bool:
        return self._embed is not None

    @property
    def min_score(self) -> float:
        """The effective cosine gate applied by ``classify()`` (read-only)."""
        return self._min_score

    def _build_centroids(self) -> None:
        if self._centroids is not None or not self.available:
            return
        centroids: dict[str, list[float]] = {}
        for task, exemplars in self._exemplars.items():
            if not exemplars:
                continue
            try:
                vectors = self._embed(exemplars)  # type: ignore[misc]
            except Exception:
                continue
            if not vectors:
                continue
            dim = len(vectors[0])
            if dim == 0:
                continue
            centroid = [0.0] * dim
            for v in vectors:
                for i in range(dim):
                    centroid[i] += v[i]
            centroid = [x / len(vectors) for x in centroid]
            # Normalize the centroid.
            norm = math.sqrt(sum(x * x for x in centroid)) or 1.0
            centroids[task] = [x / norm for x in centroid]
        self._centroids = centroids if centroids else {}

    def top_scores(self, text: str, k: int = 5) -> list[tuple[str, float]]:
        """Return the top-k ``(task, cosine)`` pairs for *text*, sorted desc.

        Returns ``[]`` when the embedder is unavailable or embedding fails.
        Does NOT apply the ``min_score`` gate — the caller decides, so the
        router can surface near-threshold scores for observability.
        """
        if not self.available or not text:
            return []
        self._build_centroids()
        if not self._centroids:
            return []
        try:
            vec = self._embed([text])[0]  # type: ignore[misc]
        except Exception:
            return []
        scored = sorted(
            ((task, _cosine(vec, centroid))
             for task, centroid in self._centroids.items()),
            key=lambda x: -x[1],
        )
        return scored[:k]

    def classify(self, text: str) -> Optional[str]:
        """Return the best task for *text*, or None when unavailable/uncertain.

        Uses cosine similarity to the nearest task centroid, gated by
        ``min_score`` so low-confidence prompts fall through to the keyword
        path.
        """
        if not self.available or not text:
            return None
        self._build_centroids()
        if not self._centroids:
            return None
        scores = self.top_scores(text, 1)
        if not scores:
            return None
        best_task, best_score = scores[0]
        if best_score < self._min_score:
            return None
        return best_task


# Cached classifiers, keyed on (taxonomy_id, taxonomy_version, embedder model). A single global --
# what this used to be -- is the one thing that cannot survive per-user taxonomies: the second
# profile to be classified would silently reuse the first profile's label space.
_classifiers: dict[tuple, SemanticClassifier] = {}
_classifier: Optional[SemanticClassifier] = None   # built-in slot, kept for back-compat readers
# The embedder weights are shared across taxonomies - only the label space differs, so reloading
# 130MB of weights per profile would be pure waste.
_embedder = None


def _cache_key(taxonomy) -> tuple:
    """The cache identity of a label space: ``(taxonomy_id, taxonomy_version)``.

    Pulled out as a function because this IS the per-user mechanism. A key that ignored the version
    would serve a stale label space after a taxonomy edit; a key that ignored the id is the single
    global cache this replaced, where the second profile silently got the first profile's labels.
    """
    return (getattr(taxonomy, "id", None) or "builtin-v0", getattr(taxonomy, "version", None))


def _probe_embed(embed) -> bool:
    """Return True when *embed* actually produces real (non-zero) vectors.

    Guards against the memory plugin's ``_noop_embed`` fallback (all-zero
    vectors) and against sentence-transformers being absent (raises) — both of
    which would produce garbage classification.
    """
    if embed is None:
        return False
    try:
        vec = embed(["semantic probe"])[0]
    except Exception as exc:  # noqa: BLE001 — surface WHY the probe failed
        logger.warning("semantic_probe_embed_failed", error=str(exc)[:300])
        return False
    if not vec:
        return False
    return any(abs(x) > 1e-9 for x in vec)


def get_semantic_classifier(taxonomy=None) -> Optional[SemanticClassifier]:
    """Return the classifier for *taxonomy*, or None when no real embedder is available.

    ``taxonomy=None`` means the built-in label space, i.e. exactly what this function returned
    before taxonomies existed. The result is cached on ``(taxonomy_id, taxonomy_version)``: the
    label space is what varies per profile, so one shared global would make the second profile
    silently reuse the first profile's labels.

    Builds an ``EmbeddingModel`` from ``plugins.router`` (the SEMANTIC ROUTING
    module — independent of the memory plugin) and probes it. When
    sentence-transformers isn't installed (or the embedder is the noop
    fallback), returns None so callers use the keyword path.

    This intentionally does NOT depend on ``plugins.memory``: semantic routing
    and the memory bank are separate installable modules. Both share the same
    embedder type, but the router module is what powers task classification.
    """
    global _classifier, _embedder
    tid, tver = _cache_key(taxonomy)
    exemplars = taxonomy.exemplars("act") if taxonomy is not None else None
    hit = _classifiers.get((tid, tver))
    if hit is not None and hit.available:
        return hit
    try:
        import sys

        from ..api.config import get_config
        from .memory.embeddings import EmbeddingModel
        from .memory import router_site, router_models

        config = get_config()
        router_cfg = (getattr(config, "plugins", None) or {}).get("router") or {}
        if not router_cfg.get("enabled", True):
            logger.info("semantic_classifier_disabled_by_config")
            return None
        # The router deps are baked into the image GLOBALLY (Dockerfile
        # WITH_ROUTER=1). For lean images (WITH_ROUTER=0) a runtime
        # `pip install --target <LCP_MODULES_DIR>/router` install is a fallback.
        # APPEND the site dir (do NOT prepend): a broken/partial --target copy
        # must never shadow the good global install, which is exactly the bug
        # that made the classifier report 'not installed' in the container.
        site = router_cfg.get("site") or router_site()
        if site and site not in sys.path:
            sys.path.append(site)
        # The router embedder caches weights in the router module's own models
        # dir so it is independent from the memory plugin's install.
        models_dir = router_cfg.get("models_dir") or router_models()
        model = EmbeddingModel(
            model_name=router_cfg.get("embedding", {}).get("model", DEFAULT_MODEL),
            device=router_cfg.get("embedding", {}).get("device", "cpu"),
            cache_dir=models_dir,
        )
        if not _probe_embed(model.embed):
            logger.warning(
                "semantic_classifier_unavailable",
                reason="embed probe returned no signal",
                models_dir=models_dir,
                site=site,
            )
            return None
        # Gate the semantic path on a HIGH-confidence match so the deterministic
        # keyword signals still win for clear intent; only genuinely ambiguous
        # prompts fall through to meaning-based classification. Configurable via
        # plugins.router.min_score.
        min_score = float(
            router_cfg.get("min_score", SemanticClassifier.DEFAULT_MIN_SCORE)
            or SemanticClassifier.DEFAULT_MIN_SCORE
        )
        _embedder = model.embed
        clf = SemanticClassifier(
            embed=model.embed, min_score=min_score, exemplars=exemplars,
            taxonomy_id=tid, taxonomy_version=tver,
        )
        _classifiers[(tid, tver)] = clf
        if tid == "builtin-v0":
            _classifier = clf
        logger.info("semantic_classifier_ready", taxonomy_id=tid, taxonomy_version=tver,
                    labels=len(clf.labels), min_score=min_score, models_dir=models_dir)
    except Exception as exc:  # noqa: BLE001 — classifier must never break routing
        logger.warning("semantic_classifier_unavailable", error=str(exc)[:300])
        clf = None
    return clf if (clf is not None and clf.available) else None


def invalidate_semantic_classifier() -> None:
    """Drop every cached classifier (e.g. after a taxonomy edit or a router module change).

    Clearing ALL keys is deliberate: invalidating one taxonomy while leaving another cached is how
    you get two profiles classifying in different label spaces with no trace of why.
    """
    global _classifier, _embedder
    _classifiers.clear()
    _classifier = None
    _embedder = None
