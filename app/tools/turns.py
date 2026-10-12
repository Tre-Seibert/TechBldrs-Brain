"""Reading the user's own words: periods ('last week'), 'me', and the person a question is about.

The model gets these wrong, so the code works them out from the sentence. Source-agnostic.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.tools.base import ChatTurn


_ASSIGNEE_CODE_RE = re.compile(r"^[A-Za-z]{2}$")
_CLIENT_CODE_TOKEN_RE = re.compile(r"[A-Z]{3,8}")
# "... for BUCK", "... at client ZEBB": a client scope the pattern routers do not handle.
_CLIENT_SCOPE_RE = re.compile(r"\b(?:at|for|from|of|in)\s+(?:client\s+)?[A-Z]{3,8}\b")
_LABEL_RE = re.compile(r"^([A-Za-z0-9]+)-([A-Za-z0-9]+)$")
_SELF_ASSIGNEE = frozenset({"me", "my", "myself", "i"})
_OWN_TICKETS_RE = re.compile(r"\b(my|mine|assigned to me|i have)\b", re.IGNORECASE)
_TICKETS_FOR_RE = re.compile(r"\btickets?\b[^.?!\n]*\bfor\s+(?P<name>.+)", re.IGNORECASE)
_TICKET_PERSON_RES = (
    re.compile(
        r"\b(?:latest|last|most recent)\s+tickets?\b.*?\b(?:involving|for|from|about)\s+(?P<name>.+)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"\btickets?\b.*?\b(?:involving|from)\s+(?P<name>.+)", re.IGNORECASE | re.DOTALL),
    _TICKETS_FOR_RE,
)
_LATEST_TICKET_RE = re.compile(r"\b(?:latest|last|most recent)\s+tickets?\b", re.IGNORECASE)
_MAIL_TURN_RE = re.compile(r"\b(emails?|mails?|reach out|inbox)\b", re.IGNORECASE)
_REACH_OUT_RES = (
    re.compile(
        r"\bwhen did\s+(?P<name>.+?)\s+last\s+(?:reach out|e-?mails?(?:\s+us)?|mails?(?:\s+us)?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwhen(?:'s| is| was)?\s+the last time\s+(?P<name>.+?)\s+(?:e-?mailed|reached out|mailed)(?:\s+us)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\blast\s+(?:e-?mail|mail|reach out)\s+from\s+(?P<name>.+?)(?:\?|$)",
        re.IGNORECASE,
    ),
)
_PERSON_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z.'-]*$")
_APOSTROPHE_RE = re.compile(r"['\u2019\u2018\u02bc`´]")


def _is_self_token(text: str | None) -> bool:
    return (text or "").strip().lower() in _SELF_ASSIGNEE


def _asked_own_tickets(turn: ChatTurn | None) -> bool:
    text = turn.user_text if turn else ""
    return bool(_OWN_TICKETS_RE.search(text or ""))


def _looks_like_person_name(text: str | None) -> bool:
    parts = [part for part in re.split(r"\s+", (text or "").strip()) if part]
    return len(parts) >= 2 and all(_PERSON_TOKEN_RE.match(part) for part in parts)


def _fold_apostrophes(text: str | None) -> str:
    """O'Brien, O’Brien, and OBrien are the same name."""
    return _APOSTROPHE_RE.sub("", text or "")


