"""Embedding model wrapper for the memory plugin and the semantic router.

Two backends, one interface:

* **fastembed (default)** — ONNX INT8 of the same ``BAAI/bge-small-en-v1.5``
  graph. No torch. Measured against the torch path on 1,000 real prompts routed
  through the real ``SemanticClassifier``: 999/1000 identical router outcome,
  cosine delta median 3e-4 / max 7e-4, p50 74.5 → 54.7 ms, RSS 997 → 306 MB,
  centroid build 8.42 → 0.88 s. Output stays 384-dim and L2-normalised, so
  stored vectors need no migration.
* **sentence-transformers** — the original path, kept for model names fastembed
  does not ship. fastembed carries a curated model list, so an arbitrary HF
  checkpoint still needs this branch.

The import is lazy: importing this module must never pull in either stack.
Embedding failures degrade to a clear :class:`MemoryError` instead of crashing
the gateway.
"""

from __future__ import annotations

import os
from typing import Optional

from ..logging_config import get_logger
from .base import MemoryError

logger = get_logger("lcp.memory.embeddings")

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DIM = 384

# Backends, in preference order.
BACKEND_FASTEMBED = "fastembed"
BACKEND_TORCH = "sentence-transformers"


def _fastembed_supports(model_name: str) -> bool:
    """Whether fastembed ships this checkpoint.

    fastembed keys bge models by their HF name (``BAAI/bge-small-en-v1.5``), so
    this is mostly a pass-through — but it must answer False for anything
    outside its curated list, so the caller falls back to torch instead of
    raising deep inside the ONNX load.
    """
    try:
        from fastembed import TextEmbedding
    except ImportError:
        return False
    try:
        return model_name in {m["model"] for m in TextEmbedding.list_supported_models()}
    except Exception:  # noqa: BLE001 — a broken model list must not block startup
        return False


