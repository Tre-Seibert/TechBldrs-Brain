from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.audit import log_tool_call
from app.config import Settings
from app.knowledge.source import KnowledgeSource
from app.sources.base import Sources, as_sources
from app.tools.base import ChatTurn, ToolResult
from app.tools.dispatch import dispatch
from app.tools.registry import openai_tools

__all__ = ["ChatTurn", "openai_tools", "run_tool"]


def run_tool(
    *,
    sources: Sources | None = None,
    name: str,
    arguments: dict[str, Any],
    actor: str,
    settings: Settings,
    actor_verified: bool = False,
    turn: ChatTurn | None = None,
    knowledge: KnowledgeSource | None = None,
    source: Any = None,
    refusal: str | None = None,
) -> ToolResult:
    """Run one tool on the source that offers it and audit the call. `source` is the pre-A2 keyword for a
    bare Flow client and means Flow only. A `refusal` (source routing: the tool's source was not offered) is
    returned as the error without dispatching, and audited like any failed call."""
    sources = as_sources(sources if sources is not None else source)
    client_code = _client_code_from_args(arguments)
    try:
        if refusal:
            result = ToolResult(
                ok=False, tool=name, source=sources.source_name_for(name), client_code=client_code, error=refusal
            )
        else:
            result = dispatch(sources, name, arguments, turn, knowledge=knowledge)
    except sources.errors as exc:
        result = ToolResult(
            ok=False,
            tool=name,
            source=sources.source_name_for(name),
            client_code=client_code,
            error=str(exc),
        )
    except ValidationError as exc:
        result = ToolResult(
            ok=False,
            tool=name,
            source=sources.source_name_for(name),
            client_code=client_code,
            error=f"Invalid arguments: {exc.errors()}",
        )
    except Exception as exc:  # noqa: BLE001 — tool loop must not crash the agent
        result = ToolResult(
            ok=False,
            tool=name,
            source=sources.source_name_for(name),
            client_code=client_code,
            error=f"{type(exc).__name__}: {exc}",
        )

    log_tool_call(
        actor=actor,
        actor_verified=actor_verified,
        tool=name,
        client_code=result.client_code or client_code,
        row_ids=result.row_ids,
        args=arguments,
        source=result.source,
        ok=result.ok,
        error=result.error,
        log_dir=settings.audit_log_dir,
    )
    return result


def _client_code_from_args(arguments: dict[str, Any]) -> str | None:
    raw = arguments.get("client_code")
    if raw is None:
        return None
    text = str(raw).strip().upper()
    return text or None
