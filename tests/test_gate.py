# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Behavior tests for the gold-set gate.

Real-query defects run against a temp SQLite DB; UNSTABLE uses a stub executor
that returns different rows each call, so the non-determinism is deterministic
in the test (no flaky RANDOM()).
"""
import sqlite3
import threading
import time

from context_lift.executor import ExecResult, SQLiteExecutor
from context_lift.gate import (
    DEGENERATE,
    EXEC_FAIL,
    NO_ACCESS,
    OVER_BUDGET,
    TABLE_GONE,
    MISSING_LITERAL,
    MISSING_PROJECTION,
    MISSING_ORDER,
    NON_SELECT,
    PLACEHOLDER_COMMENT,
    TRUNCATED,
    UNSTABLE,
    CROSS_PROJECT,
    WHOLE_ROW,
    LIMIT_NO_ORDER,
    ZERO_ROWS,
    filter_literals,
    gate,
    gate_one,
    question_names_order,
    orders_rows,
    missing_projection,
    literal_on_wrong_column,
    filter_pairs,
    missing_literals,
    selects_whole_row,
    limit_without_order,
    summarize,
)


def _seed(tmp_path):
    """Create a 2-row emp table in a temp DB and return its path."""
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE emp(name TEXT, age INT)")
    con.executemany("INSERT INTO emp VALUES (?, ?)", [("ann", 30), ("bob", 40)])
    con.commit()
    con.close()
    return str(p)


def test_a_good_gold_passes_clean(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    f = gate_one("q1", "SELECT name FROM emp", ex)
    assert f.ok is True
    assert f.defects == ()


def test_non_select_gold_is_flagged(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    f = gate_one("q1", "DELETE FROM emp", ex)
    assert f.defects == (NON_SELECT,)


def test_empty_gold_is_a_placeholder(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    # Only a comment, nothing to run.
    f = gate_one("q1", "-- TODO: write this query", ex)
    assert f.defects == (PLACEHOLDER_COMMENT,)


def test_placeholder_marker_is_flagged_even_with_real_sql(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    f = gate_one("q1", "SELECT name FROM emp -- FIXME wrong table?", ex)
    assert PLACEHOLDER_COMMENT in f.defects


def test_gold_that_errors_is_exec_fail(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    f = gate_one("q1", "SELECT nope FROM emp", ex)
    assert f.defects == (EXEC_FAIL,)
    assert "nope" in f.detail or "no such column" in f.detail.lower()


def test_a_call_the_platform_refused_reads_differently_from_broken_sql(tmp_path):
    """A 403 from VPC Service Controls stopped one gold of a 45-gold run
    while the other 41 went through, and the detail read as if the SQL was
    wrong. Nothing about the query is wrong; the org policy is."""

    class _Refusing:
        def execute(self, sql):
            return ExecResult(error="Forbidden: 403 Request is prohibited by organization's "
                                    "policy (VPC Service Controls)")

    refused = gate_one("q1", "SELECT name FROM emp", _Refusing())
    broken = gate_one("q2", "SELECT nope FROM emp", SQLiteExecutor(_seed(tmp_path)))

    assert refused.defects == (NO_ACCESS,)
    assert "the platform refused the call, not the SQL" in refused.detail
    assert "ask whoever owns the org policy" in refused.detail
    assert broken.detail.startswith("the SQL did not run:")
    assert "platform" not in broken.detail


def test_a_gold_that_selects_the_whole_row_is_never_clean(tmp_path):
    """12 of 32 mined golds on one estate were SELECT *. Five failed on the
    column count alone: 151 columns against the agent's 8, 186 against 178,
    56 against 1. Nobody asks a question and wants 151 columns, so the row
    was someone browsing a table. Neither --extra-columns setting helps:
    tolerate accepts a superset, and here the prediction is a subset."""
    executor = SQLiteExecutor(_seed(tmp_path))

    browse = gate_one("q1", "SELECT * FROM emp WHERE age > 20", executor)

    assert browse.defects == (WHOLE_ROW,)
    assert browse.ok is False
    assert "a browse and not a question" in browse.detail


def test_the_whole_row_gold_still_runs_and_is_still_reported(tmp_path):
    """The candidate is written and a person decides. A defect that hides
    the row would take the decision away from the reviewer."""

    class _Recording:
        def __init__(self):
            self.seen = []

        def execute(self, sql):
            self.seen.append(sql)
            return ExecResult(rows=[{"name": "ann", "age": 30}])

    executor = _Recording()

    finding = gate_one("q1", "SELECT * FROM emp", executor)

    assert executor.seen == ["SELECT * FROM emp"]
    assert summarize([finding]) == {WHOLE_ROW: 1}


def test_which_projections_read_as_a_whole_row_and_which_do_not():
    assert selects_whole_row("SELECT * FROM sales.orders WHERE day = '2026-01-01'")
    assert selects_whole_row("select o.* from sales.orders o join sales.items i on o.id = i.id")
    assert selects_whole_row("SELECT DISTINCT * FROM sales.orders")
    # a WITH hides the star from any check that reads the first word
    assert selects_whole_row("WITH recent AS (SELECT id FROM orders) SELECT * FROM recent")
    # the star is the argument of an aggregate, or belongs to a subquery
    assert not selects_whole_row("SELECT COUNT(*) FROM orders")
    assert not selects_whole_row("SELECT n FROM (SELECT COUNT(*) AS n FROM orders) counted")
    assert not selects_whole_row("SELECT id FROM orders WHERE id IN (SELECT * FROM ids)")
    # the columns are written down inside the query, so the answer has a shape
    assert not selects_whole_row("SELECT * FROM (SELECT id, day FROM orders) shaped")
    # multiplication, and a star inside a string, are not projections
    assert not selects_whole_row("SELECT day, SUM(price * quantity) FROM orders GROUP BY day")
    assert not selects_whole_row("SELECT '*' AS star FROM orders")


def test_gold_returning_no_rows_is_zero_rows(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    f = gate_one("q1", "SELECT name FROM emp WHERE age > 999", ex)
    assert f.defects == (ZERO_ROWS,)


def test_empty_aggregate_is_degenerate_not_zero_rows(tmp_path):
    # SUM over an empty filter returns ONE row holding NULL, so ZERO_ROWS would
    # miss it — DEGENERATE is the check that catches a non-answer aggregate.
    ex = SQLiteExecutor(_seed(tmp_path))
    f = gate_one("q1", "SELECT SUM(age) FROM emp WHERE age > 999", ex)
    assert f.defects == (DEGENERATE,)


def test_count_zero_is_degenerate(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    f = gate_one("q1", "SELECT COUNT(*) FROM emp WHERE age > 999", ex)
    assert f.defects == (DEGENERATE,)


def test_row_cap_hit_is_truncated(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path), row_cap=1)
    f = gate_one("q1", "SELECT name FROM emp", ex)
    assert TRUNCATED in f.defects


class _FlakyExecutor:
    """Returns a different single row on each call — a non-deterministic gold."""

    def __init__(self):
        self.calls = 0

    def execute(self, sql):
        self.calls += 1
        return ExecResult(rows=[{"n": self.calls}])


def test_stability_check_flags_a_nondeterministic_gold():
    ex = _FlakyExecutor()
    f = gate_one("q1", "SELECT n FROM t", ex, check_stability=True)
    assert UNSTABLE in f.defects
    assert ex.calls == 2  # ran twice to compare


def test_stability_check_is_off_by_default():
    ex = _FlakyExecutor()
    f = gate_one("q1", "SELECT n FROM t", ex)
    assert UNSTABLE not in f.defects
    assert ex.calls == 1  # ran once, no second run


def test_gate_runs_a_whole_list_and_summarize_counts_defects(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    golds = [
        ("ok", "SELECT name FROM emp"),
        ("bad1", "DELETE FROM emp"),
        ("bad2", "SELECT nope FROM emp"),
    ]
    findings = gate(golds, ex)
    assert [f.ok for f in findings] == [True, False, False]
    assert summarize(findings) == {NON_SELECT: 1, EXEC_FAIL: 1}


def test_a_gold_that_times_out_is_TIMEOUT_not_EXEC_FAIL(tmp_path):
    from context_lift.gate import TIMEOUT

    ex = SQLiteExecutor(_seed(tmp_path), timeout_seconds=0.2)
    f = gate_one(
        "q1",
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n "
        "WHERE i < 100000000) SELECT MAX(i) FROM n",
        ex,
    )
    assert f.defects == (TIMEOUT,)
    assert "did not finish" in f.detail


def test_a_sum_over_a_text_column_is_flagged_because_sqlite_runs_it_clean(tmp_path):
    from context_lift.gate import TEXT_AGGREGATE, gate_one
    path = tmp_path / "plants.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE plants(Name TEXT, Country TEXT, Capacity INT)")
    con.executemany("INSERT INTO plants VALUES (?, ?, ?)",
                    [("A", "US", 100), ("B", "US", 200), ("C", "FR", 50)])
    con.commit()
    con.close()
    executor = SQLiteExecutor(str(path))

    bad = gate_one("q1", "SELECT Country FROM plants GROUP BY Country "
                         "ORDER BY sum(Name) DESC LIMIT 1", executor)
    good = gate_one("q2", "SELECT Country FROM plants GROUP BY Country "
                          "ORDER BY sum(Capacity) DESC LIMIT 1", executor)

    assert TEXT_AGGREGATE in bad.defects
    assert "Name" in bad.detail
    assert TEXT_AGGREGATE not in good.defects


def test_the_text_aggregate_probe_does_not_run_on_a_warehouse():
    """The probe is SQLite SQL; BigQuery refuses SUM over a STRING at dry-run
    time, so such a gold is EXEC_FAIL there already."""
    from context_lift.gate import TEXT_AGGREGATE

    class _Warehouse:
        kind = "bigquery"

        def __init__(self):
            self.seen = []

        def execute(self, sql):
            self.seen.append(sql)
            return ExecResult(rows=[{"Country": "US"}])

    executor = _Warehouse()
    gold = "SELECT Country FROM plants GROUP BY Country ORDER BY sum(Name) DESC LIMIT 1"

    finding = gate_one("q1", gold, executor)

    assert executor.seen == [gold]                       # the gold itself, no probe
    assert TEXT_AGGREGATE not in finding.defects


class _Counting:
    """Wraps a real executor: counts how many statements ran at once and
    sleeps 20 ms so threads overlap."""

    def __init__(self, inner):
        self.inner = inner
        self.in_flight = 0
        self.max_in_flight = 0
        self.lock = threading.Lock()

    def execute(self, sql):
        with self.lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(0.02)
            return self.inner.execute(sql)
        finally:
            with self.lock:
                self.in_flight -= 1


def test_gate_under_workers_keeps_the_input_order_and_runs_at_once(tmp_path):
    executor = _Counting(SQLiteExecutor(_seed(tmp_path)))
    golds = [
        ("ok1", "SELECT name FROM emp"),
        ("bad1", "DELETE FROM emp"),
        ("bad2", "SELECT nope FROM emp"),
        ("ok2", "SELECT age FROM emp"),
    ]

    findings = gate(golds, executor, workers=4)

    assert [f.gold_id for f in findings] == ["ok1", "bad1", "bad2", "ok2"]
    assert [f.ok for f in findings] == [True, False, False, True]
    assert executor.max_in_flight >= 2


def test_the_gate_keeps_each_clean_golds_rows_for_score(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    cache: dict = {}

    gate([("ok", "SELECT name FROM emp"), ("bad", "SELECT nope FROM emp")], ex, gold_cache=cache)

    assert list(cache) == ["SELECT name FROM emp"]           # a failed gold is not an answer
    assert cache["SELECT name FROM emp"].rows == [{"name": "ann"}, {"name": "bob"}]


def test_profile_lists_exact_values_of_category_columns_and_skips_wide_ones(tmp_path):
    from context_lift.profile import profile_values, render_values
    path = tmp_path / "p.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE plants(Name TEXT, Status TEXT, Capacity INT)")
    con.executemany("INSERT INTO plants VALUES (?, ?, ?)", [
        ("A", "Operational", 1), ("B", "Operational", 2), ("C", "Shutdown", 3),
        ("D", "Under Construction", 4)])
    con.commit()
    con.close()

    columns = profile_values(SQLiteExecutor(str(path)), max_values=3)

    assert [(c.column, c.values) for c in columns] == [
        ("Status", ("Operational", "Shutdown", "Under Construction"))]
    text = render_values(columns)
    assert '- plants.Status is one of: "Operational", "Shutdown", "Under Construction"' in text


def test_a_question_that_drops_the_values_its_gold_pins_is_flagged(tmp_path):
    """The measured cause of a field round scoring 0.000 in both arms: 14 of
    25 golds filtered on literals their question never gave, so no agent could
    answer them and the score was a measurement of the answer key."""
    executor = SQLiteExecutor(_seed(tmp_path))

    finding = gate_one("q1", "SELECT name FROM emp WHERE name = 'ann' AND age = 30",
                       executor, question="who is this, for a specific person and age")

    assert MISSING_LITERAL in finding.warnings
    assert finding.defects == ()
    assert finding.ok is True                 # kept: split must still see it
    assert "ann" in finding.detail and "30" in finding.detail
    assert "may leave out 2 values" in finding.detail


def test_a_question_that_names_the_values_passes(tmp_path):
    executor = SQLiteExecutor(_seed(tmp_path))

    finding = gate_one("q1", "SELECT name FROM emp WHERE name = 'ann' AND age = 30",
                       executor, question="is ann 30 years old")

    assert finding.ok is True


def test_a_gold_with_no_question_is_audited_exactly_as_before(tmp_path):
    """A gold list loaded as (id, sql) pairs has no question to check, and an
    empty draft is what a reviewer fills in during review."""
    executor = SQLiteExecutor(_seed(tmp_path))

    assert gate_one("q1", "SELECT name FROM emp WHERE name = 'ann'", executor).ok is True
    assert gate_one("q1", "SELECT name FROM emp WHERE name = 'ann'", executor,
                    question="   ").ok is True


def test_spelling_and_punctuation_do_not_decide_whether_a_value_is_present():
    sql = "SELECT units FROM sales WHERE product_code = 'PRD-1042'"

    assert missing_literals(sql, "units of prd 1042 last week") == []
    assert missing_literals(sql, "units of PRD1042 last week") == []
    assert missing_literals(sql, "units of a specific product") == ["PRD-1042"]


def test_which_literals_count_as_values_the_question_must_give():
    # WHERE, HAVING and the ON of a join, and a value is a value wherever the
    # condition puts it
    assert filter_literals(
        "SELECT r, SUM(x) FROM s JOIN d ON s.id = d.id AND d.kind = 'retail' "
        "WHERE yr = 2025 GROUP BY 1 HAVING SUM(x) > 1000 ORDER BY 2 LIMIT 10"
    ) == ["retail", "2025", "1000"]
    # a row cap is not a value anyone asks for, and neither is a group-by
    # position
    assert filter_literals("SELECT a FROM t GROUP BY 1 LIMIT 100 OFFSET 20") == []
    # a one-digit number is a flag, and it also matches inside any longer
    # number the question happens to carry
    assert filter_literals("SELECT a FROM t WHERE is_deleted = 0") == []
    # '%Y-%m' tells a function how to read a date; '%blue shirt%' is a value
    assert filter_literals(
        "SELECT a FROM t WHERE FORMAT_DATE('%Y-%m', day) = '2024-01'") == ["2024-01"]
    assert filter_literals("SELECT a FROM t WHERE name LIKE '%blue shirt%'") == ["%blue shirt%"]
    # a clause word inside a value does not end the condition: 'X99' used to
    # be lost because "from" inside the quotes read as the next clause
    assert filter_literals(
        "SELECT a FROM t WHERE note = 'sent from lab' AND code = 'X99'"
    ) == ["sent from lab", "X99"]
    # a commented-out filter is not applied, so its values are not asked for
    assert filter_literals("SELECT a FROM t -- WHERE code = 'HIDDEN'\nWHERE live = 'yes'") == ["yes"]


def test_a_capped_gold_with_no_order_has_no_fixed_answer(tmp_path):
    """`LIMIT 3` with nothing deciding which 3 is not an answer key. The
    warehouse may hand back any three rows that satisfy the query, so two
    runs of the same statement can key against different rows."""
    executor = SQLiteExecutor(_seed(tmp_path))

    capped = gate_one("q1", "SELECT name FROM emp LIMIT 3", executor)

    assert capped.defects == (LIMIT_NO_ORDER,)
    assert capped.ok is False
    assert "another run can return a different set" in capped.detail


def test_which_row_caps_have_a_fixed_answer_and_which_do_not():
    assert limit_without_order("SELECT name FROM emp LIMIT 3")
    assert limit_without_order("select id from orders limit 10 offset 5")
    # the ORDER BY belongs to a window frame, so it does not order the output
    assert limit_without_order(
        "SELECT id, ROW_NUMBER() OVER (ORDER BY day) FROM orders LIMIT 5")
    # an ORDER BY inside the CTE does not decide the rows the outer query caps
    assert limit_without_order(
        "WITH ranked AS (SELECT id FROM orders ORDER BY day) SELECT id FROM ranked LIMIT 5")
    # ordered, so the same rows come back every time
    assert not limit_without_order("SELECT name FROM emp ORDER BY age DESC LIMIT 3")
    # no cap at all: every matching row comes back, in any order
    assert not limit_without_order("SELECT name FROM emp WHERE age > 20")
    # the word appears inside a string, not as a clause
    assert not limit_without_order("SELECT id FROM orders WHERE note = 'no limit here'")


def test_the_capped_gold_still_runs_and_still_reaches_the_reviewer(tmp_path):
    """Same rule as WHOLE_ROW: the gate names the defect, the person decides."""
    executor = SQLiteExecutor(_seed(tmp_path))

    finding = gate_one("q1", "SELECT name FROM emp LIMIT 2", executor)

    assert summarize([finding]) == {LIMIT_NO_ORDER: 1}


def test_a_gold_naming_the_data_project_is_not_cross_project():
    """`bq://billing/data.sales` bills one project and reads tables in
    another. A gold spelling its own table `data.sales.orders` is correct.
    Comparing against the billing project alone flagged every such gold, which
    is every estate that keeps billing separate from storage."""

    class SplitBilling:
        kind = "bigquery"
        project = "billing-project"
        dataset = "data-project.sales"

        def execute(self, sql):
            return ExecResult(rows=[{"a": 1}])

    finding = gate_one("q1", "SELECT a FROM `data-project.sales.orders`", SplitBilling())

    assert finding.defects == ()
    assert finding.ok is True


def test_a_gold_naming_a_project_the_run_reads_neither_is_still_flagged():
    """The check still has to catch the real case it was written for."""

    class SplitBilling:
        kind = "bigquery"
        project = "billing-project"
        dataset = "data-project.sales"

        def execute(self, sql):
            return ExecResult(rows=[{"a": 1}])

    finding = gate_one("q1", "SELECT a FROM `someone-else.sales.orders`", SplitBilling())

    assert finding.defects == (CROSS_PROJECT,)
    assert "someone-else" in finding.detail
    # The tool already knows the way out and has to say it. Without this
    # sentence the check flagged 26 of 26 thelook golds, produced no clean set
    # at all, and named no way forward, while `--db bq://BILLING/DATA.DATASET`
    # fixed it with no code change.
    assert "--db bq://billing-project/someone-else.DATASET" in finding.detail


def test_a_question_that_gives_the_value_in_another_spelling_keeps_the_gold(tmp_path):
    """Five of the eight golds this check flagged on the published BEAVER human
    key are questions that DO give the value, in another surface form. A person
    wrote every one of them and a benchmark shipped them.

    The check still FIRES on all five, and that is deliberate. Loosening the
    matcher enough to read "Summer Haynes" as 'Haynes, Summer' risks silencing
    the three it is right about. What this asserts is the thing that matters:
    the gold is kept, `ok` stays true, and `split` still sees it. The warning
    is a note for the reviewer, not a verdict."""
    path = tmp_path / "people.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE people(name TEXT, dept TEXT, effort REAL)")
    con.executemany("INSERT INTO people VALUES (?, ?, ?)", [
        ("Haynes, Summer", "Electrical Eng & Computer Sci", 0.9),
        ("Hopkins, Ayden", "Electrical Engineering-Computer Science", 0.5)])
    con.commit()
    con.close()
    executor = SQLiteExecutor(str(path))
    same_value_other_spelling = [
        ("Professor Summer Haynes's office",
         "SELECT dept FROM people WHERE name = 'Haynes, Summer'"),
        ("Professor Ayden Hopkins",
         "SELECT dept FROM people WHERE name = 'Hopkins, Ayden'"),
        ("in Electrical Engineering and Computer Science",
         "SELECT name FROM people WHERE dept = 'Electrical Eng & Computer Sci'"),
        ("in Electrical Engineering and Computer Science",
         "SELECT name FROM people WHERE dept = 'Electrical Engineering-Computer Science'"),
        ("staff over 75% effort", "SELECT name FROM people WHERE effort > 0.75"),
    ]

    for question, gold_sql in same_value_other_spelling:
        finding = gate_one("q1", gold_sql, executor, question=question)
        assert finding.ok is True, question
        assert finding.defects == (), question
        assert MISSING_LITERAL in finding.warnings, question    # it fires, and that is fine


def test_a_question_that_really_omits_the_value_still_warns(tmp_path):
    """The three cases on the same key where the check is right must keep
    firing. Easy to lose when a matcher is loosened: 'candle-duo-users' behind
    "the mailing list about duo users" is unguessable, a role string
    'Activity leader' behind "supervisor" is a real vocabulary gap, and a gold
    filtering `level IN ('3','4','5','6')` under a question that names neither
    is unanswerable. That is the whole point of the check."""
    path = tmp_path / "lists.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE lists(list_name TEXT, owner TEXT)")
    con.execute("INSERT INTO lists VALUES ('candle-duo-users', 'ann')")
    con.commit()
    con.close()
    executor = SQLiteExecutor(str(path))

    finding = gate_one("q1", "SELECT owner FROM lists WHERE list_name = 'candle-duo-users'",
                       executor, question="Regarding the mailing list about duo users")

    assert MISSING_LITERAL in finding.warnings
    assert finding.ok is True

    # the third: a question naming the values it excludes and not the ones it
    # includes. It reads as covered because two of the four numbers are there
    levels = missing_literals(
        "SELECT owner FROM lists WHERE level IN ('3','4','5','6') "
        "AND org_id NOT IN ('139','250')",
        "Organizations according to their level. Exclude organizations '139' and '250'")
    assert levels == ["3", "4", "5", "6"]


def test_a_case_under_a_subquery_is_not_read_as_a_row_filter():
    """A scalar subquery in the SELECT list carries its own WHERE, and that
    span used to run past the subquery's closing parenthesis and through the
    outer projection, so a CASE picking a label was read as row filters.

    This SQL is the only witness. It was written from a report naming a real
    gold, and that report was withdrawn - the values there came from a genuine
    WHERE and the check was right about them. The shape below is reachable and
    the guard is correct for it, but nobody has met it on field data."""
    sql = ("SELECT a, (SELECT t2.x FROM t2 WHERE t2.x = t.x) AS s, "
           "CASE WHEN lvl = '4' THEN a WHEN lvl = '5' THEN b ELSE '6' END "
           "FROM t WHERE id IN ('139','250')")

    assert filter_literals(sql) == ["139", "250"]


def test_a_regex_pattern_is_not_a_value_the_question_must_give():
    """`^[\\x00-\\x7F]+$` means "ASCII only". It tells a function how to match,
    like the date format already excluded, and no question would state it. One
    customer gold was flagged for it, and REGEXP_CONTAINS in a WHERE is
    ordinary on BigQuery."""
    sql = ("SELECT a FROM t WHERE REGEXP_CONTAINS(name, r'^[\\x00-\\x7F]+$') "
           "AND code = 'PG01'")

    assert filter_literals(sql) == ["PG01"]


def test_a_like_pattern_is_still_a_value():
    """Only REGEXP_* arguments are excluded, by function name. A LIKE pattern
    carries the words a question would use, and matching on the SHAPE of a
    value would drop business values that contain a bracket or a caret."""
    assert filter_literals("SELECT a FROM t WHERE d LIKE '%blue shirt%'") == ["%blue shirt%"]


def test_a_gold_that_orders_its_rows_under_a_question_that_does_not():
    """The drafter never writes an ordering phrase: 0 of 6 ordered golds on one
    customer key, 2 of 22 on a mined benchmark arm, against 11 of 22 on the
    human arm of the same benchmark. An agent that has to guess the ORDER BY
    returns the right rows in the wrong order, or the wrong rows entirely."""
    assert orders_rows("SELECT name FROM emp ORDER BY age DESC")
    # an ORDER BY inside a window frame does not order what the caller gets
    assert not orders_rows("SELECT ROW_NUMBER() OVER (ORDER BY age) FROM emp")
    assert not orders_rows("SELECT name FROM emp")

    assert question_names_order("the top 5 departments by headcount")
    assert question_names_order("who is the highest paid")
    assert not question_names_order("how many people are in each department")


def test_the_answers_columns_the_question_never_names():
    """Measured on 62 BEAVER golds, 348 projected columns: a human question
    names 76% of them, a question drafted from the SQL names 26%."""
    gold = "SELECT id, order_date, SUM(amount) AS total FROM orders GROUP BY 1, 2"

    assert missing_projection(gold, "what is the total by order date?") == ["id"]
    assert missing_projection(gold, "the id and total for each order date") == []
    # a column is named when every part of its name appears, in any spelling
    assert missing_projection("SELECT order_date FROM t", "the date of the order") == []
    # `*` is not a name; WHOLE_ROW already covers a browse
    assert missing_projection("SELECT * FROM orders", "show the orders") == []


def test_a_value_the_question_gives_against_the_wrong_column():
    """MISSING_LITERAL passes these: the value IS in the question. A gold
    filters MOIRA_LIST_OWNER_KEY under a question reading "for owner
    'LIST69...'", so the agent wrote WHERE OWNER = and got zero rows."""
    gold = "SELECT a FROM t WHERE MOIRA_LIST_OWNER_KEY = 'LIST69.377'"

    assert literal_on_wrong_column(gold, "for owner 'LIST69.377'") == [
        ("MOIRA_LIST_OWNER_KEY", "LIST69.377")]
    # the question says which identifier it means
    assert literal_on_wrong_column(gold, "for owner key 'LIST69.377'") == []
    # the value is not given at all: that is the other check's case
    assert literal_on_wrong_column(gold, "for a specific owner") == []
    # not an identifier column, so the rule does not apply
    assert literal_on_wrong_column(
        "SELECT a FROM t WHERE city = 'Rome'", "orders in Rome") == []
    # a question naming NO column is not this defect: it leaves the agent one
    # candidate, where "for owner LIST69" sends it to a column called OWNER
    assert literal_on_wrong_column(
        "SELECT a FROM t WHERE product_code = 'PRD-1042'", "units of PRD-1042") == []
    assert literal_on_wrong_column(
        "SELECT a FROM t WHERE product_code = 'PRD-1042'", "the product PRD-1042") == [
            ("product_code", "PRD-1042")]


