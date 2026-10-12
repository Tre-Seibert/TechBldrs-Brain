"""Duplicate tickets: find_similar_tickets (suggestions) and merge_tickets, the one gated write."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from app.sources.flow.handlers.common import _resolve_label, _resolve_status
from app.sources.flow.handlers.payloads import _similar_payload
from app.sources.flow.handlers.people import _resolve_assignee
from app.sources.flow.protocol import FlowSource
from app.sources.flow.schemas import TicketRecord
from app.sources.flow.tools import FIND_SIMILAR_TICKETS, MERGE_TICKETS
from app.tools.base import ChatTurn, ToolResult, _refuse
from app.tools.turns import _OWN_TICKETS_RE


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
        status=_resolve_status(args.status),
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
