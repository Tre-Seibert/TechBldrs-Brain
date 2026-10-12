"""Flow tool name + raw arguments -> the handler that runs it."""

from __future__ import annotations

import re
from typing import Any

from app.knowledge.source import KnowledgeSource
from app.sources.flow.handlers.clients import GetClientDetailArgs, ListMachinesArgs, get_client_detail, list_machines
from app.sources.flow.handlers.contacts import SearchContactArgs, SearchTechnicianArgs, search_contact, search_technician
from app.sources.flow.handlers.mail import GetMailDetailArgs, ListMailArgs, get_mail_detail, list_mail
from app.sources.flow.handlers.merge import FindSimilarTicketsArgs, MergeTicketsArgs, find_similar_tickets, merge_tickets
from app.sources.flow.handlers.stats import TicketStatsArgs, ticket_stats
from app.sources.flow.handlers.ticket_detail import GetTicketDetailArgs, get_ticket_detail
from app.sources.flow.handlers.tickets import LatestTicketArgs, ListTicketsArgs, latest_ticket, list_tickets
from app.sources.flow.handlers.time_entries import ListTimeEntriesArgs, list_time_entries
from app.sources.flow.protocol import FlowSource
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
from app.tools.base import ChatTurn, ToolResult


_LABEL_ONLY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,8}-[A-Za-z0-9]{3,6}$")


def _label_only_query(args: ListTicketsArgs) -> str | None:
    """The ticket label when a list_tickets call is only a text search for one (q='AMBS-6298')."""
    text = (args.q or "").strip()
    if not _LABEL_ONLY_RE.match(text):
        return None
    if any((value or "").strip() for value in (args.client_code, args.ticket_num, args.assignee_code, args.requestor)):
        return None
    return text.upper()


def handle(
    source: FlowSource,
    name: str,
    raw_args: dict[str, Any],
    turn: ChatTurn | None = None,
    *,
    knowledge: KnowledgeSource | None = None,
) -> ToolResult | None:
    """Run one Flow tool. None when `name` is not a Flow tool."""
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
    return None
