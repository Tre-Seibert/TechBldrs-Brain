"""Datto RMM as a SourceSpec. Off unless DATTO_MODE=stub|http; offered per question by source routing."""

from __future__ import annotations

from app.sources.base import SourceSpec
from app.sources.datto.clients import datto_site_uid
from app.sources.datto.factory import build_datto_source
from app.sources.datto.handlers.common import HOSTNAME_RE
from app.sources.datto.handlers.dispatch import handle
from app.sources.datto.prompt import PROMPT_SECTION
from app.sources.datto.protocol import DattoNotConfigured, DattoRequestError, DattoUnavailable
from app.sources.datto.tools import (
    LEAN_LIST_MACHINES_DESCRIPTION,
    LEAN_OPENAI_TOOLS,
    LIST_MACHINES_DESCRIPTION,
    OPENAI_TOOLS,
    RELAY_TOOL_NAMES,
)
from app.sources.datto.wording import WORDING, WRITE_WORDS
from app.sources.flow.tools import LIST_MACHINES

DATTO = SourceSpec(
    name="datto",
    mode=lambda settings: settings.datto_mode_normalized,
    build=build_datto_source,
    tools=tuple(OPENAI_TOOLS),
    handle=handle,
    relay_tool_names=RELAY_TOOL_NAMES,
    errors=(DattoNotConfigured, DattoUnavailable, DattoRequestError),
    prompt_section=PROMPT_SECTION,
    lean_tools=tuple(LEAN_OPENAI_TOOLS),
    client_id_hooks=(("datto_site_uid", datto_site_uid),),
    description=(
        "Datto RMM: live device state per client site (online/offline, last seen, last user, OS, patches, "
        "antivirus, alerts, installed software, hardware, disk space)."
    ),
    # Each matches its plural and -ing form too (devices, patches, patching, alerting, rebooting, warranties; see
    # app/agent/routing.py). -ed forms are listed where they mean a device ("patched", "rebooted"), not for
    # "alert", where "alerted the client" is a Flow question.
    keywords=(
        "Datto", "RMM", "device", "computer", "PC", "server", "workstation", "laptop", "desktop", "offline",
        "online", "patch", "patched", "unpatched", "antivirus", "anti-virus", "AV", "alert", "reboot", "rebooted",
        "restart", "restarted", "installed", "installed software", "software", "disk space", "free space",
        "serial number", "warranty", "last logged", "logged in", "checked in", "check in", "checking in",
        "operating system", "Windows 10", "Windows 11", "Windows Server",
    ),
    patterns=(HOSTNAME_RE,),
    wording=WORDING,
    write_words=WRITE_WORDS,
    wants_clients=True,
    describes=((LIST_MACHINES, LIST_MACHINES_DESCRIPTION, LEAN_LIST_MACHINES_DESCRIPTION),),
)
