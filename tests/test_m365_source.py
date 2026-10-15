"""Microsoft 365 as a source: routing to it, both prompt styles with it enabled, declining writes and mailbox reads,
the secret guard on Microsoft 365 phrasings, cross-source answers (Flow + Datto + Microsoft 365), that the three stubs
line up for the cross-source eval cases, and eval/m365_cases.json run on the stubs.
"""

from __future__ import annotations

import tests._isolation  # noqa: F401  (first: hermetic settings, no network)

import asyncio
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
from app.agent.secrets import SECRET_REFUSAL, asks_for_secret
from app.agent.system import SYSTEM_PROMPT
from app.config import Settings
from app.identity import current_signed_in_email
from app.main import app
from app.sources.base import Sources
from app.sources.datto.spec import DATTO
from app.sources.datto.stub import StubDattoSource
from app.sources.flow.fixtures import CLIENT_DETAIL_EXTRAS, CONTACTS, TICKETS
from app.sources.flow.spec import FLOW
from app.sources.flow.stub import StubFlowSource
from app.sources.m365 import fixtures
from app.sources.m365.prompt import PROMPT_SECTION as M365_SECTION
from app.sources.m365.spec import M365
from app.sources.m365.stub import StubM365Source
from app.sources.registry import build_sources
from app.tools import ChatTurn, run_tool

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 15, 17, 0, tzinfo=timezone.utc)
FLOW_REFUSAL = "say you only answer questions about Flow data"


def _settings(**kw: Any) -> Settings:
    return Settings(m365_mode="stub", datto_mode="stub", llm_model="fake", llm_base_url="http://llm.test/v1", _env_file=None, **kw)


def _sources(*, datto: bool = True) -> Sources:
    enabled = [(FLOW, StubFlowSource())]
    if datto:
        enabled.append((DATTO, StubDattoSource(now=lambda: NOW)))
    enabled.append((M365, StubM365Source(now=lambda: NOW)))
    return Sources(enabled)


def _call(tool: str, **arguments: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": tool, "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)}}
    ]}


def _run(question: str, script: list[dict[str, Any]], *, settings: Settings | None = None, history=(), datto: bool = True):
    bodies: list[dict[str, Any]] = []
    replies = iter(script)

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        message = next(replies, {"role": "assistant", "content": "Done."})
        return httpx.Response(200, json={"id": "x", "choices": [{"index": 0, "message": message}]})

    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    async def go():
        with mock.patch("app.agent.loop.httpx.AsyncClient", lambda **kw: real(transport=transport, **kw)):
            return await run_tool_loop(
                settings=settings or _settings(),
                sources=_sources(datto=datto),
                messages=[*history, {"role": "user", "content": question}],
                model=None,
                actor="test",
            )

    return asyncio.run(go()), bodies


def _names(tools: list[dict[str, Any]]) -> list[str]:
    return [tool["function"]["name"] for tool in tools]


