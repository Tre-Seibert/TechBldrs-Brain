"""Synthetic Microsoft Graph data for M365_MODE=stub: three tenants, shaped like Graph's JSON.

The tenant ids are the ones Flow's stub has on record for WDON, BUCK and NGMC (app/sources/flow/fixtures.py), so the
client resolver maps each client to its tenant the way it will in production. Any other tenant id (WDLB, NRTH) behaves
like a tenant that never consented to tb-brain's app.

- WDON Western Dental: Business Premium (Entra ID P1, Intune, Defender for Business), no P2. A mix of MFA states and
  an admin without MFA (Office Admin, also excluded from the MFA policy); Chris Patel signed in from Lagos and has an
  inbox rule forwarding to an outside address (a Defender alert too); Thomas Carter's password is being sprayed from
  Russia; Debe Hernandez locked herself out this morning. People line up with Flow and Datto: Debe Hernandez (Flow
  contact, last user on WDON-FD01), Chris Patel (WDON-1830 password reset), Thomas Carter and Sean O'Brien (Flow
  contacts, Sean on WDON-LT01), Brooke Ellerby (the new hire on WDON-2619 "B Ellerby Email") and the payroll mailbox
  (WDON-2621 "Create Payroll Email Address").
- BUCK Buckhead Pediatrics: Business Standard and Basic, no Entra ID P1, Intune or Defender, so sign-in, MFA-report,
  Conditional Access and device questions get the license reply. Security defaults are on.
- NGMC North Georgia Medical Clinic: Microsoft 365 E3 with an Entra ID P2 add-on, report name concealment on (the
  mailbox usage report returns hashed names), and a guest user from a partner lab. Its MFA policy is still
  report-only, so MFA is not enforced there.

Dates are relative to `now` so "this week" and "inactive for 30 days" keep working. Names and addresses are fake.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

GB = 1024**3

WDON_TENANT = "3c1e8a52-6b0d-4f7e-9a21-5d84c0b7e301"
BUCK_TENANT = "8f2a4d19-0c6e-4b3a-b7d5-1e9f6a2c4b02"
NGMC_TENANT = "d47b9e06-2a1f-4c8d-8e63-9b0a5f7c1d03"

GLOBAL_ADMIN = "62e90394-69f5-4237-9190-012177145e10"
USER_ADMIN = "fe930be7-5e62-47db-91af-98c3a49a38b1"
EXCHANGE_ADMIN = "29232cdf-9323-42fd-ade2-1d097af3e4de"

_SKU_IDS = {
    "SPB": "cbdc14ab-d96c-4c30-b9f4-6ada7cdc1d46",
    "EXCHANGESTANDARD": "4b9405b0-7788-4568-add1-99614e613b69",
    "FLOW_FREE": "f30db892-07e9-47e9-837c-80727f46fd3d",
    "O365_BUSINESS_PREMIUM": "f245ecc8-75af-4f8e-b61f-27d8114de5f3",
    "O365_BUSINESS_ESSENTIALS": "3b555118-da6a-4418-894f-7df1e2096870",
    "SPE_E3": "05e9a617-0261-4cee-bb44-138d3ef5d965",
    "AAD_PREMIUM_P2": "84a661c4-e949-4bd2-a560-ed7766fcaf2b",
    "ATP_ENTERPRISE": "4ef96642-f096-40de-a3e9-d83fb2f90211",
    "MCOEV": "e43b5b99-8dfb-405f-9987-dc307f34bcbd",
}

_MFA_METHODS = {"microsoftAuthenticator", "phone", "fido2", "softwareOath", "windowsHelloForBusiness"}
# userRegistrationDetails names for each method type.
_REGISTERED_NAMES = {
    "microsoftAuthenticator": "microsoftAuthenticatorPush",
    "phone": "mobilePhone",
    "fido2": "fido2SecurityKey",
    "softwareOath": "softwareOneTimePasscode",
    "windowsHelloForBusiness": "windowsHelloForBusiness",
}


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def concealed_name(text: str) -> str:
    """How Graph's usage reports show a name with concealment on: a 32-character hex hash."""
    return hashlib.md5(text.lower().encode("utf-8")).hexdigest().upper()  # noqa: S324 - a fake hash for a stub


