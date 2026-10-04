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


class NoThinkAndSchemaTests(unittest.TestCase):
    def test_no_think_switch_is_appended_only_when_asked(self) -> None:
        from app.agent.loop import _ensure_system

        source = StubFlowSource()
        plain = _ensure_system([{"role": "user", "content": "hi"}], source=source)
        switched = _ensure_system([{"role": "user", "content": "hi"}], source=source, no_think=True)
        self.assertNotIn("/no_think", plain[0]["content"])
        self.assertTrue(switched[0]["content"].endswith("/no_think"))
        self.assertEqual(switched[1], {"role": "user", "content": "hi"})

    def test_time_entry_from_real_flow_has_no_ticket_num(self) -> None:
        from app.flow.schemas import TimeEntryRecord

        row = TimeEntryRecord.model_validate(
            {"id": 62665, "ticket_id": 95045, "client_code": "BUCK", "ticket_label": "BUCK-3540",
             "tech_user_id": 376, "minutes": 15, "created_at": "2026-10-02 20:21:29"}
        )
        self.assertEqual(row.ticket_num, "3540")
        self.assertEqual(row.ticket_label, "BUCK-3540")


class ExtraBodyTests(unittest.IsolatedAsyncioTestCase):
    def test_extra_body_must_be_a_json_object(self) -> None:
        self.assertEqual(Settings(llm_extra_body='{"think": false}', _env_file=None).llm_extra_body_dict, {"think": False})
        self.assertEqual(Settings(_env_file=None).llm_extra_body_dict, {})
        with self.assertRaises(ValueError):
            Settings(llm_extra_body="[1, 2]", _env_file=None)

    async def test_extra_body_is_sent_to_the_llm(self) -> None:
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(
                200, json={"id": "x", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]}
            )

        real = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with mock.patch("app.agent.loop.httpx.AsyncClient", lambda **kw: real(transport=transport, **kw)):
            await run_tool_loop(
                settings=Settings(
                    llm_model="fake", llm_base_url="http://llm.test/v1",
                    llm_extra_body='{"reasoning_effort": "none"}', _env_file=None,
                ),
                source=StubFlowSource(),
                messages=[{"role": "user", "content": "anything"}],
                model=None,
                actor="test",
            )
        self.assertEqual(seen[0]["reasoning_effort"], "none")


class NudgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_answer_from_memory_gets_one_nudge_then_uses_a_tool(self) -> None:
        requests: list[dict] = []
        script = iter(
            [
                {"role": "assistant", "content": "You spent the most time on ZTB-9999."},  # invented, no tool
                _tool_call("ticket_stats", {"entity": "time", "group_by": "ticket", "metric": "hours"}),
                {"role": "assistant", "content": "ignored: the tool reply wins"},
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "x", "choices": [{"index": 0, "message": next(script)}]})

        real = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with mock.patch("app.agent.loop.httpx.AsyncClient", lambda **kw: real(transport=transport, **kw)):
            payload = await run_tool_loop(
                settings=Settings(llm_model="fake", llm_base_url="http://llm.test/v1", _env_file=None),
                source=StubFlowSource(),
                messages=[{"role": "user", "content": "what ticket have I spent the most time on?"}],
                model=None,
                actor="test",
            )
        self.assertEqual([c["name"] for c in payload["x_tb_brain"]["tool_calls"]], ["ticket_stats"])
        self.assertNotIn("ZTB-9999", payload["choices"][0]["message"]["content"])
        self.assertIn("without calling a tool", requests[1]["messages"][-1]["content"])

    async def test_a_second_invented_answer_is_still_refused(self) -> None:
        script = iter(
            [
                {"role": "assistant", "content": "It is ZTB-9999."},
                {"role": "assistant", "content": "Definitely ZTB-9999."},
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "x", "choices": [{"index": 0, "message": next(script)}]})

        real = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with mock.patch("app.agent.loop.httpx.AsyncClient", lambda **kw: real(transport=transport, **kw)):
            payload = await run_tool_loop(
                settings=Settings(llm_model="fake", llm_base_url="http://llm.test/v1", _env_file=None),
                source=StubFlowSource(),
                messages=[{"role": "user", "content": "anything"}],
                model=None,
                actor="test",
            )
        text = payload["choices"][0]["message"]["content"]
        self.assertNotIn("ZTB-9999", text)
        self.assertIn("I can only list tickets a Flow search returned", text)
