# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Which rule broke that question, and what the answer costs.

M17's exit test, stated on the board before any of this was written: *a round
with five rules where one breaks a held-out question names that rule in
`ceil(log2(5)) = 3` arms, and the two questions appear in the spent file
afterwards.*

The search takes a `reproduces(subset)` callback, so every test here drives it
with a plain function and no model is asked. That is not only for speed: it
lets a test set up the cases a paid run would almost never produce on demand --
rules that interact, a break that no half reproduces, a rule the search
narrowed to without ever testing it alone.

TWO ENDINGS, and telling them apart is the point. A subset watched reproducing
the break names a CULPRIT. A rule arrived at by elimination and never tested
alone is a SUSPECT, and the search spends one more arm rather than printing a
guess in the same words as a finding.
"""
from __future__ import annotations

import pathlib

from context_lift.bisect import (Rule, arms_needed, broke_in_run, find,
                                 record_spent,
                                 refusal, rules_in, spec_with, spend_note)
from context_lift.export import Spec, TableSchema
from context_lift.round import REPEAT_MARKER

TABLE = "shop.orders"


def a_spec(*rule_lines: str) -> Spec:
    """A spec whose rules file carries the given lines, plus prose that is
    not a candidate."""
    spec = Spec(root=pathlib.Path("(test)"))
    spec.schemas[TABLE] = TableSchema()
    body = "Some prose that is not a rule.\n" + "\n".join(rule_lines)
    spec.rules.append(("00-definitions.md", body))
    return spec


FIVE = ["- Revenue excludes returns.",
        "- Cancelled orders never count.",
        "- A completed order has status = 'Complete'.",
        "- Prices are in USD.",
        "- Test stores are excluded."]


# ---------------------------------------------------------------------------
# reading the rules out of a spec

def test_only_lines_that_look_like_rules_are_candidates():
    """Prose in a rules file reaches every arm and is never blamed. A tool
    that silently treated a paragraph as a rule would name a line nobody
    wrote as a rule."""
    rules = rules_in(a_spec(*FIVE))

    assert len(rules) == 5
    assert [r.text for r in rules] == FIVE
    assert all(r.file == "00-definitions.md" for r in rules)
    # line 1 is the prose, so the first rule is line 2
    assert [r.line for r in rules] == [2, 3, 4, 5, 6]
    assert rules[0].key == "00-definitions.md:2"


def test_a_narrowed_spec_keeps_the_prose_and_drops_the_other_rules():
    spec = a_spec(*FIVE)
    rules = rules_in(spec)

    narrowed = spec_with(spec, {rules[1].key})

    body = narrowed.rules[0][1]
    assert "Some prose that is not a rule." in body
    assert "Cancelled orders never count." in body
    assert "Revenue excludes returns." not in body
    assert body.count("\n- ") == 0 or body.count("- ") == 1


def test_a_rules_file_left_with_nothing_is_dropped_entirely():
    """`system_instruction` skips a body that strips to empty, so an arm
    carrying a lone heading would hash differently for a reason no reader
    could see."""
    spec = Spec(root=pathlib.Path("(test)"))
    spec.schemas[TABLE] = TableSchema()
    spec.rules.append(("10-output.md", "- Round money to cents."))

    assert spec_with(spec, set()).rules == []


def test_narrowing_does_not_mutate_the_spec_it_was_given():
    """The search builds many arms from one spec. If narrowing edited it in
    place, arm two would be built from arm one's leftovers."""
    spec = a_spec(*FIVE)
    spec_with(spec, set())

    assert len(rules_in(spec)) == 5


# ---------------------------------------------------------------------------
# the arithmetic

def test_the_arm_count_is_log_not_linear():
    """The whole argument for bisecting. Five rules cost three arms, not
    five, and a hundred rules cost seven."""
    assert arms_needed(5) == 3
    assert arms_needed(8) == 3
    assert arms_needed(9) == 4
    assert arms_needed(100) == 7


def test_one_rule_needs_no_arm_at_all():
    """With a single candidate the round already named it, and an arm asked
    to confirm what is already certain is money spent on nothing."""
    assert arms_needed(1) == 0
    assert arms_needed(0) == 0


# ---------------------------------------------------------------------------
# THE EXIT TEST

def test_five_rules_one_culprit_named_in_three_arms():
    """M17's exit test, first half, verbatim: a round with five rules where
    one breaks a held-out question names that rule in ceil(log2(5)) = 3
    arms."""
    rules = rules_in(a_spec(*FIVE))
    guilty = rules[1]
    asked = []

    def reproduces(subset):
        asked.append([r.key for r in subset])
        return guilty in subset

    result = find("q014", rules, reproduces)

    assert result.culprit == guilty
    assert result.confirmed is True
    assert result.arms == 3, f"took {result.arms} arms: {asked}"
    assert "00-definitions.md:3 breaks q014" in result.line()


def test_the_last_arm_tested_the_named_rule_on_its_own():
    """What makes it a culprit rather than a suspect. The final subset the
    search asked about held exactly the rule it then named."""
    rules = rules_in(a_spec(*FIVE))
    guilty = rules[1]
    asked = []

    def reproduces(subset):
        asked.append(list(subset))
        return guilty in subset

    find("q014", rules, reproduces)

    assert asked[-1] == [guilty]


