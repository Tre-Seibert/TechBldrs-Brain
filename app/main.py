from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app import __version__
from app.agent.loop import AgentError, run_tool_loop, stream_final_message
from app.config import Settings, get_settings
from app.flow.factory import build_flow_source
from app.flow.source import FlowNotConfigured, FlowSource
from app.tools import openai_tools, run_tool
from app.tools.handlers import ListMailArgs, LatestTicketArgs, SearchContactArgs
from app.tools.registry import LATEST_TICKET, LIST_MAIL, SEARCH_CONTACT, TOOL_NAMES

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
        "Read-only MSP tool-calling agent over Flow. "
        "Stable tools: search_contact, latest_ticket, list_mail. "
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


def _actor(request: Request, x_brain_actor: str | None) -> str:
    return (x_brain_actor or _settings(request).brain_actor or "lab-local").strip()


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
    }


@app.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    return {
        "object": "list",
        "data": [
            {
                "id": settings.llm_model.strip() or "tb-brain",
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
):
    settings = _settings(request)
    extra = body.model_dump(exclude={"messages", "tools", "stream", "model"}, exclude_none=True)
    try:
        payload = await run_tool_loop(
            settings=settings,
            source=_source(request),
            messages=body.messages,
            model=body.model,
            actor=_actor(request, x_brain_actor),
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


@app.post("/tools/search_contact", operation_id=SEARCH_CONTACT)
def tool_search_contact(
    request: Request,
    args: SearchContactArgs,
    x_brain_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    result = run_tool(
        source=_source(request),
        name=SEARCH_CONTACT,
        arguments=args.model_dump(),
        actor=_actor(request, x_brain_actor),
        settings=_settings(request),
    )
    return result.model_dump()


@app.post("/tools/latest_ticket", operation_id=LATEST_TICKET)
def tool_latest_ticket(
    request: Request,
    args: LatestTicketArgs,
    x_brain_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    result = run_tool(
        source=_source(request),
        name=LATEST_TICKET,
        arguments=args.model_dump(),
        actor=_actor(request, x_brain_actor),
        settings=_settings(request),
    )
    return result.model_dump()


@app.post("/tools/list_mail", operation_id=LIST_MAIL)
def tool_list_mail(
    request: Request,
    args: ListMailArgs,
    x_brain_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    result = run_tool(
        source=_source(request),
        name=LIST_MAIL,
        arguments=args.model_dump(),
        actor=_actor(request, x_brain_actor),
        settings=_settings(request),
    )
    return result.model_dump()


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
