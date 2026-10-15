"""m365_signins: a tenant's sign-in logs (Entra ID P1) summarized by user, failure reason and country, one user's
recent sign-ins, or just the latest one."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta

from pydantic import BaseModel

from app.sources.clients import ResolvedClient
from app.sources.m365.handlers.common import (
    COUNTRY_ALIASES,
    SIGN_IN_ERRORS,
    ago,
    capped,
    find_user,
    now_for,
    plural,
    refuse,
)
from app.sources.m365.handlers.users import users_with_activity
from app.sources.m365.protocol import M365Source
from app.sources.m365.schemas import SignIn
from app.sources.m365.tools import M365_SIGNINS
from app.tools.base import ToolResult

MAX_DAYS = 30
DEFAULT_DAYS = 7
MAX_USER_ROWS = 15
# Graph returns at most this many rows here (app/sources/m365/http.py MAX_SIGN_INS); the reply says when it was hit.
ROW_CAP = 5000


class SignInArgs(BaseModel):
    client_code: str | None = None
    user: str | None = None
    status: str = "all"  # failure, success, all
    hours: int | None = None
    days: int | None = None
    outside_country: str | None = None
    limit: int | None = None


def reason(row: SignIn) -> str:
    code = row.status.error_code
    words = SIGN_IN_ERRORS.get(code) or (row.status.failure_reason or "failed").rstrip(".")
    return f"{words} ({code})"


def place(row: SignIn) -> str:
    loc = row.location
    parts = [part for part in ((loc.city, loc.state, loc.country_or_region) if loc else ()) if part]
    return ", ".join(parts) or "unknown location"


def _window(args: SignInArgs) -> tuple[timedelta, str]:
    if args.hours and args.hours > 0:
        hours = min(args.hours, MAX_DAYS * 24)
        return timedelta(hours=hours), f"the last {plural(hours, 'hour')}"
    days = min(max(args.days or DEFAULT_DAYS, 1), MAX_DAYS)
    return timedelta(days=days), "the last day" if days == 1 else f"the last {days} days"


def _country(text: str | None) -> str | None:
    value = (text or "").strip().upper().replace(".", "")
    return COUNTRY_ALIASES.get(value, value) or None


def signins(m365: M365Source, client: ResolvedClient, args: SignInArgs) -> ToolResult:
    now = now_for(m365)
    window, window_words = _window(args)
    who, upn, person = f"at {client.label}", None, None
    if (args.user or "").strip():
        users, _ = users_with_activity(m365, client)
        lookup = find_user(users, args.user, client)
        if lookup.user is None:
            return refuse(M365_SIGNINS, m365, lookup.message, client_code=client.client_code)
        person = lookup.user
        upn = person.user_principal_name
        who = f"for {person.name} <{upn}>"
    every = m365.list_sign_ins(tenant_id=client.m365_tenant_id, since=now - window, user_principal_name=upn)
    rows, words = every, []
    status = (args.status or "all").lower()
    if status.startswith("fail"):
        rows = [row for row in rows if not row.ok]
        words.append("failed")
    elif status.startswith("succ"):
        rows = [row for row in rows if row.ok]
        words.append("successful")
    home = _country(args.outside_country)
    if home:
        rows = [row for row in rows if row.country and row.country != home]
        who += f" from outside {home}"
    rows = sorted(rows, key=lambda row: row.created_date_time or now, reverse=True)
    what = " ".join([*words, "sign-ins"]).capitalize()
    data = {
        "count": len(rows),
        "failed": sum(1 for row in rows if not row.ok),
        "succeeded": sum(1 for row in rows if row.ok),
    }
    if args.limit and args.limit <= 5:
        return _latest(m365, client, rows[: args.limit], who, window_words, person, now, data)
    lines = [f"{what} {who} in {window_words}: {len(rows)} ({data['succeeded']} succeeded, {data['failed']} failed)."]
    if len(every) >= ROW_CAP:
        lines.append(f"(Only the newest {ROW_CAP:,} sign-ins in that window were read.)")
    if not rows:
        if person is not None:
            last = person.sign_in_activity.last if person.sign_in_activity else None
            lines.append(f"The last sign-in on record for {person.name} was {ago(last, now)}.")
        return ToolResult(tool=M365_SIGNINS, source=m365.source_name, data=data, client_code=client.client_code, reply="\n".join(lines))
    if person is not None:
        lines.extend(["", "Newest first:"])
        lines.extend(capped([_row_line(row, now) for row in rows], limit=MAX_USER_ROWS))
    else:
        failed = [row for row in rows if not row.ok]
        if failed:
            lines.extend(["", "Failed, by user and reason:"])
            lines.extend(capped(_by_user(failed, now)))
        countries = Counter(row.country or "unknown" for row in rows)
        lines.extend(["", "Countries: " + ", ".join(f"{name} {count}" for name, count in countries.most_common())])
        base = home or "US"
        abroad = [row for row in rows if row.ok and row.country and row.country != base]
        if abroad and not home:
            lines.append(f"Successful sign-ins from outside {base}:")
            lines.extend(capped([f"- {row.user_display_name or row.user_principal_name} from {place(row)}, {ago(row.created_date_time, now)}" for row in abroad]))
        elif home:
            lines.extend(["", "Each:"])
            lines.extend(capped([
                f"- {row.user_display_name or row.user_principal_name} — {'succeeded' if row.ok else 'failed: ' + reason(row)} · {place(row)} · {ago(row.created_date_time, now)}"
                for row in rows
            ]))
    data["countries"] = dict(Counter(row.country or "unknown" for row in rows))
    return ToolResult(tool=M365_SIGNINS, source=m365.source_name, data=data, client_code=client.client_code, reply="\n".join(lines))


def _row_line(row: SignIn, now) -> str:
    stamp = row.created_date_time.strftime("%Y-%m-%d %H:%M UTC") if row.created_date_time else "?"
    outcome = "succeeded" if row.ok else f"failed: {reason(row)}"
    policies = [policy.display_name for policy in row.applied_conditional_access_policies if policy.result == "failure" and policy.display_name]
    if policies:
        outcome += f" (policy {', '.join(policies)})"
    return f"- {ago(row.created_date_time, now)} ({stamp}): {outcome} · {place(row)} · {row.app_display_name or '?'} · {row.client_app_used or '?'}"


def _by_user(rows: list[SignIn], now) -> list[str]:
    grouped: dict[str, list[SignIn]] = defaultdict(list)
    for row in rows:
        grouped[row.user_principal_name.lower()].append(row)
    lines = []
    for _, mine in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        reasons = Counter(reason(row) for row in mine)
        policies = sorted({p.display_name for row in mine for p in row.applied_conditional_access_policies if p.result == "failure" and p.display_name})
        countries = sorted({row.country for row in mine if row.country})
        newest = max(row.created_date_time for row in mine if row.created_date_time)
        parts = ", ".join(f"{count}× {text}" for text, count in reasons.most_common())
        if policies:
            parts += f" (policy {', '.join(policies)})"
        first = mine[0]
        lines.append(
            f"- {first.user_display_name or first.user_principal_name} <{first.user_principal_name}>: {parts} · from "
            f"{', '.join(countries) or 'unknown'} · latest {ago(newest, now)}"
        )
    return lines


def _latest(m365, client, rows, who, window_words, person, now, data) -> ToolResult:
    if not rows:
        message = f"No sign-ins {who} in {window_words}."
        if person is not None:
            last = person.sign_in_activity.last if person.sign_in_activity else None
            message += f" The last sign-in on record was {ago(last, now)}."
        return ToolResult(tool=M365_SIGNINS, source=m365.source_name, data=data, client_code=client.client_code, reply=message)
    if len(rows) == 1:
        row = rows[0]
        ip = f" (IP {row.ip_address})" if row.ip_address else ""
        outcome = "succeeded" if row.ok else f"failed: {reason(row)}"
        reply = (
            f"Latest sign-in {who}: {ago(row.created_date_time, now)} from {place(row)}{ip}, "
            f"{row.app_display_name or 'unknown app'} ({row.client_app_used or '?'}), {outcome}."
        )
    else:
        reply = "\n".join([f"Latest {len(rows)} sign-ins {who}:", *(_row_line(row, now) for row in rows)])
    data["latest"] = [{"when": row.created_date_time.isoformat() if row.created_date_time else None, "country": row.country, "ok": row.ok} for row in rows]
    return ToolResult(tool=M365_SIGNINS, source=m365.source_name, data=data, client_code=client.client_code, reply=reply)