def test_the_column_belonging_to_each_filter_value():
    """The pairing the wrong-column check needs. A value with nothing readable
    in front of it gets an empty column rather than a guess."""
    assert filter_pairs("SELECT a FROM t WHERE pg = 'PG01' AND pc IN ('PC9','PC7')") == [
        ("pg", "PG01"), ("pc", "PC9"), ("", "PC7")]
    assert filter_pairs("SELECT a FROM t WHERE t2.code = 'X12'") == [("code", "X12")]
    assert filter_pairs("SELECT a FROM t WHERE percent_effort > 0.75") == [
        ("percent_effort", "0.75")]


def test_the_three_new_checks_never_drop_a_gold(tmp_path):
    """All three are warnings. MISSING_ORDER fires on 11 of 22 ordered golds in
    a published human key, so a gate that rejected on it would reject half an
    academic benchmark."""
    executor = SQLiteExecutor(_seed(tmp_path))

    finding = gate_one("q1", "SELECT name, age FROM emp ORDER BY age DESC", executor,
                       question="who is in the table?")

    assert finding.ok is True
    assert finding.defects == ()
    assert set(finding.warnings) == {MISSING_ORDER, MISSING_PROJECTION}


def test_a_short_code_hiding_inside_an_english_word():
    """The gap the whole tool exists to find, and the worst kind to miss: the
    estate stores a two-letter code and the business says the word, so a
    checker reading the word as containing the code sees nothing wrong.

    Three shapes from one customer key, each of which looked answerable and
    none of which was. The second was written up as answerable by a person
    reading "active stores" as giving 'AC', which is the same mistake the code
    was making."""
    assert missing_literals(
        "SELECT units FROM plan WHERE region = 'LS' AND active_flag = 'Y'",
        "Planned and actual sales units by region for Australia") == ["LS", "Y"]

    assert missing_literals(
        "SELECT COUNT(*) FROM stores WHERE store_status = 'AC'",
        "How many active stores are in each region, country, and sub-channel "
        "in Europe?") == ["AC"]

    assert missing_literals(
        "SELECT cost FROM material WHERE country = 'CA' AND id = 'MAT-00042'",
        "What is the standard cost of material MAT-00042 in Canada?") == ["CA"]


