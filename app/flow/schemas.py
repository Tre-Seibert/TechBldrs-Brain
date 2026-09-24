"""Pydantic shapes aligned with Flow app/models (read, don't guess).

Flow tables used here:
  clients      — client_code (unique), name
  contacts     — full_name, email_1/2/3, contact_type, client_id
  tickets      — ticket_num (4-char), subject envelope |CODE|NUM| {...} topic
  mail         — ticket-attached; direction inbound | outbound | imported
  users        — technicians: display_name, email, assignee_code (two letters)
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ContactType = Literal[
    "client",
    "inactive_client",
    "employee",
    "inactive_employee",
    "prospect",
    "vendor",
    "general",
]

MailDirection = Literal["inbound", "outbound", "imported"]


def ticket_label(client_code: str, ticket_num: str) -> str:
    """Display id used in Flow (e.g. WDON-1842). ticket_num is 4 chars."""
    return f"{(client_code or '').upper()}-{ticket_num}"


class ClientRecord(BaseModel):
    id: int
    client_code: str
    name: str | None = None
    full_company_name: str | None = None
    status: str | None = None
    m365_default_domain: str | None = None


class ContactRecord(BaseModel):
    id: int
    client_id: int | None = None
    client_code: str | None = None
    contact_type: str = "client"
    full_name: str
    file_as: str | None = None
    company_name: str | None = None
    job_title: str | None = None
    email_1: str | None = None
    email_2: str | None = None
    email_3: str | None = None
    business_phone: str | None = None
    mobile_phone: str | None = None
    is_active: bool = True


class TicketRecord(BaseModel):
    id: int
    client_id: int
    client_code: str
    ticket_num: str
    subject: str
    topic: str
    status: str = "New"
    category: str = "Normal"
    requestor_text: str = ""
    contact_id: int | None = None
    machine_name: str | None = None
    assignee_code: str | None = None
    complete: bool = False
    created_at: datetime
    last_activity_at: datetime
    closed_at: datetime | None = None

    @field_validator("subject", "topic", "requestor_text", "status", "category", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        # Flow can return null for legacy rows; the list tool treats that as blank.
        return "" if value is None else value

    @property
    def label(self) -> str:
        return ticket_label(self.client_code, self.ticket_num)


class TechnicianRecord(BaseModel):
    """Flow users row, allowlisted fields only (never entra_* tokens)."""

    id: int
    display_name: str
    email: str
    assignee_code: str | None = None
    role: str | None = None
    is_active: bool = True


class SimilarTicketPair(BaseModel):
    keep_ticket: TicketRecord
    absorb_ticket: TicketRecord
    score: float
    reasons: list[str] = Field(default_factory=list)
    merge_blocked: str | None = None


class MailRecord(BaseModel):
    id: int
    ticket_id: int
    client_code: str
    ticket_num: str
    direction: MailDirection
    from_address: str | None = None
    from_name: str | None = None
    to_label: str | None = None
    subject: str | None = None
    received_at: datetime | None = None
    created_at: datetime
    snippet: str = ""

    @property
    def ticket_label(self) -> str:
        return ticket_label(self.client_code, self.ticket_num)


class ToolEnvelope(BaseModel):
    ok: bool = True
    tool: str
    read_only: bool = True
    source: str
    data: object = None
    row_ids: list[int] = Field(default_factory=list)
    client_code: str | None = None
    error: str | None = None
    note: str | None = None
