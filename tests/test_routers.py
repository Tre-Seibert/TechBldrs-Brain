from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.agent.loop import direct_answer
from app.config import ROUTER_NAMES, Settings
from app.flow.stub import StubFlowSource
from app.tools import ChatTurn

# One question each router answers on the stub fixtures.
_QUESTIONS = {
    "merge_suggestion": "Does any tickets need merged?",
    "person_mail": "when did Sean O'Brien last reach out?",
    "person_ticket": "Tell me the latest ticket involving Michael Sodl",
    "tickets_about": "What tickets are about B Ellerby Email features?",
    "longest_time": "Which open ticket has the longest time worked",
}


class RouterSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()

    def test_every_router_has_a_question_here(self) -> None:
        self.assertEqual(set(_QUESTIONS), set(ROUTER_NAMES))

    def test_all_routers_answer_by_default(self) -> None:
        for name, question in _QUESTIONS.items():
            result = direct_answer(self.source, ChatTurn(user_text=question))
            self.assertIsNotNone(result, name)

    def test_disabling_a_router_hands_its_question_to_the_llm(self) -> None:
        for name, question in _QUESTIONS.items():
            result = direct_answer(self.source, ChatTurn(user_text=question), frozenset({name}))
            self.assertIsNone(result, name)

    def test_disabling_one_router_leaves_the_others_on(self) -> None:
        disabled = frozenset({"longest_time"})
        for name, question in _QUESTIONS.items():
            if name == "longest_time":
                continue
            result = direct_answer(self.source, ChatTurn(user_text=question), disabled)
            self.assertIsNotNone(result, name)

    def test_setting_parses_names_and_defaults_to_none_disabled(self) -> None:
        self.assertEqual(Settings(disabled_routers="").disabled_router_set, frozenset())
        parsed = Settings(disabled_routers=" Longest_Time , tickets_about ").disabled_router_set
        self.assertEqual(parsed, frozenset({"longest_time", "tickets_about"}))

    def test_a_typo_fails_loudly_instead_of_leaving_the_router_on(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(disabled_routers="longest_tme")


if __name__ == "__main__":
    unittest.main()


class EvalSetTests(unittest.TestCase):
    """eval/questions.json expectations must hold for the router-answered questions.

    That keeps the expected answers honest: if a router passes its own eval cases, a
    failing model on the same case is the model's fault, not a typo in the expectation.
    """

    def test_router_answered_cases_pass_their_own_checks(self) -> None:
        import importlib.util
        from pathlib import Path

        from app.agent.loop import chat_turn_from_messages

        path = Path(__file__).resolve().parent.parent / "scripts" / "eval.py"
        spec = importlib.util.spec_from_file_location("tb_eval", path)
        assert spec and spec.loader
        evalmod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(evalmod)

        source = StubFlowSource()
        cases = evalmod.load_cases()
        self.assertGreaterEqual(len(cases), 20)
        self.assertEqual(len({case["id"] for case in cases}), len(cases), "duplicate case ids")
        answered = 0
        for case in cases:
            result = direct_answer(source, chat_turn_from_messages(case["messages"]))
            if result is None:
                continue  # Handled by the model; scored only by scripts/eval.py against a live server.
            answered += 1
            failures = evalmod.check_reply(case, result.reply or "")
            self.assertEqual(failures, [], case["id"])
        self.assertGreaterEqual(answered, 15)
