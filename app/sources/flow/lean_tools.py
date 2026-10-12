"""Flow's tool schemas for PROMPT_STYLE=lean: the same names, parameters, types and enums as tools.py, with
shorter descriptions that carry the guidance the legacy system prompt held as "question -> tool call" recipes.

Keep the parameters in step with tools.py (tests/test_prompt_style.py checks it): the handlers validate the
same arguments whichever style the model saw.
"""

from __future__ import annotations

from typing import Any

from app.sources.base import function_tool as _fn
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
)

_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_DATE = {"type": "string", "description": "YYYY-MM-DD"}
_DATE_EXCLUSIVE = {"type": "string", "description": "YYYY-MM-DD, exclusive"}


def _d(kind: dict[str, Any], description: str) -> dict[str, Any]:
    return {**kind, "description": description}


_ME_OR_TECH = "Technician name as the user said it (Tom), two-letter code, or 'me'. Names are resolved; never guess a code."

_SEARCH_CONTACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": _d(_STR, "Name, email, or fragment."),
        "client_code": _d(_STR, "Optional client code to search within."),
        "limit": _INT,
    },
    "required": ["query"],
}

_SEARCH_TECHNICIAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"query": _d(_STR, "Name, email, or two-letter code. Never 'me'.")},
    "required": ["query"],
}

_LIST_TICKETS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "assignee_code": _d(_STR, _ME_OR_TECH),
        "client_code": _d(
            _STR, "e.g. ZINT; several comma-separated. 'internal' = the internal clients plus reason Internal."
        ),
        "needs_response": _d(_BOOL, "true = the client wrote last and we have not replied."),
        "unassigned": _d(_BOOL, "true = no assignee."),
        "client_initiated": _d(
            _BOOL,
            "true = tickets a client person opened, alerts and automated senders left out. Today unless a period "
            "is named. Use this, not list_mail.",
        ),
        "exclude_alerts": _d(_BOOL, "true = drop Alert tickets ('remove the alert tickets')."),
        "summarize": _d(
            _BOOL, "true for summarize / recap / what's going on / status: you get the notes and write the summary."
        ),
        "q": _d(_STR, "Topic words to search for. Never the client code."),
        "ticket_num": _STR,
        "category": _d(_STR, "e.g. urgent (0 Urgent)."),
        "reason": _d(_STR, "Billable, Support, Internal, Resolved, Admin or Alert. Not category."),
        "complete": _d(_BOOL, "false = incomplete. Not status Done."),
        "project": _d(_BOOL, "The project checkbox (not category 6 Project)."),
        "machine_name": _d(_STR, "Only when the user said machine or hostname."),
        "invoice_num": _STR,
        "job": _STR,
        "cause": _STR,
        "overdue": _d(_BOOL, "true = past due and not complete."),
        "due_before": _DATE,
        "due_after": _DATE,
        "created_before": _DATE,
        "created_after": _DATE,
        "last_activity_before": _DATE,
        "last_activity_after": _DATE,
        "contact_id": _d(_INT, "contacts.id from search_contact."),
        "requestor": _d(_STR, "Requestor's full name, e.g. Michael Sodl."),
        "status": _d(_STR, "Exact status (New, Done). Not stage."),
        "stage": {"type": "string", "enum": ["open", "review", "live", "archived", "all"]},
        "sort": _d(
            {"type": "string", "enum": ["last_activity_at", "hrs_actual_total"]},
            "hrs_actual_total = total time worked by everyone.",
        ),
        "order": {"type": "string", "enum": ["asc", "desc"]},
        "limit": _d(_INT, "Default 100. 100 for 'all'."),
    },
}

_LATEST_TICKET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": _STR,
        "contact_id": _d(_INT, "contacts.id from search_contact."),
        "requestor": _d(_STR, "Person's full name."),
        "status": _STR,
        "stage": _d(
            {"type": "string", "enum": ["archived"]}, "Only when the user said archived. Otherwise omit it."
        ),
    },
}

_FIND_SIMILAR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": _STR,
        "assignee_code": _d(_STR, "Only if the user named a technician."),
        "ticket_id": _d(_INT, "Duplicates of this one ticket (tickets.id)."),
        "stage": {"type": "string", "enum": ["open", "review", "live"]},
        "limit": _INT,
    },
}

