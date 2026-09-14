# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The verdict word, and the order that stops a flattering lie.

The case every rule here exists for: a real customer page once printed REAL
GAIN off a curate lift of +60.6 points while the holdout scored 2 of 18 in
both arms, a lift of exactly zero.
"""
from __future__ import annotations

from context_lift.ablate import (AblationReport, DISCORDANT_PAIRS_FOR_SIGNIFICANCE,
                             questions_for_target, verdict)
from context_lift.metrics import McNemarResult, mcnemar_exact, proportion


def report(b: int, c: int, n: int, lift: float) -> AblationReport:
    """One arm pair. `b` broke, `c` fixed, out of `n` questions."""
    return AblationReport(
        without_context=proportion(1, max(n, 1)),
        with_context=proportion(1, max(n, 1)),
        lift=lift,
        mcnemar=mcnemar_exact(b, c),
        n_paired=n, adjusted=False, n_questions=n)


def test_a_failed_gate_beats_every_score():
    """A number from a run that failed its own checks gets quoted without the
    caveat, so the gate is the headline and the scores are not shown."""
    strong = report(b=0, c=20, n=60, lift=0.33)

    result = verdict(strong, gates=[("leak", False, "3 scored questions appear in the examples")])

    assert result.word == "INVALID"
    assert not result.scores_are_publishable
    assert "leak" in result.headline and "3 scored" in result.headline


def test_the_page_that_lied_now_reads_overfit():
    """Curate moved a lot, the holdout moved not at all. This is the exact
    shape that printed REAL GAIN on a customer page."""
    curate = report(b=0, c=20, n=33, lift=0.606)
    holdout = report(b=0, c=0, n=18, lift=0.0)

    result = verdict(holdout, curate)

    assert result.word == "OVERFIT"
    assert "More questions do not fix this" in result.reason


def test_a_significant_holdout_is_a_real_gain_and_names_its_p():
    holdout = report(b=0, c=12, n=60, lift=0.20)

    result = verdict(holdout)

    assert result.word == "REAL GAIN"
    assert result.p_value < 0.05
    assert "read once" in result.reason


def test_the_sign_picks_gain_or_loss():
    holdout = report(b=12, c=0, n=60, lift=-0.20)

    assert verdict(holdout).word == "REAL LOSS"


def test_a_big_enough_holdout_that_flipped_nothing_says_nothing_moved():
    """Only when the set could have seen a lift is "nothing moved" a finding
    rather than a guess. 60 questions can see 10 points."""
    result = verdict(report(b=0, c=0, n=60, lift=0.0), target_pp=10.0)

    assert result.word == "NO CHANGE"
    assert "changed none of them" in result.reason


def test_the_first_live_round_reads_unresolved_not_no_change():
    """The exact numbers of the first real round, 2026-09-09: 6 held-out
    questions flipped nothing while 6 of 20 curate questions flipped.

    Six questions would have been expected to flip about two, so seeing none
    out of six is ordinary luck. An earlier draft put NO CHANGE before
    UNDERPOWERED and printed "the context changed nothing", which states a
    finding the set could not support. A set too small to have seen anything
    is UNDERPOWERED whatever it saw.
    """
    holdout = report(b=0, c=0, n=6, lift=0.0)
    curate = report(b=2, c=4, n=20, lift=0.10)

    result = verdict(holdout, curate, target_pp=10.0)

    assert result.word == "UNDERPOWERED"
    assert result.questions_needed == 60
    assert round(result.resolution, 3) == 0.1
    assert "Nothing here says the context does not help" in result.reason


def test_the_curate_lift_of_the_first_live_round_was_not_significant():
    """4 fixed and 2 broke out of 20 is a worklist input, not a result:
    McNemar on b=2, c=4 gives about 0.69. Had it been significant with a flat
    holdout, the word would have been OVERFIT."""
    curate = report(b=2, c=4, n=20, lift=0.10)

    assert curate.mcnemar.p_value > 0.05
    assert verdict(report(b=0, c=0, n=60, lift=0.0), curate,
                   target_pp=10.0).word == "NO CHANGE"


def test_a_set_too_small_to_have_seen_the_target_is_unresolved():
    """24 questions cannot show a 10-point lift even if nothing broke, so
    "not proven" would be a claim the set could not have supported."""
    result = verdict(report(b=1, c=3, n=24, lift=0.08), target_pp=10.0)

    assert result.word == "UNDERPOWERED"
    assert result.questions_needed == 60
    assert result.resolution < 1
    assert "Nothing here says the context does not help" in result.reason


def test_a_big_enough_set_that_showed_nothing_is_not_proven():
    result = verdict(report(b=2, c=3, n=60, lift=0.02), target_pp=10.0)

    assert result.word == "NOT PROVEN"
    assert result.resolution >= 1


def test_overfit_cannot_be_reached_without_the_curate_set():
    """Stated by the shape of the call rather than hidden: one arm pair alone
    cannot tell fitting from finding."""
    holdout = report(b=1, c=3, n=24, lift=0.08)

    assert verdict(holdout, curate=None).word == "UNDERPOWERED"


def test_a_curate_set_that_moved_the_wrong_way_is_not_overfit():
    """Overfitting means the working questions improved. A curate set that got
    worse is a different problem and must not borrow this word."""
    curate = report(b=20, c=0, n=33, lift=-0.60)
    holdout = report(b=0, c=0, n=60, lift=0.0)

    assert verdict(holdout, curate).word == "NO CHANGE"


def test_six_one_sided_pairs_is_the_floor_under_every_resolution_claim():
    """McNemar with b = 0 and c = k gives p = 2 ** (1 - k), so k = 6 is the
    first count that clears 0.05. Every questions_for_target answer is that
    six divided by the target rate."""
    assert mcnemar_exact(0, DISCORDANT_PAIRS_FOR_SIGNIFICANCE).p_value < 0.05
    assert mcnemar_exact(0, DISCORDANT_PAIRS_FOR_SIGNIFICANCE - 1).p_value >= 0.05


def test_the_questions_a_target_needs():
    """Printed beside every "not proven", so nobody has to work it out."""
    assert questions_for_target(10) == 60
    assert questions_for_target(20) == 30
    assert questions_for_target(25) == 24
    assert questions_for_target(0) is None
