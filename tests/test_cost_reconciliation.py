# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""A round's printed cost against the warehouse's own invoice.

M20's exit test: *a round's printed cost reconciles with the job ledger within
5%, asserted by a test rather than checked by a person, and both cost per
answer and cost per correct answer are printed.*

WHY IT EXISTS. Per-command `spent:` lines totalled about $74 while the ledger
reported $149.61 across 2,686 jobs. Roughly half the bill was real, unreported
and invisible, because `spent:` counts MODEL calls and cannot count the SQL the
agent writes -- that query does not exist until the run does, and it is billed
to the warehouse rather than to the model API.

Nothing caught that for weeks, and a person re-checking it by hand is how it
went uncaught. So the check is a test.

The arithmetic is pure, so every case here runs offline against a synthetic
ledger. That is not a shortcut: it lets a test set up what a real round almost
never offers on demand -- an arm that got nothing right, a job missing from
history, a printed figure that is exactly half the invoice.
"""
from __future__ import annotations

import pytest

from context_lift.cost import (BYTES_PER_TIB, ON_DEMAND_USD_PER_TIB, ArmCost,
                               RoundCost, per_arm, reconciles,
                               reconciliation_note, usd_for_bytes)

ONE_TIB = BYTES_PER_TIB


def _row(question, arm, job, correct):
    return {"id": question, "arm": arm, "job_id": job,
            "correct": "1" if correct else "0"}


# ---------------------------------------------------------------------------
# the price, in one place

def test_one_tebibyte_costs_the_on_demand_rate():
    assert usd_for_bytes(ONE_TIB) == pytest.approx(ON_DEMAND_USD_PER_TIB)
    assert usd_for_bytes(0) == 0.0


def test_the_price_is_defined_once():
    """A price copied into a script and a report disagrees with itself the
    day it changes, and the disagreement shows up in a published figure."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    script = (root / "scripts" / "answer_cost.py").read_text()
    assert "ON_DEMAND_USD_PER_TIB = 6.25" not in script, (
        "scripts/answer_cost.py defines its own copy of the price. Import it "
        "from context_lift.cost instead")
    assert re.search(r"from context_lift\.cost import|from \.cost import",
                     script), "the script should take the price from the module"


# ---------------------------------------------------------------------------
# THE EXIT TEST

def test_a_printed_cost_reconciles_with_the_ledger_within_five_percent():
    """M20's exit test, first half."""
    rows = [_row("q1", "control", "j1", True),
            _row("q2", "control", "j2", False),
            _row("q3", "treatment", "j3", True)]
    ledger = {"j1": ONE_TIB, "j2": ONE_TIB, "j3": ONE_TIB}

    cost = per_arm(rows, ledger)
    invoice = usd_for_bytes(3 * ONE_TIB)

    assert reconciles(cost.total_usd, invoice)
    assert cost.total_usd == pytest.approx(invoice)


def test_both_figures_are_printed_because_they_move_in_opposite_directions():
    """M20's exit test, second half, and the reason it is worded that way.

    The treatment arm here bills MORE in total but gets twice as many answers
    right. Cost per answer rises and cost per correct answer falls, so quoting
    either alone tells the opposite story from the other.
    """
    rows = [_row("q1", "control", "c1", True),
            _row("q2", "control", "c2", False),
            _row("q3", "treatment", "t1", True),
            _row("q4", "treatment", "t2", True)]
    ledger = {"c1": ONE_TIB, "c2": ONE_TIB, "t1": ONE_TIB, "t2": 2 * ONE_TIB}

    cost = per_arm(rows, ledger)
    control, treatment = cost.arms[0], cost.arms[1]

    assert treatment.usd > control.usd
    assert treatment.usd_per_answer > control.usd_per_answer
    assert treatment.usd_per_correct < control.usd_per_correct

    printed = cost.text()
    assert "/answer" in printed and "/correct answer" in printed
    assert "not a cost result on its own" in printed


# ---------------------------------------------------------------------------
# the failure the $74 / $149.61 gap was