_MERGE_TICKETS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_ticket_id": _d(_INT, "tickets.id of the ticket to KEEP, from an earlier tool result."),
        "source_ticket_ids": _d(
            {"type": "array", "items": {"type": "integer"}}, "tickets.id of the ticket(s) to absorb."
        ),
        "target_label": _d(_STR, "Label of the ticket to keep, e.g. ZTB-1680."),
        "source_labels": _d({"type": "array", "items": {"type": "string"}}, "Labels to absorb, e.g. [\"ZTB-1691\"]."),
        "confirm": _d(_BOOL, "true only after the user's latest message restated both labels. Otherwise false."),
    },
    "required": ["target_ticket_id", "source_ticket_ids", "target_label", "source_labels", "confirm"],
}

_LIST_MAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": _STR,
        "received_after": _DATE,
        "received_before": _DATE_EXCLUSIVE,
        "direction": _d(
            {"type": "string", "enum": ["inbound", "outbound", "imported", "all"]},
            "inbound (default) = from the client to us.",
        ),
        "email": _d(_STR, "Sender address."),
        "contact_id": _d(_INT, "contacts.id from search_contact."),
        "limit": _INT,
    },
}

_LIST_TIME_ENTRIES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ticket_label": _d(_STR, "e.g. ACME-0041. Prefer this over ticket_id."),
        "ticket_id": _d(_INT, "Only from an earlier tool result."),
        "client_code": _STR,
        "assignee_code": _d(_STR, "Two-letter code or 'me'."),
        "work_after": _DATE,
        "work_before": _DATE_EXCLUSIVE,
        "billable": _BOOL,
        "reviewed": _d(_BOOL, "false = not yet reviewed."),
        "view": _d(
            {"type": "string", "enum": ["summary", "list"]},
            "summary (default): you write the prose. list: entries verbatim, only when asked to show or list them.",
        ),
        "limit": _INT,
    },
}

_TICKET_STATS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity": _d({"type": "string", "enum": ["tickets", "time", "mail"]}, "time = hours from time entries."),
        "group_by": _d(
            _STR,
            "tickets: client, cause, reason, category, assignee, status, requestor, topic, issue (recurring problem "
            "types). time: client, tech, ticket, billable, reviewed. mail: sender, client, ticket.",
        ),
        "metric": {"type": "string", "enum": ["count", "hours"]},
        "client_code": _STR,
        "assignee_code": _d(_STR, "Technician code or 'me'."),
        "stage": _d(
            {"type": "string", "enum": ["open", "review", "live", "archived", "all"]}, "tickets only. Default all."
        ),
        "direction": _d({"type": "string", "enum": ["inbound", "outbound", "all"]}, "mail only. Default inbound."),
        "billable": _d(_BOOL, "time only."),
        "reviewed": _d(_BOOL, "time only. false = not yet reviewed."),
        "invoiced": _d(_BOOL, "time only. true = billed (has an invoice number)."),
        "after": _d(_STR, "YYYY-MM-DD, inclusive."),
        "before": _d(_STR, "YYYY-MM-DD, exclusive."),
        "limit": _d(_INT, "Top N groups (default 10)."),
    },
    "required": ["entity", "group_by"],
}

_LIST_MACHINES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"client_code": _STR, "limit": _INT},
    "required": ["client_code"],
}

_GET_TICKET_DETAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ticket_label": _d(_STR, "e.g. ZTB-1691. Prefer this."),
        "view": _d({"type": "string", "enum": ["brief", "full"]}, "full only when the user asks for the full log."),
        "ticket_id": _d(_INT, "Only from an earlier tool result. Never invent one."),
    },
}

_GET_MAIL_DETAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"mail_id": _d(_INT, "mail.id from an earlier list_mail result.")},
    "required": ["mail_id"],
}

_GET_CLIENT_DETAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"client_code": _STR},
    "required": ["client_code"],
}


