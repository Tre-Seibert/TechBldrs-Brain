"""Flow records -> the plain dicts tools return as `data`."""

from __future__ import annotations

from typing import Any

from app.sources.flow.schemas import (
    ClientDetail,
    ContactRecord,
    MachineRecord,
    MailDetail,
    MailRecord,
    SimilarTicketPair,
    TechnicianRecord,
    TicketDetail,
    TicketRecord,
    TimeEntryRecord,
    ticket_label,
)


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


def _technician_payload(row: TechnicianRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "display_name": row.display_name,
        "email": row.email,
        "assignee_code": row.assignee_code,
        "role": row.role,
    }


def _derived_stage(category: str | None) -> str:
    return "review" if (category or "").strip().lower() == "9 review" else "open"


def _ticket_payload(row: TicketRecord) -> dict[str, Any]:
    payload = {
        "id": row.id,
        "client_id": row.client_id,
        "client_code": row.client_code,
        "ticket_num": row.ticket_num,
        "ticket_label": ticket_label(row.client_code, row.ticket_num),
        "subject": row.subject,
        "topic": row.topic,
        "status": row.status,
        "category": row.category,
        "stage": row.stage
        or ("archived" if (row.archived or row.archived_at) else _derived_stage(row.category)),
        "requestor_text": row.requestor_text,
        "contact_id": row.contact_id,
        "machine_name": row.machine_name,
        "assignee_code": row.assignee_code,
        "reason": row.reason,
        "cause": row.cause,
        "project": row.project,
        "complete": row.complete,
        "invoice_num": row.invoice_num,
        "job": row.job,
        "created_at": row.created_at.isoformat(sep=" "),
        "last_activity_at": row.last_activity_at.isoformat(sep=" "),
        "due_at": row.due_at.isoformat(sep=" ") if row.due_at else None,
        "closed_at": row.closed_at.isoformat(sep=" ") if row.closed_at else None,
    }
    if row.hrs_actual_total is not None:
        payload["hrs_actual_total"] = row.hrs_actual_total
    return payload


def _similar_payload(pair: SimilarTicketPair) -> dict[str, Any]:
    def brief(row: TicketRecord) -> dict[str, Any]:
        return {
            "id": row.id,
            "ticket_label": ticket_label(row.client_code, row.ticket_num),
            "topic": row.topic,
            "status": row.status,
            "category": row.category,
            "stage": row.stage or ("review" if (row.category or "").strip().lower() == "9 review" else "open"),
            "client_code": row.client_code,
            "assignee_code": row.assignee_code,
            "requestor_text": row.requestor_text,
            "machine_name": row.machine_name,
            "created_at": row.created_at.isoformat(sep=" "),
            "last_activity_at": row.last_activity_at.isoformat(sep=" "),
        }

    payload: dict[str, Any] = {
        "keep": brief(pair.keep_ticket),
        "absorb": brief(pair.absorb_ticket),
        "score": pair.score,
        "reasons": pair.reasons,
    }
    if pair.merge_blocked:
        payload["merge_blocked"] = pair.merge_blocked
    return payload


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


def _time_entry_payload(row: TimeEntryRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "ticket_id": row.ticket_id,
        "ticket_label": row.ticket_label,
        "client_code": row.client_code,
        "tech_user_id": row.tech_user_id,
        "work_date": row.work_date.isoformat(sep=" ") if row.work_date else None,
        "start_at": row.start_at.isoformat(sep=" ") if row.start_at else None,
        "end_at": row.end_at.isoformat(sep=" ") if row.end_at else None,
        "created_at": row.created_at.isoformat(sep=" ") if row.created_at else None,
        "actual_minutes": row.actual_minutes,
        "minutes": row.minutes,
        "subject": row.subject,
        "body": row.body,
        "billable": row.billable,
        "gratis": row.gratis,
        "job": row.job,
        "invoice_num": row.invoice_num,
        "invoice_desc": row.invoice_desc,
        "activity_tags": row.activity_tags,
    }


def _machine_payload(row: MachineRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "client_code": row.client_code,
        "machine_name": row.machine_name,
        "machine_support": row.machine_support,
        "source": row.source,
        "web_remote_url": row.web_remote_url,
        "last_seen_at": row.last_seen_at.isoformat(sep=" ") if row.last_seen_at else None,
    }


def _ticket_detail_payload(row: TicketDetail) -> dict[str, Any]:
    payload = _ticket_payload(row)
    payload.update(
        {
            "log_text": row.log_text,
            "notes_text": row.notes_text,
            "hrs_estimate_total": row.hrs_estimate_total,
            "hrs_actual_total": row.hrs_actual_total,
            "hrs_billable_total": row.hrs_billable_total,
            "hrs_gratis_total": row.hrs_gratis_total,
        }
    )
    return payload


def _mail_detail_payload(row: MailDetail) -> dict[str, Any]:
    payload = _mail_payload(row)
    payload.pop("snippet", None)
    payload["body"] = row.body
    payload["attachments"] = [
        {"filename": att.filename, "content_type": att.content_type, "size_bytes": att.size_bytes}
        for att in row.attachments
    ]
    return payload


def _client_detail_payload(row: ClientDetail) -> dict[str, Any]:
    return {
        "client_code": row.client_code,
        "name": row.name,
        "full_company_name": row.full_company_name,
        "status": row.status,
        "account": row.account,
        "client_rating": row.client_rating,
        "contract_minutes": row.contract_minutes,
        "balance": row.balance,
        "support_renewal": row.support_renewal,
        "antivirus_renewal": row.antivirus_renewal,
        "spam_filter_renewal": row.spam_filter_renewal,
        "business_phone": row.business_phone,
        "email": row.email,
        "web_page": row.web_page,
    }
