"""Client resolver (app/sources/clients.py): code or name -> client code, tenant id, IT Glue org, Datto site.

Runs against stub Flow. The fixture clients: WDON Western Dental and ACME Acme Labs (no tenant, no IT Glue),
WDLB Western Dental Lab (both), NRTH Northwind Traders (tenant only), BRCK Brickell Law Group (IT Glue only).
"""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from typing import Any
from unittest import mock

import httpx

from app.config import Settings
from app.sources.base import Sources, SourceSpec
from app.sources.clients import ClientResolver, Outcome
from app.sources.flow.http import HttpFlowSource
from app.sources.flow.protocol import FlowNotConfigured, FlowRequestError
from app.sources.flow.schemas import itglue_org_id
from app.sources.flow.spec import FLOW
from app.sources.flow.stub import StubFlowSource
from app.sources.registry import build_sources
from app.tools.registry import openai_tools


class CountingFlow:
    """Stub Flow that counts the client reads the resolver makes, optionally slowly or failing."""

    source_name = "stub"

    def __init__(self, *, delay: float = 0.0) -> None:
        self._stub = StubFlowSource()
        self._delay = delay
        self._lock = threading.Lock()
        self.get_client_calls: list[str] = []
        self.list_clients_calls = 0
        self.fail_with: Exception | None = None

    def get_client(self, *, client_code: str):
        with self._lock:
            self.get_client_calls.append(client_code)
        time.sleep(self._delay)
        if self.fail_with is not None:
            raise self.fail_with
        return self._stub.get_client(client_code=client_code)

    def list_clients(self, *, status: str | None = "Client"):
        with self._lock:
            self.list_clients_calls += 1
        time.sleep(self._delay)
        if self.fail_with is not None:
            raise self.fail_with
        return self._stub.list_clients(status=status)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# Datto's rule, as a fake: the site whose name starts with the client code.
_DATTO_SITES = [
    {"uid": "site-wdon-1", "name": "WDON - Western Dental Main"},
    {"uid": "site-nrth-1", "name": "NRTH Northwind HQ"},
    {"uid": "site-other", "name": "Old WDLB office"},
]


