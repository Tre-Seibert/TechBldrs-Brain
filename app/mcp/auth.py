"""Who is calling the MCP endpoint, and how that is checked (MCP_AUTH).

entra (the default): every request carries a Microsoft Entra access token that Copilot Studio obtained for the
signed-in technician (the tool's OAuth 2.0 connection, delegated). The token's signature is checked against the
TechBldrs tenant's published keys, then its issuer, tenant, audience (the app registration's Application ID URI or
client id) and scope. The technician's UPN becomes the audit actor and the identity Flow sees. App-only tokens
(client credentials, no user) are refused: every call must be traceable to a person.

api_key: one shared key (MCP_API_KEY), for scripts/mcp_smoke.py. No technician identity, so "my tickets" cannot be
answered and nothing is forwarded to Flow as an actor.

off: no check at all. Allowed only while tb-brain listens on loopback.

Tokens and keys are never logged.
"""

from __future__ import annotations

import hmac
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

import jwt

from app.config import Settings

_log = logging.getLogger("tb_brain.mcp.auth")

MODES = ("entra", "api_key", "off")
LOGIN = "https://login.microsoftonline.com"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class Principal:
    """Who made the call. `label` goes into the audit row; `email` is the signed-in technician when verified."""

    label: str
    email: str | None
    verified: bool
    mode: str


# The caller of the in-flight MCP request, set by the endpoint before the tool runs.
current_principal: ContextVar[Principal | None] = ContextVar("current_mcp_principal", default=None)


class AuthFailed(Exception):
    """The request is not allowed. The message is safe to show the caller."""


class McpAuthMisconfigured(ValueError):
    """MCP_AUTH cannot work with the settings given. Raised at startup, never per request."""


class Verifier(Protocol):
    requires_token: bool

    def verify(self, token: str) -> Principal: ...


class EntraVerifier:
    """Checks a delegated Entra access token for the TechBldrs tenant and names the technician it was issued to."""

    requires_token = True

    def __init__(
        self,
        tenant_id: str,
        audiences: tuple[str, ...],
        scope: str,
        *,
        jwk_client: Any | None = None,
        leeway_seconds: float = 60.0,
    ) -> None:
        self.tenant_id = tenant_id.strip().lower()
        self.audiences = tuple(audience.strip() for audience in audiences if audience.strip())
        self.scope = scope.strip()
        # v2 tokens name the tenant's v2.0 endpoint; an app registration left at accessTokenAcceptedVersion=1
        # gets v1 tokens with the sts.windows.net issuer. Both are signed with the tenant's keys.
        self.issuers = frozenset({f"{LOGIN}/{self.tenant_id}/v2.0", f"https://sts.windows.net/{self.tenant_id}/"})
        self._jwk_client = jwk_client or jwt.PyJWKClient(f"{LOGIN}/{self.tenant_id}/discovery/v2.0/keys", cache_keys=True)
        self._leeway = leeway_seconds

    def verify(self, token: str) -> Principal:
        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=list(self.audiences),
                leeway=self._leeway,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthFailed(f"token rejected ({type(exc).__name__})") from exc
        except Exception as exc:  # noqa: BLE001 - a key fetch failure must read as a refusal, not a stack trace
            raise AuthFailed(f"token could not be checked ({type(exc).__name__})") from exc
        if claims.get("iss") not in self.issuers:
            raise AuthFailed("token was not issued by the TechBldrs tenant")
        if str(claims.get("tid") or "").lower() != self.tenant_id:
            raise AuthFailed("token is for another tenant")
        scopes = str(claims.get("scp") or "").split()
        if not scopes:
            raise AuthFailed("token carries no signed-in user (app-only tokens are not accepted)")
        if self.scope and self.scope not in scopes:
            raise AuthFailed(f"token lacks the {self.scope} scope")
        email = str(claims.get("preferred_username") or claims.get("upn") or claims.get("email") or "").strip().lower()
        if "@" not in email:
            raise AuthFailed("token names no user")
        return Principal(label=email, email=email, verified=True, mode="entra")


class ApiKeyVerifier:
    requires_token = True

    def __init__(self, key: str, label: str = "mcp-api-key") -> None:
        self._key = key.encode("utf-8")
        self._label = label

    def verify(self, token: str) -> Principal:
        if not self._key or not hmac.compare_digest(token.encode("utf-8"), self._key):
            raise AuthFailed("bad API key")
        return Principal(label=self._label, email=None, verified=False, mode="api_key")


class NoAuthVerifier:
    requires_token = False

    def __init__(self, label: str) -> None:
        self._label = label

    def verify(self, token: str) -> Principal:
        return Principal(label=self._label, email=None, verified=False, mode="off")


class RefusingVerifier:
    """Installed when MCP_AUTH is misconfigured: every call is refused with the same short message."""

    requires_token = False

    def __init__(self, message: str = "MCP authentication is not configured on this server.") -> None:
        self._message = message

    def verify(self, token: str) -> Principal:
        raise AuthFailed(self._message)


def build_verifier(settings: Settings) -> Verifier:
    mode = settings.mcp_auth
    if mode == "entra":
        if not settings.mcp_entra_tenant_id.strip() or not settings.mcp_entra_audiences:
            raise McpAuthMisconfigured("MCP_AUTH=entra needs MCP_ENTRA_TENANT_ID and MCP_ENTRA_AUDIENCE")
        return EntraVerifier(settings.mcp_entra_tenant_id, settings.mcp_entra_audiences, settings.mcp_entra_scope)
    if mode == "api_key":
        key = settings.mcp_api_key.get_secret_value().strip()
        if len(key) < 16:
            raise McpAuthMisconfigured("MCP_AUTH=api_key needs MCP_API_KEY of at least 16 characters")
        return ApiKeyVerifier(key)
    if settings.brain_host.strip().lower() not in LOOPBACK_HOSTS:
        raise McpAuthMisconfigured(f"MCP_AUTH=off is only allowed on loopback, not BRAIN_HOST={settings.brain_host}")
    return NoAuthVerifier(settings.brain_actor or "mcp-local")


def bearer_token(scope: dict[str, Any]) -> str | None:
    """The bearer token in an ASGI scope's Authorization header, or None."""
    for key, value in scope.get("headers") or ():
        if key.lower() == b"authorization":
            text = value.decode("latin-1").strip()
            if text[:7].lower() == "bearer ":
                return text[7:].strip() or None
            return None
    return None


def authenticate(scope: dict[str, Any], verifier: Verifier) -> Principal:
    """The Principal for this request, or AuthFailed. Blocking: Entra may fetch signing keys on first use."""
    token = bearer_token(scope)
    if token is None and verifier.requires_token:
        raise AuthFailed("missing bearer token")
    return verifier.verify(token or "")
