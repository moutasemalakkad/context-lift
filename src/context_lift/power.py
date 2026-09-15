# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Whether a gold set can resolve anything, before a round is paid for.

The gap this fills. A round reports NOT PROVEN honestly and never says
whether it COULD have reported anything else. Five paid rounds on one
enterprise estate were unresolvable by construction, and the tool's only
comment on set size was a rule of thumb at `classes.py:46` -- a set resolves
a lift above about `100/sqrt(n)` points -- applied to item-level
leave-one-out and never to a round. At n = 29 that rule says 18.6 points and
the exact answer is 30, so it flatters a small set by about 1.6x.

The hard floor, which needs no assumptions at all. A round is scored with the
exact McNemar test: of the questions that DISAGREE between the two arms, how
lopsided is the split. A question right in both arms carries no information
and neither does one wrong in both. With `d` disagreements all falling one
way the two-sided p is `2 * (1/2)^d`:

    d = 4  ->  p = 0.1250   cannot reach 0.05, whatever happens
    d = 5  ->  p = 0.0625   cannot reach 0.05, whatever happens
    d = 6  ->  p = 0.0312   reaches 0.05, but only if ALL SIX agree

So six unanimous disagreements is the cheapest significant result that
exists, and a design whose EXPECTED disagreement count is under six is
hopeless however the context is written. That is what `gates.resolvable_lift`
refuses on, because it rests on arithmetic rather than on an assumed effect
size.