def test_a_short_code_the_question_really_does_give():
    """The rule must not report a code the question states as its own word."""
    assert missing_literals(
        "SELECT COUNT(*) FROM stores WHERE store_status = 'AC'",
        "how many stores have status AC?") == []
    assert missing_literals(
        "SELECT cost FROM material WHERE country = 'CA'",
        "the standard cost in CA") == []


def test_the_reason_a_gold_was_dropped_comes_first(tmp_path):
    """A cold run met `adv003  ZERO_ROWS  <90 words of advice>; returns no
    rows`. The line's whole job is to say why the gold was dropped, and the
    warnings added later were pushing that to the end. A warning is longer
    than a defect note by nature, so order is the fix, not length."""
    executor = SQLiteExecutor(_seed(tmp_path))

    finding = gate_one("q1", "SELECT name, age FROM emp WHERE age > 999 ORDER BY age",
                       executor, question="who is in the table?")

    assert finding.defects == (ZERO_ROWS,)
    assert finding.detail.startswith("returns no rows")
    # the advice is still there, after the reason
    assert MISSING_ORDER in finding.warnings
    assert "guess the ORDER BY" in finding.detail


def test_a_gold_naming_a_project_the_agent_can_read_is_not_cross_project():
    """A real estate spans several projects and --db names exactly one.

    On one warehouse, 221 mined golds named a production project and 132
    named pre-production. Pointing --db at either made the other
    cross-project: 191 of 400 candidates flagged for this alone, and the only
    way through was waiving the defect by hand outside the tool.
    """
    from context_lift.gate import EXTRA_DATA_PROJECTS, set_data_projects

    try:
        set_data_projects(["acme-analytics-p", " ACME-ANALYTICS-PP "])
        assert EXTRA_DATA_PROJECTS == {"acme-analytics-p", "acme-analytics-pp"}, (
            "names are lowercased and trimmed, because a config file has spaces")
    finally:
        set_data_projects([])


