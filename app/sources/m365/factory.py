from __future__ import annotations

from app.config import Settings
from app.sources.m365.http import HttpM365Source
from app.sources.m365.protocol import M365NotConfigured, M365Source
from app.sources.m365.stub import StubM365Source


def build_m365_source(settings: Settings) -> M365Source:
    mode = settings.m365_mode_normalized
    if mode == "stub":
        return StubM365Source()
    if mode == "http":
        # Missing settings are reported on the first call (M365NotConfigured), not at startup, like Datto's http.
        return HttpM365Source(
            client_id=settings.m365_client_id,
            client_secret=settings.m365_client_secret.get_secret_value(),
        )
    raise M365NotConfigured(f"Unknown M365_MODE={settings.m365_mode!r}")
