"""Flow mail tools: list_mail, get_mail_detail, and 'when did {person} last reach out'."""

from __future__ import annotations

import re

from pydantic import BaseModel, model_validator

from app.sources.flow.handlers.payloads import _mail_detail_payload, _mail_payload, _ticket_payload
from app.sources.flow.handlers.people import _requestor_matches, _requestor_queries
from app.sources.flow.protocol import FlowRequestError, FlowSource
from app.sources.flow.schemas import MailRecord, TicketRecord, ticket_label
from app.sources.flow.tools import GET_MAIL_DETAIL, LIST_MAIL
from app.tools.base import ChatTurn, ToolResult, _refuse
from app.tools.formatting import _format_reached_at
from app.tools.turns import _period_from_turn, _person_from_reach_out


class ListMailArgs(BaseModel):
    client_code: str | None = None
    direction: str = "inbound"
    email: str | None = None
    contact_id: int | None = None
    received_after: str | None = None
    received_before: str | None = None
    limit: int = 25

    @model_validator(mode="after")
    def _need_scope(self) -> ListMailArgs:
        if any(
            (value or "").strip()
            for value in (self.client_code, self.email, self.received_after, self.received_before)
        ) or self.contact_id is not None:
            return self
        raise ValueError("client_code, email, contact_id, or a received_after/received_before range is required")


class GetMailDetailArgs(BaseModel):
    mail_id: int


def answer_person_mail_question(source: FlowSource, turn: ChatTurn | None) -> ToolResult | None:
    """'When did {name} last reach out?' uses ticket requestor, not the contacts list.

    Several people can share a last name. The requestor field is the match.
    """
    name = _person_from_reach_out(turn)
    if not name:
        return None
    found: dict[int, TicketRecord] = {}
    for query in _requestor_queries(name):
        rows = source.list_tickets(requestor=query, stage="all", limit=100)
        for row in rows:
            if _requestor_matches(name, row.requestor_text):
                found[row.id] = row
    tickets = sorted(found.values(), key=lambda row: (row.last_activity_at, row.id), reverse=True)[:8]
    if not tickets:
        return ToolResult(
            tool=LIST_MAIL,
            source=source.source_name,
            data=[],
            row_ids=[],
            reply=f"No ticket has {name} as the requestor.",
        )
    seen: set[int] = set()
    mail_rows: list[MailRecord] = []
    for ticket in tickets:
        rows = source.list_mail(
            client_code=ticket.client_code,
            direction="inbound",
            ticket_num=ticket.ticket_num,
            limit=20,
        )
        for row in rows:
            if row.id in seen:
                continue
            seen.add(row.id)
            mail_rows.append(row)
    mail_rows.sort(key=lambda row: (row.received_at or row.created_at, row.id), reverse=True)
    named = [row for row in mail_rows if _requestor_matches(name, row.from_name)]
    pick = (named or mail_rows)[0] if mail_rows else None
    if pick is None:
        labels = ", ".join(ticket_label(row.client_code, row.ticket_num) for row in tickets[:5])
        who = (tickets[0].requestor_text or name).strip()
        return ToolResult(
            tool=LIST_MAIL,
            source=source.source_name,
            data=[_ticket_payload(row) for row in tickets],
            row_ids=[row.id for row in tickets],
            client_code=tickets[0].client_code.upper(),
            reply=f"{who} is the requestor on {labels}, but there is no inbound mail on those tickets.",
        )
    who = next(
        ((row.requestor_text or "").strip() for row in tickets if row.id == pick.ticket_id and row.requestor_text),
        None,
    ) or (pick.from_name or name).strip()
    when = _format_reached_at(pick.received_at or pick.created_at)
    subject = (pick.subject or "").strip() or "(no subject)"
    label = ticket_label(pick.client_code, pick.ticket_num)
    return ToolResult(
        tool=LIST_MAIL,
        source=source.source_name,
        data=[_mail_payload(pick)],
        row_ids=[pick.id],
        client_code=pick.client_code.upper(),
        note="Matched the ticket requestor field, then inbound mail on that ticket.",
        reply=f"{who} last reached out on {when} — {label}, {subject}.",
    )


_LAST_MAIL_RE = re.compile(r"\b(?:last|latest|most\s+recent|newest)\s+(?:e-?mail|mail|message)\b", re.IGNORECASE)
_FULL_MAIL_RE = re.compile(
    r"\b(?:full|whole|entire|complete)\s+(?:e-?mail|mail|message)\b|\be-?mail\s+body\b", re.IGNORECASE
)


def list_mail(source: FlowSource, args: ListMailArgs, turn: ChatTurn | None = None) -> ToolResult:
    period = _period_from_turn(turn)
    said = (turn.user_text if turn else "") or ""
    if period:
        args = args.model_copy(update={"received_after": period[0], "received_before": period[1]})
    elif _LAST_MAIL_RE.search(said) or _FULL_MAIL_RE.search(said):
        # 'last email from X' has no date range: the model's guess at one (today) hides the real answer
        args = args.model_copy(update={"received_after": None, "received_before": None, "limit": 1})
    client = (args.client_code or "").strip().upper() or None
    try:
        rows = source.list_mail(
            client_code=client,
            direction=args.direction,
            email=args.email,
            contact_id=args.contact_id,
            received_after=(args.received_after or "").strip() or None,
            received_before=(args.received_before or "").strip() or None,
            limit=args.limit,
        )
    except FlowRequestError as exc:
        return _refuse(LIST_MAIL, source, str(exc))
    if rows and _FULL_MAIL_RE.search(said):
        return get_mail_detail(source, GetMailDetailArgs(mail_id=rows[0].id))
    data = [_mail_payload(r) for r in rows]
    lines = [f"{len(data)} {args.direction} mail row(s) for {client or 'that search'}.", ""]
    if not data:
        lines.append("None found.")
    for row in data:
        who = (row.get("from_name") or row.get("from_address") or "unknown").strip()
        subj = (row.get("subject") or "").strip() or "(no subject)"
        when = row.get("received_at") or ""
        lines.append(f"- {row.get('ticket_label')} — {subj} — {who} ({when})")
    return ToolResult(
        tool=LIST_MAIL,
        source=source.source_name,
        data=data,
        row_ids=[r.id for r in rows],
        client_code=client,
        note="direction=inbound means from the client to TechBldrs (filed on a Flow ticket).",
        reply="\n".join(lines).rstrip(),
    )


def get_mail_detail(source: FlowSource, args: GetMailDetailArgs) -> ToolResult:
    try:
        row = source.get_mail(mail_id=args.mail_id)
    except FlowRequestError as exc:
        return _refuse(GET_MAIL_DETAIL, source, str(exc))
    data = _mail_detail_payload(row)
    who = (row.from_name or row.from_address or "unknown").strip()
    lines = [f"{data['ticket_label']} — {data['subject'] or '(no subject)'} from {who}", "", row.body or "(no body)"]
    if row.attachments:
        lines.append("")
        lines.append("Attachments: " + ", ".join(att.filename for att in row.attachments))
    return ToolResult(
        tool=GET_MAIL_DETAIL,
        source=source.source_name,
        data=data,
        row_ids=[row.id],
        client_code=row.client_code.upper(),
        reply="\n".join(lines).rstrip(),
    )
