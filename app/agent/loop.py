"""OpenAI-compatible tool loop against LLM_BASE_URL (local Ollama or any upgrade)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.agent.system import SYSTEM_PROMPT, signed_in_prompt_line
from app.config import Settings
from app.flow.source import FlowSource
from app.identity import current_signed_in_email
from app.tools import ChatTurn, openai_tools, run_tool
from app.tools.registry import (
    FIND_SIMILAR_TICKETS,
    LATEST_TICKET,
    LIST_MAIL,
    LIST_TICKETS,
    TOOL_NAMES,
)

_RELAY_TOOLS = {LIST_TICKETS, FIND_SIMILAR_TICKETS, LATEST_TICKET, LIST_MAIL}

_log = logging.getLogger("tb_brain.agent")


class AgentError(RuntimeError):
    pass


def _ignored_client_tool_names(client_tools: list[Any] | None) -> list[str]:
    """Open WebUI injects builtin tools (update_task, notes, calendar). Never forward them."""
    names: list[str] = []
    for tool in client_tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        fn = tool.get("function")
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if isinstance(name, str) and name and name not in TOOL_NAMES:
            names.append(name)
    return names


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def chat_turn_from_messages(messages: list[dict[str, Any]]) -> ChatTurn:
    """The latest user message and the assistant reply just before it, from the incoming history.

    Computed from what the client sent, not from this request's tool-loop
    messages, so the model cannot satisfy the merge gate by itself.
    """
    user_index = next(
        (index for index in range(len(messages) - 1, -1, -1) if messages[index].get("role") == "user"),
        None,
    )
    if user_index is None:
        return ChatTurn()
    previous = next(
        (messages[index] for index in range(user_index - 1, -1, -1) if messages[index].get("role") == "assistant"),
        None,
    )
    return ChatTurn(
        user_text=_message_text(messages[user_index]),
        previous_assistant_text=_message_text(previous) if previous is not None else "",
    )


def _signed_in_line(source: FlowSource) -> str:
    email = (current_signed_in_email.get() or "").strip().lower() or None
    name = None
    code = None
    if email:
        matches = source.search_technician(query=email, limit=5)
        exact = [row for row in matches if (row.email or "").strip().lower() == email]
        pick = exact[0] if exact else (matches[0] if len(matches) == 1 else None)
        if pick:
            name = pick.display_name
            code = pick.assignee_code
    return signed_in_prompt_line(name, code, email)


def _ensure_system(messages: list[dict[str, Any]], *, source: FlowSource) -> list[dict[str, Any]]:
    prompt = SYSTEM_PROMPT.rstrip() + "\n- " + _signed_in_line(source)
    if messages and messages[0].get("role") == "system":
        first = dict(messages[0])
        content = str(first.get("content") or "")
        if "tb-brain" not in content:
            first["content"] = prompt + "\n\n" + content
        else:
            first["content"] = content.rstrip() + "\n- " + _signed_in_line(source)
        return [first, *messages[1:]]
    return [{"role": "system", "content": prompt}, *messages]


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
        return parsed if isinstance(parsed, dict) else {"_raw": parsed}
    return {"_raw": raw}


async def _resolve_model(client: httpx.AsyncClient, settings: Settings, requested: str | None) -> str:
    if (requested or "").strip() and requested not in ("tb-brain", "auto"):
        return requested.strip()
    if settings.llm_model.strip():
        return settings.llm_model.strip()
    response = await client.get("/models")
    response.raise_for_status()
    payload = response.json()
    models = payload.get("data") if isinstance(payload, dict) else None
    if not models:
        raise AgentError("LLM returned no models. Set LLM_MODEL or pull an Ollama instruct model.")
    first = models[0]
    name = first.get("id") if isinstance(first, dict) else None
    if not name:
        raise AgentError("LLM /models response had no id. Set LLM_MODEL.")
    return str(name)


def _tool_message_content(result: Any) -> str:
    """Keep the model from seeing a fat JSON schema it will 'analyze' in Chinese."""
    if result.reply:
        return json.dumps(
            {
                "ok": result.ok,
                "reply": result.reply,
                "row_ids": result.row_ids,
                "error": result.error,
            }
        )
    return result.model_dump_json()


def _apply_english_reply(payload: dict[str, Any], replies: list[str]) -> dict[str, Any]:
    """Qwen 7B often answers list tools in Chinese. Show our English list instead."""
    if not replies:
        return payload
    text = replies[-1]
    choices = payload.get("choices")
    if not choices:
        payload["choices"] = [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]
        return payload
    message = dict(choices[0].get("message") or {})
    message["content"] = text
    choices[0]["message"] = message
    return payload


async def run_tool_loop(
    *,
    settings: Settings,
    source: FlowSource,
    messages: list[dict[str, Any]],
    model: str | None,
    actor: str,
    actor_verified: bool = False,
    client_tools: list[Any] | None = None,
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ignored = _ignored_client_tool_names(client_tools)
    if ignored:
        _log.info("ignoring client tools: %s", ",".join(ignored))
    tools = openai_tools()
    turn = chat_turn_from_messages(messages)
    chat = _ensure_system(list(messages), source=source)
    relayed: list[str] = []
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    timeout = httpx.Timeout(settings.llm_timeout_seconds)
    async with httpx.AsyncClient(base_url=settings.llm_base_url, headers=headers, timeout=timeout) as client:
        resolved_model = await _resolve_model(client, settings, model)
        last_payload: dict[str, Any] | None = None
        for iteration in range(settings.agent_max_tool_iters):
            body: dict[str, Any] = {
                "model": resolved_model,
                "messages": chat,
                "tools": tools,
                "stream": False,
            }
            if extra_body:
                for key, value in extra_body.items():
                    if key in ("messages", "tools", "stream"):
                        continue
                    if key == "model" and value in (None, "", "tb-brain", "auto"):
                        continue
                    body[key] = value
            try:
                response = await client.post("/chat/completions", json=body)
            except httpx.HTTPError as exc:
                raise AgentError(f"LLM unreachable at {settings.llm_base_url}: {exc}") from exc
            if response.status_code >= 400:
                raise AgentError(f"LLM error {response.status_code}: {response.text[:800]}")
            payload = response.json()
            last_payload = payload
            choice = (payload.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                payload["model"] = resolved_model
                return _apply_english_reply(payload, relayed)
            chat.append(message)
            _log.info(
                "agent tool_calls iteration=%s count=%s",
                iteration,
                len(tool_calls),
                extra={"event": "agent.tool_calls", "iteration": iteration},
            )
            for call in tool_calls:
                fn = call.get("function") or {}
                name = str(fn.get("name") or "")
                arguments = _parse_arguments(fn.get("arguments"))
                result = run_tool(
                    source=source,
                    name=name,
                    arguments=arguments,
                    actor=actor,
                    actor_verified=actor_verified,
                    settings=settings,
                    turn=turn,
                )
                if result.reply and name in _RELAY_TOOLS:
                    relayed.append(result.reply)
                chat.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or name,
                        "name": name,
                        "content": _tool_message_content(result),
                    }
                )
        raise AgentError(
            f"Tool loop exceeded AGENT_MAX_TOOL_ITERS={settings.agent_max_tool_iters}. "
            f"Last model payload keys={list((last_payload or {}).keys())}"
        )


async def stream_final_message(payload: dict[str, Any]) -> AsyncIterator[bytes]:
    """Run the full tool loop first, then SSE-stream the final assistant text."""
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    model = payload.get("model") or "tb-brain"
    chunk = {
        "id": payload.get("id") or "tb-brain",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": None,
            }
        ],
    }
    yield f"data: {json.dumps(chunk)}\n\n".encode()
    done = {
        "id": payload.get("id") or "tb-brain",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(done)}\n\n".encode()
    yield b"data: [DONE]\n\n"
