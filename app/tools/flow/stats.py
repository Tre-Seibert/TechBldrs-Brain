"""ticket_stats: counts and hours grouped by Flow, plus the recurring-issue analysis."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, model_validator

from app.flow.source import FlowRequestError, FlowSource
from app.tools.base import ChatTurn, ToolResult, _refuse
from app.tools.flow.common import _AUTOMATED_REQUESTOR_RE, _is_placeholder_category
from app.tools.flow.people import _resolve_assignee
from app.tools.formatting import _format_minutes
from app.tools.registry import TICKET_STATS
from app.tools.turns import _period_from_turn


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
    reviewed: bool | None = None
    # time only: true = entries with an invoice number, which is what 'billed' means here
    invoiced: bool | None = None
    after: str | None = None
    before: str | None = None
    # issue analysis only: leave out automated alert tickets (default true there)
    exclude_alerts: bool | None = None
    limit: int = 10

    @model_validator(mode="before")
    @classmethod
    def _date_aliases(cls, data: Any) -> Any:
        """Models reuse list_time_entries' work_after/work_before here; a silently dropped date
        range would answer an all-time question instead of 'this month'."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if str(data.get("group_by") or "").strip().lower() in ("issue", "issues", "problem", "problems", "topic_pattern"):
            data["group_by"] = "pattern"
        for canonical, aliases in _STATS_DATE_ALIASES.items():
            if not data.get(canonical):
                for alias in aliases:
                    if data.get(alias):
                        data[canonical] = data[alias]
                        break
        return data


_UNREVIEWED_RE = re.compile(r"\b(?:unreviewed|(?:isn'?t|aren'?t|not|never)\s+(?:been\s+)?reviewed)\b", re.IGNORECASE)
_STATS_PLACEHOLDER_FETCH = 40
_BILLED_RE = re.compile(r"\b(?:billed|bill|invoiced)\b", re.IGNORECASE)
_UNBILLED_RE = re.compile(r"\b(?:unbilled|uninvoiced|not\s+(?:yet\s+)?(?:been\s+)?(?:billed|invoiced))\b", re.IGNORECASE)


def _without_placeholder_tickets(source: FlowSource, stats: dict[str, Any], limit: int) -> dict[str, Any]:
    """'Longest' / 'most time' rankings never answer with a catch-all Place Holder ticket (meetings, out of
    office). Walk the ranking, skip those, keep `limit` real tickets, and say what was left out."""
    kept: list[dict[str, Any]] = []
    left_out: list[str] = []
    for row in stats.get("rows") or []:
        if len(kept) >= limit:
            break
        client, _, number = str(row.get("key") or "").rpartition("-")
        try:
            found = source.list_tickets(client_code=client or None, ticket_num=number, stage="all", limit=1) if client else []
        except FlowRequestError:
            found = []
        if found and _is_placeholder_category(found[0].category):
            left_out.append(str(row["key"]))
            continue
        kept.append(row)
    return {**stats, "rows": kept, "left_out_placeholders": left_out}


