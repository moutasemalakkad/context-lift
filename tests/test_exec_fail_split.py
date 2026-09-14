# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The four numbers in METHODOLOGY's EXEC_FAIL table, recomputed.

A figure in a shipped document has to be reproducible by a command, or nobody
can answer "how did you compute this". These four came from reading all 179
`gate_detail` strings an enterprise warehouse produced, and that sheet is
customer data and cannot ship. What CAN ship is every distinct error SHAPE
with its count, names replaced and numbers masked, because the classifier
reads the kind of error and nothing else.

So the fixture is the evidence and this test is the computation. It fails if
somebody edits the table, and it fails if the classifier changes what it does
with any of those 179 errors -- which is the drift actually worth catching,
since the whole point of the split was that the old label was wrong.
"""
import collections
import json
import pathlib
import re

from context_lift.gate import EXEC_FAIL, NO_ACCESS, OVER_BUDGET, TABLE_GONE, _failure_class

REPO = pathlib.Path(__file__).resolve().parent.parent
FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "exec_fail_details.json"


def _counted() -> collections.Counter:
    """Every recorded error run back through the classifier, one per row."""
    data = json.loads(FIXTURE.read_text())
    counted: collections.Counter = collections.Counter()
    for shape in data["details"]:
        defect, _note = _failure_class(shape["error"])
        counted[defect] += shape["count"]
    assert sum(counted.values()) == data["total"] == 179
    return counted


def test_the_four_classes_still_split_the_same_179_errors():
    """112 / 34 / 24 / 9. If this moves, either the classifier changed or the
    document is wrong, and both need a person."""
    counted = _counted()
    assert counted[OVER_BUDGET] == 112
    assert counted[NO_ACCESS] == 34
    assert counted[TABLE_GONE] == 24
    assert counted[EXEC_FAIL] == 9
    assert set(counted) == {OVER_BUDGET, NO_ACCESS, TABLE_GONE, EXEC_FAIL}, \
        "every one of the 179 lands in exactly one of the four"


def test_the_methodology_table_says_what_the_fixture_computes():
    """The document and the data cannot drift apart, which is the only way a
    number in a document stays true six months later."""
    text = (REPO / "docs" / "METHODOLOGY.md").read_text()
    # the table is written as `| description | N | who fixes it |`
    rows = dict(re.findall(r"\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|\s*[^|]+\|", text))
    by_number = {int(n) for n in rows.values()}
    counted = _counted()
    for defect, expected in ((OVER_BUDGET, 112), (NO_ACCESS, 34),
                             (TABLE_GONE, 24), (EXEC_FAIL, 9)):
        assert counted[defect] == expected
        assert expected in by_number, \
            f"METHODOLOGY's table has no row counting {expected} ({defect})"
    assert "179" in text and "446" in text, "the totals the table is a share of"


def test_the_old_label_really_did_cover_all_four():
    """The claim the split rests on: before this change every one of those
    179 rows printed EXEC_FAIL, so a reader saw one number where there were
    four problems. Checked by asking what the OLD rule would have said."""
    data = json.loads(FIXTURE.read_text())
    old_label_would_say = 0
    for shape in data["details"]:
        # the old rule: anything that is not a timeout is EXEC_FAIL
        if not shape["error"].startswith("timeout:"):
            old_label_would_say += shape["count"]
    assert old_label_would_say == 179
    # and the new rule leaves only 9 of them there
    assert _counted()[EXEC_FAIL] == 9
