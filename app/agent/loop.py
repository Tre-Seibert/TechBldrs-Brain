"""OpenAI-compatible tool loop against LLM_BASE_URL (local Ollama or any upgrade)."""

from __future__ import annotations

from datetime import datetime

import json
import logging
import re
import unicodedata
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.agent.system import SYSTEM_PROMPT, signed_in_prompt_line
from app.config import ROUTER_NAMES, Settings
from app.flow.source import FlowSource
from app.identity import current_signed_in_email
from app.knowledge.source import KnowledgeSource
from app.tools import ChatTurn, ToolResult, openai_tools, run_tool
from app.tools.handlers import (
    answer_longest_time_worked,
    answer_merge_suggestion,
    answer_person_mail_question,
    answer_person_ticket_question,
    answer_tickets_about,
)
from app.tools.registry import (
    FIND_SIMILAR_TICKETS,
    GET_CLIENT_DETAIL,
    GET_MAIL_DETAIL,
    GET_TICKET_DETAIL,
    LATEST_TICKET,
    LIST_MACHINES,
    LIST_MAIL,
    LIST_TICKETS,
    LIST_TIME_ENTRIES,
    SEARCH_CONTACT,
    SEARCH_KNOWLEDGE,
    TICKET_STATS,
    TOOL_NAMES,
)

_RELAY_TOOLS = {
    LIST_TICKETS,
    FIND_SIMILAR_TICKETS,
    LATEST_TICKET,
    LIST_MAIL,
    SEARCH_CONTACT,
    SEARCH_KNOWLEDGE,
    LIST_TIME_ENTRIES,
    TICKET_STATS,
    LIST_MACHINES,
    GET_TICKET_DETAIL,
    GET_MAIL_DETAIL,
    GET_CLIENT_DETAIL,
}
_NO_TOOL_ENGLISH = (
    "I can only answer from Flow tools, and I did not get a usable result. "
    "Ask again with a person, technician, or client code."
)
_THINK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_TICKET_LABEL_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,8}-[A-Z0-9]{3,6}\b")
_NUDGE_TO_USE_A_TOOL = (
    "You answered without calling a tool, so that answer is unverified. For any question about tickets, "
    "time, mail, contacts, or clients you must call the right tool first (for example list_tickets for "
    "lists, ticket_stats for hours or counts) and answer only from its result. Call the tool now."
)
_NO_SEARCH_RUN = (
    "I didn't run a Flow search for that, so I won't guess. Try rephrasing, for example "
    "'my urgent tickets' or 'open tickets for BUCK'."
)
# Questions that must be answered from a tool. A first-word write verb ("Close ticket ...",
# "Email Debe ...", "Merge ...") is a request the model should decline or gate, not search for.
_DATA_WORD_RE = re.compile(
    r"\b(?:tickets?|urgent|overdue|assigned|hours?|time|logged|worked|e-?mails?|mail|reach(?:ed)? out|"
    r"contacts?|clients?|machines?|respond|queue|open|review|archived|billable|cause|problem|unassigned|work(?:ed|ing)?|spent)\b"
    r"|\b[A-Z][A-Z0-9]{1,8}-[A-Z0-9]{3,6}\b",
    re.IGNORECASE,
)
_WRITE_INTENT_RE = re.compile(
    r"^\s*(?:please\s+|can you\s+)?(?:close|archive|delete|create|log|send|e-?mail|change|set|update|reassign|add|merge)\b",
    re.IGNORECASE,
)


def _expects_tool(turn: ChatTurn) -> bool:
    """True when this turn is a Flow data question, so an answer with no tool call is unverified."""
    text = turn.user_text or ""
    if _WRITE_INTENT_RE.search(text):
        return False
    return bool(_DATA_WORD_RE.search(text) or _DATA_WORD_RE.search(turn.previous_assistant_text or ""))


def _compact_history(messages: list[dict[str, Any]], *, limit: int = 240) -> list[dict[str, Any]]:
    """Shrink earlier assistant answers before they go to the model.

    Long tool-formatted lists in the history cost context (the system prompt and tools already use
    most of it) and teach the model to imitate that format instead of calling a tool.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, str) and len(content) > limit:
            first = next((line.strip() for line in content.splitlines() if line.strip()), "")[:160]
            message = {
                **message,
                "content": f"{first} [list shown to the user earlier; call a tool again if you need data]",
            }
        out.append(message)
    return out


_NO_INVENTED_TICKETS = (
    "I can only list tickets a Flow search returned. I did not run that search, so I won't guess ticket numbers."
)


def reply_without_invented_tickets(content: str, relayed: list[str]) -> str | None:
    """Ticket labels with no tool reply behind them are the model's invention."""
    if relayed or not _TICKET_LABEL_RE.search(content or ""):
        return None
    return _NO_INVENTED_TICKETS
