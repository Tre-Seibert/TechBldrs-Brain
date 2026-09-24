from __future__ import annotations

import logging
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app import __version__
from app.agent.loop import AgentError, run_tool_loop, stream_final_message
from app.config import Settings, get_settings
from app.flow.factory import build_flow_source
from app.flow.source import FlowNotConfigured, FlowSource
from app.identity import ResolvedActor, current_actor_email, current_signed_in_email, resolve_actor
from app.tools import openai_tools, run_tool
from app.tools.handlers import (
    FindSimilarTicketsArgs,
    LatestTicketArgs,
    ListMailArgs,
    ListTicketsArgs,
    SearchContactArgs,
    SearchTechnicianArgs,
)
from app.tools.registry import (
    FIND_SIMILAR_TICKETS,
    LATEST_TICKET,
    LIST_MAIL,
    LIST_TICKETS,
    SEARCH_CONTACT,
    SEARCH_TECHNICIAN,
    TOOL_NAMES,
    WRITE_TOOL_NAMES,
)

_log = logging.getLogger("tb_brain")


def _configure_logging(settings: Settings) -> None:
    root = logging.getLogger()
    if getattr(root, "_tb_brain_configured", False):
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    file_handler = logging.FileHandler(settings.audit_log_dir / "tb-brain.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(file_handler)
    root._tb_brain_configured = True  # type: ignore[attr-defined]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings)
    app.state.settings = settings
    try:
        app.state.flow = build_flow_source(settings)
    except FlowNotConfigured as exc:
        _log.warning("flow source not ready: %s", exc)
        app.state.flow = build_flow_source(Settings(flow_mode="stub"))
    _log.info(
        "tb-brain %s FLOW_MODE=%s LLM_BASE_URL=%s tools=%s",
        __version__,
        settings.flow_mode_normalized,
        settings.llm_base_url,
        ",".join(TOOL_NAMES),
    )
    yield


app = FastAPI(
    title="tb-brain",
    version=__version__,
    description=(
        "MSP tool-calling agent over Flow, read-only by default. "
        "Tools: search_contact, search_technician, list_tickets, latest_ticket, "
        "find_similar_tickets, list_mail; merge_tickets is chat-only behind a confirm gate. "
        "Not a RAG dump of tickets."
    ),
    lifespan=lifespan,
)


def _settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        settings = get_settings()
        request.app.state.settings = settings
        _configure_logging(settings)
    return settings


def _source(request: Request) -> FlowSource:
    source = getattr(request.app.state, "flow", None)
    if source is None:
        settings = _settings(request)
        try:
            source = build_flow_source(settings)
        except FlowNotConfigured:
            source = build_flow_source(Settings(flow_mode="stub"))
        request.app.state.flow = source
    return source


def _resolve_actor(
    request: Request,
    *,
    x_brain_actor: str | None,
    x_openwebui_user_jwt: str | None,
    x_openwebui_user_email: str | None,
) -> ResolvedActor:
    return resolve_actor(
        _settings(request),
        user_jwt=x_openwebui_user_jwt,
        user_email_header=x_openwebui_user_email,
        fallback_actor=x_brain_actor,
    )


@contextmanager
def _actor_scope(resolved: ResolvedActor):
    write_token = current_actor_email.set(resolved.email if resolved.verified else None)
    me_token = current_signed_in_email.set(resolved.email)
    try:
        yield
    finally:
        current_signed_in_email.reset(me_token)
        current_actor_email.reset(write_token)


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[dict[str, Any]]
    tools: list[Any] | None = None
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None

    model_config = {"extra": "allow"}


@app.get("/health")
def health(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    source = _source(request)
    return {
        "ok": True,
        "version": __version__,
        "read_only": True,
        "flow_mode": settings.flow_mode_normalized,
        "flow_source": getattr(source, "source_name", "unknown"),
        "llm_base_url": settings.llm_base_url,
        "tools": list(TOOL_NAMES),
        "write_tools": list(WRITE_TOOL_NAMES),
    }


@app.get("/v1/models")
def list_models() -> dict[str, Any]:
    # Always advertise tb-brain. run_tool_loop maps tb-brain / auto to settings.llm_model.
    return {
        "object": "list",
        "data": [
            {
                "id": "tb-brain",
                "object": "model",
                "owned_by": "tb-brain",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    body: ChatCompletionRequest,
    x_brain_actor: str | None = Header(default=None),
    x_openwebui_user_jwt: str | None = Header(default=None, alias="X-OpenWebUI-User-Jwt"),
    x_openwebui_user_email: str | None = Header(default=None, alias="X-OpenWebUI-User-Email"),
):
    settings = _settings(request)
    resolved = _resolve_actor(
        request,
        x_brain_actor=x_brain_actor,
        x_openwebui_user_jwt=x_openwebui_user_jwt,
        x_openwebui_user_email=x_openwebui_user_email,
    )
    extra = body.model_dump(exclude={"messages", "tools", "stream", "model"}, exclude_none=True)
    with _actor_scope(resolved):
        try:
            payload = await run_tool_loop(
                settings=settings,
                source=_source(request),
                messages=body.messages,
                model=body.model,
                actor=resolved.label,
                actor_verified=resolved.verified,
                client_tools=body.tools,
                extra_body=extra,
            )
        except AgentError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    if body.stream:
        return StreamingResponse(stream_final_message(payload), media_type="text/event-stream")
    return JSONResponse(payload)


@app.get("/tools", operation_id="list_tools")
def list_tools() -> dict[str, Any]:
    return {"ok": True, "read_only": True, "tools": openai_tools()}


def _run_tool_endpoint(
    request: Request,
    name: str,
    arguments: dict[str, Any],
    *,
    x_brain_actor: str | None,
    x_openwebui_user_jwt: str | None,
    x_openwebui_user_email: str | None,
) -> dict[str, Any]:
    resolved = _resolve_actor(
        request,
        x_brain_actor=x_brain_actor,
        x_openwebui_user_jwt=x_openwebui_user_jwt,
        x_openwebui_user_email=x_openwebui_user_email,
    )
    with _actor_scope(resolved):
        result = run_tool(
            source=_source(request),
            name=name,
            arguments=arguments,
            actor=resolved.label,
            actor_verified=resolved.verified,
            settings=_settings(request),
        )
    return result.model_dump()


# Read tools only. merge_tickets has no direct route: its confirm gate needs
# the chat history, so it runs only inside /v1/chat/completions.


@app.post("/tools/search_contact", operation_id=SEARCH_CONTACT)
def tool_search_contact(
    request: Request,
    args: SearchContactArgs,
    x_brain_actor: str | None = Header(default=None),
    x_openwebui_user_jwt: str | None = Header(default=None, alias="X-OpenWebUI-User-Jwt"),
    x_openwebui_user_email: str | None = Header(default=None, alias="X-OpenWebUI-User-Email"),
) -> dict[str, Any]:
    return _run_tool_endpoint(
        request,
        SEARCH_CONTACT,
        args.model_dump(),
        x_brain_actor=x_brain_actor,
        x_openwebui_user_jwt=x_openwebui_user_jwt,
        x_openwebui_user_email=x_openwebui_user_email,
    )


@app.post("/tools/search_technician", operation_id=SEARCH_TECHNICIAN)
def tool_search_technician(
    request: Request,
    args: SearchTechnicianArgs,
    x_brain_actor: str | None = Header(default=None),
    x_openwebui_user_jwt: str | None = Header(default=None, alias="X-OpenWebUI-User-Jwt"),
    x_openwebui_user_email: str | None = Header(default=None, alias="X-OpenWebUI-User-Email"),
) -> dict[str, Any]:
    return _run_tool_endpoint(
        request,
        SEARCH_TECHNICIAN,
        args.model_dump(),
        x_brain_actor=x_brain_actor,
        x_openwebui_user_jwt=x_openwebui_user_jwt,
        x_openwebui_user_email=x_openwebui_user_email,
    )


@app.post("/tools/list_tickets", operation_id=LIST_TICKETS)
def tool_list_tickets(
    request: Request,
    args: ListTicketsArgs,
    x_brain_actor: str | None = Header(default=None),
    x_openwebui_user_jwt: str | None = Header(default=None, alias="X-OpenWebUI-User-Jwt"),
    x_openwebui_user_email: str | None = Header(default=None, alias="X-OpenWebUI-User-Email"),
) -> dict[str, Any]:
    return _run_tool_endpoint(
        request,
        LIST_TICKETS,
        args.model_dump(),
        x_brain_actor=x_brain_actor,
        x_openwebui_user_jwt=x_openwebui_user_jwt,
        x_openwebui_user_email=x_openwebui_user_email,
    )


@app.post("/tools/latest_ticket", operation_id=LATEST_TICKET)
def tool_latest_ticket(
    request: Request,
    args: LatestTicketArgs,
    x_brain_actor: str | None = Header(default=None),
    x_openwebui_user_jwt: str | None = Header(default=None, alias="X-OpenWebUI-User-Jwt"),
    x_openwebui_user_email: str | None = Header(default=None, alias="X-OpenWebUI-User-Email"),
) -> dict[str, Any]:
    return _run_tool_endpoint(
        request,
        LATEST_TICKET,
        args.model_dump(),
        x_brain_actor=x_brain_actor,
        x_openwebui_user_jwt=x_openwebui_user_jwt,
        x_openwebui_user_email=x_openwebui_user_email,
    )


@app.post("/tools/find_similar_tickets", operation_id=FIND_SIMILAR_TICKETS)
def tool_find_similar_tickets(
    request: Request,
    args: FindSimilarTicketsArgs,
    x_brain_actor: str | None = Header(default=None),
    x_openwebui_user_jwt: str | None = Header(default=None, alias="X-OpenWebUI-User-Jwt"),
    x_openwebui_user_email: str | None = Header(default=None, alias="X-OpenWebUI-User-Email"),
) -> dict[str, Any]:
    return _run_tool_endpoint(
        request,
        FIND_SIMILAR_TICKETS,
        args.model_dump(),
        x_brain_actor=x_brain_actor,
        x_openwebui_user_jwt=x_openwebui_user_jwt,
        x_openwebui_user_email=x_openwebui_user_email,
    )


@app.post("/tools/list_mail", operation_id=LIST_MAIL)
def tool_list_mail(
    request: Request,
    args: ListMailArgs,
    x_brain_actor: str | None = Header(default=None),
    x_openwebui_user_jwt: str | None = Header(default=None, alias="X-OpenWebUI-User-Jwt"),
    x_openwebui_user_email: str | None = Header(default=None, alias="X-OpenWebUI-User-Email"),
) -> dict[str, Any]:
    return _run_tool_endpoint(
        request,
        LIST_MAIL,
        args.model_dump(),
        x_brain_actor=x_brain_actor,
        x_openwebui_user_jwt=x_openwebui_user_jwt,
        x_openwebui_user_email=x_openwebui_user_email,
    )


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.brain_host,
        port=settings.brain_port,
        reload=False,
    )


if __name__ == "__main__":
    run()
