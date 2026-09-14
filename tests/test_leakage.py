# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Behavior tests for the leakage check and the holdout split.

Leakage is the failure that makes every other number look good, so these tests
care most about the cases where detection is easy to lose: a stored query with
different literals, different whitespace, different case. If those slip past,
the check gives false assurance, which is worse than not having it.
"""
import pytest

from context_lift.leakage import check_leakage, split_golds

GOLD = ("g1", "What is the total revenue by region for 2025?",
        "SELECT region, SUM(amount) FROM sales WHERE year = 2025 GROUP BY region")


def test_clean_context_reports_no_leakage():
    report = check_leakage([GOLD], "The sales table holds one row per order line.")
    assert report.clean
    assert "no leakage" in report.summary()


def test_gold_sql_pasted_into_the_context_is_caught():
    context = f"Verified query:\n{GOLD[2]}\nUse it for revenue questions."
    report = check_leakage([GOLD], context)
    assert not report.clean
    assert report.leaked_ids == {"g1"}
    assert any(f.kind == "sql" for f in report.findings)


def test_the_same_query_with_different_literals_is_still_caught():
    # The realistic case: a verified query stored for a different year. Exact
    # matching would miss this, and it is the same query.
    context = "SELECT region, SUM(amount) FROM sales WHERE year = 2019 GROUP BY region"
    assert not check_leakage([GOLD], context).clean


def test_different_whitespace_and_case_do_not_hide_a_leak():
    context = "select  region,\n  SUM(amount)\nFROM sales WHERE year = 2025\ngroup by region"
    assert not check_leakage([GOLD], context).clean


def test_the_gold_question_appearing_in_a_document_is_caught():
    context = "FAQ: what is the total revenue by region for 2025? See the sales mart."
    report = check_leakage([GOLD], context)
    assert any(f.kind == "question" for f in report.findings)


def test_a_short_question_is_not_treated_as_evidence():
    # "total sales" appears in any document about sales; flagging it would make
    # the check noise and get it ignored.
    short = ("g2", "total sales", "SELECT SUM(x) FROM t")
    assert check_leakage([short], "A report of total sales by month.").clean


def test_summary_tells_the_reader_what_the_leak_means():
    report = check_leakage([GOLD], GOLD[2])
    assert "memorization" in report.summary()


# --- the split ---


def test_split_puts_roughly_the_requested_fraction_in_the_holdout():
    ids = [f"q{i:03d}" for i in range(400)]
    curate, holdout = split_golds(ids, holdout_fraction=0.3)
    assert len(curate) + len(holdout) == 400
    assert 0.24 < len(holdout) / 400 < 0.36


def test_the_split_is_stable_across_calls():
    ids = [f"q{i}" for i in range(50)]
    assert split_golds(ids) == split_golds(ids)


def test_adding_questions_never_moves_an_existing_one_across_the_line():
    # Hashing per id, not shuffling: a holdout score stays comparable as the
    # gold set grows during an engagement.
    small = [f"q{i}" for i in range(20)]
    grown = small + [f"q{i}" for i in range(20, 60)]
    _, holdout_small = split_golds(small)
    _, holdout_grown = split_golds(grown)
    assert set(holdout_small) <= set(holdout_grown)


def test_a_different_seed_gives_a_different_split():
    ids = [f"q{i}" for i in range(60)]
    assert split_golds(ids, seed="a") != split_golds(ids, seed="b")


def test_an_impossible_fraction_is_refused():
    with pytest.raises(ValueError, match="between 0 and 1"):
        split_golds(["q1"], holdout_fraction=1.5)


def test_a_question_is_checked_against_its_own_retrieved_context():
    golds = [("q1", "how many orders were cancelled last year", "SELECT count(*) FROM orders"),
             ("q2", "who bought the most in the last quarter", "SELECT user FROM orders")]

    report = check_leakage(golds, "", {"q1": "use SELECT count(*) FROM orders for this",
                                       "q2": "the orders table holds one row per order"})

    assert report.leaked_ids == {"q1"}      # q2's own context is clean


def test_a_question_the_retriever_never_served_is_checked_against_everything():
    """A reserved question is never retrieved for, so nobody hands it context -
    but its answer can sit in a document fetched for some other question, and
    that is the leak that destroys the number you report."""
    golds = [("q1", "how many orders were cancelled last year", "SELECT count(*) FROM orders"),
             ("q2", "who bought the most in the last quarter", "SELECT user FROM orders")]

    # only q1 was served; q1's document happens to quote q2's answer
    report = check_leakage(golds, "", {"q1": "background: SELECT user FROM orders finds it"})

    assert report.leaked_ids == {"q2"}


def test_retrieved_context_does_not_change_the_single_file_behaviour():
    golds = [("q1", "how many orders were cancelled last year", "SELECT count(*) FROM orders")]
    blob = "use SELECT count(*) FROM orders for this"

    assert check_leakage(golds, blob).leaked_ids == check_leakage(golds, blob, {}).leaked_ids
