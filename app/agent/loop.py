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

from app.agent.routing import USE_SOURCE, Routing, route
from app.agent.system import SYSTEM_PROMPT, lean_system_prompt, session_context_message, signed_in_prompt_line
from app.agent.wording import expects_tool, wording_for
from app.audit import log_tool_call
from app.config import ROUTER_NAMES, Settings
from app.identity import current_signed_in_email
from app.knowledge.source import KnowledgeSource
from app.sources.base import Sources, as_sources
from app.sources.flow.handlers.people import signed_in_matches
from app.sources.flow.protocol import FlowSource
from app.sources.flow.spec import FLOW
from app.sources.flow.tools import RELAY_TOOL_NAMES
from app.tools import ChatTurn, ToolResult, openai_tools, run_tool
from app.tools.handlers import (
    answer_longest_time_worked,
    answer_merge_suggestion,
    answer_person_mail_question,
    answer_person_ticket_question,
    answer_tickets_about,
)
from app.tools.registry import SEARCH_KNOWLEDGE, TOOL_NAMES, WRITE_TOOL_NAMES

# Tools whose formatted reply is shown as-is. Each source lists its own; these are Flow's plus the shared ones.
_RELAY_TOOLS = {*RELAY_TOOL_NAMES, SEARCH_KNOWLEDGE}
# The Flow-only messages, for callers with no sources at hand. run_tool_loop words its messages from the enabled
# sources instead (app/agent/wording.py); with only Flow enabled they are these.
_FLOW_ONLY = Sources([(FLOW, None)])
_FLOW_WORDING = wording_for(_FLOW_ONLY)
_NO_TOOL_ENGLISH = _FLOW_WORDING.no_tool
_THINK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_TICKET_LABEL_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,8}-[A-Z0-9]{3,6}\b")
_NUDGE_TO_USE_A_TOOL = _FLOW_WORDING.nudge_to_use_a_tool
_NUDGE_TO_WRITE = (
    "Write the answer now, in English, in plain sentences, from the digest above. Name tickets only "
    "if they appear in the digest. Do not call tools."
)
_STUCK = _FLOW_WORDING.stuck
_NO_SEARCH_RUN = _FLOW_WORDING.no_search_run


def _expects_tool(turn: ChatTurn, sources: Sources | None = None) -> bool:
    """True when this turn is a data question for an enabled source (Flow when none are given), so an answer
    with no tool call is unverified."""
    return expects_tool(turn, sources if sources is not None else _FLOW_ONLY)


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


def _tool_cheatsheet(tools: list[dict[str, Any]], write_names: tuple[str, ...] = WRITE_TOOL_NAMES) -> str:
    lines = []
    for tool in tools:
        fn = tool.get("function") or {}
        if fn.get("name") in write_names:
            continue
        params = ", ".join((fn.get("parameters") or {}).get("properties", {}).keys())
        lines.append(f"- {fn.get('name')}({params}): {str(fn.get('description') or '')[:150]}")
    return "\n".join(lines)


