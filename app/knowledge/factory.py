from __future__ import annotations

from app.config import Settings
from app.knowledge.qdrant_source import QdrantKnowledgeSource
from app.knowledge.source import KnowledgeSource, NullKnowledgeSource


def build_knowledge_source(settings: Settings) -> KnowledgeSource:
    if not (settings.qdrant_url or "").strip():
        return NullKnowledgeSource()
    return QdrantKnowledgeSource(
        qdrant_url=settings.qdrant_url,
        collection=settings.knowledge_collection,
        embedding_base_url=settings.llm_base_url,
        embedding_model=settings.embedding_model,
        embedding_api_key=settings.llm_api_key,
    )
