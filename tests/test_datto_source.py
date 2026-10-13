"""Datto RMM as a source: routing to it, both prompt styles with it enabled, declining Datto writes, the B3 split
between Flow's list_machines and Datto's live devices, cross-source answers, and that the Datto and Flow stub data
line up for the cross-source eval cases (eval/datto_cases.json).
"""

from __future__ import annotations

import tests._isolation  # noqa: F401  (first: hermetic settings, no network)

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from app.agent.loop import _expects_tool, _legacy_prompt, run_tool_loop
from app.agent.routing import matched_term
from app.agent.system import SYSTEM_PROMPT
from app.agent.wording import wording_for
from app.config import Settings
from app.identity import current_signed_in_email
from app.main import app
from app.sources.base import Sources
from app.sources.datto.prompt import PROMPT_SECTION as DATTO_SECTION
from app.sources.datto.spec import DATTO
from app.sources.datto.stub import StubDattoSource
from app.sources.datto.tools import LIST_MACHINES_DESCRIPTION
from app.sources.flow.fixtures import CLIENTS, CONTACTS, MACHINES, TICKETS
from app.sources.flow.spec import FLOW
from app.sources.flow.stub import StubFlowSource
from app.sources.registry import build_sources
from app.tools import ChatTurn, openai_tools, run_tool

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)
FLOW_REFUSAL = "say you only answer questions about Flow data"


def _settings(**kw: Any) -> Settings:
    return Settings(datto_mode="stub", llm_model="fake", llm_base_url="http://llm.test/v1", _env_file=None, **kw)


def _sources() -> Sources:
    return Sources([(FLOW, StubFlowSource()), (DATTO, StubDattoSource(now=lambda: NOW))])


def _call(tool: str, **arguments: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": tool, "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)}}
    ]}


async def _run(question: str, script: list[dict[str, Any]], *, settings: Settings | None = None, history=()):
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
            sources=_sources(),
            messages=[*history, {"role": "user", "content": question}],
            model=None,
            actor="test",
        )
    return payload, bodies


def _names(tools: list[dict[str, Any]]) -> list[str]:
    return [tool["function"]["name"] for tool in tools]


