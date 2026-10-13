"""Source registry: a second source plugs in beside Flow without changing Flow.

The fake "widgets" source below exists only here. It has the same parts a real source has under
app/sources/<name>/: a read-only protocol, stub data, a tool schema, a handler and a prompt section,
and it is enabled from env (FAKE_MODE) the way FLOW_MODE / DATTO_MODE work.
"""

from __future__ import annotations

import tests._isolation  # noqa: F401  (first: hermetic settings, no network)

import json
import logging
import os
import unittest
from typing import Any, Protocol
from unittest import mock

import httpx
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.agent.loop import run_tool_loop
from app.config import Settings
from app.flow.source import FlowNotConfigured
from app.flow.stub import StubFlowSource
from app.sources.base import Sources, SourceSpec, as_sources, function_tool
from app.sources.flow.spec import FLOW
from app.sources.registry import build_sources
from app.tools import ChatTurn, ToolResult, run_tool
from app.tools.dispatch import dispatch
from app.tools.registry import OPENAI_TOOLS, SHARED_TOOLS

FAKE_TOOL = "fake_list_widgets"


class FakeRequestError(RuntimeError):
    """The fake source refused the request; its message is safe to show."""


class FakeSource(Protocol):
    source_name: str

    def list_widgets(self, *, client_code: str) -> list[dict[str, Any]]: ...


class StubFakeSource:
    source_name = "fake-stub"
    _WIDGETS = {"ACME": [{"id": 1, "name": "ACME-W1"}, {"id": 2, "name": "ACME-W2"}]}

    def list_widgets(self, *, client_code: str) -> list[dict[str, Any]]:
        if client_code == "NOPE":
            raise FakeRequestError("client NOPE has no widget site")
        return list(self._WIDGETS.get(client_code, []))


class ListWidgetsArgs(BaseModel):
    client_code: str


def _handle(source: FakeSource, name: str, raw_args: dict[str, Any], turn: ChatTurn | None = None, *, knowledge=None):
    if name != FAKE_TOOL:
        return None
    args = ListWidgetsArgs.model_validate(raw_args)
    rows = source.list_widgets(client_code=args.client_code.upper())
    lines = [f"{len(rows)} widget(s) for {args.client_code.upper()}."] + [f"- {row['name']}" for row in rows]
    return ToolResult(
        tool=FAKE_TOOL,
        source=source.source_name,
        data=rows,
        row_ids=[row["id"] for row in rows],
        client_code=args.client_code.upper(),
        reply="\n".join(lines),
    )


FAKE = SourceSpec(
    name="fake",
    mode=lambda settings: os.environ.get("FAKE_MODE", "off").strip().lower(),
    build=lambda settings: StubFakeSource(),
    tools=(
        function_tool(
            FAKE_TOOL,
            "List a client's widgets from the fake source.",
            {"type": "object", "properties": {"client_code": {"type": "string"}}, "required": ["client_code"]},
        ),
    ),
    handle=_handle,
    relay_tool_names=(FAKE_TOOL,),
    errors=(FakeRequestError,),
    prompt_section="Fake widgets: fake_list_widgets(client_code).",
)


def _settings() -> Settings:
    return Settings(llm_model="fake", llm_base_url="http://llm.test/v1", _env_file=None)


def _with_fake(mode: str) -> Sources:
    with mock.patch.dict(os.environ, {"FAKE_MODE": mode}):
        return build_sources(_settings(), specs=(FLOW, FAKE))


def _names(tools: list[dict[str, Any]]) -> list[str]:
    return [tool["function"]["name"] for tool in tools]


class RegistryTests(unittest.TestCase):
    def test_default_is_flow_only_with_the_same_tools(self) -> None:
        sources = build_sources(_settings())
        self.assertEqual(sources.names, ("flow",))
        self.assertEqual(sources.flow.source_name, "stub")
        from app.tools import openai_tools

        self.assertEqual(json.dumps(openai_tools(sources)), json.dumps(OPENAI_TOOLS))

    def test_enabling_the_fake_source_adds_its_tool_before_the_shared_ones(self) -> None:
        from app.tools import openai_tools

        sources = _with_fake("stub")
        self.assertEqual(sources.names, ("flow", "fake"))
        flow_only = _names(OPENAI_TOOLS[: -len(SHARED_TOOLS)])
        self.assertEqual(_names(openai_tools(sources)), [*flow_only, FAKE_TOOL, *_names(SHARED_TOOLS)])
        # Flow's own definitions are untouched.
        self.assertEqual(openai_tools(sources)[: len(flow_only)], OPENAI_TOOLS[: len(flow_only)])

    def test_disabling_the_fake_source_removes_its_tool(self) -> None:
        from app.tools import openai_tools

        for mode in ("off", "", "none"):
            sources = _with_fake(mode)
            self.assertEqual(sources.names, ("flow",), mode)
            self.assertEqual(openai_tools(sources), OPENAI_TOOLS, mode)

    def test_flow_is_required_and_a_bad_flow_mode_still_fails_like_before(self) -> None:
        with self.assertRaises(FlowNotConfigured):
            build_sources(Settings(flow_mode="off", _env_file=None))

    def test_reserved_m365_mode_adds_nothing_and_says_so(self) -> None:
        with self.assertLogs("tb_brain.sources", level=logging.WARNING) as logs:
            sources = build_sources(Settings(m365_mode="http", _env_file=None))
        self.assertEqual(sources.names, ("flow",))
        self.assertTrue(any("M365_MODE" in line for line in logs.output))

    def test_datto_mode_stub_adds_datto_after_flow(self) -> None:
        sources = build_sources(Settings(datto_mode="stub", _env_file=None))
        self.assertEqual(sources.names, ("flow", "datto"))
        self.assertEqual(sources.client("datto").source_name, "datto-stub")

    def test_two_sources_cannot_offer_the_same_tool_name(self) -> None:
        clash = SourceSpec(name="clash", mode=lambda s: "stub", build=lambda s: object(), tools=FLOW.tools[:1], handle=_handle)
        with self.assertRaises(ValueError):
            Sources([(FLOW, StubFlowSource()), (clash, object())])

    def test_a_bare_flow_client_means_flow_only(self) -> None:
        stub = StubFlowSource()
        sources = as_sources(stub)
        self.assertEqual(sources.names, ("flow",))
        self.assertIs(sources.flow, stub)
        self.assertIs(as_sources(sources), sources)

    def test_prompt_sections_follow_the_enabled_sources(self) -> None:
        self.assertEqual(len(_with_fake("off").prompt_sections()), 1)
        self.assertIn("fake_list_widgets", _with_fake("stub").prompt_sections()[-1])


