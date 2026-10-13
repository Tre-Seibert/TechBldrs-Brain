"""Shared by the Datto tools: which sites a question covers, finding a device by hostname, and how a device,
its patch and antivirus state, and a date read in a reply. Replies are short summaries, capped in length;
counts and rankings are computed here, never by the model.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.sources.clients import ClientResolver, Outcome
from app.sources.datto.protocol import DattoSource
from app.sources.datto.schemas import DattoDevice, DattoSite
from app.tools.base import ToolResult

# Longest list a reply shows; the rest is counted ("... and 12 more").
MAX_LINES = 40
MAX_CLOSE_MATCHES = 5
# difflib ratio a hostname must reach to be offered as a close match (WDON-FD02 -> WDON-FD01, WDON-OP02).
CLOSE_MATCH_CUTOFF = 0.75
# What the user may say instead of a client code to mean every Datto site.
_ALL_CLIENTS = frozenset({"all", "*", "any", "every", "everyone", "all clients", "every client", "all sites"})
ALL_CLIENTS_NOTE = "All clients: this checked every Datto site, so it takes longer than a one-client question."

PATCH_WORDS = {
    "FullyPatched": "fully patched",
    "ApprovedPending": "approved patches pending",
    "InstallError": "patch install errors",
    "RebootRequired": "reboot needed to finish patching",
    "NoPolicy": "no patch policy",
    "NoData": "no patch data yet",
}
NEEDS_PATCHES = frozenset({"ApprovedPending", "InstallError", "RebootRequired"})
AV_WORDS = {
    "RunningAndUpToDate": "running, up to date",
    "RunningAndNotUpToDate": "running, out of date",
    "NotRunning": "not running",
    "NotDetected": "none detected",
}
AV_ISSUES = frozenset({"RunningAndNotUpToDate", "NotRunning", "NotDetected"})
WORKSTATION_CATEGORIES = frozenset({"desktop", "laptop", "workstation"})

_OS_BUILD_RE = re.compile(r"\s+\d+\.\d+\.\d+\s*$")
# Hostnames the way TechBldrs names them: client code, a dash, letters, then a number (WDON-FD01, BUCK-SRV01,
# WDON-IMG-01). Letters after the dash, so ticket labels (WDON-1842) don't match. Also Datto's routing pattern.
HOSTNAME_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]{2,5}-[A-Za-z]{1,6}-?\d{1,4}\b")


@dataclass(frozen=True)
class Scope:
    """The Datto sites one question covers: one client's, or every site (client_code=all)."""

    label: str  # "Western Dental (WDON)" or "all clients"
    client_code: str | None  # None for all clients
    sites: tuple[DattoSite, ...]

    @property
    def everyone(self) -> bool:
        return self.client_code is None


def now_for(datto: DattoSource) -> datetime:
    """The time 'ago' is measured from: the stub's pinned clock, else the real one."""
    return getattr(datto, "now", None) or datetime.now(timezone.utc)


def refuse(tool: str, datto: DattoSource, message: str, *, client_code: str | None = None) -> ToolResult:
    return ToolResult(ok=False, tool=tool, source=datto.source_name, client_code=client_code, error=message, reply=message)


def wants_all(text: str | None) -> bool:
    return (text or "").strip().lower() in _ALL_CLIENTS


def resolve_scope(tool: str, datto: DattoSource, clients: ClientResolver, client_code: str | None) -> Scope | ToolResult:
    """The sites for what the user called the client, or a ToolResult saying why there are none (unknown client,
    ambiguous name, no Datto site)."""
    if wants_all(client_code):
        return Scope("all clients", None, tuple(sorted(datto.list_sites(), key=lambda site: site.name.lower())))
    resolution = clients.resolve(client_code, require=("datto_site_uid",))
    if not resolution.ok:
        found = resolution.client.client_code if resolution.client else None
        return refuse(tool, datto, resolution.message, client_code=found)
    client = resolution.client
    sites = tuple(sorted((site for site in datto.list_sites() if site.client_code == client.client_code), key=lambda s: s.name))
    return Scope(client.label, client.client_code, sites)


def devices_in(datto: DattoSource, scope: Scope) -> list[DattoDevice]:
    rows = [device for site in scope.sites for device in datto.list_site_devices(site_uid=site.uid)]
    return sorted((row for row in rows if not row.deleted), key=lambda row: row.hostname.upper())


NO_DEVICE = "Datto RMM has no device named"

# A person the user's message names, as a device's owner or user: "Debe's computer", "what does Debe use", "the PC
# used by Debe". Capitalized but not all caps, so client codes don't count; nor do these words.
_PERSON_RE = re.compile(
    r"\b([A-Z][a-z]+)['’]s\b|\b(?:does|did)\s+([A-Z][a-z]+)\s+use\b|\bused\s+by\s+([A-Z][a-z]+)\b"
)
_NOT_PEOPLE = frozenset({
    "What", "That", "It", "Who", "Where", "Here", "There", "Let", "He", "She", "How", "When", "Why", "Today",
    "Yesterday", "Everyone", "Someone", "Datto", "Flow", "Microsoft", "Windows",
})


def person_named(text: str | None, clients: ClientResolver) -> str | None:
    """The first person `text` names as a device's user ("Debe" in "Debe's computer is slow"), or None. A client's
    name in the possessive ("Buckhead's server") is not a person."""
    for hit in _PERSON_RE.finditer(text or ""):
        name = next(group for group in hit.groups() if group)
        if name in _NOT_PEOPLE:
            continue
        try:
            if clients.resolve(name).outcome is not Outcome.UNKNOWN:
                continue
        except Exception:  # noqa: BLE001 - Flow unreachable: offering the last_user lookup is still right
            pass
        return name
    return None


