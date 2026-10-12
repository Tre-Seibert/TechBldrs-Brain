"""PROMPT_STYLE: legacy stays byte-identical; lean is general rules plus a section per enabled source, the tool
guidance lives in the tool descriptions, and the per-user / per-day lines come after the static prefix."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import httpx
from pydantic import ValidationError

import app.agent.loop as loop
from app.agent.loop import _ensure_system, run_tool_loop
from app.agent.system import LEAN_BASE_PROMPT, SYSTEM_PROMPT, lean_system_prompt
from app.config import Settings
from app.flow.stub import StubFlowSource
from app.identity import current_signed_in_email
from app.sources.flow.lean_tools import LEAN_OPENAI_TOOLS
from app.sources.flow.prompt import PROMPT_SECTION
from app.sources.flow.tools import MERGE_TICKETS
from app.sources.flow.tools import OPENAI_TOOLS as FLOW_OPENAI_TOOLS
from app.sources.registry import build_sources
from app.tools import openai_tools
from app.tools.registry import LEAN_SHARED_TOOLS, OPENAI_TOOLS, SHARED_TOOLS
from tests.test_sources import FAKE_TOOL, _with_fake

# sha256 of SYSTEM_PROMPT and of json.dumps(openai_tools()) at 35f8dbb, before PROMPT_STYLE existed.
LEGACY_PROMPT_SHA = "30fcfdcf0d7d6874391f80aac78b66e33fdcd7f8f5d134fb33cbcfe3af4d48b8"
LEGACY_TOOLS_SHA = "19b42551851663977d462afec95c26e39e63f1c9957623a895c48b21f27d6da1"

TRE = "tseibert@techbldrs.example"
TOM = "trivera@techbldrs.example"


def _settings(**kw) -> Settings:
    return Settings(llm_model="fake", llm_base_url="http://llm.test/v1", _env_file=None, **kw)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _messages_as(email: str | None, style: str, messages=None, *, day: int = 12, source=None) -> list[dict]:
    class Day(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, day, 9, 0, tzinfo=tz)

    token = current_signed_in_email.set(email)
    try:
        with mock.patch.object(loop, "datetime", Day):
            return _ensure_system(
                messages or [{"role": "user", "content": "What are my open tickets?"}],
                source=source or StubFlowSource(),
                style=style,
            )
    finally:
        current_signed_in_email.reset(token)


class StyleSelectionTests(unittest.TestCase):
    def test_default_is_legacy(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROMPT_STYLE", None)
            self.assertEqual(_settings().prompt_style, "legacy")

    def test_env_picks_lean_in_any_case(self) -> None:
        with mock.patch.dict(os.environ, {"PROMPT_STYLE": " Lean "}):
            self.assertEqual(_settings().prompt_style, "lean")

    def test_unknown_style_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            _settings(prompt_style="short")

    async def _first_body(self, style: str) -> dict:
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            message = {"role": "assistant", "content": "Hello."}
            return httpx.Response(200, json={"id": "x", "choices": [{"index": 0, "message": message}]})

        real = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with mock.patch("app.agent.loop.httpx.AsyncClient", lambda **kw: real(transport=transport, **kw)):
            await run_tool_loop(
                settings=_settings(prompt_style=style),
                source=StubFlowSource(),
                messages=[{"role": "user", "content": "hello"}],
                model=None,
                actor="test",
            )
        return bodies[0]

    def test_the_loop_sends_the_chosen_style(self) -> None:
        import asyncio

        legacy = asyncio.run(self._first_body("legacy"))
        lean = asyncio.run(self._first_body("lean"))
        self.assertEqual(legacy["tools"], OPENAI_TOOLS)
        self.assertTrue(legacy["messages"][0]["content"].startswith(SYSTEM_PROMPT.rstrip()))
        self.assertEqual(lean["tools"], [*LEAN_OPENAI_TOOLS, *LEAN_SHARED_TOOLS])
        self.assertTrue(lean["messages"][0]["content"].startswith(LEAN_BASE_PROMPT))


class LegacyUnchangedTests(unittest.TestCase):
    def test_prompt_and_tools_hash_as_before(self) -> None:
        self.assertEqual(_sha(SYSTEM_PROMPT), LEGACY_PROMPT_SHA)
        self.assertEqual(_sha(json.dumps(openai_tools())), LEGACY_TOOLS_SHA)
        self.assertEqual(_sha(json.dumps(openai_tools(build_sources(_settings())))), LEGACY_TOOLS_SHA)
        self.assertEqual(_sha(json.dumps(openai_tools(None, style="legacy"))), LEGACY_TOOLS_SHA)

    def test_legacy_keeps_the_per_user_lines_in_the_system_message(self) -> None:
        messages = _messages_as(TRE, "legacy")
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertTrue(messages[0]["content"].startswith(SYSTEM_PROMPT.rstrip() + "\n- Signed-in technician: Tre"))
        self.assertIn("Today is Monday 2026-10-12", messages[0]["content"])


class LeanSizeTests(unittest.TestCase):
    def test_lean_prompt_fits_the_budget(self) -> None:
        # About 1.5K tokens at chars/4; measured with the Qwen tokenizer it is about 670.
        prompt = lean_system_prompt([PROMPT_SECTION])
        self.assertLessEqual(len(prompt), 6000)
        self.assertLess(len(prompt), len(SYSTEM_PROMPT) / 3)

    def test_lean_tools_are_noticeably_smaller(self) -> None:
        legacy = len(json.dumps(openai_tools()))
        lean = len(json.dumps(openai_tools(style="lean")))
        self.assertLessEqual(lean, legacy * 0.8)

    def test_lean_tools_take_the_same_arguments(self) -> None:
        def shape(tools):
            out = {}
            for tool in tools:
                params = tool["function"]["parameters"]
                props = {
                    name: (spec.get("type"), spec.get("enum"), (spec.get("items") or {}).get("type"))
                    for name, spec in params.get("properties", {}).items()
                }
                out[tool["function"]["name"]] = (props, params.get("required"))
            return out

        self.assertEqual(list(shape(LEAN_OPENAI_TOOLS)), list(shape(FLOW_OPENAI_TOOLS)))
        self.assertEqual(shape(LEAN_OPENAI_TOOLS), shape(FLOW_OPENAI_TOOLS))
        self.assertEqual(shape(LEAN_SHARED_TOOLS), shape(SHARED_TOOLS))


class LeanRulesTests(unittest.TestCase):
    def test_merge_safety_steps_are_intact(self) -> None:
        merge = next(t for t in LEAN_OPENAI_TOOLS if t["function"]["name"] == MERGE_TICKETS)["function"]
        for text in (LEAN_BASE_PROMPT, merge["description"]):
            lowered = text.lower().replace("'", '"')
            self.assertIn("show the plan", lowered)
            self.assertIn("restating both labels", lowered)
            self.assertIn('"ok", "yes", "sure" or "do it" alone is not approval', lowered)
            self.assertIn("ok=false", lowered)
            self.assertIn("never retry", lowered)
        self.assertIn("restated both labels", merge["parameters"]["properties"]["confirm"]["description"])

    def test_general_rules_are_present_and_flow_only_refusal_is_gone(self) -> None:
        prompt = lean_system_prompt([PROMPT_SECTION])
        for needle in ("English only", "Answer from tool results only", "read-only", "passwords",
                       "report the error", "signed-in technician", "source=stub"):
            self.assertIn(needle, prompt)
        self.assertIn("client and MSP data the enabled sources", prompt)
        self.assertNotIn("only answer questions about Flow", prompt)
        self.assertNotIn("not about Flow", prompt)

    def test_recipes_moved_into_tool_descriptions(self) -> None:
        prompt = lean_system_prompt([PROMPT_SECTION])
        self.assertNotIn("latest_ticket(", prompt)
        latest = next(t for t in LEAN_OPENAI_TOOLS if t["function"]["name"] == "latest_ticket")
        self.assertIn("Last CODE ticket -> client_code=CODE", latest["function"]["description"])


class LeanMessageOrderTests(unittest.TestCase):
    def test_per_user_and_date_lines_follow_the_static_prefix(self) -> None:
        tre = _messages_as(TRE, "lean")
        tom = _messages_as(TOM, "lean")
        tre_tomorrow = _messages_as(TRE, "lean", day=13)
        self.assertEqual([m["role"] for m in tre], ["system", "user", "user"])
        # The system message (and the tool list Ollama puts right after it) is the same for everyone, every day.
        self.assertEqual(tre[0], tom[0])
        self.assertEqual(tre[0], tre_tomorrow[0])
        self.assertNotIn("Signed-in", tre[0]["content"])
        self.assertNotIn("2026-10-12", tre[0]["content"])
        # Who and when come next, in a user message, so Ollama does not fold them into the system block.
        self.assertIn("Signed-in technician: Tre Seibert (ts", tre[1]["content"])
        self.assertIn("Signed-in technician: Tom Rivera (tr", tom[1]["content"])
        self.assertIn("Today is Monday 2026-10-12", tre[1]["content"])
        self.assertIn("Today is Tuesday 2026-10-13", tre_tomorrow[1]["content"])
        self.assertEqual(tre[2], {"role": "user", "content": "What are my open tickets?"})
        self.assertEqual(openai_tools(style="lean"), openai_tools(build_sources(_settings()), style="lean"))

    def test_unknown_user_still_gets_a_context_line(self) -> None:
        messages = _messages_as(None, "lean")
        self.assertIn("Signed-in technician is unknown", messages[1]["content"])

    def test_a_client_system_message_stays_in_the_static_prefix(self) -> None:
        history = [
            {"role": "system", "content": "Keep answers short."},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "Hello."},
            {"role": "user", "content": "my tickets"},
        ]
        messages = _messages_as(TRE, "lean", history)
        self.assertEqual(messages[0]["content"], lean_system_prompt([PROMPT_SECTION]) + "\n\nKeep answers short.")
        self.assertEqual([m["role"] for m in messages], ["system", "user", "user", "assistant", "user"])
        self.assertIn("Signed-in", messages[1]["content"])
        self.assertEqual(messages[2:], history[1:])

    def test_no_think_still_lands_in_the_system_message(self) -> None:
        messages = _ensure_system([{"role": "user", "content": "hi"}], source=StubFlowSource(), no_think=True, style="lean")
        self.assertTrue(messages[0]["content"].endswith("/no_think"))


class LeanSourceSectionTests(unittest.TestCase):
    def test_sections_follow_the_enabled_sources(self) -> None:
        on = _messages_as(TRE, "lean", source=_with_fake("stub"))[0]["content"]
        off = _messages_as(TRE, "lean", source=_with_fake("off"))[0]["content"]
        self.assertIn(PROMPT_SECTION, on)
        self.assertIn("Fake widgets: fake_list_widgets", on)
        self.assertIn(PROMPT_SECTION, off)
        self.assertNotIn("fake_list_widgets", off)

    def test_a_source_without_lean_tools_offers_its_own(self) -> None:
        names = [t["function"]["name"] for t in openai_tools(_with_fake("stub"), style="lean")]
        self.assertEqual(names, [*(t["function"]["name"] for t in LEAN_OPENAI_TOOLS), FAKE_TOOL, "search_knowledge"])
        legacy = [t["function"]["name"] for t in openai_tools(_with_fake("off"))]
        self.assertNotIn(FAKE_TOOL, legacy)


class ProbeCacheTests(unittest.TestCase):
    def _probe(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "probe_cache.py"
        spec = importlib.util.spec_from_file_location("probe_cache", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_requests_for_two_users_share_the_static_prefix_in_lean_only(self) -> None:
        probe = self._probe()
        sources = build_sources(_settings())
        settings = _settings()
        lean = [probe.build_request(settings, sources, "lean", who, "my tickets", "m") for who in (TRE, TOM)]
        legacy = [probe.build_request(settings, sources, "legacy", who, "my tickets", "m") for who in (TRE, TOM)]
        self.assertEqual(lean[0]["messages"][0], lean[1]["messages"][0])
        self.assertEqual(lean[0]["tools"], lean[1]["tools"])
        self.assertNotEqual(lean[0]["messages"][1], lean[1]["messages"][1])
        self.assertNotEqual(legacy[0]["messages"][0], legacy[1]["messages"][0])
        self.assertEqual(lean[0]["options"]["num_predict"], 1)

    def test_native_url_drops_v1(self) -> None:
        probe = self._probe()
        self.assertEqual(probe.native_base("http://127.0.0.1:11434/v1/"), "http://127.0.0.1:11434")
        self.assertEqual(probe.native_base("http://box:11434"), "http://box:11434")


if __name__ == "__main__":
    unittest.main()
