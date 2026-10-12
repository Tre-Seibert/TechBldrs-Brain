"""Stable tool names. Do not rename — hardware and LLM swaps must keep these.

Each source's own tools live in app/sources/<name>/tools.py. This module holds the shared tools
(search_knowledge), the combined list sent to the model, and the Flow names under their old import path.
"""

from __future__ import annotations

from typing import Any

from app.sources.base import Sources, as_sources, function_tool
from app.sources.flow.tools import (
    FIND_SIMILAR_TICKETS,
    GET_CLIENT_DETAIL,
    GET_MAIL_DETAIL,
    GET_TICKET_DETAIL,
    LATEST_TICKET,
    LIST_MACHINES,
    LIST_MAIL,
    LIST_TICKETS,
    LIST_TIME_ENTRIES,
    MERGE_TICKETS,
    SEARCH_CONTACT,
    SEARCH_TECHNICIAN,
    TICKET_STATS,
    WRITE_TOOL_NAMES,
)
from app.sources.flow.lean_tools import LEAN_OPENAI_TOOLS as FLOW_LEAN_OPENAI_TOOLS
from app.sources.flow.tools import OPENAI_TOOLS as FLOW_OPENAI_TOOLS
from app.sources.flow.tools import TOOL_NAMES as FLOW_TOOL_NAMES

SEARCH_KNOWLEDGE = "search_knowledge"

# Tools that belong to no source; offered after every enabled source's tools.
SHARED_TOOL_NAMES = (SEARCH_KNOWLEDGE,)

# Flow plus the shared tools: the tool set when only Flow is enabled (the default).
TOOL_NAMES = (*FLOW_TOOL_NAMES, *SHARED_TOOL_NAMES)

__all__ = [
    "FIND_SIMILAR_TICKETS",
    "GET_CLIENT_DETAIL",
    "GET_MAIL_DETAIL",
    "GET_TICKET_DETAIL",
    "LATEST_TICKET",
    "LEAN_SHARED_TOOLS",
    "LIST_MACHINES",
    "LIST_MAIL",
    "LIST_TICKETS",
    "LIST_TIME_ENTRIES",
    "MERGE_TICKETS",
    "OPENAI_TOOLS",
    "SEARCH_CONTACT",
    "SEARCH_KNOWLEDGE",
    "SEARCH_TECHNICIAN",
    "SHARED_TOOLS",
    "SHARED_TOOL_NAMES",
    "TICKET_STATS",
    "TOOL_NAMES",
    "WRITE_TOOL_NAMES",
    "openai_tools",
]

_SEARCH_KNOWLEDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Free-text question, e.g. 'how do we fix VPN dropouts on a FortiGate'.",
        },
        "client_code": {
            "type": "string",
            "description": "Optional: only search this client's IT Glue docs and time-entry notes.",
        },
        "limit": {"type": "integer", "description": "Max results (default 5, max 10)."},
    },
    "required": ["query"],
}

SHARED_TOOLS: list[dict[str, Any]] = [
    function_tool(
        SEARCH_KNOWLEDGE,
        "Free-text search over IT Glue SOP/runbook documents and past time-entry fix notes. "
        "Use for 'how do we usually fix X', 'why did we do X', or anything that is not a "
        "structured filter the ticket/mail/contact tools already cover. Not for finding a "
        "specific ticket, contact, or mail row — use the other tools for that.",
        _SEARCH_KNOWLEDGE_SCHEMA,
    ),
]

# PROMPT_STYLE=lean: the same tool with a shorter description.
LEAN_SHARED_TOOLS: list[dict[str, Any]] = [
    function_tool(
        SEARCH_KNOWLEDGE,
        "Search IT Glue SOP/runbook documents and past time-entry fix notes (not live data; say so when you use "
        "it). For 'how do we usually fix X', 'why did we do X', 'what's the process for X'. Not for finding a "
        "ticket, contact or mail row. If nothing matches, say no SOP or runbook match was found. Notes may be old.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The topic, e.g. 'VPN dropouts on a FortiGate'."},
                "client_code": {"type": "string", "description": "Optional: only this client's docs and notes."},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    ),
]

OPENAI_TOOLS: list[dict[str, Any]] = [*FLOW_OPENAI_TOOLS, *SHARED_TOOLS]


def openai_tools(sources: Sources | Any | None = None, style: str = "legacy") -> list[dict[str, Any]]:
    """Every enabled source's tools, then the shared ones, in PROMPT_STYLE `style`. No sources means Flow only."""
    if style == "lean":
        if sources is None:
            return [*FLOW_LEAN_OPENAI_TOOLS, *LEAN_SHARED_TOOLS]
        return [*as_sources(sources).openai_tools(style), *LEAN_SHARED_TOOLS]
    if sources is None:
        return list(OPENAI_TOOLS)
    return [*as_sources(sources).openai_tools(), *SHARED_TOOLS]
