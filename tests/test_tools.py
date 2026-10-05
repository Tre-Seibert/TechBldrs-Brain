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
    GetClientDetailArgs,
    GetMailDetailArgs,
    GetTicketDetailArgs,
    LatestTicketArgs,
    ListMachinesArgs,
    ListMailArgs,
    ListTicketsArgs,
    ListTimeEntriesArgs,
    MergeTicketsArgs,
    SearchContactArgs,
    SearchTechnicianArgs,
    TicketStatsArgs,
    _asked_latest_for_person,
    _contact_name_score,
    _damerau,
    _person_name_from_turn,
    answer_longest_time_worked,
    answer_merge_suggestion,
    answer_person_mail_question,
    answer_person_ticket_question,
    answer_tickets_about,
    find_similar_tickets,
    format_similar_list,
    get_client_detail,
    get_mail_detail,
    get_ticket_detail,
    latest_ticket,
    list_machines,
    list_mail,
    list_tickets,
    list_time_entries,
    merge_tickets,
    search_contact,
    search_technician,
    ticket_stats,
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
        return {
            row.id
            for row in self.source.list_tickets(client_code="ACME", stage="open")
            if row.assignee_code == "ts"
        }

    def test_search_contact_debe(self) -> None:
        result = search_contact(self.source, SearchContactArgs(query="Debe"))
        self.assertTrue(result.ok)
        self.assertEqual(result.row_ids, [101])
        self.assertEqual(result.client_code, "WDON")
        self.assertEqual(result.data[0]["email_1"], "debe@westerndental.example")

    def test_tickets_for_requestor_not_whole_client(self) -> None:
        result = list_tickets(
            self.source,
            ListTicketsArgs(q="Micahel Sodl"),
            ChatTurn(user_text="What tickets are open for Micahel Sodl?"),
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual([row["ticket_label"] for row in result.data], ["ACME-0400"])
        self.assertIn("Michael Sodl", result.reply or "")
        self.assertNotIn("ACME-0041", result.reply or "")

    def test_search_contact_for_tickets_lists_requestor(self) -> None:
        result = search_contact(
            self.source,
            SearchContactArgs(query="Sodl"),
            ChatTurn(user_text="What tickets are open for Michael Sodl?"),
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.tool, "list_tickets")
        self.assertEqual([row["ticket_label"] for row in result.data], ["ACME-0400"])

    def test_tickets_for_requestor_tolerates_first_name_typo(self) -> None:
        result = search_contact(
            self.source,
            SearchContactArgs(query="Micahel"),
            ChatTurn(user_text="What tickets are open for Micahel Sodl?"),
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.tool, "list_tickets")
        self.assertEqual([row["ticket_label"] for row in result.data], ["ACME-0400"])
        self.assertIn("Michael Sodl", result.reply or "")
        self.assertNotIn("for WIDG", result.reply or "")

    def test_contact_id_heading_uses_the_person_not_the_client(self) -> None:
        result = list_tickets(
            self.source,
            ListTicketsArgs(contact_id=202, stage="open"),
            ChatTurn(user_text="What tickets are open for Michael Sodl?"),
        )
        self.assertTrue(result.ok, result.error)
        self.assertIn("Michael Sodl", result.reply or "")
        self.assertNotIn("for ACME", result.reply or "")

    def test_apostrophe_does_not_split_the_same_person(self) -> None:
        self.assertEqual(_contact_name_score("Sean OBrien", "Sean O'Brien"), 0)
        self.assertEqual(_contact_name_score("Sean O’Brien", "Sean O'Brien"), 0)
        self.assertIsNone(_contact_name_score("Sean OBrien", "April O'Brien"))

    def test_last_reach_out_uses_requestor_not_contacts(self) -> None:
        turn = ChatTurn(user_text="when did Sean O'Brien last reach out?")
        result = answer_person_mail_question(self.source, turn)
        self.assertIsNotNone(result)
        self.assertTrue(result.ok, result.error)
        self.assertIn("Sean O'Brien last reached out on Sep 10, 2026 7:44 AM", result.reply or "")
        self.assertIn("WDON-2621", result.reply or "")
        self.assertIn("Create Payroll Email Address", result.reply or "")
        self.assertNotIn("April", result.reply or "")
        self.assertNotIn("contact", (result.reply or "").lower())
        self.assertNotIn("WDON-2622", result.reply or "")

    def test_last_email_ignores_missing_apostrophe(self) -> None:
        result = answer_person_mail_question(
            self.source,
            ChatTurn(user_text="when did Sean OBrien last email us?"),
        )
        self.assertIsNotNone(result)
        self.assertIn("WDON-2621", result.reply or "")
        self.assertNotIn("April", result.reply or "")

    def test_last_reach_out_does_not_ask_which_obrien(self) -> None:
        result = search_contact(
            self.source,
            SearchContactArgs(query="Sean OBrien"),
            ChatTurn(user_text="when did Sean O’Brien last reach out?"),
        )
        self.assertTrue(result.ok, result.error)
        self.assertNotIn("matches more than one", result.reply or "")
        self.assertIn("WDON-2621", result.reply or "")

    def test_client_mail_question_is_not_a_person_reach_out(self) -> None:
        self.assertIsNone(
            answer_person_mail_question(
                self.source,
                ChatTurn(user_text="Show all emails from WDON to our tenant"),
            )
        )

    def test_debe_last_reach_out_is_her_inbound_mail(self) -> None:
        result = answer_person_mail_question(
            self.source,
            ChatTurn(user_text="When did Debe last reach out?"),
        )
        self.assertIsNotNone(result)
        self.assertIn("Debe Hernandez last reached out on Sep 18, 2026 9:40 AM", result.reply or "")
        self.assertIn("WDON-1842", result.reply or "")

    def test_first_name_typo_scores_as_same_person(self) -> None:
        self.assertEqual(_damerau("Micahel", "Michael"), 1)
        self.assertEqual(_contact_name_score("Micahel Sodl", "Michael Sodl"), 1)
        self.assertEqual(_contact_name_score("Micahel Sodl", "Sodl, Michael"), 1)
        self.assertIsNone(_contact_name_score("Micahel Sodl", "Riley Chen"))

    def test_latest_ticket_involving_requestor(self) -> None:
        turn = ChatTurn(user_text="Tell me the latest ticket involving Michael Sodl")
        self.assertEqual(_person_name_from_turn(turn), "Michael Sodl")
        self.assertTrue(_asked_latest_for_person(turn))
        result = answer_person_ticket_question(self.source, turn)
        self.assertIsNotNone(result)
        self.assertTrue(result.ok, result.error)
        self.assertEqual([row["ticket_label"] for row in result.data], ["ACME-0400"])
        self.assertIn("Latest ticket for Michael Sodl", result.reply or "")
        self.assertNotIn("Western Dental", result.reply or "")
        self.assertNotIn("WDON-1842", result.reply or "")

    def test_latest_ticket_involving_unknown_person_is_english(self) -> None:
        result = answer_person_ticket_question(
            self.source,
            ChatTurn(user_text="Tell me the latest ticket involving Nobody Fakename"),
        )
        self.assertIsNotNone(result)
        self.assertIn("No ticket found for Nobody Fakename", result.reply or "")
        self.assertNotRegex(result.reply or "", r"[\u0E00-\u0E7F]")
        self.assertNotIn("Western Dental", result.reply or "")

    def test_latest_ticket_involving_thomas_carter_uses_subject(self) -> None:
        result = answer_person_ticket_question(
            self.source,
            ChatTurn(user_text="Tell me the latest ticket involving Thomas Carter"),
        )
        self.assertIsNotNone(result)
        self.assertTrue(result.ok, result.error)
        self.assertEqual([row["ticket_label"] for row in result.data], ["WDON-2619"])
        self.assertIn("Latest ticket for Thomas Carter", result.reply or "")
        self.assertIn("WDON-2619", result.reply or "")
        self.assertNotIn("more information", (result.reply or "").lower())

    def test_latest_ticket_ignores_invented_wdon_when_person_named(self) -> None:
        result = latest_ticket(
            self.source,
            LatestTicketArgs(client_code="WDON"),
            ChatTurn(user_text="Tell me the latest ticket involving Michael Sodl"),
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual([row["ticket_label"] for row in result.data], ["ACME-0400"])
        self.assertNotEqual(
            result.data[0]["ticket_label"] if result.data else None,
            "WDON-1842",
        )

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

    def test_tickets_about_phrase_searches_text_not_invented_rows(self) -> None:
        result = answer_tickets_about(
            self.source,
            ChatTurn(user_text="What tickets are about B Ellerby Email features?"),
        )
        self.assertIsNotNone(result)
        self.assertTrue(result.ok, result.error)
        self.assertEqual([row["ticket_label"] for row in result.data], ["WDON-2619"])
        self.assertIn("WDON-2619", result.reply or "")
        self.assertNotIn("ZINT-5550", result.reply or "")

    def test_tickets_about_a_person_name_stays_a_text_search(self) -> None:
        result = answer_tickets_about(
            self.source,
            ChatTurn(user_text="What tickets are about Flow Reporting features?"),
        )
        self.assertIsNotNone(result)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data, [])
        self.assertIn("No live tickets", result.reply or "")
        self.assertNotRegex(result.reply or "", r"\b[A-Z]{2,8}-[A-Z0-9]{3,6}\b")

    def test_list_tickets_text_search(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(q="imaging"))
        self.assertEqual([row["ticket_label"] for row in result.data], ["WDON-1842"])

    def test_list_tickets_requires_a_scope(self) -> None:
        with self.assertRaises(ValueError):
            ListTicketsArgs(status="Open")

    def test_list_tickets_stage_alone_is_enough(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(stage="open"))
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.data)
        self.assertTrue(all(row["category"] != "9 REVIEW" for row in result.data))

    def test_list_tickets_sorted_by_hours_excludes_placeholders_too(self) -> None:
        # Regression: the LLM can call list_tickets(sort=hrs_actual_total) directly for
        # any "which ticket took the most X" phrasing that answer_longest_time_worked's
        # own regex correctly declines (e.g. "most challenging to resolve" has no
        # time/hours/worked/logged word) -- that path had no placeholder filtering at
        # all, so the "Meetings" catch-all ticket won by default.
        result = list_tickets(
            self.source,
            ListTicketsArgs(stage="open", sort="hrs_actual_total", order="desc", limit=1),
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data[0]["ticket_label"], "ZTST-0091")
        self.assertNotIn("ZTST-0006", result.reply or "")

    def test_list_tickets_can_still_find_the_placeholder_by_category(self) -> None:
        # The hrs_actual_total exclusion is an escape hatch, not a ban -- asking for
        # the placeholder category by name must still find it.
        result = list_tickets(
            self.source,
            ListTicketsArgs(stage="all", category="Place Holder", sort="hrs_actual_total", limit=5),
        )
        self.assertTrue(result.ok, result.error)
        self.assertIn("ZTST-0006", [row["ticket_label"] for row in result.data])

    def test_longest_time_worked_honors_an_explicit_single_result_request(self) -> None:
        # Regression: the top-10 default ignored an explicit "only include one
        # ticket" request entirely and always returned 10 anyway.
        for question in (
            "Whats the longest worked open ticket? Only include one ticket in your response, the longest.",
            "What's the single longest open ticket worked?",
            "Just the one ticket with the most hours, please.",
        ):
            result = answer_longest_time_worked(self.source, ChatTurn(user_text=question))
            self.assertIsNotNone(result, question)
            assert result is not None
            self.assertTrue(result.ok, result.error)
            self.assertEqual(len(result.data), 1, question)
            self.assertEqual(result.data[0]["ticket_label"], "ZTST-0091", question)
            self.assertIn("has the longest time worked", result.reply or "", question)

    def test_longest_time_worked_honors_an_explicit_top_n_request(self) -> None:
        result = answer_longest_time_worked(
            self.source,
            ChatTurn(user_text="Top 3 open tickets by longest time worked"),
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.ok, result.error)
        self.assertEqual(
            [row["ticket_label"] for row in result.data],
            ["ZTST-0091", "ZTST-0092", "ZTST-0093"],
        )
        self.assertIn("Top 3", result.reply or "")

    def test_longest_time_worked_returns_a_ranked_top_list(self) -> None:
        result = answer_longest_time_worked(
            self.source,
            ChatTurn(user_text="Which open ticket has the longest time worked"),
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.ok, result.error)
        # Descending by hrs_actual_total: 42.0, 18.5, 6.25, 0.75, 0.5.
        self.assertEqual(
            [row["ticket_label"] for row in result.data],
            ["ZTST-0091", "ZTST-0092", "ZTST-0093", "WDON-1842", "ACME-0041"],
        )
        self.assertEqual(result.data[0]["hrs_actual_total"], 42.0)
        self.assertIn("Top 5", result.reply or "")
        self.assertIn("Lab fixtures", result.reply or "")
        self.assertNotIn("ACME-0050", result.reply or "")

    def test_longest_time_worked_matches_phrasings_regardless_of_word_order(self) -> None:
        # Regression: an earlier order-dependent regex required "ticket ... longest ...
        # time/hours" in that order, so phrasings like "longest ticket worked" (no literal
        # "time"/"hours" word, and "longest" before "ticket") matched nothing at all.
        for question in (
            "What's the longest ticket worked?",
            "Which ticket has the most hours?",
            "Highest hours logged on an open ticket?",
        ):
            result = answer_longest_time_worked(self.source, ChatTurn(user_text=question))
            self.assertIsNotNone(result, question)
            assert result is not None
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.data[0]["ticket_label"], "ZTST-0091", question)

    def test_longest_time_worked_excludes_placeholder_tickets(self) -> None:
        # Regression: ZTST-0006 is a "Place Holder" catch-all ticket with 559.53 hours --
        # by far the most of any fixture ticket, but never belongs in this ranking,
        # with or without the user saying "not a placeholder."
        for question in (
            "Which open ticket has the longest time worked",
            "Which open ticket has the longest time worked that isn't a placeholder ticket",
        ):
            result = answer_longest_time_worked(self.source, ChatTurn(user_text=question))
            self.assertIsNotNone(result, question)
            assert result is not None
            self.assertTrue(result.ok, result.error)
            self.assertNotIn("ZTST-0006", result.reply or "", question)
            self.assertIn("Excluded 1 placeholder ticket", result.reply or "", question)

    def test_next_longest_excludes_everything_already_shown(self) -> None:
        first = answer_longest_time_worked(
            self.source,
            ChatTurn(user_text="Which open ticket has the longest time worked"),
        )
        assert first is not None
        # All 5 known-hours fixture tickets are already on the first page, so a
        # follow-up correctly finds nothing left, rather than repeating one of them.
        second = answer_longest_time_worked(
            self.source,
            ChatTurn(
                user_text="What's the next longest ticket worked?",
                previous_assistant_text=first.reply or "",
            ),
        )
        self.assertIsNotNone(second)
        assert second is not None
        self.assertTrue(second.ok, second.error)
        # Nothing left to rank -- must say so, not repeat or invent a "next" winner.
        self.assertIn("can't rank them", second.reply or "")

    def test_vague_continuation_after_a_ranking_reply_is_a_follow_up(self) -> None:
        # "How about after that?" says nothing about tickets or hours on its own --
        # it only means anything because the previous reply was a longest-time-worked
        # ranking. All 5 known-hours fixture tickets are already on the first page,
        # so this also proves the follow-up path runs (not skipped) and correctly
        # finds nothing left, rather than repeating one of them.
        first = answer_longest_time_worked(
            self.source,
            ChatTurn(user_text="Which open ticket has the longest time worked"),
        )
        assert first is not None
        second = answer_longest_time_worked(
            self.source,
            ChatTurn(
                user_text="How about after that?",
                previous_assistant_text=first.reply or "",
            ),
        )
        self.assertIsNotNone(second)
        assert second is not None
        self.assertTrue(second.ok, second.error)
        self.assertIn("can't rank them", second.reply or "")

    def test_vague_continuation_with_unrelated_prior_reply_is_not_a_match(self) -> None:
        self.assertIsNone(
            answer_longest_time_worked(
                self.source,
                ChatTurn(user_text="How about after that?", previous_assistant_text="7 open ticket(s) for ts"),
            )
        )

    def test_next_longest_without_prior_context_falls_back_to_a_fresh_ranking(self) -> None:
        # "next longest ticket worked" still contains "longest"/"ticket"/"worked", so
        # with no prior ranking to follow up on, it's read as an ordinary fresh question
        # rather than matching nothing and falling through to the LLM with no filter.
        result = answer_longest_time_worked(
            self.source,
            ChatTurn(user_text="What's the next longest ticket worked?", previous_assistant_text=""),
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data[0]["ticket_label"], "ZTST-0091")

    def test_time_on_one_ticket_is_not_a_longest_time_question(self) -> None:
        self.assertIsNone(
            answer_longest_time_worked(
                self.source,
                ChatTurn(user_text="How much time was logged on ticket WDON-1842?"),
            )
        )

    def test_list_tickets_urgent_is_category_not_my_open_list(self) -> None:
        token = current_signed_in_email.set("tseibert@techbldrs.example")
        try:
            result = list_tickets(
                self.source,
                ListTicketsArgs(assignee_code="me"),
                ChatTurn(user_text="any urgent tickets that need immediate attention?"),
            )
        finally:
            current_signed_in_email.reset(token)
        self.assertTrue(result.ok, result.error)
        self.assertEqual([row["ticket_label"] for row in result.data], ["ACME-0099"])
        self.assertEqual(result.data[0]["category"], "0 Urgent")
        self.assertIn("0 Urgent", result.reply or "")
        self.assertNotIn("ACME-0041", result.reply or "")

    def test_list_tickets_my_urgent_keeps_signed_in_assignee(self) -> None:
        token = current_signed_in_email.set("tseibert@techbldrs.example")
        try:
            result = list_tickets(
                self.source,
                ListTicketsArgs(assignee_code="me", category="urgent"),
                ChatTurn(user_text="my urgent tickets"),
            )
        finally:
            current_signed_in_email.reset(token)
        self.assertEqual(result.data, [])
        self.assertIn("No open 0 Urgent tickets", result.reply or "")

    def test_list_tickets_billable_is_reason_not_my_open_list(self) -> None:
        token = current_signed_in_email.set("tseibert@techbldrs.example")
        try:
            result = list_tickets(
                self.source,
                ListTicketsArgs(assignee_code="me"),
                ChatTurn(user_text="any billable tickets?"),
            )
        finally:
            current_signed_in_email.reset(token)
        self.assertTrue(result.ok, result.error)
        self.assertEqual([row["ticket_label"] for row in result.data], ["ACME-0041"])
        self.assertEqual(result.data[0]["reason"], "Billable/New")
        self.assertIn("Billable/New", result.reply or "")
        self.assertNotIn("ACME-0045", result.reply or "")

    def test_list_tickets_overdue_from_turn(self) -> None:
        result = list_tickets(
            self.source,
            ListTicketsArgs(overdue=True),
            ChatTurn(user_text="any overdue tickets?"),
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual([row["ticket_label"] for row in result.data], ["WDON-1842"])

    def test_list_tickets_machine_and_invoice(self) -> None:
        machine = list_tickets(self.source, ListTicketsArgs(machine_name="WDON-IMG-01"))
        self.assertEqual([row["ticket_label"] for row in machine.data], ["WDON-1842"])
        invoice = list_tickets(self.source, ListTicketsArgs(invoice_num="INV-441"))
        self.assertEqual([row["ticket_label"] for row in invoice.data], ["ACME-0041"])

    def test_list_tickets_incomplete(self) -> None:
        result = list_tickets(
            self.source,
            ListTicketsArgs(assignee_code="tr"),
            ChatTurn(user_text="incomplete tickets assigned to tr"),
        )
        self.assertEqual([row["ticket_label"] for row in result.data], ["WDON-1842"])

    def test_latest_ticket_wdon(self) -> None:
        result = latest_ticket(self.source, LatestTicketArgs(client_code="WDON"))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["ticket_label"], "WDON-2619")
        self.assertEqual(result.data["topic"], "B Ellerby Email")
        self.assertEqual(result.row_ids, [2619])

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

    def test_does_any_tickets_need_merged_scans_all_open(self) -> None:
        result = answer_merge_suggestion(
            self.source,
            ChatTurn(user_text="Does any tickets need merged?"),
        )
        self.assertIsNotNone(result)
        self.assertTrue(result.ok, result.error)
        self.assertIn("Possible merges in all open tickets", result.reply or "")
        self.assertIn("Keep ACME-0041, absorb ACME-0045", result.reply or "")
        self.assertNotIn("assigned to", (result.reply or "").lower())

    def test_need_merged_for_a_client_stays_on_that_client(self) -> None:
        result = answer_merge_suggestion(
            self.source,
            ChatTurn(user_text="What tickets need merged for ZINT?"),
        )
        self.assertIsNotNone(result)
        self.assertIn("No likely duplicates in open tickets for ZINT", result.reply or "")
        self.assertNotIn("ACME-0041", result.reply or "")

    def test_merge_command_is_not_a_suggestion_question(self) -> None:
        self.assertIsNone(
            answer_merge_suggestion(
                self.source,
                ChatTurn(user_text="merge ACME-0045 into ACME-0041"),
            )
        )

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

    def test_list_time_entries_by_ticket(self) -> None:
        result = list_time_entries(self.source, ListTimeEntriesArgs(ticket_id=9001, view="list"))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.row_ids, [70001])
        self.assertEqual(result.data[0]["minutes"], 45)
        self.assertIn("On-site: imaging workstation repair", result.reply or "")
        self.assertIn("41m", result.reply)  # actual minutes, as hours/minutes, not "41 min"

    def test_list_time_entries_by_client_and_assignee(self) -> None:
        result = list_time_entries(self.source, ListTimeEntriesArgs(client_code="ACME", assignee_code="ts"))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.row_ids, [70002])
        result_wrong_tech = list_time_entries(self.source, ListTimeEntriesArgs(client_code="ACME", assignee_code="tr"))
        self.assertEqual(result_wrong_tech.data, [])

    def test_list_time_entries_requires_ticket_or_client(self) -> None:
        with self.assertRaises(ValueError):
            ListTimeEntriesArgs(limit=5)
        # a technician alone is a scope now (resolved to tech_user_id), as is a date range
        ListTimeEntriesArgs(assignee_code="ts")
        ListTimeEntriesArgs(work_after="2026-09-01")

    def test_list_machines_for_client(self) -> None:
        result = list_machines(self.source, ListMachinesArgs(client_code="WDON"))
        self.assertTrue(result.ok, result.error)
        self.assertEqual([row["machine_name"] for row in result.data], ["WDON-FS-01", "WDON-IMG-01"])

    def test_get_ticket_detail_returns_log_and_notes(self) -> None:
        result = get_ticket_detail(self.source, GetTicketDetailArgs(ticket_id=9001))
        self.assertTrue(result.ok, result.error)
        self.assertIn("boot drive replaced", result.data["log_text"])
        self.assertIn("Recurring boot failures", result.data["notes_text"])

    def test_get_ticket_detail_unknown_id_refuses(self) -> None:
        result = get_ticket_detail(self.source, GetTicketDetailArgs(ticket_id=999999))
        self.assertFalse(result.ok)
        self.assertIn("not found", result.error or "")

    def test_get_mail_detail_returns_full_body_not_snippet(self) -> None:
        result = get_mail_detail(self.source, GetMailDetailArgs(mail_id=50003))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["body"], "Machine is back up. Thanks — Debe")
        self.assertNotIn("snippet", result.data)

    def test_get_client_detail_returns_contract_fields(self) -> None:
        result = get_client_detail(self.source, GetClientDetailArgs(client_code="WDON"))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["contract_minutes"], 600)
        self.assertEqual(result.data["balance"], 120)
        self.assertIn("Contract minutes: 600", result.reply or "")


class MergeGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()

    def _ticket_ids(self) -> set[int]:
        return {
            row.id
            for row in self.source.list_tickets(client_code="ACME", stage="open")
            if row.assignee_code == "ts"
        }

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

    def test_mismatched_ids_still_merge_the_named_labels(self) -> None:
        turn = ChatTurn(user_text="merge ACME-0045 into ACME-0041", previous_assistant_text=_PLAN)
        result = merge_tickets(self.source, _merge_args(target_ticket_id=176943, source_ticket_ids=[1]), turn)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["merged_source_labels"], ["ACME-0045"])
        self.assertEqual(self._ticket_ids(), {3100})

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
            {3100, 3105, 3300, 3400},
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


class NewCapabilityTests(unittest.TestCase):
    """Tools the model needs for real questions: sort-only lists, needs-response, stats, dates."""

    def setUp(self) -> None:
        self.source = StubFlowSource()

    def _labels(self, result) -> list[str]:
        self.assertTrue(result.ok, result.error)
        return [row["ticket_label"] for row in result.data]

    def test_sort_only_list_is_valid_and_defaults_to_open(self) -> None:
        args = ListTicketsArgs(sort="hrs_actual_total", order="desc", limit=1)
        result = list_tickets(self.source, args)
        self.assertEqual(self._labels(result), ["ZTST-0091"])

    def test_needs_response_is_newest_mail_inbound(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(needs_response=True, stage="open"))
        labels = self._labels(result)
        self.assertIn("WDON-1842", labels)  # newest mail is Debe's inbound
        self.assertIn("ACME-0041", labels)
        self.assertNotIn("ACME-0099", labels)  # no mail at all

    def test_unassigned_filter(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(unassigned=True, client_code="ZTST", stage="open"))
        self.assertEqual(
            sorted(self._labels(result)), ["ZTST-0006", "ZTST-0091", "ZTST-0092", "ZTST-0093"]
        )

    def test_comma_codes_and_internal_alias_fan_out(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(client_code="WDON,ACME", stage="open", limit=100))
        labels = self._labels(result)
        self.assertIn("WDON-1842", labels)
        self.assertIn("ACME-0041", labels)
        self.assertEqual(len(labels), len(set(labels)))
        internal = list_tickets(self.source, ListTicketsArgs(client_code="internal", stage="open"))
        self.assertTrue(internal.ok, internal.error)
        self.assertEqual(internal.data, [])

    def test_list_mail_by_contact_without_client_code(self) -> None:
        debe = self.source.search_contact(query="Debe")[0]
        result = list_mail(self.source, ListMailArgs(contact_id=debe.id))
        self.assertTrue(result.ok, result.error)
        self.assertEqual({row["ticket_label"] for row in result.data}, {"WDON-1842"})

    def test_list_mail_date_range_without_client_code(self) -> None:
        result = list_mail(self.source, ListMailArgs(received_after="2026-09-17", received_before="2026-09-18"))
        self.assertEqual({row["id"] for row in result.data}, {50001})

    def test_list_mail_needs_some_scope(self) -> None:
        with self.assertRaises(ValueError):
            ListMailArgs()

    def test_list_time_entries_by_tech_and_dates(self) -> None:
        result = list_time_entries(
            self.source,
            ListTimeEntriesArgs(assignee_code="ts", work_after="2026-09-19", work_before="2026-09-20"),
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual([row["id"] for row in result.data], [70002])

    def test_list_time_entries_unreviewed(self) -> None:
        result = list_time_entries(self.source, ListTimeEntriesArgs(reviewed=False))
        self.assertEqual([row["id"] for row in result.data], [70002])

    def test_stats_tickets_by_requestor_for_client(self) -> None:
        result = ticket_stats(
            self.source, TicketStatsArgs(entity="tickets", group_by="requestor", client_code="ACME")
        )
        self.assertTrue(result.ok, result.error)
        top = result.data["rows"][0]
        self.assertEqual((top["key"], top["count"]), ("Riley Chen", 5))  # includes archived ACME-0010
        self.assertIn("Riley Chen", result.reply)

    def test_stats_time_by_client_in_range_reports_hours(self) -> None:
        result = ticket_stats(
            self.source,
            TicketStatsArgs(entity="time", group_by="client", billable=True, after="2026-09-01", before="2026-10-01"),
        )
        self.assertTrue(result.ok, result.error)
        by_key = {row["key"]: row for row in result.data["rows"]}
        self.assertEqual(by_key["WDON"]["billed_minutes"], 45)
        self.assertEqual(by_key["ACME"]["minutes"], 20)
        self.assertIn("45m billed", result.reply)

    def test_stats_time_by_tech_resolves_me(self) -> None:
        token = current_signed_in_email.set("tseibert@techbldrs.example")
        try:
            result = ticket_stats(
                self.source, TicketStatsArgs(entity="time", group_by="tech", assignee_code="me")
            )
        finally:
            current_signed_in_email.reset(token)
        self.assertTrue(result.ok, result.error)
        self.assertEqual([row["key"] for row in result.data["rows"]], ["Tre Seibert"])

    def test_stats_mail_by_sender(self) -> None:
        result = ticket_stats(self.source, TicketStatsArgs(entity="mail", group_by="sender", client_code="WDON"))
        by_key = {row["key"]: row["count"] for row in result.data["rows"]}
        self.assertEqual(by_key["Debe Hernandez"], 2)

    def test_stats_rejects_unknown_group(self) -> None:
        result = ticket_stats(self.source, TicketStatsArgs(entity="tickets", group_by="bogus"))
        self.assertFalse(result.ok)
        self.assertIn("group_by", result.error)

    def test_stats_empty_result_says_so(self) -> None:
        result = ticket_stats(self.source, TicketStatsArgs(entity="tickets", group_by="cause", client_code="NOPE"))
        self.assertTrue(result.ok)
        self.assertIn("No tickets found", result.reply)


class RouterScopeGuardTests(unittest.TestCase):
    """A client code is a filter the pattern routers do not handle; they must hand it to the model."""

    def setUp(self) -> None:
        self.source = StubFlowSource()

    def _turn(self, text: str) -> ChatTurn:
        return ChatTurn(user_text=text)

    def test_person_ticket_router_ignores_client_codes(self) -> None:
        for text in (
            "Show me open tickets for client ZZZZ",
            "Tickets still open for BLMC that have no assignee",
            "Show me open tickets for WDON",
        ):
            self.assertIsNone(answer_person_ticket_question(self.source, self._turn(text)), text)

    def test_person_ticket_router_still_handles_people(self) -> None:
        self.assertIsNotNone(
            answer_person_ticket_question(self.source, self._turn("Tell me the latest ticket involving Michael Sodl"))
        )

    def test_tickets_about_router_declines_a_client_scope(self) -> None:
        self.assertIsNone(answer_tickets_about(self.source, self._turn("Tickets about printer issues at ZEBB")))
        self.assertIsNotNone(answer_tickets_about(self.source, self._turn("Any tickets about imaging?")))
        self.assertIsNotNone(answer_tickets_about(self.source, self._turn("Any tickets about VPN?")))

    def test_longest_time_router_declines_a_client_scope(self) -> None:
        self.assertIsNone(answer_longest_time_worked(self.source, self._turn("Top 5 longest-worked tickets for BUCK")))
        self.assertIsNotNone(
            answer_longest_time_worked(self.source, self._turn("Which open ticket has the longest time worked"))
        )


class OwnTicketsAndLabelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()
        self._token = current_signed_in_email.set("tseibert@techbldrs.example")

    def tearDown(self) -> None:
        current_signed_in_email.reset(self._token)

    def test_need_to_respond_keeps_me_even_without_the_word_my(self) -> None:
        turn = ChatTurn(user_text="What tickets do I need to respond to?")
        result = list_tickets(
            self.source, ListTicketsArgs(assignee_code="me", stage="open", needs_response=True), turn
        )
        labels = [row["ticket_label"] for row in result.data]
        self.assertIn("ACME-0041", labels)
        self.assertNotIn("WDON-1842", labels)  # newest mail is inbound, but it is Tom's ticket

    def test_my_urgent_tickets_adds_me_when_the_model_forgot(self) -> None:
        turn = ChatTurn(user_text="What are my urgent tickets?")
        result = list_tickets(self.source, ListTicketsArgs(category="urgent", stage="open"), turn)
        self.assertNotIn("ACME-0099", [row["ticket_label"] for row in result.data])  # eo's ticket

    def test_any_urgent_tickets_stays_everyone(self) -> None:
        turn = ChatTurn(user_text="Any urgent tickets?")
        result = list_tickets(self.source, ListTicketsArgs(category="urgent", stage="open"), turn)
        self.assertIn("ACME-0099", [row["ticket_label"] for row in result.data])

    def test_a_client_scope_beats_the_word_my(self) -> None:
        turn = ChatTurn(user_text="Show my client's open tickets for ACME")
        result = list_tickets(self.source, ListTicketsArgs(client_code="ACME", stage="open"), turn)
        self.assertIn("ACME-0099", [row["ticket_label"] for row in result.data])

    def test_time_entries_by_ticket_label(self) -> None:
        result = list_time_entries(self.source, ListTimeEntriesArgs(ticket_label="acme-0041", view="list"))
        self.assertTrue(result.ok, result.error)
        self.assertEqual([row["id"] for row in result.data], [70002])
        self.assertIn("ACME-0041", result.reply)

    def test_ticket_detail_by_label_and_unknown_label(self) -> None:
        good = get_ticket_detail(self.source, GetTicketDetailArgs(ticket_label="ACME-0041"))
        self.assertTrue(good.ok, good.error)
        self.assertEqual(good.row_ids, [3100])
        bad = get_ticket_detail(self.source, GetTicketDetailArgs(ticket_label="ZTB-1691"))
        self.assertFalse(bad.ok)
        with self.assertRaises(ValueError):
            GetTicketDetailArgs()


class ModelHabitTests(unittest.TestCase):
    """Argument mistakes the 14B model actually makes (seen in eval runs) must not change the answer."""

    def setUp(self) -> None:
        self.source = StubFlowSource()

    def test_stats_keeps_a_date_range_given_as_work_after(self) -> None:
        args = TicketStatsArgs.model_validate(
            {"entity": "time", "group_by": "tech", "work_after": "2026-09-19", "work_before": "2026-09-20"}
        )
        self.assertEqual((args.after, args.before), ("2026-09-19", "2026-09-20"))
        result = ticket_stats(self.source, args)
        self.assertEqual([row["key"] for row in result.data["rows"]], ["Tre Seibert"])
        self.assertEqual(result.data["total_count"], 1)  # only the 09-19 entry, not all time

    def test_explicit_after_wins_over_an_alias(self) -> None:
        args = TicketStatsArgs.model_validate({"after": "2026-01-01", "work_after": "2026-09-19"})
        self.assertEqual(args.after, "2026-01-01")

    def test_category_overdue_is_treated_as_the_overdue_filter(self) -> None:
        with_alias = list_tickets(self.source, ListTicketsArgs(category="overdue", stage="open"))
        plain = list_tickets(self.source, ListTicketsArgs(overdue=True, stage="open"))
        self.assertTrue(with_alias.ok, with_alias.error)
        self.assertEqual(with_alias.row_ids, plain.row_ids)


class LongestTimeDateRangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()

    def test_a_date_range_goes_to_the_model_not_the_all_time_ranking(self) -> None:
        for text in (
            "What was the longest ticket we spent time on last week?",
            "Which ticket has the most hours this month?",
            "longest time worked yesterday",
            "most hours logged since September",
        ):
            self.assertIsNone(answer_longest_time_worked(self.source, ChatTurn(user_text=text)), text)

    def test_the_plain_all_time_question_still_hits_the_router(self) -> None:
        self.assertIsNotNone(
            answer_longest_time_worked(self.source, ChatTurn(user_text="Which open ticket has the longest time worked"))
        )

    def test_stats_can_rank_tickets_by_time_in_a_range(self) -> None:
        result = ticket_stats(
            self.source,
            TicketStatsArgs(entity="time", group_by="ticket", metric="hours", after="2026-09-17", before="2026-09-20"),
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["rows"][0]["key"], "WDON-1842")  # 41 actual minutes beats 20


class LongestTimeOwnTimeTests(unittest.TestCase):
    def test_my_own_time_goes_to_the_model(self) -> None:
        source = StubFlowSource()
        for text in (
            "what ticket have I spent the most amount of time working on?",
            "Which of my tickets has the most hours?",
            "most hours logged by me",
        ):
            self.assertIsNone(answer_longest_time_worked(source, ChatTurn(user_text=text)), text)
        # "show me" is not "by me"
        self.assertIsNotNone(
            answer_longest_time_worked(source, ChatTurn(user_text="Show me the open ticket with the longest time worked"))
        )


class TechnicianTypoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StubFlowSource()

    def test_one_letter_off_resolves_the_technician(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(assignee_code="Tomm", stage="open"))
        self.assertTrue(result.ok, result.error)
        self.assertIn("WDON-1842", [row["ticket_label"] for row in result.data])  # Tom Rivera (tr)

    def test_two_letters_off_asks_instead_of_guessing(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(assignee_code="Tomas", stage="open"))
        self.assertFalse(result.ok)
        self.assertIn("Did you mean Tom Rivera (tr)?", result.error)

    def test_a_name_nothing_like_any_technician_is_still_not_found(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(assignee_code="Zachariah", stage="open"))
        self.assertFalse(result.ok)
        self.assertNotIn("Did you mean", result.error)

    def test_short_names_are_never_fuzzy_matched(self) -> None:
        result = list_tickets(self.source, ListTicketsArgs(assignee_code="Tim", stage="open"))
        self.assertFalse(result.ok)
        self.assertNotIn("Did you mean", result.error)
