"""Resolves the technician making a chat/tool call, from a signed Open WebUI JWT.

Open WebUI, when ENABLE_FORWARD_USER_INFO_HEADERS and
FORWARD_USER_INFO_HEADER_JWT_SECRET are both set, sends only a signed HS256
JWT (header X-OpenWebUI-User-Jwt, claims sub/email/name/role/iss/iat/exp) and
suppresses the raw X-OpenWebUI-User-* headers entirely. Without that secret it
sends the raw headers instead — unsigned, spoofable by anyone who can reach
this service (BRAIN_HOST can be 0.0.0.0), so that path is lab/dev only.

Never trust an unverified email as a Flow identity. When brain_user_jwt_secret
is configured, a bare email header is always ignored, even if present.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass

import jwt

from app.config import Settings

_log = logging.getLogger("tb_brain.identity")

# Verified actor email for the in-flight request only. HttpFlowSource reads
# this to attach X-Brain-Actor-Email to outbound Flow calls. Never set from
# an unverified source.
current_actor_email: ContextVar[str | None] = ContextVar("current_actor_email", default=None)

# Email of the person in this chat (JWT when verified, Open WebUI header in lab).
# Used only to resolve "me" / "my tickets". Not forwarded to Flow writes.
current_signed_in_email: ContextVar[str | None] = ContextVar("current_signed_in_email", default=None)


@dataclass(frozen=True)
class ResolvedActor:
    email: str | None
    verified: bool
    label: str


def resolve_actor(
    settings: Settings,
    *,
    user_jwt: str | None,
    user_email_header: str | None,
    fallback_actor: str | None,
) -> ResolvedActor:
    default_label = (fallback_actor or settings.brain_actor or "lab-local").strip()
    secret = (settings.brain_user_jwt_secret or "").strip()

    if secret:
        if user_jwt:
            try:
                claims = jwt.decode(user_jwt, secret, algorithms=["HS256"])
            except jwt.PyJWTError as exc:
                _log.warning("rejected Open WebUI user JWT: %s", type(exc).__name__)
            else:
                claim_name = settings.oauth_email_claim or "email"
                email = (claims.get(claim_name) or "").strip().lower()
                if email:
                    return ResolvedActor(email=email, verified=True, label=email)
        # Secret is configured: a bare email header is never trusted.
        return ResolvedActor(email=None, verified=False, label=default_label)

    # No secret configured (lab/dev) — fall back to the unsigned header.
    email = (user_email_header or "").strip().lower()
    if email:
        return ResolvedActor(email=email, verified=False, label=email)

    return ResolvedActor(email=None, verified=False, label=default_label)
