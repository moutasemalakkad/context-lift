# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The one command a stranger runs before deciding whether to try this."""
from context_lift.demo import QUESTIONS, WITHOUT_RULE, run


def lines():
    out = []
    assert run(out.append) == 0
    return "\n".join(out)


def test_the_demo_needs_no_key_no_cloud_and_no_network():
    """It builds its own warehouse in a temporary directory. A demo that
    needed an API key would not run for the person deciding whether to try
    this at all, which is the only moment it exists for."""
    text = lines()

    assert "without the rule 3 of 6 right" in text
    assert "with the rule    6 of 6 right" in text


def test_the_demo_says_the_answers_are_canned():
    """A scripted model proves nothing about a model, and a demo that hides
    that is dishonest about the one thing it is not showing. What it does
    show is real: the warehouse, the SQL execution, the paired test."""
    text = lines()

    assert "CANNED" in text
    assert "the real code" in text


def test_the_demo_refuses_to_oversell_six_questions():
    """The refusal is the product. A demo that printed +50 points and stopped
    would be teaching the exact habit this tool exists to break."""
    assert "no interval is narrow enough to prove much" in lines()


def test_half_the_questions_do_not_need_the_rule():
    """A rule that helps every question is usually a rule that was already in
    the schema. Three questions moving and three not is the honest shape."""
    assert len(WITHOUT_RULE) == 3
    assert len(QUESTIONS) == 6


def test_the_demo_ends_with_the_next_command_for_your_own_data():
    """The moment after a demo works is the only moment a person will act."""
    assert "contextlift round --db sqlite:///YOUR.db" in lines()


def test_the_demo_shows_both_numbers_because_they_answer_different_questions():
    """The lift is a guess about unseen questions; the safety count is not.

    A stranger's first thirty seconds with this tool is the cheapest place to
    teach that distinction, and the demo used to print the counts without the
    word so there was nothing to notice.
    """
    text = lines()
    assert "safety SAFE: 3 fixed, 0 broke, of 6" in text
    assert "The safety count is not a guess" in text


def test_the_demo_never_colours_output_that_is_read_as_data():
    """Colour in the demo exists for the recorded terminal GIF in the README.
    A pipe, a file, a CI log and a caller that passes its own `out` must get
    the same plain text they got before colour existed, or every downstream
    string comparison breaks on an invisible escape."""
    lines: list[str] = []
    assert run(lines.append) == 0
    offenders = [line for line in lines if "\033" in line]
    assert not offenders, f"escape codes leaked into collected output: {offenders}"
