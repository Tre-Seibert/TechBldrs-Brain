"""Datto RMM tools on the stub (DATTO_MODE=stub): every tool, its filters, the client hook, and the clear replies
for an unknown client, a client with no Datto site, an unknown hostname, and Datto not reachable.

The stub's clock is pinned, so "last seen 3 days ago" is the same on every run.
"""

from __future__ import annotations

import tests._isolation  # noqa: F401  (first: hermetic settings, no network)

import unittest
from datetime import datetime, timezone
from typing import Any
from unittest import mock

from app.config import Settings
from app.sources.base import Sources
from app.sources.clients import Outcome
from app.sources.datto.handlers.common import MAX_LINES, ago, short_os
from app.sources.datto.protocol import DattoNotConfigured, DattoUnavailable
from app.sources.datto.spec import DATTO
from app.sources.datto.stub import StubDattoSource
from app.sources.datto.tools import LEAN_OPENAI_TOOLS, OPENAI_TOOLS, TOOL_NAMES
from app.sources.flow.spec import FLOW
from app.sources.flow.stub import StubFlowSource
from app.tools import run_tool

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)


def _sources(datto: Any = None) -> Sources:
    return Sources([(FLOW, StubFlowSource()), (DATTO, datto or StubDattoSource(now=lambda: NOW))])


class DattoToolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(datto_mode="stub", _env_file=None)
        self.sources = _sources()

    def run_tool(self, name: str, **arguments: Any):
        return run_tool(sources=self.sources, name=name, arguments=arguments, actor="test", settings=self.settings)


class ListDevicesTests(DattoToolTestCase):
    def test_lists_a_clients_devices_with_state(self) -> None:
        result = self.run_tool("datto_list_devices", client_code="WDON")
        self.assertTrue(result.ok)
        self.assertEqual(result.source, "datto-stub")
        self.assertEqual(result.client_code, "WDON")
        self.assertTrue(result.reply.startswith("6 devices at Western Dental (WDON) in Datto RMM."))
        self.assertIn("WDON-IMG-01 — Desktop · Windows 10 Pro · offline, last seen 3 days ago (2026-10-10)", result.reply)
        self.assertIn("WDON-FD01 — Desktop · Windows 11 Pro · online · last user WDON\\debe.hernandez", result.reply)
        self.assertEqual(result.data["count"], 6)
        self.assertNotIn("uid", result.data["devices"][0])  # summaries, never the raw API record
        self.assertEqual(len(result.row_ids), 6)

    def test_client_name_works_like_the_code(self) -> None:
        result = self.run_tool("datto_list_devices", client_code="buckhead pediatrics", device_type="server")
        self.assertEqual(result.client_code, "BUCK")
        self.assertIn("1 device at Buckhead Pediatrics (BUCK)", result.reply)
        self.assertIn("BUCK-SRV01", result.reply)

    def test_filters(self) -> None:
        cases = {
            "offline": ({"client_code": "NGMC", "online": False}, {"NGMC-PC07", "NGMC-LT02"}),
            "stale": ({"client_code": "WDON", "offline_days": 30}, {"WDON-OP03"}),
            "workstations": ({"client_code": "BUCK", "device_type": "workstation"}, {"BUCK-PC01", "BUCK-PC02", "BUCK-PC03", "BUCK-LT01"}),
            "laptops": ({"client_code": "NGMC", "device_type": "laptop"}, {"NGMC-LT02"}),
            "os": ({"client_code": "BUCK", "os": "Windows 10"}, {"BUCK-PC01", "BUCK-PC03"}),
            "needs patches": ({"client_code": "BUCK", "needs_patches": True}, {"BUCK-SRV01", "BUCK-PC02", "BUCK-PC03"}),
            "av": ({"client_code": "NGMC", "av_issue": True}, {"NGMC-PC05", "NGMC-PC07"}),
            "reboot": ({"client_code": "NGMC", "reboot_required": True}, {"NGMC-PC09"}),
            "last user": ({"client_code": "WDON", "last_user": "Debe Hernandez"}, {"WDON-FD01"}),
        }
        for label, (arguments, hosts) in cases.items():
            with self.subTest(label):
                result = self.run_tool("datto_list_devices", **arguments)
                self.assertTrue(result.ok, result.error)
                self.assertEqual({row["hostname"] for row in result.data["devices"]}, hosts)
                self.assertIn(f"{len(hosts)} device", result.reply)

    def test_filters_combine_and_say_what_they_did(self) -> None:
        result = self.run_tool("datto_list_devices", client_code="WDON", online=False, os="Windows 10")
        self.assertIn("(offline, on Windows 10; 6 in all)", result.reply)
        self.assertEqual({row["hostname"] for row in result.data["devices"]}, {"WDON-IMG-01", "WDON-OP03"})

    def test_nothing_matching_says_so(self) -> None:
        result = self.run_tool("datto_list_devices", client_code="WDON", device_type="server", online=False)
        self.assertTrue(result.ok)
        self.assertIn("0 devices", result.reply)
        self.assertIn("None match.", result.reply)

    def test_all_clients_lists_every_site_and_says_it_is_slower(self) -> None:
        result = self.run_tool("datto_list_devices", client_code="all", online=False)
        self.assertIsNone(result.client_code)
        self.assertEqual(
            {row["hostname"] for row in result.data["devices"]},
            {"WDON-IMG-01", "WDON-OP03", "BUCK-LT01", "NGMC-PC07", "NGMC-LT02"},
        )
        self.assertIn("all clients", result.reply)
        self.assertIn("takes longer", result.reply)

    def test_last_user_without_a_client_searches_every_client(self) -> None:
        result = self.run_tool("datto_list_devices", last_user="debe")
        self.assertEqual([row["hostname"] for row in result.data["devices"]], ["WDON-FD01"])
        self.assertIn("all clients", result.reply)

    def test_no_client_and_no_user_asks_which_client(self) -> None:
        result = self.run_tool("datto_list_devices", online=False)
        self.assertFalse(result.ok)
        self.assertIn("Which client's devices?", result.reply)

    def test_long_lists_are_capped(self) -> None:
        result = self.run_tool("datto_list_devices", client_code="all", limit=3)
        self.assertEqual(len(result.data["devices"]), 3)
        self.assertIn("... and 13 more", result.reply)


