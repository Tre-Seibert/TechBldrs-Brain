from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.audit import log_tool_call
from app.config import Settings
from app.flow.source import FlowNotConfigured, FlowSource
from app.tools.handlers import ToolResult, dispatch
from app.tools.registry import openai_tools

__all__ = ["openai_tools", "run_tool"]


def run_tool(
    *,
    source: FlowSource,
    name: str,
    arguments: dict[str, Any],
    actor: str,
    settings: Settings,
) -> ToolResult:
    client_code = _client_code_from_args(arguments)
    try:
        result = dispatch(source, name, arguments)
    except FlowNotConfigured as exc:
        result = ToolResult(
            ok=False,
            tool=name,
            source=getattr(source, "source_name", "unknown"),
            client_code=client_code,
            error=str(exc),
        )
    except ValidationError as exc:
        result = ToolResult(
            ok=False,
            tool=name,
            source=getattr(source, "source_name", "unknown"),
            client_code=client_code,
            error=f"Invalid arguments: {exc.errors()}",
        )
    except Exception as exc:  # noqa: BLE001 — tool loop must not crash the agent
        result = ToolResult(
            ok=False,
            tool=name,
            source=getattr(source, "source_name", "unknown"),
            client_code=client_code,
            error=f"{type(exc).__name__}: {exc}",
        )

    log_tool_call(
        actor=actor,
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
