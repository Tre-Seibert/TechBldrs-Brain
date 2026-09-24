from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.agent.loop import _ignored_client_tool_names, chat_turn_from_messages
from app.main import app
from app.tools import openai_tools

EXPECTED_TOOLS = [
    "search_contact",
    "search_technician",
    "list_tickets",
    "latest_ticket",
    "find_similar_tickets",
    "merge_tickets",
    "list_mail",
]


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._cm = TestClient(app)
        self.client = self._cm.__enter__()

    def tearDown(self) -> None:
        self._cm.__exit__(None, None, None)

    def test_client_tools_are_not_forwarded(self) -> None:
        ignored = _ignored_client_tool_names(
            [
                {
                    "type": "function",
                    "function": {"name": "update_task", "parameters": {"type": "object"}},
                },
                {
                    "type": "function",
                    "function": {"name": "latest_ticket", "parameters": {"type": "object"}},
                },
            ]
        )
        self.assertEqual(ignored, ["update_task"])
        names = [tool["function"]["name"] for tool in openai_tools()]
        self.assertEqual(names, EXPECTED_TOOLS)

    def test_models_advertises_tb_brain(self) -> None:
        response = self.client.get("/v1/models")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([item["id"] for item in body["data"]], ["tb-brain"])

    def test_health(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["read_only"])
        self.assertEqual(body["tools"], EXPECTED_TOOLS)
        self.assertEqual(body["write_tools"], ["merge_tickets"])

    def test_tool_latest_ticket(self) -> None:
        response = self.client.post("/tools/latest_ticket", json={"client_code": "WDON"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["data"]["ticket_label"], "WDON-1842")
        self.assertEqual(body["row_ids"], [9001])

    def test_tool_list_tickets_by_assignee(self) -> None:
        response = self.client.post("/tools/list_tickets", json={"assignee_code": "Tre"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"], body["error"])
        labels = [row["ticket_label"] for row in body["data"]]
        self.assertEqual(labels, ["ACME-0041", "ACME-0045"])
        self.assertEqual(body["row_ids"], [3100, 3105])

    def test_tool_list_tickets_without_scope_is_422(self) -> None:
        response = self.client.post("/tools/list_tickets", json={"status": "Open"})
        self.assertEqual(response.status_code, 422)

    def test_tool_search_technician(self) -> None:
        response = self.client.post("/tools/search_technician", json={"query": "tseibert"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["assignee_code"], "ts")

    def test_tool_find_similar_tickets(self) -> None:
        response = self.client.post("/tools/find_similar_tickets", json={"assignee_code": "ts"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["read_only"])
        self.assertEqual(body["data"][0]["keep"]["ticket_label"], "ACME-0041")
        self.assertEqual(body["data"][0]["absorb"]["ticket_label"], "ACME-0045")

    def test_merge_has_no_direct_tool_route(self) -> None:
        response = self.client.post(
            "/tools/merge_tickets",
            json={"target_ticket_id": 3100, "source_ticket_ids": [3105], "confirm": True},
        )
        self.assertIn(response.status_code, (404, 405))

    def test_chat_turn_reads_latest_user_and_prior_assistant(self) -> None:
        turn = chat_turn_from_messages(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "what tickets need merged for ACME?"},
                {"role": "assistant", "content": "Keep ACME-0041, absorb ACME-0045?"},
                {"role": "user", "content": [{"type": "text", "text": "merge ACME-0045 into ACME-0041"}]},
            ]
        )
        self.assertEqual(turn.user_text, "merge ACME-0045 into ACME-0041")
        self.assertEqual(turn.previous_assistant_text, "Keep ACME-0041, absorb ACME-0045?")
