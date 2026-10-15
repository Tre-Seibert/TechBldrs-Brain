"""Microsoft 365 tool names and OpenAI schemas. All read-only; names carry the m365_ prefix.

OPENAI_TOOLS go out with PROMPT_STYLE=legacy and LEAN_OPENAI_TOOLS with lean: same names and parameters, shorter
descriptions. No tool takes a tenant id: client_code is a client code or name, and tb-brain finds the tenant. There is
no tool to reset a password, disable a user, assign a license, revoke sessions or change a group, role or policy, and
none that reads mail, files, chats or calendars; the descriptions say so, so the model declines those requests.
"""

from __future__ import annotations

from typing import Any

from app.sources.base import function_tool

M365_USERS = "m365_users"
M365_GET_USER = "m365_get_user"
M365_MFA_REPORT = "m365_mfa_report"
M365_LICENSE_SUMMARY = "m365_license_summary"
M365_SIGNINS = "m365_signins"
M365_SECURITY = "m365_security"
M365_POLICIES = "m365_policies"
M365_DEVICES = "m365_devices"
M365_MAILBOXES = "m365_mailboxes"
M365_TENANT = "m365_tenant"
M365_AUDIT = "m365_audit"

TOOL_NAMES = (
    M365_USERS,
    M365_GET_USER,
    M365_MFA_REPORT,
    M365_LICENSE_SUMMARY,
    M365_SIGNINS,
    M365_SECURITY,
    M365_POLICIES,
    M365_DEVICES,
    M365_MAILBOXES,
    M365_TENANT,
    M365_AUDIT,
)
# Every Microsoft 365 reply is a summary made in code; it is shown as-is.
RELAY_TOOL_NAMES = TOOL_NAMES

_CLIENT = {"type": "string", "description": "Client code (e.g. WDON) or client name. Never a tenant id."}
_CLIENT_ALL = {**_CLIENT, "description": _CLIENT["description"] + " 'all' checks every client's tenant (slow)."}
_USER = {"type": "string", "description": "The user's name, email address or UPN as the user wrote it, e.g. Debe or jsmith@buckheadpeds.example."}
_READ_ONLY = " Read-only: it cannot reset passwords, disable users, change licenses, MFA, groups, roles or policies."

_USERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": _CLIENT,
        "view": {"type": "string", "enum": ["list", "count"], "description": "count: totals only (licensed, unlicensed, disabled, guests)."},
        "enabled": {"type": "boolean", "description": "false: only disabled accounts; true: only enabled ones."},
        "licensed": {"type": "boolean", "description": "true: only accounts with a license; false: only unlicensed ones."},
        "user_type": {"type": "string", "enum": ["member", "guest"]},
        "inactive_days": {"type": "integer", "description": "Only enabled accounts with no sign-in for at least this many days (needs Entra ID P1)."},
        "role": {"type": "string", "description": "Only holders of this admin role, e.g. global_admin, 'Exchange Administrator'."},
        "admins_only": {"type": "boolean", "description": "true: only accounts with any admin role; TechBldrs' own accounts are marked."},
        "q": {"type": "string", "description": "Part of a name or email address."},
        "limit": {"type": "integer", "description": "Max users listed (default 40)."},
    },
}

_GET_USER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": {**_CLIENT, "description": _CLIENT["description"] + " Leave out if unknown: the user's email or Flow contact finds it."},
        "user": _USER,
        "include": {
            "type": "array",
            "items": {"type": "string", "enum": ["groups", "auth_methods"]},
            "description": "groups: group memberships. auth_methods: registered MFA methods. Account state, licenses, admin roles and last sign-in always come back.",
        },
    },
    "required": ["user"],
}

_MFA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": _CLIENT_ALL,
        "registered": {"type": "boolean", "description": "false: only users without MFA; true: only users with MFA."},
        "admins_only": {"type": "boolean", "description": "true: only accounts with an admin role."},
    },
}

_LICENSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": _CLIENT_ALL,
        "sku": {"type": "string", "description": "Only this license, e.g. business_premium, 'Business Standard', E3, 'Exchange Online'."},
        "unused_only": {"type": "boolean", "description": "true: only licenses with seats bought but not assigned."},
    },
}

_SIGNINS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": {**_CLIENT, "description": _CLIENT["description"] + " Leave out if unknown and a user is given."},
        "user": _USER,
        "status": {"type": "string", "enum": ["failure", "success", "all"], "description": "Default all."},
        "hours": {"type": "integer", "description": "Only the last N hours (24 = last day)."},
        "days": {"type": "integer", "description": "Only the last N days (default 7, at most 30)."},
        "outside_country": {"type": "string", "description": "Only sign-ins from outside this country, two-letter code, e.g. US."},
        "limit": {"type": "integer", "description": "1 = just the latest sign-in (where did X last sign in from)."},
    },
}

_SECURITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": {**_CLIENT_ALL, "description": _CLIENT["description"] + " 'all' ranks every client's Secure Score (slow)."},
        "view": {
            "type": "string",
            "enum": ["overview", "secure_score", "risky_users", "alerts", "incidents"],
            "description": "overview: Secure Score and open Defender alerts. risky_users needs Entra ID P2.",
        },
        "status": {"type": "string", "enum": ["open", "all"], "description": "Alerts and incidents: default open."},
    },
}

_POLICIES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": {**_CLIENT_ALL, "description": _CLIENT["description"] + " 'all' with kind=mfa checks every client (slow)."},
        "kind": {
            "type": "string",
            "enum": ["mfa", "conditional_access", "security_defaults"],
            "description": "mfa (default): is MFA enforced, by security defaults or Conditional Access, and who is excluded.",
        },
    },
}

_DEVICES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": _CLIENT,
        "user": {**_USER, "description": "Only this user's devices: name or email."},
        "compliant": {"type": "boolean", "description": "false: only non-compliant devices."},
        "stale_days": {"type": "integer", "description": "Only devices that have not checked in with Intune for this many days."},
        "os": {"type": "string", "description": "Windows, iOS, Android, macOS."},
        "limit": {"type": "integer", "description": "Max devices listed (default 40)."},
    },
}

_MAILBOXES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": _CLIENT,
        "view": {
            "type": "string",
            "enum": ["list", "usage", "external_forwarding_rules"],
            "description": "list: mailboxes by kind. usage: size against quota. external_forwarding_rules: inbox rules sending mail outside the client.",
        },
        "kind": {"type": "string", "enum": ["shared", "user", "all"], "description": "With view=list."},
        "user": {**_USER, "description": "One person's mailbox (usage, or all of their inbox rules)."},
        "over_percent": {"type": "integer", "description": "With view=usage: only mailboxes at least this full, e.g. 90."},
    },
}

_TENANT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": {**_CLIENT, "description": _CLIENT["description"] + " Optional for service_health (the same for every tenant)."},
        "view": {
            "type": "string",
            "enum": ["overview", "domains", "expiring_app_credentials", "service_health"],
            "description": "domains: domains and whether verified. expiring_app_credentials: app secrets and certificates ending soon. service_health: Microsoft 365 outages.",
        },
        "days": {"type": "integer", "description": "expiring_app_credentials: within this many days (default 30)."},
    },
}

_AUDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": _CLIENT,
        "category": {
            "type": "string",
            "enum": ["all", "role_management", "user_management", "group_management", "application", "policy"],
            "description": "role_management for admin role changes. Default all.",
        },
        "days": {"type": "integer", "description": "Last N days (default 7; more than 7 needs Entra ID P1, at most 30)."},
        "user": {**_USER, "description": "Only changes made by or to this person."},
    },
}

