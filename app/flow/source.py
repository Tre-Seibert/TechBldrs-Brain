"""Flow adapters. The model never sees SQL.

Reads are SELECT-equivalent. The one write, merge_tickets, is only reached
through the confirm gate in app/tools/handlers.py and must carry a verified
actor on the HTTP adapter.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.flow.schemas import (
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
)


class FlowSource(Protocol):
    source_name: str

    def search_contact(
        self,
        *,
        query: str,
        client_code: str | None = None,
        limit: int = 25,
    ) -> list[ContactRecord]: ...

    def search_technician(self, *, query: str, limit: int = 25) -> list[TechnicianRecord]: ...

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
        needs_response: bool | None = None,
        unassigned: bool | None = None,
        stage: str | None = None,
        sort: str = "last_activity_at",
        order: str = "desc",
        limit: int = 100,
    ) -> list[TicketRecord]: ...

    def find_similar_tickets(
        self,
        *,
        ticket_id: int | None = None,
        client_code: str | None = None,
        assignee_code: str | None = None,
        status: str | None = None,
        stage: str | None = None,
        limit: int = 20,
    ) -> list[SimilarTicketPair]: ...

    def merge_tickets(self, *, target_ticket_id: int, source_ticket_ids: list[int]) -> dict[str, Any]: ...

    def list_mail(
        self,
        *,
        client_code: str | None = None,
        direction: str = "inbound",
        email: str | None = None,
        contact_id: int | None = None,
        ticket_id: int | None = None,
        ticket_num: str | None = None,
        received_after: str | None = None,
        received_before: str | None = None,
        limit: int = 25,
    ) -> list[MailRecord]: ...

    def get_ticket(self, *, ticket_id: int) -> TicketDetail: ...

    def get_mail(self, *, mail_id: int) -> MailDetail: ...

    def get_client(self, *, client_code: str) -> ClientDetail: ...

    def list_time_entries(
        self,
        *,
        ticket_id: int | None = None,
        client_code: str | None = None,
        tech_user_id: int | None = None,
        work_after: str | None = None,
        work_before: str | None = None,
        billable: bool | None = None,
        reviewed: bool | None = None,
        sort: str = "start_at",
        order: str = "desc",
        limit: int = 25,
    ) -> list[TimeEntryRecord]: ...

    def ticket_stats(
        self,
        *,
        entity: str = "tickets",
        group_by: str = "client",
        metric: str = "count",
        client_code: str | None = None,
        assignee_code: str | None = None,
        tech_user_id: int | None = None,
        stage: str = "all",
        direction: str = "inbound",
        billable: bool | None = None,
        reviewed: bool | None = None,
        invoiced: bool | None = None,
        after: str | None = None,
        before: str | None = None,
        exclude_alerts: bool = False,
        limit: int = 10,
    ) -> dict[str, Any]: ...

    def ticket_samples(
        self,
        *,
        client_code: str | None = None,
        after: str | None = None,
        before: str | None = None,
        stage: str = "all",
        interval: str = "month",
        per_interval: int = 10,
        exclude_alerts: bool = True,
    ) -> dict[str, Any]: ...

    def list_machines(
        self,
        *,
        client_code: str,
        sort: str = "machine_name",
        order: str = "asc",
        limit: int = 100,
    ) -> list[MachineRecord]: ...


class FlowNotConfigured(RuntimeError):
    """FLOW_MODE is http/db but credentials or the brain API are not wired."""


class FlowRequestError(RuntimeError):
    """Flow answered but refused the request (400/403/404). Message is Flow's own."""


class FlowWriteRefused(RuntimeError):
    """A write was refused before reaching Flow (no verified actor)."""
