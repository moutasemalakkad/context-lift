# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Context nobody in the loop wrote needs no reserved half, and could not say so.

A reserved half exists to stop somebody writing rules while looking at the
questions. When the context predates the gold set and no human here touched
it, there is nothing for a split to protect against.

The case that forced this, 2026-09-12. Looker's public thelook project, read
into a context arm and scored. Looker wrote it in February 2022; the gold set
was built in 2026 from `queries.sql`. Four years apart, by different people.

`--holdout-share 0.99` was the workaround, because `split_golds` refuses a
fraction of 1.0. It left a working half of TWO questions, and the report
printed "fit on the working questions: 2 questions, +0.0 points" and "gap:
-2.0 points [-6.7, +2.7]" beside numbers computed from a hundred. It did not
just read badly: it manufactured a comparison.
"""
from __future__ import annotations

import pytest

from context_lift.leakage import split_golds

IDS = [f"q{i:03d}" for i in range(10)]
REASON = "looker-open-source/thelook @88d68c3 (2022-02-09)"


def test_a_reason_reserves_every_gold():
    curate, holdout = split_golds(IDS, external_context=REASON)
    assert curate == []
    assert holdout == IDS


def test_the_order_is_preserved_so_the_split_stays_stable():
    """The whole set, in input order. Nothing is hashed or shuffled, because
    there is nothing to divide."""
    _, holdout = split_golds(IDS, external_context=REASON)
    assert holdout == IDS


@pytest.mark.parametrize("blank", [" ", "   ", "\n", "\t"])
def test_a_whitespace_only_reason_is_refused(blank):
    """Reserving everything changes what the numbers mean, so the claim has to
    be attributable. Whitespace is not a claim."""
    with pytest.raises(ValueError, match="needs a reason"):
        split_golds(IDS, external_context=blank)


def test_an_empty_string_is_simply_off():
    """"" means the flag was not passed, so the ordinary split runs. It is
    the default, and it must not raise."""
    curate, holdout = split_golds(IDS, 0.3, external_context="")
    assert curate and holdout


def test_a_fraction_of_one_is_still_refused():
    """1.0 stays unreachable as a NUMBER. A fraction that became 1.0 by
    arithmetic -- a rounding, a config default, a typo -- would empty the
    working half and with it the OVERFIT check, and the round would still
    print a verdict. Asking in words cannot happen by accident."""
    with pytest.raises(ValueError):
        split_golds(IDS, 1.0)
    with pytest.raises(ValueError):
        split_golds(IDS, 0.0)


def test_it_does_not_change_the_ordinary_split():
    before = split_golds(IDS, 0.3, "seed")
    after = split_golds(IDS, 0.3, "seed", external_context="")
    assert before == after


def test_the_report_says_there_is_no_fit_estimate_rather_than_omitting_it():
    """An absent fit line is silent, and silence about a missing check reads
    as a check that passed. Same principle as the linter's SKIPPED lines.

    RoundResult already tolerated curate=None, so before this the two lines
    simply vanished and nothing said why.
    """
    from context_lift.round import RoundResult
    from context_lift.ablate import Verdict

    result = RoundResult(
        verdict=Verdict(word="NOT PROVEN", p_value=0.6875,
                        reason="102 held-out questions could have shown it"),
        holdout=None, curate=None, external_context=REASON)
    text = "\n".join(result.lines())
    assert REASON in text
    assert "no working half" in text
    assert "OVERFIT cannot be checked" in text
    assert "not an oversight" in text


def test_the_dry_run_and_the_round_split_the_same_way():
    """A dry run exists to show the gates BEFORE anything is paid for. One
    that splits differently checks the wrong questions: with the flag set it
    reported 30 held out where the round would hold out 104, so the leak gate
    and the template-twin gate were both reading the wrong set.

    Checked by reading the source rather than by running a round, because
    running one needs a warehouse and this is a wiring question.
    """
    import inspect
    from context_lift import cli
    source = inspect.getsource(cli)
    forwarded = source.count("external_context=")
    assert forwarded >= 2, (
        "both the dry-run split and the round call must forward the reason; "
        f"found {forwarded} forwarding site(s)")