class RoutingTests(unittest.TestCase):
    def test_fake_tool_runs_on_the_fake_source(self) -> None:
        result = dispatch(_with_fake("stub"), FAKE_TOOL, {"client_code": "acme"})
        self.assertTrue(result.ok)
        self.assertEqual(result.source, "fake-stub")
        self.assertEqual(result.row_ids, [1, 2])
        self.assertIn("ACME-W2", result.reply or "")

    def test_flow_tools_answer_the_same_with_the_fake_source_enabled(self) -> None:
        turn = ChatTurn(user_text="open tickets for ACME")
        alone = dispatch(StubFlowSource(), "list_tickets", {"client_code": "ACME"}, turn)
        beside = dispatch(_with_fake("stub"), "list_tickets", {"client_code": "ACME"}, turn)
        self.assertEqual(alone.model_dump(), beside.model_dump())

    def test_fake_tool_is_unknown_when_the_fake_source_is_off(self) -> None:
        result = dispatch(_with_fake("off"), FAKE_TOOL, {"client_code": "ACME"})
        self.assertFalse(result.ok)
        self.assertIn("Unknown tool", result.error or "")
        self.assertNotIn(FAKE_TOOL, (result.error or "").split("Allowed:")[1])

    def test_unknown_tool_lists_every_enabled_tool(self) -> None:
        result = dispatch(_with_fake("stub"), "nope", {})
        self.assertIn(FAKE_TOOL, result.error or "")
        self.assertTrue((result.error or "").rstrip(".").endswith("search_knowledge"))


class AuditTests(unittest.TestCase):
    def _run(self, sources: Sources, name: str, arguments: dict[str, Any]) -> tuple[ToolResult, dict[str, Any]]:
        with mock.patch("app.tools.log_tool_call") as audit:
            result = run_tool(sources=sources, name=name, arguments=arguments, actor="test", settings=_settings())
        audit.assert_called_once()
        return result, audit.call_args.kwargs

    def test_audit_row_names_the_source_that_served_the_call(self) -> None:
        sources = _with_fake("stub")
        _, fake_row = self._run(sources, FAKE_TOOL, {"client_code": "ACME"})
        self.assertEqual((fake_row["tool"], fake_row["source"], fake_row["ok"]), (FAKE_TOOL, "fake-stub", True))
        self.assertEqual(fake_row["row_ids"], [1, 2])
        self.assertEqual(fake_row["client_code"], "ACME")
        _, flow_row = self._run(sources, "list_tickets", {"client_code": "ACME"})
        self.assertEqual(flow_row["source"], "stub")

    def test_a_source_refusal_is_shown_as_is_and_audited_as_failed(self) -> None:
        result, row = self._run(_with_fake("stub"), FAKE_TOOL, {"client_code": "NOPE"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "client NOPE has no widget site")
        self.assertEqual((row["source"], row["ok"]), ("fake-stub", False))


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_model_is_offered_and_can_call_the_fake_tool(self) -> None:
        sent: list[dict[str, Any]] = []
        script = iter(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": FAKE_TOOL, "arguments": '{"client_code": "ACME"}'}}
                    ],
                },
                {"role": "assistant", "content": "ignored: the tool reply wins"},
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "x", "choices": [{"index": 0, "message": next(script), "finish_reason": "stop"}]})

        real = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with mock.patch("app.agent.loop.httpx.AsyncClient", lambda **kw: real(transport=transport, **kw)):
            payload = await run_tool_loop(
                settings=_settings(),
                sources=_with_fake("stub"),
                messages=[{"role": "user", "content": "Which widgets does ACME have?"}],
                model=None,
                actor="test",
            )
        self.assertIn(FAKE_TOOL, _names(sent[0]["tools"]))
        self.assertEqual([call["name"] for call in payload["x_tb_brain"]["tool_calls"]], [FAKE_TOOL])
        self.assertIn("ACME-W1", payload["choices"][0]["message"]["content"])


class ApiTests(unittest.TestCase):
    def test_tools_and_health_follow_the_enabled_sources(self) -> None:
        from app.main import app

        with TestClient(app) as client:
            default_tools = _names(client.get("/tools").json()["tools"])
            client.app.state.sources = _with_fake("stub")
            try:
                tools = _names(client.get("/tools").json()["tools"])
                health = client.get("/health").json()
            finally:
                client.app.state.sources = None
        self.assertNotIn(FAKE_TOOL, default_tools)
        self.assertIn(FAKE_TOOL, tools)
        self.assertIn(FAKE_TOOL, health["tools"])
        self.assertEqual(health["sources"], {"flow": "stub", "fake": "fake-stub"})
        self.assertEqual(health["write_tools"], ["merge_tickets"])


if __name__ == "__main__":
    unittest.main()
