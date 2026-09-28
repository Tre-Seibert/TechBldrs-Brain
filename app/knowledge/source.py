"""SOP/runbook knowledge search: IT Glue documents + Flow time-entry notes, embedded
into Qdrant. Phase 3 of the README's roadmap -- separate from Flow's structured data,
which stays on FlowSource. A missing/unreachable Qdrant must degrade the tool to a
clear "not configured" reply, never a 500.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class KnowledgeHit(BaseModel):
    text: str
    source_type: str  # "itglue_document" | "time_entry"
    source_label: str  # doc name, or a ticket label
    client_code: str | None = None
    score: float = 0.0
    url: str | None = None


class KnowledgePoint(BaseModel):
    """One embeddable chunk, written by the sync job."""

    id: str
    text: str
    source_type: str
    source_label: str
    client_code: str | None = None
    updated_at: str | None = None
    url: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class KnowledgeSource(Protocol):
    source_name: str
    configured: bool

    def search(self, *, query: str, client_code: str | None = None, limit: int = 5) -> list[KnowledgeHit]: ...

    def upsert(self, points: list[KnowledgePoint]) -> None: ...


class NullKnowledgeSource:
    """Used when QDRANT_URL is empty. Every call reports itself as unconfigured."""

    source_name = "unconfigured"
    configured = False

    def search(self, *, query: str, client_code: str | None = None, limit: int = 5) -> list[KnowledgeHit]:
        return []

    def upsert(self, points: list[KnowledgePoint]) -> None:
        raise RuntimeError("QDRANT_URL is not set; nothing to sync into.")
