"""Microsoft 365 as a SourceSpec. Off unless M365_MODE=stub|http; offered per question by source routing.

The tenant id comes from Flow's client record through the client resolver, so this source adds no client id hook.
"""

from __future__ import annotations

from app.sources.base import SourceSpec
from app.sources.m365.factory import build_m365_source
from app.sources.m365.handlers.dispatch import handle
from app.sources.m365.prompt import PROMPT_SECTION
from app.sources.m365.protocol import (
    M365ConsentMissing,
    M365LicenseRequired,
    M365NotConfigured,
    M365RequestError,
    M365Unavailable,
)
from app.sources.m365.tools import LEAN_OPENAI_TOOLS, OPENAI_TOOLS, RELAY_TOOL_NAMES
from app.sources.m365.wording import EMAIL_RE, TENANT_RE, WORDING, WRITE_WORDS

M365 = SourceSpec(
    name="m365",
    mode=lambda settings: settings.m365_mode_normalized,
    build=build_m365_source,
    tools=tuple(OPENAI_TOOLS),
    handle=handle,
    relay_tool_names=RELAY_TOOL_NAMES,
    errors=(M365NotConfigured, M365Unavailable, M365RequestError, M365ConsentMissing, M365LicenseRequired),
    prompt_section=PROMPT_SECTION,
    lean_tools=tuple(LEAN_OPENAI_TOOLS),
    description=(
        "Microsoft 365: each client's tenant via Microsoft Graph (users, MFA, licenses, sign-ins, admin roles, "
        "Conditional Access, Intune devices, mailbox size and forwarding rules, Secure Score, Defender alerts, "
        "domains, audit log)."
    ),
    # Each matches its plural and -ing form too (licenses, licensing, mailboxes, sign-ins, logins; see
    # app/agent/routing.py). -ed forms are listed where they mean an account ("licensed", "signed in", "enrolled").
    keywords=(
        "Microsoft 365", "M365", "Office 365", "O365", "Entra", "Entra ID", "Azure AD", "AAD", "MFA", "2FA",
        "multi-factor", "multifactor", "authenticator", "sign-in", "signin", "signed in", "signs in", "signing in",
        "login", "logon", "log in", "logs in", "logging in", "locked out", "lockout", "license", "licence", "licensed",
        "unlicensed", "Business Premium", "Business Standard", "Business Basic", "E3", "E5", "user", "user account",
        "disabled", "guest", "groups", "group membership",
        "mailbox", "shared mailbox", "inbox", "inbox rule", "forwarding", "admin role", "admin rights", "global admin",
        "global administrator", "conditional access", "security defaults", "Intune", "enrolled", "compliant",
        "non-compliant", "noncompliant", "compliance", "Secure Score", "Defender", "risky", "app secret",
        "app registration", "service health", "Exchange Online", "audit log", "UPN",
    ),
    patterns=(EMAIL_RE, TENANT_RE, WRITE_WORDS),
    wording=WORDING,
    write_words=WRITE_WORDS,
    wants_clients=True,
)
