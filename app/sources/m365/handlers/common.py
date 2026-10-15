"""Shared by the Microsoft 365 tools: which tenant a question is about, finding a user in it, and how licenses, admin
roles, MFA methods, dates and the license / consent / outage replies read. Replies are short summaries, capped in
length; counts and rankings are computed here, never by the model. No reply or data carries a tenant id.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.sources.clients import ClientResolver, Outcome, ResolvedClient
from app.sources.datto.handlers.common import HOSTNAME_RE
from app.sources.m365.protocol import (
    FEATURES,
    M365ConsentMissing,
    M365LicenseRequired,
    M365NotConfigured,
    M365RequestError,
    M365Source,
    M365Unavailable,
)
from app.sources.m365.schemas import User
from app.sources.m365.skus import sku_name
from app.tools.base import ToolResult

# Longest list a reply shows; the rest is counted ("... and 12 more").
MAX_LINES = 40
MAX_CANDIDATES = 5
CLOSE_MATCH_CUTOFF = 0.75
GB = 1024**3
_ALL_CLIENTS = frozenset({"all", "*", "any", "every", "everyone", "all clients", "every client", "all tenants"})
ALL_CLIENTS_NOTE = (
    "All clients: this checked every client's Microsoft 365 tenant one at a time, so it takes longer than a "
    "one-client question."
)
_GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_EMAIL_RE = re.compile(r"^[^@\s]+@([^@\s]+\.[^@\s]+)$")

MFA_KINDS = frozenset({"microsoftAuthenticator", "phone", "fido2", "softwareOath", "windowsHelloForBusiness"})
METHOD_WORDS = {
    "microsoftAuthenticator": "Microsoft Authenticator",
    "phone": "phone (SMS or call)",
    "fido2": "FIDO2 security key",
    "softwareOath": "authenticator app code",
    "windowsHelloForBusiness": "Windows Hello for Business",
    "temporaryAccessPass": "temporary access pass",
    "email": "email (password reset only)",
    "password": "password",
    # userRegistrationDetails' names for the same methods
    "microsoftAuthenticatorPush": "Microsoft Authenticator",
    "microsoftAuthenticatorPasswordless": "Microsoft Authenticator (passwordless)",
    "mobilePhone": "phone (SMS or call)",
    "alternateMobilePhone": "alternate phone",
    "officePhone": "office phone",
    "fido2SecurityKey": "FIDO2 security key",
    "softwareOneTimePasscode": "authenticator app code",
    "hardwareOneTimePasscode": "hardware token",
}

# Sign-in error codes a tech asks about, in words.
SIGN_IN_ERRORS = {
    50126: "wrong username or password",
    50053: "account locked (too many failed attempts)",
    50057: "account disabled",
    50055: "password expired",
    50056: "no password set",
    50074: "MFA required",
    50076: "MFA required (location or risk)",
    50079: "MFA registration required",
    500121: "MFA not completed",
    50158: "external security challenge not passed",
    53000: "device not compliant",
    53003: "blocked by Conditional Access",
    53004: "MFA registration blocked by risk",
    50034: "user does not exist",
    50097: "device authentication required",
    530032: "blocked by security policy",
    50173: "token revoked (password changed)",
    70044: "session expired",
}

COUNTRY_ALIASES = {"USA": "US", "UNITED STATES": "US", "AMERICA": "US", "UK": "GB", "UNITED KINGDOM": "GB", "CANADA": "CA"}


def now_for(m365: M365Source) -> datetime:
    """The time 'ago' is measured from: the stub's pinned clock, else the real one."""
    return getattr(m365, "now", None) or datetime.now(timezone.utc)


def refuse(tool: str, m365: M365Source, message: str, *, client_code: str | None = None) -> ToolResult:
    return ToolResult(ok=False, tool=tool, source=m365.source_name, client_code=client_code, error=message, reply=message)


def wants_all(text: str | None) -> bool:
    return (text or "").strip().lower() in _ALL_CLIENTS


