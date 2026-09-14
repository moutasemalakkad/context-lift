# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Reading the CURRENT shape out of a live database, and what that took.

The drift module was complete and tested against a fake shape source. Wiring
it to a real database found three bugs in one sitting, none of which a test
against a fake would ever have shown:

  1. ExecResult rows are DICTS keyed by column name, not tuples. `row[0]`
     raised KeyError: 0.
  2. `describe_tables` filters sqlite to `type = 'table'`, so a VIEW gets no
     shape at all -- and a view is the only thing this module is really
     about. A warehouse with one table and one view produced "1 table shape".
  3. `PRAGMA table_info(v)` is refused by the read-only rail, correctly: it
     does not start with SELECT or WITH. The table-valued form
     `SELECT ... FROM pragma_table_info(v)` is a SELECT and passes.
"""
from __future__ import annotations

import pathlib
import sqlite3

import pytest

from context_lift.drift import (
    VIEW_REDEFINED, build_manifest, compare, lines, live_shapes, to_regate,
)
from context_lift.executor import SQLiteExecutor, read_only_violation


class Gold:
    def __init__(self, gold_id, sql):
        self.id, self.gold_sql = gold_id, sql


@pytest.fixture
def warehouse(tmp_path):
    path = tmp_path / "w.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE orders (id INTEGER, status TEXT, amount REAL);
        INSERT INTO orders VALUES (1,'Complete',10.0),(2,'Cancelled',5.0);
        CREATE VIEW good_orders AS
            SELECT id, amount FROM orders WHERE status='Complete';
    """)
    con.commit()
    con.close()
    return path


def replace_view(path, where):
    con = sqlite3.connect(path)
    con.executescript(
        "DROP VIEW good_orders;\n"
        "CREATE VIEW good_orders AS "
        f"SELECT id, amount FROM orders WHERE {where};")
    con.commit()
    con.close()


def test_a_view_gets_a_shape_not_just_a_table(warehouse):
    """Bug 2. `describe_tables` filters sqlite to type='table', so the view --
    the only thing this module is really about -- had no shape and recorded as
    unreadable. A warehouse with one table and one view gave "1 table shape"."""
    shapes = live_shapes(SQLiteExecutor(str(warehouse)))
    assert shapes("orders") is not None
    assert shapes("good_orders") is not None
    assert [name for name, _ in shapes("good_orders").columns] == ["id", "amount"]


def test_the_view_definition_is_hashed(warehouse):
    shapes = live_shapes(SQLiteExecutor(str(warehouse)))
    assert shapes("good_orders").definition_hash
    assert shapes("orders").definition_hash == ""       # a base table has none


def test_the_pragma_form_that_the_read_only_rail_refuses():
    """Bug 3, and the rail is right. Pinned so nobody 'simplifies' it back."""
    assert read_only_violation('PRAGMA table_info("v")') is not None
    assert read_only_violation(
        "SELECT name, type FROM pragma_table_info('v')") is None


def test_rows_are_dicts_not_tuples(warehouse):
    """Bug 1, pinned at the source rather than at my call site."""
    result = SQLiteExecutor(str(warehouse)).execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'view'")
    assert result.ok
    assert isinstance(result.rows[0], dict)
    assert "name" in result.rows[0]


def test_a_view_replaced_with_the_same_columns_is_caught_on_a_real_database(
        warehouse):
    """The whole point, end to end. Same two columns, same types, different
    WHERE. The gold still runs and now answers a different question."""
    golds = [Gold("g1", "SELECT SUM(amount) FROM good_orders"),
             Gold("g2", "SELECT COUNT(*) FROM orders")]
    executor = SQLiteExecutor(str(warehouse))
    manifest = build_manifest(golds, live_shapes(executor))

    # nothing has changed yet
    drifts, unchanged, _ = compare(manifest, live_shapes(executor))
    assert drifts == []
    assert sorted(unchanged) == ["g1", "g2"]

    replace_view(warehouse, "status<>'Nope'")
    drifts, unchanged, _ = compare(manifest,
                                   live_shapes(SQLiteExecutor(str(warehouse))))
    assert [d.category for d in drifts] == [VIEW_REDEFINED]
    assert to_regate(drifts) == ["g1"]
    assert unchanged == ["g2"]         # the gold on the untouched table


def test_only_the_drifted_gold_needs_re_gating(warehouse):
    """The cost argument. `gate` executes gold SQL, so re-running the whole
    set to find the one that moved is the expensive way to learn a cheap
    fact."""
    golds = [Gold(f"g{i}", "SELECT COUNT(*) FROM orders") for i in range(5)]
    golds.append(Gold("gv", "SELECT SUM(amount) FROM good_orders"))
    executor = SQLiteExecutor(str(warehouse))
    manifest = build_manifest(golds, live_shapes(executor))
    replace_view(warehouse, "amount > 0")
    drifts, unchanged, _ = compare(manifest,
                                   live_shapes(SQLiteExecutor(str(warehouse))))
    assert to_regate(drifts) == ["gv"]
    assert len(unchanged) == 5


def test_a_skipped_check_is_reported_rather_than_passing_silently():
    """When the view definitions cannot be read, VIEW_REDEFINED did not run.
    A report that says "nothing moved" without saying so implies something it
    never checked."""
    from context_lift.drift import LiveShapes
    shapes = LiveShapes(shapes={}, skipped=["VIEW_REDEFINED: could not read"])
    text = "\n".join(lines([], [], 0, shapes.skipped))
    assert "SKIPPED" in text
    assert "VIEW_REDEFINED" in text


def test_the_gate_verb_offers_both_manifest_flags():
    from context_lift.cli import _build_parser
    gate = _build_parser()._subparsers._group_actions[0].choices["gate"]
    help_text = gate.format_help()
    assert "--write-manifest" in help_text
    assert "--since" in help_text
    assert gate.get_default("since") is None
    assert gate.get_default("write_manifest") is None
