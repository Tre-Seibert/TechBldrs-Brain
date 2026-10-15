"""Friendly names for common Microsoft 365 SKUs, by the part number Graph returns with each subscribed SKU.

Keyed by skuPartNumber rather than skuId: Graph sends both, and the part number is the readable, stable one. A SKU
not listed here shows its part number as-is. Names follow Microsoft's "Product names and service plan identifiers
for licensing" list.
"""

from __future__ import annotations

import re

SKU_NAMES: dict[str, str] = {
    "O365_BUSINESS_ESSENTIALS": "Microsoft 365 Business Basic",
    "O365_BUSINESS_PREMIUM": "Microsoft 365 Business Standard",
    "SPB": "Microsoft 365 Business Premium",
    "O365_BUSINESS": "Microsoft 365 Apps for business",
    "OFFICESUBSCRIPTION": "Microsoft 365 Apps for enterprise",
    "SPE_E3": "Microsoft 365 E3",
    "SPE_E5": "Microsoft 365 E5",
    "SPE_F1": "Microsoft 365 F3",
    "STANDARDPACK": "Office 365 E1",
    "ENTERPRISEPACK": "Office 365 E3",
    "ENTERPRISEPREMIUM": "Office 365 E5",
    "EXCHANGESTANDARD": "Exchange Online (Plan 1)",
    "EXCHANGEENTERPRISE": "Exchange Online (Plan 2)",
    "EXCHANGEDESKLESS": "Exchange Online Kiosk",
    "EXCHANGEARCHIVE_ADDON": "Exchange Online Archiving",
    "AAD_PREMIUM": "Microsoft Entra ID P1",
    "AAD_PREMIUM_P2": "Microsoft Entra ID P2",
    "EMS": "Enterprise Mobility + Security E3",
    "EMSPREMIUM": "Enterprise Mobility + Security E5",
    "INTUNE_A": "Microsoft Intune Plan 1",
    "ATP_ENTERPRISE": "Microsoft Defender for Office 365 (Plan 1)",
    "THREAT_INTELLIGENCE": "Microsoft Defender for Office 365 (Plan 2)",
    "MDE_SMB": "Microsoft Defender for Business",
    "DEFENDER_ENDPOINT_P1": "Microsoft Defender for Endpoint P1",
    "MDATP_XPLAT": "Microsoft Defender for Endpoint P2",
    "FLOW_FREE": "Microsoft Power Automate Free",
    "POWER_BI_STANDARD": "Microsoft Fabric (Free)",
    "POWER_BI_PRO": "Power BI Pro",
    "TEAMS_EXPLORATORY": "Microsoft Teams Exploratory",
    "VISIOCLIENT": "Visio Plan 2",
    "PROJECTPROFESSIONAL": "Project Plan 3",
}

# Free or self-service SKUs Microsoft provisions with thousands of seats; their "unused" seats are not waste.
FREE_SKUS = frozenset({"FLOW_FREE", "POWER_BI_STANDARD", "TEAMS_EXPLORATORY", "WINDOWS_STORE", "STREAM", "CCIBOTS_PRIVPREV_VIRAL"})


def sku_name(part_number: str | None) -> str:
    """'SPB' -> 'Microsoft 365 Business Premium'; an unlisted SKU shows its part number."""
    part = (part_number or "").strip()
    return SKU_NAMES.get(part.upper(), part or "unknown SKU")


def _key(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())


def sku_matches(part_number: str | None, wanted: str | None) -> bool:
    """True when what the user called a license ('business_premium', 'Business Premium', 'SPB', 'E3') names this SKU:
    the part number exactly, or every word of it in the friendly name."""
    if not (wanted or "").strip():
        return True
    want = _key(wanted)
    if want == _key(part_number or ""):
        return True
    words = want.split()
    have = _key(sku_name(part_number)).split()
    return all(word in have for word in words)
