"""Flow client tools: get_client_detail and list_machines."""

from __future__ import annotations

from pydantic import BaseModel

from app.sources.flow.handlers.payloads import _client_detail_payload, _machine_payload
from app.sources.flow.protocol import FlowRequestError, FlowSource
from app.sources.flow.tools import GET_CLIENT_DETAIL, LIST_MACHINES
from app.tools.base import ToolResult, _refuse


class ListMachinesArgs(BaseModel):
    client_code: str
    limit: int = 100


class GetClientDetailArgs(BaseModel):
    client_code: str


def list_machines(source: FlowSource, args: ListMachinesArgs) -> ToolResult:
    client = args.client_code.strip().upper()
    rows = source.list_machines(client_code=client, limit=args.limit)
    data = [_machine_payload(r) for r in rows]
    lines = [f"{len(data)} machine(s) for {client}.", ""]
    if not data:
        lines.append("None found.")
    for row in data:
        seen = row.get("last_seen_at") or "never"
        lines.append(f"- {row['machine_name']} ({row.get('machine_support') or 'support unknown'}, last seen {seen})")
    return ToolResult(
        tool=LIST_MACHINES,
        source=source.source_name,
        data=data,
        row_ids=[r.id for r in rows],
        client_code=client,
        reply="\n".join(lines).rstrip(),
    )


def get_client_detail(source: FlowSource, args: GetClientDetailArgs) -> ToolResult:
    try:
        row = source.get_client(client_code=args.client_code)
    except FlowRequestError as exc:
        return _refuse(GET_CLIENT_DETAIL, source, str(exc))
    data = _client_detail_payload(row)
    lines = [f"{row.client_code} — {row.name or row.full_company_name or ''}".rstrip(" —")]
    if row.contract_minutes is not None:
        lines.append(f"Contract minutes: {row.contract_minutes}")
    if row.balance is not None:
        lines.append(f"Balance: {row.balance}")
    if row.account:
        lines.append(f"Account: {row.account}")
    for label, value in (
        ("Support renewal", row.support_renewal),
        ("Antivirus renewal", row.antivirus_renewal),
        ("Spam filter renewal", row.spam_filter_renewal),
    ):
        if value:
            lines.append(f"{label}: {value}")
    if not (row.support_renewal or row.antivirus_renewal or row.spam_filter_renewal):
        lines.append("Renewal dates: none recorded in Flow.")
    return ToolResult(
        tool=GET_CLIENT_DETAIL,
        source=source.source_name,
        data=data,
        row_ids=[],
        client_code=row.client_code.upper(),
        reply="\n".join(lines).rstrip(),
    )