async def _summarize_chunk(
    client: httpx.AsyncClient, model: str, prompt: str, extra: dict[str, Any]
) -> str:
    """Answer one small prompt on its own (no tools, no chat history). Empty if the model fails."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You analyze help desk tickets. Reply in English only, briefly, from the text given."},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        **extra,
    }
    for _attempt in range(2):
        try:
            response = await client.post("/chat/completions", json=body)
            if response.status_code >= 400:
                return ""
            text = _THINK_RE.sub("", str(response.json()["choices"][0]["message"].get("content") or "")).strip()
        except Exception as exc:  # noqa: BLE001 - a missing monthly finding must not sink the whole answer
            _log.info("chunk summary failed: %s", exc)
            return ""
        if text and not _has_non_english_script(text):
            return text
    return ""


async def _force_tool_call(
    client: httpx.AsyncClient,
    model: str,
    chat: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    write_names: tuple[str, ...] = WRITE_TOOL_NAMES,
) -> dict[str, Any] | None:
    """Last resort when the model twice answers a data question without calling a tool.

    Asks for the tool call as constrained JSON (the model cannot answer in prose), so a call is
    guaranteed. The write tool is never offered. Returns a synthetic assistant tool_calls message,
    or None if the server does not support structured output.
    """
    names = [t["function"]["name"] for t in tools if t["function"]["name"] not in write_names]
    ask = {
        "role": "user",
        "content": (
            "Reply with JSON only: the single tool call that best answers the user's last question. "
            "Tools:\n" + _tool_cheatsheet(tools, write_names)
        ),
    }
    body = {
        "model": model,
        "messages": [*chat, ask],
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "tool_call",
                "schema": {
                    "type": "object",
                    "properties": {"tool": {"type": "string", "enum": names}, "arguments": {"type": "object"}},
                    "required": ["tool", "arguments"],
                },
            },
        },
    }
    try:
        response = await client.post("/chat/completions", json=body)
        if response.status_code >= 400:
            return None
        data = json.loads(str(response.json()["choices"][0]["message"].get("content") or ""))
        tool, arguments = data.get("tool"), data.get("arguments")
    except Exception as exc:  # noqa: BLE001 - the caller falls back to the safe message
        _log.info("forced tool call failed: %s", exc)
        return None
    if tool not in names or not isinstance(arguments, dict):
        return None
    _log.info("forced tool call: %s", tool, extra={"event": "agent.forced_tool_call"})
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "forced", "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)}}
        ],
    }


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
    source: Sources | FlowSource, turn: ChatTurn, disabled: frozenset[str] = frozenset()
) -> tuple[str, ToolResult] | None:
    flow = as_sources(source).flow  # the routers are Flow question shapes
    for name, router in _ROUTERS:
        if name in disabled:
            continue
        result = router(flow, turn)
        if result is not None:
            _log.info("router answered router=%s", name, extra={"event": "agent.router", "router": name})
            return name, result
    return None


def direct_answer(
    source: Sources | FlowSource, turn: ChatTurn, disabled: frozenset[str] = frozenset()
) -> ToolResult | None:
    """Pattern-matched answer that skips the LLM, or None to let the model handle the question."""
    hit = _direct_answer_named(source, turn, disabled)
    return hit[1] if hit else None


def _ignored_client_tool_names(client_tools: list[Any] | None, known: tuple[str, ...] = TOOL_NAMES) -> list[str]:
    """Open WebUI injects builtin tools (update_task, notes, calendar). Never forward them."""
    names: list[str] = []
    for tool in client_tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        fn = tool.get("function")
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if isinstance(name, str) and name and name not in known:
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


def _route(sources: Sources, messages: list[dict[str, Any]], settings: Settings) -> Routing:
    """Source routing from the incoming history: the latest user message, the earlier ones, and any tool
    calls the client sent back."""
    user_texts = [_message_text(message) for message in messages if message.get("role") == "user"]
    called: set[str] = set()
    for message in messages:
        if message.get("role") == "tool" and message.get("name"):
            called.add(str(message["name"]))
        for call in message.get("tool_calls") or []:
            name = (call.get("function") or {}).get("name") if isinstance(call, dict) else None
            if name:
                called.add(str(name))
    return route(
        sources,
        latest=user_texts[-1] if user_texts else "",
        earlier=user_texts[:-1],
        earlier_tools=called,
        enabled=settings.source_routing,
    )


def _use_source(
    routing: Routing, arguments: dict[str, Any], *, actor: str, actor_verified: bool, settings: Settings
) -> ToolResult:
    """The use_source meta tool. It reads no data, but it is a tool call, so it is audited like one."""
    result = routing.use_source(arguments)
    log_tool_call(
        actor=actor,
        actor_verified=actor_verified,
        tool=USE_SOURCE,
        client_code=None,
        row_ids=[],
        args=arguments,
        source=result.source,
        ok=result.ok,
        error=result.error,
        log_dir=settings.audit_log_dir,
    )
    return result


def _x_tb_brain(record: dict[str, Any], routing: Routing) -> dict[str, Any]:
    """The x_tb_brain block, plus the routing decision whenever routing had a choice to make."""
    if routing.active:
        record["routing"] = routing.record()
    return record


def _signed_in_line(source: FlowSource) -> str:
    email = (current_signed_in_email.get() or "").strip().lower() or None
    name = None
    code = None
    if email:
        matches = signed_in_matches(source, email)
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
    messages: list[dict[str, Any]],
    *,
    source: Sources | FlowSource,
    no_think: bool = False,
    style: str = "legacy",
    sections: list[str] | None = None,
) -> list[dict[str, Any]]:
    """`sections` (lean only) are the prompt sections of the sources offered to this call; None means every
    enabled source's."""
    if style == "lean":
        out = _ensure_lean_prompt(messages, source=source, sections=sections)
    else:
        out = _ensure_system_prompt(messages, source=source)
    if no_think:
        out = [_with_no_think(out[0]), *out[1:]]
    return out


