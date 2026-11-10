"""The MCP view of tb-brain's tools: which are offered on each endpoint, and how a ToolResult reads as text.

Offered: every enabled source's read tools, with the descriptions and schemas the chat loop sends the model
(`Sources.openai_tools`, so Datto's rewording of Flow's list_machines applies). Not offered: the write tool
(merge_tickets, whose confirm gate needs the chat) and search_knowledge (parked: no embeddings host).

A ToolResult becomes one block of text for the agent's model, following the chat loop's rules: a formatted `reply`
is shown as it is; a `digest` is what the model writes prose from, with the exact `footer` figures appended so
totals never come from the model; map-reduce `chunks` are flattened, since Copilot's model can read them in one
pass; a result with only `data` is shown as JSON. Everything is capped.
"""

from __future__ import annotations

import json
from typing import Any

from mcp import types

from app.sources.base import Sources
from app.tools.base import ToolResult

ALL = "all"
MAX_TEXT_CHARS = 48_000
MAX_CHUNK_CHARS = 36_000
TRUNCATED = "\n\n[Output cut here: ask with a narrower filter to see the rest.]"
FOOTER_LEAD = "Exact figures (quote these as they are; do not recompute them):"


def offered_specs(sources: Sources, endpoint: str, style: str = "legacy") -> list[dict[str, Any]]:
    """The tool schemas an endpoint offers: every enabled source for `all`, else that source's, read tools only."""
    if endpoint != ALL and endpoint not in sources.names:
        return []
    only = None if endpoint == ALL else (endpoint,)
    writes = set(sources.write_tool_names())
    return [tool["function"] for tool in sources.openai_tools(style=style, only=only) if tool["function"]["name"] not in writes]


def offered_names(sources: Sources, endpoint: str) -> tuple[str, ...]:
    return tuple(spec["name"] for spec in offered_specs(sources, endpoint))


def to_mcp_tool(spec: dict[str, Any]) -> types.Tool:
    return types.Tool(
        name=spec["name"],
        description=spec.get("description") or "",
        input_schema=spec.get("parameters") or {"type": "object", "properties": {}},
        annotations=types.ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )


def result_text(result: ToolResult) -> str:
    """One block of text for the model, from whichever shape the handler returned."""
    if not result.ok:
        return _cap(result.reply or result.error or f"{result.tool} failed.")
    parts: list[str] = []
    if result.header:
        parts.append(result.header)
    if result.reply:
        parts.append(result.reply)
    elif result.chunks or result.digest:
        parts.append(_digest_text(result))
    elif result.data is not None:
        parts.append(_data_text(result.data))
    elif result.fallback:
        parts.append(result.fallback)
    if result.footer:
        parts.append(f"{FOOTER_LEAD}\n{result.footer}")
    if result.note and not any(result.note.strip() in part for part in parts):  # list replies already end with it
        parts.append(f"Note: {result.note}")
    return _cap("\n\n".join(part.strip() for part in parts if part and part.strip()) or "(nothing returned)")


def _digest_text(result: ToolResult) -> str:
    """The digest, with map-reduce chunks flattened in: the chat loop summarized each chunk with the local model;
    here the agent's model reads the chunks themselves."""
    if not result.chunks:
        return result.digest or ""
    pieces = []
    for chunk in result.chunks:
        label = str(chunk.get("label") or "").strip()
        body = str(chunk.get("prompt") or "").strip()
        pieces.append(f"{label}:\n{body}" if label else body)
    chunk_text = "\n\n".join(pieces)
    if len(chunk_text) > MAX_CHUNK_CHARS:
        chunk_text = chunk_text[:MAX_CHUNK_CHARS].rstrip() + "\n\n[More periods were read than fit here.]"
    parts: list[str] = []
    for part in (
        (result.reduce_prefix or "").strip(),
        (result.digest or "").strip(),  # ticket_stats sends the same lines as both; show them once
        "Tickets read, by period:",
        chunk_text,
        (result.reduce_suffix or "").strip(),
    ):
        if part and part not in parts:
            parts.append(part)
    return "\n\n".join(parts)


def _data_text(data: Any) -> str:
    try:
        return json.dumps(data, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(data)


def _cap(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + TRUNCATED
