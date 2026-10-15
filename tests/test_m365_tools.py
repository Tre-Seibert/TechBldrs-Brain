"""Microsoft 365 tools on the stub (app/sources/m365/): every tool's reply, license limits said as such (never "none"),
report name concealment, consent and outage replies, unknown clients, users and devices, arguments the way a model
sends them, all-clients answers, and that no tenant id ever reaches a reply, a tool result or the audit log.
"""

from __future__ import annotations

import tests._isolation  # noqa: F401  (first: hermetic settings, no network)

import json
import unittest
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.sources.base import Sources
from app.sources.datto.spec import DATTO
from app.sources.datto.stub import StubDattoSource
from app.sources.flow.spec import FLOW
from app.sources.flow.stub import StubFlowSource
from app.sources.m365 import fixtures
from app.sources.m365.handlers.mailboxes import concealed, is_hashed
from app.sources.m365.protocol import M365ConsentMissing, M365Unavailable
from app.sources.m365.skus import sku_matches, sku_name
from app.sources.m365.spec import M365
from app.sources.m365.stub import StubM365Source
from app.tools import ToolResult, run_tool

NOW = datetime(2026, 10, 15, 17, 0, tzinfo=timezone.utc)
TENANTS = (fixtures.WDON_TENANT, fixtures.BUCK_TENANT, fixtures.NGMC_TENANT)


def _settings() -> Settings:
    return Settings(m365_mode="stub", datto_mode="stub", _env_file=None)


def _sources(m365: Any = None) -> Sources:
    return Sources([
        (FLOW, StubFlowSource()),
        (DATTO, StubDattoSource(now=lambda: NOW)),
        (M365, m365 or StubM365Source(now=lambda: NOW)),
    ])


class ToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = _sources()
        self.settings = _settings()

    def run_tool(self, name: str, /, **arguments: Any) -> ToolResult:
        result = run_tool(sources=self.sources, name=name, arguments=arguments, actor="test", settings=self.settings)
        text = (result.reply or "") + (result.error or "") + json.dumps(result.data, default=str)
        for tenant in TENANTS:
            self.assertNotIn(tenant, text, f"{name} leaked a tenant id")
        return result

    def reply(self, name: str, /, **arguments: Any) -> str:
        result = self.run_tool(name, **arguments)
        self.assertTrue(result.ok, result.error)
        return result.reply or ""


