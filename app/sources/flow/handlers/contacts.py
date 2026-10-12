"""Flow people tools: search_contact, search_technician, and 'tickets for {person}'."""

from __future__ import annotations

from pydantic import BaseModel

from app.sources.flow.handlers.mail import answer_person_mail_question
from app.sources.flow.handlers.payloads import _contact_payload, _technician_payload
from app.sources.flow.handlers.people import _pick_contact, _search_contacts_loose, _signed_in_technician
from app.sources.flow.handlers.tickets import ListTicketsArgs, list_tickets
from app.sources.flow.protocol import FlowSource
from app.sources.flow.tools import SEARCH_CONTACT, SEARCH_TECHNICIAN
from app.tools.base import ChatTurn, ToolResult, _refuse
from app.tools.turns import (
    _asked_latest_for_person,
    _asked_mail_for_person,
    _asked_tickets_for_person,
    _is_self_token,
    _looks_like_person_name,
    _person_name_from_turn,
)


class SearchContactArgs(BaseModel):
    query: str
    client_code: str | None = None
    limit: int = 25


class SearchTechnicianArgs(BaseModel):
    query: str = ""
    limit: int = 25


def answer_person_ticket_question(source: FlowSource, turn: ChatTurn | None) -> ToolResult | None:
    """Bypass the LLM for 'tickets for / latest ticket involving {person}'."""
    if not _asked_tickets_for_person(turn) or _asked_mail_for_person(turn):
        return None
    name = _person_name_from_turn(turn)
    if not name:
        return None
    return search_contact(source, SearchContactArgs(query=name), turn)


def search_contact(source: FlowSource, args: SearchContactArgs, turn: ChatTurn | None = None) -> ToolResult:
    mail_answer = answer_person_mail_question(source, turn)
    if mail_answer is not None:
        return mail_answer
    person = _person_name_from_turn(turn) or args.query
    looking_for_person = _looks_like_person_name(person) or _asked_tickets_for_person(turn)
    rows = (
        _search_contacts_loose(source, person)
        if looking_for_person
        else source.search_contact(
            query=args.query,
            client_code=args.client_code,
            limit=args.limit,
        )
    )
    pick, contact_error = _pick_contact(rows, person) if looking_for_person else (None, None)
    if contact_error and not _asked_tickets_for_person(turn):
        return _refuse(SEARCH_CONTACT, source, contact_error)
    codes = sorted({(r.client_code or "") for r in rows if r.client_code})
    client_code = codes[0] if len(codes) == 1 else (args.client_code or None)
    if _asked_tickets_for_person(turn):
        stage = "live"
        limit = 1 if _asked_latest_for_person(turn) else 100
        listed = list_tickets(
            source,
            ListTicketsArgs(
                contact_id=pick.id if pick is not None else None,
                requestor=(pick.full_name if pick is not None else person),
                stage=stage,
                limit=limit,
            ),
            turn,
        )
        if listed.data:
            return listed
        by_text = list_tickets(
            source,
            ListTicketsArgs(q=person, stage=stage, limit=limit),
            turn,
            force_text_query=True,
        )
        if by_text.data or _asked_latest_for_person(turn):
            return by_text
        if contact_error:
            return _refuse(SEARCH_CONTACT, source, contact_error)
        return listed
    if not rows:
        return ToolResult(
            tool=SEARCH_CONTACT,
            source=source.source_name,
            data=[],
            row_ids=[],
            reply=f"I couldn't find a contact for {person}. Check the name and try again.",
        )
    return ToolResult(
        tool=SEARCH_CONTACT,
        source=source.source_name,
        data=[_contact_payload(r) for r in rows],
        row_ids=[r.id for r in rows],
        client_code=(client_code or "").upper() or None,
        note=(
            "Tickets for this person: list_tickets(contact_id=that id, stage=open). "
            "Do not list the whole client. Mail/last reach-out: list_mail (inbound) or latest_ticket."
        ),
    )


def search_technician(source: FlowSource, args: SearchTechnicianArgs) -> ToolResult:
    if _is_self_token(args.query):
        tech, error = _signed_in_technician(source)
        if error:
            return _refuse(SEARCH_TECHNICIAN, source, error)
        rows = [tech]
    else:
        rows = source.search_technician(query=args.query, limit=args.limit)
    return ToolResult(
        tool=SEARCH_TECHNICIAN,
        source=source.source_name,
        data=[_technician_payload(r) for r in rows],
        row_ids=[r.id for r in rows],
        note=(
            "Use assignee_code with list_tickets or find_similar_tickets."
            if rows
            else "No active technician matched. Do not guess a code."
        ),
    )
