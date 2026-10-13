"""Datto tool name + raw arguments -> the handler that runs it.

Datto being down, rate limiting, or not configured comes back as a reply the user can read, not a stack of
errors: the model is told plainly that Datto RMM could not be reached, and nothing is guessed.
"""

from __future__ import annotations

from typing import Any

from app.knowledge.source import KnowledgeSource
from app.sources.clients import ClientResolver
from app.sources.datto.handlers.alerts import AlertsArgs, alerts
from app.sources.datto.handlers.common import HOSTNAME_RE, refuse
from app.sources.datto.handlers.devices import (
    GetDeviceArgs,
    ListDevicesArgs,
    SiteSummaryArgs,
    get_device,
    list_devices,
    site_summary,
)
from app.sources.datto.handlers.patches import PatchReportArgs, patch_report
from app.sources.datto.handlers.software import DeviceSoftwareArgs, device_software
from app.sources.datto.protocol import DattoNotConfigured, DattoRequestError, DattoSource, DattoUnavailable
from app.sources.datto.tools import (
    DATTO_ALERTS,
    DATTO_DEVICE_SOFTWARE,
    DATTO_GET_DEVICE,
    DATTO_LIST_DEVICES,
    DATTO_PATCH_REPORT,
    DATTO_SITE_SUMMARY,
)
from app.tools.base import ChatTurn, ToolResult


def handle(
    datto: DattoSource,
    name: str,
    raw_args: dict[str, Any],
    turn: ChatTurn | None = None,
    *,
    knowledge: KnowledgeSource | None = None,
    clients: ClientResolver,
) -> ToolResult | None:
    """Run one Datto tool. None when `name` is not a Datto tool."""
    try:
        return _run(datto, clients, name, raw_args)
    except DattoUnavailable as exc:
        return refuse(name, datto, f"Datto RMM could not be reached, so I can't check that right now. {exc}")
    except DattoNotConfigured as exc:
        return refuse(name, datto, f"Datto RMM is not set up in tb-brain: {exc}")
    except DattoRequestError as exc:
        return refuse(name, datto, str(exc))


def _hostname_as_client(raw_args: dict[str, Any]) -> dict[str, Any]:
    """A hostname sent as the client ({"client_code": "BUCK-SRV01"}) and no hostname: it is the hostname. Client
    codes and names have no dash-letters-number shape, so the meaning is not in doubt."""
    code = str(raw_args.get("client_code") or "").strip()
    if code and not str(raw_args.get("hostname") or "").strip() and HOSTNAME_RE.fullmatch(code):
        return {**{key: value for key, value in raw_args.items() if key != "client_code"}, "hostname": code}
    return raw_args


def _run(datto: DattoSource, clients: ClientResolver, name: str, raw_args: dict[str, Any]) -> ToolResult | None:
    raw_args = _hostname_as_client(raw_args)
    if name == DATTO_LIST_DEVICES:
        return list_devices(datto, clients, ListDevicesArgs.model_validate(raw_args))
    if name == DATTO_GET_DEVICE:
        return get_device(datto, GetDeviceArgs.model_validate(raw_args))
    if name == DATTO_ALERTS:
        return alerts(datto, clients, AlertsArgs.model_validate(raw_args))
    if name == DATTO_SITE_SUMMARY:
        return site_summary(datto, clients, SiteSummaryArgs.model_validate(raw_args))
    if name == DATTO_PATCH_REPORT:
        return patch_report(datto, clients, PatchReportArgs.model_validate(raw_args))
    if name == DATTO_DEVICE_SOFTWARE:
        return device_software(datto, clients, DeviceSoftwareArgs.model_validate(raw_args))
    return None