`scripts/resolvable_lift.py` is the human-drivable view of the same maths and
imports from here, so the gate and the report cannot drift.
"""
from __future__ import annotations

import math
from functools import lru_cache
from itertools import product


def _log_choose(n: int, k: int) -> float:
    return (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1))


@lru_cache(maxsize=None)
def two_sided_p(fixed: int, broke: int) -> float:
    """The exact McNemar p, the same sign test `ablate` scores a round with.

    Up to 60 disagreements this DELEGATES to `metrics.mcnemar_exact`, which
    is the same test `ablate` scores a round with. It used to carry its own
    copy of that sum; two copies of the arithmetic every verdict rests on is
    the drift this project keeps paying for, and a test now asserts the two
    agree.

    Above 60 it keeps its own log-space sum, and that is a real difference
    rather than a duplicate: `metrics` is exact at any size on purpose (its
    own note records that `0.5 ** n` returned p = 0 past 1,074
    disagreements), but `math.comb` there returns thousand-digit integers and
    summing them is what made the planning table too slow to run. Accuracy
    for the number that gets reported, speed for the table that only needs
    three digits.
    """
    d = fixed + broke
    if d == 0:
        return 1.0
    if d <= 60:
        from .metrics import mcnemar_exact
        return mcnemar_exact(fixed, broke).p_value
    k = min(fixed, broke)
    tail = sum(math.exp(_log_choose(d, i) - d * math.log(2.0))
               for i in range(k + 1))
    return min(1.0, 2 * tail)


def floor_on_disagreements(alpha: float = 0.05) -> int:
    """Fewest disagreements that can reach `alpha` even when unanimous."""
    d = 1
    while two_sided_p(d, 0) > alpha:
        d += 1
    return d


@lru_cache(maxsize=None)
def _reject_threshold(d: int, alpha: float) -> int:
    """Largest minority count that still reaches `alpha` with d disagreements.

    `two_sided_p` rises as the split evens out, so the outcomes that reach
    `alpha` are exactly those whose minority side is at or below one
    threshold. Finding that threshold once beats testing every split, which
    is what made this too slow to use at the sizes worth planning against.
    """
    best = -1
    for k in range(d // 2 + 1):
        if two_sided_p(k, d - k) <= alpha:
            best = k
        else:
            break
    return best


def _binom_tail_low(d: int, k: int, prob: float) -> float:
    """P(X <= k) for X ~ Binomial(d, prob), in log space so it survives d."""
    if k < 0:
        return 0.0
    if k >= d:
        return 1.0
    out = 0.0
    for i in range(k + 1):
        out += math.exp(_log_choose(d, i) + i * math.log(prob)
                        + (d - i) * math.log1p(-prob))
    return out


def _reject_given_d(d: int, share_fixed: float, alpha: float) -> float:
    """Chance a round with exactly d disagreements reaches `alpha`.

    Both tails of one binomial: the split is lopsided enough toward fixed,
    or lopsided enough toward broke.
    """
    k = _reject_threshold(d, alpha)
    if k < 0:
        return 0.0
    low = _binom_tail_low(d, k, share_fixed)
    high = _binom_tail_low(d, k, 1.0 - share_fixed)
    return min(1.0, low + high)


def power(n: int, fix_rate: float, break_rate: float,
          alpha: float = 0.05) -> float:
    """Chance a round of n questions reaches `alpha`, computed exactly.

    Split in two so it stays fast at n = 596: first how many questions
    disagree between the arms, then how lopsided that split is. Both are
    binomial, and neither needs a simulation or a seed.
    """
    disagree = fix_rate + break_rate
    if disagree <= 0 or disagree > 1:
        raise ValueError("fix_rate + break_rate must be in (0, 1]")
    share_fixed = fix_rate / disagree
    if disagree >= 1.0:
        # Every question moves, so the disagreement count is n with certainty
        # and there is no outer sum to take. Reached by a synthetic test that
        # simulates a near-100-point effect; `log1p(-1.0)` is undefined.
        return _reject_given_d(n, share_fixed, alpha)
    out = 0.0
    for d in range(n + 1):
        log_p = _log_choose(n, d) + d * math.log(disagree)
        if d < n:
            log_p += (n - d) * math.log1p(-disagree)
        if log_p < -60:            # this many disagreements is impossible
            continue
        out += math.exp(log_p) * _reject_given_d(d, share_fixed, alpha)
    return out


def smallest_resolvable(n: int, break_rate: float = 0.02,
                        alpha: float = 0.05, want: float = 0.80) -> float | None:
    """Smallest lift, in points, this n reaches `want` power on.

    `break_rate` is the share of questions the context damages. 2% is the
    rate this project measured on the BEAVER holdout (19 broken of 596).
    """
    for points in range(1, 101):
        fix_rate = points / 100 + break_rate
        if fix_rate + break_rate > 1:
            return None
        if power(n, fix_rate, break_rate, alpha) >= want:
            return float(points)
    return None


def questions_needed(points: float, break_rate: float = 0.02,
                     alpha: float = 0.05, want: float = 0.80,
                     cap: int = 1500) -> int | None:
    """Fewest questions that reach `want` power on a lift of `points`."""
    lo, hi = 1, cap
    if power(cap, points / 100 + break_rate, break_rate, alpha) < want:
        return None
    while lo < hi:                      # the power curve is monotone in n
        mid = (lo + hi) // 2
        if power(mid, points / 100 + break_rate, break_rate, alpha) >= want:
            hi = mid
        else:
            lo = mid + 1
    return lo


def rates_for(points: float, break_rate: float = 0.02) -> tuple[float, float]:
    """(fix rate, break rate) for a target lift, clamped to be a probability.

    A synthetic test can simulate a 100-point effect, and `points/100 +
    break_rate` would then exceed 1. Clamping keeps the caller from having to
    know that, and the clamp only ever bites above 98 points, where the
    answer is "every question moves" either way.
    """
    break_rate = max(0.0, min(break_rate, 0.5))
    fix_rate = max(0.0, min(points / 100 + break_rate, 1.0 - break_rate))
    return fix_rate, break_rate


def expected_disagreements(n: int, points: float,
                           break_rate: float = 0.02) -> float:
    """How many questions are expected to move at all.

    The number that actually decides everything. Six unanimous
    disagreements is the cheapest significant result there is, so a design
    whose EXPECTED disagreement count is under six is hopeless however the
    context is written.
    """
    return n * (points / 100 + 2 * break_rate)


# The effect a round is assumed to be paying to see, unless it says otherwise.
# From `docs/research/README.md`: the leak-checked worked-example bank moved an
# enterprise warehouse 13.3% to 20.2% on a 596-question reserved half. It is
# the largest DEPLOYABLE written-context effect this framework has measured --
# the oracle-hint rung above it is per-question and undeployable by design --
# so a set that cannot see it cannot see anything we know works.
DEFAULT_TARGET_POINTS = 6.9

# 19 broken of 596 on that same reserved half.
DEFAULT_BREAK_RATE = 0.02
