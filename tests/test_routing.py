"""Source routing (A5): each model call is offered only the sources the question needs.

Two fake routed sources stand in for Datto RMM and Microsoft 365, which don't exist yet: "rmm" (devices,
alerts, hostname-like tokens) and "cloud" (MFA, sign-ins, licenses, tenants). Flow and search_knowledge are
always offered; with only Flow enabled routing changes nothing.
"""

from __future__ import annotations

import tests._isolation  # noqa: F401  (first: hermetic settings, no network)

import json
import re
import unittest
from typing import Any
from unittest import mock

import httpx

from app.agent.routing import USE_SOURCE, matched_term, route
from app.agent.system import LEAN_BASE_PROMPT
from app.config import Settings
from app.sources.base import SourceSpec, function_tool
from app.sources.flow.prompt import PROMPT_SECTION as FLOW_SECTION
from app.sources.flow.spec import FLOW
from app.sources.registry import build_sources
from app.agent.loop import run_tool_loop
from app.tools import ToolResult, openai_tools

RMM_TOOLS = ("rmm_list_devices", "rmm_device_detail", "rmm_list_alerts")
CLOUD_TOOLS = ("cloud_mfa_status", "cloud_list_licenses", "cloud_sign_ins", "cloud_secure_score")
RMM_SECTION = "RMM (fake): devices and alerts per client site."
CLOUD_SECTION = "Cloud (fake): Microsoft 365 users, MFA, licenses and sign-ins per tenant."
FLOW_TOOL_COUNT = len(FLOW.tools) + 1  # Flow's tools plus search_knowledge


class FakeClient:
    def __init__(self, name: str) -> None:
        self.source_name = f"{name}-stub"
        self.calls: list[tuple[str, dict[str, Any]]] = []


def _handler(prefix: str):
    def handle(client: FakeClient, name: str, raw_args: dict[str, Any], turn=None, *, knowledge=None):
        if not name.startswith(prefix):
            return None
        client.calls.append((name, raw_args))
        code = str(raw_args.get("client_code") or "").upper()
        return ToolResult(tool=name, source=client.source_name, client_code=code or None, reply=f"{name} for {code}: 2 rows.")

    return handle


def _spec(name: str, tools: tuple[str, ...], **routing: Any) -> SourceSpec:
    params = {"type": "object", "properties": {"client_code": {"type": "string"}}, "required": ["client_code"]}
    return SourceSpec(
        name=name,
        mode=lambda settings: "stub",
        build=lambda settings: FakeClient(name),
        tools=tuple(function_tool(tool, f"Fake {tool}.", params) for tool in tools),
        handle=_handler(f"{name}_"),
        relay_tool_names=tools,
        **routing,
    )


RMM = _spec(
    "rmm",
    RMM_TOOLS,
    prompt_section=RMM_SECTION,
    description="Fake RMM: managed devices, online/offline state, patches, antivirus and alerts.",
    keywords=("device", "offline", "patch", "antivirus", "alert"),
    patterns=(re.compile(r"\b[A-Z]{3,5}-(?:DC|SRV|PC|LT|WS)\d{1,3}\b"),),
)
CLOUD = _spec(
    "cloud",
    CLOUD_TOOLS,
    prompt_section=CLOUD_SECTION,
    description="Fake Microsoft 365: users, MFA, sign-ins, licenses, admin roles, Intune and Secure Score.",
    keywords=("MFA", "sign-in", "license", "user account", "tenant", "admin role", "Intune", "Secure Score"),
)


def _settings(**kw: Any) -> Settings:
    return Settings(llm_model="fake", llm_base_url="http://llm.test/v1", _env_file=None, **kw)


def _sources(settings: Settings | None = None):
    return build_sources(settings or _settings(), specs=(FLOW, RMM, CLOUD))


def _names(tools: list[dict[str, Any]]) -> list[str]:
    return [tool["function"]["name"] for tool in tools]


def _user(text: str) -> dict[str, str]:
    return {"role": "user", "content": text}


def _call(tool: str, **arguments: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": tool, "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)}}
    ]}


