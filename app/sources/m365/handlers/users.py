"""m365_users and m365_get_user."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, field_validator

from app.sources.clients import ResolvedClient
from app.sources.m365.handlers.common import (
    MFA_KINDS,
    account_words,
    ago,
    capped,
    find_user,
    is_techbldrs,
    licenses_of,
    method_words,
    now_for,
    plural,
    refuse,
    roles_by_user,
    sku_names,
    user_data,
)
from app.sources.m365.protocol import FEATURES, M365LicenseRequired, M365Source
from app.sources.m365.schemas import User
from app.sources.m365.tools import M365_GET_USER, M365_USERS
from app.tools.base import ToolResult


class UsersArgs(BaseModel):
    client_code: str | None = None
    view: str = "list"  # list, count
    enabled: bool | None = None
    licensed: bool | None = None
    user_type: str | None = None  # member, guest
    inactive_days: int | None = None
    role: str | None = None
    admins_only: bool = False
    q: str | None = None
    limit: int = 40


class GetUserArgs(BaseModel):
    client_code: str | None = None
    user: str = ""  # missing: find_user asks which user
    include: list[str] = []

    @field_validator("include", mode="before")
    @classmethod
    def _include(cls, value: Any) -> list[str]:
        if value in (None, ""):
            return []
        items = value.split(",") if isinstance(value, str) else list(value)
        return [str(item).strip().lower() for item in items if str(item).strip()]


def users_with_activity(m365: M365Source, client: ResolvedClient) -> tuple[list[User], bool]:
    """The tenant's users with last sign-in dates when its license has them (Entra ID P1), else without; and which."""
    try:
        return m365.list_users(tenant_id=client.m365_tenant_id, sign_in_activity=True), True
    except M365LicenseRequired:
        return m365.list_users(tenant_id=client.m365_tenant_id), False


def _role_key(text: str) -> list[str]:
    words = text.lower().replace("_", " ").replace("-", " ").split()
    words = ["administrator" if word in ("admin", "admins", "administrators") else word for word in words]
    return ["global", "administrator"] if words in (["ga"], ["global"]) else words


def _holds(roles: list[str], wanted: str) -> bool:
    words = _role_key(wanted)
    return any(all(any(part.startswith(word) for part in role.lower().split()) for word in words) for role in roles)


def _last_sign_in(user: User, activity: bool, now) -> str:
    if not activity:
        return "last sign-in: needs Entra ID P1"
    last = user.sign_in_activity.last if user.sign_in_activity else None
    return f"last sign-in {ago(last, now)}"


def list_users(m365: M365Source, client: ResolvedClient, args: UsersArgs) -> ToolResult:
    now = now_for(m365)
    if args.inactive_days:
        users, activity = m365.list_users(tenant_id=client.m365_tenant_id, sign_in_activity=True), True
    else:
        users, activity = users_with_activity(m365, client)
    names = sku_names(m365, client)
    roles = roles_by_user(m365, client)
    every = sorted(users, key=lambda user: user.name.casefold())
    if (args.view or "").lower() == "count":
        return _count(m365, client, every, names, roles)
    rows, words = every, []
    if args.enabled is not None:
        rows = [user for user in rows if bool(user.account_enabled) == args.enabled]
        words.append("enabled" if args.enabled else "disabled")
    if args.licensed is not None:
        rows = [user for user in rows if bool(user.assigned_licenses) == args.licensed]
        words.append("licensed" if args.licensed else "unlicensed")
    if args.user_type:
        guest = args.user_type.lower().startswith("guest")
        rows = [user for user in rows if user.guest == guest]
        words.append("guests" if guest else "members")
    if args.inactive_days:
        days = args.inactive_days
        rows = [
            user for user in rows
            if user.account_enabled and (
                not user.sign_in_activity or not user.sign_in_activity.last
                or (now - user.sign_in_activity.last).total_seconds() >= days * 86400
            )
        ]
        words.append(f"enabled, no sign-in for {days}+ days")
    if args.role:
        rows = [user for user in rows if _holds(roles.get(user.id, []), args.role)]
        words.append(f"holding {args.role.replace('_', ' ')}")
    elif args.admins_only:
        rows = [user for user in rows if roles.get(user.id)]
        words.append("with an admin role")
    if args.q:
        needle = args.q.strip().lower()
        rows = [user for user in rows if needle in user.name.lower() or any(needle in address for address in user.addresses)]
        words.append(f"matching {args.q.strip()}")
    limit = max(1, min(args.limit or 40, 100))
    heading = f"{plural(len(rows), 'user')} in {client.label}'s Microsoft 365 tenant"
    if words:
        heading += f" ({'; '.join(words)}; {len(every)} in all)"
    lines = [heading + ".", ""]
    if not rows:
        lines.append("None match.")
    admin_view = bool(args.role or args.admins_only)
    lines.extend(capped([_line(user, names, roles.get(user.id, []), activity, now, admin_view) for user in rows], limit=limit))
    if admin_view and rows:
        others = [user.name for user in rows if not is_techbldrs(user.user_principal_name, user.display_name)]
        lines.extend(["", f"Not TechBldrs accounts: {', '.join(others)}." if others else "All of them are TechBldrs accounts."])
    if not activity:
        lines.extend(["", "Last sign-in dates: " + _no_activity()])
    return ToolResult(
        tool=M365_USERS,
        source=m365.source_name,
        data={"count": len(rows), "users": [user_data(user, names, roles.get(user.id, [])) for user in rows[:limit]]},
        client_code=client.client_code,
        reply="\n".join(lines).rstrip(),
    )