class RoutingTests(unittest.TestCase):
    def test_datto_questions_match_its_keywords_or_hostnames(self) -> None:
        for question, term in (
            ("Which NGMC machines are offline?", "offline"),
            ("tell me about WDON-FD01", "WDON-FD01"),
            ("Who was last logged into wdon-img-01?", "last logged"),
            ("Any BUCK machines still on Windows 10?", "Windows 10"),
            ("Which clients are worst on patching?", "patching"),
            ("Any critical alerts right now?", "alerts"),
            ("What antivirus is on WDON-FD01?", "antivirus"),
            ("wdon offline", "offline"),
        ):
            with self.subTest(question):
                self.assertEqual(matched_term(DATTO, question), term)

    def test_keywords_match_their_common_forms(self) -> None:
        for question, term in (
            ("Do we have tickets open for any of WDON's alerting machines?", "alerting"),  # eval G4
            ("Which devices alerted overnight?", "devices"),
            ("Any open alerts?", "alerts"),
            ("missing patches at WDON", "patches"),
            ("is NGMC patched", "patched"),
            ("worst on patching", "patching"),
            ("was it rebooted", "rebooted"),
            ("is it rebooting again", "rebooting"),
            ("nightly reboots", "reboots"),
            ("it restarted twice", "restarted"),
            ("list the servers", "servers"),
            ("laptops at BUCK", "laptops"),
            ("which PCs at NGMC", "PCs"),
            ("check warranties for WDON", "warranties"),
            ("serial numbers for BUCK", "serial numbers"),
            ("operating systems at NGMC", "operating systems"),
            ("which machines stopped checking in", "checking in"),
        ):
            with self.subTest(question):
                self.assertEqual(matched_term(DATTO, question), term)

    def test_keyword_forms_do_not_reach_unrelated_words(self) -> None:
        for question in (
            "Any tickets about alertness training?",
            "PCI compliance tickets for WDON",
            "Show me the patchwork ticket",
            "serverless tickets",
            "the Avenue office move",
            "Has the printer been reinstalled?",
            "Who alerted WDON about the outage?",  # -ed only where a source lists it
        ):
            with self.subTest(question):
                self.assertIsNone(matched_term(DATTO, question))

    def test_flow_questions_do_not(self) -> None:
        for question in (
            "What are my open tickets?",
            "Show me WDON-1842",
            "What machines do we have on record for WDON?",
            "How much time was logged on ZTB-1691?",
            "Any urgent tickets?",
            "Has anyone from BUCK emailed today?",
        ):
            with self.subTest(question):
                self.assertIsNone(matched_term(DATTO, question))

    def test_tool_lists_per_question(self) -> None:
        async def go():
            datto_q, _ = await _run("Which NGMC machines are offline?", [_call("datto_list_devices", client_code="NGMC", online=False)])
            flow_q, _ = await _run("Any urgent tickets?", [{"role": "assistant", "content": "None."}])
            return datto_q, flow_q

        import asyncio

        datto_q, flow_q = asyncio.run(go())
        self.assertEqual(datto_q["x_tb_brain"]["routing"]["offered"][1], {"source": "datto", "why": "keyword", "matched": "offline"})
        self.assertEqual(datto_q["x_tb_brain"]["routing"]["tool_counts"][0], len(FLOW.tools) + 6 + 1)
        self.assertEqual(flow_q["x_tb_brain"]["routing"]["hidden"], ["datto"])
        self.assertEqual(flow_q["x_tb_brain"]["routing"]["tool_counts"][0], len(FLOW.tools) + 1 + 1)  # + use_source

    def test_use_source_adds_datto_when_no_keyword_matched(self) -> None:
        import asyncio

        payload, bodies = asyncio.run(_run(
            "Show me WDON-1842 and the state of the machine on it",
            [
                _call("get_ticket_detail", ticket_label="WDON-1842"),
                _call("use_source", name="datto"),
                _call("datto_get_device", hostname="WDON-IMG-01"),
            ],
        ))
        calls = [(call["name"], call["ok"]) for call in payload["x_tb_brain"]["tool_calls"]]
        self.assertEqual(calls, [("get_ticket_detail", True), ("use_source", True), ("datto_get_device", True)])
        self.assertNotIn("datto_get_device", _names(bodies[0]["tools"]))
        # After the ticket's digest, Flow's tools are off (write the answer) but use_source stays.
        self.assertEqual(_names(bodies[1]["tools"]), ["use_source"])
        self.assertIn("datto_get_device", _names(bodies[2]["tools"]))
        # legacy: the system message picks up Datto's section once Datto joins
        self.assertNotIn(DATTO_SECTION, bodies[0]["messages"][0]["content"])
        self.assertIn(DATTO_SECTION, bodies[2]["messages"][0]["content"])
        self.assertIn("Status: OFFLINE", payload["choices"][0]["message"]["content"])

    def test_datto_is_offered_after_use_source_even_in_the_digest_reply(self) -> None:
        # Eval G2 stopped after get_ticket_detail and use_source(datto) without a Datto call. The next call does
        # offer Datto's tools (Flow's stay withdrawn after the digest), whether use_source came in its own reply or
        # in the same one as get_ticket_detail: the model chose to stop.
        import asyncio

        both = {"role": "assistant", "content": None, "tool_calls": [
            *_call("get_ticket_detail", ticket_label="WDON-1842")["tool_calls"], *_call("use_source", name="datto")["tool_calls"],
        ]}
        for style in ("legacy", "lean"):
            for script in (
                [_call("get_ticket_detail", ticket_label="WDON-1842"), _call("use_source", name="datto")],
                [both],
            ):
                with self.subTest(style=style, replies=len(script)):
                    _, bodies = asyncio.run(_run(
                        "Show me WDON-1842 and the state of the machine on it",
                        [*script, {"role": "assistant", "content": "WDON-1842 is about the imaging workstation."}],
                        settings=_settings(prompt_style=style),
                    ))
                    self.assertEqual(_names(bodies[-1]["tools"]), list(DATTO.tool_names))

    def test_a_guessed_hostname_for_a_person_leads_to_last_user(self) -> None:
        # Eval G3: the model guessed WDON-PC02 for Debe; the not-found reply names the last_user lookup.
        import asyncio

        payload, bodies = asyncio.run(_run(
            "Debe's computer is slow. Anything in Datto?",
            [_call("datto_get_device", hostname="WDON-PC02"), _call("datto_list_devices", last_user="Debe")],
        ))
        tool_reply = json.loads(bodies[1]["messages"][-1]["content"])
        self.assertIn('datto_list_devices(last_user="Debe")', tool_reply["reply"])
        self.assertIn("WDON-FD01", payload["choices"][0]["message"]["content"])

    def test_sticky_follow_up_keeps_datto(self) -> None:
        import asyncio

        history = [{"role": "user", "content": "Which NGMC machines are offline?"}, {"role": "assistant", "content": "2 devices."}]
        payload, _ = asyncio.run(_run("what about BUCK?", [_call("datto_list_devices", client_code="BUCK", online=False)], history=history))
        self.assertEqual(payload["x_tb_brain"]["routing"]["offered"][1]["why"], "sticky")
        self.assertIn("BUCK-LT01", payload["choices"][0]["message"]["content"])


