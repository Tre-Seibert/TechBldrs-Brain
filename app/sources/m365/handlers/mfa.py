"""m365_mfa_report: who has MFA registered and who doesn't, from the MFA registration report where the tenant's license
has it (Entra ID P1), otherwise from each user's registered authentication methods. The reply says which it used."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from app.sources.clients import ClientResolver, ResolvedClient
from app.sources.m365.handlers.common import (
    ALL_CLIENTS_NOTE,
    MFA_KINDS,
    capped,
    each_tenant,
    method_words,
    plural,
    roles_by_user,
    skipped_lines,
)
from app.sources.m365.protocol import M365LicenseRequired, M365RequestError, M365Source
from app.sources.m365.schemas import User
from app.sources.m365.tools import M365_MFA_REPORT
from app.tools.base import ToolResult

# Per-user method lookups are one Graph call each; past this many accounts the reply says it stopped.
MAX_PER_USER = 150
REPORT = "the MFA registration report (Entra ID P1)"
PER_USER = (
    "each user's registered authentication methods, because this tenant's license doesn't include the MFA "
    "registration report (needs Entra ID P1)"
)


class MfaArgs(BaseModel):
    client_code: str | None = None
    registered: bool | None = None
    admins_only: bool = False


@dataclass(frozen=True)
class MfaRow:
    user: User
    registered: bool
    methods: tuple[str, ...]
    roles: tuple[str, ...]


@dataclass(frozen=True)
class MfaState:
    rows: list[MfaRow]
    source: str
    unchecked: int = 0


def mfa_state(m365: M365Source, client: ResolvedClient) -> MfaState:
    """Every enabled member account's MFA registration. Guests and disabled accounts (shared mailboxes too) are left out."""
    tenant = client.m365_tenant_id
    users = [user for user in m365.list_users(tenant_id=tenant) if user.account_enabled and not user.guest]
    roles = roles_by_user(m365, client)
    try:
        details = {row.id: row for row in m365.list_registration_details(tenant_id=tenant)}
    except M365LicenseRequired:
        details = None
    if details is not None:
        rows = [
            MfaRow(
                user,
                bool(details[user.id].is_mfa_registered) if user.id in details else False,
                tuple(details[user.id].methods_registered) if user.id in details else (),
                tuple(roles.get(user.id, [])),
            )
            for user in users
        ]
        return MfaState(rows, REPORT)
    rows = []
    for user in users[:MAX_PER_USER]:
        try:
            methods = [m.kind for m in m365.list_auth_methods(tenant_id=tenant, user_id=user.id) if m.kind in MFA_KINDS]
        except M365RequestError:
            methods = []
        rows.append(MfaRow(user, bool(methods), tuple(methods), tuple(roles.get(user.id, []))))
    return MfaState(rows, PER_USER, unchecked=max(len(users) - MAX_PER_USER, 0))


def _methods(row: MfaRow) -> str:
    return ", ".join(dict.fromkeys(method_words(kind) for kind in row.methods)) or "registered"


def mfa_report(m365: M365Source, client: ResolvedClient, args: MfaArgs) -> ToolResult:
    state = mfa_state(m365, client)
    rows = sorted(state.rows, key=lambda row: (not row.roles, row.user.name.casefold()))
    if args.admins_only:
        rows = [row for row in rows if row.roles]
    without = [row for row in rows if not row.registered]
    with_mfa = [row for row in rows if row.registered]
    who = "admin accounts" if args.admins_only else "enabled accounts"
    lines = [
        f"MFA at {client.label}: {len(with_mfa)} of {plural(len(rows), who[:-1], who)} "
        f"{'has' if len(rows) == 1 else 'have'} MFA registered, {len(without)} {'doesn' if len(without) == 1 else 'don'}'t.",
    ]
    if args.registered is not True:
        lines.extend(["", "No MFA registered:" if without else "No MFA registered: none."])
        lines.extend(capped([
            f"- {row.user.name} <{row.user.user_principal_name}>" + (f" — {', '.join(row.roles)} (admin without MFA)" if row.roles else "")
            for row in without
        ]))
    if args.registered is not False:
        lines.extend(["", "MFA registered:" if with_mfa else "MFA registered: none."])
        lines.extend(capped([
            f"- {row.user.name} <{row.user.user_principal_name}> — {_methods(row)}" + (f" · {', '.join(row.roles)}" if row.roles else "")
            for row in with_mfa
        ]))
    lines.extend(["", f"Source: {state.source}. Guests and disabled accounts (shared mailboxes too) are left out."])
    if state.unchecked:
        lines.append(f"Only the first {MAX_PER_USER} accounts were checked; {state.unchecked} more were not.")
    data = {
        "source": "registration_report" if state.source == REPORT else "per_user_methods",
        "accounts": len(rows),
        "with_mfa": len(with_mfa),
        "without_mfa": [row.user.user_principal_name for row in without],
        "admins_without_mfa": [row.user.user_principal_name for row in without if row.roles],
    }
    return ToolResult(tool=M365_MFA_REPORT, source=m365.source_name, data=data, client_code=client.client_code, reply="\n".join(lines))


def mfa_all(m365: M365Source, clients: ClientResolver, args: MfaArgs) -> ToolResult:
    results = each_tenant(clients, lambda client: mfa_state(m365, client))
    checked = [(client, state) for client, state, error in results if error is None]
    failed = [(client, error) for client, _, error in results if error is not None]

    def counts(state: MfaState) -> tuple[list[MfaRow], list[MfaRow]]:
        rows = [row for row in state.rows if row.roles] if args.admins_only else state.rows
        return rows, [row for row in rows if not row.registered]

    ranked = sorted(checked, key=lambda pair: (-len(counts(pair[1])[1]), pair[0].client_code))
    who = "admins" if args.admins_only else "enabled accounts"
    lines = [f"MFA across {plural(len(checked), 'client')} in Microsoft 365, most without MFA first ({who}):", ""]
    data = []
    for client, state in ranked:
        rows, without = counts(state)
        admins = [row.user.name for row in without if row.roles]
        line = f"- {client.label}: {len(without)} of {len(rows)} without MFA"
        if admins and not args.admins_only:
            line += f" ({plural(len(admins), 'admin')}: {', '.join(admins)})"
        elif args.admins_only and without:
            line += f" ({', '.join(row.user.name for row in without)})"
        if state.source != REPORT:
            line += " [per-user methods]"
        lines.append(line)
        data.append({"client_code": client.client_code, "accounts": len(rows), "without_mfa": len(without), "admins_without_mfa": admins})
    lines.extend(skipped_lines(failed))
    lines.extend(["", ALL_CLIENTS_NOTE])
    return ToolResult(tool=M365_MFA_REPORT, source=m365.source_name, data=data, reply="\n".join(lines))