_DROP_MESSAGE_KEYS = ("reasoning", "reasoning_content", "thinking", "thought")

_log = logging.getLogger("tb_brain.agent")


class AgentError(RuntimeError):
    pass


# Order matters: the first router that answers wins. Names must match config.ROUTER_NAMES.
_ROUTERS = (
    ("merge_suggestion", answer_merge_suggestion),
    ("person_mail", answer_person_mail_question),
    ("person_ticket", answer_person_ticket_question),
    ("tickets_about", answer_tickets_about),
    ("longest_time", answer_longest_time_worked),
)
assert tuple(name for name, _ in _ROUTERS) == ROUTER_NAMES


def _direct_answer_named(
    source: FlowSource, turn: ChatTurn, disabled: frozenset[str] = frozenset()
) -> tuple[str, ToolResult] | None:
    for name, router in _ROUTERS:
        if name in disabled:
            continue
        result = router(source, turn)
        if result is not None:
            _log.info("router answered router=%s", name, extra={"event": "agent.router", "router": name})
            return name, result
    return None


def direct_answer(source: FlowSource, turn: ChatTurn, disabled: frozenset[str] = frozenset()) -> ToolResult | None:
    """Pattern-matched answer that skips the LLM, or None to let the model handle the question."""
    hit = _direct_answer_named(source, turn, disabled)
    return hit[1] if hit else None


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
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content") or ""
                if text:
                    parts.append(str(text))
        return " ".join(parts)
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


def _today_line() -> str:
    now = datetime.now()
    return f"Today is {now.strftime('%A %Y-%m-%d')} (server local time). Use it for 'today', 'yesterday', 'this month', 'last week'."


def _ensure_system(
    messages: list[dict[str, Any]], *, source: FlowSource, no_think: bool = False
) -> list[dict[str, Any]]:
    out = _ensure_system_prompt(messages, source=source)
    if no_think:
        first = dict(out[0])
        first["content"] = str(first.get("content") or "").rstrip() + "\n/no_think"
        out = [first, *out[1:]]
    return out


def _ensure_system_prompt(messages: list[dict[str, Any]], *, source: FlowSource) -> list[dict[str, Any]]:
    prompt = SYSTEM_PROMPT.rstrip() + "\n- " + _signed_in_line(source) + "\n- " + _today_line()
    if messages and messages[0].get("role") == "system":
        first = dict(messages[0])
        content = str(first.get("content") or "")
        if "tb-brain" not in content:
            first["content"] = prompt + "\n\n" + content
        else:
            first["content"] = content.rstrip() + "\n- " + _signed_in_line(source) + "\n- " + _today_line()
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
    if result.digest:
        return json.dumps({"ok": result.ok, "digest": result.digest, "row_ids": result.row_ids})
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


def _has_non_english_script(text: str) -> bool:
    """True if any letter is not a Latin letter (Thai, Chinese, Cyrillic, Arabic, ...)."""
    for char in text or "":
        if not char.isalpha():
            continue
        name = unicodedata.name(char, "")
        if not name.startswith("LATIN"):
            return True
    return False


def _english_only(text: str, fallback: str | None = None) -> str:
    cleaned = _THINK_RE.sub("", text or "").strip()
    if cleaned and not _has_non_english_script(cleaned):
        return cleaned
    return (fallback or "").strip() or _NO_TOOL_ENGLISH


def _set_assistant_content(payload: dict[str, Any], text: str) -> dict[str, Any]:
    choices = payload.get("choices")
    if not choices:
        payload["choices"] = [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]
        return payload
    message = dict(choices[0].get("message") or {})
    for key in _DROP_MESSAGE_KEYS:
        message.pop(key, None)
    message["role"] = "assistant"
    message["content"] = text
    choices[0]["message"] = message
    return payload


def _assistant_payload(text: str, model: str) -> dict[str, Any]:
    return _apply_english_reply(
        {
            "id": "tb-brain",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        },
        [text] if text else [],
    )


