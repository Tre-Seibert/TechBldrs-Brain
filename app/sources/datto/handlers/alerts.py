"""datto_alerts: a client's, every client's, or one device's alerts, listed by device or counted per client."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from pydantic import BaseModel

from app.sources.clients import ClientResolver
from app.sources.datto.handlers.common import (
    ALL_CLIENTS_NOTE,
    Scope,
    ago,
    capped,
    find_device,
    now_for,
    plural,
    refuse,
    resolve_scope,
    wants_all,
)
from app.sources.datto.protocol import DattoSource
from app.sources.datto.schemas import DattoAlert
from app.sources.datto.tools import DATTO_ALERTS
from app.tools.base import ToolResult

PRIORITIES = ("Critical", "High", "Moderate", "Low", "Information", "Unknown")
# alertContext "@class" -> what kind of alert it is, in a word or two.
_KINDS = {
    "online_offline_status_ctx": "offline",
    "perf_disk_usage_ctx": "disk space",
    "antivirus_ctx": "antivirus",
    "patch_ctx": "patching",
    "perf_resource_usage_ctx": "CPU/memory",
    "eventlog_ctx": "event log",
    "srvc_status_ctx": "service",
    "comp_script_ctx": "monitor script",
    "backup_management_ctx": "backup",
    "disk_health_ctx": "disk health",
    "ransomware_ctx": "ransomware",
    "ping_ctx": "ping",
}


class AlertsArgs(BaseModel):
    client_code: str | None = None
    hostname: str | None = None
    status: str | None = None
    days: int | None = None
    priority: str | None = None
    view: str = "list"
    limit: int = 30


def kind(alert: DattoAlert) -> str:
    context = alert.alert_context or {}
    name = str(context.get("@class") or "")
    return _KINDS.get(name, name.removesuffix("_ctx").replace("_", " ") or "monitor")


def _fetch(datto: DattoSource, status: str, *, scope: Scope | None = None, device_uid: str | None = None) -> list[DattoAlert]:
    statuses = ("open", "resolved") if status == "all" else (status,)
    rows: list[DattoAlert] = []
    for each in statuses:
        if device_uid:
            rows += datto.list_device_alerts(device_uid=device_uid, status=each)
        elif scope is not None and scope.everyone:
            rows += datto.list_account_alerts(status=each)
        elif scope is not None:
            rows += [alert for site in scope.sites for alert in datto.list_site_alerts(site_uid=site.uid, status=each)]
    return rows


def _most_urgent_first(alert: DattoAlert) -> tuple[int, float]:
    rank = PRIORITIES.index(alert.priority) if alert.priority in PRIORITIES else len(PRIORITIES)
    return rank, -(alert.timestamp.timestamp() if alert.timestamp else 0.0)


def _line(alert: DattoAlert, now, *, with_host: bool) -> str:
    parts = [alert.priority or "Unknown", kind(alert), f"raised {ago(alert.timestamp, now)}"]
    if alert.resolved:
        parts.append(f"resolved {ago(alert.resolved_on, now)}")
    if alert.ticket_number:
        parts.append(f"ticket {alert.ticket_number}")
    text = (alert.diagnostics or "").strip().replace("\n", " ")
    head = f"- {alert.hostname}: " if with_host else "- "
    return head + " · ".join(parts) + (f" — {text[:140]}" if text else "")


def alerts(datto: DattoSource, clients: ClientResolver, args: AlertsArgs) -> ToolResult:
    status = (args.status or ("all" if args.days else "open")).strip().lower()
    if status not in ("open", "resolved", "all"):
        status = "open"
    view = (args.view or "list").strip().lower()
    now = now_for(datto)
    scope: Scope | None = None
    device_uid = None
    if args.hostname and not (view == "count_by_client" and wants_all(args.client_code)):
        lookup = find_device(datto, args.hostname)
        if lookup.device is None:
            return refuse(DATTO_ALERTS, datto, lookup.message)
        device_uid = lookup.device.uid
        label, client_code = lookup.device.hostname, lookup.device.client_code or None
    elif args.client_code or view == "count_by_client":
        resolved = resolve_scope(DATTO_ALERTS, datto, clients, args.client_code or "all")
        if isinstance(resolved, ToolResult):
            return resolved
        scope = resolved
        label, client_code = scope.label, scope.client_code
    else:
        return refuse(DATTO_ALERTS, datto, "Whose alerts? Name a client code (or all clients) or a device hostname.")
    rows = _fetch(datto, status, scope=scope, device_uid=device_uid)
    words: list[str] = []
    if args.days and args.days > 0:
        since = now - timedelta(days=args.days)
        rows = [alert for alert in rows if alert.timestamp and alert.timestamp >= since]
        words.append(f"last {plural(args.days, 'day')}")
    if args.priority:
        wanted = args.priority.strip().lower()
        rows = [alert for alert in rows if (alert.priority or "").lower() == wanted]
        words.append(wanted)
    rows.sort(key=_most_urgent_first)
    status_word = {"open": "open", "resolved": "resolved", "all": "open and resolved"}[status]
    if view == "count_by_client":
        return _count_by_client(datto, scope, rows, status_word, words)
    heading = f"{plural(len(rows), status_word + ' alert')} for {label} in Datto RMM"
    if words:
        heading += f" ({', '.join(words)})"
    lines = [heading + "."]
    if rows:
        by_priority = Counter(alert.priority or "Unknown" for alert in rows)
        lines.append("By priority: " + ", ".join(f"{name} {by_priority[name]}" for name in PRIORITIES if by_priority[name]) + ".")
    lines.append("")
    if not rows:
        lines.append("None.")
    limit = max(1, min(args.limit or 30, 60))
    lines.extend(capped([_line(alert, now, with_host=device_uid is None) for alert in rows], limit=limit, what="more alerts"))
    if scope is not None and scope.everyone:
        lines.extend(["", ALL_CLIENTS_NOTE])
    data = [
        {
            "hostname": alert.hostname, "client_code": alert.client_code, "priority": alert.priority, "kind": kind(alert),
            "resolved": alert.resolved, "raised": alert.timestamp.isoformat() if alert.timestamp else None,
            "ticket": alert.ticket_number,
        }
        for alert in rows[:limit]
    ]
    return ToolResult(
        tool=DATTO_ALERTS,
        source=datto.source_name,
        data={"count": len(rows), "alerts": data},
        client_code=client_code,
        reply="\n".join(lines).rstrip(),
    )


def _count_by_client(
    datto: DattoSource, scope: Scope | None, rows: list[DattoAlert], status_word: str, words: list[str]
) -> ToolResult:
    """Alert counts per client, most first, every site listed (zero included). Computed here, not by the model."""
    sites = scope.sites if scope is not None else tuple(datto.list_sites())
    counts: dict[str, Counter[str]] = {site.client_code: Counter() for site in sites}
    for alert in rows:
        counts.setdefault(alert.client_code or "?", Counter())[alert.priority or "Unknown"] += 1
    ranked = sorted(counts.items(), key=lambda item: (-sum(item[1].values()), item[0]))
    heading = f"{status_word.capitalize()} alerts by client in Datto RMM"
    if words:
        heading += f" ({', '.join(words)})"
    lines = [heading + ", most first:", ""]
    for code, by_priority in ranked:
        detail = ", ".join(f"{by_priority[name]} {name}" for name in PRIORITIES if by_priority[name])
        lines.append(f"- {code}: {sum(by_priority.values())}" + (f" ({detail})" if detail else ""))
    lines = capped(lines)
    if scope is None or scope.everyone:
        lines.extend(["", ALL_CLIENTS_NOTE])
    return ToolResult(
        tool=DATTO_ALERTS,
        source=datto.source_name,
        data=[{"client_code": code, "count": sum(by_priority.values()), **dict(by_priority)} for code, by_priority in ranked],
        client_code=scope.client_code if scope is not None else None,
        reply="\n".join(lines),
    )
