"""Which sources are enabled, from env: FLOW_MODE=stub|http (always on), DATTO_MODE / M365_MODE (reserved).

A new source adds its SourceSpec to KNOWN_SOURCES and a <NAME>_MODE setting defaulting to off.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from app.config import Settings
from app.sources.base import OFF_MODES, Sources, SourceSpec
from app.sources.flow.spec import FLOW

_log = logging.getLogger("tb_brain.sources")

# In tool-list order. Flow first, so the default tool list is what it was before sources existed.
KNOWN_SOURCES: tuple[SourceSpec, ...] = (FLOW,)

# Settings for sources not built yet (Phase B: Datto RMM, Phase C: Microsoft 365). Off unless set.
RESERVED_MODES = {"DATTO_MODE": "datto_mode", "M365_MODE": "m365_mode"}


def build_sources(settings: Settings, specs: Iterable[SourceSpec] | None = None) -> Sources:
    """Build a client for every enabled source. A required source's own build decides a bad mode."""
    for env_name, field in RESERVED_MODES.items():
        mode = (getattr(settings, field, "") or "").strip().lower()
        if mode not in OFF_MODES:
            _log.warning("%s=%s is reserved; that source is not built yet, so it stays off", env_name, mode)
    enabled = []
    for spec in KNOWN_SOURCES if specs is None else specs:
        if not spec.required and spec.mode(settings) in OFF_MODES:
            continue
        enabled.append((spec, spec.build(settings)))
    return Sources(enabled)
