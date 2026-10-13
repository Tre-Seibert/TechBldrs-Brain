"""The agent loop's nudge and fallback messages, and its must-use-a-tool test, from the enabled sources (A7).

Each source's SourceWording names it and gives the topics, tool examples and sample questions the messages use.
With several sources each part is listed across them in enabled order ("Flow or RMM"); a source with no wording
is left out. With only Flow enabled every message is exactly the Flow-only one from before.

Messages the user sees name every enabled source. The nudge goes to the model, so it names only the sources
offered to that call: their tools are the ones it can call.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass

from app.agent.routing import matched_term
from app.sources.base import Sources, SourceSpec
from app.tools.base import ChatTurn

# A first-word write verb ("Close ticket ...", "Email Debe ...", "Merge ...") is a request the model should
# decline or gate, not search for.
_WRITE_INTENT_RE = re.compile(
    r"^\s*(?:please\s+|can you\s+)?(?:close|archive|delete|create|log|send|e-?mail|change|set|update|reassign|add|merge)\b",
    re.IGNORECASE,
)


def or_list(items: Iterable[str]) -> str:
    """'a', 'a or b', 'a, b, or c'; repeats dropped."""
    unique = list(dict.fromkeys(item for item in items if item))
    if len(unique) <= 2:
        return " or ".join(unique)
    return ", ".join(unique[:-1]) + ", or " + unique[-1]


def _quoted(items: Iterable[str]) -> list[str]:
    return [f"'{item}'" for item in items]


@dataclass(frozen=True)
class Wording:
    """The loop's messages for one set of sources."""

    specs: tuple[SourceSpec, ...]

    def _parts(self, field: str) -> list[str]:
        return [item for spec in self.specs if spec.wording for item in getattr(spec.wording, field)]

    @property
    def _titles(self) -> str:
        return or_list(spec.wording.title for spec in self.specs if spec.wording)

    @property
    def no_tool(self) -> str:
        return (
            f"I can only answer from {self._titles} tools, and I did not get a usable result. "
            f"Ask again with {or_list(self._parts('ask_with'))}."
        )

    @property
    def nudge_to_use_a_tool(self) -> str:
        return (
            "You answered without calling a tool, so that answer is unverified. For any question about "
            f"{or_list(self._parts('topics'))} you must call the right tool first (for example "
            f"{', '.join(dict.fromkeys(self._parts('tool_examples')))}) and answer only from its result. Call the tool now."
        )

    @property
    def stuck(self) -> str:
        return (
            f"I kept searching without getting to an answer. Try asking again with {or_list(self._parts('retry_with'))}, "
            f"for example {or_list(_quoted(self._parts('lookup_examples')))}."
        )

    @property
    def no_search_run(self) -> str:
        return (
            f"I didn't run a {self._titles} search for that, so I won't guess. Try rephrasing, for example "
            f"{or_list(_quoted(self._parts('examples')))}."
        )


def wording_for(sources: Sources, only: Collection[str] | None = None) -> Wording:
    """The messages for every enabled source, or for the sources named in `only`, in enabled order."""
    return Wording(tuple(spec for spec in sources.specs if only is None or spec.name in only))


def asks_for_data(spec: SourceSpec, text: str) -> bool:
    """True when `text` is about this source's data: its data words, else its routing keywords and patterns."""
    if spec.data_words is not None:
        return bool(spec.data_words.search(text))
    return matched_term(spec, text) is not None


def expects_tool(turn: ChatTurn, sources: Sources) -> bool:
    """True when this turn is a data question for an enabled source, so an answer with no tool call is unverified.
    The previous assistant message counts too, so a bare "Yes" to "Shall I list ...?" does."""
    text = turn.user_text or ""
    if _WRITE_INTENT_RE.search(text):
        return False
    previous = turn.previous_assistant_text or ""
    return any(asks_for_data(spec, part) for part in (text, previous) for spec in sources.specs)