class EmbeddingModel:
    """Lazy wrapper over an embedding backend.

    ``embed`` returns a list of vectors (one per input text), each a list of
    floats of length ``dim``. Embedding is deterministic enough for storage but
    is NOT expected to be exact — ANN recall tolerates small drift.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL,
                 device: str = "cpu", cache_dir: Optional[str] = None,
                 backend: str = ""):
        self.model_name = model_name
        self.device = device
        self.cache_dir = cache_dir
        # "" = auto: fastembed when it ships the model, else torch. Set
        # LCP_EMBED_BACKEND=sentence-transformers to force the old path.
        self.requested_backend = backend or os.environ.get("LCP_EMBED_BACKEND", "")
        self._model = None
        self._resolved_backend = ""

    # ── backend selection ───────────────────────────────────────────────────

    def _pick_backend(self) -> str:
        want = self.requested_backend
        if want == BACKEND_TORCH:
            return BACKEND_TORCH
        if _fastembed_supports(self.model_name):
            return BACKEND_FASTEMBED
        if want == BACKEND_FASTEMBED:
            logger.warning("embed_backend_unavailable",
                           backend=BACKEND_FASTEMBED, model=self.model_name)
        return BACKEND_TORCH

    # ── loading ─────────────────────────────────────────────────────────────

    def _load(self):
        if self._model is not None:
            return self._model
        if self._pick_backend() == BACKEND_FASTEMBED:
            try:
                self._model = self._load_fastembed()
                self._resolved_backend = BACKEND_FASTEMBED
                return self._model
            except Exception as exc:  # noqa: BLE001 — fall through to torch
                logger.warning("fastembed_load_failed_falling_back",
                               error=str(exc), model=self.model_name)
        self._model = self._load_torch()
        self._resolved_backend = BACKEND_TORCH
        return self._model

    def _load_fastembed(self):
        from fastembed import TextEmbedding

        # A bare EmbeddingModel() has no cache_dir, but the image bakes the
        # model at /app/models/embedding. Without this the container goes to
        # the network on first use and, offline, falls through to a torch
        # fallback that no longer exists in the image.
        cache_dir = self.cache_dir
        if not cache_dir:
            try:
                from . import memory_models
                cache_dir = memory_models()
            except Exception:  # noqa: BLE001 — no baked cache is not fatal
                cache_dir = ""
        kwargs: dict = {}
        if cache_dir:
            # fastembed keeps its own layout under the shared models dir.
            kwargs["cache_dir"] = os.path.join(cache_dir, "fastembed")
        # Leave a core for the gateway's own work, and cap it: ONNX Runtime
        # sets thread affinity per worker, and on a container whose cpuset is
        # narrower than the reported cpu_count that logs an affinity error for
        # every thread it cannot pin.
        threads = max(1, min((os.cpu_count() or 4) - 1, 8))
        try:
            model = TextEmbedding(model_name=self.model_name,
                                  threads=threads, **kwargs)
        except TypeError:
            # Older fastembed has no ``threads`` kwarg.
            model = TextEmbedding(model_name=self.model_name, **kwargs)
        logger.info("embed_backend_ready", backend=BACKEND_FASTEMBED,
                    model=self.model_name)
        return model

    def _load_torch(self):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            # Surface the UNDERLYING failure (usually torch missing from a
            # shadowing --target copy) instead of a misleading 'not installed'.
            raise MemoryError(
                "sentence-transformers import failed: "
                f"{exc} — install the semantic routing module"
            ) from exc
        kwargs: dict = {"device": self.device}
        if self.cache_dir:
            kwargs["cache_folder"] = self.cache_dir
        try:
            model = SentenceTransformer(self.model_name, **kwargs)
        except Exception:
            # The weights may already be cached while the HuggingFace Hub is
            # unreachable (e.g. an offline container). Retry offline so the
            # cached snapshot is used instead of failing on the hub check.
            if not (self.cache_dir and os.path.isdir(self.cache_dir)):
                raise
            logger.info("embedding_retry_local_files_only", model=self.model_name,
                        cache_dir=self.cache_dir)
            kwargs["local_files_only"] = True
            model = SentenceTransformer(self.model_name, **kwargs)
        logger.info("embed_backend_ready", backend=BACKEND_TORCH,
                    model=self.model_name)
        return model

    # ── interface ───────────────────────────────────────────────────────────

    @property
    def backend(self) -> str:
        """The backend actually in use (or the one that would be chosen)."""
        return self._resolved_backend or self._pick_backend()

    @property
    def dim(self) -> int:
        """Embedding dimension (default 384 for bge-small; resolved lazily)."""
        try:
            model = self._load()
        except Exception:  # noqa: BLE001 — fall back to the default dim
            return DEFAULT_DIM
        if self._resolved_backend == BACKEND_FASTEMBED:
            return DEFAULT_DIM
        return model.get_sentence_embedding_dimension()

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts. Returns one vector per input text, in order.

        Both backends normalise to unit length, which is what the cosine
        comparisons downstream assume — that is what makes the two
        interchangeable rather than merely similar.
        """
        if not texts:
            return []
        try:
            model = self._load()
            if self._resolved_backend == BACKEND_FASTEMBED:
                return [[float(x) for x in v] for v in model.embed(list(texts))]
            return [v.tolist() for v in model.encode(
                texts, normalize_embeddings=True, convert_to_numpy=True)]
        except MemoryError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory_embed_failed", error=str(exc))
            raise MemoryError(f"embedding failed: {exc}") from exc


def embedder_from_config(cfg: dict) -> EmbeddingModel:
    """Build an :class:`EmbeddingModel` from a ``plugins.memory`` config block."""
    emb = (cfg or {}).get("embedding") or {}
    # Use the SAME baked model cache the semantic router uses when present,
    # else the persistent modules dir.
    from . import memory_models
    cache_dir = memory_models()
    return EmbeddingModel(
        model_name=emb.get("model", DEFAULT_MODEL),
        device=emb.get("device", "cpu"),
        cache_dir=cache_dir,
        backend=emb.get("backend", ""),
    )