class RoutingTests(unittest.TestCase):
    def test_m365_questions_match_its_keywords_or_patterns(self) -> None:
        for question, term in (
            ("How many users does WDON have?", "users"),
            ("Is Debe's account at WDON disabled?", "disabled"),
            ("Who at WDON hasn't signed in for 30 days?", "signed in"),
            ("Why is Debe getting blocked when she logs in?", "logs in"),
            ("Who at WDON doesn't have MFA set up?", "MFA"),
            ("Any failed sign-ins for WDON?", "sign-ins"),
            ("Debe is locked out", "locked out"),
            ("What licenses does BUCK have?", "licenses"),
            ("is anyone unlicensed at NGMC", "unlicensed"),
            ("Does NGMC have Business Premium?", "Business Premium"),
            ("Who are the global admins at WDON?", "global admins"),
            ("How full is Debe's mailbox?", "mailbox"),
            ("Which BUCK mailboxes are almost full?", "mailboxes"),
            ("any forwarding rules at WDON", "forwarding"),
            ("Which WDON devices are non-compliant?", "non-compliant"),
            ("What's WDON's Secure Score?", "Secure Score"),
            ("What conditional access policies does NGMC have?", "conditional access"),
            ("Is Microsoft 365 having an outage?", "Microsoft 365"),
            ("anything in O365 for BUCK", "O365"),
            ("Is Entra syncing?", "Entra"),
            ("Where did jsmith@buckheadpeds.example last sign in from?", "sign in"),
            ("tell me about jsmith@buckheadpeds.example", "jsmith@buckheadpeds.example"),
            ("When was the user for Front Desk at WDON created?", "user"),
            ("Disable jsmith at BUCK", "Disable"),
        ):
            with self.subTest(question):
                self.assertEqual(matched_term(M365, question), term)

    def test_flow_and_datto_questions_do_not(self) -> None:
        for question in (
            "What are my open tickets?", "Show me WDON-1842", "Any urgent tickets?", "Has anyone from WDON emailed today?",
            "When did Debe Hernandez last email us?", "Which NGMC machines are offline?", "Tell me about WDON-FD01",
            "Who was last logged into BUCK-PC03?", "password reset tickets for WDON", "Add a note to ZTB-1680",
            "Remove the alert tickets", "How much time was logged on ZTB-1691?",
        ):
            with self.subTest(question):
                self.assertIsNone(matched_term(M365, question))

    def test_no_eval_or_live_flow_question_routes_to_m365(self) -> None:
        texts = []
        for name in ("flow_cases.json", "questions.json", "datto_cases.json"):
            for case in json.loads((ROOT / "eval" / name).read_text(encoding="utf-8"))["cases"]:
                texts += [m["content"] for m in case["messages"] if m["role"] == "user" and isinstance(m.get("content"), str)]
        texts += [line.strip() for line in (ROOT / "eval" / "live_questions.txt").read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.startswith("#")]
        hits = {text: matched_term(M365, text) for text in texts if matched_term(M365, text)}
        self.assertEqual(hits, {})
        # "our tenant" is TechBldrs' own (Flow mail, answered by a router); a client's tenant is Microsoft 365's.
        self.assertIsNone(matched_term(M365, "Show all emails from WDON to our tenant"))
        self.assertEqual(matched_term(M365, "What changed in WDON's tenant this week?"), "tenant")

    def test_tool_lists_per_question(self) -> None:
        m365, _ = _run("Who at WDON doesn't have MFA set up?", [_call("m365_mfa_report", client_code="WDON", registered=False)])
        flow, _ = _run("Any urgent tickets?", [{"role": "assistant", "content": "None."}])
        offered = m365["x_tb_brain"]["routing"]["offered"]
        self.assertEqual(offered[1], {"source": "m365", "why": "keyword", "matched": "MFA"})
        self.assertEqual(m365["x_tb_brain"]["routing"]["hidden"], ["datto"])
        self.assertEqual(m365["x_tb_brain"]["routing"]["tool_counts"][0], len(FLOW.tools) + 11 + 1 + 1)  # + knowledge, use_source
        self.assertEqual(flow["x_tb_brain"]["routing"]["hidden"], ["datto", "m365"])
        self.assertEqual(flow["x_tb_brain"]["routing"]["tool_counts"][0], len(FLOW.tools) + 1 + 1)

    def test_use_source_lists_m365(self) -> None:
        _, bodies = _run("Show me WDON-2621", [{"role": "assistant", "content": "x"}])
        use_source = next(t for t in bodies[0]["tools"] if t["function"]["name"] == "use_source")
        self.assertIn("m365: Microsoft 365", use_source["function"]["description"])
        self.assertEqual(use_source["function"]["parameters"]["properties"]["name"]["enum"], ["datto", "m365"])

    def test_sticky_follow_up_keeps_m365(self) -> None:
        history = [{"role": "user", "content": "Who at WDON doesn't have MFA set up?"}, {"role": "assistant", "content": "4 don't."}]
        payload, _ = _run("what about BUCK?", [_call("m365_mfa_report", client_code="BUCK")], history=history)
        self.assertEqual(payload["x_tb_brain"]["routing"]["offered"][1]["why"], "sticky")
        self.assertIn("Amy Nguyen", payload["choices"][0]["message"]["content"])


