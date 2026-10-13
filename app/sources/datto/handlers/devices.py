"""datto_list_devices, datto_get_device and datto_site_summary."""

from __future__ import annotations

from collections import Counter
from typing import Any

from pydantic import BaseModel, field_validator

from app.sources.clients import ClientResolver
from app.sources.datto.handlers.common import (
    ALL_CLIENTS_NOTE,
    WORKSTATION_CATEGORIES,
    Scope,
    ago,
    av_issue,
    av_text,
    capped,
    days_since,
    device_data,
    device_line,
    devices_in,
    find_device,
    needs_patches,
    now_for,
    patch_text,
    plural,
    refuse,
    resolve_scope,
    short_os,
)
from app.sources.datto.protocol import DattoSource
from app.sources.datto.schemas import DattoDevice
from app.sources.datto.tools import DATTO_GET_DEVICE, DATTO_LIST_DEVICES, DATTO_SITE_SUMMARY
from app.tools.base import ToolResult

GB = 1024**3


class ListDevicesArgs(BaseModel):
    # Optional only with last_user ("What computer does Debe use?"): then every client's devices are searched.
    client_code: str | None = None
    online: bool | None = None
    offline_days: int | None = None
    device_type: str | None = None
    os: str | None = None
    needs_patches: bool | None = None
    av_issue: bool | None = None
    reboot_required: bool | None = None
    last_user: str | None = None
    # A hostname sent here too means that one device (datto_get_device is the tool for it).
    hostname: str | None = None
    limit: int = 40


class GetDeviceArgs(BaseModel):
    hostname: str = ""  # missing: find_device asks which device
    include: list[str] = []

    @field_validator("include", mode="before")
    @classmethod
    def _include(cls, value: Any) -> list[str]:
        if value in (None, ""):
            return []
        items = value.split(",") if isinstance(value, str) else list(value)
        return [str(item).strip().lower() for item in items if str(item).strip()]


class SiteSummaryArgs(BaseModel):
    client_code: str | None = None
    # With no client code, the site of the client this device belongs to.
    hostname: str | None = None


def _type_matches(device: DattoDevice, wanted: str) -> bool:
    category = device.category.lower()
    if wanted == "workstation":
        return category in WORKSTATION_CATEGORIES
    return category == wanted or category.rstrip("s") == wanted.rstrip("s")


def _filtered(rows: list[DattoDevice], args: ListDevicesArgs, now) -> tuple[list[DattoDevice], list[str]]:
    """The devices that pass every filter, and the filters in words for the heading."""
    words: list[str] = []
    if args.online is not None:
        rows = [row for row in rows if bool(row.online) == args.online]
        words.append("online" if args.online else "offline")
    if args.offline_days is not None and args.offline_days > 0:
        days = args.offline_days
        rows = [row for row in rows if not row.online and (days_since(row.last_seen, now) or float("inf")) >= days]
        words.append(f"not seen for {days}+ days")
    if args.device_type:
        wanted = args.device_type.strip().lower()
        rows = [row for row in rows if _type_matches(row, wanted)]
        words.append(wanted + "s" if not wanted.endswith("s") else wanted)
    if args.os:
        text = args.os.strip().lower()
        rows = [row for row in rows if text in (row.operating_system or "").lower()]
        words.append(f"on {args.os.strip()}")
    if args.needs_patches is not None:
        rows = [row for row in rows if needs_patches(row) == args.needs_patches]
        words.append("missing patches" if args.needs_patches else "not missing patches")
    if args.av_issue is not None:
        rows = [row for row in rows if av_issue(row) == args.av_issue]
        words.append("with antivirus problems" if args.av_issue else "with healthy antivirus")
    if args.reboot_required is not None:
        rows = [row for row in rows if bool(row.reboot_required) == args.reboot_required]
        words.append("needing a reboot" if args.reboot_required else "not needing a reboot")
    if args.last_user:
        parts = [part for part in args.last_user.lower().replace(".", " ").split() if part]
        rows = [row for row in rows if all(part in (row.last_logged_in_user or "").lower() for part in parts)]
        words.append(f"last used by {args.last_user.strip()}")
    return rows, words