def ticket_stats(source: FlowSource, args: TicketStatsArgs, turn: ChatTurn | None = None) -> ToolResult:
    entity_said = (args.entity or "tickets").strip().lower()
    group_said = (args.group_by or "").strip().lower()
    if entity_said == "tickets" and group_said in ("ticket", "tickets", "ticket_label", "label"):
        # "top 5 longest-worked tickets" is a time ranking; tickets cannot be grouped by ticket
        args = args.model_copy(update={"entity": "time", "group_by": "ticket", "metric": "hours"})
        entity_said, group_said = "time", "ticket"
    if entity_said == "time" and group_said == "ticket" and (args.metric or "count").strip().lower() != "hours":
        args = args.model_copy(update={"metric": "hours"})
    said = (turn.user_text if turn else "") or ""
    if entity_said == "time" and _UNBILLED_RE.search(said):
        args = args.model_copy(update={"invoiced": False, "billable": None})
    elif entity_said == "time" and _BILLED_RE.search(said):
        # 'billed' means an invoice number is on the entry; the billable flag is a different thing
        args = args.model_copy(update={"invoiced": True, "billable": None})
    elif entity_said == "time" and args.invoiced is not None:
        args = args.model_copy(update={"invoiced": None})  # the model added a billing filter nobody asked for
    if entity_said == "time" and (args.metric or "count").strip().lower() != "hours":
        args = args.model_copy(update={"metric": "hours"})  # 'who uses the most time' ranks by hours
    if entity_said == "time" and args.reviewed is None and _UNREVIEWED_RE.search(turn.user_text if turn else ""):
        args = args.model_copy(update={"reviewed": False})
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
    period = _period_from_turn(turn)
    if period:
        args = args.model_copy(update={"after": period[0], "before": period[1]})
    issue_analysis = (args.entity or "tickets").strip().lower() == "tickets" and (args.group_by or "").strip().lower() == "pattern"
    defaulted_period = False
    if issue_analysis and not (args.after or args.before):
        from datetime import timedelta

        args = args.model_copy(update={"after": (datetime.now() - timedelta(days=365)).date().isoformat()})
        defaulted_period = True  # an unbounded history is too much to read; say so in the answer
    if issue_analysis and (args.stage or "all").strip().lower() in ("open", "review", "live") and not re.search(
        r"\b(?:open|in\s+review|currently|right\s+now)\b", said, re.IGNORECASE
    ):
        args = args.model_copy(update={"stage": "all"})  # 'repeat issues' needs the history, not just what is open now
    ranks_tickets = entity_said == "time" and group_said == "ticket"
    ranks_requestors = entity_said == "tickets" and group_said == "requestor"
    skip_alerts = bool(args.exclude_alerts) if args.exclude_alerts is not None else (
        issue_analysis and not re.search(r"\balerts?\b", (turn.user_text if turn else "") or "", re.IGNORECASE)
    )
    try:
        stats = source.ticket_stats(
            exclude_alerts=skip_alerts,
            entity=(args.entity or "tickets").strip().lower(),
            group_by=(args.group_by or "client").strip().lower(),
            metric=(args.metric or "count").strip().lower(),
            client_code=client,
            assignee_code=ticket_assignee,
            tech_user_id=tech_user_id,
            stage=(args.stage or "all").strip().lower(),
            direction=(args.direction or "inbound").strip().lower(),
            billable=args.billable,
            reviewed=args.reviewed if entity_said == "time" else None,
            invoiced=args.invoiced if entity_said == "time" else None,
            after=(args.after or "").strip() or None,
            before=(args.before or "").strip() or None,
            limit=25 if issue_analysis else (
                max(args.limit, _STATS_PLACEHOLDER_FETCH) if (ranks_tickets or ranks_requestors) else args.limit
            ),
        )
    except FlowRequestError as exc:
        return _refuse(TICKET_STATS, source, str(exc))
    if ranks_tickets:
        stats = _without_placeholder_tickets(source, stats, args.limit)
    if ranks_requestors:
        # 'who contacts us the most' is people: robots and alert senders are counted separately
        people = [r for r in stats.get("rows") or [] if not _AUTOMATED_REQUESTOR_RE.search(str(r.get("key") or ""))
                  and str(r.get("key")) != "(none)"]
        robots = [str(r["key"]) for r in stats.get("rows") or [] if r not in people and str(r.get("key")) != "(none)"]
        stats = {**stats, "rows": people[: args.limit], "left_out_automated": robots}
    if issue_analysis and stats.get("rows"):
        samples = None
        try:
            samples = source.ticket_samples(
                client_code=client,
                after=(args.after or "").strip() or None,
                before=(args.before or "").strip() or None,
                stage=(args.stage or "all").strip().lower(),
                interval="month",
                per_interval=_SAMPLES_PER_INTERVAL,
                exclude_alerts=skip_alerts,
            )
        except (FlowRequestError, AttributeError):
            samples = None  # an older Flow without the sampling endpoint: fall back to titles only
        return _issue_analysis_result(
            source, stats, client=client, after=args.after, before=args.before, skipped=skip_alerts,
            samples=samples, defaulted_period=defaulted_period,
        )
    return ToolResult(
        tool=TICKET_STATS,
        source=source.source_name,
        data=stats,
        client_code=client,
        note="Counts and hours are computed by Flow. Quote them as given; do not recount.",
        reply=format_stats(
            stats, client=client, assignee=assignee, after=args.after, before=args.before,
            invoiced=bool(args.invoiced) and entity_said == "time",
            unreviewed=args.reviewed is False and entity_said == "time",
        ),
    )


