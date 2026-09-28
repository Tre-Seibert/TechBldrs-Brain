from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.knowledge.source import KnowledgeHit, KnowledgePoint, NullKnowledgeSource
from app.tools.handlers import SearchKnowledgeArgs, search_knowledge
from scripts.sync_knowledge import _chunk_text, sync_time_entries


class FakeKnowledgeSource:
    source_name = "fake"
    configured = True

    def __init__(self, hits: list[KnowledgeHit]) -> None:
        self._hits = hits

    def search(self, *, query: str, client_code: str | None = None, limit: int = 5) -> list[KnowledgeHit]:
        return self._hits[:limit]

    def upsert(self, points) -> None:  # pragma: no cover - unused in these tests
        raise NotImplementedError


class ChunkTextTests(unittest.TestCase):
    def test_packs_short_paragraphs_together(self) -> None:
        text = "para one.\n\npara two.\n\npara three."
        chunks = _chunk_text(text, max_chars=1000)
        self.assertEqual(chunks, ["para one.\n\npara two.\n\npara three."])

    def test_splits_at_max_chars_on_paragraph_boundaries(self) -> None:
        text = ("a" * 900) + "\n\n" + ("b" * 900)
        chunks = _chunk_text(text, max_chars=1000)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0].startswith("a"))
        self.assertTrue(chunks[1].startswith("b"))

    def test_splits_a_single_oversized_paragraph(self) -> None:
        text = "x" * 2500
        chunks = _chunk_text(text, max_chars=1000)
        self.assertEqual(len(chunks), 3)
        self.assertEqual("".join(chunks), text)

    def test_empty_text_yields_no_chunks(self) -> None:
        self.assertEqual(_chunk_text(""), [])
        self.assertEqual(_chunk_text("\n\n  \n\n"), [])


class RecordingKnowledgeSource:
    """Captures every upsert call instead of hitting a real Qdrant."""

    source_name = "recording"
    configured = True

    def __init__(self) -> None:
        self.upserted: list[KnowledgePoint] = []

    def search(self, *, query: str, client_code: str | None = None, limit: int = 5) -> list[KnowledgeHit]:
        return []

    def upsert(self, points: list[KnowledgePoint]) -> None:
        self.upserted.extend(points)


def _entry_row(entry_id: int, updated_at: str) -> dict:
    return {
        "id": entry_id,
        "ticket_id": 9001,
        "client_code": "WDON",
        "ticket_label": "WDON-1842",
        "subject": f"entry {entry_id}",
        "body": "fix notes",
        "updated_at": updated_at,
    }


class SyncTimeEntriesCursorTests(unittest.TestCase):
    """Regression coverage for a real bug: updated_at alone is not a safe cursor
    when more rows than one page share the exact same timestamp (bulk-imported
    data) -- the sync job re-fetched the same page forever until after_id was added."""

    def test_advances_past_a_tie_group_larger_than_one_page(self) -> None:
        # 150 rows share one timestamp (simulates a bulk import), then one newer row.
        # Paginated strictly in chunks of 100, like the real endpoint: page 1 is full
        # (100, all tied) so pagination continues; page 2 is 51 (< 100) so it's last.
        tied = [_entry_row(i, "2026-08-18 18:03:45") for i in range(1, 151)]
        newer = [_entry_row(151, "2026-08-19 09:00:00")]
        all_rows = tied + newer
        pages = [all_rows[:100], all_rows[100:]]
        calls: list[dict] = []

        def fake_flow_get(flow, path, params):
            calls.append(dict(params))
            return pages[len(calls) - 1] if len(calls) <= len(pages) else []

        with mock.patch("scripts.sync_knowledge._flow_get", side_effect=fake_flow_get):
            knowledge = RecordingKnowledgeSource()
            total, cursor, cursor_id = sync_time_entries(
                mock.Mock(), knowledge, cursor=None, cursor_id=None
            )

        self.assertEqual(total, 151)
        self.assertEqual(cursor, "2026-08-19 09:00:00")
        self.assertEqual(cursor_id, 151)
        # Each page's after_id must differ -- proof the cursor actually advanced,
        # not the same params repeated forever.
        after_ids = [c.get("after_id") for c in calls]
        self.assertEqual(after_ids, [None, 100])

    def test_no_progress_page_stops_instead_of_looping_forever(self) -> None:
        stuck_page = [_entry_row(1, "2026-08-18 18:03:45")]

        with mock.patch("scripts.sync_knowledge._flow_get", return_value=stuck_page):
            knowledge = RecordingKnowledgeSource()
            total, cursor, cursor_id = sync_time_entries(
                mock.Mock(), knowledge, cursor="2026-08-18 18:03:45", cursor_id=1
            )

        self.assertEqual(total, 1)  # the one row is still embedded before the loop bails
        self.assertEqual((cursor, cursor_id), ("2026-08-18 18:03:45", 1))


class SearchKnowledgeHandlerTests(unittest.TestCase):
    def test_none_knowledge_source_degrades_gracefully(self) -> None:
        result = search_knowledge(None, SearchKnowledgeArgs(query="how do we fix VPN dropouts"))
        self.assertFalse(result.ok)
        self.assertIn("not set up", result.error or "")

    def test_unconfigured_source_degrades_gracefully(self) -> None:
        result = search_knowledge(NullKnowledgeSource(), SearchKnowledgeArgs(query="how do we fix VPN dropouts"))
        self.assertFalse(result.ok)
        self.assertIn("not set up", result.error or "")

    def test_no_hits_says_so_without_guessing(self) -> None:
        result = search_knowledge(FakeKnowledgeSource([]), SearchKnowledgeArgs(query="something obscure"))
        self.assertTrue(result.ok)
        self.assertEqual(result.data, [])
        self.assertIn("do not guess", (result.note or "").lower())

    def test_hits_are_formatted_with_source_and_client(self) -> None:
        hits = [
            KnowledgeHit(
                text="Restart the FortiGate WAN interface, then re-check the tunnel.",
                source_type="itglue_document",
                source_label="VPN Troubleshooting",
                client_code="ACME",
                score=0.83,
            )
        ]
        result = search_knowledge(FakeKnowledgeSource(hits), SearchKnowledgeArgs(query="VPN dropouts", limit=3))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.data), 1)
        self.assertIn("VPN Troubleshooting", result.reply or "")
        self.assertIn("ACME", result.reply or "")
        self.assertIn("itglue_document", result.reply or "")

    def test_limit_is_clamped_to_ten(self) -> None:
        hits = [
            KnowledgeHit(text=f"note {i}", source_type="time_entry", source_label=f"WDON-{i:04d}")
            for i in range(20)
        ]
        result = search_knowledge(FakeKnowledgeSource(hits), SearchKnowledgeArgs(query="x", limit=50))
        self.assertEqual(len(result.data), 10)


if __name__ == "__main__":
    unittest.main()