class PromptTests(unittest.TestCase):
    def test_legacy_names_every_source_and_adds_the_section_when_offered(self) -> None:
        sources = _sources()
        offered = _legacy_prompt(sources, sources.prompt_sections())
        self.assertIn("over Flow, Datto RMM, and Microsoft 365", offered)
        self.assertNotIn(FLOW_REFUSAL, offered)
        self.assertIn(M365_SECTION, offered)
        self.assertLess(offered.index(M365_SECTION), offered.index("\nRules:\n"))
        hidden = _legacy_prompt(sources, sources.prompt_sections(only=("flow",)))
        self.assertNotIn(M365_SECTION, hidden)
        only_m365 = _sources(datto=False)
        self.assertIn("over Flow and Microsoft 365", _legacy_prompt(only_m365, None))
        self.assertIs(_legacy_prompt(Sources([(FLOW, StubFlowSource())]), None), SYSTEM_PROMPT)

    def test_lean_has_the_section_only_when_offered(self) -> None:
        settings = _settings(prompt_style="lean")
        _, m365 = _run("What's WDON's Secure Score?", [_call("m365_security", client_code="WDON", view="secure_score")], settings=settings)
        _, flow = _run("Any urgent tickets?", [{"role": "assistant", "content": "x"}], settings=settings)
        self.assertIn(M365_SECTION, m365[0]["messages"][0]["content"])
        self.assertNotIn(M365_SECTION, flow[0]["messages"][0]["content"])
        lean = {t["function"]["name"]: t["function"] for t in m365[0]["tools"]}
        self.assertEqual(lean["m365_users"]["parameters"], M365.tools[0]["function"]["parameters"])
        self.assertLess(len(lean["m365_users"]["description"]), len(M365.tools[0]["function"]["description"]))

    def test_no_tool_takes_a_tenant_id(self) -> None:
        for tool in (*M365.tools, *M365.lean_tools):
            self.assertNotIn("tenant_id", json.dumps(tool["function"]["parameters"]))


class DeclineTests(unittest.TestCase):
    WRITES = (
        "Reset Debe's password", "Disable jsmith at BUCK", "Add a Business Premium license to Debe", "Revoke Debe's sessions",
        "Add Debe to the Finance group", "Unlock Debe's account", "please block sign-in for jsmith",
        "Remove Debe's license", "Turn on MFA for Thomas Carter", "Assign an E3 to Gary", "Convert frontdesk to a shared mailbox",
    )
    MAIL_READS = (
        "Show me the emails in Debe's inbox", "Read Debe's email", "What's in Debe's inbox?", "search Debe's mailbox for invoices",
        "what is in the billing mailbox", "Give me the contents of Chris's inbox",
    )

    def test_writes_and_mail_reads_expect_no_tool(self) -> None:
        sources = _sources()
        for question in (*self.WRITES, *self.MAIL_READS):
            with self.subTest(question):
                self.assertFalse(_expects_tool(ChatTurn(user_text=question), sources))
                self.assertIsNotNone(matched_term(M365, question))  # routed, so the section saying to decline is there
        for question in ("How full is Debe's mailbox?", "Who has a forwarding rule at WDON?", "Is MFA enforced at BUCK?",
                         "Which WDON users are shared mailboxes?", "Show me Debe's mailbox size", "Remove the alert tickets"):
            with self.subTest(question):
                self.assertTrue(_expects_tool(ChatTurn(user_text=question), sources) or question == "Remove the alert tickets")

    def test_a_decline_is_not_nudged_into_a_tool_call(self) -> None:
        for style in ("legacy", "lean"):
            for question in ("Reset Debe's password", "Show me the emails in Debe's inbox"):
                with self.subTest(style=style, question=question):
                    decline = {"role": "assistant", "content": "I can't do that; tb-brain is read-only in Microsoft 365."}
                    payload, bodies = _run(question, [decline], settings=_settings(prompt_style=style))
                    self.assertEqual(len(bodies), 1)
                    self.assertEqual(payload["x_tb_brain"]["tool_calls"], [])
                    self.assertEqual(payload["choices"][0]["message"]["content"], decline["content"])
                    system = bodies[0]["messages"][0]["content"]
                    self.assertIn("you cannot reset passwords", system)
                    self.assertIn("never reads client mailboxes", system)

    def test_secret_guard_covers_m365_phrasings(self) -> None:
        for question in ("what's Debe's password", "What's the global admin password for WDON?", "send me jsmith's password",
                         "Debe's password?", "what is the app secret for Scan2Mail", "give me the client secret for BUCK's app"):
            with self.subTest(question):
                self.assertTrue(asks_for_secret(question))
        for question in ("Reset Debe's password", "Any app secrets expiring soon at BUCK?", "When did Debe last change her password?",
                         "Is Debe's password expired?"):
            with self.subTest(question):
                self.assertFalse(asks_for_secret(question))
        payload, bodies = _run("What's the global admin password for WDON?", [])
        self.assertEqual(bodies, [])
        self.assertEqual(payload["choices"][0]["message"]["content"], SECRET_REFUSAL)

    def test_no_m365_tool_can_write(self) -> None:
        self.assertEqual(_sources().write_tool_names(), ("merge_tickets",))
        for tool in M365.tools:
            self.assertIn("Read-only", tool["function"]["description"])


