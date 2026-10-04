from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

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
    ticket_label,
)
from app.flow.source import FlowRequestError, FlowSource
from app.identity import current_signed_in_email
from app.knowledge.source import KnowledgeSource
from app.tools.registry import (
    FIND_SIMILAR_TICKETS,
    GET_CLIENT_DETAIL,
    GET_MAIL_DETAIL,
    GET_TICKET_DETAIL,
    LATEST_TICKET,
    LIST_MACHINES,
    LIST_MAIL,
    LIST_TICKETS,
    LIST_TIME_ENTRIES,
    MERGE_TICKETS,
    SEARCH_CONTACT,
    SEARCH_KNOWLEDGE,
    SEARCH_TECHNICIAN,
    TICKET_STATS,
    TOOL_NAMES,
)

_ASSIGNEE_CODE_RE = re.compile(r"^[A-Za-z]{2}$")
_CLIENT_CODE_TOKEN_RE = re.compile(r"[A-Z]{3,8}")
# "... for BUCK", "... at client ZEBB": a client scope the pattern routers do not handle.
_CLIENT_SCOPE_RE = re.compile(r"\b(?:at|for|from|of|in)\s+(?:client\s+)?[A-Z]{3,8}\b")
_LABEL_RE = re.compile(r"^([A-Za-z0-9]+)-([A-Za-z0-9]+)$")
_SELF_ASSIGNEE = frozenset({"me", "my", "myself", "i"})
_OWN_TICKETS_RE = re.compile(r"\b(my|mine|assigned to me|i have)\b", re.IGNORECASE)
_TICKETS_FOR_RE = re.compile(r"\btickets?\b[^.?!\n]*\bfor\s+(?P<name>.+)", re.IGNORECASE)
_TICKET_PERSON_RES = (
    re.compile(
        r"\b(?:latest|last|most recent)\s+tickets?\b.*?\b(?:involving|for|from|about)\s+(?P<name>.+)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"\btickets?\b.*?\b(?:involving|from)\s+(?P<name>.+)", re.IGNORECASE | re.DOTALL),
    _TICKETS_FOR_RE,
)
_ABOUT_TICKET_RE = re.compile(
    r"\btickets?\b(?:\s+\w+){0,6}?\s+(?:about|regarding|related to|mentioning)\s+(?P<q>.+)",
    re.IGNORECASE,
)
_ABOUT_FILLER = frozenset(
    {
        "a",
        "an",
        "the",
        "any",
        "some",
        "feature",
        "features",
        "issue",
        "issues",
        "problem",
        "problems",
        "thing",
        "things",
        "stuff",
        "update",
        "updates",
        "ticket",
        "tickets",
        "related",
        "please",
    }
)
_LATEST_TICKET_RE = re.compile(r"\b(?:latest|last|most recent)\s+tickets?\b", re.IGNORECASE)
_MAIL_TURN_RE = re.compile(r"\b(emails?|mails?|reach out|inbox)\b", re.IGNORECASE)
_REACH_OUT_RES = (
    re.compile(
        r"\bwhen did\s+(?P<name>.+?)\s+last\s+(?:reach out|e-?mails?(?:\s+us)?|mails?(?:\s+us)?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwhen(?:'s| is| was)?\s+the last time\s+(?P<name>.+?)\s+(?:e-?mailed|reached out|mailed)(?:\s+us)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\blast\s+(?:e-?mail|mail|reach out)\s+from\s+(?P<name>.+?)(?:\?|$)",
        re.IGNORECASE,
    ),
)
_NEED_MERGED_RE = re.compile(
    r"\b(?:need(?:s|ed)?(?:\s+to\s+be)?\s+merged|duplicates?\s+to\s+merge|should\s+(?:be\s+)?merged)\b",
    re.IGNORECASE,
)
_MERGE_INTO_RE = re.compile(
    r"\bmerge\s+[A-Za-z0-9]+-[A-Za-z0-9]+\s+into\s+[A-Za-z0-9]+-[A-Za-z0-9]+\b",
    re.IGNORECASE,
)
_MERGE_SCOPE_RE = re.compile(
    r"\b(?:assigned\s+to|for|at)\s+([A-Za-z][A-Za-z0-9.'-]{1,30})\b",
    re.IGNORECASE,
)
_NOT_A_MERGE_SCOPE = frozenset(
    {"me", "my", "any", "the", "all", "open", "our", "us", "to", "be", "a", "an", "some", "tickets", "ticket"}
)
_PERSON_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z.'-]*$")
_APOSTROPHE_RE = re.compile(r"['\u2019\u2018\u02bc`´]")
_URGENT_RE = re.compile(r"\burgent\b", re.IGNORECASE)
_BILLABLE_RE = re.compile(r"\bbillable\b", re.IGNORECASE)
_OVERDUE_RE = re.compile(r"\boverdue\b", re.IGNORECASE)
_RANK_WORD_RE = re.compile(r"\b(?:longest|most|highest|greatest)\b", re.IGNORECASE)
_WORK_AMOUNT_WORD_RE = re.compile(r"\b(?:time|hours?|worked|logged)\b", re.IGNORECASE)
_TICKET_WORD_RE = re.compile(r"\btickets?\b", re.IGNORECASE)
# "last week", "this month", "yesterday": a time-bounded question the all-time ranking cannot answer.
_DATE_RANGE_RE = re.compile(
    r"\b(?:(?:last|this|past|previous|current)\s+(?:week|month|quarter|year|\d+\s+(?:days?|weeks?|months?))"
    r"|yesterday|today|since|between|(?:in|during|for)\s+(?:january|february|march|april|may|june|july|"
    r"august|september|october|november|december))\b",
    re.IGNORECASE,
)
_ORDINAL_WORD_RE = re.compile(
    r"\b(?:next|another|second|2nd|third|3rd|fourth|4th|fifth|5th)\b", re.IGNORECASE
)


def _is_longest_time_question(text: str) -> bool:
    """Word-order-independent on purpose: "longest time worked", "most hours logged",
    and "longest ticket worked" all mean the same thing here, in any order. An earlier,
    order-dependent regex kept missing real phrasings (each fix just uncovered the next
    one) because natural language doesn't commit to one fixed word order."""
    return bool(
        _RANK_WORD_RE.search(text) and _WORK_AMOUNT_WORD_RE.search(text) and _TICKET_WORD_RE.search(text)
    )
# Short, vague continuations ("How about after that?") that only mean anything because
# the previous reply was specifically a longest-time-worked ranking -- checked separately
# from the ordinal+rank-word follow-up check below and length-capped so a longer,
# unrelated message that happens to contain e.g. "next" isn't misread as a follow-up.
_VAGUE_CONTINUATION_RE = re.compile(
    r"\b(?:after that|and then|then what|next one|another one|keep going|what'?s next|and the next)\b",
    re.IGNORECASE,
)
_STAGE_WORD_RE = re.compile(r"\b(?:open|archived|review)\b", re.IGNORECASE)
_SINGLE_RESULT_RE = re.compile(
    r"\b(?:only|just)\b.{0,20}\b(?:one|1|single)\b|\bonly\s+the\s+longest\b|\bsingle\b",
    re.IGNORECASE,
)
_TOP_N_RE = re.compile(r"\btop\s+(\d{1,2})\b", re.IGNORECASE)


def _longest_time_page_size(text: str, default: int) -> int:
    """How many ranked tickets to show: an explicit "only one"/"top N" in the
    question wins over the default, so the deterministic bypass doesn't ignore
    an explicit count the way it ignored other qualifiers before."""
    if _SINGLE_RESULT_RE.search(text):
        return 1
    match = _TOP_N_RE.search(text)
    if match:
        return max(1, min(int(match.group(1)), 20))
    return default
_TICKET_LABEL_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,8}-[A-Z0-9]{3,6}\b")
_LIST_STAGES = frozenset({"open", "review", "live", "archived", "all"})
INTERNAL_CLIENT_CODES = ("ZTB", "ZINT", "ZAWE", "ZFRIENDS")
_INCOMPLETE_RE = re.compile(r"\b(incomplete|not complete|not completed)\b", re.IGNORECASE)
_CATEGORY_ALIASES = {
    "urgent": "0 Urgent",
    "0 urgent": "0 Urgent",
    "high": "1 High",
    "1 high": "1 High",
    "re-opened": "1 Re-Opened",
    "reopened": "1 Re-Opened",
    "normal": "2 Normal",
    "2 normal": "2 Normal",
    "follow up": "3 Follow Up",
    "follow-up": "3 Follow Up",
    "waiting": "4 Waiting",
    "4 waiting": "4 Waiting",
    "on-site": "5 On-Site",
    "onsite": "5 On-Site",
    "project": "6 Project",
    "6 project": "6 Project",
}
_REASON_ALIASES = {
    "billable": "Billable/New",
    "billable/new": "Billable/New",
    "billable new": "Billable/New",
    "support": "Support",
    "internal": "Internal",
    "resolved": "Resolved",
    "admin": "Admin",
    "alert": "Alert",
}
_UNKNOWN_SELF = (
    "I don't know who you are signed in as. Sign in with your Flow email, or name a technician."
)


