# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""M18's exit test: the rules file that cost a round is refused for free.

The board states it as *"the 2026-09-12 rules file is refused, the message
names the five subjectless lines and the ambiguous geography rule, and the
refusal costs nothing."*

THE REAL FILE CANNOT BE COMMITTED. It is a customer's, it lives under
`_personal/`, and that directory is gitignored precisely so it never ships. So
the fixture below is a synthetic file with the same SHAPE -- five rule lines
whose object is a pronoun with nothing to refer to, one rule naming a column
that sits on two tables, one naming a column no table has, and one naming a
column no answer key touches.

That substitution is a real weakening and it is worth saying plainly: this
proves the checks fire on the shape, not that they fired on that file. What
the original file produced is recorded in `docs/PRACTICES.md` and in the
round's own numbers -- 49 rule lines, a reserved lift of minus 6.9 points, and
three faults visible for free beforehand.

"COSTS NOTHING" IS THE HALF PEOPLE FORGET. The point of a linter here is not
tidiness, it is that the refusal happens before any model call. So the last
test asserts the whole thing runs with no executor, no runner and no network.
"""
from __future__ import annotations

import pathlib

import pytest

from context_lift.export import Spec, TableSchema
from context_lift.lint import REFUSE, WARN, golds_a_rule_could_change, lint

TABLE = "shop.orders"
OTHER = "shop.order_items"

# Five lines that command something and never say what it is about. The
# original file's author was told naming the metric was a person's job, and no
# person came.
SUBJECTLESS = [
    "- Compute it as `sum(gross_amount)` minus `sum(refund_amount)`.",
    "- Use that when the period is a fiscal one.",
    "- Apply these before any rounding.",
    "- Take the value from the most recent load.",
    "- Set them to zero when the source is missing.",
]

AMBIGUOUS_GEOGRAPHY = (
    "- Regional totals group by `country_code`, which is the shipping "
    "country.")

UNKNOWN = "- Margin uses `inventory_landed_cost`, net of returns."

UNREACHED = "- Loyalty tier comes from `loyalty_points` at order time."

GOLDS = [
    f"SELECT COUNT(*) FROM `{TABLE}` WHERE status = 'Complete'",
    f"SELECT SUM(gross_amount) FROM `{TABLE}`",
    f"SELECT country_code, COUNT(*) FROM `{OTHER}` GROUP BY country_code",
]


@pytest.fixture
def spec() -> Spec:
    """A spec whose arm carries the columns, and a rules file with the faults."""
    built = Spec(root=pathlib.Path("(test)"))
    built.schemas[TABLE] = TableSchema(columns={
        "status": {"description": "", "synonyms": [], "tags": []},
        "gross_amount": {"description": "", "synonyms": [], "tags": []},
        "refund_amount": {"description": "", "synonyms": [], "tags": []},
        "country_code": {"description": "", "synonyms": [], "tags": []},
        "loyalty_points": {"description": "", "synonyms": [], "tags": []},
    })
    # country_code on a SECOND table is what makes the geography rule
    # ambiguous. One table and it would be fine.
    built.schemas[OTHER] = TableSchema(columns={
        "country_code": {"description": "", "synonyms": [], "tags": []},
    })
    body = "\n".join(SUBJECTLESS + [AMBIGUOUS_GEOGRAPHY, UNKNOWN, UNREACHED])
    built.rules.append(("00-definitions.md", body))
    return built


# ---------------------------------------------------------------------------
# THE EXIT TEST

def test_the_file_is_refused(spec):
    report = lint(spec, GOLDS)

    assert not report.passed, "a file with these faults must not pass"
    assert report.refusals, "no refusal at all"


def test_the_message_names_the_five_subjectless_lines(spec):
    """Five, not 'some'. A linter that says a file has problems without
    saying which lines is a linter people switch off."""
    report = lint(spec, GOLDS)

    subjectless = [f for f in report.findings if f.check == "no subject"]
    assert len(subjectless) == 5, [f.where for f in subjectless]
    assert all(f.severity == REFUSE for f in subjectless)

    printed = report.summary()
    assert "no subject x5" in printed
    # and the first locations, so somebody can open the file at a line
    assert "00-definitions.md:1" in printed


def test_the_message_names_the_ambiguous_geography_rule(spec):
    """`country_code` is on orders AND order_items, and the rule does not say
    which. Observed once: a rule true of the estate moved the agent off the
    table that already had the column."""
    report = lint(spec, GOLDS)

    ambiguous = [f for f in report.findings if f.check == "ambiguous column"]
    assert len(ambiguous) == 1, [f.where for f in ambiguous]
    assert ambiguous[0].severity == WARN
    assert "country_code is on 2 tables" in ambiguous[0].detail


def test_the_refusal_costs_nothing(spec):
    """The half that is easy to forget. A linter that runs after the model
    calls is a report, not a gate.

    `lint` takes a spec and a list of gold SQL strings. No executor, no
    runner, no project, no credentials — so there is nothing it could bill
    even if it wanted to.
    """
    import inspect

    parameters = set(inspect.signature(lint).parameters)
    assert parameters == {"spec", "gold_sql"}, (
        f"lint now takes {parameters}. If it ever takes an executor or a "
        f"runner, the refusal stops being free")

    # and it really does run with nothing but the two
    assert lint(spec, GOLDS).refusals
    assert lint(spec).findings, "it still works with no answer keys at all"


# ---------------------------------------------------------------------------
# the check the exit test does not cover: a rule no question can reach

def test_a_rule_no_answer_key_touches_is_named_with_its_count(spec):
    """M18's last open item. `loyalty_points` is a real column on a real
    table, so every other check passes it, and no gold touches it — applied
    to all three it changes nothing about any of them."""
    report = lint(spec, GOLDS)

    unreached = [f for f in report.findings
                 if f.check == "no question would change"]
    assert len(unreached) == 1, [f.detail for f in unreached]
    assert "loyalty_points" in unreached[0].detail
    assert f"0 of {len(GOLDS)} answer keys touch" in unreached[0].detail
    assert unreached[0].severity == WARN, (
        "a gold set is a sample; a rule it does not reach is unmeasurable "
        "here, not wrong")


def test_the_count_is_how_many_golds_not_whether_any():
    """The number is the point. On a 3-question set a zero means little; on
    43 it means the rule is about a part of the business nobody asked about,
    and only the count tells those apart."""
    assert golds_a_rule_could_change(["status"], GOLDS) == 1
    assert golds_a_rule_could_change(["country_code"], GOLDS) == 1
    assert golds_a_rule_could_change(["gross_amount"], GOLDS) == 1
    assert golds_a_rule_could_change(["loyalty_points"], GOLDS) == 0
    # a rule naming two columns reaches the union, not the intersection
    assert golds_a_rule_could_change(["status", "gross_amount"], GOLDS) == 2


def test_reach_is_not_a_claim_that_the_rule_would_change_the_answer():
    """A rule naming `status` reaches every gold mentioning `status`.
    Whether it moves any of them is what a round measures, and the docstring
    says so; this pins the distinction so nobody 'improves' the count into a
    prediction."""
    assert "reach count and NOT a claim" in golds_a_rule_could_change.__doc__


def test_no_answer_keys_means_the_check_is_skipped_not_passed(spec):
    """A skipped check reported as a pass is the failure mode this whole
    module is built to avoid."""
    report = lint(spec)

    assert not [f for f in report.findings
                if f.check == "no question would change"]
    assert any("no question would change" in note for note in report.skipped)
    assert "SKIPPED" in report.summary()
