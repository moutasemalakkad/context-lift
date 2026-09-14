# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The planning board, checked against the code.

The planning board drifted for a week because nothing compared it to the
code. Its rename item still said `TRUENESS_*` after the 2026-09-13 rename deleted those
variables, so work that had shipped sat open and the owner's reaction was, in
their words, "why it hasnt been ticked? that s reakky frustrating".

That is the same failure as three broken `make example` targets, a mermaid
diagram that never rendered, and `--db sqlite://` failing on the exact form
the demo prints: a claim nobody executed.

THE DIRECTION OF EACH CHECK IS THE WHOLE DESIGN, and the obvious version was
wrong. "An open item must name a command that exists" flags every PROPOSAL on
the board: M16's item exists in order to build `contextlift ask`, so of course
that verb is missing. Written, run once, deleted.

What survives points the other way. An OPEN item must name a real PATH,
because a path in open work is a place to go. A DONE item must name a real
VERB, because a tick claiming shipped work is false if the command is not
there. And no open item may name the pre-rename project, because that is either
stale text or shipped-and-unticked, and both need a person to look.

The board lives outside this repository, so this test needs
`CONTEXTLIFT_CHECKLIST` pointing at it and skips loudly otherwise, following
the pattern in tests/test_case_study_numbers.py. A skip that does not say
what is missing is the failure mode this project keeps finding.
"""
from __future__ import annotations

import os
import pathlib
import re

import pytest

from context_lift.cli import _build_parser

CHECKLIST = os.environ.get("CONTEXTLIFT_CHECKLIST", "")

# `contextlift verb` or the bare `verb` inside a command line
_VERB = re.compile(r"`contextlift ([a-z][a-z-]*)")
# a long flag, and the verb it was written next to
_FLAGGED = re.compile(r"`contextlift ([a-z][a-z-]*)([^`]*)`")
_FLAG = re.compile(r"(--[a-z][a-z-]+)")
# a repo-relative path in backticks
_PATH = re.compile(r"`((?:src|tests|scripts|examples|docs)/[\w./-]+)`")
# Names the 2026-09-13 rename deleted. `(?<!/)` exempts a repository path such as
# `moutasemalakkad/trueness`, which is a proper noun that still exists as a
# redirect, not a command anybody can run. Caught on this test's first run.
_DEAD = re.compile(r"TRUENESS_[A-Z_]*|(?<![\w/])trueness\b")

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _open_items() -> list[tuple[int, str]]:
    text = pathlib.Path(CHECKLIST).read_text()
    return [(n, line) for n, line in enumerate(text.splitlines(), 1)
            if line.startswith("- [ ] ")]


pytestmark = pytest.mark.skipif(
    not CHECKLIST or not pathlib.Path(CHECKLIST).is_file(),
    reason=("needs CONTEXTLIFT_CHECKLIST pointing at the planning board "
            "(kc_harness/docs/checklist.md), which lives outside this repo"))


def _verbs() -> set[str]:
    return set(_build_parser()._subparsers._group_actions[0].choices)


def test_the_board_parses_into_items_at_all():
    """The guard on every check below: if the parser finds nothing, they all
    pass on an empty list and the board is unchecked.

    THIS USED TO ASSERT THERE WERE OPEN ITEMS, and on 2026-09-14 the board
    reached zero open. That version could not tell "the regex broke" from
    "the work is finished" -- it would have failed on the good outcome and
    been deleted, taking the real guard with it.

    So the guard is now on the TOTAL. A board that parses to zero items of
    either kind is a broken parser. A board that parses to many done items
    and no open ones is a finished board, and that is allowed to be true.
    """
    total = len(_open_items()) + len(_done_items())
    assert total > 20, (
        f"only {total} checklist items parsed out of {CHECKLIST}. Every check "
        f"below reads the same list, so they are all passing vacuously.")


def _done_items() -> list[tuple[int, str]]:
    text = pathlib.Path(CHECKLIST).read_text()
    return [(n, line) for n, line in enumerate(text.splitlines(), 1)
            if line.lower().startswith("- [x] ")]


def test_every_verb_a_DONE_item_names_exists():
    """The check that has no ambiguity, and it is the inverse of the one you
    would reach for first.

    An OPEN item naming `contextlift ask` is not a bug: M16 is a proposal to
    BUILD that verb, and an instruction to build something necessarily names
    something that does not exist. Checking open items for verb existence
    flags every proposal on the board, which is why that check was written
    and then deleted on its first run.

    A DONE item is different. If it says the work shipped and names a verb,
    the verb has to be there, or the tick is false.
    """
    verbs = _verbs()
    bad = []
    for number, line in _done_items():
        for verb in _VERB.findall(line):
            if verb not in verbs:
                bad.append(f"{number}: ticked, but `contextlift {verb}` "
                           f"does not exist")
    assert not bad, (
        "items marked done name commands that are not in the CLI:\n  "
        + "\n  ".join(bad)
        + "\n\nA false tick is worse than an open item: it stops anyone "
          "looking.")


def test_every_path_an_open_item_names_is_on_disk():
    bad = []
    for number, line in _open_items():
        for path in _PATH.findall(line):
            if not (ROOT / path).exists():
                bad.append(f"{number}: {path}")
    assert not bad, (
        "open items name files that do not exist:\n  " + "\n  ".join(bad)
        + "\n\nA path in an open item is a place to go and work. If it is not "
          "there, say where it went.")


def test_no_open_item_still_names_the_deleted_project_name():
    """The specific drift that caused this file. The 2026-09-13 rename moved the package to
    `context_lift`, the command to `contextlift` and 11 `TRUENESS_*`
    variables to `CONTEXTLIFT_*`. An OPEN item naming the old ones is either
    stale or shipped-and-unticked, and both need a person to look."""
    bad = [f"{n}: {line.strip()[:110]}"
           for n, line in _open_items() if _DEAD.search(line)]
    assert not bad, (
        "open items still name the pre-rename project:\n  " + "\n  ".join(bad)
        + "\n\nIf the work shipped, tick it. If the name is history, the item "
          "is done and should not be open.")


# A command that no longer exists, anywhere on the board. Narrower than
# `_DEAD` on purpose: the board may say "rename the trueness repository",
# because that is history and the repository really was called that. What it
# may not do is tell a reader to RUN something.
_DEAD_COMMAND = re.compile(r"`trueness[ `]|uv run trueness |"
                           r"src/trueness|~/\.local/bin/trueness|"
                           r"\.claude/skills/trueness|\.gemini/extensions/trueness")


def test_no_line_anywhere_names_a_command_the_rename_deleted():
    """The gap that let M16's heading keep `trueness ask` for a week.

    The check above reads only lines starting `- [ ] `, so a HEADING escaped
    it, and so did every closed item. Closed items matter here: one of them
    told a reader `trueness demo` runs in 0.14 seconds, and that command has
    not existed since the 2026-09-13 rename. A done item is the thing
    people copy from.

    Deliberately not the same pattern as `_DEAD`. Prose about the old
    repository is history and may stay; an instruction to run a command that
    is gone may not.
    """
    text = pathlib.Path(CHECKLIST).read_text()
    bad = [f"{n}: {line.strip()[:110]}"
           for n, line in enumerate(text.splitlines(), 1)
           if _DEAD_COMMAND.search(line)]
    assert not bad, (
        "the board names commands or paths the rename deleted:\n  "
        + "\n  ".join(bad)
        + "\n\nThese are what a reader copies. Rename them to `contextlift "
          "...` and `src/context_lift/`; keep prose about the old repository, "
          "which is history.")


def test_that_check_would_actually_catch_something():
    """A pattern that matches nothing passes forever. These are the exact
    strings found on the board on 2026-09-13, so the check is pinned to real
    text rather than to a regex nobody has run against a positive case."""
    for caught in ("`trueness ask`", "uv run trueness demo",
                   "src/trueness/drift.py", "~/.local/bin/trueness"):
        assert _DEAD_COMMAND.search(caught), f"would not catch {caught!r}"
    for kept in ("rename the trueness repository to kc_harness",
                 "/tmp/trueness-backup.git"):
        assert not _DEAD_COMMAND.search(kept), f"wrongly catches {kept!r}"