@dataclass(frozen=True)
class ChatTurn:
    """What the user just said, and what the assistant said right before it.

    The merge gate reads this, never the model's arguments alone: a write
    needs the plan shown first and then restated by the human.
    """

    user_text: str = ""
    previous_assistant_text: str = ""


class SearchContactArgs(BaseModel):
    query: str
    client_code: str | None = None
    limit: int = 25


class SearchTechnicianArgs(BaseModel):
    query: str = ""
    limit: int = 25


class LatestTicketArgs(BaseModel):
    client_code: str | None = None
    contact_id: int | None = None
    requestor: str | None = None
    status: str | None = None


class ListTicketsArgs(BaseModel):
    assignee_code: str | None = None
    client_code: str | None = None
    q: str | None = None
    ticket_num: str | None = None
    status: str | None = None
    category: str | None = None
    contact_id: int | None = None
    reason: str | None = None
    complete: bool | None = None
    project: bool | None = None
    machine_name: str | None = None
    invoice_num: str | None = None
    job: str | None = None
    cause: str | None = None
    overdue: bool | None = None
    due_before: str | None = None
    due_after: str | None = None
    created_before: str | None = None
    created_after: str | None = None
    last_activity_before: str | None = None
    last_activity_after: str | None = None
    requestor: str | None = None
    needs_response: bool | None = None
    unassigned: bool | None = None
    stage: str | None = None
    sort: str | None = None
    order: str | None = None
    limit: int = 100

    @model_validator(mode="after")
    def _need_scope(self) -> ListTicketsArgs:
        if any(
            (value or "").strip()
            for value in (
                self.assignee_code,
                self.client_code,
                self.q,
                self.ticket_num,
                self.category,
                self.reason,
                self.machine_name,
                self.invoice_num,
                self.job,
                self.cause,
                self.due_before,
                self.due_after,
                self.created_before,
                self.created_after,
                self.last_activity_before,
                self.last_activity_after,
                self.requestor,
            )
        ):
            return self
        if any(
            value is not None
            for value in (self.contact_id, self.complete, self.project)
        ) or self.overdue or self.needs_response or self.unassigned:
            return self
        if (self.sort or "").strip():
            return self
        if (self.stage or "").strip().lower() in _LIST_STAGES:
            return self
        raise ValueError(
            "assignee_code, client_code, q, ticket_num, category, reason, or another ticket filter is required"
        )


class FindSimilarTicketsArgs(BaseModel):
    client_code: str | None = None
    assignee_code: str | None = None
    ticket_id: int | None = None
    status: str | None = None
    stage: str | None = None
    limit: int = 20


class MergeTicketsArgs(BaseModel):
    target_ticket_id: int
    source_ticket_ids: list[int] = Field(min_length=1)
    target_label: str | None = None
    source_labels: list[str] = Field(default_factory=list)
    confirm: bool = False


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


class ListTimeEntriesArgs(BaseModel):
    ticket_id: int | None = None
    ticket_label: str | None = None
    client_code: str | None = None
    assignee_code: str | None = None
    work_after: str | None = None
    work_before: str | None = None
    billable: bool | None = None
    reviewed: bool | None = None
    limit: int = 25

    @model_validator(mode="after")
    def _need_scope(self) -> ListTimeEntriesArgs:
        if (
            self.ticket_id is None
            and self.reviewed is None
            and not any(
                (value or "").strip()
                for value in (
                    self.ticket_label,
                    self.client_code,
                    self.assignee_code,
                    self.work_after,
                    self.work_before,
                )
            )
        ):
            raise ValueError(
                "ticket_id, client_code, assignee_code, a work_after/work_before range, or reviewed is required"
            )
        return self


_STATS_DATE_ALIASES = {
    "after": ("work_after", "received_after", "created_after", "date_from", "start", "from_date", "since"),
    "before": ("work_before", "received_before", "created_before", "date_to", "end", "to_date", "until"),
}


class TicketStatsArgs(BaseModel):
    entity: str = "tickets"
    group_by: str = "client"
    metric: str = "count"
    client_code: str | None = None
    assignee_code: str | None = None
    stage: str = "all"
    direction: str = "inbound"
    billable: bool | None = None
    after: str | None = None
    before: str | None = None
    limit: int = 10

    @model_validator(mode="before")
    @classmethod
    def _date_aliases(cls, data: Any) -> Any:
        """Models reuse list_time_entries' work_after/work_before here; a silently dropped date
        range would answer an all-time question instead of 'this month'."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for canonical, aliases in _STATS_DATE_ALIASES.items():
            if not data.get(canonical):
                for alias in aliases:
                    if data.get(alias):
                        data[canonical] = data[alias]
                        break
        return data


class ListMachinesArgs(BaseModel):
    client_code: str
    limit: int = 100


class GetTicketDetailArgs(BaseModel):
    ticket_id: int | None = None
    ticket_label: str | None = None

    @model_validator(mode="after")
    def _need_one(self) -> GetTicketDetailArgs:
        if self.ticket_id is None and not (self.ticket_label or "").strip():
            raise ValueError("ticket_label (e.g. ACME-0041) or ticket_id is required")
        return self


class GetMailDetailArgs(BaseModel):
    mail_id: int


class GetClientDetailArgs(BaseModel):
    client_code: str


class SearchKnowledgeArgs(BaseModel):
    query: str
    client_code: str | None = None
    limit: int = 5


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
    reply: str | None = None


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
        "stage": row.stage,
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


def _refuse(
    tool: str,
    source: FlowSource,
    error: str,
    *,
    client_code: str | None = None,
    reply: str | None = None,
) -> ToolResult:
    return ToolResult(
        ok=False,
        tool=tool,
        source=source.source_name,
        client_code=client_code,
        error=error,
        reply=reply or error,
    )


def format_ticket_list(rows: list[dict[str, Any]], *, heading: str, note: str | None = None) -> str:
    if not rows:
        cleaned = heading.rstrip(".")
        if cleaned.lower().startswith("no "):
            return cleaned + "."
        return cleaned + ". None found."
    lines = [heading.rstrip("."), ""]
    for row in rows:
        label = row.get("ticket_label") or "?"
        topic = (row.get("topic") or row.get("subject") or "").strip() or "(no topic)"
        status = (row.get("status") or "").strip() or "unknown status"
        category = (row.get("category") or "").strip() or "uncategorized"
        client = (row.get("client_code") or "").strip()
        assignee = (row.get("assignee_code") or "").strip() or "unassigned"
        reason = (row.get("reason") or "").strip()
        extra = f"{client} · {status} · {category} · {assignee}" if client else f"{status} · {category} · {assignee}"
        if reason:
            extra = f"{extra} · {reason}"
        hours = row.get("hrs_actual_total")
        if isinstance(hours, (int, float)):
            extra = f"{extra} · {_format_hours(float(hours))}"
        lines.append(f"- {label} — {topic} ({extra})")
    if note:
        lines.extend(["", note])
    return "\n".join(lines)


_REASON_WORDS = {
    "similar_topic": "same topic",
    "similar_subject": "same subject",
    "same_contact": "same contact",
    "same_machine": "same machine",
    "same_requestor": "same requestor",
}


def _ticket_topic(row: dict[str, Any]) -> str:
    return (row.get("topic") or row.get("subject") or "").strip() or "(no topic)"


def _ticket_who(row: dict[str, Any]) -> str:
    return (row.get("requestor_text") or "").strip() or "unknown"


def _ticket_assignee(row: dict[str, Any]) -> str:
    return (row.get("assignee_code") or "").strip() or "unassigned"


def _similar_why(pair: dict[str, Any]) -> str:
    keep = pair.get("keep") or {}
    absorb = pair.get("absorb") or {}
    bits = [_REASON_WORDS.get(code, code) for code in (pair.get("reasons") or [])]
    keep_who, absorb_who = _ticket_who(keep), _ticket_who(absorb)
    if keep_who.lower() != absorb_who.lower() and "same requestor" not in bits:
        bits.append(f"different requestors ({keep_who} vs {absorb_who})")
    elif keep_who != "unknown" and "same requestor" in bits:
        bits = [f"same requestor ({keep_who})" if bit == "same requestor" else bit for bit in bits]
    keep_asg, absorb_asg = _ticket_assignee(keep), _ticket_assignee(absorb)
    if keep_asg != absorb_asg:
        bits.append(f"assignees {keep_asg} vs {absorb_asg}")
    keep_machine = (keep.get("machine_name") or "").strip()
    absorb_machine = (absorb.get("machine_name") or "").strip()
    if keep_machine and absorb_machine and keep_machine.lower() != absorb_machine.lower():
        bits.append(f"machines {keep_machine} vs {absorb_machine}")
    return ". ".join(bit[:1].upper() + bit[1:] for bit in bits) + "." if bits else "Similar tickets."


def format_similar_list(pairs: list[dict[str, Any]], *, heading: str) -> str:
    if not pairs:
        return heading
    lines = [heading.rstrip("."), ""]
    for pair in pairs:
        keep = pair.get("keep") or {}
        absorb = pair.get("absorb") or {}
        keep_label = keep.get("ticket_label") or "?"
        absorb_label = absorb.get("ticket_label") or "?"
        keep_topic = _ticket_topic(keep)
        absorb_topic = _ticket_topic(absorb)
        title = keep_topic if keep_topic.lower() == absorb_topic.lower() else f"{keep_topic} / {absorb_topic}"
        lines.append(f"Keep {keep_label}, absorb {absorb_label} — {title}")
        lines.append(_similar_why(pair))
        if pair.get("merge_blocked"):
            lines.append(f"Cannot merge: {pair['merge_blocked']}")
        else:
            lines.append(f"To merge: merge {absorb_label} into {keep_label}")
        lines.append("")
    lines.append("Nothing was merged. Reply with a To merge line to confirm.")
    return "\n".join(lines).rstrip()


def _is_self_token(text: str | None) -> bool:
    return (text or "").strip().lower() in _SELF_ASSIGNEE


def _asked_own_tickets(turn: ChatTurn | None) -> bool:
    text = turn.user_text if turn else ""
    return bool(_OWN_TICKETS_RE.search(text or ""))


def _looks_like_person_name(text: str | None) -> bool:
    parts = [part for part in re.split(r"\s+", (text or "").strip()) if part]
    return len(parts) >= 2 and all(_PERSON_TOKEN_RE.match(part) for part in parts)


def _fold_apostrophes(text: str | None) -> str:
    """O'Brien, O’Brien, and OBrien are the same name."""
    return _APOSTROPHE_RE.sub("", text or "")


