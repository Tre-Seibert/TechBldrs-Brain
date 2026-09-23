"""HTTP adapter for Flow's read-only brain API (PRIVATE_API_TOKEN).

  GET /api/private/brain/contacts?q=&client_code=&limit=
  GET /api/private/brain/tickets?client_code=&contact_id=&status=&sort=&order=&limit=
  GET /api/private/brain/mail?client_code=&direction=&email=&contact_id=&limit=

The model must never issue SQL. This client is SELECT-equivalent via HTTP.
latest_ticket is a tool, not a Flow route: it calls /tickets with sort=last_activity_at&limit=1.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.flow.schemas import ContactRecord, MailRecord, TicketRecord
from app.flow.source import FlowNotConfigured
from app.identity import current_actor_email


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
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        actor_email = current_actor_email.get()
        if actor_email:
            headers["X-Brain-Actor-Email"] = actor_email
        return headers

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
        params: dict[str, Any] = {
            "client_code": client_code,
            "sort": "last_activity_at",
            "order": "desc",
            "limit": 1,
        }
        if contact_id is not None:
            params["contact_id"] = contact_id
        if status:
            params["status"] = status
        data = self._get("/api/private/brain/tickets", params)
        rows = data if isinstance(data, list) else []
        if not rows:
            return None
        return TicketRecord.model_validate(rows[0])

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
