from __future__ import annotations

import json
import unittest
from unittest import mock

import httpx

from app.agent.loop import run_tool_loop, strip_unbacked_figures
from app.config import Settings
from app.flow.stub import StubFlowSource
from app.tools.handlers import (
    ChatTurn,
    GetTicketDetailArgs,
    LatestTicketArgs,
    ListTicketsArgs,
    ListTimeEntriesArgs,
    _latest_stage,
    get_ticket_detail,
    latest_ticket,
    list_tickets,
    list_time_entries,
)


class LatestTicketStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()

    def test_archived_in_the_question_returns_an_archived_ticket(self) -> None:
        result = latest_ticket(
            self.source,
            LatestTicketArgs(client_code="ACME"),
            ChatTurn(user_text="What is the most recent archived ACME ticket?"),
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["stage"], "archived")
        self.assertIn("Latest archived ticket for ACME", result.reply)

    def test_without_archived_the_latest_ticket_is_never_an_archived_one(self) -> None:
        result = latest_ticket(self.source, LatestTicketArgs(client_code="ACME"), ChatTurn(user_text="last ACME ticket"))
        self.assertNotEqual(result.data["stage"], "archived")
        self.assertIn("Latest ticket for ACME", result.reply)

    def test_the_users_word_beats_a_stage_the_model_forgot(self) -> None:
        # The model dropped "archived" from its arguments; the question still says it.
        result = latest_ticket(
            self.source,
            LatestTicketArgs(client_code="ACME", stage="live"),
            ChatTurn(user_text="most recent archived ACME ticket"),
        )
        self.assertEqual(result.data["stage"], "archived")

    def test_the_archive_is_searched_only_when_nothing_live_matches(self) -> None:
        real = self.source.list_tickets
        stages: list[str | None] = []

        def only_archived(**kwargs):
            stages.append(kwargs.get("stage"))
            return [] if kwargs.get("stage") != "archived" else real(**kwargs)

        with mock.patch.object(self.source, "list_tickets", side_effect=only_archived):
            result = latest_ticket(self.source, LatestTicketArgs(client_code="ACME"), ChatTurn(user_text="last ACME ticket"))
        self.assertEqual(stages, ["live", "archived"])  # live first, archive second
        self.assertEqual(result.data["stage"], "archived")
        self.assertIn("Nothing open or in review matched for ACME", result.reply)

    def test_a_live_match_never_touches_the_archive(self) -> None:
        real = self.source.list_tickets
        stages: list[str | None] = []

        def spy(**kwargs):
            stages.append(kwargs.get("stage"))
            return real(**kwargs)

        with mock.patch.object(self.source, "list_tickets", side_effect=spy):
            latest_ticket(self.source, LatestTicketArgs(client_code="ACME"), ChatTurn(user_text="last ACME ticket"))
        self.assertEqual(stages, ["live"])


class ModelStageHabitTests(unittest.TestCase):
    def test_a_model_filled_open_does_not_hide_in_review_tickets(self) -> None:
        # Seen live: the model sent stage=open for "the last BUCK ticket", which skipped the newest
        # ticket because it was in 9 REVIEW.
        stage = _latest_stage(LatestTicketArgs(client_code="BUCK", stage="open"), ChatTurn(user_text="What was the last BUCK ticket?"))
        self.assertEqual(stage, "live")

    def test_only_the_users_words_or_an_explicit_archived_change_the_stage(self) -> None:
        self.assertEqual(_latest_stage(LatestTicketArgs(stage="archived"), ChatTurn(user_text="last BUCK ticket")), "archived")
        self.assertEqual(_latest_stage(LatestTicketArgs(), ChatTurn(user_text="last open BUCK ticket")), "open")
        self.assertEqual(_latest_stage(LatestTicketArgs(), ChatTurn(user_text="last BUCK ticket in review")), "review")


class GuessedLabelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()

    def _one_open_ticket(self):
        real = self.source.list_tickets
        only = real(client_code="ACME", ticket_num="0041", stage="live")[0]

        def fake(**kwargs):
            if kwargs.get("ticket_num"):
                return []  # the guessed label does not exist
            if kwargs.get("stage") == "open":
                return [only]
            return real(**kwargs)

        return mock.patch.object(self.source, "list_tickets", side_effect=fake), only

    def test_a_label_the_user_never_typed_falls_back_to_the_clients_one_open_ticket(self) -> None:
        patch, only = self._one_open_ticket()
        with patch:
            result = list_time_entries(
                self.source,
                ListTimeEntriesArgs(ticket_label="ACME-9999", view="list"),
                ChatTurn(user_text="Summarize the time entries logged on the ACME ticket"),
            )
        self.assertTrue(result.ok, result.error)

    def test_several_open_tickets_ask_which_instead_of_guessing(self) -> None:
        result = list_time_entries(
            self.source,
            ListTimeEntriesArgs(ticket_label="ACME-9999", view="list"),
            ChatTurn(user_text="Summarize the time entries logged on the ACME ticket"),
        )
        self.assertFalse(result.ok)
        self.assertIn("Which one do you mean", result.error)

    def test_a_label_the_user_did_type_is_never_swapped_for_another_ticket(self) -> None:
        patch, _ = self._one_open_ticket()
        with patch:
            result = get_ticket_detail(
                self.source, GetTicketDetailArgs(ticket_label="ACME-9999"), ChatTurn(user_text="Show me ACME-9999")
            )
        self.assertFalse(result.ok)
        self.assertIn("No single ticket found", result.error)


class ArchivedLabelLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()

    def test_a_label_that_is_only_in_the_archive_is_found_and_marked(self) -> None:
        result = get_ticket_detail(self.source, GetTicketDetailArgs(ticket_label="ACME-0010"))
        self.assertTrue(result.ok, result.error)
        self.assertIn("ACME-0010 is an archived ticket", result.reply)
        self.assertEqual(result.data["stage"], "archived")

    def test_a_live_label_is_still_answered_from_live(self) -> None:
        real = self.source.list_tickets
        stages: list[str | None] = []

        def spy(**kwargs):
            stages.append(kwargs.get("stage"))
            return real(**kwargs)

        with mock.patch.object(self.source, "list_tickets", side_effect=spy):
            result = get_ticket_detail(self.source, GetTicketDetailArgs(ticket_label="ACME-0041"))
        self.assertTrue(result.ok, result.error)
        self.assertNotIn("archived", stages)

    def test_a_label_in_neither_place_still_fails(self) -> None:
        result = get_ticket_detail(self.source, GetTicketDetailArgs(ticket_label="ZTB-1681"))
        self.assertFalse(result.ok)


class ListByTicketNumberTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()

    def test_a_number_that_is_only_archived_is_found_after_live_comes_up_empty(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(client_code="ACME", ticket_num="0010"))
        self.assertEqual([row["ticket_label"] for row in result.data], ["ACME-0010"])
        self.assertIn("ACME-0010 is an archived ticket", result.reply)

    def test_a_number_that_exists_nowhere_names_the_ticket_not_the_client(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(client_code="ACME", ticket_num="9999"))
        self.assertEqual(result.reply, "No ticket found for ACME-9999.")

    def test_a_live_number_never_looks_in_the_archive(self) -> None:
        real = self.source.list_tickets
        stages: list[str | None] = []

        def spy(**kwargs):
            stages.append(kwargs.get("stage"))
            return real(**kwargs)

        with mock.patch.object(self.source, "list_tickets", side_effect=spy):
            result = list_tickets(self.source, ListTicketsArgs(client_code="ACME", ticket_num="0041"))
        self.assertNotIn("archived", stages)
        self.assertEqual([row["ticket_label"] for row in result.data], ["ACME-0041"])


class StripUnbackedFiguresTests(unittest.TestCase):
    DIGEST = "[ZINT-5426] Contact sync -- notes: x\n[ZTB-0006] Meetings -- notes: y"

    def test_tickets_on_consecutive_lines_become_separate_paragraphs(self) -> None:
        text = strip_unbacked_figures("ZINT-5426 Did the sync.\nZTB-0006 Held meetings.", self.DIGEST)
        self.assertEqual(text, "ZINT-5426 Did the sync.\n\nZTB-0006 Held meetings.")

    def test_durations_intro_and_closing_lines_are_removed(self) -> None:
        prose = (
            "Last week from September 29 to October 5 you worked on client WDON.\n\n"
            "ZINT-5426: Finished the contact sync dry run. (Total: 7 hours 3 minutes)\n\n"
            "ZTB-0006: Attended internal meetings. This took 2 hours 18 minutes.\n\n"
            "These tasks were part of ongoing projects."
        )
        text = strip_unbacked_figures(prose, self.DIGEST)
        self.assertNotIn("WDON", text)
        self.assertNotIn("hours", text)
        self.assertNotIn("ongoing projects", text)
        self.assertIn("ZINT-5426: Finished the contact sync dry run.", text)
        self.assertIn("ZTB-0006: Attended internal meetings.", text)

    def test_prose_with_nothing_usable_comes_back_empty(self) -> None:
        self.assertEqual(strip_unbacked_figures("You worked a lot, about 5 hours.", self.DIGEST), "")


def _tool_call(name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}],
    }


class BlankReplyLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_model_that_says_nothing_after_a_tool_error_shows_the_error(self) -> None:
        script = iter(
            [
                _tool_call("get_ticket_detail", {"ticket_label": "ZTB-1681"}),
                {"role": "assistant", "content": ""},
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
                messages=[{"role": "user", "content": "Show me ZTB-1681"}],
                model=None,
                actor="test",
            )
        text = payload["choices"][0]["message"]["content"]
        self.assertIn("No single ticket found", text)


class TimeSummaryFiguresLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_figures_the_model_makes_up_never_reach_the_answer(self) -> None:
        script = iter(
            [
                _tool_call("list_time_entries", {"assignee_code": "ts"}),
                {
                    "role": "assistant",
                    "content": (
                        "Last week you worked on client WDON.\n\n"
                        "ACME-0041: Fixed a hardware fault on the server. (Total: 9 hours 14 minutes)"
                    ),
                },
            ]
        )
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "x", "choices": [{"index": 0, "message": next(script)}]})

        real = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with mock.patch("app.agent.loop.httpx.AsyncClient", lambda **kw: real(transport=transport, **kw)):
            payload = await run_tool_loop(
                settings=Settings(llm_model="fake", llm_base_url="http://llm.test/v1", _env_file=None),
                source=StubFlowSource(),
                messages=[{"role": "user", "content": "What did I work on recently?"}],
                model=None,
                actor="test",
            )
        text = payload["choices"][0]["message"]["content"]
        self.assertNotIn("WDON", text)
        self.assertNotIn("9 hours", text)
        self.assertIn("ACME-0041: Fixed a hardware fault on the server.", text)
        self.assertIn("Total: 20m worked across 1 entry on 1 ticket", text)  # the exact footer
        digest = json.loads(seen[1]["messages"][-1]["content"])["digest"]
        self.assertNotIn("20m", digest)  # the model was never handed a duration to re-add


if __name__ == "__main__":
    unittest.main()