def _with_no_think(message: dict[str, Any]) -> dict[str, Any]:
    first = dict(message)
    first["content"] = str(first.get("content") or "").rstrip() + "\n/no_think"
    return first


def _ensure_system_prompt(messages: list[dict[str, Any]], *, source: Sources | FlowSource) -> list[dict[str, Any]]:
    source = as_sources(source).flow  # who is signed in comes from Flow's technicians
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


def _ensure_lean_prompt(
    messages: list[dict[str, Any]], *, source: Sources | FlowSource, sections: list[str] | None = None
) -> list[dict[str, Any]]:
    """PROMPT_STYLE=lean: [static system message][session context][conversation].

    The system message is the same bytes for every user and day, and the model server puts the tool list right
    after it, so that whole prefix can come from its cache. Who is signed in and today's date follow it in
    their own message (see session_context_message for why that is not a system message).
    """
    sources = as_sources(source)
    first = _lean_system_message(messages, sources.prompt_sections() if sections is None else sections)
    context = session_context_message([_signed_in_line(sources.flow), _today_line()])
    if messages and messages[0].get("role") == "system":
        return [first, context, *messages[1:]]
    return [first, context, *messages]


def _lean_system_message(messages: list[dict[str, Any]], sections: list[str]) -> dict[str, Any]:
    """The lean system message for these sections, merged ahead of a client's own system message."""
    prompt = lean_system_prompt(sections)
    if messages and messages[0].get("role") == "system":
        first = dict(messages[0])
        content = str(first.get("content") or "")
        if "tb-brain" not in content:
            first["content"] = prompt + "\n\n" + content
        return first
    return {"role": "system", "content": prompt}


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


