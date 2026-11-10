"""The MCP endpoint (app/mcp/): which tools each endpoint offers, how results read as text, the bearer check in every
mode, and that the signed-in technician from an Entra token reaches the audit row and Flow's identity."""

from __future__ import annotations

import tests._isolation  # noqa: F401  (first: hermetic settings, no network)

import json
import os
import time
import unittest
from typing import Any
from unittest import mock

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app
from app.mcp.auth import (
    ApiKeyVerifier,
    AuthFailed,
    EntraVerifier,
    McpAuthMisconfigured,
    NoAuthVerifier,
    build_verifier,
)
from app.mcp.server import ENDPOINTS
from app.mcp.tools import FOOTER_LEAD, TRUNCATED, offered_names, offered_specs, result_text, to_mcp_tool
from app.sources.datto.tools import TOOL_NAMES as DATTO_TOOL_NAMES
from app.sources.registry import build_sources
from app.tools.base import ToolResult

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
AUDIENCE = f"api://{CLIENT_ID}"
STUB_TECH = "tseibert@techbldrs.example"  # the stub Flow technician scripts/ask.py signs in as
API_KEY = "test-api-key-0123456789abcdef"
HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}

_PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _PRIVATE.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)


class _SigningKey:
    def __init__(self) -> None:
        self.key = _PRIVATE.public_key()


class FakeJwkClient:
    """Stands in for PyJWKClient: hands out the test key without fetching anything."""

    def get_signing_key_from_jwt(self, token: str) -> _SigningKey:
        return _SigningKey()


def make_token(**overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "tid": TENANT,
        "aud": AUDIENCE,
        "scp": "access_as_user",
        "preferred_username": STUB_TECH,
        "iat": now,
        "nbf": now,
        "exp": now + 600,
    }
    claims.update(overrides)
    claims = {key: value for key, value in claims.items() if value is not None}
    return jwt.encode(claims, _PRIVATE_PEM, algorithm="RS256", headers={"kid": "test"})


def test_verifier() -> EntraVerifier:
    return EntraVerifier(TENANT, (AUDIENCE, CLIENT_ID), "access_as_user", jwk_client=FakeJwkClient())


def rpc(client: TestClient, path: str, method: str, params: dict[str, Any] | None = None, token: str | None = None):
    headers = dict(HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers=headers)


def tool_text(response) -> str:
    body = response.json()
    assert "result" in body, body
    return "\n".join(block["text"] for block in body["result"]["content"] if block.get("type") == "text")


class OfferedToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = build_sources(Settings(datto_mode="stub", _env_file=None))

    def test_all_offers_every_enabled_sources_read_tools_and_nothing_else(self) -> None:
        names = offered_names(self.sources, "all")
        self.assertIn("list_tickets", names)
        self.assertIn("datto_list_devices", names)
        self.assertNotIn("merge_tickets", names)  # the one write tool: chat-only
        self.assertNotIn("search_knowledge", names)  # parked: no embeddings host
        self.assertNotIn("use_source", names)
        self.assertEqual(len(names), len(set(names)))

    def test_per_source_endpoints(self) -> None:
        self.assertEqual(offered_names(self.sources, "datto"), DATTO_TOOL_NAMES)
        flow = offered_names(self.sources, "flow")
        self.assertIn("list_tickets", flow)
        self.assertTrue(all(not name.startswith("datto_") for name in flow))
        self.assertEqual(offered_names(self.sources, "m365"), ())  # not enabled
        self.assertEqual(offered_names(self.sources, "nope"), ())

    def test_datto_rewords_list_machines_as_in_the_chat_loop(self) -> None:
        spec = next(spec for spec in offered_specs(self.sources, "all") if spec["name"] == "list_machines")
        self.assertIn("datto_ tools", spec["description"])  # Datto's wording of Flow's tool, as in the chat loop

    def test_lean_style_is_offered_on_request(self) -> None:
        legacy = {spec["name"]: spec["description"] for spec in offered_specs(self.sources, "flow", style="legacy")}
        lean = {spec["name"]: spec["description"] for spec in offered_specs(self.sources, "flow", style="lean")}
        self.assertEqual(set(legacy), set(lean))
        self.assertLess(sum(map(len, lean.values())), sum(map(len, legacy.values())))

    def test_mcp_tool_shape(self) -> None:
        spec = next(spec for spec in offered_specs(self.sources, "all") if spec["name"] == "list_tickets")
        tool = to_mcp_tool(spec)
        wire = tool.model_dump(by_alias=True, exclude_none=True)
        self.assertEqual(wire["name"], "list_tickets")
        self.assertEqual(wire["inputSchema"]["type"], "object")
        self.assertIn("client_code", wire["inputSchema"]["properties"])
        self.assertTrue(wire["annotations"]["readOnlyHint"])
        self.assertFalse(wire["annotations"]["destructiveHint"])

    def test_endpoints_cover_every_known_source(self) -> None:
        self.assertEqual(ENDPOINTS, ("all", "flow", "datto", "m365"))


