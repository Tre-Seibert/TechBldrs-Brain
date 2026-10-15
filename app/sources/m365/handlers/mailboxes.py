"""m365_mailboxes: mailbox facts, never mail content. Shared vs user mailboxes (mailboxSettings.userPurpose), size
against quota (Microsoft's mailbox usage report), and inbox rules that forward mail outside the client.

A tenant with "conceal user names in reports" on returns hashed names from the usage report; that is detected and
said, never shown as if the names were real. Forwarding set on the mailbox itself in Exchange (ForwardingSmtpAddress)
is not in Microsoft Graph, so it is not covered, and the reply says so.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from app.sources.clients import ResolvedClient
from app.sources.m365.handlers.common import capped, find_user, gb, plural, refuse
from app.sources.m365.protocol import M365RequestError, M365Source
from app.sources.m365.schemas import MailboxUsage, User
from app.sources.m365.tools import M365_MAILBOXES
from app.tools.base import ToolResult

# Per-mailbox lookups are one Graph call each; past this many the reply says it stopped.
MAX_MAILBOXES = 200
MAX_USAGE_LINES = 15
_HASH_RE = re.compile(r"^[0-9A-F]{16,64}$", re.IGNORECASE)
CONCEALED = (
    "{label}'s tenant conceals user names in Microsoft 365 usage reports (the report returns hashed names), so mailbox "
    "sizes can't be matched to people. To show names: Microsoft 365 admin center > Settings > Org settings > Reports > "
    "clear 'Display concealed user, group, and site names in all reports'."
)
NOT_COVERED = (
    "Not covered: forwarding set on the mailbox itself in Exchange (ForwardingSmtpAddress), which Microsoft Graph "
    "doesn't show."
)


class MailboxesArgs(BaseModel):
    client_code: str | None = None
    view: str | None = None  # list, usage, external_forwarding_rules
    kind: str | None = None  # shared, user, all
    user: str | None = None
    over_percent: int | None = None


def is_hashed(name: str | None) -> bool:
    """A concealed report name: a long hex string, no @."""
    text = (name or "").strip()
    return bool(text) and "@" not in text and bool(_HASH_RE.match(text))


def concealed(rows: list[MailboxUsage]) -> bool:
    return bool(rows) and sum(1 for row in rows if is_hashed(row.user_principal_name)) * 2 >= len(rows)


def _mailbox_users(users: list[User]) -> list[User]:
    return sorted((user for user in users if user.mail and not user.guest), key=lambda user: user.name.casefold())


def mailboxes(m365: M365Source, client: ResolvedClient, args: MailboxesArgs) -> ToolResult:
    view = (args.view or "").lower()
    if view not in ("list", "usage", "external_forwarding_rules"):
        view = "usage" if (args.user or args.over_percent) else "list"
    if view == "usage":
        return _usage(m365, client, args)
    if view == "external_forwarding_rules":
        return _forwarding(m365, client, args)
    return _list(m365, client, args)


def _list(m365: M365Source, client: ResolvedClient, args: MailboxesArgs) -> ToolResult:
    tenant = client.m365_tenant_id
    kind = (args.kind or "all").lower()
    candidates = _mailbox_users(m365.list_users(tenant_id=tenant))
    found: list[tuple[User, str]] = []
    for user in candidates[:MAX_MAILBOXES]:
        try:
            purpose = (m365.mailbox_settings(tenant_id=tenant, user_id=user.id).user_purpose or "user").lower()
        except M365RequestError:
            continue  # no Exchange mailbox
        if kind in ("all", purpose):
            found.append((user, purpose))
    what = {"shared": "shared mailboxes", "user": "user mailboxes"}.get(kind, "mailboxes")
    lines = [f"{len(found)} {what if len(found) != 1 else what[:-2]} at {client.label}.", ""]
    if not found:
        lines.append("None.")
    for user, purpose in found:
        created = user.created_date_time.date().isoformat() if user.created_date_time else "?"
        state = "sign-in blocked" if not user.account_enabled else "sign-in allowed"
        lines.append(f"- {user.name} <{user.user_principal_name}> — {purpose} mailbox · created {created} · {state}")
    lines = capped(lines, limit=60)
    if len(candidates) > MAX_MAILBOXES:
        lines.append(f"Only the first {MAX_MAILBOXES} of {len(candidates)} mailboxes were checked.")
    data = [{"upn": user.user_principal_name, "kind": purpose} for user, purpose in found]
    return ToolResult(tool=M365_MAILBOXES, source=m365.source_name, data=data, client_code=client.client_code, reply="\n".join(lines).rstrip())


def _quota(row: MailboxUsage) -> int | None:
    return row.prohibit_send_receive_quota_in_bytes or row.prohibit_send_quota_in_bytes or row.issue_warning_quota_in_bytes


def _percent(row: MailboxUsage) -> float | None:
    quota = _quota(row)
    return (row.storage_used_in_bytes or 0) / quota * 100 if quota else None


def _usage_line(row: MailboxUsage, name: str) -> str:
    percent = _percent(row)
    full = f" ({percent:.0f}%)" if percent is not None else ""
    kind = (row.recipient_type or "user").lower()
    return f"- {name} — {gb(row.storage_used_in_bytes)} of {gb(_quota(row))}{full} · {kind} mailbox"


def _usage(m365: M365Source, client: ResolvedClient, args: MailboxesArgs) -> ToolResult:
    tenant = client.m365_tenant_id
    rows = [row for row in m365.mailbox_usage(tenant_id=tenant) if not row.is_deleted]
    over = args.over_percent
    if concealed(rows):
        full = [row for row in rows if (_percent(row) or 0) >= (over or 90)]
        message = CONCEALED.format(label=client.label)
        totals = f" Without names: {plural(len(rows), 'mailbox', 'mailboxes')}, {len(full)} at least {over or 90}% full."
        reply = message + totals
        data = {"concealed": True, "mailboxes": len(rows), "over_percent": len(full)}
        return ToolResult(tool=M365_MAILBOXES, source=m365.source_name, data=data, client_code=client.client_code, reply=reply)
    users = m365.list_users(tenant_id=tenant)
    names = {user.user_principal_name.lower(): user for user in users}
    if (args.user or "").strip():
        lookup = find_user(users, args.user, client)
        if lookup.user is None:
            return refuse(M365_MAILBOXES, m365, lookup.message, client_code=client.client_code)
        user = lookup.user
        row = next((row for row in rows if row.user_principal_name.lower() == user.user_principal_name.lower()), None)
        if row is None:
            reply = f"Microsoft's mailbox usage report has no mailbox for {user.name} <{user.user_principal_name}> (no Exchange mailbox, or created in the last two days)."
            return ToolResult(tool=M365_MAILBOXES, source=m365.source_name, data={}, client_code=client.client_code, reply=reply)
        percent = _percent(row)
        lines = [
            f"{user.name}'s mailbox <{user.user_principal_name}>: {gb(row.storage_used_in_bytes)} of {gb(_quota(row))}"
            + (f" ({percent:.0f}% full)" if percent is not None else "") + ".",
            f"Warning at {gb(row.issue_warning_quota_in_bytes)}; sending stops at {gb(row.prohibit_send_quota_in_bytes)}.",
            f"Items: {row.item_count:,}" if row.item_count is not None else "Items: ?",
        ]
        lines.append(REFRESHED)
        data = {"upn": user.user_principal_name, "used_gb": round((row.storage_used_in_bytes or 0) / 1024**3, 1), "percent": round(percent or 0)}
        return ToolResult(tool=M365_MAILBOXES, source=m365.source_name, data=data, client_code=client.client_code, reply="\n".join(lines))
    ranked = sorted(rows, key=lambda row: -(_percent(row) or 0))
    if over:
        ranked = [row for row in ranked if (_percent(row) or 0) >= over]
    heading = (
        f"{plural(len(ranked), 'mailbox', 'mailboxes')} at {client.label} at least {over}% full ({len(rows)} in all)."
        if over else f"Mailbox sizes at {client.label}, fullest first ({plural(len(rows), 'mailbox', 'mailboxes')}):"
    )
    lines = [heading, ""]
    if not ranked:
        lines.append("None.")

    def name(row: MailboxUsage) -> str:
        user = names.get(row.user_principal_name.lower())
        return f"{user.name} <{row.user_principal_name}>" if user else (row.display_name or row.user_principal_name)

    lines.extend(capped([_usage_line(row, name(row)) for row in ranked], limit=MAX_USAGE_LINES))
    lines.extend(["", REFRESHED])
    data = [{"upn": row.user_principal_name, "percent": round(_percent(row) or 0)} for row in ranked[:MAX_USAGE_LINES]]
    return ToolResult(tool=M365_MAILBOXES, source=m365.source_name, data=data, client_code=client.client_code, reply="\n".join(lines))


REFRESHED = "(From Microsoft's mailbox usage report, which runs a day or two behind.)"


def _forwarding(m365: M365Source, client: ResolvedClient, args: MailboxesArgs) -> ToolResult:
    tenant = client.m365_tenant_id
    users = m365.list_users(tenant_id=tenant)
    own = {domain.id.lower() for domain in m365.list_domains(tenant_id=tenant)}
    if (args.user or "").strip():
        lookup = find_user(users, args.user, client)
        if lookup.user is None:
            return refuse(M365_MAILBOXES, m365, lookup.message, client_code=client.client_code)
        candidates = [lookup.user]
    else:
        candidates = _mailbox_users(users)
    checked, outside, inside, rules_seen = 0, [], [], []
    for user in candidates[:MAX_MAILBOXES]:
        try:
            rules = m365.list_inbox_rules(tenant_id=tenant, user_id=user.id)
        except M365RequestError:
            continue
        checked += 1
        for rule in rules:
            rules_seen.append((user, rule))
            for address in rule.actions.forwards:
                domain = address.rsplit("@", 1)[-1].lower()
                (inside if domain in own else outside).append((user, rule, address))
    lines = [
        f"Inbox rules forwarding mail outside {client.label}: {len(outside)} (checked {plural(checked, 'mailbox', 'mailboxes')})."
    ]
    for user, rule, address in outside:
        extra = []
        if rule.actions.move_to_folder:
            extra.append(f"moves mail to {rule.actions.move_to_folder}")
        if rule.actions.mark_as_read:
            extra.append("marks it read")
        if rule.actions.delete:
            extra.append("deletes it")
        state = "enabled" if rule.is_enabled else "disabled"
        lines.append(
            f"- {user.name} <{user.user_principal_name}> — rule \"{rule.display_name or '?'}\" forwards to {address} ({state}"
            + (f"; also {', '.join(extra)}" if extra else "") + ")"
        )
    if inside:
        lines.append("")
        lines.append(f"Forwarding inside the organization: {plural(len(inside), 'rule')} (" + "; ".join(
            f"{user.name} → {address}" for user, _, address in inside) + ").")
    if (args.user or "").strip() and candidates:
        user = candidates[0]
        mine = [rule for owner, rule in rules_seen if owner.id == user.id]
        lines.extend(["", f"All inbox rules for {user.name}: " + (", ".join(f'"{rule.display_name}"' for rule in mine) or "none") + "."])
    lines.extend(["", NOT_COVERED])
    data = {
        "checked": checked,
        "external": [{"upn": user.user_principal_name, "rule": rule.display_name, "to": address} for user, rule, address in outside],
    }
    return ToolResult(tool=M365_MAILBOXES, source=m365.source_name, data=data, client_code=client.client_code, reply="\n".join(lines))