LEAN_OPENAI_TOOLS: list[dict[str, Any]] = [
    _fn(
        SEARCH_CONTACT,
        "Find client people (Flow contacts) by name or email. Not for TechBldrs technicians. Not needed before "
        "list_tickets or latest_ticket, which take requestor=full name.",
        _SEARCH_CONTACT_SCHEMA,
    ),
    _fn(
        SEARCH_TECHNICIAN,
        "Look up a TechBldrs technician by name, email or code. Not needed to list their tickets: list_tickets "
        "takes the name. Never for 'me'.",
        _SEARCH_TECHNICIAN_SCHEMA,
    ),
    _fn(
        LIST_TICKETS,
        "List Flow tickets; needs at least one filter. Default stage is live. Examples: my tickets -> "
        "assignee_code=me, stage=open. Assigned to Tom -> assignee_code=Tom, stage=open, then ask if they also want "
        "archived. Urgent -> category=urgent, stage=open, no assignee unless they said my. Billable/support -> "
        "reason. Overdue -> overdue=true. Tickets for/requested by a person -> requestor=full name. Tickets about X "
        "(at CODE) -> q=X, client_code=CODE, no stage. All open for CODE -> client_code, stage=open, limit=100. What "
        "do I need to respond to -> assignee_code=me, stage=open, needs_response=true. Summarize / recap / what is "
        "going on with CODE -> summarize=true, never a bare list. Open ticket with the most time "
        "worked -> stage=open, sort=hrs_actual_total, order=desc, limit=1. For a label like ZTB-1691 use "
        "get_ticket_detail.",
        _LIST_TICKETS_SCHEMA,
    ),
    _fn(
        LATEST_TICKET,
        "The single most recent ticket. Last CODE ticket -> client_code=CODE. Latest ticket for a person -> "
        "requestor=full name. Never for 'all' tickets or assignee lists.",
        _LATEST_TICKET_SCHEMA,
    ),
    _fn(
        FIND_SIMILAR_TICKETS,
        "Suggest likely duplicate tickets (keep/absorb pairs). 'Do any tickets need merged?' -> stage=open with no "
        "assignee: all open tickets, not the signed-in tech. Pass client_code or assignee_code only if named. "
        "Never say nothing needs merging without calling this. Suggests only; never merges.",
        _FIND_SIMILAR_SCHEMA,
    ),
    _fn(
        MERGE_TICKETS,
        "WRITE: merge absorb ticket(s) into a keep ticket as the signed-in technician. First show the plan (keep "
        "TARGET, absorb SOURCE) and ask the user to reply restating both labels. Call with confirm=true only when "
        "the user's latest message restates both labels; 'ok', 'yes', 'sure' or 'do it' alone is not approval. If "
        "it returns ok=false, tell the user why and stop; never retry with different arguments.",
        _MERGE_TICKETS_SCHEMA,
    ),
    _fn(
        LIST_MAIL,
        "Mail filed on Flow tickets (not a live mailbox), as snippets. Emails from CODE -> client_code=CODE, "
        "direction=inbound; 'today' adds received_after=today.",
        _LIST_MAIL_SCHEMA,
    ),
    _fn(
        LIST_TIME_ENTRIES,
        "Individual time entries; needs ticket_label, client_code, assignee_code or a work date range. What did I "
        "work on last week -> assignee_code=me, work_after=last Monday, work_before=this Monday. My last time entry "
        "-> assignee_code=me alone, no dates. Time logged on a ticket -> ticket_label. Write 2-3 short paragraphs "
        "from the notes; totals are added for you. For hour totals use ticket_stats.",
        _LIST_TIME_ENTRIES_SCHEMA,
    ),
    _fn(
        TICKET_STATS,
        "Counts and hour totals grouped by a field, computed by Flow: how many, how many hours, which client / tech "
        "/ cause has the most, who contacts us most. Examples: most common problems at X -> entity=tickets, "
        "group_by=issue, client_code=X. Who contacts us most from X -> group_by=requestor. Longest-worked tickets "
        "in a period or for a client -> entity=time, group_by=ticket, metric=hours with after/before or "
        "client_code. Ticket I spent the most time on / my hours -> entity=time, assignee_code=me. Hours billed -> "
        "entity=time, invoiced=true. This month = the 1st of this month to the 1st of next.",
        _TICKET_STATS_SCHEMA,
    ),
    _fn(
        LIST_MACHINES,
        "A client's machines from Flow's records (not live Datto RMM).",
        _LIST_MACHINES_SCHEMA,
    ),
    _fn(
        GET_TICKET_DETAIL,
        "Everything about one ticket by label: fields, notes, mail and time. For show me / what is / summarize / "
        "status of a label, and for advice on it ('best way to resolve ZTB-1691'): it also finds how similar issues "
        "were fixed, so do not call search_knowledge for that. Write two short paragraphs: what it is about, then "
        "the latest update. Finds archived labels on its own.",
        _GET_TICKET_DETAIL_SCHEMA,
    ),
    _fn(
        GET_MAIL_DETAIL,
        "Full body of one mail row from an earlier list_mail result (list_mail gives only a snippet).",
        _GET_MAIL_DETAIL_SCHEMA,
    ),
    _fn(
        GET_CLIENT_DETAIL,
        "Client account: contract minutes, balance, support / antivirus / spam-filter renewal dates. Not for "
        "tickets, contacts or mail.",
        _GET_CLIENT_DETAIL_SCHEMA,
    ),
]
