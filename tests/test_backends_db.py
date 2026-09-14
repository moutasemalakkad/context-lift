# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""`--db` picks a backend; on BigQuery the cost is stated before any scan.

The BigQuery paths run against a fake google.cloud.bigquery module
(Client, QueryJobConfig) swapped in through `executor._load_bigquery`, so
the fast suite needs no credentials and no network. The fake records what
was dry-run and what was executed, which is what these tests assert on:
an estimate must not execute, and a refused budget must not either.
"""
import json
import sqlite3
import threading
import time

import pytest

from context_lift.backends_db import (demand_sql, make_executor, parse_db_spec, query_log_sql,
                                  schema_text, shape_sql)
from context_lift.cli import main
from context_lift.executor import BigQueryExecutor, SQLiteExecutor, bigquery_usd

GB = 1_000_000_000


class FakeJob:
    def __init__(self, sql, dry_run, scanned, rows):
        self.sql = sql
        self.total_bytes_processed = scanned
        # billed rounds up to the 10 MB minimum, as BigQuery does
        self.total_bytes_billed = 0 if dry_run else max(scanned, 10 * 1024 * 1024)
        self._rows = rows
        self.cancelled = False

    def result(self, timeout=None, max_results=None):
        if self.sql == "SELECT slow FROM t":
            raise TimeoutError("Operation did not complete within the designated timeout.")
        read = FakeClient.killed_at_cap.get(self.sql)
        if read is not None:
            # BigQuery kills the job at maximum_bytes_billed and still reports
            # what it read, which is the figure the message should carry
            self.total_bytes_processed = read
            raise RuntimeError(f"400 Query exceeded limit for bytes billed: 1000000000. "
                               f"{read} or higher required.")
        rows = self._rows
        return rows if max_results is None else rows[:max_results]

    def cancel(self):
        self.cancelled = True


class FakeClient:
    """`query()` answers from a table of sql -> (bytes, rows) and records
    every call, dry-run or real, so a test can prove nothing executed.

    `delay` makes each call sleep (20 ms, no longer) and `max_in_flight`
    counts how many calls overlapped, so a test can prove that workers ran
    at once. `fail_once_with` is a message the next call raises, then
    clears: the corporate mTLS error, replayed. `errors` maps a SQL text to
    the message every call on it raises: the warehouse's own "Not found"
    text, replayed. `killed_at_cap` maps a SQL text to the bytes its job read
    before maximum_bytes_billed stopped it, after the dry run had passed it."""
    table: dict = {}
    errors: dict = {}
    killed_at_cap: dict = {}
    dry_runs: list = []
    executed: list = []
    configs: list = []
    clients_made = 0
    delay = 0.0
    in_flight = 0
    max_in_flight = 0
    fail_once_with = None
    lock = threading.Lock()

    def __init__(self, project=None, location=None, client_info=None):
        FakeClient.clients_made += 1
        self.project = project
        # Kept, not ignored. The real client sends this as the user agent,
        # and a fake that drops it is how an attribution bug hides: the
        # wrong ClientInfo class raised only on a real gRPC call.
        self.client_info = client_info

    def query(self, sql, job_config=None, location=None):
        with FakeClient.lock:
            if FakeClient.fail_once_with:
                message, FakeClient.fail_once_with = FakeClient.fail_once_with, None
                raise RuntimeError(message)
            FakeClient.configs.append(job_config)
            if job_config.settings.get("dry_run"):
                FakeClient.dry_runs.append(sql)
            else:
                FakeClient.executed.append(sql)
            FakeClient.in_flight += 1
            FakeClient.max_in_flight = max(FakeClient.max_in_flight, FakeClient.in_flight)
        try:
            if FakeClient.delay:
                time.sleep(FakeClient.delay)
            if sql in FakeClient.errors:
                raise RuntimeError(FakeClient.errors[sql])
            scanned, rows = FakeClient.table.get(sql, (None, None))
            if scanned is None:
                raise ValueError(f"Unrecognized name in {sql!r}")
            return FakeJob(sql, job_config.settings.get("dry_run", False), scanned, rows)
        finally:
            with FakeClient.lock:
                FakeClient.in_flight -= 1


class FakeConfig:
    def __init__(self, **settings):
        self.settings = settings
        self.default_dataset = None


class FakeBigQuery:
    Client = FakeClient
    QueryJobConfig = FakeConfig


@pytest.fixture
def fake_bigquery(monkeypatch):
    FakeClient.table = {
        "SELECT status FROM orders": (2 * GB, [{"status": "Shipped"}, {"status": "Complete"}]),
        "SELECT COUNT(*) AS n FROM orders": (0, [{"n": 2}]),
        "SELECT slow FROM t": (100, []),
    }
    FakeClient.errors = {}
    FakeClient.killed_at_cap = {}
    FakeClient.dry_runs, FakeClient.executed, FakeClient.configs = [], [], []
    FakeClient.clients_made = 0
    FakeClient.delay, FakeClient.in_flight, FakeClient.max_in_flight = 0.0, 0, 0
    FakeClient.fail_once_with = None
    monkeypatch.setattr("context_lift.executor._load_bigquery", lambda: FakeBigQuery)
    return FakeClient


def _seed_db(tmp_path):
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE emp(name TEXT, age INT)")
    con.execute("CREATE TABLE dept(id INT, title TEXT)")
    con.commit()
    con.close()
    return str(path)


def _write_csv(path, header, rows):
    import csv
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return str(path)


# --- the spec grammar and the factory -----------------------------------------

def test_a_path_is_sqlite_and_a_bq_spec_is_bigquery_with_an_optional_dataset():
    assert parse_db_spec("warehouse.db").kind == "sqlite"
    plain = parse_db_spec("bq://billing-proj")
    assert (plain.kind, plain.project, plain.dataset) == ("bigquery", "billing-proj", None)
    same = parse_db_spec("bq://billing-proj/sales")
    assert same.dataset == "billing-proj.sales"            # unqualified: the billing project owns it
    cross = parse_db_spec("bq://billing-proj/bigquery-public-data.thelook_ecommerce")
    assert cross.project == "billing-proj"
    assert cross.dataset == "bigquery-public-data.thelook_ecommerce"
    with pytest.raises(ValueError):
        parse_db_spec("bq://")
    with pytest.raises(ValueError):
        parse_db_spec("bq://p/d/extra")


def test_the_factory_picks_the_executor_by_spec(tmp_path, fake_bigquery):
    sqlite = make_executor(_seed_db(tmp_path), timeout_seconds=5)
    assert isinstance(sqlite, SQLiteExecutor) and sqlite.timeout_seconds == 5
    warehouse = make_executor("bq://billing/data.sales", maximum_bytes_billed=7, row_cap=3)
    assert isinstance(warehouse, BigQueryExecutor)
    assert (warehouse.project, warehouse.dataset) == ("billing", "data.sales")
    assert (warehouse.maximum_bytes_billed, warehouse.row_cap) == (7, 3)


def test_a_forbidden_billing_project_is_refused_with_zero_clients_made(monkeypatch, fake_bigquery):
    monkeypatch.setenv("CONTEXTLIFT_FORBIDDEN_PROJECTS", "acme-")

    with pytest.raises(SystemExit) as refused:
        make_executor("bq://acme-prod/shop")

    assert str(refused.value) == ("refusing: project 'acme-prod' matches CONTEXTLIFT_FORBIDDEN_PROJECTS "
                                  "('acme-'); BigQuery jobs would be billed there. Use a project "
                                  "you own.")
    assert fake_bigquery.clients_made == 0


def test_the_factory_names_the_missing_extra_instead_of_a_traceback(monkeypatch):
    def no_extra():
        raise ImportError("No module named 'google'")
    monkeypatch.setattr("context_lift.executor._load_bigquery", no_extra)
    with pytest.raises(ImportError):
        make_executor("bq://p")


# --- the executor against the fake client -------------------------------------

def test_estimate_dry_runs_each_distinct_statement_and_prices_the_total(fake_bigquery):
    # a ceiling above the 2 GB statement: the estimate prices only what will run
    executor = make_executor("bq://billing/data.sales", maximum_bytes_billed=3 * GB)

    estimate = executor.estimate(["SELECT status FROM orders", "SELECT status FROM orders",
                                  "SELECT COUNT(*) AS n FROM orders", "DROP TABLE orders",
                                  "SELECT nope FROM nowhere"])

    assert estimate["queries"] == 3                       # distinct, read-only only
    assert estimate["failed"] == 1                        # the unknown name
    assert estimate["bytes"] == 2 * GB
    assert estimate["billable_bytes"] == 2 * GB + 10 * 1024 * 1024   # the 10 MB minimum
    assert estimate["usd"] == round(bigquery_usd(estimate["billable_bytes"]), 6)
    assert fake_bigquery.executed == []                   # an estimate never runs anything
    assert sorted(fake_bigquery.dry_runs) == sorted([
        "SELECT status FROM orders", "SELECT COUNT(*) AS n FROM orders", "SELECT nope FROM nowhere"])


def test_execute_dry_runs_first_refuses_over_the_ceiling_and_bills_the_rest(fake_bigquery):
    executor = make_executor("bq://billing/data.sales", maximum_bytes_billed=GB)

    refused = executor.execute("SELECT status FROM orders")           # 2 GB > 1 GB
    ran = executor.execute("SELECT COUNT(*) AS n FROM orders")

    assert refused.error.startswith("rejected: the query would scan more than the 1 GB ceiling")
    assert ran.rows == [{"n": 2}]
    assert fake_bigquery.executed == ["SELECT COUNT(*) AS n FROM orders"]
    assert {k: executor.usage[k] for k in ("queries", "bytes_processed", "bytes_billed")} == {
        "queries": 1, "bytes_processed": 0, "bytes_billed": 10 * 1024 * 1024}
    assert executor.usage["elapsed_seconds"] >= 0
    real = [c for c in fake_bigquery.configs if not c.settings.get("dry_run")][0]
    # on by default: the same SQL over the same data gives the same rows
    # either way, and a job served from the cache bills nothing
    assert real.settings["use_query_cache"] is True
    assert real.settings["maximum_bytes_billed"] == GB
    assert real.default_dataset == "data.sales"


def test_a_job_the_cap_kills_carries_the_bytes_it_read_and_is_counted(fake_bigquery):
    """On one gate run the dry run passed 45 statements, 41 ran and 4 were
    killed at execution. The killed ones are free, but their reject named
    the ceiling and no figure while the dry-run rejects named 10.87 to
    1042.51 GB, so the same defect read as two different ones."""
    FakeClient.table["SELECT wide FROM t"] = (900_000_000, [])      # under the ceiling
    FakeClient.killed_at_cap["SELECT wide FROM t"] = 10_870_000_000
    executor = make_executor("bq://billing", maximum_bytes_billed=GB)

    killed = executor.execute("SELECT wide FROM t")

    assert killed.error == ("rejected: the query would scan more than the 1 GB ceiling "
                            "(the killed job read: 10.87 GB); narrow it or raise --max-bytes")
    assert executor.usage["ceiling_kills"] == 1
    assert executor.usage["queries"] == 0                            # nothing billed


def test_one_client_per_thread_is_rebuilt_after_the_mtls_error(fake_bigquery):
    # on an mTLS network a client's SSL context opens one connection; when
    # that connection dies the next call fails with this message and only
    # a new client can open another
    executor = make_executor("bq://billing")
    first = executor.execute("SELECT COUNT(*) AS n FROM orders")
    fake_bigquery.fail_once_with = "Context has already been used to create a Connection"
    second = executor.execute("SELECT COUNT(*) AS n FROM orders")

    assert first.rows == second.rows == [{"n": 2}]
    assert fake_bigquery.clients_made == 2                 # one for the thread, one rebuilt
    assert fake_bigquery.dry_runs == ["SELECT COUNT(*) AS n FROM orders"]   # remembered
    assert fake_bigquery.executed == ["SELECT COUNT(*) AS n FROM orders"] * 2


def test_estimate_under_workers_dry_runs_each_distinct_statement_once_and_at_once(fake_bigquery):
    fake_bigquery.delay = 0.02
    executor = make_executor("bq://billing/data.sales")

    estimate = executor.estimate(["SELECT status FROM orders", "SELECT COUNT(*) AS n FROM orders",
                                  "SELECT status FROM orders", "SELECT slow FROM t"], workers=4)

    assert estimate["queries"] == 3
    assert sorted(fake_bigquery.dry_runs) == sorted([
        "SELECT status FROM orders", "SELECT COUNT(*) AS n FROM orders", "SELECT slow FROM t"])
    assert fake_bigquery.max_in_flight >= 2
    assert fake_bigquery.executed == []


def test_execute_after_estimate_does_not_dry_run_again(fake_bigquery):
    executor = make_executor("bq://billing/data.sales")
    executor.estimate(["SELECT COUNT(*) AS n FROM orders"])

    ran = executor.execute("SELECT COUNT(*) AS n FROM orders")

    assert ran.rows == [{"n": 2}]
    assert fake_bigquery.dry_runs == ["SELECT COUNT(*) AS n FROM orders"]   # once, not twice
    assert fake_bigquery.executed == ["SELECT COUNT(*) AS n FROM orders"]


def test_a_failed_dry_run_is_remembered(fake_bigquery):
    executor = make_executor("bq://billing/data.sales")
    estimate = executor.estimate(["SELECT nope FROM nowhere"])

    ran = executor.execute("SELECT nope FROM nowhere")

    assert estimate["failed"] == 1
    assert ran.error.startswith("ValueError: Unrecognized name")
    assert fake_bigquery.dry_runs == ["SELECT nope FROM nowhere"]
    assert fake_bigquery.executed == []


def test_the_estimate_counts_statements_not_found_in_the_billing_project(fake_bigquery):
    from context_lift.executor import not_found_in
    FakeClient.errors["SELECT 1 FROM shop.orders"] = \
        "404 Not found: Table billing:shop.orders was not found in location US"
    FakeClient.errors["SELECT 1 FROM nowhere.at_all"] = \
        "404 Not found: Dataset billing:nowhere was not found in location US"
    FakeClient.errors["SELECT 1 FROM pub.shop.orders"] = \
        "404 Not found: Table pub:shop.orders was not found in location US"   # the data project: real
    executor = make_executor("bq://billing/pub.shop")

    estimate = executor.estimate(["SELECT 1 FROM shop.orders", "SELECT 1 FROM nowhere.at_all",
                                  "SELECT 1 FROM pub.shop.orders", "SELECT nope FROM nowhere"])

    assert (estimate["queries"], estimate["failed"], estimate["not_found"]) == (4, 4, 2)
    assert not_found_in("Unrecognized name: x", "billing") is False
    assert not_found_in(None, "billing") is False


def test_a_timeout_is_reported_with_the_sqlite_prefix_and_the_job_is_cancelled(fake_bigquery):
    executor = make_executor("bq://billing", timeout_seconds=2)
    result = executor.execute("SELECT slow FROM t")
    assert result.error == "timeout: exceeded 2s"
    real = [c for c in fake_bigquery.configs if not c.settings.get("dry_run")][0]
    assert real.settings["job_timeout_ms"] == 2000


# --- the CLI: cost before scoring, telemetry in the manifest -------------------

def test_gate_on_bigquery_prints_the_estimate_and_a_low_budget_refuses_before_any_scan(
        tmp_path, fake_bigquery, capsys):
    golds = _write_csv(tmp_path / "g.csv", ["id", "gold_sql"],
                       [("q1", "SELECT status FROM orders"), ("q2", "SELECT COUNT(*) AS n FROM orders")])

    code = main(["gate", "--db", "bq://billing/data.sales", "--golds", golds,
                 "--max-bytes", str(3 * GB), "--budget", "0.001"])

    out, err = capsys.readouterr()
    assert code == 2
    assert "estimated: 2 queries, 2.000 GB (dry-run), ~$0.0114 on-demand" in out
    assert "time: 2 queries at " in out and "minutes at --workers 4 (dry-runs went through" in out
    assert "refusing to start: estimate $0.0114 is above the budget $0.0010" in out
    assert fake_bigquery.executed == []
    # no --timeout given: the warehouse gets a 60 s bound, said once
    assert "--timeout unset: each statement is bounded at 60 s on BigQuery" in err


def test_the_pre_pass_is_an_upper_bound_and_the_spend_line_reports_the_slip(
        tmp_path, fake_bigquery, capsys):
    """One gate run: the dry run passed 45 statements under the 10 GB
    ceiling, 41 ran and BigQuery killed 4 at the cap. The killed jobs cost
    nothing, but "45 will run" sized an answer key at 45 golds and it came
    back 41."""
    FakeClient.table["SELECT 1 FROM DATASET.a"] = (100, [{"n": 1}])
    FakeClient.table["SELECT 2 FROM DATASET.b"] = (900_000_000, [{"n": 1}])
    FakeClient.killed_at_cap["SELECT 2 FROM DATASET.b"] = 10_870_000_000
    golds = _write_csv(tmp_path / "g.csv", ["id", "gold_sql"],
                       [("q1", "SELECT 1 FROM DATASET.a"), ("q2", "SELECT 2 FROM DATASET.b")])

    code = main(["gate", "--db", "bq://PROJECT", "--golds", golds, "--max-bytes", str(GB)])

    out = capsys.readouterr().out
    assert code == 1
    assert "up to 2 statements will run: the dry run measures what each one reads now" in out
    assert ("pre-pass: up to 2 statements were expected to run, 1 ran, 1 killed at the 1 GB "
            "--max-bytes ceiling (killed jobs are free)") in out


def test_bigquery_gets_the_60s_timeout_when_none_is_given(tmp_path, fake_bigquery, capsys):
    golds = _write_csv(tmp_path / "g.csv", ["id", "gold_sql"],
                       [("q1", "SELECT COUNT(*) AS n FROM orders")])

    assert main(["gate", "--db", "bq://billing/data.sales", "--golds", golds]) == 0

    real = [c for c in fake_bigquery.configs if not c.settings.get("dry_run")][0]
    assert real.settings["job_timeout_ms"] == 60_000
    assert capsys.readouterr().err.count("--timeout unset") == 1


def test_a_gold_that_names_another_project_is_flagged_and_never_clean(
        tmp_path, fake_bigquery, capsys):
    """--db says where the run reads; a mined statement carries its own
    names. 2 of 60 candidates on one estate named another project, ran
    there, returned rows and came back ok. Rewriting the name to the --db
    project fixed one of them; the other table did not exist there at all,
    so the status has to be its own value and not a hard reject."""
    elsewhere = "SELECT n FROM `OTHER_PROJECT.DATASET.orders`"
    FakeClient.table[elsewhere] = (100, [{"n": 7}])
    golds = _write_csv(tmp_path / "g.csv", ["id", "gold_sql"],
                       [("q1", elsewhere), ("q2", "SELECT COUNT(*) AS n FROM orders")])

    code = main(["gate", "--db", "bq://PROJECT/PROJECT.DATASET", "--golds", golds,
                 "--flagged-out", str(tmp_path / "flagged.csv")])

    out = capsys.readouterr().out
    assert code == 1
    assert "gate: 2 golds, 1 clean, 1 flagged" in out
    assert "defects: CROSS_PROJECT=1" in out
    assert "names project other_project, not the --db project PROJECT" in out
    assert elsewhere in fake_bigquery.executed          # it still ran, and returned rows
    assert "q1" in (tmp_path / "flagged.csv").read_text()


def test_a_call_the_platform_refuses_is_not_reported_as_broken_sql(
        tmp_path, fake_bigquery, capsys):
    """The pre-pass said "1 rejected at dry-run" for a 403 from VPC Service
    Controls, the same words as a query that does not parse, while the other
    statements in the run went through. One is the query's author's problem,
    the other the security team's."""
    FakeClient.errors["SELECT 1 FROM DATASET.blocked"] = (
        "403 Request is prohibited by organization's policy. "
        "vpcServiceControlsUniqueIdentifier: abc123")
    FakeClient.errors["SELECT 2 FROM DATASET.typo"] = "400 Syntax error: Unexpected end of script"
    golds = _write_csv(tmp_path / "g.csv", ["id", "gold_sql"],
                       [("q1", "SELECT 1 FROM DATASET.blocked"),
                        ("q2", "SELECT 2 FROM DATASET.typo"),
                        ("q3", "SELECT COUNT(*) AS n FROM orders")])

    code = main(["gate", "--db", "bq://PROJECT", "--golds", golds])

    out = capsys.readouterr().out
    assert code == 1
    assert "1 rejected at dry-run, 1 refused by the platform" in out
    assert ("2 statements will not run and cost nothing (1 the SQL does not parse, 1 the "
            "platform refused the call, 0 not read-only):") in out
    assert "  q1 (the platform refused the call)" in out
    assert "a refused call is not a query defect" in out
    assert "ask whoever owns the org policy" in out


