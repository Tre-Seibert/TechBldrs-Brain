"""Tool name + raw arguments -> the handler that runs it."""

from __future__ import annotations

import re
from typing import Any

from app.flow.source import FlowSource
from app.knowledge.source import KnowledgeSource
from app.tools.base import ChatTurn, ToolResult
from app.tools.flow.clients import GetClientDetailArgs, ListMachinesArgs, get_client_detail, list_machines
from app.tools.flow.contacts import SearchContactArgs, SearchTechnicianArgs, search_contact, search_technician
from app.tools.flow.mail import GetMailDetailArgs, ListMailArgs, get_mail_detail, list_mail
from app.tools.flow.merge import FindSimilarTicketsArgs, MergeTicketsArgs, find_similar_tickets, merge_tickets
from app.tools.flow.stats import TicketStatsArgs, ticket_stats
from app.tools.flow.ticket_detail import GetTicketDetailArgs, get_ticket_detail
from app.tools.flow.tickets import LatestTicketArgs, ListTicketsArgs, latest_ticket, list_tickets
from app.tools.flow.time_entries import ListTimeEntriesArgs, list_time_entries
from app.tools.knowledge import SearchKnowledgeArgs, search_knowledge
from app.tools.registry import (
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
    SEARCH_KNOWLEDGE,
    SEARCH_TECHNICIAN,
    TICKET_STATS,
    TOOL_NAMES,
)


_LABEL_ONLY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,8}-[A-Za-z0-9]{3,6}$")


def _label_only_query(args: ListTicketsArgs) -> str | None:
    """The ticket label when a list_tickets call is only a text search for one (q='AMBS-6298')."""
    text = (args.q or "").strip()
    if not _LABEL_ONLY_RE.match(text):
        return None
    if any((value or "").strip() for value in (args.client_code, args.ticket_num, args.assignee_code, args.requestor)):
        return None
    return text.upper()


def dispatch(
    source: FlowSource,
    name: str,
    raw_args: dict[str, Any],
    turn: ChatTurn | None = None,
    *,
    knowledge: KnowledgeSource | None = None,
) -> ToolResult:
    if name == SEARCH_CONTACT:
        return search_contact(source, SearchContactArgs.model_validate(raw_args), turn)
    if name == SEARCH_TECHNICIAN:
        return search_technician(source, SearchTechnicianArgs.model_validate(raw_args))
    if name == LIST_TICKETS:
        list_args = ListTicketsArgs.model_validate(raw_args)
        only_label = _label_only_query(list_args)
        if only_label:
            # "what is AMBS-6298 about" is a lookup of that one ticket, not a text search for its label.
            return get_ticket_detail(source, GetTicketDetailArgs(ticket_label=only_label), turn, knowledge)
        return list_tickets(source, list_args, turn)
    if name == LATEST_TICKET:
        return latest_ticket(source, LatestTicketArgs.model_validate(raw_args), turn)
    if name == FIND_SIMILAR_TICKETS:
        return find_similar_tickets(source, FindSimilarTicketsArgs.model_validate(raw_args), turn)
    if name == MERGE_TICKETS:
        return merge_tickets(source, MergeTicketsArgs.model_validate(raw_args), turn)
    if name == LIST_MAIL:
        return list_mail(source, ListMailArgs.model_validate(raw_args), turn)
    if name == LIST_TIME_ENTRIES:
        return list_time_entries(source, ListTimeEntriesArgs.model_validate(raw_args), turn)
    if name == TICKET_STATS:
        return ticket_stats(source, TicketStatsArgs.model_validate(raw_args), turn)
    if name == LIST_MACHINES:
        return list_machines(source, ListMachinesArgs.model_validate(raw_args))
    if name == GET_TICKET_DETAIL:
        return get_ticket_detail(source, GetTicketDetailArgs.model_validate(raw_args), turn, knowledge)
    if name == GET_MAIL_DETAIL:
        return get_mail_detail(source, GetMailDetailArgs.model_validate(raw_args))
    if name == GET_CLIENT_DETAIL:
        return get_client_detail(source, GetClientDetailArgs.model_validate(raw_args))
    if name == SEARCH_KNOWLEDGE:
        return search_knowledge(knowledge, SearchKnowledgeArgs.model_validate(raw_args))
    return ToolResult(
        ok=False,
        tool=name,
        source=source.source_name,
        error=f"Unknown tool {name!r}. Allowed: {', '.join(TOOL_NAMES)}.",
    )
