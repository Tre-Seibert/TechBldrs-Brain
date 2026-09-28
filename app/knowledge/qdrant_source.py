from __future__ import annotations

import uuid
from typing import Any

import httpx

from app.knowledge.source import KnowledgeHit, KnowledgePoint

_NAMESPACE = uuid.UUID("6f6a5e2e-9b8e-4e0a-8b8b-5a1a9a2f6b3e")


def _point_id(logical_id: str) -> str:
    """Qdrant point ids must be an unsigned int or UUID; a stable UUID5 makes
    re-syncing the same logical id (e.g. 'itglue:12345:0') overwrite in place."""
    return str(uuid.uuid5(_NAMESPACE, logical_id))


class QdrantKnowledgeSource:
    source_name = "qdrant"
    configured = True

    def __init__(
        self,
        *,
        qdrant_url: str,
        collection: str,
        embedding_base_url: str,
        embedding_model: str,
        embedding_api_key: str = "ollama",
        timeout: float = 60.0,
    ) -> None:
        self._qdrant_url = qdrant_url.rstrip("/")
        self._collection = collection
        self._embedding_model = embedding_model
        self._timeout = timeout
        self._embed_client = httpx.Client(
            base_url=embedding_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {embedding_api_key}"},
            timeout=timeout,
        )
        self._qdrant_client = httpx.Client(base_url=self._qdrant_url, timeout=timeout)
        self._collection_ready = False

    def _embed(self, text: str) -> list[float]:
        response = self._embed_client.post(
            "/embeddings", json={"model": self._embedding_model, "input": text[:8000]}
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or []
        if not data:
            raise RuntimeError(f"Embedding endpoint returned no data for model {self._embedding_model!r}")
        return data[0]["embedding"]

    def _ensure_collection(self, vector_size: int) -> None:
        if self._collection_ready:
            return
        existing = self._qdrant_client.get(f"/collections/{self._collection}")
        if existing.status_code == 200:
            self._collection_ready = True
            return
        response = self._qdrant_client.put(
            f"/collections/{self._collection}",
            json={"vectors": {"size": vector_size, "distance": "Cosine"}},
        )
        response.raise_for_status()
        self._collection_ready = True

    def search(self, *, query: str, client_code: str | None = None, limit: int = 5) -> list[KnowledgeHit]:
        needle = (query or "").strip()
        if not needle:
            return []
        vector = self._embed(needle)
        self._ensure_collection(len(vector))
        body: dict[str, Any] = {"vector": vector, "limit": limit, "with_payload": True}
        if client_code:
            body["filter"] = {"must": [{"key": "client_code", "match": {"value": client_code.strip().upper()}}]}
        response = self._qdrant_client.post(f"/collections/{self._collection}/points/search", json=body)
        response.raise_for_status()
        results = response.json().get("result") or []
        hits: list[KnowledgeHit] = []
        for row in results:
            payload = row.get("payload") or {}
            hits.append(
                KnowledgeHit(
                    text=payload.get("text", ""),
                    source_type=payload.get("source_type", "unknown"),
                    source_label=payload.get("source_label", ""),
                    client_code=payload.get("client_code"),
                    score=row.get("score", 0.0),
                    url=payload.get("url"),
                )
            )
        return hits

    def upsert(self, points: list[KnowledgePoint]) -> None:
        if not points:
            return
        vectors = [self._embed(point.text) for point in points]
        self._ensure_collection(len(vectors[0]))
        qdrant_points = [
            {
                "id": _point_id(point.id),
                "vector": vector,
                "payload": {
                    "text": point.text,
                    "source_type": point.source_type,
                    "source_label": point.source_label,
                    "client_code": (point.client_code or "").upper() or None,
                    "updated_at": point.updated_at,
                    "url": point.url,
                    **point.extra,
                },
            }
            for point, vector in zip(points, vectors)
        ]
        response = self._qdrant_client.put(
            f"/collections/{self._collection}/points", json={"points": qdrant_points}
        )
        response.raise_for_status()
