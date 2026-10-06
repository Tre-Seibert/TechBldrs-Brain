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
    get_ticket_detail,
    latest_ticket,
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


class StripUnbackedFiguresTests(unittest.TestCase):
    DIGEST = "[ZINT-5426] Contact sync -- notes: x\n[ZTB-0006] Meetings -- notes: y"

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