class UsersTests(ToolTest):
    def test_count_is_computed(self) -> None:
        result = self.run_tool("m365_users", client_code="WDON", view="count")
        self.assertIn("Western Dental (WDON) has 12 accounts", result.reply)
        self.assertEqual(result.data["licensed"], 7)
        self.assertEqual(result.data["disabled"], 4)
        self.assertIn("Disabled but still licensed: 1 (Jenna Brooks)", result.reply)
        self.assertEqual(result.client_code, "WDON")

    def test_filters(self) -> None:
        wasted = self.reply("m365_users", client_code="NGMC", enabled=False, licensed=True)
        self.assertIn("Gary Fields", wasted)
        self.assertNotIn("Reception", wasted)
        guests = self.reply("m365_users", client_code="NGMC", user_type="guest")
        self.assertIn("Pat Morgan", guests)
        self.assertIn("1 user", guests)
        admins = self.reply("m365_users", client_code="WDON", role="global_admin")
        self.assertIn("Office Admin", admins)
        self.assertNotIn("Debe", admins)  # User Administrator, not global
        self.assertIn("Not TechBldrs accounts: Office Admin.", admins)
        buck = self.reply("m365_users", client_code="BUCK", admins_only=True)
        self.assertIn("Dr. Priya Patel", buck)
        self.assertIn("Not TechBldrs accounts: Dr. Priya Patel.", buck)

    def test_inactive_needs_p1(self) -> None:
        wdon = self.reply("m365_users", client_code="WDON", inactive_days=30)
        self.assertIn("Maria Lopez", wdon)
        self.assertNotIn("Jenna Brooks", wdon)  # disabled accounts are not "inactive"
        buck = self.run_tool("m365_users", client_code="BUCK", inactive_days=30)
        self.assertTrue(buck.ok)
        self.assertEqual(buck.data, {"license_required": "sign_in_activity"})
        self.assertIn("license doesn't include last sign-in dates", buck.reply)
        self.assertIn("not a sign that there is nothing there", buck.reply)
        self.assertNotRegex(buck.reply, r"\d+ days ago")

    def test_get_user(self) -> None:
        debe = self.reply("m365_get_user", client_code="WDON", user="Debe", include=["auth_methods", "groups"])
        self.assertIn("Debe Hernandez <debe@westerndental.example>", debe)
        self.assertIn("Licenses: Microsoft 365 Business Premium", debe)
        self.assertIn("Admin roles: User Administrator", debe)
        self.assertIn("Last sign-in attempt: 40 minutes ago; last successful: 1 day ago", debe)
        self.assertIn("MFA methods: Microsoft Authenticator (iPhone 14), phone", debe)
        self.assertIn("Groups (3): Billing Mailbox Access, Front Office, WDON Staff", debe)
        frontdesk = self.reply("m365_get_user", client_code="WDON", user="Front Desk")
        self.assertIn("disabled (member), created 2019-06-01", frontdesk)
        thomas = self.reply("m365_get_user", client_code="WDON", user="thomas.carter", include="mfa")
        self.assertIn("MFA methods: none registered (password only)", thomas)
        john = self.reply("m365_get_user", client_code="BUCK", user="jsmith@buckheadpeds.example")
        self.assertIn("Last sign-in: not available; the tenant's license doesn't include last sign-in dates", john)
        alias = self.reply("m365_get_user", client_code="WDON", user="dhernandez@westerndental.example")
        self.assertIn("Debe Hernandez", alias)

    def test_unknown_ambiguous_and_device_like_users(self) -> None:
        close = self.run_tool("m365_get_user", client_code="WDON", user="Debbie Hernandes")
        self.assertFalse(close.ok)
        self.assertIn("has no user named Debbie Hernandes. Close matches: Debe Hernandez", close.error)
        several = self.run_tool("m365_get_user", client_code="WDON", user="B")
        self.assertIn("matches more than one user", several.error)
        self.assertIn("Billing", several.error)
        self.assertIn("Brooke Ellerby", several.error)
        device = self.run_tool("m365_get_user", client_code="WDON", user="WDON-FD01")
        self.assertIn("looks like a device name, not a user", device.error)
        self.assertIn("Datto RMM", device.error)
        missing = self.run_tool("m365_get_user", client_code="WDON")
        self.assertEqual(missing.error, "Which user? Give their name or email address.")


class ClientFromUserTests(ToolTest):
    def test_flow_contact_gives_the_client(self) -> None:
        result = self.run_tool("m365_get_user", user="Debe")
        self.assertTrue(result.ok)
        self.assertTrue(result.reply.startswith("Debe Hernandez is a Flow contact at WDON, so this is WDON's tenant."))
        self.assertEqual(result.client_code, "WDON")

    def test_email_domain_gives_the_client(self) -> None:
        result = self.run_tool("m365_get_user", user="jsmith@buckheadpeds.example")
        self.assertEqual(result.client_code, "BUCK")
        self.assertIn("John Smith", result.reply)
        # An email sent as the client is the user.
        moved = self.run_tool("m365_get_user", client_code="kwells@buckheadpeds.example")
        self.assertIn("Kara Wells", moved.reply)
        unknown = self.run_tool("m365_get_user", user="someone@nowhere.example")
        self.assertIn("No client in Flow uses nowhere.example", unknown.error)

    def test_several_or_no_contacts_ask(self) -> None:
        several = self.run_tool("m365_get_user", user="O'Brien")
        self.assertIn("matches Flow contacts at more than one client", several.error)
        self.assertIn("Sean O'Brien (WDON)", several.error)
        self.assertIn("April O'Brien (ACME)", several.error)
        nobody = self.run_tool("m365_get_user", user="Zelda")
        self.assertEqual(nobody.error, "Which client is Zelda at? Give the client code or name.")
        # A person's name sent as the client code is looked up as the person.
        as_client = self.run_tool("m365_get_user", client_code="Debe Hernandez")
        self.assertEqual(as_client.client_code, "WDON")

    def test_contact_at_a_client_without_a_tenant(self) -> None:
        result = self.run_tool("m365_get_user", user="Michael Sodl", include=["groups"])
        self.assertFalse(result.ok)
        self.assertIn("Michael Sodl is a Flow contact at ACME", result.error)
        self.assertIn("Acme Labs (ACME) has no Microsoft 365 tenant id on record in Flow", result.error)