def test_the_score_estimate_counts_a_refused_call_apart_from_a_parse_error(
        tmp_path, fake_bigquery, capsys):
    """gate and the estimate line said the two apart. The line that adds up
    from the predictions to the calls still put a 403 under "rejected at
    dry-run", the same words as SQL that does not parse."""
    FakeClient.errors["SELECT 1 FROM DATASET.blocked"] = (
        "403 Request is prohibited by organization's policy. "
        "vpcServiceControlsUniqueIdentifier: abc123")
    FakeClient.errors["SELECT 2 FROM DATASET.typo"] = "400 Syntax error: Unexpected end of script"
    FakeClient.table["SELECT name FROM orders"] = (100, [{"name": "ann"}])
    gold = "SELECT COUNT(*) AS n FROM orders"
    predictions = _write_csv(tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
                             [("q1", "how many?", gold, "SELECT 1 FROM DATASET.blocked"),
                              ("q2", "how many?", gold, "SELECT 2 FROM DATASET.typo"),
                              ("q3", "how many?", gold, "SELECT name FROM orders")])

    code = main(["score", "--db", "bq://PROJECT", "--predictions", predictions,
                 "--estimate-only"])

    out = capsys.readouterr().out
    assert code == 0
    assert ("3 predictions: 0 not read-only, 1 rejected at dry-run, 1 refused by the platform, "
            "0 identical to a gold or to another prediction, 1 to run; 1 more statement to run "
            "beside them") in out
    assert "ask whoever owns the org policy (VPC Service Controls answers 403)" in out
    assert fake_bigquery.executed == []