def is_email(text: str | None) -> bool:
    return bool(_EMAIL_RE.match((text or "").strip()))


def looks_like_tenant_id(text: str | None) -> bool:
    return bool(_GUID_RE.match((text or "").strip()))


def plural(count: int, word: str, many: str | None = None) -> str:
    return f"{count} {word if count == 1 else (many or word + 's')}"


def capped(lines: list[str], *, limit: int = MAX_LINES, what: str = "more") -> list[str]:
    if len(lines) <= limit:
        return lines
    return [*lines[:limit], f"- ... and {len(lines) - limit} {what} (ask with a narrower filter to see them)"]


def ago(moment: datetime | None, now: datetime) -> str:
    """'40 minutes ago', '3 days ago (2026-10-12)'; 'never' when there is no date."""
    if moment is None:
        return "never"
    seconds = (now - moment).total_seconds()
    if seconds < 0:
        days = int(-seconds // 86400)
        return f"in {plural(days, 'day')} ({moment.date().isoformat()})" if days else f"today ({moment.date().isoformat()})"
    if seconds < 3600:
        minutes = int(seconds // 60)
        return "just now" if minutes == 0 else f"{plural(minutes, 'minute')} ago"
    if seconds < 86400:
        return f"{plural(int(seconds // 3600), 'hour')} ago"
    return f"{plural(int(seconds // 86400), 'day')} ago ({moment.date().isoformat()})"


def gb(value: int | None) -> str:
    if value is None:
        return "?"
    size = value / GB
    return f"{size:,.1f} GB" if size < 100 else f"{size:,.0f} GB"


def is_techbldrs(user_principal_name: str | None, display_name: str | None = None) -> bool:
    """TechBldrs' own accounts in a client tenant (techbldrs@client.onmicrosoft.com, "TechBldrs Admin")."""
    return "techbldrs" in f"{user_principal_name or ''} {display_name or ''}".lower()


def method_words(kind: str) -> str:
    return METHOD_WORDS.get(kind, kind)


def license_message(client: ResolvedClient, feature: str) -> str:
    what, needs = FEATURES.get(feature, (feature, "a higher Microsoft 365 license"))
    return (
        f"{client.label}'s Microsoft 365 license doesn't include {what} (that needs {needs}), so tb-brain can't see it. "
        "This is a license limit, not a sign that there is nothing there."
    )


def error_message(client: ResolvedClient | None, exc: Exception) -> str:
    """The reply for a Microsoft 365 error while working on `client`'s tenant."""
    label = client.label if client else "this client"
    if isinstance(exc, M365LicenseRequired) and client is not None:
        return license_message(client, exc.feature)
    if isinstance(exc, M365ConsentMissing):
        if exc.permission:
            return (
                f"tb-brain's Microsoft 365 app can't read that in {label}'s tenant: the consent given there is missing "
                f"the {exc.permission} permission. {exc} Re-consent the app in that tenant with the full permission list."
            )
        return (
            f"tb-brain's Microsoft 365 app has not been granted access to {label}'s tenant (consent not given, or "
            f"removed). {exc} A Global Administrator or Privileged Role Administrator there needs to consent to the "
            "app once."
        )
    if isinstance(exc, M365Unavailable):
        return f"Microsoft 365 could not be reached, so I can't check {label} right now. {exc}"
    if isinstance(exc, M365NotConfigured):
        return f"Microsoft 365 is not set up in tb-brain: {exc}"
    return f"Microsoft 365 ({label}): {exc}"


ERRORS = (M365LicenseRequired, M365ConsentMissing, M365Unavailable, M365NotConfigured, M365RequestError)


@dataclass(frozen=True)
class TenantScope:
    """The one client a question is about, and a line saying how it was found when the user didn't name it."""

    client: ResolvedClient
    note: str = ""


def resolve_tenant(
    tool: str, m365: M365Source, clients: ClientResolver, client_code: str | None, user: str | None
) -> TenantScope | ToolResult:
    """The client (with a Microsoft 365 tenant) for what the user called it, or for the user named when no client
    was: their email's domain, else their Flow contact. A ToolResult says why there is none or asks which one."""
    code = (client_code or "").strip()
    person = (user or "").strip()
    if looks_like_tenant_id(code):
        return refuse(tool, m365, "Name the client by its code or name (e.g. WDON); tb-brain finds the tenant itself.")
    if code:
        resolution = clients.resolve(code, require=("m365_tenant_id",))
        if resolution.ok:
            return TenantScope(resolution.client)
        # A person's name sent as the client ("Debe") with no user: look them up as the user instead.
        if resolution.outcome is Outcome.UNKNOWN and not person and not re.fullmatch(r"[A-Za-z0-9]{2,5}", code):
            found = _client_for_person(tool, m365, clients, code)
            if isinstance(found, TenantScope):
                return found
        found_code = resolution.client.client_code if resolution.client else None
        return refuse(tool, m365, resolution.message, client_code=found_code)
    if person:
        return _client_for_person(tool, m365, clients, person)
    return refuse(tool, m365, "Which client? Give the client code or name.")


def _client_for_person(tool: str, m365: M365Source, clients: ClientResolver, person: str) -> TenantScope | ToolResult:
    match = _EMAIL_RE.match(person)
    if match:
        code = clients.code_for_domain(match.group(1))
        if code is None:
            hits = {c.client_code.upper() for c in clients.contacts(person) if person.lower() in _emails(c) and c.client_code}
            code = next(iter(hits)) if len(hits) == 1 else None
        if code is None:
            return refuse(tool, m365, f"No client in Flow uses {match.group(1)}. Which client is {person} at?")
        return _scope_for(tool, m365, clients, code, "")
    words = _words(person)
    contacts = [
        contact for contact in clients.contacts(person)
        if contact.client_code and words and all(any(part.startswith(word) for part in _words(contact.full_name)) for word in words)
    ]
    codes = sorted({contact.client_code.upper() for contact in contacts})
    if len(codes) == 1:
        contact = contacts[0]
        named = contact.full_name if len({c.full_name for c in contacts}) == 1 else person
        return _scope_for(tool, m365, clients, codes[0], f"{named} is a Flow contact at {codes[0]}, so this is {codes[0]}'s tenant.")
    if codes:
        listed = "; ".join(f"{contact.full_name} ({contact.client_code.upper()})" for contact in contacts[:MAX_CANDIDATES])
        return refuse(tool, m365, f'"{person}" matches Flow contacts at more than one client: {listed}. Which client?')
    return refuse(tool, m365, f"Which client is {person} at? Give the client code or name.")


def _scope_for(tool: str, m365: M365Source, clients: ClientResolver, code: str, note: str) -> TenantScope | ToolResult:
    resolution = clients.resolve(code, require=("m365_tenant_id",))
    if not resolution.ok:
        return refuse(tool, m365, f"{note} {resolution.message}".strip(), client_code=code)
    return TenantScope(resolution.client, note)


def _emails(contact: Any) -> set[str]:
    return {str(value).lower() for value in (contact.email_1, contact.email_2, contact.email_3) if value}


def _words(text: str | None) -> list[str]:
    return re.sub(r"[^a-z0-9@._' -]+", " ", (text or "").casefold()).replace(".", " ").split()


def each_tenant(
    clients: ClientResolver, run: Callable[[ResolvedClient], Any]
) -> list[tuple[ResolvedClient, Any | None, str | None]]:
    """run(client) for every client with a tenant id: (client, result, None), or (client, None, why it failed)."""
    out = []
    for client in clients.every(require=("m365_tenant_id",)):
        try:
            out.append((client, run(client), None))
        except M365NotConfigured:
            raise  # the same for every client: said once, not per client
        except ERRORS as exc:
            out.append((client, None, error_message(client, exc)))
    return out


def skipped_lines(failed: list[tuple[ResolvedClient, str]]) -> list[str]:
    """The clients an all-clients answer could not check, and why, in one short line each."""
    if not failed:
        return []
    lines = ["", f"Not checked ({len(failed)}):"]
    for client, why in failed:
        lines.append(f"- {client.label}: {_short_why(why)}")
    return lines


def _short_why(message: str) -> str:
    if "not been granted access" in message:
        return "tb-brain's app has no consent in this tenant"
    if "license doesn't include " in message:
        return "its license doesn't include " + message.split("license doesn't include ", 1)[1].split(" (that needs")[0]
    return message[:160]


# --- users -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class UserLookup:
    user: User | None
    message: str = ""


def find_user(users: list[User], text: str | None, client: ResolvedClient) -> UserLookup:
    """The one user `text` names (email, UPN, its first part, or name), or why there is none: several match (listed
    to ask about), close matches, or it looks like a device name."""
    wanted = (text or "").strip().strip("\"'`").strip()
    if not wanted:
        return UserLookup(None, "Which user? Give their name or email address.")
    low = wanted.lower()
    exact = [user for user in users if low in user.addresses]
    if not exact and "@" not in low:
        exact = [user for user in users if any(address.split("@")[0] == low for address in user.addresses)]
    if not exact:
        exact = [user for user in users if (user.display_name or "").casefold() == wanted.casefold()]
    if len(exact) == 1:
        return UserLookup(exact[0])
    words = _words(wanted)
    named = exact or [
        user for user in users
        if words and all(any(part.startswith(word) for part in _words(user.display_name)) for word in words)
    ]
    if len(named) == 1:
        return UserLookup(named[0])
    if named:
        listed = "; ".join(f"{user.name} <{user.user_principal_name}>" for user in named[:MAX_CANDIDATES])
        return UserLookup(None, f'"{wanted}" matches more than one user in {client.label}\'s tenant: {listed}. Which one?')
    message = f"{client.label}'s Microsoft 365 tenant has no user named {wanted}."
    if HOSTNAME_RE.fullmatch(wanted):
        return UserLookup(
            None,
            f"{message} {wanted} looks like a device name, not a user: its Intune record is in m365_devices, and its "
            "live state (online, patches, last user) is in Datto RMM.",
        )
    names = {(user.display_name or user.user_principal_name): user for user in users}
    close = difflib.get_close_matches(wanted.casefold(), [name.casefold() for name in names], n=MAX_CANDIDATES, cutoff=CLOSE_MATCH_CUTOFF)
    by_fold = {name.casefold(): name for name in names}
    if close:
        message += " Close matches: " + ", ".join(f"{by_fold[name]} <{names[by_fold[name]].user_principal_name}>" for name in close) + "."
    return UserLookup(None, message)


def roles_by_user(m365: M365Source, client: ResolvedClient) -> dict[str, list[str]]:
    """user id -> the admin roles they hold."""
    held: dict[str, list[str]] = {}
    for role in m365.list_directory_roles(tenant_id=client.m365_tenant_id):
        for member in role.members:
            held.setdefault(member.id, []).append(role.display_name)
    return {user_id: sorted(names) for user_id, names in held.items()}


def sku_names(m365: M365Source, client: ResolvedClient) -> dict[str, str]:
    """skuId -> friendly name, for the tenant's subscribed SKUs."""
    return {sku.sku_id.lower(): sku_name(sku.sku_part_number) for sku in m365.list_subscribed_skus(tenant_id=client.m365_tenant_id)}


def licenses_of(user: User, names: dict[str, str]) -> list[str]:
    return [names.get(lic.sku_id.lower(), lic.sku_id) for lic in user.assigned_licenses]


def account_words(user: User) -> str:
    state = "enabled" if user.account_enabled else "disabled"
    return f"{state}, guest" if user.guest else state


def user_data(user: User, names: dict[str, str], roles: list[str]) -> dict[str, Any]:
    """What a tool result's data carries per user: summary fields, never the raw Graph record or a tenant id."""
    return {
        "name": user.name,
        "upn": user.user_principal_name,
        "enabled": user.account_enabled,
        "guest": user.guest,
        "licenses": licenses_of(user, names),
        "roles": roles,
    }