class AfterDigestTests(unittest.TestCase):
    def test_flow_only_offers_nothing_after_a_digest(self) -> None:
        from app.agent.routing import route

        flow_only = Sources([(FLOW, StubFlowSource())])
        self.assertEqual(route(flow_only, latest="Show me WDON-1842").tools_after_digest("legacy"), [])
        offered = route(_sources(), latest="Show me WDON-1842 and is WDON-IMG-01 online?")
        self.assertEqual(_names(offered.tools_after_digest("lean")), list(DATTO.tool_names))


class PromptTests(unittest.TestCase):
    def test_legacy_with_only_flow_is_unchanged(self) -> None:
        flow_only = Sources([(FLOW, StubFlowSource())])
        self.assertIs(_legacy_prompt(flow_only, None), SYSTEM_PROMPT)

    def test_legacy_with_datto_drops_the_flow_only_refusal_and_adds_datto(self) -> None:
        sources = _sources()
        offered = _legacy_prompt(sources, sources.prompt_sections())
        self.assertNotIn(FLOW_REFUSAL, offered)
        self.assertIn("over Flow and Datto RMM", offered)
        self.assertIn("Only answer questions about the data Flow and Datto RMM hold", offered)
        self.assertIn(DATTO_SECTION, offered)
        self.assertLess(offered.index(DATTO_SECTION), offered.index("\nRules:\n"))
        # Datto enabled but not offered for this question: no section, and still no Flow-only refusal.
        hidden = _legacy_prompt(sources, sources.prompt_sections(only=("flow",)))
        self.assertNotIn(DATTO_SECTION, hidden)
        self.assertNotIn(FLOW_REFUSAL, hidden)

    def test_lean_has_datto_section_only_when_offered(self) -> None:
        import asyncio

        settings = _settings(prompt_style="lean")
        _, datto_bodies = asyncio.run(_run("tell me about WDON-FD01", [_call("datto_get_device", hostname="WDON-FD01")], settings=settings))
        _, flow_bodies = asyncio.run(_run("Any urgent tickets?", [{"role": "assistant", "content": "x"}], settings=settings))
        self.assertIn(DATTO_SECTION, datto_bodies[0]["messages"][0]["content"])
        self.assertNotIn(DATTO_SECTION, flow_bodies[0]["messages"][0]["content"])
        self.assertNotIn(FLOW_REFUSAL, datto_bodies[0]["messages"][0]["content"])

    def test_b3_list_machines_reads_as_flow_records_only_with_datto_enabled(self) -> None:
        with_datto = {t["function"]["name"]: t["function"] for t in openai_tools(_sources())}
        flow_only = {t["function"]["name"]: t["function"] for t in openai_tools(Sources([(FLOW, StubFlowSource())]))}
        self.assertEqual(with_datto["list_machines"]["description"], LIST_MACHINES_DESCRIPTION)
        self.assertEqual(with_datto["list_machines"]["parameters"], flow_only["list_machines"]["parameters"])
        self.assertIn("not live data", with_datto["list_machines"]["description"])
        self.assertIn("Live list", with_datto["datto_list_devices"]["description"])
        self.assertEqual(flow_only["list_machines"]["description"], next(t for t in FLOW.tools if t["function"]["name"] == "list_machines")["function"]["description"])
        lean = {t["function"]["name"]: t["function"] for t in openai_tools(_sources(), style="lean")}
        self.assertIn("recorded in Flow", lean["list_machines"]["description"])

    def test_wording_names_both_sources(self) -> None:
        words = wording_for(_sources())
        self.assertEqual(
            words.no_tool,
            "I can only answer from Flow or Datto RMM tools, and I did not get a usable result. "
            "Ask again with a person, technician, client code, or a device hostname.",
        )
        self.assertIn("devices, patches, antivirus, or alerts", words.nudge_to_use_a_tool)


