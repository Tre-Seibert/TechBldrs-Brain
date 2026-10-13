"""datto_device_software: one device's installed software, or which of a client's devices have a program.

A client-wide search reads every device's software audit (one API call each), so it is capped and slower.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.sources.clients import ClientResolver
from app.sources.datto.handlers.common import (
    ALL_CLIENTS_NOTE,
    capped,
    devices_in,
    find_device,
    plural,
    refuse,
    resolve_scope,
)
from app.sources.datto.protocol import DattoSource
from app.sources.datto.schemas import Software
from app.sources.datto.tools import DATTO_DEVICE_SOFTWARE
from app.tools.base import ToolResult

MAX_DEVICES_SEARCHED = 150
MAX_PROGRAMS_LISTED = 60


class DeviceSoftwareArgs(BaseModel):
    client_code: str | None = None
    hostname: str | None = None
    q: str | None = None


def _matches(rows: list[Software], q: str) -> list[Software]:
    words = q.lower().split()
    return [row for row in rows if all(word in row.name.lower() for word in words)]


def _program(row: Software) -> str:
    return f"{row.name} {row.version}" if row.version else row.name


def device_software(datto: DattoSource, clients: ClientResolver, args: DeviceSoftwareArgs) -> ToolResult:
    q = (args.q or "").strip()
    if args.hostname:
        return _one_device(datto, args.hostname, q)
    if not args.client_code:
        return refuse(DATTO_DEVICE_SOFTWARE, datto, "Whose software? Name a device hostname, or a client code and a program.")
    if not q:
        return refuse(
            DATTO_DEVICE_SOFTWARE,
            datto,
            "Which program? A client-wide search needs part of its name (e.g. Dentrix), or name one device's hostname.",
        )
    scope = resolve_scope(DATTO_DEVICE_SOFTWARE, datto, clients, args.client_code)
    if isinstance(scope, ToolResult):
        return scope
    rows = devices_in(datto, scope)
    searched = rows[:MAX_DEVICES_SEARCHED]
    found: list[tuple[str, list[Software]]] = []
    for device in searched:
        hits = _matches(datto.list_device_software(device_uid=device.uid), q)
        if hits:
            found.append((device.hostname, hits))
    without = [device.hostname for device in searched if device.hostname not in {host for host, _ in found}]
    lines = [f"{q} is installed on {len(found)} of {plural(len(searched), 'device')} at {scope.label} (Datto RMM software audit)."]
    lines.append("")
    lines.extend(capped([f"- {host}: {'; '.join(_program(row) for row in hits)}" for host, hits in found]))
    if found and without:
        lines.extend(["", "Not found on: " + ", ".join(without[:20]) + (", ..." if len(without) > 20 else "")])
    if len(rows) > len(searched):
        lines.extend(["", f"Searched the first {len(searched)} of {len(rows)} devices."])
    lines.extend(["", ALL_CLIENTS_NOTE if scope.everyone else "This reads each device's software audit, so it is slower."])
    return ToolResult(
        tool=DATTO_DEVICE_SOFTWARE,
        source=datto.source_name,
        data={
            "q": q,
            "found": [{"hostname": host, "software": [_program(row) for row in hits]} for host, hits in found],
            "searched": len(searched),
        },
        client_code=scope.client_code,
        reply="\n".join(lines).rstrip(),
    )


def _one_device(datto: DattoSource, hostname: str, q: str) -> ToolResult:
    lookup = find_device(datto, hostname)
    if lookup.device is None:
        return refuse(DATTO_DEVICE_SOFTWARE, datto, lookup.message)
    device = lookup.device
    rows = sorted(datto.list_device_software(device_uid=device.uid), key=lambda row: row.name.lower())
    hits = _matches(rows, q) if q else rows
    if q:
        heading = (
            f"{device.hostname} has {plural(len(hits), 'match', 'matches')} for {q} in its software audit"
            + (":" if hits else f" ({plural(len(rows), 'program')} installed).")
        )
    else:
        heading = f"{device.hostname} has {plural(len(rows), 'program')} in its software audit:"
    lines = [heading, *capped([f"- {_program(row)}" for row in hits], limit=MAX_PROGRAMS_LISTED, what="more programs")]
    return ToolResult(
        tool=DATTO_DEVICE_SOFTWARE,
        source=datto.source_name,
        data={"hostname": device.hostname, "q": q or None, "software": [_program(row) for row in hits[:MAX_PROGRAMS_LISTED]]},
        row_ids=[device.id] if device.id is not None else [],
        client_code=device.client_code or None,
        reply="\n".join(lines),
    )
