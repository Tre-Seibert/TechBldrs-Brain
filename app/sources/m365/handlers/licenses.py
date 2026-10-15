"""m365_license_summary: a client's licenses by friendly name (bought, assigned, free) and disabled accounts still
holding one, or every client's free seats of one license."""

from __future__ import annotations

from pydantic import BaseModel

from app.sources.clients import ClientResolver, ResolvedClient
from app.sources.m365.handlers.common import ALL_CLIENTS_NOTE, each_tenant, licenses_of, plural, skipped_lines
from app.sources.m365.protocol import M365Source
from app.sources.m365.schemas import SubscribedSku
from app.sources.m365.skus import FREE_SKUS, sku_matches, sku_name
from app.sources.m365.tools import M365_LICENSE_SUMMARY
from app.tools.base import ToolResult


class LicenseArgs(BaseModel):
    client_code: str | None = None
    sku: str | None = None
    unused_only: bool = False


def _free(sku: SubscribedSku) -> int:
    return max(sku.prepaid_units.enabled - sku.consumed_units, 0)


def _chosen(skus: list[SubscribedSku], args: LicenseArgs) -> list[SubscribedSku]:
    rows = [sku for sku in skus if sku_matches(sku.sku_part_number, args.sku)]
    if args.unused_only:
        rows = [sku for sku in rows if _free(sku) > 0 and sku.sku_part_number.upper() not in FREE_SKUS]
    return sorted(rows, key=lambda sku: (sku.sku_part_number.upper() in FREE_SKUS, sku_name(sku.sku_part_number)))


def _line(sku: SubscribedSku) -> str:
    total = sku.prepaid_units.enabled
    line = f"- {sku_name(sku.sku_part_number)}: {sku.consumed_units} of {total} assigned, {_free(sku)} free"
    if sku.sku_part_number.upper() in FREE_SKUS:
        line += " (free or self-service seats)"
    if sku.capability_status and sku.capability_status != "Enabled":
        line += f" (subscription {sku.capability_status})"
    return line


def license_summary(m365: M365Source, client: ResolvedClient, args: LicenseArgs) -> ToolResult:
    tenant = client.m365_tenant_id
    skus = m365.list_subscribed_skus(tenant_id=tenant)
    rows = _chosen(skus, args)
    what = f"{sku_name(rows[0].sku_part_number) if len(rows) == 1 else args.sku}" if args.sku else "Microsoft 365"
    if args.unused_only:
        heading = f"Unused {what} licenses at {client.label}: " + (f"{plural(len(rows), 'license')} with free seats." if rows else "none, every seat bought is assigned.")
    else:
        heading = f"{what} licenses at {client.label}: {plural(len(rows), 'subscription')}." if rows else f"{client.label} has no {what} licenses."
    lines = [heading]
    if rows:
        lines.append("")
        lines.extend(_line(sku) for sku in rows)
    names = {sku.sku_id.lower(): sku_name(sku.sku_part_number) for sku in skus}
    wasted = [
        user for user in m365.list_users(tenant_id=tenant)
        if not user.account_enabled and not user.guest and user.assigned_licenses
        and any(sku_matches(sku.sku_part_number, args.sku) for sku in skus if sku.sku_id.lower() in {l.sku_id.lower() for l in user.assigned_licenses})
    ]
    if wasted:
        lines.extend(["", "Disabled accounts still holding a license:"])
        lines.extend(f"- {user.name} <{user.user_principal_name}> — {', '.join(licenses_of(user, names))}" for user in wasted)
    data = {
        "licenses": [
            {"name": sku_name(sku.sku_part_number), "part_number": sku.sku_part_number, "total": sku.prepaid_units.enabled,
             "assigned": sku.consumed_units, "free": _free(sku)}
            for sku in rows
        ],
        "disabled_but_licensed": [user.user_principal_name for user in wasted],
    }
    return ToolResult(tool=M365_LICENSE_SUMMARY, source=m365.source_name, data=data, client_code=client.client_code, reply="\n".join(lines))


def license_all(m365: M365Source, clients: ClientResolver, args: LicenseArgs) -> ToolResult:
    results = each_tenant(clients, lambda client: m365.list_subscribed_skus(tenant_id=client.m365_tenant_id))
    failed = [(client, error) for client, _, error in results if error is not None]
    checked = [(client, skus) for client, skus, error in results if error is None]
    named = sorted({sku_name(sku.sku_part_number) for _, skus in checked for sku in skus if sku_matches(sku.sku_part_number, args.sku)})
    what = (named[0] if len(named) == 1 else args.sku.replace("_", " ")) if args.sku else "Microsoft 365"
    hits, all_used, lacking = [], [], []
    for client, skus in checked:
        rows = _chosen(skus, args)
        if rows:
            hits.append((client, rows))
        elif any(sku_matches(sku.sku_part_number, args.sku) for sku in skus):
            all_used.append(client)
        else:
            lacking.append(client)
    ranked = sorted(hits, key=lambda pair: (-sum(_free(sku) for sku in pair[1]), pair[0].client_code))
    if args.unused_only:
        heading = f"Clients with unused {what} seats: {len(ranked)} of {plural(len(checked), 'client')} checked."
    else:
        heading = f"{what} licenses across {plural(len(checked), 'client')}:"
    lines = [heading, ""]
    data = []
    for client, rows in ranked:
        parts = [f"{sku_name(sku.sku_part_number)} {_free(sku)} free of {sku.prepaid_units.enabled}" for sku in rows]
        lines.append(f"- {client.label}: {'; '.join(parts)}")
        data.append({"client_code": client.client_code, "licenses": [
            {"name": sku_name(sku.sku_part_number), "total": sku.prepaid_units.enabled, "assigned": sku.consumed_units, "free": _free(sku)}
            for sku in rows
        ]})
    if not ranked:
        lines.append("None.")
    if all_used:
        lines.append(f"- every {what} seat assigned: {', '.join(client.client_code for client in all_used)}")
    if lacking:
        lines.append(f"- no {what} at all: {', '.join(client.client_code for client in lacking)}")
    lines.extend(skipped_lines(failed))
    lines.extend(["", ALL_CLIENTS_NOTE])
    return ToolResult(tool=M365_LICENSE_SUMMARY, source=m365.source_name, data=data, reply="\n".join(lines))
