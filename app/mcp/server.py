"""The MCP endpoints: one low-level MCP server per endpoint, served over Streamable HTTP inside the FastAPI app.

Endpoints: /mcp and /mcp/all offer every enabled source's read tools; /mcp/<source> (flow, datto, m365) offers one
source's, so each can be attached to its own child agent later without a code change. The routes exist from import
(MCP_ROUTES); the servers and their session managers are created when the app's lifespan runs (`mcp_runtime`), because
a session manager can only be run once and the test client starts the app many times.

A request is checked by the MCP_AUTH verifier first (401 otherwise), then the caller becomes the audit actor and the
signed-in identity for the tool call, exactly as app/main.py does for the chat route. Stateless mode: no session ids,
no SSE streams, one JSON response per request, which is what works best behind nginx and across Copilot Studio's
connector.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from app import __version__
from app.identity import current_actor_email, current_signed_in_email
from app.mcp.auth import (
    AuthFailed,
    McpAuthMisconfigured,
    Principal,
    RefusingVerifier,
    authenticate,
    build_verifier,
    current_principal,
)
from app.mcp.tools import ALL, offered_names, offered_specs, result_text, to_mcp_tool
from app.sources.registry import KNOWN_SOURCES
from app.tools import run_tool

_log = logging.getLogger("tb_brain.mcp")

ENDPOINTS: tuple[str, ...] = (ALL, *(spec.name for spec in KNOWN_SOURCES))

INSTRUCTIONS = (
    "Read-only tools over TechBldrs' MSP systems. Name clients by their 4-character code (WDON) or name; the tools "
    "resolve ids. Counts, hours and dates in a result are computed by the tools: quote them as given. Never ask these "
    "tools for passwords or secrets; they have none."
)


def _paths(endpoint: str) -> tuple[str, ...]:
    return ("/mcp", "/mcp/all") if endpoint == ALL else (f"/mcp/{endpoint}",)


class McpEntry:
    """ASGI entry for one endpoint: authenticate, bind the caller's identity, hand over to the MCP transport."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await _json_response(send, 404, {"error": "not_found"})
            return
        state = scope["app"].state
        apps = getattr(state, "mcp_apps", None) or {}
        verifier = getattr(state, "mcp_verifier", None)
        if verifier is None or self.endpoint not in apps:
            await _json_response(send, 503, {"error": "mcp_not_ready", "error_description": "tb-brain is starting."})
            return
        try:
            principal = await asyncio.to_thread(authenticate, scope, verifier)
        except AuthFailed as exc:
            _log.warning("mcp auth refused endpoint=%s reason=%s", self.endpoint, exc, extra={"event": "mcp.auth_refused"})
            await _json_response(
                send,
                401,
                {"error": "invalid_token", "error_description": str(exc)},
                extra_headers=[(b"www-authenticate", b'Bearer error="invalid_token"')],
            )
            return
        tokens = (
            current_principal.set(principal),
            current_actor_email.set(principal.email if principal.verified else None),
            current_signed_in_email.set(principal.email),
        )
        try:
            await apps[self.endpoint](scope, receive, send)
        finally:
            current_signed_in_email.reset(tokens[2])
            current_actor_email.reset(tokens[1])
            current_principal.reset(tokens[0])


async def _json_response(send: Any, status: int, body: dict[str, Any], extra_headers: list[tuple[bytes, bytes]] | None = None) -> None:
    payload = json.dumps(body).encode("utf-8")
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode("ascii"))]
    await send({"type": "http.response.start", "status": status, "headers": [*headers, *(extra_headers or [])]})
    await send({"type": "http.response.body", "body": payload})


MCP_ROUTES: list[Route] = [
    Route(path, endpoint=McpEntry(endpoint), include_in_schema=False) for endpoint in ENDPOINTS for path in _paths(endpoint)
]


def build_server(endpoint: str, app: Any) -> Server[Any]:
    """A low-level MCP server for `endpoint`. `app` is the FastAPI app; its state holds the sources and settings."""

    def _runtime() -> tuple[Any, Any]:
        return app.state.sources, app.state.settings

    async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        sources, settings = _runtime()
        specs = offered_specs(sources, endpoint, style=settings.mcp_tool_style)
        return types.ListToolsResult(tools=[to_mcp_tool(spec) for spec in specs])

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        sources, settings = _runtime()
        name = params.name
        arguments = dict(params.arguments or {})
        principal: Principal = current_principal.get() or Principal("mcp-unknown", None, False, "none")
        if name not in offered_names(sources, endpoint):
            text = f"Unknown tool {name!r} on this endpoint. Available: {', '.join(offered_names(sources, endpoint))}."
            return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)
        result = await asyncio.to_thread(
            run_tool,
            sources=sources,
            name=name,
            arguments=arguments,
            actor=principal.label,
            actor_verified=principal.verified,
            settings=settings,
            turn=None,
            knowledge=None,
        )
        _log.info(
            "mcp tool_call endpoint=%s tool=%s ok=%s actor=%s",
            endpoint,
            name,
            result.ok,
            principal.label,
            extra={"event": "mcp.tool_call", "endpoint": endpoint, "tool": name, "ok": result.ok},
        )
        # A refusal with a reply (unknown client, no device named X) is an answer the agent should relay, not an
        # error; only a failure with no reply (bad arguments, source down) is flagged so the model can retry.
        technical_failure = not result.ok and not result.reply
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result_text(result))], is_error=technical_failure
        )

    return Server(
        "tb-brain" if endpoint == ALL else f"tb-brain-{endpoint}",
        version=__version__,
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


@asynccontextmanager
async def mcp_runtime(app: Any) -> AsyncIterator[None]:
    """Create the verifier, the servers and their session managers for this run of the app. Used by the lifespan."""
    settings = app.state.settings
    try:
        verifier = build_verifier(settings)
    except McpAuthMisconfigured as exc:
        _log.warning("mcp auth not configured, every MCP call will be refused: %s", exc)
        verifier = RefusingVerifier()
    app.state.mcp_verifier = verifier
    apps: dict[str, StreamableHTTPASGIApp] = {}
    async with AsyncExitStack() as stack:
        for endpoint in ENDPOINTS:
            manager = StreamableHTTPSessionManager(
                app=build_server(endpoint, app),
                json_response=True,
                stateless=True,
                # nginx fronts the app with the public hostname; Host checks belong there, not here.
                security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
            )
            await stack.enter_async_context(manager.run())
            apps[endpoint] = StreamableHTTPASGIApp(manager)
        app.state.mcp_apps = apps
        _log.info("mcp endpoints ready auth=%s paths=%s", settings.mcp_auth, ",".join(path for e in ENDPOINTS for path in _paths(e)))
        try:
            yield
        finally:
            app.state.mcp_apps = {}


def endpoint_summary(sources: Any, settings: Any) -> dict[str, Any]:
    """What /health reports about the MCP side."""
    return {
        "auth": settings.mcp_auth,
        "tool_style": settings.mcp_tool_style,
        "endpoints": {
            path: list(offered_names(sources, endpoint)) for endpoint in ENDPOINTS for path in _paths(endpoint)[:1]
        },
    }
