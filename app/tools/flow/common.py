"""Flow ticket helpers shared by several tool modules: the ticket list format, status names,
label lookup, placeholder and automated-sender checks, and worked minutes.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.flow.schemas import TicketRecord, TimeEntryRecord
from app.flow.source import FlowSource
from app.tools.formatting import _activity_text, _created_text, _due_text, _format_hours
from app.tools.turns import _LABEL_RE


def format_ticket_list(
    rows: list[dict[str, Any]],
    *,
    heading: str,
    note: str | None = None,
    extras: tuple[str, ...] = (),
    now: datetime | None = None,
) -> str:
    now = now or datetime.now()
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
        if "stage" in extras and row.get("stage") in _STAGE_TAGS:
            extra = f"{extra} · {_STAGE_TAGS[row['stage']]}"
        if "created" in extras:
            extra = f"{extra} · {_created_text(row.get('created_at'), now)}"
        if "due" in extras:
            extra = f"{extra} · {_due_text(row.get('due_at'), now)}"
        if "activity" in extras:
            extra = f"{extra} · {_activity_text(row.get('last_activity_at'), now)}"
        lines.append(f"- {label} — {topic} ({extra})")
    if note:
        lines.extend(["", note])
    return "\n".join(lines)


_TICKET_STATUSES = {"done": "Done", "new": "New", "email sent": "Email Sent", "client replied": "Client Replied"}


def _resolve_status(raw: str | None) -> str | None:
    """Flow matches status exactly. A model-made 'open' / 'active' matches nothing, so a value that is
    not a real status is dropped; open vs review is the stage filter's job."""
    return _TICKET_STATUSES.get((raw or "").strip().lower())


_STAGE_TAGS = {"open": "open", "review": "in review", "archived": "archived"}


_AUTOMATED_REQUESTOR_RE = re.compile(r"@|\brmm\b|datto|synology|\bnas\b|no-?reply|alert|monitor|backup|\d", re.IGNORECASE)


def _is_placeholder_category(category: str | None) -> bool:
    """'Place Holder' is a catch-all bucket ticket (e.g. a running "Meetings" ticket that
    accumulates unrelated time entries for years) -- never the intended answer to "which
    ticket took the longest," with or without the user saying so explicitly."""
    normalized = re.sub(r"[\s_-]+", "", (category or "").strip().lower())
    return normalized == "placeholder"


def _resolve_label(source: FlowSource, label: str) -> TicketRecord | None:
    match = _LABEL_RE.match(label)
    if match is None:
        return None
    rows = source.list_tickets(client_code=match.group(1).upper(), ticket_num=match.group(2), limit=2)
    return rows[0] if len(rows) == 1 else None


_SUBJECT_PREFIX_RE = re.compile(r"^\|[^|]*\|[^|]*\|\s*")


def _worked_minutes(row: TimeEntryRecord) -> int:
    """Time actually worked; the billed (rounded) minutes only when no actual figure was logged."""
    return row.actual_minutes or row.minutes