class ResultTextTests(unittest.TestCase):
    def test_reply_is_shown_as_is_with_note(self) -> None:
        result = ToolResult(tool="t", source="stub", reply="Open tickets for WDON\n\n- WDON-1842 — Printer", note="2 ticket(s).")
        self.assertEqual(result_text(result), "Open tickets for WDON\n\n- WDON-1842 — Printer\n\nNote: 2 ticket(s).")

    def test_note_already_in_the_reply_is_not_repeated(self) -> None:
        result = ToolResult(tool="t", source="stub", reply="3 open ticket(s)\n\n- WDON-1842\n\nShowing 3 tickets.", note="Showing 3 tickets.")
        self.assertEqual(result_text(result).count("Showing 3 tickets."), 1)

    def test_digest_then_exact_footer(self) -> None:
        result = ToolResult(tool="t", source="stub", digest="Worked on WDON-1842: printer jam.", footer="Total: 3h 15m across 2 entries.")
        text = result_text(result)
        self.assertTrue(text.startswith("Worked on WDON-1842"))
        self.assertIn(FOOTER_LEAD, text)
        self.assertTrue(text.endswith("Total: 3h 15m across 2 entries."))

    def test_header_comes_first(self) -> None:
        result = ToolResult(tool="t", source="stub", header="Ticket: WDON-1842\nStatus: New", digest="The printer jams daily.")
        self.assertTrue(result_text(result).startswith("Ticket: WDON-1842"))

    def test_chunks_are_flattened_without_repeating_the_digest(self) -> None:
        lines = "Most common titles:\n- printer (4)"
        result = ToolResult(
            tool="t",
            source="stub",
            digest=lines,
            reduce_prefix=lines,
            reduce_suffix="Now write three short paragraphs.",
            chunks=[{"label": "Oct 2026", "prompt": "Tickets: WDON-1842 printer jam."}, {"label": "Nov 2026", "prompt": "Tickets: WDON-1850 VPN."}],
            footer="Tickets counted: 12.",
        )
        text = result_text(result)
        self.assertEqual(text.count("Most common titles:"), 1)
        self.assertIn("Oct 2026:\nTickets: WDON-1842 printer jam.", text)
        self.assertIn("Now write three short paragraphs.", text)
        self.assertLess(text.index("Oct 2026"), text.index("Now write"))
        self.assertTrue(text.endswith("Tickets counted: 12."))

    def test_data_only_result_is_json(self) -> None:
        result = ToolResult(tool="t", source="stub", data=[{"id": 7, "full_name": "Debe Example"}])
        self.assertEqual(json.loads(result_text(result)), [{"id": 7, "full_name": "Debe Example"}])

    def test_refusal_shows_its_reply_and_failure_its_error(self) -> None:
        self.assertEqual(result_text(ToolResult(ok=False, tool="t", source="stub", error="x", reply="No client matches ZZZZ.")), "No client matches ZZZZ.")
        self.assertEqual(result_text(ToolResult(ok=False, tool="t", source="stub", error="Invalid arguments: [...]")), "Invalid arguments: [...]")

    def test_long_output_is_capped(self) -> None:
        text = result_text(ToolResult(tool="t", source="stub", reply="x" * 100_000))
        self.assertTrue(text.endswith(TRUNCATED))
        self.assertLess(len(text), 50_000)