class MfaTests(ToolTest):
    def test_report_where_licensed(self) -> None:
        result = self.run_tool("m365_mfa_report", client_code="WDON", registered=False)
        self.assertIn("4 of 8 enabled accounts have MFA registered, 4 don't", result.reply)
        self.assertIn("Office Admin <officeadmin@westerndental.example> — Global Administrator (admin without MFA)", result.reply)
        self.assertIn("Source: the MFA registration report (Entra ID P1)", result.reply)
        self.assertNotIn("Front Desk", result.reply)  # disabled shared mailbox
        self.assertEqual(result.data["source"], "registration_report")
        self.assertEqual(result.data["admins_without_mfa"], ["officeadmin@westerndental.example"])
        admins = self.reply("m365_mfa_report", client_code="WDON", registered=False, admins_only=True)
        self.assertIn("2 of 3 admin accounts have MFA registered, 1 doesn't", admins)

    def test_per_user_methods_without_p1(self) -> None:
        result = self.run_tool("m365_mfa_report", client_code="BUCK")
        self.assertIn("Amy Nguyen", result.reply)
        self.assertIn("Source: each user's registered authentication methods, because this tenant's license doesn't include", result.reply)
        self.assertEqual(result.data["source"], "per_user_methods")

    def test_all_clients(self) -> None:
        reply = self.reply("m365_mfa_report", client_code="all")
        self.assertRegex(reply, r"(?s)WDON.*BUCK.*NGMC")
        self.assertIn("Western Dental (WDON): 4 of 8 without MFA (1 admin: Office Admin)", reply)
        self.assertIn("Northwind Traders (NRTH): tb-brain's app has no consent in this tenant", reply)
        self.assertIn("All clients: this checked every client's Microsoft 365 tenant", reply)


class LicenseTests(ToolTest):
    def test_summary_and_unused(self) -> None:
        buck = self.reply("m365_license_summary", client_code="BUCK")
        self.assertIn("Microsoft 365 Business Standard: 3 of 5 assigned, 2 free", buck)
        self.assertIn("Microsoft 365 Business Basic: 2 of 3 assigned, 1 free", buck)
        self.assertIn("Lisa Grant", buck)
        wdon = self.reply("m365_license_summary", client_code="WDON", unused_only=True)
        self.assertIn("Microsoft 365 Business Premium: 7 of 10 assigned, 3 free", wdon)
        self.assertNotIn("Power Automate", wdon)  # free SKUs' seats are not waste
        ngmc = self.reply("m365_license_summary", client_code="NGMC")
        self.assertIn("Microsoft 365 E3", ngmc)
        self.assertIn("- MCOEV: 1 of 2 assigned", ngmc)  # an SKU not in the table shows its part number
        self.assertNotIn("Business Premium", ngmc)

    def test_all_clients_with_a_sku(self) -> None:
        reply = self.reply("m365_license_summary", client_code="all", sku="business_premium", unused_only=True)
        self.assertIn("Clients with unused Microsoft 365 Business Premium seats: 1 of 3 clients checked.", reply)
        self.assertIn("Western Dental (WDON): Microsoft 365 Business Premium 3 free of 10", reply)
        self.assertIn("no Microsoft 365 Business Premium at all: BUCK, NGMC", reply)

    def test_sku_names(self) -> None:
        self.assertEqual(sku_name("SPB"), "Microsoft 365 Business Premium")
        self.assertEqual(sku_name("O365_BUSINESS_PREMIUM"), "Microsoft 365 Business Standard")
        self.assertEqual(sku_name("AAD_PREMIUM_P2"), "Microsoft Entra ID P2")
        self.assertEqual(sku_name("SOMETHING_NEW"), "SOMETHING_NEW")
        self.assertTrue(sku_matches("SPB", "Business Premium"))
        self.assertTrue(sku_matches("SPB", "spb"))
        self.assertFalse(sku_matches("O365_BUSINESS_PREMIUM", "business_premium"))
        self.assertTrue(sku_matches("SPE_E3", "E3") and sku_matches("ENTERPRISEPACK", "E3"))


