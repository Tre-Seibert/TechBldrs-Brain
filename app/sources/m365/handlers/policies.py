"""m365_policies: whether a client enforces MFA (security defaults or Conditional Access, and who is excluded), its
Conditional Access policies, or its security defaults; client_code=all with kind=mfa checks every client."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from app.sources.clients import ClientResolver, ResolvedClient
from app.sources.m365.handlers.common import ALL_CLIENTS_NOTE, capped, each_tenant, license_message, plural, skipped_lines
from app.sources.m365.protocol import M365LicenseRequired, M365Source
from app.sources.m365.schemas import CaPolicy
from app.sources.m365.tools import M365_POLICIES
from app.tools.base import ToolResult

_STATES = {"enabled": "on", "disabled": "off", "enabledForReportingButNotEnforced": "report-only (not enforced)"}
_SPECIAL_USERS = {"All": "all users", "None": "nobody", "GuestsOrExternalUsers": "guests and external users"}
SECURITY_DEFAULTS_ON = (
    "Security defaults are on: every account has to register MFA and is asked for it when Microsoft sees risk, "
    "and admins every time. Legacy authentication is blocked."
)


class PoliciesArgs(BaseModel):
    client_code: str | None = None
    kind: str = "mfa"  # mfa, conditional_access, security_defaults


@dataclass(frozen=True)
class Enforcement:
    level: str  # security_defaults, conditional_access, none
    lines: list[str]
    excluded: tuple[str, ...] = ()


def _names(m365: M365Source, client: ResolvedClient) -> tuple[dict[str, str], dict[str, str]]:
    """user id -> name, and role template id -> role name, to say who a policy covers or leaves out."""
    tenant = client.m365_tenant_id
    users = {user.id: user.name for user in m365.list_users(tenant_id=tenant)}
    roles = {role.role_template_id: role.display_name for role in m365.list_directory_roles(tenant_id=tenant) if role.role_template_id}
    return users, roles


def _who(policy: CaPolicy, users: dict[str, str], roles: dict[str, str]) -> str:
    cond = policy.conditions.users
    included = [_SPECIAL_USERS.get(item, users.get(item, "1 user")) for item in cond.include_users]
    included += [roles.get(item, "an admin role") for item in cond.include_roles]
    included += [f"{plural(len(cond.include_groups), 'group')}"] if cond.include_groups else []
    text = ", ".join(included) or "no one"
    excluded = [_SPECIAL_USERS.get(item, users.get(item, "1 user")) for item in cond.exclude_users]
    excluded += [roles.get(item, "an admin role") for item in cond.exclude_roles]
    excluded += [f"{plural(len(cond.exclude_groups), 'group')}"] if cond.exclude_groups else []
    return text + (f", except {', '.join(excluded)}" if excluded else "")


def _does(policy: CaPolicy) -> str:
    grant = policy.grant_controls
    if not grant:
        return "session controls only"
    controls = list(grant.built_in_controls)
    if grant.authentication_strength and grant.authentication_strength.display_name:
        controls.append(f"authentication strength {grant.authentication_strength.display_name}")
    words = {"mfa": "requires MFA", "block": "blocks access", "compliantDevice": "requires a compliant device",
             "domainJoinedDevice": "requires a hybrid-joined device"}
    return f" {(grant.operator or 'or').lower()} ".join(words.get(item, item) for item in controls) or "no grant controls"


def _policy_line(policy: CaPolicy, users, roles) -> str:
    apps = [app for app in policy.conditions.client_app_types if app != "all"]
    scope = f" · legacy clients ({', '.join(apps)})" if apps else ""
    return f"- {policy.display_name} — {_STATES.get(policy.state, policy.state)} · {_does(policy)} · {_who(policy, users, roles)}{scope}"


def enforcement(m365: M365Source, client: ResolvedClient) -> Enforcement:
    tenant = client.m365_tenant_id
    if m365.security_defaults(tenant_id=tenant).is_enabled:
        return Enforcement("security_defaults", [f"MFA at {client.label}: enforced. {SECURITY_DEFAULTS_ON}"])
    try:
        policies = m365.list_conditional_access_policies(tenant_id=tenant)
    except M365LicenseRequired:
        return Enforcement("none", [
            f"MFA at {client.label}: not enforced tenant-wide. Security defaults are off, and the license doesn't "
            "include Conditional Access (needs Entra ID P1, e.g. Business Premium). Per-user MFA settings can't be "
            "read through Microsoft Graph here."
        ])
    users, roles = _names(m365, client)
    mfa = [policy for policy in policies if policy.requires_mfa]
    on = [policy for policy in mfa if policy.state == "enabled"]
    off = [policy for policy in mfa if policy.state != "enabled"]
    if on:
        lines = [f"MFA at {client.label}: enforced by Conditional Access (security defaults are off)."]
        lines.extend(_policy_line(policy, users, roles) for policy in on)
        excluded = sorted({name for policy in on for name in _excluded_people(policy, users)})
        if excluded:
            lines.append(f"Left out of MFA by policy: {', '.join(excluded)}.")
    else:
        lines = [f"MFA at {client.label}: not enforced. Security defaults are off and no Conditional Access policy that requires MFA is on."]
    if off:
        lines.append("Not enforcing:")
        lines.extend(_policy_line(policy, users, roles) for policy in off)
    return Enforcement("conditional_access" if on else "none", lines, tuple(excluded) if on else ())


def _excluded_people(policy: CaPolicy, users: dict[str, str]) -> list[str]:
    cond = policy.conditions.users
    return [_SPECIAL_USERS.get(item, users.get(item, "1 user")) for item in cond.exclude_users]


def policies(m365: M365Source, client: ResolvedClient, args: PoliciesArgs) -> ToolResult:
    kind = (args.kind or "mfa").lower()
    tenant = client.m365_tenant_id
    if kind == "security_defaults":
        on = m365.security_defaults(tenant_id=tenant).is_enabled
        lines = [f"Security defaults at {client.label}: {'on' if on else 'off'}."]
        lines.append(SECURITY_DEFAULTS_ON if on else "MFA and blocking legacy sign-ins have to come from Conditional Access (Entra ID P1) instead.")
        return ToolResult(tool=M365_POLICIES, source=m365.source_name, data={"security_defaults": on}, client_code=client.client_code, reply="\n".join(lines))
    if kind == "conditional_access":
        try:
            rows = m365.list_conditional_access_policies(tenant_id=tenant)
        except M365LicenseRequired as exc:
            on = m365.security_defaults(tenant_id=tenant).is_enabled
            message = license_message(client, exc.feature) + f" Security defaults are {'on' if on else 'off'}."
            return ToolResult(tool=M365_POLICIES, source=m365.source_name, data={"license_required": exc.feature, "security_defaults": on},
                              client_code=client.client_code, reply=message)
        users, roles = _names(m365, client)
        lines = [f"Conditional Access at {client.label}: {plural(len(rows), 'policy', 'policies')}."]
        lines.extend(capped([_policy_line(policy, users, roles) for policy in sorted(rows, key=lambda p: (p.state != "enabled", p.display_name))]))
        data = [{"name": policy.display_name, "state": policy.state, "requires_mfa": policy.requires_mfa} for policy in rows]
        return ToolResult(tool=M365_POLICIES, source=m365.source_name, data=data, client_code=client.client_code, reply="\n".join(lines))
    found = enforcement(m365, client)
    return ToolResult(tool=M365_POLICIES, source=m365.source_name, data={"mfa_enforced_by": found.level},
                      client_code=client.client_code, reply="\n".join(found.lines))


def policies_all(m365: M365Source, clients: ClientResolver, args: PoliciesArgs) -> ToolResult:
    results = each_tenant(clients, lambda client: enforcement(m365, client))
    failed = [(client, error) for client, _, error in results if error is not None]
    checked = [(client, found) for client, found, error in results if error is None]
    order = {"none": 0, "conditional_access": 1, "security_defaults": 2}
    words = {"none": "no MFA enforcement", "conditional_access": "Conditional Access", "security_defaults": "security defaults"}
    none = [client.label for client, found in checked if found.level == "none"]
    lines = [
        f"MFA enforcement across {plural(len(checked), 'client')}: "
        + (f"{len(none)} with none at all ({', '.join(none)})." if none else "every client checked enforces it."),
        "",
    ]
    for client, found in sorted(checked, key=lambda pair: (order[pair[1].level], pair[0].client_code)):
        left_out = f" ({', '.join(found.excluded)} left out)" if found.excluded else ""
        lines.append(f"- {client.label}: {words[found.level]}{left_out}")
    lines.extend(skipped_lines(failed))
    lines.extend(["", ALL_CLIENTS_NOTE])
    data = [{"client_code": client.client_code, "mfa_enforced_by": found.level} for client, found in checked]
    return ToolResult(tool=M365_POLICIES, source=m365.source_name, data=data, reply="\n".join(lines))