def test_naming_no_extra_projects_leaves_the_check_as_it_was():
    """The default must stay strict: a gold naming a project nobody said the
    agent reads really is keying against another copy of the data."""
    from context_lift.gate import EXTRA_DATA_PROJECTS, set_data_projects

    set_data_projects([])
    assert EXTRA_DATA_PROJECTS == set()


def test_the_data_projects_flag_actually_reaches_the_check():
    """It shipped once parsed-and-ignored. The argument existed, the gate
    still flagged 191 of 400 golds, and only reading the defect counts caught
    it. This asserts the wiring, not the parsing."""
    import argparse

    from context_lift.cli import _cmd_gate  # noqa: F401  - imported for the wiring
    from context_lift.gate import EXTRA_DATA_PROJECTS, set_data_projects

    source = __import__("inspect").getsource(_cmd_gate)
    try:
        assert "set_data_projects" in source, (
            "the gate command must pass --data-projects to the gate module")
        assert source.index("set_data_projects") < source.index("_executor_for"), (
            "it has to be set before the first gold is audited")
    finally:
        set_data_projects([])
        assert EXTRA_DATA_PROJECTS == set()


def test_mine_also_passes_data_projects_to_the_gate():
    """`mine` runs the gold gate itself, so the flag has to reach it there
    too. Adding it only to `gate` left the ordinary path - mine, review,
    split, round - still flagging 191 of 400 golds."""
    from context_lift.cli import _cmd_mine

    source = __import__("inspect").getsource(_cmd_mine)

    assert "set_data_projects" in source


