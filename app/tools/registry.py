"""Stable tool names. Do not rename — hardware and LLM swaps must keep these.

Keep this catalog small: a 7B model picks the wrong tool when tools overlap.
New questions should be expressible as search / list / similar / merge, not a
new tool per question.
"""

from __future__ import annotations

from typing import Any

SEARCH_CONTACT = "search_contact"
SEARCH_TECHNICIAN = "search_technician"
LIST_TICKETS = "list_tickets"
LATEST_TICKET = "latest_ticket"
FIND_SIMILAR_TICKETS = "find_similar_tickets"
MERGE_TICKETS = "merge_tickets"
LIST_MAIL = "list_mail"

TOOL_NAMES = (
    SEARCH_CONTACT,
    SEARCH_TECHNICIAN,
    LIST_TICKETS,
    LATEST_TICKET,
    FIND_SIMILAR_TICKETS,
    MERGE_TICKETS,
    LIST_MAIL,
)
WRITE_TOOL_NAMES = (MERGE_TICKETS,)

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

_SEARCH_TECHNICIAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Technician first name, full name, email, or two-letter code (e.g. Tre, tseibert, ts). "
                "Do not search for 'me' — use list_tickets(assignee_code=me) instead."
            ),
        },
    },
    "required": ["query"],
}

_LIST_TICKETS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "assignee_code": {
            "type": "string",
            "description": (
                "Two-letter technician code from search_technician (e.g. ts), "
                "or 'me' for the signed-in technician. Use me for 'my tickets'."
            ),
        },
        "client_code": {
            "type": "string",
            "description": "Flow client_code (e.g. ZINT).",
        },
        "q": {
            "type": "string",
            "description": "Search text in topic, subject, requestor, machine name, or a label like ZINT-5466.",
        },
        "ticket_num": {
            "type": "string",
            "description": "Four-character ticket number.",
        },
        "category": {
            "type": "string",
            "description": (
                "Ticket category. Urgent is '0 Urgent' (also accept urgent). "
                "Any urgent tickets? → category=urgent, no assignee_code."
            ),
        },
        "reason": {
            "type": "string",
            "description": (
                "Ticket reason: Billable/New, Support, Internal, Resolved, Admin, Alert. "
                "Any billable tickets? → reason=billable, no assignee_code."
            ),
        },
        "complete": {
            "type": "boolean",
            "description": "true = marked complete. false = incomplete. Not the same as status Done.",
        },
        "project": {
            "type": "boolean",
            "description": "Ticket project checkbox. Not the same as category 6 Project.",
        },
        "machine_name": {"type": "string", "description": "Machine / hostname on the ticket."},
        "invoice_num": {"type": "string", "description": "Invoice number on the ticket."},
        "job": {"type": "string", "description": "Job number on the ticket."},
        "cause": {"type": "string", "description": "Substring match on ticket cause."},
        "overdue": {
            "type": "boolean",
            "description": "true = due_at is in the past and the ticket is not complete.",
        },
        "due_before": {"type": "string", "description": "ISO date. Tickets due before this instant."},
        "due_after": {"type": "string", "description": "ISO date. Tickets due on or after this instant."},
        "created_before": {"type": "string", "description": "ISO date."},
        "created_after": {"type": "string", "description": "ISO date."},
        "last_activity_before": {"type": "string", "description": "ISO date."},
        "last_activity_after": {"type": "string", "description": "ISO date."},
        "contact_id": {
            "type": "integer",
            "description": (
                "contacts.id from search_contact. Tickets for a named person use this, "
                "not client_code. Also matches tickets that only have that name in requestor."
            ),
        },
        "requestor": {
            "type": "string",
            "description": "Requestor name on the ticket (e.g. Michael Sodl). Prefer contact_id after search_contact.",
        },
        "status": {
            "type": "string",
            "description": "Optional exact status (New, Done, ...). Not the same as stage. Works on archived too.",
        },
        "stage": {
            "type": "string",
            "enum": ["open", "review", "live", "archived", "all"],
            "description": (
                "open = not archived and category is not 9 REVIEW. "
                "review = not archived and category is 9 REVIEW. "
                "archived = archived tickets. live = open+review. "
                "Assigned-to queries are always open unless the user said review, archived, or all. "
                "Do not pass stage=live for 'tickets assigned to {tech}'."
            ),
        },
        "limit": {
            "type": "integer",
            "description": "Max rows (default 100, max 100). Use 100 when the user says all.",
        },
    },
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