def test_reporting_half_the_bill_does_not_reconcile():
    """The exact shape of the defect: about $74 printed against $149.61
    billed. A check comparing the printed figure to itself would pass."""
    assert not reconciles(74.0, 149.61)

    note = reconciliation_note(74.0, 149.61)
    assert "COST DOES NOT RECONCILE" in note
    assert "49% of it" in note
    assert "not rounding" in note


def test_the_tolerance_is_measured_against_the_ledger_not_the_claim():
    """Relative to the invoice, because the invoice is the truth. Dividing by
    the printed figure would let an under-report pass by being consistent
    with its own mistake."""
    assert reconciles(100.0, 104.0)        # 3.8% under the ledger
    assert not reconciles(100.0, 110.0)    # 9.1% under the ledger
    assert reconciles(0.0, 0.0)
    assert not reconciles(1.0, 0.0), (
        "a charge with nothing behind it in the ledger is not a rounding "
        "difference")


def test_a_reconciling_round_says_so_in_one_sentence():
    note = reconciliation_note(100.0, 102.0)

    assert "cost reconciles" in note
    assert "within 5%" in note


# ---------------------------------------------------------------------------
# a job nobody can read is not a free answer

def test_a_job_missing_from_history_is_excluded_and_counted():
    """Counting it as zero would make a round look cheaper the LESS of it
    could be read, which is the wrong direction for a number to fail in."""
    rows = [_row("q1", "control", "j1", True),
            _row("q2", "control", "gone", True)]

    cost = per_arm(rows, {"j1": ONE_TIB})

    assert cost.jobs_not_in_history == 1
    assert cost.answers_with_a_job == 1
    assert cost.arms[0].answers == 1
    assert cost.total_usd == pytest.approx(ON_DEMAND_USD_PER_TIB)
    assert "EXCLUDED rather than counted as free" in cost.text()


def test_an_answer_with_no_job_id_at_all_is_not_counted_as_unreadable():
    """A refused answer wrote no SQL, so it has no job and nothing is
    missing. Counting it as an unreadable job would report a data problem
    that is not there."""
    rows = [_row("q1", "control", "", False),
            _row("q2", "control", "j1", True)]

    cost = per_arm(rows, {"j1": ONE_TIB})

    assert cost.jobs_not_in_history == 0
    assert cost.answers_with_a_job == 1


def test_a_round_nothing_could_be_read_for_says_it_is_not_zero():
    """The most dangerous output in the file: a cost line reading $0.0000
    would be quoted as a free round."""
    cost = per_arm([_row("q1", "control", "gone", True)], {})

    assert cost.arms == ()
    text = cost.text()
    assert "not recovered" in text
    assert "It is not zero" in text


# ---------------------------------------------------------------------------
# dividing by nothing

def test_an_arm_that_got_nothing_right_has_no_cost_per_correct_answer():
    """Undefined, not infinite, and not zero. Printing 0 would read as a
    free correct answer from an arm that produced none."""
    arm = ArmCost(arm="control", answers=3, bytes_billed=ONE_TIB, correct=0)

    assert arm.usd_per_correct is None
    assert arm.usd_per_answer == pytest.approx(ON_DEMAND_USD_PER_TIB / 3)
    assert "n/a/correct answer" in RoundCost(arms=(arm,),
                                             answers_with_a_job=3).text()


def test_an_arm_with_no_readable_answers_has_no_per_answer_figure():
    arm = ArmCost(arm="control", answers=0, bytes_billed=0, correct=0)

    assert arm.usd_per_answer is None
    assert arm.usd_per_correct is None


# ---------------------------------------------------------------------------
# what the cost line must keep saying

def test_the_cost_line_says_the_model_bill_is_somewhere_else():
    """The whole defect in one sentence: two separate bills, and a reader who
    thinks either line is the total is wrong by about half."""
    cost = per_arm([_row("q1", "control", "j1", True)], {"j1": ONE_TIB})

    assert "Neither line is the whole bill" in cost.text()
    assert "`spent:` line" in cost.text()
