"""What every tool returns (ToolResult) and what it is told about the chat turn (ChatTurn)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from app.flow.source import FlowSource


@dataclass(frozen=True)
class ChatTurn:
    """What the user just said, and what the assistant said right before it.

    The merge gate reads this, never the model's arguments alone: a write
    needs the plan shown first and then restated by the human.
    """

    user_text: str = ""
    previous_assistant_text: str = ""


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
    # Summary mode (list_time_entries): the model reads `digest` and writes prose; `footer` holds the
    # exact numbers and is appended after the model's text so the totals never come from the model.
    digest: str | None = None
    header: str | None = None
    footer: str | None = None
    # Map-reduce summaries: each chunk is a small prompt the loop answers on its own (one per month),
    # then reduce_prefix + the answers + reduce_suffix become the digest the final answer is written from.
    chunks: list[dict[str, str]] | None = None
    reduce_prefix: str | None = None
    reduce_suffix: str | None = None
    # Plain list shown instead of prose if the model never produces a usable summary.
    fallback: str | None = None
    # True when the digest carries no figures (time summaries): any hours/minutes the model still writes
    # are invented, so the loop strips them and keeps only paragraphs that name a ticket from the digest.
    prose_without_figures: bool = False


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
