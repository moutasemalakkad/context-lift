# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""One question across every round: what fixed it, what broke it, when."""
from __future__ import annotations

import contextlib
import csv
import io

import pytest

from context_lift.history import build, lines, write_csv


@pytest.fixture
def workspace(tmp_path):
    """Two scored arms in one workspace, so there is a round to pivot."""
    import sys
    sys.path.insert(0, "tests")
    from test_loop import build_workspace
    with contextlib.redirect_stdout(io.StringIO()):
        return build_workspace(tmp_path)


def test_every_scored_question_gets_a_row_with_its_status_per_round(workspace):
    history = build(workspace)

    assert history["rounds"], "the fixture has one fully scored round"
    ids = {row["id"] for row in history["rows"]}
    assert ids == {"q1", "q2", "q3", "q4"}
    numbers = [r["n"] for r in history["rounds"]]
    for row in history["rows"]:
        assert set(row["status_by_round"]) == set(numbers)


def test_a_broken_question_sorts_above_everything_else(workspace):
    """A human reads down the file until the rows stop mattering, so a real
    regression must not be alphabetically buried under questions that never
    moved."""
    rows = build(workspace)["rows"]

    assert rows[0]["latest"] == "broke"
    assert rows[0]["open"] is True
    assert rows[0]["times_broken"] == 1
    assert rows[0]["first_broken_in"] == 1


def test_the_counts_are_exclusive_and_sum_to_the_question_count(workspace):
    """An earlier version counted a FIXED question under "never moved", which
    read as though the context had done nothing."""
    history = build(workspace)
    text = "\n".join(lines(history))

    numbers = [int(part.split(":")[1].strip().split()[0])
               for part in text.splitlines()
               if part.startswith(("  broken in the newest", "  fixed and still",
                                   "  unstable in the newest", "  never moved"))]
    assert sum(numbers) == len(history["rows"])
    assert "fixed and still fixed      : 1" in text


def test_a_flagged_answer_key_is_carried_so_a_row_is_not_read_as_evidence(workspace):
    """Both arms are graded against the same wrong key, so the row says
    nothing about the context either way."""
    rows = build(workspace)["rows"]

    flagged = [r for r in rows if r["key_flagged"]]
    assert len(flagged) == 1
    assert flagged[0]["id"] == "q4"
    assert "text column" in flagged[0]["key_detail"]
    assert "answer key flagged" in "\n".join(lines(build(workspace)))


def test_a_blamed_rule_travels_with_the_question_that_broke(workspace):
    """"broke 1" without which rule sends a person back to grepping two
    score reports by hand."""
    rows = build(workspace)["rows"]
    broken = next(r for r in rows if r["open"])

    assert broken["blamed_rule"] == "R001"


def test_the_csv_has_one_row_per_question_and_one_column_per_round(workspace, tmp_path):
    history = build(workspace)
    out = tmp_path / "history.csv"

    written = write_csv(history, out)

    assert written == len(history["rows"])
    with open(out, newline="", encoding="utf-8") as handle:
        table = list(csv.DictReader(handle))
    assert len(table) == 4
    assert "round_1" in table[0]
    assert table[0]["id"] == "q2" and table[0]["round_1"] == "broke"
    # a person sorts this column in a spreadsheet; it must be a number
    assert all(row["times_broken"].isdigit() for row in table)


def test_an_empty_workspace_says_why_instead_of_failing(tmp_path):
    """`history` is the verb a person runs when they do not know where they
    are, so it must answer rather than raise."""
    history = build(tmp_path)

    assert history["rows"] == []
    assert "no baseline" in lines(history)[0]


def test_next_recommends_history_once_there_are_two_rounds_to_compare():
    """A verb nothing recommends is a verb nobody runs.

    That is the fault D62 found in `split`: it wrote two files and `round`
    did not read them, so people stopped running it. `history` only becomes
    useful at the second round, so `next` proposes it from there and not
    before.
    """
    import inspect

    from context_lift import loop
    source = inspect.getsource(loop)
    assert '"history", "--workspace"' in source
    assert "rounds_closed >= 2" in source
