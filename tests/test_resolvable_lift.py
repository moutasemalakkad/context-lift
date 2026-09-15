# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Pins the numbers `scripts/resolvable_lift.py` puts in the status page.

These are shipped figures, so a script has to compute them and a test has
to fail when they drift.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from resolvable_lift import (floor_on_disagreements, power,  # noqa: E402
                             smallest_resolvable, two_sided_p)


def test_six_disagreements_is_the_cheapest_significant_result():
    """Five cannot reach 0.05 even unanimous; six can. Nothing gets under."""
    assert floor_on_disagreements() == 6
    assert two_sided_p(5, 0) > 0.05
    assert two_sided_p(6, 0) <= 0.05


def test_the_acme_round_could_not_have_reached_a_verdict():
    """1 fixed and 3 broke is four disagreements, and four is not enough."""
    assert two_sided_p(1, 3) == 0.625
    # Every arrangement of four disagreements, not just the one observed.
    assert all(two_sided_p(k, 4 - k) > 0.05 for k in range(5))


def test_twenty_nine_questions_cannot_see_the_effect_we_already_measured():
    """The worked-example rung on BEAVER DW was +6.9 points.

    `docs/research/README.md`: 13.3% to 20.2% on a 596-question holdout,
    p < 0.0001. On 29 reserved questions the same effect has almost no
    chance of reading as significant, which is why the Acme round's
    NOT PROVEN carries no information about the context.
    """
    assert power(29, 0.069 + 0.02, 0.02) < 0.05
    # Not even the whole ladder, 3.8% to 20.2%, clears 80% power at n=29.
    assert power(29, 0.164 + 0.02, 0.02) < 0.40


def test_the_rule_of_thumb_in_classes_py_is_optimistic():
    """`100/sqrt(n)` says 18.6 points at n=29; the exact answer is larger.

    Worth pinning because the heuristic is what the tool prints today, and
    a number that flatters a small set is the one failure this tool exists
    to prevent.
    """
    exact = smallest_resolvable(29)
    assert exact is not None
    assert exact > 100 / 29 ** 0.5
    assert exact == 30.0


def test_a_set_big_enough_reads_the_effect():
    """The 596-question BEAVER holdout resolves a 4-point lift. It did."""
    assert smallest_resolvable(596) == 4.0


# --------------------------------------------------------------------------
# The gate that uses this maths.
# --------------------------------------------------------------------------

from context_lift.gates import resolvable_lift  # noqa: E402


def test_the_real_reserved_half_is_refused():
    """29 reserved questions cannot reach a verdict, so the round is refused.

    The gate that was missing. Five paid rounds on one estate reported
    NOT PROVEN and none of them could have reported anything else.
    """
    gate = resolvable_lift(29)
    assert not gate.passed
    assert "3.2 questions expected to move" in gate.detail
    assert "property of the SET" in gate.detail


def test_a_set_that_can_see_the_target_passes():
    for n in (60, 104, 596):
        assert resolvable_lift(n).passed, n


def test_a_bigger_declared_target_is_honoured_not_ignored():
    """Declaring the effect you really expect is a commitment, not a bypass.

    thelook measured +50 points, so a 29-question set genuinely can see it.
    """
    assert resolvable_lift(29, target_points=50).passed
    assert not resolvable_lift(29, target_points=6.9).passed


def test_force_passes_but_says_so_on_the_page():
    """The `--force` shape the holdout-reads gate already uses."""
    gate = resolvable_lift(29, forced=True)
    assert gate.passed
    assert "FORCED" in gate.detail
    assert "property of the set rather than of the context" in gate.detail


def test_no_reserved_questions_is_not_a_failure():
    assert resolvable_lift(0).passed


def test_a_hundred_point_target_does_not_raise():
    """A synthetic test simulates a total effect; the rates must stay a
    probability. `log1p(-1.0)` is undefined and this is where it surfaced."""
    assert resolvable_lift(18, target_points=100).passed
