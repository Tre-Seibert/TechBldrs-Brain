from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.knowledge.source import KnowledgeHit, NullKnowledgeSource
from app.tools.handlers import SearchKnowledgeArgs, search_knowledge
from scripts.sync_knowledge import _chunk_text


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