def list_devices(datto: DattoSource, clients: ClientResolver, args: ListDevicesArgs) -> ToolResult:
    if (args.hostname or "").strip():
        return _one_listed(datto, args.hostname)
    if not (args.client_code or "").strip() and not (args.last_user or "").strip():
        return refuse(DATTO_LIST_DEVICES, datto, "Which client's devices? Give the client code or name, or 'all'.")
    scope = resolve_scope(DATTO_LIST_DEVICES, datto, clients, args.client_code or "all")
    if isinstance(scope, ToolResult):
        return scope
    now = now_for(datto)
    every = devices_in(datto, scope)
    rows, words = _filtered(every, args, now)
    limit = max(1, min(args.limit or 40, 100))
    heading = f"{plural(len(rows), 'device')} at {scope.label} in Datto RMM"
    if words:
        heading += f" ({', '.join(words)}; {len(every)} in all)"
    lines = [heading + ".", ""]
    if not rows:
        lines.append("None match.")
    lines.extend(capped([device_line(row, now) for row in rows], limit=limit))
    if scope.everyone:
        lines.extend(["", ALL_CLIENTS_NOTE])
    return ToolResult(
        tool=DATTO_LIST_DEVICES,
        source=datto.source_name,
        data={"count": len(rows), "devices": [device_data(row) for row in rows[:limit]]},
        row_ids=[row.id for row in rows[:limit] if row.id is not None],
        client_code=scope.client_code,
        reply="\n".join(lines).rstrip(),
    )


def _one_listed(datto: DattoSource, hostname: str) -> ToolResult:
    """A hostname sent to datto_list_devices: that device's line, as a list shows it."""
    lookup = find_device(datto, hostname)
    if lookup.device is None:
        return refuse(DATTO_LIST_DEVICES, datto, lookup.message)
    device = lookup.device
    lines = [
        f"{device.hostname} in Datto RMM (site {device.site_name or device.client_code or '?'}).",
        "",
        device_line(device, now_for(datto)),
    ]
    return ToolResult(
        tool=DATTO_LIST_DEVICES,
        source=datto.source_name,
        data={"count": 1, "devices": [device_data(device)]},
        row_ids=[device.id] if device.id is not None else [],
        client_code=device.client_code or None,
        reply="\n".join(lines),
    )


def _size(value: int | None) -> str:
    if value is None:
        return "?"
    gb = value / GB
    return f"{gb:,.0f} GB" if gb >= 10 else f"{gb:.1f} GB"


def get_device(datto: DattoSource, args: GetDeviceArgs) -> ToolResult:
    lookup = find_device(datto, args.hostname)
    if lookup.device is None:
        return refuse(DATTO_GET_DEVICE, datto, lookup.message)
    device = lookup.device
    now = now_for(datto)
    state = f"online (last seen {ago(device.last_seen, now)})" if device.online else f"OFFLINE, last seen {ago(device.last_seen, now)}"
    installed = device.patch_management.patches_installed if device.patch_management else None
    lines = [
        f"{device.hostname} — {device.category}, {short_os(device.operating_system)}",
        f"Site: {device.site_name or '?'}",
        f"Status: {state}",
        f"Last user: {device.last_logged_in_user or 'none recorded'}",
        f"Last reboot: {ago(device.last_reboot, now)}" + ("; a reboot is needed" if device.reboot_required else ""),
        f"IP: {device.int_ip_address or '?'}" + (f" (public {device.ext_ip_address})" if device.ext_ip_address else ""),
        f"Patches: {patch_text(device)}"
        + (f" ({installed} installed)" if installed else ""),
        f"Antivirus: {av_text(device)}",
        f"Warranty: {device.warranty_date or 'not recorded in Datto'}",
    ]
    open_alerts = datto.list_device_alerts(device_uid=device.uid, status="open")
    if open_alerts:
        newest = max(open_alerts, key=lambda alert: alert.timestamp or now)
        lines.append(
            f"Open alerts: {len(open_alerts)} (newest: {newest.priority or 'Unknown'}, {(newest.diagnostics or '').strip()[:120]})"
        )
    else:
        lines.append("Open alerts: none")
    data = {**device_data(device), "open_alerts": len(open_alerts), "warranty_date": device.warranty_date}
    if "hardware" in args.include or "disks" in args.include:
        audit = datto.get_device_audit(device_uid=device.uid)
        if "hardware" in args.include:
            info, bios = audit.system_info, audit.bios
            ram = f"{info.total_physical_memory / GB:.0f} GB RAM" if info and info.total_physical_memory else None
            cpu = audit.processors[0].name if audit.processors else None
            parts = [
                " ".join(part for part in (info.manufacturer if info else None, info.model if info else None) if part) or None,
                f"serial {bios.serial_number}" if bios and bios.serial_number else None,
                ram,
                cpu,
            ]
            lines.append("Hardware: " + (", ".join(part for part in parts if part) or "no hardware audit"))
            data["serial_number"] = bios.serial_number if bios else None
        if "disks" in args.include:
            disks = [
                f"{disk.disk_identifier or disk.description or '?'} {_size(disk.freespace)} free of {_size(disk.size)}"
                + (f" ({disk.freespace / disk.size:.0%} free)" if disk.size and disk.freespace is not None else "")
                for disk in audit.logical_disks
            ]
            lines.append("Disks: " + ("; ".join(disks) if disks else "no disk audit"))
            data["disks"] = [
                {
                    "disk": disk.disk_identifier,
                    "free_gb": round((disk.freespace or 0) / GB, 1),
                    "size_gb": round((disk.size or 0) / GB, 1),
                }
                for disk in audit.logical_disks
            ]
        lines.append(f"(Audit from {ago(device.last_audit_date, now)}.)")
    return ToolResult(
        tool=DATTO_GET_DEVICE,
        source=datto.source_name,
        data=data,
        row_ids=[device.id] if device.id is not None else [],
        client_code=device.client_code or None,
        reply="\n".join(lines),
    )