class ClientOutcomeTests(DattoToolTestCase):
    def test_unknown_client(self) -> None:
        result = self.run_tool("datto_list_devices", client_code="ZZZZ")
        self.assertFalse(result.ok)
        self.assertEqual(result.reply, result.error)
        self.assertIn('No client in Flow matches "ZZZZ"', result.reply)

    def test_client_with_no_datto_site(self) -> None:
        result = self.run_tool("datto_site_summary", client_code="ACME")
        self.assertFalse(result.ok)
        self.assertEqual(result.client_code, "ACME")
        self.assertEqual(result.reply, "Acme Labs (ACME) has no Datto RMM site (none whose name starts with ACME).")

    def test_ambiguous_name_asks(self) -> None:
        result = self.run_tool("datto_patch_report", client_code="Western")
        self.assertFalse(result.ok)
        self.assertIn("Western Dental (WDON), Western Dental Lab (WDLB). Which one?", result.reply)

    def test_hook_fills_the_datto_site_for_the_resolver(self) -> None:
        resolution = self.sources.clients.resolve("NGMC", require=("datto_site_uid",))
        self.assertEqual(resolution.outcome, Outcome.FOUND)
        self.assertEqual(resolution.client.datto_site_uid, "f0c3d5a8-91b2-4e7d-a6c4-8b2e0f1d5c03")
        self.assertEqual(self.sources.clients.resolve("WDLB", require=("datto_site_uid",)).outcome, Outcome.NO_DATTO_SITE)

    def test_hook_matches_the_first_word_of_the_site_name_only(self) -> None:
        from app.sources.datto.clients import datto_site_uid

        datto = StubDattoSource(now=lambda: NOW)
        self.assertEqual(datto_site_uid(datto, "wdon"), "6d1f0b6e-3c8a-4f0e-9d2b-1a7c5e2f9b01")
        self.assertIsNone(datto_site_uid(datto, "WEST"))  # "Western" is the second word of "WDON Western Dental"
        self.assertIsNone(datto_site_uid(datto, "ACME"))