async def _run(messages, script, *, settings=None, sources=None):
    """Run the loop against a fake LLM that answers from `script` in order; returns (payload, request bodies)."""
    bodies: list[dict[str, Any]] = []
    replies = iter(script)

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        message = next(replies, {"role": "assistant", "content": "Done."})
        return httpx.Response(200, json={"id": "x", "choices": [{"index": 0, "message": message}]})

    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    with mock.patch("app.agent.loop.httpx.AsyncClient", lambda **kw: real(transport=transport, **kw)):
        payload = await run_tool_loop(
            settings=settings or _settings(),
            sources=sources or _sources(),
            messages=messages,
            model=None,
            actor="test",
        )
    return payload, bodies


class MatchingTests(unittest.TestCase):
    def test_keywords_match_any_case_plurals_and_spaced_or_hyphenated_phrases(self) -> None:
        self.assertEqual(matched_term(RMM, "Which DEVICES are offline?"), "DEVICES")
        self.assertEqual(matched_term(RMM, "any alerts at BUCK"), "alerts")
        self.assertEqual(matched_term(RMM, "pending patches for ACME"), "patches")
        self.assertEqual(matched_term(CLOUD, "failed sign in for Debe"), "sign in")
        self.assertEqual(matched_term(CLOUD, "who has an admin-role at BUCK"), "admin-role")
        self.assertEqual(matched_term(CLOUD, "check their secure score"), "secure score")

    def test_keywords_match_whole_words_only(self) -> None:
        self.assertIsNone(matched_term(RMM, "dispatch a tech to ACME"))
        self.assertIsNone(matched_term(RMM, "alerted the client"))
        self.assertIsNone(matched_term(CLOUD, "licensed tickets for ACME"))

    def test_patterns_catch_hostname_like_tokens_but_not_ticket_labels(self) -> None:
        self.assertEqual(matched_term(RMM, "is ACME-DC01 up?"), "ACME-DC01")
        self.assertIsNone(matched_term(RMM, "show me ZTB-1691"))


