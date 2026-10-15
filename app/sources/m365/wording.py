"""How the agent loop names Microsoft 365 in its nudge and fallback messages (app/agent/wording.py), and which
requests have no tool and are declined rather than data questions it must answer from a tool.
"""

from __future__ import annotations

import re

from app.sources.base import SourceWording

WORDING = SourceWording(
    title="Microsoft 365",
    topics=("users", "MFA", "licenses", "sign-ins"),
    tool_examples=("m365_users for a client's users", "m365_get_user for one person"),
    ask_with=("a user's name or email",),
    retry_with=("a user's email address",),
    examples=("who doesn't have MFA at WDON",),
    lookup_examples=("what licenses does Debe have at WDON",),
)

# An email address or UPN in the question (jsmith@buckheadpeds.example): a Microsoft 365 user. Also a routing pattern.
EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b")
# A client's tenant ("What changed in WDON's tenant?"), not TechBldrs' own ("emails from WDON to our tenant" is Flow mail).
TENANT_RE = re.compile(r"(?<!\bour )(?<!\bmy )(?<!techbldrs )(?<!techbldrs' )\btenants?\b", re.IGNORECASE)

_POLITE = r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|go\s+ahead\s+and\s+)?"
# Verbs that are a change in a tenant whatever follows ("Reset Debe's password", "Disable jsmith at BUCK").
_ALWAYS = r"(?:reset|disable|re-?enable|unlock|unblock|revoke|assign|unassign|grant|force|convert|offboard)"
# Verbs that are one only with a Microsoft 365 object after them ("Add a Business Premium license to Debe", "Block
# sign-in for jsmith"), so "Remove the alert tickets" and "Add a note to ZTB-1680" are not.
_WITH_OBJECT = r"(?:add|remove|give|take\s+away|block|enable|delete|create|turn\s+(?:on|off)|require|enforce|sign\s+out|set\s+up)"
_OBJECT = (
    r"(?:licen[cs]es?|mfa|2fa|multi-?factor|groups?|roles?|admins?|mailbox(?:es)?|sign-?ins?|sessions?|accounts?|"
    r"guests?|access|permissions?|delegates?|forwarding|security\s+defaults|conditional\s+access|polic(?:y|ies)|"
    r"passwords?|authenticator|business\s+(?:premium|standard|basic)|e[35])"
)
_WHOSE = r"(?:\w+['’]s\s+|(?:her|his|their|the|my)\s+(?:[\w-]+\s+)?)?"
# Reading what is in a client's mailbox ("Show me the emails in Debe's inbox", "Read Debe's email", "What's in her
# inbox?"). tb-brain has no Mail.Read and no tool for it; mailbox size and rules are not this.
_MAIL_CONTENT = (
    rf"\b(?:e-?mails?|messages?|mail)\s+(?:in|from)\s+{_WHOSE}(?:inbox|mailbox|outlook)\b"
    rf"|\bread\s+{_WHOSE}(?:e-?mails?|mail|inbox|mailbox|messages)\b"
    rf"|\bwhat(?:['’]s|\s+is)\s+in\s+{_WHOSE}(?:inbox|mailbox)\b"
    rf"|\b(?:search|open|look\s+(?:in|through)|go\s+through)\s+{_WHOSE}(?:inbox|mailbox|e-?mails?)\b"
    rf"|\bcontents?\s+of\s+{_WHOSE}(?:inbox|mailbox|e-?mails?)\b"
)
# Microsoft 365 requests with no tool: writes in a tenant, and reading a mailbox's mail. Answering them without a tool
# is the expected decline (app/agent/wording.py), and they route to Microsoft 365 so its prompt section, which says to
# decline, is in the prompt.
WRITE_WORDS = re.compile(
    _POLITE + rf"(?:{_ALWAYS}\b|{_WITH_OBJECT}\b(?=.*\b{_OBJECT}\b))" + "|" + _MAIL_CONTENT,
    re.IGNORECASE,
)
