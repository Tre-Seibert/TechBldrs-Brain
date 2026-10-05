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
LIST_TIME_ENTRIES = "list_time_entries"
TICKET_STATS = "ticket_stats"
LIST_MACHINES = "list_machines"
GET_TICKET_DETAIL = "get_ticket_detail"
GET_MAIL_DETAIL = "get_mail_detail"
GET_CLIENT_DETAIL = "get_client_detail"
SEARCH_KNOWLEDGE = "search_knowledge"

TOOL_NAMES = (
    SEARCH_CONTACT,
    SEARCH_TECHNICIAN,
    LIST_TICKETS,
    LATEST_TICKET,
    FIND_SIMILAR_TICKETS,
    MERGE_TICKETS,
    LIST_MAIL,
    LIST_TIME_ENTRIES,
    TICKET_STATS,
    LIST_MACHINES,
    GET_TICKET_DETAIL,
    GET_MAIL_DETAIL,
    GET_CLIENT_DETAIL,
    SEARCH_KNOWLEDGE,
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
                "Technician name as the user said it (e.g. Tom) or two-letter code (e.g. ts); names are "
                "resolved for you. Use 'me' for 'my tickets'. Never guess a code."
            ),
        },
        "client_code": {
            "type": "string",
            "description": (
                "Flow client_code (e.g. ZINT). Several codes comma-separated (ZTB,ZINT) are searched together. "
                "'internal' means all internal clients (ZTB, ZINT, ZAWE, ZZTB, ZFRIENDS) plus any ticket with reason Internal."
            ),
        },
        "needs_response": {
            "type": "boolean",
            "description": (
                "true = the newest mail on the ticket is inbound (the client wrote last and we have not replied). "
                "'What do I need to respond to?' -> assignee_code=me, stage=open, needs_response=true."
            ),
        },
        "unassigned": {
            "type": "boolean",
            "description": "true = tickets with no assignee.",
        },
        "summarize": {
            "type": "boolean",
            "description": (
                "true when the user asks to summarize, recap, or 'what's going on with' these tickets, or asks "
                "for the status of a ticket. You then receive each ticket's notes and recent log and write the "
                "summary yourself; the tool does not list them."
            ),
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
        "sort": {
            "type": "string",
            "enum": ["last_activity_at", "hrs_actual_total"],
            "description": (
                "hrs_actual_total ranks by time worked (actual hours logged). "
                "Which open ticket has the longest time worked? → stage=open, "
                "sort=hrs_actual_total, order=desc, limit=1. No assignee or client."
            ),
        },
        "order": {
            "type": "string",
            "enum": ["asc", "desc"],
            "description": "desc puts the longest time worked first when sort=hrs_actual_total.",
        },
        "limit": {
            "type": "integer",
            "description": "Max rows (default 100, max 100). Use 100 when the user says all. Use 1 for the single longest.",
        },
    },
}

_LATEST_TICKET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": {
            "type": "string",
            "description": "Flow client_code for 'last {CODE} ticket'. Do not invent WDON.",
        },
        "contact_id": {
            "type": "integer",
            "description": "Optional contacts.id from search_contact when the user named a person.",
        },
        "requestor": {
            "type": "string",
            "description": "Person name when asking the latest ticket involving that requestor.",
        },
        "status": {
            "type": "string",
            "description": "Optional ticket status filter (Open, Closed, New, ...).",
        },
    },
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
            "description": "Flow client_code whose ticket-attached mail to list. Optional when contact_id, email, or a date range is given.",
        },
        "received_after": {
            "type": "string",
            "description": "ISO date (YYYY-MM-DD). Mail received on or after this date. 'Today' = today's date.",
        },
        "received_before": {
            "type": "string",
            "description": "ISO date (YYYY-MM-DD). Mail received before this date (exclusive).",
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
}