OPENAI_TOOLS: list[dict[str, Any]] = [
    function_tool(
        M365_USERS,
        "A client's Microsoft 365 user accounts: name, email, enabled or disabled, licenses, admin roles, last sign-in. "
        "Filters: enabled, licensed, user_type, inactive_days, role, admins_only, q. 'How many users does WDON have?' -> "
        "client_code=WDON, view=count. 'Any disabled accounts at NGMC that still have a license?' -> enabled=false, "
        "licensed=true. 'Who are the global admins at WDON?' -> role=global_admin. 'Does anyone at BUCK besides us have "
        "admin rights?' -> admins_only=true. 'Who hasn't signed in for 30 days?' -> inactive_days=30. Counts are "
        "computed for you." + _READ_ONLY,
        _USERS_SCHEMA,
    ),
    function_tool(
        M365_GET_USER,
        "One Microsoft 365 user by name or email: account enabled or disabled, created date, licenses, admin roles, "
        "last sign-in; include=groups for group memberships, include=auth_methods for MFA methods. 'Is Debe's account "
        "disabled?' -> user=Debe (client_code if known). 'What MFA method does Debe use?' -> include=[auth_methods]. "
        "'What groups is X in?' -> include=[groups]. With no client, the user's email or Flow contact finds the client; "
        "several matches are listed for you to ask about." + _READ_ONLY,
        _GET_USER_SCHEMA,
    ),
    function_tool(
        M365_MFA_REPORT,
        "Who has MFA registered and who doesn't, for a client's enabled accounts, admins flagged. Uses the MFA "
        "registration report where the license has it, otherwise each user's methods, and says which. 'Who at WDON "
        "doesn't have MFA?' -> client_code=WDON, registered=false. 'Which admins don't have MFA?' -> registered=false, "
        "admins_only=true. client_code=all counts every client." + _READ_ONLY,
        _MFA_SCHEMA,
    ),
    function_tool(
        M365_LICENSE_SUMMARY,
        "A client's Microsoft 365 licenses by friendly name: seats bought, assigned and free, plus disabled accounts "
        "still holding one. 'What licenses does BUCK have?' -> client_code=BUCK. 'Does WDON have unused licenses?' -> "
        "unused_only=true. 'Which clients have unused Business Premium?' -> client_code=all, sku=business_premium, "
        "unused_only=true." + _READ_ONLY,
        _LICENSE_SCHEMA,
    ),
    function_tool(
        M365_SIGNINS,
        "Microsoft 365 sign-in logs (needs Entra ID P1), summarized by user, failure reason and country. 'Any failed "
        "sign-ins at BUCK in the last 24 hours?' -> status=failure, hours=24. 'Has anyone at NGMC signed in from outside "
        "the US this week?' -> outside_country=US, days=7. 'Where did jsmith@... last sign in from?' -> user, limit=1. "
        "'Why is Debe locked out / blocked?' -> user=Debe, status=failure." + _READ_ONLY,
        _SIGNINS_SCHEMA,
    ),
    function_tool(
        M365_SECURITY,
        "Microsoft 365 security for a client: Secure Score with the top improvement actions, Defender alerts and "
        "incidents, risky users (Entra ID P2). 'What's WDON's Secure Score?' -> view=secure_score. 'Any open Defender "
        "alerts at NGMC?' -> view=alerts. 'Any risky users at WDON?' -> view=risky_users." + _READ_ONLY,
        _SECURITY_SCHEMA,
    ),
    function_tool(
        M365_POLICIES,
        "Whether and how a client enforces MFA: security defaults, Conditional Access policies (Entra ID P1) with who "
        "is excluded. 'Is MFA enforced at BUCK?' -> kind=mfa. 'Is security defaults on at X?' -> kind=security_defaults. "
        "'What conditional access policies does NGMC have?' -> kind=conditional_access. 'Which clients have no MFA "
        "enforcement?' -> client_code=all, kind=mfa." + _READ_ONLY,
        _POLICIES_SCHEMA,
    ),
    function_tool(
        M365_DEVICES,
        "A client's Intune-managed devices (needs Intune): compliance, user, OS, last check-in. 'Which WDON devices are "
        "non-compliant?' -> compliant=false. 'What devices does Debe have enrolled?' -> user=Debe. 'Devices that haven't "
        "checked in for 2 weeks?' -> stale_days=14. Live online state, patches and antivirus are Datto RMM's." + _READ_ONLY,
        _DEVICES_SCHEMA,
    ),
    function_tool(
        M365_MAILBOXES,
        "Mailbox facts, never mail content: shared vs user mailboxes, size against quota, and inbox rules that forward "
        "mail outside the client. 'Which WDON users are shared mailboxes?' -> view=list, kind=shared. 'How full is "
        "Debe's mailbox?' -> view=usage, user=Debe. 'Which mailboxes are almost full?' -> view=usage, over_percent=90. "
        "'Does anyone have a forwarding rule to an outside address?' -> view=external_forwarding_rules. tb-brain never "
        "reads the emails in a client mailbox." + _READ_ONLY,
        _MAILBOXES_SCHEMA,
    ),
    function_tool(
        M365_TENANT,
        "A client's Microsoft 365 tenant: overview, domains and verification, app secrets and certificates expiring "
        "soon, Microsoft 365 service health. 'What domains does WDON have in M365?' -> view=domains. 'Any app secrets "
        "expiring soon at BUCK?' -> view=expiring_app_credentials, days=30. 'Is Microsoft 365 having an outage?' -> "
        "view=service_health (no client needed)." + _READ_ONLY,
        _TENANT_SCHEMA,
    ),
    function_tool(
        M365_AUDIT,
        "A client's Entra directory audit log, summarized by category and who made the change: users, groups, admin "
        "roles, apps, policies. 'What changed in WDON's tenant this week?' -> days=7. 'Who changed the admin roles at "
        "WDON recently?' -> category=role_management, days=30." + _READ_ONLY,
        _AUDIT_SCHEMA,
    ),
]

