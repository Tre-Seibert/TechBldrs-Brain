"""Datto RMM HTTP adapter against mocked responses only: token, paging, token refresh, 429 back-off, errors,
caching, and that the key and secret never show up anywhere.
"""

from __future__ import annotations

import tests._isolation  # noqa: F401  (first: hermetic settings, no network)

import logging
import unittest
from typing import Any
from urllib.parse import parse_qs

import httpx

from app.config import Settings
from app.sources.datto.factory import build_datto_source
from app.sources.datto.http import HttpDattoSource
from app.sources.datto.protocol import DattoNotConfigured, DattoRequestError, DattoUnavailable
from app.sources.datto.stub import StubDattoSource

BASE = "https://vidal-api.centrastage.net"
KEY = "tb-brain-key-abc123"
SECRET = "tb-brain-secret-xyz789"


def _device(n: int, site: str = "WDON Western Dental") -> dict[str, Any]:
    return {"uid": f"dev-{n}", "id": n, "hostname": f"WDON-PC{n:02d}", "siteUid": "site-1", "siteName": site,
            "online": True, "lastSeen": 1791900000000, "deviceType": {"category": "Desktop"}}


class FakeDatto:
    """A scripted Datto RMM: answers each request from `routes`, records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.tokens_issued = 0
        self.queue: dict[str, list[httpx.Response]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/auth/oauth/token":
            self.tokens_issued += 1
            return httpx.Response(200, json={"access_token": f"token-{self.tokens_issued}", "token_type": "bearer", "expires_in": 359999})
        queued = self.queue.get(path)
        if queued:
            return queued.pop(0)
        return httpx.Response(404, json={"message": f"no route {path}"})

    def add(self, path: str, *responses: httpx.Response) -> None:
        self.queue.setdefault(path, []).extend(responses)

    def gets(self, path: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path == path]


class HttpDattoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeDatto()
        self.slept: list[float] = []
        self.clock = [1000.0]
        self.datto = HttpDattoSource(
            base_url=BASE + "/",
            api_key=KEY,
            api_secret=SECRET,
            transport=httpx.MockTransport(self.fake),
            sleep=self.slept.append,
            clock=lambda: self.clock[0],
        )

    def test_token_request_follows_datto_and_flow(self) -> None:
        self.fake.add("/api/v2/account/sites", httpx.Response(200, json={"sites": [], "pageDetails": {"nextPageUrl": None}}))
        self.datto.list_sites()
        token = self.fake.requests[0]
        self.assertEqual(str(token.url), f"{BASE}/auth/oauth/token")
        self.assertEqual(token.method, "POST")
        self.assertEqual(token.headers["Authorization"], "Basic cHVibGljLWNsaWVudDpwdWJsaWM=")  # public-client:public
        form = parse_qs(token.content.decode())
        self.assertEqual(form, {"grant_type": ["password"], "username": [KEY], "password": [SECRET]})
        sites = self.fake.gets("/api/v2/account/sites")[0]
        self.assertEqual(sites.headers["Authorization"], "Bearer token-1")
        self.assertEqual(sites.url.params["max"], "250")

    def test_paging_follows_next_page_url_until_null(self) -> None:
        path = "/api/v2/site/site-1/devices"
        self.fake.add(
            path,
            httpx.Response(200, json={"devices": [_device(1), _device(2)], "pageDetails": {"count": 2, "nextPageUrl": f"{BASE}{path}?max=250&page=1"}}),
            httpx.Response(200, json={"devices": [_device(3)], "pageDetails": {"count": 1, "nextPageUrl": f"{BASE}{path}?max=250&page=2"}}),
            httpx.Response(200, json={"devices": [], "pageDetails": {"count": 0, "nextPageUrl": None}}),
        )
        devices = self.datto.list_site_devices(site_uid="site-1")
        self.assertEqual([device.hostname for device in devices], ["WDON-PC01", "WDON-PC02", "WDON-PC03"])
        pages = self.fake.gets(path)
        self.assertEqual([request.url.params.get("page") for request in pages], [None, "1", "2"])
        self.assertEqual(self.fake.tokens_issued, 1)
        self.assertEqual(devices[0].last_seen.year, 2026)  # epoch milliseconds parse too

    def test_next_page_on_another_host_is_refused(self) -> None:
        self.fake.add(
            "/api/v2/account/devices",
            httpx.Response(200, json={"devices": [_device(1)], "pageDetails": {"nextPageUrl": "https://evil.example/api/v2/account/devices?page=1"}}),
        )
        with self.assertRaises(DattoRequestError):
            self.datto.list_account_devices()
        self.assertFalse(any(request.url.host == "evil.example" for request in self.fake.requests))

    def test_expired_token_is_renewed_once(self) -> None:
        self.fake.add(
            "/api/v2/device/dev-1",
            httpx.Response(401, json={"error": "invalid_token"}),
            httpx.Response(200, json=_device(1)),
        )
        device = self.datto.get_device(device_uid="dev-1")
        self.assertEqual(device.hostname, "WDON-PC01")
        self.assertEqual(self.fake.tokens_issued, 2)
        self.assertEqual([r.headers["Authorization"] for r in self.fake.gets("/api/v2/device/dev-1")], ["Bearer token-1", "Bearer token-2"])

    def test_a_second_401_is_not_retried_forever(self) -> None:
        self.fake.add("/api/v2/device/dev-1", httpx.Response(401), httpx.Response(401), httpx.Response(200, json=_device(1)))
        with self.assertRaises(DattoNotConfigured):
            self.datto.get_device(device_uid="dev-1")
        self.assertEqual(len(self.fake.gets("/api/v2/device/dev-1")), 2)

    def test_token_is_reused_until_it_nearly_expires(self) -> None:
        for _ in range(2):
            self.fake.add("/api/v2/device/dev-1", httpx.Response(200, json=_device(1)))
        self.datto.get_device(device_uid="dev-1")
        self.clock[0] += 359999 - 60  # inside the renewal margin
        self.datto.get_device(device_uid="dev-1")
        self.assertEqual(self.fake.tokens_issued, 2)

    def test_429_waits_retry_after_then_succeeds(self) -> None:
        self.fake.add(
            "/api/v2/account/alerts/open",
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(429),
            httpx.Response(200, json={"alerts": [], "pageDetails": {"nextPageUrl": None}}),
        )
        with self.assertLogs("tb_brain.datto", level=logging.WARNING):
            self.assertEqual(self.datto.list_account_alerts(status="open"), [])
        self.assertEqual(self.slept, [7.0, 15.0])  # Retry-After, then the back-off schedule

    def test_429_gives_up_after_its_retries(self) -> None:
        self.fake.add("/api/v2/account/sites", *[httpx.Response(429) for _ in range(5)])
        with self.assertLogs("tb_brain.datto", level=logging.WARNING), self.assertRaises(DattoUnavailable) as caught:
            self.datto.list_sites()
        self.assertIn("rate limiting", str(caught.exception))
        self.assertEqual(len(self.fake.gets("/api/v2/account/sites")), 3)
        self.assertEqual(self.slept, [5.0, 15.0])

    def test_retry_after_is_capped(self) -> None:
        self.fake.add(
            "/api/v2/account/sites",
            httpx.Response(429, headers={"Retry-After": "900"}),
            httpx.Response(200, json={"sites": [], "pageDetails": {}}),
        )
        with self.assertLogs("tb_brain.datto", level=logging.WARNING):
            self.datto.list_sites()
        self.assertEqual(self.slept, [60.0])

    def test_403_after_a_429_means_datto_paused_us(self) -> None:
        self.fake.add("/api/v2/account/sites", httpx.Response(429), httpx.Response(403))
        with self.assertLogs("tb_brain.datto", level=logging.WARNING), self.assertRaises(DattoUnavailable) as caught:
            self.datto.list_sites()
        self.assertIn("paused", str(caught.exception))

    def test_refusals_and_server_errors(self) -> None:
        self.fake.add("/api/v2/device/missing", httpx.Response(404, json={"message": "Device not found"}))
        with self.assertRaises(DattoRequestError) as caught:
            self.datto.get_device(device_uid="missing")
        self.assertEqual(str(caught.exception), "Datto RMM 404: Device not found")
        self.fake.add("/api/v2/device/dev-1", httpx.Response(503, text="down"))
        with self.assertRaises(DattoUnavailable):
            self.datto.get_device(device_uid="dev-1")

    def test_unreachable(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out", request=request)

        datto = HttpDattoSource(base_url=BASE, api_key=KEY, api_secret=SECRET, transport=httpx.MockTransport(refuse))
        with self.assertRaises(DattoUnavailable) as caught:
            datto.list_sites()
        self.assertEqual(str(caught.exception), f"Datto RMM is not reachable at {BASE} (ConnectTimeout).")

    def test_bad_key_or_secret(self) -> None:
        datto = HttpDattoSource(
            base_url=BASE, api_key=KEY, api_secret=SECRET,
            transport=httpx.MockTransport(lambda request: httpx.Response(400, json={"error": "invalid_grant"})),
        )
        with self.assertRaises(DattoNotConfigured) as caught:
            datto.list_sites()
        self.assertIn("rejected tb-brain's API key and secret (HTTP 400)", str(caught.exception))

    def test_missing_settings_fail_before_any_request(self) -> None:
        for kwargs in ({"base_url": "", "api_key": KEY, "api_secret": SECRET}, {"base_url": BASE, "api_key": KEY, "api_secret": ""}):
            datto = HttpDattoSource(**kwargs, transport=httpx.MockTransport(self.fake))
            with self.assertRaises(DattoNotConfigured):
                datto.list_sites()
        self.assertEqual(self.fake.requests, [])

    def test_site_and_device_lists_are_cached_briefly(self) -> None:
        sites = {"sites": [{"uid": "site-1", "name": "WDON Western Dental"}], "pageDetails": {}}
        self.fake.add("/api/v2/account/sites", httpx.Response(200, json=sites), httpx.Response(200, json=sites))
        self.datto.list_sites()
        self.datto.list_sites()
        self.assertEqual(len(self.fake.gets("/api/v2/account/sites")), 1)
        self.clock[0] += 61
        self.datto.list_sites()
        self.assertEqual(len(self.fake.gets("/api/v2/account/sites")), 2)

    def test_alerts_are_not_cached(self) -> None:
        for _ in range(2):
            self.fake.add("/api/v2/site/site-1/alerts/open", httpx.Response(200, json={"alerts": [], "pageDetails": {}}))
        self.datto.list_site_alerts(site_uid="site-1")
        self.datto.list_site_alerts(site_uid="site-1")
        self.assertEqual(len(self.fake.gets("/api/v2/site/site-1/alerts/open")), 2)

    def test_every_read_hits_the_documented_path(self) -> None:
        page = lambda key: httpx.Response(200, json={key: [], "pageDetails": {}})  # noqa: E731
        self.fake.add("/api/v2/account/devices", page("devices"))
        self.fake.add("/api/v2/audit/device/dev-1", httpx.Response(200, json={"logicalDisks": []}))
        self.fake.add("/api/v2/audit/device/dev-1/software", page("software"))
        self.fake.add("/api/v2/device/dev-1/alerts/resolved", page("alerts"))
        self.fake.add("/api/v2/site/site-1/alerts/resolved", page("alerts"))
        self.datto.list_account_devices(hostname="WDON-FD01")
        self.datto.get_device_audit(device_uid="dev-1")
        self.datto.list_device_software(device_uid="dev-1")
        self.datto.list_device_alerts(device_uid="dev-1", status="resolved")
        self.datto.list_site_alerts(site_uid="site-1", status="resolved")
        self.assertEqual(self.fake.gets("/api/v2/account/devices")[0].url.params["hostname"], "WDON-FD01")
        self.assertTrue(all(request.method in ("GET", "POST") for request in self.fake.requests))
        self.assertEqual([r.url.path for r in self.fake.requests if r.method == "POST"], ["/auth/oauth/token"])
        with self.assertRaises(ValueError):
            self.datto.list_site_alerts(site_uid="site-1", status="muted")

    def test_secrets_never_show(self) -> None:
        self.assertNotIn(KEY, repr(self.datto))
        self.assertNotIn(SECRET, repr(self.datto))
        settings = Settings(datto_mode="http", datto_api_url=BASE, datto_api_key=KEY, datto_api_secret=SECRET, _env_file=None)
        self.assertNotIn(SECRET, repr(settings))
        self.assertNotIn(KEY, str(settings.model_dump()))
        failures = [
            httpx.MockTransport(lambda request: httpx.Response(400)),
            httpx.MockTransport(lambda request: httpx.Response(500)),
        ]
        with self.assertLogs("tb_brain", level=logging.DEBUG) as logs:
            logging.getLogger("tb_brain.datto").debug("probe")
            for transport in failures:
                datto = HttpDattoSource(base_url=BASE, api_key=KEY, api_secret=SECRET, transport=transport)
                with self.assertRaises((DattoNotConfigured, DattoUnavailable)) as caught:
                    datto.list_sites()
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertNotIn(KEY, str(caught.exception))
        self.assertFalse(any(SECRET in line or KEY in line for line in logs.output))


class FactoryTests(unittest.TestCase):
    def test_modes(self) -> None:
        self.assertIsInstance(build_datto_source(Settings(datto_mode="stub", _env_file=None)), StubDattoSource)
        http = build_datto_source(
            Settings(datto_mode="HTTP", datto_api_url=BASE, datto_api_key=KEY, datto_api_secret=SECRET, _env_file=None)
        )
        self.assertIsInstance(http, HttpDattoSource)
        self.assertEqual(http.source_name, "datto-http")

    def test_unknown_mode_is_rejected_at_startup(self) -> None:
        with self.assertRaises(ValueError):
            Settings(datto_mode="db", _env_file=None)


if __name__ == "__main__":
    unittest.main()
