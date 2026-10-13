"""Open WebUI background tasks (title, tags, follow-ups, search queries, autocomplete) skip the tool loop.

The prompts below are Open WebUI's default templates (backend/open_webui/config.py on main, v0.12.0) with the
chat history filled in the way Open WebUI renders it ("USER: ...\\nASSISTANT: ...").
"""

from __future__ import annotations

import tests._isolation  # noqa: F401  (first: hermetic settings, no network)

import json
import unittest
from typing import Any
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from app.agent.passthrough import open_webui_task
from app.config import Settings
from app.main import app
from app.sources.registry import build_sources

HISTORY = "USER: Which WDON tickets are open?\nASSISTANT: 2 open ticket(s) for WDON: WDON-1842, WDON-1850."

TITLE = (
    "### Task:\nGenerate a concise title summarizing the chat history.\n### Guidelines:\n"
    "- The title should clearly represent the main theme or subject of the conversation.\n"
    "- Keep it short: 2-4 words is best.\n- Do not use emojis, quotation marks, or special formatting.\n"
    "- Write the title in the chat's primary language; default to English if multilingual.\n"
    "- Prioritize accuracy over creativity.\n"
    "- Your entire response must consist solely of the JSON object, without any introductory or concluding text.\n"
    "- The output must be a single, raw JSON object, without any markdown code fences or other encapsulating text.\n"
    "- Ensure no conversational text, affirmations, or explanations precede or follow the raw JSON output, as this "
    "will cause direct parsing failure.\n### Output:\nJSON format: { \"title\": \"your concise title here\" }\n"
    "### Examples:\n- { \"title\": \"Stock Trends\" },\n- { \"title\": \"Chocolate Chip Cookies\" },\n"
    "- { \"title\": \"Music Streaming\" },\n- { \"title\": \"Remote Work\" }\n### Chat History:\n"
    f"<chat_history>\n{HISTORY}\n</chat_history>"
)


def _task(first_line: str, tail: str = "") -> str:
    return f"### Task:\n{first_line}\n\n### Guidelines:\n- ...\n\n### Chat History:\n<chat_history>\n{HISTORY}\n</chat_history>{tail}"


TASKS = {
    "title_generation": TITLE,
    "tags_generation": _task(
        "Generate 1-3 broad tags categorizing the main themes of the chat history, along with 1-3 more specific subtopic tags."
    ),
    "follow_up_generation": _task(
        "Suggest 3-5 relevant follow-up questions or prompts that the user might naturally ask next in this "
        "conversation as a **user**, based on the chat history, to help continue or deepen the discussion."
    ),
    "query_generation": _task(
        "Analyze the chat history to determine the necessity of generating search queries, in the given language."
    ),
    "autocomplete_generation": _task(
        "You are an autocompletion system. Continue the text in `<text>` based on the **completion type** in `<type>` "
        "and the given language.  ",
        "\n<type>General</type>  \n<text>Which WDON</text>  \n#### Output:\n",
    ),
}

RAG_SYSTEM = (
    "### Task:\nRespond to the user query using the provided context, incorporating inline citations in the format "
    "[id] **only when the <source> tag includes an explicit id attribute** (e.g., <source id=\"1\">).\n\n"
    "<context>\n<source id=\"1\">VPN runbook</source>\n</context>\n"
)


def _user(text: str) -> dict[str, str]:
    return {"role": "user", "content": text}


