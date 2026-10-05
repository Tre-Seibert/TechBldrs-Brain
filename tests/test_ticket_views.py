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
    GetTicketDetailArgs,
    LatestTicketArgs,
    ListTicketsArgs,
    _search_terms,
    _stem,
    get_ticket_detail,
    latest_ticket,
    list_tickets,
)


class SearchTermTests(unittest.TestCase):
    def test_stems_let_imaging_find_image_and_reimage(self) -> None:
        self.assertEqual(_stem("imaging"), "imag")
        self.assertEqual(_stem("images"), "imag")
        self.assertEqual(_stem("printing"), "print")
        self.assertEqual(_stem("ZTB-1680"), "ztb-1680")  # labels are never stemmed
        self.assertIn("imag", "laptop reimage and imaging")

    def test_generic_words_are_dropped_and_each_remaining_word_is_searched(self) -> None:
        self.assertEqual(_search_terms("printer issues"), ["printer"])
        self.assertEqual(_search_terms("Platform Infrastructure"), ["platform", "infrastructure"])
        self.assertEqual(_search_terms("issues"), [])  # nothing specific left: the caller searches the phrase

    def test_a_topic_search_finds_inflected_words(self) -> None:
        source = StubFlowSource()
        result = list_tickets(source, ListTicketsArgs(q="imaging"))
        self.assertTrue(result.ok, result.error)
        self.assertIn("WDON-1842", [row["ticket_label"] for row in result.data])


class HonestListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()

    def test_two_plain_words_that_are_not_a_contact_are_searched_as_text(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(q="printer issues", client_code="ACME"))
        self.assertTrue(result.ok, result.error)
        self.assertIn("printer issues", result.reply)  # a search heading, not "tickets for requestor ..."
        self.assertNotIn("requestor", (result.error or "").lower())

    def test_the_word_open_means_open_only(self) -> None:
        turn = ChatTurn(user_text="what ACME tickets are open?")
        result = list_tickets(self.source, ListTicketsArgs(client_code="ACME"), turn)
        labels = [row["ticket_label"] for row in result.data]
        self.assertNotIn("ACME-0050", labels)  # in review
        self.assertIn("open", result.reply.splitlines()[0])

    def test_a_mixed_list_says_so_and_tags_each_row(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(client_code="ACME", stage="live"))
        head = result.reply.splitlines()[0]
        self.assertIn("open and in-review", head)
        self.assertRegex(head, r"\d+ open, \d+ in review")
        self.assertIn("· in review", result.reply)
        self.assertNotIn(" live ", head)

    def test_a_topic_search_covers_archived_and_labels_it(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(q="Monthly", client_code="ACME"))
        self.assertTrue(result.ok, result.error)
        self.assertNotIn("live", result.reply.splitlines()[0])

    def test_the_latest_ticket_is_the_newest_opened_and_shows_when(self) -> None:
        result = latest_ticket(self.source, LatestTicketArgs(client_code="ACME"))
        self.assertTrue(result.ok, result.error)
        self.assertIn("ACME-0400", result.reply)  # created Sep 23, newer than the rest
        self.assertIn("created Sep 23", result.reply)
        self.assertIn("last activity", result.reply)


class SummaryModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()

    def test_summarize_hands_the_model_notes_and_log_not_a_list(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(client_code="ACME", stage="review", summarize=True))
        self.assertTrue(result.ok, result.error)
        self.assertIsNone(result.reply)
        self.assertIn("ACME-0050", result.digest)
        self.assertIn("Write a short plain-English summary", result.digest)
        self.assertIn("ACME-0050", result.footer)

    def test_summarize_with_no_tickets_is_a_plain_reply(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(client_code="ZZZZ", stage="open", summarize=True))
        self.assertIsNone(result.digest)
        self.assertIn("No open tickets", result.reply)

    def test_a_ticket_briefing_has_exact_fields_first_and_a_digest_to_summarize(self) -> None:
        result = get_ticket_detail(self.source, GetTicketDetailArgs(ticket_label="ACME-0041"))
        self.assertTrue(result.ok, result.error)
        self.assertIsNone(result.reply)
        self.assertIn("ACME-0041", result.header)
        self.assertIn("Assigned to ts", result.header)
        self.assertIn("created Sep 10", result.header)
        self.assertIn("mail inbound from Riley Chen", result.digest)
        self.assertIn("time entry", result.digest)
        self.assertIn("Most recent activity", result.digest)

    def test_view_full_still_returns_the_raw_log(self) -> None:
        result = get_ticket_detail(self.source, GetTicketDetailArgs(ticket_label="ACME-0041", view="full"))
        self.assertIsNotNone(result.reply)
        self.assertIsNone(result.digest)
        self.assertIn("ACME-0041", result.reply)


def _tool_call(name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}],
    }


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, question: str, script: list[dict], **settings: object) -> dict:
        replies = iter(script)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "x", "choices": [{"index": 0, "message": next(replies)}]})

        real = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with mock.patch("app.agent.loop.httpx.AsyncClient", lambda **kw: real(transport=transport, **kw)):
            return await run_tool_loop(
                settings=Settings(llm_model="fake", llm_base_url="http://llm.test/v1", _env_file=None, **settings),
                source=StubFlowSource(),
                messages=[{"role": "user", "content": question}],
                model=None,
                actor="test",
            )

    async def test_a_ticket_briefing_is_fields_then_prose(self) -> None:
        payload = await self._run(
            "Show me ACME-0041",
            [
                _tool_call("get_ticket_detail", {"ticket_label": "ACME-0041"}),
                {"role": "assistant", "content": "A VPN tunnel kept dropping. The latest update is the firewall log check."},
            ],
        )
        text = payload["choices"][0]["message"]["content"]
        self.assertTrue(text.startswith("ACME-0041"))  # fields first
        self.assertIn("Assigned to ts", text.split("\n\n")[0])
        self.assertTrue(text.rstrip().endswith("firewall log check."))  # then the prose

    async def test_a_summary_of_tickets_is_prose_then_the_label_list(self) -> None:
        payload = await self._run(
            "Can you summarize review tickets for ACME?",
            [
                _tool_call("list_tickets", {"client_code": "ACME", "stage": "review", "summarize": True}),
                {"role": "assistant", "content": "ACME-0050 is a finished invoice question, waiting on review."},
            ],
        )
        text = payload["choices"][0]["message"]["content"]
        self.assertTrue(text.startswith("ACME-0050 is a finished invoice question"))
        self.assertIn("1 ticket(s): ACME-0050", text)

    async def test_a_router_is_the_safety_net_when_the_model_never_calls_a_tool(self) -> None:
        payload = await self._run(
            "Tickets for Michael Sodl?",
            [
                {"role": "assistant", "content": "Sure, I can look that up for you."},
                {"role": "assistant", "content": "Looking now."},
            ],
            disabled_routers="person_ticket",
        )
        self.assertEqual(payload["x_tb_brain"]["router"], "fallback:person_ticket")
        self.assertIn("ACME-0400", payload["choices"][0]["message"]["content"])


if __name__ == "__main__":
    unittest.main()


class MultiClientTests(unittest.TestCase):
    def test_internal_open_means_open_and_says_how_many_were_cut_off(self) -> None:
        source = StubFlowSource()
        turn = ChatTurn(user_text="What ACME and WDON tickets are open?")
        result = list_tickets(source, ListTicketsArgs(client_code="ACME,WDON", limit=2), turn)
        self.assertTrue(result.ok, result.error)
        head = result.reply.splitlines()[0]
        self.assertTrue(head.startswith("2 open ticket(s) for ACME, WDON"), head)
        self.assertIn("Showing the 2 most recently active of", result.reply)
        self.assertNotIn("in review", result.reply)
