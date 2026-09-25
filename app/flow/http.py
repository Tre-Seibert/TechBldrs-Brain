"""HTTP adapter for Flow's brain API (PRIVATE_API_TOKEN).

  GET  /api/private/brain/contacts?q=&client_code=&limit=
  GET  /api/private/brain/technicians?q=&limit=
  GET  /api/private/brain/tickets?client_code=&assignee_code=&q=&ticket_num=&contact_id=&status=&sort=&order=&limit=
  GET  /api/private/brain/tickets/similar?ticket_id=&client_code=&assignee_code=&status=&limit=
  GET  /api/private/brain/mail?client_code=&direction=&email=&contact_id=&limit=
  POST /api/private/brain/tickets/merge   {target_ticket_id, source_ticket_ids, confirm: true}

The model must never issue SQL. Reads are SELECT-equivalent via HTTP.
latest_ticket is a tool, not a Flow route: it calls /tickets with sort=last_activity_at&limit=1.

X-Brain-Actor-Email is attached only from current_actor_email, which main.py
sets only for a verified Open WebUI JWT. Writes without it fail closed here,
before Flow's machine-token admin fallback could ever apply.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from app.flow.schemas import ContactRecord, MailRecord, SimilarTicketPair, TechnicianRecord, TicketRecord
from app.flow.source import FlowNotConfigured, FlowRequestError, FlowWriteRefused
from app.identity import current_actor_email

_HTML_DESCRIPTION_RE = re.compile(r"<p>(.*?)</p>", re.S)


def _flow_error_message(response: httpx.Response) -> str:
    """Flow's JSON 'error', or the <p> description of a werkzeug abort() page."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for key in ("error", "message", "description"):
            if payload.get(key):
                return str(payload[key])
    match = _HTML_DESCRIPTION_RE.search(response.text or "")
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()
    return (response.text or "").strip()[:300] or f"HTTP {response.status_code}"


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

    def _unwrap(self, response: httpx.Response, url: str) -> Any:
        if response.status_code == 401:
            raise FlowNotConfigured("Flow brain API rejected the token (401)")
        if response.status_code == 403:
            raise FlowRequestError(f"Flow refused the request (403): {_flow_error_message(response)}")
        if response.status_code in (400, 404):
            message = _flow_error_message(response)
            # werkzeug's stock 404 means the route itself is missing (Flow older than 1.3.17),
            # not a row-level "ticket not found".
            if response.status_code == 404 and message.startswith("The requested URL was not found"):
                raise FlowNotConfigured(f"Flow has no route at {url}. Is Flow on 1.3.17+?")
            raise FlowRequestError(f"Flow {response.status_code}: {message}")
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        self._require_ready()
        url = f"{self._base}{path}"
        try:
            response = httpx.get(url, headers=self._headers(), params=params, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise FlowNotConfigured(f"Flow brain API unreachable at {url}: {exc}") from exc
        return self._unwrap(response, url)

    def _post_as_actor(self, path: str, body: dict[str, Any]) -> Any:
        self._require_ready()
        if not current_actor_email.get():
            raise FlowWriteRefused(
                "No verified technician identity on this chat. Sign in to Open WebUI with your "
                "Microsoft account (signed JWT) before tb-brain can write to Flow as you."
            )
        url = f"{self._base}{path}"
        try:
            response = httpx.post(url, headers=self._headers(), json=body, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise FlowNotConfigured(f"Flow brain API unreachable at {url}: {exc}") from exc
        return self._unwrap(response, url)

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

    def search_technician(self, *, query: str, limit: int = 25) -> list[TechnicianRecord]:
        data = self._get("/api/private/brain/technicians", {"q": query, "limit": limit})
        rows = data if isinstance(data, list) else []
        return [TechnicianRecord.model_validate(row) for row in rows]

    def list_tickets(
        self,
        *,
        client_code: str | None = None,
        assignee_code: str | None = None,
        query: str | None = None,
        contact_id: int | None = None,
        status: str | None = None,
        ticket_num: str | None = None,
        category: str | None = None,
        reason: str | None = None,
        complete: bool | None = None,
        project: bool | None = None,
        machine_name: str | None = None,
        invoice_num: str | None = None,
        job: str | None = None,
        cause: str | None = None,
        overdue: bool | None = None,
        due_before: str | None = None,
        due_after: str | None = None,
        created_before: str | None = None,
        created_after: str | None = None,
        last_activity_before: str | None = None,
        last_activity_after: str | None = None,
        stage: str | None = None,
        sort: str = "last_activity_at",
        order: str = "desc",
        limit: int = 100,
    ) -> list[TicketRecord]:
        params: dict[str, Any] = {"sort": sort, "order": order, "limit": limit}
        optional = {
            "client_code": client_code,
            "assignee_code": assignee_code,
            "q": query,
            "contact_id": contact_id,
            "status": status,
            "ticket_num": ticket_num,
            "category": category,
            "reason": reason,
            "complete": complete,
            "project": project,
            "machine_name": machine_name,
            "invoice_num": invoice_num,
            "job": job,
            "cause": cause,
            "overdue": overdue,
            "due_before": due_before,
            "due_after": due_after,
            "created_before": created_before,
            "created_after": created_after,
            "last_activity_before": last_activity_before,
            "last_activity_after": last_activity_after,
            "stage": stage,
        }
        params.update({key: value for key, value in optional.items() if value not in (None, "")})
        data = self._get("/api/private/brain/tickets", params)
        rows = data if isinstance(data, list) else []
        return [TicketRecord.model_validate(row) for row in rows]

    def find_similar_tickets(
        self,
        *,
        ticket_id: int | None = None,
        client_code: str | None = None,
        assignee_code: str | None = None,
        status: str | None = None,
        stage: str | None = None,
        limit: int = 20,
    ) -> list[SimilarTicketPair]:
        params: dict[str, Any] = {"limit": limit}
        optional = {
            "ticket_id": ticket_id,
            "client_code": client_code,
            "assignee_code": assignee_code,
            "status": status,
            "stage": stage,
        }
        params.update({key: value for key, value in optional.items() if value not in (None, "")})
        data = self._get("/api/private/brain/tickets/similar", params)
        rows = data if isinstance(data, list) else []
        return [SimilarTicketPair.model_validate(row) for row in rows]

    def merge_tickets(self, *, target_ticket_id: int, source_ticket_ids: list[int]) -> dict[str, Any]:
        data = self._post_as_actor(
            "/api/private/brain/tickets/merge",
            {"target_ticket_id": target_ticket_id, "source_ticket_ids": source_ticket_ids, "confirm": True},
        )
        return data if isinstance(data, dict) else {"result": data}

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