class DetectionTests(unittest.TestCase):
    def test_each_default_template_is_a_task(self) -> None:
        for name, prompt in TASKS.items():
            with self.subTest(name):
                self.assertEqual(open_webui_task([_user(prompt)]), name)

    def test_a_model_system_prompt_ahead_of_the_task_still_counts(self) -> None:
        messages = [{"role": "system", "content": "Be brief."}, _user(TITLE)]
        self.assertEqual(open_webui_task(messages), "title_generation")

    def test_list_content_reads_its_text_part(self) -> None:
        self.assertEqual(open_webui_task([{"role": "user", "content": [{"type": "text", "text": TITLE}]}]), "title_generation")

    def test_metadata_or_header_marker_wins(self) -> None:
        self.assertEqual(open_webui_task([_user("hi")], {"task": "tags_generation"}), "tags_generation")
        self.assertEqual(open_webui_task([_user("hi")], None, "follow_up_generation"), "follow_up_generation")
        # Open WebUI fills an X-OpenWebUI-Task: {{TASK}} header with "" for ordinary chats.
        self.assertIsNone(open_webui_task([_user("hi")], None, ""))
        # Open WebUI's own tool picking wants tools, so it is not passed through.
        self.assertIsNone(open_webui_task([_user("hi")], {"task": "function_calling"}))

    def test_real_user_messages_are_not_tasks(self) -> None:
        cases = {
            "heading only": "### Task:\nList the open WDON tickets.",
            "mentions both markers": "Open WebUI sent '### Task:' with <chat_history> in it; which WDON tickets are open?",
            "template pasted after a question": "Why does this run tools?\n" + TITLE,
            "unknown task line": "### Task:\nSummarize these tickets.\n<chat_history>\nUSER: hi\n</chat_history>",
            "no chat history block": "### Task:\nGenerate a concise title summarizing the chat history.\n",
            "unclosed chat history": "### Task:\nGenerate a concise title summarizing the chat history.\n<chat_history>\nUSER: hi",
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.assertIsNone(open_webui_task([_user(text)]))

    def test_rag_template_is_a_real_chat_turn(self) -> None:
        self.assertIsNone(open_webui_task([{"role": "system", "content": RAG_SYSTEM}, _user("Which WDON tickets are open?")]))

    def test_a_template_as_a_later_turn_is_not_a_task(self) -> None:
        messages = [_user("hi"), {"role": "assistant", "content": "Hello"}, _user(TITLE)]
        self.assertIsNone(open_webui_task(messages))


class PassthroughTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.status = 200
        self.reply: dict[str, Any] = {"role": "assistant", "content": '{ "title": "WDON Open Tickets" }'}
        real = httpx.AsyncClient
        transport = httpx.MockTransport(self._llm)
        patcher = mock.patch("httpx.AsyncClient", lambda **kw: real(transport=transport, **kw))
        patcher.start()
        self.addCleanup(patcher.stop)
        flow_calls: list[str] = []
        sources = build_sources(Settings(_env_file=None))
        flow = sources.flow
        for name in ("list_tickets", "search_technician", "get_client", "list_clients"):
            original = getattr(flow, name)
            patch = mock.patch.object(flow, name, side_effect=lambda *a, _n=name, _o=original, **k: (flow_calls.append(_n), _o(*a, **k))[1])
            patch.start()
            self.addCleanup(patch.stop)
        self.flow_calls = flow_calls
        app.state.settings = Settings(llm_model="fake", llm_base_url="http://llm.test/v1", _env_file=None)
        app.state.sources = sources
        self.addCleanup(lambda: [delattr(app.state, name) for name in ("settings", "sources") if hasattr(app.state, name)])
        self.client = TestClient(app)

    def _llm(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        if self.status != 200:
            return httpx.Response(self.status, text="model crashed")
        return httpx.Response(200, json={"id": "x", "model": "fake", "choices": [{"index": 0, "message": self.reply}]})

    def _post(self, messages: list[dict[str, Any]], **body: Any) -> httpx.Response:
        return self.client.post(
            "/v1/chat/completions",
            json={"model": "tb-brain", "messages": messages, **body},
            headers={"X-OpenWebUI-User-Email": "tseibert@techbldrs.example"},
        )

    def test_title_task_goes_straight_to_the_model(self) -> None:
        response = self._post([_user(TITLE)], stream=False, max_tokens=1000)
        payload = response.json()
        self.assertEqual(payload["choices"][0]["message"]["content"], '{ "title": "WDON Open Tickets" }')
        self.assertEqual(payload["x_tb_brain"], {"task_passthrough": True, "task": "title_generation"})
        self.assertEqual(len(self.bodies), 1)
        body = self.bodies[0]
        self.assertEqual(body["messages"], [_user(TITLE)])  # no tb-brain system prompt, no session context
        self.assertNotIn("tools", body)
        self.assertEqual(body["model"], "fake")
        self.assertEqual(body["max_tokens"], 1000)
        self.assertEqual(self.flow_calls, [])  # not even the signed-in technician lookup

    def test_every_task_runs_one_model_call_and_no_flow_calls(self) -> None:
        for name, prompt in TASKS.items():
            with self.subTest(name):
                self.bodies.clear()
                payload = self._post([_user(prompt)]).json()
                self.assertEqual(payload["x_tb_brain"]["task"], name)
                self.assertEqual(len(self.bodies), 1)
                self.assertNotIn("tools", self.bodies[0])
        self.assertEqual(self.flow_calls, [])

    def test_reply_is_returned_as_is(self) -> None:
        # The chat's English gate would replace this; a task's reply is the model's own.
        self.reply = {"role": "assistant", "content": '{ "tags": ["Technology", "Help Desk"] }'}
        payload = self._post([_user(TASKS["tags_generation"])]).json()
        self.assertEqual(payload["choices"][0]["message"]["content"], '{ "tags": ["Technology", "Help Desk"] }')

    def test_metadata_is_not_forwarded_to_the_model(self) -> None:
        self._post([_user("hi")], metadata={"task": "title_generation", "chat_id": "c1"})
        self.assertNotIn("metadata", self.bodies[0])
        self.assertEqual(self.bodies[0]["messages"], [_user("hi")])

    def test_streamed_task_streams_the_reply(self) -> None:
        response = self._post([_user(TITLE)], stream=True)
        self.assertIn('{ \\"title\\": \\"WDON Open Tickets\\" }', response.text)
        self.assertTrue(response.text.rstrip().endswith("data: [DONE]"))

    def test_llm_error_is_a_502(self) -> None:
        self.status = 500
        response = self._post([_user(TITLE)])
        self.assertEqual(response.status_code, 502)
        self.assertIn("LLM error 500", response.text)

    def test_a_real_question_still_runs_the_tool_loop(self) -> None:
        self.reply = {"role": "assistant", "content": "No tool."}
        payload = self._post([_user("### Task:\nList the open WDON tickets.")]).json()
        self.assertNotIn("task_passthrough", payload["x_tb_brain"])
        self.assertIn("tools", self.bodies[0])
        self.assertEqual(self.bodies[0]["messages"][0]["role"], "system")


if __name__ == "__main__":
    unittest.main()
