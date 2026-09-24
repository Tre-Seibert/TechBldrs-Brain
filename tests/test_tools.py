from __future__ import annotations

import unittest
from unittest import mock

from app.config import Settings
from app.flow.http import HttpFlowSource
from app.flow.stub import StubFlowSource
from app.identity import current_actor_email, current_signed_in_email
from app.tools import ChatTurn, run_tool
from app.tools.handlers import (
    FindSimilarTicketsArgs,
    LatestTicketArgs,
    ListMailArgs,
    ListTicketsArgs,
    MergeTicketsArgs,
    SearchContactArgs,
    SearchTechnicianArgs,
    find_similar_tickets,
    format_similar_list,
    latest_ticket,
    list_mail,
    list_tickets,
    merge_tickets,
    search_contact,
    search_technician,
)

_PLAN = "Keep ACME-0041 and absorb ACME-0045? Reply 'merge ACME-0045 into ACME-0041' to confirm."


def _merge_args(**overrides: object) -> MergeTicketsArgs:
    values: dict[str, object] = {
        "target_ticket_id": 3100,
        "source_ticket_ids": [3105],
        "target_label": "ACME-0041",
        "source_labels": ["ACME-0045"],
        "confirm": True,
    }
    values.update(overrides)
    return MergeTicketsArgs(**values)


class ToolStubTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()

    def _ticket_ids(self) -> set[int]:
        return {row.id for row in self.source.list_tickets(client_code="ACME", stage="open")}

    def test_search_contact_debe(self) -> None:
        result = search_contact(self.source, SearchContactArgs(query="Debe"))
        self.assertTrue(result.ok)
        self.assertEqual(result.row_ids, [101])
        self.assertEqual(result.client_code, "WDON")
        self.assertEqual(result.data[0]["email_1"], "debe@westerndental.example")

    def test_search_technician_resolves_name_email_and_code(self) -> None:
        for query in ("Tre", "tseibert", "ts", "TS"):
            result = search_technician(self.source, SearchTechnicianArgs(query=query))
            self.assertTrue(result.ok, query)
            self.assertEqual(result.data[0]["assignee_code"], "ts", query)
            self.assertEqual(result.data[0]["display_name"], "Tre Seibert")
        # "tr" is Tom's code even though "Tre" contains "tr".
        result = search_technician(self.source, SearchTechnicianArgs(query="tr"))
        self.assertEqual(result.data[0]["assignee_code"], "tr")

    def test_search_technician_then_list_tickets(self) -> None:
        tech = search_technician(self.source, SearchTechnicianArgs(query="Tre"))
        code = tech.data[0]["assignee_code"]
        result = list_tickets(self.source, ListTicketsArgs(assignee_code=code))
        self.assertTrue(result.ok)
        self.assertEqual([row["ticket_label"] for row in result.data], ["ACME-0041", "ACME-0045"])
        self.assertIn("Want archived tickets too?", result.reply or "")
        self.assertNotIn("ACME-0050", result.reply or "")

    def test_list_tickets_me_uses_signed_in_email(self) -> None:
        token = current_signed_in_email.set("tseibert@techbldrs.example")
        try:
            result = list_tickets(self.source, ListTicketsArgs(assignee_code="me"))
        finally:
            current_signed_in_email.reset(token)
        self.assertTrue(result.ok)
        self.assertEqual([row["ticket_label"] for row in result.data], ["ACME-0041", "ACME-0045"])
        self.assertIn("Want archived tickets too?", result.reply or "")

    def test_list_tickets_my_as_two_letter_token_is_still_self(self) -> None:
        token = current_signed_in_email.set("tseibert@techbldrs.example")
        try:
            result = list_tickets(self.source, ListTicketsArgs(assignee_code="my"))
        finally:
            current_signed_in_email.reset(token)
        self.assertTrue(result.ok)
        self.assertEqual({row["assignee_code"] for row in result.data}, {"ts"})

    def test_list_tickets_me_without_sign_in_does_not_search_names(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(assignee_code="me"))
        self.assertFalse(result.ok)
        self.assertIn("signed in", result.error or "")
        self.assertNotIn("Kimora", result.error or "")
        self.assertNotIn("Sammer", result.error or "")

    def test_search_technician_me_returns_signed_in_tech(self) -> None:
        token = current_signed_in_email.set("tseibert@techbldrs.example")
        try:
            result = search_technician(self.source, SearchTechnicianArgs(query="me"))
        finally:
            current_signed_in_email.reset(token)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.data), 1)
        self.assertEqual(result.data[0]["assignee_code"], "ts")

    def test_list_tickets_resolves_a_technician_name(self) -> None:
        # A 7B model sometimes passes the name straight through; the handler
        # resolves it with search_technician rather than an alias table.
        result = list_tickets(self.source, ListTicketsArgs(assignee_code="Tre"))
        self.assertTrue(result.ok)
        self.assertEqual({row["assignee_code"] for row in result.data}, {"ts"})
        self.assertEqual(len(result.data), 2)

    def test_list_tickets_unknown_technician_errors(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(assignee_code="Nobody"))
        self.assertFalse(result.ok)
        self.assertIn("No active technician", result.error)

    def test_list_tickets_by_assignee_code_is_case_insensitive(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(assignee_code="TR"))
        self.assertEqual([row["ticket_label"] for row in result.data], ["WDON-1842", "WDON-1830"])

    def test_list_tickets_client_and_assignee(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(client_code="WDON", assignee_code="ts"))
        self.assertEqual(result.data, [])

    def test_list_tickets_text_search(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(q="imaging"))
        self.assertEqual([row["ticket_label"] for row in result.data], ["WDON-1842"])

    def test_list_tickets_requires_a_scope(self) -> None:
        with self.assertRaises(ValueError):
            ListTicketsArgs(status="Open")

    def test_latest_ticket_wdon(self) -> None:
        result = latest_ticket(self.source, LatestTicketArgs(client_code="WDON"))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["ticket_label"], "WDON-1842")
        self.assertEqual(result.data["topic"], "Imaging workstation offline")
        self.assertEqual(result.row_ids, [9001])

    def test_find_similar_tickets_returns_fixture_pair(self) -> None:
        result = find_similar_tickets(self.source, FindSimilarTicketsArgs(client_code="ACME"))
        self.assertTrue(result.ok)
        self.assertTrue(result.read_only)
        self.assertEqual(len(result.data), 1)
        pair = result.data[0]
        self.assertEqual(pair["keep"]["ticket_label"], "ACME-0041")
        self.assertEqual(pair["absorb"]["ticket_label"], "ACME-0045")
        self.assertIn("similar_topic", pair["reasons"])
        self.assertIn("same_contact", pair["reasons"])
        self.assertEqual(self._ticket_ids(), {3100, 3105})

    def test_find_similar_tickets_without_scope_scans_all_open(self) -> None:
        result = find_similar_tickets(self.source, FindSimilarTicketsArgs())
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data[0]["keep"]["ticket_label"], "ACME-0041")
        self.assertEqual(result.data[0]["absorb"]["ticket_label"], "ACME-0045")
        self.assertIn("all open tickets", result.reply or "")
        self.assertIn("Keep ACME-0041, absorb ACME-0045", result.reply or "")
        self.assertIn("To merge: merge ACME-0045 into ACME-0041", result.reply or "")
        self.assertNotIn("last activity", result.reply or "")
        self.assertNotIn("your tickets", result.reply or "")

    def test_merge_reply_shows_only_decision_facts(self) -> None:
        reply = format_similar_list(
            [
                {
                    "keep": {
                        "ticket_label": "STRX-0025",
                        "topic": "TechBldrs Quote for New Network Line",
                        "requestor_text": "Joseph Awe",
                        "assignee_code": "ja",
                    },
                    "absorb": {
                        "ticket_label": "STRX-0026",
                        "topic": "TechBldrs Quote for New Network Line",
                        "requestor_text": "Dani Lindsay",
                        "assignee_code": None,
                    },
                    "reasons": ["similar_topic", "similar_subject"],
                }
            ],
            heading="Possible merges in all open tickets (1)",
        )
        self.assertIn("Keep STRX-0025, absorb STRX-0026 — TechBldrs Quote for New Network Line", reply)
        self.assertIn("Different requestors (Joseph Awe vs Dani Lindsay)", reply)
        self.assertIn("Assignees ja vs unassigned", reply)
        self.assertIn("To merge: merge STRX-0026 into STRX-0025", reply)
        self.assertNotIn("created", reply)
        self.assertNotIn("stage open", reply)

    def test_list_tickets_assigned_defaults_to_open_not_review(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(assignee_code="ts", stage="review"))
        self.assertEqual([row["ticket_label"] for row in result.data], ["ACME-0050"])
        archived = list_tickets(self.source, ListTicketsArgs(assignee_code="ts", stage="archived"))
        self.assertEqual([row["ticket_label"] for row in archived.data], ["ACME-0010"])

    def test_list_tickets_assigned_ignores_model_live(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(assignee_code="ts", stage="live"))
        self.assertEqual([row["ticket_label"] for row in result.data], ["ACME-0041", "ACME-0045"])
        self.assertIn("Want archived tickets too?", result.reply or "")
        self.assertNotIn("(live)", result.reply or "")

    def test_list_tickets_empty_archived_does_not_invent_reason(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(assignee_code="tr", stage="archived"))
        self.assertEqual(result.data, [])
        self.assertEqual(result.reply, "No archived tickets assigned to tr.")
        self.assertNotIn("reviewed and closed", result.reply or "")

    def test_list_tickets_reply_is_english_lines(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(assignee_code="Tre"))
        self.assertIn("ACME-0041", result.reply or "")
        self.assertIn("ACME-0045", result.reply or "")
        self.assertNotRegex(result.reply or "", r"[\u4e00-\u9fff]")
        self.assertNotIn("client_id", result.reply or "")

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


class MergeGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()

    def _ticket_ids(self) -> set[int]:
        return {row.id for row in self.source.list_tickets(client_code="ACME", stage="open")}

    def _assert_refused(self, args: MergeTicketsArgs, turn: ChatTurn | None, fragment: str) -> None:
        result = merge_tickets(self.source, args, turn)
        self.assertFalse(result.ok)
        self.assertTrue(result.read_only)
        self.assertIn(fragment, result.error)
        self.assertEqual(self._ticket_ids(), {3100, 3105})

    def test_confirm_false_does_not_mutate(self) -> None:
        turn = ChatTurn(user_text="merge ACME-0045 into ACME-0041", previous_assistant_text=_PLAN)
        self._assert_refused(_merge_args(confirm=False), turn, "confirm is false")

    def test_bare_yes_is_not_approval(self) -> None:
        for reply in ("ok", "yes", "do it", "Yes please merge them"):
            self._assert_refused(
                _merge_args(), ChatTurn(user_text=reply, previous_assistant_text=_PLAN), "does not name"
            )

    def test_labels_required(self) -> None:
        turn = ChatTurn(user_text="merge ACME-0045 into ACME-0041", previous_assistant_text=_PLAN)
        self._assert_refused(_merge_args(target_label=None, source_labels=[]), turn, "target_label")

    def test_plan_must_be_shown_first(self) -> None:
        turn = ChatTurn(user_text="merge ACME-0045 into ACME-0041", previous_assistant_text="")
        self._assert_refused(_merge_args(), turn, "not shown this plan")

    def test_no_chat_context_refuses(self) -> None:
        self._assert_refused(_merge_args(), None, "only runs from chat")

    def test_backwards_direction_refuses(self) -> None:
        turn = ChatTurn(user_text="merge ACME-0041 into ACME-0045", previous_assistant_text=_PLAN)
        self._assert_refused(_merge_args(), turn, "opposite direction")

    def test_mismatched_ids_refuse(self) -> None:
        turn = ChatTurn(user_text="merge ACME-0045 into ACME-0041", previous_assistant_text=_PLAN)
        self._assert_refused(_merge_args(target_ticket_id=9001), turn, "is ticket id 3100")

    def test_restated_labels_merge_on_stub(self) -> None:
        turn = ChatTurn(user_text="Yes, merge ACME-0045 into ACME-0041", previous_assistant_text=_PLAN)
        result = merge_tickets(self.source, _merge_args(), turn)
        self.assertTrue(result.ok, result.error)
        self.assertFalse(result.read_only)
        self.assertEqual(result.data["merged_source_labels"], ["ACME-0045"])
        self.assertEqual(self._ticket_ids(), {3100})
        # Fixtures are per-instance: a fresh stub still has both tickets.
        self.assertEqual(
            {row.id for row in StubFlowSource().list_tickets(client_code="ACME", stage="open")},
            {3100, 3105},
        )

    def test_http_write_without_verified_actor_fails_closed(self) -> None:
        source = HttpFlowSource(base_url="https://flow.example", token="token")
        resolved = [
            {"id": 3100, "client_id": 7, "client_code": "ACME", "ticket_num": "0041", "subject": "s",
             "topic": "t", "created_at": "2026-09-10 08:00:00", "last_activity_at": "2026-09-10 08:00:00"},
            {"id": 3105, "client_id": 7, "client_code": "ACME", "ticket_num": "0045", "subject": "s",
             "topic": "t", "created_at": "2026-09-19 08:00:00", "last_activity_at": "2026-09-19 08:00:00"},
        ]
        turn = ChatTurn(user_text="merge ACME-0045 into ACME-0041", previous_assistant_text=_PLAN)
        settings = Settings(flow_mode="stub", brain_data_dir="C:/nonexistent-tb-brain-test")
        with (
            mock.patch.object(HttpFlowSource, "_get", side_effect=[[resolved[0]], [resolved[1]]]),
            mock.patch("app.flow.http.httpx.post") as post,
            mock.patch("app.tools.log_tool_call"),
        ):
            result = run_tool(
                source=source,
                name="merge_tickets",
                arguments=_merge_args().model_dump(),
                actor="lab-local",
                settings=settings,
                turn=turn,
            )
        self.assertFalse(result.ok)
        self.assertIn("Sign in", result.error)
        post.assert_not_called()

    def test_http_write_sends_verified_actor(self) -> None:
        source = HttpFlowSource(base_url="https://flow.example", token="token")
        response = mock.Mock(status_code=200)
        response.json.return_value = {"ok": True, "data": {"status": "merged"}}
        token = current_actor_email.set("tseibert@techbldrs.example")
        try:
            with mock.patch("app.flow.http.httpx.post", return_value=response) as post:
                source.merge_tickets(target_ticket_id=3100, source_ticket_ids=[3105])
        finally:
            current_actor_email.reset(token)
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["X-Brain-Actor-Email"], "tseibert@techbldrs.example")
        self.assertEqual(
            post.call_args.kwargs["json"],
            {"target_ticket_id": 3100, "source_ticket_ids": [3105], "confirm": True},
        )


if __name__ == "__main__":
    unittest.main()
