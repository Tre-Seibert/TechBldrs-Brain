"""Old import path. Flow now lives in app/sources/flow/; each module here is an alias for the one there."""

from __future__ import annotations

from app.sources.flow.stub import StubFlowSource

__all__ = ["StubFlowSource"]
