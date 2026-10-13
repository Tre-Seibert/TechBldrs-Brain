"""Datto RMM adapters. Read-only: every method is a GET on the Datto RMM API v2 (or the stub's fixtures).

Nothing here can reboot a device, run a job or component, approve patches, resolve an alert or change a site,
user-defined field or variable; tb-brain has no tool for any of that.
"""

from __future__ import annotations

from typing import Protocol

from app.sources.datto.schemas import DattoAlert, DattoDevice, DattoSite, DeviceAudit, Software

# Alert lists: /alerts/open or /alerts/resolved.
ALERT_STATUSES = ("open", "resolved")


class DattoSource(Protocol):
    source_name: str

    # GET /v2/account/sites
    def list_sites(self) -> list[DattoSite]: ...

    # GET /v2/site/{siteUid}/devices
    def list_site_devices(self, *, site_uid: str) -> list[DattoDevice]: ...

    # GET /v2/account/devices?hostname= (Datto matches part of the hostname; callers pick exact matches).
    # hostname=None lists every device on the account.
    def list_account_devices(self, *, hostname: str | None = None) -> list[DattoDevice]: ...

    # GET /v2/device/{deviceUid}
    def get_device(self, *, device_uid: str) -> DattoDevice: ...

    # GET /v2/audit/device/{deviceUid}
    def get_device_audit(self, *, device_uid: str) -> DeviceAudit: ...

    # GET /v2/audit/device/{deviceUid}/software
    def list_device_software(self, *, device_uid: str) -> list[Software]: ...

    # GET /v2/site/{siteUid}/alerts/{open|resolved}
    def list_site_alerts(self, *, site_uid: str, status: str = "open") -> list[DattoAlert]: ...

    # GET /v2/device/{deviceUid}/alerts/{open|resolved}
    def list_device_alerts(self, *, device_uid: str, status: str = "open") -> list[DattoAlert]: ...

    # GET /v2/account/alerts/{open|resolved}
    def list_account_alerts(self, *, status: str = "open") -> list[DattoAlert]: ...


class DattoNotConfigured(RuntimeError):
    """DATTO_MODE=http but the API url, key or secret is missing, or Datto rejected them."""


class DattoUnavailable(RuntimeError):
    """Datto RMM did not answer: unreachable, timed out, rate limited past our retries, or a server error."""


class DattoRequestError(RuntimeError):
    """Datto answered but refused the request (400/403/404). The message is safe to show."""