def _datto_site_for(client_code: str, name: str | None) -> str | None:
    return next((site["uid"] for site in _DATTO_SITES if site["name"].upper().startswith(client_code)), None)


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = ClientResolver(StubFlowSource())

    def test_exact_code(self) -> None:
        result = self.resolver.resolve("WDON")
        self.assertIs(result.outcome, Outcome.FOUND)
        self.assertTrue(result.ok)
        self.assertEqual(result.client.client_code, "WDON")
        self.assertEqual(result.client.name, "Western Dental")
        self.assertEqual(result.client.label, "Western Dental (WDON)")
        self.assertEqual(result.message, "")

    def test_lowercase_code(self) -> None:
        for text in ("wdon", " Wdon ", "'wdon'"):
            result = self.resolver.resolve(text)
            self.assertIs(result.outcome, Outcome.FOUND, text)
            self.assertEqual(result.client.client_code, "WDON")

    def test_code_wins_over_a_name(self) -> None:
        # "acme" is both a code and the start of a name; the code is checked first.
        self.assertEqual(self.resolver.resolve("acme").client.client_code, "ACME")

    def test_exact_name(self) -> None:
        # Exact beats close: "Western Dental" is also the start of "Western Dental Lab".
        for text in ("Western Dental", "western dental", "The Western Dental, Inc.", "Western  Dental."):
            result = self.resolver.resolve(text)
            self.assertIs(result.outcome, Outcome.FOUND, text)
            self.assertEqual(result.client.client_code, "WDON")
        self.assertEqual(self.resolver.resolve("Western Dental Lab").client.client_code, "WDLB")

    def test_close_name(self) -> None:
        cases = {
            "Northwind": "NRTH",  # leading word
            "brickell law": "BRCK",
            "Dental Lab": "WDLB",
            "Nortwind Traders": "NRTH",  # typo
            "Westren Dental Lab": "WDLB",
            "Brickel": "BRCK",
        }
        for text, code in cases.items():
            result = self.resolver.resolve(text)
            self.assertIs(result.outcome, Outcome.FOUND, text)
            self.assertEqual(result.client.client_code, code, text)

    def test_former_clients_resolve_too(self) -> None:
        self.assertEqual(self.resolver.resolve("Brickell Law Group").client.client_code, "BRCK")

    def test_ambiguous_name_lists_candidates(self) -> None:
        for text in ("Western", "western dent", "Westren"):
            result = self.resolver.resolve(text)
            self.assertIs(result.outcome, Outcome.AMBIGUOUS, text)
            self.assertIsNone(result.client)
            self.assertEqual([c.client_code for c in result.candidates], ["WDON", "WDLB"])
            self.assertIn("Western Dental (WDON), Western Dental Lab (WDLB)", result.message)
            self.assertTrue(result.message.endswith("Which one?"))

    def test_ambiguous_exact_name(self) -> None:
        flow = StubFlowSource()
        rows = flow.list_clients(status=None)
        twin = rows[0].model_copy(update={"client_code": "WDN2", "name": "Western Dental Lab"})
        with mock.patch.object(StubFlowSource, "list_clients", return_value=[*rows, twin]):
            result = ClientResolver(flow).resolve("Western Dental Lab")
        self.assertIs(result.outcome, Outcome.AMBIGUOUS)
        self.assertEqual({c.client_code for c in result.candidates}, {"WDLB", "WDN2"})

    def test_candidates_are_capped(self) -> None:
        flow = StubFlowSource()
        rows = [
            flow.list_clients(status=None)[0].model_copy(update={"client_code": f"SM{i:02d}", "name": f"Smith Dental {i}"})
            for i in range(8)
        ]
        with mock.patch.object(StubFlowSource, "list_clients", return_value=rows):
            result = ClientResolver(flow).resolve("Smith")
        self.assertIs(result.outcome, Outcome.AMBIGUOUS)
        self.assertEqual(len(result.candidates), 5)

    def test_unknown(self) -> None:
        for text in ("Globex", "ZZZZ", "zz"):
            result = self.resolver.resolve(text)
            self.assertIs(result.outcome, Outcome.UNKNOWN, text)
            self.assertIsNone(result.client)
            self.assertEqual(result.candidates, ())
            self.assertIn(f'No client in Flow matches "{text}"', result.message)
        for text in ("", "  ", None):
            result = self.resolver.resolve(text)
            self.assertIs(result.outcome, Outcome.UNKNOWN)
            self.assertTrue(result.message.startswith("Which client?"))

    def test_tenant_id_from_client_detail(self) -> None:
        result = self.resolver.resolve("WDLB", require=("m365_tenant_id",))
        self.assertIs(result.outcome, Outcome.FOUND)
        self.assertEqual(result.client.m365_tenant_id, "0f5d7c1e-58b0-4b1a-9c11-3a7e5d2f0b58")
        self.assertEqual(self.resolver.resolve("Northwind Traders").client.m365_tenant_id, "6a2e9b40-71c3-4d8e-8f20-5b1c0d9e7a63")

    def test_no_tenant(self) -> None:
        for text, label in (("Western Dental", "Western Dental (WDON)"), ("BRCK", "Brickell Law Group (BRCK)")):
            result = self.resolver.resolve(text, require=("m365_tenant_id",))
            self.assertIs(result.outcome, Outcome.NO_M365_TENANT, text)
            self.assertFalse(result.ok)
            self.assertIsNotNone(result.client)
            self.assertIsNone(result.client.m365_tenant_id)
            self.assertIn(f"{label} has no Microsoft 365 tenant id on record in Flow", result.message)
        # Not required: the same client is simply found, with no tenant.
        self.assertIs(self.resolver.resolve("WDON").outcome, Outcome.FOUND)

    def test_blank_tenant_counts_as_none(self) -> None:
        flow = StubFlowSource()
        detail = flow.get_client(client_code="WDLB").model_copy(update={"m365_tenant_id": "   "})
        with mock.patch.object(StubFlowSource, "get_client", return_value=detail):
            result = ClientResolver(flow).resolve("WDLB", require=("m365_tenant_id",))
        self.assertIs(result.outcome, Outcome.NO_M365_TENANT)

    def test_itglue_org_from_url(self) -> None:
        self.assertEqual(self.resolver.resolve("WDLB").client.itglue_org_id, 4815162)
        self.assertEqual(self.resolver.resolve("BRCK").client.itglue_org_id, 2342)
        result = self.resolver.resolve("NRTH", require=("itglue_org_id",))
        self.assertIs(result.outcome, Outcome.NO_ITGLUE_ORG)
        self.assertIn("Northwind Traders (NRTH) has no IT Glue organization", result.message)

    def test_itglue_url_parsing(self) -> None:
        cases = {
            "https://techbldrs.itglue.com/4815162": 4815162,
            "https://techbldrs.itglue.com/4815162/": 4815162,
            "https://techbldrs.itglue.com/2342/organizations/2342": 2342,
            "https://techbldrs.itglue.com/123/docs/9": 123,
            "https://techbldrs.itglue.com/organizations": None,
            "https://techbldrs.itglue.com/12ab/": None,
            "": None,
            None: None,
        }
        for url, expected in cases.items():
            self.assertEqual(itglue_org_id(url), expected, url)

    def test_unknown_requirement_is_a_bug(self) -> None:
        with self.assertRaises(ValueError):
            self.resolver.resolve("WDON", require=("tenant",))

    def test_flow_errors_other_than_not_found_raise(self) -> None:
        flow = CountingFlow()
        flow.fail_with = FlowRequestError("Flow 403: forbidden")
        with self.assertRaises(FlowRequestError):
            ClientResolver(flow).resolve("WDON")
        flow.fail_with = FlowNotConfigured("FLOW_BASE_URL is empty")
        with self.assertRaises(FlowNotConfigured):
            ClientResolver(flow).resolve("Western Dental")

    def test_async_resolve(self) -> None:
        result = asyncio.run(self.resolver.aresolve("wdlb", require=["m365_tenant_id"]))
        self.assertIs(result.outcome, Outcome.FOUND)
        self.assertEqual(result.client.client_code, "WDLB")


