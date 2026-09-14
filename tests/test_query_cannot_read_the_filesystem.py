# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""A model-written SELECT must not be able to open a file.

THE HOLE, verified before it was closed. DuckDB ships filesystem readers as
ordinary table functions, so

    SELECT content FROM read_text('/etc/passwd')

starts with SELECT, names no write keyword, and passed every check in
`read_only_violation`. Run against `duckdb.connect(db, read_only=True)` it
returned the file's contents. `read_only=True` protects the database FILE from
writes; it says nothing about reads elsewhere on disk, which is not what the
name suggests to most readers.

WHY IT MATTERS IN THIS TOOL RATHER THAN IN GENERAL. Every SQL string executed
here was written by a MODEL, from a prompt assembled out of business context
that people author and that `mine` also reads out of a customer's query log.
A sentence in that context is untrusted input with a filesystem behind it, and
the answer lands in a CSV somebody then reads.

TWO LAYERS, because either alone is not enough:

1. `read_only_violation` refuses the function names. A regex over SQL text
   will always miss a spelling, so this is not the control, it is the message
   -- it gives a person a clear reason instead of an engine error.
2. `SET disabled_filesystems='LocalFileSystem'` on the DuckDB connection.
   This one is authoritative: the engine refuses however the query is
   written.
"""
from __future__ import annotations

import pathlib

import pytest

from context_lift.executor import read_only_violation

SECRET = "TOP-SECRET-VALUE-THAT-MUST-NOT-APPEAR-IN-AN-ANSWER"


# ---------------------------------------------------------------------------
# layer 1: the SQL text check

@pytest.mark.parametrize("sql", [
    "SELECT content FROM read_text('/etc/passwd')",
    "SELECT * FROM read_csv('~/.ssh/id_rsa')",
    "SELECT * FROM read_csv_auto('/tmp/x.csv')",
    "SELECT * FROM read_blob('/etc/shadow')",
    "SELECT * FROM read_json('/tmp/creds.json')",
    "SELECT * FROM read_parquet('/tmp/x.parquet')",
    "WITH x AS (SELECT * FROM READ_PARQUET('/tmp/a')) SELECT * FROM x",
    "SELECT * FROM parquet_scan('/tmp/a')",
    "select * from read_ndjson('/tmp/a')",
])
def test_a_select_that_opens_a_file_is_refused(sql):
    reason = read_only_violation(sql)

    assert reason is not None, f"{sql!r} was allowed"
    assert "reads the filesystem" in reason
    assert "not the database" in reason


def test_the_refusal_names_the_function_so_a_person_can_see_why():
    reason = read_only_violation("SELECT content FROM read_text('/etc/passwd')")

    assert "read_text" in reason


def test_an_ordinary_query_is_still_allowed():
    """The guard is worthless if it refuses real work."""
    assert read_only_violation("SELECT a, b FROM orders WHERE state = 'x'") is None
    assert read_only_violation(
        "WITH t AS (SELECT 1 AS a) SELECT a FROM t") is None


def test_the_function_name_inside_a_string_literal_is_inert():
    """A gold whose WHERE clause happens to contain the text is not an
    attack. Literals are masked before the check for exactly this reason, and
    a guard that fires on them would be refusing valid golds."""
    assert read_only_violation(
        "SELECT * FROM t WHERE note = 'call read_text(x) later'") is None


def test_sqlite_glob_is_not_blocked():
    """`glob` is deliberately absent from the list.

    In DuckDB `glob('/etc/*')` lists the filesystem. In SQLite `glob(X, Y)` is
    the function form of the GLOB pattern operator -- `SELECT glob('a*','abc')`
    returns 1 and is ordinary string matching. `read_only_violation` is shared
    by every backend, so blocking the name would refuse valid SQLite golds to
    close a DuckDB hole. DuckDB's engine setting handles it where it is real.
    """
    assert read_only_violation("SELECT glob('a*', name) FROM t") is None


# ---------------------------------------------------------------------------
# layer 2: the engine, which is the one that actually holds

def test_duckdb_refuses_to_touch_the_filesystem_at_all(tmp_path):
    """THE TEST THAT MATTERS. Goes around layer 1 entirely by calling the
    connection directly, which is what any missed spelling would do."""
    duckdb = pytest.importorskip("duckdb")

    from context_lift.executor import DuckDBExecutor

    database = tmp_path / "t.duckdb"
    setup = duckdb.connect(str(database))
    setup.execute("CREATE TABLE orders(id INTEGER)")
    setup.execute("INSERT INTO orders VALUES (7)")
    setup.close()

    secret = tmp_path / "secret.txt"
    secret.write_text(SECRET)

    executor = DuckDBExecutor(str(database))
    connection = executor._connect()

    # the ordinary query still works, or the guard has broken the tool
    assert connection.execute("SELECT id FROM orders").fetchall() == [(7,)]

    for sql in (f"SELECT content FROM read_text('{secret}')",
                f"SELECT * FROM read_csv('{secret}')",
                f"SELECT * FROM glob('{tmp_path}/*')"):
        with pytest.raises(Exception) as raised:
            connection.execute(sql)
        assert SECRET not in str(raised.value), (
            f"the secret leaked through the error message of {sql!r}")


def test_the_secret_never_reaches_an_answer_through_the_executor(tmp_path):
    """End to end, the way a run would hit it: through execute(), which is
    what the scorer calls."""
    duckdb = pytest.importorskip("duckdb")

    from context_lift.executor import DuckDBExecutor

    database = tmp_path / "t.duckdb"
    setup = duckdb.connect(str(database))
    setup.execute("CREATE TABLE orders(id INTEGER)")
    setup.close()

    secret = tmp_path / "secret.txt"
    secret.write_text(SECRET)

    result = DuckDBExecutor(str(database)).execute(
        f"SELECT content FROM read_text('{secret}')")

    assert result.error, "the query succeeded"
    assert SECRET not in str(result.rows)
    assert SECRET not in result.error


def test_a_read_only_executor_still_refuses_writes(tmp_path):
    """The protection that already existed must not have been traded away."""
    pytest.importorskip("duckdb")

    from context_lift.executor import read_only_violation

    assert read_only_violation("DROP TABLE orders") is not None
    assert read_only_violation("SELECT 1; DROP TABLE orders") is not None