_SAMPLES_PER_INTERVAL = 10


def _sample_chunk(interval: dict[str, Any]) -> dict[str, str]:
    """One month's sampled tickets as a self-contained prompt for the model to boil down."""
    tickets = interval.get("tickets") or []
    lines = [
        f"These are {len(tickets)} real help desk tickets sampled from {interval['label']} "
        f"(out of {interval.get('population', len(tickets))} non-alert tickets that period). For each you see the "
        "title, the cause field, the ticket notes, the client's first email, and the technician's work entries.",
        "List the 3 most common kinds of issue in this sample (fewer if fewer). One short bullet each: the "
        "issue, about how many of these tickets it covers, and what actually happened or fixed it, taken from "
        "the emails, notes and work entries. No introduction and no ticket-by-ticket list.",
        "",
    ]
    for ticket in tickets:
        head = f"[{ticket.get('label')} | {str(ticket.get('created_at') or '')[:10]} | cause: {ticket.get('cause') or 'none'}]"
        lines.append(f"{head} {ticket.get('topic') or '(no title)'}")
        mail = ticket.get("first_email") or {}
        if mail.get("text"):
            who = f" ({mail['from']})" if mail.get("from") else ""
            lines.append(f"  client email{who}: {mail['text'][:320]}")
        if ticket.get("notes"):
            lines.append(f"  notes: {ticket['notes'][:260]}")
        work = "; ".join(
            f"{(e.get('subject') or '').strip()}" + (f" -- {e['notes'][:150]}" if e.get("notes") else "")
            for e in (ticket.get("time_entries") or [])[:2]
        )
        if work:
            lines.append(f"  work done: {work}")
    return {"label": interval["label"], "prompt": "\n".join(lines)}