def _name_parts(text: str | None) -> tuple[str, str]:
    raw = _fold_apostrophes(text).strip()
    if "," in raw:
        last, _, rest = raw.partition(",")
        first = rest.strip().split()[0] if rest.strip() else ""
        last = last.strip()
        if last and first:
            return first, last
    parts = [part for part in re.split(r"\s+", raw) if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def _damerau(left: str, right: str) -> int:
    """Edit distance including adjacent transpositions (Micahel → Michael)."""
    a, b = left.lower(), right.lower()
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    prev_prev = list(range(len(b) + 1))
    prev = [1] + [0] * len(b)
    for j in range(1, len(b) + 1):
        prev[j] = prev_prev[j - 1] if a[0] == b[j - 1] else 1 + min(prev_prev[j], prev[j - 1], prev_prev[j - 1])
    for i in range(2, len(a) + 1):
        current = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            current[j] = min(prev[j] + 1, current[j - 1] + 1, prev[j - 1] + cost)
            if j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                current[j] = min(current[j], prev_prev[j - 2] + 1)
        prev_prev, prev = prev, current
    return prev[len(b)]


def _contact_name_score(query: str, stored: str) -> int | None:
    """Lower is closer. None means do not treat this as the same person."""
    q_first, q_last = _name_parts(query)
    n_first, n_last = _name_parts(stored)
    if q_last and n_last and q_last.lower() == n_last.lower():
        if not q_first:
            return 2
        distance = _damerau(q_first, n_first)
        return distance if distance <= 2 else None
    if q_first and n_first and not q_last:
        distance = _damerau(q_first, n_first)
        return distance if distance <= 1 else None
    return None


def _clean_person_name(name: str | None) -> str | None:
    text = (name or "").strip(" ?.!,*;:\"'()")
    if not text or _is_self_token(text) or _OWN_TICKETS_RE.search(text):
        return None
    if _LABEL_RE.match(text) or _ASSIGNEE_CODE_RE.match(text):
        return None
    if re.fullmatch(r"[A-Za-z]{2,8}", text) and text.isupper():
        return None
    tokens = text.split()
    # A client code ("BLMC", "client ZZZZ") or a long clause is a filter, not a person.
    if len(tokens) > 4 or any(_CLIENT_CODE_TOKEN_RE.fullmatch(token) for token in tokens):
        return None
    if tokens[0].lower() == "client":
        return None
    return text


def _person_name_from_turn(turn: ChatTurn | None) -> str | None:
    text = turn.user_text if turn else ""
    for pattern in _TICKET_PERSON_RES:
        match = pattern.search(text or "")
        if match:
            name = _clean_person_name(match.group("name"))
            if name:
                return name
    return None


def _asked_tickets_for_person(turn: ChatTurn | None) -> bool:
    return _person_name_from_turn(turn) is not None


def _asked_latest_for_person(turn: ChatTurn | None) -> bool:
    text = turn.user_text if turn else ""
    return bool(text and _LATEST_TICKET_RE.search(text) and _person_name_from_turn(turn))


def _asked_mail_for_person(turn: ChatTurn | None) -> bool:
    text = turn.user_text if turn else ""
    return bool(text and _MAIL_TURN_RE.search(text))


def _person_from_reach_out(turn: ChatTurn | None) -> str | None:
    """Name in 'when did Sean O'Brien last reach out / last email us'."""
    text = turn.user_text if turn else ""
    if not text:
        return None
    for pattern in _REACH_OUT_RES:
        match = pattern.search(text)
        if not match:
            continue
        name = _clean_person_name(match.group("name"))
        if name and _usable_person_name(name):
            return name
    return None


def _usable_person_name(name: str) -> bool:
    folded = _fold_apostrophes(name)
    if _looks_like_person_name(folded):
        return True
    return bool(_PERSON_TOKEN_RE.match(folded)) and len(folded) >= 3


_WEEKDAYS_BACK = 7


def _period_from_turn(turn: ChatTurn | None, now: datetime | None = None) -> tuple[str, str, str] | None:
    """(after, before_exclusive, label) for 'last week', 'this month', 'yesterday'... in the user's words.

    The 14B model gets these dates wrong (it once answered 'last week' as Oct 1 to Oct 7), so when the
    question names a period the code computes it and overrides the model's dates.
    """
    from datetime import timedelta

    text = (turn.user_text if turn else "") or ""
    now = now or datetime.now()
    today = now.date()
    monday = today - timedelta(days=today.weekday())
    first = today.replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    last_month = (first - timedelta(days=1)).replace(day=1)
    low = text.lower()
    span: tuple[Any, Any] | None = None
    year = re.search(r"\b(?:in|during|for|of)\s+(20\d{2})\b", low)
    if re.search(r"\blast\s+week\b", low):
        span = (monday - timedelta(days=7), monday)
    elif re.search(r"\bthis\s+week\b", low):
        span = (monday, monday + timedelta(days=7))
    elif re.search(r"\byesterday\b", low):
        span = (today - timedelta(days=1), today)
    elif re.search(r"\btoday\b", low):
        span = (today, today + timedelta(days=1))
    elif re.search(r"\blast\s+month\b", low):
        span = (last_month, first)
    elif re.search(r"\bthis\s+month\b", low):
        span = (first, next_month)
    elif re.search(r"\b(?:this|current|last)\s+quarter\b", low):
        q_month = 3 * ((today.month - 1) // 3)  # 0, 3, 6 or 9 months into the year
        step = -3 if re.search(r"\blast\s+quarter\b", low) else 0
        start_idx = today.year * 12 + q_month + step
        end_idx = start_idx + 3
        span = (
            today.replace(year=start_idx // 12, month=start_idx % 12 + 1, day=1),
            today.replace(year=end_idx // 12, month=end_idx % 12 + 1, day=1),
        )
    elif year:
        y = int(year.group(1))
        span = (today.replace(year=y, month=1, day=1), today.replace(year=y + 1, month=1, day=1))
    if span is None:
        return None
    start, end = span
    last_day = end - timedelta(days=1)
    label = f"{start:%b} {start.day}" if start == last_day else f"{start:%b} {start.day} to {last_day:%b} {last_day.day}"
    if year and span and span[0].month == 1 and span[0].day == 1 and (span[1] - span[0]).days in (365, 366):
        label = year.group(1)
    return start.isoformat(), end.isoformat(), label
