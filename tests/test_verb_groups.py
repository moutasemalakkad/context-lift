# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Forty-eight verbs, and every one has to say which family it is in.

THE QUESTION THIS ANSWERS. The owner asked twice why there are 48 commands. A
flat braced list of 48 in `--help-all` hides the real answer, which is that
they are eight stages plus a few small families of tools. Grouping them makes
the answer visible without renaming anything.

MEASURED 2026-09-14: 14 of the 48 are mentioned by NO document, test, script or
example -- audit-golds, bisect, catalog-pull, catalog-route, context-write,
curate-examples, import, index, lint, mine-context, profile,
rewrite-questions, routing-gate, schema. The problem was never that they exist.
It was that most of them are invisible.

DELETING WAS REFUSED FOR NOW, and that is a judgement rather than a finding. A
verb removed breaks every script already written against it, and a reference
count cannot tell a tool somebody relies on from one nobody has needed yet.
Two of the fourteen -- `bisect` and `lint` -- were built the same week and are
referenced by their own test modules rather than by a documented command line,
which is exactly the false positive that makes deletion-by-count unsafe.

THIS TEST IS THE BRAKE. A forty-ninth verb fails until somebody says which
family it belongs to, which is the only thing that stops 48 becoming 60.
"""
from __future__ import annotations

from context_lift.cli import (CORE_VERBS, VERB_GROUPS, _build_parser,
                              grouped_verbs)


def _verbs() -> set[str]:
    return set(_build_parser()._subparsers._group_actions[0].choices)


def test_every_verb_is_in_exactly_one_group():
    """The brake. A new verb has to be classified, and a verb in two groups
    means somebody could not decide what it is for."""
    verbs = _verbs()
    placed: dict[str, list[str]] = {}
    for title, group in VERB_GROUPS.items():
        for verb in group:
            placed.setdefault(verb, []).append(title)

    ungrouped = sorted(verbs - set(placed))
    assert not ungrouped, (
        f"these verbs are in no group: {ungrouped}\n"
        f"Add each to VERB_GROUPS in cli.py. A help page that silently omits "
        f"a command is worse than an untidy one.")

    twice = {v: t for v, t in placed.items() if len(t) > 1}
    assert not twice, f"verbs in more than one group: {twice}"


def test_no_group_names_a_verb_that_does_not_exist():
    """A group naming a deleted verb is a help page advertising something
    that is gone."""
    verbs = _verbs()
    ghosts = {title: [v for v in group if v not in verbs]
              for title, group in VERB_GROUPS.items()}
    ghosts = {t: g for t, g in ghosts.items() if g}
    assert not ghosts, f"groups name verbs that do not exist: {ghosts}"


def test_the_first_group_is_the_eight_that_complete_a_measurement():
    """The default help page shows exactly these. If the grouped list
    disagreed with it, two help pages would teach two different products."""
    first = list(VERB_GROUPS)[0]
    assert VERB_GROUPS[first] == CORE_VERBS
    assert len(CORE_VERBS) == 8


def test_the_grouped_list_prints_nothing_under_not_yet_grouped():
    """The escape hatch exists so a help page never drops a verb. It should
    always be empty; if it is not, the test above already failed."""
    printed = grouped_verbs(_verbs())

    assert "not yet grouped" not in printed
    for verb in _verbs():
        assert verb in printed, f"{verb} is missing from the grouped help"


def test_the_group_titles_say_what_the_family_is_for():
    """A title like "misc" or "other" defeats the point: the reader still
    cannot tell why the verb exists."""
    for title in VERB_GROUPS:
        assert len(title.split()) >= 3, f"{title!r} is too short to explain anything"
        for vague in ("misc", "other", "advanced", "extra", "utilities"):
            assert vague not in title.lower(), (
                f"{title!r} is a bucket, not a purpose")


# ---------------------------------------------------------------------------
# the README's own count of them

def test_the_readme_verb_count_is_recomputed_not_typed():
    """The README said 47 while the parser built 48.

    Nobody mistyped it: a verb was added and the prose was not. That is the
    shipped-number rule -- a figure in a committed document has to be
    computed by something that runs, or it drifts the first time the code
    moves. This is the cheapest possible form of that check.
    """
    import pathlib
    import re

    readme = (pathlib.Path(__file__).resolve().parent.parent
              / "README.md").read_text()
    found = re.search(r"the package: (\d+) commands", readme)
    assert found, "the README no longer states a command count in that shape"

    parser = _build_parser()
    sub = next(a for a in parser._actions if hasattr(a, "choices") and a.choices)

    assert int(found.group(1)) == len(sub.choices), (
        f"README.md says {found.group(1)} commands, the parser builds "
        f"{len(sub.choices)}")
