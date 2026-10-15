"""Microsoft 365 adapters. Read-only: every method is a GET on Microsoft Graph in one customer tenant (or the stub's
fixtures), with tb-brain's own multi-tenant app and only the application permissions in docs/end-goal-plan.md.

Nothing here reads mail, files, chats or calendars, and nothing can reset a password, disable a user, assign a license,
revoke sessions or change a group, role or policy; tb-brain has no tool for any of that. The tenant id always comes from
the client resolver (Flow's client record), never from the model.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

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
)

# Data a tenant's license has to include. Graph refuses these calls outright when it doesn't, which is not the same as
# there being nothing to see: (what it is, the license that brings it).
FEATURES: dict[str, tuple[str, str]] = {
    "sign_in_activity": ("last sign-in dates", "Entra ID P1, e.g. Business Premium or E3"),
    "signins": ("sign-in logs", "Entra ID P1, e.g. Business Premium or E3"),
    "mfa_report": ("the MFA registration report", "Entra ID P1, e.g. Business Premium or E3"),
    "conditional_access": ("Conditional Access", "Entra ID P1, e.g. Business Premium or E3"),
    "risky_users": ("risky users from Identity Protection", "Entra ID P2, e.g. E5"),
    "intune": ("Intune device management", "Intune, e.g. Business Premium or E3"),
    "defender": ("Microsoft Defender alerts and incidents", "Microsoft Defender, e.g. Business Premium or E5"),
}


class M365Source(Protocol):
    source_name: str

    # GET /organization
    def organization(self, *, tenant_id: str) -> Organization: ...

    # GET /domains
    def list_domains(self, *, tenant_id: str) -> list[Domain]: ...

    # GET /subscribedSkus
    def list_subscribed_skus(self, *, tenant_id: str) -> list[SubscribedSku]: ...

    # GET /users ($select; with signInActivity only when asked, since that needs Entra ID P1)
    def list_users(self, *, tenant_id: str, sign_in_activity: bool = False) -> list[User]: ...

    # GET /users/{id}/memberOf
    def list_member_of(self, *, tenant_id: str, user_id: str) -> list[DirectoryObject]: ...

    # GET /users/{id}/authentication/methods
    def list_auth_methods(self, *, tenant_id: str, user_id: str) -> list[AuthMethod]: ...

    # GET /reports/authenticationMethods/userRegistrationDetails (Entra ID P1)
    def list_registration_details(self, *, tenant_id: str) -> list[RegistrationDetail]: ...

    # GET /directoryRoles?$expand=members (only roles someone holds are listed)
    def list_directory_roles(self, *, tenant_id: str) -> list[DirectoryRole]: ...

    # GET /auditLogs/signIns?$filter=createdDateTime ge ... (Entra ID P1)
    def list_sign_ins(self, *, tenant_id: str, since: datetime, user_principal_name: str | None = None) -> list[SignIn]: ...

    # GET /identityProtection/riskyUsers (Entra ID P2)
    def list_risky_users(self, *, tenant_id: str) -> list[RiskyUser]: ...

    # GET /identity/conditionalAccess/policies (Entra ID P1)
    def list_conditional_access_policies(self, *, tenant_id: str) -> list[CaPolicy]: ...

    # GET /policies/identitySecurityDefaultsEnforcementPolicy
    def security_defaults(self, *, tenant_id: str) -> SecurityDefaults: ...

    # GET /deviceManagement/managedDevices (Intune)
    def list_managed_devices(self, *, tenant_id: str) -> list[ManagedDevice]: ...

    # GET /users/{id}/mailboxSettings (MailboxSettings.Read: settings only, never mail)
    def mailbox_settings(self, *, tenant_id: str, user_id: str) -> MailboxSettings: ...

    # GET /users/{id}/mailFolders/inbox/messageRules (the rules, never the messages)
    def list_inbox_rules(self, *, tenant_id: str, user_id: str) -> list[InboxRule]: ...

    # GET beta /reports/getMailboxUsageDetail(period='D7')?$format=application/json
    def mailbox_usage(self, *, tenant_id: str) -> list[MailboxUsage]: ...

    # GET /security/secureScores?$top=1; None when the tenant has no score yet
    def secure_score(self, *, tenant_id: str) -> SecureScore | None: ...

    # GET /security/secureScoreControlProfiles
    def list_secure_score_controls(self, *, tenant_id: str) -> list[ControlProfile]: ...

    # GET /security/alerts_v2 (Defender)
    def list_security_alerts(self, *, tenant_id: str) -> list[SecurityAlert]: ...

    # GET /security/incidents (Defender)
    def list_security_incidents(self, *, tenant_id: str) -> list[Incident]: ...

    # GET /applications ($select names and credential end dates only; never secret values, which Graph never returns)
    def list_applications(self, *, tenant_id: str) -> list[Application]: ...

    # GET /auditLogs/directoryAudits?$filter=activityDateTime ge ...
    def list_directory_audits(self, *, tenant_id: str, since: datetime) -> list[DirectoryAudit]: ...

    # GET /admin/serviceAnnouncement/healthOverviews
    def list_service_health(self, *, tenant_id: str) -> list[ServiceHealth]: ...

    # GET /admin/serviceAnnouncement/issues?$filter=isResolved eq false
    def list_service_issues(self, *, tenant_id: str) -> list[ServiceIssue]: ...


class M365NotConfigured(RuntimeError):
    """M365_MODE=http but M365_CLIENT_ID or M365_CLIENT_SECRET is missing, or Microsoft rejected them."""


class M365Unavailable(RuntimeError):
    """Microsoft Graph or the sign-in endpoint did not answer: unreachable, timed out, throttled past our retries,
    or a server error."""


class M365RequestError(RuntimeError):
    """Graph answered but refused the request for another reason (400/404). The message is safe to show."""


class M365ConsentMissing(RuntimeError):
    """tb-brain's app has no access to this tenant: never consented, consent removed, or one permission missing."""

    def __init__(self, message: str, *, permission: str | None = None) -> None:
        super().__init__(message)
        self.permission = permission


class M365LicenseRequired(RuntimeError):
    """Graph refused because the tenant's license doesn't include the feature (a key of FEATURES)."""

    def __init__(self, feature: str) -> None:
        what, needs = FEATURES.get(feature, (feature, "a higher Microsoft 365 license"))
        super().__init__(f"the tenant's license doesn't include {what} (needs {needs})")
        self.feature = feature
