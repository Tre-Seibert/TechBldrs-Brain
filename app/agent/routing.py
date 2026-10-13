"""Source routing: which sources' tools each model call is offered (SOURCE_ROUTING=on|off).

Required sources (Flow) and sources with no description, plus the shared tools (search_knowledge), are
offered on every call. A routed source (SourceSpec.routed) is offered when:

- keyword:   the user's latest message matches its keywords or patterns;
- sticky:    an earlier user message matched them, or one of its tools was called earlier in the
             conversation, so "what about BUCK?" keeps the source the last question used;
- requested: the model called use_source(name). That tool is offered only while an enabled source is
             hidden; the source's tools (and, with PROMPT_STYLE=lean, its prompt section) join the next
             loop iteration, which the tool call starts anyway, so asking costs no LLM call of its own.

A call to a tool of a hidden source is refused, not dispatched. With no routed source enabled (Flow only)
or SOURCE_ROUTING=off every enabled source is offered, which is exactly the tool list from before routing.

Cache cost (lean): the system message is the base rules, then the offered sources' sections in enabled order,
and the model server renders the tool list after it. A source joining mid-conversation keeps the base and the
sections before its own cached, and re-reads its section, the whole tool list, the session context and the
conversation, once. Sticky routing keeps the set stable afterwards, so later turns hit the cache again.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

from app.sources.base import Sources, SourceSpec, function_tool
from app.tools import ToolResult, openai_tools

USE_SOURCE = "use_source"

# Why a source was offered, as recorded in x_tb_brain.routing.
ALWAYS, KEYWORD, STICKY, REQUESTED, ROUTING_OFF = "always", "keyword", "sticky", "requested", "routing_off"


@functools.lru_cache(maxsize=64)
def _keyword_re(keywords: tuple[str, ...]) -> re.Pattern[str] | None:
    """One regex for a source's keywords: any case, whole words, spaces or hyphens between words, plural too."""
    words = [word.strip() for word in keywords if word.strip()]
    if not words:
        return None
    alternatives = "|".join(
        r"[\s-]+".join(map(re.escape, re.split(r"[\s-]+", word))) for word in sorted(words, key=len, reverse=True)
    )
    return re.compile(rf"(?<!\w)(?:{alternatives})(?:e?s)?(?!\w)", re.IGNORECASE)


def matched_term(spec: SourceSpec, text: str) -> str | None:
    """The first keyword or pattern of `spec` found in `text`, as written there; None when nothing matches."""
    if not text:
        return None
    keywords = _keyword_re(spec.keywords)
    hit = keywords.search(text) if keywords else None
    if hit:
        return hit.group(0)
    for pattern in spec.patterns:
        hit = pattern.search(text)
        if hit:
            return hit.group(0)
    return None


@dataclass
class Offer:
    source: str
    why: str
    matched: str | None = None

    def record(self) -> dict[str, Any]:
        out: dict[str, Any] = {"source": self.source, "why": self.why}
        if self.matched:
            out["matched"] = self.matched
        return out


