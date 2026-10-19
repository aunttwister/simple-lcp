"""Backend selection for the embedder: fastembed (ONNX) vs torch.

The swap is only safe because the two backends produce interchangeable output
for bge-small — same 384 dims, both L2-normalised. These tests pin the
selection rules and the fallback, not the model quality (that was measured on
real traffic: 999/1000 identical router outcome).
"""

import sys
import types
from unittest.mock import patch

import pytest

from src.api.memory import embeddings as emb


def _fake_fastembed(supported=("BAAI/bge-small-en-v1.5",), fail_load=None,
                    vectors=None):
    """A stand-in fastembed module with the two attributes the wrapper touches."""
    mod = types.ModuleType("fastembed")

    class TextEmbedding:
        def __init__(self, model_name, threads=None, cache_dir=None):
            if fail_load:
                raise fail_load
            self.model_name = model_name
            self.cache_dir = cache_dir

        @staticmethod
        def list_supported_models():
            return [{"model": m} for m in supported]

        def embed(self, texts):
            vecs = vectors or [[1.0] + [0.0] * 383]
            return iter([vecs[0] for _ in texts])

    mod.TextEmbedding = TextEmbedding
    return mod


# ── selection ───────────────────────────────────────────────────────────────

def test_auto_picks_fastembed_when_the_model_is_shipped():
    with patch.dict(sys.modules, {"fastembed": _fake_fastembed()}):
        assert emb.EmbeddingModel()._pick_backend() == emb.BACKEND_FASTEMBED


def test_auto_falls_back_when_fastembed_does_not_ship_the_model():
    """fastembed's list is curated; an arbitrary HF checkpoint is not on it."""
    mod = _fake_fastembed(supported=("BAAI/bge-small-en-v1.5",))
    with patch.dict(sys.modules, {"fastembed": mod}):
        m = emb.EmbeddingModel(model_name="some-org/custom-model")
        assert m._pick_backend() == emb.BACKEND_TORCH


def test_auto_falls_back_when_fastembed_is_not_installed():
    with patch.dict(sys.modules, {"fastembed": None}):
        assert emb.EmbeddingModel()._pick_backend() == emb.BACKEND_TORCH


def test_forcing_torch_wins_over_a_shipped_model():
    with patch.dict(sys.modules, {"fastembed": _fake_fastembed()}):
        m = emb.EmbeddingModel(backend=emb.BACKEND_TORCH)
        assert m._pick_backend() == emb.BACKEND_TORCH


def test_env_var_can_force_the_torch_backend(monkeypatch):
    monkeypatch.setenv("LCP_EMBED_BACKEND", emb.BACKEND_TORCH)
    with patch.dict(sys.modules, {"fastembed": _fake_fastembed()}):
        assert emb.EmbeddingModel()._pick_backend() == emb.BACKEND_TORCH


# ── loading ─────────────────────────────────────────────────────────────────

def test_embed_uses_fastembed_and_returns_plain_floats():
    with patch.dict(sys.modules, {"fastembed": _fake_fastembed()}):
        m = emb.EmbeddingModel()
        out = m.embed(["a", "b"])
    assert len(out) == 2
    assert all(isinstance(x, float) for x in out[0])
    assert len(out[0]) == 384
    assert m.backend == emb.BACKEND_FASTEMBED


def test_dim_is_reported_without_loading_for_fastembed():
    """dim must not need a loaded model on the ONNX path — it is a constant."""
    with patch.dict(sys.modules, {"fastembed": _fake_fastembed()}):
        m = emb.EmbeddingModel()
        assert m.dim == emb.DEFAULT_DIM


def test_fastembed_load_failure_falls_back_to_torch():
    """A broken ONNX runtime must degrade to torch, not to a dead embedder."""
    mod = _fake_fastembed(fail_load=RuntimeError("onnx boom"))
    st = types.ModuleType("sentence_transformers")

    class FakeST:
        def __init__(self, name, **kwargs):
            pass

        def get_sentence_embedding_dimension(self):
            return 384

        def encode(self, texts, **kwargs):
            # Real encode() returns numpy rows; the wrapper calls .tolist().
            class _Row(list):
                def tolist(self):
                    return list(self)
            return [_Row([1.0] + [0.0] * 383) for _ in texts]

    st.SentenceTransformer = FakeST
    with patch.dict(sys.modules, {"fastembed": mod,
                                  "sentence_transformers": st}):
        m = emb.EmbeddingModel()
        out = m.embed(["x"])
    assert m.backend == emb.BACKEND_TORCH
    assert len(out[0]) == 384


def test_empty_input_embeds_nothing():
    with patch.dict(sys.modules, {"fastembed": _fake_fastembed()}):
        assert emb.EmbeddingModel().embed([]) == []


def test_a_raw_failure_is_wrapped_as_MemoryError():
    """Callers catch MemoryError; a backend blowing up must not escape as-is."""
    mod = _fake_fastembed()
    with patch.dict(sys.modules, {"fastembed": mod}):
        m = emb.EmbeddingModel()
        m._load()
        m._model.embed = lambda texts: (_ for _ in ()).throw(ValueError("bad"))
        with pytest.raises(emb.MemoryError):
            m.embed(["x"])


def test_embedder_from_config_reads_the_backend_key():
    cfg = {"embedding": {"model": emb.DEFAULT_MODEL,
                         "backend": emb.BACKEND_TORCH}}
    with patch.object(emb, "embedder_from_config", emb.embedder_from_config):
        m = emb.embedder_from_config(cfg)
    assert m.requested_backend == emb.BACKEND_TORCH


# ── baked-cache resolution ──────────────────────────────────────────────────

def test_fastembed_uses_the_baked_cache_when_no_cache_dir_was_given(tmp_path):
    """A bare EmbeddingModel() must still find the baked model.

    Regression: cache_dir defaulted to None, so the container ignored
    /app/models/embedding, tried the network on first use, and offline fell
    through to a torch fallback that the image no longer contains.
    """
    seen = {}
    mod = _fake_fastembed()

    class Recording(mod.TextEmbedding):
        def __init__(self, model_name, threads=None, cache_dir=None):
            seen["cache_dir"] = cache_dir
            super().__init__(model_name, threads=threads, cache_dir=cache_dir)

    mod.TextEmbedding = Recording
    with patch.dict(sys.modules, {"fastembed": mod}), \
         patch.object(emb, "memory_models", lambda: str(tmp_path), create=True):
        # memory_models is imported inside the function; patch the source module.
        with patch("src.api.memory.memory_models", lambda: str(tmp_path)):
            emb.EmbeddingModel()._load()
    assert seen["cache_dir"] == str(tmp_path / "fastembed")


def test_an_explicit_cache_dir_wins_over_the_baked_one(tmp_path):
    seen = {}
    mod = _fake_fastembed()

    class Recording(mod.TextEmbedding):
        def __init__(self, model_name, threads=None, cache_dir=None):
            seen["cache_dir"] = cache_dir
            super().__init__(model_name, threads=threads, cache_dir=cache_dir)

    mod.TextEmbedding = Recording
    explicit = tmp_path / "explicit"
    with patch.dict(sys.modules, {"fastembed": mod}):
        emb.EmbeddingModel(cache_dir=str(explicit))._load()
    assert seen["cache_dir"] == str(explicit / "fastembed")
