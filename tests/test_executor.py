# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Behavior tests for the SQLite backend and the read-only guard.

BigQueryExecutor needs credentials, so it's out of the fast path; these cover
the stdlib backend and the safety rail that protects every backend. The one
BigQuery test here fakes the dry-run and checks the estimate's arithmetic.
"""
import sqlite3

from context_lift.executor import (
    BigQueryExecutor,
    ExecResult,
    SQLiteExecutor,
    bigquery_usd,
    NOT_READ_ONLY,
    is_read_only,
    read_only_violation,
    strip_comments_and_mask_literals,
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


def test_read_only_accepts_select_and_with():
    assert is_read_only("SELECT 1") is True
    assert is_read_only("WITH t AS (SELECT 1 AS x) SELECT x FROM t") is True


def test_read_only_rejects_data_modifying_statements():
    assert is_read_only("DELETE FROM emp") is False
    assert is_read_only("DROP TABLE emp") is False
    assert is_read_only("UPDATE emp SET age = 0") is False
    assert is_read_only("INSERT INTO emp VALUES ('x', 1)") is False


def test_read_only_rejects_a_second_statement():
    # The classic "SELECT then sneak a write" gets caught by the semicolon.
    assert is_read_only("SELECT 1; DROP TABLE emp") is False


def test_read_only_ignores_keywords_inside_comments():
    # A DROP in a comment is stripped, so the query is still read-only.
    assert is_read_only("-- cleanup DROP TABLE emp\nSELECT 1") is True


def test_a_federated_query_is_rejected_by_name_while_the_literal_alone_is_inert():
    # The DELETE sits inside a string literal, which the rail masks. On its
    # own that is right: a string is text. Inside EXTERNAL_QUERY the same
    # string is a statement run on another database, so the call is refused.
    assert (read_only_violation("SELECT * FROM EXTERNAL_QUERY('conn', 'DELETE FROM t')")
            == "rejected: reaches outside the warehouse (EXTERNAL_QUERY)")
    assert read_only_violation("SELECT 'DELETE FROM t' AS note") is None


def test_remote_model_calls_are_rejected_by_name_and_ml_predict_stays_allowed():
    assert (read_only_violation("SELECT * FROM ML.GENERATE_TEXT(MODEL `p.d.m`, TABLE t)")
            == "rejected: reaches outside the warehouse (ML.GENERATE_TEXT)")
    assert (read_only_violation("SELECT AI.GENERATE_BOOL(('is this spam?', body)) FROM mail")
            == "rejected: reaches outside the warehouse (AI.GENERATE_BOOL)")
    # ML.PREDICT over a model trained in the warehouse never leaves it, and
    # ML.PREDICT over a remote model reads the same by text; the rail cannot
    # tell them apart, so it allows the call and IAM (no
    # bigquery.connections.use) is the control for the remote case.
    assert read_only_violation("SELECT * FROM ML.PREDICT(MODEL `p.d.m`, TABLE t)") is None


def test_outside_call_names_in_strings_comments_and_column_names_are_ignored():
    assert read_only_violation("SELECT 'EXTERNAL_QUERY(' AS s FROM t") is None
    assert read_only_violation("-- was EXTERNAL_QUERY(...)\nSELECT 1") is None
    assert read_only_violation("/* AI.GENERATE_TEXT( */ SELECT 1") is None
    assert read_only_violation("SELECT external_query_count FROM stats") is None
    assert read_only_violation("SELECT ai.generate_total FROM accounts AS ai") is None
    assert read_only_violation("SELECT SUM(ai.generate_total) FROM accounts AS ai") is None


def test_is_read_only_is_the_boolean_view_of_the_violation():
    assert is_read_only("SELECT * FROM EXTERNAL_QUERY('c', 'SELECT 1')") is False
    assert read_only_violation("DROP TABLE emp") == "rejected: statement is not read-only"
    assert read_only_violation("") == "rejected: statement is not read-only"


def test_the_executor_reports_the_outside_call_by_name(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    res = ex.execute("SELECT * FROM EXTERNAL_QUERY('conn', 'SELECT 1')")
    assert res.error == "rejected: reaches outside the warehouse (EXTERNAL_QUERY)"
    assert ex.check("SELECT AI.GENERATE_INT(name) FROM emp") \
        == "rejected: reaches outside the warehouse (AI.GENERATE_INT)"


def test_select_returns_rows_as_dicts(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    res = ex.execute("SELECT name, age FROM emp ORDER BY age")
    assert res.ok is True
    assert res.rows == [{"name": "ann", "age": 30}, {"name": "bob", "age": 40}]


def test_zero_rows_is_ok_not_error(tmp_path):
    # "ran, returned nothing" must be distinct from "failed to run".
    ex = SQLiteExecutor(_seed(tmp_path))
    res = ex.execute("SELECT name FROM emp WHERE age > 999")
    assert res.ok is True
    assert res.rows == []


def test_bad_sql_becomes_error_not_exception(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    res = ex.execute("SELECT nope FROM emp")
    assert res.ok is False
    assert "nope" in res.error or "no such column" in res.error.lower()


def test_non_read_only_is_rejected_before_touching_db(tmp_path):
    db = _seed(tmp_path)
    ex = SQLiteExecutor(db)
    res = ex.execute("DELETE FROM emp")
    assert res.ok is False
    assert "read-only" in res.error
    # the table is untouched
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM emp").fetchone()[0] == 2
    con.close()


def test_the_estimate_prices_only_what_will_run_and_names_the_rest(monkeypatch):
    """On one estate the estimate priced 25 golds at $0.39 while 14 of them
    were over the 1 GB ceiling; the run rejected those for free and billed
    $0.01. A statement over the ceiling, or one the dry-run rejects, is
    counted and reported but never priced."""
    # no client is made: the dry-run is replaced below, nothing else is called
    monkeypatch.setattr("context_lift.executor._load_bigquery", lambda: None)
    executor = BigQueryExecutor(project="billing", maximum_bytes_billed=1_000_000_000)
    dry_runs = {"SELECT small FROM t": 500_000_000,
                "SELECT big FROM t": 3_000_000_000,
                "SELECT nope FROM t": ValueError("Unrecognized name: nope")}

    def fake_dry_run(sql):
        if isinstance(dry_runs[sql], Exception):
            raise dry_runs[sql]
        return dry_runs[sql]
    executor.dry_run_bytes = fake_dry_run

    estimate = executor.estimate(["SELECT small FROM t", "SELECT big FROM t",
                                  "SELECT nope FROM t", "DROP TABLE t"])

    assert estimate["queries"] == 3 and estimate["failed"] == 1
    assert estimate["bytes"] == 3_500_000_000              # what the dry-runs measured
    assert estimate["billable_bytes"] == 500_000_000       # only the statement that will run
    assert estimate["usd"] == round(bigquery_usd(500_000_000), 6)
    assert estimate["ceiling_bytes"] == 1_000_000_000
    assert estimate["over_ceiling"] == {"count": 1, "largest_bytes": 3_000_000_000,
                                        "bytes": 3_000_000_000}
    assert estimate["statements"] == [
        {"bytes": 500_000_000, "over_ceiling": False, "failed": False,
         "refused": False, "no_credential": False},
        {"bytes": 3_000_000_000, "over_ceiling": True, "failed": False,
         "refused": False, "no_credential": False},
        {"bytes": None, "over_ceiling": False, "failed": True,
         "refused": False, "no_credential": False},
        # not read-only: never dry-run
        {"bytes": None, "over_ceiling": False, "failed": True,
         "refused": False, "no_credential": False},
    ]


def test_row_cap_truncates_and_flags(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path), row_cap=1)
    res = ex.execute("SELECT name FROM emp ORDER BY age")
    assert len(res.rows) == 1
    assert res.truncated is True


def test_row_cap_not_hit_is_not_truncated(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path), row_cap=10)
    res = ex.execute("SELECT name FROM emp")
    assert res.truncated is False


def test_exec_result_ok_reflects_error_field():
    assert ExecResult(rows=[]).ok is True
    assert ExecResult(error="boom").ok is False


def test_timeout_bounds_a_query_row_cap_cannot(tmp_path):
    # A recursive CTE that counts to 100 million never yields its first row
    # quickly, so row_cap alone would wait forever. The timeout must abort it
    # and name itself in the error.
    ex = SQLiteExecutor(_seed(tmp_path), row_cap=10, timeout_seconds=0.2)
    res = ex.execute(
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n "
        "WHERE i < 100000000) SELECT MAX(i) FROM n"
    )
    assert res.ok is False
    assert res.error.startswith("timeout:")


def test_a_fast_query_is_untouched_by_the_timeout(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path), timeout_seconds=5.0)
    res = ex.execute("SELECT name FROM emp ORDER BY age")
    assert res.ok is True
    assert [r["name"] for r in res.rows] == ["ann", "bob"]


def test_semicolon_inside_a_string_literal_is_not_a_second_statement():
    # Real gold from KaggleDBQA GreaterManchesterCrime: gate flagged it
    # NON_SELECT and silently cost the analyst 1 of 27 questions.
    assert is_read_only(
        'SELECT Type FROM crime WHERE Outcome = '
        '"Investigation complete; no suspect identified"'
    ) is True
    assert is_read_only(
        "SELECT name FROM emp WHERE note = 'done; archived'"
    ) is True


def test_write_keyword_inside_a_string_literal_is_inert():
    assert is_read_only("SELECT 1 WHERE label = 'DROP TABLE emp'") is True
    # but a real second statement still fails, quoted decoy or not
    assert is_read_only("SELECT 'x'; DROP TABLE emp") is False


def test_a_comment_marker_inside_a_literal_does_not_end_the_statement():
    """`-- ` inside quotes is text. Reading comments first cut the literal
    open, the loose quote swallowed the next line, and the INSERT it exposed
    got a valid read-only query rejected."""
    sql = ("SELECT * FROM audit_logs\n"
           "WHERE description = 'Admin note -- reviewed'\n"
           "AND operation = 'INSERT'")

    assert read_only_violation(sql) is None


def test_a_write_cannot_hide_behind_a_comment_marker_in_a_literal():
    """The other direction, and the one that matters: reading comments first
    deleted the semicolon and the DROP with it, and the rail passed a write."""
    assert read_only_violation("SELECT ' -- '; DROP TABLE users; -- '") == NOT_READ_ONLY


def test_an_apostrophe_in_a_comment_does_not_swallow_the_next_line():
    """Masking literals first instead is wrong the same way: `don't` opens a
    literal that closes on the real quote below, and removing that comment
    takes the WHERE with it. Reading both in one pass is the only order that
    holds."""
    sql = "SELECT * FROM t  -- don't\nWHERE x = 'a' AND y = 1"

    assert strip_comments_and_mask_literals(sql).split() == [
        "SELECT", "*", "FROM", "t", "WHERE", "x", "=", "'?'", "AND", "y", "=", "1"]


def test_an_unterminated_literal_is_refused_rather_than_guessed():
    """The rail cannot read the statement, so it must not pass it."""
    assert read_only_violation("SELECT 'unterminated") == NOT_READ_ONLY


def test_a_backtick_table_name_survives_the_read():
    """Identifiers are what a caller reads the statement FOR, so they are kept
    while values are masked."""
    assert strip_comments_and_mask_literals(
        "SELECT a FROM `p.d.t` WHERE s = 'x'") == "SELECT a FROM `p.d.t` WHERE s = '?'"


def test_replace_and_truncate_as_scalar_functions_are_read_only():
    """REPLACE(...) and TRUNCATE(...) read a value. A real gold used
    REPLACE(cast(day as string), '-', '') and the rail refused it."""
    assert read_only_violation("SELECT REPLACE(name, 'a', 'b') FROM t") is None
    assert read_only_violation("SELECT TRUNCATE(x, 2) FROM t") is None


def test_truncate_table_and_replace_into_are_still_refused():
    assert read_only_violation("TRUNCATE TABLE t") == NOT_READ_ONLY
    assert read_only_violation("REPLACE INTO t VALUES (1)") == NOT_READ_ONLY
    # whitespace does not hide either shape: the parenthesis decides, not the gap
    assert read_only_violation("TRUNCATE  TABLE t") == NOT_READ_ONLY
    assert read_only_violation("SELECT REPLACE (name, 'a', 'b') FROM t") is None


def test_a_byte_ceiling_can_be_written_with_a_unit():
    """--max-bytes 50 read as fifty bytes and refused every query on an
    estate, and the refusal printed the ceiling as "5e-08 GB", which reads
    like a real setting rather than a typo."""
    from context_lift.executor import parse_bytes
    assert parse_bytes("50GB") == 50_000_000_000
    assert parse_bytes("500MB") == 500_000_000
    assert parse_bytes("2TiB") == 2 * 2**40
    assert parse_bytes("1.5GB") == 1_500_000_000
    assert parse_bytes("5 GB") == 5_000_000_000, "a space before the unit is normal typing"
    assert parse_bytes("50_000_000_000") == 50_000_000_000
    assert parse_bytes("50000000000") == 50_000_000_000, "plain bytes still work"


def test_a_plain_number_below_bigquerys_billing_minimum_is_refused():
    """Nobody has ever wanted a fifty-byte ceiling, and the run that used one
    refused 73 of 81 queries before any of them ran."""
    import pytest
    from context_lift.executor import parse_bytes
    with pytest.raises(ValueError) as stop:
        parse_bytes("50")
    assert "is 50 bytes" in str(stop.value)
    assert "write 50GB if you meant 50 gigabytes" in str(stop.value), \
        "the message must carry the number the author probably meant"
    # 10 MB is BigQuery's own per-query billing minimum: at it, fine
    assert parse_bytes("10000000") == 10_000_000
    with pytest.raises(ValueError):
        parse_bytes("9999999")
    # a unit is always believed: 1KB is a deliberate, if useless, ceiling
    assert parse_bytes("1KB") == 1000


def test_an_unreadable_byte_size_says_what_to_write():
    import pytest
    from context_lift.executor import parse_bytes
    for bad in ("abc", "", "7XB", "GB"):
        with pytest.raises(ValueError) as stop:
            parse_bytes(bad)
        assert "50GB" in str(stop.value) or "unknown unit" in str(stop.value)


def test_an_auth_failure_is_not_called_a_parse_failure(tmp_path):
    """The three reasons a statement never runs need three different people.

    Measured 2026-09-12. A corp account is refused by two Google APIs with
    `access_denied: Account restricted`, raised while fetching the token, and
    the BigQuery Python client surfaces it as `RefreshError`. The platform
    pattern has "Access Denied" with a space, so `access_denied` with an
    underscore fell through and **285 of 286 statements printed as "the SQL
    does not parse"** on queries that had run correctly two days earlier.

    A reader of that output goes and rewrites working SQL.
    """
    from context_lift.executor import credential_refused, platform_refused

    # The real strings, from the run that found this.
    for error in ("('access_denied: Account restricted', {'error': "
                  "'access_denied'})",
                  "RefreshError: access_denied: Account restricted",
                  "503 Getting metadata from plugin failed with error: "
                  "('access_denied: Account restricted'"):
        assert credential_refused(error), error
        # And it must win over the 403 class, which would send the reader to
        # a security team with nothing to fix.
        assert not (platform_refused(error) and not credential_refused(error))

    # A real 403 is still a 403, not a credential problem.
    for error in ("Forbidden: 403 VPC Service Controls: Request is prohibited "
                  "by organization's policy",
                  "Permission denied on resource project foo"):
        assert platform_refused(error), error
        assert not credential_refused(error), error

    # And ordinary broken SQL is neither.
    for error in ("Unrecognized name: nope", "Syntax error: Unexpected end of "
                  "script", "Not found: Table p:d.t was not found"):
        assert not credential_refused(error), error
        assert not platform_refused(error), error
