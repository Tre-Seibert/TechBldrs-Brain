"""HTTP adapter for Microsoft Graph, app-only and read-only, with tb-brain's one multi-tenant app (M365_CLIENT_ID /
M365_CLIENT_SECRET), consented once per customer tenant with the permissions in docs/end-goal-plan.md.

  POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token   client credentials, scope .default
  GET  https://graph.microsoft.com/{v1.0|beta}/...                      only the paths in ENDPOINTS

A token is fetched per tenant and cached until shortly before it expires. ENDPOINTS is the whole list of Graph paths
tb-brain may call, each with the permission it needs; _get refuses any other path, and the tests check every path in
this file against it. There is no mail, file, chat or calendar path in it, and nothing but GET.

Lists follow @odata.nextLink, only on graph.microsoft.com, so the token never goes anywhere else. 429, 503 and 504
wait (Retry-After when sent, else a short back-off) and give up after a few tries. 401 gets one new token and one
retry. Graph refusing a call because of the tenant's license is M365LicenseRequired, never an empty list.
Lists are cached briefly, so one question that looks at several users lists them once.

The client secret is never logged, put in an error message, or shown by repr(). Microsoft's error descriptions name
the tenant, so they are not passed on; error codes are. The model never sees a tenant id.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx

from app.sources.cache import TtlCache
from app.sources.m365.protocol import (
    M365ConsentMissing,
    M365LicenseRequired,
    M365NotConfigured,
    M365RequestError,
    M365Unavailable,
)
from app.sources.m365.schemas import (
    Application,
    AuthMethod,
    CaPolicy,
    ControlProfile,
    DirectoryAudit,
    DirectoryObject,
    DirectoryRole,
    Domain,
    InboxRule,
    Incident,
    MailboxSettings,
    MailboxUsage,
    ManagedDevice,
    Organization,
    RegistrationDetail,
    RiskyUser,
    SecureScore,
    SecurityAlert,
    SecurityDefaults,
    ServiceHealth,
    ServiceIssue,
    SignIn,
    SubscribedSku,
    User,
)

_log = logging.getLogger("tb_brain.m365")

GRAPH = "https://graph.microsoft.com"
LOGIN = "https://login.microsoftonline.com"
SCOPE = "https://graph.microsoft.com/.default"

# Every Graph path tb-brain may call ({id} is one path segment) and the application permission it needs.
ENDPOINTS: dict[str, str] = {
    "/v1.0/organization": "Organization.Read.All",
    "/v1.0/domains": "Domain.Read.All",
    "/v1.0/subscribedSkus": "Organization.Read.All",
    "/v1.0/users": "User.Read.All",
    "/v1.0/users/{id}/memberOf": "GroupMember.Read.All",
    "/v1.0/users/{id}/authentication/methods": "UserAuthenticationMethod.Read.All",
    "/v1.0/users/{id}/mailboxSettings": "MailboxSettings.Read",
    "/v1.0/users/{id}/mailFolders/inbox/messageRules": "MailboxSettings.Read",
    "/v1.0/reports/authenticationMethods/userRegistrationDetails": "AuditLog.Read.All",
    "/beta/reports/getMailboxUsageDetail(period='D7')": "Reports.Read.All",
    "/v1.0/directoryRoles": "RoleManagement.Read.Directory",
    "/v1.0/auditLogs/signIns": "AuditLog.Read.All",
    "/v1.0/auditLogs/directoryAudits": "AuditLog.Read.All",
    "/v1.0/identityProtection/riskyUsers": "IdentityRiskyUser.Read.All",
    "/v1.0/identity/conditionalAccess/policies": "Policy.Read.All",
    "/v1.0/policies/identitySecurityDefaultsEnforcementPolicy": "Policy.Read.All",
    "/v1.0/deviceManagement/managedDevices": "DeviceManagementManagedDevices.Read.All",
    "/v1.0/security/secureScores": "SecurityEvents.Read.All",
    "/v1.0/security/secureScoreControlProfiles": "SecurityEvents.Read.All",
    "/v1.0/security/alerts_v2": "SecurityAlert.Read.All",
    "/v1.0/security/incidents": "SecurityIncident.Read.All",
    "/v1.0/applications": "Application.Read.All",
    "/v1.0/admin/serviceAnnouncement/healthOverviews": "ServiceHealth.Read.All",
    "/v1.0/admin/serviceAnnouncement/issues": "ServiceHealth.Read.All",
}
_ENDPOINT_RES = {
    template: re.compile("^" + re.escape(template).replace(re.escape("{id}"), "[^/]+") + "$") for template in ENDPOINTS
}

PAGE_SIZE = 999
MAX_PAGES = 20
# Sign-in logs can be large: the newest this many rows are read, and the reply says when that was the limit.
MAX_SIGN_INS = 5000
LIST_CACHE_SECONDS = 120.0
# Waits after a 429/503/504 when Graph sends no Retry-After; one retry per entry, then give up.
RETRY_DELAYS = (2.0, 5.0, 15.0)
MAX_RETRY_AFTER = 60.0
# A token is renewed this long before it expires; Microsoft's app tokens last about an hour.
TOKEN_MARGIN_SECONDS = 300.0
DEFAULT_TOKEN_SECONDS = 3599.0

_USER_FIELDS = (
    "id,displayName,userPrincipalName,mail,proxyAddresses,accountEnabled,userType,createdDateTime,jobTitle,"
    "department,assignedLicenses"
)
_GUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
# AADSTS codes on the token call: the app is not consented in (or was removed from) the tenant, or tb-brain's own
# credential was rejected, or the tenant id is not a Microsoft tenant.
_NO_CONSENT_CODES = frozenset({700016, 65001, 7000112, 500011, 50020})
_BAD_CREDENTIAL_CODES = frozenset({7000215, 7000222, 7000216, 700027, 700024})
_NO_TENANT_CODES = frozenset({90002, 900023, 90072})
# Graph's ways of saying the tenant's license doesn't include something (checked on 400/403 for licensed features).
_LICENSE_CODES = frozenset({"Authentication_RequestFromNonPremiumTenantOrB2CTenant"})
_LICENSE_RE = re.compile(
    r"premium licen[cs]e|not licensed|licen[cs]e (?:is )?required|doesn'?t have (?:a |the )?(?:valid |required )?licen[cs]e|"
    r"sku required|required sku|not applicable to target tenant|intune.*not (?:enabled|licensed)|subscription",
    re.IGNORECASE,
)


class HttpM365Source:
    source_name = "m365-http"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        timeout: float = 30.0,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
        cache_seconds: float = LIST_CACHE_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client_id = (client_id or "").strip()
        self._secret = (client_secret or "").strip()
        self._retry_delays = retry_delays
        self._sleep = sleep
        self._clock = clock
        # Redirects are not followed: a report's CSV redirect points at another host, and the token must not go there.
        self._client = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)
        self._cache = TtlCache(cache_seconds, clock=clock)
        self._tokens_lock = threading.Lock()
        self._tokens: dict[str, tuple[str, float]] = {}
        self._token_locks: dict[str, threading.Lock] = {}

    def __repr__(self) -> str:
        return f"HttpM365Source(client_id={self._client_id!r})"

    # --- reads -------------------------------------------------------------------------------------------------

    def organization(self, *, tenant_id: str) -> Organization:
        rows = self._list(tenant_id, "/v1.0/organization")
        return Organization.model_validate(rows[0] if rows else {})

    def list_domains(self, *, tenant_id: str) -> list[Domain]:
        return [Domain.model_validate(row) for row in self._list(tenant_id, "/v1.0/domains")]

    def list_subscribed_skus(self, *, tenant_id: str) -> list[SubscribedSku]:
        return [SubscribedSku.model_validate(row) for row in self._list(tenant_id, "/v1.0/subscribedSkus")]

    def list_users(self, *, tenant_id: str, sign_in_activity: bool = False) -> list[User]:
        fields = _USER_FIELDS + (",signInActivity" if sign_in_activity else "")
        params = {"$select": fields, "$top": PAGE_SIZE}
        feature = "sign_in_activity" if sign_in_activity else None
        return [User.model_validate(row) for row in self._list(tenant_id, "/v1.0/users", params, feature=feature)]

    def list_member_of(self, *, tenant_id: str, user_id: str) -> list[DirectoryObject]:
        path = f"/v1.0/users/{_seg(user_id)}/memberOf"
        return [DirectoryObject.model_validate(row) for row in self._list(tenant_id, path)]

    def list_auth_methods(self, *, tenant_id: str, user_id: str) -> list[AuthMethod]:
        path = f"/v1.0/users/{_seg(user_id)}/authentication/methods"
        return [AuthMethod.model_validate(row) for row in self._list(tenant_id, path)]

    def list_registration_details(self, *, tenant_id: str) -> list[RegistrationDetail]:
        path = "/v1.0/reports/authenticationMethods/userRegistrationDetails"
        return [RegistrationDetail.model_validate(row) for row in self._list(tenant_id, path, feature="mfa_report")]

    def list_directory_roles(self, *, tenant_id: str) -> list[DirectoryRole]:
        params = {"$expand": "members"}
        return [DirectoryRole.model_validate(row) for row in self._list(tenant_id, "/v1.0/directoryRoles", params)]

    def list_sign_ins(self, *, tenant_id: str, since: datetime, user_principal_name: str | None = None) -> list[SignIn]:
        where = f"createdDateTime ge {_iso(since)}"
        if user_principal_name:
            where += f" and userPrincipalName eq '{user_principal_name.replace(chr(39), chr(39) * 2)}'"
        rows = self._pages(
            tenant_id, "/v1.0/auditLogs/signIns", {"$filter": where, "$top": 1000}, feature="signins", max_rows=MAX_SIGN_INS
        )
        return [SignIn.model_validate(row) for row in rows]

    def list_risky_users(self, *, tenant_id: str) -> list[RiskyUser]:
        path = "/v1.0/identityProtection/riskyUsers"
        return [RiskyUser.model_validate(row) for row in self._list(tenant_id, path, feature="risky_users")]

    def list_conditional_access_policies(self, *, tenant_id: str) -> list[CaPolicy]:
        path = "/v1.0/identity/conditionalAccess/policies"
        return [CaPolicy.model_validate(row) for row in self._list(tenant_id, path, feature="conditional_access")]

    def security_defaults(self, *, tenant_id: str) -> SecurityDefaults:
        path = "/v1.0/policies/identitySecurityDefaultsEnforcementPolicy"
        return SecurityDefaults.model_validate(self._cached(tenant_id, path, None, lambda: self._get(tenant_id, path)))

    def list_managed_devices(self, *, tenant_id: str) -> list[ManagedDevice]:
        path = "/v1.0/deviceManagement/managedDevices"
        return [ManagedDevice.model_validate(row) for row in self._list(tenant_id, path, feature="intune")]

    def mailbox_settings(self, *, tenant_id: str, user_id: str) -> MailboxSettings:
        path = f"/v1.0/users/{_seg(user_id)}/mailboxSettings"
        return MailboxSettings.model_validate(self._cached(tenant_id, path, None, lambda: self._get(tenant_id, path)))

    def list_inbox_rules(self, *, tenant_id: str, user_id: str) -> list[InboxRule]:
        path = f"/v1.0/users/{_seg(user_id)}/mailFolders/inbox/messageRules"
        return [InboxRule.model_validate(row) for row in self._list(tenant_id, path)]

    def mailbox_usage(self, *, tenant_id: str) -> list[MailboxUsage]:
        path = "/beta/reports/getMailboxUsageDetail(period='D7')"
        return [MailboxUsage.model_validate(row) for row in self._list(tenant_id, path, {"$format": "application/json"})]

    def secure_score(self, *, tenant_id: str) -> SecureScore | None:
        rows = self._list(tenant_id, "/v1.0/security/secureScores", {"$top": 1})
        return SecureScore.model_validate(rows[0]) if rows else None

    def list_secure_score_controls(self, *, tenant_id: str) -> list[ControlProfile]:
        path = "/v1.0/security/secureScoreControlProfiles"
        return [ControlProfile.model_validate(row) for row in self._list(tenant_id, path)]

    def list_security_alerts(self, *, tenant_id: str) -> list[SecurityAlert]:
        path = "/v1.0/security/alerts_v2"
        return [SecurityAlert.model_validate(row) for row in self._list(tenant_id, path, feature="defender")]

    def list_security_incidents(self, *, tenant_id: str) -> list[Incident]:
        path = "/v1.0/security/incidents"
        return [Incident.model_validate(row) for row in self._list(tenant_id, path, feature="defender")]

    def list_applications(self, *, tenant_id: str) -> list[Application]:
        params = {"$select": "id,appId,displayName,passwordCredentials,keyCredentials", "$top": PAGE_SIZE}
        return [Application.model_validate(row) for row in self._list(tenant_id, "/v1.0/applications", params)]

    def list_directory_audits(self, *, tenant_id: str, since: datetime) -> list[DirectoryAudit]:
        params = {"$filter": f"activityDateTime ge {_iso(since)}", "$top": 500}
        rows = self._pages(tenant_id, "/v1.0/auditLogs/directoryAudits", params)
        return [DirectoryAudit.model_validate(row) for row in rows]

    def list_service_health(self, *, tenant_id: str) -> list[ServiceHealth]:
        path = "/v1.0/admin/serviceAnnouncement/healthOverviews"
        return [ServiceHealth.model_validate(row) for row in self._list(tenant_id, path)]

    def list_service_issues(self, *, tenant_id: str) -> list[ServiceIssue]:
        path = "/v1.0/admin/serviceAnnouncement/issues"
        params = {"$filter": "isResolved eq false"}
        return [ServiceIssue.model_validate(row) for row in self._list(tenant_id, path, params)]

    # --- tokens ------------------------------------------------------------------------------------------------

    def _require_ready(self) -> None:
        if not (self._client_id and self._secret):
            raise M365NotConfigured(
                "M365_MODE=http but M365_CLIENT_ID or M365_CLIENT_SECRET is empty. Set tb-brain's multi-tenant app "
                "in the runtime box's .env, or use M365_MODE=stub."
            )

    def _access_token(self, tenant_id: str, *, renew: bool = False) -> str:
        with self._tokens_lock:
            lock = self._token_locks.setdefault(tenant_id, threading.Lock())
        with lock:  # one token request per tenant however many calls need it at once
            cached = self._tokens.get(tenant_id)
            if not renew and cached and self._clock() < cached[1]:
                return cached[0]
            token, lifetime = self._fetch_token(tenant_id)
            self._tokens[tenant_id] = (token, self._clock() + max(lifetime - TOKEN_MARGIN_SECONDS, 0.0))
            return token

    def _credential(self) -> dict[str, str]:
        """The app's credential in the token request. A certificate would send client_assertion here instead."""
        return {"client_id": self._client_id, "client_secret": self._secret}

    def _fetch_token(self, tenant_id: str) -> tuple[str, float]:
        url = f"{LOGIN}/{_seg(tenant_id)}/oauth2/v2.0/token"
        try:
            response = self._client.post(url, data={"grant_type": "client_credentials", "scope": SCOPE, **self._credential()})
        except httpx.HTTPError as exc:
            raise M365Unavailable(f"Microsoft's sign-in service is not reachable ({type(exc).__name__}).") from None
        if response.status_code in (400, 401):
            codes = _aadsts_codes(response)
            listed = f" (AADSTS{', AADSTS'.join(str(code) for code in sorted(codes))})" if codes else ""
            if codes & _BAD_CREDENTIAL_CODES:
                raise M365NotConfigured(
                    f"Microsoft rejected tb-brain's app credential{listed}. Check M365_CLIENT_ID and M365_CLIENT_SECRET "
                    "in the runtime box's .env; the secret may have expired."
                )
            if codes & _NO_TENANT_CODES:
                raise M365RequestError(
                    f"Microsoft doesn't recognise the tenant id Flow has on record for this client{listed}. "
                    "Check the tenant id on the client's page in Flow."
                )
            raise M365ConsentMissing(f"Microsoft refused tb-brain's app a token for this tenant{listed}.")
        if response.status_code >= 400:
            raise M365Unavailable(f"Microsoft could not issue a token (HTTP {response.status_code}).")
        payload = _json(response)
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise M365Unavailable("Microsoft's token response had no access_token.")
        try:
            lifetime = float(payload.get("expires_in") or DEFAULT_TOKEN_SECONDS)
        except (TypeError, ValueError):
            lifetime = DEFAULT_TOKEN_SECONDS
        _log.info("m365 token issued", extra={"event": "m365.token"})
        return token, lifetime

    # --- transport ---------------------------------------------------------------------------------------------

    def _url(self, path_or_url: str) -> tuple[str, str]:
        """The URL to call and the ENDPOINTS template it falls under. Anything else is refused before any request."""
        if path_or_url.startswith(("http://", "https://")):
            # An @odata.nextLink: only ever on Graph itself, so the token goes nowhere else.
            parts = urlsplit(path_or_url)
            if (parts.scheme, parts.netloc) != tuple(urlsplit(GRAPH)[:2]):
                raise M365RequestError("Microsoft Graph returned a next-page link on another host; stopped paging.")
            url, path = path_or_url, unquote(parts.path)
        else:
            url, path = GRAPH + path_or_url, path_or_url
        template = endpoint_for(path)
        if template is None:
            raise M365RequestError(f"tb-brain does not call {path} (not in its Microsoft Graph allowlist).")
        return url, template

    def _get(self, tenant_id: str, path_or_url: str, params: dict[str, Any] | None = None, *, feature: str | None = None) -> dict[str, Any]:
        self._require_ready()
        url, template = self._url(path_or_url)
        renewed = False
        delays = list(self._retry_delays)
        while True:
            token = self._access_token(tenant_id)
            try:
                response = self._client.get(
                    url, params=params, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
                )
            except httpx.HTTPError as exc:
                raise M365Unavailable(f"Microsoft Graph is not reachable ({type(exc).__name__}).") from None
            status = response.status_code
            if status == 401 and not renewed:
                renewed = True  # the token expired or was revoked: one new token, one retry
                self._access_token(tenant_id, renew=True)
                continue
            if status in (429, 503, 504):
                if not delays:
                    raise M365Unavailable(
                        f"Microsoft Graph is throttling or busy (HTTP {status}) and still was after retrying. Try again in a minute."
                    )
                wait = _retry_after(response) or delays[0]
                delays.pop(0)
                _log.warning("m365 graph busy (%s); waiting %.0fs", status, wait, extra={"event": "m365.retry"})
                self._sleep(wait)
                continue
            if status in (400, 403) and feature and _license_refusal(response):
                raise M365LicenseRequired(feature)
            if status in (401, 403):
                permission = ENDPOINTS[template]
                raise M365ConsentMissing(
                    f"Microsoft Graph refused tb-brain's app (HTTP {status}{_code_suffix(response)}); it needs {permission}.",
                    permission=permission,
                )
            if status in (400, 404):
                raise M365RequestError(f"Microsoft Graph {status}: {_error_message(response)}")
            if status >= 300:
                raise M365Unavailable(f"Microsoft Graph returned an error (HTTP {status}). Try again shortly.")
            return _json(response)

    def _pages(
        self,
        tenant_id: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        feature: str | None = None,
        max_rows: int | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        url: str | None = path
        query = params
        for _ in range(MAX_PAGES):
            data = self._get(tenant_id, url, query, feature=feature)
            rows.extend(row for row in data.get("value") or [] if isinstance(row, dict))
            if max_rows is not None and len(rows) >= max_rows:
                return rows[:max_rows]
            url = data.get("@odata.nextLink")
            if not url:
                return rows
            query = None  # the next-page link carries its own query
        _log.warning("m365 paging stopped after %s pages at %s", MAX_PAGES, path)
        return rows

    def _list(self, tenant_id: str, path: str, params: dict[str, Any] | None = None, *, feature: str | None = None) -> list[dict[str, Any]]:
        return self._cached(tenant_id, path, params, lambda: self._pages(tenant_id, path, params, feature=feature))

    def _cached(self, tenant_id: str, path: str, params: dict[str, Any] | None, fetch: Callable[[], Any]) -> Any:
        key = (tenant_id, path, tuple(sorted((params or {}).items())))
        return self._cache.get(key, fetch)


def endpoint_for(path: str) -> str | None:
    """The ENDPOINTS template a Graph path falls under, or None when tb-brain may not call it."""
    clean = path.split("?", 1)[0]
    return next((template for template, pattern in _ENDPOINT_RES.items() if pattern.match(clean)), None)


def _seg(value: str) -> str:
    return quote(str(value), safe="")


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        raise M365Unavailable("Microsoft Graph returned something that is not JSON.") from None
    return payload if isinstance(payload, dict) else {}


def _aadsts_codes(response: httpx.Response) -> set[int]:
    try:
        payload = response.json()
    except ValueError:
        return set()
    codes = payload.get("error_codes") if isinstance(payload, dict) else None
    return {int(code) for code in codes or [] if str(code).isdigit()}


def _graph_error(response: httpx.Response) -> tuple[str, str]:
    try:
        payload = response.json()
    except ValueError:
        return "", (response.text or "").strip()
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("code") or ""), str(error.get("message") or "")
    return "", ""


def _license_refusal(response: httpx.Response) -> bool:
    code, message = _graph_error(response)
    return code in _LICENSE_CODES or bool(_LICENSE_RE.search(message))


def _code_suffix(response: httpx.Response) -> str:
    code, _ = _graph_error(response)
    return f", {code}" if code else ""


def _error_message(response: httpx.Response) -> str:
    code, message = _graph_error(response)
    text = _GUID_RE.sub("…", message).strip()[:300]
    return " ".join(part for part in (code, text) if part) or f"HTTP {response.status_code}"


def _retry_after(response: httpx.Response) -> float | None:
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)