def test_a_byte_ceiling_refusal_is_OVER_BUDGET_not_EXEC_FAIL():
    """112 of 179 candidates labelled EXEC_FAIL on one estate were this: the
    dry run said the query scans more than --max-bytes, so nothing ran. The
    SQL is fine and a reviewer reading EXEC_FAIL went looking for a SQL bug
    that was not there."""

    class _OverCeiling:
        project = "p"

        def execute(self, sql):
            return ExecResult(error="rejected: the query would scan more than the 5 GB "
                                    "ceiling (dry-run: 41.20 GB); narrow it or raise "
                                    "--max-bytes")

    found = gate_one("q1", "SELECT name FROM emp", _OverCeiling())
    assert found.defects == (OVER_BUDGET,)
    assert "raise --max-bytes" in found.detail
    assert "41.20 GB" in found.detail, "the figure to raise the ceiling to must survive"
    # the class must be able NOT to fire: the same executor answering normally
    # leaves a clean gold, so this is not a check that always passes
    class _Fine(_OverCeiling):
        def execute(self, sql):
            return ExecResult(rows=[{"name": "a"}])

    assert OVER_BUDGET not in gate_one("q2", "SELECT name FROM emp", _Fine()).defects


def test_a_dropped_table_is_TABLE_GONE_not_EXEC_FAIL():
    """One 404 on a dropped table stopped a 136-call round dead, and the
    label said the analyst's SQL was broken. It was right when it was
    written. No ceiling and no grant brings the table back."""

    class _Missing:
        project = "p"

        def execute(self, sql):
            return ExecResult(error="NotFound: 404 Not found: Table "
                                    "acme-analytics-pp:ba_test.dim_x was not found in "
                                    "location US")

    found = gate_one("q1", "SELECT name FROM emp", _Missing())
    assert found.defects == (TABLE_GONE,)
    assert "not there" in found.detail
    assert "not a SQL bug" in found.detail


