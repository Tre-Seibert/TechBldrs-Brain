"""Microsoft Graph records, parsed from the JSON Graph returns (field names as in Graph, camelCase).

Only the fields tb-brain reads; everything else in a response is ignored. Graph dates are ISO date-time strings, often
with seven fractional digits; report dates are plain dates. Nothing here is shown to the model as-is: the handlers
summarize.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

_FRACTION_RE = re.compile(r"\.(\d{6})\d+")


class _Graph(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


def when(value: Any) -> datetime | None:
    """A Graph date-time -> aware UTC datetime; None when absent. Seven fractional digits are cut to six."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = _FRACTION_RE.sub(r".\1", str(value).strip()).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _dates(*names: str):
    return field_validator(*names, mode="before")(lambda cls, value: when(value))


class VerifiedDomain(_Graph):
    name: str
    is_default: bool = False
    is_initial: bool = False


class Organization(_Graph):
    id: str | None = None
    display_name: str | None = None
    created_date_time: datetime | None = None
    country_letter_code: str | None = None
    on_premises_sync_enabled: bool | None = None
    verified_domains: list[VerifiedDomain] = []

    parse_dates = _dates("created_date_time")


class Domain(_Graph):
    id: str
    is_verified: bool = False
    is_default: bool = False
    is_initial: bool = False
    authentication_type: str | None = None  # Managed, Federated
    supported_services: list[str] = []


class PrepaidUnits(_Graph):
    enabled: int = 0
    suspended: int = 0
    warning: int = 0


class SubscribedSku(_Graph):
    sku_id: str
    sku_part_number: str = ""
    capability_status: str | None = None  # Enabled, Warning, Suspended, Deleted, LockedOut
    consumed_units: int = 0
    prepaid_units: PrepaidUnits = PrepaidUnits()
    applies_to: str | None = None  # User, Company


class AssignedLicense(_Graph):
    sku_id: str


class SignInActivity(_Graph):
    last_sign_in_date_time: datetime | None = None  # interactive
    last_non_interactive_sign_in_date_time: datetime | None = None
    last_successful_sign_in_date_time: datetime | None = None

    parse_dates = _dates("last_sign_in_date_time", "last_non_interactive_sign_in_date_time", "last_successful_sign_in_date_time")

    @property
    def last(self) -> datetime | None:
        moments = [m for m in (self.last_sign_in_date_time, self.last_non_interactive_sign_in_date_time) if m]
        return max(moments) if moments else None


class User(_Graph):
    id: str
    display_name: str | None = None
    user_principal_name: str = ""
    mail: str | None = None
    proxy_addresses: list[str] = []  # "SMTP:debe@...", "smtp:dhernandez@..."
    account_enabled: bool | None = None
    user_type: str | None = None  # Member, Guest
    created_date_time: datetime | None = None
    job_title: str | None = None
    department: str | None = None
    assigned_licenses: list[AssignedLicense] = []
    sign_in_activity: SignInActivity | None = None

    parse_dates = _dates("created_date_time")

    @property
    def name(self) -> str:
        return self.display_name or self.user_principal_name

    @property
    def guest(self) -> bool:
        return (self.user_type or "").lower() == "guest"

    @property
    def addresses(self) -> set[str]:
        """Every address the user answers to, lower case: UPN, mail, and proxy addresses."""
        found = {self.user_principal_name.lower(), (self.mail or "").lower()}
        found.update(item.split(":", 1)[-1].lower() for item in self.proxy_addresses)
        return {item for item in found if item}


class DirectoryObject(_Graph):
    odata_type: str | None = Field(default=None, alias="@odata.type")
    id: str
    display_name: str | None = None
    user_principal_name: str | None = None
    security_enabled: bool | None = None
    mail_enabled: bool | None = None
    group_types: list[str] = []

    @property
    def kind(self) -> str:
        """'group', 'directoryRole', 'user', ... from @odata.type."""
        return (self.odata_type or "").rsplit(".", 1)[-1] or "object"


class AuthMethod(_Graph):
    odata_type: str = Field(default="", alias="@odata.type")
    id: str | None = None
    display_name: str | None = None  # the phone or key name for Authenticator / FIDO2

    @property
    def kind(self) -> str:
        """'microsoftAuthenticator', 'phone', 'password', ... from @odata.type."""
        return (self.odata_type or "").rsplit(".", 1)[-1].removesuffix("AuthenticationMethod") or "unknown"


