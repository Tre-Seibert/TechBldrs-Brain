"""get_ticket_detail: one ticket's fields, a briefing digest, and past fixes for advice."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, model_validator

from app.knowledge.source import KnowledgeSource
from app.sources.flow.handlers.common import (
    _STAGE_TAGS,
    _SUBJECT_PREFIX_RE,
    _resolve_label,
    _worked_minutes,
    format_ticket_list,
)
from app.sources.flow.handlers.payloads import _ticket_detail_payload, _ticket_payload
from app.sources.flow.protocol import FlowRequestError, FlowSource
from app.sources.flow.schemas import MailRecord, TicketDetail, TicketRecord, TimeEntryRecord, ticket_label
from app.sources.flow.tools import GET_TICKET_DETAIL
from app.tools.base import ChatTurn, ToolResult, _refuse
from app.tools.formatting import _activity_text, _created_text, _due_text, _format_hours, _format_minutes
from app.tools.turns import _LABEL_RE


_GUESSED_LABEL_RE = re.compile(r"^([A-Za-z0-9]{2,6})[-\s]+(\S.*)$")


class GetTicketDetailArgs(BaseModel):
    ticket_id: int | None = None
    ticket_label: str | None = None
    # brief (default): the fields, then a prose summary of what the ticket is about and its latest
    # update, written from the notes, mail and time entries. full: the raw log text, verbatim.
    view: str = "brief"

    @model_validator(mode="after")
    def _need_one(self) -> GetTicketDetailArgs:
        if self.ticket_id is None and not (self.ticket_label or "").strip():
            raise ValueError("ticket_label (e.g. ACME-0041) or ticket_id is required")
        return self


def _ticket_id_for(
    source: FlowSource, ticket_id: int | None, label: str | None, turn: ChatTurn | None = None
) -> tuple[int | None, str | None]:
    """A ticket id from an explicit id or a label like ACME-0041. Returns (id, error)."""
    text = (label or "").strip()
    if not text:
        return ticket_id, None
    row = _resolve_label(source, text.upper())
    if row is not None:
        return row.id, None
    said = (turn.user_text if turn else "") or ""
    match = _LABEL_RE.match(text)
    if match is None:
        # 'VANG-Platform infrastructure': a client code plus the model's idea of a title, no ticket number
        guess = _GUESSED_LABEL_RE.match(text)
        if guess and re.search(rf"\b{re.escape(guess.group(1))}\b", said, re.IGNORECASE):
            match = guess
    if match and turn is not None and text.lower() not in said.lower():
        # The user never typed this label ('the VANG ticket'), so the model guessed it. Use the client's
        # one open ticket if there is exactly one; otherwise ask which, never guess.
        code = match.group(1).upper()
        try:
            open_rows = source.list_tickets(client_code=code, stage="open", limit=6)
        except FlowRequestError:
            open_rows = []
        if len(open_rows) == 1:
            return open_rows[0].id, None
        if open_rows:
            labels = ", ".join(ticket_label(r.client_code, r.ticket_num) for r in open_rows)
            return None, f"{code} has several open tickets ({labels}). Which one do you mean? Give the ticket label."
    return None, f"No single ticket found for label {text!r}. Check the client code and number."


_FULL_LOG_RE = re.compile(
    r"\b(?:full|raw|entire|whole|complete)\s+(?:ticket\s+)?log\b|\bshow\s+(?:me\s+)?the\s+log\b|\bticket\s+log\b",
    re.IGNORECASE,
)


def _resolve_archived_label(source: FlowSource, label: str) -> TicketRecord | None:
    """A label that is not a live ticket may be an archived one. Read-only; never used for merging."""
    match = _LABEL_RE.match(label)
    if match is None:
        return None
    try:
        rows = source.list_tickets(
            client_code=match.group(1).upper(), ticket_num=match.group(2), stage="archived", limit=2
        )
    except FlowRequestError:
        return None
    return rows[0] if len(rows) == 1 else None


def _archived_ticket_result(source: FlowSource, row: TicketRecord) -> ToolResult:
    data = _ticket_payload(row)
    label = data["ticket_label"]
    reply = format_ticket_list([data], heading=f"{label} is an archived ticket", extras=("created", "activity"))
    return ToolResult(
        tool=GET_TICKET_DETAIL,
        source=source.source_name,
        data=data,
        row_ids=[row.id],
        client_code=row.client_code.upper(),
        note="Archived ticket: fields only.",
        reply=f"{reply}\n\nIt was found in the archive, not in live tickets. The archive only gives these fields here, "
        "not the notes, emails or time entries.",
    )


def get_ticket_detail(
    source: FlowSource,
    args: GetTicketDetailArgs,
    turn: ChatTurn | None = None,
    knowledge: KnowledgeSource | None = None,
) -> ToolResult:
    ticket_id, label_error = _ticket_id_for(source, args.ticket_id, args.ticket_label, turn)
    if label_error:
        # Nothing live matched, so (and only then) look in the archive.
        archived = _resolve_archived_label(source, (args.ticket_label or "").strip().upper())
        if archived is not None:
            return _archived_ticket_result(source, archived)
        return _refuse(GET_TICKET_DETAIL, source, label_error)
    try:
        row = source.get_ticket(ticket_id=ticket_id)
    except FlowRequestError as exc:
        return _refuse(GET_TICKET_DETAIL, source, str(exc))
    data = _ticket_detail_payload(row)
    asked_for_log = bool(turn and _FULL_LOG_RE.search(turn.user_text or ""))
    if (args.view or "brief").strip().lower() != "full" or (turn is not None and not asked_for_log):
        return _ticket_briefing(source, row, data, turn, knowledge)
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


def _field_block(row: TicketDetail, now: datetime) -> str:
    """The ticket's fields as a short, exact block that goes before the prose summary."""
    label = ticket_label(row.client_code, row.ticket_num)
    stage = _STAGE_TAGS.get(row.stage or "", row.stage or "")
    lines = [
        f"{label} — {(row.topic or row.subject or '').strip() or '(no topic)'}",
        f"Client {row.client_code} · {row.status or 'no status'} · {row.category or 'no category'}"
        + (f" · {stage}" if stage else "")
        + (" · complete" if row.complete else ""),
        f"Assigned to {row.assignee_code or 'nobody'}"
        + (f" · requestor {row.requestor_text}" if row.requestor_text else "")
        + (f" · reason {row.reason}" if row.reason else "")
        + (f" · cause {row.cause}" if row.cause else ""),
        f"{_created_text(row.created_at.isoformat(sep=' '), now)} · "
        f"{_activity_text(row.last_activity_at.isoformat(sep=' '), now)}"
        + (f" · {_due_text(row.due_at.isoformat(sep=' '), now)}" if row.due_at else ""),
    ]
    if row.hrs_actual_total is not None:
        lines.append(f"Time logged: {_format_hours(float(row.hrs_actual_total))}")
    if row.machine_name:
        lines.append(f"Machine: {row.machine_name}")
    return "\n".join(lines)


