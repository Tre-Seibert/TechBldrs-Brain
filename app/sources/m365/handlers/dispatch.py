"""Microsoft 365 tool name + raw arguments -> the handler that runs it.

Arguments are taken the way a model naturally sends them when the meaning is not in doubt: the user under upn / email
/ name, the client under client / client_name, an email address sent as the client (it is the user), "failed" for
failure, "Secure Score" for secure_score, null for "not given". A missing client is found from the user's email
domain or Flow contact, or asked for; a missing user is asked for. The tenant id always comes from the client resolver.

Microsoft 365 being unreachable, not set up, or not consented in the client's tenant comes back as a reply the user
can read. A license that lacks the feature is a successful answer that says so (data.license_required), never "none".
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from app.knowledge.source import KnowledgeSource
from app.sources.clients import ClientResolver
from app.sources.m365.handlers.audit import AuditArgs, audit
from app.sources.m365.handlers.common import (
    ERRORS,
    error_message,
    is_email,
    license_message,
    refuse,
    resolve_tenant,
    wants_all,
)
from app.sources.m365.handlers.devices import DevicesArgs, devices
from app.sources.m365.handlers.licenses import LicenseArgs, license_all, license_summary
from app.sources.m365.handlers.mailboxes import MailboxesArgs, mailboxes
from app.sources.m365.handlers.mfa import MfaArgs, mfa_all, mfa_report
from app.sources.m365.handlers.policies import PoliciesArgs, policies, policies_all
from app.sources.m365.handlers.security import SecurityArgs, security, security_all
from app.sources.m365.handlers.signins import SignInArgs, signins
from app.sources.m365.handlers.tenant import TenantArgs, service_health_any, tenant
from app.sources.m365.handlers.users import GetUserArgs, UsersArgs, get_user, list_users
from app.sources.m365.protocol import M365LicenseRequired, M365NotConfigured, M365Source
from app.sources.m365.tools import (
    M365_AUDIT,
    M365_DEVICES,
    M365_GET_USER,
    M365_LICENSE_SUMMARY,
    M365_MAILBOXES,
    M365_MFA_REPORT,
    M365_POLICIES,
    M365_SECURITY,
    M365_SIGNINS,
    M365_TENANT,
    M365_USERS,
)
from app.tools.base import ChatTurn, ToolResult

_TOOLS: dict[str, tuple[type[BaseModel], Callable[..., ToolResult]]] = {
    M365_USERS: (UsersArgs, list_users),
    M365_GET_USER: (GetUserArgs, get_user),
    M365_MFA_REPORT: (MfaArgs, mfa_report),
    M365_LICENSE_SUMMARY: (LicenseArgs, license_summary),
    M365_SIGNINS: (SignInArgs, signins),
    M365_SECURITY: (SecurityArgs, security),
    M365_POLICIES: (PoliciesArgs, policies),
    M365_DEVICES: (DevicesArgs, devices),
    M365_MAILBOXES: (MailboxesArgs, mailboxes),
    M365_TENANT: (TenantArgs, tenant),
    M365_AUDIT: (AuditArgs, audit),
}
# Tools that answer client_code=all, and the views of them that do.
_EVERY_CLIENT: dict[str, Callable[..., ToolResult]] = {
    M365_MFA_REPORT: mfa_all,
    M365_LICENSE_SUMMARY: license_all,
    M365_SECURITY: security_all,
    M365_POLICIES: policies_all,
}

_CLIENT_KEYS = ("client", "client_name", "customer", "company", "tenant", "tenant_name", "code")
_USER_KEYS = ("upn", "email", "user_principal_name", "username", "user_name", "name", "person", "mail", "user_email")
# Values a model sends for each enum, keyed by the canonical value (matched lower case, spaces and hyphens as _).
_VALUES: dict[str, dict[str, dict[str, str]]] = {
    M365_USERS: {
        "view": {"count": "count", "counts": "count", "total": "count", "totals": "count", "summary": "count", "number": "count",
                 "list": "list", "all": "list"},
        "user_type": {"guest": "guest", "guests": "guest", "external": "guest", "member": "member", "members": "member",
                      "internal": "member", "employee": "member", "employees": "member"},
    },
    M365_SIGNINS: {
        "status": {"failure": "failure", "failures": "failure", "fail": "failure", "failed": "failure", "error": "failure",
                   "errors": "failure", "unsuccessful": "failure", "blocked": "failure", "success": "success",
                   "successful": "success", "succeeded": "success", "ok": "success", "all": "all", "any": "all", "both": "all"},
    },
    M365_SECURITY: {
        "view": {"secure_score": "secure_score", "securescore": "secure_score", "score": "secure_score",
                 "risky_users": "risky_users", "risky": "risky_users", "risk": "risky_users", "risky_user": "risky_users",
                 "alerts": "alerts", "alert": "alerts", "defender": "alerts", "defender_alerts": "alerts",
                 "incidents": "incidents", "incident": "incidents", "overview": "overview", "summary": "overview", "all": "overview"},
        "status": {"open": "open", "active": "open", "new": "open", "unresolved": "open", "all": "all", "any": "all"},
    },
    M365_POLICIES: {
        "kind": {"mfa": "mfa", "mfa_enforcement": "mfa", "enforcement": "mfa", "multi_factor": "mfa", "2fa": "mfa",
                 "conditional_access": "conditional_access", "ca": "conditional_access", "policies": "conditional_access",
                 "conditional_access_policies": "conditional_access", "security_defaults": "security_defaults",
                 "securitydefaults": "security_defaults", "defaults": "security_defaults"},
    },
    M365_MAILBOXES: {
        "view": {"list": "list", "kinds": "list", "type": "list", "usage": "usage", "size": "usage", "sizes": "usage",
                 "quota": "usage", "storage": "usage", "full": "usage", "external_forwarding_rules": "external_forwarding_rules",
                 "external_forwarding": "external_forwarding_rules", "forwarding": "external_forwarding_rules",
                 "forwarding_rules": "external_forwarding_rules", "inbox_rules": "external_forwarding_rules",
                 "rules": "external_forwarding_rules"},
        "kind": {"shared": "shared", "shared_mailbox": "shared", "shared_mailboxes": "shared", "user": "user", "users": "user",
                 "user_mailbox": "user", "all": "all", "any": "all"},
    },
    M365_TENANT: {
        "view": {"overview": "overview", "info": "overview", "summary": "overview", "domains": "domains", "domain": "domains",
                 "expiring_app_credentials": "expiring_app_credentials", "app_credentials": "expiring_app_credentials",
                 "expiring_secrets": "expiring_app_credentials", "app_secrets": "expiring_app_credentials",
                 "secrets": "expiring_app_credentials", "certificates": "expiring_app_credentials",
                 "expiring": "expiring_app_credentials", "service_health": "service_health", "health": "service_health",
                 "outage": "service_health", "outages": "service_health", "status": "service_health"},
    },
    M365_AUDIT: {
        "category": {"role_management": "role_management", "roles": "role_management", "role": "role_management",
                     "admin_roles": "role_management", "rolemanagement": "role_management", "user_management": "user_management",
                     "users": "user_management", "user": "user_management", "group_management": "group_management",
                     "groups": "group_management", "group": "group_management", "application": "application",
                     "applications": "application", "apps": "application", "app": "application", "policy": "policy",
                     "policies": "policy", "all": "all", "any": "all"},
    },
}


def handle(
    m365: M365Source,
    name: str,
    raw_args: dict[str, Any],
    turn: ChatTurn | None = None,
    *,
    knowledge: KnowledgeSource | None = None,
    clients: ClientResolver,
) -> ToolResult | None:
    """Run one Microsoft 365 tool. None when `name` is not a Microsoft 365 tool."""
    if name not in _TOOLS:
        return None
    model, run = _TOOLS[name]
    args = model.model_validate(normalized(name, raw_args))
    code = getattr(args, "client_code", None)
    try:
        if name == M365_TENANT and args.view == "service_health" and not (code or "").strip():
            return service_health_any(m365, clients)
        if wants_all(code):
            every = _EVERY_CLIENT.get(name)
            if (name == M365_SECURITY and args.view not in ("overview", "secure_score")) or (
                name == M365_POLICIES and args.kind == "conditional_access"
            ):
                every = None
            if every is None:
                return refuse(name, m365, f"{name} looks at one client at a time here. Which client?")
            return every(m365, clients, args)
    except M365NotConfigured as exc:
        return refuse(name, m365, error_message(None, exc))
    scope = resolve_tenant(name, m365, clients, code, getattr(args, "user", None))
    if isinstance(scope, ToolResult):
        return scope
    client = scope.client
    try:
        result = run(m365, client, args)
    except M365LicenseRequired as exc:
        message = license_message(client, exc.feature)
        result = ToolResult(tool=name, source=m365.source_name, data={"license_required": exc.feature},
                            client_code=client.client_code, reply=message)
    except ERRORS as exc:
        return refuse(name, m365, error_message(client, exc), client_code=client.client_code)
    if scope.note and result.reply:
        result = result.model_copy(update={"reply": f"{scope.note}\n{result.reply}"})
    return result


def _key(value: str) -> str:
    return re.sub(r"[\s-]+", "_", value.strip().lower())


def normalized(name: str, raw_args: dict[str, Any] | None) -> dict[str, Any]:
    """The arguments as the handler's model expects them: nulls dropped, the client and user under their own names,
    an email sent as the client moved to the user, and enum values in their canonical spelling."""
    args = {key: value for key, value in (raw_args or {}).items() if value is not None and value != ""}
    if "client_code" not in args:
        hit = next((key for key in _CLIENT_KEYS if str(args.get(key) or "").strip()), None)
        if hit:
            args["client_code"] = args.pop(hit)
    if "user" not in args:
        hit = next((key for key in _USER_KEYS if str(args.get(key) or "").strip()), None)
        if hit:
            args["user"] = args.pop(hit)
    code = str(args.get("client_code") or "").strip()
    if is_email(code) and not str(args.get("user") or "").strip():
        args["user"] = args.pop("client_code")
    if name == M365_USERS and args.get("user") and not args.get("q"):
        args["q"] = args.pop("user")  # m365_users filters by name; one person is m365_get_user, but this answers too
    for field, values in _VALUES.get(name, {}).items():
        value = args.get(field)
        if isinstance(value, str):
            args[field] = values.get(_key(value), _key(value))
    if name == M365_GET_USER and isinstance(args.get("include"), (str, list)):
        items = args["include"].split(",") if isinstance(args["include"], str) else args["include"]
        args["include"] = [_key(str(item)) for item in items if str(item).strip()]
    return args