class VerifierTests(unittest.TestCase):
    def test_entra_token_names_the_technician(self) -> None:
        principal = test_verifier().verify(make_token())
        self.assertEqual((principal.label, principal.email, principal.verified, principal.mode), (STUB_TECH, STUB_TECH, True, "entra"))

    def test_v1_issuer_and_upn_claim_are_accepted(self) -> None:
        token = make_token(iss=f"https://sts.windows.net/{TENANT}/", preferred_username=None, upn="Tech@TechBldrs.example")
        self.assertEqual(test_verifier().verify(token).email, "tech@techbldrs.example")

    def test_entra_refusals(self) -> None:
        verifier = test_verifier()
        cases = {
            "other tenant": make_token(tid="99999999-0000-0000-0000-000000000000", iss="https://login.microsoftonline.com/99999999-0000-0000-0000-000000000000/v2.0"),
            "other issuer": make_token(iss="https://login.microsoftonline.com/common/v2.0"),
            "other audience": make_token(aud="api://someone-else"),
            "app-only": make_token(scp=None, roles=["Reader"]),
            "wrong scope": make_token(scp="other.scope"),
            "expired": make_token(exp=int(time.time()) - 3600),
            "no user": make_token(preferred_username=None),
            "garbage": "not.a.token",
        }
        for label, token in cases.items():
            with self.subTest(label), self.assertRaises(AuthFailed):
                verifier.verify(token)

    def test_api_key_and_off(self) -> None:
        verifier = ApiKeyVerifier(API_KEY)
        self.assertEqual(verifier.verify(API_KEY).label, "mcp-api-key")
        self.assertFalse(verifier.verify(API_KEY).verified)
        with self.assertRaises(AuthFailed):
            verifier.verify("wrong")
        self.assertEqual(NoAuthVerifier("lab").verify("").label, "lab")

    def test_build_verifier_from_settings(self) -> None:
        self.assertIsInstance(build_verifier(Settings(mcp_auth="entra", mcp_entra_tenant_id=TENANT, mcp_entra_audience=AUDIENCE, _env_file=None)), EntraVerifier)
        self.assertIsInstance(build_verifier(Settings(mcp_auth="api_key", mcp_api_key=API_KEY, _env_file=None)), ApiKeyVerifier)
        self.assertIsInstance(build_verifier(Settings(mcp_auth="off", _env_file=None)), NoAuthVerifier)
        with self.assertRaises(McpAuthMisconfigured):
            build_verifier(Settings(mcp_auth="entra", _env_file=None))  # no tenant, no audience
        with self.assertRaises(McpAuthMisconfigured):
            build_verifier(Settings(mcp_auth="api_key", mcp_api_key="short", _env_file=None))
        with self.assertRaises(McpAuthMisconfigured):
            build_verifier(Settings(mcp_auth="off", brain_host="0.0.0.0", _env_file=None))
        with self.assertRaises(ValueError):
            Settings(mcp_auth="basic", _env_file=None)


class _AppTestCase(unittest.TestCase):
    env: dict[str, str] = {}

    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {"DATTO_MODE": "stub", **self.env})
        self._env.start()
        get_settings.cache_clear()
        self._cm = TestClient(app)
        self.client = self._cm.__enter__()

    def tearDown(self) -> None:
        self._cm.__exit__(None, None, None)
        self._env.stop()
        get_settings.cache_clear()


