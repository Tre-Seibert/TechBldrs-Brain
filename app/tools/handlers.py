from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.flow.schemas import ContactRecord, MailRecord, SimilarTicketPair, TechnicianRecord, TicketRecord, ticket_label
from app.flow.source import FlowSource
from app.tools.registry import (
    FIND_SIMILAR_TICKETS,
    LATEST_TICKET,
    LIST_MAIL,
    LIST_TICKETS,
    MERGE_TICKETS,
    SEARCH_CONTACT,
    SEARCH_TECHNICIAN,
    TOOL_NAMES,
)

_ASSIGNEE_CODE_RE = re.compile(r"^[A-Za-z]{2}$")
_LABEL_RE = re.compile(r"^([A-Za-z0-9]+)-([A-Za-z0-9]+)$")


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
    client_code: str
    contact_id: int | None = None
    status: str | None = None


class ListTicketsArgs(BaseModel):
    assignee_code: str | None = None
    client_code: str | None = None
    q: str | None = None
    ticket_num: str | None = None
    status: str | None = None
    stage: str | None = None
    limit: int = 100

    @model_validator(mode="after")
    def _need_scope(self) -> ListTicketsArgs:
        if not any((value or "").strip() for value in (self.assignee_code, self.client_code, self.q, self.ticket_num)):
            raise ValueError("assignee_code, client_code, q, or ticket_num is required")
        return self


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
        "stage": row.stage,
        "requestor_text": row.requestor_text,
        "contact_id": row.contact_id,
        "machine_name": row.machine_name,
        "assignee_code": row.assignee_code,
        "complete": row.complete,
        "created_at": row.created_at.isoformat(sep=" "),
        "last_activity_at": row.last_activity_at.isoformat(sep=" "),
        "closed_at": row.closed_at.isoformat(sep=" ") if row.closed_at else None,
    }


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
        extra = f"{client} · {status} · {category} · {assignee}" if client else f"{status} · {category} · {assignee}"
        lines.append(f"- {label} — {topic} ({extra})")
    if note:
        lines.extend(["", note])
    return "\n".join(lines)


def _similar_ticket_block(title: str, row: dict[str, Any]) -> list[str]:
    label = row.get("ticket_label") or "?"
    topic = (row.get("topic") or "").strip() or "(no topic)"
    return [
        f"{title} {label} — {topic}",
        (
            f"  {(row.get('client_code') or '?')} · status {row.get('status') or '?'} · "
            f"category {row.get('category') or '?'} · stage {row.get('stage') or '?'} · "
            f"assignee {row.get('assignee_code') or 'unassigned'}"
        ),
        (
            f"  requestor {row.get('requestor_text') or 'n/a'} · "
            f"machine {row.get('machine_name') or 'n/a'}"
        ),
        (
            f"  created {row.get('created_at') or 'n/a'} · "
            f"last activity {row.get('last_activity_at') or 'n/a'}"
        ),
    ]


def format_similar_list(pairs: list[dict[str, Any]], *, heading: str) -> str:
    if not pairs:
        return heading
    lines = [heading.rstrip("."), ""]
    for pair in pairs:
        reasons = ", ".join(pair.get("reasons") or []) or "similar"
        lines.extend(_similar_ticket_block("Keep", pair.get("keep") or {}))
        lines.extend(_similar_ticket_block("Absorb", pair.get("absorb") or {}))
        lines.append(f"  Why: {reasons}")
        if pair.get("merge_blocked"):
            lines.append(f"  Cannot merge: {pair['merge_blocked']}")
        lines.append("")
    lines.append("Nothing was merged. To merge, reply with both labels, e.g. merge ZTB-1691 into ZTB-1680.")
    return "\n".join(lines).rstrip()


def _resolve_assignee(source: FlowSource, raw: str | None) -> tuple[str | None, str | None]:
    """Return (assignee_code, error). A two-letter code passes through; a name or email is
    resolved through search_technician — never a hardcoded alias table."""
    text = (raw or "").strip()
    if not text:
        return None, None
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


def search_technician(source: FlowSource, args: SearchTechnicianArgs) -> ToolResult:
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


def _default_list_stage(args: ListTicketsArgs) -> str:
    """Assigned-to lists are Open unless the user asked for review/archived/all.

    The 7B often passes stage=live on its own, which pulls 9 REVIEW back in.
    """
    wanted = (args.stage or "").strip().lower()
    assignee_only = bool((args.assignee_code or "").strip()) and not any(
        (value or "").strip() for value in (args.client_code, args.q, args.ticket_num)
    )
    if assignee_only and wanted not in ("review", "archived", "all"):
        return "open"
    return wanted or "live"


