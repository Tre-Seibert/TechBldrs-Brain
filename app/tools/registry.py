"""Stable tool names. Do not rename — hardware and LLM swaps must keep these."""

from __future__ import annotations

from typing import Any

SEARCH_CONTACT = "search_contact"
LATEST_TICKET = "latest_ticket"
LIST_MAIL = "list_mail"

TOOL_NAMES = (SEARCH_CONTACT, LATEST_TICKET, LIST_MAIL)

_SEARCH_CONTACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Name, email, or fragment (e.g. Debe).",
        },
        "client_code": {
            "type": "string",
            "description": "Optional Flow client_code to scope the search (e.g. WDON).",
        },
        "limit": {"type": "integer", "description": "Max rows (default 25, max 100)."},
    },
    "required": ["query"],
}

_LATEST_TICKET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": {
            "type": "string",
            "description": "Flow client_code (e.g. WDON for Western Dental).",
        },
        "contact_id": {
            "type": "integer",
            "description": "Optional contacts.id from search_contact.",
        },
        "status": {
            "type": "string",
            "description": "Optional ticket status filter (Open, Closed, New, ...).",
        },
    },
    "required": ["client_code"],
}

_LIST_MAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": {
            "type": "string",
            "description": "Flow client_code whose ticket-attached mail to list.",
        },
        "direction": {
            "type": "string",
            "enum": ["inbound", "outbound", "imported", "all"],
            "description": (
                "Flow mail.direction. inbound = from the client to the TechBldrs tenant. "
                "Default inbound. Graph is not queried; unfiled mail is a later tool."
            ),
        },
        "email": {
            "type": "string",
            "description": "Optional from_address filter (inbound sender).",
        },
        "contact_id": {
            "type": "integer",
            "description": "Optional contacts.id from search_contact.",
        },
        "limit": {"type": "integer", "description": "Max rows (default 25, max 100)."},
    },
    "required": ["client_code"],
}

OPENAI_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": SEARCH_CONTACT,
            "description": (
                "Find Flow contacts by name or email. Then call list_mail or latest_ticket "
                "for last inbound mail / ticket activity. Does not return passwords."
            ),
            "parameters": _SEARCH_CONTACT_SCHEMA,
        },
    },
    {
        "type": "function",
        "function": {
            "name": LATEST_TICKET,
            "description": (
                "Return the most recently active Flow ticket for a client_code "
                "(order: tickets.last_activity_at). Ticket label is CLIENT-NNNN."
            ),
            "parameters": _LATEST_TICKET_SCHEMA,
        },
    },
    {
        "type": "function",
        "function": {
            "name": LIST_MAIL,
            "description": (
                "List Flow mail rows already filed on tickets for a client. "
                "direction=inbound is mail from that client to the TechBldrs tenant. "
                "Does not call Microsoft Graph."
            ),
            "parameters": _LIST_MAIL_SCHEMA,
        },
    },
]


def openai_tools() -> list[dict[str, Any]]:
    return list(OPENAI_TOOLS)