class WriteDeclineTests(unittest.TestCase):
    def test_datto_writes_expect_no_tool(self) -> None:
        sources = _sources()
        for question in ("Reboot WDON-FD01", "Run the disk cleanup component on BUCK-PC03", "Approve the pending patches for NGMC",
                         "please restart BUCK-SRV01", "Resolve the alert on WDON-SRV01"):
            with self.subTest(question):
                self.assertFalse(_expects_tool(ChatTurn(user_text=question), sources))
        for question in ("Which NGMC machines need a reboot?", "Run a report of WDON's offline machines", "Remove the alert tickets"):
            with self.subTest(question):
                self.assertTrue(_expects_tool(ChatTurn(user_text=question), sources))

    def test_a_decline_is_not_nudged_into_a_tool_call(self) -> None:
        import asyncio

        for style in ("legacy", "lean"):
            with self.subTest(style):
                decline = {"role": "assistant", "content": "I can't do that; tb-brain is read-only in Datto RMM."}
                payload, bodies = asyncio.run(_run("Reboot WDON-FD01", [decline], settings=_settings(prompt_style=style)))
                self.assertEqual(len(bodies), 1)
                self.assertEqual(payload["x_tb_brain"]["tool_calls"], [])
                self.assertEqual(payload["choices"][0]["message"]["content"], decline["content"])
                system = bodies[0]["messages"][0]["content"]
                self.assertIn("cannot reboot devices", system)

    def test_no_datto_tool_can_write(self) -> None:
        self.assertEqual(_sources().write_tool_names(), ("merge_tickets",))


class CrossSourceTests(unittest.TestCase):
    def test_both_sources_replies_are_shown(self) -> None:
        import asyncio

        payload, _ = asyncio.run(_run(
            "Which open WDON tickets are for machines that are offline?",
            [
                _call("list_tickets", client_code="WDON", stage="open"),
                _call("datto_list_devices", client_code="WDON", online=False),
                {"role": "assistant", "content": "WDON-1842 is on WDON-IMG-01."},
            ],
        ))
        reply = payload["choices"][0]["message"]["content"]
        self.assertEqual(payload["x_tb_brain"]["router"], None)  # the person_ticket router did not take it
        self.assertIn("WDON-1842", reply)
        self.assertIn("WDON-IMG-01 — Desktop", reply)
        self.assertLess(reply.index("WDON-1842"), reply.index("in Datto RMM"))

    def test_one_source_still_shows_only_its_last_reply(self) -> None:
        import asyncio

        payload, _ = asyncio.run(_run(
            "Which NGMC machines are offline? And BUCK?",
            [_call("datto_list_devices", client_code="NGMC", online=False), _call("datto_list_devices", client_code="BUCK", online=False)],
        ))
        reply = payload["choices"][0]["message"]["content"]
        self.assertIn("BUCK-LT01", reply)
        self.assertNotIn("NGMC-PC07", reply)

    def test_stub_data_lines_up(self) -> None:
        datto = StubDattoSource(now=lambda: NOW)
        hosts = {device.hostname: device for device in datto.list_account_devices()}
        # Every Datto site is a Flow client.
        flow_codes = {client.client_code for client in CLIENTS}
        self.assertTrue({site.client_code for site in datto.list_sites()} <= flow_codes)
        # Ticket WDON-1842 is about WDON-IMG-01, which Flow has on record and Datto shows offline with an alert on that ticket.
        ticket = next(t for t in TICKETS if t.client_code == "WDON" and t.ticket_num == "1842")
        self.assertEqual(ticket.machine_name, "WDON-IMG-01")
        self.assertIn("WDON-IMG-01", {machine.machine_name for machine in MACHINES})
        self.assertFalse(hosts["WDON-IMG-01"].online)
        alert = next(a for a in datto.list_account_alerts() if a.hostname == "WDON-IMG-01")
        self.assertEqual(alert.ticket_number, "WDON-1842")
        # Debe Hernandez is a WDON contact and the last user on WDON-FD01.
        debe = next(contact for contact in CONTACTS if contact.full_name == "Debe Hernandez")
        self.assertEqual(debe.client_code, "WDON")
        self.assertIn("debe", hosts["WDON-FD01"].last_logged_in_user.lower())
        # Flow's WDON-FS-01 has no Datto device: the G5 comparison finds it.
        self.assertIn("WDON-FS-01", {machine.machine_name for machine in MACHINES if machine.client_code == "WDON"})
        self.assertNotIn("WDON-FS-01", hosts)


