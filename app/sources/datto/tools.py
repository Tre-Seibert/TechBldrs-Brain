"""Datto RMM tool names and OpenAI schemas. All read-only; names carry the datto_ prefix.

OPENAI_TOOLS go out with PROMPT_STYLE=legacy and LEAN_OPENAI_TOOLS with lean: same names and parameters, shorter
descriptions. There is no tool to reboot, run a job or component, approve or install patches, or resolve an
alert, and the descriptions say so, so the model declines those requests instead of looking for one.
"""

from __future__ import annotations

from typing import Any

from app.sources.base import function_tool

DATTO_LIST_DEVICES = "datto_list_devices"
DATTO_GET_DEVICE = "datto_get_device"
DATTO_ALERTS = "datto_alerts"
DATTO_SITE_SUMMARY = "datto_site_summary"
DATTO_PATCH_REPORT = "datto_patch_report"
DATTO_DEVICE_SOFTWARE = "datto_device_software"

TOOL_NAMES = (
    DATTO_LIST_DEVICES,
    DATTO_GET_DEVICE,
    DATTO_ALERTS,
    DATTO_SITE_SUMMARY,
    DATTO_PATCH_REPORT,
    DATTO_DEVICE_SOFTWARE,
)
# Every Datto reply is a summary made in code; it is shown as-is.
RELAY_TOOL_NAMES = TOOL_NAMES

_CLIENT = {"type": "string", "description": "Client code (e.g. WDON) or client name. 'all' means every client (slower)."}
_HOSTNAME = {"type": "string", "description": "Device hostname as the user wrote it, e.g. WDON-FD01."}
_READ_ONLY = " Read-only: it cannot reboot, run components or scripts, approve patches, or resolve alerts."

_LIST_DEVICES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": {
            **_CLIENT,
            "description": _CLIENT["description"] + " Needed unless last_user is given; then every client is searched.",
        },
        "online": {"type": "boolean", "description": "true: only online devices; false: only offline devices."},
        "offline_days": {
            "type": "integer",
            "description": "Only devices that have not checked in for at least this many days (stale agents).",
        },
        "device_type": {
            "type": "string",
            "enum": ["workstation", "desktop", "laptop", "server"],
            "description": "workstation means desktops and laptops.",
        },
        "os": {"type": "string", "description": "Text in the operating system, e.g. 'Windows 10', 'Server 2016'."},
        "needs_patches": {
            "type": "boolean",
            "description": "true: only devices with approved patches pending, install errors, or a patch reboot.",
        },
        "av_issue": {"type": "boolean", "description": "true: only devices whose antivirus is out of date, not running, or missing."},
        "reboot_required": {"type": "boolean", "description": "true: only devices that need a reboot."},
        "last_user": {"type": "string", "description": "Part of the last logged-in user's name, e.g. 'Debe'."},
        "limit": {"type": "integer", "description": "Max devices listed (default 40)."},
    },
}

_GET_DEVICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hostname": _HOSTNAME,
        "include": {
            "type": "array",
            "items": {"type": "string", "enum": ["hardware", "disks"]},
            "description": "hardware: make, model, serial number, RAM, CPU. disks: free space per drive.",
        },
    },
    "required": ["hostname"],
}

_ALERTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": _CLIENT,
        "hostname": {**_HOSTNAME, "description": "One device's alerts instead of a client's, e.g. BUCK-SRV01."},
        "status": {
            "type": "string",
            "enum": ["open", "resolved", "all"],
            "description": "Default open; with days, default all (open and resolved).",
        },
        "days": {"type": "integer", "description": "Only alerts raised in the last N days (this week = 7)."},
        "priority": {"type": "string", "enum": ["critical", "high", "moderate", "low", "information"]},
        "view": {
            "type": "string",
            "enum": ["list", "count_by_client"],
            "description": "count_by_client: open alert counts per client, most first (use with client_code=all).",
        },
    },
}

_SITE_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"client_code": _CLIENT},
    "required": ["client_code"],
}

_PATCH_REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": {**_CLIENT, "description": "Client code or name, or 'all' to rank every client (slower)."},
        "hostname": {**_HOSTNAME, "description": "One device's patch status instead of a client's, e.g. BUCK-SRV01."},
    },
}

_SOFTWARE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_code": {**_CLIENT, "description": "Search every device of this client (slower). Needs q."},
        "hostname": {**_HOSTNAME, "description": "One device's installed software."},
        "q": {"type": "string", "description": "Part of the software name, e.g. 'Dentrix', 'Chrome'."},
    },
}

