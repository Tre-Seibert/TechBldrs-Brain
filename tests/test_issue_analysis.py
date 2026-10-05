from __future__ import annotations

import json
import unittest
from unittest import mock

import httpx

from app.agent.loop import run_tool_loop
from app.config import Settings
from app.flow.stub import StubFlowSource
from app.tools.handlers import ChatTurn, TicketStatsArgs, _period_from_turn, ticket_stats


class IssueAnalysisToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()

    def _run(self, text: str = "What is the most common issue for ACME?", **args):
        return ticket_stats(
            self.source, TicketStatsArgs.model_validate({"entity": "tickets", "group_by": "issue", **args}), ChatTurn(user_text=text)
        )

    def test_issue_is_grouped_by_title_pattern_not_by_the_cause_field(self) -> None:
        result = self._run(client_code="ACME")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["group_by"], "pattern")
        self.assertIsNone(result.reply)  # the model writes the analysis from the digest
        self.assertIn("Ticket issue analysis for ACME", result.digest)
        self.assertIn("not on reading each ticket's emails or notes", result.digest)
        self.assertIn("Basis: ticket titles", result.footer)

    def test_the_digest_gives_the_model_sample_titles_and_a_usual_cause(self) -> None:
        result = self._run(client_code="ACME")
        self.assertRegex(result.digest, r"\[\d+ tickets, [\d.]+ h, usual cause: [^\]]+\] .+ -- e\.g\. \"")

    def test_automated_alert_tickets_are_left_out_unless_the_question_is_about_alerts(self) -> None:
        with mock.patch.object(self.source, "ticket_stats", wraps=self.source.ticket_stats) as spy:
            self._run("most common issue for ACME")
            self._run("what are the most common alerts for ACME")
        self.assertTrue(spy.call_args_list[0].kwargs["exclude_alerts"])
        self.assertFalse(spy.call_args_list[1].kwargs["exclude_alerts"])

    def test_other_groupings_still_return_the_plain_reply(self) -> None:
        result = ticket_stats(self.source, TicketStatsArgs(entity="tickets", group_by="cause", client_code="ACME"))
        self.assertIsNotNone(result.reply)
        self.assertIsNone(result.digest)

    def test_issue_synonyms_map_to_the_same_grouping(self) -> None:
        for word in ("issue", "issues", "problem", "problems", "topic_pattern"):
            self.assertEqual(TicketStatsArgs.model_validate({"group_by": word}).group_by, "pattern")


class YearPeriodTests(unittest.TestCase):
    def test_in_2025_means_that_calendar_year(self) -> None:
        period = _period_from_turn(ChatTurn(user_text="What was the most common issue in 2025 in archived tickets?"))
        self.assertEqual(period, ("2025-01-01", "2026-01-01", "2025"))

    def test_other_phrases_still_win_and_plain_text_has_no_period(self) -> None:
        self.assertIsNone(_period_from_turn(ChatTurn(user_text="tickets for client 2025 inc")))
        self.assertIsNotNone(_period_from_turn(ChatTurn(user_text="what happened last week in 2025")))


def _tool_call(name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}],
    }


class IssueAnalysisLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_answer_is_prose_then_the_exact_table(self) -> None:
        replies = iter(
            [
                _tool_call("ticket_stats", {"entity": "tickets", "group_by": "issue", "client_code": "ACME"}),
                {"role": "assistant", "content": "Clients mostly reported VPN and printing problems."},
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "x", "choices": [{"index": 0, "message": next(replies)}]})

        real = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with mock.patch("app.agent.loop.httpx.AsyncClient", lambda **kw: real(transport=transport, **kw)):
            payload = await run_tool_loop(
                settings=Settings(llm_model="fake", llm_base_url="http://llm.test/v1", _env_file=None),
                source=StubFlowSource(),
                messages=[{"role": "user", "content": "What is the most common issue for ACME?"}],
                model=None,
                actor="test",
            )
        text = payload["choices"][0]["message"]["content"]
        self.assertTrue(text.startswith("Clients mostly reported VPN and printing problems."))
        self.assertIn("Top title patterns:", text)
        self.assertIn("Basis: ticket titles", text)


if __name__ == "__main__":
    unittest.main()
