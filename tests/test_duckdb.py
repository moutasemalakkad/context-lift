# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""DuckDB, the third backend.

Skipped when duckdb is not installed, because it is an optional extra and
the core must stay dependency-free.
"""
import pytest

duckdb = pytest.importorskip("duckdb")

from context_lift.backends_db import make_executor, parse_db_spec  # noqa: E402


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "demo.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE order_items (month TEXT, price DOUBLE, status TEXT)")
    connection.execute("INSERT INTO order_items VALUES "
                       "('2024-01', 100.0, 'Complete'), "
                       "('2024-01', 40.0, 'Cancelled'), "
                       "('2024-02', 200.0, 'Complete')")
    connection.close()
    return f"duckdb://{path}"


def test_a_duckdb_spec_is_a_prefix_not_a_file_extension(tmp_path):
    """`.duckdb` is only a convention and a database may be called anything,
    so guessing from the name would send some files to the wrong reader with
    no error at all."""
    assert parse_db_spec("duckdb:///tmp/x.db").kind == "duckdb"
    assert parse_db_spec("/tmp/x.duckdb").kind == "sqlite"


def test_it_reads_rows_as_dicts_like_the_other_backends(database):
    """Same ExecResult shape, so nothing downstream knows which backend ran."""
    result = make_executor(database).execute(
        "SELECT month, SUM(price) AS revenue FROM order_items "
        "WHERE status <> 'Cancelled' GROUP BY month ORDER BY month")

    assert result.error is None
    assert result.rows == [{"month": "2024-01", "revenue": 100.0},
                           {"month": "2024-02", "revenue": 200.0}]


def test_a_write_is_refused_before_it_reaches_the_database(database):
    """Predicted SQL is untrusted model output. The read-only check is the
    same one SQLite and BigQuery use, not a second copy."""
    result = make_executor(database).execute("DROP TABLE order_items")

    assert result.error and result.rows == []
    assert make_executor(database).execute(
        "SELECT COUNT(*) AS n FROM order_items").rows == [{"n": 3}]


def test_the_row_cap_says_whether_it_actually_truncated(database):
    """A cut result is an unknown answer, not a wrong one, so the caller has
    to be able to tell a cap that fired from one that did not."""
    capped = make_executor(database, row_cap=2).execute(
        "SELECT * FROM order_items")
    uncapped = make_executor(database, row_cap=99).execute(
        "SELECT * FROM order_items")

    assert len(capped.rows) == 2 and capped.truncated
    assert len(uncapped.rows) == 3 and not uncapped.truncated


def test_a_bad_query_is_an_error_string_not_a_crash(database):
    """A bad gold or predicted query becomes EXEC_FAIL and the run moves on."""
    result = make_executor(database).execute("SELECT nope FROM order_items")

    assert result.error and "nope" in result.error


def test_check_plans_without_executing(database):
    """DuckDB's EXPLAIN is the free equivalent of a warehouse dry run."""
    executor = make_executor(database)

    assert executor.check("SELECT * FROM order_items") is None
    assert executor.check("SELECT * FROM no_such_table") is not None
