"""m365_devices: a client's Intune-managed devices, with compliance, user, OS and last check-in."""

from __future__ import annotations

from pydantic import BaseModel

from app.sources.clients import ResolvedClient
from app.sources.m365.handlers.common import ago, capped, find_user, now_for, plural, refuse
from app.sources.m365.protocol import M365Source
from app.sources.m365.schemas import ManagedDevice
from app.sources.m365.tools import M365_DEVICES
from app.tools.base import ToolResult

_COMPLIANCE = {"compliant": "compliant", "noncompliant": "NOT compliant", "inGracePeriod": "in grace period",
               "conflict": "conflicting policies", "error": "compliance error", "unknown": "compliance unknown"}


class DevicesArgs(BaseModel):
    client_code: str | None = None
    user: str | None = None
    compliant: bool | None = None
    stale_days: int | None = None
    os: str | None = None
    limit: int = 40


def _line(device: ManagedDevice, now) -> str:
    parts = [
        f"{device.operating_system or '?'} {device.os_version or ''}".strip(),
        _COMPLIANCE.get(device.compliance_state or "unknown", device.compliance_state or "?"),
        device.user_display_name or "no user",
        f"last check-in {ago(device.last_sync_date_time, now)}",
        f"{device.managed_device_owner_type or '?'}-owned",
    ]
    return f"- {device.device_name} — " + " · ".join(parts)


def devices(m365: M365Source, client: ResolvedClient, args: DevicesArgs) -> ToolResult:
    tenant = client.m365_tenant_id
    now = now_for(m365)
    every = sorted(m365.list_managed_devices(tenant_id=tenant), key=lambda device: device.device_name.casefold())
    rows, words = every, []
    if (args.user or "").strip():
        lookup = find_user(m365.list_users(tenant_id=tenant), args.user, client)
        if lookup.user is None:
            return refuse(M365_DEVICES, m365, lookup.message, client_code=client.client_code)
        upn = lookup.user.user_principal_name.lower()
        rows = [device for device in rows if (device.user_principal_name or "").lower() == upn]
        words.append(f"enrolled to {lookup.user.name}")
    if args.compliant is not None:
        rows = [device for device in rows if (device.compliance_state == "compliant") == args.compliant]
        words.append("compliant" if args.compliant else "not compliant")
    if args.stale_days:
        days = args.stale_days
        rows = [
            device for device in rows
            if device.last_sync_date_time is None or (now - device.last_sync_date_time).total_seconds() >= days * 86400
        ]
        words.append(f"no check-in for {days}+ days")
    if args.os:
        text = args.os.strip().lower()
        rows = [device for device in rows if text in (device.operating_system or "").lower()]
        words.append(f"on {args.os.strip()}")
    limit = max(1, min(args.limit or 40, 100))
    heading = f"{plural(len(rows), 'Intune device')} at {client.label}"
    if words:
        heading += f" ({'; '.join(words)}; {len(every)} in all)"
    lines = [heading + ".", ""]
    if not rows:
        lines.append("None match.")
    lines.extend(capped([_line(device, now) for device in rows], limit=limit))
    lines.extend(["", "Intune's view: enrollment and compliance. Live online state, patches and antivirus are in Datto RMM."])
    data = {
        "count": len(rows),
        "devices": [
            {"name": device.device_name, "os": device.operating_system, "compliance": device.compliance_state,
             "user": device.user_principal_name, "last_sync": device.last_sync_date_time.isoformat() if device.last_sync_date_time else None}
            for device in rows[:limit]
        ],
    }
    return ToolResult(tool=M365_DEVICES, source=m365.source_name, data=data, client_code=client.client_code, reply="\n".join(lines).rstrip())
