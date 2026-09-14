# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""An arm that returns the answer key is not a measurement.

THE HOLE, found on 2026-09-14 by reading a pull request rather than by
anything failing. Every other guard in gates.py looks at the CONTEXT:
`pasted_sql` reads the rules text, `template_twins` reads the worked
examples, and the leak gate reads the bundle handed to the agent. Nothing
read what the agent RETURNED.

So a proposed example shipped an "agent" whose entire logic was

    sql = gold if marker in context else baseline

-- return the gold SQL when the context is present, a deliberately wrong
template when it is not. It scored 100%, produced a +75 point lift at
p = 1.49e-08, and passed every gate including the leak gate. The leak gate was
right: there was no leak in the context. The leak was in the agent.

WHY 100% AND NOT A LOWER THRESHOLD. A legitimate agent sometimes writes the
gold statement: "how many teams are there" has one obvious form, and a gold
written by a person will coincide with SQL written by a model. A zero-tolerance
check would fire on honest rounds. 100% needs no measurement to justify -- an
arm reproducing the key on every question is definitionally a copy of the key,
not an agent answering questions.
"""
from __future__ import annotations

from context_lift.gates import (_same_statement,
                                predictions_are_not_the_answer_key)

GOLD = "SELECT COUNT(*) FROM teams"
OTHER = "SELECT COUNT(DISTINCT team_id) FROM teams"


def test_an_arm_that_is_entirely_the_answer_key_is_refused():
    gate = predictions_are_not_the_answer_key([(GOLD, GOLD)] * 12)

    assert not gate.passed
    assert "copy of the answer key" in gate.detail
    assert "true by construction" in gate.detail


def test_a_partial_match_passes_and_reports_the_share():
    """Some coincidence is normal and refusing it would fire on honest
    rounds. The share is the information, so it is printed."""
    gate = predictions_are_not_the_answer_key(
        [(GOLD, GOLD)] * 9 + [(OTHER, GOLD)])

    assert gate.passed
    assert "9 of 10 predictions (90%)" in gate.detail
    assert "worth a look" in gate.detail


def test_an_honest_arm_says_zero_rather_than_staying_silent():
    """A gate nobody sees when it passes teaches a reader to read its
    absence as good news."""
    gate = predictions_are_not_the_answer_key([(OTHER, GOLD)] * 10)

    assert gate.passed
    assert "0 of 10 predictions (0%)" in gate.detail


def test_a_deliberate_positive_control_can_ask_for_an_exemption():
    """A pipeline self-test with a known answer is a real thing to want.
    It has to be asked for in writing, and the pass says what it means."""
    gate = predictions_are_not_the_answer_key([(GOLD, GOLD)] * 12, allow=True)

    assert gate.passed
    assert "ALLOWED on purpose" in gate.detail
    assert "measures the pipeline and not the agent" in gate.detail


def test_formatting_does_not_hide_a_copy():
    """An agent copying the key would not reproduce its indentation, so a
    match has to survive whitespace, case and a trailing semicolon.

    The first version stripped the semicolon AFTER the final strip, leaving a
    trailing space, so it reported 0% on exactly the case it exists to catch.
    Found by trying it rather than by reading it.
    """
    assert _same_statement("select  count(*)  from teams ;", GOLD)
    assert _same_statement("SELECT COUNT(*)\nFROM teams", GOLD)
    assert _same_statement("  SELECT COUNT(*) FROM TEAMS  ", GOLD)


def test_a_genuinely_different_statement_is_not_a_match():
    assert not _same_statement(OTHER, GOLD)
    assert not _same_statement("SELECT COUNT(*) FROM players", GOLD)


def test_an_empty_prediction_is_not_a_match_and_is_not_counted():
    """A refusal is a wrong answer, not a copy. Counting empties either way
    would move the share for a reason that has nothing to do with the key."""
    assert not _same_statement("", GOLD)

    gate = predictions_are_not_the_answer_key([("", GOLD)] * 5)
    assert gate.passed
    assert "nothing to compare" in gate.detail
