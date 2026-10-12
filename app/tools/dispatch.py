"""Tool name + raw arguments -> the source that runs it. search_knowledge is shared and runs here."""

from __future__ import annotations

from typing import Any

from app.knowledge.source import KnowledgeSource
from app.sources.base import Sources, as_sources
from app.tools.base import ChatTurn, ToolResult
from app.tools.knowledge import SearchKnowledgeArgs, search_knowledge
from app.tools.registry import SEARCH_KNOWLEDGE, SHARED_TOOL_NAMES


def dispatch(
    sources: Sources | Any,
    name: str,
    raw_args: dict[str, Any],
    turn: ChatTurn | None = None,
    *,
    knowledge: KnowledgeSource | None = None,
) -> ToolResult:
    """`sources` is the enabled Sources bundle; a bare Flow client still works and means Flow only."""
    sources = as_sources(sources)
    if name == SEARCH_KNOWLEDGE:
        return search_knowledge(knowledge, SearchKnowledgeArgs.model_validate(raw_args))
    result = sources.dispatch(name, raw_args, turn, knowledge=knowledge)
    if result is not None:
        return result
    allowed = (*sources.tool_names(), *SHARED_TOOL_NAMES)
    return ToolResult(
        ok=False,
        tool=name,
        source=sources.source_name_for(name),
        error=f"Unknown tool {name!r}. Allowed: {', '.join(allowed)}.",
    )
