from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger("tb_brain.audit")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_tool_call(
    *,
    actor: str,
    tool: str,
    client_code: str | None,
    row_ids: list[int] | tuple[int, ...] | None,
    args: dict[str, Any],
    source: str,
    ok: bool,
    error: str | None = None,
    log_dir: Path | None = None,
) -> None:
    """Append one JSON line per tool call. Bodies and secrets are not logged."""
    payload = {
        "ts": _now_iso(),
        "event": "tool_call",
        "actor": actor,
        "tool": tool,
        "client_code": (client_code or "").upper() or None,
        "row_ids": list(row_ids or []),
        "args": _safe_args(args),
        "source": source,
        "ok": ok,
        "error": error,
    }
    line = json.dumps(payload, default=str)
    extra = {
        "event": "tool_call",
        "actor": actor,
        "tool": tool,
        "client_code": payload["client_code"],
        "row_ids": payload["row_ids"],
        "source": source,
        "ok": ok,
    }
    if ok:
        _log.info("tool_call %s", tool, extra=extra)
    else:
        _log.warning("tool_call_failed %s: %s", tool, error, extra=extra)

    if log_dir is not None:
        path = log_dir / "tool_calls.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _safe_args(args: dict[str, Any]) -> dict[str, Any]:
    blocked = {"password", "secret", "token", "api_key", "authorization", "body"}
    out: dict[str, Any] = {}
    for key, value in args.items():
        if key.lower() in blocked:
            out[key] = "[redacted]"
        else:
            out[key] = value
    return out
