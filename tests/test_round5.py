from __future__ import annotations

import json
import unittest
from datetime import datetime
from unittest import mock

import httpx

from app.agent.loop import run_tool_loop
from app.config import Settings
from app.flow.schemas import TechnicianRecord
from app.flow.stub import StubFlowSource
from app.tools.handlers import (
    ChatTurn,
    GetTicketDetailArgs,
    ListTicketsArgs,
    ListTimeEntriesArgs,
    _period_from_turn,
    _resolve_assignee,
    get_ticket_detail,
    list_tickets,
    list_time_entries,
)

MONDAY = datetime(2026, 10, 5, 9, 28)  # the day the model said "last week" was Oct 1 to Oct 7


class PeriodTests(unittest.TestCase):
    def test_periods_are_computed_in_code(self) -> None:
        def period(text: str):
            return _period_from_turn(ChatTurn(user_text=text), MONDAY)

        self.assertEqual(period("What did I work on last week?"), ("2026-09-28", "2026-10-05", "Sep 28 to Oct 4"))
        self.assertEqual(period("hours this week"), ("2026-10-05", "2026-10-12", "Oct 5 to Oct 11"))
        self.assertEqual(period("what did I do yesterday"), ("2026-10-04", "2026-10-05", "Oct 4"))
        self.assertEqual(period("mail today"), ("2026-10-05", "2026-10-06", "Oct 5"))
        self.assertEqual(period("hours this month"), ("2026-10-01", "2026-11-01", "Oct 1 to Oct 31"))
        self.assertEqual(period("hours last month"), ("2026-09-01", "2026-10-01", "Sep 1 to Sep 30"))
        self.assertIsNone(period("what tickets are open?"))

    def test_the_period_overrides_the_dates_the_model_guessed(self) -> None:
        source = StubFlowSource()
        turn = ChatTurn(user_text="What did I work on last week?")
        expected = _period_from_turn(turn)
        with mock.patch.object(source, "list_time_entries", wraps=source.list_time_entries) as spy:
            list_time_entries(
                source, ListTimeEntriesArgs(assignee_code="ts", work_after="2026-10-01", work_before="2026-10-08"), turn
            )
        self.assertEqual((spy.call_args.kwargs["work_after"], spy.call_args.kwargs["work_before"]), expected[:2])


class _EddieSource(StubFlowSource):
    def __init__(self, people: list[tuple[str, str]]) -> None:
        super().__init__()
        self._people = people

    def search_technician(self, *, query: str, limit: int = 25) -> list[TechnicianRecord]:
        everyone = [
            TechnicianRecord(id=i + 1, display_name=name, email=f"{code}@x.example", assignee_code=code, is_active=True)
            for i, (name, code) in enumerate(self._people)
        ]
        needle = query.strip().lower()
        if not needle:
            return everyone
        return [t for t in everyone if needle == (t.assignee_code or "") or (len(needle) >= 3 and needle in t.display_name.lower())]


class AssigneeCodeTests(unittest.TestCase):
    def test_a_made_up_code_that_starts_a_first_name_resolves_to_that_person(self) -> None:
        source = _EddieSource([("Eddie Ortiz", "eo"), ("Tre Seibert", "ts")])
        self.assertEqual(_resolve_assignee(source, "ed"), ("eo", None))

    def test_a_real_code_is_left_alone(self) -> None:
        source = _EddieSource([("Eddie Ortiz", "eo"), ("Tre Seibert", "ts")])
        self.assertEqual(_resolve_assignee(source, "ts"), ("ts", None))

    def test_two_people_starting_the_same_way_asks(self) -> None:
        source = _EddieSource([("Eddie Ortiz", "eo"), ("Edward Lee", "el")])
        code, error = _resolve_assignee(source, "ed")
        self.assertIsNone(code)
        self.assertIn("Eddie Ortiz (eo) or Edward Lee (el)", error)

    def test_an_unknown_code_that_starts_no_name_still_goes_to_flow(self) -> None:
        source = _EddieSource([("Eddie Ortiz", "eo")])
        self.assertEqual(_resolve_assignee(source, "zz"), ("zz", None))


class ArchiveRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()

    def test_a_topic_search_never_includes_archived_tickets_unasked(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(q="printer"))
        self.assertTrue(result.ok, result.error)
        self.assertNotIn("archived", " ".join(row.get("stage", "") for row in result.data))

    def test_nothing_live_but_archives_have_it_asks_before_showing(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(q="Badge", client_code="ACME"))
        self.assertEqual(result.data, [])
        self.assertIn("I found 1 archived ticket(s) that match. Want me to show them?", result.reply)

    def test_nothing_anywhere_says_the_archives_were_checked(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(q="zzzzqqq"))
        self.assertIn("checked the archives and found nothing", result.reply)

    def test_asking_for_archived_just_shows_them(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(q="Badge", client_code="ACME", stage="archived"))
        self.assertIn("ACME-0700", result.reply)
        self.assertNotIn("Want me to show them", result.reply)

    def test_a_persons_tickets_include_review_not_just_open(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(requestor="Riley Chen"))
        labels = [row["ticket_label"] for row in result.data]
        self.assertIn("ACME-0041", labels)  # open
        self.assertIn("ACME-0050", labels)  # in review

    def test_filters_like_needs_a_reply_do_not_offer_the_archives(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(assignee_code="eo", needs_response=True, stage="open"))
        self.assertNotIn("archive", result.reply)


class BriefingViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()

    def test_the_model_cannot_pick_the_raw_log_unless_the_user_asked_for_it(self) -> None:
        args = GetTicketDetailArgs(ticket_label="ACME-0041", view="full")
        quiet = get_ticket_detail(self.source, args, ChatTurn(user_text="Show me ACME-0041"))
        self.assertIsNone(quiet.reply)
        self.assertIsNotNone(quiet.digest)
        loud = get_ticket_detail(self.source, args, ChatTurn(user_text="show me the full log for ACME-0041"))
        self.assertIsNotNone(loud.reply)


def _tool_call(name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}],
    }


class LoopSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, script: list[dict], bodies: list[dict], **settings: object) -> dict:
        replies = iter(script)

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "x", "choices": [{"index": 0, "message": next(replies)}]})

        real = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with mock.patch("app.agent.loop.httpx.AsyncClient", lambda **kw: real(transport=transport, **kw)):
            return await run_tool_loop(
                settings=Settings(llm_model="fake", llm_base_url="http://llm.test/v1", _env_file=None, **settings),
                source=StubFlowSource(),
                messages=[{"role": "user", "content": "Show me ACME-0041"}],
                model=None,
                actor="test",
            )

    async def test_after_a_digest_the_model_is_not_offered_tools(self) -> None:
        bodies: list[dict] = []
        await self._run(
            [
                _tool_call("get_ticket_detail", {"ticket_label": "ACME-0041"}),
                {"role": "assistant", "content": "A VPN tunnel kept dropping."},
            ],
            bodies,
        )
        self.assertIn("tools", bodies[0])
        self.assertNotIn("tools", bodies[1])

    async def test_a_model_that_loops_gets_an_answer_not_a_502(self) -> None:
        bodies: list[dict] = []
        looping = [_tool_call("get_ticket_detail", {"ticket_label": "ACME-0041"}) for _ in range(6)]
        payload = await self._run(looping, bodies, agent_max_tool_iters=3)
        text = payload["choices"][0]["message"]["content"]
        self.assertIn("ACME-0041", text)  # at least the exact field block
        self.assertTrue(payload["x_tb_brain"]["stuck"])

    async def test_a_loop_with_no_fields_says_what_to_try(self) -> None:
        looping = [_tool_call("get_mail_detail", {"mail_id": 999}) for _ in range(6)]
        payload = await self._run(looping, [], agent_max_tool_iters=2)
        self.assertIn("kept searching", payload["choices"][0]["message"]["content"])


if __name__ == "__main__":
    unittest.main()


class LatestEntryTests(unittest.TestCase):
    """'What was my last time entry?' is one entry, the newest submitted, not a date range of them."""

    def setUp(self) -> None:
        self.source = StubFlowSource()

    def _ask(self, text: str, **args):
        return list_time_entries(
            self.source,
            ListTimeEntriesArgs(assignee_code="ts", **args),
            ChatTurn(user_text=text),
        )

    def test_the_latest_entry_is_exactly_one_even_if_the_model_added_dates(self) -> None:
        result = self._ask("what was my last time entry?", work_after="2026-09-01", work_before="2026-12-01")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.data), 1)
        self.assertEqual(result.data[0]["id"], 70002)  # submitted Sep 19, after the Sep 18 one
        self.assertTrue(result.reply.startswith("Latest time entry for ts"))

    def test_the_entry_shows_its_notes_and_when_it_was_submitted(self) -> None:
        result = self._ask("what was my latest time entry")
        self.assertIn("Notes: Checked firewall logs", result.reply)
        self.assertIn("Submitted Sep 19", result.reply)
        self.assertIn("ACME-0041", result.reply)

    def test_last_three_entries_means_three(self) -> None:
        result = self._ask("show my last 3 time entries")
        self.assertEqual(len(result.data), 1)  # the stub only has one entry for ts
        self.assertTrue(result.reply.startswith("Latest time entry"))

    def test_asking_to_summarize_the_last_entry_summarizes_only_that_entry(self) -> None:
        result = self._ask("summarize the last time entry I submitted")
        self.assertIsNone(result.reply)
        self.assertEqual(len(result.data), 1)
        self.assertIn("ACME-0041", result.digest)
        self.assertNotIn("WDON-1842", result.digest)

    def test_a_period_in_the_question_still_wins(self) -> None:
        result = self._ask("what was my last time entry last week")
        self.assertNotIn("Latest time entry", result.reply or "")

    def test_ordinary_time_questions_are_not_mistaken_for_it(self) -> None:
        from app.tools.handlers import _latest_entry_count

        for text in ("what did I work on last week?", "my time entries", "how many entries did I log"):
            self.assertIsNone(_latest_entry_count(ChatTurn(user_text=text)), text)
