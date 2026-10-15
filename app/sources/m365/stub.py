"""M365_MODE=stub: the Microsoft 365 protocol over in-process fixtures (app/sources/m365/fixtures.py).

It refuses what Graph refuses: a feature the tenant's license lacks raises M365LicenseRequired, a tenant id with no
fixture is a tenant that never consented to tb-brain's app, and a tenant with report concealment on gets hashed names
from the mailbox usage report.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from app.sources.m365 import fixtures
from app.sources.m365.protocol import M365ConsentMissing, M365LicenseRequired, M365RequestError
from app.sources.m365.schemas import (
    Application,
    AuthMethod,
    CaPolicy,
    ControlProfile,
    DirectoryAudit,
    DirectoryObject,
    DirectoryRole,
    Domain,
    InboxRule,
    Incident,
    MailboxSettings,
    MailboxUsage,
    ManagedDevice,
    Organization,
    RegistrationDetail,
    RiskyUser,
    SecureScore,
    SecurityAlert,
    SecurityDefaults,
    ServiceHealth,
    ServiceIssue,
    SignIn,
    SubscribedSku,
    User,
    when,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class StubM365Source:
    source_name = "m365-stub"

    def __init__(self, *, now: Callable[[], datetime] = _now) -> None:
        # Pinned at construction, so one process sees one consistent picture.
        self.now = now()
        self._tenants = fixtures.tenants(self.now)

    # --- reads -------------------------------------------------------------------------------------------------

    def organization(self, *, tenant_id: str) -> Organization:
        return Organization.model_validate(self._tenant(tenant_id)["organization"])

    def list_domains(self, *, tenant_id: str) -> list[Domain]:
        return [Domain.model_validate(row) for row in self._tenant(tenant_id)["domains"]]

    def list_subscribed_skus(self, *, tenant_id: str) -> list[SubscribedSku]:
        return [SubscribedSku.model_validate(row) for row in self._tenant(tenant_id)["skus"]]

    def list_users(self, *, tenant_id: str, sign_in_activity: bool = False) -> list[User]:
        tenant = self._tenant(tenant_id)
        if sign_in_activity:
            self._licensed(tenant, "p1", "sign_in_activity")
        return [User.model_validate(self._user_json(tenant, row, sign_in_activity)) for row in tenant["users"]]

    def list_member_of(self, *, tenant_id: str, user_id: str) -> list[DirectoryObject]:
        tenant = self._tenant(tenant_id)
        user = self._user(tenant, user_id)
        groups = [
            {"@odata.type": "#microsoft.graph.group", "id": f"group-{name}", "displayName": name, "securityEnabled": True,
             "mailEnabled": False, "groupTypes": []}
            for name in user["_stub"]["groups"]
        ]
        roles = [
            {"@odata.type": "#microsoft.graph.directoryRole", "id": f"role-{name}", "displayName": name}
            for name in user["_stub"]["roles"]
        ]
        return [DirectoryObject.model_validate(row) for row in (*groups, *roles)]

    def list_auth_methods(self, *, tenant_id: str, user_id: str) -> list[AuthMethod]:
        user = self._user(self._tenant(tenant_id), user_id)
        return [AuthMethod.model_validate(row) for row in fixtures.auth_methods(user)]

    def list_registration_details(self, *, tenant_id: str) -> list[RegistrationDetail]:
        tenant = self._tenant(tenant_id)
        self._licensed(tenant, "p1", "mfa_report")
        return [
            RegistrationDetail.model_validate(fixtures.registration_detail(row, bool(row["_stub"]["roles"])))
            for row in tenant["users"]
        ]

    def list_directory_roles(self, *, tenant_id: str) -> list[DirectoryRole]:
        tenant = self._tenant(tenant_id)
        roles = []
        for name, template in tenant["role_ids"].items():
            members = [
                {"@odata.type": "#microsoft.graph.user", "id": row["id"], "displayName": row["displayName"],
                 "userPrincipalName": row["userPrincipalName"]}
                for row in tenant["users"]
                if name in row["_stub"]["roles"]
            ]
            roles.append({"id": f"role-{template}", "displayName": name, "roleTemplateId": template, "members": members})
        return [DirectoryRole.model_validate(row) for row in roles]

    def list_sign_ins(self, *, tenant_id: str, since: datetime, user_principal_name: str | None = None) -> list[SignIn]:
        tenant = self._tenant(tenant_id)
        self._licensed(tenant, "p1", "signins")
        wanted = (user_principal_name or "").lower()
        rows = [
            SignIn.model_validate(row)
            for row in tenant["sign_ins"]
            if when(row["createdDateTime"]) >= since and (not wanted or row["userPrincipalName"].lower() == wanted)
        ]
        return sorted(rows, key=lambda row: row.created_date_time, reverse=True)  # Graph returns newest first

    def list_risky_users(self, *, tenant_id: str) -> list[RiskyUser]:
        tenant = self._tenant(tenant_id)
        self._licensed(tenant, "p2", "risky_users")
        return [RiskyUser.model_validate(row) for row in tenant["risky_users"]]

    def list_conditional_access_policies(self, *, tenant_id: str) -> list[CaPolicy]:
        tenant = self._tenant(tenant_id)
        self._licensed(tenant, "p1", "conditional_access")
        return [CaPolicy.model_validate(row) for row in tenant["ca_policies"]]

    def security_defaults(self, *, tenant_id: str) -> SecurityDefaults:
        return SecurityDefaults(is_enabled=self._tenant(tenant_id)["security_defaults"])

    def list_managed_devices(self, *, tenant_id: str) -> list[ManagedDevice]:
        tenant = self._tenant(tenant_id)
        self._licensed(tenant, "intune", "intune")
        return [ManagedDevice.model_validate(row) for row in tenant["devices"]]

    def mailbox_settings(self, *, tenant_id: str, user_id: str) -> MailboxSettings:
        user = self._user(self._tenant(tenant_id), user_id)
        if user["_stub"]["purpose"] is None:
            # What Graph says for an account with no Exchange mailbox (admins, guests).
            raise M365RequestError("Microsoft Graph 404: MailboxNotEnabledForRESTAPI The mailbox is either inactive, soft-deleted, or is hosted on-premise.")
        return MailboxSettings(user_purpose=user["_stub"]["purpose"])

    def list_inbox_rules(self, *, tenant_id: str, user_id: str) -> list[InboxRule]:
        user = self._user(self._tenant(tenant_id), user_id)
        if user["_stub"]["purpose"] is None:
            raise M365RequestError("Microsoft Graph 404: MailboxNotEnabledForRESTAPI The mailbox is either inactive, soft-deleted, or is hosted on-premise.")
        return [InboxRule.model_validate(row) for row in user["_stub"]["rules"]]

    def mailbox_usage(self, *, tenant_id: str) -> list[MailboxUsage]:
        tenant = self._tenant(tenant_id)
        return [MailboxUsage.model_validate(row) for row in fixtures.mailbox_usage(tenant, self.now)]

    def secure_score(self, *, tenant_id: str) -> SecureScore | None:
        score = self._tenant(tenant_id)["secure_score"]
        return SecureScore.model_validate(score) if score else None

    def list_secure_score_controls(self, *, tenant_id: str) -> list[ControlProfile]:
        return [ControlProfile.model_validate(row) for row in self._tenant(tenant_id)["control_profiles"]]

    def list_security_alerts(self, *, tenant_id: str) -> list[SecurityAlert]:
        tenant = self._tenant(tenant_id)
        self._licensed(tenant, "defender", "defender")
        return [SecurityAlert.model_validate(row) for row in tenant["alerts"]]

    def list_security_incidents(self, *, tenant_id: str) -> list[Incident]:
        tenant = self._tenant(tenant_id)
        self._licensed(tenant, "defender", "defender")
        return [Incident.model_validate(row) for row in tenant["incidents"]]

    def list_applications(self, *, tenant_id: str) -> list[Application]:
        return [Application.model_validate(row) for row in self._tenant(tenant_id)["applications"]]

    def list_directory_audits(self, *, tenant_id: str, since: datetime) -> list[DirectoryAudit]:
        rows = [DirectoryAudit.model_validate(row) for row in self._tenant(tenant_id)["audits"]]
        return sorted((row for row in rows if row.activity_date_time and row.activity_date_time >= since),
                      key=lambda row: row.activity_date_time, reverse=True)

    def list_service_health(self, *, tenant_id: str) -> list[ServiceHealth]:
        self._tenant(tenant_id)
        return [ServiceHealth.model_validate(row) for row in fixtures.SERVICE_HEALTH]

    def list_service_issues(self, *, tenant_id: str) -> list[ServiceIssue]:
        self._tenant(tenant_id)
        return [ServiceIssue.model_validate(row) for row in fixtures._service_issues(self.now)]

    # --- helpers -----------------------------------------------------------------------------------------------

    def _tenant(self, tenant_id: str) -> dict[str, Any]:
        tenant = self._tenants.get((tenant_id or "").strip().lower())
        if tenant is None:
            # What the token endpoint says for a tenant that never consented to the app.
            raise M365ConsentMissing("Microsoft refused tb-brain's app a token for this tenant (AADSTS700016).")
        return tenant

    @staticmethod
    def _licensed(tenant: dict[str, Any], license_key: str, feature: str) -> None:
        if not tenant["licenses"][license_key]:
            raise M365LicenseRequired(feature)

    @staticmethod
    def _user(tenant: dict[str, Any], user_id: str) -> dict[str, Any]:
        hit = next((row for row in tenant["users"] if row["id"] == user_id), None)
        if hit is None:
            raise M365RequestError(f"Microsoft Graph 404: Request_ResourceNotFound Resource '{user_id}' does not exist.")
        return hit

    def _user_json(self, tenant: dict[str, Any], row: dict[str, Any], sign_in_activity: bool) -> dict[str, Any]:
        out = {key: value for key, value in row.items() if key != "_stub"}
        if sign_in_activity:
            out["signInActivity"] = self._activity(tenant, row)
        return out

    @staticmethod
    def _activity(tenant: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        """signInActivity from the user's last success and their sign-in rows (any status counts as a sign-in)."""
        mine = [entry for entry in tenant["sign_ins"] if entry["userId"] == row["id"]]
        success = [when(entry["createdDateTime"]) for entry in mine if entry["status"]["errorCode"] == 0]
        last_success = row["_stub"]["last_success"]
        succeeded = max([moment for moment in (last_success, *success) if moment], default=None)
        attempted = max([moment for moment in (succeeded, *(when(entry["createdDateTime"]) for entry in mine)) if moment], default=None)
        return {
            "lastSignInDateTime": fixtures._iso(attempted) if attempted else None,
            "lastNonInteractiveSignInDateTime": None,
            "lastSuccessfulSignInDateTime": fixtures._iso(succeeded) if succeeded else None,
        }
