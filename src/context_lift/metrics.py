# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Statistics for reporting eval results: proportions with confidence
intervals, and a paired significance test for comparing two systems.

Why this module exists: an accuracy like "19/35 = 54%" is a point estimate
from a noisy process. Reporting it bare invites over-reading a number that a
35-question sample can barely pin down. Every rate here ships with a Wilson
score interval, and two systems are compared with McNemar's exact test rather
than by eyeballing which number is bigger.

Why hand-rolled, not statsmodels/scipy:
- statsmodels.proportion_confint(method="wilson") and
  contingency_tables.mcnemar(exact=True) give the identical numbers, and for
  a one-off analysis script they'd be the right call.
- But this is a library others install. statsmodels pulls scipy + numpy +
  pandas to provide ~40 lines of math, which would make `make test` and
  `make example` require the whole scientific stack from a fresh clone (and
  in some locked-down environments that stack isn't even reachable). The
  pure-stdlib core keeps the install story to "Python 3.10+".
- The risk of hand-rolled stats is subtly-wrong math, so it's retired: the
  numbers are pinned to a published reference (Wilson 19/35 -> [0.382,
  0.695]) AND an optional parity test asserts equality with statsmodels when
  it happens to be installed (see tests/test_metrics.py). Trusted numbers,
  zero runtime deps.

The inverse-normal is a stdlib rational approximation; the McNemar p-value is
an exact binomial sum via math.comb. References: Wilson (1927) for the score
interval, McNemar (1947) for the paired test.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Proportion:
    """A success rate k/n with a Wilson score confidence interval.

    point/low/high are in [0, 1]. Carry this instead of a bare float so a
    report can always show the interval next to the rate.
    """
    k: int
    n: int
    point: float
    low: float
    high: float

    @property
    def width(self) -> float:
        """Interval width — a one-number read on how much the sample pins
        the rate down. Wide width means 'too few questions to conclude'."""
        return self.high - self.low


@dataclass(frozen=True)
class McNemarResult:
    """Outcome of McNemar's exact paired test on two systems' per-item
    correctness. `b` = first right & second wrong, `c` = first wrong &
    second right; concordant pairs (both right / both wrong) are ignored by
    design — only disagreements carry signal."""
    b: int
    c: int
    p_value: float


# Acklam's rational approximation to the inverse normal CDF, accurate to
# ~1.15e-9 over the open interval. Turns a confidence level into a z
# multiplier without SciPy. Constants are from the published algorithm.
_A = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
      1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
_B = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
      6.680131188771972e+01, -1.328068155288572e+01]
_C = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
      -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
_D = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
      3.754408661907416e+00]


def inverse_normal_cdf(p: float) -> float:
    """Return z such that the standard normal CDF Phi(z) == p.

    Defined on the open interval 0 < p < 1; the bounds map to +/-inf and are
    rejected so callers don't silently get a garbage z.
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"p must be in (0, 1), got {p}")
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return ((((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5])
                / ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1))
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return ((((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5]) * q
                / (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1))
    q = math.sqrt(-2 * math.log(1 - p))
    return -((((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5])
             / ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1))


def z_for_confidence(confidence: float = 0.95) -> float:
    """Two-sided z multiplier for a confidence level (0.95 -> ~1.95996).

    Two-sided splits the leftover mass across both tails, so it looks up the
    (1 + confidence)/2 quantile, not the `confidence` quantile.
    """
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    return inverse_normal_cdf((1 + confidence) / 2)


def wilson_interval(k: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials.

    Preferred over the normal approximation p +/- z*sqrt(p(1-p)/n): that one
    produces impossible bounds (<0 or >1) near the extremes and is too narrow
    at small n. Wilson inverts the score test instead, so it stays inside
    [0, 1] and behaves at small n. n == 0 returns the maximally-ignorant
    [0, 1] rather than dividing by zero.
    """
    if n == 0:
        return (0.0, 1.0)
    z = z_for_confidence(confidence)
    z2 = z * z
    p = k / n
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / denom
    return (center - margin, center + margin)


def proportion(k: int, n: int, confidence: float = 0.95) -> Proportion:
    """Build a Proportion: the rate k/n with its Wilson interval.

    The single call a report uses to turn raw counts into a rate-with-CI.
    """
    low, high = wilson_interval(k, n, confidence)
    point = k / n if n else 0.0
    return Proportion(k=k, n=n, point=point, low=low, high=high)


