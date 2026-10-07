from __future__ import annotations

import unittest
from unittest import mock

from app.flow.stub import StubFlowSource
from app.knowledge.source import KnowledgeHit
from app.tools.handlers import (
    ChatTurn,
    GetTicketDetailArgs,
    ListTicketsArgs,
    _is_client_initiated,
    _issue_query,
    dispatch,
    get_ticket_detail,
    list_tickets,
)

ALERT_TOPIC = (
    "kdeguia04286 | 3c | AMBS Ambler Surgical | performance | Disk Space Monitor | "
    "C drive has passed 10.0 GB Free for 10 mins"
)


class FakeKnowledge:
    source_name = "fake"
    configured = True

    def __init__(self, hits):
        self.hits = hits
        self.queries: list[str] = []

    def search(self, *, query, client_code=None, limit=5):
        self.queries.append(query)
        return self.hits


class IssueQueryTests(unittest.TestCase):
    def test_an_alert_title_keeps_the_issue_and_drops_who_and_where(self) -> None:
        query = _issue_query(ALERT_TOPIC)
        self.assertIn("Disk Space Monitor", query)
        self.assertNotIn("kdeguia04286", query)
        self.assertNotIn("AMBS", query)

    def test_a_plain_title_is_kept_without_labels_and_numbers(self) -> None:
        self.assertEqual(_issue_query("VPN drops again at ACME-0041 since 12 May"), "VPN drops again at since May")


class ResolveAdviceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()
        self.args = GetTicketDetailArgs(ticket_label="ACME-0041")

    def test_an_advice_question_adds_past_fixes_found_with_the_issue_text(self) -> None:
        knowledge = FakeKnowledge(
            [
                KnowledgeHit(text="Cleared temp files and the C: drive recovered 13GB", source_type="time_entry", source_label="BUCK-2001"),
                KnowledgeHit(text="own note", source_type="time_entry", source_label="ACME-0041"),
            ]
        )
        result = get_ticket_detail(
            self.source, self.args, ChatTurn(user_text="What would be the best way to resolve this ticket?"), knowledge
        )
        self.assertIn("Past fixes for similar issues", result.digest)
        self.assertIn("[BUCK-2001] Cleared temp files", result.digest)
        self.assertNotIn("own note", result.digest)  # the ticket's own notes are not a "past fix"
        self.assertIn("advice on resolving this ticket", result.digest)
        self.assertEqual(len(knowledge.queries), 1)
        self.assertNotIn("ACME-0041", knowledge.queries[0])  # the label is never the search text

    def test_no_past_fix_is_stated_not_invented(self) -> None:
        result = get_ticket_detail(
            self.source, self.args, ChatTurn(user_text="suggestions to fix this ticket"), FakeKnowledge([])
        )
        self.assertIn("Past fixes: none found", result.digest)
        self.assertIn("general IT advice, not from our records", result.footer)  # added by the code, not the model

    def test_without_a_knowledge_index_flow_supplies_past_fixes_with_the_same_cause(self) -> None:
        base = self.source.list_tickets(client_code="ACME", ticket_num="0041", stage="live")[0]
        other = base.model_copy(update={"id": 5555, "ticket_num": "0999", "hrs_actual_total": 1.5})
        entry = self.source.list_time_entries(client_code="ACME", limit=1)[0].model_copy(
            update={"subject": "Cleared temp files", "body": "Freed 13GB on the C: drive"}
        )
        with mock.patch.object(self.source, "list_tickets", wraps=self.source.list_tickets) as listed:
            with mock.patch.object(self.source, "list_time_entries", return_value=[entry]):
                listed.side_effect = lambda **kw: [other] if kw.get("cause") else [base]
                result = get_ticket_detail(self.source, self.args, ChatTurn(user_text="how do we fix this?"), None)
        self.assertIn("[ACME-0999] Cleared temp files: Freed 13GB", result.digest)
        self.assertIsNone(result.footer)

    def test_without_knowledge_or_a_matching_ticket_the_advice_is_marked_general(self) -> None:
        with mock.patch.object(self.source, "list_tickets", wraps=self.source.list_tickets) as listed:
            base = self.source.list_tickets(client_code="ACME", ticket_num="0041", stage="live")[0]
            listed.side_effect = lambda **kw: [] if kw.get("cause") else [base]
            result = get_ticket_detail(self.source, self.args, ChatTurn(user_text="how do we fix this?"), None)
        self.assertIn("Past fixes: none found", result.digest)
        self.assertIn("not from our records", result.footer)

    def test_a_plain_summary_does_not_search_for_fixes(self) -> None:
        knowledge = FakeKnowledge([])
        result = get_ticket_detail(self.source, self.args, ChatTurn(user_text="Summarize ACME-0041"), knowledge)
        self.assertEqual(knowledge.queries, [])
        self.assertNotIn("Past fixes", result.digest)