class Routing:
    """The sources offered to the model for one request; grows when the model calls use_source."""

    def __init__(self, sources: Sources, offers: list[Offer], *, enabled: bool) -> None:
        self.sources = sources
        self.enabled = enabled
        self._offers = {offer.source: offer for offer in offers}
        self.tool_counts: list[int] = []

    @property
    def active(self) -> bool:
        """True when routing had a choice to make (a routed source is enabled); only then is it recorded."""
        return any(spec.routed for spec in self.sources.specs)

    @property
    def names_another_source(self) -> bool:
        """True when the user's latest message matched a routed source's keywords (a Datto device question)."""
        return any(offer.why == KEYWORD for offer in self._offers.values())

    @property
    def offered(self) -> tuple[str, ...]:
        return tuple(name for name in self.sources.names if name in self._offers)

    @property
    def hidden(self) -> tuple[SourceSpec, ...]:
        return tuple(spec for spec in self.sources.specs if spec.name not in self._offers)

    def openai_tools(self, style: str) -> list[dict[str, Any]]:
        """The offered sources' tools, the shared ones, then use_source while any source is hidden."""
        tools = openai_tools(self.sources, style=style, only=self.offered)
        hidden = self.hidden
        if hidden:
            tools.append(use_source_tool(hidden))
        return tools

    def tools_after_digest(self, style: str) -> list[dict[str, Any]]:
        """The tools still offered once a tool returned a digest to write the answer from: the offered routed
        sources' tools, then use_source while any source is hidden. Not Flow's (or another required source's), so
        a summary is written rather than searched again, but a cross-source question ("show me WDON-1842 and the
        state of the machine on it") can still look the machine up. Empty with only Flow enabled."""
        routed = [spec.name for spec in self.sources.specs if spec.routed and spec.name in self._offers]
        tools = self.sources.openai_tools(style, only=routed) if routed else []
        hidden = self.hidden
        if hidden:
            tools.append(use_source_tool(hidden))
        return tools

    def prompt_sections(self) -> list[str]:
        return self.sources.prompt_sections(only=self.offered)

    def refusal(self, tool: str, offered_tools: Collection[str]) -> str | None:
        """Why `tool` may not run: it belongs to an enabled source whose tools were not in `offered_tools`, the
        list the model was given for this call. A source use_source added in the same reply counts from the next
        call on. None for a tool that may run, and for shared or unknown tools (dispatch handles those)."""
        spec = self.sources.spec_for_tool(tool)
        if spec is None or tool in offered_tools:
            return None
        return (
            f"{tool} is a {spec.name} tool and {spec.name} was not offered for this question, so it did not run. "
            f'If the question needs {spec.name}, call {USE_SOURCE}(name="{spec.name}") first.'
        )

    def use_source(self, arguments: dict[str, Any]) -> ToolResult:
        """Run the use_source meta tool: offer the named hidden source from the next model call on."""
        name = str(arguments.get("name") or "").strip().lower()
        spec = next((spec for spec in self.sources.specs if spec.name == name), None)
        if spec is None:
            known = ", ".join(spec.name for spec in self.hidden) or "none"
            return ToolResult(
                ok=False, tool=USE_SOURCE, source="tb-brain", error=f"Unknown source {name!r}. Hidden sources: {known}."
            )
        if name in self._offers:
            return ToolResult(
                ok=True, tool=USE_SOURCE, source="tb-brain", data={"source": name, "tools": list(spec.tool_names)},
                note=f"{name} is already available. Call its tools directly.",
            )
        self._offers[name] = Offer(name, REQUESTED)
        return ToolResult(
            ok=True, tool=USE_SOURCE, source="tb-brain", data={"source": name, "tools": list(spec.tool_names)},
            note=f"{name} added. Its tools are available now: call the one that answers the question.",
        )

    def record(self) -> dict[str, Any]:
        """x_tb_brain.routing: what was offered and why, what stayed hidden, and how many tools each call had."""
        return {
            "enabled": self.enabled,
            "offered": [self._offers[name].record() for name in self.offered],
            "hidden": [spec.name for spec in self.hidden],
            "tool_counts": list(self.tool_counts),
        }


def use_source_tool(hidden: Sequence[SourceSpec]) -> dict[str, Any]:
    listing = " ".join(f"{spec.name}: {spec.description}" for spec in hidden)
    return function_tool(
        USE_SOURCE,
        "Add another data source's tools when the question needs data the tools you have do not cover. "
        f"Available: {listing} Its tools arrive with the tool result; then call the one that answers.",
        {
            "type": "object",
            "properties": {"name": {"type": "string", "enum": [spec.name for spec in hidden]}},
            "required": ["name"],
        },
    )


def route(
    sources: Sources,
    *,
    latest: str,
    earlier: Sequence[str] = (),
    earlier_tools: Collection[str] = (),
    enabled: bool = True,
) -> Routing:
    """Pick the sources for one request from the user's latest message, the earlier user messages, and the
    tools already called in the conversation."""
    offers: list[Offer] = []
    for spec in sources.specs:
        if not spec.routed:
            offers.append(Offer(spec.name, ALWAYS))
            continue
        if not enabled:
            offers.append(Offer(spec.name, ROUTING_OFF))
            continue
        term = matched_term(spec, latest)
        if term:
            offers.append(Offer(spec.name, KEYWORD, term))
            continue
        used = next((tool for tool in spec.tool_names if tool in earlier_tools), None)
        term = used or next((hit for hit in (matched_term(spec, text) for text in reversed(earlier)) if hit), None)
        if term:
            offers.append(Offer(spec.name, STICKY, term))
    return Routing(sources, offers, enabled=enabled)
