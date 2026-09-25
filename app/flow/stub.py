from __future__ import annotations

import re
from datetime import datetime
from itertools import combinations
from typing import Any

from app.flow.fixtures import CLIENTS, CONTACTS, MAIL, TECHNICIANS, TICKETS
from app.flow.schemas import ContactRecord, MailRecord, SimilarTicketPair, TechnicianRecord, TicketRecord, ticket_label
from app.flow.source import FlowRequestError


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _parse_dt(raw: str | None) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _clamp_limit(limit: int, default: int = 25, maximum: int = 100) -> int:
    if limit <= 0:
        return default
    return min(limit, maximum)


def _topic_tokens(topic: str | None) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", _norm(topic)))


def _ticket_stage(ticket: TicketRecord) -> str:
    if ticket.archived or ticket.archived_at is not None:
        return "archived"
    if _norm(ticket.category) == "9 review":
        return "review"
    return "open"


def _last_name_token(name: str | None) -> str | None:
    parts = [part for part in re.split(r"\s+", (name or "").strip()) if part]
    if len(parts) >= 2 and len(parts[-1]) >= 4:
        return _norm(parts[-1])
    return None


def _person_hit(ticket: TicketRecord, *, contact_id: int | None, requestor: str | None) -> bool:
    if contact_id is None and not (requestor or "").strip():
        return True
    if contact_id is not None and ticket.contact_id == contact_id:
        return True
    names = [_norm(requestor)] if (requestor or "").strip() else []
    last = _last_name_token(requestor)
    if last:
        names.append(last)
    if contact_id is not None:
        contact = next((row for row in CONTACTS if row.id == contact_id), None)
        if contact is not None:
            names.append(_norm(contact.full_name))
            names.append(_norm(contact.file_as))
            last = _last_name_token(contact.full_name)
            if last:
                names.append(last)
    stored = " ".join(
        filter(None, (_norm(ticket.requestor_text), _norm(ticket.subject), _norm(ticket.topic)))
    )
    return any(name and (name in stored or stored in name) for name in names)


def _stage_match(ticket: TicketRecord, stage: str | None) -> bool:
    wanted = _norm(stage) or "live"
    current = _ticket_stage(ticket)
    if wanted == "all":
        return True
    if wanted == "live":
        return current in ("open", "review")
    return current == wanted