class SignInTests(ToolTest):
    def test_failures_by_user_and_reason(self) -> None:
        reply = self.reply("m365_signins", client_code="WDON", status="failed", hours=24)
        self.assertIn("Failed sign-ins at Western Dental (WDON) in the last 24 hours: 11", reply)
        self.assertIn("Thomas Carter <thomas.carter@westerndental.example>: 4× wrong username or password (50126), "
                      "2× blocked by Conditional Access (53003) (policy Block legacy authentication) · from RU", reply)
        self.assertIn("account locked (too many failed attempts) (50053)", reply)

    def test_outside_country_and_abroad(self) -> None:
        reply = self.reply("m365_signins", client_code="NGMC", outside_country="USA", days=7)
        self.assertIn("from outside US in the last 7 days: 2", reply)
        self.assertIn("Toronto, Ontario, CA", reply)
        self.assertIn("Beijing, Beijing, CN", reply)
        week = self.reply("m365_signins", client_code="WDON")
        self.assertIn("Successful sign-ins from outside US:", week)
        self.assertIn("Chris Patel from Lagos, Lagos, NG", week)

    def test_one_user_and_latest(self) -> None:
        debe = self.reply("m365_signins", user="Debe", status="failure")
        self.assertIn("Failed sign-ins for Debe Hernandez <debe@westerndental.example> in the last 7 days: 5", debe)
        self.assertIn("40 minutes ago", debe.split("Newest first:")[1].splitlines()[1])
        latest = self.reply("m365_signins", client_code="WDON", user="Chris Patel", limit=1)
        self.assertTrue(latest.startswith("Latest sign-in for Chris Patel <cpatel@westerndental.example>: 20 hours ago from Newark"))

    def test_no_p1_is_a_license_reply(self) -> None:
        result = self.run_tool("m365_signins", user="jsmith@buckheadpeds.example", limit=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.data, {"license_required": "signins"})
        self.assertIn("Buckhead Pediatrics (BUCK)'s Microsoft 365 license doesn't include sign-in logs", result.reply)


class SecurityTests(ToolTest):
    def test_secure_score_and_actions(self) -> None:
        reply = self.reply("m365_security", client_code="WDON", view="Secure Score")
        self.assertIn("Secure Score for Western Dental (WDON): 48.2 of 82 (59%)", reply)
        self.assertIn("1. Block automatic forwarding of mail to external domains (+8 points)", reply)
        self.assertIn("2. Require multifactor authentication for administrative roles (+5 points)", reply)
        ranking = self.reply("m365_security", client_code="all", view="secure_score")
        self.assertRegex(ranking, r"(?s)BUCK.*WDON.*NGMC")

    def test_alerts_and_risky_users(self) -> None:
        alerts = self.reply("m365_security", client_code="WDON", view="alerts")
        self.assertIn("Open Defender alerts at Western Dental (WDON): 1.", alerts)
        self.assertIn("high · Suspicious inbox forwarding rule", alerts)
        everything = self.reply("m365_security", client_code="WDON", view="alerts", status="all")
        self.assertIn("malicious URL", everything)
        risky = self.reply("m365_security", client_code="NGMC", view="risky")
        self.assertIn("Nora Jones <njones@ngmc.example> — risk medium, atRisk", risky)
        p2 = self.run_tool("m365_security", client_code="WDON", view="risky_users")
        self.assertIn("doesn't include risky users from Identity Protection (that needs Entra ID P2", p2.reply)
        overview = self.reply("m365_security", client_code="BUCK")
        self.assertIn("Secure Score for Buckhead Pediatrics (BUCK): 31 of 70", overview)
        self.assertIn("Defender alerts: Buckhead Pediatrics (BUCK)'s Microsoft 365 license doesn't include Microsoft Defender", overview)