@dataclass(frozen=True)
class HostLookup:
    device: DattoDevice | None
    message: str = ""


def find_device(datto: DattoSource, hostname: str) -> HostLookup:
    """The device with exactly this hostname (any case), or why there is none, with close matches."""
    name = (hostname or "").strip().strip("\"'`").strip()
    if not name:
        return HostLookup(None, "Which device? Give its hostname, e.g. WDON-FD01.")
    exact = [row for row in datto.list_account_devices(hostname=name) if row.hostname.lower() == name.lower() and not row.deleted]
    if exact:
        return HostLookup(sorted(exact, key=lambda row: row.last_seen or datetime.min.replace(tzinfo=timezone.utc))[-1])
    # Close matches from the site the hostname's prefix names (WDON-...), else from every device on the account.
    prefix = name.split("-")[0].upper()
    site_uids = [site.uid for site in datto.list_sites() if site.client_code == prefix]
    pool = (
        [row for uid in site_uids for row in datto.list_site_devices(site_uid=uid)]
        if site_uids
        else datto.list_account_devices()
    )
    names = sorted({row.hostname for row in pool if not row.deleted}, key=str.upper)
    by_upper = {item.upper(): item for item in names}
    close = [
        by_upper[item]
        for item in difflib.get_close_matches(name.upper(), list(by_upper), n=MAX_CLOSE_MATCHES, cutoff=CLOSE_MATCH_CUTOFF)
    ]
    message = f"{NO_DEVICE} {name}."
    if close:
        message += f" Close matches: {', '.join(close)}."
    elif site_uids and names:
        shown = names[:12]
        more = f", and {len(names) - len(shown)} more" if len(names) > len(shown) else ""
        message += f" {prefix} devices in Datto: {', '.join(shown)}{more}."
    else:
        message += " No hostname in Datto RMM is close to it."
    return HostLookup(None, message)


def short_os(text: str | None) -> str:
    """'Microsoft Windows 11 Pro 10.0.22631' -> 'Windows 11 Pro'."""
    value = _OS_BUILD_RE.sub("", (text or "").strip())
    return re.sub(r"^Microsoft\s+", "", value) or "unknown OS"


def ago(moment: datetime | None, now: datetime) -> str:
    """'2 minutes ago', '3 days ago (2026-10-10)'; 'never' when there is no date."""
    if moment is None:
        return "never"
    seconds = max((now - moment).total_seconds(), 0)
    if seconds < 3600:
        minutes = max(int(seconds // 60), 1) if seconds >= 60 else 0
        return "just now" if minutes == 0 else f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(seconds // 86400)
    return f"{days} day{'s' if days != 1 else ''} ago ({moment.date().isoformat()})"


def days_since(moment: datetime | None, now: datetime) -> float | None:
    return None if moment is None else (now - moment).total_seconds() / 86400


def patch_text(device: DattoDevice) -> str:
    patches = device.patch_management
    status = patches.patch_status if patches else None
    if not status:
        return "no patch data"
    if status == "ApprovedPending" and patches.patches_approved_pending:
        count = patches.patches_approved_pending
        return f"{count} approved patch{'es' if count != 1 else ''} pending"
    words = PATCH_WORDS.get(status, status)
    if status == "InstallError" and patches.patches_approved_pending:
        words += f", {patches.patches_approved_pending} pending"
    return words


def av_text(device: DattoDevice) -> str:
    av = device.antivirus
    if not av or not av.antivirus_status:
        return "no antivirus data"
    words = AV_WORDS.get(av.antivirus_status, av.antivirus_status)
    return f"{av.antivirus_product}, {words}" if av.antivirus_product else words


def needs_patches(device: DattoDevice) -> bool:
    patches = device.patch_management
    return bool(patches and (patches.patch_status in NEEDS_PATCHES or (patches.patches_approved_pending or 0) > 0))


def av_issue(device: DattoDevice) -> bool:
    return bool(device.antivirus and device.antivirus.antivirus_status in AV_ISSUES)


def device_line(device: DattoDevice, now: datetime) -> str:
    """One device in a list."""
    state = "online" if device.online else f"offline, last seen {ago(device.last_seen, now)}"
    parts = [device.category, short_os(device.operating_system), state]
    if device.last_logged_in_user:
        parts.append(f"last user {device.last_logged_in_user}")
    parts.append(f"patches: {patch_text(device)}")
    parts.append(f"AV: {av_text(device)}")
    if device.reboot_required:
        parts.append("reboot needed")
    return f"- {device.hostname} — " + " · ".join(parts)


def device_data(device: DattoDevice) -> dict[str, Any]:
    """What a tool result's data carries per device: the summary fields, never the raw API record."""
    return {
        "hostname": device.hostname,
        "client_code": device.client_code,
        "type": device.category,
        "os": short_os(device.operating_system),
        "online": device.online,
        "last_seen": device.last_seen.isoformat() if device.last_seen else None,
        "last_user": device.last_logged_in_user,
        "patch_status": device.patch_management.patch_status if device.patch_management else None,
        "av_status": device.antivirus.antivirus_status if device.antivirus else None,
        "reboot_required": device.reboot_required,
    }


def capped(lines: list[str], *, limit: int = MAX_LINES, what: str = "more") -> list[str]:
    if len(lines) <= limit:
        return lines
    return [*lines[:limit], f"- ... and {len(lines) - limit} {what} (ask with a narrower filter to see them)"]


def plural(count: int, word: str, many: str | None = None) -> str:
    return f"{count} {word if count == 1 else (many or word + 's')}"
