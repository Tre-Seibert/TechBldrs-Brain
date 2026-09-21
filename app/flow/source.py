"""Read-only Flow adapters. The model never sees SQL."""

from __future__ import annotations

from typing import Protocol

from app.flow.schemas import ContactRecord, MailRecord, TicketRecord


class FlowSource(Protocol):
    source_name: str

    def search_contact(
        self,
        *,
        query: str,
        client_code: str | None = None,
        limit: int = 25,
    ) -> list[ContactRecord]: ...

    def latest_ticket(
        self,
        *,
        client_code: str,
        contact_id: int | None = None,
        status: str | None = None,
    ) -> TicketRecord | None: ...

    def list_mail(
        self,
        *,
        client_code: str,
        direction: str = "inbound",
        email: str | None = None,
        contact_id: int | None = None,
        limit: int = 25,
    ) -> list[MailRecord]: ...


class FlowNotConfigured(RuntimeError):
    """FLOW_MODE is http/db but credentials or the brain API are not wired."""
