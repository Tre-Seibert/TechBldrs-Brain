"""What every data source looks like to the rest of tb-brain, and the bundle of enabled sources.

Each source lives in app/sources/<name>/ with the same parts: a read-only protocol, stub fixtures, an HTTP
adapter, its tools (OpenAI schemas), the handlers that run them, and a short prompt section. Its spec.py
ties those together as a SourceSpec; app/sources/registry.py turns the enabled ones into a Sources bundle.

Nothing here imports a source or app.tools at module level: sources import app.tools for ToolResult, and
app.tools imports this module, so an import here would loop.
"""

from __future__ import annotations

import functools
import re
import threading
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.config import Settings
    from app.knowledge.source import KnowledgeSource
    from app.sources.clients import ClientResolver
    from app.tools.base import ChatTurn, ToolResult

# A mode that turns an optional source off (DATTO_MODE=off). Required sources (Flow) cannot be off.
OFF_MODES = frozenset({"", "off", "none", "disabled", "false", "0"})


def function_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


@dataclass(frozen=True)
class SourceWording:
    """How the agent loop's nudge and fallback messages name a source (app/agent/wording.py).

    With several sources enabled, each part is listed across them in enabled order: "Flow or RMM",
    "tickets, time, ..., or devices". Write each item so it reads in such a list.
    """

    title: str  # "Flow"
    topics: tuple[str, ...]  # what its data questions are about: "tickets", "time", ...
    tool_examples: tuple[str, ...]  # "list_tickets for lists"
    ask_with: tuple[str, ...]  # what to name when asking again: "a person", "technician", "client code"
    retry_with: tuple[str, ...]  # what to name after the loop got stuck: "a client code", "ticket label"
    examples: tuple[str, ...]  # questions it answers: "my urgent tickets"
    lookup_examples: tuple[str, ...]  # direct lookups: "show me ZTB-1680"


@dataclass(frozen=True)
class SourceSpec:
    """One data source: how to build its read-only client, its tools, and the handler that runs them.

    The client's `source_name` goes into every audit row and ToolResult, so name it after the source and
    mode (e.g. 'datto-http'). Flow's 'stub' / 'http' predate this and stay as they are.
    """

    name: str
    # The source's mode from settings, normalized: 'stub', 'http', or one of OFF_MODES.
    mode: Callable[[Settings], str]
    # A read-only client for that mode. Raises the source's own NotConfigured error when it can't.
    build: Callable[[Settings], Any]
    tools: tuple[dict[str, Any], ...]
    # (client, tool name, raw args, turn, knowledge=) -> ToolResult, or None for a tool it does not have.
    handle: Callable[..., ToolResult | None]
    write_tool_names: tuple[str, ...] = ()
    relay_tool_names: tuple[str, ...] = ()
    # Errors whose message is safe to show as-is (the source refused, or is not configured).
    errors: tuple[type[Exception], ...] = ()
    prompt_section: str = ""
    # The tools with shorter descriptions for PROMPT_STYLE=lean; same names and parameters. Empty means `tools`.
    lean_tools: tuple[dict[str, Any], ...] = ()
    required: bool = False
    # (field, hook) pairs this source fills in for the client resolver, e.g. ("datto_site_uid", hook) where
    # hook(client, client_code, name) returns the source's id for that client or None. See app/sources/clients.py.
    client_id_hooks: tuple[tuple[str, Callable[..., str | None]], ...] = ()
    # Source routing (app/agent/routing.py). One line on what the source holds; the model reads it in the
    # use_source tool. A source with a description is offered only when the question needs it: its keywords
    # (words or phrases, any case, plural too) or patterns (e.g. hostname-like tokens) match the user's latest
    # message, it was used earlier in the conversation, or the model asks for it. A required source (Flow) and
    # one with no description are offered on every call.
    description: str = ""
    keywords: tuple[str, ...] = ()
    patterns: tuple[re.Pattern[str], ...] = ()
    # The agent loop (app/agent/wording.py). `wording` names the source in its nudge and fallback messages; None
    # leaves it out of them. `data_words` marks a question as one for this source's data, so an answer with no
    # tool call is unverified; None uses the routing keywords and patterns above.
    wording: SourceWording | None = None
    data_words: re.Pattern[str] | None = None

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(tool["function"]["name"] for tool in self.tools)

    @property
    def routed(self) -> bool:
        """True when this source is offered per question rather than on every call."""
        return bool(self.description) and not self.required


