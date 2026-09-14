# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The clean room: the statements, and the two facts it exists for."""
import pytest

from context_lift.evalproject import arm_tables, billing_warning, plan, view_name

TABLES = ["acme-prod.sales.orders", "acme-prod.sales.order_items",
          "acme-prod.marketing.events"]


def test_a_view_is_named_after_its_dataset_and_table_together():
    """Two datasets on one estate carry the same table name often enough
    that a collision would point two views at one table."""
    assert view_name("eval-p", "room", "acme-prod.sales.orders") == "eval-p.room.sales__orders"
    assert (view_name("eval-p", "room", "acme-prod.marketing.orders")
            != view_name("eval-p", "room", "acme-prod.sales.orders"))


def test_a_table_must_name_its_project():
    with pytest.raises(ValueError, match="project.dataset.table"):
        view_name("eval-p", "room", "sales.orders")


def test_the_plan_creates_the_dataset_then_a_view_per_table():
    steps = plan("eval-p", "room", TABLES).steps

    assert "CREATE SCHEMA IF NOT EXISTS `eval-p.room`" in steps[0].statement
    views = [s for s in steps if s.statement.startswith("CREATE OR REPLACE VIEW")]
    assert len(views) == 3
    assert "SELECT * FROM `acme-prod.sales.orders`" in views[2].statement


def test_the_view_selects_star_so_a_new_column_is_not_silently_dropped():
    """A frozen column list would drop a column added later, which reads as
    the agent failing to find it."""
    for step in plan("eval-p", "room", TABLES).steps:
        if step.statement.startswith("CREATE OR REPLACE VIEW"):
            assert "SELECT *" in step.statement


def test_one_authorization_step_per_source_dataset_not_per_table():
    """Authorization is granted on the dataset, so two tables in one dataset
    are one write on production, not two."""
    production = plan("eval-p", "room", TABLES).touches_production

    assert len(production) == 2
    assert {"acme-prod.sales", "acme-prod.marketing"} == {
        s.what.split(" on ")[1].split(" ")[0] for s in production}


def test_the_plan_says_which_steps_write_on_production_and_what_they_do_not_touch():
    text = plan("eval-p", "room", TABLES).text()

    assert "2 of 6 steps write on a production dataset" in text
    assert "Nothing here" in text and "changes a table, a row, or any existing description" in text
    assert "roles/bigquery.dataOwner" in text


def test_an_arm_references_the_views_and_never_the_production_tables():
    """The whole point of the room: an arm that named a production table
    would carry that table's descriptions whether or not the arm asked."""
    names = arm_tables("eval-p", "room", TABLES)

    assert names == ["eval-p.room.marketing__events", "eval-p.room.sales__order_items",
                     "eval-p.room.sales__orders"]
    assert not [name for name in names if "acme-prod" in name]


def test_billing_the_gold_gate_in_the_same_project_is_warned_about():
    """The catalog mines join links from query history with no scan, so a
    gold executed here becomes context on the views it read."""
    warning = billing_warning("eval-p", harness_project="eval-p")

    assert warning is not None
    assert "mines join links from query history" in warning
    assert "--harness-project" in warning

    assert billing_warning("eval-p", harness_project="harness-p") is None
    assert billing_warning("eval-p", harness_project=None) is not None


def test_a_plan_with_no_tables_is_refused():
    with pytest.raises(ValueError, match="at least one production table"):
        plan("eval-p", "room", [])


def test_the_room_plan_runs_from_the_command_line(capsys):
    from context_lift.cli import main

    assert main(["eval-room", "--eval-project", "eval-p", "--tables"] + TABLES) == 0
    out = capsys.readouterr().out

    assert "CREATE OR REPLACE VIEW" in out
    assert "eval-p.room.sales__orders" in out
    assert "the arms then reference these, never the production tables" in out
    # the gold gate would bill the room itself, so the plan says why not to
    assert "warning:" in out and "mines join links from query history" in out


def test_naming_a_separate_harness_project_removes_the_warning(capsys):
    from context_lift.cli import main

    assert main(["eval-room", "--eval-project", "eval-p", "--harness-project", "harness-p",
                 "--tables", "acme-prod.sales.orders"]) == 0

    assert "warning:" not in capsys.readouterr().out
