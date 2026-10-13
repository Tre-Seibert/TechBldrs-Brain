"""DATTO_MODE=stub: the Datto RMM protocol over in-process fixtures (app/sources/datto/fixtures.py)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from app.sources.datto import fixtures
from app.sources.datto.protocol import ALERT_STATUSES, DattoRequestError
from app.sources.datto.schemas import DattoAlert, DattoDevice, DattoSite, DeviceAudit, Software


def _now() -> datetime:
    return datetime.now(timezone.utc)


class StubDattoSource:
    source_name = "datto-stub"

    def __init__(self, *, now: Callable[[], datetime] = _now) -> None:
        # Pinned at construction, so one process sees one consistent picture.
        moment = now()
        self.now = moment
        self._sites = [DattoSite.model_validate(row) for row in fixtures.SITES]
        raw_devices = fixtures.devices(moment)
        self._devices = [DattoDevice.model_validate(row) for row in raw_devices]
        self._alerts = [DattoAlert.model_validate(row) for row in fixtures.alerts(moment, raw_devices)]

    def list_sites(self) -> list[DattoSite]:
        return list(self._sites)

    def list_site_devices(self, *, site_uid: str) -> list[DattoDevice]:
        if not any(site.uid == site_uid for site in self._sites):
            raise DattoRequestError(f"Datto RMM 404: site {site_uid} not found")
        return [device for device in self._devices if device.site_uid == site_uid]

    def list_account_devices(self, *, hostname: str | None = None) -> list[DattoDevice]:
        wanted = (hostname or "").strip().lower()
        return [device for device in self._devices if wanted in device.hostname.lower()]

    def get_device(self, *, device_uid: str) -> DattoDevice:
        return self._device(device_uid)

    def get_device_audit(self, *, device_uid: str) -> DeviceAudit:
        return DeviceAudit.model_validate(fixtures.audit(self._device(device_uid).hostname))

    def list_device_software(self, *, device_uid: str) -> list[Software]:
        return [Software.model_validate(row) for row in fixtures.software(self._device(device_uid).hostname)]

    def list_site_alerts(self, *, site_uid: str, status: str = "open") -> list[DattoAlert]:
        return [alert for alert in self._by_status(status) if alert.alert_source_info and alert.alert_source_info.site_uid == site_uid]

    def list_device_alerts(self, *, device_uid: str, status: str = "open") -> list[DattoAlert]:
        return [
            alert for alert in self._by_status(status) if alert.alert_source_info and alert.alert_source_info.device_uid == device_uid
        ]

    def list_account_alerts(self, *, status: str = "open") -> list[DattoAlert]:
        return self._by_status(status)

    def _by_status(self, status: str) -> list[DattoAlert]:
        if status not in ALERT_STATUSES:
            raise ValueError(f"alert status must be one of {', '.join(ALERT_STATUSES)}")
        return [alert for alert in self._alerts if alert.resolved == (status == "resolved")]

    def _device(self, device_uid: str) -> DattoDevice:
        hit = next((device for device in self._devices if device.uid == device_uid), None)
        if hit is None:
            raise DattoRequestError(f"Datto RMM 404: device {device_uid} not found")
        return hit
