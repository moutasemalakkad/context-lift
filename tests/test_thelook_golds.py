# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The thelook gold set, built from the committed seed queries."""
from __future__ import annotations

import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "examples" / "thelook"))
from make_golds import build, read_pairs, split_definition  # noqa: E402

HERE = pathlib.Path(__file__).parent.parent / "examples" / "thelook"


def test_a_definition_in_brackets_leaves_the_question():
    """The parenthetical is the exact rule the framework measures. Left in the
    question, the bare arm gets it free and no context can show a lift."""
    question, definition = split_definition(
        "revenue by month, 2023 (revenue = sale price of items neither "
        "cancelled nor returned)")

    assert question == "revenue by month, 2023"
    assert definition == "revenue = sale price of items neither cancelled nor returned"


def test_a_bracket_that_is_not_a_rule_stays_in_the_question():
    """"products never sold (no order item at all)" is one question, not a
    question plus a rule. Splitting it would leave a question nobody asked."""
    question, definition = split_definition("products never sold (no order item at all)")

    assert question == "products never sold (no order item at all)"
    assert definition == ""


def test_no_committed_gold_question_carries_a_rule():
    """The whole point. A question that defines its own terms cannot measure
    whether a written definition helps."""
    rows = list(csv.DictReader(open(HERE / "golds.csv", encoding="utf-8")))

    assert rows, "run examples/thelook/make_golds.py"
    for row in rows:
        assert "=" not in row["question"], row["question"]
        assert "revenue = " not in row["question"].lower()


def test_a_repeated_shape_is_a_seed_and_never_a_gold():
    """The seed file repeats a shape so mining sees it more than once. As a
    gold it is a template twin, and a twin split across curate and holdout
    turns the held-out answer into substitution."""
    text = ("-- revenue by month, 2023 (revenue = x)\nSELECT 1;\n\n"
            "-- same shape, 2024\nSELECT 2;\n")
    golds, _definitions, skipped = build(text)

    assert [g["question"] for g in golds] == ["revenue by month, 2023"]
    assert skipped == ["same shape, 2024"]


def test_every_gold_records_who_wrote_it_and_where_it_came_from():
    """A set mixing SQL-first questions with model drafts behaves differently,
    and six months on nobody remembers which is which.

    Both kinds are in this set now: questions taken from the seed file's own
    comments, and questions a model drafted from mined SQL. The origin column
    is the only thing that says which, so every row must carry one and both
    kinds must be represented.
    """
    rows = list(csv.DictReader(open(HERE / "golds.csv", encoding="utf-8")))

    assert all(row["author"] for row in rows)
    assert all(row["origin"].strip() for row in rows)
    origins = {row["origin"].split(":")[0] for row in rows}
    assert any(o.startswith("SQL first") for o in origins)
    assert any(o.startswith("mined from the query log") for o in origins)


def test_the_opening_block_comment_never_becomes_a_question():
    """The file starts with several comment lines and no query under them."""
    text = ("-- Queries an analyst would run.\n-- A second header line.\n\n"
            "-- revenue by month\nSELECT 1;\n")

    assert [d for d, _sql in read_pairs(text)] == ["revenue by month"]


def test_the_committed_set_is_what_the_script_produces():
    """Committed output and the script that makes it must not drift.

    The set is the seed golds, then the mined ones, then the authored ones,
    and all three inputs are committed. A hand-edited golds.csv nobody can regenerate is a file that
    drifts, which is why the mined rows live in their own file rather than
    being typed into the output.
    """
    golds, definitions, _skipped = build((HERE / "queries.sql").read_text(encoding="utf-8"))
    mined = list(csv.DictReader(open(HERE / "mined-golds.csv", encoding="utf-8")))
    authored = list(csv.DictReader(open(HERE / "authored-golds.csv", encoding="utf-8")))
    committed = list(csv.DictReader(open(HERE / "golds.csv", encoding="utf-8")))

    assert len(committed) == len(golds) + len(mined) + len(authored)
    assert ([row["question"] for row in committed]
            == [g["question"] for g in golds] + [m["question"] for m in mined]
            + [a["question"] for a in authored])
    assert all(line in (HERE / "definitions.md").read_text(encoding="utf-8")
               for line in definitions)


def test_no_mined_question_states_a_business_rule():
    """The whole reason `mine --rules-in-context` exists.

    A question that says "excluding Cancelled and Returned" hands the bare arm
    the rule for free, so no written context can show a lift. Without the flag
    10 of 25 mined golds named a rule this way.
    """
    import re

    # The leak is rule PHRASING, not any mention of a value. "the number of
    # cancelled orders" is the subject of a question about cancellations and
    # belongs in it; "excluding orders with a status of Cancelled" is a rule
    # the context is supposed to supply, and stating it hands the bare arm the
    # answer.
    rule_phrasing = re.compile(
        r"(exclud\w*|ignor\w*|other than|not in|without)\s+[^.]{0,40}"
        r"(cancelled|returned)", re.I)
    mined = list(csv.DictReader(open(HERE / "mined-golds.csv", encoding="utf-8")))

    assert mined, "run the mining path, or drop this file"
    leaky = [m["question"] for m in mined if rule_phrasing.search(m["question"])]
    assert leaky == [], leaky


def test_no_two_golds_share_a_query_shape():
    """A template twin split across working and reserved turns the reserved
    answer into substitution: the model finds the same query next to it and
    swaps a literal. 104 golds, 104 shapes, checked over the whole set rather
    than only over the batch that was added last."""
    import csv

    from context_lift.leakage import normalize_sql

    golds = list(csv.DictReader(open(HERE / "golds.csv", encoding="utf-8")))
    shapes = {}
    twins = []
    for gold in golds:
        shape = normalize_sql(gold["gold_sql"])
        if shape in shapes:
            twins.append((gold["id"], shapes[shape]))
        shapes[shape] = gold["id"]

    assert not twins, f"template twins: {twins}"
    assert len(shapes) == len(golds)


def test_the_set_can_resolve_the_effect_it_was_grown_to_measure():
    """The whole reason for growing it. Every round before this read
    UNDERPOWERED because 8 reserved questions cannot show a 10-point lift. The
    ladder rule says a set resolves a lift above about 100/sqrt(n) points."""
    import csv

    from context_lift.leakage import split_golds

    golds = list(csv.DictReader(open(HERE / "golds.csv", encoding="utf-8")))
    _working, reserved = split_golds([g["id"] for g in golds], 0.3, "contextlift")

    resolvable = 100 / len(reserved) ** 0.5

    assert len(reserved) >= 30
    assert resolvable < 20, (
        f"{len(reserved)} reserved questions resolve only {resolvable:.0f} "
        "points; the working-set effect measured on 2026-09-03 was +50")