_LIST_TIME_ENTRIES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ticket_label": {
            "type": "string",
            "description": "Ticket label such as ACME-0041. Prefer this over ticket_id; never invent a ticket_id.",
        },
        "ticket_id": {"type": "integer", "description": "tickets.id, only if a previous tool result gave it to you."},
        "client_code": {"type": "string", "description": "Flow client_code (e.g. WDON). ticket_id or client_code is required."},
        "assignee_code": {
            "type": "string",
            "description": (
                "Optional: narrow to one technician's entries. Two-letter code from search_technician, "
                "or 'me' for the signed-in technician."
            ),
        },
        "work_after": {"type": "string", "description": "ISO date (YYYY-MM-DD). Work on or after this date."},
        "work_before": {"type": "string", "description": "ISO date (YYYY-MM-DD). Work before this date (exclusive)."},
        "billable": {"type": "boolean", "description": "true = only billable entries."},
        "reviewed": {"type": "boolean", "description": "false = entries not yet reviewed."},
        "view": {
            "type": "string",
            "enum": ["summary", "list"],
            "description": (
                "summary (default): you receive the entries' titles and notes and write a short prose summary "
                "of the work; exact totals are added for you. list: every entry verbatim, for 'show/list the "
                "time entries'."
            ),
        },
        "limit": {"type": "integer", "description": "Max rows for view=list (default 25, max 100). Summaries read up to 100."},
    },
}

_TICKET_STATS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity": {
            "type": "string",
            "enum": ["tickets", "time", "mail"],
            "description": "What to count: tickets (live + archived), time entries (hours), or inbound mail.",
        },
        "group_by": {
            "type": "string",
            "description": (
                "tickets: client, cause, reason, category, assignee, status, requestor, topic, issue (what problems come "
                "up most: groups similar ticket titles into recurring issue types). "
                "time: client, tech, ticket, billable, reviewed. mail: sender, client, ticket."
            ),
        },
        "metric": {
            "type": "string",
            "enum": ["count", "hours"],
            "description": "Rank by row count or by hours (tickets: hours worked; time: minutes logged).",
        },
        "client_code": {"type": "string", "description": "Optional Flow client_code (e.g. BUCK)."},
        "assignee_code": {
            "type": "string",
            "description": "Optional technician code or 'me' (tickets, or time entries by that tech).",
        },
        "stage": {
            "type": "string",
            "enum": ["open", "review", "live", "archived", "all"],
            "description": "tickets only. Default all (open + review + archived).",
        },
        "direction": {
            "type": "string",
            "enum": ["inbound", "outbound", "all"],
            "description": "mail only. Default inbound (client to us).",
        },
        "billable": {"type": "boolean", "description": "time only: true = billable entries only."},
        "after": {"type": "string", "description": "ISO date (YYYY-MM-DD), inclusive. Tickets: created. Time: worked. Mail: received."},
        "before": {"type": "string", "description": "ISO date (YYYY-MM-DD), exclusive."},
        "limit": {"type": "integer", "description": "How many top groups to return (default 10, max 100)."},
    },
    "required": ["entity", "group_by"],
}

_LIST_MACHINES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": {"type": "string", "description": "Flow client_code (e.g. WDON)."},
        "limit": {"type": "integer", "description": "Max rows (default 100, max 100)."},
    },
    "required": ["client_code"],
}

_GET_TICKET_DETAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ticket_label": {
            "type": "string",
            "description": "Ticket label such as ZTB-1691. Prefer this; it is resolved for you.",
        },
        "view": {
            "type": "string",
            "enum": ["brief", "full"],
            "description": (
                "brief (default): the fields, then you write a two-paragraph summary of what the ticket is "
                "about and its latest update from its notes, emails and time entries. full: the raw log text, "
                "only when the user asks for the full log."
            ),
        },
        "ticket_id": {
            "type": "integer",
            "description": "tickets.id, only if an earlier tool result gave it to you. Never invent one.",
        },
    },
}

_GET_MAIL_DETAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mail_id": {
            "type": "integer",
            "description": "mail.id from an earlier list_mail result.",
        },
    },
    "required": ["mail_id"],
}

