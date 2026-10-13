"""Datto's part of the client resolver (app/sources/clients.py): the client's Datto site.

A site belongs to a client when the first word of its name is the client code ("WDON Western Dental"), the
same rule Flow uses (Flow/app/services/datto_rmm.py _site_client_code).
"""

from __future__ import annotations

from app.sources.datto.protocol import DattoSource


def datto_site_uid(datto: DattoSource, client_code: str, name: str | None = None) -> str | None:
    """The uid of the client's Datto site (the first by name when it has several); None when it has none."""
    code = (client_code or "").strip().upper()
    sites = sorted((site for site in datto.list_sites() if site.client_code == code), key=lambda site: site.name)
    return sites[0].uid if sites else None
