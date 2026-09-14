# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Behavior tests for reading a semantic layer.

Two things carry the weight. The context bundle has to put MEANING before
mechanics, because the SQL expression is the part a model can reconstruct and
the description is the part it cannot. And candidate questions must not be
generated from key columns, or the review file fills with "revenue by order id".

LookML is written inline rather than read from a fixture directory, so each test
shows the input that produces the behavior it pins.
"""
import pytest

# lkml ships in the dev extra, so this normally runs. The guard is for anyone
# installing without it: a missing optional dependency should skip a test, not
# fail one.
pytest.importorskip("lkml")
from context_lift.semantic import (
    Dimension,
    JoinPath,
    Metric,
    SemanticModel,
    read_lookml,
)

VIEW = """
view: order_items {
  sql_table_name: shop.order_items ;;

  dimension: id {
    primary_key: yes
    type: number
    sql: ${TABLE}.id ;;
  }

  dimension: user_id {
    type: number
    hidden: yes
    sql: ${TABLE}.user_id ;;
  }

  dimension: status {
    type: string
    description: "Fulfilment state of the line"
    sql: ${TABLE}.status ;;
  }

  measure: total_revenue {
    type: sum
    description: "Total revenue from item sales, in USD"
    sql: ${TABLE}.sale_price ;;
  }

  measure: undocumented_count {
    type: count
  }
}
"""

EXPLORE = """
explore: order_items {
  join: users {
    relationship: many_to_one
    sql_on: ${order_items.user_id} = ${users.id} ;;
  }
}
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "views").mkdir()
    (tmp_path / "views" / "order_items.view.lkml").write_text(VIEW)
    (tmp_path / "shop.model.lkml").write_text(EXPLORE)
    return tmp_path


def test_reads_metrics_with_their_descriptions(project):
    model = read_lookml(project)
    revenue = next(m for m in model.metrics if m.name == "total_revenue")
    assert revenue.description == "Total revenue from item sales, in USD"
    assert revenue.kind == "sum"
    assert revenue.source_table == "shop.order_items"


def test_the_lookml_table_marker_is_stripped_from_expressions(project):
    # ${TABLE}.sale_price is the tool saying "this view's table". A human
    # reviewing the rule does not need it, and it is not valid SQL anywhere.
    #
    # This assertion read `== "sale_price"` until 2026-09-12, which pinned a
    # BUG as though it were the feature: the measure's `type: sum` was being
    # dropped, so the context told an agent a total was a bare column. The
    # marker-stripping this test is actually about still holds -- what changed
    # is that the aggregation is now applied around it.
    revenue = next(m for m in read_lookml(project).metrics if m.name == "total_revenue")
    assert revenue.sql == "SUM(sale_price)"
    assert "TABLE" not in revenue.sql
    assert "TABLE" not in revenue.sql


def test_a_metric_without_a_description_is_not_counted_as_documented(project):
    model = read_lookml(project)
    assert len(model.metrics) == 2
    assert [m.name for m in model.documented_metrics] == ["total_revenue"]


def test_hidden_dimensions_are_skipped(project):
    # hidden: yes means no user sees the field, so no user asks about it.
    names = [d.name for d in read_lookml(project).dimensions]
    assert "user_id" not in names
    assert "status" in names


def test_join_paths_are_read_from_the_explore(project):
    joins = read_lookml(project).joins
    assert len(joins) == 1
    assert joins[0].left == "order_items"
    assert joins[0].right == "users"
    assert joins[0].relationship == "many_to_one"
    assert "order_items.user_id = users.id" in joins[0].on


def test_an_unparseable_file_does_not_lose_the_rest(project):
    # A real project usually has one file using a construct the parser does not
    # know. Losing the other 141 files to it helps nobody.
    (project / "views" / "broken.view.lkml").write_text("view: { this is not lookml")
    assert len(read_lookml(project).metrics) == 2


def test_a_directory_with_no_lookml_is_an_error_not_an_empty_model(tmp_path):
    with pytest.raises(FileNotFoundError, match="no .lkml files"):
        read_lookml(tmp_path)


# --- the context bundle ---


def test_context_puts_documented_metrics_before_undocumented_ones():
    model = SemanticModel(metrics=[
        Metric(name="bare", kind="count"),
        Metric(name="explained", description="What this means", sql="x"),
    ])
    context = model.to_context()
    assert context.index("explained") < context.index("bare")


def test_context_carries_the_meaning_and_the_computation():
    model = SemanticModel(metrics=[
        Metric(name="aur", description="Average unit retail",
               sql="sales / units", source_table="shop.sales"),
    ])
    context = model.to_context()
    assert "Average unit retail" in context
    assert "sales / units" in context
    assert "shop.sales" in context


def test_context_states_how_tables_join():
    model = SemanticModel(joins=[
        JoinPath(left="orders", right="users", on="orders.user_id = users.id",
                 relationship="many_to_one")
    ])
    assert "orders.user_id = users.id" in model.to_context()
    assert "many_to_one" in model.to_context()


def test_summary_warns_when_most_metrics_are_undocumented():
    mostly_bare = SemanticModel(metrics=[Metric(name=f"m{i}") for i in range(9)]
                                + [Metric(name="ok", description="explained")])
    assert "Most metrics have no description" in mostly_bare.summary()

    documented = SemanticModel(metrics=[
        Metric(name=f"m{i}", description="explained") for i in range(4)])
    assert "Most metrics have no description" not in documented.summary()


# --- candidate questions ---


def test_a_documented_metric_becomes_a_question(project):
    questions = [q for q, _ in read_lookml(project).to_candidates()]
    assert "What is total revenue from item sales, in usd?" in questions


def test_key_columns_never_become_grouping_dimensions(project):
    # "revenue by order id" is one row per order, not a question anyone asks.
    questions = " ".join(q for q, _ in read_lookml(project).to_candidates())
    assert " by id" not in questions
    assert " by user id" not in questions
    assert " by status" in questions


def test_a_trailing_qualifier_is_dropped_from_crossed_questions(project):
    # "total revenue from item sales, in USD by status" reads badly.
    crossed = [q for q, _ in read_lookml(project).to_candidates() if " by " in q]
    assert crossed == ["What is total revenue from item sales by status?"]


def test_undocumented_metrics_produce_no_questions():
    model = SemanticModel(metrics=[Metric(name="mystery", kind="count")])
    assert model.to_candidates() == []


def test_candidates_carry_no_sql():
    # Writing the query is the human step, same as with mined queries.
    model = SemanticModel(
        metrics=[Metric(name="m", description="A thing", source_table="t")],
        dimensions=[Dimension(name="region", source_table="t")],
    )
    for question, metric_name in model.to_candidates():
        assert isinstance(question, str) and isinstance(metric_name, str)
        assert "SELECT" not in question.upper()
