from __future__ import annotations

import unittest

from app.flow.stub import StubFlowSource
from app.tools.handlers import (
    LatestTicketArgs,
    ListMailArgs,
    SearchContactArgs,
    latest_ticket,
    list_mail,
    search_contact,
)


class ToolStubTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()

    def test_search_contact_debe(self) -> None:
        result = search_contact(self.source, SearchContactArgs(query="Debe"))
        self.assertTrue(result.ok)
        self.assertEqual(result.row_ids, [101])
        self.assertEqual(result.client_code, "WDON")
        self.assertEqual(result.data[0]["email_1"], "debe@westerndental.example")

    def test_latest_ticket_wdon(self) -> None:
        result = latest_ticket(self.source, LatestTicketArgs(client_code="WDON"))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["ticket_label"], "WDON-1842")
        self.assertEqual(result.data["topic"], "Imaging workstation offline")
        self.assertEqual(result.row_ids, [9001])

    def test_list_mail_wdon_inbound(self) -> None:
        result = list_mail(
            self.source,
            ListMailArgs(client_code="WDON", direction="inbound"),
        )
        self.assertTrue(result.ok)
        self.assertGreaterEqual(len(result.data), 3)
        latest = result.data[0]
        self.assertEqual(latest["direction"], "inbound")
        self.assertEqual(latest["from_address"], "debe@westerndental.example")
        self.assertTrue(latest["received_at"].startswith("2026-09-18"))

    def test_debe_last_reach_out_chain(self) -> None:
        found = search_contact(self.source, SearchContactArgs(query="Debe"))
        contact_id = found.data[0]["id"]
        mail = list_mail(
            self.source,
            ListMailArgs(client_code="WDON", direction="inbound", contact_id=contact_id),
        )
        self.assertEqual(mail.data[0]["id"], 50003)
        self.assertIn("Debe", mail.data[0]["from_name"])

    def test_does_not_cross_clients(self) -> None:
        mail = list_mail(self.source, ListMailArgs(client_code="WDON", direction="all"))
        codes = {row["client_code"] for row in mail.data}
        self.assertEqual(codes, {"WDON"})


if __name__ == "__main__":
    unittest.main()
