"""search_knowledge: SOPs, runbooks and past fix notes from the knowledge index."""

from __future__ import annotations

from pydantic import BaseModel

from app.knowledge.source import KnowledgeSource
from app.tools.base import ToolResult
from app.tools.registry import SEARCH_KNOWLEDGE


class SearchKnowledgeArgs(BaseModel):
    query: str
    client_code: str | None = None
    limit: int = 5


def search_knowledge(knowledge: KnowledgeSource | None, args: SearchKnowledgeArgs) -> ToolResult:
    if knowledge is None or not getattr(knowledge, "configured", False):
        return ToolResult(
            ok=False,
            tool=SEARCH_KNOWLEDGE,
            source="unconfigured",
            error=(
                "Knowledge search is not set up (QDRANT_URL is empty). "
                "Answer from Flow tools only; do not guess at SOPs."
            ),
        )
    limit = min(max(args.limit, 1), 10)
    hits = knowledge.search(query=args.query, client_code=args.client_code, limit=limit)
    data = [hit.model_dump() for hit in hits]
    if not hits:
        return ToolResult(
            tool=SEARCH_KNOWLEDGE,
            source=knowledge.source_name,
            data=[],
            reply="Nothing in the knowledge index matched that.",
            note="No SOP/runbook or past fix note matched. Say so; do not guess a procedure.",
        )
    lines = [f"{len(hits)} knowledge match(es):", ""]
    for hit in hits:
        where = f" ({hit.client_code})" if hit.client_code else ""
        lines.append(f"- [{hit.source_type}] {hit.source_label}{where}: {hit.text[:400]}")
    return ToolResult(
        tool=SEARCH_KNOWLEDGE,
        source=knowledge.source_name,
        data=data,
        reply="\n".join(lines).rstrip(),
        note="From SOP docs / past fix notes, not Flow's live ticket data. Verify anything safety- or client-critical.",
    )