def list_tickets(source: FlowSource, args: ListTicketsArgs) -> ToolResult:
    assignee, error = _resolve_assignee(source, args.assignee_code)
    if error:
        return _refuse(LIST_TICKETS, source, error)
    client = (args.client_code or "").strip().upper() or None
    stage = _default_list_stage(args)
    rows = source.list_tickets(
        client_code=client,
        assignee_code=assignee,
        query=(args.q or "").strip() or None,
        status=args.status,
        ticket_num=(args.ticket_num or "").strip() or None,
        stage=stage,
        limit=args.limit,
    )
    codes = sorted({(r.client_code or "") for r in rows if r.client_code})
    scoped = client or (codes[0] if len(codes) == 1 else None)
    note = f"{len(rows)} ticket(s)."
    if len(rows) >= max(args.limit, 1):
        note = (
            f"Showing {len(rows)} tickets (limit {args.limit}, max 100). "
            "More may exist; narrow with a client code or status."
        )
    if stage == "open" and assignee:
        note = (
            f"{note} These are Open tickets (not archived, not 9 REVIEW). "
            "Want archived tickets too?"
        )
    data = [_ticket_payload(r) for r in rows]
    who = assignee or client or (args.q or args.ticket_num or "that search")
    if not rows:
        heading = (
            f"No {stage} tickets assigned to {who}."
            if assignee
            else f"No {stage} tickets for {who}."
        )
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


def latest_ticket(source: FlowSource, args: LatestTicketArgs) -> ToolResult:
    """Convenience wrapper: list_tickets(client_code, limit=1) by last activity."""
    code = args.client_code.strip().upper()
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


def find_similar_tickets(source: FlowSource, args: FindSimilarTicketsArgs) -> ToolResult:
    assignee, error = _resolve_assignee(source, args.assignee_code)
    if error:
        return _refuse(FIND_SIMILAR_TICKETS, source, error)
    client = (args.client_code or "").strip().upper() or None
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
    expected = [(target_label, args.target_ticket_id)] + list(zip(source_labels, args.source_ticket_ids))
    resolved: list[TicketRecord] = []
    for label, ticket_id in expected:
        row = _resolve_label(source, label)
        if row is None:
            return _refuse(MERGE_TICKETS, source, f"Not merged: {label} did not resolve to exactly one ticket.")
        if row.id != ticket_id:
            return _refuse(
                MERGE_TICKETS,
                source,
                f"Not merged: {label} is ticket id {row.id}, not {ticket_id}. Re-check the ids from the tool results.",
            )
        resolved.append(row)
    if len({row.client_id for row in resolved}) != 1:
        return _refuse(MERGE_TICKETS, source, "Not merged: tickets from different clients cannot be merged.")

    summary = source.merge_tickets(target_ticket_id=args.target_ticket_id, source_ticket_ids=args.source_ticket_ids)
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
    rows = source.list_mail(
        client_code=args.client_code,
        direction=args.direction,
        email=args.email,
        contact_id=args.contact_id,
        limit=args.limit,
    )
    data = [_mail_payload(r) for r in rows]
    lines = [f"{len(data)} {args.direction} mail row(s) for {args.client_code.upper()}.", ""]
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
        client_code=args.client_code.upper(),
        note="direction=inbound means from the client to TechBldrs (filed on a Flow ticket).",
        reply="\n".join(lines).rstrip(),
    )


def dispatch(source: FlowSource, name: str, raw_args: dict[str, Any], turn: ChatTurn | None = None) -> ToolResult:
    if name == SEARCH_CONTACT:
        return search_contact(source, SearchContactArgs.model_validate(raw_args))
    if name == SEARCH_TECHNICIAN:
        return search_technician(source, SearchTechnicianArgs.model_validate(raw_args))
    if name == LIST_TICKETS:
        return list_tickets(source, ListTicketsArgs.model_validate(raw_args))
    if name == LATEST_TICKET:
        return latest_ticket(source, LatestTicketArgs.model_validate(raw_args))
    if name == FIND_SIMILAR_TICKETS:
        return find_similar_tickets(source, FindSimilarTicketsArgs.model_validate(raw_args))
    if name == MERGE_TICKETS:
        return merge_tickets(source, MergeTicketsArgs.model_validate(raw_args), turn)
    if name == LIST_MAIL:
        return list_mail(source, ListMailArgs.model_validate(raw_args))
    return ToolResult(
        ok=False,
        tool=name,
        source=source.source_name,
        error=f"Unknown tool {name!r}. Allowed: {', '.join(TOOL_NAMES)}.",
    )
