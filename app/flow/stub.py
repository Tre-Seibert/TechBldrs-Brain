from __future__ import annotations

from app.flow.fixtures import CLIENTS, CONTACTS, MAIL, TICKETS
from app.flow.schemas import ContactRecord, MailRecord, TicketRecord


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _clamp_limit(limit: int, default: int = 25, maximum: int = 100) -> int:
    if limit <= 0:
        return default
    return min(limit, maximum)


class StubFlowSource:
    """In-process fixtures. Used until a read-only Flow brain API or DB user exists."""

    source_name = "stub"

    def search_contact(
        self,
        *,
        query: str,
        client_code: str | None = None,
        limit: int = 25,
    ) -> list[ContactRecord]:
        needle = _norm(query)
        if not needle:
            return []
        code = _norm(client_code)
        hits: list[ContactRecord] = []
        for contact in CONTACTS:
            if code and _norm(contact.client_code) != code:
                continue
            haystack = " ".join(
                filter(
                    None,
                    [
                        contact.full_name,
                        contact.file_as,
                        contact.company_name,
                        contact.email_1,
                        contact.email_2,
                        contact.email_3,
                        contact.job_title,
                    ],
                )
            ).lower()
            if needle in haystack:
                hits.append(contact)
            if len(hits) >= _clamp_limit(limit):
                break
        return hits

    def latest_ticket(
        self,
        *,
        client_code: str,
        contact_id: int | None = None,
        status: str | None = None,
    ) -> TicketRecord | None:
        code = _norm(client_code)
        if not code:
            return None
        wanted_status = _norm(status)
        matches = [
            t
            for t in TICKETS
            if _norm(t.client_code) == code
            and (contact_id is None or t.contact_id == contact_id)
            and (not wanted_status or _norm(t.status) == wanted_status)
        ]
        if not matches:
            return None
        matches.sort(key=lambda t: (t.last_activity_at, t.id), reverse=True)
        return matches[0]

    def list_mail(
        self,
        *,
        client_code: str,
        direction: str = "inbound",
        email: str | None = None,
        contact_id: int | None = None,
        limit: int = 25,
    ) -> list[MailRecord]:
        code = _norm(client_code)
        if not code:
            return []
        direction_norm = _norm(direction) or "inbound"
        email_norm = _norm(email)
        contact_emails: set[str] = set()
        if contact_id is not None:
            for contact in CONTACTS:
                if contact.id == contact_id:
                    contact_emails = {
                        _norm(contact.email_1),
                        _norm(contact.email_2),
                        _norm(contact.email_3),
                    } - {""}
                    break
        allowed_ticket_ids: set[int] | None = None
        if contact_id is not None:
            allowed_ticket_ids = {t.id for t in TICKETS if t.contact_id == contact_id}

        hits: list[MailRecord] = []
        for mail in MAIL:
            if _norm(mail.client_code) != code:
                continue
            if direction_norm not in ("all", "*") and _norm(mail.direction) != direction_norm:
                continue
            if email_norm and _norm(mail.from_address) != email_norm:
                continue
            if contact_id is not None:
                from_ok = _norm(mail.from_address) in contact_emails
                ticket_ok = allowed_ticket_ids is not None and mail.ticket_id in allowed_ticket_ids
                if not from_ok and not ticket_ok:
                    continue
            hits.append(mail)
        hits.sort(key=lambda m: (m.received_at or m.created_at, m.id), reverse=True)
        return hits[: _clamp_limit(limit)]

    def client_codes(self) -> list[str]:
        return [c.client_code for c in CLIENTS]