def test_gate_counts_not_found_golds_and_prints_the_three_part_fix(tmp_path, fake_bigquery, capsys):
    """Every gold on one estate was `dataset.table` and every one failed:
    a 2-part name resolves in the billing project, and the data was in
    another. N EXEC_FAIL rows said nothing about that."""
    FakeClient.errors["SELECT 1 FROM shop.orders"] = \
        "404 Not found: Table billing:shop.orders was not found in location US"
    FakeClient.errors["SELECT 2 FROM shop.users"] = \
        "404 Not found: Table billing:shop.users was not found in location US"
    golds = _write_csv(tmp_path / "g.csv", ["id", "gold_sql"],
                       [("q1", "SELECT 1 FROM shop.orders"), ("q2", "SELECT 2 FROM shop.users"),
                        ("q3", "SELECT COUNT(*) AS n FROM orders")])

    code = main(["gate", "--db", "bq://billing/pub.shop", "--golds", golds])

    out = capsys.readouterr().out
    assert code == 1
    assert "estimated: 3 queries, 0.000 GB (dry-run, 2 rejected at dry-run, 2 not found in the " \
           "billing project)" in out
    assert "gate: 3 golds, 1 clean, 2 flagged" in out
    assert ("  2 golds failed with 'not found' in project billing: a 2-part name resolves there, "
            "not in pub; write pub.dataset.table, or run with --db bq://pub/...") in out
    # the same billing and data project: nothing to say, the names are simply wrong
    FakeClient.dry_runs.clear()
    assert main(["gate", "--db", "bq://billing/shop", "--golds", golds]) == 1
    assert "2-part name" not in capsys.readouterr().out


def test_the_query_cache_is_off_under_check_stability(tmp_path, fake_bigquery):
    golds = _write_csv(tmp_path / "g.csv", ["id", "gold_sql"],
                       [("q1", "SELECT COUNT(*) AS n FROM orders")])

    assert main(["gate", "--db", "bq://billing/data.sales", "--golds", golds,
                 "--check-stability"]) == 0

    real = [c for c in fake_bigquery.configs if not c.settings.get("dry_run")]
    assert len(real) == 2 and all(c.settings["use_query_cache"] is False for c in real)


def test_score_skips_dry_runs_for_cached_golds_and_says_how_many_were_reused(
        tmp_path, fake_bigquery, capsys):
    preds = _write_csv(tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
                       [("q1", "n?", "SELECT status FROM orders", "SELECT COUNT(*) AS n FROM orders")])
    cache = tmp_path / "golds.json"
    argv = ["score", "--db", "bq://billing/data.sales", "--predictions", preds,
            "--max-bytes", str(3 * GB), "--gold-cache", str(cache)]
    assert main(argv) == 0
    assert sorted(fake_bigquery.dry_runs) == ["SELECT COUNT(*) AS n FROM orders",
                                              "SELECT status FROM orders"]
    fake_bigquery.dry_runs.clear()
    fake_bigquery.executed.clear()
    capsys.readouterr()

    assert main(argv) == 0

    out = capsys.readouterr().out
    assert fake_bigquery.dry_runs == ["SELECT COUNT(*) AS n FROM orders"]      # the gold: not dry-run
    assert fake_bigquery.executed == ["SELECT COUNT(*) AS n FROM orders"]      # nor run
    assert "estimated: 1 queries, 0.000 GB (dry-run)" in out
    assert "; 1 answers reused from the gold cache, not dry-run" in out
    assert "time: 1 queries at " in out


