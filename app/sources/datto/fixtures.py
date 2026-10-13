"""Synthetic Datto RMM API v2 data for DATTO_MODE=stub: three sites, shaped like the API's JSON.

Sites are named the way TechBldrs names them, client code first ("WDON Western Dental"), so the client resolver
maps them to Flow's stub clients. Hostnames line up with Flow's stub where the cross-source questions need it:
WDON-IMG-01 is Flow's machine record and the subject of ticket WDON-1842 ("Imaging workstation offline"), and it
is offline here with an open alert; Debe Hernandez (Flow contact, WDON) is the last user on WDON-FD01. Flow's
WDON-FS-01 has no Datto device, so comparing the two lists finds it.

Dates are relative to `now` so "offline for 30 days" and "alerts this week" keep working. Names, users and
serial numbers are fake.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

GB = 1024**3

SITES: list[dict[str, Any]] = [
    {
        "id": 3101,
        "uid": "6d1f0b6e-3c8a-4f0e-9d2b-1a7c5e2f9b01",
        "accountUid": "tbld-acct",
        "name": "WDON Western Dental",
        "description": "Western Dental of New Jersey",
        "onDemand": False,
        "devicesStatus": {"numberOfDevices": 6, "numberOfOnlineDevices": 4, "numberOfOfflineDevices": 2},
    },
    {
        "id": 3102,
        "uid": "a42e7c19-58d3-4b6a-8e0f-2c9d1b3e7a02",
        "accountUid": "tbld-acct",
        "name": "BUCK Buckhead Pediatrics",
        "description": "",
        "onDemand": False,
        "devicesStatus": {"numberOfDevices": 5, "numberOfOnlineDevices": 4, "numberOfOfflineDevices": 1},
    },
    {
        "id": 3103,
        "uid": "f0c3d5a8-91b2-4e7d-a6c4-8b2e0f1d5c03",
        "accountUid": "tbld-acct",
        "name": "NGMC North Georgia Medical Clinic",
        "description": "",
        "onDemand": False,
        "devicesStatus": {"numberOfDevices": 5, "numberOfOnlineDevices": 3, "numberOfOfflineDevices": 2},
    },
]

_SITE = {site["name"].split(" ")[0]: site for site in SITES}

_WIN11 = "Microsoft Windows 11 Pro 10.0.22631"
_WIN10 = "Microsoft Windows 10 Pro 10.0.19045"
_AV = "Datto AV"


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _device(
    now: datetime,
    *,
    id: int,
    code: str,
    hostname: str,
    category: str,
    os: str,
    user: str | None,
    ip: str,
    online: bool,
    seen_ago: timedelta,
    reboot_ago: timedelta,
    patch: str,
    pending: int = 0,
    not_approved: int = 0,
    installed: int = 120,
    av: str = "RunningAndUpToDate",
    av_product: str | None = _AV,
    reboot_required: bool = False,
    warranty: str | None = None,
    description: str = "3s",
    udf: dict[str, str] | None = None,
) -> dict[str, Any]:
    site = _SITE[code]
    return {
        "id": id,
        "uid": f"{id:08x}-0d7e-4c1a-9b2f-{id:012x}",
        "siteId": site["id"],
        "siteUid": site["uid"],
        "siteName": site["name"],
        "deviceType": {"category": category, "type": category},
        "deviceClass": "device",
        "hostname": hostname,
        "intIpAddress": ip,
        "extIpAddress": "203.0.113.10",
        "operatingSystem": os,
        "lastLoggedInUser": user,
        "domain": code,
        "cagVersion": "4.4.2238.2238",
        "displayVersion": "4.4.2238",
        "description": description,
        "a64Bit": True,
        "rebootRequired": reboot_required,
        "online": online,
        "suspended": False,
        "deleted": False,
        "lastSeen": _iso(now - seen_ago),
        "lastReboot": _iso(now - reboot_ago),
        "lastAuditDate": _iso(now - seen_ago - timedelta(hours=2)),
        "creationDate": "2022-03-14T15:02:11Z",
        "snmpEnabled": False,
        "portalUrl": f"https://vidal.centrastage.net/csm/search?qs=uid%3A{id:08x}",
        "warrantyDate": warranty,
        "antivirus": {"antivirusProduct": av_product, "antivirusStatus": av},
        "patchManagement": {
            "patchStatus": patch,
            "patchesApprovedPending": pending,
            "patchesNotApproved": not_approved,
            "patchesInstalled": installed,
        },
        "softwareStatus": "Compliant",
        "udf": {"udf1": "Managed", "udf2": None, **(udf or {})},
    }


def devices(now: datetime) -> list[dict[str, Any]]:
    minutes, hours, days = (lambda n: timedelta(minutes=n)), (lambda n: timedelta(hours=n)), (lambda n: timedelta(days=n))
    return [
        # WDON: a dental office. The imaging PC is the one on Flow ticket WDON-1842.
        _device(now, id=50101, code="WDON", hostname="WDON-IMG-01", category="Desktop", os=_WIN10, user="WDON\\imaging",
                ip="10.41.0.31", online=False, seen_ago=days(3) + hours(4), reboot_ago=days(9), patch="InstallError",
                pending=3, installed=212, warranty="2025-08-30"),
        _device(now, id=50102, code="WDON", hostname="WDON-FD01", category="Desktop", os=_WIN11, user="WDON\\debe.hernandez",
                ip="10.41.0.21", online=True, seen_ago=minutes(2), reboot_ago=days(16), patch="ApprovedPending",
                pending=4, installed=188, av="RunningAndNotUpToDate", reboot_required=True, warranty="2027-02-11"),
        _device(now, id=50103, code="WDON", hostname="WDON-OP02", category="Desktop", os=_WIN11, user="WDON\\op2",
                ip="10.41.0.42", online=True, seen_ago=minutes(1), reboot_ago=days(2), patch="FullyPatched",
                installed=176, warranty="2027-02-11"),
        _device(now, id=50104, code="WDON", hostname="WDON-OP03", category="Desktop", os=_WIN10, user="WDON\\op3",
                ip="10.41.0.43", online=False, seen_ago=days(45), reboot_ago=days(52), patch="NoData", installed=140,
                av="NotRunning", warranty="2024-05-02"),
        _device(now, id=50105, code="WDON", hostname="WDON-SRV01", category="Server",
                os="Microsoft Windows Server 2019 Standard 10.0.17763", user="WDON\\administrator", ip="10.41.0.5",
                online=True, seen_ago=minutes(1), reboot_ago=days(27), patch="FullyPatched", installed=301,
                warranty="2026-11-30", description="3c"),
        _device(now, id=50106, code="WDON", hostname="WDON-LT01", category="Laptop", os=_WIN11, user="WDON\\sobrien",
                ip="10.41.0.77", online=True, seen_ago=minutes(6), reboot_ago=days(1), patch="FullyPatched",
                installed=164, warranty="2028-01-19"),
        # BUCK: a pediatric practice. Server behind on patches, one PC failing to install.
        _device(now, id=50201, code="BUCK", hostname="BUCK-SRV01", category="Server",
                os="Microsoft Windows Server 2016 Standard 10.0.14393", user="BUCK\\admin.tb", ip="192.168.20.5",
                online=True, seen_ago=minutes(1), reboot_ago=days(41), patch="ApprovedPending", pending=12,
                not_approved=2, installed=410, reboot_required=True, warranty="2025-03-31", description="3c"),
        _device(now, id=50202, code="BUCK", hostname="BUCK-PC01", category="Desktop", os=_WIN10, user="BUCK\\frontdesk",
                ip="192.168.20.21", online=True, seen_ago=minutes(3), reboot_ago=days(4), patch="FullyPatched",
                installed=198, warranty="2026-06-30"),
        _device(now, id=50203, code="BUCK", hostname="BUCK-PC02", category="Desktop", os=_WIN11, user="BUCK\\nurse1",
                ip="192.168.20.22", online=True, seen_ago=minutes(1), reboot_ago=days(6), patch="ApprovedPending",
                pending=6, installed=150, warranty="2027-09-15"),
        _device(now, id=50204, code="BUCK", hostname="BUCK-PC03", category="Desktop", os=_WIN10, user="BUCK\\billing",
                ip="192.168.20.23", online=True, seen_ago=minutes(9), reboot_ago=days(12), patch="InstallError",
                pending=2, installed=171, warranty="2026-06-30"),
        _device(now, id=50205, code="BUCK", hostname="BUCK-LT01", category="Laptop", os=_WIN11, user="BUCK\\dr.patel",
                ip="192.168.20.60", online=False, seen_ago=days(2) + hours(1), reboot_ago=days(3), patch="FullyPatched",
                installed=160, warranty="2027-12-01"),
        # NGMC: a medical clinic. Antivirus trouble and two machines offline.
        _device(now, id=50301, code="NGMC", hostname="NGMC-DC01", category="Server",
                os="Microsoft Windows Server 2022 Standard 10.0.20348", user="NGMC\\administrator", ip="10.8.1.10",
                online=True, seen_ago=minutes(1), reboot_ago=days(8), patch="FullyPatched", installed=233,
                warranty="2028-04-30", description="3c"),
        _device(now, id=50302, code="NGMC", hostname="NGMC-PC05", category="Desktop", os=_WIN11, user="NGMC\\reception",
                ip="10.8.1.105", online=True, seen_ago=minutes(4), reboot_ago=days(5), patch="FullyPatched",
                installed=171, av="NotDetected", av_product=None, warranty="2027-03-20"),
        _device(now, id=50303, code="NGMC", hostname="NGMC-PC07", category="Desktop", os=_WIN10, user="NGMC\\exam2",
                ip="10.8.1.107", online=False, seen_ago=days(8), reboot_ago=days(14), patch="ApprovedPending", pending=9,
                installed=133, av="RunningAndNotUpToDate", warranty="2025-01-15"),
        _device(now, id=50304, code="NGMC", hostname="NGMC-LT02", category="Laptop", os=_WIN11, user="NGMC\\np.jones",
                ip="10.8.1.140", online=False, seen_ago=hours(20), reboot_ago=days(2), patch="FullyPatched",
                installed=158, warranty="2028-02-10"),
        _device(now, id=50305, code="NGMC", hostname="NGMC-PC09", category="Desktop", os=_WIN11, user="NGMC\\billing2",
                ip="10.8.1.109", online=True, seen_ago=minutes(2), reboot_ago=days(19), patch="RebootRequired",
                installed=166, reboot_required=True, warranty="2027-03-20"),
    ]


def _alert(
    now: datetime,
    *,
    uid: str,
    device: dict[str, Any],
    priority: str,
    diagnostics: str,
    context: dict[str, Any],
    ago: timedelta,
    resolved_ago: timedelta | None = None,
    ticket: str | None = None,
) -> dict[str, Any]:
    return {
        "alertUid": uid,
        "priority": priority,
        "diagnostics": diagnostics,
        "resolved": resolved_ago is not None,
        "resolvedBy": "auto-resolve" if resolved_ago is not None else None,
        "resolvedOn": _iso(now - resolved_ago) if resolved_ago is not None else None,
        "muted": False,
        "ticketNumber": ticket,
        "timestamp": _iso(now - ago),
        "alertMonitorInfo": {"sendsEmails": True, "createsTicket": True},
        "alertContext": context,
        "alertSourceInfo": {
            "deviceUid": device["uid"],
            "deviceName": device["hostname"],
            "siteUid": device["siteUid"],
            "siteName": device["siteName"],
        },
        "responseActions": [],
        "autoresolveMins": 0,
    }


def alerts(now: datetime, device_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_host = {row["hostname"]: row for row in device_rows}
    hours, days = (lambda n: timedelta(hours=n)), (lambda n: timedelta(days=n))
    offline = {"@class": "online_offline_status_ctx", "status": "OFFLINE"}
    return [
        _alert(now, uid="al-wdon-0001", device=by_host["WDON-IMG-01"], priority="Critical",
               diagnostics="Device has been offline for more than 15 minutes.", context=offline, ago=days(3) + hours(4),
               ticket="WDON-1842"),
        _alert(now, uid="al-wdon-0002", device=by_host["WDON-SRV01"], priority="High",
               diagnostics="Disk C: is 92% full (14.1 GB free of 180 GB).",
               context={"@class": "perf_disk_usage_ctx", "diskName": "C:", "totalVolume": 180, "freeSpace": 14.1,
                        "unitOfMeasure": "GB", "diskNameDesignation": "C:"}, ago=hours(30)),
        _alert(now, uid="al-wdon-0003", device=by_host["WDON-FD01"], priority="Moderate",
               diagnostics="Antivirus definitions are out of date.",
               context={"@class": "antivirus_ctx", "status": "RunningAndNotUpToDate", "productName": _AV},
               ago=days(12), resolved_ago=days(11)),
        _alert(now, uid="al-buck-0001", device=by_host["BUCK-SRV01"], priority="Moderate",
               diagnostics="2 patches failed to install (KB5044293, KB5043051).",
               context={"@class": "patch_ctx", "patchUid": "KB5044293", "policyUid": "pp-buck", "result": "Failed",
                        "info": "0x80070643"}, ago=days(1)),
        _alert(now, uid="al-buck-0002", device=by_host["BUCK-SRV01"], priority="High",
               diagnostics="CPU above 95% for 20 minutes (sqlservr.exe).",
               context={"@class": "perf_resource_usage_ctx", "processName": "sqlservr.exe", "sample": 97, "type": "CPU"},
               ago=days(3), resolved_ago=days(3) - hours(1)),
        _alert(now, uid="al-buck-0003", device=by_host["BUCK-PC03"], priority="Low",
               diagnostics="Event 7031: Print Spooler service terminated unexpectedly.",
               context={"@class": "eventlog_ctx", "logName": "System", "code": "7031", "type": "Error",
                        "source": "Service Control Manager", "triggerCount": 3}, ago=days(20), resolved_ago=days(19)),
        _alert(now, uid="al-ngmc-0001", device=by_host["NGMC-PC07"], priority="High",
               diagnostics="Device has been offline for more than 24 hours.", context=offline, ago=days(7)),
        _alert(now, uid="al-ngmc-0002", device=by_host["NGMC-PC09"], priority="Critical",
               diagnostics="Threat quarantined: Trojan:Win32/Wacatac.B!ml in C:\\Users\\billing2\\Downloads.",
               context={"@class": "antivirus_ctx", "status": "ThreatDetected", "productName": _AV}, ago=hours(5)),
        _alert(now, uid="al-ngmc-0003", device=by_host["NGMC-PC05"], priority="Moderate",
               diagnostics="No antivirus product detected.",
               context={"@class": "antivirus_ctx", "status": "NotDetected", "productName": None}, ago=days(2)),
    ]


AUDITS: dict[str, dict[str, Any]] = {
    "WDON-IMG-01": {"manufacturer": "Dell Inc.", "model": "OptiPlex 7060", "serial": "7XK2LM2", "ram_gb": 16,
                    "cpu": "Intel(R) Core(TM) i7-8700 CPU @ 3.20GHz", "disks": [("C:", 476, 61)]},
    "WDON-FD01": {"manufacturer": "Dell Inc.", "model": "OptiPlex 7010", "serial": "9QF3TR4", "ram_gb": 16,
                  "cpu": "13th Gen Intel(R) Core(TM) i5-13500", "disks": [("C:", 476, 318)]},
    "WDON-OP02": {"manufacturer": "Dell Inc.", "model": "OptiPlex 7010", "serial": "9QF3TR7", "ram_gb": 16,
                  "cpu": "13th Gen Intel(R) Core(TM) i5-13500", "disks": [("C:", 476, 290)]},
    "WDON-OP03": {"manufacturer": "Dell Inc.", "model": "OptiPlex 5050", "serial": "3HJ8KD1", "ram_gb": 8,
                  "cpu": "Intel(R) Core(TM) i5-7500 CPU @ 3.40GHz", "disks": [("C:", 238, 22)]},
    "WDON-SRV01": {"manufacturer": "Dell Inc.", "model": "PowerEdge T340", "serial": "5RT9WX2", "ram_gb": 64,
                   "cpu": "Intel(R) Xeon(R) E-2234 CPU @ 3.60GHz", "disks": [("C:", 180, 14.1), ("D:", 1800, 960)]},
    "WDON-LT01": {"manufacturer": "Lenovo", "model": "ThinkPad T14 Gen 4", "serial": "PF4KQ8ZN", "ram_gb": 32,
                  "cpu": "13th Gen Intel(R) Core(TM) i7-1365U", "disks": [("C:", 953, 702)]},
    "BUCK-SRV01": {"manufacturer": "HPE", "model": "ProLiant ML350 Gen10", "serial": "MXQ93104KL", "ram_gb": 96,
                   "cpu": "Intel(R) Xeon(R) Silver 4210R CPU @ 2.40GHz", "disks": [("C:", 278, 41), ("E:", 3725, 1810)]},
    "BUCK-PC01": {"manufacturer": "HP", "model": "EliteDesk 800 G6", "serial": "MXL1203K9P", "ram_gb": 16,
                  "cpu": "Intel(R) Core(TM) i5-10500 CPU @ 3.10GHz", "disks": [("C:", 476, 233)]},
    "BUCK-PC02": {"manufacturer": "HP", "model": "EliteDesk 800 G9", "serial": "MXL3311B2Q", "ram_gb": 16,
                  "cpu": "12th Gen Intel(R) Core(TM) i5-12500", "disks": [("C:", 476, 380)]},
    "BUCK-PC03": {"manufacturer": "HP", "model": "EliteDesk 800 G6", "serial": "MXL1203K7Z", "ram_gb": 8,
                  "cpu": "Intel(R) Core(TM) i5-10500 CPU @ 3.10GHz", "disks": [("C:", 238, 9.6)]},
    "BUCK-LT01": {"manufacturer": "Lenovo", "model": "ThinkPad X1 Carbon Gen 11", "serial": "PF3ZK2MA", "ram_gb": 32,
                  "cpu": "13th Gen Intel(R) Core(TM) i7-1355U", "disks": [("C:", 953, 640)]},
    "NGMC-DC01": {"manufacturer": "Dell Inc.", "model": "PowerEdge R350", "serial": "C8FQ2M3", "ram_gb": 32,
                  "cpu": "Intel(R) Xeon(R) E-2314 CPU @ 2.80GHz", "disks": [("C:", 446, 301)]},
    "NGMC-PC05": {"manufacturer": "Dell Inc.", "model": "OptiPlex 7000", "serial": "4LMN8V3", "ram_gb": 16,
                  "cpu": "12th Gen Intel(R) Core(TM) i5-12500", "disks": [("C:", 476, 352)]},
    "NGMC-PC07": {"manufacturer": "Dell Inc.", "model": "OptiPlex 7070", "serial": "2BQ7HJ3", "ram_gb": 8,
                  "cpu": "Intel(R) Core(TM) i5-9500 CPU @ 3.00GHz", "disks": [("C:", 238, 48)]},
    "NGMC-LT02": {"manufacturer": "Dell Inc.", "model": "Latitude 5440", "serial": "8DXW4Y3", "ram_gb": 16,
                  "cpu": "13th Gen Intel(R) Core(TM) i5-1345U", "disks": [("C:", 476, 310)]},
    "NGMC-PC09": {"manufacturer": "Dell Inc.", "model": "OptiPlex 7000", "serial": "4LMN8V9", "ram_gb": 16,
                  "cpu": "12th Gen Intel(R) Core(TM) i5-12500", "disks": [("C:", 476, 299)]},
}


def audit(hostname: str) -> dict[str, Any]:
    """GET /v2/audit/device/{deviceUid}, shaped like the API (sizes in bytes)."""
    row = AUDITS[hostname]
    return {
        "portalUrl": "https://vidal.centrastage.net/csm/",
        "systemInfo": {"manufacturer": row["manufacturer"], "model": row["model"], "totalPhysicalMemory": row["ram_gb"] * GB,
                       "username": None, "dotNetVersion": "4.8.09032", "totalCpuCores": 6},
        "bios": {"instance": "Default System BIOS", "releaseDate": "2023-04-11", "serialNumber": row["serial"],
                 "smBiosVersion": "3.3"},
        "baseBoard": {"manufacturer": row["manufacturer"], "product": row["model"]},
        "logicalDisks": [
            {"description": "Local Fixed Disk", "diskIdentifier": disk, "size": int(size * GB), "freespace": int(free * GB)}
            for disk, size, free in row["disks"]
        ],
        "processors": [{"name": row["cpu"]}],
        "nics": [],
        "physicalMemory": [],
    }


_COMMON = [("Google Chrome", "129.0.6668.90"), ("Microsoft 365 Apps for business - en-us", "16.0.17928.20156"),
           ("Adobe Acrobat Reader (64-bit)", "24.003.20112"), ("Datto RMM", "4.4.2238.2238"),
           ("Datto AV", "3.2.1.18")]
SOFTWARE: dict[str, list[tuple[str, str]]] = {
    "WDON-IMG-01": [*_COMMON[1:], ("Dexis Imaging Suite", "10.4.2"), ("Google Chrome", "126.0.6478.127")],
    "WDON-FD01": [*_COMMON, ("Dentrix G7", "7.4.0.2"), ("Zoom Workplace", "6.2.5")],
    "WDON-OP02": [*_COMMON, ("Dentrix G7", "7.4.0.2"), ("Dexis Imaging Suite", "10.4.2")],
    "WDON-OP03": [*_COMMON[1:], ("Dentrix G6", "6.9.1.5"), ("Google Chrome", "118.0.5993.118")],
    "WDON-SRV01": [("Dentrix G7 Server", "7.4.0.2"), ("Microsoft SQL Server 2017 (64-bit)", "14.0.3465.1"),
                   ("Datto RMM", "4.4.2238.2238"), ("Datto AV", "3.2.1.18")],
    "WDON-LT01": [*_COMMON, ("Zoom Workplace", "6.2.5")],
    "BUCK-SRV01": [("Microsoft SQL Server 2019 (64-bit)", "15.0.4385.2"), ("athenaPractice Server", "22.4"),
                   ("Datto RMM", "4.4.2238.2238"), ("Datto AV", "3.2.1.18")],
    "BUCK-PC01": [*_COMMON, ("athenaPractice Client", "22.4")],
    "BUCK-PC02": [*_COMMON, ("athenaPractice Client", "22.4")],
    "BUCK-PC03": [*_COMMON[1:], ("athenaPractice Client", "22.2"), ("Google Chrome", "121.0.6167.140"),
                  ("QuickBooks Desktop Pro 2022", "32.0.4007.3207")],
    "BUCK-LT01": [*_COMMON, ("athenaPractice Client", "22.4"), ("Zoom Workplace", "6.2.5")],
    "NGMC-DC01": [("Datto RMM", "4.4.2238.2238"), ("Datto AV", "3.2.1.18")],
    "NGMC-PC05": [*_COMMON[:4], ("eClinicalWorks", "12.0.1")],
    "NGMC-PC07": [*_COMMON, ("eClinicalWorks", "11.52.3")],
    "NGMC-LT02": [*_COMMON, ("eClinicalWorks", "12.0.1")],
    "NGMC-PC09": [*_COMMON, ("eClinicalWorks", "12.0.1"), ("QuickBooks Desktop Pro 2022", "32.0.4007.3207")],
}


def software(hostname: str) -> list[dict[str, str]]:
    """GET /v2/audit/device/{deviceUid}/software rows."""
    return [{"name": name, "version": version} for name, version in SOFTWARE.get(hostname, [])]
