from __future__ import annotations

import json
import unittest
from datetime import datetime
from unittest import mock

import httpx

from app.agent.loop import run_tool_loop
from app.config import Settings
from app.flow.stub import StubFlowSource
from app.tools.handlers import (
    ListTicketsArgs,
    ListTimeEntriesArgs,
    _format_minutes,
    format_ticket_list,
    list_tickets,
    list_time_entries,
)

NOW = datetime(2026, 10, 5, 9, 28)


class MinutesFormatTests(unittest.TestCase):
    def test_hours_and_minutes_never_a_bare_minute_count(self) -> None:
        self.assertEqual(_format_minutes(0), "0m")
        self.assertEqual(_format_minutes(45), "45m")
        self.assertEqual(_format_minutes(60), "1h")
        self.assertEqual(_format_minutes(125), "2h 05m")
        self.assertEqual(_format_minutes(1905), "31h 45m")


class TicketExtrasTests(unittest.TestCase):
    ROW = {
        "ticket_label": "ZINT-5466",
        "topic": "Flow Reporting",
        "status": "New",
        "category": "0 Urgent",
        "client_code": "ZINT",
        "assignee_code": "ts",
        "due_at": "2026-10-04 08:00:00",
        "last_activity_at": "2026-09-22 10:00:00",
    }

    def test_overdue_rows_show_the_due_date_and_how_late(self) -> None:
        text = format_ticket_list([self.ROW], heading="1 open overdue ticket(s)", extras=("due",), now=NOW)
        self.assertIn("due Oct 4, 8:00 AM (1d 1h overdue)", text)

    def test_a_future_due_date_says_how_far_off(self) -> None:
        row = {**self.ROW, "due_at": "2026-10-07 09:28:00"}
        text = format_ticket_list([row], heading="x", extras=("due",), now=NOW)
        self.assertIn("due Oct 7, 9:28 AM (due in 2d)", text)

    def test_a_missing_due_date_is_stated(self) -> None:
        text = format_ticket_list([{**self.ROW, "due_at": None}], heading="x", extras=("due",), now=NOW)
        self.assertIn("no due date", text)

    def test_inactive_rows_show_when_the_last_activity_was(self) -> None:
        text = format_ticket_list([self.ROW], heading="x", extras=("activity",), now=NOW)
        self.assertIn("last activity Sep 22 (13 days ago)", text)

    def test_rows_stay_short_without_extras(self) -> None:
        text = format_ticket_list([self.ROW], heading="x", now=NOW)
        self.assertNotIn("due", text)
        self.assertNotIn("last activity", text)

    def test_list_tickets_adds_the_right_extra_for_each_filter(self) -> None:
        source = StubFlowSource()
        overdue = list_tickets(source, ListTicketsArgs(overdue=True, stage="open"))
        self.assertTrue(all("due " in line or "no due date" in line for line in overdue.reply.splitlines() if line.startswith("- ")))
        quiet = list_tickets(source, ListTicketsArgs(last_activity_before="2026-12-31", stage="open", client_code="ACME"))
        self.assertIn("last activity", quiet.reply)

    def test_nothing_waiting_on_a_reply_says_so_instead_of_no_assigned_tickets(self) -> None:
        result = list_tickets(StubFlowSource(), ListTicketsArgs(assignee_code="eo", needs_response=True, stage="open"))
        self.assertEqual(result.reply, "No open tickets for eo are waiting on a reply.")


class TimeSummaryToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()

    def test_summary_is_the_default_and_hands_the_model_a_digest_not_a_reply(self) -> None:
        result = list_time_entries(self.source, ListTimeEntriesArgs(assignee_code="ts"))
        self.assertTrue(result.ok, result.error)
        self.assertIsNone(result.reply)
        self.assertIn("ACME-0041", result.digest)
        self.assertIn("Write a plain-English summary", result.digest)
        self.assertIn("Total: 20m worked across 1 entry on 1 ticket", result.footer)

    def test_the_footer_totals_are_hours_and_minutes_by_ticket(self) -> None:
        result = list_time_entries(self.source, ListTimeEntriesArgs(client_code="ACME"))
        self.assertIn("By ticket: ACME-0041 20m", result.footer)
        both = list_time_entries(self.source, ListTimeEntriesArgs(reviewed=False))
        self.assertIn("20m", both.footer)

    def test_the_list_view_is_verbatim_and_has_no_digest(self) -> None:
        result = list_time_entries(self.source, ListTimeEntriesArgs(assignee_code="ts", view="list"))
        self.assertIsNotNone(result.reply)
        self.assertIsNone(result.digest)
        self.assertIn("20m", result.reply)

    def test_nothing_found_is_a_plain_reply(self) -> None:
        result = list_time_entries(self.source, ListTimeEntriesArgs(client_code="ACME", work_after="2030-01-01"))
        self.assertIn("None found", result.reply)
        self.assertIsNone(result.digest)


def _tool_call(name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}],
    }


class TimeSummaryLoopTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, script: list[dict], seen: list[dict]) -> dict:
        replies = iter(script)

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "x", "choices": [{"index": 0, "message": next(replies)}]})

        real = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with mock.patch("app.agent.loop.httpx.AsyncClient", lambda **kw: real(transport=transport, **kw)):
            return await run_tool_loop(
                settings=Settings(llm_model="fake", llm_base_url="http://llm.test/v1", _env_file=None),
                source=StubFlowSource(),
                messages=[{"role": "user", "content": "What did I work on recently?"}],
                model=None,
                actor="test",
            )

    async def test_the_answer_is_the_models_prose_followed_by_exact_totals(self) -> None:
        seen: list[dict] = []
        payload = await self._run(
            [
                _tool_call("list_time_entries", {"assignee_code": "ts", "work_after": "2026-09-14"}),
                {"role": "assistant", "content": "You spent the week on a hardware fault on ACME-0041."},
            ],
            seen,
        )
        text = payload["choices"][0]["message"]["content"]
        self.assertTrue(text.startswith("You spent the week on a hardware fault on ACME-0041."))
        self.assertIn("Total: 20m worked across 1 entry on 1 ticket", text)
        tool_message = json.loads(seen[1]["messages"][-1]["content"])
        self.assertIn("digest", tool_message)  # the model was shown the entries, not a finished reply

    async def test_a_ticket_the_digest_never_mentioned_is_not_allowed(self) -> None:
        payload = await self._run(
            [
                _tool_call("list_time_entries", {"assignee_code": "ts"}),
                {"role": "assistant", "content": "You mostly worked on ZTB-9999."},  # invented
                {"role": "assistant", "content": "Mostly ZTB-9999, honestly."},  # still invented after the nudge
            ],
            [],
        )
        text = payload["choices"][0]["message"]["content"]
        self.assertNotIn("ZTB-9999", text)
        self.assertIn("Total: 20m worked", text)  # the exact numbers survive


if __name__ == "__main__":
    unittest.main()
