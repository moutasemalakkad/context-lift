# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Two power figures answer different questions, and neither stands alone.

`questions_for_target` is the BEST CASE: it assumes every question the context
changed was fixed and none broke (b = 0). Its own docstring says so and says
"which the caller must say out loud". Nothing enforced that.

`questions_to_prove` scales the swing a run ACTUALLY saw, breaks included.

Printing only the first reads as "get this many questions and you are done",
when a round with even one break needs more. Printing only the second reads as
"this particular swing is unprovable", when the set could never have resolved
the target whatever it saw.

WHY THIS FILE EXISTS. An outside review flagged `questions_needed` as a
best-case figure reported as definitive. Checked: it is not -- both numbers
are printed on one line and a comment in `ablate.py` explains why. This test
is what keeps that true, because the arrangement is a convention that one edit
could quietly undo.

THE SENSITIVITY IS LARGE, which is why it matters. `questions_needed(5)`
assumes a 2% break rate and returns 303. At the 10% break rate this project
has actually measured in perturbation runs it is 817, and the 450-question
floor doubles from 4.0 points to 8.0.
"""
from __future__ import annotations

import pytest

from context_lift.ablate import (PROVE_CAP, questions_for_target,
                                 questions_to_prove)
from context_lift.power import questions_needed, smallest_resolvable


# ---------------------------------------------------------------------------
# the two functions really are different, and the gap is big

def test_the_best_case_figure_is_smaller_than_a_realistic_one():
    """If these agreed there would be no reason to print both."""
    best = questions_for_target(10.0)

    # a run that fixed 8 and broke 3 of 60 -- an ordinary shape here
    realistic = questions_to_prove(b=3, c=8, n=60)

    assert best is not None and realistic is not None
    assert realistic > best, (best, realistic)


def test_the_break_rate_assumption_moves_the_answer_by_more_than_double():
    """2% is the rate measured on the BEAVER holdout (19 broken of 596). The
    perturbation runs broke 24 to 33 of 450, which is 5 to 7%, and the
    positive control broke 92 of 450, which is 20%. So 2% is the optimistic
    end of this project's own range, not the middle of it."""
    assert questions_needed(5, break_rate=0.02) == 303
    assert questions_needed(5, break_rate=0.10) == 817

    assert smallest_resolvable(450, break_rate=0.02) == 4.0
    assert smallest_resolvable(450, break_rate=0.10) == 8.0


def test_an_equal_swing_can_never_be_proven_and_says_so():
    """As many broke as were fixed. No sample size fixes that, and returning
    a big number would suggest one would."""
    assert questions_to_prove(b=5, c=5, n=100) is None


# ---------------------------------------------------------------------------
# THE CONTRACT: both numbers on the line

def _line(b: int, c: int, n: int, lift: float) -> str:
    """The verdict sentence a report of this shape produces.

    The word is NOT passed in: `verdict_sentences` derives it from the report
    via `verdict_word(self)`, which is the right design -- a caller cannot ask
    for a verdict the numbers do not support. So each test below builds the
    SHAPE it wants and reads what comes out.
    """
    from context_lift.ablate import AblationReport
    from context_lift.metrics import mcnemar_exact, proportion

    report = AblationReport(
        without_context=proportion(10, n), with_context=proportion(10, n),
        lift=lift, mcnemar=mcnemar_exact(b, c), n_paired=n, adjusted=None)
    lines = report.verdict_sentences()
    assert lines, "no verdict sentence was produced at all"
    return lines[0]


def test_a_not_proven_line_carries_the_floor_and_the_realistic_count():
    line = _line(b=2, c=6, n=60, lift=0.05)

    assert "can only prove a lift above about" in line, line
    assert "1/sqrt(60)" in line, line
    assert "you would need about" in line or "more than" in line, line


def test_an_underpowered_line_states_the_best_case_assumption_out_loud():
    """The exact words matter. "even if nothing broke" is the caveat that
    stops the number reading as a promise."""
    line = _line(b=0, c=1, n=12, lift=0.05)

    assert "even if nothing broke" in line, line
    assert "questions could" in line, line


def test_a_loss_is_not_offered_a_sample_size_to_chase():
    """A negative lift with a bigger set is a bigger loss. Printing "you
    would need N questions" beside it invites somebody to buy one."""
    line = _line(b=6, c=2, n=60, lift=-0.05)

    assert "is inside that" in line, line
    assert "you would need" not in line, line


