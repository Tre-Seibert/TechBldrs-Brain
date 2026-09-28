"""Sync IT Glue SOP/runbook documents and Flow time-entry fix notes into Qdrant
for the search_knowledge tool. Run manually or on a schedule (Task Scheduler);
there is no live/webhook sync in this phase.

Usage (from repo root, after `. .\\scripts\\dev_venv.ps1`):
    python scripts\\sync_knowledge.py            # IT Glue docs (full) + time entries (incremental)
    python scripts\\sync_knowledge.py --full      # also resync all time entries from scratch
    python scripts\\sync_knowledge.py --skip-itglue
    python scripts\\sync_knowledge.py --skip-time-entries

Requires FLOW_MODE=http (FLOW_BASE_URL + FLOW_API_TOKEN) and QDRANT_URL set in .env.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.knowledge.factory import build_knowledge_source  # noqa: E402
from app.knowledge.source import KnowledgePoint  # noqa: E402

_MAX_CHUNK_CHARS = 1500
_UPSERT_BATCH = 20


def _chunk_text(text: str, *, max_chars: int = _MAX_CHUNK_CHARS) -> list[str]:
    """Paragraph-aware chunking: pack paragraphs up to max_chars, split any single
    paragraph that alone exceeds it. Good enough for SOP-length documents; this
    is ingestion-only and never runs against a live request."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}".strip() if current else para
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(para) <= max_chars:
            current = para
        else:
            for start in range(0, len(para), max_chars):
                chunks.append(para[start : start + max_chars])
    if current:
        chunks.append(current)
    return chunks


def _flow_get(client: httpx.Client, path: str, params: dict[str, Any]) -> Any:
    response = client.get(path, params=params)
    response.raise_for_status()
    payload = response.json()
    return payload.get("data") if isinstance(payload, dict) else payload


def sync_itglue_documents(flow: httpx.Client, knowledge) -> int:
    clients = _flow_get(flow, "/api/private/brain/clients", {}) or []
    total_points = 0
    for client_row in clients:
        if not client_row.get("has_itglue"):
            continue
        code = client_row["client_code"]
        try:
            docs = _flow_get(flow, "/api/private/brain/itglue/documents", {"client_code": code}) or []
        except httpx.HTTPStatusError as exc:
            print(f"  {code}: skipped ({exc.response.status_code})")
            continue
        points: list[KnowledgePoint] = []
        for doc in docs:
            for index, chunk in enumerate(_chunk_text(doc["content"])):
                points.append(
                    KnowledgePoint(
                        id=f"itglue:{code}:{doc['id']}:{index}",
                        text=chunk,
                        source_type="itglue_document",
                        source_label=doc.get("name") or "(untitled document)",
                        client_code=code,
                        updated_at=doc.get("updated_at"),
                        url=doc.get("resource_url") or None,
                    )
                )
        for start in range(0, len(points), _UPSERT_BATCH):
            knowledge.upsert(points[start : start + _UPSERT_BATCH])
        total_points += len(points)
        print(f"  {code}: {len(docs)} document(s), {len(points)} chunk(s)")
    return total_points


def sync_time_entries(
    flow: httpx.Client, knowledge, *, cursor: str | None, cursor_id: int | None
) -> tuple[int, str | None, int | None]:
    """Pages via a compound (updated_at, id) cursor.

    updated_at alone is not a safe cursor: many rows can share the exact same
    timestamp (bulk-imported/migrated data), so advancing only by updated_at can
    re-fetch the same page forever once a tie group exceeds one page. after_id
    breaks the tie. Flow returns rows ordered (updated_at asc, id asc), so the
    next cursor is always the last row's (updated_at, id).
    """
    total_points = 0
    latest_seen = cursor
    latest_id = cursor_id
    while True:
        params: dict[str, Any] = {"limit": 100}
        if latest_seen:
            params["updated_after"] = latest_seen
        if latest_id is not None:
            params["after_id"] = latest_id
        rows = _flow_get(flow, "/api/private/brain/time-entries/export", params) or []
        if not rows:
            break
        points: list[KnowledgePoint] = []
        for row in rows:
            text = "\n".join(part for part in (row.get("subject"), row.get("body")) if (part or "").strip())
            if text.strip():
                points.append(
                    KnowledgePoint(
                        id=f"timeentry:{row['id']}",
                        text=text,
                        source_type="time_entry",
                        source_label=row.get("ticket_label") or f"ticket {row.get('ticket_id')}",
                        client_code=row.get("client_code"),
                        updated_at=row.get("updated_at"),
                    )
                )
        for start in range(0, len(points), _UPSERT_BATCH):
            knowledge.upsert(points[start : start + _UPSERT_BATCH])
        total_points += len(points)
        last_row = rows[-1]
        new_seen, new_id = last_row.get("updated_at"), last_row.get("id")
        print(f"  page: {len(rows)} entrie(s), {len(points)} synced, cursor now {new_seen} (id>{new_id})")
        if (new_seen, new_id) == (latest_seen, latest_id):
            break  # safety net: no forward progress, stop instead of looping forever
        latest_seen, latest_id = new_seen, new_id
        if len(rows) < 100:
            break
    return total_points, latest_seen, latest_id


def _state_path(brain_data_dir: Path) -> Path:
    return brain_data_dir / "knowledge_sync_state.json"


def _load_cursor(path: Path) -> tuple[str | None, int | None]:
    if not path.exists():
        return None, None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, None
    return state.get("time_entries_cursor"), state.get("time_entries_cursor_id")


def _save_cursor(path: Path, cursor: str | None, cursor_id: int | None) -> None:
    if cursor is None:
        return
    path.write_text(
        json.dumps({"time_entries_cursor": cursor, "time_entries_cursor_id": cursor_id}), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="Resync all time entries from scratch")
    parser.add_argument("--skip-itglue", action="store_true")
    parser.add_argument("--skip-time-entries", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    if settings.flow_mode_normalized != "http":
        print(f"FLOW_MODE={settings.flow_mode} — set FLOW_MODE=http and FLOW_API_TOKEN in .env first.")
        raise SystemExit(1)
    knowledge = build_knowledge_source(settings)
    if not knowledge.configured:
        print("QDRANT_URL is empty — set it in .env first (see docker-compose.yml).")
        raise SystemExit(1)

    started = datetime.now()
    with httpx.Client(
        base_url=settings.flow_base_url,
        headers={"Authorization": f"Bearer {settings.flow_api_token}"},
        timeout=30.0,
    ) as flow:
        if not args.skip_itglue:
            print("Syncing IT Glue documents...")
            count = sync_itglue_documents(flow, knowledge)
            print(f"IT Glue: {count} chunk(s) synced.")

        if not args.skip_time_entries:
            print("Syncing time entries...")
            state_path = _state_path(settings.brain_data_dir)
            cursor, cursor_id = (None, None) if args.full else _load_cursor(state_path)
            count, next_cursor, next_cursor_id = sync_time_entries(
                flow, knowledge, cursor=cursor, cursor_id=cursor_id
            )
            _save_cursor(state_path, next_cursor, next_cursor_id)
            print(f"Time entries: {count} chunk(s) synced. Next cursor: {next_cursor} (id>{next_cursor_id})")

    print(f"Done in {(datetime.now() - started).total_seconds():.1f}s.")


if __name__ == "__main__":
    main()
