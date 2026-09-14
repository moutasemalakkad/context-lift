# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Drop what the gate refused, before anyone pays to review it."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from usable_candidates import split  # noqa: E402


def rows(*statuses: str) -> list[dict]:
    return [{"id": f"m{i:03d}", "gate_status": s} for i, s in enumerate(statuses)]


def test_a_warning_is_not_a_defect():
    """The subtle case, and it is most of a real set.

    `mine --rules-in-context` drafts a question that leaves OUT the business
    rule, on purpose, so the written context has to supply it. That raises
    MISSING_LITERAL as a warning. Filtering on strict equality to "ok" would
    have dropped 123 of 177 usable candidates on one real pull - the best
    ones in the set, because omitting the rule IS the measurement.
    """
    usable, refused = split(rows(
        "ok",
        "ok (MISSING_LITERAL)",
        "ok (MISSING_PROJECTION)",
        "ok (MISSING_LITERAL+MISSING_PROJECTION)",
    ))

    assert len(usable) == 4
    assert refused == []


def test_a_query_that_cannot_run_never_reaches_review():
    """A 403 is not a question defect and no reviewer can act on it, but the
    drafter kept 24 of them on one estate and inflated the kept count."""
    usable, refused = split(rows(
        "NO_ACCESS", "TABLE_GONE", "ZERO_ROWS", "OVER_BUDGET",
        "CROSS_PROJECT", "EXEC_FAIL", "MISSING_PARAMETER",
    ))

    assert usable == []
    assert len(refused) == 7


def test_a_defect_beside_a_warning_is_still_refused():
    """`NO_ACCESS+MISSING_LITERAL` ran into a 403. The warning does not
    rescue it, and a substring check for "ok" would have let it through."""
    usable, refused = split(rows(
        "NO_ACCESS+MISSING_LITERAL",
        "TABLE_GONE+MISSING_LITERAL+MISSING_PROJECTION",
    ))

    assert usable == []
    assert len(refused) == 2


def test_a_missing_or_empty_status_is_refused_not_assumed_clean():
    """An unreadable row must not become a gold by default."""
    usable, refused = split([{"id": "m1"}, {"id": "m2", "gate_status": ""}])

    assert usable == []
    assert len(refused) == 2
