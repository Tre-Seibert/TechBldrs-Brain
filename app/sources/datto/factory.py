from __future__ import annotations

from app.config import Settings
from app.sources.datto.http import HttpDattoSource
from app.sources.datto.protocol import DattoNotConfigured, DattoSource
from app.sources.datto.stub import StubDattoSource


def build_datto_source(settings: Settings) -> DattoSource:
    mode = settings.datto_mode_normalized
    if mode == "stub":
        return StubDattoSource()
    if mode == "http":
        # Missing settings are reported on the first call (DattoNotConfigured), not at startup, like Flow's http.
        return HttpDattoSource(
            base_url=settings.datto_api_url,
            api_key=settings.datto_api_key.get_secret_value(),
            api_secret=settings.datto_api_secret.get_secret_value(),
        )
    raise DattoNotConfigured(f"Unknown DATTO_MODE={settings.datto_mode!r}")