def _issue_analysis_result(
    source: FlowSource, stats: dict[str, Any], *, client: str | None, after: str | None, before: str | None,
    skipped: bool, samples: dict[str, Any] | None = None, defaulted_period: bool = False,
) -> ToolResult:
    """Recurring issue types from ticket titles, for the model to name and describe in prose."""
    rows = stats["rows"]
    total = int(stats.get("total_count") or 0)
    left_out = int(stats.get("excluded_alerts") or 0)
    scope = " ".join(
        part
        for part in (
            f"for {client}" if client else "across all clients",
            f"({after or 'start'} to {before or 'now'})" if (after or before) else "",
        )
        if part
    )
    intervals = [i for i in ((samples or {}).get("intervals") or []) if i.get("tickets")]
    if defaulted_period:
        scope += " (last 12 months; ask for another period if you want one)"
    lines = [
        f"Ticket issue analysis {scope}: {total} tickets" + (f", {left_out} automated alert tickets left out" if skipped else "")
        + f", grouped into {stats.get('groups', len(rows))} recurring title patterns (top {len(rows)} below).",
        "Using only these groups, write two or three short paragraphs naming the most common kinds of issue "
        "clients reported and what each involves. Merge groups that are really the same issue. Cite ticket counts "
        "for the top issues. Say that this is based on ticket titles and the cause field, not on reading each "
        "ticket's emails or notes.",
        "",
    ]
    if intervals:
        lines[1] = (
            "The title patterns below count every ticket. Findings from reading real tickets follow them."
        )
    for rank, row in enumerate(rows, 1):
        sample_text = " | ".join(f'"{text[:110]}"' for text in (row.get("samples") or [])[:3])
        cause = f", usual cause: {row['cause']}" if row.get("cause") else ""
        lines.append(f"{rank}. [{row['count']} tickets, {row['hours']} h{cause}] {row['key']} -- e.g. {sample_text}")
    table = [f"{rank}. {row['key']} — {row['count']:,} tickets ({row['hours']:g} h)" for rank, row in enumerate(rows[:10], 1)]
    basis = "Basis: ticket titles (cause used as a hint)."
    if intervals:
        per = (samples or {}).get("per_interval", _SAMPLES_PER_INTERVAL)
        basis = (
            f"Basis: counts come from all {total:,} ticket titles; the findings come from reading "
            f"{(samples or {}).get('sampled_total', 0):,} real tickets (up to {per} from each "
            f"{(samples or {}).get('interval', 'month')}) including their emails, notes and time entries."
        )
    footer = (
        f"Analyzed {total:,} tickets {scope}"
        + (f"; left out {left_out:,} automated alert tickets" if skipped and left_out else "")
        + f". {basis}\nTop title patterns:\n"
        + "\n".join(table)
    )
    if intervals:
        return ToolResult(
            tool=TICKET_STATS,
            source=source.source_name,
            data=stats,
            client_code=client,
            digest="\n".join(lines),  # used as-is if the loop cannot run the per-month readings
            footer=footer,
            chunks=[_sample_chunk(interval) for interval in intervals],
            reduce_prefix="\n".join(lines),
            reduce_suffix=(
                f"Now write three or four short paragraphs: the most common kinds of issue overall (use the title "
                f"pattern counts for how common), how they changed from {intervals[0]['label']} to "
                f"{intervals[-1]['label']}, and one or two concrete examples of what actually happened, from the "
                f"findings above. Say this comes from reading about {(samples or {}).get('per_interval', 10)} "
                f"real tickets per {(samples or {}).get('interval', 'month')} plus a count of all ticket titles."
            ),
        )
    return ToolResult(
        tool=TICKET_STATS,
        source=source.source_name,
        data=stats,
        client_code=client,
        digest="\n".join(lines),
        footer=footer,
    )


def format_stats(
    stats: dict[str, Any],
    *,
    client: str | None,
    assignee: str | None,
    after: str | None,
    before: str | None,
    invoiced: bool = False,
    unreviewed: bool = False,
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
    if unreviewed and not invoiced:
        head = "Unreviewed " + head
    if invoiced:
        # billed = the entry has an invoice number; show billed minutes only, never a second 'billed' figure
        where = head[len(f'{entity} by {group_by}'):].lstrip(", ")
        head = f"Billed time by {group_by}" + (f", {where}" if where else "")
        if not rows:
            return (
                f"No billed time found" + (f" ({where})" if where else "")
                + ". An entry counts as billed when it has an invoice number."
            )
        lines = [f"{head}: {_format_minutes(stats.get('total_billed_minutes', 0))} billed across {stats.get('total_count', 0)} entries.", ""]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {row['key']} — {_format_minutes(row['billed_minutes'])} billed ({row['count']} entries)")
        return "\n".join(lines)
    if not rows:
        return f"No {entity} found ({head})."
    if entity == "time":
        # only time worked here: "billed" means an invoice number and has its own invoiced=true answer
        lines = [
            f"{head}: {_format_minutes(stats.get('total_minutes', 0))} logged across {stats.get('total_count', 0)} entries.",
            "",
        ]
        for i, row in enumerate(rows, 1):
            lines.append(
                f"{i}. {row['key']} — {_format_minutes(row['minutes'])} logged "
                f"({row['count']} entr{'y' if row['count'] == 1 else 'ies'})"
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
    robots = stats.get("left_out_automated") or []
    if robots:
        lines.append(f"Left out automated senders: {', '.join(robots[:6])}.")
    left_out = stats.get("left_out_placeholders") or []
    if left_out:
        lines.append(f"Left out placeholder tickets (meetings, out of office): {', '.join(left_out)}.")
    return "\n".join(lines)