class DattoHookTests(unittest.TestCase):
    def test_no_datto_source_leaves_site_none(self) -> None:
        resolver = ClientResolver(StubFlowSource())
        self.assertIsNone(resolver.resolve("WDON").client.datto_site_uid)
        result = resolver.resolve("WDON", require=("datto_site_uid",))
        self.assertIs(result.outcome, Outcome.NO_DATTO_SITE)
        self.assertIn("Datto RMM is not enabled", result.message)

    def test_hook_fills_the_site(self) -> None:
        resolver = ClientResolver(StubFlowSource())
        resolver.register_id_hook("datto_site_uid", _datto_site_for, source="datto")
        result = resolver.resolve("Western Dental", require=("datto_site_uid",))
        self.assertIs(result.outcome, Outcome.FOUND)
        self.assertEqual(result.client.datto_site_uid, "site-wdon-1")
        # "Old WDLB office" does not start with WDLB, so WDLB has no site.
        result = resolver.resolve("WDLB", require=("datto_site_uid",))
        self.assertIs(result.outcome, Outcome.NO_DATTO_SITE)
        self.assertEqual(result.client.client_code, "WDLB")
        self.assertIn("Western Dental Lab (WDLB) has no Datto RMM site", result.message)

    def test_hook_gets_code_and_name(self) -> None:
        seen: list[tuple[str, str | None]] = []
        resolver = ClientResolver(StubFlowSource())
        resolver.register_id_hook("datto_site_uid", lambda code, name: seen.append((code, name)) or None, source="datto")
        resolver.resolve("northwind")
        self.assertEqual(seen, [("NRTH", "Northwind Traders")])

    def test_hook_cannot_set_flow_fields_or_register_twice(self) -> None:
        resolver = ClientResolver(StubFlowSource())
        with self.assertRaises(ValueError):
            resolver.register_id_hook("m365_tenant_id", _datto_site_for, source="datto")
        resolver.register_id_hook("datto_site_uid", _datto_site_for, source="datto")
        with self.assertRaises(ValueError):
            resolver.register_id_hook("datto_site_uid", _datto_site_for, source="other")

    def test_failing_hook_only_fails_callers_that_need_it(self) -> None:
        def down(code: str, name: str | None) -> str | None:
            raise httpx.ConnectError("datto unreachable")

        resolver = ClientResolver(StubFlowSource())
        resolver.register_id_hook("datto_site_uid", down, source="datto")
        with self.assertLogs("tb_brain.clients", "WARNING"):
            result = resolver.resolve("WDLB", require=("m365_tenant_id",))
        self.assertIs(result.outcome, Outcome.FOUND)
        self.assertIsNone(result.client.datto_site_uid)
        with self.assertRaises(httpx.ConnectError):
            resolver.resolve("WDLB", require=("datto_site_uid",))

    def test_source_spec_registers_its_hook(self) -> None:
        def site_hook(client: Any, client_code: str, name: str | None) -> str | None:
            return client.site_for(client_code)

        class FakeDatto:
            source_name = "datto-stub"

            def site_for(self, client_code: str) -> str | None:
                return _datto_site_for(client_code, None)

        datto = SourceSpec(
            name="datto",
            mode=lambda settings: "stub",
            build=lambda settings: FakeDatto(),
            tools=(),
            handle=lambda *args, **kwargs: None,
            client_id_hooks=(("datto_site_uid", site_hook),),
        )
        sources = Sources([(FLOW, StubFlowSource()), (datto, FakeDatto())])
        self.assertIs(sources.clients, sources.clients)
        self.assertEqual(sources.clients.resolve("NRTH").client.datto_site_uid, "site-nrth-1")
        # Flow alone: no hook, no site.
        flow_only = build_sources(Settings(_env_file=None, flow_mode="stub", brain_data_dir="C:/nonexistent-tb-brain-test"))
        self.assertIsNone(flow_only.clients.resolve("NRTH").client.datto_site_uid)


