"""Flow time tools: list_time_entries and the 'longest time worked' ranking."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, model_validator

from app.flow.schemas import TimeEntryRecord
from app.flow.source import FlowRequestError, FlowSource
from app.tools.base import ChatTurn, ToolResult, _refuse
from app.tools.flow.common import (
    _SUBJECT_PREFIX_RE,
    _is_placeholder_category,
    _worked_minutes,
    format_ticket_list,
)
from app.tools.flow.payloads import _ticket_payload, _time_entry_payload
from app.tools.flow.people import _resolve_assignee
from app.tools.flow.stats import _UNREVIEWED_RE, TicketStatsArgs, ticket_stats
from app.tools.flow.ticket_detail import _ticket_id_for
from app.tools.formatting import _ago, _format_hours, _format_minutes, _stamp, _when
from app.tools.registry import LIST_TICKETS, LIST_TIME_ENTRIES
from app.tools.turns import _CLIENT_SCOPE_RE, _period_from_turn


_RANK_WORD_RE = re.compile(r"\b(?:longest|most|highest|greatest)\b", re.IGNORECASE)
_WORK_AMOUNT_WORD_RE = re.compile(r"\b(?:time|hours?|worked|logged)\b", re.IGNORECASE)
_TICKET_WORD_RE = re.compile(r"\btickets?\b", re.IGNORECASE)
# "the ticket *I* spent the most time on" is one technician's time, not the all-time ticket total.
_OWN_TIME_RE = re.compile(r"\b(?:i|my|mine|myself)\b|\b(?:by|for)\s+me\b", re.IGNORECASE)
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


class ListTimeEntriesArgs(BaseModel):
    ticket_id: int | None = None
    ticket_label: str | None = None
    client_code: str | None = None
    assignee_code: str | None = None
    work_after: str | None = None
    work_before: str | None = None
    billable: bool | None = None
    reviewed: bool | None = None
    # summary: the model writes a short prose summary of the notes; list: every entry, verbatim.
    view: str = "summary"
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
        or _OWN_TIME_RE.search(text)
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


_LATEST_ENTRY_RE = re.compile(
    r"\b(?:last|latest|most\s+recent|newest)\s+(?:(\d{1,2})\s+)?(?:time\s*)?entr(?:y|ies)\b", re.IGNORECASE
)
_SUMMARIZE_WORD_RE = re.compile(r"\b(?:summari[sz]e|summary|recap|explain|describe)\b", re.IGNORECASE)


def _latest_entry_count(turn: ChatTurn | None) -> int | None:
    """How many entries 'my last time entry' asks for: 1, or N for 'my last 3 time entries'."""
    match = _LATEST_ENTRY_RE.search((turn.user_text if turn else "") or "")
    if not match:
        return None
    return min(max(int(match.group(1) or 1), 1), 10)


_TIME_SUMMARY_FETCH = 100


def _time_digest(rows: list[TimeEntryRecord], *, scope: str, span: str, total_minutes: int) -> str:
    """What the model reads to write the summary: one line per entry, notes trimmed to fit."""
    # No durations, dates or totals in here on purpose: given numbers, the model re-adds them and gets
    # them wrong. The exact figures are the footer's, appended after the model's text.
    per_note = max(120, min(400, 12000 // max(len(rows), 1)))
    ordered = sorted(rows, key=lambda r: (r.ticket_label, r.work_date or r.start_at or r.created_at))
    lines = [
        f"Time entries for {scope}{span}: {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}.",
        "Write a plain-English summary of what was worked on: exactly one short paragraph for each ticket "
        "listed below, so every ticket appears once. Put a blank line between paragraphs and start each "
        "with its ticket label (like ZTB-1680). No bullets or dashes. Past tense. Use only what the notes "
        "below say. No intro or closing sentence. Do not state dates, hours, minutes, counts or client "
        "names; the exact totals are added after your text.",
        "",
    ]
    for row in ordered:
        subject = _SUBJECT_PREFIX_RE.sub("", row.subject or "").strip() or "(no title)"
        notes = " ".join((row.body or "").split())[:per_note]
        line = f"[{row.ticket_label}] {subject}"
        lines.append(f"{line} -- notes: {notes}" if notes else line)
    return "\n".join(lines)


def _time_footer(rows: list[TimeEntryRecord], *, total_minutes: int, truncated: bool) -> str:
    by_ticket: dict[str, int] = {}
    billable = 0
    for row in rows:
        by_ticket[row.ticket_label] = by_ticket.get(row.ticket_label, 0) + _worked_minutes(row)
        if row.billable:
            billable += _worked_minutes(row)
    ranked = sorted(by_ticket.items(), key=lambda item: (-item[1], item[0]))
    lines = [
        f"Total: {_format_minutes(total_minutes)} worked across {len(rows)} entr{'y' if len(rows) == 1 else 'ies'} on "
        f"{len(by_ticket)} ticket{'s' if len(by_ticket) != 1 else ''}.",
        "By ticket: " + " · ".join(f"{label} {_format_minutes(minutes)}" for label, minutes in ranked[:12]),
    ]
    if len(ranked) > 12:
        lines[-1] += f" · +{len(ranked) - 12} more"
    if billable:
        lines.append(f"Billable: {_format_minutes(billable)}")
    if truncated:
        lines.append(f"(Showing the first {_TIME_SUMMARY_FETCH} entries; narrow the dates to see the rest.)")
    return "\n".join(lines)


def _placeholder_labels(source: FlowSource, rows: list[TimeEntryRecord]) -> set[str]:
    """Labels of the tickets in `rows` whose category is Place Holder (one lookup per ticket)."""
    found: set[str] = set()
    checked: set[str] = set()
    for row in rows:
        if row.ticket_label in checked:
            continue
        checked.add(row.ticket_label)
        try:
            tickets = source.list_tickets(client_code=row.client_code, ticket_num=row.ticket_num, stage="all", limit=1)
        except FlowRequestError:
            continue
        if tickets and _is_placeholder_category(tickets[0].category):
            found.add(row.ticket_label)
    return found


_HOW_MUCH_TIME_RE = re.compile(r"\bhow\s+(?:much|many)\s+(?:time|hours?)\b", re.IGNORECASE)


def list_time_entries(source: FlowSource, args: ListTimeEntriesArgs, turn: ChatTurn | None = None) -> ToolResult:
    unscoped = not any((args.ticket_id, (args.ticket_label or "").strip(), (args.client_code or "").strip()))
    if unscoped and (args.view or "summary").strip().lower() != "list" and _UNREVIEWED_RE.search(
        turn.user_text if turn else ""
    ):
        # "which tickets have time that isn't reviewed": a per-ticket tally, not 100 entries of prose
        return ticket_stats(
            source,
            TicketStatsArgs(
                entity="time", group_by="ticket", metric="hours", reviewed=False,
                assignee_code=args.assignee_code, limit=10,
            ),
            turn,
        )
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
    ticket_id, label_error = _ticket_id_for(source, args.ticket_id, args.ticket_label, turn)
    if label_error:
        return _refuse(LIST_TIME_ENTRIES, source, label_error)
    client = (args.client_code or "").strip().upper() or None
    summary = (args.view or "summary").strip().lower() != "list"
    fetch_limit = _TIME_SUMMARY_FETCH if summary else args.limit
    work_after = (args.work_after or "").strip() or None
    work_before = (args.work_before or "").strip() or None
    period = _period_from_turn(turn)
    if period:
        work_after, work_before = period[0], period[1]
    latest_n = _latest_entry_count(turn)
    order_by: dict[str, Any] = {}
    if latest_n and not period:
        # "my last time entry": the newest one submitted, not a date range of them.
        work_after = work_before = None
        fetch_limit = latest_n
        order_by = {"sort": "created_at", "order": "desc"}
        summary = bool(_SUMMARIZE_WORD_RE.search(turn.user_text or ""))  # else show the entry itself
    try:
        rows = source.list_time_entries(
            **order_by,
            ticket_id=ticket_id,
            client_code=client,
            tech_user_id=tech_user_id,
            work_after=work_after,
            work_before=work_before,
            billable=args.billable,
            reviewed=args.reviewed,
            limit=fetch_limit,
        )
    except FlowRequestError as exc:
        return _refuse(LIST_TIME_ENTRIES, source, str(exc))
    data = [_time_entry_payload(r) for r in rows]
    total_minutes = sum(_worked_minutes(r) for r in rows)
    digest_rows = rows
    if summary and rows:
        # Catch-all "Place Holder" tickets (huddles, meetings) are not work worth describing: the prose
        # leaves them out. The footer still counts their time, so the totals stay exact.
        placeholders = _placeholder_labels(source, rows)
        digest_rows = [r for r in rows if r.ticket_label not in placeholders]
        if not digest_rows:
            summary = False  # nothing but placeholder time: show the plain list instead of prose
    if ticket_id and rows:
        scope = rows[0].ticket_label
    else:
        scope = (f"ticket {ticket_id}" if ticket_id else client) or assignee or "that scope"
    span = f", {work_after or 'start'} to {work_before or 'now'}" if (work_after or work_before) else ""
    if period:
        span = f", {period[2]}"
    count = f"{len(data)} time entr{'y' if len(data) == 1 else 'ies'}"
    if not data or not summary:
        if latest_n and data and not period:
            lines = [f"{'Latest time entry' if len(data) == 1 else f'Latest {len(data)} time entries'} for {scope}:", ""]
        else:
            lines = [f"{count} for {scope}{span} ({_format_minutes(total_minutes)} total).", ""]
        if not data:
            lines.append("None found.")
        for row in data:
            billed = "billable" if row["billable"] else ("gratis" if row["gratis"] else "non-billable")
            when = (row["work_date"] or row["start_at"] or "")[:10]
            worked = row["actual_minutes"] or row["minutes"]
            lines.append(f"- {when} — {row['ticket_label']} — {row['subject']} ({_format_minutes(worked)}, {billed})")
            if len(data) <= 3:  # a handful of entries: show what was written, and when it was submitted
                notes = " ".join((row.get("body") or "").split())
                if notes:
                    lines.append(f"    Notes: {notes[:600]}")
                submitted = _when(row.get("created_at"))
                if submitted:
                    lines.append(f"    Submitted {_stamp(submitted, with_time=True)} ({_ago(submitted, datetime.now())})")
        return ToolResult(
            tool=LIST_TIME_ENTRIES,
            source=source.source_name,
            data=data,
            row_ids=[r.id for r in rows],
            client_code=client,
            reply="\n".join(lines).rstrip(),
        )
    if _HOW_MUCH_TIME_RE.search(turn.user_text if turn else "") and not _SUMMARIZE_WORD_RE.search(turn.user_text or ""):
        # "how much time was logged on X": the exact totals, not a retelling of every entry
        return ToolResult(
            tool=LIST_TIME_ENTRIES,
            source=source.source_name,
            data=data,
            row_ids=[r.id for r in rows],
            client_code=client,
            reply=f"{scope}{span}:\n" + _time_footer(rows, total_minutes=total_minutes, truncated=len(rows) >= _TIME_SUMMARY_FETCH),
        )
    plain = [f"{count} for {scope}{span} ({_format_minutes(total_minutes)} total).", ""]
    for row in data:
        when = (row["work_date"] or row["start_at"] or "")[:10]
        plain.append(f"- {when} — {row['subject']} ({_format_minutes(row['actual_minutes'] or row['minutes'])})")
    return ToolResult(
        fallback="\n".join(plain),
        tool=LIST_TIME_ENTRIES,
        source=source.source_name,
        data=data,
        row_ids=[r.id for r in rows],
        client_code=client,
        digest=_time_digest(digest_rows, scope=scope, span=span, total_minutes=total_minutes),
        footer=_time_footer(rows, total_minutes=total_minutes, truncated=len(rows) >= _TIME_SUMMARY_FETCH),
        header=f"Period: {period[2]}" if period else None,
        prose_without_figures=True,
    )
