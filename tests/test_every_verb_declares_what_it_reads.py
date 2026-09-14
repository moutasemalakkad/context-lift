# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""A verb that reads an argument must declare it.

THE DEFECT, found 2026-09-14 by an outside review reading the code. `bisect`
called `_executor_for(args)`, which reads `args.db`, and its parser declared no
`--db`. So the verb printed its gate results, printed a cost sheet, took the
typed confirmation, and THEN raised

    AttributeError: 'Namespace' object has no attribute 'db'

after the money was approved. It had never run. Every test of it used
`--estimate-only`, which returns before the executor is built, so the gap was
invisible to 1,788 passing tests.

WHAT THIS CHECKS, and why it is a parser check rather than a run. Actually
invoking 48 verbs needs 48 warehouses, credentials and answer keys. What is
checkable for free is the CONTRACT: a handler that reads `args.<name>` must
have a parser that declares `--<name>`. That catches this whole class of
defect, which is a handler and a parser drifting apart.
"""
from __future__ import annotations

import inspect
import re

import pytest

from context_lift import cli

# Attributes every parser gets from the top level or from argparse itself, so
# a handler may read them without its own subparser declaring anything.
GLOBAL = {"workspace", "func", "help_all"}

# What each helper reads off the namespace UNCONDITIONALLY. Only these can
# raise AttributeError, so only these are required of a caller.
#
# THE FIRST VERSION OF THIS LIST WAS WRONG and flagged four innocent verbs.
# It claimed `_executor_for` needs db, timeout and row_cap. It does not:
# `row_cap` is a PARAMETER of the function, and `timeout` is read with
# `getattr(args, "timeout", None)`. Only `args.db` is read directly. Checking
# that before reporting saved four false accusations -- which is the same
# discipline the review that found the real bug was held to.
HELPER_READS = {
    "_executor_for": {"db"},
    "_spec_for": {"spec"},
}


def _verbs():
    parser = cli._build_parser()
    sub = next(a for a in parser._actions if hasattr(a, "choices") and a.choices)
    return sub.choices


def _declared(command) -> set[str]:
    """Every dest a subparser declares, plus the globals."""
    return {a.dest for a in command._actions} | GLOBAL


@pytest.mark.parametrize("name", sorted(_verbs()))
def test_the_helpers_a_verb_calls_have_the_flags_they_read(name):
    """The check that would have caught `bisect --db`.

    A handler calling `_executor_for(args)` is promising its parser declares
    --db, --timeout and --row-cap. Nothing enforced that promise.
    """
    command = _verbs()[name]
    handler = command.get_default("func")
    if handler is None:
        pytest.skip(f"{name} has no handler")
    try:
        body = inspect.getsource(handler)
    except (OSError, TypeError):
        pytest.skip(f"cannot read the source of {name}")

    declared = _declared(command)
    missing = {}
    for helper, reads in HELPER_READS.items():
        if re.search(rf"\b{helper}\(args", body):
            gaps = sorted(reads - declared)
            if gaps:
                missing[helper] = gaps
    assert not missing, (
        f"`{name}` calls {missing} but its parser declares none of those "
        f"flags. The handler will raise AttributeError at run time -- and for "
        f"a paid verb, AFTER the money is approved.")


def test_bisect_specifically_declares_a_database():
    """Pinned by name because this is the verb that had the defect, and a
    parametrised test can pass by the verb disappearing."""
    assert "bisect" in _verbs()
    assert "db" in _declared(_verbs()["bisect"])


def test_every_verb_that_takes_a_row_cap_bounds_it_by_default():
    """Overlaps a check in test_cli.py on purpose. That one was written after
    two multi-hour hangs; this file repeats it because the bisect fix
    introduced exactly that mistake again and was caught by it."""
    for name, command in _verbs().items():
        for action in command._actions:
            if "--row-cap" in (action.option_strings or []):
                assert action.default is not None, (
                    f"{name} executes SQL with no row cap by default")
                assert action.default > 0, f"{name}'s row cap is {action.default}"