_BRIEF_ENTRIES = 12
_BRIEF_MAIL = 8


_RESOLVE_ASK_RE = re.compile(
    r"\b(?:resolve|resolution|fix|fixing|solve|troubleshoot|remediat\w*|suggestions?|next steps?|best way|"
    r"what should (?:i|we) do|how (?:do|would|can|should) (?:i|we))\b",
    re.IGNORECASE,
)


def _issue_query(topic: str) -> str:
    """The part of a ticket title that describes the problem, for searching past fixes.

    Alert titles look like 'host | code | CLIENT name | performance | Disk Space Monitor | C drive ...':
    the last two segments are the issue, the rest is who and where. Ticket labels and numbers are dropped.
    """
    parts = [part.strip() for part in topic.split("|") if part.strip()]
    text = " ".join(parts[-2:]) if len(parts) >= 4 else topic
    text = re.sub(r"\b[A-Z][A-Z0-9]{1,8}-[A-Z0-9]{3,6}\b", " ", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\b", " ", text)
    return " ".join(text.split())[:160]


def _past_fix_lines(knowledge: KnowledgeSource | None, row: TicketDetail, query: str) -> list[str] | None:
    """Notes from past tickets and runbooks about a similar issue; None when knowledge search is off."""
    if knowledge is None or not getattr(knowledge, "configured", False) or not query:
        return None
    try:
        hits = knowledge.search(query=query, client_code=None, limit=8)
    except Exception:  # an unreachable index must not break the ticket briefing
        return None
    own = ticket_label(row.client_code, row.ticket_num)
    lines = [
        f"- [{hit.source_label}] {' '.join(hit.text.split())[:320]}" for hit in hits if hit.source_label != own
    ]
    return lines[:5]


_PAST_FIX_TICKETS = 5


def _past_fix_from_flow(source: FlowSource, row: TicketDetail) -> list[str]:
    """Time-entry notes from other tickets with the same cause, straight from Flow. Used when the knowledge
    index is not set up or finds nothing, so advice still rests on our own records when they exist."""
    cause = (row.cause or "").strip()
    if not cause:
        return []
    own = ticket_label(row.client_code, row.ticket_num)
    try:
        similar = source.list_tickets(cause=cause, stage="live", sort="last_activity_at", order="desc", limit=40)
    except FlowRequestError:
        return []
    lines: list[str] = []
    seen = 0
    for ticket in similar:
        label = ticket_label(ticket.client_code, ticket.ticket_num)
        if label == own or not (ticket.hrs_actual_total or 0) > 0:
            continue
        try:
            entries = source.list_time_entries(ticket_id=ticket.id, limit=3)
        except FlowRequestError:
            continue
        notes = [
            f"{(e.subject or '').strip()}: {' '.join((e.body or '').split())[:260]}".strip(": ")
            for e in entries
            if (e.body or "").strip() or (e.subject or "").strip()
        ]
        if not notes:
            continue
        lines.append(f"- [{label}] " + " | ".join(notes))
        seen += 1
        if seen >= _PAST_FIX_TICKETS:
            break
    return lines


def _ticket_briefing(
    source: FlowSource,
    row: TicketDetail,
    data: dict[str, Any],
    turn: ChatTurn | None = None,
    knowledge: KnowledgeSource | None = None,
) -> ToolResult:
    """Fields (exact) + a digest of notes, mail and time entries for the model to summarize."""
    now = datetime.now()
    label = data["ticket_label"]
    wants_fix = bool(turn and _RESOLVE_ASK_RE.search(turn.user_text or ""))
    notes = " ".join((row.notes_text or "").split())[:600]
    log_lines = [
        line.strip() for line in (row.log_text or "").splitlines() if line.strip() and "Time entry" not in line
    ]
    mail_rows: list[MailRecord] = []
    entry_rows: list[TimeEntryRecord] = []
    try:
        mail_rows = source.list_mail(ticket_id=row.id, direction="all", limit=_BRIEF_MAIL)
    except FlowRequestError:
        pass
    try:
        entry_rows = source.list_time_entries(ticket_id=row.id, limit=_BRIEF_ENTRIES)
    except FlowRequestError:
        pass
    events: list[tuple[datetime, str]] = []
    title = (row.topic or row.subject or "").strip()
    if wants_fix:
        instruction = (
            "The user wants advice on resolving this ticket. You can only advise; never say you did or will "
            "do anything. Write: one sentence on what the ticket is, then 3 to 6 numbered suggested steps. "
            "If 'Past fixes' below has relevant notes, base the steps on them and say they come from past "
            "tickets, naming the ticket labels. If there are none, just give two or three general steps; "
            "a line saying they are not from our records is added for you. "
            f"The ticket is titled '{title}': do not rename it. Do not give totals or dates."
        )
    else:
        instruction = (
            "Write a short plain-English briefing on this ticket in two short paragraphs: first what it is "
            "about and why it was opened, then the latest update (who did or said what most recently, and "
            "what is pending or next). Use only the facts below; do not repeat the fields or give totals. "
            f"The ticket is titled '{title}': do not rename it or give it a different subject. If the facts "
            "below do not say why it was opened, say that is not recorded instead of guessing, and never say "
            "a ticket was completed unless the facts say so."
        )
    lines = [f"{label}: {title}", instruction, ""]
    if notes:
        lines.append(f"Ticket notes: {notes}")
    if log_lines:
        lines.append("Ticket log (non-time lines): " + " | ".join(log_lines[-6:])[:700])
    for mail in mail_rows:
        when = mail.received_at or mail.created_at
        who = (mail.from_name or mail.from_address or "unknown").strip()
        events.append((when, f"mail {mail.direction} from {who} -- {(mail.subject or '').strip()}: {mail.snippet[:260]}"))
    for entry in entry_rows:
        when = entry.work_date or entry.start_at or entry.created_at
        subject = _SUBJECT_PREFIX_RE.sub("", entry.subject or "").strip() or "(no title)"
        body = " ".join((entry.body or "").split())[:260]
        events.append((when, f"time entry {_format_minutes(_worked_minutes(entry))} -- {subject}" + (f": {body}" if body else "")))
    events.sort(key=lambda item: item[0], reverse=True)
    if events:
        lines.append("")
        lines.append("Activity, newest first:")
        for when, text in events[:14]:
            lines.append(f"- {when:%Y-%m-%d}: {text}")
        newest = events[0]
        lines.append(f"(Most recent activity: {newest[0]:%Y-%m-%d}, {newest[1].split(' -- ')[0]}.)")
    else:
        lines.append("There are no emails or time entries on this ticket yet.")
    footer = None
    if wants_fix:
        past = _past_fix_lines(knowledge, row, _issue_query(title))
        if not past:
            past = _past_fix_from_flow(source, row)  # no knowledge index, or it found nothing
        lines.append("")
        if past:
            lines.append("Past fixes for similar issues (time-entry notes and runbooks):")
            lines.extend(past)
        else:
            lines.append("Past fixes: none found for a similar issue.")
            footer = "No similar past fix is on record, so these steps are general IT advice, not from our records."
    return ToolResult(
        tool=GET_TICKET_DETAIL,
        source=source.source_name,
        data=data,
        row_ids=[row.id],
        client_code=row.client_code.upper(),
        header=_field_block(row, now),
        digest="\n".join(lines),
        footer=footer,
    )