class LabelAsSearchTextTests(unittest.TestCase):
    def test_a_text_search_for_one_ticket_label_becomes_a_lookup_of_that_ticket(self) -> None:
        result = dispatch(StubFlowSource(), "list_tickets", {"q": "ACME-0041"}, ChatTurn(user_text="What is ACME-0041 about?"))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.tool, "get_ticket_detail")
        self.assertIsNotNone(result.digest)

    def test_a_real_text_search_is_left_alone(self) -> None:
        result = dispatch(StubFlowSource(), "list_tickets", {"q": "VPN", "client_code": "ACME"}, ChatTurn(user_text="tickets about VPN"))
        self.assertEqual(result.tool, "list_tickets")


class ClientInitiatedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()
        self.base = self.source.list_tickets(client_code="ACME", ticket_num="0041", stage="live")[0]

    def row(self, **update):
        return self.base.model_copy(update=update)

    def test_which_tickets_count_as_client_initiated(self) -> None:
        self.assertTrue(_is_client_initiated(self.row(requestor_text="Sean O'Brien", reason="Support", client_code="WDON")))
        self.assertFalse(_is_client_initiated(self.row(requestor_text="Sean O'Brien", reason="Alert")))
        self.assertFalse(_is_client_initiated(self.row(requestor_text="Techbldrs RMM", reason="Support")))
        self.assertFalse(_is_client_initiated(self.row(requestor_text="T1078NAS - Synology NAS", reason=None)))
        self.assertFalse(_is_client_initiated(self.row(requestor_text="admin@mail.saasalerts.com", reason=None)))
        self.assertFalse(_is_client_initiated(self.row(requestor_text="Tre Seibert", reason="Support", client_code="ZTB")))
        self.assertFalse(_is_client_initiated(self.row(requestor_text=None, reason="Support")))

    def test_the_list_leaves_out_alerts_and_says_how_many(self) -> None:
        rows = [
            self.row(id=1, ticket_num="1001", requestor_text="Tara Miller", reason="Billable/New"),
            self.row(id=2, ticket_num="1002", requestor_text="Techbldrs RMM", reason="Alert"),
            self.row(id=3, ticket_num="1003", requestor_text="no-reply@rocketcyber.com", reason=None),
        ]
        with mock.patch.object(self.source, "list_tickets", return_value=rows):
            result = list_tickets(
                self.source, ListTicketsArgs(client_initiated=True), ChatTurn(user_text="client initiated tickets today")
            )
        self.assertEqual([r["ticket_label"] for r in result.data], ["ACME-1001"])
        self.assertIn("1 client-initiated ticket(s) created today", result.reply)
        self.assertIn("Left out 2 alert or automated ticket(s)", result.reply)

    def test_it_defaults_to_today_and_a_named_period_wins(self) -> None:
        with mock.patch.object(self.source, "list_tickets", return_value=[]) as spy:
            list_tickets(self.source, ListTicketsArgs(client_initiated=True), ChatTurn(user_text="client tickets"))
            today_call = spy.call_args.kwargs
            list_tickets(self.source, ListTicketsArgs(client_initiated=True), ChatTurn(user_text="client tickets last week"))
            week_call = spy.call_args.kwargs
        self.assertIsNone(today_call["created_before"])
        self.assertIsNotNone(week_call["created_before"])
        self.assertLess(week_call["created_after"], today_call["created_after"])

    def test_exclude_alerts_drops_alert_tickets_from_any_list(self) -> None:
        rows = [self.row(id=1, ticket_num="1001", reason="Support"), self.row(id=2, ticket_num="1002", reason="Alert")]
        with mock.patch.object(self.source, "list_tickets", return_value=rows):
            result = list_tickets(self.source, ListTicketsArgs(client_code="ACME", stage="open", exclude_alerts=True))
        self.assertEqual([r["ticket_label"] for r in result.data], ["ACME-1001"])


if __name__ == "__main__":
    unittest.main()
