from __future__ import annotations

import json
import unittest
from unittest import mock

import httpx

from app.agent.loop import run_tool_loop
from app.config import Settings
from app.flow.stub import StubFlowSource


def _fake_llm(script: list[dict]):
    """An OpenAI-compatible endpoint that replays `script`, one message per request."""
    replies = iter(script)

    def handler(request: httpx.Request) -> httpx.Response:
        message = next(replies)
        return httpx.Response(200, json={"id": "x", "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]})

    return handler


def _tool_call(name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}],
    }


class LoopTraceTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, question: str, script: list[dict]) -> dict:
        real = httpx.AsyncClient
        transport = httpx.MockTransport(_fake_llm(script))
        with mock.patch(
            "app.agent.loop.httpx.AsyncClient",
            lambda **kw: real(transport=transport, **kw),
        ):
            return await run_tool_loop(
                settings=Settings(llm_model="fake", llm_base_url="http://llm.test/v1", _env_file=None),
                source=StubFlowSource(),
                messages=[{"role": "user", "content": question}],
                model=None,
                actor="test",
            )

    async def test_trace_lists_the_tool_calls_the_model_made(self) -> None:
        payload = await self._run(
            "Who contacts us the most from ACME?",
            [
                _tool_call("ticket_stats", {"entity": "tickets", "group_by": "requestor", "client_code": "ACME"}),
                {"role": "assistant", "content": "ignored: the tool reply wins"},
            ],
        )
        trace = payload["x_tb_brain"]
        self.assertIsNone(trace["router"])
        self.assertEqual([c["name"] for c in trace["tool_calls"]], ["ticket_stats"])
        self.assertTrue(trace["tool_calls"][0]["ok"])
        self.assertEqual(trace["tool_calls"][0]["arguments"]["group_by"], "requestor")
        # ticket_stats is relayed verbatim, so the numbers the user sees are Flow's.
        text = payload["choices"][0]["message"]["content"]
        self.assertIn("Riley Chen", text)

    async def test_failed_tool_call_is_traced_with_its_error(self) -> None:
        payload = await self._run(
            "anything",
            [_tool_call("list_tickets", {"limit": 5}), {"role": "assistant", "content": "done"}],
        )
        call = payload["x_tb_brain"]["tool_calls"][0]
        self.assertFalse(call["ok"])
        self.assertIn("Invalid arguments", call["error"])

    async def test_router_answer_has_empty_calls(self) -> None:
        payload = await self._run("Which open ticket has the longest time worked", [])
        self.assertEqual(payload["x_tb_brain"], {"router": "longest_time", "tool_calls": []})


if __name__ == "__main__":
    unittest.main()
