"""How the agent loop names Datto RMM in its nudge and fallback messages (app/agent/wording.py), and which
requests are writes it declines rather than data questions it must answer from a tool.
"""

from __future__ import annotations

import re

from app.sources.base import SourceWording

WORDING = SourceWording(
    title="Datto RMM",
    topics=("devices", "patches", "antivirus", "alerts"),
    tool_examples=("datto_list_devices for a client's devices", "datto_get_device for one hostname"),
    ask_with=("a device hostname",),
    retry_with=("a device hostname",),
    examples=("offline devices for NGMC",),
    lookup_examples=("tell me about WDON-FD01",),
)

# "Reboot WDON-FD01", "Run the disk cleanup component on ...", "Approve the pending patches for BPIE": Datto
# writes. There is no tool for them, so answering without one is the expected decline. Not "remove" or "delete":
# "Remove the alert tickets" asks Flow for a filtered list. "Run a report / search" is a question too.
WRITE_WORDS = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+)?(?:reboot|restart|shut\s*down|power\s*(?:off|on|cycle)|"
    r"(?:run|execute)(?!\s+(?:a\s+|an\s+|the\s+)?(?:report|search|query|summary)\b)|push|deploy|install|"
    r"uninstall|approve|mute|wipe|isolate|quarantine|resolve\s+(?:the\s+|that\s+|this\s+|all\s+)?alerts?)\b",
    re.IGNORECASE,
)
