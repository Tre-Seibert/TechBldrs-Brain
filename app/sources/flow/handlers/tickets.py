"""Flow ticket lists: list_tickets, latest_ticket and 'tickets about X'."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, model_validator

from app.sources.flow.handlers.common import (
    _AUTOMATED_REQUESTOR_RE,
    _STAGE_TAGS,
    _is_placeholder_category,
    _resolve_status,
    format_ticket_list,
)
from app.sources.flow.handlers.mail import answer_person_mail_question
from app.sources.flow.handlers.payloads import _ticket_payload
from app.sources.flow.handlers.people import _pick_contact, _resolve_assignee, _search_contacts_loose
from app.sources.flow.handlers.stats import TicketStatsArgs, ticket_stats
from app.sources.flow.protocol import FlowRequestError, FlowSource
from app.sources.flow.schemas import TicketRecord, ticket_label
from app.sources.flow.tools import LATEST_TICKET, LIST_TICKETS
from app.tools.base import ChatTurn, ToolResult, _refuse
from app.tools.formatting import _activity_text, _format_hours
from app.tools.turns import (
    _CLIENT_SCOPE_RE,
    _asked_latest_for_person,
    _asked_own_tickets,
    _is_self_token,
    _looks_like_person_name,
    _period_from_turn,
    _person_name_from_turn,
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
_URGENT_RE = re.compile(r"\burgent\b", re.IGNORECASE)
_BILLABLE_RE = re.compile(r"\bbillable\b", re.IGNORECASE)
_OVERDUE_RE = re.compile(r"\boverdue\b", re.IGNORECASE)
_LIST_STAGES = frozenset({"open", "review", "live", "archived", "all"})
INTERNAL_CLIENT_CODES = ("ZTB", "ZINT", "ZAWE", "ZZTB", "ZFRIENDS")
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


class LatestTicketArgs(BaseModel):
    client_code: str | None = None
    contact_id: int | None = None
    requestor: str | None = None
    status: str | None = None
    stage: str | None = None


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
    # true when the user asks to summarize / recap / "what's going on with" these tickets: the model
    # then reads each ticket's notes and recent log and writes prose instead of the tool listing them.
    summarize: bool | None = None
    # true leaves out tickets whose reason is Alert (RMM, SaaS Alerts, RocketCyber and similar).
    exclude_alerts: bool | None = None
    # true = tickets a client started: no alerts, no automated senders (RMM, Datto, NAS, no-reply),
    # no internal clients. Defaults to tickets created today unless the question names a period.
    client_initiated: bool | None = None
    stage: str | None = None
    sort: str | None = None
    order: str | None = None
    limit: int = 100

    @model_validator(mode="after")
    def _need_scope(self) -> ListTicketsArgs:
        if self.client_initiated:
            return self
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


_NO_ACTIVITY_RE = re.compile(
    r"\b(?:no|without|hasn'?t\s+had|haven'?t\s+had|not\s+had)\s+(?:any\s+)?(?:activity|updates?|movement|replies)\b"
    r"[^.?]*?\b(?:in|for|since|over|within|past|last)\s+(?:the\s+)?(?:(?:past|last)\s+)?(\d+|a|an|one|two|three|four)?\s*(day|week|month)s?\b",
    re.IGNORECASE,
)
_WORD_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4}


def _no_activity_cutoff(turn: ChatTurn | None, now: datetime | None = None) -> str | None:
    """'no activity in 1 week' is a date the code works out, never the model."""
    match = _NO_ACTIVITY_RE.search(turn.user_text if turn else "")
    if not match:
        return None
    count_text = (match.group(1) or "1").lower()
    count = int(count_text) if count_text.isdigit() else _WORD_NUMBERS.get(count_text, 1)
    days = {"day": 1, "week": 7, "month": 30}[match.group(2).lower()] * count
    return ((now or datetime.now()) - timedelta(days=days)).replace(microsecond=0).isoformat()


def _complete_from_turn(turn: ChatTurn | None) -> bool | None:
    text = turn.user_text if turn else ""
    if text and _INCOMPLETE_RE.search(text):
        return False
    return None


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


_SEARCH_STOPWORDS = frozenset(
    "issue issues problem problems ticket tickets about with for the and any from related regarding "
    "support help need needs".split()
)
_OPEN_WORD_RE = re.compile(r"\bopen\b", re.IGNORECASE)
_NOT_OPEN_RE = re.compile(r"\b(?:review|archived?|closed|all)\b", re.IGNORECASE)
_STAGE_LABELS = {
    "open": "open",
    "review": "in-review",
    "archived": "archived",
    "live": "open and in-review",
    "all": "",
}


def _stem(word: str) -> str:
    """imaging / images / imaged -> imag, so a search for one finds the others (and 'reimage')."""
    low = word.lower()
    if any(ch.isdigit() or ch == "-" for ch in low):
        return low  # labels and codes are matched exactly
    for suffix, minimum in (("ing", 6), ("ies", 5), ("es", 5), ("ed", 5), ("s", 4)):
        if low.endswith(suffix) and len(low) >= minimum:
            return low[: -len(suffix)]
    return low


def _search_terms(query: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", query)
    terms: list[str] = []
    for word in words:
        if word.lower() in _SEARCH_STOPWORDS or len(word) < 3:
            continue
        stem = _stem(word)
        if stem not in terms:
            terms.append(stem)
    return terms[:3]


def _text_search(run: Any, query: str) -> list[TicketRecord]:  # noqa: C901
    """Word-by-word, stemmed text search. Flow only does one substring match, so each word is searched
    on its own and the results are intersected (every word must appear)."""
    terms = _search_terms(query)
    if not terms:
        return run(query)
    found: list[list[TicketRecord]] = [run(term) for term in terms]
    keep = set.intersection(*({row.id for row in rows} for rows in found))
    merged = [row for row in found[0] if row.id in keep]
    return merged or (run(query) if len(terms) > 1 else merged)


def _is_client_initiated(row: TicketRecord) -> bool:
    """A ticket a client person started: not an alert, not an automated sender, not an internal client."""
    if (row.reason or "").strip().lower() == "alert":
        return False
    if (row.client_code or "").upper() in INTERNAL_CLIENT_CODES:
        return False
    who = (row.requestor_text or "").strip()
    return bool(who) and not _AUTOMATED_REQUESTOR_RE.search(who)


def _client_initiated_tickets(source: FlowSource, args: ListTicketsArgs, turn: ChatTurn | None) -> ToolResult:
    """Tickets created in a period (today unless the question says otherwise) that a client started."""
    period = _period_from_turn(turn)
    if period:
        after, before, label = period[0], period[1], period[2]
    elif (args.created_after or "").strip():
        after, before = args.created_after.strip(), (args.created_before or "").strip() or None
        label = f"since {after}"
    else:
        after, before, label = datetime.now().date().isoformat(), None, "today"
    from datetime import timedelta

    today = datetime.now().date()
    if after == today.isoformat() and before in (None, (today + timedelta(days=1)).isoformat()):
        label = "today"
    codes = _client_codes(args.client_code)
    rows = source.list_tickets(
        client_code=codes[0] if len(codes) == 1 else None,
        created_after=after,
        created_before=before,
        stage="all",
        sort="created_at",
        order="desc",
        limit=100,
    )
    kept = [row for row in rows if _is_client_initiated(row)]
    left_out = len(rows) - len(kept)
    data = [_ticket_payload(row) for row in kept]
    heading = f"{len(kept)} client-initiated ticket(s) created {label}"
    reply = format_ticket_list(data, heading=heading, extras=("created",)) if kept else f"No client-initiated tickets created {label}."
    if left_out:
        reply += f"\n\nLeft out {left_out} alert or automated ticket(s) (RMM, Datto, NAS, SaaS Alerts, no-reply)."
    return ToolResult(
        tool=LIST_TICKETS,
        source=source.source_name,
        data=data,
        row_ids=[row.id for row in kept],
        note=f"{len(kept)} ticket(s).",
        reply=reply,
    )


def list_tickets(
    source: FlowSource,
    args: ListTicketsArgs,
    turn: ChatTurn | None = None,
    *,
    force_text_query: bool = False,
) -> ToolResult:
    if args.client_initiated:
        return _client_initiated_tickets(source, args, turn)
    mail_answer = answer_person_mail_question(source, turn)
    if mail_answer is not None:
        return mail_answer
    if (args.sort or "").strip().lower() == "hrs_actual_total" and _period_from_turn(turn):
        # 'the longest ticket last week' is time logged in that week, not a ticket's all-time total
        return ticket_stats(
            source,
            TicketStatsArgs(
                entity="time", group_by="ticket", metric="hours", client_code=args.client_code,
                assignee_code=args.assignee_code, limit=10,
            ),
            turn,
        )
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
    person_from_q = False
    if not force_text_query and not person and raw_q and _looks_like_person_name(raw_q):
        person = raw_q  # a guess: two plain words might be a name, or might be "printer issues"
        person_from_q = True
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
        elif person_from_q:
            person = None  # no contact by that name: search the words as text instead of as a requestor
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
            stage = "live"  # a person's tickets: open and in review, never archived unless asked
    if _asked_latest_for_person(turn) and (args.stage or "").strip().lower() not in (
        "review",
        "archived",
        "all",
    ):
        stage = "live"
    stage_given = (args.stage or "").strip().lower() in ("open", "review", "live", "archived", "all")
    said = turn.user_text if turn else ""
    searching = bool(raw_q or person_label or contact_id is not None)
    if searching and (args.stage or "").strip().lower() == "open" and not _OPEN_WORD_RE.search(said):
        stage = "live"  # "tickets about X" / "tickets for Y": open and in review, unless they said open
    if not stage_given and stage in ("live", "open") and _OPEN_WORD_RE.search(said) and not _NOT_OPEN_RE.search(said):
        stage = "open"  # the user said "open": never hand back review tickets
    limit = 1 if _asked_latest_for_person(turn) else args.limit
    resolved_sort = (args.sort or "last_activity_at").strip() or "last_activity_at"
    # Placeholder tickets get filtered out *after* fetching below, which would just
    # shrink an already-capped page -- overfetch here so trimming to the caller's
    # real limit still happens after that filter, not before it.
    fetch_limit = max(limit, 100) if (resolved_sort == "hrs_actual_total" and not category) else limit
    no_activity_before = _no_activity_cutoff(turn)

    def run(query: str | None, stage_override: str | None = None) -> list[TicketRecord]:
        return source.list_tickets(
            client_code=client,
            assignee_code=assignee,
            query=query,
            contact_id=contact_id,
            status=_resolve_status(args.status),
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
            last_activity_before=no_activity_before or (args.last_activity_before or "").strip() or None,
            last_activity_after=(args.last_activity_after or "").strip() or None,
            requestor=requestor,
            needs_response=args.needs_response,
            unassigned=args.unassigned,
            stage=stage_override or stage,
            sort=resolved_sort,
            order=(args.order or "desc").strip() or "desc",
            limit=fetch_limit,
        )

    rows = _text_search(run, raw_q) if raw_q and not force_text_query else run(raw_q)
    if resolved_sort == "hrs_actual_total" and not category:
        # A catch-all bucket ticket (e.g. a running "Meetings" ticket) always wins a
        # by-hours ranking otherwise -- never the real answer, whether the model got
        # here through answer_longest_time_worked's own phrasing or by calling this
        # tool directly for some other "which ticket took the most X" question.
        # Skipped only if the user is deliberately asking for that category by name.
        rows = [r for r in rows if not _is_placeholder_category(r.category)][:limit]
    if args.exclude_alerts:
        rows = [r for r in rows if (r.reason or "").strip().lower() != "alert"]
    wanted_num = (args.ticket_num or "").strip()
    archived_hit = False
    if not rows and client and wanted_num and not raw_q and stage in ("live", "open", "review"):
        # A specific ticket number that is not live: only now look in the archive.
        rows = run(None, "archived")
        archived_hit = bool(rows)
    codes = sorted({(r.client_code or "") for r in rows if r.client_code})
    scoped = client or (codes[0] if len(codes) == 1 else None)
    archive_offer = None
    searched = bool(raw_q or person_label or contact_id is not None)  # not for overdue / needs-reply style filters
    if not rows and searched and stage in ("live", "open", "review") and not _asked_latest_for_person(turn):
        archive_offer = _archive_offer(source, run, raw_q, stage)
    note = f"{len(rows)} ticket(s)."
    if len(rows) >= max(args.limit, 1) and args.limit > 1:
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
    label = _STAGE_LABELS.get(stage, stage)
    sl = f"{label} " if label else ""
    if latest_person:
        heading = f"Latest ticket for {who}" if rows else f"No ticket found for {who}."
    elif not rows:
        if column_label:
            heading = f"No {sl}{who} tickets."
        elif assignee:
            heading = f"No {sl}tickets assigned to {who}."
        else:
            heading = f"No {sl}tickets for {who}."
    elif column_label:
        heading = f"{len(rows)} {sl}{who} ticket(s)"
    else:
        heading = f"{len(rows)} {sl}ticket(s) for {who}"
    if raw_q and not person_label and not column_label and not latest_person:
        scope = f" for {client}" if client else (f" for {assignee}" if assignee else "")
        heading = (
            f"{len(rows)} {sl}ticket(s) about '{raw_q}'{scope}"
            if rows
            else f"No {sl}tickets about '{raw_q}'{scope}."
        )
    if client and wanted_num and not raw_q and not latest_person:
        ticket_name = f"{client}-{wanted_num}"
        if archived_hit:
            heading = f"{ticket_name} is an archived ticket (not found in live tickets)"
        elif not rows:
            heading = f"No ticket found for {ticket_name}."
    if rows and stage in ("live", "all") and not latest_person:
        counts = {key: sum(1 for row in data if row.get("stage") == key) for key in _STAGE_TAGS}
        parts = [f"{n} {_STAGE_TAGS[k]}" for k, n in counts.items() if n]
        if parts:
            heading += " (" + ", ".join(parts) + ")"
    if args.needs_response and not latest_person:
        whose = assignee or client or "you"
        heading = (
            f"{len(rows)} {stage} ticket(s) for {whose} waiting on a reply"
            if rows
            else f"No {stage} tickets for {whose} are waiting on a reply."
        )
    if args.summarize and rows and not latest_person:
        return _ticket_summary_result(source, rows, data, heading=heading, scoped=scoped)
    extras: list[str] = []
    if stage in ("live", "all"):
        extras.append("stage")
    if overdue or args.due_before or args.due_after:
        extras.append("due")
    if args.last_activity_before or args.last_activity_after or args.needs_response:
        extras.append("activity")
    reply = format_ticket_list(data, heading=heading, note=note if rows else None, extras=tuple(extras))
    if archive_offer:
        reply = f"{reply}\n\n{archive_offer}"
    return ToolResult(
        tool=LIST_TICKETS,
        source=source.source_name,
        data=data,
        row_ids=[r.id for r in rows],
        client_code=scoped,
        note=note,
        reply=reply,
    )


def _archive_offer(source: FlowSource, run: Any, raw_q: str | None, stage: str) -> str | None:
    """When nothing live matched, say whether the archives have matches and ask before showing them."""
    try:
        found = _text_search(lambda q: _run_archived(source, run, q), raw_q) if raw_q else _run_archived(source, run, None)
    except FlowRequestError:
        return None
    if not found:
        return "I also checked the archives and found nothing there either."
    return f"I found {len(found)} archived ticket(s) that match. Want me to show them?"


def _run_archived(source: FlowSource, run: Any, query: str | None) -> list[TicketRecord]:
    return run(query, "archived")


_SUMMARY_MAX_TICKETS = 15


def _ticket_summary_result(
    source: FlowSource, rows: list[TicketRecord], data: list[dict[str, Any]], *, heading: str, scoped: str | None
) -> ToolResult:
    """Digest for the model to summarize: each ticket's notes and the tail of its log, not just titles."""
    now = datetime.now()
    shown = rows[:_SUMMARY_MAX_TICKETS]
    lines = [
        f"{heading}.",
        "Write a short plain-English summary of these tickets: one or two sentences per ticket on what it "
        "was about, where it stands, and who handled it. Group routine automated tickets (alerts, digests, "
        "phishing or junk reports) into a single sentence. Name tickets by label. Use only the facts below.",
        "",
    ]
    for row in shown:
        notes = tail = ""
        try:
            detail = source.get_ticket(ticket_id=row.id)
            notes = " ".join((detail.notes_text or "").split())[:250]
            log_lines = [line.strip() for line in (detail.log_text or "").splitlines() if line.strip()]
            tail = " | ".join(log_lines[-3:])[:350]
        except FlowRequestError:
            pass
        facts = [
            ticket_label(row.client_code, row.ticket_num),
            row.status or "no status",
            row.category or "no category",
            row.assignee_code or "unassigned",
        ]
        if row.hrs_actual_total:
            facts.append(_format_hours(float(row.hrs_actual_total)))
        facts.append(_activity_text(row.last_activity_at.isoformat(sep=" "), now))
        topic = (row.topic or row.subject or "").strip() or "(no topic)"
        lines.append(f"[{' | '.join(facts)}] {topic}")
        if notes:
            lines.append(f"    notes: {notes}")
        if tail:
            lines.append(f"    recent log: {tail}")
    extra = f" (summarized the {_SUMMARY_MAX_TICKETS} most recently active)" if len(rows) > len(shown) else ""
    footer = f"{len(rows)} ticket(s){extra}: " + ", ".join(ticket_label(r.client_code, r.ticket_num) for r in rows)
    return ToolResult(
        fallback=format_ticket_list(data, heading=heading),
        tool=LIST_TICKETS,
        source=source.source_name,
        data=data,
        row_ids=[r.id for r in rows],
        client_code=scoped,
        digest="\n".join(lines),
        footer=footer,
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
    said_text = turn.user_text if turn else ""
    # "Internal tickets" means the internal clients (the important part) OR any ticket whose reason is
    # Internal, whichever client it belongs to. Run both and merge, so the answer does not depend on
    # whether the model happened to add a reason filter.
    internal = set(codes) <= set(INTERNAL_CLIENT_CODES) and (
        re.search(r"\binternal\b", said_text, re.IGNORECASE) or "internal" in (args.client_code or "").lower()
    )
    shared: dict[str, Any] = {}
    if internal:
        codes = list(INTERNAL_CLIENT_CODES)  # every internal client, even if the model named only some
        if (args.reason or "").strip().lower() == "internal":
            shared["reason"] = None  # the reason query below covers it
    results = [
        list_tickets(source, args.model_copy(update={"client_code": code, **shared}), turn) for code in codes
    ]
    if internal:
        results.append(
            list_tickets(source, args.model_copy(update={"client_code": None, "reason": "Internal"}), turn)
        )
    failed = next((r for r in results if not r.ok), None)
    if failed is not None:
        return failed
    seen: set[Any] = set()
    data = []
    for r in results:
        for row in r.data or []:
            if row.get("id") not in seen:
                seen.add(row.get("id"))
                data.append(row)
    data.sort(key=lambda row: (row.get("last_activity_at") or "", row.get("id") or 0), reverse=True)
    available = len(data)
    data = data[: max(args.limit, 1)]
    said = turn.user_text if turn else ""
    stage = (args.stage or "").strip().lower()
    if not stage:
        if args.assignee_code or (_OPEN_WORD_RE.search(said) and not _NOT_OPEN_RE.search(said)):
            stage = "open"
        else:
            stage = "live"
    label = "the internal clients or reason Internal" if internal else ", ".join(codes)
    stage_label = _STAGE_LABELS.get(stage, stage)
    sl = f"{stage_label} " if stage_label else ""
    heading = f"{len(data)} {sl}ticket(s) for {label}" if data else f"No {sl}tickets for {label}."
    if data and stage in ("live", "all"):
        counts = {key: sum(1 for row in data if row.get("stage") == key) for key in _STAGE_TAGS}
        parts = [f"{n} {_STAGE_TAGS[k]}" for k, n in counts.items() if n]
        if parts:
            heading += " (" + ", ".join(parts) + ")"
    note = f"{len(data)} ticket(s) across {label}."
    if available > len(data):
        note = f"Showing the {len(data)} most recently active of {available}. Ask for one client or a narrower filter to see the rest."
    extras = ("stage",) if stage in ("live", "all") else ()
    return ToolResult(
        tool=LIST_TICKETS,
        source=source.source_name,
        data=data,
        row_ids=[row["id"] for row in data],
        client_code=None,
        note=note,
        reply=format_ticket_list(data, heading=heading, note=note if data else None, extras=extras),
    )


_REVIEW_WORD_RE = re.compile(r"\breview\b", re.IGNORECASE)
_ARCHIVED_WORD_RE = re.compile(r"\barchived?\b", re.IGNORECASE)


def _latest_stage(args: LatestTicketArgs, turn: ChatTurn | None) -> str:
    """Which kind of ticket 'the latest ticket' means. The user's words win over the model's argument:
    archived / open / review when they said so, otherwise open + in review (live)."""
    said = (turn.user_text if turn else "") or ""
    if _ARCHIVED_WORD_RE.search(said):
        return "archived"
    if _OPEN_WORD_RE.search(said):
        return "open"
    if _REVIEW_WORD_RE.search(said):
        return "review"
    # The model fills in "open" for plain 'last ticket' questions, which would hide 9 REVIEW tickets, so
    # only "archived" is taken from its arguments; everything else comes from the user's words.
    return "archived" if (args.stage or "").strip().lower() == "archived" else "live"


def latest_ticket(source: FlowSource, args: LatestTicketArgs, turn: ChatTurn | None = None) -> ToolResult:
    """Single most recent ticket for a client code, or for a named person from the turn.

    Live tickets are searched first. The archive is searched only when the user asked for archived
    tickets, or when nothing live matched (and the reply says so).
    """
    mail_answer = answer_person_mail_question(source, turn)
    if mail_answer is not None:
        return mail_answer
    stage = _latest_stage(args, turn)
    person = _person_name_from_turn(turn) or (args.requestor or "").strip() or None
    if person:
        person_turn = turn or ChatTurn(user_text=f"latest ticket involving {person}")
        result = list_tickets(
            source,
            ListTicketsArgs(contact_id=args.contact_id, requestor=person, stage=stage, sort="created_at", limit=1),
            person_turn,
        )
        if not result.data and stage != "archived":
            archived = list_tickets(
                source,
                ListTicketsArgs(contact_id=args.contact_id, requestor=person, stage="archived", sort="created_at", limit=1),
                person_turn,
            )
            if archived.data:
                archived.reply = f"Nothing open or in review matched, so this is the latest archived ticket.\n\n{archived.reply}"
                return archived
        return result
    code = (args.client_code or "").strip().upper()
    if not code:
        return _refuse(
            LATEST_TICKET,
            source,
            "latest_ticket needs a client_code or a person's name (e.g. latest ticket involving Thomas Carter).",
        )

    def newest(wanted: str) -> list[TicketRecord]:
        return source.list_tickets(
            client_code=code,
            contact_id=args.contact_id,
            status=_resolve_status(args.status),
            stage=wanted,
            sort="created_at",  # "the last BUCK ticket" is the newest one opened
            order="desc",
            limit=1,
        )

    rows = newest(stage)
    fallback_note = ""
    if not rows and stage != "archived":
        rows = newest("archived")
        if rows:
            fallback_note = f"Nothing open or in review matched for {code}, so this is the latest archived ticket.\n\n"
    if not rows:
        none = f"No archived ticket found for {code}." if stage == "archived" else f"No ticket found for {code}."
        return ToolResult(
            tool=LATEST_TICKET,
            source=source.source_name,
            data=None,
            row_ids=[],
            client_code=code,
            note="No matching ticket.",
            reply=none,
        )
    row = rows[0]
    data = _ticket_payload(row)
    archived_row = data.get("stage") == "archived"
    heading = f"Latest {'archived ' if archived_row else ''}ticket for {code}"
    return ToolResult(
        tool=LATEST_TICKET,
        source=source.source_name,
        data=data,
        row_ids=[row.id],
        client_code=row.client_code.upper(),
        reply=fallback_note + format_ticket_list([data], heading=heading, extras=("created", "activity")),
    )


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
