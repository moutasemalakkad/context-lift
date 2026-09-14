# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""What one whole class of context was worth, by taking it away."""
import pytest

from context_lift.classes import (CLASSES, ITEM_LOO_MINIMUM, item_loo_refusal,
                              ladder, summary, without)
from context_lift.runners import Arm, Example, Term


def full():
    return Arm(label="1 authored", tables=["p.d.t"],
               table_descriptions={"p.d.t": "every order"},
               terms=[Term(name="revenue", definition="sales minus returns")],
               examples=[Example(question="how many", sql="SELECT 1")],
               system_instruction="- Exclude test stores.")


def test_removing_a_class_leaves_everything_else_untouched():
    """A removal, not a rebuild. Building each arm up from the control would
    let a later edit change two things at once and credit the class with both."""
    arm = full()
    stripped = without(arm, "examples")

    assert stripped.examples == []
    assert stripped.terms == arm.terms
    assert stripped.system_instruction == arm.system_instruction
    assert stripped.table_descriptions == arm.table_descriptions


def test_the_original_arm_is_not_changed():
    """Two classes priced in one process would otherwise share one arm and
    the second would be measured against an arm the first had emptied."""
    arm = full()
    without(arm, "examples")
    without(arm, "terms")

    assert len(arm.examples) == 1 and len(arm.terms) == 1


def test_a_class_the_arm_does_not_carry_is_skipped_not_run():
    """An arm with no terms measured against itself-minus-terms is two
    identical arms, and a round on two identical arms costs money to prove
    nothing."""
    bare = Arm(label="x", tables=["p.d.t"], system_instruction="- A rule.")

    priced = [item.name for item in ladder(bare)]

    assert priced == ["rules"]


def test_every_class_names_the_fields_it_owns():
    """Named deliberately rather than derived from the dataclass: a field
    added later must be assigned on purpose, not swept into whichever class
    its name sorts near."""
    for name, fields in CLASSES.items():
        assert fields, name
        for attribute in fields:
            assert hasattr(Arm(label="x"), attribute), (name, attribute)


def test_an_unknown_class_lists_the_real_ones():
    with pytest.raises(SystemExit) as refused:
        without(full(), "vibes")

    assert "examples" in str(refused.value)


def test_a_small_set_refuses_item_level_attribution_with_the_reason():
    """Printing a number the set cannot support is the mistake this whole
    tool exists to prevent. The person asking has a reason and needs to know
    what would make it possible."""
    refusal = item_loo_refusal(23)

    assert refusal is not None
    assert str(ITEM_LOO_MINIMUM) in refusal
    assert "Price a whole class instead" in refusal


def test_a_hundred_questions_allows_it():
    assert item_loo_refusal(ITEM_LOO_MINIMUM) is None


def test_the_plan_is_printed_before_anything_is_asked():
    text = summary(ladder(full()), 23)

    assert "4 class(es) to price" in text
    assert "refused" in text


def report(right, n):
    """An EvalReport-shaped stand-in: `right` of `n` questions correct."""
    from context_lift.harness import EvalReport, QuestionResult
    from context_lift.metrics import proportion
    results = [
        QuestionResult(id=f"q{i}", question="", verdict="MATCH" if i < right else "MISMATCH",
                       execution_correct=i < right, adjusted_correct=i < right,
                       judged=False, judge_verdict="", detail="")
        for i in range(n)]
    return EvalReport(results=results, n_total=n, n_scored=n, n_excluded=0,
                      execution=proportion(right, n), adjusted=proportion(right, n))


def test_a_class_doing_work_reads_as_positive_not_negative():
    """The sign is the thing a reader gets backwards. The arithmetic produces
    "removing examples scored -8"; what a person needs is "examples are worth
    +8". The report flips it and the raw field keeps the arithmetic."""
    from context_lift.classes import price, priced_text

    full = report(40, 74)          # the arm with everything
    without = report(28, 74)       # worse without the class

    [priced] = price(full, {"examples": (without, 22)})

    assert priced.lift_pp < 0, "the raw field keeps the arithmetic's sign"
    assert "+16.2 points" in priced.line(), "the report shows what it is worth"
    assert "carries its weight" in priced.line()


def test_a_class_the_arm_is_better_without_says_so():
    """The cheapest finding there is: context that makes the agent worse."""
    from context_lift.classes import price

    [priced] = price(report(28, 74), {"synonyms": (report(40, 74), 6)})

    assert priced.costs and not priced.carries
    assert "better WITHOUT it" in priced.line()


def test_not_shown_either_way_is_not_the_same_as_worthless():
    """A wide interval around zero means the set could not tell, and a reader
    who takes that as "delete it" deletes context that was working."""
    from context_lift.classes import price, priced_text

    [priced] = price(report(40, 74), {"terms": (report(39, 74), 5)})

    assert not priced.carries and not priced.costs
    assert "not shown either way" in priced.line()
    assert "too small to tell them apart" in priced_text([priced], 74)


def test_the_classes_are_ranked_worst_news_first():
    """The one to act on should not be the one a person scrolls to."""
    from context_lift.classes import price, priced_text

    priced = price(report(40, 74), {
        "helps": (report(20, 74), 10),      # removing it lost a lot
        "hurts": (report(60, 74), 10),      # removing it gained a lot
    })
    # The first line that names a class, not a fixed index: counting lines
    # ties the test to the header, and adding a sentence of explanation then
    # fails a test about ordering.
    rows = [line for line in priced_text(priced, 74).splitlines()
            if "helps" in line or "hurts" in line]

    assert rows, "no class rows in the report at all"
    assert "hurts" in rows[0], "the class to delete belongs at the top"


def test_every_class_is_compared_against_the_same_full_arm():
    """Two classes measured against different baselines cannot be read beside
    each other, and a page that puts them in one table invites exactly that."""
    from context_lift.classes import price

    full = report(40, 74)
    priced = price(full, {"a": (report(30, 74), 5), "b": (report(35, 74), 5)})

    assert len(priced) == 2
    assert all(p.n == 74 for p in priced)


def test_the_report_shows_both_rates_not_only_the_difference():
    """A difference of zero between 55% and 55% is a class that adds nothing.
    A difference of zero between 5% and 5% is an agent failing for a reason no
    context will fix. The line has to let a reader tell them apart; the first
    version printed only the difference and could not."""
    from context_lift.classes import Priced

    line = Priced("rules", 20, 0.0, -6.5, 6.5, 3, 3, 74,
                  full_pct=55.4, without_pct=55.4).line()

    assert "55% without it against 55% with" in line
    assert "3 fixed 3 broke" in line