OPENAI_TOOLS: list[dict[str, Any]] = [
    function_tool(
        DATTO_LIST_DEVICES,
        "Live list of a client's devices from Datto RMM: hostname, type, OS, online or offline, last seen, last "
        "user, patch and antivirus state. Filters: online, offline_days, device_type, os, needs_patches, av_issue, "
        "reboot_required, last_user. 'Which NGMC machines are offline?' -> client_code=NGMC, online=false. "
        "'What computer does Debe use?' -> last_user=Debe (with Debe's client code if known). 'Any BUCK machines on Windows 10?' -> "
        "os='Windows 10'. Counts are computed for you." + _READ_ONLY,
        _LIST_DEVICES_SCHEMA,
    ),
    function_tool(
        DATTO_GET_DEVICE,
        "One device from Datto RMM by hostname (no client code needed): online state, last seen, last reboot, "
        "last user, OS, IP, patch and antivirus state, warranty date and open alerts. include=hardware for serial "
        "number and model, include=disks for free disk space. 'Tell me about WDON-FD01', 'Who was last logged into "
        "X?', 'When was X last rebooted?'. Unknown hostnames come back with close matches." + _READ_ONLY,
        _GET_DEVICE_SCHEMA,
    ),
    function_tool(
        DATTO_ALERTS,
        "Datto RMM monitoring alerts for a client (client_code), every client (client_code=all) or one device "
        "(hostname), summarized by device and type. 'Any open alerts for WDON?' -> client_code=WDON. 'What alerts "
        "has BUCK-SRV01 had this week?' -> hostname=BUCK-SRV01, days=7. 'Which client has the most open alerts?' -> "
        "client_code=all, view=count_by_client. 'Any critical alerts right now?' -> client_code=all, "
        "priority=critical." + _READ_ONLY,
        _ALERTS_SCHEMA,
    ),
    function_tool(
        DATTO_SITE_SUMMARY,
        "Counts for a client's Datto RMM site: devices by type (workstations, servers), online and offline, "
        "operating systems, patch and antivirus problems, reboots needed, open alerts. 'How many workstations and "
        "servers does BUCK have?' -> client_code=BUCK.",
        _SITE_SUMMARY_SCHEMA,
    ),
    function_tool(
        DATTO_PATCH_REPORT,
        "Patch status from Datto RMM: devices by patch status with the worst ones named, for one client, "
        "client_code=all to rank every client worst first, or one device by hostname. 'Are WDON's machines "
        "patched?' -> client_code=WDON. "
        "'Which clients are worst on patching?' -> client_code=all." + _READ_ONLY,
        _PATCH_REPORT_SCHEMA,
    ),
    function_tool(
        DATTO_DEVICE_SOFTWARE,
        "Installed software from Datto RMM's software audit: one device (hostname, optional q), or which of a "
        "client's devices have a program (client_code and q; slower). 'Which WDON computers have Dentrix "
        "installed?' -> client_code=WDON, q=Dentrix. 'What version of Chrome is on BUCK-PC03?' -> "
        "hostname=BUCK-PC03, q=Chrome." + _READ_ONLY.replace("approve patches", "install or remove software"),
        _SOFTWARE_SCHEMA,
    ),
]

LEAN_OPENAI_TOOLS: list[dict[str, Any]] = [
    function_tool(
        DATTO_LIST_DEVICES,
        "A client's devices, live from Datto RMM, with filters (online=false for offline, needs_patches, av_issue, "
        "os, last_user, ...).",
        _LIST_DEVICES_SCHEMA,
    ),
    function_tool(
        DATTO_GET_DEVICE,
        "One device by hostname from Datto RMM: status, last seen and reboot, last user, patches, antivirus, "
        "alerts; include=hardware or disks for serial, model and disk space.",
        _GET_DEVICE_SCHEMA,
    ),
    function_tool(
        DATTO_ALERTS,
        "Datto RMM alerts for a client, every client (client_code=all) or one hostname. days=7 for this week; "
        "view=count_by_client to rank clients.",
        _ALERTS_SCHEMA,
    ),
    function_tool(
        DATTO_SITE_SUMMARY,
        "Device counts for a client from Datto RMM: by type, online/offline, OS, patch and AV problems, alerts.",
        _SITE_SUMMARY_SCHEMA,
    ),
    function_tool(
        DATTO_PATCH_REPORT,
        "Patch status from Datto RMM for a client, client_code=all to rank clients worst first, or one hostname.",
        _PATCH_REPORT_SCHEMA,
    ),
    function_tool(
        DATTO_DEVICE_SOFTWARE,
        "Installed software from Datto RMM: one hostname, or a client's devices that have q (slower).",
        _SOFTWARE_SCHEMA,
    ),
]

# B3: while Datto is enabled, Flow's list_machines says plainly that it is Flow's records, not live state.
LIST_MACHINES_DESCRIPTION = (
    "Flow's own machine records for a client (what is on file in Flow and on its tickets), not live data. "
    "'What machines do we have on record for client Y?' -> client_code=Y. For live state (online, offline, "
    "last seen, patches, antivirus, alerts, installed software) use the datto_ tools."
)
LEAN_LIST_MACHINES_DESCRIPTION = (
    "A client's machines as recorded in Flow (on file and on tickets), not live. Live device state is datto_ tools."
)
