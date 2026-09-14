# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Bounding a re-score's payoff before paying for one.

Task #29 asked for the 86 paid answers to be re-scored under both column
rules. Re-scoring is NOT free: the saved rows carry `pred_sql` and no result
rows, and the column rule compares result COLUMNS, so scoring means
re-executing every gold and every prediction against a customer's warehouse.

`compare()` reaches MATCH_EXTRA_COLUMNS only when the row counts are equal
AND the prediction is wider. The second is necessary and readable from the
SQL text, so anything narrower or equal can never change, whatever the data
holds. That makes a free bound possible.
"""
from __future__ import annotations

import csv
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from column_rule_bound import outer_select_star, select_width  # noqa: E402


@pytest.mark.parametrize("sql,width", [
    ("SELECT a FROM t", 1),
    # A bare star is unknown; a star as a function ARGUMENT is not. Treating
    # any `*` as unknown put 22 of 68 real answers in the unknown bucket.
    ("SELECT COUNT(*) FROM t", 1),
    ("SELECT COUNT(*), a FROM t", 2),
    ("SELECT a * b FROM t", 1),
    ("SELECT a, b FROM t", 2),
    ("SELECT a, b, c FROM t", 3),
    ("SELECT COUNT(*), SUM(x) FROM t", 2),          # commas inside a call
    ("SELECT f(a, b), c FROM t", 2),
    ("SELECT CAST(a AS INT64), b, c FROM t", 3),
    ("select   a ,  b   from t", 2),                # spacing and case
])
def test_it_counts_the_select_list(sql, width):
    assert select_width(sql) == width


@pytest.mark.parametrize("sql", [
    "SELECT * FROM t",                 # width depends on the table
    "SELECT DISTINCT * FROM t",
    "UPDATE t SET a = 1",              # no SELECT..FROM
    "",
    "   ",
    "SELECT a, (b FROM t",             # unbalanced inside the select list
])
def test_it_returns_none_rather_than_guessing(sql):
    """A guess here turns a bound into a number nobody should trust."""
    assert select_width(sql) is None


def test_a_cte_is_measured_by_its_final_select():
    """With a CTE the last top-level select shapes the result, and that is the
    one whose width matters."""
    sql = "WITH x AS (SELECT a, b, c FROM t) SELECT a, b FROM x"
    assert select_width(sql) == 2


def test_a_comma_inside_a_string_literal_is_not_a_column_break():
    """Literals are masked first, the same way the read-only rail does it. A
    real gold once broke a different check on a semicolon inside quotes."""
    assert select_width("SELECT 'a, b' AS label, x FROM t") == 2


@pytest.mark.parametrize("sql,outer", [
    ("SELECT * FROM t", True),
    ("SELECT a FROM (SELECT * FROM t)", False),
    ("SELECT a FROM (SELECT * FROM t) WHERE a > 1", False),
    ("WITH c AS (SELECT * FROM t) SELECT a FROM c", False),
    ("SELECT a FROM t", False),
])
def test_it_tells_an_outer_select_star_from_a_subquery_one(sql, outer):
    """`gate` refuses a gold that hands back the whole row. Three saved golds
    held `SELECT *` and looked like escapes; all three had it inside a
    subquery, which is ordinary SQL. Distinguishing them is what turns "three
    suspicious golds" into "the gate is right"."""
    assert outer_select_star(sql) is outer


def test_it_runs_end_to_end_and_names_the_arm_difference(tmp_path):
    """The number that decides the whole question. A round measures a LIFT --
    the difference between two arms -- so a change landing equally on both
    arms cancels. The payoff is the GAP between the arms' counts, not the
    total."""
    rows = tmp_path / "rows.csv"
    with open(rows, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "id", "arm_id", "verdict", "gold_sql", "pred_sql"])
        writer.writeheader()
        # four questions; the wider prediction lands on BOTH arms equally,
        # so the lift cannot move at all
        for question in ("q1", "q2"):
            for arm in ("control", "treatment"):
                writer.writerow({"id": question, "arm_id": arm,
                                 "verdict": "MISMATCH",
                                 "gold_sql": "SELECT a FROM t",
                                 "pred_sql": "SELECT a, b FROM t"})
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "column_rule_bound.py"),
         "--rows", str(rows)],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "COULD change" in result.stdout
    assert "4" in [w for w in result.stdout.split("COULD change")[1].split()][:1]
    assert "the arms differ by 0 answer(s)" in result.stdout
    assert "cancels in the difference" in result.stdout
    assert "no gold has SELECT * as its outer select" in result.stdout


def test_it_reports_a_real_outer_star_as_a_gate_escape(tmp_path):
    """The warning must be able to fire, or it is decoration."""
    rows = tmp_path / "rows.csv"
    with open(rows, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "id", "arm_id", "verdict", "gold_sql", "pred_sql"])
        writer.writeheader()
        writer.writerow({"id": "q1", "arm_id": "a", "verdict": "MATCH",
                         "gold_sql": "SELECT * FROM t",
                         "pred_sql": "SELECT * FROM t"})
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "column_rule_bound.py"),
         "--rows", str(rows)],
        capture_output=True, text=True, timeout=120)
    assert "WARNING: 1 gold(s) have SELECT * as the OUTER select" in result.stdout