class DecisionTests(unittest.TestCase):
    def test_keyword_offers_the_matching_source_only(self) -> None:
        routing = route(_sources(), latest="Which devices are offline at ACME?")
        self.assertEqual(routing.offered, ("flow", "rmm"))
        self.assertEqual([spec.name for spec in routing.hidden], ["cloud"])

    def test_no_match_is_flow_only_plus_use_source(self) -> None:
        routing = route(_sources(), latest="open tickets for ACME")
        self.assertEqual(routing.offered, ("flow",))
        names = _names(routing.openai_tools("legacy"))
        self.assertEqual(names, [*_names(FLOW.tools), "search_knowledge", USE_SOURCE])
        use_source = routing.openai_tools("legacy")[-1]["function"]
        self.assertEqual(use_source["parameters"]["properties"]["name"]["enum"], ["rmm", "cloud"])
        self.assertIn(RMM.description, use_source["description"])
        self.assertIn(CLOUD.description, use_source["description"])

    def test_sticky_from_an_earlier_user_message(self) -> None:
        routing = route(_sources(), latest="what about BUCK?", earlier=["Which devices are offline at ACME?"])
        self.assertEqual(routing.offered, ("flow", "rmm"))
        self.assertEqual(routing.record()["offered"][1], {"source": "rmm", "why": "sticky", "matched": "devices"})

    def test_sticky_from_an_earlier_tool_call(self) -> None:
        routing = route(_sources(), latest="and for BUCK?", earlier=["check ACME"], earlier_tools={"cloud_mfa_status"})
        self.assertEqual(routing.offered, ("flow", "cloud"))
        self.assertEqual(routing.record()["offered"][1]["why"], "sticky")

    def test_a_keyword_in_the_latest_message_wins_over_sticky(self) -> None:
        routing = route(_sources(), latest="any alerts?", earlier=["devices offline?"])
        self.assertEqual(routing.record()["offered"][1], {"source": "rmm", "why": "keyword", "matched": "alerts"})

    def test_routing_off_offers_everything_and_no_use_source(self) -> None:
        routing = route(_sources(), latest="open tickets for ACME", enabled=False)
        self.assertEqual(routing.offered, ("flow", "rmm", "cloud"))
        self.assertEqual(_names(routing.openai_tools("legacy")), _names(openai_tools(_sources())))
        self.assertEqual(
            routing.record(),
            {
                "enabled": False,
                "offered": [
                    {"source": "flow", "why": "always"},
                    {"source": "rmm", "why": "routing_off"},
                    {"source": "cloud", "why": "routing_off"},
                ],
                "hidden": [],
                "tool_counts": [],
            },
        )

    def test_source_routing_setting_reads_on_and_off(self) -> None:
        self.assertTrue(_settings().source_routing)
        self.assertFalse(_settings(source_routing="off").source_routing)
        self.assertTrue(_settings(source_routing="on").source_routing)

    def test_flow_only_routing_is_a_no_op_in_both_styles(self) -> None:
        flow_only = build_sources(_settings())
        for style in ("legacy", "lean"):
            for enabled in (True, False):
                routing = route(flow_only, latest="which devices need patches? MFA?", enabled=enabled)
                self.assertFalse(routing.active)
                self.assertEqual(routing.openai_tools(style), openai_tools(flow_only, style=style))
                self.assertEqual(routing.prompt_sections(), flow_only.prompt_sections())

    def test_a_source_without_a_description_is_always_offered(self) -> None:
        plain = _spec("plain", ("plain_list",))
        routing = route(build_sources(_settings(), specs=(FLOW, plain)), latest="open tickets")
        self.assertEqual(routing.offered, ("flow", "plain"))
        self.assertFalse(routing.active)


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_keyword_routing_sends_only_the_matching_tools(self) -> None:
        payload, bodies = await _run(
            [_user("Which devices are offline at ACME?")], [_call("rmm_list_devices", client_code="ACME")]
        )
        first = _names(bodies[0]["tools"])
        self.assertEqual(first, [*_names(FLOW.tools), *RMM_TOOLS, "search_knowledge", USE_SOURCE])
        self.assertEqual(len(first), FLOW_TOOL_COUNT + len(RMM_TOOLS) + 1)
        self.assertEqual(payload["x_tb_brain"]["tool_calls"][0]["name"], "rmm_list_devices")
        self.assertIn("rmm_list_devices for ACME", payload["choices"][0]["message"]["content"])

    async def test_x_tb_brain_records_the_routing_decision(self) -> None:
        payload, _ = await _run(
            [_user("Which devices are offline at ACME?")], [_call("rmm_list_devices", client_code="ACME")]
        )
        self.assertEqual(
            payload["x_tb_brain"]["routing"],
            {
                "enabled": True,
                "offered": [{"source": "flow", "why": "always"}, {"source": "rmm", "why": "keyword", "matched": "devices"}],
                "hidden": ["cloud"],
                # The tool call, then the final answer written after the relayed reply.
                "tool_counts": [FLOW_TOOL_COUNT + len(RMM_TOOLS) + 1] * 2,
            },
        )

    async def test_no_match_offers_flow_and_use_source_only(self) -> None:
        payload, bodies = await _run([_user("open tickets for ACME")], [{"role": "assistant", "content": "None open."}])
        self.assertEqual(_names(bodies[0]["tools"]), [*_names(FLOW.tools), "search_knowledge", USE_SOURCE])
        self.assertEqual(payload["x_tb_brain"]["routing"]["offered"], [{"source": "flow", "why": "always"}])
        self.assertEqual(payload["x_tb_brain"]["routing"]["hidden"], ["rmm", "cloud"])

    async def test_sticky_keeps_the_source_for_a_follow_up(self) -> None:
        messages = [
            _user("Which devices are offline at ACME?"),
            {"role": "assistant", "content": "rmm_list_devices for ACME: 2 rows."},
            _user("what about BUCK?"),
        ]
        payload, bodies = await _run(messages, [_call("rmm_list_devices", client_code="BUCK")])
        self.assertIn("rmm_list_devices", _names(bodies[0]["tools"]))
        self.assertEqual(payload["x_tb_brain"]["routing"]["offered"][1]["why"], "sticky")
        self.assertTrue(payload["x_tb_brain"]["tool_calls"][0]["ok"])

    async def test_use_source_adds_the_tools_on_the_next_iteration(self) -> None:
        sources = _sources()
        with mock.patch("app.agent.loop.log_tool_call") as audit:
            payload, bodies = await _run(
                [_user("Is Debe enrolled for two-factor at ACME?")],
                [_call(USE_SOURCE, name="cloud"), _call("cloud_mfa_status", client_code="ACME")],
                sources=sources,
            )
        self.assertNotIn("cloud_mfa_status", _names(bodies[0]["tools"]))
        second = _names(bodies[1]["tools"])
        self.assertEqual(second, [*_names(FLOW.tools), *CLOUD_TOOLS, "search_knowledge", USE_SOURCE])
        self.assertEqual(bodies[1]["tools"][-1]["function"]["parameters"]["properties"]["name"]["enum"], ["rmm"])
        # use_source is the model's own step and makes no LLM call of its own: ask, use, answer.
        self.assertEqual(len(bodies), 3)
        self.assertEqual([call["name"] for call in payload["x_tb_brain"]["tool_calls"]], [USE_SOURCE, "cloud_mfa_status"])
        self.assertEqual(sources.client("cloud").calls, [("cloud_mfa_status", {"client_code": "ACME"})])
        self.assertEqual(
            payload["x_tb_brain"]["routing"]["offered"][1:], [{"source": "cloud", "why": "requested"}]
        )
        self.assertEqual(
            payload["x_tb_brain"]["routing"]["tool_counts"],
            [FLOW_TOOL_COUNT + 1, *[FLOW_TOOL_COUNT + len(CLOUD_TOOLS) + 1] * 2],
        )
        audit.assert_called_once()
        self.assertEqual((audit.call_args.kwargs["tool"], audit.call_args.kwargs["ok"]), (USE_SOURCE, True))

    async def test_use_source_with_an_unknown_name_is_an_error_and_changes_nothing(self) -> None:
        payload, bodies = await _run(
            [_user("open tickets for ACME")], [_call(USE_SOURCE, name="itglue"), {"role": "assistant", "content": "ok"}]
        )
        self.assertFalse(payload["x_tb_brain"]["tool_calls"][0]["ok"])
        self.assertIn("Unknown source 'itglue'", payload["x_tb_brain"]["tool_calls"][0]["error"])
        self.assertEqual(_names(bodies[0]["tools"]), _names(bodies[1]["tools"]))

    async def test_a_tool_from_a_hidden_source_is_refused_not_dispatched(self) -> None:
        sources = _sources()
        with mock.patch("app.tools.log_tool_call") as audit:
            payload, _ = await _run(
                [_user("open tickets for ACME")],
                [_call("cloud_mfa_status", client_code="ACME"), {"role": "assistant", "content": "Could not check."}],
                sources=sources,
            )
        call = payload["x_tb_brain"]["tool_calls"][0]
        self.assertEqual((call["name"], call["ok"]), ("cloud_mfa_status", False))
        self.assertIn("cloud was not offered", call["error"])
        self.assertIn('use_source(name="cloud")', call["error"])
        self.assertEqual(sources.client("cloud").calls, [])
        row = audit.call_args.kwargs
        self.assertEqual((row["tool"], row["ok"], row["source"]), ("cloud_mfa_status", False, "cloud-stub"))

    async def test_a_source_added_in_the_same_reply_counts_from_the_next_call(self) -> None:
        sources = _sources()
        both = {"role": "assistant", "content": None, "tool_calls": [
            *_call(USE_SOURCE, name="cloud")["tool_calls"], *_call("cloud_mfa_status", client_code="ACME")["tool_calls"]
        ]}
        payload, _ = await _run([_user("check ACME")], [both], sources=sources)
        self.assertEqual([c["ok"] for c in payload["x_tb_brain"]["tool_calls"]], [True, False])
        self.assertEqual(sources.client("cloud").calls, [])

    async def test_routing_off_sends_every_enabled_tool(self) -> None:
        payload, bodies = await _run(
            [_user("open tickets for ACME")], [{"role": "assistant", "content": "None."}], settings=_settings(source_routing=False)
        )
        self.assertEqual(_names(bodies[0]["tools"]), _names(openai_tools(_sources())))
        self.assertNotIn(USE_SOURCE, _names(bodies[0]["tools"]))
        self.assertFalse(payload["x_tb_brain"]["routing"]["enabled"])

    async def test_lean_sections_follow_the_offered_sources(self) -> None:
        lean = _settings(prompt_style="lean")
        _, bodies = await _run([_user("any alerts at ACME?")], [{"role": "assistant", "content": "None."}], settings=lean)
        system = bodies[0]["messages"][0]["content"]
        self.assertTrue(system.startswith(LEAN_BASE_PROMPT))
        self.assertIn(RMM_SECTION, system)
        self.assertNotIn(CLOUD_SECTION, system)
        self.assertLess(system.index(FLOW_SECTION.splitlines()[0]), system.index(RMM_SECTION))
        self.assertEqual(bodies[0]["messages"][1]["role"], "user")  # session context still follows the static prefix

    async def test_lean_use_source_adds_its_section_after_the_ones_before_it(self) -> None:
        lean = _settings(prompt_style="lean")
        _, bodies = await _run(
            [_user("check ACME")],
            [_call(USE_SOURCE, name="cloud"), {"role": "assistant", "content": "Done."}],
            settings=lean,
        )
        before, after = bodies[0]["messages"][0]["content"], bodies[1]["messages"][0]["content"]
        self.assertNotIn(CLOUD_SECTION, before)
        self.assertIn(CLOUD_SECTION, after)
        # The base and Flow's section are the same bytes before and after, so that much stays cached.
        self.assertTrue(after.startswith(before.rstrip("\n")))
        self.assertEqual(bodies[0]["messages"][1:], bodies[1]["messages"][1 : len(bodies[0]["messages"])])

    async def test_lean_tools_are_routed_too(self) -> None:
        lean = _settings(prompt_style="lean")
        _, bodies = await _run([_user("MFA status at ACME")], [{"role": "assistant", "content": "None."}], settings=lean)
        names = _names(bodies[0]["tools"])
        self.assertEqual(names, [*_names(FLOW.lean_tools), *CLOUD_TOOLS, "search_knowledge", USE_SOURCE])

    async def test_flow_only_payload_has_no_routing_record(self) -> None:
        payload, bodies = await _run(
            [_user("open tickets for ACME")], [{"role": "assistant", "content": "None."}], sources=build_sources(_settings())
        )
        self.assertNotIn("routing", payload["x_tb_brain"])
        self.assertEqual(bodies[0]["tools"], openai_tools(build_sources(_settings())))


