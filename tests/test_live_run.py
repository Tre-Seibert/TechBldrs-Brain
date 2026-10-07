from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("tb_live_run", ROOT / "scripts" / "live_run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LiveQuestionFileTests(unittest.TestCase):
    def test_blocks_split_on_blank_lines_and_comments_are_ignored(self) -> None:
        live = _load()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.txt"
            path.write_text("# heading\nOne?\n\nTwo?\nFollow up\n\n\n# more\nThree?\n", encoding="utf-8")
            self.assertEqual(live.load_conversations(path), [["One?"], ["Two?", "Follow up"], ["Three?"]])
            self.assertEqual(live.load_conversations(path, only="follow"), [["Two?", "Follow up"]])

    def test_sections_and_through_pick_parts_of_the_list(self) -> None:
        live = _load()
        text = (
            "# intro comment\n\n# Queue\nQ1?\n\nQ2?\n\n# Finding\nF1?\nF1 follow up\n\n"
            "# Time\nT1?\n\n# Email\nE1?\n\n# Regression extras (old)\nR1?\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.txt"
            path.write_text(text, encoding="utf-8")

            def flat(blocks):
                return [turn for block in blocks for turn in block]

            everything = ["Q1?", "Q2?", "F1?", "F1 follow up", "T1?", "E1?", "R1?"]
            self.assertEqual(flat(live.load_conversations(path)), everything)
            self.assertEqual(
                flat(live.load_conversations(path, through="time")),
                ["Q1?", "Q2?", "F1?", "F1 follow up", "T1?", "R1?"],
            )
            self.assertEqual(
                flat(live.load_conversations(path, through="time", extras=False)),
                ["Q1?", "Q2?", "F1?", "F1 follow up", "T1?"],
            )
            self.assertEqual(
                flat(live.load_conversations(path, sections=["queue", "email"])), ["Q1?", "Q2?", "E1?", "R1?"]
            )
            self.assertEqual(
                flat(live.load_conversations(path, sections=["finding"], only="follow")), ["F1?", "F1 follow up"]
            )
            with self.assertRaises(SystemExit):
                live.load_conversations(path, through="nope")

    def test_the_shipped_list_runs_through_time_with_the_listed_sections(self) -> None:
        live = _load()
        names = list(dict.fromkeys(name for name, _ in live.parse_sections()))
        self.assertEqual(names[0], "My queue")
        self.assertLess(names.index("Finding tickets"), names.index("Time and hours"))
        upto = [t for b in live.load_conversations(through="Time") for t in b]
        self.assertIn("Which tickets have time that isn't reviewed?", upto)
        self.assertIn("Show me ZTB-1691", upto)  # a regression extra rides along
        self.assertNotIn("What machines does ACME have?", upto)
        self.assertNotIn("When did Debe last reach out?", upto)

    def test_the_shipped_question_list_has_no_merge_command(self) -> None:
        live = _load()
        turns = [turn for block in live.load_conversations() for turn in block]
        self.assertGreater(len(turns), 40)
        self.assertFalse([t for t in turns if t.lower().startswith("merge ")])

    def test_calls_are_listed_with_their_arguments_or_the_router(self) -> None:
        live = _load()
        self.assertEqual(live.format_calls({"router": "longest_time"}), ["answered by router: longest_time"])
        lines = live.format_calls({"tool_calls": [{"name": "list_tickets", "arguments": {"stage": "open"}, "ok": True}]})
        self.assertEqual(lines, ['list_tickets({"stage": "open"})'])
        self.assertEqual(live.format_calls(None), ["no tools called"])


class LiveRunTests(unittest.TestCase):
    def test_every_question_is_asked_and_saved_with_its_tool_calls(self) -> None:
        live = _load()

        class FakeResponse:
            def json(self):
                return {"version": "t", "flow_source": "http", "llm_model": "m", "disabled_routers": []}

        class FakeClient:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url):
                return FakeResponse()

        asked: list[list[str]] = []

        def fake_ask(client, base, headers, messages):
            asked.append([m["content"] for m in messages if m["role"] == "user"])
            return "an answer", {"router": None, "tool_calls": [{"name": "list_tickets", "arguments": {}, "ok": True}]}

        with tempfile.TemporaryDirectory() as tmp:
            q = Path(tmp) / "q.txt"
            q.write_text("First?\n\nSecond?\nThird?\n", encoding="utf-8")
            ask_mod = mock.Mock(ask=fake_ask)
            eval_mod = mock.Mock(identity_headers=lambda email, secret: {}, _env_value=lambda name: "")
            with mock.patch.object(live, "RESULTS", Path(tmp)), mock.patch.object(live.httpx, "Client", FakeClient), mock.patch.object(
                live, "_load", side_effect=lambda name: ask_mod if name == "ask" else eval_mod
            ), mock.patch.object(sys, "argv", ["live_run.py", "--questions", str(q), "--label", "t"]):
                self.assertEqual(live.main(), 0)
            text = (Path(tmp) / "live-t.md").read_text(encoding="utf-8")
        self.assertEqual(asked, [["First?"], ["Second?"], ["Second?", "Third?"]])  # follow-ups share a conversation
        for question in ("First?", "Second?", "Third?"):
            self.assertIn(question, text)
        self.assertIn("list_tickets({})", text)


if __name__ == "__main__":
    unittest.main()
