from __future__ import annotations

from app.config import Settings
from app.flow.http import HttpFlowSource
from app.flow.source import FlowNotConfigured, FlowSource
from app.flow.stub import StubFlowSource


def build_flow_source(settings: Settings) -> FlowSource:
    mode = settings.flow_mode_normalized
    if mode in ("stub", "fixtures", "fake"):
        return StubFlowSource()
    if mode == "http":
        return HttpFlowSource(
            base_url=settings.flow_base_url,
            token=settings.flow_api_token,
        )
    if mode == "db":
        raise FlowNotConfigured(
            "FLOW_MODE=db is reserved for a SELECT-only MariaDB user. "
            "Not wired in phase 0 — use FLOW_MODE=stub."
        )
    raise FlowNotConfigured(f"Unknown FLOW_MODE={settings.flow_mode!r}")
