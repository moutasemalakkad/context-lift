# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The help page is the first thing a new person reads, and it listed 48 verbs.

Before this, `contextlift --help` opened with every verb in one braced list that
wrapped over two lines before the description started. Forty-eight equally
weighted choices is not a menu, it is a wall.

The rule these tests hold: hiding a verb from the LISTING must never make it
unavailable. Nothing is renamed and nothing is removed.
"""
from __future__ import annotations

import argparse
import io
import contextlib

import pytest

from context_lift.cli import CORE_VERBS, _build_parser


def help_text(show_all: bool = False) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        _build_parser(show_all=show_all).print_help()
    return buffer.getvalue()


def listed(text: str) -> list[str]:
    """The verbs argparse actually printed as choices, in printed order."""
    out = []
    inside = False
    for line in text.split("\n"):
        if line.startswith("positional arguments:"):
            inside = True
            continue
        if inside:
            if line.startswith("options:"):
                break
            stripped = line.strip()
            # a choice line starts at four spaces; its wrapped continuation
            # is indented further
            if line.startswith("    ") and not line.startswith("     ") \
                    and stripped and " " in stripped:
                out.append(stripped.split()[0])
            elif line.startswith("    ") and not line.startswith("     ") \
                    and stripped:
                out.append(stripped)
    return out


def test_the_default_help_lists_only_the_eight():
    assert set(listed(help_text())) == set(CORE_VERBS)


def test_the_eight_are_listed_in_the_order_they_are_run():
    """Registration order put `bundle` (read the result) above `round`
    (produce the result), which teaches the loop backwards."""
    assert listed(help_text()) == list(CORE_VERBS)


def test_the_literal_string_suppress_never_reaches_the_page():
    """The bug this pins: argparse honours SUPPRESS on a top-level action but
    NOT on a subcommand choice -- `add_parser` does `if 'help' in kwargs` and
    builds the pseudo-action either way. Setting help=SUPPRESS printed
    `==SUPPRESS==` beside forty verbs. The key has to be removed."""
    assert "SUPPRESS" not in help_text()
    assert "SUPPRESS" not in help_text(show_all=True)


def test_the_usage_line_is_no_longer_a_wall_of_choices():
    usage = help_text().split("\n")[0]
    assert "COMMAND" in usage
    assert "demo," not in usage      # the old braced list
    assert len(usage) < 80


def test_help_all_lists_every_verb():
    everything = listed(help_text(show_all=True))
    assert len(everything) > 40
    assert set(CORE_VERBS) <= set(everything)


def test_a_hidden_verb_is_still_callable_with_its_own_help():
    """The whole contract. `classes` is not on the default page and must still
    parse, dispatch and document itself."""
    parser = _build_parser()
    hidden = [v for v in parser._subparsers._group_actions[0].choices
              if v not in CORE_VERBS]
    assert len(hidden) > 35
    for verb in ("classes", "attribute", "history", "mine", "semantic"):
        assert verb in hidden
        sub = parser._subparsers._group_actions[0].choices[verb]
        assert sub.format_help()          # it documents itself
        assert sub.get_default("func") is not None   # and it dispatches


def test_the_epilog_names_the_after_the_round_verbs():
    """A verb nobody can find is a verb nobody uses. The three that come after
    a result are named in prose even though they are off the listing."""
    text = help_text()
    for verb in ("classes", "attribute", "history"):
        assert verb in text
    assert "--help-all" in text


@pytest.mark.parametrize("verb", CORE_VERBS)
def test_every_core_verb_exists(verb):
    """Guards a typo in CORE_VERBS: a misspelled name would silently hide the
    real verb and list nothing in its place."""
    assert verb in _build_parser()._subparsers._group_actions[0].choices
