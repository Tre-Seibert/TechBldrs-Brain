"""Source-aware loop wording (A7): the nudge and fallback messages and the must-use-a-tool test come from the enabled
sources. With only Flow enabled they are exactly the Flow-only messages from before; with the fake routed sources
from test_routing (given wording here) they name what is enabled.
"""

from __future__ import annotations

import tests._isolation  # noqa: F401  (first: hermetic settings, no network)

import dataclasses
import json
import re
import unittest
from pathlib import Path

from app.agent import loop
from app.agent.loop import _expects_tool, no_tool_message
from app.agent.wording import expects_tool, or_list, wording_for
from app.sources.base import Sources, SourceWording
from app.sources.flow.spec import FLOW
from app.sources.flow.stub import StubFlowSource
from app.sources.registry import build_sources
from app.tools import ChatTurn
from tests.test_routing import CLOUD, RMM, _call, _run, _settings, _user

# The Flow-only messages as loop.py spelled them out before A7.
FLOW_NO_TOOL = (
    "I can only answer from Flow tools, and I did not get a usable result. "
    "Ask again with a person, technician, or client code."
)
FLOW_NUDGE = (
    "You answered without calling a tool, so that answer is unverified. For any question about tickets, "
    "time, mail, contacts, or clients you must call the right tool first (for example list_tickets for "
    "lists, ticket_stats for hours or counts) and answer only from its result. Call the tool now."
)
FLOW_STUCK = (
    "I kept searching without getting to an answer. Try asking again with a client code or ticket label, "
    "for example 'show me ZTB-1680'."
)
FLOW_NO_SEARCH = (
    "I didn't run a Flow search for that, so I won't guess. Try rephrasing, for example "
    "'my urgent tickets' or 'open tickets for BUCK'."
)
# loop.py's data-word test before A7, to check the decisions against.
OLD_DATA_WORD_RE = re.compile(
    r"\b(?:tickets?|urgent|overdue|assigned|hours?|time|logged|worked|e-?mails?|mail|reach(?:ed)? out|"
    r"contacts?|clients?|machines?|respond|queue|open|review|archived|billable|cause|problem|unassigned|work(?:ed|ing)?|spent)\b"
    r"|\b[A-Z][A-Z0-9]{1,8}-[A-Z0-9]{3,6}\b",
    re.IGNORECASE,
)
OLD_WRITE_INTENT_RE = re.compile(
    r"^\s*(?:please\s+|can you\s+)?(?:close|archive|delete|create|log|send|e-?mail|change|set|update|reassign|add|merge)\b",
    re.IGNORECASE,
)

RMM_WORDED = dataclasses.replace(
    RMM,
    wording=SourceWording(
        title="RMM",
        topics=("devices", "alerts", "patches"),
        tool_examples=("rmm_list_devices for devices",),
        ask_with=("device name",),
        retry_with=("device name",),
        examples=("offline devices at BUCK",),
        lookup_examples=("show me BUCK-DC01",),
    ),
)
CLOUD_WORDED = dataclasses.replace(
    CLOUD,
    wording=SourceWording(
        title="Microsoft 365",
        topics=("users", "MFA", "licenses", "sign-ins", "clients"),  # "clients" repeats Flow's: listed once
        tool_examples=("cloud_mfa_status for MFA",),
        ask_with=("user email", "client code"),
        retry_with=("user email",),
        examples=("who has no MFA at BUCK",),
        lookup_examples=(),
    ),
)

ALL_NO_TOOL = (
    "I can only answer from Flow, RMM, or Microsoft 365 tools, and I did not get a usable result. "
    "Ask again with a person, technician, client code, device name, or user email."
)
ALL_STUCK = (
    "I kept searching without getting to an answer. Try asking again with a client code, ticket label, device name, "
    "or user email, for example 'show me ZTB-1680' or 'show me BUCK-DC01'."
)
ALL_NO_SEARCH = (
    "I didn't run a Flow, RMM, or Microsoft 365 search for that, so I won't guess. Try rephrasing, for example "
    "'my urgent tickets', 'open tickets for BUCK', 'offline devices at BUCK', or 'who has no MFA at BUCK'."
)
FLOW_RMM_NUDGE = (
    "You answered without calling a tool, so that answer is unverified. For any question about tickets, time, mail, "
    "contacts, clients, devices, alerts, or patches you must call the right tool first (for example list_tickets for "
    "lists, ticket_stats for hours or counts, rmm_list_devices for devices) and answer only from its result. "
    "Call the tool now."
)