LEAN_OPENAI_TOOLS: list[dict[str, Any]] = [
    function_tool(M365_USERS, "A client's Microsoft 365 users, with filters (enabled, licensed, guest, inactive_days, role, admins_only) or view=count.", _USERS_SCHEMA),
    function_tool(M365_GET_USER, "One Microsoft 365 user by name or email: account state, licenses, roles, last sign-in; include groups or auth_methods.", _GET_USER_SCHEMA),
    function_tool(M365_MFA_REPORT, "Who has MFA and who doesn't at a client (registered=false), admins flagged; client_code=all counts every client.", _MFA_SCHEMA),
    function_tool(M365_LICENSE_SUMMARY, "A client's Microsoft 365 licenses: bought, assigned, free; client_code=all with sku and unused_only to compare clients.", _LICENSE_SCHEMA),
    function_tool(M365_SIGNINS, "Microsoft 365 sign-in logs: failures, countries, one user's latest (limit=1).", _SIGNINS_SCHEMA),
    function_tool(M365_SECURITY, "Secure Score, Defender alerts and incidents, risky users for a client.", _SECURITY_SCHEMA),
    function_tool(M365_POLICIES, "Is MFA enforced (security defaults, Conditional Access, exclusions); client_code=all for every client.", _POLICIES_SCHEMA),
    function_tool(M365_DEVICES, "A client's Intune devices: compliance, user, last check-in (stale_days).", _DEVICES_SCHEMA),
    function_tool(M365_MAILBOXES, "Mailbox facts, never content: shared mailboxes, usage vs quota, external forwarding rules.", _MAILBOXES_SCHEMA),
    function_tool(M365_TENANT, "Tenant overview, domains, expiring app secrets, Microsoft 365 service health.", _TENANT_SCHEMA),
    function_tool(M365_AUDIT, "Directory audit log for a client: changes by category and who made them.", _AUDIT_SCHEMA),
]
