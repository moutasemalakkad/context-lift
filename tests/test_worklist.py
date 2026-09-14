# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Which context item to look at first, and why."""
from __future__ import annotations

from context_lift.worklist import build


def row(qid, arm, correct, citations="", gold_sql="SELECT 1", matched=""):
    return {"id": qid, "arm": arm, "correct": correct, "citations": citations,
            "gold_sql": gold_sql, "matched_example": matched}


ITEMS = [("revenue", "term", "Sale price of items neither cancelled nor returned."),
         ("margin", "term", "Sale price minus product cost."),
         ("ex_q0", "example", "SELECT COUNT(*) FROM orders")]


def test_an_item_cited_on_a_question_the_context_fixed_is_credited_with_the_fix():
    rows = [row("q1", "control", False), row("q1", "treatment", True, "term:revenue")]

    work = build(rows, ITEMS)
    revenue = next(i for i in work.items if i.name == "revenue")

    assert revenue.cited == 1 and revenue.fixed == 1 and revenue.broke == 0


def test_an_item_cited_where_the_context_broke_something_is_credited_with_that():
    rows = [row("q1", "control", True), row("q1", "treatment", False, "term:margin")]

    margin = next(i for i in build(rows, ITEMS).items if i.name == "margin")

    assert margin.broke == 1 and margin.fixed == 0


def test_a_break_outranks_everything_else():
    """Worst news first. An item that broke a question is the first thing to
    read however much else it did."""
    rows = [row("q1", "control", False), row("q1", "treatment", True, "term:revenue"),
            row("q2", "control", False), row("q2", "treatment", True, "term:revenue"),
            row("q3", "control", True), row("q3", "treatment", False, "term:margin")]

    ranked = build(rows, ITEMS).ranked()

    assert ranked[0].name == "margin", "the item that broke a question comes first"
    assert ranked[0].broke == 1 and ranked[0].fixed == 0


def test_an_item_nobody_cited_is_named_as_the_cheapest_thing_to_prune():
    """An item no answer referred to either did not reach the model or was not
    needed. Removing one and re-running is the cheapest test there is."""
    rows = [row("q1", "control", False), row("q1", "treatment", True, "term:revenue")]

    work = build(rows, ITEMS)

    assert {i.name for i in work.uncited()} == {"margin", "ex_q0"}
    assert "never cited" in work.text()


def test_two_items_cited_on_one_question_both_get_the_credit():
    """Nothing here can say which of them did the work, and picking one would
    be inventing a cause."""
    rows = [row("q1", "control", False),
            row("q1", "treatment", True, "term:revenue;term:margin")]

    work = build(rows, ITEMS)

    assert next(i for i in work.items if i.name == "revenue").fixed == 1
    assert next(i for i in work.items if i.name == "margin").fixed == 1


def test_an_answer_from_a_stored_example_is_a_lookup_not_a_fix():
    """The agent ran the example as written, so the answer says nothing about
    writing SQL and must not be counted as the context teaching it something."""
    rows = [row("q1", "control", False),
            row("q1", "treatment", True, "example:ex_q0", matched="how many orders?")]

    example = next(i for i in build(rows, ITEMS).items if i.name == "ex_q0")

    assert example.lookups == 1 and example.fixed == 0
    assert "stored example" in example.line()


def test_an_item_repeating_a_scored_questions_own_values_is_flagged():
    """It may be fitted to that question rather than saying anything general.
    Printed beside every count so a lift is read with it."""
    rows = [row("q1", "control", False,
                gold_sql="SELECT * FROM orders WHERE status = 'Complete'"),
            row("q1", "treatment", True, "term:revenue",
                gold_sql="SELECT * FROM orders WHERE status = 'Complete'")]
    items = [("revenue", "term", "Revenue counts rows where status = 'Complete'.")]

    assert build(rows, items).items[0].memorised


def test_an_excluded_question_is_not_counted_either_way():
    """`correct` empty means the question was never scored: a gold that did
    not run, or a row the cap cut. Counting it would move the denominator."""
    rows = [row("q1", "control", ""), row("q1", "treatment", "", "term:revenue")]

    work = build(rows, ITEMS)

    assert work.scored_questions == 0
    assert next(i for i in work.items if i.name == "revenue").cited == 0


def test_the_report_says_these_are_leads_and_not_results():
    """At these set sizes no per-item test has the power to decide anything,
    and a reader who takes the order for a ranking of truth will act on noise."""
    rows = [row("q1", "control", False), row("q1", "treatment", True, "term:revenue")]

    text = build(rows, ITEMS).text()

    assert "leads, not results" in text


def test_written_from_one_question_is_carried_through():
    """25 of 29 rules on one estate were drafted from a single question each.
    A 1 here means the item is a fit to one question."""
    rows = [row("q1", "control", False), row("q1", "treatment", True, "term:revenue")]

    work = build(rows, ITEMS, written_from={"revenue": 1})

    assert next(i for i in work.items if i.name == "revenue").written_from == 1
    assert "written from 1 question" in next(
        i for i in work.ranked() if i.name == "revenue").line()


def test_a_run_with_no_citations_column_says_so_rather_than_reporting_zero():
    """"nobody cited this item" and "nobody recorded citations" are different
    claims. A worklist that prints the first when it means the second sends a
    person to delete context that was working."""
    rows = [{"id": "q1", "arm": "control", "correct": "False", "gold_sql": "SELECT 1"},
            {"id": "q1", "arm": "treatment", "correct": "True", "gold_sql": "SELECT 1"}]

    work = build(rows, ITEMS)

    assert not work.citations_recorded
    assert work.uncited() == [], "nothing is uncited on evidence never collected"
    assert "recorded no citations" in work.text()
    assert "never cited" not in work.text()


def test_an_example_is_named_by_what_a_citation_says_not_by_an_index():
    """`ex_q0` joins to nothing, so every example reads "cited 0" forever.

    An answer cites an example by its question text. An uncited item is the
    cheapest thing the worklist tells a person to delete, so a join that
    silently fails sends them to delete context that was working. The same
    mistake was made once with terms, where the id was `g_completed` on one
    run and `g_completed_order` on the next.
    """
    from context_lift.worklist import build

    question = "inventory on hand by distribution center"
    rows = [
        {"id": "q1#rep0", "arm": "control", "correct": "False", "citations": ""},
        {"id": "q1#rep0", "arm": "treatment", "correct": "True",
         "citations": f"example:{question}"},
    ]

    by_index = build(rows, [("ex_q0", "example", question)])
    by_question = build(rows, [(question, "example", question)])

    assert by_index.items[0].cited == 0, "an index name cannot ever join"
    assert by_question.items[0].cited == 1
    assert by_question.items[0].fixed == 1