def test_broken_sql_is_still_EXEC_FAIL_after_the_split(tmp_path):
    """The three new classes must take rows OUT of EXEC_FAIL and nothing
    else. A syntax error is the query's author's problem and stays theirs."""
    found = gate_one("q1", "SELECT nope FROM emp", SQLiteExecutor(_seed(tmp_path)))
    assert found.defects == (EXEC_FAIL,)
    assert found.detail.startswith("the SQL did not run:")


# --- a business rule and a question parameter are not one warning ------------

def test_a_code_is_a_rule_and_a_date_is_a_parameter():
    """The split that decides whether MISSING_LITERAL is worth acting on.

    With `mine --rules-in-context` the question deliberately omits the values
    the gold filters on, because stating the business rule hands it to the
    bare arm for free. That is right for a CODE and fatal for a PARAMETER:
    no written context can tell an agent which month the person meant, so
    such a gold scores wrong in both arms and dilutes the lift.

    All five values below are real, from one estate's golds.
    """
    from context_lift.gate import parameter_literals
    sql = ("SELECT * FROM t WHERE company_code = '1000' "
           "AND fiscal_period = '2026007' AND ship_date = '2026-04-01' "
           "AND channel = 'ONLINE' AND order_number = '64891778'")
    got = parameter_literals(sql, ["1000", "2026007", "2026-04-01",
                                   "ONLINE", "64891778"])
    assert set(got) == {"2026007", "2026-04-01", "64891778"}
    # and the two that context SHOULD supply are not called parameters
    assert "1000" not in got and "ONLINE" not in got