_FIND_SIMILAR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": {"type": "string", "description": "Look for duplicates within this client."},
        "assignee_code": {
            "type": "string",
            "description": "Look for duplicates among this technician's tickets (code from search_technician).",
        },
        "ticket_id": {"type": "integer", "description": "Find duplicates of this one ticket (tickets.id)."},
        "stage": {
            "type": "string",
            "enum": ["open", "review", "live"],
            "description": "Default open (all open tickets, not just the signed-in tech). Do not pass assignee unless the user named one.",
        },
        "limit": {"type": "integer", "description": "Max pairs (default 20, max 50)."},
    },
}

_MERGE_TICKETS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_ticket_id": {"type": "integer", "description": "tickets.id of the ticket to KEEP."},
        "source_ticket_ids": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "tickets.id of the ticket(s) to absorb into the target.",
        },
        "target_label": {"type": "string", "description": "Label of the ticket to keep, e.g. ZTB-1680."},
        "source_labels": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Labels of the ticket(s) to absorb, e.g. [\"ZTB-1691\"].",
        },
        "confirm": {
            "type": "boolean",
            "description": "true only after the user replied restating both labels. Otherwise false.",
        },
    },
    "required": ["target_ticket_id", "source_ticket_ids", "target_label", "source_labels", "confirm"],
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


def _fn(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


OPENAI_TOOLS: list[dict[str, Any]] = [
    _fn(
        SEARCH_CONTACT,
        "Find client people (Flow contacts) by name or email. Not for technician assignees; "
        "use search_technician for TechBldrs staff. Does not return passwords.",
        _SEARCH_CONTACT_SCHEMA,
    ),
    _fn(
        SEARCH_TECHNICIAN,
        "Resolve a TechBldrs technician's name, email, or code to their two-letter assignee_code. "
        "Call this before list_tickets when the user names a technician.",
        _SEARCH_TECHNICIAN_SCHEMA,
    ),
    _fn(
        LIST_TICKETS,
        "List Flow tickets. The main ticket tool: assignee, client, category, reason, complete, "
        "project, machine, invoice, job, cause, overdue/dates, or text search. Needs at least one "
        "of those scopes. Urgent/billable/overdue questions do not use assignee=me unless the user said my.",
        _LIST_TICKETS_SCHEMA,
    ),
    _fn(
        LATEST_TICKET,
        "Only for 'what is the last {CODE} ticket': the single most recently active ticket for one "
        "client. Never for assignee lists or 'all' tickets; use list_tickets for those.",
        _LATEST_TICKET_SCHEMA,
    ),
    _fn(
        FIND_SIMILAR_TICKETS,
        "Suggest likely duplicate tickets (keep/absorb pairs with reasons). "
        "If the user named a client or technician, pass that. If they asked about open tickets "
        "in general, call with no assignee_code and stage=open — never default to the signed-in "
        "tech or to WDON. Suggests only; never merges.",
        _FIND_SIMILAR_SCHEMA,
    ),
    _fn(
        MERGE_TICKETS,
        "WRITE. Merge absorb ticket(s) into a keep ticket as the signed-in technician. Only call after "
        "you showed the keep/absorb labels and the user's latest reply restates those labels. "
        "Never on 'ok', 'yes', or 'do it' alone.",
        _MERGE_TICKETS_SCHEMA,
    ),
    _fn(
        LIST_MAIL,
        "List Flow mail rows already filed on tickets for a client. direction=inbound is mail from "
        "that client to the TechBldrs tenant. Does not call Microsoft Graph.",
        _LIST_MAIL_SCHEMA,
    ),
]


def openai_tools() -> list[dict[str, Any]]:
    return list(OPENAI_TOOLS)
