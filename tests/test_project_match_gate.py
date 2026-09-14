# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The coverage gate was project-blind, and it certified 100%.

`_arm_table_key` returns `".".join(parts[-2:])`, which drops the project. Its
docstring explains why, and the reason is good: the arm names tables as
`project.dataset.table` while a gold's SQL often names them `dataset.table`,
so comparing the full names directly would fail on every ordinary round.

THE COST OF THAT CHOICE, measured. The gate printed "43 of 43 golds have every
table they need (100%)" while one gold's answer key read project `-p` and the
arm carried only `-pp`. That gold was one of three published breaks. Its
control arm scored MATCH, which meant only that two physical copies of the
data agreed at score time -- not that the agent had been handed the table the
answer key reads.

THE FIX IS A SECOND GATE, NOT A CHANGED KEY. Putting the project back into the
key would refuse the common case and the gate would stop being usable.
`project_mismatches` fires only where BOTH sides name a project and they
differ, which is exactly the situation nobody could see before.
"""
from __future__ import annotations

import pathlib

import pytest

from context_lift.round import Gold, _arm_table_key, project_mismatches
from context_lift.runners import Arm

# THE SUFFIX SHAPE IS THE POINT, and the names are invented. A real round
# compared a project ending `-pp` against one ending `-p`, which is why
# nobody spotted it by eye: the two differ by one character at the end of a
# long name. Generic names keep that property without putting a customer's
# project into a repository that may go public -- `make scan` refused the
# first version of this file for exactly that.
PROD = "acme-analytics-pp"
OTHER = "acme-analytics-p"


def an_arm(*tables: str) -> Arm:
    return Arm(label="1 authored", tables=list(tables))


def a_gold(sql: str, ident: str = "m711") -> Gold:
    return Gold(id=ident, question="how many?", gold_sql=sql)


# ---------------------------------------------------------------------------
# THE ACCEPTANCE CRITERIA

def test_a_gold_naming_a_project_the_arm_lacks_is_named():
    """The real case, from a real round: the key read `-p` and the arm
    carried `-pp`."""
    arm = an_arm(f"{PROD}.sales.orders")
    gold = a_gold(f"SELECT COUNT(*) FROM `{OTHER}.sales.orders`")

    found = project_mismatches([gold], arm)

    assert list(found) == ["m711"]
    named, carried = found["m711"]
    assert named == OTHER
    assert carried == PROD


def test_the_same_project_on_both_sides_passes():
    arm = an_arm(f"{PROD}.sales.orders")
    gold = a_gold(f"SELECT COUNT(*) FROM `{PROD}.sales.orders`")

    assert project_mismatches([gold], arm) == {}


def test_a_gold_that_names_no_project_is_not_a_mismatch():
    """Nothing else in the gate changes. A gold written `dataset.table` has
    said nothing about which project, so there is nothing to disagree with,
    and refusing it would refuse the ordinary round."""
    arm = an_arm(f"{PROD}.sales.orders")
    gold = a_gold("SELECT COUNT(*) FROM `sales.orders`")

    assert project_mismatches([gold], arm) == {}


def test_an_arm_that_names_no_project_cannot_judge():
    """The other direction of the same rule. An arm carrying `sales.orders`
    has not claimed a project either, so it cannot contradict one."""
    arm = an_arm("sales.orders")
    gold = a_gold(f"SELECT COUNT(*) FROM `{OTHER}.sales.orders`")

    assert project_mismatches([gold], arm) == {}


def test_the_two_copies_case():
    """THE CASE THE ACCEPTANCE CRITERIA NAMES, and the reason the old gate's
    pass was worthless.

    Two projects hold the same dataset and table. Both queries run and both
    return the same rows, so scoring says MATCH. But the arm handed the agent
    one copy and the answer key reads the other, so the round is not measuring
    what it claims: the agent was never given the table the key uses. The old
    gate saw two identical `dataset.table` keys and passed.
    """
    arm = an_arm(f"{PROD}.sales.orders")
    gold = a_gold(f"SELECT COUNT(*) FROM `{OTHER}.sales.orders`", "m323")

    # the project-blind key cannot tell them apart, which is the whole point
    assert (_arm_table_key(f"{PROD}.sales.orders")
            == _arm_table_key(f"{OTHER}.sales.orders") == "sales.orders")

    # and the new check can
    assert "m323" in project_mismatches([gold], arm)


def test_both_real_golds_from_that_round_are_caught():
    """m711 and m323 are the two the brief names. Kept as one test so the
    pair travels together: they were found together and they were published
    together."""
    arm = an_arm(f"{PROD}.sales.orders", f"{PROD}.sales.items")
    golds = [a_gold(f"SELECT 1 FROM `{OTHER}.sales.orders`", "m711"),
             a_gold(f"SELECT 1 FROM `{OTHER}.sales.items`", "m323")]

    assert set(project_mismatches(golds, arm)) == {"m711", "m323"}


# ---------------------------------------------------------------------------
# the gate as a round prints it

def test_the_gate_appears_in_a_round_and_says_which_gold():
    """A gate that fails without naming the gold sends a person into the
    code. This one names up to three and counts the rest."""
    from context_lift.round import free_gates

    arm = an_arm(f"{PROD}.sales.orders")
    golds = [a_gold(f"SELECT 1 FROM `{OTHER}.sales.orders`", "m711")]

    gates = free_gates(golds, arm)
    named = {g.name for g in gates}
    assert "project match" in named, sorted(named)

    gate = [g for g in gates if g.name == "project match"][0]
    assert not gate.passed
    assert "m711" in gate.detail
    assert OTHER in gate.detail and PROD in gate.detail


def test_a_clean_round_says_so_rather_than_staying_silent():
    """A gate nobody sees when it passes teaches a reader to read its
    absence as good news."""
    from context_lift.round import free_gates

    arm = an_arm(f"{PROD}.sales.orders")
    golds = [a_gold(f"SELECT 1 FROM `{PROD}.sales.orders`")]

    gate = [g for g in free_gates(golds, arm)
            if g.name == "project match"][0]

    assert gate.passed
    assert "every gold that names a project names one the arm carries" in gate.detail
