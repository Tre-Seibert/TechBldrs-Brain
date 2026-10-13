"""datto_patch_report: one client's devices by patch status with the worst named, or every client ranked."""

from __future__ import annotations

from collections import Counter

from pydantic import BaseModel

from app.sources.clients import ClientResolver
from app.sources.datto.handlers.common import (
    ALL_CLIENTS_NOTE,
    PATCH_WORDS,
    capped,
    devices_in,
    needs_patches,
    patch_text,
    plural,
    resolve_scope,
)
from app.sources.datto.protocol import DattoSource
from app.sources.datto.schemas import DattoDevice
from app.sources.datto.tools import DATTO_PATCH_REPORT
from app.tools.base import ToolResult

# Worst first: failing installs, then a reboot to finish, then waiting approved patches.
_SEVERITY = {"InstallError": 0, "RebootRequired": 1, "ApprovedPending": 2}
_STATUS_ORDER = ("FullyPatched", "ApprovedPending", "RebootRequired", "InstallError", "NoPolicy", "NoData")
MAX_WORST = 10


class PatchReportArgs(BaseModel):
    client_code: str


def _status(device: DattoDevice) -> str:
    return (device.patch_management.patch_status if device.patch_management else None) or "NoData"


def _pending(device: DattoDevice) -> int:
    return (device.patch_management.patches_approved_pending or 0) if device.patch_management else 0


def _worst(rows: list[DattoDevice]) -> list[DattoDevice]:
    behind = [row for row in rows if needs_patches(row)]
    return sorted(behind, key=lambda row: (_SEVERITY.get(_status(row), 3), -_pending(row), row.hostname))


def _by_status(rows: list[DattoDevice]) -> str:
    counts = Counter(_status(row) for row in rows)
    order = [*_STATUS_ORDER, *sorted(set(counts) - set(_STATUS_ORDER))]
    return ", ".join(f"{PATCH_WORDS.get(name, name)} {counts[name]}" for name in order if counts[name]) or "no devices"


def patch_report(datto: DattoSource, clients: ClientResolver, args: PatchReportArgs) -> ToolResult:
    scope = resolve_scope(DATTO_PATCH_REPORT, datto, clients, args.client_code)
    if isinstance(scope, ToolResult):
        return scope
    if scope.everyone:
        return _ranking(datto, scope)
    rows = devices_in(datto, scope)
    worst = _worst(rows)
    patched = sum(1 for row in rows if _status(row) == "FullyPatched")
    lines = [
        f"Patching at {scope.label} in Datto RMM: {patched} of {plural(len(rows), 'device')} fully patched, "
        f"{len(worst)} behind.",
        f"By status: {_by_status(rows)}.",
    ]
    if worst:
        lines.extend(["", "Behind, worst first:"])
        lines.extend(capped([f"- {row.hostname}: {patch_text(row)}" for row in worst], limit=MAX_WORST))
    return ToolResult(
        tool=DATTO_PATCH_REPORT,
        source=datto.source_name,
        data={
            "devices": len(rows),
            "fully_patched": patched,
            "behind": [
                {"hostname": row.hostname, "patch_status": _status(row), "pending": _pending(row)} for row in worst[:MAX_WORST]
            ],
        },
        row_ids=[row.id for row in worst[:MAX_WORST] if row.id is not None],
        client_code=scope.client_code,
        reply="\n".join(lines),
    )


def _ranking(datto: DattoSource, scope) -> ToolResult:
    """Every client ranked worst first by the share of its devices behind on patches, then by patches pending."""
    by_client: dict[str, list[DattoDevice]] = {}
    for site in scope.sites:
        by_client.setdefault(site.client_code, []).extend(row for row in datto.list_site_devices(site_uid=site.uid) if not row.deleted)
    table = []
    for code, rows in by_client.items():
        behind = _worst(rows)
        share = len(behind) / len(rows) if rows else 0.0
        table.append((code, rows, behind, share, sum(_pending(row) for row in rows)))
    table.sort(key=lambda item: (-item[3], -item[4], item[0]))
    lines = ["Patching by client in Datto RMM, worst first:", ""]
    for code, rows, behind, share, pending in table:
        named = ", ".join(row.hostname for row in behind[:3]) + (", ..." if len(behind) > 3 else "")
        lines.append(
            f"- {code}: {len(behind)} of {plural(len(rows), 'device')} behind ({share:.0%}), "
            f"{plural(pending, 'approved patch', 'approved patches')} pending" + (f" — {named}" if named else "")
        )
    lines = capped(lines)
    lines.extend(["", ALL_CLIENTS_NOTE])
    return ToolResult(
        tool=DATTO_PATCH_REPORT,
        source=datto.source_name,
        data=[
            {"client_code": code, "devices": len(rows), "behind": len(behind), "share_behind": round(share, 3), "pending": pending}
            for code, rows, behind, share, pending in table
        ],
        reply="\n".join(lines),
    )
