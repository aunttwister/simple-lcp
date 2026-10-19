"""Memory plugin — backend contract.

LCP's memory layer exposes a unified, per-profile semantic memory bank to any
client that talks to the gateway. The backend is pluggable: the protocol below
is the only contract the HTTP layer depends on, which keeps tests independent
of the embedding model and the vector store.

The real backend (``LanceDBMemoryBackend``) is baked in at build time as
a module, exactly like LiveBench — it is NOT part of the core dependencies, so
the gateway stays lean when memory is not used.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


class MemoryError(Exception):
    """Raised for user-facing memory operations failures (backend unavailable,
    embedding failure, etc.)."""


@runtime_checkable
class MemoryBackend(Protocol):
    """Semantic memory storage contract.

    Implementations must be thread-safe and must never raise arbitrary
    exceptions — wrap failures in :class:`MemoryError`.
    """

    def retain(self, content: str, metadata: Optional[dict] = None,
               tags: Optional[list[str]] = None) -> str:
        """Store a fact and return its ``memory_id``."""
        ...

    def recall(self, query_text: str, top_k: int = 10,
               tag_filter: Optional[list[str]] = None) -> list[dict]:
        """Semantic search. Returns ``[{id, content, metadata, tags, score}]``
        sorted by relevance (best first)."""
        ...

    def forget(self, memory_id: str) -> bool:
        """Remove a memory. Returns True when a row was deleted."""
        ...

    def count(self) -> int:
        """Total stored facts."""
        ...