def test_mine_dry_runs_the_chosen_shapes_and_refuses_above_the_budget_before_gating(
        tmp_path, fake_bigquery, capsys):
    FakeClient.table.update({
        "SELECT status, COUNT(*) AS n FROM orders GROUP BY status": (2 * GB, [{"status": "x", "n": 1}]),
        "SELECT id FROM users": (GB // 2, [{"id": 1}]),
    })
    log = _write_csv(tmp_path / "log.csv", ["sql", "times_seen"], [
        ("SELECT status FROM orders", 5),
        ("SELECT status, COUNT(*) AS n FROM orders GROUP BY status", 3),
        ("SELECT id FROM users", 1),
    ])
    out_path = tmp_path / "candidates.csv"
    argv = ["mine", "--db", "bq://billing/data.sales", "--queries", log, "--out", str(out_path),
            "--all-datasets",   # this test mines one synthetic log on purpose
            "--limit", "2", "--no-spread", "--max-bytes", str(3 * GB)]

    code = main(argv + ["--budget", "0.001"])

    out = capsys.readouterr().out
    assert code == 2
    assert "ceiling before ranking: up to 2 candidate queries" in out
    assert "the measured estimate follows once the shapes are chosen" in out
    assert "estimated: 2 queries, 4.000 GB (dry-run)" in out          # the two chosen shapes only
    assert "refusing to start: estimate $0.0227 is above the budget $0.0010" in out
    assert sorted(fake_bigquery.dry_runs) == sorted([
        "SELECT status FROM orders", "SELECT status, COUNT(*) AS n FROM orders GROUP BY status"])
    assert fake_bigquery.executed == [] and not out_path.exists()

    fake_bigquery.dry_runs.clear()
    assert main(argv + ["--estimate-only"]) == 0
    assert "estimated: 2 queries" in capsys.readouterr().out
    assert fake_bigquery.executed == [] and not out_path.exists()   # an estimate writes nothing


def test_estimate_only_prints_json_the_page_reads_and_records_no_run(tmp_path, fake_bigquery, capsys):
    preds = _write_csv(tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
                       [("q1", "n?", "SELECT COUNT(*) AS n FROM orders", "SELECT status FROM orders")])
    workspace = tmp_path / "ws"

    code = main(["--workspace", str(workspace), "score", "--db", "bq://billing/data.sales",
                 "--predictions", preds, "--estimate-only", "--json"])

    sheet = json.loads(capsys.readouterr().out)
    assert code == 0
    assert sheet["queries"] == 2 and sheet["bytes"] == 2 * GB
    assert sheet["estimated_cost_usd"] == sheet["usd"]
    assert sheet["price_table"]["model"] == "BigQuery on-demand"
    assert not (workspace / "runs").exists()               # an estimate is not a run
    assert fake_bigquery.executed == []


def test_a_recorded_score_run_on_bigquery_keeps_the_spec_unhashed_and_the_bytes_billed(
        tmp_path, fake_bigquery):
    preds = _write_csv(tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
                       [("q1", "n?", "SELECT COUNT(*) AS n FROM orders",
                         "SELECT COUNT(*) AS n FROM orders")])
    workspace = tmp_path / "ws"

    code = main(["--workspace", str(workspace), "score", "--db", "bq://billing/data.sales",
                 "--predictions", preds, "--max-bytes", str(3 * GB)])

    assert code == 0
    run_dir = next(p for p in (workspace / "runs").iterdir() if "score" in p.name)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["inputs"]["db"] == {"path": "bq://billing/data.sales", "sha256": None,
                                        "kind": "bigquery"}
    telemetry = manifest["telemetry"]
    # the prediction is the gold's own text, so it reuses the gold's rows
    # instead of running the same query a second time
    assert telemetry["queries"] == 1
    assert telemetry["bytes_billed"] == 10 * 1024 * 1024
    assert telemetry["usd"] == round(bigquery_usd(telemetry["bytes_billed"]), 6)
    assert telemetry["estimate"]["queries"] == 1           # the same SQL twice, dry-run once
    assert telemetry["workers"] == 4                       # the BigQuery default
    assert telemetry["elapsed_seconds"] >= 0
    assert telemetry["estimate"]["seconds_per_call"] >= 0
    assert "minutes" in (run_dir / "stdout.txt").read_text()   # the spent line says how long
    index = json.loads((workspace / "index.json").read_text())
    assert index["workspace"]["db"] == {"path": "bq://billing/data.sales", "sha256": None,
                                        "kind": "bigquery"}
    assert "execution accuracy: 1.000" in (run_dir / "stdout.txt").read_text()


def test_sqlite_score_output_has_no_estimate_line(tmp_path, capsys):
    db = _seed_db(tmp_path)
    preds = _write_csv(tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
                       [("q1", "n?", "SELECT COUNT(*) AS n FROM emp", "SELECT COUNT(*) AS n FROM emp")])
    assert main(["score", "--db", db, "--predictions", preds, "--budget", "0"]) == 0
    out = capsys.readouterr().out
    assert "estimated" not in out and "spent" not in out


# --- schema and log ------------------------------------------------------------

def test_schema_on_sqlite_writes_the_create_table_text(tmp_path, capsys):
    db = _seed_db(tmp_path)
    out = tmp_path / "schema.txt"

    assert main(["schema", "--db", db, "--out", str(out)]) == 0

    assert out.read_text() == "CREATE TABLE dept(id INT, title TEXT);\nCREATE TABLE emp(name TEXT, age INT);\n"
    assert "schema -> " in capsys.readouterr().out
    assert main(["schema", "--db", db, "--out", str(out), "--tables", "emp"]) == 0
    assert out.read_text() == "CREATE TABLE emp(name TEXT, age INT);\n"


def test_schema_on_bigquery_renders_columns_with_descriptions_as_comments(fake_bigquery):
    FakeClient.table.update({
        "SELECT table_name, column_name, data_type FROM `pub.shop.INFORMATION_SCHEMA.COLUMNS` "
        "ORDER BY table_name, ordinal_position": (0, [
            {"table_name": "orders", "column_name": "order_id", "data_type": "INT64"},
            {"table_name": "orders", "column_name": "status", "data_type": "STRING"},
        ]),
        "SELECT table_name, column_name, description FROM `pub.shop.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS` "
        "WHERE field_path = column_name AND description IS NOT NULL": (0, [
            {"table_name": "orders", "column_name": "status", "description": "Shipped, Complete, ..."},
        ]),
    })
    executor = make_executor("bq://billing/pub.shop")

    text = schema_text(executor)

    # another project owns the dataset, so the table name is spelled in full
    assert text == ("CREATE TABLE `pub.shop.orders` (\n"
                    "  order_id INT64,\n"
                    "  status STRING  -- Shipped, Complete, ...\n"
                    ");\n")


def test_profile_on_bigquery_counts_per_table_and_names_a_table_over_the_ceiling(
        fake_bigquery, capsys):
    from context_lift.profile import profile_values
    FakeClient.table.update({
        "SELECT table_name, column_name FROM `pub.shop.INFORMATION_SCHEMA.COLUMNS` "
        "WHERE data_type = 'STRING' ORDER BY table_name, ordinal_position": (0, [
            {"table_name": "events", "column_name": "uri"},
            {"table_name": "orders", "column_name": "status"},
            {"table_name": "orders", "column_name": "note"},
        ]),
        "SELECT APPROX_COUNT_DISTINCT(`uri`) AS d_0, COUNT(`uri`) AS r_0 FROM `pub.shop.events`":
            (5 * GB, []),
        "SELECT APPROX_COUNT_DISTINCT(`status`) AS d_0, COUNT(`status`) AS r_0, "
        "APPROX_COUNT_DISTINCT(`note`) AS d_1, COUNT(`note`) AS r_1 FROM `pub.shop.orders`":
            (GB // 2, [{"d_0": 2, "r_0": 9, "d_1": 900, "r_1": 9}]),
        "SELECT `status` AS v, COUNT(*) AS n FROM `pub.shop.orders` WHERE `status` IS NOT NULL "
        "GROUP BY v ORDER BY n DESC, v": (GB // 4, [{"v": "Shipped", "n": 6}, {"v": "Returned", "n": 3}]),
    })
    executor = make_executor("bq://billing/pub.shop", maximum_bytes_billed=GB)
    lines = []

    found = profile_values(executor, max_values=20, report=lines.append)

    assert [(c.table, c.column, c.values, c.distinct, c.rows) for c in found] == [
        ("shop.orders", "status", ("Shipped", "Returned"), 2, 9)]   # note: 900 values, not a category
    assert lines == ["  shop.events: skipped (rejected: the query would scan more than the 1 GB "
                     "ceiling (dry-run: 5.00 GB); narrow it or raise --max-bytes)"]
    assert fake_bigquery.executed.count("SELECT `status` AS v, COUNT(*) AS n FROM `pub.shop.orders` "
                                        "WHERE `status` IS NOT NULL GROUP BY v ORDER BY n DESC, v") == 1


def test_log_on_sqlite_refuses_in_one_line(tmp_path, capsys):
    assert main(["log", "--db", _seed_db(tmp_path), "--out", str(tmp_path / "log.csv")]) == 2
    assert capsys.readouterr().out.strip() == (
        "SQLite keeps no query log; give mine a CSV with columns "
        "sql[,times_seen,ran_by,distinct_users] instead")


def test_log_prints_the_dry_run_as_a_ceiling_and_lets_bigquery_cap_the_job(
        tmp_path, fake_bigquery, capsys):
    """One estate's dry run on the job-history view said 266 GB for a job
    that billed 29 GB, so the figure is a ceiling: nothing is refused on
    it, and the job carries --max-bytes for BigQuery to enforce."""
    # the CLI drops machine SQL unless --keep-machine-sql, so the SQL the
    # fake is keyed on has to be built the same way
    sql = query_log_sql("billing", 30, "us", 400, drop_machine_sql=True)
    assert "`billing`.`region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT" in sql
    assert "INTERVAL 30 DAY" in sql and sql.endswith("LIMIT 400")
    FakeClient.table[sql] = (8 * GB, [{"sql": "SELECT 1", "times_seen": 3,
                                       "ran_by": "a@x", "distinct_users": 1}])
    out = tmp_path / "log.csv"

    code = main(["log", "--db", "bq://billing", "--days", "30", "--out", str(out),
                 "--estimate-only"])

    printed = capsys.readouterr().out
    assert code == 0
    assert ("dry-run ceiling: 8.000 GB (~$0.0455); on the job-history view the dry run "
            "overstates, one estate measured 10x; the job is capped at --max-bytes 1 GB by "
            "BigQuery itself and fails free above it") in printed
    assert "refusing" not in printed
    assert fake_bigquery.executed == [] and not out.exists()

    code = main(["log", "--db", "bq://billing", "--days", "30", "--out", str(out)])

    printed = capsys.readouterr().out
    assert code == 0 and fake_bigquery.executed == [sql]
    assert fake_bigquery.configs[-1].settings["maximum_bytes_billed"] == GB
    assert "spent: 1 queries, 8.000 GB billed" in printed
    assert out.read_text().splitlines() == ["sql,times_seen,ran_by,distinct_users",
                                            "SELECT 1,3,a@x,1"]


def test_a_log_query_the_platform_refuses_points_at_the_policy_owner(
        tmp_path, fake_bigquery, capsys):
    """log had one sentence for both a query the engine could not parse and a
    call an org policy stopped, and it asked about permissions either way.
    gate and the estimate pre-pass already used executor.platform_refused."""
    # the CLI drops machine SQL unless --keep-machine-sql, so the SQL the
    # fake is keyed on has to be built the same way
    sql = query_log_sql("billing", 30, "us", 400, drop_machine_sql=True)
    FakeClient.errors[sql] = ("403 Request is prohibited by organization's policy. "
                              "vpcServiceControlsUniqueIdentifier: abc123")
    out = tmp_path / "log.csv"

    code = main(["log", "--db", "bq://billing", "--days", "30", "--out", str(out)])

    printed = capsys.readouterr().out
    assert code == 2 and not out.exists()
    assert "the platform refused the log query, not the SQL" in printed
    assert "ask whoever owns the org policy (VPC Service Controls answers 403)" in printed
    assert "rejected at dry-run" not in printed

    # a query the engine itself turns away still reads as the query's problem
    FakeClient.errors[sql] = "400 Syntax error: Unexpected end of script"
    assert main(["log", "--db", "bq://billing", "--days", "30", "--out", str(out)]) == 2
    printed = capsys.readouterr().out
    assert "the log query was rejected at dry-run: RuntimeError: 400 Syntax error" in printed
    assert "org policy" not in printed


def test_ran_by_carries_every_account_of_the_shape_sorted():
    """ANY_VALUE credited a shape to one account picked at random, so
    mine --exclude-accounts dropped it only when the pick happened to be
    the excluded one; 76 of 3,000 shapes on one estate had two accounts."""
    plain = query_log_sql("billing", 7, "us", 10, datasets=["shop"],
                          drop_machine_sql=True)

    assert ("  STRING_AGG(DISTINCT user_email, ';' ORDER BY user_email) AS ran_by,\n"
            in plain)
    assert "ANY_VALUE" not in plain


def test_log_hash_accounts_replaces_only_the_ran_by_expression():
    plain = query_log_sql("billing", 7, "us", 10, datasets=["shop"],
                          drop_machine_sql=True)
    hashed = query_log_sql("billing", 7, "us", 10, datasets=["shop"], hash_accounts=True,
                           drop_machine_sql=True)
    account = "TO_HEX(MD5(LOWER(user_email)))"
    assert f"  STRING_AGG(DISTINCT {account}, ';' ORDER BY {account}) AS ran_by,\n" in hashed
    # byte-identical apart from that one line
    assert hashed.replace(account, "user_email") == plain


def test_log_says_that_ran_by_is_personal_data_or_a_hash(tmp_path, fake_bigquery, capsys):
    out = tmp_path / "log.csv"
    plain_sql = query_log_sql("billing", 30, "us", 400, drop_machine_sql=True)
    hashed_sql = query_log_sql("billing", 30, "us", 400, hash_accounts=True,
                               drop_machine_sql=True)
    FakeClient.table[plain_sql] = (100, [{"sql": "SELECT 1", "times_seen": 3,
                                          "ran_by": "analyst@corp.com;other@corp.com",
                                          "distinct_users": 2}])
    FakeClient.table[hashed_sql] = (100, [{"sql": "SELECT 1", "times_seen": 3,
                                           "ran_by": "0cc175b9c0f1b6a831c399e269772661",
                                           "distinct_users": 1}])

    assert main(["log", "--db", "bq://billing", "--days", "30", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert ("  ran_by holds every account that ran the shape, sorted and joined by ';': "
            "personal data. It is in this CSV and in the copy under runs/; --hash-accounts "
            "stores TO_HEX(MD5(LOWER(user_email))) for each instead, and mine "
            "--exclude-accounts still works") in printed
    assert "analyst@corp.com;other@corp.com" in out.read_text()

    assert main(["log", "--db", "bq://billing", "--days", "30", "--out", str(out),
                 "--hash-accounts"]) == 0
    printed = capsys.readouterr().out
    assert ("  ran_by is TO_HEX(MD5(LOWER(user_email))) per account, joined by ';', not the "
            "address; mine --exclude-accounts takes plain addresses and matches the hash "
            "itself. The sql column can still carry an address a person typed into a WHERE "
            "clause") in printed
    assert "analyst@corp.com" not in out.read_text()
    assert fake_bigquery.executed == [plain_sql, hashed_sql]


def test_log_sql_dataset_filter_checks_referenced_tables_and_query_text():
    # a cached job has no referenced_tables, so the name in the text counts too
    sql = query_log_sql("billing", 7, "us", 10, datasets=["bigquery-public-data.thelook_ecommerce"])
    assert "t.dataset_id IN ('thelook_ecommerce')" in sql
    assert r"REGEXP_CONTAINS(query, r'\b(thelook_ecommerce)\b')" in sql
    assert "bigquery-public-data" not in sql.split("WHERE", 1)[1]
    assert "AND (EXISTS" not in query_log_sql("billing", 7, "us", 10)


def test_log_datasets_build_one_in_filter_and_one_alternation(tmp_path, fake_bigquery, capsys):
    """Six datasets used to be six pulls and a hand-made CSV; the union of
    --dataset and --datasets is one filter of each kind."""
    # Two forms of the same pull. The first is the dataset filter on its own,
    # which is what the counts below are about; the second is what the CLI
    # actually builds, since it drops machine SQL unless --keep-machine-sql,
    # and the fake client is keyed on exact SQL text.
    sql = query_log_sql("billing", 7, "us", 10, datasets=["pub.shop", "ops", "shop"])
    assert sql.count("EXISTS") == 1 and sql.count("REGEXP_CONTAINS") == 1
    assert "t.dataset_id IN ('shop', 'ops')" in sql                 # deduped, order kept
    assert r"REGEXP_CONTAINS(query, r'\b(shop|ops)\b')" in sql
    sql = query_log_sql("billing", 7, "us", 10, datasets=["pub.shop", "ops", "shop"],
                        drop_machine_sql=True)
    FakeClient.table[sql] = (100, [{"sql": "SELECT 1", "times_seen": 3,
                                    "ran_by": "a@x", "distinct_users": 1}])
    out = tmp_path / "log.csv"

    code = main(["log", "--db", "bq://billing", "--days", "7", "--region", "us", "--limit", "10",
                 "--dataset", "pub.shop", "--datasets", "ops,shop", "--out", str(out)])

    assert code == 0 and fake_bigquery.executed == [sql]
    assert "log -> " in capsys.readouterr().out


def test_log_sql_groups_by_the_masked_shape_and_ranks_by_runs_first():
    """Grouping on the raw text made one report with a different date a
    new shape each time; the shape masks literals, and runs sort before
    users (on one estate no shape had four users, so users sorted nothing)."""
    sql = query_log_sql("billing", 7, "us", 10)
    shape = shape_sql("query")

    assert shape.startswith("TO_HEX(MD5(LOWER(TRIM(REGEXP_REPLACE(")
    assert "REGEXP_REPLACE(query, r\"'[^']*'\", \"'?'\")" in shape
    assert r"r'\b\d+(?:\.\d+)?\b', '?'" in shape
    assert r"r'\s+', ' '" in shape
    assert f"GROUP BY {shape}\n" in sql
    assert "ORDER BY times_seen DESC, distinct_users DESC" in sql
    assert "ARRAY_AGG(query ORDER BY LENGTH(query), query LIMIT 1)[OFFSET(0)] AS sql" in sql


def test_log_exclude_accounts_is_a_not_in_before_any_hash_in_both_statements():
    log = query_log_sql("billing", 7, "us", 10, hash_accounts=True,
                        exclude_accounts=["Me@x.com", "b@y.com", "me@x.com"])
    demand = demand_sql("billing", 7, "us", exclude_accounts=["Me@x.com", "b@y.com"])

    assert "  AND LOWER(user_email) NOT IN ('me@x.com', 'b@y.com')\n" in log
    assert "  AND LOWER(user_email) NOT IN ('me@x.com', 'b@y.com')\n" in demand
    # the filter is on the plain column in WHERE, so a hashed ran_by never sees them
    assert "NOT IN" in log.split("WHERE", 1)[1]
    assert "MD5(LOWER(user_email)) NOT IN" not in log
    assert "NOT IN" not in query_log_sql("billing", 7, "us", 10)
    assert "NOT IN" not in demand_sql("billing", 7, "us")


def test_both_statements_leave_out_service_accounts_before_the_cut():
    """Over 30 days on one estate, 7 service accounts ran 169,789 of
    184,301 jobs and held 2,592 of the 3,000 shapes a --limit 3000 pull
    returned, so the top-N cut was theirs before a person's question
    reached it."""
    log = query_log_sql("PROJECT", 30, "us", 400)
    demand = demand_sql("PROJECT", 30, "us")

    assert "  AND NOT ENDS_WITH(LOWER(user_email), 'gserviceaccount.com')\n" in log
    assert log.index("gserviceaccount.com") < log.index("ORDER BY times_seen")
    # demand reads the window with them in and drops them per row, so the
    # same statement can still count how many jobs they ran
    assert "jobs AS (\n  SELECT * FROM window_jobs\n  WHERE NOT is_service_account\n)" in demand
    assert "(SELECT COUNTIF(is_service_account) FROM window_jobs) AS service_jobs" in demand

    kept_log = query_log_sql("PROJECT", 30, "us", 400, include_service_accounts=True)
    kept_demand = demand_sql("PROJECT", 30, "us", include_service_accounts=True)

    assert "gserviceaccount.com" not in kept_log
    assert "WHERE NOT is_service_account" not in kept_demand
    assert "AS service_jobs" in kept_demand


def test_log_says_how_many_jobs_the_service_accounts_ran(tmp_path, fake_bigquery, capsys):
    demand = demand_sql("PROJECT", 30, "us")
    FakeClient.table[demand] = (100, [
        {"dataset": "DATA.DATASET", "jobs": 14000, "distinct_shapes": 900,
         "distinct_accounts": 82, "bytes_billed_total": 5000, "top_account_share": 0.11,
         "all_jobs": 14512, "service_jobs": 169789, "attributed_jobs": 14000,
         "unattributed_jobs": 512},
    ])
    shapes = query_log_sql("PROJECT", 30, "us", 400, drop_machine_sql=True)
    FakeClient.table[shapes] = (100, [{"sql": "SELECT 1", "times_seen": 3,
                                       "ran_by": "analyst@corp.com", "distinct_users": 1}])

    assert main(["log", "--demand", "--db", "bq://PROJECT", "--days", "30",
                 "--out", str(tmp_path / "demand.csv")]) == 0
    assert ("service accounts: 169789 of 184301 jobs in the window (excluded; "
            "--include-service-accounts keeps them)") in capsys.readouterr().out

    assert main(["log", "--db", "bq://PROJECT", "--days", "30",
                 "--out", str(tmp_path / "log.csv")]) == 0
    # the shapes pull has no count of its own: a second pass over the
    # job-history view would double what it scans
    assert ("  service accounts: excluded (--include-service-accounts keeps them; "
            "log --demand counts how many jobs they ran)") in capsys.readouterr().out


def test_demand_sql_counts_per_dataset_over_the_same_jobs_the_log_reads():
    sql = demand_sql("billing", 30, "us", datasets=["pub.shop", "ops"])

    assert "`billing`.`region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT" in sql
    assert "INTERVAL 30 DAY" in sql
    assert "AND job_type = 'QUERY' AND state = 'DONE' AND error_result IS NULL" in sql
    assert "AND LENGTH(query) < 3000" in sql
    assert "FROM jobs AS j, UNNEST(j.referenced_tables) AS t" in sql
    assert "  WHERE t.dataset_id IN ('shop', 'ops')\n" in sql
    assert f"{shape_sql('query')} AS shape" in sql
    assert "COUNT(DISTINCT shape)" in sql and "COUNT(DISTINCT user_email)" in sql
    assert "ORDER BY d.distinct_accounts DESC, d.jobs DESC" in sql
    # the final projection never names an account
    assert "user_email" not in sql.split("FROM per_dataset AS d")[0].split("SELECT\n  d.dataset")[1]
    assert "t.dataset_id IN" not in demand_sql("billing", 30, "us")


def test_log_demand_writes_one_row_per_dataset_and_says_what_the_top_account_ran(
        tmp_path, fake_bigquery, capsys):
    """Three personal accounts on one estate ran 15,488 jobs each in 30
    days; by jobs that dataset topped the table, by accounts it sinks. The
    line under the table states what the busiest account ran and leaves the
    reading to the person: the old 50-jobs-a-day machinery threshold came
    from a retracted count, and a real analyst there runs about 70 a day."""
    sql = demand_sql("billing", 30, "us", datasets=["shop", "ops", "hr"])
    totals = {"all_jobs": 20000, "attributed_jobs": 15400, "unattributed_jobs": 4000}
    FakeClient.table[sql] = (100, [
        {"dataset": "data.shop", "jobs": 400, "distinct_shapes": 120, "distinct_accounts": 14,
         "bytes_billed_total": 5000, "top_account_share": 0.2, **totals},
        {"dataset": "data.ops", "jobs": 15000, "distinct_shapes": 6, "distinct_accounts": 3,
         "bytes_billed_total": 90000, "top_account_share": 0.34, **totals},
    ])
    out = tmp_path / "demand.csv"

    code = main(["log", "--demand", "--db", "bq://billing", "--days", "30",
                 "--datasets", "shop,ops,hr", "--out", str(out)])

    printed = capsys.readouterr().out
    assert code == 0 and fake_bigquery.executed == [sql]
    assert out.read_text().splitlines() == [
        "dataset,jobs,distinct_shapes,distinct_accounts,bytes_billed_total,"
        "jobs_per_account_day,top_account_share",
        "data.shop,400,120,14,5000,1.0,0.2",
        "data.ops,15000,6,3,90000,166.7,0.34",
    ]
    assert "dataset        jobs   shapes  accounts  jobs/acct/day   share" in printed
    assert "data.shop       400      120        14            1.0    2.0%" in printed
    assert "data.ops      15000        6         3          166.7   75.0%" in printed
    # 0.34 of 15000 jobs over 30 days = 170.0 a day; no line calls it machinery
    assert "data.ops: one account ran 170.0 jobs a day, 34% of its jobs" in printed
    assert "data.shop: one account ran 2.7 jobs a day, 20% of its jobs" in printed
    assert ("--exclude-accounts drops an account's jobs from the pull (log without --demand "
            "shows ran_by per shape)") in printed
    assert "machinery" not in printed
    assert "the 3 listed datasets hold 15400 of 20000 jobs in the window (77.0%)" in printed
    assert "1 of 3 listed datasets had no job in the window: hr" in printed
    assert "4000 of 20000 jobs carry no referenced table (cache hits) and are in no row" in printed
    assert f"demand -> {out}  (2 datasets over 30 days; distinct_accounts is a count" in printed
    assert "spent: 1 queries" in printed


def test_describe_tables_on_bigquery_reads_the_table_description_out_of_its_quotes(fake_bigquery):
    from context_lift.backends_db import describe_tables
    FakeClient.table.update({
        "SELECT table_name, column_name, data_type FROM `pub.shop.INFORMATION_SCHEMA.COLUMNS` "
        "ORDER BY table_name, ordinal_position": (0, [
            {"table_name": "orders", "column_name": "order_id", "data_type": "INT64"},
        ]),
        "SELECT table_name, column_name, description FROM `pub.shop.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS` "
        "WHERE field_path = column_name AND description IS NOT NULL": (0, []),
        "SELECT table_name, option_value FROM `pub.shop.INFORMATION_SCHEMA.TABLE_OPTIONS` "
        "WHERE option_name = 'description'": (0, [
            {"table_name": "orders", "option_value": '"""One row per order.\\nUpdated nightly."""'},
        ]),
    })
    executor = make_executor("bq://billing/pub.shop")

    tables = describe_tables(executor)

    assert len(tables) == 1
    assert tables[0].qualified == "`pub.shop.orders`"
    assert tables[0].dataset == "shop"
    assert tables[0].description == "One row per order.\nUpdated nightly."


def test_describe_tables_undoes_the_escapes_of_the_description_literal(fake_bigquery):
    """One live document carried 8 backslash-quote pairs after only the outer
    quotes were stripped."""
    from context_lift.backends_db import describe_tables
    FakeClient.table.update({
        "SELECT table_name, column_name, data_type FROM `pub.shop.INFORMATION_SCHEMA.COLUMNS` "
        "ORDER BY table_name, ordinal_position": (0, [
            {"table_name": "orders", "column_name": "order_id", "data_type": "INT64"},
        ]),
        "SELECT table_name, column_name, description FROM `pub.shop.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS` "
        "WHERE field_path = column_name AND description IS NOT NULL": (0, []),
        "SELECT table_name, option_value FROM `pub.shop.INFORMATION_SCHEMA.TABLE_OPTIONS` "
        "WHERE option_name = 'description'": (0, [
            {"table_name": "orders",
             "option_value": '"Status \\"Fixed\\" means received; path C:\\\\x"'},
        ]),
    })

    tables = describe_tables(make_executor("bq://billing/pub.shop"))

    assert tables[0].description == 'Status "Fixed" means received; path C:\\x'


def test_schema_tables_accepts_dataset_dot_table_when_the_dataset_is_the_one_in_db(fake_bigquery, tmp_path):
    """catalog-list writes `dataset.table`; pasting that into --tables used to
    match nothing, because INFORMATION_SCHEMA spells table names bare."""
    FakeClient.table.update({
        "SELECT table_name, column_name, data_type FROM `pub.shop.INFORMATION_SCHEMA.COLUMNS` "
        "WHERE table_name IN ('orders') ORDER BY table_name, ordinal_position": (0, [
            {"table_name": "orders", "column_name": "order_id", "data_type": "INT64"},
        ]),
        "SELECT table_name, column_name, description FROM `pub.shop.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS` "
        "WHERE field_path = column_name AND description IS NOT NULL AND table_name IN ('orders')": (0, []),
    })
    out = tmp_path / "schema.txt"

    code = main(["schema", "--db", "bq://billing/pub.shop", "--tables", "shop.orders", "--out", str(out)])

    assert code == 0
    assert out.read_text() == "CREATE TABLE `pub.shop.orders` (\n  order_id INT64\n);\n"
    # a bare name belongs to every dataset read; a qualified one to its own
    from context_lift.backends_db import _wanted_in
    assert _wanted_in({"shop.orders", "pub.shop.users", "other.items", "events"}, "pub.shop") \
        == {"orders", "users", "events"}
    assert _wanted_in({"shop.orders", "other.items", "events"}, "billing.other") == {"items", "events"}


# --- several datasets in one read ---------------------------------------------

def _two_datasets():
    """The three INFORMATION_SCHEMA views of `pub.shop` (another project's)
    and `billing.ops` (the billing project's own), one table each."""
    FakeClient.table.update({
        "SELECT table_name, column_name, data_type FROM `pub.shop.INFORMATION_SCHEMA.COLUMNS` "
        "ORDER BY table_name, ordinal_position": (0, [
            {"table_name": "orders", "column_name": "order_id", "data_type": "INT64"},
        ]),
        "SELECT table_name, column_name, description FROM `pub.shop.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS` "
        "WHERE field_path = column_name AND description IS NOT NULL": (0, []),
        "SELECT table_name, option_value FROM `pub.shop.INFORMATION_SCHEMA.TABLE_OPTIONS` "
        "WHERE option_name = 'description'": (0, []),
        "SELECT table_name, column_name, data_type FROM `billing.ops.INFORMATION_SCHEMA.COLUMNS` "
        "ORDER BY table_name, ordinal_position": (0, [
            {"table_name": "jobs", "column_name": "job_id", "data_type": "INT64"},
            {"table_name": "jobs", "column_name": "state", "data_type": "STRING"},
        ]),
        "SELECT table_name, column_name, description FROM `billing.ops.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS` "
        "WHERE field_path = column_name AND description IS NOT NULL": (0, [
            {"table_name": "jobs", "column_name": "state", "description": "RUNNING or DONE"},
        ]),
        "SELECT table_name, option_value FROM `billing.ops.INFORMATION_SCHEMA.TABLE_OPTIONS` "
        "WHERE option_name = 'description'": (0, []),
    })


def test_schema_over_two_datasets_writes_every_table_fully_qualified_in_one_file(
        tmp_path, fake_bigquery, capsys):
    """A 27-dataset estate was 27 runs of `schema` and a hand-made
    concatenation; one --datasets is one file, in the order given."""
    _two_datasets()
    out = tmp_path / "schema.txt"

    code = main(["schema", "--db", "bq://billing/pub.shop", "--datasets", "shop,ops",
                 "--out", str(out)])

    assert code == 0
    # `shop` is the id of the dataset in --db, so it keeps that project;
    # `ops` is bare, so it is the billing project's and needs no project
    assert out.read_text() == ("CREATE TABLE `pub.shop.orders` (\n  order_id INT64\n);\n"
                               "CREATE TABLE ops.jobs (\n  job_id INT64,\n"
                               "  state STRING  -- RUNNING or DONE\n);\n")
    printed = capsys.readouterr().out
    assert "reading 3 INFORMATION_SCHEMA views x 2 datasets = 6 metadata queries" in printed
    assert "schema -> " in printed and "(2 tables across 2 datasets; give it to generate --schema)" in printed
    assert len([s for s in fake_bigquery.executed if "INFORMATION_SCHEMA" in s]) == 6


def test_a_tables_file_sets_both_the_datasets_and_the_tables_read(tmp_path, fake_bigquery, capsys):
    """catalog-list writes `dataset.table` lines; that file alone is the
    scope: which datasets to read and which tables to keep in each."""
    FakeClient.table.update({
        "SELECT table_name, column_name, data_type FROM `billing.shop.INFORMATION_SCHEMA.COLUMNS` "
        "WHERE table_name IN ('orders') ORDER BY table_name, ordinal_position": (0, [
            {"table_name": "orders", "column_name": "order_id", "data_type": "INT64"},
        ]),
        "SELECT table_name, column_name, data_type FROM `billing.ops.INFORMATION_SCHEMA.COLUMNS` "
        "WHERE table_name IN ('jobs') ORDER BY table_name, ordinal_position": (0, [
            {"table_name": "jobs", "column_name": "job_id", "data_type": "INT64"},
        ]),
    })
    listed = tmp_path / "catalog.txt"
    listed.write_text("# contextlift catalog-list: project p\nshop.orders\n\nops.jobs\n")
    out = tmp_path / "schema.txt"
    workspace = tmp_path / "ws"

    code = main(["--workspace", str(workspace), "schema", "--db", "bq://billing",
                 "--tables-file", str(listed), "--out", str(out)])

    assert code == 0
    assert out.read_text() == ("CREATE TABLE shop.orders (\n  order_id INT64\n);\n"
                               "CREATE TABLE ops.jobs (\n  job_id INT64\n);\n")
    columns_reads = [s for s in fake_bigquery.executed if "INFORMATION_SCHEMA.COLUMNS" in s]
    assert [s.split("`")[1] for s in columns_reads] == ["billing.shop.INFORMATION_SCHEMA.COLUMNS",
                                                        "billing.ops.INFORMATION_SCHEMA.COLUMNS"]
    manifest = json.loads(next(workspace.glob("runs/*schema*/manifest.json")).read_text())
    assert manifest["inputs"]["tables_file"]["sha256"]          # the list is an input of the run


def test_a_project_only_db_names_the_three_ways_to_give_a_dataset(tmp_path, fake_bigquery, capsys):
    code = main(["schema", "--db", "bq://billing", "--out", str(tmp_path / "s.txt")])

    assert code == 2
    assert capsys.readouterr().out.strip() == (
        "schema on BigQuery needs a dataset: --db bq://PROJECT/DATASET, --datasets A,B, "
        "or --tables-file FILE (one dataset.table per line)")
    assert fake_bigquery.dry_runs == [] and fake_bigquery.executed == []
    assert main(["draft-context", "--db", "bq://billing", "--out", str(tmp_path / "docs")]) == 2
    assert capsys.readouterr().out.startswith("draft-context on BigQuery needs a dataset")


def test_draft_context_over_two_datasets_writes_one_file_per_table_with_the_dataset_in_the_name(
        tmp_path, fake_bigquery, capsys):
    _two_datasets()
    out = tmp_path / "docs"

    code = main(["draft-context", "--db", "bq://billing/pub.shop", "--datasets", "shop,ops",
                 "--out", str(out)])

    assert code == 0
    assert sorted(p.name for p in out.iterdir()) == ["ops.jobs.md", "shop.orders.md"]
    assert (out / "shop.orders.md").read_text().splitlines()[0] == "# `pub.shop.orders`"
    assert (out / "ops.jobs.md").read_text().splitlines()[0] == "# ops.jobs"
    assert "drafted 2 documents across 2 datasets -> " in capsys.readouterr().out


def test_profile_prefixes_the_table_with_its_dataset(fake_bigquery):
    """One values file now spans datasets; `orders.status` would not say
    which dataset's orders."""
    from context_lift.profile import profile_values, render_values
    FakeClient.table.update({
        "SELECT table_name, column_name FROM `billing.shop.INFORMATION_SCHEMA.COLUMNS` "
        "WHERE data_type = 'STRING' ORDER BY table_name, ordinal_position": (0, [
            {"table_name": "orders", "column_name": "status"},
        ]),
        "SELECT table_name, column_name FROM `billing.ops.INFORMATION_SCHEMA.COLUMNS` "
        "WHERE data_type = 'STRING' ORDER BY table_name, ordinal_position": (0, [
            {"table_name": "jobs", "column_name": "state"},
        ]),
        "SELECT APPROX_COUNT_DISTINCT(`status`) AS d_0, COUNT(`status`) AS r_0 FROM `billing.shop.orders`":
            (100, [{"d_0": 1, "r_0": 4}]),
        "SELECT `status` AS v, COUNT(*) AS n FROM `billing.shop.orders` WHERE `status` IS NOT NULL "
        "GROUP BY v ORDER BY n DESC, v": (100, [{"v": "Shipped", "n": 4}]),
        "SELECT APPROX_COUNT_DISTINCT(`state`) AS d_0, COUNT(`state`) AS r_0 FROM `billing.ops.jobs`":
            (100, [{"d_0": 2, "r_0": 5}]),
        "SELECT `state` AS v, COUNT(*) AS n FROM `billing.ops.jobs` WHERE `state` IS NOT NULL "
        "GROUP BY v ORDER BY n DESC, v": (100, [{"v": "DONE", "n": 3}, {"v": "RUNNING", "n": 2}]),
    })

    found = profile_values(make_executor("bq://billing"), datasets=["shop", "ops"])

    assert [(c.table, c.column) for c in found] == [("shop.orders", "status"), ("ops.jobs", "state")]
    assert '- ops.jobs.state is one of: "DONE", "RUNNING"' in render_values(found)


def test_log_can_keep_only_the_queries_a_gold_set_wants():
    """The filters existed on `mine` and only there, which is one stage too
    late: `log` keeps the top --limit shapes BY TRAFFIC, and a dashboard runs
    hundreds of times for one viewer.

    Counted on one estate's 180-day history: 1,205,904 successful SELECTs,
    290,940 with a GROUP BY, 29,684 that also named two or more tables, and
    80,904 written by dbt or GitHub Actions. So the analytical share is 2.5%,
    a top-2,000 pull by traffic held about fifty of them, and `mine
    --min-tables 2` then cut 446 candidates down to 2 usable ones.
    """
    plain = query_log_sql("billing", 180, "us", 2000)
    assert "ARRAY_LENGTH" not in plain, "no filter unless asked for one"
    assert "GROUP BY'" not in plain and "dbt_version" not in plain

    filtered = query_log_sql("billing", 180, "us", 2000, min_tables=2,
                             require_analytical=True, drop_machine_sql=True)
    assert ">= 2" in filtered, "two or more tables"
    assert r"\bGROUP BY\b" in filtered
    assert "APPROX_COUNT_DISTINCT" in filtered, \
        "GROUP BY alone misses SELECT COUNT(*) FROM t, which is a question"
    assert "dbt_version" in filtered and "github-actions" in filtered
    assert "NOT (REGEXP_CONTAINS" in filtered, "machine SQL is excluded, not required"

    # the machine-SQL pattern has to match the real header, and inside a
    # BigQuery raw string a backslash-quote stays a backslash-quote, so
    # r'\"app\"' would never match "app". This is the bug that check catches.
    assert '"app"' in filtered
    assert '\\"app\\"' not in filtered

    # each filter is independent: asking for one does not bring the others
    only_tables = query_log_sql("billing", 180, "us", 2000, min_tables=3)
    assert ">= 3" in only_tables
    assert "dbt_version" not in only_tables and r"\bGROUP BY\b" not in only_tables


def test_log_can_keep_one_teams_jobs_and_nobody_elses():
    """A gold set about ONE team needs that team's queries, which is the only
    way to ask whether the sales rules helped the sales team rather than
    whether they helped on average."""
    sql = query_log_sql("billing", 30, "us", 100,
                        only_accounts=["Sales.Lead@corp.com", "b@corp.com"])
    assert "LOWER(user_email) IN ('sales.lead@corp.com', 'b@corp.com')" in sql, \
        "matched lower-cased, the way --exclude-accounts matches"
    # the blanket service-account exclusion steps aside: naming one service
    # account and getting nothing back is silence nobody debugs
    assert "NOT ENDS_WITH" not in sql
    assert "NOT ENDS_WITH" in query_log_sql("billing", 30, "us", 100)


def test_log_can_keep_only_the_jobs_carrying_a_label():
    """dbt, Looker and the Conversational Analytics API all label their own
    jobs, so a team that labels its scheduled work can select it without
    naming a person, which is the version that survives somebody leaving."""
    sql = query_log_sql("billing", 30, "us", 100, labels=["team=Sales", "ca-bq-job"])
    assert ("EXISTS (SELECT 1 FROM UNNEST(labels) AS l WHERE l.key = 'team' "
            "AND LOWER(l.value) = 'sales')") in sql
    # a bare key means "carries this label whatever its value", which is how
    # the API's own ca-bq-job marker is used
    assert "WHERE l.key = 'ca-bq-job')" in sql
    assert "l.value" not in sql.split("ca-bq-job")[1]
    assert "UNNEST(labels)" not in query_log_sql("billing", 30, "us", 100)


def test_the_two_account_filters_are_quoted_against_injection():
    """An account name reaches the SQL as a literal. A quote in it used to
    end the string."""
    sql = query_log_sql("billing", 30, "us", 100,
                        only_accounts=["o'brien@corp.com"], labels=["it's=odd"])
    assert r"\'" in sql, "the quote is escaped, not closing the literal"
    assert "o'brien@corp.com'," not in sql


def test_both_log_statements_take_the_same_team_filters():
    """`--demand` answers "where is the demand", and asking it for one team
    is the same question. A filter on one statement and not the other is how
    two commands come to disagree about who is in scope."""
    from context_lift.backends_db import demand_sql
    demand = demand_sql("billing", 30, "us", only_accounts=["a@corp.com"],
                        labels=["team=sales"])
    assert "LOWER(user_email) IN ('a@corp.com')" in demand
    assert "UNNEST(labels)" in demand


def test_the_sqlite_prefix_is_accepted_like_the_other_two():
    """`duckdb://` and `bq://` were both prefixed and sqlite was not, so
    `--db sqlite:///data.db` reached sqlite3 as a literal filename and failed
    with "unable to open database file".

    That form is not hypothetical: `contextlift demo` prints it as the next
    command to run, and that output is the recording in README. A reader who
    followed the demo's own advice hit the error.
    """
    from context_lift.backends_db import parse_db_spec

    for typed, expected in (
        ("/tmp/x.db", "/tmp/x.db"),               # a plain path still works
        ("x.db", "x.db"),
        ("sqlite:///tmp/x.db", "/tmp/x.db"),      # what demo prints
        ("sqlite://x.db", "x.db"),                # relative after the prefix
        ("sqlite:////tmp/x.db", "/tmp/x.db"),     # the SQLAlchemy four-slash
    ):
        parsed = parse_db_spec(typed)
        assert parsed.kind == "sqlite", f"{typed} is not read as sqlite"
        assert parsed.spec == expected, f"{typed} became {parsed.spec!r}"


def test_the_other_two_prefixes_are_untouched():
    from context_lift.backends_db import parse_db_spec

    assert parse_db_spec("duckdb://x.duckdb").kind == "duckdb"
    assert parse_db_spec("duckdb://x.duckdb").spec == "duckdb://x.duckdb"
    bq = parse_db_spec("bq://proj/ds")
    assert (bq.kind, bq.project, bq.dataset) == ("bigquery", "proj", "proj.ds")


def test_every_db_form_the_demo_prints_actually_parses():
    """Pins the pair: whatever demo tells a reader to type must parse. If the
    demo's wording changes, this test is what notices."""
    import re

    from context_lift.backends_db import parse_db_spec
    from context_lift import demo

    lines: list[str] = []
    demo.run(lines.append)
    typed = set(re.findall(r"--db\s+(\S+)", "\n".join(lines)))
    assert typed, "demo prints no --db example any more"
    for value in typed:
        parsed = parse_db_spec(value)
        assert parsed.kind in ("sqlite", "duckdb", "bigquery"), \
            f"demo prints --db {value} and it does not parse"