def test_never_disagreed_is_reported_as_that_and_sent_somewhere_useful():
    """Two arms that never disagreed is not a power problem, so the sentence
    does not offer a sample size. It names the checks that explain it."""
    line = _line(b=0, c=0, n=60, lift=0.0)

    assert "never disagreed" in line, line
    assert "you would need" not in line, line
    assert "score-routing" in line, line


def test_an_equal_swing_line_says_no_number_would_help():
    line = _line(b=4, c=4, n=60, lift=0.0)

    assert "no number of questions would prove a lift of this shape" in line


def test_the_comment_explaining_why_both_print_is_still_there():
    """The arrangement is a convention, and a convention with no note beside
    it gets simplified away by somebody tidying up."""
    import inspect

    from context_lift import ablate

    # collapsed: these phrases wrap across lines in the source
    source = " ".join(inspect.getsource(ablate).split())
    assert "they answer different questions" in source
    assert "Printing only one" in source
    assert "must say out loud" in source, (
        "questions_for_target's docstring no longer states that it is the "
        "best case, which is the whole reason two numbers are printed")


# ---------------------------------------------------------------------------
# the ladder's noise floor, which was 1.73x too small at K=3

def _report(questions: int, reps: int):
    from context_lift.harness import EvalReport, QuestionResult
    from context_lift.metrics import proportion

    rows = [
        QuestionResult(id=(f"q{q}#rep{r}" if reps > 1 else f"q{q}"),
                       question="x", verdict="MATCH",
                       execution_correct=(q % 2 == 0), adjusted_correct=None,
                       judged=False, judge_verdict=None, detail="")
        for q in range(questions) for r in range(reps)]
    n = questions * reps
    return EvalReport(results=rows, n_total=n, n_scored=n, n_excluded=0,
                      execution=proportion(n // 2, n),
                      adjusted=proportion(n // 2, n))


def test_the_ladder_floor_counts_questions_not_answers():
    """THE FOURTH INSTANCE of the same denominator mistake, found the same day
    as the OVERFIT check and the perturbation example's power floor.

    `correctness_vector` returns one entry per SCORED ROW. With three
    repetitions a question contributes three, so `climb` divided by 3n and got
    a floor of 1/sqrt(3n) -- 18.3 points where it should read 31.6 on ten
    questions. Blum & Hardt's step exists precisely to stop a climb on noise,
    and an undersized step lets a 20-point non-result move the reported score.
    """
    from context_lift.ablate import _questions_in, ladder_step

    one = _report(questions=10, reps=1)
    three = _report(questions=10, reps=3)

    assert len(three.correctness_vector()) == 30, "the vector is per row"
    assert _questions_in(three) == 10, "but the floor must count questions"

    # the same ten questions, so the same floor whatever K is
    assert ladder_step(_questions_in(one)) == ladder_step(_questions_in(three))
    assert round(ladder_step(_questions_in(three)), 4) == 0.3162

    # and the old, wrong denominator really was smaller
    assert ladder_step(len(three.correctness_vector())) < \
        ladder_step(_questions_in(three))


def test_the_question_count_uses_the_same_rep_convention_as_the_clustered_test():
    """`split("#rep")[0]` in five places already. A sixth definition of "what
    is a question" is how two parts of one report come to disagree."""
    import inspect

    from context_lift import ablate

    source = inspect.getsource(ablate._questions_in)
    assert 'split("#rep")[0]' in source


def test_rows_that_were_never_scored_do_not_count_as_questions():
    """An excluded question is not a question the floor gets to divide by."""
    from context_lift.ablate import _questions_in
    from context_lift.harness import EvalReport, QuestionResult
    from context_lift.metrics import proportion

    def row(i, ok):
        return QuestionResult(id=i, question="x", verdict="MATCH",
                              execution_correct=ok, adjusted_correct=None,
                              judged=False, judge_verdict=None, detail="")

    report = EvalReport(
        results=[row("q1", True), row("q2", None), row("q3", False)],
        n_total=3, n_scored=2, n_excluded=1,
        execution=proportion(1, 2), adjusted=proportion(1, 2))

    assert _questions_in(report) == 2


def test_an_empty_report_is_zero_rather_than_a_crash():
    from context_lift.ablate import _questions_in
    from context_lift.harness import EvalReport
    from context_lift.metrics import proportion

    empty = EvalReport(results=[], n_total=0, n_scored=0, n_excluded=0,
                       execution=proportion(0, 0), adjusted=proportion(0, 0))

    assert _questions_in(empty) == 0
