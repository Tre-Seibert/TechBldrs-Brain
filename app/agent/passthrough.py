"""Open WebUI's background tasks (chat title, tags, follow-up questions, search queries, autocomplete) go straight
to the model: no tb-brain system prompt, no tools, no routing, no routers and no Flow calls.

Open WebUI sends them to whatever model the chat uses, so with tb-brain as the chat model each one used to run the
whole tool loop (and look up the signed-in technician in Flow) only to write a three-word title.

How a request is recognized, checked against Open WebUI main (v0.12.0, the image docker-compose.yml pulls):

- backend/open_webui/routers/tasks.py marks each task in the payload's metadata ("task": "title_generation"),
  but routers/openai.py pops metadata before calling an OpenAI connection, so tb-brain never receives it. A body
  that does carry metadata.task (another client, or a later version) is still honored.
- A connection can send it as a header: in Open WebUI's connection settings add the header
  X-OpenWebUI-Task with the value {{TASK}} (utils/headers.py fills it in; empty for ordinary chats).
- Otherwise the prompt: the task is the only non-system message, a user message built from one of the default
  templates in backend/open_webui/config.py. Each starts "### Task:", names the task on the next line, and quotes
  the chat inside <chat_history> ... </chat_history>. The RAG template also starts "### Task:" but has no chat
  history block and wraps a real question, so it is not a task. A user who types "### Task:" in a chat does not
  match unless they also paste a whole task template as the only message of a new chat.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.agent.loop import AgentError, _resolve_model
from app.config import Settings

_log = logging.getLogger("tb_brain.agent")

# backend/open_webui/constants.py TASKS, without function_calling (Open WebUI's own tool picking, which wants tools).
TASK_NAMES = frozenset(
    {
        "title_generation",
        "tags_generation",
        "follow_up_generation",
        "query_generation",
        "image_prompt_generation",
        "autocomplete_generation",
        "emoji_generation",
        "moa_response_generation",
    }
)

# The line after "### Task:" in each default template (backend/open_webui/config.py, DEFAULT_*_PROMPT_TEMPLATE).
_TEMPLATE_OPENINGS = (
    ("title_generation", "Generate a concise title summarizing the chat history."),
    ("tags_generation", "Generate 1-3 broad tags categorizing the main themes of the chat history"),
    ("follow_up_generation", "Suggest 3-5 relevant follow-up questions or prompts"),
    ("query_generation", "Analyze the chat history to determine the necessity of generating search queries"),
    ("image_prompt_generation", "Generate a detailed prompt for am image generation task"),
    ("autocomplete_generation", "You are an autocompletion system."),
)
_TASK_HEADING = "### Task:"


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # Open WebUI's own message-content helper takes the first text part
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return str(part.get("text") or "")
    return ""


def _from_prompt(messages: list[dict[str, Any]]) -> str | None:
    rest = [message for message in messages if message.get("role") != "system"]
    if len(rest) != 1 or rest[0].get("role") != "user":
        return None
    lines = _text(rest[0].get("content")).strip().splitlines()
    if len(lines) < 2 or lines[0].strip() != _TASK_HEADING:
        return None
    text = "\n".join(lines)
    start = text.find("<chat_history>")
    if start < 0 or text.find("</chat_history>", start) < 0:
        return None
    opening = lines[1].strip()
    return next((task for task, first_line in _TEMPLATE_OPENINGS if opening.startswith(first_line)), None)


def open_webui_task(messages: list[dict[str, Any]], metadata: Any = None, header: str | None = None) -> str | None:
    """The Open WebUI background task this request is (e.g. 'title_generation'), or None for a real chat turn."""
    for marker in ((metadata or {}).get("task") if isinstance(metadata, dict) else None, header):
        name = str(marker or "").strip().lower()
        if name in TASK_NAMES:
            return name
    return _from_prompt(messages)


async def run_task_passthrough(
    *,
    settings: Settings,
    task: str,
    messages: list[dict[str, Any]],
    model: str | None,
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send the request to LLM_BASE_URL as it came (no tools, no system prompt) and return the model's reply as-is."""
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    timeout = httpx.Timeout(settings.llm_timeout_seconds)
    async with httpx.AsyncClient(base_url=settings.llm_base_url, headers=headers, timeout=timeout) as client:
        resolved_model = await _resolve_model(client, settings, model)
        body: dict[str, Any] = {"model": resolved_model, "messages": messages, "stream": False}
        body.update(settings.llm_extra_body_dict)
        for key, value in (extra_body or {}).items():
            if key in ("messages", "tools", "tool_choice", "stream", "model", "metadata"):
                continue
            body[key] = value
        try:
            response = await client.post("/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise AgentError(f"LLM unreachable at {settings.llm_base_url}: {exc}") from exc
    if response.status_code >= 400:
        raise AgentError(f"LLM error {response.status_code}: {response.text[:800]}")
    payload = response.json()
    payload["x_tb_brain"] = {"task_passthrough": True, "task": task}
    _log.info("open webui task passed through task=%s", task, extra={"event": "agent.task_passthrough", "task": task})
    return payload