class EvalCaseTests(unittest.TestCase):
    """eval/datto_cases.json: well formed, and its expected calls, run on the stubs, give replies that pass its
    reply checks. That is what the eval scores a model against."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = json.loads((ROOT / "eval" / "datto_cases.json").read_text(encoding="utf-8"))["cases"]

    def test_shape(self) -> None:
        ids = [case["id"] for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(self.cases), 40)
        groups = {case["group"] for case in self.cases}
        self.assertTrue({"cross_source", "safety", "boundary"} <= groups)
        known = set(_sources().tool_names()) | {"search_knowledge", "use_source"}
        for case in self.cases:
            for want in case.get("expect", []):
                for option in want.get("any", [want]):
                    tools = option["tool"] if isinstance(option["tool"], list) else [option["tool"]]
                    self.assertTrue(set(tools) <= known, case["id"])
            for name in case.get("sources", []) + case.get("forbid_sources", []):
                self.assertIn(name, ("flow", "datto"))
        safety = [case for case in self.cases if case["id"] in ("H1", "H2", "H3", "H4")]
        self.assertTrue(all(case.get("expect_no_calls") for case in safety))

    @staticmethod
    def _value(name: str, wanted: Any) -> Any:
        if isinstance(wanted, list):
            wanted = wanted[0]
        if isinstance(wanted, dict) and "re" in wanted:
            literal = wanted["re"].replace("(?i)", "")
            return [literal] if name == "include" else literal
        return wanted

    def test_expected_calls_on_the_stubs_pass_the_reply_checks(self) -> None:
        sources = _sources()
        settings = _settings()
        token = current_signed_in_email.set("tseibert@techbldrs.example")
        self.addCleanup(current_signed_in_email.reset, token)
        checked = 0
        for case in self.cases:
            if not case.get("expect") or not (case.get("must_contain") or case.get("must_match")):
                continue
            with self.subTest(case["id"]):
                replies: dict[str, str] = {}
                for want in case["expect"]:
                    option = want.get("any", [want])[0]
                    arguments = {k: self._value(k, v) for k, v in option["args"].items() if self._value(k, v) is not None}
                    result = run_tool(sources=sources, name=option["tool"], arguments=arguments, actor="eval", settings=settings)
                    self.assertTrue(result.ok, f"{option['tool']}({arguments}): {result.error}")
                    spec = sources.spec_for_tool(option["tool"])
                    replies[spec.name] = result.reply or ""
                reply = "\n\n".join(replies.values())
                lowered = reply.lower()
                for needle in case.get("must_contain", []):
                    self.assertIn(needle.lower(), lowered)
                for needle in case.get("must_not_contain", []):
                    self.assertNotIn(needle.lower(), lowered)
                for pattern in case.get("must_match", []):
                    self.assertRegex(reply, pattern)
                checked += 1
        self.assertGreaterEqual(checked, 30)


class HealthTests(unittest.TestCase):
    def test_health_and_tools_report_datto(self) -> None:
        settings = _settings()
        app.state.settings = settings
        app.state.sources = build_sources(settings)
        self.addCleanup(lambda: [delattr(app.state, n) for n in ("settings", "sources") if hasattr(app.state, n)])
        client = TestClient(app)
        health = client.get("/health").json()
        self.assertEqual(health["sources"], {"flow": "stub", "datto": "datto-stub"})
        self.assertIn("datto_list_devices", health["tools"])
        self.assertEqual(health["write_tools"], ["merge_tickets"])
        self.assertNotIn("datto_api", json.dumps(health))
        tools = _names(client.get("/tools").json()["tools"])
        self.assertEqual(tools[:len(FLOW.tools)], list(FLOW.tool_names))
        self.assertEqual(tools[len(FLOW.tools):len(FLOW.tools) + 6], list(DATTO.tool_names))


if __name__ == "__main__":
    unittest.main()
