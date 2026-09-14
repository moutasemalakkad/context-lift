# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The perturbation suite at three repetitions, and the claim it withdrew.

The 1-repetition run printed, in its own output, "one repetition per arm: a
single sample, not a measurement; run each arm again before quoting this".
This is that second run: 450 questions x 3 reps = 1350 observations per arm.

WHY THIS FILE EXISTS RATHER THAN AN EDIT TO THE 1-REP TEST. The second run
CONTRADICTED a published lead. At one repetition `irrelevant-context` was the
worst of the four realistic defects at -2.9 and was written up as worth
following. At three it is the least harmful at -0.3. Keeping both numbers
checkable, in one place, is what stops the withdrawal being quietly forgotten
and the -2.9 being re-quoted from an old document.
"""
from __future__ import annotations

import pathlib
import re

import pytest

EVIDENCE = pathlib.Path(__file__).resolve().parent.parent / "docs" / "evidence"
THREE = EVIDENCE / "perturbation-bird-3reps.txt"
ONE = EVIDENCE / "perturbation-bird.txt"


@pytest.fixture(scope="module")
def three() -> str:
    if not THREE.is_file():
        pytest.skip("the 3-rep evidence file is absent")
    return THREE.read_text()


# ---------------------------------------------------------------------------
# the control, which is what makes every other row worth reading

def test_the_positive_control_still_registers_as_a_real_loss(three):
    """A suite whose control comes back null is measuring its own deafness.
    This one broke 92 questions of 450 and fixed 14."""
    assert "lift            : -17.0%  [-21.5%, -13.1%]" in three
    assert "fixed 14 / broke 92 of 450 questions" in three
    assert "ruling          : REAL LOSS" in three


def test_the_per_question_number_is_the_one_the_file_tells_you_to_quote(three):
    """Three answers to one question are not three independent facts. The
    per-answer p-value has an inflated denominator and reads low."""
    assert "READ THE PER-QUESTION NUMBER" in three
    assert "each question counted once - the number to defend" in three
    assert "each answer counted separately - optimistic" in three
    # the control's two denominators, both present so the gap is visible
    assert "of 450 questions" in three and "of 1350 observations" in three


def test_all_five_arms_are_reported_not_only_the_ones_that_moved(three):
    for name in ("positive-control-drop-value-decoding", "unknown-column",
                 "strip-subject", "conflicting-definitions",
                 "irrelevant-context"):
        assert f"=================== {name} ===================" in three


# ---------------------------------------------------------------------------
# THE WITHDRAWAL

def test_the_irrelevant_context_lead_is_withdrawn_in_writing(three):
    """The one claim this run reversed. -2.9 and worst-of-four became -0.3
    and least-of-four."""
    flat = " ".join(three.split())

    assert "lift            :  -0.3%  [-2.4%, +3.8%]" in three
    assert "That lead is withdrawn" in flat
    assert "noise read as a ranking" in flat


def test_the_one_rep_column_matches_the_one_rep_evidence_file(three):
    """THE CHECK THAT CAUGHT A REAL MISTAKE. The first draft of the
    comparison table had strip-subject at -1.1 and conflicting-definitions at
    -1.5; the committed 1-rep file says -2.7 and -1.1. Two numbers typed from
    memory into a table whose whole purpose is comparing two runs.

    So the table is now checked against the file it claims to quote.
    """
    if not ONE.is_file():
        pytest.skip("the 1-rep evidence file is absent")
    one = ONE.read_text()

    # the 1-rep curve, as that file prints it: name then lift
    quoted = dict(re.findall(r"^(\S+)\s+(-?\d+\.\d)%", one, re.M))
    assert quoted, "the 1-rep curve did not parse; the check would be vacuous"

    # a lift can be positive: unknown-column reads +0.2 at three repetitions,
    # and the first version of this regex silently dropped that row
    table = re.findall(r"^    (\S+)\s+([-+]\d+\.\d)\s+([-+]\d+\.\d)\s", three, re.M)
    assert len(table) == 5, f"the comparison table has {len(table)} rows, not 5"

    for name, one_rep, _three_rep in table:
        matching = [v for k, v in quoted.items() if k.startswith(name)]
        assert matching, f"{name} is in the table but not in the 1-rep file"
        assert matching[0] == one_rep, (
            f"the table says {name} measured {one_rep} at one repetition; "
            f"the 1-rep evidence file says {matching[0]}")


def test_the_table_says_where_its_first_column_came_from(three):
    """A number with no source is the thing this project keeps catching
    itself doing."""
    flat = " ".join(three.split())
    assert "quoted from docs/evidence/perturbation-bird.txt" in flat
    assert "not retyped from memory" in flat


# ---------------------------------------------------------------------------
# the limits, which are most of the result

def test_four_of_five_rows_are_not_proven_and_the_file_says_what_that_means(three):
    assert three.count("ruling          : NOT PROVEN") == 4

    flat = " ".join(three.split())
    assert "450 questions resolve a change above about 5 points" in flat
    assert "costs less than 5 points OR that this set cannot see it" in flat
    # the tool's own answer to "how many would it take"
    assert "more than 5000 questions" in three


def test_it_refuses_to_generalise_to_an_estate(three):
    flat = " ".join(three.split())
    assert "may cost a great deal on a 900-dataset estate" in flat
    assert "INJECTED defects on public data, not defects found in the wild" in flat


def test_the_re_score_path_is_free_and_the_file_says_so(three):
    """A run this size should never be paid for twice to fix a print."""
    assert "perturb_bird_score.py" in three
    assert "costs no model calls" in three
