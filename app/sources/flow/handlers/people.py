"""Resolving people a question names: technicians (assignee codes, 'me') and client contacts / requestors."""

from __future__ import annotations

import re

from app.identity import current_signed_in_email
from app.sources.flow.protocol import FlowSource
from app.sources.flow.schemas import TechnicianRecord
from app.tools.turns import (
    _APOSTROPHE_RE,
    _ASSIGNEE_CODE_RE,
    _contact_name_score,
    _damerau,
    _fold_apostrophes,
    _is_self_token,
    _name_parts,
)


_UNKNOWN_SELF = (
    "I don't know who you are signed in as. Sign in with your Flow email, or name a technician."
)


def _requestor_matches(query: str, stored: str | None) -> bool:
    """Full-name match on a ticket requestor. A shared last name is not enough."""
    if not (stored or "").strip():
        return False
    score = _contact_name_score(query, stored or "")
    return score is not None and score <= 1


def _requestor_queries(name: str) -> list[str]:
    """Spellings Flow's ILIKE can hit: straight apostrophe, stripped, and O'Brien."""
    straight = _APOSTROPHE_RE.sub("'", name.strip())
    stripped = straight.replace("'", "")
    curly = straight.replace("'", "\u2019")
    variants = [straight]
    if curly not in variants:
        variants.append(curly)
    if stripped and stripped not in variants:
        variants.append(stripped)
    parts = stripped.split()
    if len(parts) >= 2 and re.fullmatch(r"O[A-Za-z]{2,}", parts[-1]):
        irish = " ".join([*parts[:-1], f"O'{parts[-1][1:]}"])
        if irish not in variants:
            variants.append(irish)
    return variants


def _search_contacts_loose(source: FlowSource, person: str) -> list:
    matches = source.search_contact(query=person, limit=10)
    scored = [row for row in matches if _best_contact_score(person, row) is not None]
    if scored:
        return matches
    last = _name_parts(person)[1]
    if last and len(last) >= 4:
        return source.search_contact(query=last, limit=10)
    return matches


def _best_contact_score(person: str, row) -> int | None:
    scores = [
        score
        for label in (row.full_name, row.file_as)
        if label
        for score in [_contact_name_score(person, label)]
        if score is not None
    ]
    return min(scores) if scores else None


def _pick_contact(matches: list, person: str):
    wanted = person.strip().lower()
    exact = [row for row in matches if (row.full_name or "").strip().lower() == wanted]
    if exact:
        return exact[0], None
    ranked = sorted(
        ((score, row) for row in matches if (score := _best_contact_score(person, row)) is not None),
        key=lambda item: (item[0], item[1].full_name or ""),
    )
    if ranked:
        best_score, best = ranked[0]
        tied = [row for score, row in ranked if score == best_score]
        if len({row.id for row in tied}) == 1:
            return best, None
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, None
    options = ", ".join(
        f"{row.full_name} ({row.client_code})" for row in matches if row.full_name
    )
    return None, f"{person!r} matches more than one contact: {options}. Ask which one."


def _signed_in_technician(source: FlowSource) -> tuple[TechnicianRecord | None, str | None]:
    email = (current_signed_in_email.get() or "").strip().lower()
    if not email:
        return None, _UNKNOWN_SELF
    matches = source.search_technician(query=email, limit=5)
    exact = [row for row in matches if (row.email or "").strip().lower() == email]
    pick = exact[0] if exact else (matches[0] if len(matches) == 1 else None)
    if pick and pick.assignee_code:
        return pick, None
    return None, f"No Flow technician uses the signed-in email {email}."


def _first_name_starts(source: FlowSource, prefix: str) -> list[TechnicianRecord]:
    found = []
    for tech in source.search_technician(query="", limit=100):
        first = (tech.display_name or "").strip().split(" ")[0].lower()
        if tech.assignee_code and first.startswith(prefix.lower()) and first != prefix.lower():
            found.append(tech)
    return found


def _closest_technicians(source: FlowSource, text: str) -> list[tuple[int, TechnicianRecord]]:
    """Technicians whose first or full name is within two typos of `text`, closest tier only.

    One technician at distance 1 is an obvious typo and is used directly; anything fuzzier is
    offered back as "Did you mean ...?" so the tool never silently guesses a person.
    """
    query = _fold_apostrophes(text).strip().lower()
    if len(query) < 4:
        return []
    scored: list[tuple[int, TechnicianRecord]] = []
    for tech in source.search_technician(query="", limit=100):
        name = _fold_apostrophes(tech.display_name).strip().lower()
        if not tech.assignee_code or not name:
            continue
        distance = min(_damerau(query, name), _damerau(query, name.split()[0]))
        if distance <= 2:
            scored.append((distance, tech))
    if not scored:
        return []
    best = min(distance for distance, _ in scored)
    return [(d, t) for d, t in scored if d == best]


def _resolve_assignee(source: FlowSource, raw: str | None) -> tuple[str | None, str | None]:
    """Return (assignee_code, error). A two-letter code passes through; a name or email is
    resolved through search_technician — never a hardcoded alias table.

    'me' / 'my' / 'myself' / 'i' is the signed-in Flow email, not a name search.
    Those tokens are also valid two-letter codes, so self-check comes first.
    """
    text = (raw or "").strip()
    if not text:
        return None, None
    if _is_self_token(text):
        tech, error = _signed_in_technician(source)
        if error:
            return None, error
        return (tech.assignee_code or "").lower(), None
    if _ASSIGNEE_CODE_RE.match(text):
        code = text.lower()
        known = source.search_technician(query=code, limit=5)
        if any((m.assignee_code or "").lower() == code for m in known):
            return code, None
        starts = _first_name_starts(source, code)  # "ed" is the start of Eddie, not anyone's code
        if len(starts) == 1:
            return (starts[0].assignee_code or "").lower(), None
        if starts:
            options = " or ".join(f"{t.display_name} ({t.assignee_code})" for t in starts)
            return None, f"{code!r} could be {options}. Ask again with the full name."
        return code, None  # not a known code and not a name start: let Flow decide
    matches = source.search_technician(query=text, limit=10)
    codes = sorted({(m.assignee_code or "").lower() for m in matches if m.assignee_code})
    if len(codes) == 1:
        return codes[0], None
    if not codes:
        close = _closest_technicians(source, text)
        if len(close) == 1 and close[0][0] <= 1:
            return (close[0][1].assignee_code or "").lower(), None  # one letter off: "Eddoe" -> Eddie
        if close:
            options = " or ".join(f"{t.display_name} ({t.assignee_code})" for _, t in close)
            return None, f"No active technician matches {text!r}. Did you mean {options}? Ask again with that name."
        return None, f"No active technician matches {text!r}. Try search_technician with another spelling."
    options = ", ".join(f"{m.display_name} ({m.assignee_code})" for m in matches if m.assignee_code)
    return None, f"{text!r} matches more than one technician: {options}. Ask which one."