class PolicyTests(ToolTest):
    def test_mfa_enforcement(self) -> None:
        buck = self.reply("m365_policies", client_code="BUCK", kind="mfa")
        self.assertIn("MFA at Buckhead Pediatrics (BUCK): enforced. Security defaults are on", buck)
        wdon = self.reply("m365_policies", client_code="WDON")
        self.assertIn("enforced by Conditional Access", wdon)
        self.assertIn("Require MFA for all users — on · requires MFA · all users, except Office Admin", wdon)
        self.assertIn("Left out of MFA by policy: Office Admin.", wdon)
        ngmc = self.reply("m365_policies", client_code="NGMC")
        self.assertIn("not enforced", ngmc)
        self.assertIn("MFA for all users — report-only (not enforced)", ngmc)

    def test_conditional_access_and_defaults(self) -> None:
        ngmc = self.reply("m365_policies", client_code="NGMC", kind="CA")
        self.assertIn("Conditional Access at North Georgia Medical Clinic (NGMC): 3 policies.", ngmc)
        self.assertIn("except guests and external users", ngmc)
        buck = self.run_tool("m365_policies", client_code="BUCK", kind="conditional_access")
        self.assertTrue(buck.ok)
        self.assertIn("license doesn't include Conditional Access", buck.reply)
        self.assertIn("Security defaults are on.", buck.reply)
        self.assertIn("Security defaults at Western Dental (WDON): off.", self.reply("m365_policies", client_code="WDON", kind="security_defaults"))

    def test_all_clients(self) -> None:
        reply = self.reply("m365_policies", client_code="all", kind="mfa")
        self.assertIn("1 with none at all (North Georgia Medical Clinic (NGMC))", reply)
        self.assertIn("Western Dental (WDON): Conditional Access (Office Admin left out)", reply)
        self.assertIn("All clients:", reply)
        refused = self.run_tool("m365_policies", client_code="all", kind="conditional_access")
        self.assertIn("looks at one client at a time", refused.error)


class DeviceTests(ToolTest):
    def test_intune_devices(self) -> None:
        bad = self.reply("m365_devices", client_code="WDON", compliant=False)
        for name in ("WDON-OP02", "WDON-OP03", "Chris's iPhone"):
            self.assertIn(name, bad)
        self.assertNotIn("WDON-FD01", bad)
        debe = self.reply("m365_devices", client_code="WDON", user="Debe")
        self.assertIn("WDON-FD01", debe)
        self.assertIn("Debe's iPhone", debe)
        stale = self.reply("m365_devices", client_code="NGMC", stale_days=14)
        self.assertIn("NGMC-LT02", stale)
        self.assertNotIn("NGMC-PC09", stale)
        buck = self.run_tool("m365_devices", client_code="BUCK", stale_days=14)
        self.assertIn("license doesn't include Intune device management", buck.reply)


