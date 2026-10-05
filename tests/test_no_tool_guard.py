from __future__ import annotations

import json
import unittest
from unittest import mock

import httpx

from app.agent.loop import (
    _NO_SEARCH_RUN,
    _compact_history,
    _expects_tool,
    run_tool_loop,
)
from app.config import Settings
from app.flow.stub import StubFlowSource
from app.tools import ChatTurn

LONG_LIST = "6 open ticket(s) for ts\n\n" + "\n".join(
    f"- ZTB-16{n:02d} — Some long ticket subject number {n} (ZTB · New · 6 Project · ts · Internal · 3.1 hours)"
    for n in range(8)
)


class ExpectsToolTests(unittest.TestCase):
    def test_data_questions_expect_a_tool(self) -> None:
        for text in (
            "my urgent tickets",
            "what are my urgent tickets?",
            "Which of my tickets are overdue?",
            "buck tickets",
            "What did I work on last week?",
            "Who contacts us the most from ZEBB?",
            "Show me ZTB-1680",
            "emails from WDON",
        ):
            self.assertTrue(_expects_tool(ChatTurn(user_text=text)), text)

    def test_writes_off_topic_and_secrets_do_not(self) -> None:
        for text in (
            "Close ticket BUCK-1234",
            "Email Debe that we are on it",
            "Log 30 minutes on BPIE-0042",
            "Archive ticket NGMC-0101",
            "Merge those",
            "Whats the weather",
            "What's the admin password for BUCK?",
            "hi",
        ):
            self.assertFalse(_expects_tool(ChatTurn(user_text=text)), text)

    def test_a_bare_yes_follows_the_previous_question(self) -> None:
        self.assertFalse(_expects_tool(ChatTurn(user_text="Yes")))
        self.assertTrue(
            _expects_tool(ChatTurn(user_text="Yes", previous_assistant_text="Shall I list all open tickets for WDON?"))
        )


class CompactHistoryTests(unittest.TestCase):
    def test_long_assistant_answers_shrink_and_everything_else_is_untouched(self) -> None:
        history = [
            {"role": "user", "content": "what are my open tickets?"},
            {"role": "assistant", "content": LONG_LIST},
            {"role": "assistant", "content": "short reply"},
            {"role": "user", "content": LONG_LIST},
        ]
        out = _compact_history(history)
        self.assertEqual(out[0], history[0])
        self.assertIn("6 open ticket(s) for ts", out[1]["content"])
        self.assertNotIn("ZTB-1601", out[1]["content"])
        self.assertLess(len(out[1]["content"]), len(LONG_LIST) // 2)
        self.assertEqual(out[2], history[2])
        self.assertEqual(out[3], history[3])  # user text is never touched
        self.assertIn("ZTB-1601", history[1]["content"])  # input not mutated


def _llm(script: list[dict], seen: list[dict]):
    replies = iter(script)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "x",
                "usage": {"prompt_tokens": 1234, "completion_tokens": 5},
                "choices": [{"index": 0, "message": next(replies)}],
            },
        )

    return handler


def _tool_call(name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}],
    }


class NoToolGuardTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, messages: list[dict], script: list[dict], seen: list[dict]) -> dict:
        real = httpx.AsyncClient
        transport = httpx.MockTransport(_llm(script, seen))
        with mock.patch("app.agent.loop.httpx.AsyncClient", lambda **kw: real(transport=transport, **kw)):
            return await run_tool_loop(
                settings=Settings(llm_model="fake", llm_base_url="http://llm.test/v1", _env_file=None),
                source=StubFlowSource(),
                messages=messages,
                model=None,
                actor="test",
            )

    async def test_a_made_up_answer_with_no_tool_is_nudged_then_uses_the_tool(self) -> None:
        seen: list[dict] = []
        payload = await self._run(
            [{"role": "user", "content": "what are my urgent tickets?"}],
            [
                {"role": "assistant", "content": "Checking for urgent tickets... 0 urgent ticket(s) assigned to ts."},
                _tool_call("list_tickets", {"category": "urgent", "assignee_code": "ts"}),
                {"role": "assistant", "content": "ignored"},
            ],
            seen,
        )
        self.assertEqual([c["name"] for c in payload["x_tb_brain"]["tool_calls"]], ["list_tickets"])
        self.assertNotIn("0 urgent ticket(s) assigned to ts.", payload["choices"][0]["message"]["content"].split("\n")[0])
        self.assertIn("must call the right tool", seen[1]["messages"][-1]["content"])

    async def test_still_no_tool_after_the_nudge_shows_a_safe_message_not_the_guess(self) -> None:
        payload = await self._run(
            [{"role": "user", "content": "my urgent tickets"}],
            [
                {"role": "assistant", "content": "No urgent tickets assigned to you at the moment."},
                {"role": "assistant", "content": "Still none, sorry."},
            ],
            [],
        )
        self.assertEqual(payload["choices"][0]["message"]["content"], _NO_SEARCH_RUN)
        self.assertEqual(payload["x_tb_brain"]["tool_calls"], [])

    async def test_a_decline_to_a_write_request_is_left_alone(self) -> None:
        seen: list[dict] = []
        payload = await self._run(
            [{"role": "user", "content": "Close ticket BUCK-1234"}],
            [{"role": "assistant", "content": "I can't do that; I'm read-only except for merging tickets."}],
            seen,
        )
        self.assertEqual(len(seen), 1)  # no nudge
        self.assertIn("read-only", payload["choices"][0]["message"]["content"])

    async def test_off_topic_is_left_alone(self) -> None:
        seen: list[dict] = []
        payload = await self._run(
            [{"role": "user", "content": "Whats the weather"}],
            [{"role": "assistant", "content": "I only answer questions about Flow data."}],
            seen,
        )
        self.assertEqual(len(seen), 1)
        self.assertIn("only answer", payload["choices"][0]["message"]["content"])

    async def test_a_tool_that_errored_is_not_replaced_by_the_safe_message(self) -> None:
        payload = await self._run(
            [{"role": "user", "content": "show me tickets for ZZZZ"}],
            [
                _tool_call("list_tickets", {"limit": 5}),  # invalid arguments -> tool error, no reply
                {"role": "assistant", "content": "The search failed because it needs a client or technician."},
            ],
            [],
        )
        self.assertIn("search failed", payload["choices"][0]["message"]["content"])

    async def test_long_history_is_compacted_for_the_model_and_prompt_tokens_are_reported(self) -> None:
        seen: list[dict] = []
        payload = await self._run(
            [
                {"role": "user", "content": "what are my open tickets?"},
                {"role": "assistant", "content": LONG_LIST},
                {"role": "user", "content": "Who contacts us the most from ACME?"},
            ],
            [
                _tool_call("ticket_stats", {"entity": "tickets", "group_by": "requestor", "client_code": "ACME"}),
                {"role": "assistant", "content": "ignored"},
            ],
            seen,
        )
        sent = json.dumps(seen[0]["messages"])
        self.assertNotIn("ZTB-1601", sent)
        self.assertEqual(payload["x_tb_brain"]["prompt_tokens"], 1234)


if __name__ == "__main__":
    unittest.main()
