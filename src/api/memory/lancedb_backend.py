"""LanceDB-backed semantic memory backend.

Each profile gets its own LanceDB table (table name = profile) inside a shared
database directory. The backend is **embedder-injectable**: it accepts an
``embed(text) -> list[list[float]]`` callable, so tests and the HTTP layer can
run without a real embedding model installed.

Storage is a columnar Lance table with a fixed schema::

    id (string), content (string), metadata (json), tags (list<string>),
    vector (fixed-size list<float>)

ANN search uses a brute-force vector search by default and creates an IVF_PQ
index once the table grows past ``index_threshold`` rows.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Callable, Optional

from ..logging_config import get_logger
from .base import MemoryBackend, MemoryError

logger = get_logger("lcp.memory.lancedb")

EmbedFn = Callable[[list[str]], list[list[float]]]

_SCHEMA_NAMES = ("id", "content", "metadata", "tags", "vector", "created_at")


class LanceDBMemoryBackend(MemoryBackend):
    """Per-profile semantic memory backed by embedded LanceDB.

    Parameters
    ----------
    db_path : str
        Directory containing the LanceDB database (one sub-dir per table).
    embed : EmbedFn
        Callable ``embed(texts: list[str]) -> list[list[float]]``.
    dim : int
        Expected embedding dimension (default 384 for bge-small).
    index_threshold : int
        Rows above which an ANN index is (re)built on recall.
    """

    def __init__(self, db_path: str, embed: EmbedFn, dim: int = 384,
                 index_threshold: int = 10_000):
        # The path feeds os.makedirs below — a non-string (e.g. a MagicMock)
        # would silently create a directory tree named after the mock instead
        # of raising. Fail loudly so callers never create junk storage dirs.
        if not isinstance(db_path, str):
            raise MemoryError(
                f"memory storage path must be a string, got {type(db_path).__name__}"
            )
        try:
            import importlib.util
            if importlib.util.find_spec("lancedb") is None:
                raise ImportError("no lancedb")
        except (ImportError, AttributeError) as exc:
            raise MemoryError(
                "lancedb is not installed — rebuild the image with "
                "WITH_MEMORY=1 (the runtime module installer is gone)"
            ) from exc
        self._db_path = db_path
        self._embed = embed
        self._dim = dim
        self._index_threshold = index_threshold
        self._db = None
        try:
            os.makedirs(db_path, exist_ok=True)
        except OSError as exc:
            raise MemoryError(f"cannot create memory storage: {exc}") from exc
        self._connect()

    # ── internal helpers ──────────────────────────────────────────────────

    def _connect(self):
        try:
            from lancedb import connect as _connect_db
            self._db = _connect_db(self._db_path)
        except Exception as exc:  # noqa: BLE001
            raise MemoryError(f"failed to open LanceDB at {self._db_path}: {exc}") from exc

    def _table(self, profile: str, create: bool = True):
        """Return the LanceDB table for ``profile`` (created on demand).

        An empty table can't be inferred from data, so we create it with an
        explicit pyarrow schema (``mode="overwrite"`` is only used when the
        schema actually differs; adding rows to an existing table reuses it).
        """
        try:
            if create:
                if profile in self._table_names():
                    return self._db.open_table(profile)
                import pyarrow as pa
                schema = pa.schema([
                    pa.field("id", pa.string()),
                    pa.field("content", pa.string()),
                    pa.field("metadata", pa.string()),
                    pa.field("tags", pa.list_(pa.string())),
                    pa.field("vector", pa.list_(pa.float32(), self._dim)),
                    pa.field("created_at", pa.float64()),
                ])
                return self._db.create_table(profile, schema=schema, mode="overwrite")
            return self._db.open_table(profile)
        except Exception as exc:  # noqa: BLE001
            raise MemoryError(f"failed to access memory table '{profile}': {exc}") from exc

    def _table_names(self) -> list[str]:
        try:
            names = self._db.table_names()
            if isinstance(names, list) and names and not isinstance(names[0], str):
                # Older API returned table objects; normalize to names.
                names = [getattr(t, "name", str(t)) for t in names]
            return list(names) if names else []
        except Exception:
            return []

    @staticmethod
    def _sanitize(content: str, metadata: Optional[dict], tags: Optional[list[str]]) -> tuple[str, dict, list[str]]:
        content = (content or "").strip()
        if not content:
            raise MemoryError("memory content must be non-empty")
        meta = dict(metadata or {})
        tag_list = [str(t).strip() for t in (tags or []) if str(t).strip()]
        return content, meta, tag_list

    # ── MemoryBackend contract ────────────────────────────────────────────

    def retain(self, content: str, metadata: Optional[dict] = None,
               tags: Optional[list[str]] = None, profile: str = "default") -> str:
        """Store a fact in ``profile``'s table. Returns the new ``memory_id``."""
        content, meta, tag_list = self._sanitize(content, metadata, tags)
        try:
            vector = self._embed([content])[0]
        except MemoryError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MemoryError(f"embedding failed: {exc}") from exc

        memory_id = uuid.uuid4().hex[:12]
        row = {
            "id": memory_id,
            "content": content,
            "metadata": json.dumps(meta, ensure_ascii=False, default=str),
            "tags": tag_list,
            "vector": list(vector),
            "created_at": time.time(),
        }
        try:
            table = self._table(profile, create=True)
            table.add([row])
        except Exception as exc:  # noqa: BLE001
            raise MemoryError(f"failed to retain memory: {exc}") from exc
        logger.debug("memory_retained", profile=profile, memory_id=memory_id)
        return memory_id

    def recall(self, query_text: str, top_k: int = 10,
               tag_filter: Optional[list[str]] = None,
               profile: str = "default") -> list[dict]:
        """Semantic search over ``profile``'s table.

        Returns ``[{id, content, metadata, tags, score}]`` sorted best-first.
        """
        if top_k <= 0:
            return []
        if profile not in self._table_names():
            return []
        try:
            vector = self._embed([query_text])[0]
        except MemoryError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MemoryError(f"embedding failed: {exc}") from exc

        try:
            table = self._table(profile, create=False)
            self._ensure_index(table)
            if tag_filter:
                tag_list = [str(t).strip() for t in tag_filter if str(t).strip()]
                if tag_list:
                    # Literal array literal is inlined (LanceDB 0.37 does not
                    # support parameterised WHERE on vector search).
                    literal = "[" + ", ".join(f"'{t}'" for t in tag_list) + "]"
                    results = (
                        table.search(vector)
                        .where(f"array_has_all(tags, {literal})")
                        .limit(top_k)
                        .to_list()
                    )
                else:
                    results = table.search(vector).limit(top_k).to_list()
            else:
                results = table.search(vector).limit(top_k).to_list()
        except MemoryError:
            raise
        except Exception as exc:  # noqa: BLE001
            # A malformed WHERE (e.g. unsupported filter) degrades to an
            # unfiltered search rather than failing the whole recall.
            logger.warning("memory_recall_filter_failed", error=str(exc), profile=profile)
            try:
                results = table.search(vector).limit(top_k).to_list()
            except Exception as exc2:  # noqa: BLE001
                raise MemoryError(f"memory recall failed: {exc2}") from exc2

        out: list[dict] = []
        for r in results:
            # LanceDB returns `_distance` (L2, lower = closer). Convert to a
            # 0..1 similarity so higher always means more relevant.
            dist = float(r.get("_distance", r.get("score", 1.0)))
            similarity = 1.0 / (1.0 + max(dist, 0.0))
            out.append({
                "id": r.get("id"),
                "content": r.get("content", ""),
                "metadata": self._decode_meta(r.get("metadata")),
                "tags": list(r.get("tags") or []),
                "score": round(similarity, 4),
            })
        return out

    def forget(self, memory_id: str, profile: str = "default") -> bool:
        """Delete ``memory_id`` from ``profile``'s table. True when removed."""
        if not memory_id or profile not in self._table_names():
            return False
        try:
            table = self._table(profile, create=False)
            before = table.count_rows()
            table.delete(f"id = '{memory_id}'")
            return table.count_rows() < before
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory_forget_failed", error=str(exc), profile=profile)
            return False

    def count(self, profile: str = "default") -> int:
        """Total stored facts in ``profile``'s table (0 when absent)."""
        if profile not in self._table_names():
            return 0
        try:
            return self._table(profile, create=False).count_rows()
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory_count_failed", error=str(exc), profile=profile)
            return 0

    def list_tables(self) -> list[str]:
        """Return the names of all memory tables (profiles with memories)."""
        return sorted(self._table_names())

    @staticmethod
    def _decode_meta(raw) -> dict:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (ValueError, TypeError):
                return {}
        return {}

    def _ensure_index(self, table) -> None:
        """Build an IVF_PQ index once the table is large enough.

        Uses the default metric (L2). Best-effort — index build failures are
        logged and searches fall back to the flat scan.
        """
        if self._index_threshold <= 0:
            return
        try:
            if table.count_rows() < self._index_threshold:
                return
            table.create_index(
                metric="L2",
                num_partitions=16,
                num_sub_vectors=48,
                replace=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory_index_build_failed", error=str(exc))