class MailboxTests(ToolTest):
    def test_shared_mailboxes(self) -> None:
        reply = self.reply("m365_mailboxes", client_code="WDON", kind="shared")
        self.assertIn("3 shared mailboxes at Western Dental (WDON).", reply)
        self.assertIn("Payroll <payroll@westerndental.example> — shared mailbox · created 2026-09-11", reply)
        self.assertNotIn("Debe", reply)

    def test_usage(self) -> None:
        debe = self.reply("m365_mailboxes", client_code="WDON", view="usage", user="Debe")
        self.assertIn("47.6 GB of 50.0 GB (95% full)", debe)
        full = self.reply("m365_mailboxes", client_code="BUCK", view="size", over_percent=90)
        self.assertIn("Amy Nguyen", full)
        self.assertNotIn("Priya", full)  # 89.6%

    def test_concealed_report_names_are_detected(self) -> None:
        result = self.run_tool("m365_mailboxes", client_code="NGMC", view="usage")
        self.assertTrue(result.data["concealed"])
        self.assertIn("conceals user names in Microsoft 365 usage reports", result.reply)
        self.assertIn("Display concealed user, group, and site names in all reports", result.reply)
        self.assertNotRegex(result.reply, r"[0-9A-F]{32}")
        one = self.run_tool("m365_mailboxes", client_code="NGMC", view="usage", user="Tina Brooks")
        self.assertIn("conceals user names", one.reply)
        self.assertTrue(is_hashed(fixtures.concealed_name("tbrooks@ngmc.example")))
        self.assertFalse(is_hashed("tbrooks@ngmc.example"))
        self.assertFalse(is_hashed("Reception"))
        ngmc = StubM365Source(now=lambda: NOW)
        self.assertTrue(concealed(ngmc.mailbox_usage(tenant_id=fixtures.NGMC_TENANT)))
        self.assertFalse(concealed(ngmc.mailbox_usage(tenant_id=fixtures.WDON_TENANT)))

    def test_external_forwarding(self) -> None:
        result = self.run_tool("m365_mailboxes", client_code="WDON", view="external_forwarding_rules")
        self.assertIn("Inbox rules forwarding mail outside Western Dental (WDON): 1", result.reply)
        self.assertIn('Chris Patel <cpatel@westerndental.example> — rule ".." forwards to cpatel.archive@protonmail.example', result.reply)
        self.assertIn("Debe Hernandez → billing@westerndental.example", result.reply)  # internal, not flagged
        self.assertIn("ForwardingSmtpAddress", result.reply)
        self.assertEqual([item["upn"] for item in result.data["external"]], ["cpatel@westerndental.example"])
        clean = self.reply("m365_mailboxes", client_code="BUCK", view="forwarding")
        self.assertIn("outside Buckhead Pediatrics (BUCK): 0", clean)


class TenantAndAuditTests(ToolTest):
    def test_domains_credentials_overview(self) -> None:
        domains = self.reply("m365_tenant", client_code="WDON", view="domains")
        self.assertIn("1 not verified (wdonsmiles.example)", domains)
        expiring = self.reply("m365_tenant", client_code="BUCK", view="expiring_app_credentials", days=30)
        self.assertIn("Scan2Mail — secret 'smtp relay' ends in", expiring)
        self.assertNotIn("QuickBooks", expiring)
        expired = self.reply("m365_tenant", client_code="WDON", view="app_secrets")
        self.assertIn("WDON Scanner to Mail — certificate 'scanner cert' expired", expired)
        overview = self.reply("m365_tenant", client_code="NGMC")
        self.assertIn("Directory sync from on-premises AD: on", overview)

    def test_service_health_needs_no_client(self) -> None:
        result = self.run_tool("m365_tenant", view="outage")
        self.assertTrue(result.ok)
        self.assertIn("it is the same service for every tenant", result.reply)
        self.assertIn("Exchange Online: degraded", result.reply)
        self.assertIn("EX912345", result.reply)

    def test_audit(self) -> None:
        week = self.reply("m365_audit", client_code="WDON", days=7)
        self.assertIn("last 7 days: 5 changes", week)
        self.assertIn("Consent to application · by officeadmin@westerndental.example · on PDF Converter Pro", week)
        roles = self.reply("m365_audit", client_code="WDON", category="roles", days=30)
        self.assertIn("Add member to role · by officeadmin@westerndental.example · on Debe Hernandez", roles)
        self.assertNotIn("Consent", roles)
        chris = self.reply("m365_audit", client_code="WDON", user="Chris Patel")
        self.assertIn("User registered security info", chris)


