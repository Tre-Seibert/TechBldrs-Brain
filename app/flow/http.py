"""HTTP adapter for a future read-only Flow brain API.

Not wired in phase 0. Intended Flow routes (PRIVATE_API_TOKEN / api_key):

  GET /api/private/brain/contacts?q=&client_code=&limit=
  GET /api/private/brain/tickets/latest?client_code=&contact_id=&status=
  GET /api/private/brain/mail?client_code=&direction=&email=&contact_id=&limit=

The model must never issue SQL. This client is SELECT-equivalent via HTTP.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.flow.schemas import ContactRecord, MailRecord, TicketRecord
from app.flow.source import FlowNotConfigured


class HttpFlowSource:
    source_name = "http"

    def __init__(self, *, base_url: str, token: str, timeout: float = 30.0) -> None:
        self._base = (base_url or "").rstrip("/")
        self._token = (token or "").strip()
        self._timeout = timeout

    def _require_ready(self) -> None:
        if not self._base:
            raise FlowNotConfigured("FLOW_BASE_URL is empty")
        if not self._token:
            raise FlowNotConfigured(
                "FLOW_MODE=http but FLOW_API_TOKEN is empty. Stay on FLOW_MODE=stub "
                "until a read-only Flow brain API is wired."
            )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        self._require_ready()
        url = f"{self._base}{path}"
        try:
            response = httpx.get(url, headers=self._headers(), params=params, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise FlowNotConfigured(f"Flow brain API unreachable at {url}: {exc}") from exc
        if response.status_code in (401, 403):
            raise FlowNotConfigured("Flow brain API rejected the token (401/403)")
        if response.status_code == 404:
            raise FlowNotConfigured(
                "Flow has no /api/private/brain/* routes yet. Keep FLOW_MODE=stub."
            )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def search_contact(
        self,
        *,
        query: str,
        client_code: str | None = None,
        limit: int = 25,
    ) -> list[ContactRecord]:
        data = self._get(
            "/api/private/brain/contacts",
            {"q": query, "client_code": client_code or "", "limit": limit},
        )
        rows = data if isinstance(data, list) else []
        return [ContactRecord.model_validate(row) for row in rows]

    def latest_ticket(
        self,
        *,
        client_code: str,
        contact_id: int | None = None,
        status: str | None = None,
    ) -> TicketRecord | None:
        params: dict[str, Any] = {"client_code": client_code}
        if contact_id is not None:
            params["contact_id"] = contact_id
        if status:
            params["status"] = status
        data = self._get("/api/private/brain/tickets/latest", params)
        if not data:
            return None
        return TicketRecord.model_validate(data)

    def list_mail(
        self,
        *,
        client_code: str,
        direction: str = "inbound",
        email: str | None = None,
        contact_id: int | None = None,
        limit: int = 25,
    ) -> list[MailRecord]:
        params: dict[str, Any] = {
            "client_code": client_code,
            "direction": direction,
            "limit": limit,
        }
        if email:
            params["email"] = email
        if contact_id is not None:
            params["contact_id"] = contact_id
        data = self._get("/api/private/brain/mail", params)
        rows = data if isinstance(data, list) else []
        return [MailRecord.model_validate(row) for row in rows]
