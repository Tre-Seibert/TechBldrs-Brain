from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.agent.loop import _ignored_client_tool_names
from app.main import app
from app.tools import openai_tools


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
        self.assertEqual(names, ["search_contact", "latest_ticket", "list_mail"])

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
        self.assertEqual(body["tools"], ["search_contact", "latest_ticket", "list_mail"])

    def test_tool_latest_ticket(self) -> None:
        response = self.client.post("/tools/latest_ticket", json={"client_code": "WDON"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["data"]["ticket_label"], "WDON-1842")
        self.assertEqual(body["row_ids"], [9001])
