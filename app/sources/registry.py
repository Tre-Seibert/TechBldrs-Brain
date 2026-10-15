"""Which sources are enabled, from env: FLOW_MODE=stub|http (always on), DATTO_MODE=off|stub|http, M365_MODE=off|stub|http.

A new source adds its SourceSpec to KNOWN_SOURCES and a <NAME>_MODE setting defaulting to off.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.config import Settings
from app.sources.base import OFF_MODES, Sources, SourceSpec
from app.sources.datto.spec import DATTO
from app.sources.flow.spec import FLOW
from app.sources.m365.spec import M365

# In tool-list order. Flow first, so the default tool list is what it was before sources existed.
KNOWN_SOURCES: tuple[SourceSpec, ...] = (FLOW, DATTO, M365)


def build_sources(settings: Settings, specs: Iterable[SourceSpec] | None = None) -> Sources:
    """Build a client for every enabled source. A required source's own build decides a bad mode."""
    enabled = []
    for spec in KNOWN_SOURCES if specs is None else specs:
        if not spec.required and spec.mode(settings) in OFF_MODES:
            continue
        enabled.append((spec, spec.build(settings)))
    return Sources(enabled)
