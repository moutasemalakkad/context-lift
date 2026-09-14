# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The thelook class experiment, pinned to its own evidence file.

M20's measurement half. The claim is not "context helps" -- it is that the
value of a class of context has TWO axes, accuracy and query bill, and that
they can move in opposite directions. This run is the case where they do.

WHY A TEST AND NOT JUST THE FILE. Every figure quoted anywhere else in the
repository about this experiment has to come from here, and a quote that
drifts from its run is worse than no quote. These assertions are the reason
the numbers in docs/ can be trusted: they fail if the evidence file changes
and the prose does not, or the other way round.
"""
from __future__ import annotations

import pathlib
import re

import pytest

EVIDENCE = (pathlib.Path(__file__).resolve().parent.parent
            / "docs" / "evidence" / "classes-thelook-cost.txt")


@pytest.fixture(scope="module")
def text() -> str:
    if not EVIDENCE.is_file():
        pytest.skip(f"{EVIDENCE.name} is absent")
    return EVIDENCE.read_text()


# ---------------------------------------------------------------------------
# the accuracy axis

def test_the_examples_class_is_the_only_one_that_clears_the_power_floor(text):
    """74 questions resolve about 12 points. Three of the four classes came
    back inside that floor, and the file has to say so rather than reading
    them as zero."""
    assert "-27.0 points [-37.8, -16.2]" in text
    assert "the arm is better WITHOUT it" in text

    for smaller in ("terms", "authored descriptions", "rules"):
        line = [l for l in text.splitlines() if l.strip().startswith(smaller)]
        assert line, f"no line for {smaller}"
        assert "not shown either way" in line[0], line[0]


def test_the_examples_interval_excludes_zero(text):
    """The one claim here strong enough to publish. If the interval included
    zero this would be a direction, not a result."""
    found = re.search(r"-27\.0 points \[(-[\d.]+), (-[\d.]+)\]", text)
    assert found, "the examples interval is not in the file in that shape"
    low, high = float(found.group(1)), float(found.group(2))
    assert low < high < 0, (low, high)


def test_the_direction_is_stated_in_words_not_only_as_a_sign(text):
    """A minus sign in front of a lift is read backwards by about half of
    everybody. The file says which arm was better in English."""
    assert "a NEGATIVE number means the arm scored" in text
    assert "BETTER without that class" in text


# ---------------------------------------------------------------------------
# the cost axis, and the reason both are needed

def test_removing_examples_raised_the_bill_and_halved_the_cost_per_correct(text):
    """THE M20 RESULT. The two axes moved in opposite directions, which is
    the whole argument for reporting both.

    More bytes scanned ($0.0107 -> $0.0113) and yet half the cost per useful
    answer ($0.0004 -> $0.0002), because accuracy went from 39% to 66%.
    """
    assert "0 full arm     $0.0107  $0.0001/answer  $0.0004/correct answer" in text
    assert "minus examples $0.0113  $0.0002/answer  $0.0002/correct answer" in text

    assert "RAISED the query bill, from $0.0107 to $0.0113" in text
    assert "HALVED the cost per correct answer, from $0.0004 to $0.0002" in text


def test_neither_cost_figure_is_offered_alone(text):
    """Cost per answer hides the accuracy change; cost per correct hides the
    bytes. The file refuses to let either stand as the result."""
    assert "Neither number is the result on its own" in text
    assert "Neither line is the whole bill" in text, (
        "the query bill is not the model bill and the file must say so")


def test_the_n_a_bug_is_recorded_rather_than_quietly_replaced(text):
    """The first run of this experiment printed n/a on every cost-per-correct
    row. Re-running from the checkpoint fixed it for the price of the scoring
    queries. Where the numbers came from belongs with the numbers."""
    assert 'printed\n"n/a/correct answer" on every row' in text or \
           'n/a/correct answer' in text
    assert "re-run off that run's checkpoint" in text


# ---------------------------------------------------------------------------
# it has to be re-runnable by a person

def test_the_file_names_a_committed_target_not_a_chat_command(text):
    """This experiment was first run as a command that existed only in a
    terminal. A result nobody else can reproduce is an anecdote."""
    assert "make classes-thelook PROJECT=" in text

    makefile = (pathlib.Path(__file__).resolve().parent.parent
                / "Makefile").read_text()
    assert "classes-thelook:" in makefile, (
        "the evidence file names a target the Makefile does not define")
    assert "RESUME" in makefile, "the free replay path is not in the target"


def test_no_project_name_leaked_into_the_evidence(text):
    """Reads the banned names from leakcheck rather than naming one here: a
    leak check that spells out what it forbids is itself an occurrence of
    it, and the tree scan flags it."""
    from leakcheck import leaks_in

    assert not leaks_in(text), leaks_in(text)
    assert "<project>" in text


def test_the_limits_are_stated_including_the_power_floor(text):
    assert "power.smallest_resolvable(74)" in text
    # the phrase wraps in the file, so compare on collapsed whitespace
    flat = " ".join(text.split())
    assert "does not generalise to an estate" in flat
    assert "does not say worked examples are bad" in flat, (
        "one schema's 22 examples are not a claim about worked examples")
