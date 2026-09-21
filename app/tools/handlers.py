from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.flow.schemas import ContactRecord, MailRecord, TicketRecord, ticket_label
from app.flow.source import FlowSource
from app.tools.registry import LATEST_TICKET, LIST_MAIL, SEARCH_CONTACT


class SearchContactArgs(BaseModel):
    query: str
    client_code: str | None = None
    limit: int = 25


class LatestTicketArgs(BaseModel):
    client_code: str
    contact_id: int | None = None
    status: str | None = None


class ListMailArgs(BaseModel):
    client_code: str
    direction: str = "inbound"
    email: str | None = None
    contact_id: int | None = None
    limit: int = 25


class ToolResult(BaseModel):
    ok: bool = True
    tool: str
    read_only: bool = True
    source: str
    data: Any = None
    row_ids: list[int] = Field(default_factory=list)
    client_code: str | None = None
    error: str | None = None
    note: str | None = None


def _contact_payload(row: ContactRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "client_id": row.client_id,
        "client_code": row.client_code,
        "contact_type": row.contact_type,
        "full_name": row.full_name,
        "file_as": row.file_as,
        "company_name": row.company_name,
        "job_title": row.job_title,
        "email_1": row.email_1,
        "email_2": row.email_2,
        "email_3": row.email_3,
        "business_phone": row.business_phone,
        "mobile_phone": row.mobile_phone,
        "is_active": row.is_active,
    }


def _ticket_payload(row: TicketRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "client_id": row.client_id,
        "client_code": row.client_code,
        "ticket_num": row.ticket_num,
        "ticket_label": ticket_label(row.client_code, row.ticket_num),
        "subject": row.subject,
        "topic": row.topic,
        "status": row.status,
        "category": row.category,
        "requestor_text": row.requestor_text,
        "contact_id": row.contact_id,
        "machine_name": row.machine_name,
        "assignee_code": row.assignee_code,
        "complete": row.complete,
        "created_at": row.created_at.isoformat(sep=" "),
        "last_activity_at": row.last_activity_at.isoformat(sep=" "),
        "closed_at": row.closed_at.isoformat(sep=" ") if row.closed_at else None,
    }


def _mail_payload(row: MailRecord) -> dict[str, Any]:
    received = row.received_at or row.created_at
    return {
        "id": row.id,
        "ticket_id": row.ticket_id,
        "client_code": row.client_code,
        "ticket_num": row.ticket_num,
        "ticket_label": ticket_label(row.client_code, row.ticket_num),
        "direction": row.direction,
        "from_address": row.from_address,
        "from_name": row.from_name,
        "to_label": row.to_label,
        "subject": row.subject,
        "received_at": received.isoformat(sep=" "),
        "snippet": row.snippet,
    }


def search_contact(source: FlowSource, args: SearchContactArgs) -> ToolResult:
    rows = source.search_contact(
        query=args.query,
        client_code=args.client_code,
        limit=args.limit,
    )
    codes = sorted({(r.client_code or "") for r in rows if r.client_code})
    client_code = codes[0] if len(codes) == 1 else (args.client_code or None)
    return ToolResult(
        tool=SEARCH_CONTACT,
        source=source.source_name,
        data=[_contact_payload(r) for r in rows],
        row_ids=[r.id for r in rows],
        client_code=(client_code or "").upper() or None,
        note="Follow with list_mail (inbound) or latest_ticket for last reach-out / last ticket.",
    )


def latest_ticket(source: FlowSource, args: LatestTicketArgs) -> ToolResult:
    row = source.latest_ticket(
        client_code=args.client_code,
        contact_id=args.contact_id,
        status=args.status,
    )
    if row is None:
        return ToolResult(
            tool=LATEST_TICKET,
            source=source.source_name,
            data=None,
            row_ids=[],
            client_code=args.client_code.upper(),
            note="No matching ticket.",
        )
    return ToolResult(
        tool=LATEST_TICKET,
        source=source.source_name,
        data=_ticket_payload(row),
        row_ids=[row.id],
        client_code=row.client_code.upper(),
    )


def list_mail(source: FlowSource, args: ListMailArgs) -> ToolResult:
    rows = source.list_mail(
        client_code=args.client_code,
        direction=args.direction,
        email=args.email,
        contact_id=args.contact_id,
        limit=args.limit,
    )
    return ToolResult(
        tool=LIST_MAIL,
        source=source.source_name,
        data=[_mail_payload(r) for r in rows],
        row_ids=[r.id for r in rows],
        client_code=args.client_code.upper(),
        note=(
            "Flow mail is ticket-attached. Graph is not called. "
            "Unfiled tenant mail is a later tool."
        ),
    )


def dispatch(source: FlowSource, name: str, raw_args: dict[str, Any]) -> ToolResult:
    if name == SEARCH_CONTACT:
        return search_contact(source, SearchContactArgs.model_validate(raw_args))
    if name == LATEST_TICKET:
        return latest_ticket(source, LatestTicketArgs.model_validate(raw_args))
    if name == LIST_MAIL:
        return list_mail(source, ListMailArgs.model_validate(raw_args))
    return ToolResult(
        ok=False,
        tool=name,
        source=source.source_name,
        error=f"Unknown tool {name!r}. Allowed: search_contact, latest_ticket, list_mail.",
    )