def _no_activity() -> str:
    what, needs = FEATURES["sign_in_activity"]
    return f"the tenant's license doesn't include {what} (needs {needs})."


def _line(user: User, names: dict[str, str], roles: list[str], activity: bool, now, admin_view: bool) -> str:
    parts = [account_words(user), ", ".join(licenses_of(user, names)) or "no license", _last_sign_in(user, activity, now)]
    if roles:
        parts.append(", ".join(roles))
    if admin_view:
        parts.append("TechBldrs" if is_techbldrs(user.user_principal_name, user.display_name) else "not a TechBldrs account")
    return f"- {user.name} <{user.user_principal_name}> — " + " · ".join(parts)


def _count(m365: M365Source, client: ResolvedClient, users: list[User], names, roles) -> ToolResult:
    members = [user for user in users if not user.guest]
    enabled = [user for user in members if user.account_enabled]
    disabled = [user for user in members if not user.account_enabled]
    guests = [user for user in users if user.guest]
    licensed = [user for user in users if user.assigned_licenses]
    licensed_enabled = [user for user in enabled if user.assigned_licenses]
    admins = [user for user in users if roles.get(user.id)]
    lines = [
        f"{client.label} has {plural(len(users), 'account')} in Microsoft 365.",
        f"- Members: {len(members)} ({len(enabled)} enabled, {len(disabled)} disabled; shared mailboxes are disabled accounts)",
        f"- Licensed: {len(licensed)} ({len(licensed_enabled)} enabled); unlicensed: {len(users) - len(licensed)}",
        f"- Guests: {len(guests)}",
        f"- With an admin role: {len(admins)}",
    ]
    if any(not user.account_enabled and user.assigned_licenses for user in members):
        held = [user.name for user in members if not user.account_enabled and user.assigned_licenses]
        lines.append(f"- Disabled but still licensed: {len(held)} ({', '.join(held)})")
    data = {
        "accounts": len(users), "members": len(members), "enabled": len(enabled), "disabled": len(disabled),
        "guests": len(guests), "licensed": len(licensed), "unlicensed": len(users) - len(licensed), "admins": len(admins),
    }
    return ToolResult(tool=M365_USERS, source=m365.source_name, data=data, client_code=client.client_code, reply="\n".join(lines))


def get_user(m365: M365Source, client: ResolvedClient, args: GetUserArgs) -> ToolResult:
    users, activity = users_with_activity(m365, client)
    lookup = find_user(users, args.user, client)
    if lookup.user is None:
        return refuse(M365_GET_USER, m365, lookup.message, client_code=client.client_code)
    user = lookup.user
    now = now_for(m365)
    names = sku_names(m365, client)
    roles = roles_by_user(m365, client).get(user.id, [])
    created = user.created_date_time.date().isoformat() if user.created_date_time else "unknown"
    lines = [
        f"{user.name} <{user.user_principal_name}> in {client.label}'s Microsoft 365 tenant",
        f"Account: {account_words(user)}{'' if user.guest else ' (member)'}, created {created}",
    ]
    if user.job_title:
        lines.append(f"Job title: {user.job_title}")
    lines.append(f"Licenses: {', '.join(licenses_of(user, names)) or 'none'}")
    lines.append(f"Admin roles: {', '.join(roles) or 'none'}")
    data: dict[str, Any] = {**user_data(user, names, roles), "created": created}
    if activity:
        info = user.sign_in_activity
        last = info.last if info else None
        success = info.last_successful_sign_in_date_time if info else None
        line = f"Last sign-in: {ago(last, now)}"
        if success and (last is None or abs((last - success).total_seconds()) > 60):
            line = f"Last sign-in attempt: {ago(last, now)}; last successful: {ago(success, now)}"
        lines.append(line)
        data["last_sign_in"] = last.isoformat() if last else None
    else:
        lines.append("Last sign-in: not available; " + _no_activity())
    include = set(args.include)
    if include & {"auth_methods", "mfa", "methods", "authentication", "auth", "authentication_methods"}:
        methods = m365.list_auth_methods(tenant_id=client.m365_tenant_id, user_id=user.id)
        mfa = [method for method in methods if method.kind in MFA_KINDS]
        shown = [
            method_words(method.kind) + (f" ({method.display_name})" if method.kind == "microsoftAuthenticator" and method.display_name else "")
            for method in mfa
        ]
        lines.append(f"MFA methods: {', '.join(shown)}" if shown else "MFA methods: none registered (password only)")
        data["mfa_methods"] = [method.kind for method in mfa]
    if include & {"groups", "group", "membership", "memberships", "member_of"}:
        groups = sorted(
            (item.display_name or item.id for item in m365.list_member_of(tenant_id=client.m365_tenant_id, user_id=user.id) if item.kind == "group"),
            key=str.casefold,
        )
        lines.append(f"Groups ({len(groups)}): {', '.join(groups)}" if groups else "Groups: none")
        data["groups"] = groups
    return ToolResult(tool=M365_GET_USER, source=m365.source_name, data=data, client_code=client.client_code, reply="\n".join(lines))
