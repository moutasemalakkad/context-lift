# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Five defects, all found on 2026-09-12 by pointing the reader at Looker's
own public project `looker-open-source/thelook` rather than at a fixture.

That distinction is the reason these tests exist and is worth keeping. A
fixture written by the same person who writes the reader carries that person's
assumptions: mine would have had descriptions on every measure, because I knew
descriptions were the point. The real project has descriptions on NONE of its
nine measures, and it was that file which showed `type: sum` being dropped.

The project ships no LICENSE file, so it is not vendored. The constructs are
reproduced in `tests/fixtures/lookml/` and the source is named there.
"""
from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("lkml")

from context_lift.semantic import (  # noqa: E402
    apply_measure_type, measure_filters, read_lookml,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "lookml"


@pytest.fixture(scope="module")
def model():
    return read_lookml(FIXTURES)


def metric(model, name):
    found = [m for m in model.metrics if m.name == name]
    assert found, f"{name} not read; got {[m.name for m in model.metrics]}"
    return found[0]


# --- Defect 1: the aggregation was dropped, so the context was WRONG. -------

def test_sum_is_applied_not_dropped(model):
    """The defect verbatim: `type: sum` + `sql: ${sale_price}` read as
    `sale_price`, telling an agent a total is a bare column."""
    assert metric(model, "total_sale_price").sql == "SUM(sale_price)"


def test_average_is_applied(model):
    assert metric(model, "average_sale_price").sql == "AVG(sale_price)"


def test_count_distinct_is_applied(model):
    assert metric(model, "distinct_orders").sql == "COUNT(DISTINCT order_id)"


def test_count_names_the_primary_key_not_star(model):
    """Looker's reference: "type: count measures perform table counts that are
    based on the table's primary key". COUNT(*) and COUNT(pk) agree until a
    join fans out, and a fanned-out join is when somebody asks why the total
    is too high."""
    assert metric(model, "count").sql == "COUNT(id)"


def test_no_aggregate_metric_is_left_as_a_bare_column(model):
    """The property, not one case. An aggregate whose rendered SQL has no
    function call is the original defect, whatever its type."""
    from context_lift.semantic import _AGGREGATION
    for m in model.metrics:
        if m.kind in _AGGREGATION:
            assert "(" in m.sql, f"{m.name} ({m.kind}) rendered bare: {m.sql!r}"


def test_a_non_aggregate_is_not_wrapped(model):
    """The fix in the other direction. `type: number`'s sql IS the whole
    expression, so wrapping it would invent an aggregation that is not there."""
    assert metric(model, "margin_rate").sql.startswith("1.0 *")
    assert not metric(model, "margin_rate").sql.startswith("SUM(")


def test_an_unknown_type_says_so_rather_than_reading_as_a_column():
    """A LookML type this reader has never seen must not silently render as a
    bare column, which is the whole defect. LookML gains types over time."""
    out = apply_measure_type("some_future_type", "amount")
    assert "amount" in out
    assert "some_future_type" in out


def test_a_measure_referencing_another_measure_is_marked(model):
    """percent_of_total takes a MEASURE, so its expression is incomplete on
    its own. Said out loud instead of guessed at."""
    assert "percent of total" in metric(model, "share_of_revenue").sql


# --- Defect 2: a filtered measure read as unfiltered. -----------------------

def test_modern_filter_syntax_is_read(model):
    """`filters: { field: x value: "Yes" }`. Without this the metric counts
    every row in the context while counting only some in Looker -- and the
    aggregation fix made that line MORE confident, so this is the fix that
    stops the fix from doing harm."""
    assert metric(model, "first_purchase_count").restricted_to == \
        'is_first_purchase is "Yes"'


def test_legacy_filter_syntax_is_read(model):
    """`filters: [status: "Complete"]`. The parser stores both syntaxes under
    `filters__all` in DIFFERENT shapes, so handling one leaves the other
    silent."""
    assert metric(model, "completed_count").restricted_to == \
        'status is "Complete" and cancelled_at is "-NULL"'


def test_the_restriction_is_never_presented_as_sql(model):
    """Looker's filter language has `-NULL`, `>10`, `Complete,Returned` for OR,
    `%foo%` and `last 7 days`. A guessed translation is the same confident
    falsehood in a new place, so the context says which language it is in."""
    text = model.to_context()
    assert "not SQL" in text
    assert "-NULL" in text          # passed through, not translated
    assert "IS NOT NULL" not in text


def test_a_restricted_metric_is_visible_in_the_context(model):
    line = [b for b in model.to_context().split("\n- ")
            if b.startswith("first_purchase_count")]
    assert line and "counts only rows where" in line[0]


# --- Defect 3: a count of what was read, not of what was exported. ----------

def test_the_summary_says_how_many_dimensions_are_described(model):
    """A real Looker project reported "38 dimensions" and shipped none of
    them, because only described dimensions reach the context. Same shape as
    the round that printed "62 model calls" having made none."""
    assert "of them described" in model.summary()


def test_zero_described_dimensions_is_stated_outright(tmp_path):
    (tmp_path / "v.view.lkml").write_text(
        "view: v {\n  dimension: a { type: string }\n"
        "  measure: count { type: count }\n}\n")
    summary = read_lookml(tmp_path).summary()
    assert "none reach the context" in summary


# --- Defect 4: a file that did not parse was skipped in silence. ------------

def test_an_unparseable_file_is_reported_not_swallowed(tmp_path):
    """Skipping is right; silence is not. A directory where every file failed
    printed "0 metrics" and exited 0, which reads as an empty project."""
    (tmp_path / "bad.view.lkml").write_text("view: x {\n  dimension: d { sql: a ;; ;; }\n")
    (tmp_path / "good.view.lkml").write_text(
        "view: y {\n  measure: count { type: count }\n}\n")
    model = read_lookml(tmp_path)
    assert len(model.unreadable) == 1
    assert model.unreadable[0][0] == "bad.view.lkml"
    assert "0 file" not in model.summary()
    assert "did NOT parse" in model.summary()
    # and the good file survived, which is why skipping is right
    assert len(model.metrics) == 1


# --- Defect 5: constructs this reader does not resolve, unannounced. --------

def test_a_view_refinement_never_becomes_a_table_name(tmp_path):
    """The M9 spec predicted this before the code was written: `lkml` keeps the
    `+` in `view: +orders`, and with no sql_table_name the name became the
    table, so context named the table `+orders`, which exists nowhere."""
    (tmp_path / "r.view.lkml").write_text(
        "view: +orders {\n  measure: extra { type: count }\n}\n")
    model = read_lookml(tmp_path)
    tables = {m.source_table for m in model.metrics}
    assert "+orders" not in tables
    assert tables == {"orders"}
    assert "view refinement(s) (`view: +name`)" in model.unresolved


@pytest.mark.parametrize("body,construct", [
    ("view: c {\n  extends: [base]\n  measure: count { type: count }\n}\n",
     "view(s) extending another view"),
    ("view: d {\n  derived_table: { sql: SELECT 1 ;; }\n"
     "  measure: count { type: count }\n}\n",
     "derived table(s)"),
    ('include: "*.view.lkml"\nview: e {\n  measure: count { type: count }\n}\n',
     "include statement(s)"),
])
def test_each_unresolved_construct_is_counted(tmp_path, body, construct):
    (tmp_path / "v.view.lkml").write_text(body)
    model = read_lookml(tmp_path)
    assert construct in model.unresolved, model.unresolved


def test_every_unresolved_construct_explains_what_it_costs(tmp_path):
    """A bare count in a warning is what this module spent the day removing.
    `_count` asserts the key exists, so this pins the other half: the
    explanation must actually reach the summary."""
    (tmp_path / "v.view.lkml").write_text(
        "view: c {\n  extends: [base]\n  measure: count { type: count }\n}\n")
    summary = read_lookml(tmp_path).summary()
    assert "their parent's fields are NOT included" in summary


def test_a_clean_project_warns_about_nothing(model):
    """The over-fitting check. A warning that fires on a well-formed project
    is a warning people switch off -- the lesson from the linter's `no
    subject` regex, which fired on a perfectly good rule."""
    clean = read_lookml(FIXTURES)
    assert clean.unresolved == {}
    assert clean.unreadable == []


# --- The unit table, checked against Looker's published reference. ----------

@pytest.mark.parametrize("kind,sql,expected", [
    ("sum", "x", "SUM(x)"),
    ("sum_distinct", "x", "SUM(DISTINCT x)"),
    ("average", "x", "AVG(x)"),
    ("min", "x", "MIN(x)"),
    ("max", "x", "MAX(x)"),
    ("count_distinct", "x", "COUNT(DISTINCT x)"),
    ("number", "a / b", "a / b"),
    ("string", "x", "x"),
    ("yesno", "x > 0", "x > 0"),
])
def test_the_type_table_matches_lookers_reference(kind, sql, expected):
    assert apply_measure_type(kind, sql) == expected


def test_count_with_no_primary_key_falls_back_to_star():
    assert apply_measure_type("count", "") == "COUNT(*)"


def test_filters_reader_handles_an_empty_measure():
    assert measure_filters({"type": "count"}) == ""