class CacheTests(unittest.TestCase):
    def test_repeat_lookups_hit_the_cache(self) -> None:
        flow = CountingFlow()
        resolver = ClientResolver(flow)
        for _ in range(3):
            resolver.resolve("WDLB")
            resolver.resolve("Western Dental Lab", require=("m365_tenant_id",))
            resolver.resolve("Westren Dental Lab")
        self.assertEqual(flow.get_client_calls, ["WDLB"])
        self.assertEqual(flow.list_clients_calls, 1)

    def test_not_found_is_cached_too(self) -> None:
        flow = CountingFlow()
        resolver = ClientResolver(flow)
        resolver.resolve("ZZZZ")
        resolver.resolve("zzzz")
        self.assertEqual(flow.get_client_calls, ["ZZZZ"])

    def test_entries_expire(self) -> None:
        flow = CountingFlow()
        clock = Clock()
        resolver = ClientResolver(flow, ttl=300, clock=clock)
        resolver.resolve("Northwind")
        clock.now += 299
        resolver.resolve("Northwind")
        self.assertEqual((len(flow.get_client_calls), flow.list_clients_calls), (1, 1))
        clock.now += 2
        resolver.resolve("Northwind")
        self.assertEqual((len(flow.get_client_calls), flow.list_clients_calls), (2, 2))

    def test_failures_are_not_cached(self) -> None:
        flow = CountingFlow()
        resolver = ClientResolver(flow)
        flow.fail_with = FlowNotConfigured("Flow brain API unreachable")
        with self.assertRaises(FlowNotConfigured):
            resolver.resolve("WDON")
        flow.fail_with = None
        self.assertIs(resolver.resolve("WDON").outcome, Outcome.FOUND)
        self.assertEqual(flow.get_client_calls, ["WDON", "WDON"])

    def test_hook_results_are_cached(self) -> None:
        calls: list[str] = []
        resolver = ClientResolver(StubFlowSource())
        resolver.register_id_hook("datto_site_uid", lambda code, name: calls.append(code) or None, source="datto")
        resolver.resolve("WDON")
        resolver.resolve("wdon", require=("datto_site_uid",))
        self.assertEqual(calls, ["WDON"])

    def test_clear(self) -> None:
        flow = CountingFlow()
        resolver = ClientResolver(flow)
        resolver.resolve("WDON")
        resolver.clear()
        resolver.resolve("WDON")
        self.assertEqual(flow.get_client_calls, ["WDON", "WDON"])

    def test_concurrent_threads_fetch_once(self) -> None:
        flow = CountingFlow(delay=0.05)
        resolver = ClientResolver(flow)
        results: list[Any] = []
        start = threading.Barrier(8)

        def ask() -> None:
            start.wait()
            results.append(resolver.resolve("Northwind Traders", require=("m365_tenant_id",)))

        threads = [threading.Thread(target=ask) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([r.client.client_code for r in results], ["NRTH"] * 8)
        self.assertEqual(flow.list_clients_calls, 1)
        self.assertEqual(flow.get_client_calls, ["NRTH"])

    def test_concurrent_coroutines_fetch_once(self) -> None:
        flow = CountingFlow(delay=0.05)
        resolver = ClientResolver(flow)

        async def ask_all() -> list[Any]:
            return await asyncio.gather(*(resolver.aresolve(text) for text in ("wdlb", "WDLB", "Western Dental Lab") * 3))

        results = asyncio.run(ask_all())
        self.assertEqual({r.client.client_code for r in results}, {"WDLB"})
        self.assertEqual(flow.get_client_calls, ["WDLB"])
        self.assertLessEqual(flow.list_clients_calls, 1)

    def test_sources_bundle_shares_one_resolver(self) -> None:
        sources = build_sources(Settings(_env_file=None, flow_mode="stub", brain_data_dir="C:/nonexistent-tb-brain-test"))
        self.assertIs(sources.clients, sources.clients)


class FlowClientListTests(unittest.TestCase):
    def test_stub_roster_filters_by_status(self) -> None:
        stub = StubFlowSource()
        everyone = stub.list_clients(status=None)
        self.assertEqual([row.client_code for row in everyone], ["ACME", "BRCK", "NRTH", "WDLB", "WDON"])
        self.assertEqual({row.client_code for row in everyone if row.has_itglue}, {"BRCK", "WDLB"})
        self.assertEqual([row.client_code for row in stub.list_clients(status="Former")], ["BRCK"])

    def test_http_roster_asks_flow_for_every_status(self) -> None:
        source = HttpFlowSource(base_url="https://flow.example", token="token")
        payload = {"ok": True, "read_only": True, "data": [{"client_code": "WDON", "name": "Western Dental", "has_itglue": False}]}
        response = httpx.Response(200, json=payload, request=httpx.Request("GET", "https://flow.example/api/private/brain/clients"))
        with mock.patch("app.sources.flow.http.httpx.get", return_value=response) as get:
            rows = source.list_clients(status=None)
            source.list_clients()
        self.assertEqual([(row.client_code, row.name, row.has_itglue) for row in rows], [("WDON", "Western Dental", False)])
        first, second = get.call_args_list
        self.assertEqual(first.args[0], "https://flow.example/api/private/brain/clients")
        self.assertEqual(first.kwargs["params"], {"status": ""})
        self.assertEqual(second.kwargs["params"], {"status": "Client"})

    def test_http_not_found_client_is_unknown(self) -> None:
        source = HttpFlowSource(base_url="https://flow.example", token="token")
        missing = httpx.Response(
            404,
            json={"error": "client not found"},
            request=httpx.Request("GET", "https://flow.example/api/private/brain/clients/ZZZZ"),
        )
        roster = httpx.Response(200, json={"data": []}, request=httpx.Request("GET", "https://flow.example/x"))
        with mock.patch("app.sources.flow.http.httpx.get", side_effect=[missing, roster]):
            result = ClientResolver(source).resolve("ZZZZ")
        self.assertIs(result.outcome, Outcome.UNKNOWN)

    def test_resolver_is_not_a_model_tool(self) -> None:
        names = {tool["function"]["name"] for tool in openai_tools()}
        self.assertFalse({name for name in names if "resolve" in name or name == "list_clients"})


if __name__ == "__main__":
    unittest.main()