class Sources:
    """The enabled sources and their clients, in the order their tools are offered to the model."""

    def __init__(self, enabled: list[tuple[SourceSpec, Any]] | tuple[tuple[SourceSpec, Any], ...]) -> None:
        self._enabled = tuple(enabled)
        self._by_tool: dict[str, tuple[SourceSpec, Any]] = {}
        for spec, client in self._enabled:
            for name in spec.tool_names:
                if name in self._by_tool:
                    raise ValueError(f"tool {name!r} is offered by both {self._by_tool[name][0].name} and {spec.name}")
                self._by_tool[name] = (spec, client)
        self._clients: ClientResolver | None = None
        self._clients_lock = threading.Lock()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec, _ in self._enabled)

    @property
    def specs(self) -> tuple[SourceSpec, ...]:
        return tuple(spec for spec, _ in self._enabled)

    def spec_for_tool(self, tool: str) -> SourceSpec | None:
        """The enabled source that offers `tool`; None for a shared or unknown tool."""
        hit = self._by_tool.get(tool)
        return hit[0] if hit else None

    def client(self, name: str) -> Any | None:
        return next((client for spec, client in self._enabled if spec.name == name), None)

    @property
    def flow(self) -> Any:
        """Flow's client. Flow is required, so every bundle has one."""
        client = self.client("flow")
        if client is None:
            raise LookupError("Flow is not enabled")
        return client

    @property
    def clients(self) -> ClientResolver:
        """The client resolver source tools share: Flow plus each enabled source's id hooks, built once."""
        with self._clients_lock:
            if self._clients is None:
                from app.sources.clients import ClientResolver

                resolver = ClientResolver(self.flow)
                for spec, client in self._enabled:
                    for field, hook in spec.client_id_hooks:
                        resolver.register_id_hook(field, functools.partial(hook, client), source=spec.name)
                self._clients = resolver
            return self._clients

    def _offered(self, only: Collection[str] | None) -> tuple[SourceSpec, ...]:
        return tuple(spec for spec, _ in self._enabled if only is None or spec.name in only)

    def openai_tools(self, style: str = "legacy", only: Collection[str] | None = None) -> list[dict[str, Any]]:
        """The tools of every enabled source, or of the sources named in `only`, in enabled order."""
        if style == "lean":
            return [tool for spec in self._offered(only) for tool in (spec.lean_tools or spec.tools)]
        return [tool for spec in self._offered(only) for tool in spec.tools]

    def tool_names(self) -> tuple[str, ...]:
        return tuple(name for spec, _ in self._enabled for name in spec.tool_names)

    def write_tool_names(self) -> tuple[str, ...]:
        return tuple(name for spec, _ in self._enabled for name in spec.write_tool_names)

    def relay_tool_names(self) -> tuple[str, ...]:
        return tuple(name for spec, _ in self._enabled for name in spec.relay_tool_names)

    @property
    def errors(self) -> tuple[type[Exception], ...]:
        return tuple(error for spec, _ in self._enabled for error in spec.errors)

    def prompt_sections(self, only: Collection[str] | None = None) -> list[str]:
        return [spec.prompt_section for spec in self._offered(only) if spec.prompt_section]

    def source_name_for(self, tool: str) -> str:
        """The client that serves `tool`; the first source's for a tool no source has (shared or unknown)."""
        spec_client = self._by_tool.get(tool) or (self._enabled[0] if self._enabled else None)
        return getattr(spec_client[1], "source_name", "unknown") if spec_client else "unknown"

    def dispatch(
        self,
        name: str,
        raw_args: dict[str, Any],
        turn: ChatTurn | None = None,
        *,
        knowledge: KnowledgeSource | None = None,
    ) -> ToolResult | None:
        """Run `name` on the source that offers it. None when no enabled source has that tool."""
        hit = self._by_tool.get(name)
        if hit is None:
            return None
        spec, client = hit
        return spec.handle(client, name, raw_args, turn, knowledge=knowledge)


def as_sources(value: Any) -> Sources:
    """A Sources bundle as-is; anything else is a bare Flow client from before A2, wrapped as Flow only."""
    if isinstance(value, Sources):
        return value
    from app.sources.flow.spec import FLOW

    return Sources([(FLOW, value)])