class GetDeviceTests(DattoToolTestCase):
    def test_one_device_by_hostname_without_a_client_code(self) -> None:
        result = self.run_tool("datto_get_device", hostname="wdon-fd01")
        self.assertTrue(result.ok)
        self.assertEqual(result.client_code, "WDON")
        for line in (
            "WDON-FD01 — Desktop, Windows 11 Pro",
            "Status: online (last seen 2 minutes ago)",
            "Last user: WDON\\debe.hernandez",
            "Last reboot: 16 days ago (2026-09-27); a reboot is needed",
            "Patches: 4 approved patches pending (188 installed)",
            "Antivirus: Datto AV, running, out of date",
            "Warranty: 2027-02-11",
            "Open alerts: none",
        ):
            self.assertIn(line, result.reply)
        self.assertNotIn("Hardware:", result.reply)

    def test_offline_device_with_open_alert(self) -> None:
        result = self.run_tool("datto_get_device", hostname="WDON-IMG-01")
        self.assertIn("Status: OFFLINE, last seen 3 days ago (2026-10-10)", result.reply)
        self.assertIn("Open alerts: 1 (newest: Critical, Device has been offline", result.reply)

    def test_hardware_and_disks(self) -> None:
        result = self.run_tool("datto_get_device", hostname="WDON-SRV01", include=["hardware", "disks"])
        self.assertIn("Hardware: Dell Inc. PowerEdge T340, serial 5RT9WX2, 64 GB RAM", result.reply)
        self.assertIn("Disks: C: 14 GB free of 180 GB (8% free); D: 960 GB free of 1,800 GB (53% free)", result.reply)
        self.assertEqual(result.data["serial_number"], "5RT9WX2")
        self.assertEqual(result.data["disks"][0], {"disk": "C:", "free_gb": 14.1, "size_gb": 180.0})

    def test_include_accepts_a_comma_string(self) -> None:
        result = self.run_tool("datto_get_device", hostname="BUCK-PC03", include="disks")
        self.assertIn("Disks: C: 9.6 GB free of 238 GB (4% free)", result.reply)

    def test_unknown_hostname_offers_close_matches(self) -> None:
        result = self.run_tool("datto_get_device", hostname="WDON-FD02")
        self.assertFalse(result.ok)
        self.assertEqual(result.reply, "Datto RMM has no device named WDON-FD02. Close matches: WDON-FD01, WDON-OP02.")

    def test_unknown_hostname_lists_that_clients_devices_when_nothing_is_close(self) -> None:
        result = self.run_tool("datto_get_device", hostname="WDON-XX99")
        self.assertEqual(
            result.reply,
            "Datto RMM has no device named WDON-XX99. WDON devices in Datto: WDON-FD01, WDON-IMG-01, WDON-LT01, "
            "WDON-OP02, WDON-OP03, WDON-SRV01.",
        )

    def test_unknown_hostname_with_no_known_prefix(self) -> None:
        result = self.run_tool("datto_get_device", hostname="QQQQ-ZZ77")
        self.assertEqual(result.reply, "Datto RMM has no device named QQQQ-ZZ77. No hostname in Datto RMM is close to it.")


