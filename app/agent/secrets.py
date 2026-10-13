"""Requests for a password, credential, key or other secret: answered with a fixed refusal before the model runs.

tb-brain never reads secrets, and no source's tool returns one, so a model that tries anyway can only call some
unrelated tool ("What's the local admin password on WDON-SRV01?" called get_client_detail). The check is
deterministic, like the write-intent check in app/agent/wording.py, and the same for every source and prompt style.

A question *about* a password is not a request for one and still goes to the model: "when was the password last
changed", "is the admin password expired", "password reset tickets", "how do we reset a password", "tickets about
the admin password". A secret word followed by one of _ABOUT_AFTER, or preceded by one of _ABOUT_BEFORE, marks
such a question.
"""

from __future__ import annotations

import re

SECRET_REFUSAL = (
    "I can't look up or share passwords, credentials, API keys, recovery keys or other secrets. "
    "Get them from IT Glue directly."
)

# The secret itself. Multi-word ones first, so "recovery key" is one match.
_SECRET = (
    r"(?:(?:bitlocker|recovery|api|access|private|product|licen[cs]e|wi-?fi|wireless|wpa2?|encryption)[\s-]+keys?"
    r"|(?:api|access|auth|bearer|refresh)[\s-]+tokens?|client[\s-]+secrets?|log-?in[\s-]+(?:details|info)"
    r"|pass(?:word|code|phrase)s?|pw|pwd|creds?|credentials?|secrets?|pins?)"
)
_SECRET_RE = re.compile(rf"(?<![\w-]){_SECRET}(?![\w-])", re.IGNORECASE)

# Words that ask for something: "what's", "tell me", "send", "show", "get me", "I need", "do you have", ...
_ASK_RE = re.compile(
    r"(?<!\w)(?:what(?:'s|’s|\s+is|\s+are|\s+was|\s+were)|whats|tell|give|send|e-?mail|text|show|get|share|need|"
    r"want|find|look\s+up|pull\s+up|provide|read\s+(?:me|out)|list|paste|fetch|retrieve|reveal|display|have|know)(?!\w)",
    re.IGNORECASE,
)

# Right after the secret: the question is about it, not for it ("password reset", "password expired", "password
# policy", "password is expired", "credentials not working", "recovery key escrowed").
_ABOUT_AFTER = re.compile(
    r"^['’]?s?\s+(?:resets?|resetting|changes?|changed|changing|expir\w*|polic(?:y|ies)|managers?|rotation|rotated|"
    r"requirements?|complexity|length|age|history|tickets?|requests?|issues?|problems?|lockouts?|locked|sync\w*|"
    r"updates?|updated|portal|spray\w*|leaks?|leaked|breach\w*|strength|prompts?|errors?|fail\w*|escrow\w*|stored|"
    r"saved|backed|enabled|last|was|were|"
    r"is|isn['’]?t|has|hasn['’]?t|had|been|did|does|doesn['’]?t|not|stopped|working|to\s+work|protected|protection)\b",
    re.IGNORECASE,
)

# Before the secret: the question is about tickets, mail, notes or a process that concerns a secret, or about how,
# when or whether something happened to it.
_ABOUT_BEFORE = re.compile(
    r"(?<!\w)(?:tickets?|mails?|emails?|messages?|entr(?:y|ies)|notes?|sops?|runbooks?|docs?|documents?|articles?|"
    r"process|how|when|whether|if|last|failed|reset\w*|chang\w*|expir\w*|many|count|number)(?!\w)",
    re.IGNORECASE,
)

# A message this short with a secret in it and no asking word ("admin password for NGMC?") is still a request.
_SHORT_WORDS = 6


def asks_for_secret(text: str) -> bool:
    """True when `text` asks for a password, credential, key or other secret rather than about one."""
    text = text or ""
    for secret in _SECRET_RE.finditer(text):
        if _ABOUT_AFTER.search(text[secret.end():]):
            continue
        before = text[: secret.start()]
        if _ABOUT_BEFORE.search(before):
            continue
        if _ASK_RE.search(before) or len(text.split()) <= _SHORT_WORDS:
            return True
    return False