_DURATION_WORD = r"\d+(?:\.\d+)?\s*(?:hours?|hrs?|minutes?|mins?)\b"
_DURATION_RE = re.compile(_DURATION_WORD, re.IGNORECASE)
_DURATION_PAREN_RE = re.compile(rf"\s*\([^()]*{_DURATION_WORD}[^()]*\)", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_BLANK_LINE_RE = re.compile(r"\n\s*\n")
_LABEL_LINE_START_RE = re.compile(r"\n+(?=[A-Z][A-Z0-9]{1,8}-[A-Z0-9]{3,6}\b)")


def strip_unbacked_figures(prose: str, digest_text: str) -> str:
    """Prose written from a digest that had no hours or minutes in it can only have invented them.

    Drops parenthetical and sentence-level durations, then keeps only paragraphs that name a ticket
    the digest had (which also drops invented intros and closing sentences). The exact totals come
    from the tool's footer, not from the model.
    """
    labels = set(_TICKET_LABEL_RE.findall(digest_text))
    kept: list[str] = []
    # A new ticket label at the start of a line starts a new paragraph, blank line or not.
    prose = _LABEL_LINE_START_RE.sub("\n\n", prose.strip())
    for block in _BLANK_LINE_RE.split(prose):
        block = _DURATION_PAREN_RE.sub("", block)
        sentences = [s for s in _SENTENCE_SPLIT_RE.split(block) if not _DURATION_RE.search(s)]
        block = " ".join(sentences).strip()
        if block and any(label in block for label in labels):
            kept.append(block)
    return "\n\n".join(kept)


def _has_non_english_script(text: str) -> bool:
    """True if any letter is not a Latin letter (Thai, Chinese, Cyrillic, Arabic, ...)."""
    for char in text or "":
        if not char.isalpha():
            continue
        name = unicodedata.name(char, "")
        if not name.startswith("LATIN"):
            return True
    return False


def _english_only(text: str, fallback: str | None = None, *, no_tool: str = _NO_TOOL_ENGLISH) -> str:
    cleaned = _THINK_RE.sub("", text or "").strip()
    if cleaned and not _has_non_english_script(cleaned):
        return cleaned
    return (fallback or "").strip() or no_tool


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


def _assistant_payload(text: str, model: str, *, no_tool: str = _NO_TOOL_ENGLISH) -> dict[str, Any]:
    return _apply_english_reply(
        {
            "id": "tb-brain",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        },
        [text] if text else [],
        no_tool=no_tool,
    )


def no_tool_message(sources: Sources | FlowSource | None = None) -> str:
    """What the user sees when there is no usable answer, naming the enabled sources (Flow when none are given)."""
    return wording_for(as_sources(sources)).no_tool if sources is not None else _NO_TOOL_ENGLISH


def enforce_english_payload(payload: dict[str, Any], *, no_tool: str = _NO_TOOL_ENGLISH) -> dict[str, Any]:
    """Public last-mile gate for /v1/chat/completions."""
    return _apply_english_reply(payload, [], no_tool=no_tool)


def _apply_english_reply(
    payload: dict[str, Any], replies: list[str], *, no_tool: str = _NO_TOOL_ENGLISH
) -> dict[str, Any]:
    """Last gate: the user only ever sees English. Tool lists win; model text is discarded if not English."""
    if replies:
        text = _THINK_RE.sub("", replies[-1]).strip() or no_tool
    else:
        content = ""
        choices = payload.get("choices")
        if choices:
            content = str((choices[0].get("message") or {}).get("content") or "")
        text = _english_only(content, no_tool=no_tool)
    return _set_assistant_content(payload, text)


async def run_tool_loop(
    *,
    settings: Settings,
    sources: Sources | None = None,
    messages: list[dict[str, Any]],
    model: str | None,
    actor: str,
    actor_verified: bool = False,
    client_tools: list[Any] | None = None,
    extra_body: dict[str, Any] | None = None,
    knowledge: KnowledgeSource | None = None,
    source: FlowSource | None = None,
) -> dict[str, Any]:
    """`sources` is the enabled Sources bundle. `source` is the pre-A2 keyword for a bare Flow client."""
    sources = as_sources(sources if sources is not None else source)
    routing = _route(sources, messages, settings)
    words = wording_for(sources)  # what the user sees names every enabled source
    tools = routing.openai_tools(settings.prompt_style)
    relay_tools = {*sources.relay_tool_names(), SEARCH_KNOWLEDGE}
    write_names = sources.write_tool_names()
    known = tuple(tool["function"]["name"] for tool in openai_tools(sources, style=settings.prompt_style))
    ignored = _ignored_client_tool_names(client_tools, known)
    if ignored:
        _log.info("ignoring client tools: %s", ",".join(ignored))
    turn = chat_turn_from_messages(messages)
    hit = _direct_answer_named(sources, turn, settings.disabled_router_set)
    if hit is not None:
        router_name, direct = hit
        payload = _assistant_payload(
            direct.reply or direct.error or words.no_tool,
            settings.llm_model.strip() or "tb-brain",
            no_tool=words.no_tool,
        )
        payload["x_tb_brain"] = {"router": router_name, "tool_calls": []}
        return payload
    trace: list[dict[str, Any]] = []
    nudged = False
    forced_tried = False
    fallbacks: list[str] = []  # plain lists, used if a summary never comes out of the model
    history = _compact_history(list(messages))
    chat = _ensure_system(
        history,
        source=sources,
        no_think=settings.llm_no_think,
        style=settings.prompt_style,
        sections=routing.prompt_sections(),
    )
    prompt_tokens = 0
    relayed: list[str] = []
    digests: list[str] = []  # what summary-mode tools gave the model to write prose from
    footers: list[str] = []  # exact numbers appended after the model's prose
    field_headers: list[str] = []  # exact fields printed before the model's prose
    scrub_figures = False  # the digest had no numbers, so any the model writes are invented
    tool_errors: list[str] = []  # what to show if the model answers a failed tool call with nothing
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    timeout = httpx.Timeout(settings.llm_timeout_seconds)
    async with httpx.AsyncClient(base_url=settings.llm_base_url, headers=headers, timeout=timeout) as client:
        resolved_model = await _resolve_model(client, settings, model)
        last_payload: dict[str, Any] | None = None
        for iteration in range(settings.agent_max_tool_iters):
            body: dict[str, Any] = {
                "model": resolved_model,
                "messages": chat,
                "stream": False,
            }
            if not digests:  # a digest means "write the answer now": offering tools invites repeat calls
                body["tools"] = tools
            routing.tool_counts.append(len(body.get("tools") or ()))
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
                content = str(message.get("content") or "")
                blocked = None
                skipped = unusable = False
                if not relayed:
                    if digests:
                        # Prose written from a digest may name tickets, but only ones the digest had.
                        grounded = " ".join(digests)
                        invented = [label for label in _TICKET_LABEL_RE.findall(content) if label not in grounded]
                        blocked = _NO_INVENTED_TICKETS if invented else None
                        unusable = not content.strip() or _has_non_english_script(content)
                    else:
                        blocked = reply_without_invented_tickets(content, relayed)
                    skipped = not trace and _expects_tool(turn, sources)
                    if (blocked or skipped or unusable) and not nudged:
                        # Made-up labels, no tool call for a data question, or an empty / non-English
                        # summary. One retry before anything else.
                        nudged = True
                        chat.append({"role": "assistant", "content": content})
                        # The nudge names only the sources this call offered: their tools are the ones it can call.
                        nudge = _NUDGE_TO_WRITE if digests else wording_for(sources, routing.offered).nudge_to_use_a_tool
                        chat.append({"role": "user", "content": nudge})
                        _log.info("nudging model (digest=%s)", bool(digests), extra={"event": "agent.nudge"})
                        continue
                    if (blocked or skipped) and not digests:
                        # Twice without a tool. A pattern router is a better safety net than a refusal,
                        # even for routers the operator turned off.
                        fallback = _direct_answer_named(sources, turn)
                        if fallback is not None:
                            name, direct = fallback
                            payload = _assistant_payload(
                                direct.reply or direct.error or words.no_tool,
                                settings.llm_model.strip() or "tb-brain",
                                no_tool=words.no_tool,
                            )
                            payload["x_tb_brain"] = _x_tb_brain(
                                {"router": f"fallback:{name}", "tool_calls": trace}, routing
                            )
                            return payload
                        if not forced_tried:
                            # No router fits either: make the tool call with constrained JSON, which
                            # cannot come back as prose, and carry on as if the model had called it.
                            forced_tried = True
                            forced = await _force_tool_call(
                                client, resolved_model, chat, tools, (*write_names, USE_SOURCE)
                            )
                            if forced is not None:
                                message = forced
                                tool_calls = forced["tool_calls"]
                if not tool_calls:
                    if not relayed:
                        if blocked:
                            payload = _set_assistant_content(payload, blocked)
                        elif skipped:
                            payload = _set_assistant_content(payload, words.no_search_run)
                    payload = _apply_english_reply(payload, relayed, no_tool=words.no_tool)
                    if not relayed and not (footers or field_headers):
                        blank = not str(payload["choices"][0]["message"].get("content") or "").strip()
                        if blank:  # the model said nothing: show the tool's own error, never an empty reply
                            payload = _set_assistant_content(payload, tool_errors[-1] if tool_errors else words.no_search_run)
                    if (footers or field_headers) and not relayed:
                        prose = str(payload["choices"][0]["message"].get("content") or "").strip()
                        if scrub_figures and prose not in (_NO_INVENTED_TICKETS, words.no_tool) and not unusable:
                            prose = strip_unbacked_figures(prose, " ".join(digests)) or (fallbacks[-1] if fallbacks else "")
                        if prose in (_NO_INVENTED_TICKETS, words.no_tool) or unusable:
                            prose = fallbacks[-1] if fallbacks else ""  # no usable summary: show the plain list
                        parts = [field_headers[-1] if field_headers else "", prose, footers[-1] if footers else ""]
                        payload = _set_assistant_content(payload, "\n\n".join(part for part in parts if part))
                    payload["x_tb_brain"] = _x_tb_brain(
                        {"router": None, "tool_calls": trace, "prompt_tokens": prompt_tokens}, routing
                    )
                    return payload
            chat.append(message)
            _log.info(
                "agent tool_calls iteration=%s count=%s",
                iteration,
                len(tool_calls),
                extra={"event": "agent.tool_calls", "iteration": iteration},
            )
            offered_tools = {tool["function"]["name"] for tool in tools}  # what this call was given
            for call in tool_calls:
                fn = call.get("function") or {}
                name = str(fn.get("name") or "")
                arguments = _parse_arguments(fn.get("arguments"))
                if name == USE_SOURCE and name in offered_tools:
                    result = _use_source(routing, arguments, actor=actor, actor_verified=actor_verified, settings=settings)
                    tools = routing.openai_tools(settings.prompt_style)
                    if settings.prompt_style == "lean":  # the added source's section joins the next call
                        first = _lean_system_message(history, routing.prompt_sections())
                        chat[0] = _with_no_think(first) if settings.llm_no_think else first
                else:
                    result = run_tool(
                        sources=sources,
                        name=name,
                        arguments=arguments,
                        actor=actor,
                        actor_verified=actor_verified,
                        settings=settings,
                        turn=turn,
                        knowledge=knowledge,
                        refusal=routing.refusal(name, offered_tools),
                    )
                trace.append({"name": name, "arguments": arguments, "ok": result.ok, "error": result.error})
                if result.reply and name in relay_tools:
                    relayed.append(result.reply)
                if not result.ok and result.error:
                    tool_errors.append(result.error)
                if result.chunks:
                    findings = []
                    for chunk in result.chunks:
                        text = await _summarize_chunk(client, resolved_model, chunk["prompt"], settings.llm_extra_body_dict)
                        _log.info("read %s", chunk["label"], extra={"event": "agent.chunk", "label": chunk["label"]})
                        if text:
                            findings.append(f"{chunk['label']}:\n{text}")
                    if findings:
                        result = result.model_copy(
                            update={
                                "digest": "\n".join(
                                    [result.reduce_prefix or "", "", "Findings from reading real tickets:", *findings, "", result.reduce_suffix or ""]
                                ),
                                "chunks": None,
                            }
                        )
                    else:
                        result = result.model_copy(update={"chunks": None})  # keep the title-only digest
                if result.digest:
                    digests.append(result.digest)
                    scrub_figures = result.prose_without_figures
                if result.fallback:
                    fallbacks.append(result.fallback)
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
        _log.warning("tool loop hit AGENT_MAX_TOOL_ITERS=%s", settings.agent_max_tool_iters)
        stuck = _assistant_payload(
            "\n\n".join(
                part
                for part in (
                    field_headers[-1] if field_headers else "",
                    words.stuck if not (field_headers or footers) else "",
                    relayed[-1] if relayed else "",
                    footers[-1] if footers else "",
                )
                if part
            ),
            settings.llm_model.strip() or "tb-brain",
            no_tool=words.no_tool,
        )
        stuck["x_tb_brain"] = _x_tb_brain(
            {"router": None, "tool_calls": trace, "prompt_tokens": prompt_tokens, "stuck": True}, routing
        )
        return stuck


async def stream_final_message(payload: dict[str, Any], *, no_tool: str = _NO_TOOL_ENGLISH) -> AsyncIterator[bytes]:
    """Run the full tool loop first, then SSE-stream the final assistant text."""
    raw = ""
    if payload.get("choices"):
        raw = str((payload["choices"][0].get("message") or {}).get("content") or "")
    payload = _set_assistant_content(payload, _THINK_RE.sub("", raw).strip() or no_tool)
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
