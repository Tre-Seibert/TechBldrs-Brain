"""Client resolver: what the user called a client -> the ids every source needs for that client.

Flow is the record of clients. Its client detail carries the Microsoft 365 tenant id and the IT Glue url;
other sources fill in their own id through a hook (Datto: the site whose name starts with the client code).
Source tools call this, so the model only ever names a client by code or name, never by tenant or site id.

Read only, and not a model-facing tool. Results are cached in memory for a few minutes, so a question that
runs several tools for one client fetches it once.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.sources.cache import TtlCache
from app.sources.flow.protocol import FlowRequestError, FlowSource
from app.sources.flow.schemas import ClientDetail, ClientListItem, itglue_org_id

_log = logging.getLogger("tb_brain.clients")

CACHE_TTL_SECONDS = 300.0
MAX_CANDIDATES = 5

# Ids a source hook may fill. Flow's own fields (tenant, IT Glue) are never set by a hook.
HOOK_FIELDS = ("datto_site_uid",)

# (client_code, name) -> the source's id for that client, or None when it has none.
IdHook = Callable[[str, "str | None"], "str | None"]

_CODE_RE = re.compile(r"^[A-Za-z0-9]{4}$")
_NAME_SUFFIXES = frozenset({"inc", "llc", "ltd", "co", "corp", "corporation", "company", "pc", "pa", "pllc", "llp", "lp"})
# Close matches: every word the user typed starts a word of the name, else a fuzzy score this high.
_MIN_CLOSE_LEN = 3
_FUZZY_CUTOFF = 0.8
# A fuzzy best match this far ahead of the next one is not a guess; any closer and we ask.
_FUZZY_CLEAR_LEAD = 0.1


class Outcome(str, Enum):
    FOUND = "found"
    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"
    NO_M365_TENANT = "no_m365_tenant"
    NO_ITGLUE_ORG = "no_itglue_org"
    NO_DATTO_SITE = "no_datto_site"


@dataclass(frozen=True)
class ResolvedClient:
    client_code: str
    name: str | None
    m365_tenant_id: str | None = None
    itglue_org_id: int | None = None
    datto_site_uid: str | None = None

    @property
    def label(self) -> str:
        return f"{self.name} ({self.client_code})" if self.name else self.client_code


@dataclass(frozen=True)
class ClientCandidate:
    client_code: str
    name: str | None

    @property
    def label(self) -> str:
        return f"{self.name} ({self.client_code})" if self.name else self.client_code


@dataclass(frozen=True)
class ClientResolution:
    """What resolve() found. `message` is a reply a tool can show as-is; empty when the client was found.

    `client` is set for FOUND and for the NO_* outcomes (the client exists but lacks the id asked for);
    `candidates` is set for AMBIGUOUS.
    """

    outcome: Outcome
    query: str
    client: ResolvedClient | None = None
    candidates: tuple[ClientCandidate, ...] = ()
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.FOUND


# The ids a tool can require, and what it gets when the client has none.
_REQUIREMENTS: dict[str, Outcome] = {
    "m365_tenant_id": Outcome.NO_M365_TENANT,
    "itglue_org_id": Outcome.NO_ITGLUE_ORG,
    "datto_site_uid": Outcome.NO_DATTO_SITE,
}


class ClientResolver:
    """Resolves client codes and names against Flow. Safe to share across threads and requests."""

    def __init__(
        self,
        flow: FlowSource,
        *,
        ttl: float = CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._flow = flow
        self._hooks: dict[str, tuple[str, IdHook]] = {}
        self._lock = threading.Lock()
        self._cache = TtlCache(ttl, clock=clock)

    def register_id_hook(self, field: str, hook: IdHook, *, source: str) -> None:
        """Let `source` fill `field` (e.g. Datto's datto_site_uid) for every client resolved from now on."""
        if field not in HOOK_FIELDS:
            raise ValueError(f"{field!r} is not an id a source can fill; allowed: {', '.join(HOOK_FIELDS)}")
        with self._lock:
            if field in self._hooks:
                raise ValueError(f"{field!r} is already filled by {self._hooks[field][0]}")
            self._hooks[field] = (source, hook)
            self._cache.discard(lambda key: key[:2] == ("hook", field))

    def clear(self) -> None:
        self._cache.clear()

    def resolve(self, text: str | None, *, require: Iterable[str] = ()) -> ClientResolution:
        """Client code or name -> ClientResolution. `require` names ids the caller cannot work without
        (m365_tenant_id, itglue_org_id, datto_site_uid); a found client missing one gets that NO_* outcome.

        Exact code first, then an exact name, then a close name. Flow errors other than "not found" raise.
        """
        required = tuple(require)
        for field in required:
            if field not in _REQUIREMENTS:
                raise ValueError(f"unknown id {field!r}; can require: {', '.join(_REQUIREMENTS)}")
        query = _clean(text)
        if not query:
            return ClientResolution(
                Outcome.UNKNOWN, "", message="Which client? Give the 4-character client code or the client's name."
            )
        code, candidates = self._match(query)
        if code is None:
            if candidates:
                listed = ", ".join(candidate.label for candidate in candidates)
                return ClientResolution(
                    Outcome.AMBIGUOUS,
                    query,
                    candidates=candidates,
                    message=f'"{query}" matches more than one client in Flow: {listed}. Which one?',
                )
            return ClientResolution(Outcome.UNKNOWN, query, message=_unknown_message(query))
        client = self._client(code, required)
        if client is None:
            return ClientResolution(Outcome.UNKNOWN, query, message=_unknown_message(query))
        for field in required:
            if getattr(client, field) is None:
                return ClientResolution(
                    _REQUIREMENTS[field], query, client=client, message=self._missing_message(field, client)
                )
        _log.debug("client resolved query=%r code=%s", query, client.client_code)
        return ClientResolution(Outcome.FOUND, query, client=client)

    async def aresolve(self, text: str | None, *, require: Iterable[str] = ()) -> ClientResolution:
        """resolve() off the event loop; Flow and the hooks are blocking HTTP."""
        return await asyncio.to_thread(self.resolve, text, require=tuple(require))

    def _match(self, query: str) -> tuple[str | None, tuple[ClientCandidate, ...]]:
        """The one client code `query` means, or None and the candidates to ask about (none when unknown)."""
        if _CODE_RE.match(query) and self._detail(query.upper()) is not None:
            return query.upper(), ()
        wanted = _name_key(query)
        if not wanted:
            return None, ()
        named = [(row, _name_key(row.name)) for row in self._roster() if row.name]
        exact = [row for row, key in named if key == wanted]
        if len(exact) == 1:
            return exact[0].client_code.upper(), ()
        if exact:
            return None, _candidates(exact)
        if len(wanted) < _MIN_CLOSE_LEN:
            return None, ()
        words = wanted.split()
        contains = [row for row, key in named if all(any(part.startswith(word) for part in key.split()) for word in words)]
        if len(contains) == 1:
            return contains[0].client_code.upper(), ()
        if contains:
            return None, _candidates(contains)
        scored = sorted(
            ((_fuzzy_score(wanted, key), row) for row, key in named),
            key=lambda pair: pair[0],
            reverse=True,
        )
        plausible = [(score, row) for score, row in scored if score >= _FUZZY_CUTOFF]
        if len(plausible) == 1 or (plausible and plausible[0][0] - plausible[1][0] >= _FUZZY_CLEAR_LEAD):
            return plausible[0][1].client_code.upper(), ()
        return None, _candidates([row for _, row in plausible[:MAX_CANDIDATES]])

    def _client(self, code: str, required: tuple[str, ...]) -> ResolvedClient | None:
        detail = self._detail(code)
        if detail is None:
            return None
        name = detail.name or detail.full_company_name
        ids: dict[str, str | None] = {}
        with self._lock:
            hooks = dict(self._hooks)
        for field, (source, hook) in hooks.items():
            try:
                ids[field] = self._cached(("hook", field, code), lambda hook=hook: hook(code, name))
            except Exception as exc:
                if field in required:
                    raise
                # Another source being down must not stop this one; the id just stays unknown.
                _log.warning("client id lookup failed source=%s field=%s code=%s error=%s", source, field, code, type(exc).__name__)
        return ResolvedClient(
            client_code=detail.client_code.upper(),
            name=name,
            m365_tenant_id=(detail.m365_tenant_id or "").strip() or None,
            itglue_org_id=itglue_org_id(detail.itglue_url),
            **ids,
        )

    def _detail(self, code: str) -> ClientDetail | None:
        def fetch() -> ClientDetail | None:
            try:
                return self._flow.get_client(client_code=code)
            except FlowRequestError as exc:
                if str(exc).startswith("Flow 404"):
                    return None
                raise

        return self._cached(("detail", code), fetch)

    def _roster(self) -> list[ClientListItem]:
        # Every status: a former client is still a client someone may ask about.
        return self._cached(("roster",), lambda: self._flow.list_clients(status=None))

    def _cached(self, key: tuple[str, ...], fetch: Callable[[], Any]) -> Any:
        """The cached value for `key`, fetched once per TTL; failures are not cached (see TtlCache)."""
        return self._cache.get(key, fetch)

    def _missing_message(self, field: str, client: ResolvedClient) -> str:
        if field == "m365_tenant_id":
            return (
                f"{client.label} has no Microsoft 365 tenant id on record in Flow, so its Microsoft 365 data "
                "can't be looked up. Add the tenant id on the client's page in Flow."
            )
        if field == "itglue_org_id":
            return f"{client.label} has no IT Glue organization on record in Flow."
        if field not in self._hooks:
            return f"Datto RMM is not enabled in tb-brain, so there is no Datto site for {client.label}."
        return f"{client.label} has no Datto RMM site (none whose name starts with {client.client_code})."


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().strip("\"'`“”‘’").strip(" .,;:!?")).strip()


def _name_key(name: str | None) -> str:
    """Name for comparing: lower case, '&' as 'and', no punctuation, no leading 'the' or company suffix."""
    words = re.sub(r"[^a-z0-9]+", " ", (name or "").casefold().replace("&", " and ")).split()
    if words[:1] == ["the"] and len(words) > 1:
        words = words[1:]
    while len(words) > 1 and words[-1] in _NAME_SUFFIXES:
        words.pop()
    return " ".join(words)


def _fuzzy_score(wanted: str, key: str) -> float:
    """Typo tolerance: against the whole name, and against its first words when fewer were typed."""
    lead = " ".join(key.split()[: len(wanted.split())])
    return max(difflib.SequenceMatcher(None, wanted, key).ratio(), difflib.SequenceMatcher(None, wanted, lead).ratio())


def _candidates(rows: list[ClientListItem]) -> tuple[ClientCandidate, ...]:
    ordered = sorted(rows, key=lambda row: ((row.name or "").casefold(), row.client_code))
    return tuple(ClientCandidate(row.client_code.upper(), row.name) for row in ordered[:MAX_CANDIDATES])


def _unknown_message(query: str) -> str:
    return f'No client in Flow matches "{query}". Give the 4-character client code or the client\'s name as Flow has it.'
