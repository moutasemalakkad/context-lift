# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""What to write next, from the shape of what failed."""
from __future__ import annotations

from context_lift.lever import (DEFINITION, EXAMPLES, FIX_THE_GOLD, OVERVIEW,
                            READ_THE_PAIRS, TABLE_LIST, for_bucket, suggest)


def test_wrong_numbers_in_the_right_shape_asks_for_a_definition():
    """A metric meaning something different from what the model assumed."""
    assert for_bucket("WRONG_VALUES").lever == DEFINITION
    assert for_bucket("GRAIN").lever == DEFINITION


def test_sql_that_did_not_compile_asks_for_an_example():
    """One worked query in the right dialect teaches that faster than any
    sentence about it."""
    assert for_bucket("DIALECT").lever == EXAMPLES
    assert for_bucket("ROW_COUNT").lever == EXAMPLES


def test_a_missing_column_asks_for_a_table_overview():
    assert for_bucket("SCHEMA").lever == OVERVIEW


def test_a_broken_gold_says_do_not_write_context_at_all():
    """The part worth having. A question whose gold never ran is a broken
    answer key, and writing a rule against it buys nothing and hides the
    fault behind a round of work."""
    assert for_bucket("DID_NOT_RUN").lever == FIX_THE_GOLD
    assert for_bucket("TIMEOUT").lever == FIX_THE_GOLD


def test_an_unknown_bucket_sends_a_person_to_read_the_pairs():
    """A bucket guessed wrong sends a person to curate the wrong table."""
    assert for_bucket("UNCLASSIFIED").lever == READ_THE_PAIRS
    assert for_bucket("SOMETHING_NEW").lever == READ_THE_PAIRS


def test_a_table_the_agent_never_resolved_comes_before_every_other_lever():
    """No written context reaches a table the model cannot see."""
    lines = suggest({"WRONG_VALUES": 9}, recall_misses=2)

    assert TABLE_LIST in lines[0]
    assert "before any of it" in lines[0]


def test_the_largest_failure_shape_is_named_first():
    lines = suggest({"WRONG_VALUES": 2, "DIALECT": 7})

    assert "DIALECT" in lines[0] and "7 of 9" in lines[0]
    assert "WRONG_VALUES" in lines[1]


def test_nothing_failed_says_so_rather_than_suggesting_a_lever():
    assert suggest({}) == ["nothing failed, so there is no lever to pull"]


def test_the_suggestion_says_it_read_a_count_and_not_a_cause():
    """Guessing costs a round: on one estate a curator wrote 29 rules against
    a bucket that rules cannot move, and the holdout did not shift a point."""
    lines = suggest({"WRONG_VALUES": 3})

    assert any("count, not a cause" in line for line in lines)
    assert any("three failing pairs" in line for line in lines)