def mcnemar_exact(b: int, c: int) -> McNemarResult:
    """McNemar's exact paired test from the two discordant counts.

    Under the null 'on a disagreement, either system is equally likely to be
    the one that's right', each of the b + c discordant items is a fair coin
    flip. The two-sided p-value is the exact binomial tail doubled, clamped to
    1.0. b + c == 0 means the systems never disagreed -> p = 1.0.

    Exact (not the chi-square approximation) because eval disagreement counts
    are usually small, where the approximation is unreliable.
    """
    n = b + c
    if n == 0:
        return McNemarResult(b=b, c=c, p_value=1.0)
    k = min(b, c)
    # integer over integer, not `* 0.5 ** n`: 0.5 ** 1075 is 0.0 in a float,
    # so every test past 1,074 disagreements came back p = 0 (three pooled
    # repetitions of a 596-question holdout get there)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (1 << n)
    return McNemarResult(b=b, c=c, p_value=min(1.0, 2 * tail))


def paired_difference_interval(b: int, c: int, n: int,
                               confidence: float = 0.95) -> tuple[float, float]:
    """Confidence interval for the difference of two paired proportions,
    from the discordant counts alone: `b` first right and second wrong, `c`
    the reverse, `n` pairs in all.

    A lift line without an interval reads as a fact; at 18 questions a +6
    point lift can sit inside [-10, +22]. This is the Wald form on the
    discordant pairs (Agresti, Categorical Data Analysis, section 10.1): the
    difference d = (c - b) / n, standard error sqrt(b + c - (c - b)^2 / n) / n,
    bounds d +/- z * se clipped to [-1, 1]. Chosen over Newcombe's score
    interval because it is a closed form in the stdlib; it is narrow when b + c
    is very small, which is exactly when the p-value beside it says "not
    proven" anyway. n == 0 returns [-1, 1]; b + c == 0 returns [0, 0], the
    arms never disagreed.
    """
    if n == 0:
        return (-1.0, 1.0)
    difference = (c - b) / n
    inside = b + c - (c - b) ** 2 / n
    standard_error = math.sqrt(max(inside, 0.0)) / n
    margin = z_for_confidence(confidence) * standard_error
    return (max(-1.0, difference - margin), min(1.0, difference + margin))


def mcnemar_from_vectors(
    a_correct: list[bool], b_correct: list[bool]
) -> McNemarResult:
    """McNemar from two aligned per-item correctness vectors.

    The vectors must be index-aligned over the same items in the same order
    (the harness's correctness_vector guarantees this). Concordant items
    (both right or both wrong) carry no signal and drop out.
    """
    if len(a_correct) != len(b_correct):
        raise ValueError("correctness vectors must be the same length")
    b = sum(1 for a, bb in zip(a_correct, b_correct) if a and not bb)
    c = sum(1 for a, bb in zip(a_correct, b_correct) if not a and bb)
    return mcnemar_exact(b, c)


def independent_difference_interval(
        first: tuple[int, int, int], second: tuple[int, int, int],
        confidence: float = 0.95) -> tuple[float, float]:
    """Interval for the difference of two paired differences measured on
    DISJOINT question sets. Each argument is `(b, c, n)`.

    This is the generalization gap: the working-set lift minus the held-out
    lift. A big positive gap says the context fitted the questions it was
    written from and did not carry.

    Disjoint is what makes this simple. The two sets share no question by
    construction - that is the whole point of the split - so the two
    differences are independent and the variance of their difference is the
    sum of the two variances. Sharing questions would need a covariance term
    and this formula would understate the width.

    Same standard error as `paired_difference_interval` on each side, so the
    gap's interval and the two lift intervals beside it are built the same
    way and a reader can compare them.
    """
    def parts(counts: tuple[int, int, int]) -> tuple[float, float]:
        b, c, n = counts
        if n == 0:
            return 0.0, float("inf")
        return (c - b) / n, math.sqrt(max(b + c - (c - b) ** 2 / n, 0.0)) / n

    first_d, first_se = parts(first)
    second_d, second_se = parts(second)
    if math.isinf(first_se) or math.isinf(second_se):
        return (-1.0, 1.0)
    margin = z_for_confidence(confidence) * math.sqrt(first_se ** 2 + second_se ** 2)
    difference = first_d - second_d
    return (max(-1.0, difference - margin), min(1.0, difference + margin))
