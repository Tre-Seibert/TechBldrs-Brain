"""The signed-in technician is looked up in Flow once, not on every model call and tool call (A7).

The loop's prompt line and the Flow "me" handlers share one short cache per Flow client, built on the same TtlCache
as the client resolver. What the model is sent does not change.
"""

from __future__ import annotations

import tests._isolation  # noqa: F401  (first: hermetic settings, no network)

import threading
import time
import unittest
import weakref
from unittest import mock

from app.identity import current_signed_in_email
from app.sources.base import Sources
from app.sources.cache import TtlCache
from app.sources.flow.handlers import people
from app.sources.flow.protocol import FlowRequestError
from app.sources.flow.spec import FLOW
from app.sources.flow.stub import StubFlowSource
from tests.test_routing import _call, _run, _settings, _user

ME = "tseibert@techbldrs.example"


class CountingFlow(StubFlowSource):
    """Stub Flow that counts technician lookups by query, optionally failing the next one."""

    def __init__(self) -> None:
        super().__init__()
        self.queries: list[str] = []
        self.fail_next = False

    def search_technician(self, *, query: str, limit: int = 25):
        self.queries.append(query)
        if self.fail_next:
            self.fail_next = False
            raise FlowRequestError("Flow 503: unavailable")
        return super().search_technician(query=query, limit=limit)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TtlCacheTests(unittest.TestCase):
    def test_hits_until_the_ttl_runs_out(self) -> None:
        clock, fetches = Clock(), []
        cache = TtlCache(60, clock=clock)
        fetch = lambda: fetches.append(1) or len(fetches)  # noqa: E731
        self.assertEqual(cache.get("k", fetch), 1)
        clock.now += 59
        self.assertEqual(cache.get("k", fetch), 1)
        clock.now += 2
        self.assertEqual(cache.get("k", fetch), 2)

    def test_failures_are_not_cached_and_discard_drops_matching_keys(self) -> None:
        cache = TtlCache(60)
        with self.assertRaises(RuntimeError):
            cache.get("k", lambda: (_ for _ in ()).throw(RuntimeError("down")))
        self.assertEqual(cache.get("k", lambda: "up"), "up")
        cache.get(("hook", "a"), lambda: 1)
        cache.discard(lambda key: key[:1] == ("hook",))
        self.assertEqual(cache.get(("hook", "a"), lambda: 2), 2)
        self.assertEqual(cache.get("k", lambda: "again"), "up")

    def test_concurrent_callers_share_one_fetch(self) -> None:
        cache, calls = TtlCache(60), []

        def slow() -> str:
            calls.append(1)
            time.sleep(0.05)
            return "v"

        threads = [threading.Thread(target=cache.get, args=("k", slow)) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(calls), 1)


class SignedInMatchesTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(people, "_signed_in_caches", weakref.WeakKeyDictionary())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_lookup_per_email_per_flow_client(self) -> None:
        flow, other = CountingFlow(), CountingFlow()
        first = people.signed_in_matches(flow, ME)
        self.assertEqual([row.assignee_code for row in first], ["ts"])
        self.assertEqual(people.signed_in_matches(flow, ME), first)
        people.signed_in_matches(flow, "trivera@techbldrs.example")
        people.signed_in_matches(other, ME)
        self.assertEqual(flow.queries, [ME, "trivera@techbldrs.example"])
        self.assertEqual(other.queries, [ME])

    def test_expires_after_the_ttl(self) -> None:
        flow, clock = CountingFlow(), Clock()
        people._signed_in_caches[flow] = TtlCache(people.SIGNED_IN_TTL_SECONDS, clock=clock)
        people.signed_in_matches(flow, ME)
        clock.now += people.SIGNED_IN_TTL_SECONDS - 1
        people.signed_in_matches(flow, ME)
        clock.now += 2
        people.signed_in_matches(flow, ME)
        self.assertEqual(flow.queries, [ME, ME])

    def test_a_failed_lookup_is_not_cached(self) -> None:
        flow = CountingFlow()
        flow.fail_next = True
        with self.assertRaises(FlowRequestError):
            people.signed_in_matches(flow, ME)
        self.assertEqual([row.assignee_code for row in people.signed_in_matches(flow, ME)], ["ts"])
        self.assertEqual(flow.queries, [ME, ME])

    def test_callers_cannot_change_the_cached_list(self) -> None:
        flow = CountingFlow()
        people.signed_in_matches(flow, ME).clear()
        self.assertEqual(len(people.signed_in_matches(flow, ME)), 1)


class LoopLookupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(people, "_signed_in_caches", weakref.WeakKeyDictionary())
        patcher.start()
        self.addCleanup(patcher.stop)
        token = current_signed_in_email.set(ME)
        self.addCleanup(current_signed_in_email.reset, token)

    async def _ask(self, flow: CountingFlow, messages, script, **settings):
        return await _run(messages, script, settings=_settings(**settings), sources=Sources([(FLOW, flow)]))

    async def test_one_lookup_for_the_prompt_and_every_me_tool_call(self) -> None:
        for style in ("legacy", "lean"):
            with self.subTest(style=style):
                people._signed_in_caches.clear()
                flow = CountingFlow()
                payload, bodies = await self._ask(
                    flow,
                    [{"role": "system", "content": "You are tb-brain."}, _user("my open tickets, then my urgent ones")],
                    [
                        _call("list_tickets", assignee_code="me", stage="open"),
                        _call("list_tickets", assignee_code="me", category="urgent"),
                        {"role": "assistant", "content": "Done."},
                    ],
                    prompt_style=style,
                )
                self.assertEqual([c["ok"] for c in payload["x_tb_brain"]["tool_calls"]], [True, True])
                self.assertEqual(flow.queries.count(ME), 1)
                self.assertIn("Tre Seibert", str(bodies[0]["messages"][:2]))

    async def test_later_requests_reuse_the_lookup_and_send_the_same_prompt(self) -> None:
        flow = CountingFlow()
        _, first = await self._ask(flow, [_user("hi")], [{"role": "assistant", "content": "Hello."}])
        _, second = await self._ask(flow, [_user("hi")], [{"role": "assistant", "content": "Hello."}])
        self.assertEqual(flow.queries.count(ME), 1)
        self.assertEqual(first[0]["messages"], second[0]["messages"])


if __name__ == "__main__":
    unittest.main()