def _counts(counter: Counter[str]) -> str:
    return ", ".join(f"{name} {count}" for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))) or "none"


def site_summary(datto: DattoSource, clients: ClientResolver, args: SiteSummaryArgs) -> ToolResult:
    client_code = (args.client_code or "").strip()
    if not client_code and (args.hostname or "").strip():
        lookup = find_device(datto, args.hostname)
        if lookup.device is None:
            return refuse(DATTO_SITE_SUMMARY, datto, lookup.message)
        client_code = lookup.device.client_code
    if not client_code:
        return refuse(DATTO_SITE_SUMMARY, datto, "Which client's site? Give the client code or name, or 'all'.")
    scope = resolve_scope(DATTO_SITE_SUMMARY, datto, clients, client_code)
    if isinstance(scope, ToolResult):
        return scope
    if scope.everyone:
        return _all_sites_summary(datto, scope)
    rows = devices_in(datto, scope)
    types = Counter(row.category for row in rows)
    workstations = sum(1 for row in rows if row.category.lower() in WORKSTATION_CATEGORIES)
    servers = sum(1 for row in rows if row.category.lower() == "server")
    other = len(rows) - workstations - servers
    online = sum(1 for row in rows if row.online)
    offline = [row.hostname for row in rows if not row.online]
    patching = [row.hostname for row in rows if needs_patches(row)]
    av = [row.hostname for row in rows if av_issue(row)]
    reboot = [row.hostname for row in rows if row.reboot_required]
    alerts = [alert for site in scope.sites for alert in datto.list_site_alerts(site_uid=site.uid, status="open")]
    sites = ", ".join(site.name for site in scope.sites)
    lines = [
        f"{scope.label} in Datto RMM ({'site' if len(scope.sites) == 1 else 'sites'}: {sites}): {plural(len(rows), 'device')}.",
        f"- Workstations: {workstations}; servers: {servers}" + (f"; other: {other}" if other else ""),
        f"- By type: {_counts(types)}",
        f"- Online: {online}; offline: {len(offline)}" + (f" ({', '.join(offline)})" if offline else ""),
        f"- Operating systems: {_counts(Counter(short_os(row.operating_system) for row in rows))}",
        f"- Missing patches: {len(patching)}" + (f" ({', '.join(patching)})" if patching else ""),
        f"- Antivirus problems: {len(av)}" + (f" ({', '.join(av)})" if av else ""),
        f"- Reboot needed: {len(reboot)}" + (f" ({', '.join(reboot)})" if reboot else ""),
        f"- Open alerts: {len(alerts)}",
    ]
    data = {
        "devices": len(rows), "workstations": workstations, "servers": servers, "by_type": dict(types),
        "online": online, "offline": len(offline), "missing_patches": len(patching), "av_issues": len(av),
        "reboot_required": len(reboot), "open_alerts": len(alerts),
    }
    return ToolResult(
        tool=DATTO_SITE_SUMMARY,
        source=datto.source_name,
        data=data,
        client_code=scope.client_code,
        reply="\n".join(lines),
    )


def _all_sites_summary(datto: DattoSource, scope: Scope) -> ToolResult:
    lines = [f"Datto RMM, {plural(len(scope.sites), 'site')}:", ""]
    data: list[dict[str, Any]] = []
    for site in scope.sites:
        rows = [row for row in datto.list_site_devices(site_uid=site.uid) if not row.deleted]
        online = sum(1 for row in rows if row.online)
        servers = sum(1 for row in rows if row.category.lower() == "server")
        lines.append(
            f"- {site.name}: {plural(len(rows), 'device')} ({len(rows) - servers} workstations/other, "
            f"{plural(servers, 'server')}), {online} online, {len(rows) - online} offline"
        )
        data.append({"site": site.name, "client_code": site.client_code, "devices": len(rows), "servers": servers, "online": online})
    lines = capped(lines)
    lines.extend(["", ALL_CLIENTS_NOTE])
    return ToolResult(tool=DATTO_SITE_SUMMARY, source=datto.source_name, data=data, reply="\n".join(lines))
