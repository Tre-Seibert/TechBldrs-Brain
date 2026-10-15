"""m365_security: Secure Score with the top improvement actions, Defender alerts and incidents, risky users, or an
overview of the first two; client_code=all ranks every client's Secure Score."""

from __future__ import annotations

from pydantic import BaseModel

from app.sources.clients import ClientResolver, ResolvedClient
from app.sources.m365.handlers.common import (
    ALL_CLIENTS_NOTE,
    ago,
    capped,
    each_tenant,
    license_message,
    now_for,
    plural,
    skipped_lines,
)
from app.sources.m365.protocol import M365LicenseRequired, M365Source
from app.sources.m365.schemas import SecureScore
from app.sources.m365.tools import M365_SECURITY
from app.tools.base import ToolResult

TOP_ACTIONS = 3
_SEVERITY = {"high": 0, "medium": 1, "low": 2, "informational": 3}


class SecurityArgs(BaseModel):
    client_code: str | None = None
    view: str = "overview"  # overview, secure_score, risky_users, alerts, incidents
    status: str = "open"  # open, all


def _percent(score: SecureScore) -> float:
    return score.current_score / score.max_score * 100 if score.max_score else 0.0


def _score_lines(m365: M365Source, client: ResolvedClient) -> tuple[list[str], dict]:
    tenant = client.m365_tenant_id
    score = m365.secure_score(tenant_id=tenant)
    if score is None:
        return [f"{client.label} has no Secure Score yet (Microsoft calculates it once a day)."], {}
    now = now_for(m365)
    lines = [
        f"Secure Score for {client.label}: {score.current_score:g} of {score.max_score:g} ({_percent(score):.0f}%), "
        f"calculated {ago(score.created_date_time, now)}."
    ]
    current = {control.control_name: control.score for control in score.control_scores}
    gains = [
        (profile.max_score - current.get(profile.id, 0.0), profile)
        for profile in m365.list_secure_score_controls(tenant_id=tenant)
        if not profile.deprecated
    ]
    top = sorted((pair for pair in gains if pair[0] > 0), key=lambda pair: (-pair[0], pair[1].rank or 999))[:TOP_ACTIONS]
    if top:
        lines.append("Top improvement actions:")
        lines.extend(f"{n}. {profile.title} (+{gain:g} points)" for n, (gain, profile) in enumerate(top, start=1))
    data = {
        "score": score.current_score, "max": score.max_score, "percent": round(_percent(score)),
        "top_actions": [{"title": profile.title, "points": gain} for gain, profile in top],
    }
    return lines, data


def _alert_lines(m365: M365Source, client: ResolvedClient, status: str, *, incidents: bool) -> tuple[list[str], int]:
    tenant = client.m365_tenant_id
    now = now_for(m365)
    if incidents:
        rows = m365.list_security_incidents(tenant_id=tenant)
        title, done = "Defender incidents", ("resolved", "redirected")
        items = [(row.severity, row.status, row.display_name, row.created_date_time) for row in rows]
    else:
        rows = m365.list_security_alerts(tenant_id=tenant)
        title, done = "Defender alerts", ("resolved",)
        items = [(row.severity, row.status, row.title, row.created_date_time) for row in rows]
    if status != "all":
        items = [item for item in items if (item[1] or "").lower() not in done]
    items.sort(key=lambda item: (_SEVERITY.get((item[0] or "").lower(), 4), -(item[3].timestamp() if item[3] else 0)))
    lines = [f"{'Open ' if status != 'all' else ''}{title} at {client.label}: {len(items)}."]
    lines.extend(capped([f"- {severity or '?'} · {name} · {state or '?'} · {ago(created, now)}" for severity, state, name, created in items]))
    return lines, len(items)


def security(m365: M365Source, client: ResolvedClient, args: SecurityArgs) -> ToolResult:
    view = (args.view or "overview").lower()
    status = (args.status or "open").lower()
    data: dict = {}
    if view == "secure_score":
        lines, data = _score_lines(m365, client)
    elif view in ("alerts", "incidents"):
        lines, count = _alert_lines(m365, client, status, incidents=view == "incidents")
        data = {"count": count}
    elif view == "risky_users":
        rows = [row for row in m365.list_risky_users(tenant_id=client.m365_tenant_id) if (row.risk_state or "") in ("atRisk", "confirmedCompromised")]
        now = now_for(m365)
        lines = [f"Risky users at {client.label} (Entra ID Protection): {len(rows)} at risk."]
        lines.extend(capped([
            f"- {row.user_display_name or row.user_principal_name} <{row.user_principal_name}> — risk {row.risk_level or '?'}, "
            f"{row.risk_state}, updated {ago(row.risk_last_updated_date_time, now)}"
            for row in rows
        ]))
        data = {"count": len(rows), "users": [row.user_principal_name for row in rows]}
    else:
        lines, data = _score_lines(m365, client)
        try:
            alert_lines, count = _alert_lines(m365, client, "open", incidents=False)
            lines.extend(["", *alert_lines])
            data["open_alerts"] = count
        except M365LicenseRequired as exc:
            lines.extend(["", "Defender alerts: " + license_message(client, exc.feature)])
    return ToolResult(tool=M365_SECURITY, source=m365.source_name, data=data, client_code=client.client_code, reply="\n".join(lines))


def security_all(m365: M365Source, clients: ClientResolver, args: SecurityArgs) -> ToolResult:
    results = each_tenant(clients, lambda client: m365.secure_score(tenant_id=client.m365_tenant_id))
    failed = [(client, error) for client, _, error in results if error is not None]
    scored = [(client, score) for client, score, error in results if error is None and score is not None]
    ranked = sorted(scored, key=lambda pair: (_percent(pair[1]), pair[0].client_code))
    lines = [f"Secure Score across {plural(len(scored), 'client')}, lowest first:", ""]
    lines.extend(f"- {client.label}: {score.current_score:g} of {score.max_score:g} ({_percent(score):.0f}%)" for client, score in ranked)
    lines.extend(skipped_lines(failed))
    lines.extend(["", ALL_CLIENTS_NOTE])
    data = [{"client_code": client.client_code, "score": score.current_score, "max": score.max_score} for client, score in ranked]
    return ToolResult(tool=M365_SECURITY, source=m365.source_name, data=data, reply="\n".join(lines))
