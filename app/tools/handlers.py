"""Old import path for the tools. The code lives in app.tools.{base,dispatch,knowledge,...} and in
app.sources.flow.handlers.

Kept so `from app.tools.handlers import ...` in the app and the tests still works, including the
private helpers the tests check directly.
"""

from __future__ import annotations

from app.sources.flow.handlers.clients import GetClientDetailArgs, ListMachinesArgs, get_client_detail, list_machines
from app.sources.flow.handlers.common import format_ticket_list
from app.sources.flow.handlers.contacts import (
    SearchContactArgs,
    SearchTechnicianArgs,
    answer_person_ticket_question,
    search_contact,
    search_technician,
)
from app.sources.flow.handlers.mail import (
    GetMailDetailArgs,
    ListMailArgs,
    answer_person_mail_question,
    get_mail_detail,
    list_mail,
)
from app.sources.flow.handlers.merge import (
    FindSimilarTicketsArgs,
    MergeTicketsArgs,
    answer_merge_suggestion,
    find_similar_tickets,
    format_similar_list,
    merge_tickets,
)
from app.sources.flow.handlers.people import _resolve_assignee
from app.sources.flow.handlers.stats import TicketStatsArgs, format_stats, ticket_stats
from app.sources.flow.handlers.ticket_detail import GetTicketDetailArgs, _issue_query, get_ticket_detail
from app.sources.flow.handlers.tickets import (
    INTERNAL_CLIENT_CODES,
    LatestTicketArgs,
    ListTicketsArgs,
    _is_client_initiated,
    _latest_stage,
    _no_activity_cutoff,
    _search_terms,
    _stem,
    answer_tickets_about,
    latest_ticket,
    list_tickets,
)
from app.sources.flow.handlers.time_entries import (
    ListTimeEntriesArgs,
    _latest_entry_count,
    answer_longest_time_worked,
    list_time_entries,
)
from app.tools.base import ChatTurn, ToolResult
from app.tools.dispatch import dispatch
from app.tools.formatting import _format_minutes
from app.tools.knowledge import SearchKnowledgeArgs, search_knowledge
from app.tools.turns import (
    _asked_latest_for_person,
    _clean_person_name,
    _contact_name_score,
    _damerau,
    _period_from_turn,
    _person_name_from_turn,
)

__all__ = [
    "INTERNAL_CLIENT_CODES",
    "ChatTurn",
    "FindSimilarTicketsArgs",
    "GetClientDetailArgs",
    "GetMailDetailArgs",
    "GetTicketDetailArgs",
    "LatestTicketArgs",
    "ListMachinesArgs",
    "ListMailArgs",
    "ListTicketsArgs",
    "ListTimeEntriesArgs",
    "MergeTicketsArgs",
    "SearchContactArgs",
    "SearchKnowledgeArgs",
    "SearchTechnicianArgs",
    "TicketStatsArgs",
    "ToolResult",
    "_asked_latest_for_person",
    "_clean_person_name",
    "_contact_name_score",
    "_damerau",
    "_format_minutes",
    "_is_client_initiated",
    "_issue_query",
    "_latest_entry_count",
    "_latest_stage",
    "_no_activity_cutoff",
    "_period_from_turn",
    "_person_name_from_turn",
    "_resolve_assignee",
    "_search_terms",
    "_stem",
    "answer_longest_time_worked",
    "answer_merge_suggestion",
    "answer_person_mail_question",
    "answer_person_ticket_question",
    "answer_tickets_about",
    "dispatch",
    "find_similar_tickets",
    "format_similar_list",
    "format_stats",
    "format_ticket_list",
    "get_client_detail",
    "get_mail_detail",
    "get_ticket_detail",
    "latest_ticket",
    "list_machines",
    "list_mail",
    "list_tickets",
    "list_time_entries",
    "merge_tickets",
    "search_contact",
    "search_knowledge",
    "search_technician",
    "ticket_stats",
]
