"""Flow as a SourceSpec. Flow is required: FLOW_MODE picks stub or http, and it cannot be off."""

from __future__ import annotations

from app.sources.base import SourceSpec
from app.sources.flow.factory import build_flow_source
from app.sources.flow.handlers.dispatch import handle
from app.sources.flow.lean_tools import LEAN_OPENAI_TOOLS
from app.sources.flow.prompt import PROMPT_SECTION
from app.sources.flow.protocol import FlowNotConfigured, FlowRequestError, FlowWriteRefused
from app.sources.flow.tools import OPENAI_TOOLS, RELAY_TOOL_NAMES, WRITE_TOOL_NAMES

FLOW = SourceSpec(
    name="flow",
    mode=lambda settings: settings.flow_mode_normalized,
    build=build_flow_source,
    tools=tuple(OPENAI_TOOLS),
    handle=handle,
    write_tool_names=WRITE_TOOL_NAMES,
    relay_tool_names=RELAY_TOOL_NAMES,
    errors=(FlowNotConfigured, FlowRequestError, FlowWriteRefused),
    prompt_section=PROMPT_SECTION,
    lean_tools=tuple(LEAN_OPENAI_TOOLS),
    required=True,
    # Required, so offered on every call whatever the question says; the description is for listings.
    description="Flow: tickets, time entries, ticket-attached mail, client contacts, technicians, client accounts.",
)