class RegistrationDetail(_Graph):
    id: str
    user_principal_name: str = ""
    user_display_name: str | None = None
    user_type: str | None = None  # member, guest
    is_admin: bool = False
    is_mfa_registered: bool = False
    is_mfa_capable: bool = False
    is_sspr_registered: bool = False
    methods_registered: list[str] = []
    default_mfa_method: str | None = None


class DirectoryRole(_Graph):
    id: str
    display_name: str = ""
    role_template_id: str | None = None
    members: list[DirectoryObject] = []


class SignInStatus(_Graph):
    error_code: int = 0
    failure_reason: str | None = None
    additional_details: str | None = None


class SignInLocation(_Graph):
    city: str | None = None
    state: str | None = None
    country_or_region: str | None = None


class AppliedPolicy(_Graph):
    display_name: str | None = None
    result: str | None = None  # success, failure, notApplied, notEnabled, ...


class SignIn(_Graph):
    id: str
    created_date_time: datetime | None = None
    user_principal_name: str = ""
    user_display_name: str | None = None
    user_id: str | None = None
    app_display_name: str | None = None
    ip_address: str | None = None
    client_app_used: str | None = None
    status: SignInStatus = SignInStatus()
    location: SignInLocation | None = None
    conditional_access_status: str | None = None
    applied_conditional_access_policies: list[AppliedPolicy] = []

    parse_dates = _dates("created_date_time")

    @property
    def ok(self) -> bool:
        return self.status.error_code == 0

    @property
    def country(self) -> str:
        return ((self.location.country_or_region if self.location else None) or "").upper()


class RiskyUser(_Graph):
    id: str
    user_principal_name: str = ""
    user_display_name: str | None = None
    risk_level: str | None = None  # low, medium, high, hidden, none
    risk_state: str | None = None  # atRisk, confirmedCompromised, remediated, dismissed, ...
    risk_detail: str | None = None
    risk_last_updated_date_time: datetime | None = None

    parse_dates = _dates("risk_last_updated_date_time")


class CaUsers(_Graph):
    include_users: list[str] = []
    exclude_users: list[str] = []
    include_groups: list[str] = []
    exclude_groups: list[str] = []
    include_roles: list[str] = []
    exclude_roles: list[str] = []


class CaConditions(_Graph):
    users: CaUsers = CaUsers()
    client_app_types: list[str] = []


class AuthenticationStrength(_Graph):
    display_name: str | None = None


class CaGrantControls(_Graph):
    operator: str | None = None
    built_in_controls: list[str] = []  # mfa, block, compliantDevice, domainJoinedDevice, ...
    authentication_strength: AuthenticationStrength | None = None


class CaPolicy(_Graph):
    id: str
    display_name: str = ""
    state: str = ""  # enabled, disabled, enabledForReportingButNotEnforced
    conditions: CaConditions = CaConditions()
    grant_controls: CaGrantControls | None = None

    @property
    def requires_mfa(self) -> bool:
        grant = self.grant_controls
        return bool(grant and ("mfa" in grant.built_in_controls or grant.authentication_strength))

    @property
    def blocks(self) -> bool:
        return bool(self.grant_controls and "block" in self.grant_controls.built_in_controls)


class SecurityDefaults(_Graph):
    is_enabled: bool = False


class ManagedDevice(_Graph):
    id: str
    device_name: str = ""
    user_principal_name: str | None = None
    user_display_name: str | None = None
    operating_system: str | None = None
    os_version: str | None = None
    compliance_state: str | None = None  # compliant, noncompliant, conflict, error, inGracePeriod, unknown
    last_sync_date_time: datetime | None = None
    enrolled_date_time: datetime | None = None
    managed_device_owner_type: str | None = None  # company, personal
    model: str | None = None
    manufacturer: str | None = None

    parse_dates = _dates("last_sync_date_time", "enrolled_date_time")


class MailboxSettings(_Graph):
    user_purpose: str | None = None  # user, shared, room, equipment, linked, others


class EmailAddress(_Graph):
    address: str = ""
    name: str | None = None


class Recipient(_Graph):
    email_address: EmailAddress = EmailAddress()


