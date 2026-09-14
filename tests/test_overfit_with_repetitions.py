# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""One page must not hold two opposite verdicts, at any repetition count.

THE DEFECT, found 2026-09-14 by an outside review reading the code rather than
by anything failing. The OVERFIT rule computed its gap from
`curate.mcnemar.b/c` -- the POOLED per-ANSWER discordant counts -- against
`curate.n_defended`, a per-QUESTION denominator. At three repetitions the
counts are three times larger and the denominator is not, so the interval
narrows by about sqrt(K) and its lower bound crosses above zero on a gap that
genuinely includes zero.

The tool then printed OVERFIT, and `RoundResult.gap()` printed
+22.2 [-4.0, +48.4] on the line below. Two opposite statements, one page.

WHY 1,788 TESTS STAYED GREEN. Every one used a single repetition per arm, and
at K = 1 `mcnemar` and `defended` are the same object, so the bug is invisible.
The README tells every user to run three. That is the shape of defect this
file exists to stop: correct under the conditions the suite uses, wrong under
the conditions the documentation recommends.

This is also the SECOND time the page contradicted itself about OVERFIT. On
2026-09-10 the cause was the difference-in-significance fallacy, fixed by
making the gap interval the test. The lesson that did not carry across: the
verdict has to quote the number the reader is shown.
"""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import pytest

from context_lift.ablate import ablate, verdict
from context_lift.round import RoundResult
from test_ablate import _report


def _arm(n: int, fixed: int, reps: int):
    """`n` questions, `fixed` of them wrong without and right with, `reps` each."""
    without, with_ = [], []
    for q in range(n):
        w0, w1 = (False, True) if q < fixed else (True, True)
        for r in range(reps):
            without.append((f"q{q}#rep{r}", w0))
            with_.append((f"q{q}#rep{r}", w1))
    return ablate(_report(without), _report(with_))


def _round(n: int, curate_fixed: int, holdout_fixed: int, reps: int):
    curate = _arm(n, curate_fixed, reps)
    holdout = _arm(n, holdout_fixed, reps)
    word = verdict(holdout, curate=curate)
    return RoundResult(
        verdict=word, curate=curate, holdout=holdout, k_repeats=reps,
        curate_ids=[f"q{q}" for q in range(n)],
        holdout_ids=[f"q{q}" for q in range(n)])


@pytest.mark.parametrize("reps", [1, 2, 3, 5])
def test_overfit_never_contradicts_the_gap_printed_below_it(reps):
    """The exact fixture from the review: 18 questions, 6 working fixed, 2
    held out fixed. At three repetitions this printed OVERFIT while the gap
    included zero."""
    result = _round(18, curate_fixed=6, holdout_fixed=2, reps=reps)
    point, low, high = result.gap()

    if result.verdict.word == "OVERFIT":
        assert not (low <= 0 <= high), (
            f"K={reps}: the verdict says OVERFIT while the gap it prints on "
            f"the next line is {point:+.1f} [{low:+.1f}, {high:+.1f}], which "
            f"includes zero. One page, two opposite verdicts.")


@pytest.mark.parametrize("reps", [2, 3, 5])
def test_the_verdict_does_not_change_with_the_repetition_count_alone(reps):
    """Repetitions measure STABILITY, not more questions. Running the same
    questions again must not turn a verdict nobody could demonstrate into
    one that sounds certain."""
    one = _round(18, 6, 2, reps=1)
    many = _round(18, 6, 2, reps=reps)

    assert one.verdict.word == many.verdict.word, (
        f"K=1 says {one.verdict.word} and K={reps} says {many.verdict.word} on "
        f"identical per-question outcomes. Repeating an answer is not new "
        f"evidence about a new question.")


@pytest.mark.parametrize("reps", [1, 3])
def test_the_gap_is_unchanged_by_repetitions(reps):
    """The control for the test above: the gap itself is already per
    question, so it should not move. If this ever fails, the fix went into
    the wrong place."""
    point, low, high = _round(18, 6, 2, reps=reps).gap()

    assert round(point, 1) == 22.2
    assert round(low, 1) == -4.0
    assert round(high, 1) == 48.4


def test_a_real_overfit_is_still_reported():
    """The fix must not silence the verdict it repairs. A working set that
    moves far more than the held-out one, with a gap excluding zero, is what
    OVERFIT is for."""
    result = _round(60, curate_fixed=40, holdout_fixed=2, reps=3)
    point, low, high = result.gap()

    assert low > 0, f"this fixture no longer has a gap excluding zero: {low:+.1f}"
    assert result.verdict.word == "OVERFIT", (
        f"a working set up {point:+.1f} points over the held-out one, with the "
        f"gap excluding zero, must still read OVERFIT. Got "
        f"{result.verdict.word}.")