def test_a_rule_reached_by_elimination_costs_one_more_arm_to_confirm():
    """Narrowing can end holding a rule the search never tested alone. That
    is a guess until it is watched reproducing the break, so one more arm is
    spent rather than printing a guess in the words of a finding."""
    rules = rules_in(a_spec(*FIVE))
    guilty = rules[4]

    def reproduces(subset):
        return guilty in subset

    result = find("q031", rules, reproduces)

    assert result.culprit == guilty
    assert result.confirmed is True
    assert result.arms == arms_needed(5) + 1 == 4


def test_rules_that_interact_are_reported_as_such_not_blamed_on_one():
    """The ending that matters most. A break needing two rules together must
    not be pinned on whichever one the halving happened to land on."""
    rules = rules_in(a_spec(*FIVE))
    pair = {rules[0], rules[4]}

    def reproduces(subset):
        return pair <= set(subset)

    result = find("q014", rules, reproduces)

    assert result.culprit is None
    assert result.confirmed is False
    assert "the rules interact" in result.note
    assert "no single rule reproduces it" in result.line()


def test_a_spec_with_no_rule_lines_says_so_rather_than_crashing():
    result = find("q014", [], lambda subset: True)

    assert result.culprit is None
    assert result.arms == 0
    assert "no rule lines to blame" in result.note


# ---------------------------------------------------------------------------
# reading the broken questions out of the round

def _row(question, arm, correct, split="holdout", rep=0):
    return {"id": f"{question}{REPEAT_MARKER}{rep}", "arm": arm, "split": split,
            "correct": "1" if correct else "0"}


def test_the_broken_questions_come_from_the_run_not_from_typing():
    rows = [
        _row("q014", "0d control", True), _row("q014", "1 authored", False),
        _row("q031", "0d control", True), _row("q031", "1 authored", True),
        _row("q002", "0d control", False), _row("q002", "1 authored", True),
    ]

    assert broke_in_run(rows) == ["q014"]


def test_repetitions_collapse_by_majority():
    """Three answers to one question are not three facts. A question right
    twice and wrong once is not broken."""
    rows = []
    for rep, correct in enumerate([True, True, True]):
        rows.append(_row("q014", "0d control", correct, rep=rep))
    for rep, correct in enumerate([False, True, True]):
        rows.append(_row("q014", "1 authored", correct, rep=rep))

    assert broke_in_run(rows) == []


def test_a_working_question_that_broke_is_not_bisected():
    """There is nothing to protect: a working question can be asked as often
    as anyone likes, so spending a holdout read on it buys nothing."""
    rows = [_row("q014", "0d control", True, split="curate"),
            _row("q014", "1 authored", False, split="curate")]

    assert broke_in_run(rows) == []


def test_a_run_with_one_arm_yields_nothing_rather_than_guessing():
    """A break is a comparison. With one arm there is no without-context
    answer, so there is no way to know the context caused anything."""
    rows = [_row("q014", "1 authored", False)]

    assert broke_in_run(rows) == []


# ---------------------------------------------------------------------------
# the refusal and the trade

def test_a_safe_round_is_refused_because_the_arms_cost_money():
    stop = refusal("SAFE")

    assert stop is not None
    assert "broke nothing" in stop
    assert refusal("REGRESSION") is None
    assert refusal("MIXED") is None


def test_the_trade_is_stated_as_arithmetic_a_person_can_check():
    """With 36 golds spending 2 is a good trade; with 12 it may not be. The
    tool prints the numbers rather than deciding for somebody."""
    note = spend_note(["q014", "q031"], reserved=12)

    assert "this bisect spends q014, q031" in note
    assert "you have 12 reserved question(s); after this you have 10" in note
    assert "never counts in a reported number again" in note


# ---------------------------------------------------------------------------
# THE EXIT TEST, second half: the questions appear in the spent file

def test_the_bisected_questions_land_in_the_spent_file(tmp_path):
    """M17's exit test, second half, verbatim: *and the two questions appear
    in the spent file afterwards.*

    That file is the only thing between a bisected question and its
    reappearance in a reported number, which is why the board insisted the
    guard be enforced rather than described.
    """
    spent = tmp_path / "spent.txt"

    record_spent(spent, ["q014", "q031"])

    assert spent.read_text().split() == ["q014", "q031"]


def test_a_second_bisect_adds_to_the_file_rather_than_replacing_it():
    """Overwriting would quietly un-spend every earlier question, and the
    reserved half would grow back without anyone touching it."""
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        path = pathlib.Path(directory) / "spent.txt"
        record_spent(path, ["q014"])
        combined = record_spent(path, ["q002"], existing={"q014"})

    assert combined == ["q002", "q014"]


def test_the_spent_file_is_written_even_when_nothing_was_found(tmp_path):
    """The interaction case still read the questions. Recording only
    successful bisects would leak exactly the reads nobody learned from."""
    spent = tmp_path / "spent.txt"

    record_spent(spent, ["q014"])

    assert "q014" in spent.read_text()
