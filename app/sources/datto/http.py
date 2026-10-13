"""HTTP adapter for the Datto RMM API v2, read-only, with tb-brain's own API user (DATTO_API_KEY / DATTO_API_SECRET).

Auth and paging follow Flow's client (Flow/app/services/datto_rmm.py) and Datto's API docs:

  POST {DATTO_API_URL}/auth/oauth/token        password grant, Basic public-client:public; token lasts 100 hours
  GET  {DATTO_API_URL}/api/v2/account/sites
  GET  {DATTO_API_URL}/api/v2/site/{siteUid}/devices
  GET  {DATTO_API_URL}/api/v2/account/devices?hostname=
  GET  {DATTO_API_URL}/api/v2/device/{deviceUid}
  GET  {DATTO_API_URL}/api/v2/audit/device/{deviceUid}
  GET  {DATTO_API_URL}/api/v2/audit/device/{deviceUid}/software
  GET  {DATTO_API_URL}/api/v2/{site|device}/{uid}/alerts/{open|resolved}, /api/v2/account/alerts/{open|resolved}

Lists come 250 to a page; pageDetails.nextPageUrl is followed until it is null, and only on DATTO_API_URL's own
host, so the token never goes anywhere else. A 401 gets one new token and one retry. Datto allows 600 reads a
minute per account and answers 429 past that, then 403 and a temporary IP block if requests keep coming, so a 429
waits (Retry-After when sent, else a short back-off) and gives up after a few tries rather than pressing on.
Site and device lists are cached for a minute, so one question that looks at several devices lists them once.

The key and secret are never logged, put in an error message, or shown by repr().
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.sources.cache import TtlCache
from app.sources.datto.protocol import ALERT_STATUSES, DattoNotConfigured, DattoRequestError, DattoUnavailable
from app.sources.datto.schemas import DattoAlert, DattoDevice, DattoSite, DeviceAudit, Software

_log = logging.getLogger("tb_brain.datto")

PAGE_SIZE = 250
MAX_PAGES = 40  # 10,000 rows; a guard against a paging loop, far above TechBldrs' account
LIST_CACHE_SECONDS = 60.0
# Waits after a 429 when Datto sends no Retry-After; one retry per entry, then give up.
RETRY_DELAYS = (5.0, 15.0)
MAX_RETRY_AFTER = 60.0
# A token is renewed this long before Datto's expiry, and lasts 100 hours when Datto doesn't say.
TOKEN_MARGIN_SECONDS = 300.0
DEFAULT_TOKEN_SECONDS = 100 * 3600.0
_PUBLIC_CLIENT = "Basic " + base64.b64encode(b"public-client:public").decode("ascii")


class HttpDattoSource:
    source_name = "datto-http"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_secret: str,
        timeout: float = 30.0,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
        cache_seconds: float = LIST_CACHE_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = (base_url or "").strip().rstrip("/")
        self._key = (api_key or "").strip()
        self._secret = (api_secret or "").strip()
        self._retry_delays = retry_delays
        self._sleep = sleep
        self._clock = clock
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._cache = TtlCache(cache_seconds, clock=clock)
        self._token_lock = threading.Lock()
        self._token: str | None = None
        self._token_expires = 0.0

    def __repr__(self) -> str:
        return f"HttpDattoSource(base_url={self._base!r})"

    # --- reads -------------------------------------------------------------------------------------------------

    def list_sites(self) -> list[DattoSite]:
        return self._cache.get(
            ("sites",), lambda: [DattoSite.model_validate(row) for row in self._pages("/v2/account/sites", "sites")]
        )

    def list_site_devices(self, *, site_uid: str) -> list[DattoDevice]:
        path = f"/v2/site/{_seg(site_uid)}/devices"
        return self._cache.get(
            ("site_devices", site_uid), lambda: [DattoDevice.model_validate(row) for row in self._pages(path, "devices")]
        )

    def list_account_devices(self, *, hostname: str | None = None) -> list[DattoDevice]:
        params = {"hostname": hostname} if hostname else {}
        return self._cache.get(
            ("account_devices", (hostname or "").lower()),
            lambda: [DattoDevice.model_validate(row) for row in self._pages("/v2/account/devices", "devices", params)],
        )

    def get_device(self, *, device_uid: str) -> DattoDevice:
        return DattoDevice.model_validate(self._get(f"/v2/device/{_seg(device_uid)}"))

    def get_device_audit(self, *, device_uid: str) -> DeviceAudit:
        return DeviceAudit.model_validate(self._get(f"/v2/audit/device/{_seg(device_uid)}"))

    def list_device_software(self, *, device_uid: str) -> list[Software]:
        path = f"/v2/audit/device/{_seg(device_uid)}/software"
        return self._cache.get(
            ("software", device_uid), lambda: [Software.model_validate(row) for row in self._pages(path, "software")]
        )

    def list_site_alerts(self, *, site_uid: str, status: str = "open") -> list[DattoAlert]:
        return self._alerts(f"/v2/site/{_seg(site_uid)}/alerts/{_status(status)}")

    def list_device_alerts(self, *, device_uid: str, status: str = "open") -> list[DattoAlert]:
        return self._alerts(f"/v2/device/{_seg(device_uid)}/alerts/{_status(status)}")

    def list_account_alerts(self, *, status: str = "open") -> list[DattoAlert]:
        return self._alerts(f"/v2/account/alerts/{_status(status)}")

    def _alerts(self, path: str) -> list[DattoAlert]:
        return [DattoAlert.model_validate(row) for row in self._pages(path, "alerts")]

    # --- transport ---------------------------------------------------------------------------------------------

    def _require_ready(self) -> None:
        if not self._base:
            raise DattoNotConfigured("DATTO_MODE=http but DATTO_API_URL is empty.")
        if not (self._key and self._secret):
            raise DattoNotConfigured(
                "DATTO_MODE=http but DATTO_API_KEY or DATTO_API_SECRET is empty. Set tb-brain's own read-only "
                "Datto API user in the runtime box's .env, or use DATTO_MODE=stub."
            )

    def _access_token(self, *, renew: bool = False) -> str:
        with self._token_lock:
            if renew or self._token is None or self._clock() >= self._token_expires:
                self._token, lifetime = self._fetch_token()
                self._token_expires = self._clock() + max(lifetime - TOKEN_MARGIN_SECONDS, 0.0)
            return self._token

    def _fetch_token(self) -> tuple[str, float]:
        url = f"{self._base}/auth/oauth/token"
        try:
            response = self._client.post(
                url,
                data={"grant_type": "password", "username": self._key, "password": self._secret},
                headers={"Authorization": _PUBLIC_CLIENT, "Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise DattoUnavailable(f"Datto RMM is not reachable at {self._base} ({type(exc).__name__}).") from None
        if response.status_code in (400, 401):
            raise DattoNotConfigured(
                f"Datto RMM rejected tb-brain's API key and secret (HTTP {response.status_code}). "
                "Check DATTO_API_KEY and DATTO_API_SECRET in the runtime box's .env."
            )
        if response.status_code >= 400:
            raise DattoUnavailable(f"Datto RMM could not issue a token (HTTP {response.status_code}).")
        payload = _json(response)
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise DattoUnavailable("Datto RMM's token response had no access_token.")
        try:
            lifetime = float(payload.get("expires_in") or DEFAULT_TOKEN_SECONDS)
        except (TypeError, ValueError):
            lifetime = DEFAULT_TOKEN_SECONDS
        _log.info("datto token issued", extra={"event": "datto.token"})
        return token, lifetime

    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            # A nextPageUrl: only ever on our own Datto host, so the token goes nowhere else.
            if urlsplit(path_or_url)[:2] != urlsplit(self._base)[:2]:
                raise DattoRequestError("Datto RMM returned a next-page link on another host; stopped paging.")
            return path_or_url
        return f"{self._base}/api{path_or_url}"

    def _get(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_ready()
        url = self._url(path_or_url)
        renewed = False
        delays = list(self._retry_delays)
        limited = False
        while True:
            token = self._access_token()
            try:
                response = self._client.get(
                    url, params=params, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
                )
            except httpx.HTTPError as exc:
                raise DattoUnavailable(f"Datto RMM is not reachable at {self._base} ({type(exc).__name__}).") from None
            status = response.status_code
            if status == 401 and not renewed:
                renewed = True  # the token expired or was revoked: one new token, one retry
                self._access_token(renew=True)
                continue
            if status == 429:
                limited = True
                if not delays:
                    raise DattoUnavailable(
                        "Datto RMM is rate limiting requests right now (HTTP 429). Try again in a minute."
                    )
                wait = _retry_after(response) or delays[0]
                delays.pop(0)
                _log.warning("datto rate limited; waiting %.0fs", wait, extra={"event": "datto.rate_limited"})
                self._sleep(wait)
                continue
            if status == 403 and limited:
                raise DattoUnavailable("Datto RMM has paused requests from tb-brain for a few minutes (HTTP 403).")
            if status == 401:
                raise DattoNotConfigured("Datto RMM rejected tb-brain's token (HTTP 401).")
            if status in (400, 403, 404):
                raise DattoRequestError(f"Datto RMM {status}: {_error_message(response)}")
            if status >= 400:
                raise DattoUnavailable(f"Datto RMM returned an error (HTTP {status}). Try again shortly.")
            return _json(response)

    def _pages(self, path: str, key: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        url: str | None = path
        query: dict[str, Any] | None = {**(params or {}), "max": PAGE_SIZE}
        for _ in range(MAX_PAGES):
            data = self._get(url, query)
            rows.extend(row for row in data.get(key) or [] if isinstance(row, dict))
            url = (data.get("pageDetails") or {}).get("nextPageUrl")
            if not url:
                return rows
            query = None  # the next-page link carries its own query
        _log.warning("datto paging stopped after %s pages at %s", MAX_PAGES, path)
        return rows


def _seg(value: str) -> str:
    return quote(str(value), safe="")


def _status(status: str) -> str:
    value = (status or "open").strip().lower()
    if value not in ALERT_STATUSES:
        raise ValueError(f"alert status must be one of {', '.join(ALERT_STATUSES)}")
    return value


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        raise DattoUnavailable("Datto RMM returned something that is not JSON.") from None
    return payload if isinstance(payload, dict) else {}


def _retry_after(response: httpx.Response) -> float | None:
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for key in ("message", "error_description", "error"):
            if payload.get(key):
                return str(payload[key])[:300]
    return (response.text or "").strip()[:300] or f"HTTP {response.status_code}"
