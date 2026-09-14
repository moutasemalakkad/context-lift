# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Behavior tests for the statistics layer.

The headline tests are pinned to published / externally-computable oracles
(Wilson 19/35, McNemar exact), so green means the hand-rolled math is right,
not just self-consistent. The parity tests at the bottom cross-check against
statsmodels when it is installed.
"""
import pytest

from context_lift.metrics import (
    inverse_normal_cdf,
    mcnemar_exact,
    mcnemar_from_vectors,
    paired_difference_interval,
    proportion,
    wilson_interval,
    z_for_confidence,
)


def test_wilson_interval_matches_published_value():
    # Oracle: a 19/35 (~54%) rate has a 95% Wilson interval of [0.382, 0.695].
    low, high = wilson_interval(19, 35)
    assert round(low, 3) == 0.382
    assert round(high, 3) == 0.695


def test_wilson_interval_is_about_31_points_wide():
    # The point of reporting a CI: 35 questions can't tell 40% from 70%.
    low, high = wilson_interval(19, 35)
    assert round((high - low) * 100) == 31


def test_wilson_lower_bound_never_below_zero():
    # Where the normal approximation would go negative, Wilson sits at 0.
    low, _ = wilson_interval(0, 10)
    assert low == 0.0


def test_wilson_upper_bound_never_above_one():
    _, high = wilson_interval(10, 10)
    assert high == 1.0


def test_zero_trials_gives_maximal_interval():
    # No data -> maximal ignorance, not a ZeroDivisionError.
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_proportion_carries_rate_and_interval():
    pr = proportion(19, 35)
    assert round(pr.point, 3) == 0.543
    assert round(pr.low, 3) == 0.382
    assert round(pr.high, 3) == 0.695
    assert round(pr.width, 3) == 0.313


def test_z_for_95_percent_is_1_96():
    assert round(z_for_confidence(0.95), 2) == 1.96


def test_inverse_normal_of_half_is_zero():
    assert round(inverse_normal_cdf(0.5), 9) == 0.0


def test_mcnemar_lopsided_disagreement_is_significant():
    # 10 vs 2 disagreements: exact p = 2 * (C(12,0)+C(12,1)+C(12,2)) / 2**12.
    assert round(mcnemar_exact(10, 2).p_value, 4) == 0.0386


def test_mcnemar_reproduces_statsmodels_example_value():
    # The off-diagonal of [[75,5],[10,10]] is b=5, c=10; statsmodels reports
    # p=0.302 for exact=True. The 75/10 on the diagonal are ignored.
    assert round(mcnemar_exact(5, 10).p_value, 3) == 0.302


def test_mcnemar_balanced_disagreement_is_not_significant():
    assert mcnemar_exact(6, 6).p_value == 1.0


def test_mcnemar_no_disagreement_is_p_one():
    assert mcnemar_exact(0, 0).p_value == 1.0


def test_mcnemar_stays_exact_past_a_thousand_disagreements():
    # 0.5 ** 1100 is 0.0 in a float, so the old product form returned p = 0
    # for every large test; the integer division keeps the true tail.
    assert round(mcnemar_exact(600, 500).p_value, 4) == 0.0028
    assert mcnemar_exact(550, 550).p_value == 1.0


def test_paired_difference_interval_is_the_wald_form_on_the_discordant_pairs():
    # 4 fixed, 0 broke, 16 pairs: d = 0.25, se = sqrt(4 - 1) / 16, z = 1.96
    low, high = paired_difference_interval(0, 4, 16)
    assert (round(low, 3), round(high, 3)) == (0.038, 0.462)


def test_paired_difference_interval_is_a_point_when_the_arms_never_disagreed():
    assert paired_difference_interval(0, 0, 3) == (0.0, 0.0)


def test_paired_difference_interval_stays_inside_minus_one_and_one():
    assert paired_difference_interval(0, 0, 0) == (-1.0, 1.0)
    low, high = paired_difference_interval(1, 1, 3)
    assert -1.0 <= low < 0 < high <= 1.0


def test_mcnemar_from_vectors_counts_discordant_pairs():
    # idx0 (T,F)->b, idx1 (T,T)->concordant, idx2 (F,F)->concordant,
    # idx3 (T,F)->b. So b=2, c=0.
    a = [True, True, False, True]
    b = [False, True, False, False]
    res = mcnemar_from_vectors(a, b)
    assert (res.b, res.c) == (2, 0)


# --- Parity cross-checks: only run if statsmodels is installed. They prove
# the stdlib implementation equals the trusted library, without making it a
# runtime or dev dependency. ---

def test_wilson_matches_statsmodels_when_available():
    sm = pytest.importorskip("statsmodels.stats.proportion")
    expected = sm.proportion_confint(19, 35, alpha=0.05, method="wilson")
    assert wilson_interval(19, 35) == pytest.approx(expected, abs=1e-9)


def test_mcnemar_matches_statsmodels_when_available():
    sm = pytest.importorskip("statsmodels.stats.contingency_tables")
    # Diagonal is ignored by McNemar, so set it to zero; off-diagonal b=5, c=10.
    result = sm.mcnemar([[0, 5], [10, 0]], exact=True)
    assert mcnemar_exact(5, 10).p_value == pytest.approx(result.pvalue, abs=1e-12)
