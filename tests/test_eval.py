from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

from app.tools.registry import OPENAI_TOOLS, TOOL_NAMES

ROOT = Path(__file__).resolve().parent.parent


def _load_eval():
    spec = importlib.util.spec_from_file_location("tb_eval", ROOT / "scripts" / "eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


evalmod = _load_eval()
SCHEMAS = {t["function"]["name"]: t["function"]["parameters"]["properties"] for t in OPENAI_TOOLS}


def _specs(case: dict):
    for want in case.get("expect") or []:
        yield from (want["any"] if "any" in want else [want])
    yield from case.get("forbid_calls") or []


class FlowCaseFileTests(unittest.TestCase):
    """eval/flow_cases.json must only name tools and arguments that exist."""

    def setUp(self) -> None:
        self.cases = evalmod.load_cases(ROOT / "eval" / "flow_cases.json")

    def test_ids_are_unique_and_cases_are_scored(self) -> None:
        ids = [case["id"] for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)))
        for case in self.cases:
            scored = any(
                case.get(key)
                for key in ("expect", "forbid_calls", "expect_no_calls", "must_contain", "must_match", "must_not_match")
            )
            self.assertTrue(scored, f"{case['id']} checks nothing")

    def test_specs_use_real_tools_and_arguments(self) -> None:
        for case in self.cases:
            for spec in _specs(case):
                tools = spec["tool"] if isinstance(spec["tool"], list) else [spec["tool"]]
                for tool in tools:
                    self.assertIn(tool, TOOL_NAMES, case["id"])
                    for arg in spec.get("args", {}):
                        self.assertIn(arg, SCHEMAS[tool], f"{case['id']}: {tool} has no argument {arg!r}")

    def test_routers_ok_names_are_real(self) -> None:
        from app.config import ROUTER_NAMES

        for case in self.cases:
            for name in case.get("routers_ok", []):
                self.assertIn(name, ROUTER_NAMES, case["id"])


class CallScoringTests(unittest.TestCase):
    def call(self, name: str, **arguments):
        return {"name": name, "arguments": arguments, "ok": True}

    def test_arg_matching_rules(self) -> None:
        m = evalmod._arg_matches
        self.assertTrue(m("OPEN", "open"))
        self.assertTrue(m("ts", ["me", "ts"]))
        self.assertFalse(m("tr", ["me", "ts"]))
        self.assertTrue(m("2026-10-01", {"re": r"^\d{4}-\d{2}-\d{2}"}))
        self.assertFalse(m(None, "*"))
        self.assertTrue(m("x", "*"))
        self.assertTrue(m(True, True))
        self.assertFalse(m("true", True))  # a string is not a boolean

    def test_expect_passes_and_fails(self) -> None:
        case = {"expect": [{"tool": "list_tickets", "args": {"stage": "open"}}]}
        ok = {"tool_calls": [self.call("list_tickets", stage="open", client_code="ACME")]}
        bad = {"tool_calls": [self.call("list_tickets", stage="live")]}
        self.assertEqual(evalmod.check_calls(case, ok), [])
        self.assertEqual(len(evalmod.check_calls(case, bad)), 1)
        self.assertEqual(len(evalmod.check_calls(case, {})), 1)

    def test_any_alternatives(self) -> None:
        case = {"expect": [{"any": [{"tool": "list_mail"}, {"tool": "list_tickets"}]}]}
        self.assertEqual(evalmod.check_calls(case, {"tool_calls": [self.call("list_tickets")]}), [])
        self.assertEqual(len(evalmod.check_calls(case, {"tool_calls": [self.call("search_contact")]})), 1)

    def test_router_counts_only_when_allowed(self) -> None:
        case = {"expect": [{"tool": "list_tickets"}], "routers_ok": ["longest_time"]}
        self.assertEqual(evalmod.check_calls(case, {"router": "longest_time", "tool_calls": []}), [])
        self.assertEqual(len(evalmod.check_calls(case, {"router": "person_mail", "tool_calls": []})), 1)

    def test_forbidden_call_fails_even_when_router_allowed(self) -> None:
        case = {
            "expect": [{"tool": "find_similar_tickets"}],
            "routers_ok": ["merge_suggestion"],
            "forbid_calls": [{"tool": "merge_tickets", "args": {"confirm": True}}],
        }
        trace = {"tool_calls": [self.call("merge_tickets", confirm=True)]}
        self.assertTrue(any("forbidden" in f for f in evalmod.check_calls(case, trace)))
        self.assertEqual(
            evalmod.check_calls(case, {"tool_calls": [self.call("find_similar_tickets"), self.call("merge_tickets", confirm=False)]}),
            [],
        )

    def test_expect_no_calls(self) -> None:
        case = {"expect_no_calls": True}
        self.assertEqual(evalmod.check_calls(case, {"tool_calls": []}), [])
        self.assertEqual(len(evalmod.check_calls(case, {"tool_calls": [self.call("list_tickets")]})), 1)
        self.assertEqual(len(evalmod.check_calls(case, {"router": "longest_time", "tool_calls": []})), 1)