class ErrorTests(ToolTest):
    def test_unknown_client_and_no_tenant(self) -> None:
        unknown = self.run_tool("m365_mfa_report", client_code="ZZZZ")
        self.assertFalse(unknown.ok)
        self.assertIn('No client in Flow matches "ZZZZ"', unknown.error)
        none = self.run_tool("m365_mfa_report", client_code="Acme Labs")
        self.assertIn("Acme Labs (ACME) has no Microsoft 365 tenant id on record in Flow", none.error)
        self.assertEqual(none.client_code, "ACME")
        ask = self.run_tool("m365_users")
        self.assertEqual(ask.error, "Which client? Give the client code or name.")
        guid = self.run_tool("m365_users", client_code=fixtures.WDON_TENANT)
        self.assertIn("tb-brain finds the tenant itself", guid.error)

    def test_consent_missing(self) -> None:
        nrth = self.run_tool("m365_users", client_code="Northwind Traders")
        self.assertFalse(nrth.ok)
        self.assertIn("tb-brain's Microsoft 365 app has not been granted access to Northwind Traders (NRTH)'s tenant", nrth.error)
        self.assertIn("needs to consent to the app once", nrth.error)

        class OnePermissionMissing(StubM365Source):
            def secure_score(self, *, tenant_id: str):
                raise M365ConsentMissing("Microsoft Graph refused tb-brain's app (HTTP 403, Authorization_RequestDenied); it needs SecurityEvents.Read.All.", permission="SecurityEvents.Read.All")

        self.sources = _sources(OnePermissionMissing(now=lambda: NOW))
        partial = self.run_tool("m365_security", client_code="WDON", view="secure_score")
        self.assertIn("the consent given there is missing the SecurityEvents.Read.All permission", partial.error)

    def test_graph_unreachable(self) -> None:
        class Down(StubM365Source):
            def list_users(self, **kw):
                raise M365Unavailable("Microsoft Graph is not reachable (ConnectError).")

        self.sources = _sources(Down(now=lambda: NOW))
        result = self.run_tool("m365_users", client_code="WDON")
        self.assertEqual(result.error, "Microsoft 365 could not be reached, so I can't check Western Dental (WDON) right now. "
                                       "Microsoft Graph is not reachable (ConnectError).")
        every = self.run_tool("m365_mfa_report", client_code="all")
        self.assertIn("Microsoft 365 could not be reached", every.reply)

    def test_not_configured(self) -> None:
        from app.sources.m365.http import HttpM365Source

        self.sources = _sources(HttpM365Source(client_id="", client_secret=""))
        one = self.run_tool("m365_users", client_code="WDON")
        self.assertIn("Microsoft 365 is not set up in tb-brain", one.error)
        every = self.run_tool("m365_license_summary", client_code="all")
        self.assertEqual(every.error.count("not set up"), 1)


class ArgumentTests(ToolTest):
    def test_arguments_the_way_a_model_sends_them(self) -> None:
        self.assertIn("Debe Hernandez", self.reply("m365_get_user", client="Western Dental", upn="debe@westerndental.example"))
        self.assertIn("Debe Hernandez", self.reply("m365_get_user", client_code="WDON", name="Debe", include="groups,auth_methods"))
        self.assertIn("Failed sign-ins", self.reply("m365_signins", client_code="WDON", status="Failures", days=None, limit=None))
        self.assertIn("Debe Hernandez", self.reply("m365_users", client_code="WDON", user="Debe"))
        self.assertIn("Secure Score", self.reply("m365_security", client_code="WDON", view="score"))
        self.assertIn("1 change", self.reply("m365_audit", client_code="WDON", category="Role Management", days="30"))
        self.assertIn("Inbox rules", self.reply("m365_mailboxes", client_code="WDON", view="inbox rules"))
        bad = self.run_tool("m365_signins", client_code="WDON", days="lots")
        self.assertFalse(bad.ok)
        self.assertIn("Invalid arguments", bad.error)


class AuditLogTests(ToolTest):
    def test_calls_are_audited_with_client_and_no_tenant(self) -> None:
        log = self.settings.audit_log_dir / "tool_calls.jsonl"
        before = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        self.run_tool("m365_get_user", user="Debe")
        self.run_tool("m365_mfa_report", client_code="NRTH")
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()[len(before):]]
        self.assertEqual([(row["tool"], row["client_code"], row["source"], row["ok"]) for row in rows], [
            ("m365_get_user", "WDON", "m365-stub", True),
            ("m365_mfa_report", "NRTH", "m365-stub", False),
        ])
        for tenant in (*TENANTS, "6a2e9b40-71c3-4d8e-8f20-5b1c0d9e7a63"):
            self.assertNotIn(tenant, json.dumps(rows))


if __name__ == "__main__":
    unittest.main()
