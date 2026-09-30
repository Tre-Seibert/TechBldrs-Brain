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


class ClientDetail(ClientRecord):
    """Fuller client read: contract/balance/renewal fields. Never itglue passwords."""

    file_as: str | None = None
    account: str | None = None
    client_rating: str | None = None
    contract_minutes: int | None = None
    balance: int | None = None
    tot_tkt_time_0: str | None = None
    tot_tkt_time_1: str | None = None
    tot_tkt_time_2: str | None = None
    tot_tkt_time_3: str | None = None
    accounting_flag: str | None = None
    o365_email_enabled: bool = False
    o365_storage_enabled: bool = False
    business_address: str | None = None
    business_phone: str | None = None
    business_phone_2: str | None = None
    business_fax: str | None = None
    email: str | None = None
    web_page: str | None = None
    itglue_url: str | None = None
    m365_tenant_id: str | None = None
    support_renewal: str | None = None
    antivirus_renewal: str | None = None
    spam_filter_renewal: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


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
    stage: str | None = None
    requestor_text: str = ""
    contact_id: int | None = None
    machine_name: str | None = None
    assignee_code: str | None = None
    reason: str | None = None
    cause: str | None = None
    project: bool = False
    complete: bool = False
    invoice_num: str | None = None
    job: str | None = None
    created_at: datetime
    last_activity_at: datetime
    due_at: datetime | None = None
    closed_at: datetime | None = None
    archived: bool = False
    archived_at: datetime | None = None
    hrs_actual_total: float | None = None

    @field_validator("subject", "topic", "requestor_text", "status", "category", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        # Flow can return null for legacy rows; the list tool treats that as blank.
        return "" if value is None else value

    @property
    def label(self) -> str:
        return ticket_label(self.client_code, self.ticket_num)


class TicketDetail(TicketRecord):
    """Fuller ticket read: log/notes text and hours. list_tickets/latest_ticket stay lean."""

    hrs_duration: float | None = None
    hrs_first_touch: float | None = None
    hrs_estimate_total: float | None = None
    hrs_billable_total: float | None = None
    hrs_gratis_total: float | None = None
    log_text: str | None = None
    notes_text: str | None = None
    assignee_user_id: int | None = None
    updated_at: datetime | None = None


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


class MailAttachmentRecord(BaseModel):
    id: int
    filename: str
    content_type: str | None = None
    size_bytes: int = 0
    is_inline: bool = False
    content_id: str | None = None


class MailDetail(MailRecord):
    """Fuller mail read: full body + attachment metadata. list_mail stays snippet-only."""

    body: str | None = None
    approval: bool | None = None
    importance: str | None = None
    cc_label: str | None = None
    updated_at: datetime | None = None
    attachments: list[MailAttachmentRecord] = Field(default_factory=list)


class TimeEntryRecord(BaseModel):
    id: int
    ticket_id: int
    client_code: str
    ticket_num: str
    tech_user_id: int
    start_at: datetime | None = None
    end_at: datetime | None = None
    bill_start_at: datetime | None = None
    bill_end_at: datetime | None = None
    work_date: datetime | None = None
    actual_minutes: int = 0
    minutes: int = 0
    subject: str = ""
    body: str | None = None
    billable: bool = False
    gratis: bool = False
    communication_type: str | None = None
    quoted: bool = False
    reviewed: bool = False
    job: str | None = None
    invoice_num: str | None = None
    invoice_desc: str | None = None
    activity_tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime | None = None

    @property
    def ticket_label(self) -> str:
        return ticket_label(self.client_code, self.ticket_num)


class MachineRecord(BaseModel):
    id: int
    client_id: int
    client_code: str
    machine_name: str
    machine_support: str | None = None
    source: str | None = None
    web_remote_url: str | None = None
    last_seen_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


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