def _name_parts(text: str | None) -> tuple[str, str]:
    raw = _fold_apostrophes(text).strip()
    if "," in raw:
        last, _, rest = raw.partition(",")
        first = rest.strip().split()[0] if rest.strip() else ""
        last = last.strip()
        if last and first:
            return first, last
    parts = [part for part in re.split(r"\s+", raw) if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def _damerau(left: str, right: str) -> int:
    """Edit distance including adjacent transpositions (Micahel → Michael)."""
    a, b = left.lower(), right.lower()
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    prev_prev = list(range(len(b) + 1))
    prev = [1] + [0] * len(b)
    for j in range(1, len(b) + 1):
        prev[j] = prev_prev[j - 1] if a[0] == b[j - 1] else 1 + min(prev_prev[j], prev[j - 1], prev_prev[j - 1])
    for i in range(2, len(a) + 1):
        current = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            current[j] = min(prev[j] + 1, current[j - 1] + 1, prev[j - 1] + cost)
            if j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                current[j] = min(current[j], prev_prev[j - 2] + 1)
        prev_prev, prev = prev, current
    return prev[len(b)]


def _contact_name_score(query: str, stored: str) -> int | None:
    """Lower is closer. None means do not treat this as the same person."""
    q_first, q_last = _name_parts(query)
    n_first, n_last = _name_parts(stored)
    if q_last and n_last and q_last.lower() == n_last.lower():
        if not q_first:
            return 2
        distance = _damerau(q_first, n_first)
        return distance if distance <= 2 else None
    if q_first and n_first and not q_last:
        distance = _damerau(q_first, n_first)
        return distance if distance <= 1 else None
    return None


def _clean_person_name(name: str | None) -> str | None:
    text = (name or "").strip(" ?.!,")
    if not text or _is_self_token(text) or _OWN_TICKETS_RE.search(text):
        return None
    if _LABEL_RE.match(text) or _ASSIGNEE_CODE_RE.match(text):
        return None
    if re.fullmatch(r"[A-Za-z]{2,8}", text) and text.isupper():
        return None
    tokens = text.split()
    # A client code ("BLMC", "client ZZZZ") or a long clause is a filter, not a person.
    if len(tokens) > 4 or any(_CLIENT_CODE_TOKEN_RE.fullmatch(token) for token in tokens):
        return None
    if tokens[0].lower() == "client":
        return None
    return text


def _person_name_from_turn(turn: ChatTurn | None) -> str | None:
    text = turn.user_text if turn else ""
    for pattern in _TICKET_PERSON_RES:
        match = pattern.search(text or "")
        if match:
            name = _clean_person_name(match.group("name"))
            if name:
                return name
    return None


def _asked_tickets_for_person(turn: ChatTurn | None) -> bool:
    return _person_name_from_turn(turn) is not None


def _asked_latest_for_person(turn: ChatTurn | None) -> bool:
    text = turn.user_text if turn else ""
    return bool(text and _LATEST_TICKET_RE.search(text) and _person_name_from_turn(turn))


def _asked_mail_for_person(turn: ChatTurn | None) -> bool:
    text = turn.user_text if turn else ""
    return bool(text and _MAIL_TURN_RE.search(text))


def _person_from_reach_out(turn: ChatTurn | None) -> str | None:
    """Name in 'when did Sean O'Brien last reach out / last email us'."""
    text = turn.user_text if turn else ""
    if not text:
        return None
    for pattern in _REACH_OUT_RES:
        match = pattern.search(text)
        if not match:
            continue
        name = _clean_person_name(match.group("name"))
        if name and _usable_person_name(name):
            return name
    return None


def _usable_person_name(name: str) -> bool:
    folded = _fold_apostrophes(name)
    if _looks_like_person_name(folded):
        return True
    return bool(_PERSON_TOKEN_RE.match(folded)) and len(folded) >= 3


def _requestor_matches(query: str, stored: str | None) -> bool:
    """Full-name match on a ticket requestor. A shared last name is not enough."""
    if not (stored or "").strip():
        return False
    score = _contact_name_score(query, stored or "")
    return score is not None and score <= 1


def _requestor_queries(name: str) -> list[str]:
    """Spellings Flow's ILIKE can hit: straight apostrophe, stripped, and O'Brien."""
    straight = _APOSTROPHE_RE.sub("'", name.strip())
    stripped = straight.replace("'", "")
    curly = straight.replace("'", "\u2019")
    variants = [straight]
    if curly not in variants:
        variants.append(curly)
    if stripped and stripped not in variants:
        variants.append(stripped)
    parts = stripped.split()
    if len(parts) >= 2 and re.fullmatch(r"O[A-Za-z]{2,}", parts[-1]):
        irish = " ".join([*parts[:-1], f"O'{parts[-1][1:]}"])
        if irish not in variants:
            variants.append(irish)
    return variants


def _format_reached_at(value: datetime) -> str:
    hour = value.hour % 12 or 12
    ampm = "AM" if value.hour < 12 else "PM"
    return f"{value.strftime('%b')} {value.day}, {value.year} {hour}:{value.minute:02d} {ampm}"


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


def answer_person_ticket_question(source: FlowSource, turn: ChatTurn | None) -> ToolResult | None:
    """Bypass the LLM for 'tickets for / latest ticket involving {person}'."""
    if not _asked_tickets_for_person(turn) or _asked_mail_for_person(turn):
        return None
    name = _person_name_from_turn(turn)
    if not name:
        return None
    return search_contact(source, SearchContactArgs(query=name), turn)


def _search_contacts_loose(source: FlowSource, person: str) -> list:
    matches = source.search_contact(query=person, limit=10)
    scored = [row for row in matches if _best_contact_score(person, row) is not None]
    if scored:
        return matches
    last = _name_parts(person)[1]
    if last and len(last) >= 4:
        return source.search_contact(query=last, limit=10)
    return matches


def _best_contact_score(person: str, row) -> int | None:
    scores = [
        score
        for label in (row.full_name, row.file_as)
        if label
        for score in [_contact_name_score(person, label)]
        if score is not None
    ]
    return min(scores) if scores else None


def _pick_contact(matches: list, person: str):
    wanted = person.strip().lower()
    exact = [row for row in matches if (row.full_name or "").strip().lower() == wanted]
    if exact:
        return exact[0], None
    ranked = sorted(
        ((score, row) for row in matches if (score := _best_contact_score(person, row)) is not None),
        key=lambda item: (item[0], item[1].full_name or ""),
    )
    if ranked:
        best_score, best = ranked[0]
        tied = [row for score, row in ranked if score == best_score]
        if len({row.id for row in tied}) == 1:
            return best, None
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, None
    options = ", ".join(
        f"{row.full_name} ({row.client_code})" for row in matches if row.full_name
    )
    return None, f"{person!r} matches more than one contact: {options}. Ask which one."


def _resolve_category(raw: str | None) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    return _CATEGORY_ALIASES.get(text.lower(), text)


def _resolve_reason(raw: str | None) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    return _REASON_ALIASES.get(text.lower(), text)


def _alias_from_query(query: str | None, aliases: dict[str, str]) -> tuple[str | None, str | None]:
    text = (query or "").strip()
    if not text:
        return None, None
    mapped = aliases.get(text.lower())
    if mapped:
        return mapped, None
    return None, text


def _category_from_query(query: str | None) -> tuple[str | None, str | None]:
    return _alias_from_query(query, _CATEGORY_ALIASES)


def _reason_from_query(query: str | None) -> tuple[str | None, str | None]:
    return _alias_from_query(query, _REASON_ALIASES)


def _category_from_turn(turn: ChatTurn | None) -> str | None:
    text = turn.user_text if turn else ""
    if text and _URGENT_RE.search(text):
        return "0 Urgent"
    return None


def _reason_from_turn(turn: ChatTurn | None) -> str | None:
    text = turn.user_text if turn else ""
    if text and _BILLABLE_RE.search(text):
        return "Billable/New"
    return None


def _overdue_from_turn(turn: ChatTurn | None) -> bool:
    text = turn.user_text if turn else ""
    return bool(text and _OVERDUE_RE.search(text))


def _complete_from_turn(turn: ChatTurn | None) -> bool | None:
    text = turn.user_text if turn else ""
    if text and _INCOMPLETE_RE.search(text):
        return False
    return None


def _signed_in_technician(source: FlowSource) -> tuple[TechnicianRecord | None, str | None]:
    email = (current_signed_in_email.get() or "").strip().lower()
    if not email:
        return None, _UNKNOWN_SELF
    matches = source.search_technician(query=email, limit=5)
    exact = [row for row in matches if (row.email or "").strip().lower() == email]
    pick = exact[0] if exact else (matches[0] if len(matches) == 1 else None)
    if pick and pick.assignee_code:
        return pick, None
    return None, f"No Flow technician uses the signed-in email {email}."


def _resolve_assignee(source: FlowSource, raw: str | None) -> tuple[str | None, str | None]:
    """Return (assignee_code, error). A two-letter code passes through; a name or email is
    resolved through search_technician — never a hardcoded alias table.

    'me' / 'my' / 'myself' / 'i' is the signed-in Flow email, not a name search.
    Those tokens are also valid two-letter codes, so self-check comes first.
    """
    text = (raw or "").strip()
    if not text:
        return None, None
    if _is_self_token(text):
        tech, error = _signed_in_technician(source)
        if error:
            return None, error
        return (tech.assignee_code or "").lower(), None
    if _ASSIGNEE_CODE_RE.match(text):
        return text.lower(), None
    matches = source.search_technician(query=text, limit=10)
    codes = sorted({(m.assignee_code or "").lower() for m in matches if m.assignee_code})
    if len(codes) == 1:
        return codes[0], None
    if not codes:
        return None, f"No active technician matches {text!r}. Try search_technician with another spelling."
    options = ", ".join(f"{m.display_name} ({m.assignee_code})" for m in matches if m.assignee_code)
    return None, f"{text!r} matches more than one technician: {options}. Ask which one."


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
        stage = "live" if _asked_latest_for_person(turn) else "open"
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


def _default_list_stage(
    args: ListTicketsArgs,
    *,
    assignee_code: str | None,
    query: str | None,
    category: str | None,
    reason: str | None = None,
    overdue: bool | None = None,
    complete: bool | None = None,
    project: bool | None = None,
) -> str:
    """Assigned-to and column lists are Open unless review/archived/all was asked."""
    wanted = (args.stage or "").strip().lower()
    column_scope = bool(
        assignee_code
        or category
        or reason
        or overdue
        or complete is not None
        or project is not None
        or (args.machine_name or "").strip()
        or (args.invoice_num or "").strip()
        or (args.job or "").strip()
        or (args.cause or "").strip()
        or args.contact_id is not None
        or (args.requestor or "").strip()
        or args.needs_response
        or args.unassigned
    )
    scoped = column_scope and not any(
        (value or "").strip() for value in (args.client_code, query, args.ticket_num)
    )
    if scoped and wanted not in ("review", "archived", "all"):
        return "open"
    if not wanted and (args.sort or "").strip() == "hrs_actual_total":
        return "open"
    return wanted or "live"


def list_tickets(
    source: FlowSource,
    args: ListTicketsArgs,
    turn: ChatTurn | None = None,
    *,
    force_text_query: bool = False,
) -> ToolResult:
    codes = _client_codes(args.client_code)
    if len(codes) > 1:
        return _list_tickets_multi(source, args, turn, codes)
    raw_assignee = args.assignee_code
    raw_q = (args.q or "").strip() or None
    if not (raw_assignee or "").strip() and _is_self_token(raw_q):
        raw_assignee = "me"
        raw_q = None
    if (
        not (raw_assignee or "").strip()
        and _asked_own_tickets(turn)
        and not any(
            (value or "").strip()
            for value in (args.client_code, raw_q, args.ticket_num, args.requestor, args.machine_name)
        )
        and args.contact_id is None
    ):
        raw_assignee = "me"  # "my urgent tickets": the model dropped the assignee
    category = _resolve_category(args.category)
    model_said_overdue = False
    if category and category.strip().lower() in ("overdue", "past due", "late"):
        category, model_said_overdue = None, True  # overdue is a due-date filter, not a category
    if not category:
        from_q, raw_q = _category_from_query(raw_q)
        category = from_q
    if not category:
        category = _category_from_turn(turn)
    reason = _resolve_reason(args.reason)
    if not reason:
        from_q, raw_q = _reason_from_query(raw_q)
        reason = from_q
    if not reason:
        reason = _reason_from_turn(turn)
    overdue = args.overdue
    if overdue is None and (model_said_overdue or _overdue_from_turn(turn)):
        overdue = True
    complete = args.complete
    if complete is None:
        complete = _complete_from_turn(turn)
    column_asked = bool(
        category
        or reason
        or overdue
        or complete is not None
        or args.project is not None
        or (args.machine_name or "").strip()
        or (args.invoice_num or "").strip()
        or (args.job or "").strip()
        or (args.cause or "").strip()
        or args.contact_id is not None
        or (args.requestor or "").strip()
    )
    if column_asked and _is_self_token(raw_assignee) and not _asked_own_tickets(turn):
        raw_assignee = None
    assignee, error = _resolve_assignee(source, raw_assignee)
    contact_id = args.contact_id
    requestor = (args.requestor or "").strip() or None
    person = None if force_text_query else (requestor or _person_name_from_turn(turn))
    if not force_text_query and not person and raw_q and _looks_like_person_name(raw_q):
        person = raw_q
    if error and raw_assignee and _looks_like_person_name(raw_assignee):
        person = person or raw_assignee
        error = None
        raw_assignee = None
        assignee = None
    if error:
        return _refuse(LIST_TICKETS, source, error)
    person_label = _person_name_from_turn(turn) if force_text_query else None
    if person and contact_id is None:
        matches = _search_contacts_loose(source, person)
        pick, contact_error = _pick_contact(matches, person)
        if contact_error:
            return _refuse(LIST_TICKETS, source, contact_error)
        if pick is not None:
            contact_id = pick.id
            requestor = pick.full_name
            person_label = pick.full_name
            if raw_q and _looks_like_person_name(raw_q):
                raw_q = None
        else:
            requestor = person
            person_label = person
            if raw_q and _looks_like_person_name(raw_q):
                raw_q = None
    elif contact_id is not None:
        person_label = requestor or _person_name_from_turn(turn)
    if person_label or contact_id is not None:
        column_asked = True
    client = (args.client_code or "").strip().upper() or None
    stage = _default_list_stage(
        args,
        assignee_code=assignee,
        query=raw_q,
        category=category,
        reason=reason,
        overdue=overdue,
        complete=complete,
        project=args.project,
    )
    if (person_label or contact_id is not None) and (args.stage or "").strip().lower() not in (
        "review",
        "archived",
        "all",
        "live",
    ):
        if not client and not raw_q and not args.ticket_num:
            stage = "live" if _asked_latest_for_person(turn) else "open"
    if _asked_latest_for_person(turn) and (args.stage or "").strip().lower() not in (
        "review",
        "archived",
        "all",
    ):
        stage = "live"
    limit = 1 if _asked_latest_for_person(turn) else args.limit
    resolved_sort = (args.sort or "last_activity_at").strip() or "last_activity_at"
    # Placeholder tickets get filtered out *after* fetching below, which would just
    # shrink an already-capped page -- overfetch here so trimming to the caller's
    # real limit still happens after that filter, not before it.
    fetch_limit = max(limit, 100) if (resolved_sort == "hrs_actual_total" and not category) else limit
    rows = source.list_tickets(
        client_code=client,
        assignee_code=assignee,
        query=raw_q,
        contact_id=contact_id,
        status=args.status,
        ticket_num=(args.ticket_num or "").strip() or None,
        category=category,
        reason=reason,
        complete=complete,
        project=args.project,
        machine_name=(args.machine_name or "").strip() or None,
        invoice_num=(args.invoice_num or "").strip() or None,
        job=(args.job or "").strip() or None,
        cause=(args.cause or "").strip() or None,
        overdue=overdue,
        due_before=(args.due_before or "").strip() or None,
        due_after=(args.due_after or "").strip() or None,
        created_before=(args.created_before or "").strip() or None,
        created_after=(args.created_after or "").strip() or None,
        last_activity_before=(args.last_activity_before or "").strip() or None,
        last_activity_after=(args.last_activity_after or "").strip() or None,
        requestor=requestor,
        needs_response=args.needs_response,
        unassigned=args.unassigned,
        stage=stage,
        sort=resolved_sort,
        order=(args.order or "desc").strip() or "desc",
        limit=fetch_limit,
    )
    if resolved_sort == "hrs_actual_total" and not category:
        # A catch-all bucket ticket (e.g. a running "Meetings" ticket) always wins a
        # by-hours ranking otherwise -- never the real answer, whether the model got
        # here through answer_longest_time_worked's own phrasing or by calling this
        # tool directly for some other "which ticket took the most X" question.
        # Skipped only if the user is deliberately asking for that category by name.
        rows = [r for r in rows if not _is_placeholder_category(r.category)][:limit]
    codes = sorted({(r.client_code or "") for r in rows if r.client_code})
    scoped = client or (codes[0] if len(codes) == 1 else None)
    note = f"{len(rows)} ticket(s)."
    if len(rows) >= max(args.limit, 1):
        note = (
            f"Showing {len(rows)} tickets (limit {args.limit}, max 100). "
            "More may exist; narrow with a client code or status."
        )
    if stage == "open" and assignee and not (category or reason or overdue):
        note = (
            f"{note} These are Open tickets (not archived, not 9 REVIEW). "
            "Want archived tickets too?"
        )
    data = [_ticket_payload(r) for r in rows]
    who = (
        person_label
        or category
        or reason
        or ("overdue" if overdue else None)
        or assignee
        or client
        or (args.q or args.ticket_num or args.machine_name or args.invoice_num or args.job or "that search")
    )
    column_label = bool(category or reason or overdue)
    latest_person = _asked_latest_for_person(turn)
    if latest_person:
        heading = f"Latest ticket for {who}" if rows else f"No ticket found for {who}."
    elif not rows:
        if column_label:
            heading = f"No {stage} {who} tickets."
        elif assignee:
            heading = f"No {stage} tickets assigned to {who}."
        else:
            heading = f"No {stage} tickets for {who}."
    elif column_label:
        heading = f"{len(rows)} open {who} ticket(s)" if stage == "open" else f"{len(rows)} {stage} {who} ticket(s)"
    elif stage == "open":
        heading = f"{len(rows)} open ticket(s) for {who}"
    else:
        heading = f"{len(rows)} {stage} ticket(s) for {who}"
    return ToolResult(
        tool=LIST_TICKETS,
        source=source.source_name,
        data=data,
        row_ids=[r.id for r in rows],
        client_code=scoped,
        note=note,
        reply=format_ticket_list(data, heading=heading, note=note if rows else None),
    )


def _client_codes(raw: str | None) -> list[str]:
    """'ZTB,ZINT' or 'internal' -> codes. A single code (or none) comes back as 0-1 items."""
    codes: list[str] = []
    for part in re.split(r"[,\s]+", (raw or "").strip().upper()):
        if not part:
            continue
        for code in INTERNAL_CLIENT_CODES if part == "INTERNAL" else (part,):
            if code not in codes:
                codes.append(code)
    return codes


def _list_tickets_multi(
    source: FlowSource, args: ListTicketsArgs, turn: ChatTurn | None, codes: list[str]
) -> ToolResult:
    results = [
        list_tickets(source, args.model_copy(update={"client_code": code}), turn) for code in codes
    ]
    failed = next((r for r in results if not r.ok), None)
    if failed is not None:
        return failed
    data = [row for r in results for row in (r.data or [])]
    data.sort(key=lambda row: (row.get("last_activity_at") or "", row.get("id") or 0), reverse=True)
    data = data[: max(args.limit, 1)]
    stage = (args.stage or "").strip().lower() or ("open" if args.assignee_code else "live")
    label = ", ".join(codes)
    note = f"{len(data)} ticket(s) across {label}."
    heading = f"{len(data)} {stage} ticket(s) for {label}" if data else f"No {stage} tickets for {label}."
    return ToolResult(
        tool=LIST_TICKETS,
        source=source.source_name,
        data=data,
        row_ids=[row["id"] for row in data],
        client_code=None,
        note=note,
        reply=format_ticket_list(data, heading=heading, note=note if data else None),
    )


def latest_ticket(source: FlowSource, args: LatestTicketArgs, turn: ChatTurn | None = None) -> ToolResult:
    """Single most recent ticket for a client code, or for a named person from the turn."""
    person = _person_name_from_turn(turn) or (args.requestor or "").strip() or None
    if person:
        return list_tickets(
            source,
            ListTicketsArgs(
                contact_id=args.contact_id,
                requestor=person,
                stage="live",
                limit=1,
            ),
            turn or ChatTurn(user_text=f"latest ticket involving {person}"),
        )
    code = (args.client_code or "").strip().upper()
    if not code:
        return _refuse(
            LATEST_TICKET,
            source,
            "latest_ticket needs a client_code or a person's name (e.g. latest ticket involving Thomas Carter).",
        )
    rows = source.list_tickets(
        client_code=code,
        contact_id=args.contact_id,
        status=args.status,
        stage="live",
        sort="last_activity_at",
        order="desc",
        limit=1,
    )
    if not rows:
        return ToolResult(
            tool=LATEST_TICKET,
            source=source.source_name,
            data=None,
            row_ids=[],
            client_code=code,
            note="No matching ticket.",
            reply=f"No ticket found for {code}.",
        )
    row = rows[0]
    data = _ticket_payload(row)
    return ToolResult(
        tool=LATEST_TICKET,
        source=source.source_name,
        data=data,
        row_ids=[row.id],
        client_code=row.client_code.upper(),
        reply=format_ticket_list([data], heading=f"Latest ticket for {code}"),
    )


def _merge_ask_scope(turn: ChatTurn | None) -> tuple[str | None, str | None] | None:
    """None when this is not a 'need merged' question.

    Otherwise (client_code, assignee). Both None means every open ticket.
    """
    text = (turn.user_text if turn else "").strip()
    if not text or _MERGE_INTO_RE.search(text) or not _NEED_MERGED_RE.search(text):
        return None
    client: str | None = None
    assignee: str | None = None
    if _OWN_TICKETS_RE.search(text):
        assignee = "me"
    match = _MERGE_SCOPE_RE.search(text)
    if match:
        token = match.group(1).strip(" ?.!,")
        if token and token.lower() not in _NOT_A_MERGE_SCOPE:
            if token.isupper():
                client = token
                if _OWN_TICKETS_RE.search(text):
                    assignee = "me"
                else:
                    assignee = None
            else:
                assignee = token
    return client, assignee


def _about_query(turn: ChatTurn | None) -> str | None:
    """Topic words in 'what tickets are about {text}'. Not a person's name."""
    text = turn.user_text if turn else ""
    if not text or _asked_latest_for_person(turn):
        return None
    match = _ABOUT_TICKET_RE.search(text)
    if not match or _CLIENT_SCOPE_RE.search(match.group("q")):
        return None
    words = [part.strip(" ?.!,") for part in re.split(r"\s+", match.group("q").strip()) if part.strip(" ?.!,")]
    while words and words[-1].lower() in _ABOUT_FILLER:
        words.pop()
    while len(words) > 1 and words[0].lower() in _ABOUT_FILLER:
        words.pop(0)
    phrase = " ".join(words).strip()
    if not phrase or _is_self_token(phrase):
        return None
    return phrase


def _about_stage(turn: ChatTurn | None) -> str:
    text = (turn.user_text if turn else "").lower()
    if re.search(r"\barchived\b", text):
        return "archived"
    if re.search(r"\breview\b", text):
        return "review"
    if re.search(r"\bopen\b", text):
        return "open"
    return "live"


def _format_hours(hours: float) -> str:
    text = f"{hours:.2f}".rstrip("0").rstrip(".")
    return f"{text} hour" if text == "1" else f"{text} hours"


def _is_placeholder_category(category: str | None) -> bool:
    """'Place Holder' is a catch-all bucket ticket (e.g. a running "Meetings" ticket that
    accumulates unrelated time entries for years) -- never the intended answer to "which
    ticket took the longest," with or without the user saying so explicitly."""
    normalized = re.sub(r"[\s_-]+", "", (category or "").strip().lower())
    return normalized == "placeholder"


def _longest_time_stage(text: str) -> str:
    if re.search(r"\barchived\b", text, re.IGNORECASE):
        return "archived"
    if re.search(r"\breview\b", text, re.IGNORECASE) and not re.search(r"\bopen\b", text, re.IGNORECASE):
        return "review"
    if re.search(r"\bopen\b", text, re.IGNORECASE):
        return "open"
    return "live"


_LONGEST_TIME_PAGE_SIZE = 10


def answer_longest_time_worked(source: FlowSource, turn: ChatTurn | None) -> ToolResult | None:
    """Bypass the LLM so 'which open ticket has the longest time worked?' cannot ask for a filter.

    Returns a ranked top-N list up front, not just the single #1 -- most "what's next"
    follow-ups are already answered by the list, without needing a second question. Still
    supports a genuine "show me more" follow-up ("next longest", "how about after that")
    beyond what was already shown; it only fires when the previous assistant reply actually
    was one of these rankings, and excludes every ticket that reply already named.
    """
    text = (turn.user_text if turn else "").strip()
    if (
        not text
        or _TICKET_LABEL_RE.search(text)
        or _CLIENT_SCOPE_RE.search(text)
        or _DATE_RANGE_RE.search(text)
    ):
        return None
    previous_text = (turn.previous_assistant_text if turn else "") or ""
    prior_is_ranking = "longest time worked" in previous_text.lower()
    is_follow_up = prior_is_ranking and bool(
        (_ORDINAL_WORD_RE.search(text) and _RANK_WORD_RE.search(text))
        or (len(text) <= 50 and _VAGUE_CONTINUATION_RE.search(text))
    )
    if not is_follow_up and not _is_longest_time_question(text):
        return None
    exclude_labels = set(_TICKET_LABEL_RE.findall(previous_text)) if is_follow_up else set()
    stage_source = text if (_STAGE_WORD_RE.search(text) or not is_follow_up) else previous_text
    stage = _longest_time_stage(stage_source)
    try:
        rows = source.list_tickets(stage=stage, sort="hrs_actual_total", order="desc", limit=500)
    except FlowRequestError as exc:
        return _refuse(LIST_TICKETS, source, str(exc))
    stage_label = "open" if stage == "open" else stage
    if not rows:
        return ToolResult(
            tool=LIST_TICKETS,
            source=source.source_name,
            data=[],
            row_ids=[],
            reply=f"No {stage_label} tickets.",
        )
    excluded_placeholders = sum(1 for row in rows if _is_placeholder_category(row.category))
    ranked = [
        row
        for row in rows
        if (row.hrs_actual_total or 0.0) > 0
        and not _is_placeholder_category(row.category)
        and row.label not in exclude_labels
    ]
    if not ranked:
        return ToolResult(
            tool=LIST_TICKETS,
            source=source.source_name,
            data=[_ticket_payload(row) for row in rows[:20]],
            row_ids=[row.id for row in rows[:20]],
            reply=(
                f"Flow returned {len(rows)} {stage_label} ticket(s) without hours logged "
                "(excluding placeholder tickets" + (" and already-shown ones" if exclude_labels else "")
                + "), so I can't rank them by time worked."
            ),
        )
    page_size = _longest_time_page_size(text, _LONGEST_TIME_PAGE_SIZE)
    top = ranked[:page_size]
    data = [_ticket_payload(row) for row in top]
    heading_verb = "Next" if is_follow_up else "Top"
    ticket_word = "ticket" if len(top) == 1 else "tickets"
    heading = (
        f"{top[0].label} has {'the next-longest' if is_follow_up else 'the longest'} time worked "
        f"among {stage_label} tickets: {_format_hours(top[0].hrs_actual_total or 0.0)}."
        if len(top) == 1
        else f"{heading_verb} {len(top)} {stage_label} {ticket_word}, ranked by longest time worked:"
    )
    note_bits = []
    if excluded_placeholders:
        note_bits.append(f"Excluded {excluded_placeholders} placeholder ticket(s) (e.g. a catch-all 'Meetings' bucket).")
    if len(ranked) > len(top):
        note_bits.append(
            f"{len(ranked) - len(top)} more with time logged weren't shown -- "
            "ask again (e.g. \"what's next\") to see them."
        )
    if len(rows) >= 500:
        note_bits.append("Ranked 500 tickets. More may exist.")
    if source.source_name == "stub":
        note_bits.insert(0, "Lab fixtures, not production Flow.")
    note = " ".join(note_bits) or None
    return ToolResult(
        tool=LIST_TICKETS,
        source=source.source_name,
        data=data,
        row_ids=[row.id for row in top],
        note=note,
        reply=format_ticket_list(data, heading=heading, note=note),
    )


def answer_tickets_about(source: FlowSource, turn: ChatTurn | None) -> ToolResult | None:
    """'What tickets are about X?' is a text search. The model must not invent labels."""
    phrase = _about_query(turn)
    if not phrase:
        return None
    return list_tickets(
        source,
        ListTicketsArgs(q=phrase, stage=_about_stage(turn), limit=100),
        turn,
        force_text_query=True,
    )


def answer_merge_suggestion(source: FlowSource, turn: ChatTurn | None) -> ToolResult | None:
    """Bypass the LLM so 'does any tickets need merged?' cannot invent an empty answer."""
    scope = _merge_ask_scope(turn)
    if scope is None:
        return None
    client, assignee = scope
    return find_similar_tickets(
        source,
        FindSimilarTicketsArgs(client_code=client, assignee_code=assignee, stage="open"),
    )


def find_similar_tickets(
    source: FlowSource,
    args: FindSimilarTicketsArgs,
    turn: ChatTurn | None = None,
) -> ToolResult:
    scope = _merge_ask_scope(turn)
    if scope is not None:
        client_code, assignee_raw = scope
    else:
        client_code = (args.client_code or "").strip().upper() or None
        assignee_raw = args.assignee_code
    assignee, error = _resolve_assignee(source, assignee_raw)
    if error:
        return _refuse(FIND_SIMILAR_TICKETS, source, error)
    client = (client_code or "").strip().upper() or None
    stage = (args.stage or "").strip().lower() or "open"
    pairs = source.find_similar_tickets(
        ticket_id=args.ticket_id,
        client_code=client,
        assignee_code=assignee,
        status=args.status,
        stage=stage,
        limit=args.limit,
    )
    row_ids: list[int] = []
    for pair in pairs:
        row_ids.extend((pair.keep_ticket.id, pair.absorb_ticket.id))
    data = [_similar_payload(pair) for pair in pairs]
    if assignee and client:
        scope = f"open tickets for {client} assigned to {assignee}"
    elif assignee:
        scope = f"open tickets assigned to {assignee}"
    elif client:
        scope = f"open tickets for {client}"
    else:
        scope = "all open tickets"
    if pairs:
        heading = f"Possible merges in {scope} ({len(data)})"
    else:
        heading = f"No likely duplicates in {scope}."
    return ToolResult(
        tool=FIND_SIMILAR_TICKETS,
        source=source.source_name,
        data=data,
        row_ids=row_ids,
        client_code=client,
        note="Suggestions only; nothing was merged." if pairs else heading,
        reply=format_similar_list(data, heading=heading),
    )


def _label_in(label: str, text: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(label)}(?![A-Za-z0-9])", text, re.IGNORECASE) is not None


def _check_merge_gate(args: MergeTicketsArgs, turn: ChatTurn | None) -> str | None:
    """Return why the merge must not run, or None when every gate passes."""
    if args.confirm is not True:
        return (
            "Not merged: confirm is false. Show the plan (keep TARGET, absorb SOURCE) and wait for "
            "the user to reply restating both labels."
        )
    target = (args.target_label or "").strip().upper()
    sources = [label.strip().upper() for label in args.source_labels if label.strip()]
    if not target or not sources or len(sources) != len(args.source_ticket_ids):
        return "Not merged: target_label and one source_label per source_ticket_id are required."
    if target in sources:
        return "Not merged: a ticket cannot be merged into itself."
    if turn is None:
        return "Not merged: merge_tickets only runs from chat, after the user approves by restating the labels."
    labels = [target, *sources]
    missing = [label for label in labels if not _label_in(label, turn.user_text)]
    if missing:
        return (
            f"Not merged: the user's latest message does not name {', '.join(missing)}. 'ok' or 'yes' is "
            "not approval. Ask them to reply restating the labels, e.g. "
            f"'merge {sources[0]} into {target}'."
        )
    unshown = [label for label in labels if not _label_in(label, turn.previous_assistant_text)]
    if unshown:
        return (
            f"Not merged: you have not shown this plan yet ({', '.join(unshown)} missing from your last "
            "reply). Show keep/absorb labels first and wait for the user to restate them."
        )
    for source_label in sources:
        backwards = rf"{re.escape(target)}\W+(?:into|to|->)\W+{re.escape(source_label)}"
        if re.search(backwards, turn.user_text, re.IGNORECASE):
            return (
                f"Not merged: the user said {target} into {source_label}, the opposite direction. "
                "Confirm which ticket to keep."
            )
    return None


def _resolve_label(source: FlowSource, label: str) -> TicketRecord | None:
    match = _LABEL_RE.match(label)
    if match is None:
        return None
    rows = source.list_tickets(client_code=match.group(1).upper(), ticket_num=match.group(2), limit=2)
    return rows[0] if len(rows) == 1 else None


def merge_tickets(source: FlowSource, args: MergeTicketsArgs, turn: ChatTurn | None) -> ToolResult:
    refusal = _check_merge_gate(args, turn)
    if refusal:
        return _refuse(MERGE_TICKETS, source, refusal)

    target_label = (args.target_label or "").strip().upper()
    source_labels = [label.strip().upper() for label in args.source_labels if label.strip()]
    resolved: list[TicketRecord] = []
    for label in [target_label, *source_labels]:
        row = _resolve_label(source, label)
        if row is None:
            return _refuse(MERGE_TICKETS, source, f"Not merged: {label} did not resolve to exactly one ticket.")
        resolved.append(row)
    if len({row.client_id for row in resolved}) != 1:
        return _refuse(MERGE_TICKETS, source, "Not merged: tickets from different clients cannot be merged.")

    # Labels the user typed are the source of truth. The 7B/14B often invents ticket ids.
    target_id = resolved[0].id
    source_ids = [row.id for row in resolved[1:]]
    summary = source.merge_tickets(target_ticket_id=target_id, source_ticket_ids=source_ids)
    return ToolResult(
        tool=MERGE_TICKETS,
        read_only=False,
        source=source.source_name,
        data=summary,
        row_ids=[args.target_ticket_id, *args.source_ticket_ids],
        client_code=resolved[0].client_code.upper(),
        note=f"Merged {', '.join(source_labels)} into {target_label}.",
    )


def list_mail(source: FlowSource, args: ListMailArgs) -> ToolResult:
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


def list_time_entries(source: FlowSource, args: ListTimeEntriesArgs) -> ToolResult:
    assignee, error = _resolve_assignee(source, args.assignee_code)
    if error:
        return _refuse(LIST_TIME_ENTRIES, source, error)
    tech_user_id = None
    if assignee:
        matches = source.search_technician(query=assignee, limit=5)
        pick = next((m for m in matches if (m.assignee_code or "").lower() == assignee), None)
        if pick is None:
            return _refuse(LIST_TIME_ENTRIES, source, f"No active technician matches {assignee!r}.")
        tech_user_id = pick.id
    ticket_id, label_error = _ticket_id_for(source, args.ticket_id, args.ticket_label)
    if label_error:
        return _refuse(LIST_TIME_ENTRIES, source, label_error)
    client = (args.client_code or "").strip().upper() or None
    try:
        rows = source.list_time_entries(
            ticket_id=ticket_id,
            client_code=client,
            tech_user_id=tech_user_id,
            work_after=(args.work_after or "").strip() or None,
            work_before=(args.work_before or "").strip() or None,
            billable=args.billable,
            reviewed=args.reviewed,
            limit=args.limit,
        )
    except FlowRequestError as exc:
        return _refuse(LIST_TIME_ENTRIES, source, str(exc))
    data = [_time_entry_payload(r) for r in rows]
    total_minutes = sum(r.minutes for r in rows)
    if ticket_id and rows:
        scope = rows[0].ticket_label
    else:
        scope = (f"ticket {ticket_id}" if ticket_id else client) or assignee or "that scope"
    lines = [f"{len(data)} time entr{'y' if len(data) == 1 else 'ies'} for {scope} ({total_minutes} min total).", ""]
    if not data:
        lines.append("None found.")
    for row in data:
        billed = "billable" if row["billable"] else ("gratis" if row["gratis"] else "non-billable")
        when = row["work_date"] or row["start_at"] or ""
        lines.append(f"- {when} — {row['subject']} ({row['minutes']} min, {billed})")
    return ToolResult(
        tool=LIST_TIME_ENTRIES,
        source=source.source_name,
        data=data,
        row_ids=[r.id for r in rows],
        client_code=client,
        reply="\n".join(lines).rstrip(),
    )


def ticket_stats(source: FlowSource, args: TicketStatsArgs) -> ToolResult:
    assignee, error = _resolve_assignee(source, args.assignee_code)
    if error:
        return _refuse(TICKET_STATS, source, error)
    tech_user_id = None
    ticket_assignee = None
    if assignee and args.entity == "time":
        matches = source.search_technician(query=assignee, limit=5)
        pick = next((m for m in matches if (m.assignee_code or "").lower() == assignee), None)
        if pick is None:
            return _refuse(TICKET_STATS, source, f"No active technician matches {assignee!r}.")
        tech_user_id = pick.id
    elif assignee:
        ticket_assignee = assignee
    client = (args.client_code or "").strip().upper() or None
    try:
        stats = source.ticket_stats(
            entity=(args.entity or "tickets").strip().lower(),
            group_by=(args.group_by or "client").strip().lower(),
            metric=(args.metric or "count").strip().lower(),
            client_code=client,
            assignee_code=ticket_assignee,
            tech_user_id=tech_user_id,
            stage=(args.stage or "all").strip().lower(),
            direction=(args.direction or "inbound").strip().lower(),
            billable=args.billable,
            after=(args.after or "").strip() or None,
            before=(args.before or "").strip() or None,
            limit=args.limit,
        )
    except FlowRequestError as exc:
        return _refuse(TICKET_STATS, source, str(exc))
    return ToolResult(
        tool=TICKET_STATS,
        source=source.source_name,
        data=stats,
        client_code=client,
        note="Counts and hours are computed by Flow. Quote them as given; do not recount.",
        reply=format_stats(stats, client=client, assignee=assignee, after=args.after, before=args.before),
    )


def _format_minutes(minutes: int) -> str:
    hours, mins = divmod(int(minutes), 60)
    return f"{hours}h {mins:02d}m"


def format_stats(
    stats: dict[str, Any],
    *,
    client: str | None,
    assignee: str | None,
    after: str | None,
    before: str | None,
) -> str:
    entity = stats.get("entity", "tickets")
    group_by = stats.get("group_by", "")
    rows = stats.get("rows") or []
    scope = [f"{entity} by {group_by}"]
    if client:
        scope.append(f"client {client}")
    if assignee:
        scope.append(f"tech {assignee}")
    if after or before:
        scope.append(f"{after or 'start'} to {before or 'now'}")
    head = ", ".join(scope)
    if not rows:
        return f"No {entity} found ({head})."
    if entity == "time":
        total = f"{_format_minutes(stats.get('total_minutes', 0))} logged, {_format_minutes(stats.get('total_billed_minutes', 0))} billed"
        lines = [f"{head}: {stats.get('total_count', 0)} entries, {total}.", ""]
        for i, row in enumerate(rows, 1):
            lines.append(
                f"{i}. {row['key']} — {_format_minutes(row['minutes'])} logged, "
                f"{_format_minutes(row['billed_minutes'])} billed ({row['count']} entries)"
            )
    elif entity == "tickets":
        lines = [f"{head}: {stats.get('total_count', 0)} tickets, {stats.get('total_hours', 0)} h worked.", ""]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {row['key']} — {row['count']} ticket(s), {row['hours']} h worked")
    else:
        lines = [f"{head}: {stats.get('total_count', 0)} mail rows.", ""]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {row['key']} — {row['count']}")
    shown = len(rows)
    if stats.get("groups", shown) > shown:
        lines.append(f"(top {shown} of {stats['groups']} groups)")
    return "\n".join(lines)


def list_machines(source: FlowSource, args: ListMachinesArgs) -> ToolResult:
    client = args.client_code.strip().upper()
    rows = source.list_machines(client_code=client, limit=args.limit)
    data = [_machine_payload(r) for r in rows]
    lines = [f"{len(data)} machine(s) for {client}.", ""]
    if not data:
        lines.append("None found.")
    for row in data:
        seen = row.get("last_seen_at") or "never"
        lines.append(f"- {row['machine_name']} ({row.get('machine_support') or 'support unknown'}, last seen {seen})")
    return ToolResult(
        tool=LIST_MACHINES,
        source=source.source_name,
        data=data,
        row_ids=[r.id for r in rows],
        client_code=client,
        reply="\n".join(lines).rstrip(),
    )


def _ticket_id_for(source: FlowSource, ticket_id: int | None, label: str | None) -> tuple[int | None, str | None]:
    """A ticket id from an explicit id or a label like ACME-0041. Returns (id, error)."""
    text = (label or "").strip()
    if not text:
        return ticket_id, None
    row = _resolve_label(source, text.upper())
    if row is None:
        return None, f"No single ticket found for label {text!r}. Check the client code and number."
    return row.id, None


def get_ticket_detail(source: FlowSource, args: GetTicketDetailArgs) -> ToolResult:
    ticket_id, label_error = _ticket_id_for(source, args.ticket_id, args.ticket_label)
    if label_error:
        return _refuse(GET_TICKET_DETAIL, source, label_error)
    try:
        row = source.get_ticket(ticket_id=ticket_id)
    except FlowRequestError as exc:
        return _refuse(GET_TICKET_DETAIL, source, str(exc))
    data = _ticket_detail_payload(row)
    lines = [f"{data['ticket_label']} — {data['topic']} ({data['status']}, {data['category']})"]
    if row.log_text:
        lines.append(f"Log: {row.log_text}")
    if row.notes_text:
        lines.append(f"Notes: {row.notes_text}")
    if row.hrs_actual_total is not None:
        lines.append(f"Hours logged: {row.hrs_actual_total}")
    return ToolResult(
        tool=GET_TICKET_DETAIL,
        source=source.source_name,
        data=data,
        row_ids=[row.id],
        client_code=row.client_code.upper(),
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


def get_client_detail(source: FlowSource, args: GetClientDetailArgs) -> ToolResult:
    try:
        row = source.get_client(client_code=args.client_code)
    except FlowRequestError as exc:
        return _refuse(GET_CLIENT_DETAIL, source, str(exc))
    data = _client_detail_payload(row)
    lines = [f"{row.client_code} — {row.name or row.full_company_name or ''}".rstrip(" —")]
    if row.contract_minutes is not None:
        lines.append(f"Contract minutes: {row.contract_minutes}")
    if row.balance is not None:
        lines.append(f"Balance: {row.balance}")
    if row.account:
        lines.append(f"Account: {row.account}")
    for label, value in (
        ("Support renewal", row.support_renewal),
        ("Antivirus renewal", row.antivirus_renewal),
        ("Spam filter renewal", row.spam_filter_renewal),
    ):
        if value:
            lines.append(f"{label}: {value}")
    return ToolResult(
        tool=GET_CLIENT_DETAIL,
        source=source.source_name,
        data=data,
        row_ids=[],
        client_code=row.client_code.upper(),
        reply="\n".join(lines).rstrip(),
    )


def search_knowledge(knowledge: KnowledgeSource | None, args: SearchKnowledgeArgs) -> ToolResult:
    if knowledge is None or not getattr(knowledge, "configured", False):
        return ToolResult(
            ok=False,
            tool=SEARCH_KNOWLEDGE,
            source="unconfigured",
            error=(
                "Knowledge search is not set up (QDRANT_URL is empty). "
                "Answer from Flow tools only; do not guess at SOPs."
            ),
        )
    limit = min(max(args.limit, 1), 10)
    hits = knowledge.search(query=args.query, client_code=args.client_code, limit=limit)
    data = [hit.model_dump() for hit in hits]
    if not hits:
        return ToolResult(
            tool=SEARCH_KNOWLEDGE,
            source=knowledge.source_name,
            data=[],
            reply="Nothing in the knowledge index matched that.",
            note="No SOP/runbook or past fix note matched. Say so; do not guess a procedure.",
        )
    lines = [f"{len(hits)} knowledge match(es):", ""]
    for hit in hits:
        where = f" ({hit.client_code})" if hit.client_code else ""
        lines.append(f"- [{hit.source_type}] {hit.source_label}{where}: {hit.text[:400]}")
    return ToolResult(
        tool=SEARCH_KNOWLEDGE,
        source=knowledge.source_name,
        data=data,
        reply="\n".join(lines).rstrip(),
        note="From SOP docs / past fix notes, not Flow's live ticket data. Verify anything safety- or client-critical.",
    )


def dispatch(
    source: FlowSource,
    name: str,
    raw_args: dict[str, Any],
    turn: ChatTurn | None = None,
    *,
    knowledge: KnowledgeSource | None = None,
) -> ToolResult:
    if name == SEARCH_CONTACT:
        return search_contact(source, SearchContactArgs.model_validate(raw_args), turn)
    if name == SEARCH_TECHNICIAN:
        return search_technician(source, SearchTechnicianArgs.model_validate(raw_args))
    if name == LIST_TICKETS:
        return list_tickets(source, ListTicketsArgs.model_validate(raw_args), turn)
    if name == LATEST_TICKET:
        return latest_ticket(source, LatestTicketArgs.model_validate(raw_args), turn)
    if name == FIND_SIMILAR_TICKETS:
        return find_similar_tickets(source, FindSimilarTicketsArgs.model_validate(raw_args), turn)
    if name == MERGE_TICKETS:
        return merge_tickets(source, MergeTicketsArgs.model_validate(raw_args), turn)
    if name == LIST_MAIL:
        return list_mail(source, ListMailArgs.model_validate(raw_args))
    if name == LIST_TIME_ENTRIES:
        return list_time_entries(source, ListTimeEntriesArgs.model_validate(raw_args))
    if name == TICKET_STATS:
        return ticket_stats(source, TicketStatsArgs.model_validate(raw_args))
    if name == LIST_MACHINES:
        return list_machines(source, ListMachinesArgs.model_validate(raw_args))
    if name == GET_TICKET_DETAIL:
        return get_ticket_detail(source, GetTicketDetailArgs.model_validate(raw_args))
    if name == GET_MAIL_DETAIL:
        return get_mail_detail(source, GetMailDetailArgs.model_validate(raw_args))
    if name == GET_CLIENT_DETAIL:
        return get_client_detail(source, GetClientDetailArgs.model_validate(raw_args))
    if name == SEARCH_KNOWLEDGE:
        return search_knowledge(knowledge, SearchKnowledgeArgs.model_validate(raw_args))
    return ToolResult(
        ok=False,
        tool=name,
        source=source.source_name,
        error=f"Unknown tool {name!r}. Allowed: {', '.join(TOOL_NAMES)}.",
    )
