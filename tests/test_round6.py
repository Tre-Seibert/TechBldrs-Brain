from __future__ import annotations

import json
import unittest
from unittest import mock

import httpx

from app.agent.loop import run_tool_loop
from app.config import Settings
from app.flow.stub import StubFlowSource
from app.tools.handlers import (
    ChatTurn,
    ListTicketsArgs,
    _clean_person_name,
    answer_person_ticket_question,
    list_tickets,
)


def _tool_call(name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}],
    }


def _say(text: str) -> dict:
    return {"role": "assistant", "content": text}


class NameCleaningTests(unittest.TestCase):
    def test_a_stray_asterisk_is_not_part_of_the_name(self) -> None:
        self.assertEqual(_clean_person_name("Thomas Carter *"), "Thomas Carter")
        self.assertEqual(_clean_person_name('"Thomas Carter"?'), "Thomas Carter")

    def test_the_person_router_handles_the_asterisk(self) -> None:
        source = StubFlowSource()
        turn = ChatTurn(user_text="the latest ticket involving Michael Sodl *")
        self.assertIsNotNone(answer_person_ticket_question(source, turn))


class StageOverrideTests(unittest.TestCase):
    def test_a_search_the_user_did_not_call_open_covers_review_too(self) -> None:
        source = StubFlowSource()
        said = ChatTurn(user_text="tickets about monthly at ACME")
        result = list_tickets(source, ListTicketsArgs(q="Monthly", client_code="ACME", stage="open"), said)
        self.assertIn("open and in-review", result.reply.splitlines()[0])

    def test_when_they_say_open_it_stays_open(self) -> None:
        source = StubFlowSource()
        said = ChatTurn(user_text="open tickets about monthly at ACME")
        result = list_tickets(source, ListTicketsArgs(q="Monthly", client_code="ACME", stage="open"), said)
        self.assertNotIn("in-review", result.reply.splitlines()[0])


class LoopRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, question: str, script: list[dict], bodies: list[dict]) -> dict:
        replies = iter(script)

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "x", "choices": [{"index": 0, "message": next(replies)}]})

        real = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with mock.patch("app.agent.loop.httpx.AsyncClient", lambda **kw: real(transport=transport, **kw)):
            return await run_tool_loop(
                settings=Settings(llm_model="fake", llm_base_url="http://llm.test/v1", _env_file=None),
                source=StubFlowSource(),
                messages=[{"role": "user", "content": question}],
                model=None,
                actor="test",
            )

    async def test_a_model_that_never_calls_a_tool_is_made_to_with_constrained_json(self) -> None:
        bodies: list[dict] = []
        payload = await self._run(
            "What's the status of the ACME ticket?",
            [
                _say("ACME-0041 is fine, I think."),  # made up, no tool
                _say("It is ACME-0041, open."),  # still no tool after the nudge
                _say(json.dumps({"tool": "list_tickets", "arguments": {"client_code": "ACME", "stage": "open"}})),
                _say("ignored: the list is relayed"),
            ],
            bodies,
        )
        self.assertEqual([c["name"] for c in payload["x_tb_brain"]["tool_calls"]], ["list_tickets"])
        self.assertIn("ACME-0041", payload["choices"][0]["message"]["content"])
        forced = bodies[2]
        self.assertIn("response_format", forced)
        offered = forced["response_format"]["json_schema"]["schema"]["properties"]["tool"]["enum"]
        self.assertIn("list_tickets", offered)
        self.assertNotIn("merge_tickets", offered)  # the write tool is never forced

    async def test_if_structured_output_fails_the_safe_message_is_shown(self) -> None:
        payload = await self._run(
            "What's the status of the ACME ticket?",
            [_say("It is ACME-0041."), _say("Still ACME-0041."), _say("not json at all")],
            [],
        )
        self.assertIn("I can only list tickets a Flow search returned", payload["choices"][0]["message"]["content"])

    async def test_an_empty_summary_is_retried_then_replaced_by_the_plain_list(self) -> None:
        payload = await self._run(
            "What did I work on recently?",
            [_tool_call("list_time_entries", {"assignee_code": "ts"}), _say(""), _say("")],
            [],
        )
        text = payload["choices"][0]["message"]["content"]
        self.assertIn("VPN tunnel troubleshooting", text)  # the plain entry list, not nothing
        self.assertIn("Total: 20m worked", text)  # exact totals still appended

    async def test_a_non_english_summary_is_retried_and_the_second_try_is_used(self) -> None:
        payload = await self._run(
            "What did I work on recently?",
            [
                _tool_call("list_time_entries", {"assignee_code": "ts"}),
                _say("你在 ACME-0041 上排查了 VPN 隧道。"),
                _say("You troubleshot a VPN tunnel on ACME-0041."),
            ],
            [],
        )
        text = payload["choices"][0]["message"]["content"]
        self.assertTrue(text.startswith("You troubleshot a VPN tunnel on ACME-0041."))
        self.assertIn("Total: 20m worked", text)


if __name__ == "__main__":
    unittest.main()