class EvalRoutingTests(unittest.TestCase):
    """scripts/eval.py scores routing on its own line from x_tb_brain.routing."""

    def setUp(self) -> None:
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "scripts" / "eval.py"
        spec = importlib.util.spec_from_file_location("tb_eval_routing", path)
        self.evalmod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.evalmod)

    def test_routing_checks(self) -> None:
        check = self.evalmod.check_routing
        trace = {"routing": route(_sources(), latest="devices offline?").record()}
        self.assertIsNone(check({}, trace))
        self.assertEqual(check({"sources": ["rmm"], "forbid_sources": ["cloud"]}, trace), [])
        self.assertEqual(check({"sources": ["cloud"]}, trace), ["source cloud not offered"])
        self.assertEqual(check({"forbid_sources": ["rmm"]}, trace), ["source rmm offered"])
        # A Flow-only server records no routing: only Flow is offered.
        self.assertEqual(check({"sources": ["flow"], "forbid_sources": ["rmm"]}, {"router": None}), [])

    def test_routing_is_summarized_separately(self) -> None:
        results = [
            {"group": "g", "pass": True, "seconds": 1.0, "routing_pass": True},
            {"group": "g", "pass": False, "seconds": 1.0, "routing_pass": False},
            {"group": "g", "pass": True, "seconds": 1.0, "routing_pass": None},
        ]
        summary = self.evalmod.summarize(results)
        self.assertEqual((summary["passed"], summary["routing"]), (2, [1, 2]))
        self.assertIn("routing           1/2", self.evalmod.format_summary(summary))
        self.assertNotIn("routing", self.evalmod.summarize(results[2:]))


if __name__ == "__main__":
    unittest.main()