class CrossSourceTests(unittest.TestCase):
    def test_three_sources_replies_are_shown(self) -> None:
        payload, _ = _run(
            "Debe Hernandez can't sign in and her PC is acting up. Check Datto, Microsoft 365 and her open tickets.",
            [
                _call("list_tickets", requestor="Debe Hernandez"),
                _call("datto_list_devices", last_user="Debe"),
                _call("m365_signins", user="Debe", status="failure"),
                {"role": "assistant", "content": "Debe is locked out."},
            ],
        )
        reply = payload["choices"][0]["message"]["content"]
        self.assertIn("WDON-1842", reply)
        self.assertIn("WDON-FD01", reply)
        self.assertIn("account locked", reply)
        self.assertLess(reply.index("WDON-1842"), reply.index("in Datto RMM"))
        self.assertLess(reply.index("in Datto RMM"), reply.index("Failed sign-ins for Debe Hernandez"))
        offered = [item["source"] for item in payload["x_tb_brain"]["routing"]["offered"]]
        self.assertEqual(offered, ["flow", "datto", "m365"])

    def test_stub_data_lines_up(self) -> None:
        tenants = fixtures.tenants(NOW)
        # Flow's stub carries each stub tenant's id; other tenant ids (WDLB, NRTH) have no stub tenant (no consent).
        for code, tenant_id in (("WDON", fixtures.WDON_TENANT), ("BUCK", fixtures.BUCK_TENANT), ("NGMC", fixtures.NGMC_TENANT)):
            self.assertEqual(CLIENT_DETAIL_EXTRAS[code]["m365_tenant_id"], tenant_id)
            self.assertEqual(tenants[tenant_id]["client_code"], code)
        self.assertNotIn(CLIENT_DETAIL_EXTRAS["NRTH"]["m365_tenant_id"], tenants)
        wdon = {user["userPrincipalName"]: user for user in tenants[fixtures.WDON_TENANT]["users"]}
        # Every WDON contact in Flow is a WDON user, by email.
        for contact in (c for c in CONTACTS if c.client_code == "WDON"):
            self.assertIn(contact.email_1, wdon, contact.full_name)
        # Debe: Flow contact, Datto last user on WDON-FD01, enrolled WDON-FD01 in Intune, locked out in sign-ins.
        datto = {device.hostname: device for device in StubDattoSource(now=lambda: NOW).list_account_devices()}
        self.assertIn("debe.hernandez", datto["WDON-FD01"].last_logged_in_user)
        devices = {device["deviceName"]: device for device in tenants[fixtures.WDON_TENANT]["devices"]}
        self.assertEqual(devices["WDON-FD01"]["userPrincipalName"], "debe@westerndental.example")
        self.assertTrue(any(row["userPrincipalName"] == "debe@westerndental.example" and row["status"]["errorCode"] == 50053
                            for row in tenants[fixtures.WDON_TENANT]["sign_ins"]))
        # WDON-OP03: offline in Datto, not compliant in Intune.
        self.assertFalse(datto["WDON-OP03"].online)
        self.assertEqual(devices["WDON-OP03"]["complianceState"], "noncompliant")
        # Tickets: WDON-1830 Chris Patel's password reset; WDON-2619 B Ellerby's email (account made the next day);
        # WDON-2621 the payroll mailbox; WDON-2619's requestor Thomas Carter has no MFA.
        by_num = {t.ticket_num: t for t in TICKETS if t.client_code == "WDON"}
        self.assertEqual(by_num["1830"].requestor_text, "Chris Patel")
        self.assertIn("Ellerby", by_num["2619"].subject)
        self.assertGreater(wdon["bellerby@westerndental.example"]["createdDateTime"], by_num["2619"].created_at.isoformat())
        self.assertIn("Payroll", by_num["2621"].subject)
        self.assertEqual(wdon["payroll@westerndental.example"]["_stub"]["purpose"], "shared")
        self.assertEqual(wdon["thomas.carter@westerndental.example"]["_stub"]["methods"], ("password",))
        self.assertEqual(by_num["2619"].requestor_text, "Thomas Carter")