class StubFlowSource:
    """In-process fixtures shaped like Flow's brain API.

    Each instance holds its own ticket list so a lab merge never leaks into
    another instance (or another test).
    """

    source_name = "stub"

    def __init__(self) -> None:
        self._tickets: list[TicketRecord] = [ticket.model_copy() for ticket in TICKETS]

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
            last = _last_name_token(query)
            if needle in haystack or (last and last in _norm(contact.full_name) + " " + _norm(contact.file_as)):
                hits.append(contact)
            if len(hits) >= _clamp_limit(limit):
                break
        return hits

    def search_technician(self, *, query: str, limit: int = 25) -> list[TechnicianRecord]:
        needle = _norm(query)
        hits = [
            tech
            for tech in TECHNICIANS
            if tech.is_active
            and (
                not needle
                or needle == _norm(tech.assignee_code)
                or (
                    len(needle) >= 3
                    and (needle in _norm(tech.display_name) or needle in _norm(tech.email))
                )
            )
        ]
        # Same as Flow: an exact assignee-code hit outranks a display-name substring.
        hits.sort(key=lambda tech: (bool(needle) and _norm(tech.assignee_code) != needle, tech.display_name))
        return hits[: _clamp_limit(limit)]

    def list_tickets(
        self,
        *,
        client_code: str | None = None,
        assignee_code: str | None = None,
        query: str | None = None,
        contact_id: int | None = None,
        status: str | None = None,
        ticket_num: str | None = None,
        category: str | None = None,
        reason: str | None = None,
        complete: bool | None = None,
        project: bool | None = None,
        machine_name: str | None = None,
        invoice_num: str | None = None,
        job: str | None = None,
        cause: str | None = None,
        overdue: bool | None = None,
        due_before: str | None = None,
        due_after: str | None = None,
        created_before: str | None = None,
        created_after: str | None = None,
        last_activity_before: str | None = None,
        last_activity_after: str | None = None,
        requestor: str | None = None,
        stage: str | None = None,
        sort: str = "last_activity_at",
        order: str = "desc",
        limit: int = 100,
    ) -> list[TicketRecord]:
        code = _norm(client_code)
        assignee = _norm(assignee_code)
        needle = _norm(query)
        wanted_num = (ticket_num or "").strip()
        wanted_category = _norm(category)
        if wanted_category in ("urgent", "0 urgent"):
            wanted_category = "0 urgent"
        wanted_reason = _norm(reason)
        if wanted_reason in ("billable", "billable/new", "billable new"):
            wanted_reason = "billable/new"
        wanted_machine = _norm(machine_name)
        wanted_invoice = _norm(invoice_num)
        wanted_job = _norm(job)
        wanted_cause = _norm(cause)
        due_before_dt = _parse_dt(due_before)
        due_after_dt = _parse_dt(due_after)
        created_before_dt = _parse_dt(created_before)
        created_after_dt = _parse_dt(created_after)
        last_before_dt = _parse_dt(last_activity_before)
        last_after_dt = _parse_dt(last_activity_after)
        has_scope = any(
            (
                code,
                assignee,
                needle,
                wanted_num,
                wanted_category,
                wanted_reason,
                wanted_machine,
                wanted_invoice,
                wanted_job,
                wanted_cause,
                complete is not None,
                project is not None,
                overdue,
                due_before_dt,
                due_after_dt,
                created_before_dt,
                created_after_dt,
                last_before_dt,
                last_after_dt,
                contact_id is not None,
                (requestor or "").strip(),
            )
        )
        if not has_scope:
            raise FlowRequestError(
                "Flow 400: client_code, assignee_code, q, ticket_num, category, reason, or another ticket filter is required"
            )
        wanted_status = _norm(status)
        now = datetime.now()

        def text_hit(ticket: TicketRecord) -> bool:
            fields = (ticket.topic, ticket.subject, ticket.requestor_text, ticket.machine_name, ticket.ticket_num)
            return any(needle in _norm(value) for value in fields) or needle == _norm(ticket.label)

        def reason_hit(ticket: TicketRecord) -> bool:
            stored = _norm(ticket.reason)
            if not wanted_reason:
                return True
            return stored == wanted_reason or (wanted_reason == "billable/new" and stored == "billable/new")

        matches = [
            t
            for t in self._tickets
            if _stage_match(t, stage)
            and (not code or _norm(t.client_code) == code)
            and (not assignee or _norm(t.assignee_code) == assignee)
            and (not needle or text_hit(t))
            and _person_hit(t, contact_id=contact_id, requestor=requestor)
            and (not wanted_status or _norm(t.status) == wanted_status)
            and (not wanted_num or t.ticket_num == wanted_num)
            and (not wanted_category or _norm(t.category) == wanted_category)
            and reason_hit(t)
            and (complete is None or t.complete is complete)
            and (project is None or t.project is project)
            and (not wanted_machine or wanted_machine in _norm(t.machine_name))
            and (not wanted_invoice or wanted_invoice == _norm(t.invoice_num))
            and (not wanted_job or wanted_job == _norm(t.job))
            and (not wanted_cause or wanted_cause in _norm(t.cause))
            and (due_before_dt is None or (t.due_at is not None and t.due_at < due_before_dt))
            and (due_after_dt is None or (t.due_at is not None and t.due_at >= due_after_dt))
            and (created_before_dt is None or t.created_at < created_before_dt)
            and (created_after_dt is None or t.created_at >= created_after_dt)
            and (last_before_dt is None or t.last_activity_at < last_before_dt)
            and (last_after_dt is None or t.last_activity_at >= last_after_dt)
            and (not overdue or (t.due_at is not None and t.due_at < now and not t.complete))
        ]
        reverse = (order or "desc").lower() != "asc"
        matches.sort(key=lambda t: (t.last_activity_at, t.id), reverse=reverse)
        return matches[: _clamp_limit(limit, default=100)]

    def find_similar_tickets(
        self,
        *,
        ticket_id: int | None = None,
        client_code: str | None = None,
        assignee_code: str | None = None,
        status: str | None = None,
        stage: str | None = None,
        limit: int = 20,
    ) -> list[SimilarTicketPair]:
        """Lab stand-in for Flow's scorer: same client + same contact + overlapping topic."""
        code = _norm(client_code)
        assignee = _norm(assignee_code)
        wanted_status = _norm(status)
        pool = [
            t
            for t in self._tickets
            if _stage_match(t, stage or "open")
            and (not code or _norm(t.client_code) == code)
            and (not assignee or _norm(t.assignee_code) == assignee)
            and (not wanted_status or wanted_status == "all" or _norm(t.status) == wanted_status)
        ]
        pairs: list[SimilarTicketPair] = []
        for left, right in combinations(pool, 2):
            if left.client_id != right.client_id:
                continue
            if ticket_id is not None and ticket_id not in (left.id, right.id):
                continue
            a, b = _topic_tokens(left.topic), _topic_tokens(right.topic)
            overlap = len(a & b) / len(a | b) if a and b else 0.0
            if overlap < 0.5:
                continue
            reasons = ["similar_topic"]
            score = 5.0 * overlap
            if left.contact_id and left.contact_id == right.contact_id:
                reasons.append("same_contact")
                score += 2.0
            if ticket_id is not None:
                keep, absorb = (left, right) if left.id == ticket_id else (right, left)
            else:
                keep, absorb = sorted((left, right), key=lambda t: (t.created_at, t.id))
            pairs.append(
                SimilarTicketPair(keep_ticket=keep, absorb_ticket=absorb, score=round(score, 2), reasons=reasons)
            )
        pairs.sort(key=lambda pair: -pair.score)
        return pairs[: _clamp_limit(limit, default=20, maximum=50)]

    def merge_tickets(self, *, target_ticket_id: int, source_ticket_ids: list[int]) -> dict[str, Any]:
        by_id = {ticket.id: ticket for ticket in self._tickets}
        target = by_id.get(target_ticket_id)
        sources = [by_id.get(ticket_id) for ticket_id in source_ticket_ids]
        if target is None or any(source is None for source in sources) or not sources:
            raise FlowRequestError("Flow 400: Target or source ticket not found.")
        if any(source.client_id != target.client_id for source in sources):
            raise FlowRequestError("Flow 400: Tickets from different clients cannot be merged.")
        merged_ids = [source.id for source in sources]
        self._tickets = [ticket for ticket in self._tickets if ticket.id not in merged_ids]
        return {
            "ok": True,
            "status": "merged",
            "target_ticket_id": target.id,
            "target_ticket_label": target.label,
            "merged_source_ids": merged_ids,
            "merged_source_labels": [ticket_label(s.client_code, s.ticket_num) for s in sources],
            "counts": {
                "mail": sum(1 for mail in MAIL if mail.ticket_id in merged_ids),
                "time_entries": 0,
                "parts": 0,
            },
        }

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
            allowed_ticket_ids = {t.id for t in self._tickets if t.contact_id == contact_id}

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