def _user(
    now: datetime,
    code: str,
    n: int,
    name: str,
    upn: str,
    *,
    title: str | None = None,
    skus: tuple[str, ...] = (),
    enabled: bool = True,
    guest: bool = False,
    created: datetime | timedelta = timedelta(days=900),
    last_success: timedelta | None = timedelta(days=1),
    methods: tuple[str, ...] = ("microsoftAuthenticator",),
    authenticator_device: str | None = None,
    purpose: str | None = "user",
    mailbox_gb: float | None = None,
    quota_gb: float = 50.0,
    groups: tuple[str, ...] = (),
    roles: tuple[str, ...] = (),
    aliases: tuple[str, ...] = (),
    mail: str | None = "",
    rules: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """One user as Graph returns it, plus the stub's own keys (methods, groups, roles, mailbox) under "_stub"."""
    created_at = now - created if isinstance(created, timedelta) else created
    return {
        "id": f"{code.lower()}-user-{n:02d}",
        "displayName": name,
        "userPrincipalName": upn,
        "mail": upn if mail == "" else mail,
        "proxyAddresses": [f"SMTP:{upn}", *(f"smtp:{alias}" for alias in aliases)],
        "accountEnabled": enabled,
        "userType": "Guest" if guest else "Member",
        "createdDateTime": _iso(created_at),
        "jobTitle": title,
        "department": None,
        "assignedLicenses": [{"skuId": _SKU_IDS[sku], "disabledPlans": []} for sku in skus],
        "_stub": {
            "last_success": None if last_success is None else now - last_success,
            "methods": ("password", *methods),
            "authenticator_device": authenticator_device,
            "purpose": purpose,
            "mailbox_gb": mailbox_gb,
            "quota_gb": quota_gb,
            "groups": groups,
            "roles": roles,
            "rules": rules,
        },
    }


def _rule(n: int, name: str, *, forward: tuple[str, ...] = (), redirect: tuple[str, ...] = (), move: str | None = None,
          read: bool | None = None) -> dict[str, Any]:
    return {
        "id": f"rule-{n}",
        "displayName": name,
        "isEnabled": True,
        "actions": {
            "forwardTo": [{"emailAddress": {"address": address, "name": address}} for address in forward],
            "redirectTo": [{"emailAddress": {"address": address, "name": address}} for address in redirect],
            "moveToFolder": move,
            "markAsRead": read,
        },
    }


def _sign_in(
    now: datetime,
    n: int,
    user: dict[str, Any],
    ago: timedelta,
    *,
    city: str,
    state: str,
    country: str,
    error: int = 0,
    reason: str | None = None,
    app: str = "Office 365 Exchange Online",
    client: str = "Browser",
    ip: str = "203.0.113.10",
    policies: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    return {
        "id": f"signin-{n}",
        "createdDateTime": _iso(now - ago),
        "userPrincipalName": user["userPrincipalName"],
        "userDisplayName": user["displayName"],
        "userId": user["id"],
        "appDisplayName": app,
        "ipAddress": ip,
        "clientAppUsed": client,
        "status": {"errorCode": error, "failureReason": reason if error else None},
        "location": {"city": city, "state": state, "countryOrRegion": country},
        "conditionalAccessStatus": "failure" if error == 53003 else "success",
        "appliedConditionalAccessPolicies": [{"displayName": name, "result": result} for name, result in policies],
    }


def _audit(now: datetime, n: int, ago: timedelta, activity: str, category: str, by: str, target: str,
           target_upn: str | None = None) -> dict[str, Any]:
    return {
        "id": f"audit-{n}",
        "activityDateTime": _iso(now - ago),
        "activityDisplayName": activity,
        "category": category,
        "result": "success",
        "initiatedBy": {"user": {"displayName": None, "userPrincipalName": by}},
        "targetResources": [{"displayName": target, "userPrincipalName": target_upn, "type": "User"}],
    }


def _sku(part: str, enabled: int, users: list[dict[str, Any]], *, status: str = "Enabled", consumed: int | None = None) -> dict[str, Any]:
    sku_id = _SKU_IDS[part]
    used = sum(1 for user in users if any(lic["skuId"] == sku_id for lic in user["assignedLicenses"]))
    return {
        "skuId": sku_id,
        "skuPartNumber": part,
        "capabilityStatus": status,
        "consumedUnits": used if consumed is None else consumed,
        "prepaidUnits": {"enabled": enabled, "suspended": 0, "warning": 0},
        "appliesTo": "User",
    }


def _device(n: int, name: str, user: dict[str, Any] | None, os: str, version: str, state: str, synced: datetime,
            *, owner: str = "company", model: str = "OptiPlex 7090", maker: str = "Dell Inc.") -> dict[str, Any]:
    return {
        "id": f"device-{n}",
        "deviceName": name,
        "userPrincipalName": user["userPrincipalName"] if user else None,
        "userDisplayName": user["displayName"] if user else None,
        "operatingSystem": os,
        "osVersion": version,
        "complianceState": state,
        "lastSyncDateTime": _iso(synced),
        "enrolledDateTime": _iso(synced - timedelta(days=400)),
        "managedDeviceOwnerType": owner,
        "model": model,
        "manufacturer": maker,
    }


def _ca(n: int, name: str, state: str, *, include_users=("All",), exclude_users=(), include_roles=(), apps=(),
        controls=("mfa",)) -> dict[str, Any]:
    return {
        "id": f"ca-{n}",
        "displayName": name,
        "state": state,
        "conditions": {
            "users": {
                "includeUsers": list(include_users), "excludeUsers": list(exclude_users), "includeGroups": [],
                "excludeGroups": [], "includeRoles": list(include_roles), "excludeRoles": [],
            },
            "clientAppTypes": list(apps) or ["all"],
        },
        "grantControls": {"operator": "OR", "builtInControls": list(controls)},
    }


def _controls(scores: dict[str, tuple[str, float, float]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(controlScores for the score, control profiles) from {id: (title, max, current)}."""
    profiles = [
        {"id": key, "title": title, "maxScore": top, "rank": rank, "deprecated": False, "controlCategory": "Identity"}
        for rank, (key, (title, top, _)) in enumerate(scores.items(), start=1)
    ]
    current = [{"controlName": key, "score": score, "controlCategory": "Identity"} for key, (_, _, score) in scores.items()]
    return current, profiles


SERVICE_HEALTH = [
    {"id": "Exchange", "service": "Exchange Online", "status": "serviceDegradation"},
    {"id": "Microsoft365Defender", "service": "Microsoft Defender XDR", "status": "serviceOperational"},
    {"id": "MicrosoftTeams", "service": "Microsoft Teams", "status": "serviceOperational"},
    {"id": "SharePoint", "service": "SharePoint Online", "status": "serviceOperational"},
    {"id": "OneDriveForBusiness", "service": "OneDrive for Business", "status": "serviceOperational"},
    {"id": "OrgLiveID", "service": "Microsoft Entra", "status": "serviceOperational"},
    {"id": "Intune", "service": "Microsoft Intune", "status": "serviceOperational"},
]


def _service_issues(now: datetime) -> list[dict[str, Any]]:
    return [{
        "id": "EX912345",
        "title": "Some users may see delays receiving email in Outlook",
        "service": "Exchange Online",
        "status": "serviceDegradation",
        "classification": "incident",
        "isResolved": False,
        "startDateTime": _iso(now - timedelta(hours=3)),
        "impactDescription": "Inbound mail may arrive up to 30 minutes late.",
    }]


def _wdon(now: datetime) -> dict[str, Any]:
    code = "WDON"
    domain = "westerndental.example"
    debe = _user(now, code, 1, "Debe Hernandez", f"debe@{domain}", title="Office Manager", skus=("SPB",),
                 created=datetime(2021, 3, 15, 14, 0, tzinfo=timezone.utc), last_success=timedelta(hours=26),
                 methods=("microsoftAuthenticator", "phone"), authenticator_device="iPhone 14",
                 aliases=(f"dhernandez@{domain}",), mailbox_gb=47.6, groups=("WDON Staff", "Front Office", "Billing Mailbox Access"),
                 roles=("User Administrator",),
                 rules=(_rule(1, "Invoices to billing", redirect=(f"billing@{domain}",)),))
    chris = _user(now, code, 2, "Chris Patel", f"cpatel@{domain}", title="Front Desk", skus=("SPB",),
                  last_success=timedelta(hours=20), methods=("phone",), mailbox_gb=6.2, groups=("WDON Staff", "Front Office"),
                  rules=(_rule(2, "..", forward=("cpatel.archive@protonmail.example",), move="RSS Feeds", read=True),))
    thomas = _user(now, code, 3, "Thomas Carter", f"thomas.carter@{domain}", title="Associate Dentist", skus=("SPB",),
                   last_success=timedelta(days=3), methods=(), mailbox_gb=3.1, groups=("WDON Staff", "Clinical"))
    sean = _user(now, code, 4, "Sean O'Brien", f"sean.obrien@{domain}", title="Operations Manager", skus=("SPB",),
                 last_success=timedelta(hours=5), methods=("microsoftAuthenticator", "fido2"), authenticator_device="Pixel 8",
                 mailbox_gb=12.4, groups=("WDON Staff", "Billing Mailbox Access"),
                 rules=(_rule(3, "Newsletters", move="Newsletters"),))
    maria = _user(now, code, 5, "Maria Lopez", f"mlopez@{domain}", title="Hygienist", skus=("SPB",),
                  last_success=timedelta(days=45), methods=(), mailbox_gb=1.2, groups=("WDON Staff", "Clinical"))
    brooke = _user(now, code, 6, "Brooke Ellerby", f"bellerby@{domain}", title="Dental Assistant", skus=("SPB",),
                   created=datetime(2026, 9, 26, 15, 10, tzinfo=timezone.utc), last_success=timedelta(days=18), methods=(),
                   mailbox_gb=0.1, groups=("WDON Staff", "Clinical"))
    jenna = _user(now, code, 7, "Jenna Brooks", f"jbrooks@{domain}", title="Former Hygienist", skus=("SPB",), enabled=False,
                  last_success=timedelta(days=96), methods=("phone",), mailbox_gb=22.5)
    admin = _user(now, code, 8, "Office Admin", f"officeadmin@{domain}", last_success=timedelta(days=10), methods=(),
                  purpose=None, roles=("Global Administrator",), mail=None)
    techbldrs = _user(now, code, 9, "TechBldrs Admin", "techbldrs@westerndental.onmicrosoft.com", last_success=timedelta(days=1),
                      methods=("microsoftAuthenticator", "fido2"), authenticator_device="TechBldrs YubiKey", purpose=None,
                      roles=("Global Administrator",), mail=None)
    frontdesk = _user(now, code, 10, "Front Desk", f"frontdesk@{domain}", enabled=False,
                      created=datetime(2019, 6, 1, 13, 30, tzinfo=timezone.utc), last_success=None, methods=(),
                      purpose="shared", mailbox_gb=9.8)
    billing = _user(now, code, 11, "Billing", f"billing@{domain}", enabled=False, last_success=None, methods=(), purpose="shared",
                    mailbox_gb=31.0)
    payroll = _user(now, code, 12, "Payroll", f"payroll@{domain}", enabled=False,
                    created=datetime(2026, 9, 11, 10, 5, tzinfo=timezone.utc), last_success=None, methods=(), purpose="shared",
                    mailbox_gb=0.02)
    users = [debe, chris, thomas, sean, maria, brooke, jenna, admin, techbldrs, frontdesk, billing, payroll]
    newark = {"city": "Newark", "state": "New Jersey", "country": "US", "ip": "198.51.100.24"}
    sign_ins = [
        _sign_in(now, 1, debe, timedelta(minutes=40), error=50053, reason="Account is locked because the user tried to sign in too many times with an incorrect user ID or password.", app="Microsoft Office", client="Mobile Apps and Desktop clients", **newark),
        _sign_in(now, 2, debe, timedelta(minutes=55), error=50053, reason="Account is locked because the user tried to sign in too many times with an incorrect user ID or password.", app="Microsoft Office", client="Mobile Apps and Desktop clients", **newark),
        _sign_in(now, 3, debe, timedelta(minutes=60), error=50126, reason="Error validating credentials due to invalid username or password.", app="Microsoft Office", client="Mobile Apps and Desktop clients", **newark),
        _sign_in(now, 4, debe, timedelta(minutes=65), error=50126, reason="Error validating credentials due to invalid username or password.", app="Microsoft Office", client="Mobile Apps and Desktop clients", **newark),
        _sign_in(now, 5, debe, timedelta(minutes=70), error=50126, reason="Error validating credentials due to invalid username or password.", app="Microsoft Office", client="Mobile Apps and Desktop clients", **newark),
        _sign_in(now, 6, debe, timedelta(hours=26), app="Microsoft Office", client="Mobile Apps and Desktop clients", **newark),
        _sign_in(now, 7, chris, timedelta(hours=20), **newark),
        _sign_in(now, 8, chris, timedelta(days=2, hours=4), city="Lagos", state="Lagos", country="NG", ip="102.89.34.17"),
        _sign_in(now, 9, sean, timedelta(hours=5), **newark),
        _sign_in(now, 10, techbldrs, timedelta(days=1), city="Atlanta", state="Georgia", country="US", ip="203.0.113.50"),
        *(
            _sign_in(now, 11 + i, thomas, timedelta(hours=2 + 3 * i), city="Moscow", state="Moscow", country="RU", ip="185.220.101.7",
                     error=50126, reason="Error validating credentials due to invalid username or password.")
            for i in range(4)
        ),
        *(
            _sign_in(now, 15 + i, thomas, timedelta(hours=4 + 5 * i), city="Moscow", state="Moscow", country="RU", ip="185.220.101.7",
                     error=53003, reason="Access has been blocked by Conditional Access policies.", client="IMAP4",
                     policies=(("Block legacy authentication", "failure"),))
            for i in range(2)
        ),
        _sign_in(now, 17, thomas, timedelta(days=3), **newark),
    ]
    scores, profiles = _controls({
        "ExternalForwardingDisabled": ("Block automatic forwarding of mail to external domains", 8, 0),
        "AdminMFAV2": ("Require multifactor authentication for administrative roles", 10, 5),
        "IntegratedApps": ("Do not allow users to grant consent to unreliable applications", 4, 0),
        "MFARegistrationV2": ("Ensure all users can complete multifactor authentication", 9, 5.6),
        "BlockLegacyAuthentication": ("Enable policy to block legacy authentication", 8, 8),
        "SelfServicePasswordReset": ("Enable self-service password reset", 1, 0),
        "PWAgePolicyNew": ("Set the password expiration policy to never expire", 8, 8),
    })
    return {
        "client_code": code,
        "licenses": {"p1": True, "p2": False, "intune": True, "defender": True},
        "concealed": False,
        "organization": {
            "id": WDON_TENANT, "displayName": "Western Dental", "createdDateTime": "2019-05-20T16:00:00Z",
            "countryLetterCode": "US", "onPremisesSyncEnabled": False,
            "verifiedDomains": [{"name": domain, "isDefault": True, "isInitial": False},
                                {"name": "westerndental.onmicrosoft.com", "isDefault": False, "isInitial": True}],
        },
        "domains": [
            {"id": domain, "isVerified": True, "isDefault": True, "isInitial": False, "authenticationType": "Managed",
             "supportedServices": ["Email", "OfficeCommunicationsOnline"]},
            {"id": "westerndental.onmicrosoft.com", "isVerified": True, "isDefault": False, "isInitial": True,
             "authenticationType": "Managed", "supportedServices": ["Email"]},
            {"id": "wdonsmiles.example", "isVerified": False, "isDefault": False, "isInitial": False,
             "authenticationType": "Managed", "supportedServices": []},
        ],
        "users": users,
        "skus": [_sku("SPB", 10, users), _sku("EXCHANGESTANDARD", 2, users), _sku("FLOW_FREE", 10000, users, consumed=3)],
        "role_ids": {"Global Administrator": GLOBAL_ADMIN, "User Administrator": USER_ADMIN},
        "sign_ins": sign_ins,
        "ca_policies": [
            _ca(1, "Require MFA for all users", "enabled", exclude_users=(admin["id"],)),
            _ca(2, "Block legacy authentication", "enabled", apps=("exchangeActiveSync", "other"), controls=("block",)),
            _ca(3, "Require compliant device for admins", "enabledForReportingButNotEnforced", include_users=(),
                include_roles=(GLOBAL_ADMIN,), controls=("compliantDevice",)),
        ],
        "security_defaults": False,
        "devices": [
            _device(1, "WDON-FD01", debe, "Windows", "10.0.22631.4317", "compliant", now - timedelta(hours=3)),
            _device(2, "WDON-OP02", maria, "Windows", "10.0.22631.4169", "noncompliant", now - timedelta(days=1)),
            _device(3, "WDON-OP03", thomas, "Windows", "10.0.19045.4894", "noncompliant", now - timedelta(days=34)),
            _device(4, "WDON-LT01", sean, "Windows", "10.0.22631.4317", "compliant", now - timedelta(hours=2),
                    model="Latitude 5440"),
            _device(5, "Debe's iPhone", debe, "iOS", "17.6.1", "compliant", now - timedelta(days=1), owner="personal",
                    model="iPhone 14", maker="Apple"),
            _device(6, "Chris's iPhone", chris, "iOS", "16.7.8", "noncompliant", now - timedelta(days=2), owner="personal",
                    model="iPhone 11", maker="Apple"),
        ],
        "secure_score": {"currentScore": 48.2, "maxScore": 82.0, "createdDateTime": _iso(now - timedelta(hours=9)),
                         "controlScores": scores},
        "control_profiles": profiles,
        "alerts": [
            {"id": "alert-1", "title": "Suspicious inbox forwarding rule", "severity": "high", "status": "new",
             "category": "Exfiltration", "serviceSource": "microsoftDefenderForOffice365",
             "createdDateTime": _iso(now - timedelta(days=2, hours=2))},
            {"id": "alert-2", "title": "Email messages containing malicious URL removed after delivery", "severity": "low",
             "status": "resolved", "category": "InitialAccess", "serviceSource": "microsoftDefenderForOffice365",
             "createdDateTime": _iso(now - timedelta(days=9))},
        ],
        "incidents": [
            {"id": "inc-1", "displayName": "Suspicious inbox rule on one mailbox", "severity": "high", "status": "active",
             "createdDateTime": _iso(now - timedelta(days=2, hours=2))},
        ],
        "applications": [
            {"id": "app-1", "appId": "app-1", "displayName": "Dentrix Cloud Sync",
             "passwordCredentials": [{"displayName": "sync", "endDateTime": _iso(now + timedelta(days=20))}], "keyCredentials": []},
            {"id": "app-2", "appId": "app-2", "displayName": "WDON Scanner to Mail", "passwordCredentials": [],
             "keyCredentials": [{"displayName": "scanner cert", "endDateTime": _iso(now - timedelta(days=5))}]},
            {"id": "app-3", "appId": "app-3", "displayName": "Patient Portal SSO",
             "passwordCredentials": [{"displayName": "portal", "endDateTime": _iso(now + timedelta(days=300))}], "keyCredentials": []},
        ],
        "audits": [
            _audit(now, 1, timedelta(days=1, hours=3), "Update conditional access policy", "Policy",
                   "techbldrs@westerndental.onmicrosoft.com", "Block legacy authentication"),
            _audit(now, 2, timedelta(days=2, hours=1), "User registered security info", "UserManagement", f"cpatel@{domain}",
                   "Chris Patel", f"cpatel@{domain}"),
            _audit(now, 3, timedelta(days=3), "Consent to application", "ApplicationManagement", f"officeadmin@{domain}",
                   "PDF Converter Pro"),
            _audit(now, 4, timedelta(days=5), "Add member to group", "GroupManagement",
                   "techbldrs@westerndental.onmicrosoft.com", "Sean O'Brien", f"sean.obrien@{domain}"),
            _audit(now, 5, timedelta(days=6), "Add member to role", "RoleManagement", f"officeadmin@{domain}",
                   "Debe Hernandez", f"debe@{domain}"),
            _audit(now, 6, timedelta(days=18), "Reset user password", "UserManagement", f"officeadmin@{domain}",
                   "Brooke Ellerby", f"bellerby@{domain}"),
        ],
        "risky_users": [],
    }


def _buck(now: datetime) -> dict[str, Any]:
    code = "BUCK"
    domain = "buckheadpeds.example"
    john = _user(now, code, 1, "John Smith", f"jsmith@{domain}", title="Practice Administrator", skus=("O365_BUSINESS_PREMIUM",),
                 last_success=timedelta(hours=2), methods=("microsoftAuthenticator",), mailbox_gb=21.0, groups=("BUCK Staff",))
    priya = _user(now, code, 2, "Dr. Priya Patel", f"priya.patel@{domain}", title="Pediatrician", skus=("O365_BUSINESS_PREMIUM",),
                  last_success=timedelta(hours=8), methods=("phone",), mailbox_gb=44.8, groups=("BUCK Staff", "Providers"),
                  roles=("Global Administrator",))
    amy = _user(now, code, 3, "Amy Nguyen", f"anguyen@{domain}", title="Billing", skus=("O365_BUSINESS_PREMIUM",),
                last_success=timedelta(hours=1), methods=(), mailbox_gb=49.2, groups=("BUCK Staff", "Billing"))
    kara = _user(now, code, 4, "Kara Wells", f"kwells@{domain}", title="Nurse", skus=("O365_BUSINESS_ESSENTIALS",),
                 last_success=timedelta(days=2), methods=("microsoftAuthenticator",), mailbox_gb=2.1, groups=("BUCK Staff",))
    lisa = _user(now, code, 5, "Lisa Grant", f"lgrant@{domain}", title="Former Receptionist", skus=("O365_BUSINESS_ESSENTIALS",),
                 enabled=False, last_success=timedelta(days=140), methods=(), mailbox_gb=5.0)
    frontdesk = _user(now, code, 6, "Front Desk", f"frontdesk@{domain}", enabled=False, last_success=None, methods=(),
                      purpose="shared", mailbox_gb=13.0)
    techbldrs = _user(now, code, 7, "TechBldrs Admin", "techbldrs@buckheadpeds.onmicrosoft.com", last_success=timedelta(days=4),
                      methods=("microsoftAuthenticator",), purpose=None, roles=("Global Administrator",), mail=None)
    users = [john, priya, amy, kara, lisa, frontdesk, techbldrs]
    scores, profiles = _controls({
        "AdminMFAV2": ("Require multifactor authentication for administrative roles", 10, 10),
        "MFARegistrationV2": ("Ensure all users can complete multifactor authentication", 9, 6),
        "ExternalForwardingDisabled": ("Block automatic forwarding of mail to external domains", 8, 0),
        "mdo_safelinksforemail": ("Turn on Safe Links for email", 8, 0),
        "SelfServicePasswordReset": ("Enable self-service password reset", 1, 0),
    })
    return {
        "client_code": code,
        "licenses": {"p1": False, "p2": False, "intune": False, "defender": False},
        "concealed": False,
        "organization": {
            "id": BUCK_TENANT, "displayName": "Buckhead Pediatrics", "createdDateTime": "2020-02-11T15:00:00Z",
            "countryLetterCode": "US", "onPremisesSyncEnabled": False,
            "verifiedDomains": [{"name": domain, "isDefault": True, "isInitial": False},
                                {"name": "buckheadpeds.onmicrosoft.com", "isDefault": False, "isInitial": True}],
        },
        "domains": [
            {"id": domain, "isVerified": True, "isDefault": True, "isInitial": False, "authenticationType": "Managed",
             "supportedServices": ["Email"]},
            {"id": "buckheadpeds.onmicrosoft.com", "isVerified": True, "isDefault": False, "isInitial": True,
             "authenticationType": "Managed", "supportedServices": ["Email"]},
        ],
        "users": users,
        "skus": [_sku("O365_BUSINESS_PREMIUM", 5, users), _sku("O365_BUSINESS_ESSENTIALS", 3, users)],
        "role_ids": {"Global Administrator": GLOBAL_ADMIN},
        "sign_ins": [],
        "ca_policies": [],
        "security_defaults": True,
        "devices": [],
        "secure_score": {"currentScore": 31.0, "maxScore": 70.0, "createdDateTime": _iso(now - timedelta(hours=11)),
                         "controlScores": scores},
        "control_profiles": profiles,
        "alerts": [],
        "incidents": [],
        "applications": [
            {"id": "app-1", "appId": "app-1", "displayName": "Scan2Mail",
             "passwordCredentials": [{"displayName": "smtp relay", "endDateTime": _iso(now + timedelta(days=12))}], "keyCredentials": []},
            {"id": "app-2", "appId": "app-2", "displayName": "QuickBooks Connector",
             "passwordCredentials": [{"displayName": "qb", "endDateTime": _iso(now + timedelta(days=280))}], "keyCredentials": []},
        ],
        "audits": [
            _audit(now, 1, timedelta(days=3), "Update user", "UserManagement", f"priya.patel@{domain}", "Amy Nguyen", f"anguyen@{domain}"),
        ],
        "risky_users": [],
    }


def _ngmc(now: datetime) -> dict[str, Any]:
    code = "NGMC"
    domain = "ngmc.example"
    raj = _user(now, code, 1, "Dr. Raj Rao", f"rrao@{domain}", title="Medical Director", skus=("SPE_E3", "AAD_PREMIUM_P2", "MCOEV"),
                last_success=timedelta(hours=6), methods=("microsoftAuthenticator",), mailbox_gb=38.0, groups=("NGMC Staff", "Providers"),
                roles=("Exchange Administrator",))
    nora = _user(now, code, 2, "Nora Jones", f"njones@{domain}", title="Nurse Practitioner", skus=("SPE_E3", "AAD_PREMIUM_P2"),
                 last_success=timedelta(hours=3), methods=("phone",), mailbox_gb=12.0, groups=("NGMC Staff", "Providers"))
    tina = _user(now, code, 3, "Tina Brooks", f"tbrooks@{domain}", title="Billing", skus=("SPE_E3",), last_success=timedelta(days=1),
                 methods=(), mailbox_gb=46.5, groups=("NGMC Staff", "Billing"))
    reception = _user(now, code, 4, "Reception", f"reception@{domain}", enabled=False, last_success=None, methods=(),
                      purpose="shared", mailbox_gb=8.0)
    gary = _user(now, code, 5, "Gary Fields", f"gfields@{domain}", title="Former Physician Assistant", skus=("SPE_E3",), enabled=False,
                 last_success=timedelta(days=75), methods=("microsoftAuthenticator",), mailbox_gb=17.0)
    pat = _user(now, code, 6, "Pat Morgan", "pat.morgan_partnerlab.example#EXT#@ngmc.onmicrosoft.com", guest=True,
                created=timedelta(days=60), last_success=timedelta(days=50), methods=(), purpose=None,
                mail="pat.morgan@partnerlab.example", groups=("Lab Results Sharing",))
    techbldrs = _user(now, code, 7, "TechBldrs Admin", "techbldrs@ngmc.onmicrosoft.com", last_success=timedelta(days=2),
                      methods=("microsoftAuthenticator", "fido2"), purpose=None, roles=("Global Administrator",), mail=None)
    users = [raj, nora, tina, reception, gary, pat, techbldrs]
    atlanta = {"city": "Atlanta", "state": "Georgia", "country": "US", "ip": "198.51.100.80"}
    sign_ins = [
        _sign_in(now, 1, raj, timedelta(days=3), city="Toronto", state="Ontario", country="CA", ip="142.112.10.4"),
        _sign_in(now, 2, raj, timedelta(hours=6), **atlanta),
        _sign_in(now, 3, nora, timedelta(days=2), city="Beijing", state="Beijing", country="CN", ip="123.112.4.9", error=50126,
                 reason="Error validating credentials due to invalid username or password."),
        _sign_in(now, 4, nora, timedelta(hours=3), **atlanta),
        _sign_in(now, 5, tina, timedelta(days=1), city="Gainesville", state="Georgia", country="US", ip="198.51.100.91"),
    ]
    scores, profiles = _controls({
        "MFARegistrationV2": ("Ensure all users can complete multifactor authentication", 9, 7),
        "IntegratedApps": ("Do not allow users to grant consent to unreliable applications", 4, 4),
        "ExternalForwardingDisabled": ("Block automatic forwarding of mail to external domains", 8, 8),
        "SelfServicePasswordReset": ("Enable self-service password reset", 1, 1),
        "mdo_safelinksforemail": ("Turn on Safe Links for email", 8, 3),
    })
    return {
        "client_code": code,
        "licenses": {"p1": True, "p2": True, "intune": True, "defender": True},
        "concealed": True,
        "organization": {
            "id": NGMC_TENANT, "displayName": "North Georgia Medical Clinic", "createdDateTime": "2018-08-02T12:00:00Z",
            "countryLetterCode": "US", "onPremisesSyncEnabled": True,
            "verifiedDomains": [{"name": domain, "isDefault": True, "isInitial": False},
                                {"name": "northgamedical.example", "isDefault": False, "isInitial": False},
                                {"name": "ngmc.onmicrosoft.com", "isDefault": False, "isInitial": True}],
        },
        "domains": [
            {"id": domain, "isVerified": True, "isDefault": True, "isInitial": False, "authenticationType": "Managed",
             "supportedServices": ["Email"]},
            {"id": "northgamedical.example", "isVerified": True, "isDefault": False, "isInitial": False,
             "authenticationType": "Managed", "supportedServices": ["Email"]},
            {"id": "ngmc.onmicrosoft.com", "isVerified": True, "isDefault": False, "isInitial": True,
             "authenticationType": "Managed", "supportedServices": ["Email"]},
        ],
        "users": users,
        "skus": [_sku("SPE_E3", 8, users), _sku("AAD_PREMIUM_P2", 2, users), _sku("ATP_ENTERPRISE", 8, users, consumed=4),
                 _sku("MCOEV", 2, users)],
        "role_ids": {"Global Administrator": GLOBAL_ADMIN, "Exchange Administrator": EXCHANGE_ADMIN},
        "sign_ins": sign_ins,
        "ca_policies": [
            _ca(1, "MFA for all users", "enabledForReportingButNotEnforced", exclude_users=("GuestsOrExternalUsers",)),
            _ca(2, "Block sign-ins from outside the US", "enabledForReportingButNotEnforced", controls=("block",)),
            _ca(3, "Require compliant devices", "disabled", controls=("compliantDevice",)),
        ],
        "security_defaults": False,
        "devices": [
            _device(1, "NGMC-PC05", None, "Windows", "10.0.22631.4317", "compliant", now - timedelta(hours=4)),
            _device(2, "NGMC-PC07", None, "Windows", "10.0.19045.4651", "noncompliant", now - timedelta(days=6)),
            _device(3, "NGMC-LT02", nora, "Windows", "10.0.22631.4169", "compliant", now - timedelta(days=16),
                    model="ThinkPad T14"),
            _device(4, "NGMC-PC09", tina, "Windows", "10.0.22631.4317", "compliant", now - timedelta(hours=1)),
        ],
        "secure_score": {"currentScore": 61.5, "maxScore": 80.0, "createdDateTime": _iso(now - timedelta(hours=8)),
                         "controlScores": scores},
        "control_profiles": profiles,
        "alerts": [
            {"id": "alert-1", "title": "Email messages containing malware removed after delivery", "severity": "medium",
             "status": "new", "category": "Malware", "serviceSource": "microsoftDefenderForOffice365",
             "createdDateTime": _iso(now - timedelta(days=1))},
        ],
        "incidents": [],
        "applications": [],
        "audits": [
            _audit(now, 1, timedelta(days=6), "Disable account", "UserManagement", "techbldrs@ngmc.onmicrosoft.com", "Gary Fields",
                   f"gfields@{domain}"),
            _audit(now, 2, timedelta(days=2), "Update conditional access policy", "Policy", "techbldrs@ngmc.onmicrosoft.com",
                   "Block sign-ins from outside the US"),
        ],
        "risky_users": [
            {"id": nora["id"], "userPrincipalName": nora["userPrincipalName"], "userDisplayName": nora["displayName"],
             "riskLevel": "medium", "riskState": "atRisk", "riskDetail": "none",
             "riskLastUpdatedDateTime": _iso(now - timedelta(days=2))},
        ],
    }


def tenants(now: datetime) -> dict[str, dict[str, Any]]:
    """Every stub tenant by tenant id."""
    return {WDON_TENANT: _wdon(now), BUCK_TENANT: _buck(now), NGMC_TENANT: _ngmc(now)}


def auth_methods(user: dict[str, Any]) -> list[dict[str, Any]]:
    stub = user["_stub"]
    rows = []
    for n, kind in enumerate(stub["methods"]):
        row: dict[str, Any] = {"@odata.type": f"#microsoft.graph.{kind}AuthenticationMethod", "id": f"{user['id']}-m{n}"}
        if kind == "microsoftAuthenticator":
            row["displayName"] = stub["authenticator_device"] or "phone"
        if kind == "fido2":
            row["displayName"] = "Security key"
        rows.append(row)
    return rows


def registration_detail(user: dict[str, Any], admin: bool) -> dict[str, Any]:
    kinds = [kind for kind in user["_stub"]["methods"] if kind in _MFA_METHODS]
    registered = [_REGISTERED_NAMES[kind] for kind in kinds]
    return {
        "id": user["id"],
        "userPrincipalName": user["userPrincipalName"],
        "userDisplayName": user["displayName"],
        "userType": "guest" if user["userType"] == "Guest" else "member",
        "isAdmin": admin,
        "isMfaRegistered": bool(kinds),
        "isMfaCapable": bool(kinds) and user["accountEnabled"],
        "isSsprRegistered": bool(kinds),
        "methodsRegistered": registered,
        "defaultMfaMethod": registered[0] if registered else "none",
    }


def mailbox_usage(tenant: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    rows = []
    for user in tenant["users"]:
        stub = user["_stub"]
        if stub["mailbox_gb"] is None:
            continue
        upn, name = user["userPrincipalName"], user["displayName"]
        if tenant["concealed"]:
            upn, name = concealed_name(upn), concealed_name(name)
        quota = int(stub["quota_gb"] * GB)
        rows.append({
            "reportRefreshDate": (now - timedelta(days=1)).date().isoformat(),
            "userPrincipalName": upn,
            "displayName": name,
            "isDeleted": False,
            "recipientType": "Shared" if stub["purpose"] == "shared" else "User",
            "storageUsedInBytes": int(stub["mailbox_gb"] * GB),
            "issueWarningQuotaInBytes": quota - 1 * GB,
            "prohibitSendQuotaInBytes": quota - int(0.5 * GB),
            "prohibitSendReceiveQuotaInBytes": quota,
            "itemCount": int(stub["mailbox_gb"] * 9000),
            "lastActivityDate": (now - timedelta(days=1)).date().isoformat(),
        })
    return rows
