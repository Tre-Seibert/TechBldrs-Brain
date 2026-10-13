"""Datto RMM API v2 records, parsed from the JSON the API returns (field names as in its OpenAPI spec, camelCase).

Only the fields tb-brain reads; everything else in a response is ignored. Dates are ISO date-time strings in the
spec; epoch milliseconds are accepted too. Nothing here is shown to the model as-is: the handlers summarize.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic.alias_generators import to_camel


class _Datto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


def _when(value: Any) -> datetime | None:
    """A Datto date: ISO date-time string or epoch milliseconds -> aware UTC datetime; None when absent."""
    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class DevicesStatus(_Datto):
    number_of_devices: int = 0
    number_of_online_devices: int = 0
    number_of_offline_devices: int = 0


class DattoSite(_Datto):
    id: int | None = None
    uid: str
    name: str = ""
    description: str | None = None
    on_demand: bool = False
    devices_status: DevicesStatus | None = None

    @property
    def client_code(self) -> str:
        """The site's client code: the first word of its name, as Flow matches it (_site_client_code)."""
        return site_client_code(self.name)


def site_client_code(site_name: str | None) -> str:
    return (site_name or "").strip().split(" ")[0].strip().upper()


class DeviceType(_Datto):
    category: str | None = None  # Desktop, Laptop, Server, ESXi Host, Printer, Network Device, ...
    type: str | None = None


class Antivirus(_Datto):
    antivirus_product: str | None = None
    # RunningAndUpToDate, RunningAndNotUpToDate, NotRunning, NotDetected
    antivirus_status: str | None = None


class PatchManagement(_Datto):
    # NoPolicy, NoData, RebootRequired, InstallError, ApprovedPending, FullyPatched
    patch_status: str | None = None
    patches_approved_pending: int | None = None
    patches_not_approved: int | None = None
    patches_installed: int | None = None


class DattoDevice(_Datto):
    id: int | None = None
    uid: str
    site_uid: str | None = None
    site_name: str | None = None
    device_type: DeviceType | None = None
    device_class: str | None = None  # device, printer, esxihost, rmmnetworkdevice, unknown
    hostname: str
    int_ip_address: str | None = None
    ext_ip_address: str | None = None
    operating_system: str | None = None
    last_logged_in_user: str | None = None
    domain: str | None = None
    description: str | None = None
    reboot_required: bool | None = None
    online: bool | None = None
    suspended: bool | None = None
    deleted: bool | None = None
    last_seen: datetime | None = None
    last_reboot: datetime | None = None
    last_audit_date: datetime | None = None
    creation_date: datetime | None = None
    warranty_date: str | None = None
    antivirus: Antivirus | None = None
    patch_management: PatchManagement | None = None
    software_status: str | None = None
    # User-defined fields. Not in the v2 Device schema, but returned by the API ("udf": {"udf1": ...}); V3 calls it udfs.
    udf: dict[str, str | None] | None = None

    @field_validator("last_seen", "last_reboot", "last_audit_date", "creation_date", mode="before")
    @classmethod
    def _date(cls, value: Any) -> datetime | None:
        return _when(value)

    @property
    def client_code(self) -> str:
        return site_client_code(self.site_name)

    @property
    def category(self) -> str:
        return (self.device_type.category if self.device_type else None) or "Unknown"


class AlertSourceInfo(_Datto):
    device_uid: str | None = None
    device_name: str | None = None
    site_uid: str | None = None
    site_name: str | None = None


class DattoAlert(_Datto):
    alert_uid: str
    priority: str | None = None  # Critical, High, Moderate, Low, Information, Unknown
    diagnostics: str | None = None
    resolved: bool = False
    resolved_by: str | None = None
    resolved_on: datetime | None = None
    muted: bool = False
    ticket_number: str | None = None
    timestamp: datetime | None = None
    # One of the spec's *Context schemas; "@class" says which (e.g. online_offline_status_ctx).
    alert_context: dict[str, Any] | None = None
    alert_source_info: AlertSourceInfo | None = None

    @field_validator("resolved_on", "timestamp", mode="before")
    @classmethod
    def _date(cls, value: Any) -> datetime | None:
        return _when(value)

    @property
    def hostname(self) -> str:
        return (self.alert_source_info.device_name if self.alert_source_info else None) or "unknown device"

    @property
    def client_code(self) -> str:
        return site_client_code(self.alert_source_info.site_name if self.alert_source_info else None)


class SystemInfo(_Datto):
    manufacturer: str | None = None
    model: str | None = None
    total_physical_memory: int | None = None  # bytes
    username: str | None = None
    total_cpu_cores: int | None = None


class Bios(_Datto):
    serial_number: str | None = None
    release_date: str | None = None
    sm_bios_version: str | None = None


class LogicalDisk(_Datto):
    description: str | None = None
    disk_identifier: str | None = None  # C:
    freespace: int | None = None  # bytes
    size: int | None = None  # bytes


class Processor(_Datto):
    name: str | None = None


class DeviceAudit(_Datto):
    system_info: SystemInfo | None = None
    bios: Bios | None = None
    logical_disks: list[LogicalDisk] = []
    processors: list[Processor] = []


class Software(_Datto):
    name: str
    version: str | None = None