class EvalCaseTests(unittest.TestCase):
    """eval/m365_cases.json: well formed, and its expected calls, run on the stubs, give replies that pass its reply
    checks. That is what the eval scores a model against."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = json.loads((ROOT / "eval" / "m365_cases.json").read_text(encoding="utf-8"))["cases"]

    def test_shape(self) -> None:
        ids = [case["id"] for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(self.cases), 40)
        self.assertTrue({"cross_source", "safety", "boundary"} <= {case["group"] for case in self.cases})
        known = set(_sources().tool_names()) | {"search_knowledge", "use_source"}
        for case in self.cases:
            for want in case.get("expect", []):
                for option in want.get("any", [want]):
                    self.assertIn(option["tool"], known, case["id"])
            for name in case.get("sources", []) + case.get("forbid_sources", []):
                self.assertIn(name, ("flow", "datto", "m365"))
        three_way = [case["id"] for case in self.cases if set(case.get("sources", [])) == {"flow", "datto", "m365"}]
        self.assertGreaterEqual(len(three_way), 2)
        for case_id in ("I1", "I2", "I3", "I4", "I10", "I11", "I12", "I14", "E4"):
            self.assertTrue(next(c for c in self.cases if c["id"] == case_id).get("expect_no_calls"), case_id)

    def test_no_tool_safety_questions_are_declines_or_guarded(self) -> None:
        sources = _sources()
        for case in self.cases:
            if case.get("expect_no_calls"):
                text = case["messages"][-1]["content"]
                with self.subTest(case["id"]):
                    self.assertTrue(asks_for_secret(text) or not _expects_tool(ChatTurn(user_text=text), sources))

    @staticmethod
    def _value(name: str, wanted: Any) -> Any:
        if isinstance(wanted, list):
            wanted = wanted[0]
        if wanted == "*":
            return "Global Administrator" if name == "role" else wanted
        if isinstance(wanted, dict) and "re" in wanted:
            literal = wanted["re"].replace("(?i)", "").replace("^", "").replace(" ?", " ")
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
                    if not result.ok:
                        self.assertTrue(case["id"].startswith("I"), f"{option['tool']}({arguments}): {result.error}")
                    replies[sources.spec_for_tool(option["tool"]).name] = result.reply or result.error or ""
                reply = "\n\n".join(replies.values())
                for needle in case.get("must_contain", []):
                    self.assertIn(needle.lower(), reply.lower())
                for needle in case.get("must_not_contain", []):
                    self.assertNotIn(needle.lower(), reply.lower())
                for pattern in case.get("must_match", []):
                    self.assertRegex(reply, pattern)
                for pattern in case.get("must_not_match", []):
                    self.assertNotRegex(reply, pattern)
                checked += 1
        self.assertGreaterEqual(checked, 50)

    def test_routing_expectations_hold_on_the_first_message(self) -> None:
        for case in self.cases:
            text = " ".join(m["content"] for m in case["messages"] if m["role"] == "user")
            with self.subTest(case["id"]):
                if "m365" in case.get("forbid_sources", []):
                    self.assertIsNone(matched_term(M365, text))
                if "m365" in case.get("sources", []) and case["id"] not in ("I9",):
                    self.assertIsNotNone(matched_term(M365, case["messages"][-1]["content"]))


class HealthTests(unittest.TestCase):
    def test_health_and_tools_report_m365(self) -> None:
        settings = _settings(m365_client_secret="never-shown-secret")
        app.state.settings = settings
        app.state.sources = build_sources(settings)
        self.addCleanup(lambda: [delattr(app.state, n) for n in ("settings", "sources") if hasattr(app.state, n)])
        client = TestClient(app)
        health = client.get("/health").json()
        self.assertEqual(health["sources"], {"flow": "stub", "datto": "datto-stub", "m365": "m365-stub"})
        self.assertIn("m365_mfa_report", health["tools"])
        self.assertEqual(health["write_tools"], ["merge_tickets"])
        self.assertNotIn("never-shown-secret", json.dumps(health))
        tools = _names(client.get("/tools").json()["tools"])
        self.assertEqual(tools[len(FLOW.tools) + 6:len(FLOW.tools) + 17], list(M365.tool_names))


if __name__ == "__main__":
    unittest.main()