def test_the_column_decides_when_the_value_shape_cannot():
    """`company_code = '1000'` and `fiscal_period = '2026007'` are both short
    digit strings. Only the column name tells them apart, which is why the
    column is read rather than the value guessed."""
    from context_lift.gate import parameter_literals
    assert parameter_literals("WHERE brand_code = '30'", ["30"]) == []
    assert parameter_literals("WHERE snapshot_month = '2026003'",
                              ["2026003"]) == ["2026003"]


def test_an_unknown_value_is_treated_as_a_rule():
    """Being wrong is cheap on the rule side and expensive on the other.
    Calling a rule a parameter sends a reviewer to fix a gold that was fine."""
    from context_lift.gate import parameter_literals
    assert parameter_literals("WHERE something = 'MYSTERY'", ["MYSTERY"]) == []


def test_a_missing_code_warns_and_a_missing_parameter_refuses():
    from context_lift.gate import gate_one
    from context_lift.executor import ExecResult

    class Fake:
        def execute(self, sql):
            return ExecResult(rows=[{"n": 1}])

    rule_only = gate_one(
        "g1",
        "SELECT region, SUM(units) FROM t JOIN d USING (k) "
        "WHERE company_code = '1000' GROUP BY 1",
        Fake(), question="units by region for Chile")
    assert "business CODES" in rule_only.detail
    assert "deliberate" in rule_only.detail

    has_parameter = gate_one(
        "g2",
        "SELECT region, SUM(units) FROM t JOIN d USING (k) "
        "WHERE ship_date = '2026-04-01' GROUP BY 1",
        Fake(), question="units by region")
    assert "PARAMETERS" in has_parameter.detail
    assert "only dilutes a lift" in has_parameter.detail
    # and it is a DEFECT, not a warning: nothing can supply the value, so the
    # gold is wrong in both arms in every round anybody will ever run
    assert not has_parameter.ok
    assert "MISSING_PARAMETER" in has_parameter.classes


def test_a_sentinel_date_is_not_a_question_parameter():
    """`BETWEEN '1900-01-01' AND '9999-12-31'` is an open range written as a
    closed one. An agent that writes no date filter returns the same rows, so
    a question omitting these is still answerable.

    Found by disagreement: the classifier flagged one of these on a real gold
    and the person reviewing had not. The person was right.
    """
    from context_lift.gate import parameter_literals
    sql = "WHERE valid_from BETWEEN '1900-01-01' AND '9999-12-31'"
    assert parameter_literals(sql, ["1900-01-01", "9999-12-31"]) == []
    # a real bound on the same column is still a parameter
    assert parameter_literals("WHERE valid_from >= '2026-04-01'",
                              ["2026-04-01"]) == ["2026-04-01"]