class ApiKeyEndpointTests(_AppTestCase):
    env = {"MCP_AUTH": "api_key", "MCP_API_KEY": API_KEY}

    def test_missing_or_wrong_token_is_401(self) -> None:
        missing = rpc(self.client, "/mcp", "tools/list")
        self.assertEqual(missing.status_code, 401)
        self.assertIn("Bearer", missing.headers.get("www-authenticate", ""))
        self.assertEqual(rpc(self.client, "/mcp", "tools/list", token="nope").status_code, 401)

    def test_tools_list_on_each_endpoint(self) -> None:
        for path, expected in (
            ("/mcp", lambda names: "list_tickets" in names and "datto_alerts" in names),
            ("/mcp/all", lambda names: "list_tickets" in names and "datto_alerts" in names),
            ("/mcp/flow", lambda names: "list_tickets" in names and "datto_alerts" not in names),
            ("/mcp/datto", lambda names: set(names) == set(DATTO_TOOL_NAMES)),
            ("/mcp/m365", lambda names: names == []),
        ):
            with self.subTest(path):
                response = rpc(self.client, path, "tools/list", token=API_KEY)
                self.assertEqual(response.status_code, 200, response.text)
                tools = response.json()["result"]["tools"]
                names = [tool["name"] for tool in tools]
                self.assertTrue(expected(names), names)
                self.assertNotIn("merge_tickets", names)
                for tool in tools:
                    self.assertIn("inputSchema", tool)
                    self.assertTrue(tool["description"])

    def test_tool_call_runs_and_is_audited_as_the_api_key(self) -> None:
        with mock.patch("app.tools.log_tool_call") as audit:
            response = rpc(self.client, "/mcp", "tools/call", {"name": "list_tickets", "arguments": {"client_code": "WDON", "stage": "open", "limit": 5}}, token=API_KEY)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()["result"]
        self.assertFalse(body.get("isError", False))
        self.assertIn("WDON-", tool_text(response))
        audit.assert_called_once()
        self.assertEqual(audit.call_args.kwargs["actor"], "mcp-api-key")
        self.assertFalse(audit.call_args.kwargs["actor_verified"])
        self.assertEqual(audit.call_args.kwargs["tool"], "list_tickets")

    def test_datto_tool_through_its_own_endpoint(self) -> None:
        response = rpc(self.client, "/mcp/datto", "tools/call", {"name": "datto_list_devices", "arguments": {"client_code": "WDON"}}, token=API_KEY)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("WDON-FD01", tool_text(response))

    def test_refusal_is_an_answer_not_an_error(self) -> None:
        response = rpc(self.client, "/mcp", "tools/call", {"name": "datto_get_device", "arguments": {"hostname": "WDON-XX99"}}, token=API_KEY)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["result"].get("isError", False))
        self.assertIn("no device named WDON-XX99", tool_text(response))

    def test_bad_arguments_and_unknown_tools_are_errors_the_model_can_read(self) -> None:
        bad = rpc(self.client, "/mcp", "tools/call", {"name": "datto_list_devices", "arguments": {"client_code": "WDON", "offline_days": "a while"}}, token=API_KEY)
        self.assertEqual(bad.status_code, 200, bad.text)
        self.assertTrue(bad.json()["result"]["isError"])
        self.assertIn("Invalid arguments", tool_text(bad))
        wrong_endpoint = rpc(self.client, "/mcp/flow", "tools/call", {"name": "datto_alerts", "arguments": {"client_code": "WDON"}}, token=API_KEY)
        self.assertEqual(wrong_endpoint.status_code, 200, wrong_endpoint.text)
        self.assertTrue(wrong_endpoint.json()["result"]["isError"])
        self.assertIn("Unknown tool", tool_text(wrong_endpoint))
        write = rpc(self.client, "/mcp", "tools/call", {"name": "merge_tickets", "arguments": {}}, token=API_KEY)
        self.assertTrue(write.json()["result"]["isError"])

    def test_me_cannot_be_resolved_without_a_signed_in_technician(self) -> None:
        response = rpc(self.client, "/mcp", "tools/call", {"name": "list_tickets", "arguments": {"assignee_code": "me", "stage": "open"}}, token=API_KEY)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("WDON-", tool_text(response))

    def test_health_reports_the_mcp_side(self) -> None:
        health = self.client.get("/health").json()
        self.assertEqual(health["mcp"]["auth"], "api_key")
        self.assertIn("datto_alerts", health["mcp"]["endpoints"]["/mcp/datto"])
        self.assertNotIn("merge_tickets", health["mcp"]["endpoints"]["/mcp"])


class EntraEndpointTests(_AppTestCase):
    env = {"MCP_AUTH": "entra", "MCP_ENTRA_TENANT_ID": TENANT, "MCP_ENTRA_AUDIENCE": f"{AUDIENCE},{CLIENT_ID}"}

    def setUp(self) -> None:
        self._verifier = mock.patch("app.mcp.server.build_verifier", return_value=test_verifier())
        self._verifier.start()
        super().setUp()

    def tearDown(self) -> None:
        super().tearDown()
        self._verifier.stop()

    def test_signed_in_technician_is_the_actor_and_me(self) -> None:
        with mock.patch("app.tools.log_tool_call") as audit:
            response = rpc(self.client, "/mcp", "tools/call", {"name": "list_tickets", "arguments": {"assignee_code": "me", "stage": "open"}}, token=make_token())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["result"].get("isError", False), tool_text(response))
        self.assertEqual(audit.call_args.kwargs["actor"], STUB_TECH)
        self.assertTrue(audit.call_args.kwargs["actor_verified"])
        self.assertIn("-", tool_text(response))  # ticket labels for the stub technician's open tickets

    def test_app_only_and_foreign_tokens_are_refused(self) -> None:
        for label, token in (("app-only", make_token(scp=None, roles=["Reader"])), ("other tenant", make_token(tid="0" * 36)), ("expired", make_token(exp=int(time.time()) - 60))):
            with self.subTest(label):
                self.assertEqual(rpc(self.client, "/mcp", "tools/list", token=token).status_code, 401)


class UnconfiguredEndpointTests(_AppTestCase):
    env = {"MCP_AUTH": "entra"}  # no tenant or audience: the endpoint refuses everything, the rest of the app runs

    def test_everything_is_refused_but_the_app_is_up(self) -> None:
        self.assertEqual(rpc(self.client, "/mcp", "tools/list", token=make_token()).status_code, 401)
        self.assertEqual(self.client.get("/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
