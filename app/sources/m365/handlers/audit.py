"""m365_audit: a client's Entra directory audit log, summarized by category and by who made the change, newest first."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from pydantic import BaseModel

from app.sources.clients import ResolvedClient
from app.sources.m365.handlers.common import ago, capped, find_user, now_for, plural, refuse
from app.sources.m365.protocol import M365Source
from app.sources.m365.schemas import DirectoryAudit
from app.sources.m365.tools import M365_AUDIT
from app.tools.base import ToolResult

MAX_DAYS = 30
FREE_DAYS = 7  # Entra keeps 7 days of audit log without P1, 30 with it
CATEGORIES = {
    "role_management": "RoleManagement",
    "user_management": "UserManagement",
    "group_management": "GroupManagement",
    "application": "ApplicationManagement",
    "policy": "Policy",
}


class AuditArgs(BaseModel):
    client_code: str | None = None
    category: str = "all"
    days: int = 7
    user: str | None = None


def _line(row: DirectoryAudit, now) -> str:
    targets = ", ".join(target.display_name or target.user_principal_name or "?" for target in row.target_resources) or "?"
    result = "" if (row.result or "success") == "success" else f" ({row.result})"
    return f"- {ago(row.activity_date_time, now)} · {row.activity_display_name}{result} · by {row.initiated_by.name} · on {targets}"


def audit(m365: M365Source, client: ResolvedClient, args: AuditArgs) -> ToolResult:
    now = now_for(m365)
    days = min(max(args.days or 7, 1), MAX_DAYS)
    category = CATEGORIES.get((args.category or "all").lower())
    rows = m365.list_directory_audits(tenant_id=client.m365_tenant_id, since=now - timedelta(days=days))
    words = []
    if category:
        rows = [row for row in rows if row.category == category]
        words.append(category)
    if (args.user or "").strip():
        lookup = find_user(m365.list_users(tenant_id=client.m365_tenant_id), args.user, client)
        if lookup.user is None:
            return refuse(M365_AUDIT, m365, lookup.message, client_code=client.client_code)
        upn = lookup.user.user_principal_name.lower()
        rows = [
            row for row in rows
            if (row.initiated_by.user and (row.initiated_by.user.user_principal_name or "").lower() == upn)
            or any((target.user_principal_name or "").lower() == upn for target in row.target_resources)
        ]
        words.append(f"by or to {lookup.user.name}")
    rows = sorted(rows, key=lambda row: row.activity_date_time or now, reverse=True)
    heading = f"Directory audit log for {client.label}, last {plural(days, 'day')}"
    lines = [heading + (f" ({'; '.join(words)})" if words else "") + f": {plural(len(rows), 'change')}."]
    if rows:
        by_category = Counter(row.category or "Other" for row in rows)
        by_actor = Counter(row.initiated_by.name for row in rows)
        lines.append("By category: " + ", ".join(f"{name} {count}" for name, count in by_category.most_common()))
        lines.append("By who: " + ", ".join(f"{name} {count}" for name, count in by_actor.most_common()))
        lines.extend(["", "Newest first:"])
        lines.extend(capped([_line(row, now) for row in rows]))
    if days > FREE_DAYS:
        lines.extend(["", f"(Entra keeps {FREE_DAYS} days of audit log without Entra ID P1 and 30 days with it.)"])
    data = [
        {"when": row.activity_date_time.isoformat() if row.activity_date_time else None, "activity": row.activity_display_name,
         "category": row.category, "by": row.initiated_by.name}
        for row in rows[:40]
    ]
    return ToolResult(tool=M365_AUDIT, source=m365.source_name, data=data, client_code=client.client_code, reply="\n".join(lines))
