"""m365_tenant: a client's tenant overview, domains, app secrets and certificates expiring soon, and Microsoft 365
service health (the same service for every tenant, so any one with consent answers it)."""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel

from app.sources.clients import ClientResolver, ResolvedClient
from app.sources.m365.handlers.common import ERRORS, ago, capped, error_message, now_for, plural, refuse
from app.sources.m365.protocol import M365Source
from app.sources.m365.skus import FREE_SKUS, sku_name
from app.sources.m365.tools import M365_TENANT
from app.tools.base import ToolResult

_HEALTH = {
    "serviceOperational": "operational",
    "serviceDegradation": "degraded",
    "serviceInterruption": "interrupted",
    "restoringService": "restoring",
    "extendedRecovery": "extended recovery",
    "investigating": "investigating",
    "falsePositive": "false positive",
    "serviceRestored": "restored",
    "investigationSuspended": "investigation suspended",
}


class TenantArgs(BaseModel):
    client_code: str | None = None
    view: str = "overview"  # overview, domains, expiring_app_credentials, service_health
    days: int = 30


def tenant(m365: M365Source, client: ResolvedClient, args: TenantArgs) -> ToolResult:
    view = (args.view or "overview").lower()
    if view == "domains":
        return _domains(m365, client)
    if view == "expiring_app_credentials":
        return _expiring(m365, client, args)
    if view == "service_health":
        return _health(m365, client, note="")
    return _overview(m365, client)


def _overview(m365: M365Source, client: ResolvedClient) -> ToolResult:
    tenant_id = client.m365_tenant_id
    org = m365.organization(tenant_id=tenant_id)
    users = m365.list_users(tenant_id=tenant_id)
    skus = [sku for sku in m365.list_subscribed_skus(tenant_id=tenant_id) if sku.sku_part_number.upper() not in FREE_SKUS]
    default = next((domain.name for domain in org.verified_domains if domain.is_default), "?")
    created = org.created_date_time.date().isoformat() if org.created_date_time else "?"
    enabled = sum(1 for user in users if user.account_enabled and not user.guest)
    lines = [
        f"{client.label}'s Microsoft 365 tenant: {org.display_name or '?'}, default domain {default}, created {created}.",
        f"- Accounts: {len(users)} ({enabled} enabled members, {plural(sum(1 for user in users if user.guest), 'guest')})",
        "- Licenses: " + (", ".join(f"{sku_name(sku.sku_part_number)} {sku.consumed_units}/{sku.prepaid_units.enabled}" for sku in skus) or "none"),
        f"- Directory sync from on-premises AD: {'on' if org.on_premises_sync_enabled else 'off'}",
        f"- Verified domains: {plural(len(org.verified_domains), 'domain')}",
    ]
    data = {"name": org.display_name, "default_domain": default, "accounts": len(users), "directory_sync": bool(org.on_premises_sync_enabled)}
    return ToolResult(tool=M365_TENANT, source=m365.source_name, data=data, client_code=client.client_code, reply="\n".join(lines))


def _domains(m365: M365Source, client: ResolvedClient) -> ToolResult:
    rows = sorted(m365.list_domains(tenant_id=client.m365_tenant_id), key=lambda d: (not d.is_default, d.is_initial, d.id))
    unverified = [domain.id for domain in rows if not domain.is_verified]
    lines = [
        f"Domains in {client.label}'s Microsoft 365 tenant: {len(rows)}"
        + (f", {len(unverified)} not verified ({', '.join(unverified)})." if unverified else ", all verified.")
    ]
    for domain in rows:
        tags = ["verified" if domain.is_verified else "NOT verified"]
        if domain.is_default:
            tags.append("default")
        if domain.is_initial:
            tags.append("initial onmicrosoft.com domain")
        if (domain.authentication_type or "").lower() == "federated":
            tags.append("federated")
        lines.append(f"- {domain.id} — {', '.join(tags)}")
    data = [{"domain": domain.id, "verified": domain.is_verified, "default": domain.is_default} for domain in rows]
    return ToolResult(tool=M365_TENANT, source=m365.source_name, data=data, client_code=client.client_code, reply="\n".join(lines))


def _expiring(m365: M365Source, client: ResolvedClient, args: TenantArgs) -> ToolResult:
    now = now_for(m365)
    days = min(max(args.days or 30, 1), 365)
    until = now + timedelta(days=days)
    found = []
    for app in m365.list_applications(tenant_id=client.m365_tenant_id):
        for kind, credentials in (("secret", app.password_credentials), ("certificate", app.key_credentials)):
            for credential in credentials:
                if credential.end_date_time and credential.end_date_time <= until:
                    found.append((credential.end_date_time, app.display_name, kind, credential.display_name))
    found.sort()
    expired = [item for item in found if item[0] <= now]
    lines = [
        f"App secrets and certificates at {client.label} ending within {days} days: {len(found) - len(expired)}"
        + (f"; already expired: {len(expired)}." if expired else ".")
    ]
    lines.extend(capped([
        f"- {name} — {kind}{f' {label!r}' if label else ''} {'expired' if end <= now else 'ends'} {ago(end, now)}"
        for end, name, kind, label in found
    ]))
    data = [{"app": name, "kind": kind, "ends": end.isoformat()} for end, name, kind, _ in found]
    return ToolResult(tool=M365_TENANT, source=m365.source_name, data=data, client_code=client.client_code, reply="\n".join(lines))


def _health(m365: M365Source, client: ResolvedClient, *, note: str) -> ToolResult:
    tenant_id = client.m365_tenant_id
    now = now_for(m365)
    services = m365.list_service_health(tenant_id=tenant_id)
    issues = sorted(m365.list_service_issues(tenant_id=tenant_id), key=lambda issue: issue.start_date_time or now, reverse=True)
    bad = [service for service in services if service.status != "serviceOperational"]
    lines = [
        "Microsoft 365 service health"
        + (f" (checked through {client.label}'s tenant; it is the same service for every tenant)" if note else f" for {client.label}")
        + ": " + ("everything operational." if not bad else f"{plural(len(bad), 'service')} not fully working.")
    ]
    lines.extend(f"- {service.service}: {_HEALTH.get(service.status, service.status)}" for service in bad)
    incidents = [issue for issue in issues if not issue.is_resolved]
    if incidents:
        lines.extend(["", "Open issues:"])
        lines.extend(capped([
            f"- {issue.id} ({issue.service}, {issue.classification or 'issue'}): {issue.title} — since {ago(issue.start_date_time, now)}"
            for issue in incidents
        ], limit=10))
    lines.append(f"{len(services) - len(bad)} other services operational.")
    data = {"degraded": [service.service for service in bad], "open_issues": [issue.id for issue in incidents]}
    return ToolResult(tool=M365_TENANT, source=m365.source_name, data=data, client_code=client.client_code, reply="\n".join(lines))


def service_health_any(m365: M365Source, clients: ClientResolver) -> ToolResult:
    """Service health with no client named: the first client whose tenant answers."""
    reasons = []
    for client in clients.every(require=("m365_tenant_id",)):
        try:
            return _health(m365, client, note="any")
        except ERRORS as exc:
            reasons.append(error_message(client, exc))
    message = "No client's Microsoft 365 tenant could be checked for service health."
    if reasons:
        message += " " + reasons[-1]
    return refuse(M365_TENANT, m365, message)