class AlertTests(DattoToolTestCase):
    def test_open_alerts_for_a_client(self) -> None:
        result = self.run_tool("datto_alerts", client_code="WDON", status="open")
        self.assertTrue(result.reply.startswith("2 open alerts for Western Dental (WDON) in Datto RMM.\nBy priority: Critical 1, High 1."))
        self.assertIn("- WDON-IMG-01: Critical · offline · raised 3 days ago (2026-10-10) · ticket WDON-1842", result.reply)
        self.assertNotIn("WDON-FD01", result.reply)  # its antivirus alert is resolved

    def test_a_devices_alerts_this_week_include_resolved_ones(self) -> None:
        result = self.run_tool("datto_alerts", hostname="BUCK-SRV01", days=7)
        self.assertIn("2 open and resolved alerts for BUCK-SRV01 in Datto RMM (last 7 days).", result.reply)
        self.assertIn("sqlservr.exe", result.reply)
        self.assertIn("resolved", result.reply)
        self.assertNotIn("Print Spooler", result.reply)  # BUCK-PC03, 20 days ago

    def test_count_by_client_is_ranked_in_code(self) -> None:
        result = self.run_tool("datto_alerts", client_code="all", status="open", view="count_by_client")
        self.assertIn("- NGMC: 3 (1 Critical, 1 High, 1 Moderate)\n- WDON: 2 (1 Critical, 1 High)\n- BUCK: 1 (1 Moderate)", result.reply)
        self.assertEqual([row["client_code"] for row in result.data], ["NGMC", "WDON", "BUCK"])
        self.assertIn("takes longer", result.reply)

    def test_count_by_client_without_a_client_means_all(self) -> None:
        result = self.run_tool("datto_alerts", view="count_by_client")
        self.assertEqual([row["count"] for row in result.data], [3, 2, 1])

    def test_critical_across_every_client(self) -> None:
        result = self.run_tool("datto_alerts", client_code="all", priority="critical")
        self.assertEqual({row["hostname"] for row in result.data["alerts"]}, {"NGMC-PC09", "WDON-IMG-01"})

    def test_resolved_only(self) -> None:
        result = self.run_tool("datto_alerts", client_code="BUCK", status="resolved")
        self.assertEqual({row["hostname"] for row in result.data["alerts"]}, {"BUCK-SRV01", "BUCK-PC03"})

    def test_needs_a_client_or_hostname(self) -> None:
        result = self.run_tool("datto_alerts")
        self.assertFalse(result.ok)
        self.assertIn("Name a client code (or all clients) or a device hostname", result.reply)

    def test_unknown_hostname(self) -> None:
        self.assertIn("no device named BUCK-SRV09", self.run_tool("datto_alerts", hostname="BUCK-SRV09").reply)


class SummaryPatchSoftwareTests(DattoToolTestCase):
    def test_site_summary_counts(self) -> None:
        result = self.run_tool("datto_site_summary", client_code="BUCK")
        self.assertIn("- Workstations: 4; servers: 1", result.reply)
        self.assertIn("- Online: 4; offline: 1 (BUCK-LT01)", result.reply)
        self.assertIn("- Missing patches: 3 (BUCK-PC02, BUCK-PC03, BUCK-SRV01)", result.reply)
        self.assertIn("- Open alerts: 1", result.reply)
        self.assertEqual(result.data["workstations"], 4)

    def test_site_summary_for_all(self) -> None:
        result = self.run_tool("datto_site_summary", client_code="all")
        self.assertIn("- WDON Western Dental: 6 devices", result.reply)
        self.assertIn("takes longer", result.reply)

    def test_patch_report_names_the_worst(self) -> None:
        result = self.run_tool("datto_patch_report", client_code="WDON")
        self.assertIn("3 of 6 devices fully patched, 2 behind.", result.reply)
        self.assertIn("Behind, worst first:\n- WDON-IMG-01: patch install errors, 3 pending\n- WDON-FD01: 4 approved patches pending", result.reply)

    def test_patch_report_ranks_every_client(self) -> None:
        result = self.run_tool("datto_patch_report", client_code="all")
        self.assertEqual([row["client_code"] for row in result.data], ["BUCK", "NGMC", "WDON"])
        self.assertIn("- BUCK: 3 of 5 devices behind (60%), 20 approved patches pending", result.reply)
        self.assertIn("takes longer", result.reply)

    def test_software_across_a_client(self) -> None:
        result = self.run_tool("datto_device_software", client_code="WDON", q="dentrix")
        self.assertTrue(result.reply.startswith("dentrix is installed on 4 of 6 devices at Western Dental (WDON)"))
        self.assertIn("- WDON-OP03: Dentrix G6 6.9.1.5", result.reply)
        self.assertIn("Not found on: WDON-IMG-01, WDON-LT01", result.reply)
        self.assertIn("slower", result.reply)

    def test_software_on_one_device(self) -> None:
        result = self.run_tool("datto_device_software", hostname="BUCK-PC03", q="Chrome")
        self.assertEqual(result.reply, "BUCK-PC03 has 1 match for Chrome in its software audit:\n- Google Chrome 121.0.6167.140")
        everything = self.run_tool("datto_device_software", hostname="NGMC-DC01")
        self.assertIn("NGMC-DC01 has 2 programs in its software audit:", everything.reply)

    def test_client_wide_software_search_needs_a_program(self) -> None:
        result = self.run_tool("datto_device_software", client_code="WDON")
        self.assertFalse(result.ok)
        self.assertIn("Which program?", result.reply)


