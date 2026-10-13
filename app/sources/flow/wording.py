"""How the agent loop talks about Flow in its nudge and fallback messages, and which questions need Flow data.

Each part is the Flow-only wording the loop used before it became source-aware (A7), so with only Flow enabled
the messages and the must-use-a-tool decisions are unchanged.
"""

from __future__ import annotations

import re

from app.sources.base import SourceWording

WORDING = SourceWording(
    title="Flow",
    topics=("tickets", "time", "mail", "contacts", "clients"),
    tool_examples=("list_tickets for lists", "ticket_stats for hours or counts"),
    ask_with=("a person", "technician", "client code"),
    retry_with=("a client code", "ticket label"),
    examples=("my urgent tickets", "open tickets for BUCK"),
    lookup_examples=("show me ZTB-1680",),
)

# Questions that must be answered from a Flow tool. Ticket labels count too.
DATA_WORDS = re.compile(
    r"\b(?:tickets?|urgent|overdue|assigned|hours?|time|logged|worked|e-?mails?|mail|reach(?:ed)? out|"
    r"contacts?|clients?|machines?|respond|queue|open|review|archived|billable|cause|problem|unassigned|work(?:ed|ing)?|spent)\b"
    r"|\b[A-Z][A-Z0-9]{1,8}-[A-Z0-9]{3,6}\b",
    re.IGNORECASE,
)
