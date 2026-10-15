"""The Microsoft Graph HTTP adapter (app/sources/m365/http.py) on mocked responses only: a client-credentials token
per tenant, cached until shortly before it expires; paging on @odata.nextLink, only on graph.microsoft.com; waiting on
429/503 with Retry-After; license and consent refusals; the client secret never showing; and the endpoint allowlist,
checked in the source and on every request the adapter makes. No live call is made.
"""

from __future__ import annotations

import tests._isolation  # noqa: F401  (first: hermetic settings, no network)

import ast
import inspect
import json
import logging
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx

from app.config import Settings
from app.sources.m365 import http as m365_http
from app.sources.m365.factory import build_m365_source
from app.sources.m365.http import ENDPOINTS, GRAPH, HttpM365Source, endpoint_for
from app.sources.m365.protocol import (
    M365ConsentMissing,
    M365LicenseRequired,
    M365NotConfigured,
    M365RequestError,
    M365Source,
    M365Unavailable,
)

ROOT = Path(__file__).resolve().parent.parent
TENANT_A = "11111111-2222-3333-4444-555555555555"
TENANT_B = "66666666-7777-8888-9999-000000000000"
SECRET = "s3cr3t-value-never-shown"
CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

# docs/end-goal-plan.md, "Microsoft Graph application permissions (all read-only)", and the ones it deliberately
# does not request.
PLAN_PERMISSIONS = {
    "User.Read.All", "Directory.Read.All", "GroupMember.Read.All", "RoleManagement.Read.Directory",
    "Organization.Read.All", "LicenseAssignment.Read.All", "Domain.Read.All", "AuditLog.Read.All",
    "UserAuthenticationMethod.Read.All", "Reports.Read.All", "Policy.Read.All", "IdentityRiskyUser.Read.All",
    "IdentityRiskEvent.Read.All", "SecurityEvents.Read.All", "SecurityAlert.Read.All", "SecurityIncident.Read.All",
    "DeviceManagementManagedDevices.Read.All", "DeviceManagementConfiguration.Read.All", "MailboxSettings.Read",
    "Application.Read.All", "ServiceHealth.Read.All",
}
NOT_REQUESTED = ("Mail.Read", "Mail.ReadBasic", "Files.Read.All", "Sites.Read.All", "Chat.Read.All", "Calendars.Read")
# Path pieces that would read mail, files, chats or calendars. mailFolders appears only as the inbox rules path.
FORBIDDEN = re.compile(
    r"/messages(?!Rules)|/mailFolders(?!/inbox/messageRules)|/drives?\b|/sites\b|/chats?\b|/calendars?\b|/events\b|"
    r"/contacts\b|/onenote\b|/attachments\b|/teams/[^\"']*/channels",
    re.IGNORECASE,
)