def _flow_only() -> Sources:
    return build_sources(_settings(), specs=(FLOW,))


def _worded() -> Sources:
    return build_sources(_settings(), specs=(FLOW, RMM_WORDED, CLOUD_WORDED))


def _eval_turns() -> list[ChatTurn]:
    """Every user turn in the eval files, with the assistant text before it where the case has one."""
    root = Path(__file__).resolve().parent.parent / "eval"
    turns = []
    for name in ("flow_cases.json", "questions.json"):
        for case in json.loads((root / name).read_text(encoding="utf-8"))["cases"]:
            turns.append(loop.chat_turn_from_messages(case["messages"]))
    for line in (root / "live_questions.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            turns.append(ChatTurn(user_text=line.strip()))
    return turns


class FlowOnlyTests(unittest.TestCase):
    def test_flow_only_messages_are_the_pre_a7_ones(self) -> None:
        words = wording_for(_flow_only())
        self.assertEqual(words.no_tool, FLOW_NO_TOOL)
        self.assertEqual(words.nudge_to_use_a_tool, FLOW_NUDGE)
        self.assertEqual(words.stuck, FLOW_STUCK)
        self.assertEqual(words.no_search_run, FLOW_NO_SEARCH)
        self.assertEqual(
            (loop._NO_TOOL_ENGLISH, loop._NUDGE_TO_USE_A_TOOL, loop._STUCK, loop._NO_SEARCH_RUN),
            (FLOW_NO_TOOL, FLOW_NUDGE, FLOW_STUCK, FLOW_NO_SEARCH),
        )
        self.assertEqual(no_tool_message(), FLOW_NO_TOOL)
        self.assertEqual(no_tool_message(StubFlowSource()), FLOW_NO_TOOL)  # a bare Flow client is Flow only

    def test_flow_only_decisions_match_the_old_data_word_test_on_every_eval_turn(self) -> None:
        sources = _flow_only()
        turns = _eval_turns()
        self.assertGreater(len(turns), 100)
        for turn in turns:
            text = turn.user_text or ""
            old = not OLD_WRITE_INTENT_RE.search(text) and bool(
                OLD_DATA_WORD_RE.search(text) or OLD_DATA_WORD_RE.search(turn.previous_assistant_text or "")
            )
            self.assertEqual(expects_tool(turn, sources), old, text)
            self.assertEqual(_expects_tool(turn), old, text)  # no sources given: Flow only

    def test_routed_sources_without_wording_are_left_out_of_the_messages(self) -> None:
        # test_routing's fakes have keywords but no wording: same messages as Flow only.
        words = wording_for(build_sources(_settings(), specs=(FLOW, RMM, CLOUD)))
        self.assertEqual(words.no_tool, FLOW_NO_TOOL)
        self.assertEqual(words.no_search_run, FLOW_NO_SEARCH)


class WordedSourcesTests(unittest.TestCase):
    def test_or_list(self) -> None:
        self.assertEqual(or_list(["a"]), "a")
        self.assertEqual(or_list(["a", "b"]), "a or b")
        self.assertEqual(or_list(["a", "b", "c", "b"]), "a, b, or c")
        self.assertEqual(or_list([]), "")

    def test_messages_name_every_enabled_source(self) -> None:
        words = wording_for(_worded())
        self.assertEqual(words.no_tool, ALL_NO_TOOL)
        self.assertEqual(words.stuck, ALL_STUCK)
        self.assertEqual(words.no_search_run, ALL_NO_SEARCH)
        self.assertEqual(no_tool_message(_worded()), ALL_NO_TOOL)

    def test_the_nudge_names_only_the_offered_sources(self) -> None:
        self.assertEqual(wording_for(_worded(), ("flow", "rmm")).nudge_to_use_a_tool, FLOW_RMM_NUDGE)
        self.assertEqual(wording_for(_worded(), ("flow",)).nudge_to_use_a_tool, FLOW_NUDGE)

    def test_enabled_sources_add_their_data_words(self) -> None:
        flow_only, worded = _flow_only(), _worded()
        for text in ("which devices are offline?", "any alerts today", "who has MFA turned off", "license count for Debe"):
            self.assertFalse(expects_tool(ChatTurn(user_text=text), flow_only), text)
            self.assertTrue(expects_tool(ChatTurn(user_text=text), worded), text)
        # Routing patterns count too: a hostname-like token is an RMM question.
        self.assertTrue(expects_tool(ChatTurn(user_text="is ACME-PC7 up"), worded))
        # A bare "Yes" follows the previous question, whichever source it was about.
        yes = ChatTurn(user_text="Yes", previous_assistant_text="Shall I check the patches on those devices?")
        self.assertFalse(expects_tool(yes, flow_only))
        self.assertTrue(expects_tool(yes, worded))

    def test_writes_and_off_topic_still_do_not_expect_a_tool(self) -> None:
        worded = _worded()
        for text in ("Update the license for Debe", "Reassign the alert to Tom", "Whats the weather", "hi"):
            self.assertFalse(expects_tool(ChatTurn(user_text=text), worded), text)


class LoopWordingTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_tool_twice_names_every_enabled_source_and_the_nudge_names_the_offered_ones(self) -> None:
        payload, bodies = await _run(
            [_user("which devices are offline at BUCK?")],
            [
                {"role": "assistant", "content": "All devices look fine."},
                {"role": "assistant", "content": "Still fine."},
            ],
            sources=_worded(),
        )
        self.assertEqual(bodies[1]["messages"][-1]["content"], FLOW_RMM_NUDGE)  # cloud was not offered
        self.assertEqual(payload["choices"][0]["message"]["content"], ALL_NO_SEARCH)

    async def test_flow_only_question_with_sources_enabled_is_nudged_with_flow_only_wording(self) -> None:
        payload, bodies = await _run(
            [_user("my urgent tickets")],
            [{"role": "assistant", "content": "None."}, {"role": "assistant", "content": "None."}],
            sources=_worded(),
        )
        self.assertEqual(bodies[1]["messages"][-1]["content"], FLOW_NUDGE)  # only Flow was offered
        self.assertEqual(payload["choices"][0]["message"]["content"], ALL_NO_SEARCH)

    async def test_data_question_for_a_routed_source_is_nudged(self) -> None:
        # Flow only would not nudge this: none of Flow's data words are in it.
        _, flow_bodies = await _run([_user("who has MFA turned off?")], [{"role": "assistant", "content": "Nobody."}],
                                    sources=_flow_only())
        self.assertEqual(len(flow_bodies), 1)
        _, bodies = await _run(
            [_user("who has MFA turned off?")],
            [{"role": "assistant", "content": "Nobody."}, _call("cloud_mfa_status", client_code="BUCK")],
            sources=_worded(),
        )
        self.assertEqual(
            bodies[1]["messages"][-1]["content"],
            "You answered without calling a tool, so that answer is unverified. For any question about tickets, time, "
            "mail, contacts, clients, users, MFA, licenses, or sign-ins you must call the right tool first (for example "
            "list_tickets for lists, ticket_stats for hours or counts, cloud_mfa_status for MFA) and answer only from "
            "its result. Call the tool now.",
        )
        self.assertIn("cloud_mfa_status", [tool["function"]["name"] for tool in bodies[2]["tools"]])

    async def test_non_english_reply_falls_back_to_the_enabled_sources_message(self) -> None:
        payload, _ = await _run([_user("hello")], [{"role": "assistant", "content": "สวัสดี"}], sources=_worded())
        self.assertEqual(payload["choices"][0]["message"]["content"], ALL_NO_TOOL)
        payload, _ = await _run([_user("hello")], [{"role": "assistant", "content": "สวัสดี"}], sources=_flow_only())
        self.assertEqual(payload["choices"][0]["message"]["content"], FLOW_NO_TOOL)

    async def test_stuck_names_every_enabled_source(self) -> None:
        settings = _settings(agent_max_tool_iters=2)
        payload, _ = await _run(
            [_user("which devices are offline at BUCK?")],
            [_call("no_such_tool"), _call("no_such_tool")],
            settings=settings,
            sources=build_sources(settings, specs=(FLOW, RMM_WORDED, CLOUD_WORDED)),
        )
        self.assertEqual(payload["choices"][0]["message"]["content"], ALL_STUCK)
        self.assertTrue(payload["x_tb_brain"]["stuck"])


if __name__ == "__main__":
    unittest.main()