_GET_CLIENT_DETAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": {"type": "string", "description": "Flow client_code (e.g. WDON)."},
    },
    "required": ["client_code"],
}


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
        "Look up a TechBldrs technician by name, email, or code (shows who they are and their assignee_code). "
        "list_tickets accepts a technician's name directly, so you do not need this just to list their tickets.",
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
        "Single most recent ticket. Use client_code for 'last {CODE} ticket'. For a person "
        "('latest ticket involving Thomas Carter') pass requestor or contact_id after search_contact. "
        "Never invent WDON. Never for assignee lists or 'all' tickets.",
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
    _fn(
        LIST_TIME_ENTRIES,
        "List individual time entries. Needs ticket_id, client_code, assignee_code, or a work_after/work_before "
        "range. 'What did I work on last week' -> assignee_code=me + work_after=<last Monday> + "
        "work_before=<this Monday> (summary view, you write the prose). 'Show my time entries' -> view=list. "
        "'My last time entry' -> assignee_code=me alone: the tool then returns only the newest one submitted, "
        "with its notes. 'Time entries not reviewed' -> reviewed=false. For totals ('how many hours') use ticket_stats instead "
        "of adding rows yourself.",
        _LIST_TIME_ENTRIES_SCHEMA,
    ),
    _fn(
        TICKET_STATS,
        "Counts and hour totals grouped by something, computed by Flow. Use for 'how many', 'how many hours', "
        "'which client/person/cause has the most', 'who contacts us most', 'what problem does X face most'. "
        "Never count or add up rows from other tools yourself. Examples: problem a client has most -> "
        "entity=tickets, group_by=issue, client_code=X (you receive the grouped titles and write the analysis). "
        "Who contacts us most from X -> entity=tickets, "
        "group_by=requestor, client_code=X. Most open tickets by client -> entity=tickets, group_by=client, "
        "stage=open. Longest ticket worked last week/this month -> entity=time, group_by=ticket, metric=hours, "
        "after/before (the all-time longest uses list_tickets sort=hrs_actual_total instead). "
        "Hours billed to X this month -> entity=time, group_by=client, billable=true, client_code=X, "
        "after/before. My hours yesterday -> entity=time, group_by=tech, assignee_code=me, after/before. "
        "The ticket I spent the most time on -> entity=time, group_by=ticket, metric=hours, assignee_code=me.",
        _TICKET_STATS_SCHEMA,
    ),
    _fn(
        LIST_MACHINES,
        "List a client's machines/workstations from Flow (not Datto RMM directly). "
        "'What machines does client Y have' -> client_code=Y.",
        _LIST_MACHINES_SCHEMA,
    ),
    _fn(
        GET_TICKET_DETAIL,
        "Fuller read of one ticket: full log/notes text and hours, beyond what list_tickets/"
        "latest_ticket return. Only call after another tool already gave you the ticket_id — "
        "never to find a ticket in the first place.",
        _GET_TICKET_DETAIL_SCHEMA,
    ),
    _fn(
        GET_MAIL_DETAIL,
        "Full body of one mail row list_mail already found (list_mail only returns a snippet). "
        "Only call with a mail_id from a prior list_mail result.",
        _GET_MAIL_DETAIL_SCHEMA,
    ),
    _fn(
        GET_CLIENT_DETAIL,
        "Client account info: contract minutes, balance, support/antivirus/spam-filter renewal "
        "dates. Not for tickets/contacts/mail — use the ticket/contact/mail tools for those.",
        _GET_CLIENT_DETAIL_SCHEMA,
    ),
    _fn(
        SEARCH_KNOWLEDGE,
        "Free-text search over IT Glue SOP/runbook documents and past time-entry fix notes. "
        "Use for 'how do we usually fix X', 'why did we do X', or anything that is not a "
        "structured filter the ticket/mail/contact tools already cover. Not for finding a "
        "specific ticket, contact, or mail row — use the other tools for that.",
        _SEARCH_KNOWLEDGE_SCHEMA,
    ),
]


def openai_tools() -> list[dict[str, Any]]:
    return list(OPENAI_TOOLS)