class RuleActions(_Graph):
    forward_to: list[Recipient] = []
    forward_as_attachment_to: list[Recipient] = []
    redirect_to: list[Recipient] = []
    move_to_folder: str | None = None
    delete: bool | None = None
    mark_as_read: bool | None = None

    @property
    def forwards(self) -> list[str]:
        """Every address the rule sends mail on to (forward, forward as attachment, redirect)."""
        return [r.email_address.address for r in (*self.forward_to, *self.forward_as_attachment_to, *self.redirect_to) if r.email_address.address]


class InboxRule(_Graph):
    id: str
    display_name: str | None = None
    is_enabled: bool = True
    actions: RuleActions = RuleActions()


class MailboxUsage(_Graph):
    user_principal_name: str = ""
    display_name: str | None = None
    is_deleted: bool = False
    recipient_type: str | None = None  # User, Shared, Room, Equipment
    storage_used_in_bytes: int | None = None
    issue_warning_quota_in_bytes: int | None = None
    prohibit_send_quota_in_bytes: int | None = None
    prohibit_send_receive_quota_in_bytes: int | None = None
    item_count: int | None = None
    last_activity_date: date | None = None

    @field_validator("last_activity_date", mode="before")
    @classmethod
    def _day(cls, value: Any) -> date | None:
        return date.fromisoformat(str(value)[:10]) if value else None


class ControlScore(_Graph):
    control_name: str
    score: float = 0.0
    control_category: str | None = None


class SecureScore(_Graph):
    current_score: float = 0.0
    max_score: float = 0.0
    created_date_time: datetime | None = None
    control_scores: list[ControlScore] = []

    parse_dates = _dates("created_date_time")


class ControlProfile(_Graph):
    id: str
    title: str = ""
    max_score: float = 0.0
    rank: int | None = None
    deprecated: bool = False
    control_category: str | None = None


class SecurityAlert(_Graph):
    id: str
    title: str = ""
    severity: str | None = None  # informational, low, medium, high
    status: str | None = None  # new, inProgress, resolved
    category: str | None = None
    service_source: str | None = None
    created_date_time: datetime | None = None

    parse_dates = _dates("created_date_time")


class Incident(_Graph):
    id: str
    display_name: str = ""
    severity: str | None = None
    status: str | None = None  # active, resolved, inProgress, redirected
    created_date_time: datetime | None = None

    parse_dates = _dates("created_date_time")


class Credential(_Graph):
    display_name: str | None = None
    end_date_time: datetime | None = None

    parse_dates = _dates("end_date_time")


class Application(_Graph):
    id: str
    app_id: str | None = None
    display_name: str = ""
    password_credentials: list[Credential] = []
    key_credentials: list[Credential] = []


class AuditUser(_Graph):
    display_name: str | None = None
    user_principal_name: str | None = None


class AuditApp(_Graph):
    display_name: str | None = None


class InitiatedBy(_Graph):
    user: AuditUser | None = None
    app: AuditApp | None = None

    @property
    def name(self) -> str:
        if self.user and (self.user.user_principal_name or self.user.display_name):
            return self.user.user_principal_name or self.user.display_name or "?"
        if self.app and self.app.display_name:
            return f"app {self.app.display_name}"
        return "unknown"


class TargetResource(_Graph):
    display_name: str | None = None
    user_principal_name: str | None = None
    type: str | None = None


class DirectoryAudit(_Graph):
    id: str
    activity_date_time: datetime | None = None
    activity_display_name: str = ""
    category: str = ""  # UserManagement, RoleManagement, GroupManagement, ApplicationManagement, Policy, ...
    result: str | None = None
    initiated_by: InitiatedBy = InitiatedBy()
    target_resources: list[TargetResource] = []

    parse_dates = _dates("activity_date_time")


class ServiceHealth(_Graph):
    id: str | None = None
    service: str = ""
    status: str = ""  # serviceOperational, serviceDegradation, serviceInterruption, ...


class ServiceIssue(_Graph):
    id: str
    title: str = ""
    service: str = ""
    status: str | None = None
    classification: str | None = None  # incident, advisory
    is_resolved: bool = False
    start_date_time: datetime | None = None
    impact_description: str | None = None

    parse_dates = _dates("start_date_time")