class FailureTests(DattoToolTestCase):
    def test_datto_not_reachable_is_a_plain_reply(self) -> None:
        datto = StubDattoSource(now=lambda: NOW)
        self.sources = _sources(datto)
        with mock.patch.object(datto, "list_sites", side_effect=DattoUnavailable("Datto RMM is not reachable at https://x (ConnectTimeout).")):
            for name, arguments in (
                ("datto_list_devices", {"client_code": "WDON"}),
                ("datto_alerts", {"client_code": "all"}),
                ("datto_patch_report", {"client_code": "BUCK"}),
            ):
                with self.subTest(name):
                    self.sources.clients.clear()
                    result = self.run_tool(name, **arguments)
                    self.assertFalse(result.ok)
                    self.assertTrue(result.reply.startswith("Datto RMM could not be reached, so I can't check that right now."))

    def test_datto_not_configured_is_a_plain_reply(self) -> None:
        datto = StubDattoSource(now=lambda: NOW)
        self.sources = _sources(datto)
        with mock.patch.object(datto, "list_account_devices", side_effect=DattoNotConfigured("DATTO_API_KEY is empty.")):
            result = self.run_tool("datto_get_device", hostname="WDON-FD01")
        self.assertEqual(result.reply, "Datto RMM is not set up in tb-brain: DATTO_API_KEY is empty.")

    def test_bad_arguments_are_reported(self) -> None:
        result = self.run_tool("datto_get_device")
        self.assertFalse(result.ok)
        self.assertIn("Invalid arguments", result.error)


class SchemaTests(unittest.TestCase):
    def test_every_tool_is_prefixed_and_both_styles_match(self) -> None:
        self.assertEqual(len(TOOL_NAMES), 6)
        self.assertTrue(all(name.startswith("datto_") for name in TOOL_NAMES))
        self.assertEqual([t["function"]["name"] for t in OPENAI_TOOLS], list(TOOL_NAMES))
        self.assertEqual([t["function"]["name"] for t in LEAN_OPENAI_TOOLS], list(TOOL_NAMES))
        for legacy, lean in zip(OPENAI_TOOLS, LEAN_OPENAI_TOOLS):
            self.assertEqual(legacy["function"]["parameters"], lean["function"]["parameters"])
            self.assertLess(len(lean["function"]["description"]), len(legacy["function"]["description"]))

    def test_descriptions_say_read_only_where_a_write_could_be_asked(self) -> None:
        for tool in OPENAI_TOOLS:
            name = tool["function"]["name"]
            if name in ("datto_list_devices", "datto_get_device", "datto_alerts", "datto_patch_report", "datto_device_software"):
                self.assertIn("Read-only", tool["function"]["description"], name)

    def test_no_write_tool(self) -> None:
        self.assertEqual(DATTO.write_tool_names, ())
        for verb in ("reboot", "run", "approve", "resolve", "install"):
            self.assertFalse(any(verb in name for name in TOOL_NAMES))

    def test_helpers(self) -> None:
        self.assertEqual(short_os("Microsoft Windows Server 2019 Standard 10.0.17763"), "Windows Server 2019 Standard")
        self.assertEqual(ago(None, NOW), "never")
        self.assertEqual(ago(NOW, NOW), "just now")
        self.assertGreaterEqual(MAX_LINES, 20)


if __name__ == "__main__":
    unittest.main()