def enforce_english_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Public last-mile gate for /v1/chat/completions."""
    return _apply_english_reply(payload, [])


def _apply_english_reply(payload: dict[str, Any], replies: list[str]) -> dict[str, Any]:
    """Last gate: the user only ever sees English. Tool lists win; model text is discarded if not English."""
    if replies:
        text = _THINK_RE.sub("", replies[-1]).strip() or _NO_TOOL_ENGLISH
    else:
        content = ""
        choices = payload.get("choices")
        if choices:
            content = str((choices[0].get("message") or {}).get("content") or "")
        text = _english_only(content)
    return _set_assistant_content(payload, text)


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
    knowledge: KnowledgeSource | None = None,
) -> dict[str, Any]:
    ignored = _ignored_client_tool_names(client_tools)
    if ignored:
        _log.info("ignoring client tools: %s", ",".join(ignored))
    tools = openai_tools()
    turn = chat_turn_from_messages(messages)
    hit = _direct_answer_named(source, turn, settings.disabled_router_set)
    if hit is not None:
        router_name, direct = hit
        payload = _assistant_payload(
            direct.reply or direct.error or _NO_TOOL_ENGLISH,
            settings.llm_model.strip() or "tb-brain",
        )
        payload["x_tb_brain"] = {"router": router_name, "tool_calls": []}
        return payload
    trace: list[dict[str, Any]] = []
    nudged = False
    chat = _ensure_system(_compact_history(list(messages)), source=source, no_think=settings.llm_no_think)
    prompt_tokens = 0
    relayed: list[str] = []
    digests: list[str] = []  # what summary-mode tools gave the model to write prose from
    footers: list[str] = []  # exact numbers appended after the model's prose
    field_headers: list[str] = []  # exact fields printed before the model's prose
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
            body.update(settings.llm_extra_body_dict)
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
            used = int(((payload.get("usage") or {}).get("prompt_tokens")) or 0)
            prompt_tokens = max(prompt_tokens, used)
            if used:
                _log.info("llm prompt_tokens=%s", used, extra={"event": "agent.usage", "prompt_tokens": used})
            choice = (payload.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                payload["model"] = resolved_model
                if not relayed:
                    content = str(message.get("content") or "")
                    if digests:
                        # Prose written from a digest may name tickets, but only ones the digest had.
                        grounded = " ".join(digests)
                        invented = [label for label in _TICKET_LABEL_RE.findall(content) if label not in grounded]
                        blocked = _NO_INVENTED_TICKETS if invented else None
                    else:
                        blocked = reply_without_invented_tickets(content, relayed)
                    skipped = not trace and _expects_tool(turn)
                    if (blocked or skipped) and not nudged:
                        # The model answered from memory (made-up labels, or a data question with no
                        # tool call at all). Give it one chance to call a tool.
                        nudged = True
                        chat.append({"role": "assistant", "content": str(message.get("content") or "")})
                        chat.append({"role": "user", "content": _NUDGE_TO_USE_A_TOOL})
                        _log.info("nudging model to use a tool", extra={"event": "agent.nudge"})
                        continue
                    if blocked:
                        payload = _set_assistant_content(payload, blocked)
                    elif skipped:
                        # The model twice failed to call a tool. A pattern router is a better safety net
                        # than a refusal, even for routers the operator turned off.
                        fallback = _direct_answer_named(source, turn)
                        if fallback is not None:
                            name, direct = fallback
                            payload = _assistant_payload(
                                direct.reply or direct.error or _NO_TOOL_ENGLISH,
                                settings.llm_model.strip() or "tb-brain",
                            )
                            payload["x_tb_brain"] = {"router": f"fallback:{name}", "tool_calls": trace}
                            return payload
                        payload = _set_assistant_content(payload, _NO_SEARCH_RUN)
                payload = _apply_english_reply(payload, relayed)
                if (footers or field_headers) and not relayed:
                    prose = str(payload["choices"][0]["message"].get("content") or "").strip()
                    if prose in (_NO_INVENTED_TICKETS, _NO_TOOL_ENGLISH):
                        prose = ""
                    parts = [field_headers[-1] if field_headers else "", prose, footers[-1] if footers else ""]
                    payload = _set_assistant_content(payload, "\n\n".join(part for part in parts if part))
                payload["x_tb_brain"] = {"router": None, "tool_calls": trace, "prompt_tokens": prompt_tokens}
                return payload
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
                    knowledge=knowledge,
                )
                trace.append({"name": name, "arguments": arguments, "ok": result.ok, "error": result.error})
                if result.reply and name in _RELAY_TOOLS:
                    relayed.append(result.reply)
                if result.digest:
                    digests.append(result.digest)
                if result.footer:
                    footers.append(result.footer)
                if result.header:
                    field_headers.append(result.header)
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
    raw = ""
    if payload.get("choices"):
        raw = str((payload["choices"][0].get("message") or {}).get("content") or "")
    payload = _set_assistant_content(payload, _THINK_RE.sub("", raw).strip() or _NO_TOOL_ENGLISH)
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