class FakeGraph:
    """A mock transport: token requests per tenant, Graph pages from a route table, everything recorded."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []
        self.tokens = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "login.microsoftonline.com":
            self.tokens += 1
            return httpx.Response(200, json={"expires_in": 3599, "access_token": f"tok-{request.url.path.split('/')[1]}-{self.tokens}"})
        path = unquote(request.url.path)
        route = self.routes.get(path)
        if callable(route):
            return route(request)
        if route is None:
            return httpx.Response(200, json={"value": []})
        return httpx.Response(200, json=route)

    def graph_requests(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.host == "graph.microsoft.com"]


def _source(fake: FakeGraph, **kw: Any) -> HttpM365Source:
    sleeps: list[float] = kw.pop("sleeps", [])
    return HttpM365Source(
        client_id=CLIENT_ID,
        client_secret=SECRET,
        transport=httpx.MockTransport(fake),
        sleep=sleeps.append,
        **kw,
    )


class TokenTests(unittest.TestCase):
    def test_one_token_per_tenant_reused_until_near_expiry(self) -> None:
        fake = FakeGraph()
        now = [1000.0]
        source = _source(fake, clock=lambda: now[0], cache_seconds=0.0)
        source.list_domains(tenant_id=TENANT_A)
        source.list_domains(tenant_id=TENANT_A)
        source.list_domains(tenant_id=TENANT_B)
        self.assertEqual(fake.tokens, 2)
        token_urls = [str(r.url) for r in fake.requests if r.url.host == "login.microsoftonline.com"]
        self.assertEqual(token_urls, [
            f"https://login.microsoftonline.com/{TENANT_A}/oauth2/v2.0/token",
            f"https://login.microsoftonline.com/{TENANT_B}/oauth2/v2.0/token",
        ])
        body = dict(pair.split("=", 1) for pair in fake.requests[0].content.decode().split("&"))
        self.assertEqual(body["grant_type"], "client_credentials")
        self.assertEqual(unquote(body["scope"]), "https://graph.microsoft.com/.default")
        # Each tenant's calls carry that tenant's token.
        auth = [r.headers["Authorization"] for r in fake.graph_requests()]
        self.assertEqual(auth, ["Bearer tok-" + TENANT_A + "-1"] * 2 + ["Bearer tok-" + TENANT_B + "-2"])
        # 3599 s lifetime, renewed 300 s early: still cached at +3000 s, fetched again at +3300 s.
        now[0] += 3000
        source.list_domains(tenant_id=TENANT_A)
        self.assertEqual(fake.tokens, 2)
        now[0] += 300
        source.list_domains(tenant_id=TENANT_A)
        self.assertEqual(fake.tokens, 3)

    def test_a_401_gets_one_new_token_and_one_retry(self) -> None:
        calls = []

        def domains(request: httpx.Request) -> httpx.Response:
            calls.append(request.headers["Authorization"])
            if len(calls) == 1:
                return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken", "message": "expired"}})
            return httpx.Response(200, json={"value": [{"id": "a.example", "isVerified": True}]})

        fake = FakeGraph({"/v1.0/domains": domains})
        rows = _source(fake).list_domains(tenant_id=TENANT_A)
        self.assertEqual([row.id for row in rows], ["a.example"])
        self.assertEqual(fake.tokens, 2)
        self.assertNotEqual(calls[0], calls[1])

    def test_missing_settings_are_reported_on_first_call(self) -> None:
        source = build_m365_source(Settings(m365_mode="http", _env_file=None))
        with self.assertRaises(M365NotConfigured) as caught:
            source.list_domains(tenant_id=TENANT_A)
        self.assertIn("M365_CLIENT_ID or M365_CLIENT_SECRET", str(caught.exception))

    def test_consent_missing_and_bad_credentials_from_the_token_endpoint(self) -> None:
        def token_error(code: int):
            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(400, json={
                    "error": "unauthorized_client",
                    "error_description": f"AADSTS{code}: Application with identifier '{CLIENT_ID}' was not found in the directory '{TENANT_A}'.",
                    "error_codes": [code],
                })
            return handler

        for code, error in ((700016, M365ConsentMissing), (65001, M365ConsentMissing), (7000215, M365NotConfigured),
                            (7000222, M365NotConfigured), (90002, M365RequestError)):
            with self.subTest(code):
                source = HttpM365Source(client_id=CLIENT_ID, client_secret=SECRET, transport=httpx.MockTransport(token_error(code)))
                with self.assertRaises(error) as caught:
                    source.list_users(tenant_id=TENANT_A)
                message = str(caught.exception)
                self.assertIn(f"AADSTS{code}", message)
                # Microsoft's description names the tenant and the app; neither is passed on.
                self.assertNotIn(TENANT_A, message)
                self.assertNotIn(SECRET, message)


class TransportTests(unittest.TestCase):
    def test_paging_follows_next_link_on_graph_only(self) -> None:
        def users(request: httpx.Request) -> httpx.Response:
            if "skiptoken" not in str(request.url):
                return httpx.Response(200, json={
                    "value": [{"id": "1", "userPrincipalName": "a@x.example"}],
                    "@odata.nextLink": f"{GRAPH}/v1.0/users?$skiptoken=abc",
                })
            return httpx.Response(200, json={"value": [{"id": "2", "userPrincipalName": "b@x.example"}]})

        fake = FakeGraph({"/v1.0/users": users})
        rows = _source(fake).list_users(tenant_id=TENANT_A)
        self.assertEqual([row.id for row in rows], ["1", "2"])
        first, second = fake.graph_requests()
        self.assertIn("%24select=", str(first.url))
        self.assertEqual(str(second.url), f"{GRAPH}/v1.0/users?$skiptoken=abc")

        off_host = FakeGraph({"/v1.0/users": lambda request: httpx.Response(200, json={
            "value": [], "@odata.nextLink": "https://evil.example/v1.0/users?$skiptoken=abc"})})
        with self.assertRaises(M365RequestError):
            _source(off_host).list_users(tenant_id=TENANT_A)
        self.assertEqual({r.url.host for r in off_host.requests}, {"login.microsoftonline.com", "graph.microsoft.com"})

    def test_a_next_link_outside_the_allowlist_is_refused(self) -> None:
        fake = FakeGraph({"/v1.0/users": lambda request: httpx.Response(200, json={
            "value": [], "@odata.nextLink": f"{GRAPH}/v1.0/users/1/messages?$skip=10"})})
        with self.assertRaises(M365RequestError) as caught:
            _source(fake).list_users(tenant_id=TENANT_A)
        self.assertIn("allowlist", str(caught.exception))
        self.assertEqual(len(fake.graph_requests()), 1)

    def test_429_and_503_wait_for_retry_after_then_succeed(self) -> None:
        answers = iter([
            httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"code": "TooManyRequests"}}),
            httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}}),
            httpx.Response(200, json={"value": [{"id": "a.example"}]}),
        ])
        fake = FakeGraph({"/v1.0/domains": lambda request: next(answers)})
        sleeps: list[float] = []
        rows = _source(fake, sleeps=sleeps).list_domains(tenant_id=TENANT_A)
        self.assertEqual([row.id for row in rows], ["a.example"])
        self.assertEqual(sleeps, [7.0, m365_http.RETRY_DELAYS[1]])

    def test_throttling_past_the_retries_gives_up(self) -> None:
        fake = FakeGraph({"/v1.0/domains": lambda request: httpx.Response(429, headers={"Retry-After": "999"})})
        sleeps: list[float] = []
        with self.assertRaises(M365Unavailable) as caught:
            _source(fake, sleeps=sleeps).list_domains(tenant_id=TENANT_A)
        self.assertIn("429", str(caught.exception))
        self.assertEqual(sleeps, [m365_http.MAX_RETRY_AFTER] * len(m365_http.RETRY_DELAYS))

    def test_unreachable_is_unavailable(self) -> None:
        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        source = HttpM365Source(client_id=CLIENT_ID, client_secret=SECRET, transport=httpx.MockTransport(down))
        with self.assertRaises(M365Unavailable):
            source.list_domains(tenant_id=TENANT_A)

    def test_license_refusals_are_license_required_not_empty(self) -> None:
        refusals = {
            "/v1.0/users": (403, "Authentication_RequestFromNonPremiumTenantOrB2CTenant",
                            "Neither tenant is B2C or tenant doesn't have premium license"),
            "/v1.0/auditLogs/signIns": (403, "Authentication_RequestFromNonPremiumTenantOrB2CTenant",
                                        "Neither tenant is B2C or tenant doesn't have premium license"),
            "/v1.0/identityProtection/riskyUsers": (403, "Forbidden", "Your tenant is not licensed for this feature."),
            "/v1.0/deviceManagement/managedDevices": (400, "BadRequest", "Request not applicable to target tenant."),
        }
        fake = FakeGraph({
            path: (lambda status, code, message: lambda request: httpx.Response(status, json={"error": {"code": code, "message": message}}))(*answer)
            for path, answer in refusals.items()
        })
        source = _source(fake)
        for call, feature in (
            (lambda: source.list_users(tenant_id=TENANT_A, sign_in_activity=True), "sign_in_activity"),
            (lambda: source.list_sign_ins(tenant_id=TENANT_A, since=datetime(2026, 10, 1, tzinfo=timezone.utc)), "signins"),
            (lambda: source.list_risky_users(tenant_id=TENANT_A), "risky_users"),
            (lambda: source.list_managed_devices(tenant_id=TENANT_A), "intune"),
        ):
            with self.subTest(feature):
                with self.assertRaises(M365LicenseRequired) as caught:
                    call()
                self.assertEqual(caught.exception.feature, feature)

    def test_a_permission_refusal_is_consent_missing_with_the_permission(self) -> None:
        fake = FakeGraph({"/v1.0/security/secureScores": lambda request: httpx.Response(403, json={
            "error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges to complete the operation."}})})
        with self.assertRaises(M365ConsentMissing) as caught:
            _source(fake).secure_score(tenant_id=TENANT_A)
        self.assertEqual(caught.exception.permission, "SecurityEvents.Read.All")

    def test_a_404_is_a_request_error_with_ids_removed(self) -> None:
        fake = FakeGraph({f"/v1.0/users/{TENANT_B}/mailboxSettings": lambda request: httpx.Response(404, json={
            "error": {"code": "MailboxNotEnabledForRESTAPI", "message": f"Mailbox for {TENANT_B} is not enabled."}})})
        with self.assertRaises(M365RequestError) as caught:
            _source(fake).mailbox_settings(tenant_id=TENANT_A, user_id=TENANT_B)
        self.assertIn("MailboxNotEnabledForRESTAPI", str(caught.exception))
        self.assertNotIn(TENANT_B, str(caught.exception))

    def test_lists_are_cached_briefly(self) -> None:
        now = [0.0]
        fake = FakeGraph({"/v1.0/subscribedSkus": {"value": [{"skuId": "x", "skuPartNumber": "SPB"}]}})
        source = _source(fake, clock=lambda: now[0])
        source.list_subscribed_skus(tenant_id=TENANT_A)
        source.list_subscribed_skus(tenant_id=TENANT_A)
        self.assertEqual(len(fake.graph_requests()), 1)
        source.list_subscribed_skus(tenant_id=TENANT_B)  # another tenant is another entry
        self.assertEqual(len(fake.graph_requests()), 2)
        now[0] += m365_http.LIST_CACHE_SECONDS + 1
        source.list_subscribed_skus(tenant_id=TENANT_A)
        self.assertEqual(len(fake.graph_requests()), 3)

    def test_redirects_are_not_followed(self) -> None:
        fake = FakeGraph({"/beta/reports/getMailboxUsageDetail(period='D7')": lambda request: httpx.Response(
            302, headers={"Location": "https://reports.example/download.csv"})})
        with self.assertRaises(M365Unavailable):
            _source(fake).mailbox_usage(tenant_id=TENANT_A)
        self.assertNotIn("reports.example", {r.url.host for r in fake.requests})


class SecretTests(unittest.TestCase):
    def test_secret_never_in_repr_logs_or_requests_to_graph(self) -> None:
        fake = FakeGraph()
        source = _source(fake)
        self.assertNotIn(SECRET, repr(source))
        with self.assertLogs("tb_brain.m365", level=logging.DEBUG) as logs:
            logging.getLogger("tb_brain.m365").debug("start")
            source.list_domains(tenant_id=TENANT_A)
        self.assertFalse(any(SECRET in line for line in logs.output))
        for request in fake.graph_requests():
            self.assertNotIn(SECRET, str(request.url) + json.dumps(dict(request.headers)))
        settings = Settings(m365_mode="http", m365_client_id=CLIENT_ID, m365_client_secret=SECRET, _env_file=None)
        self.assertNotIn(SECRET, repr(settings))
        self.assertNotIn(SECRET, str(settings.model_dump()))


def _f_string_path(node: ast.JoinedStr) -> str:
    """f"/v1.0/users/{_seg(user_id)}/memberOf" -> "/v1.0/users/{id}/memberOf"."""
    parts = []
    for value in node.values:
        parts.append(value.value if isinstance(value, ast.Constant) else "{id}")
    return "".join(parts)


class AllowlistTests(unittest.TestCase):
    """Fails when the adapter references a Graph path that is not in ENDPOINTS, or ENDPOINTS strays outside the plan."""

    def test_every_path_in_the_adapter_is_allowlisted(self) -> None:
        tree = ast.parse(inspect.getsource(m365_http))
        pieces = {id(part) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for part in node.values}
        found = set()
        for node in ast.walk(tree):
            if id(node) in pieces:
                continue  # a literal piece of an f-string, checked as part of the whole below
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.match(r"^/(v1\.0|beta)/", node.value):
                found.add(node.value)
            elif isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant) \
                    and re.match(r"^/(v1\.0|beta)/", str(node.values[0].value)):
                found.add(_f_string_path(node))
        self.assertGreaterEqual(len(found), len(ENDPOINTS))
        for path in found:
            with self.subTest(path):
                self.assertIn(path, ENDPOINTS)

    def test_the_allowlist_stays_inside_the_plan(self) -> None:
        plan = (ROOT / "docs" / "end-goal-plan.md").read_text(encoding="utf-8")
        for template, permission in ENDPOINTS.items():
            with self.subTest(template):
                self.assertIn(permission, PLAN_PERMISSIONS)
                self.assertIn(f"`{permission}`", plan)
                self.assertIsNone(FORBIDDEN.search(template), template)
        source = inspect.getsource(m365_http)
        for permission in NOT_REQUESTED:
            self.assertNotIn(f'"{permission}"', source)
        self.assertIsNone(FORBIDDEN.search(source.replace("/mailFolders/inbox/messageRules", "")))

    def test_endpoint_for(self) -> None:
        self.assertEqual(endpoint_for("/v1.0/users/abc/memberOf"), "/v1.0/users/{id}/memberOf")
        self.assertEqual(endpoint_for("/v1.0/users/abc/mailFolders/inbox/messageRules"), "/v1.0/users/{id}/mailFolders/inbox/messageRules")
        for path in ("/v1.0/users/abc/messages", "/v1.0/users/abc/mailFolders/inbox/messages", "/v1.0/me/drive",
                     "/v1.0/users/abc/calendar/events", "/v1.0/chats", "/v1.0/users/abc/sendMail", "/v1.0/users/a/b/memberOf"):
            with self.subTest(path):
                self.assertIsNone(endpoint_for(path))

    def test_every_method_only_gets_allowlisted_graph_urls(self) -> None:
        fake = FakeGraph()
        source = _source(fake, cache_seconds=0.0)
        since = datetime(2026, 10, 1, tzinfo=timezone.utc)
        protocol_methods = [name for name in M365Source.__dict__ if not name.startswith("_") and callable(getattr(M365Source, name))]
        for name in protocol_methods:
            kwargs: dict[str, Any] = {"tenant_id": TENANT_A}
            parameters = inspect.signature(getattr(HttpM365Source, name)).parameters
            if "user_id" in parameters:
                kwargs["user_id"] = "user-1"
            if "since" in parameters:
                kwargs["since"] = since
            if "user_principal_name" in parameters:
                kwargs["user_principal_name"] = "o'neil@x.example"
            getattr(source, name)(**kwargs)
        graph = fake.graph_requests()
        self.assertGreaterEqual(len(graph), len(protocol_methods))
        for request in fake.requests:
            if request.url.host == "login.microsoftonline.com":
                self.assertEqual(request.method, "POST")
                continue
            self.assertEqual(request.method, "GET")
            self.assertEqual(f"{request.url.scheme}://{request.url.host}", GRAPH)
            self.assertIsNotNone(endpoint_for(unquote(request.url.path)), str(request.url))
        templates = {endpoint_for(unquote(r.url.path)) for r in graph}
        self.assertEqual(templates, set(ENDPOINTS))
        sign_in = next(r for r in graph if r.url.path == "/v1.0/auditLogs/signIns")
        self.assertIn("userPrincipalName eq 'o''neil@x.example'", unquote(str(sign_in.url)).replace("+", " "))


if __name__ == "__main__":
    unittest.main()
