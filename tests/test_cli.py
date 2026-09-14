# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Behavior tests for the command-line interface.

Each test writes a temp SQLite DB and CSV(s), runs main() with argv, and checks
the printed report and exit code — no cloud, the judge stays off.
"""
import argparse
import csv
import json
import sqlite3

import pytest

from context_lift.cli import _build_parser, format_gate_report, main
from context_lift.gate import GateFinding
from context_lift.workspace import rebuild_index


def _seed_db(tmp_path):
    """Create a 2-row emp table in a temp DB and return its path."""
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE emp(name TEXT, age INT)")
    con.executemany("INSERT INTO emp VALUES (?, ?)", [("ann", 30), ("bob", 40)])
    con.commit()
    con.close()
    return str(p)


def _write_csv(path, header, rows):
    """Write a CSV with a header line and quoted rows."""
    import csv

    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return str(path)


def test_score_prints_rates_and_exits_zero(tmp_path, capsys):
    db = _seed_db(tmp_path)
    preds = _write_csv(
        tmp_path / "p.csv",
        ["id", "question", "gold_sql", "pred_sql"],
        [
            ("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp"),       # right
            ("q2", "old?", "SELECT name FROM emp WHERE age > 35", "SELECT name FROM emp"),  # wrong
        ],
    )
    code = main(["score", "--db", db, "--predictions", preds])
    out = capsys.readouterr().out
    assert code == 0
    assert "execution accuracy: 0.500" in out
    assert "1/2" in out


def test_gate_reports_clean_set_and_exits_zero(tmp_path, capsys):
    db = _seed_db(tmp_path)
    golds = _write_csv(
        tmp_path / "g.csv",
        ["id", "gold_sql"],
        [("q1", "SELECT name FROM emp")],
    )
    code = main(["gate", "--db", db, "--golds", golds])
    out = capsys.readouterr().out
    assert code == 0
    assert "1 clean, 0 flagged" in out


def _round_files(tmp_path):
    db = _seed_db(tmp_path)
    spec = tmp_path / "spec"
    (spec / "rules").mkdir(parents=True)
    (spec / "rules" / "business.md").write_text("Revenue excludes returns.\n")
    rows = [(f"q{i}", f"question {i}", f"SELECT {i} AS n") for i in range(12)]
    golds = _write_csv(tmp_path / "golds.csv",
                       ["id", "question", "gold_sql"], rows)
    control = _write_csv(tmp_path / "control.csv", ["id", "pred_sql"],
                         [(row[0], row[2]) for row in rows])
    treatment = _write_csv(tmp_path / "treatment.csv", ["id", "pred_sql"],
                           [(row[0], row[2]) for row in rows])
    curate = tmp_path / "curate_ids.txt"
    holdout = tmp_path / "holdout_ids.txt"
    curate.write_text("\n".join(row[0] for row in rows[:6]) + "\n")
    holdout.write_text("\n".join(row[0] for row in rows[6:]) + "\n")
    return db, spec, golds, control, treatment, curate, holdout


def test_round_saves_a_score_report_that_triage_reads_offline(tmp_path, capsys):
    db, spec, golds, control, treatment, curate, holdout = _round_files(tmp_path)
    report = tmp_path / "round_score.json"
    cache = tmp_path / "golds_cache.json"

    code = main([
        "round", "--db", db, "--spec", str(spec), "--golds", golds,
        "--predictions-control", control, "--predictions-treatment", treatment,
        "--curate-ids", str(curate), "--holdout-ids", str(holdout),
        "--target-lift", "100", "--yes", "--budget", "0",
        "--extra-columns", "tolerate", "--gold-cache", str(cache),
        "--save-report", str(report),
    ])

    assert code == 0
    assert report.is_file()
    assert cache.is_file()
    assert main(["triage", "--report", str(report)]) == 0
    assert "report ->" in capsys.readouterr().out


def test_round_applies_data_projects_before_its_free_gates(tmp_path, monkeypatch):
    db, spec, golds, control, treatment, curate, holdout = _round_files(tmp_path)
    seen = []
    monkeypatch.setattr("context_lift.gate.set_data_projects",
                        lambda projects: seen.append(projects))

    code = main([
        "round", "--db", db, "--spec", str(spec), "--golds", golds,
        "--predictions-control", control, "--predictions-treatment", treatment,
        "--curate-ids", str(curate), "--holdout-ids", str(holdout),
        "--data-projects", "warehouse-a,warehouse-b", "--target-lift", "100",
        "--dry-run",
    ])

    assert code == 0
    assert seen == [["warehouse-a", "warehouse-b"]]


def test_gate_report_aligns_the_defect_column_across_uneven_ids():
    # Different-length ids must not stagger the defect column.
    findings = [
        GateFinding("a", ("ZERO_ROWS",), "returns no rows"),
        GateFinding("much_longer_id", ("EXEC_FAIL",), "boom"),
    ]
    out = format_gate_report(findings)
    flagged = [line for line in out.splitlines() if line.startswith("  ")]
    starts = {line.index(line.split()[1]) for line in flagged}  # column of the defect token
    assert len(starts) == 1


def test_gate_exits_nonzero_when_a_gold_is_flagged(tmp_path, capsys):
    db = _seed_db(tmp_path)
    golds = _write_csv(
        tmp_path / "g.csv",
        ["id", "gold_sql"],
        [
            ("good", "SELECT name FROM emp"),
            ("bad", "SELECT nope FROM emp"),  # errors -> EXEC_FAIL
        ],
    )
    code = main(["gate", "--db", db, "--golds", golds])
    out = capsys.readouterr().out
    assert code == 1  # so a CI step fails on a defective answer key
    assert "EXEC_FAIL" in out
    assert "bad" in out


def test_next_prints_paths_joined_to_the_workspace_so_the_line_runs_from_anywhere(tmp_path, capsys, monkeypatch):
    """Field: `next` printed `--golds dataset/questions.csv`; run from one
    level above the workspace, gate ended in a FileNotFoundError traceback."""
    import shutil
    db = _seed_db(tmp_path)
    ws = tmp_path / "ws"
    (ws / "dataset").mkdir(parents=True)
    shutil.copyfile(db, ws / "emp.sqlite")
    _write_csv(ws / "dataset" / "questions.csv", ["id", "question", "gold_sql"],
               [("q1", "names?", "SELECT name FROM emp")])
    monkeypatch.chdir(tmp_path)                      # not inside the workspace

    code = main(["--workspace", "ws", "next"])

    out = capsys.readouterr().out
    assert code == 0
    line = next(l for l in out.splitlines() if l.strip().startswith("$ "))
    assert f"--workspace {ws.resolve()} gate" in line
    assert f"--golds {ws.resolve() / 'dataset' / 'questions.csv'}" in line
    assert f"--db {ws.resolve() / 'emp.sqlite'}" in line
    assert "--timeout 60" in line                    # a number is not a path
    assert main(["--workspace", "ws", "next", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"]["argvs"][0][4] == "dataset/questions.csv"          # relative: start_job's cwd
    assert payload["action"]["argvs_absolute"][0][4] == str(ws.resolve() / "dataset" / "questions.csv")


def test_a_missing_input_file_is_one_line_and_exit_2_not_a_traceback(tmp_path, capsys):
    db = _seed_db(tmp_path)
    ws = tmp_path / "ws"

    code = main(["--workspace", str(ws), "gate", "--db", db, "--golds", "dataset/questions.csv"])

    err = capsys.readouterr().err
    assert code == 2
    assert "contextlift: dataset/questions.csv: no such file" in err
    assert "not from --workspace" in err
    assert "Traceback" not in err
    manifest = json.loads(next(ws.glob("runs/*gate*/manifest.json")).read_text())
    assert manifest["exit_code"] == 2
    assert main(["gate", "--db", db, "--golds", "dataset/questions.csv"]) == 2   # same line without a workspace
    assert "Traceback" not in capsys.readouterr().err

    # an absolute path that is not there gets no hint about the current
    # directory: it sent a reader after a problem that was not the cause
    absent = tmp_path / "dataset" / "questions.csv"
    assert main(["gate", "--db", db, "--golds", str(absent)]) == 2
    err = capsys.readouterr().err
    assert f"contextlift: {absent}: no such file" in err
    assert "not from --workspace" not in err


def test_score_routing_prints_both_rates_and_exits_zero(tmp_path, capsys):
    preds = _write_csv(
        tmp_path / "r.csv",
        ["id", "question", "gold_table", "pred_table"],
        [
            ("r1", "orders?", "sales.orders", "sales.orders"),      # exact
            ("r2", "returns?", "sales.returns", "sales.orders"),    # dataset only
        ],
    )
    code = main(["score-routing", "--predictions", preds])
    out = capsys.readouterr().out
    assert code == 0
    assert "dataset accuracy: 1.000" in out
    assert "table accuracy:   0.500" in out
    assert "dataset only" in out
    assert "snapped" not in out  # no catalog -> no snap line at all


def test_score_routing_with_catalog_reports_snap_count(tmp_path, capsys):
    preds = _write_csv(
        tmp_path / "r.csv",
        ["id", "question", "gold_table", "pred_table"],
        [
            ("r1", "stock?", "inventory.stock_levels", "inventory.stock_level"),
        ],
    )
    catalog = tmp_path / "catalog.txt"
    catalog.write_text("# valid tables\ninventory.stock_levels\nsales.orders\n")
    code = main(
        ["score-routing", "--predictions", preds, "--catalog", str(catalog)]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "snapped 1 of 1 predictions to the catalog" in out
    assert "table accuracy:   1.000" in out  # the snap upgraded the miss
    assert "inventory.stock_level -> inventory.stock_levels" in out


def test_gate_clean_out_writes_only_the_passing_golds(tmp_path, capsys):
    # The report says which golds are broken; the filtered CSV is what every
    # next stage actually needs, so gate must hand it over itself.
    import csv

    db = _seed_db(tmp_path)
    golds = _write_csv(
        tmp_path / "g.csv",
        ["id", "gold_sql"],
        [("good", "SELECT name FROM emp"),
         ("broken", "SELECT nope FROM emp")],
    )
    clean_path = tmp_path / "clean.csv"
    code = main(["gate", "--db", db, "--golds", golds,
                 "--clean-out", str(clean_path)])
    assert code == 1  # still fails CI: a flagged gold exists
    rows = list(csv.DictReader(open(clean_path, newline="")))
    assert [r["id"] for r in rows] == ["good"]
    assert "clean golds ->" in capsys.readouterr().out


def test_ablate_show_names_the_flipped_questions(tmp_path, capsys):
    db = _seed_db(tmp_path)
    right = "SELECT name FROM emp"
    wrong = "SELECT name FROM emp WHERE age > 999"
    gold = "SELECT name FROM emp"
    without = _write_csv(
        tmp_path / "a.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q1", "?", gold, right), ("q2", "?", gold, right)])
    with_ = _write_csv(
        tmp_path / "b.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q1", "?", gold, right), ("q2", "?", gold, wrong)])
    code = main(["ablate", "--db", db, "--without-context", without,
                 "--with-context", with_, "--show"])
    out = capsys.readouterr().out
    assert code == 0
    assert "context broke (1):" in out
    assert "q2" in out


def test_score_json_is_machine_readable(tmp_path, capsys):
    import json

    db = _seed_db(tmp_path)
    preds = _write_csv(
        tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q1", "?", "SELECT name FROM emp", "SELECT name FROM emp")])
    code = main(["score", "--db", db, "--predictions", preds, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["execution"]["k"] == 1
    assert payload["results"][0]["id"] == "q1"


def test_attribute_names_the_rule_behind_a_break(tmp_path, capsys):
    # End to end through the CLI: the glossary redirects q2 to a table where
    # the filter matches nothing, the ablation flips, and the report points at
    # the glossary line.
    db = _seed_db(tmp_path)
    gold = "SELECT name FROM emp WHERE age > 35"
    without = _write_csv(
        tmp_path / "a.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q2", "?", gold, "SELECT name FROM emp WHERE age > 35")])
    # the glossary redirects to a table that does not exist in this DB, the
    # canonical wrong-table-family failure
    with_ = _write_csv(
        tmp_path / "b.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q2", "?", gold, "SELECT name FROM STAFF_DIRECTORY WHERE age_years > 35")])
    context = tmp_path / "rules.md"
    context.write_text('- "employee age" -> STAFF_DIRECTORY.age_years\n')
    code = main(["attribute", "--db", db, "--without-context", without,
                 "--with-context", with_, "--context", str(context)])
    out = capsys.readouterr().out
    assert code == 0
    assert "suspect rules for regressions" in out
    assert '"employee age"' in out
    assert "broke q2" in out


def test_score_timeout_turns_a_runaway_prediction_into_an_error(tmp_path, capsys):
    db = _seed_db(tmp_path)
    runaway = ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n "
               "WHERE i < 100000000) SELECT MAX(i) FROM n")
    preds = _write_csv(
        tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q1", "?", "SELECT name FROM emp", runaway)])
    code = main(["score", "--db", db, "--predictions", preds,
                 "--timeout", "0.2"])
    out = capsys.readouterr().out
    assert code == 0
    assert "PRED_ERROR" in out


def test_ablate_reuses_saved_reports_without_touching_the_db(tmp_path, capsys):
    # Scoring executes SQL; the paired analysis afterwards is free. A saved
    # report must let ablate run against a database path that does not even
    # exist any more.
    db = _seed_db(tmp_path)
    right = "SELECT name FROM emp"
    wrong = "SELECT name FROM emp WHERE age > 999"
    a = _write_csv(tmp_path / "a.csv", ["id", "question", "gold_sql", "pred_sql"],
                   [("q1", "?", right, right)])
    b = _write_csv(tmp_path / "b.csv", ["id", "question", "gold_sql", "pred_sql"],
                   [("q1", "?", right, wrong)])
    main(["score", "--db", db, "--predictions", a,
          "--save-report", str(tmp_path / "a.json")])
    main(["score", "--db", db, "--predictions", b,
          "--save-report", str(tmp_path / "b.json")])
    capsys.readouterr()
    code = main(["ablate", "--db", str(tmp_path / "gone.db"),
                 "--without-report", str(tmp_path / "a.json"),
                 "--with-report", str(tmp_path / "b.json"), "--show"])
    out = capsys.readouterr().out
    assert code == 0
    assert "context broke (1):" in out


def test_split_writes_the_filtered_rows_with_every_column_when_asked(tmp_path, capsys):
    questions = _write_csv(
        tmp_path / "q.csv", ["id", "question", "gold_sql", "note"],
        [(f"q{i}", f"question {i}", f"SELECT {i}", f"n{i}") for i in range(10)],
    )

    code = main(["split", "--golds", questions,
                 "--out-curate", str(tmp_path / "c.txt"),
                 "--out-holdout", str(tmp_path / "h.txt"),
                 "--out-curate-csv", str(tmp_path / "c.csv"),
                 "--out-holdout-csv", str(tmp_path / "h.csv")])

    assert code == 0
    import csv as _csv
    curate = list(_csv.DictReader(open(tmp_path / "c.csv", newline="")))
    holdout = list(_csv.DictReader(open(tmp_path / "h.csv", newline="")))
    assert list(curate[0]) == ["id", "question", "gold_sql", "note"]
    assert len(curate) + len(holdout) == 10
    assert {r["id"] for r in curate} == set((tmp_path / "c.txt").read_text().split())
    assert {r["id"] for r in holdout} == set((tmp_path / "h.txt").read_text().split())


def test_triage_from_a_saved_report_runs_no_sql(tmp_path, capsys):
    db = _seed_db(tmp_path)
    preds = _write_csv(
        tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q1", "names?", "SELECT name FROM emp", "SELECT age FROM emp"),
         ("q2", "old?", "SELECT name FROM emp WHERE age > 35", "SELECT 1 FROM nope")],
    )
    report = tmp_path / "report.json"
    main(["score", "--db", db, "--predictions", preds, "--save-report", str(report)])
    capsys.readouterr()
    main(["triage", "--db", db, "--predictions", preds, "--json"])
    from_sql = capsys.readouterr().out

    code = main(["triage", "--report", str(report), "--json"])

    assert code == 0
    assert capsys.readouterr().out == from_sql
    assert main(["triage", "--json"]) == 2
    assert "needs --report" in capsys.readouterr().out


def test_ablate_from_reports_needs_no_db_flag_at_all(tmp_path, capsys):
    db = _seed_db(tmp_path)
    preds = _write_csv(
        tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp")],
    )
    report = tmp_path / "report.json"
    main(["score", "--db", db, "--predictions", preds, "--save-report", str(report)])
    capsys.readouterr()

    code = main(["ablate", "--without-report", str(report), "--with-report", str(report)])

    assert code == 0
    assert "lift" in capsys.readouterr().out
    assert main(["ablate", "--without-context", preds, "--with-context", preds]) == 2


def test_generate_refuses_above_budget_before_any_call_and_records_spend_otherwise(tmp_path, capsys):
    questions = _write_csv(
        tmp_path / "q.csv", ["id", "question", "gold_sql"],
        [("q1", "names?", "SELECT name FROM emp"), ("q2", "ages?", "SELECT age FROM emp")],
    )
    schema = tmp_path / "schema.txt"
    schema.write_text("CREATE TABLE emp(name TEXT, age INT)")
    workspace = tmp_path / "ws"
    base = ["--workspace", str(workspace), "generate", "--questions", questions,
            "--schema", str(schema), "--out", str(tmp_path / "arm.csv"),
            "--backend", "echo"]

    refused = main(base + ["--budget", "0.0000001"])
    out = capsys.readouterr().out
    assert refused == 2
    assert "refusing to start" in out
    assert not (tmp_path / "arm.csv").exists()

    assert main(base + ["--reps", "2"]) == 0
    out = capsys.readouterr().out
    assert "estimated: 2 questions x 2 reps = 4 calls" in out
    assert "spent: 4 calls" in out
    import json as _json
    manifests = [_json.loads(p.read_text()) for p in workspace.glob("runs/*generate*/manifest.json")]
    assert len(manifests) == 2
    refused_manifest = next(m for m in manifests if m["exit_code"] == 2)
    ran_manifest = next(m for m in manifests if m["exit_code"] == 0)
    telemetry = ran_manifest["telemetry"]
    assert telemetry["calls"] == 4
    assert telemetry["stopped_at_budget"] is False
    assert telemetry["price_table"]["model"] == "gemini-2.5-flash"
    assert telemetry["estimated_cost_usd"] > 0
    assert refused_manifest.get("telemetry") is None   # it never got as far as a call


def _fake_warehouse(monkeypatch, dry_runs):
    """Stand in for google.cloud.bigquery: a dry-run answers from `dry_runs`
    (sql -> bytes, or an exception to raise) and a real run returns no
    rows. Enough to reach the estimate, the budget check and a run."""
    from types import SimpleNamespace

    class Job:
        def __init__(self, scanned):
            self.total_bytes_processed = scanned
            self.total_bytes_billed = scanned

        def result(self, timeout=None, max_results=None):
            return []

    class Client:
        def __init__(self, project=None, location=None, client_info=None):
            self.client_info = client_info

        def query(self, sql, job_config=None, location=None):
            if isinstance(dry_runs[sql], Exception):
                raise dry_runs[sql]
            return Job(dry_runs[sql])

    class Config:
        def __init__(self, **settings):
            self.__dict__.update(settings)

    monkeypatch.setattr("context_lift.executor._load_bigquery",
                        lambda: SimpleNamespace(Client=Client, QueryJobConfig=Config))


def test_score_estimate_names_what_the_ceiling_rejects_and_prices_only_the_rest(
        tmp_path, capsys, monkeypatch):
    """On one estate the estimate priced 18 predictions at $1.86; the 10 GB
    ceiling then rejected 7 of them for free and the rep billed $0.16. The
    estimate has to say which statements will not run."""
    _fake_warehouse(monkeypatch, {
        "SELECT name FROM emp": 500_000_000,
        "SELECT name FROM emp WHERE age > 35": 3_000_000_000,
        "SELECT nope FROM emp": ValueError("Unrecognized name: nope"),
    })
    preds = _write_csv(
        tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp WHERE age > 35"),
         ("q2", "old?", "SELECT name FROM emp", "SELECT nope FROM emp")],
    )

    code = main(["score", "--db", "bq://billing/data.sales", "--predictions", preds,
                 "--max-bytes", "1000000000", "--estimate-only"])

    out = capsys.readouterr().out
    assert code == 0
    assert ("estimated: 3 queries, 3.500 GB (dry-run, 1 rejected at dry-run; 1 over the 1 GB "
            "ceiling, not priced), ~$0.0028 on-demand") in out
    assert ("1 of 3 statements are over the --max-bytes ceiling (1 GB) and will be rejected "
            "without running (largest 3.00 GB); raise --max-bytes or narrow them") in out
    assert ("1 statements will not run and cost nothing (1 the SQL does not parse, 0 the "
            "platform refused the call, 0 not read-only):\n  q2 prediction\n") in out


def test_score_budget_is_checked_against_what_will_run_not_what_the_ceiling_rejects(
        tmp_path, capsys, monkeypatch):
    """--budget 2.00 once refused an arm estimated at $2.20 whose own 10 GB
    ceiling capped it at $1.12; it billed $0.045 when the budget was lifted."""
    _fake_warehouse(monkeypatch, {"SELECT name FROM emp": 500_000_000,
                                  "SELECT name FROM emp WHERE age > 35": 3_000_000_000})
    preds = _write_csv(
        tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp WHERE age > 35")],
    )
    base = ["score", "--db", "bq://billing/data.sales", "--predictions", preds,
            "--max-bytes", "1000000000"]

    # 3.5 GB dry-run would be $0.0199; the 0.5 GB that will run is $0.0028
    assert main(base + ["--budget", "0.01"]) == 0
    assert "refusing to start" not in capsys.readouterr().out
    assert main(base + ["--budget", "0.002"]) == 2
    assert ("refusing to start: estimate $0.0028 is above the budget $0.0020"
            in capsys.readouterr().out)


def test_draft_rules_keeps_the_model_reply_and_says_why_no_rule_survived(tmp_path, capsys):
    """A paid run drafted 0 rules from a reply of 8 well-shaped rules that
    cited q1..q12, the ids of the prompt's own examples. The reply was thrown
    away, so the call had to be made again by hand to see that."""
    db = _seed_db(tmp_path)
    preds = _write_csv(tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
                       [("bird_847", "names?", "SELECT name FROM emp", "SELECT age FROM emp")])
    report = tmp_path / "report.json"
    assert main(["score", "--db", db, "--predictions", preds, "--save-report", str(report)]) == 0
    capsys.readouterr()
    workspace = tmp_path / "ws"
    draft = tmp_path / "rules_draft.md"
    reply = '- "lap time" -> emp.age (q1, q7)\n- "fastest" -> emp.name (q12)\n'
    argv = ["--workspace", str(workspace), "draft-rules", "--report", str(report),
            "--predictions", preds, "--out", str(draft), "--backend", "echo",
            "--echo-response", reply]

    assert main(argv) == 0

    out = capsys.readouterr().out
    # q1 is the one question shown, so that rule survives under its real id
    assert '- "lap time" -> emp.age   <!-- bird_847 -->' in draft.read_text()
    assert "(1 rules, each citing" in out
    raw = tmp_path / "rules_draft.raw.txt"
    assert raw.read_text() == reply
    assert f"the reply as it came: {raw}" in out
    import json as _json
    manifest = _json.loads(next(workspace.glob("runs/*draft-rules*/manifest.json")).read_text())
    assert manifest["outputs"]["raw_out"][0]["copy"].endswith("rules_draft.raw.txt")


def test_draft_rules_counts_the_dropped_citations_when_no_rule_survives(tmp_path, capsys):
    db = _seed_db(tmp_path)
    preds = _write_csv(tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
                       [("bird_847", "names?", "SELECT name FROM emp", "SELECT age FROM emp")])
    report = tmp_path / "report.json"
    assert main(["score", "--db", db, "--predictions", preds, "--save-report", str(report)]) == 0
    capsys.readouterr()
    draft = tmp_path / "rules_draft.md"

    assert main(["draft-rules", "--report", str(report), "--predictions", preds,
                 "--out", str(draft), "--backend", "echo",
                 "--echo-response", '- "lap time" -> emp.age (q2, q7)\n'
                                    '- "fastest" -> emp.name (q12)\n']) == 0

    out = capsys.readouterr().out
    assert "(0 rules, each citing" in out
    assert ("0 rules survived: the reply had 2 rule lines and 3 citations that named no "
            "question shown. The questions were shown as q1; read the reply.") in out


def test_draft_rules_makes_the_directory_for_its_out_and_records_zero_spend_on_a_refusal(
        tmp_path, capsys):
    """--out WS/context/rules_draft.md failed after the call was paid for,
    because context/ did not exist; the refused run then recorded a null
    spend, which reads as unmeasured."""
    db = _seed_db(tmp_path)
    preds = _write_csv(tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
                       [("q1", "names?", "SELECT name FROM emp", "SELECT age FROM emp")])
    report = tmp_path / "report.json"
    assert main(["score", "--db", db, "--predictions", preds, "--save-report", str(report)]) == 0
    capsys.readouterr()
    workspace = tmp_path / "ws"
    draft = workspace / "context" / "rules_draft.md"       # context/ is not there yet
    base = ["--workspace", str(workspace), "draft-rules", "--report", str(report),
            "--predictions", preds, "--backend", "echo",
            "--echo-response", '- "names" -> emp.name (q1)\n']

    assert main(base + ["--out", str(draft)]) == 0
    assert '- "names" -> emp.name' in draft.read_text()

    assert main(base + ["--out", str(draft), "--bucket", "NO_SUCH_BUCKET"]) == 2

    out = capsys.readouterr().out
    assert "no failures in bucket NO_SUCH_BUCKET" in out
    manifests = [json.loads(p.read_text())
                 for p in workspace.glob("runs/*draft-rules*/manifest.json")]
    refused = next(m for m in manifests if m["exit_code"] == 2)
    assert refused["telemetry"]["estimated_cost_usd"] == 0.0
    assert refused["telemetry"]["calls"] == 0


def test_draft_rules_on_the_echo_backend_prints_and_records_nothing_spent(tmp_path, capsys):
    """It printed "one gemini-2.5-flash call ... ~$0.0038" and recorded
    $0.0012 in its manifest for a run that reached no model. The echo
    backend's token counts are characters/4, not a price."""
    db = _seed_db(tmp_path)
    preds = _write_csv(tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
                       [("q1", "names?", "SELECT name FROM emp", "SELECT age FROM emp")])
    report = tmp_path / "report.json"
    assert main(["score", "--db", db, "--predictions", preds, "--save-report", str(report)]) == 0
    capsys.readouterr()
    workspace = tmp_path / "ws"
    draft = tmp_path / "rules_draft.md"
    base = ["--workspace", str(workspace), "draft-rules", "--report", str(report),
            "--predictions", preds, "--out", str(draft), "--backend", "echo",
            "--echo-response", '- "names" -> emp.name (q1)\n']

    assert main(base + ["--estimate-only"]) == 0
    out = capsys.readouterr().out
    assert "the echo backend, no model call, $0.0000" in out
    assert "gemini" not in out and "(cents)" not in out
    assert not draft.exists()

    assert main(base) == 0
    out = capsys.readouterr().out
    assert '- "names" -> emp.name   <!-- q1 -->' in draft.read_text()
    assert "the echo backend, no model call, $0.0000" in out
    assert "spent:" not in out
    manifests = [json.loads(p.read_text())
                 for p in workspace.glob("runs/*draft-rules*/manifest.json")]
    assert [m["exit_code"] for m in manifests] == [0]     # --estimate-only is not recorded
    telemetry = manifests[0]["telemetry"]
    assert telemetry["calls"] == 0
    assert telemetry["estimated_cost_usd"] == 0.0
    assert telemetry["price_table"] is None


CANDIDATE_COLUMNS = ["id", "question", "gold_sql", "times_seen", "gate_status", "gate_detail", "table"]
CANDIDATES = [("c1", "names?", "SELECT name FROM emp", 9, "ok", "", "main.emp"),
              ("c2", "all units", "SELECT bu FROM t GROUP BY 1", 40, "ok", "", "main.t"),
              ("c3", "broken", "SELECT x", 2, "EXEC_FAIL", "no such column", "main.emp")]
# the model reviewer's answer: keeps c1, rejects c2, gives c3 a reason off the list
REVIEW_ANSWER = ('[{"id": "c1", "keep": "yes", "reason": "matches"}, '
                 '{"id": "c2", "keep": "no", "reason": "dropdown_or_metadata"}, '
                 '{"id": "c3", "keep": "no", "reason": "broken"}]')


def test_review_draft_prices_its_calls_first_and_estimate_only_writes_nothing(tmp_path, capsys):
    candidates = _write_csv(tmp_path / "c.csv", CANDIDATE_COLUMNS, CANDIDATES)
    workspace = tmp_path / "ws"
    assert main(["review", "--candidates", candidates, "--out", str(tmp_path / "r")]) == 0
    sheet = tmp_path / "r" / "review.csv"
    before = sheet.read_bytes()
    base = ["--workspace", str(workspace), "review", "--draft", "--sheet", str(sheet),
            "--backend", "echo", "--echo-response", REVIEW_ANSWER]
    capsys.readouterr()

    assert main(base + ["--estimate-only"]) == 0
    out = capsys.readouterr().out
    assert "review draft: 2 undecided of 3 rows: 1 gemini-2.5-flash call(s) of up to 10 rows" in out
    assert "prompt tokens, ~$0.00" in out
    assert sheet.read_bytes() == before
    assert not list(workspace.glob("runs/*review*"))     # an estimate is not a run

    assert main(base + ["--budget", "0"]) == 2
    assert "refusing to start: estimate $0.00" in capsys.readouterr().out
    assert sheet.read_bytes() == before

    assert main(base) == 0
    out = capsys.readouterr().out
    # 0 left for a person, not 1: the third row's SQL was refused by the gate,
    # so prepare() decided it and neither the model nor a person is asked
    assert ("review draft: 2 of 3 rows decided by gemini-2.5-flash (kept 1, rejected 1; reasons: "
            "matches 1, dropdown_or_metadata 1), 0 left for a person") in out
    with open(sheet, newline="") as handle:
        rows = {row["id"]: row for row in csv.DictReader(handle)}
    assert (rows["c1"]["keep"], rows["c1"]["source"]) == ("yes", "model-reviewed:gemini-2.5-flash")
    # c3 carries the gate's own refusal, which is distinct from a model's
    # verdict and from a person's, so all three can be counted apart
    assert (rows["c3"]["keep"], rows["c3"]["source"]) == ("no", "gate-refused:EXEC_FAIL")
    manifests = [json.loads(p.read_text()) for p in workspace.glob("runs/*review*/manifest.json")]
    assert sorted(m["exit_code"] for m in manifests) == [0, 2]   # the budget refusal is a run too
    manifest = next(m for m in manifests if m["exit_code"] == 0)
    assert manifest["telemetry"]["reviewer"] == {"model": "gemini-2.5-flash", "decided": 2,
                                                 "kept": 1, "rejected": 1, "undecided": 0}
    assert manifest["telemetry"]["estimate"]["calls"] == 1
    assert manifest["outputs"]["out"][0]["path"] == str(sheet)   # the sheet is the run's output


def test_leak_with_a_missing_context_file_says_so_in_one_line(tmp_path, capsys):
    # The common mistake: a path typed for a file that lives under another name
    # (rules.md when the workspace has context_r2.md). It used to be a pathlib
    # traceback, which does not say which flag was wrong.
    preds = _write_csv(tmp_path / "q.csv", ["id", "question", "gold_sql"],
                       [("q1", "?", "SELECT 1")])
    with pytest.raises(SystemExit) as stop:
        main(["leak", "--context", str(tmp_path / "rules.md"), "--predictions", preds])
    assert "context file (--context) not found" in str(stop.value)
    assert "rules.md" in str(stop.value)


def test_attribute_reuses_saved_reports_and_needs_no_database(tmp_path, capsys):
    """The expensive part of attribute is the scoring, which a saved report
    already holds. Re-executing to ask "which rule fixed which question" about a
    run scored minutes earlier cost about $1.45 on one enterprise warehouse."""
    db = _seed_db(tmp_path)
    preds = _write_csv(
        tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp")],
    )
    report = tmp_path / "r.json"
    assert main(["score", "--db", db, "--predictions", preds,
                 "--save-report", str(report)]) == 0
    rules = tmp_path / "rules.md"
    rules.write_text('- "names" -> emp.name\n')

    code = main(["attribute", "--without-context", preds, "--with-context", preds,
                 "--without-report", str(report), "--with-report", str(report),
                 "--context", str(rules)])

    assert code == 0                                   # no --db anywhere above
    assert "paired ablation" in capsys.readouterr().out


def test_attribute_refuses_one_arm_from_reports_and_one_from_execution(tmp_path, capsys):
    db = _seed_db(tmp_path)
    preds = _write_csv(
        tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp")],
    )
    report = tmp_path / "r.json"
    assert main(["score", "--db", db, "--predictions", preds,
                 "--save-report", str(report)]) == 0
    rules = tmp_path / "rules.md"
    rules.write_text('- "names" -> emp.name\n')

    code = main(["attribute", "--db", db, "--without-context", preds,
                 "--with-context", preds, "--without-report", str(report),
                 "--context", str(rules)])

    assert code == 2
    assert "both arms or neither" in capsys.readouterr().out


def test_score_checkpoint_resumes_instead_of_re_executing(tmp_path, capsys):
    """A long warehouse run that dies should not pay twice. evaluate() has had
    a checkpoint since a six-arm run died with its shell after five arms; no
    flag reached it."""
    db = _seed_db(tmp_path)
    preds = _write_csv(
        tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp"),
         ("q2", "old?", "SELECT name FROM emp WHERE age > 35", "SELECT 1 FROM nope")],
    )
    checkpoint = tmp_path / "ck.json"

    assert main(["score", "--db", db, "--predictions", preds,
                 "--checkpoint", str(checkpoint)]) == 0
    import json as _json
    saved = _json.loads(checkpoint.read_text())
    assert [r["id"] for r in saved["results"]] == ["q1", "q2"]

    # the second run answers from the file: point it at a database that does
    # not exist, and it still reports the same numbers
    capsys.readouterr()
    assert main(["score", "--db", str(tmp_path / "gone.sqlite"),
                 "--predictions", preds, "--checkpoint", str(checkpoint)]) == 0
    assert "1/2" in capsys.readouterr().out


def test_score_estimate_leaves_out_the_questions_a_checkpoint_already_holds(
        tmp_path, capsys, monkeypatch):
    """A resumed run does not execute what the checkpoint holds, so the
    estimate must not price it; on one estate it priced all 18 again."""
    _fake_warehouse(monkeypatch, {"SELECT name FROM emp": 500_000_000,
                                  "SELECT age FROM emp": 500_000_000})
    preds = _write_csv(
        tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp"),
         ("q2", "ages?", "SELECT age FROM emp", "SELECT age FROM emp")],
    )
    checkpoint = tmp_path / "ck.json"
    checkpoint.write_text(json.dumps({"results": [{"id": "q1"}]}))

    code = main(["score", "--db", "bq://billing/data.sales", "--predictions", preds,
                 "--checkpoint", str(checkpoint), "--estimate-only"])

    out = capsys.readouterr().out
    assert code == 0
    assert "estimated: 1 queries, 0.500 GB (dry-run)" in out       # q2 only
    assert "1 questions already in the checkpoint, not priced" in out


def test_context_write_refuses_a_draft_with_no_rules_unless_told(tmp_path, capsys):
    """A draft that parses to zero rules is almost always the wrong file; it used
    to install and report '0 rule lines' as if that were fine."""
    draft = tmp_path / "draft.md"
    draft.write_text("Some prose about revenue.\n")
    target = tmp_path / "rules.md"

    code = main(["context-write", "--source-file", str(draft), "--to", str(target)])

    assert code == 2 and not target.exists()
    assert "no rule lines" in capsys.readouterr().out
    assert main(["context-write", "--source-file", str(draft), "--to", str(target),
                 "--allow-no-rules"]) == 0
    assert target.read_text() == "Some prose about revenue.\n"


def test_context_write_says_when_it_is_writing_without_history(tmp_path, capsys):
    draft = tmp_path / "draft.md"
    draft.write_text('- "revenue" -> orders.amount\n')

    assert main(["context-write", "--source-file", str(draft), "--to", str(tmp_path / "r.md")]) == 0

    assert "without history" in capsys.readouterr().out


def test_ablate_says_when_no_split_guarded_the_curation(tmp_path, capsys):
    """The guard used to fire on split and stay silent at the comparison a
    skipped split invalidates. It prints beside the verdict now."""
    db = _seed_db(tmp_path)
    preds = _write_csv(
        tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp")],
    )
    workspace = tmp_path / "ws"

    code = main(["--workspace", str(workspace), "ablate", "--db", db,
                 "--without-context", preds, "--with-context", preds])

    assert code == 0
    out = capsys.readouterr().out
    assert "no split recorded in this workspace" in out
    assert "one repetition per arm: a single sample" in out


def test_ablate_says_when_a_model_accepted_the_golds_and_names_both_models(tmp_path, capsys):
    """A model-drafted answer key is quoted like an audited one unless the
    verdict says otherwise; the sentence also names the reviewer beside the
    agent, since here both are gemini."""
    db = _seed_db(tmp_path)
    ws = ["--workspace", str(tmp_path / "ws")]
    candidates = _write_csv(tmp_path / "c.csv", CANDIDATE_COLUMNS, CANDIDATES)
    assert main(ws + ["review", "--candidates", candidates, "--out", str(tmp_path / "r")]) == 0
    sheet = tmp_path / "r" / "review.csv"
    assert main(ws + ["review", "--draft", "--sheet", str(sheet), "--backend", "echo",
                      "--echo-response", REVIEW_ANSWER]) == 0
    with open(sheet, newline="") as handle:
        rows = list(csv.DictReader(handle))
    # rows[2] is the gate-refused row: prepare() already set keep=no with
    # reason=gate_defect, so a person re-affirming it changes nothing. Kept
    # as an explicit step because it is what a reviewer would do.
    rows[2]["keep"] = "no"
    with open(sheet, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    golds = tmp_path / "golds.csv"
    assert main(ws + ["review", "--sheet", str(sheet), "--out", str(golds)]) == 0
    out = capsys.readouterr().out
    # `gate_defect` rather than `(no reason given)`: the gate says why it
    # refused, so the funnel names the cause instead of a blank
    assert "golds -> " in out and "(1 kept of 3; rejected: dropdown_or_metadata 1, gate_defect 1)" in out
    assert "the answer key: 1 of 1 golds accepted by a model reviewer (gemini-2.5-flash), 0 by a person" in out
    schema = tmp_path / "schema.txt"
    schema.write_text("CREATE TABLE emp(name TEXT)")

    def arm(name, answer):
        preds, report = tmp_path / f"{name}.csv", tmp_path / f"{name}.json"
        assert main(ws + ["generate", "--questions", str(golds), "--schema", str(schema), "--out", str(preds),
                          "--backend", "echo", "--echo-response", answer]) == 0
        assert main(ws + ["score", "--db", db, "--predictions", str(preds), "--save-report", str(report)]) == 0
        return str(report)

    before = arm("a", "SELECT age FROM emp")
    after = arm("b", "SELECT name FROM emp")
    capsys.readouterr()

    assert main(ws + ["ablate", "--without-report", before, "--with-report", after]) == 0

    out = capsys.readouterr().out
    assert ("the answer key: 1 of 1 golds accepted by a model reviewer (gemini-2.5-flash), 0 by a "
            "person; have a person re-review a sample before quoting the number; the reviewer and "
            "the agent are the same model family (gemini-2.5-flash / gemini-2.5-flash), so a shared "
            "blind spot passes as correct") in out


def _routing_round(tmp_path, workspace, name, catalog_lines, pred_table):
    preds = _write_csv(
        tmp_path / f"{name}.csv", ["id", "question", "gold_table", "pred_table"],
        [("r1", "orders?", "sales.orders", pred_table)],
    )
    catalog = tmp_path / f"{name}_catalog.txt"
    catalog.write_text("\n".join(catalog_lines) + "\n")
    report = tmp_path / f"{name}_report.json"
    assert main(["--workspace", str(workspace), "score-routing", "--predictions", preds,
                 "--catalog", str(catalog), "--save-report", str(report)]) == 0
    return report


def test_a_routing_report_is_recorded_as_its_runs_output(tmp_path, capsys):
    """Nulling --save-report to keep _emit_json off the bridge file also hid the
    file from the recorder, so no routing report had a run behind it."""
    from context_lift.workspace import run_that_wrote
    workspace = tmp_path / "ws"
    report = _routing_round(tmp_path, workspace, "r0", ["sales.orders"], "sales.orders")

    found = run_that_wrote(workspace, report)

    assert found is not None
    assert found[1]["command"] == "score-routing"
    assert found[1]["inputs"]["catalog"]["copy"] == "inputs/r0_catalog.txt"


def test_ablate_says_when_the_catalog_changed_between_the_arms(tmp_path, capsys):
    """A round that adds documents and drops junk tables reports one change
    and made two; the verdict has to say so where it is read."""
    workspace = tmp_path / "ws"
    before = _routing_round(tmp_path, workspace, "r0",
                            ["sales.orders", "sales.orders_tmp", "sales.orders_bkp"],
                            "sales.orders_tmp")
    after = _routing_round(tmp_path, workspace, "r1",
                           ["sales.orders", "sales.returns"], "sales.orders")
    capsys.readouterr()

    code = main(["--workspace", str(workspace), "ablate",
                 "--without-report", str(before), "--with-report", str(after)])

    assert code == 0
    out = capsys.readouterr().out
    assert ("the catalog changed between without context and with context: "
            "+1 tables, -2 tables") in out


def test_the_split_caveat_asks_whether_these_arms_were_on_the_holdout(tmp_path, capsys):
    """Any finished split used to silence it; a split on the questions and a
    comparison on the curate half is still a training score."""
    db = _seed_db(tmp_path)
    golds = _write_csv(
        tmp_path / "q.csv", ["id", "question", "gold_sql"],
        [(f"q{i}", f"question {i}", "SELECT name FROM emp") for i in range(12)],
    )
    schema = tmp_path / "schema.txt"
    schema.write_text("CREATE TABLE emp(name TEXT, age INT)")
    workspace = tmp_path / "ws"
    ws = ["--workspace", str(workspace)]
    curate, holdout = tmp_path / "c.csv", tmp_path / "h.csv"
    assert main(ws + ["split", "--golds", golds,
                      "--out-curate", str(tmp_path / "c.txt"),
                      "--out-holdout", str(tmp_path / "h.txt"),
                      "--out-curate-csv", str(curate), "--out-holdout-csv", str(holdout)]) == 0

    def scored(questions, name):
        preds, report = tmp_path / f"{name}.csv", tmp_path / f"{name}.json"
        assert main(ws + ["generate", "--questions", str(questions), "--schema", str(schema),
                          "--out", str(preds), "--backend", "echo"]) == 0
        assert main(ws + ["score", "--db", db, "--predictions", str(preds),
                          "--save-report", str(report)]) == 0
        return str(report)

    on_holdout = scored(holdout, "h")
    on_curate = scored(curate, "c")
    capsys.readouterr()

    assert main(ws + ["ablate", "--without-report", on_holdout, "--with-report", on_holdout]) == 0
    quiet = capsys.readouterr().out
    assert main(ws + ["ablate", "--without-report", on_curate, "--with-report", on_curate]) == 0
    loud = capsys.readouterr().out

    assert "training score" not in quiet
    assert "not scored on its holdout" in loud


def test_generate_with_a_command_backend_writes_predictions_and_the_routing_sidecar(tmp_path, capsys):
    import sys
    questions = _write_csv(tmp_path / "q.csv", ["id", "question", "gold_sql"],
                           [("q1", "names?", "SELECT name FROM main.emp")])
    schema = tmp_path / "schema.txt"; schema.write_text("CREATE TABLE emp(name TEXT)")
    runner = tmp_path / "agent.py"
    runner.write_text("import json, sys\nq = json.load(sys.stdin)\n"
                      "print(json.dumps({'sql': 'SELECT name FROM emp', 'table': 'main.emp'}))\n")
    workspace = tmp_path / "ws"

    code = main(["--workspace", str(workspace), "generate", "--questions", questions,
                 "--schema", str(schema), "--out", str(tmp_path / "arm.csv"),
                 "--backend", "command", "--command", f"{sys.executable} {runner}",
                 "--runner-version", "agent@abc123", "--reps", "2"])

    assert code == 0
    routing = (tmp_path / "arm_rep1_routing.csv").read_text().splitlines()
    assert routing == ["id,question,gold_table,pred_table", "q1,names?,main.emp,main.emp"]
    manifest = json.loads(next(workspace.glob("runs/*generate*/manifest.json")).read_text())
    assert manifest["options"]["runner_version"] == "agent@abc123"
    assert manifest["telemetry"]["calls"] == 2 and manifest["telemetry"]["failures"] == 0
    assert any(r["path"].endswith("arm_rep1_routing.csv") for r in manifest["outputs"]["out"])
    printed = capsys.readouterr().out
    assert "the runner's own cost" in printed
    assert ("  the runner inherits this shell's credentials and environment; the read-only "
            "rail and the byte ceiling cover only what contextlift executes, not what the "
            "runner runs. Give it a read-only service account and its own byte ceiling."
            ) in printed


def test_a_runner_that_abstains_is_recorded_as_such_and_copies_are_named(tmp_path, capsys):
    import sys
    questions = _write_csv(tmp_path / "q.csv", ["id", "question", "gold_sql"],
                           [("q1", "names?", "SELECT name FROM main.emp")])
    schema = tmp_path / "schema.txt"; schema.write_text("CREATE TABLE emp(name TEXT)")
    runner = tmp_path / "agent.py"
    runner.write_text("import json, sys\nprint(json.dumps({'sql': '', 'abstained': True, 'note': 'no table fits'}))\n")

    code = main(["generate", "--questions", questions, "--schema", str(schema),
                 "--out", str(tmp_path / "arm.csv"), "--backend", "command",
                 "--command", f"{sys.executable} {runner}", "--reps", "2"])

    assert code == 0
    out = capsys.readouterr().out
    assert "1 abstained" in out
    assert "identical across repetitions: 1 of 1 questions; these are copies, not samples" in out
    assert (tmp_path / "arm_rep1.csv").read_text().splitlines() == [
        "id,question,gold_sql,pred_sql,note", "q1,names?,SELECT name FROM main.emp,,abstained: no table fits"]


def test_ablate_ids_reads_one_scored_arm_two_ways(tmp_path, capsys):
    """Arms are generated over the whole key; --ids gives the curate reading
    and the holdout reading from the same reports without re-scoring."""
    db = _seed_db(tmp_path)
    before = _write_csv(tmp_path / "a.csv", ["id", "question", "gold_sql", "pred_sql"],
                        [("q1", "?", "SELECT name FROM emp", "SELECT age FROM emp"),
                         ("q2", "?", "SELECT name FROM emp", "SELECT name FROM emp")])
    after = _write_csv(tmp_path / "b.csv", ["id", "question", "gold_sql", "pred_sql"],
                       [("q1", "?", "SELECT name FROM emp", "SELECT name FROM emp"),
                        ("q2", "?", "SELECT name FROM emp", "SELECT name FROM emp")])
    main(["score", "--db", db, "--predictions", before, "--save-report", str(tmp_path / "a.json")])
    main(["score", "--db", db, "--predictions", after, "--save-report", str(tmp_path / "b.json")])
    (tmp_path / "ids.txt").write_text("q2\n")
    capsys.readouterr()

    code = main(["ablate", "--without-report", str(tmp_path / "a.json"),
                 "--with-report", str(tmp_path / "b.json"), "--ids", str(tmp_path / "ids.txt")])

    out = capsys.readouterr().out
    assert code == 0
    assert "restricted to 1 ids" in out and "paired ablation on 1 questions" in out
    assert "the context changed nothing" in out          # q1's fix is outside the ids


def test_ablate_refuses_ids_that_none_of_the_arms_answered(tmp_path, capsys):
    """A reading restricted to 46 reserved ids the arms never saw used to run
    happily: 0 scored per arm, both rates 0.0% [0.0%, 100.0%], exit 0, and the
    workspace counted it as the one look at the reserved questions."""
    db = _seed_db(tmp_path)
    preds = _write_csv(tmp_path / "a.csv", ["id", "question", "gold_sql", "pred_sql"],
                       [("q1", "?", "SELECT name FROM emp", "SELECT name FROM emp"),
                        ("q2", "?", "SELECT name FROM emp", "SELECT age FROM emp"),
                        ("q3", "?", "SELECT name FROM emp", "SELECT name FROM emp")])
    report = str(tmp_path / "a.json")
    main(["score", "--db", db, "--predictions", preds, "--save-report", report])
    (tmp_path / "reserved.txt").write_text("h1\nh2\n")
    capsys.readouterr()

    code = main(["ablate", "--without-report", report, "--with-report", report,
                 "--ids", str(tmp_path / "reserved.txt")])

    assert code == 2
    assert capsys.readouterr().out.strip() == (
        f"none of the 2 ids in {tmp_path / 'reserved.txt'} is in these reports: they hold "
        "3 questions (q1, q2, q3), so the arms were answered over another file; read them "
        "with the ids file that matches, or answer these questions with a generate over "
        "the reserved rows")


def test_climb_refuses_ids_that_none_of_the_rounds_answered(tmp_path, capsys):
    db = _seed_db(tmp_path)
    preds = _write_csv(tmp_path / "a.csv", ["id", "question", "gold_sql", "pred_sql"],
                       [("q1", "?", "SELECT name FROM emp", "SELECT name FROM emp")])
    report = str(tmp_path / "a.json")
    main(["score", "--db", db, "--predictions", preds, "--save-report", report])
    (tmp_path / "reserved.txt").write_text("h1\n")
    capsys.readouterr()

    code = main(["climb", "--round", f"r0={report}", "--round", f"r1={report}",
                 "--ids", str(tmp_path / "reserved.txt")])

    assert code == 2
    assert "none of the 1 ids in" in capsys.readouterr().out


def test_a_refused_ids_reading_is_not_counted_as_a_look_at_the_reserved_half(tmp_path, capsys):
    db = _seed_db(tmp_path)
    golds = _write_csv(tmp_path / "q.csv", ["id", "question", "gold_sql"],
                       [(f"q{i}", f"question {i}", "SELECT name FROM emp") for i in range(12)])
    schema = tmp_path / "schema.txt"; schema.write_text("CREATE TABLE emp(name TEXT, age INT)")
    workspace = tmp_path / "ws"
    ws = ["--workspace", str(workspace)]
    assert main(ws + ["split", "--golds", golds, "--out-curate", str(tmp_path / "c.txt"),
                      "--out-holdout", str(tmp_path / "h.txt"),
                      "--out-curate-csv", str(tmp_path / "c.csv"),
                      "--out-holdout-csv", str(tmp_path / "h.csv")]) == 0
    curate_only = _write_csv(tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
                             [(qid, "?", "SELECT name FROM emp", "SELECT name FROM emp")
                              for qid in (tmp_path / "c.txt").read_text().split()])
    report = str(tmp_path / "p.json")
    assert main(ws + ["score", "--db", db, "--predictions", curate_only,
                      "--save-report", report]) == 0
    capsys.readouterr()

    code = main(ws + ["ablate", "--without-report", report, "--with-report", report,
                      "--ids", str(tmp_path / "h.txt")])

    assert code == 2
    assert rebuild_index(workspace)["holdout_looks"] == 0


def test_ablate_ids_on_the_holdout_half_prints_no_training_caveat(tmp_path, capsys):
    """--ids with exactly the split's holdout ids is the holdout reading; the
    caveat used to fire because the arms were generated over the whole key."""
    db = _seed_db(tmp_path)
    golds = _write_csv(tmp_path / "q.csv", ["id", "question", "gold_sql"],
                       [(f"q{i}", f"question {i}", "SELECT name FROM emp") for i in range(12)])
    schema = tmp_path / "schema.txt"; schema.write_text("CREATE TABLE emp(name TEXT, age INT)")
    ws = ["--workspace", str(tmp_path / "ws")]
    assert main(ws + ["split", "--golds", golds, "--out-curate", str(tmp_path / "c.txt"),
                      "--out-holdout", str(tmp_path / "h.txt"),
                      "--out-curate-csv", str(tmp_path / "c.csv"), "--out-holdout-csv", str(tmp_path / "h.csv")]) == 0
    preds, report = tmp_path / "p.csv", tmp_path / "p.json"
    assert main(ws + ["generate", "--questions", golds, "--schema", str(schema), "--out", str(preds), "--backend", "echo"]) == 0
    assert main(ws + ["score", "--db", db, "--predictions", str(preds), "--save-report", str(report)]) == 0
    capsys.readouterr()

    assert main(ws + ["ablate", "--without-report", str(report), "--with-report", str(report),
                      "--ids", str(tmp_path / "h.txt")]) == 0
    holdout_reading = capsys.readouterr().out
    assert main(ws + ["ablate", "--without-report", str(report), "--with-report", str(report),
                      "--ids", str(tmp_path / "c.txt")]) == 0
    curate_reading = capsys.readouterr().out

    assert "training score" not in holdout_reading
    assert "restricted to the curate half: this is a training score" in curate_reading


class _DryRunOnly:
    """A warehouse executor that only dry-runs: 100 MB per distinct statement.
    Anything in `failing` is rejected at dry-run, anything not read-only is
    never dry-run, exactly as BigQueryExecutor.estimate reports them."""

    def __init__(self, failing=()):
        self.failing = set(failing)

    def estimate(self, sqls, workers=1):
        from context_lift.executor import is_read_only
        distinct = list(dict.fromkeys(sqls))
        statements = [{"bytes": None if (sql in self.failing or not is_read_only(sql))
                       else 100_000_000, "over_ceiling": False,
                       "failed": sql in self.failing or not is_read_only(sql),
                       "refused": False}       # a parse error here, never a 403
                      for sql in distinct]
        priced = [r for r in statements if not r["failed"]]
        return {"queries": sum(1 for sql in distinct if is_read_only(sql)),
                "failed": sum(1 for sql in distinct
                              if sql in self.failing and is_read_only(sql)),
                "not_found": 0, "refused": 0,
                "bytes": 100_000_000 * len(priced),
                "billable_bytes": 100_000_000 * len(priced),
                "usd": round(0.0006 * len(priced), 6), "usd_per_tib": 6.25,
                "price_as_of": "2026-09-03", "ceiling_bytes": 1_000_000_000,
                "over_ceiling": {"count": 0, "largest_bytes": 0, "bytes": 0},
                "statements": statements}


def test_the_score_estimate_adds_up_from_the_predictions_to_the_calls(tmp_path, capsys, monkeypatch):
    """A field estimate printed 117 statements dry-run, 116 queries, 3
    rejected, 117 reused and 113 calls for 118 predictions, and no reader
    could get from one number to the next."""
    gold = "SELECT COUNT(*) FROM emp"
    rows = [("q1", "?", gold, "DELETE FROM emp"),
            ("q2", "?", gold, "SELECT nope FROM emp"),
            ("q3", "?", gold, "SELECT name FROM emp"),
            ("q4", "?", gold, "SELECT age FROM emp"),
            ("q5", "?", gold, "SELECT age FROM emp"),
            ("q6", "?", gold, "SELECT name, age FROM emp")]
    preds = _write_csv(tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"], rows)
    monkeypatch.setattr("context_lift.cli._executor_for",
                        lambda *a, **k: _DryRunOnly(failing={"SELECT nope FROM emp"}))
    capsys.readouterr()

    code = main(["score", "--db", "bq://billing/data.set", "--predictions", preds,
                 "--estimate-only"])

    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert lines[1] == ("6 predictions: 1 not read-only, 1 rejected at dry-run, 1 identical "
                        "to a gold or to another prediction, 3 to run; 1 more statement to "
                        "run beside them")
    assert lines[2].startswith("time: 4 queries at ")        # the 3 to run plus the one gold


def test_score_prices_the_rows_the_judge_re_executes_from_a_checkpoint(tmp_path, capsys, monkeypatch):
    """The checkpoint holds verdicts, not result rows, so an unjudged
    disagreement runs its SQL again. The estimate priced 0 queries at $0.0000
    and --budget passed, then the run executed them."""
    from context_lift.harness import rows_needing_judge
    db = _seed_db(tmp_path)
    preds = _write_csv(tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
                       [("q1", "?", "SELECT name FROM emp", "SELECT age FROM emp"),
                        ("q2", "?", "SELECT name FROM emp", "SELECT name FROM emp")])
    checkpoint = str(tmp_path / "ck.json")
    assert main(["score", "--db", db, "--predictions", preds, "--checkpoint", checkpoint]) == 0
    assert rows_needing_judge(checkpoint) == ["q1"]
    monkeypatch.setattr("context_lift.cli._executor_for", lambda *a, **k: _DryRunOnly())
    capsys.readouterr()

    code = main(["score", "--db", "bq://billing/data.set", "--predictions", preds,
                 "--checkpoint", checkpoint, "--judge-gemini", "--estimate-only"])

    out = capsys.readouterr().out
    assert code == 0
    assert ("judge: 1 checkpointed disagreements will be re-executed for the judge (the "
            "checkpoint holds verdicts, not result rows); their SQL is in the estimate "
            "below") in out
    assert "judge: up to 1 calls to gemini-2.5-pro" in out
    assert "estimated: 2 queries" in out              # q1's gold and its prediction


def test_score_without_a_judge_still_prices_nothing_a_checkpoint_already_holds(tmp_path, capsys, monkeypatch):
    db = _seed_db(tmp_path)
    preds = _write_csv(tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
                       [("q1", "?", "SELECT name FROM emp", "SELECT age FROM emp"),
                        ("q2", "?", "SELECT name FROM emp", "SELECT name FROM emp")])
    checkpoint = str(tmp_path / "ck.json")
    assert main(["score", "--db", db, "--predictions", preds, "--checkpoint", checkpoint]) == 0
    monkeypatch.setattr("context_lift.cli._executor_for", lambda *a, **k: _DryRunOnly())
    capsys.readouterr()

    code = main(["score", "--db", "bq://billing/data.set", "--predictions", preds,
                 "--checkpoint", checkpoint, "--estimate-only"])

    out = capsys.readouterr().out
    assert code == 0 and "judge:" not in out
    assert "estimated: 0 queries" in out
    assert "2 questions already in the checkpoint, not priced" in out


def test_score_says_in_prose_whether_a_judge_ran(tmp_path, capsys):
    """A field engineer read 'wrong' as reviewed; the judge is opt-in and the
    report said 'judged: 0' only in JSON."""
    db = _seed_db(tmp_path)
    preds = _write_csv(tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
                       [("q1", "?", "SELECT name FROM emp", "SELECT age FROM emp"),
                        ("q2", "?", "SELECT name FROM emp", "SELECT name FROM emp")])

    assert main(["score", "--db", db, "--predictions", preds]) == 0

    assert "judge: not run; 1 wrong answers were not reviewed" in capsys.readouterr().out


def test_score_with_a_judge_over_a_checkpoint_says_what_it_re_executes_and_what_the_judge_cost(
        tmp_path, capsys, monkeypatch):
    """A field run passed --judge-gemini with --checkpoint: the judge was
    skipped without a word, and its calls were in no manifest."""
    from context_lift.backends import echo_backend
    monkeypatch.setattr("context_lift.backends.gemini_backend",
                        lambda **kw: echo_backend("VERDICT: EQUIVALENT\nREASON: same intent"))
    db = _seed_db(tmp_path)
    preds = _write_csv(tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
                       [("q1", "?", "SELECT name FROM emp", "SELECT age FROM emp"),
                        ("q2", "?", "SELECT name FROM emp", "SELECT name FROM emp")])
    checkpoint = tmp_path / "ckpt.json"
    workspace = tmp_path / "ws"
    base = ["--workspace", str(workspace), "score", "--db", db, "--predictions", preds,
            "--checkpoint", str(checkpoint)]
    assert main(base) == 0
    capsys.readouterr()

    # --yes with --budget: the judge is a paid path and the test runner has
    # no controlling terminal, which is the same position a host agent is in.
    assert main(base + ["--judge-gemini", "--yes", "--budget", "1"]) == 0

    out = capsys.readouterr().out
    assert ("judge: 1 checkpointed disagreements will be re-executed for the judge "
            "(the checkpoint holds verdicts, not result rows)") in out
    assert "judge: reviewed 1 disagreements, upgraded 1" in out
    assert "judge spent: 1 calls, " in out and "cost unknown (no price)" in out
    manifests = [json.loads(p.read_text()) for p in workspace.glob("runs/*score*/manifest.json")]
    judged = next(m["telemetry"] for m in manifests if m.get("telemetry"))
    assert judged["judge"]["calls"] == 1 and judged["judge"]["estimated_cost_usd"] is None


def test_climb_takes_saved_reports_and_needs_no_database_then(tmp_path, capsys):
    """AGENTS.md said to give climb reports; it re-executed every round and
    demanded --db. Two reports per round pool as repetitions."""
    db = _seed_db(tmp_path)
    bad = _write_csv(tmp_path / "bad.csv", ["id", "question", "gold_sql", "pred_sql"],
                     [("q1", "?", "SELECT name FROM emp", "SELECT age FROM emp")])
    good = _write_csv(tmp_path / "good.csv", ["id", "question", "gold_sql", "pred_sql"],
                      [("q1", "?", "SELECT name FROM emp", "SELECT name FROM emp")])
    for name, path in (("r0a", bad), ("r0b", bad), ("r1a", good), ("r1b", good)):
        assert main(["score", "--db", db, "--predictions", path,
                     "--save-report", str(tmp_path / f"{name}.json")]) == 0
    capsys.readouterr()

    code = main(["climb", "--round", f"base={tmp_path / 'r0a.json'}", "--round", f"base={tmp_path / 'r0b.json'}",
                 "--round", f"rules={tmp_path / 'r1a.json'}", "--round", f"rules={tmp_path / 'r1b.json'}"])

    out = capsys.readouterr().out
    assert code == 0
    assert "estimated" not in out          # nothing executed
    assert "rules" in out and "base" in out
    assert main(["climb", "--round", f"base={bad}"]) == 2       # a CSV round still needs --db
    assert "needs --db" in capsys.readouterr().out


def test_routing_commands_derive_the_gold_table_from_the_gold_sql(tmp_path, capsys):
    """gate's clean output has no table column; the docs told the engineer to
    route it and it could not run."""
    from context_lift.routing import load_routing_golds
    golds = _write_csv(tmp_path / "clean.csv", ["id", "question", "gold_sql"],
                       [("q1", "?", "SELECT a FROM sales.orders o JOIN sales.items i ON 1=1"),
                        ("q2", "?", "SELECT b FROM `p`.`ops`.`jobs`")])

    assert load_routing_golds(golds) == [("q1", "sales.orders"), ("q2", "ops.jobs")]
    catalog = tmp_path / "catalog.txt"; catalog.write_text("sales.orders\nops.jobs\n")
    assert main(["routing-gate", "--golds", golds, "--catalog", str(catalog)]) == 0
    assert "2 questions, 2 golds" in capsys.readouterr().out
    preds = _write_csv(tmp_path / "routed.csv", ["id", "question", "gold_sql", "pred_table"],
                       [("q1", "?", "SELECT a FROM sales.orders", "sales.orders")])
    assert main(["score-routing", "--predictions", preds]) == 0
    assert "table accuracy:   1.000" in capsys.readouterr().out


def test_triage_points_at_tolerant_columns_when_column_count_leads(tmp_path, capsys):
    """Golds that are dashboard queries return lookup columns the question
    never asked for; 5 of 18 'failures' on one estate were that."""
    db = _seed_db(tmp_path)
    preds = _write_csv(tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
                       [("q1", "?", "SELECT name FROM emp", "SELECT name, age FROM emp")])
    assert main(["score", "--db", db, "--predictions", preds, "--save-report", str(tmp_path / "r.json")]) == 0
    capsys.readouterr()

    assert main(["triage", "--report", str(tmp_path / "r.json")]) == 0

    assert "score --extra-columns tolerate" in capsys.readouterr().out


def test_generate_infers_the_bigquery_dialect_from_the_workspace(tmp_path, capsys):
    """Two paid arms on one estate asked for SQLite SQL because the default
    never looked at the warehouse the workspace already named."""
    questions = _write_csv(tmp_path / "q.csv", ["id", "question", "gold_sql"], [("q1", "?", "SELECT 1")])
    schema = tmp_path / "schema.txt"; schema.write_text("CREATE TABLE t(a INT)")
    workspace = tmp_path / "ws"; workspace.mkdir()
    (workspace / "bigquery.txt").write_text("bq://billing/data.sales\n")

    assert main(["--workspace", str(workspace), "generate", "--questions", questions, "--schema", str(schema),
                 "--out", str(tmp_path / "arm.csv"), "--backend", "echo"]) == 0

    assert "dialect: BigQuery (from the workspace" in capsys.readouterr().err
    assert main(["generate", "--questions", questions, "--schema", str(schema),
                 "--out", str(tmp_path / "arm2.csv"), "--backend", "echo"]) == 0
    assert "dialect: BigQuery" not in capsys.readouterr().err        # SQLite by default, silently


def test_ablate_says_when_judged_rows_are_read_raw(tmp_path, capsys):
    from context_lift.harness import EvalReport, QuestionResult, save_report
    from context_lift.metrics import proportion
    results = [QuestionResult("q1", "?", "MISMATCH", False, True, True, "EQUIVALENT", "same answer", answer="SELECT 1")]
    report = EvalReport(results=results, n_total=1, n_scored=1, n_excluded=0,
                        execution=proportion(0, 1), adjusted=proportion(1, 1))
    save_report(report, tmp_path / "r.json")

    assert main(["ablate", "--without-report", str(tmp_path / "r.json"), "--with-report", str(tmp_path / "r.json")]) == 0
    raw = capsys.readouterr().out
    assert main(["ablate", "--without-report", str(tmp_path / "r.json"), "--with-report", str(tmp_path / "r.json"),
                 "--adjusted"]) == 0
    adjusted = capsys.readouterr().out

    assert "2 judged answers in these reports: this reading is raw execution" in raw
    assert "judged answers in these reports" not in adjusted and "judge-adjusted" in adjusted


# the fake's custom group, named the way --project p --location us --entry-group kb builds it
GROUP_ENTRIES = "projects/p/locations/us/entryGroups/kb/entries/"


def _fake_catalog(monkeypatch, documents=None, **knobs):
    from tests.test_catalog import FakeCatalog
    knobs.setdefault("name_prefix", GROUP_ENTRIES)
    fake = FakeCatalog(documents or {}, **knobs)
    monkeypatch.setattr("context_lift.catalog.transport_for", lambda *a, **k: fake)
    return fake


GROUP = ["--project", "p", "--location", "us", "--entry-group", "kb", "--entry-id", "{dataset}__{table}"]
PUSH = ["catalog-push"] + GROUP
PULL = ["catalog-pull"] + GROUP


def test_catalog_list_writes_the_table_list_by_the_parent_entry_rule(tmp_path, capsys, monkeypatch):
    """127 names were typed by hand; the group's self-entry and an index
    entry are the two dropped, by role, not by type."""
    _fake_catalog(monkeypatch)
    out = tmp_path / "catalog.txt"

    code = main(["catalog-list", "--project", "p", "--location", "us", "--out", str(out)])

    assert code == 0
    lines = out.read_text().splitlines()
    assert lines[0].startswith("# contextlift catalog-list") and "kept 3" in lines[1]
    assert lines[2:] == sorted(lines[2:]) and all("." in l for l in lines[2:])
    assert "catalog -> " in capsys.readouterr().out


def test_catalog_pull_pages_writes_the_stored_overview_unstamped_and_no_page_for_a_table_without_one(
        tmp_path, capsys, monkeypatch):
    """The bundle under --out is YAML text a person cannot edit; the page
    under --pages is what catalog-push --docs sends back."""
    from context_lift.push import stamp
    page = "# sales.orders\n\nOne row per order.\n" + "| x | y |\n" * 300
    _, stamped = stamp(page)
    _fake_catalog(monkeypatch, {"sales__orders": stamped, "sales__items": ""})
    monkeypatch.setattr("context_lift.context._sleep", lambda s: None)   # the empty bundle retries
    out, pages = tmp_path / "served", tmp_path / "pages"

    code = main(PULL + ["--tables", "sales.orders,sales.items", "--out", str(out), "--pages", str(pages)])

    assert code == 0
    assert (out / "sales.orders.md").read_text() == "overview: " + stamped   # the bundle, as served
    assert (pages / "sales.orders.md").read_text() == page
    assert not (pages / "sales.items.md").exists()
    printed = capsys.readouterr().out
    assert f"pages -> {pages} (1 of 2 tables carry an overview; edit, review, then catalog-push --docs {pages})" in printed
    assert "kept byte-exact); --pages writes the editable overview" in printed


def test_catalog_pull_with_an_entry_id_builds_every_name_and_never_searches(tmp_path, capsys, monkeypatch):
    """The same table resolved right in one run and wrong minutes later;
    only the search ranking had changed."""
    fake = _fake_catalog(monkeypatch, {"sales__orders": "x" * 2000})

    def no_search(query):
        raise AssertionError("search was called; addressing is built from the key")

    fake.search = no_search

    code = main(PULL + ["--tables", "sales.orders,sales.gone", "--out", str(tmp_path / "served")])

    assert code == 0
    assert (tmp_path / "served" / "sales.orders.md").read_text() == "overview: " + "x" * 2000
    assert not (tmp_path / "served" / "sales.gone.md").exists()          # a built name that is not there: nothing found
    assert "  sales.gone: 0 chars   (nothing found)" in capsys.readouterr().out


def test_catalog_pull_with_a_wrong_entry_id_exits_2_after_every_name_missed(tmp_path, capsys, monkeypatch):
    _fake_catalog(monkeypatch, {"sales__orders": "x" * 2000},
                  read_raises={GROUP_ENTRIES + "orders", GROUP_ENTRIES + "items"})

    code = main(["catalog-pull", "--project", "p", "--location", "us", "--entry-group", "kb", "--entry-id", "{table}",
                 "--tables", "sales.orders,sales.items", "--out", str(tmp_path / "served")])

    assert code == 2
    assert (f"not one of the entry names built from --entry-id {{table}} exists in entry group "
            f"kb (2 tried, the first {GROUP_ENTRIES}orders).") in capsys.readouterr().out
    assert not (tmp_path / "served").exists() or not list((tmp_path / "served").iterdir())


def test_catalog_pull_goes_on_past_a_first_table_that_is_not_catalogued(tmp_path, capsys, monkeypatch):
    """Listed first, a missing table stopped the pull and blamed --entry-id;
    listed second, the same table was one "nothing found" line."""
    _fake_catalog(monkeypatch, {"sales__orders": "x" * 2000},
                  read_raises={GROUP_ENTRIES + "sales__typo"})

    code = main(PULL + ["--tables", "sales.typo,sales.orders", "--out", str(tmp_path / "served")])

    printed = capsys.readouterr().out
    assert code == 0
    assert (tmp_path / "served" / "sales.orders.md").read_text() == "overview: " + "x" * 2000
    assert "  sales.typo: 0 chars   (nothing found)" in printed
    assert "pulled 1 of 2 documents" in printed
    assert "check the dataset.table spelling" in printed
    assert "--entry-id" not in printed.split("pulled 1 of 2")[1]


def test_catalog_pull_says_the_reader_is_served_less_than_the_entry_stores(tmp_path, capsys, monkeypatch):
    """On a live estate 9 of 127 entries were above the budget the pipeline
    asked for, and they were the four largest finance and shipment views. The
    served body is a well-formed page, so the pull compares it with the stored
    aspect and names the flag; the richest pages were the ones nobody saw."""
    fake = _fake_catalog(monkeypatch, {"sales__orders": "x" * 71690, "sales__items": "y" * 2000},
                         stub_below_budget=572)
    monkeypatch.setattr("context_lift.context._sleep", lambda seconds: None)   # the stub is retried

    code = main(PULL + ["--tables", "sales.orders,sales.items", "--out", str(tmp_path / "served")])

    printed = capsys.readouterr().out
    assert code == 0
    assert ("  sales.orders: 572 chars   the reader is served 572 characters where the entry "
            "stores 71690, at budget 60000; raise --budget to 90000") in printed
    assert "  sales.items: 2010 chars\n" in printed          # served whole, nothing said
    assert ("  1 of 2 tables were served less than their entry stores at budget 60000: "
            "sales.orders 572 of 71690") in printed
    assert ("  the pages are in the catalog and a reader is not getting them; pull again with "
            "--budget 90000 to serve the largest of them") in printed
    assert set(fake.budgets) == {60000}    # no silent retry at a budget the caller did not ask for


def test_catalog_route_refuses_a_pattern_without_the_placeholder_and_records_all_three_outputs(
        tmp_path, capsys, monkeypatch):
    from tests.test_catalog import _RoutingCatalog
    monkeypatch.setattr("context_lift.catalog.transport_for", lambda *a, **k: _RoutingCatalog({}))
    questions = _write_csv(tmp_path / "q.csv", ["id", "question", "gold_sql"],
                           [("q1", "how many orders?", "SELECT COUNT(*) FROM sales.orders")])
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "sales.orders.md").write_text("# sales.orders\n\n- an order is a paid basket\n")
    catalog = tmp_path / "catalog.txt"
    catalog.write_text("# from catalog-list\nsales.orders\nsales.items\n")
    workspace = tmp_path / "ws"
    route = ["--workspace", str(workspace), "catalog-route", "--project", "p", "--location", "us",
             "--questions", questions, "--docs", str(docs)]

    assert main(route + ["--search-pattern", "tables about orders"]) == 2
    assert capsys.readouterr().out.strip() == "--search-pattern must contain {question}"

    code = main(route + ["--catalog", str(catalog), "--within", "sales", "--within", "ops", "--top", "2"])

    out = capsys.readouterr().out
    assert code == 0
    assert "dropped 1 routed tables not in --catalog, 1 questions affected: ops.jobs (1)" in out
    manifests = [json.loads(p.read_text()) for p in workspace.glob("runs/*catalog-route*/manifest.json")]
    manifest = next(m for m in manifests if m["exit_code"] == 0)
    run_dir = workspace / "runs" / manifest["run_id"]
    assert sorted(manifest["outputs"]) == ["context_out", "routes_out", "routing_out"]
    routes = json.loads((run_dir / "outputs" / "routes.json").read_text())
    assert routes == {"q1": ["sales.orders", "sales.items"]}
    with open(run_dir / "outputs" / "routing.csv", newline="") as handle:
        assert [(r["gold_table"], r["pred_table"]) for r in csv.DictReader(handle)] \
            == [("sales.orders", "sales.orders")]
    assert manifest["inputs"]["catalog"]["sha256"]                 # the list is an input of the run


def test_catalog_route_keeps_the_entry_group_it_was_given(tmp_path, capsys, monkeypatch):
    """The flag promised a scoping it never applied: four runs with four
    different values gave identical routes."""
    from tests.test_catalog import FakeCatalog

    class _TwoGroups(FakeCatalog):
        def search_semantic(self, query):
            return ["projects/p/locations/us/entryGroups/scratch/entries/sales__orders",
                    "projects/p/locations/us/entryGroups/kb/entries/ops__jobs"]

    monkeypatch.setattr("context_lift.catalog.transport_for", lambda *a, **k: _TwoGroups({}))
    questions = _write_csv(tmp_path / "q.csv", ["id", "question"], [("q1", "how many orders?")])
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "ops.jobs.md").write_text("# ops.jobs\n")
    route = ["catalog-route", "--project", "p", "--location", "us", "--questions", questions,
             "--docs", str(docs), "--routes-out", str(tmp_path / "routes.json"),
             "--context-out", str(tmp_path / "context.json"),
             "--routing-out", str(tmp_path / "routing.csv")]

    assert main(route + ["--entry-group", "kb"]) == 0

    printed = capsys.readouterr().out
    assert json.loads((tmp_path / "routes.json").read_text()) == {"q1": ["ops.jobs"]}
    assert ("dropped 1 routed tables outside --entry-group kb, 1 questions affected: "
            "sales.orders (1)") in printed

    assert main(route) == 0
    assert json.loads((tmp_path / "routes.json").read_text()) == {"q1": ["sales.orders", "ops.jobs"]}


def test_catalog_push_dry_run_plans_and_writes_nothing_then_the_push_verifies(tmp_path, capsys, monkeypatch):
    fake = _fake_catalog(monkeypatch, {"sales__orders": "old overview " * 200})
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "sales.orders.md").write_text("# sales.orders\n\n## Rules\n\n- orders exclude cancelled\n\n"
                                          "## Columns\n| a | b | c |\n" + "| x | y | z |\n" * 200)
    (docs / "bare.md").write_text("no dataset")

    assert main(PUSH + ["--docs", str(docs), "--dry-run", "--manifest-out", str(tmp_path / "m.json")]) == 0
    dry = capsys.readouterr().out
    assert fake.write_calls == 0 and "not leak-checked" in dry
    assert f"  sales.orders -> {GROUP_ENTRIES}sales__orders  would write (" in dry   # the target, checked before a real push
    assert json.loads((tmp_path / "m.json").read_text())["status"] == "plan"
    assert dry.rstrip().splitlines()[-1] == "dry run: nothing written; not recorded in the workspace (a real push is)"
    assert main(PUSH + ["--docs", str(docs), "--manifest-out", str(tmp_path / "m.json")]) == 0
    pushed = capsys.readouterr().out

    assert fake.write_calls == 1
    assert "verified 1" in pushed
    manifest = json.loads((tmp_path / "m.json").read_text())
    assert manifest["status"] == "complete"
    assert manifest["counts"]["verified"] == 1 and manifest["counts"]["bare_skipped"] == 1

    # the come-back-later reading writes its own manifest and says what the
    # bundle adds around the page (a real estate: more than written on 99 of 99)
    assert main(PUSH + ["--docs", str(docs), "--verify-only", "--manifest-out", str(tmp_path / "v.json")]) == 0
    verified = capsys.readouterr().out
    assert "verified 1 of 1 pages as a reader sees them now" in verified
    assert "  served minus written: min " in verified and "over 1 verified pages" in verified
    assert json.loads((tmp_path / "v.json").read_text())["status"] == "verified"


def test_a_second_push_into_the_same_manifest_keeps_the_first_runs_records(tmp_path, capsys, monkeypatch):
    """The live push took 8 runs and wrote one manifest; the manifests after
    the first said "0 pages written" and held none of the page records."""
    _fake_catalog(monkeypatch, {"sales__orders": "old " * 400, "sales__items": "old " * 400})
    docs = tmp_path / "docs"; docs.mkdir()
    for name in ("orders", "items"):
        (docs / f"sales.{name}.md").write_text(f"# sales.{name}\n" + "| x | y |\n" * 300)
    manifest = tmp_path / "push.json"

    assert main(PUSH + ["--docs", str(docs), "--manifest-out", str(manifest)]) == 0
    # the resumed run is given one page: without the merge the other record is lost
    assert main(PUSH + ["--docs", str(docs), "--tables", "sales.items",
                        "--manifest-out", str(manifest)]) == 0

    loaded = json.loads(manifest.read_text())
    assert [(e["key"], e["status"]) for e in loaded["entries"]] == [
        ("sales.items", "unchanged"), ("sales.orders", "written")]
    assert loaded["counts"]["written"] == 1 and loaded["counts"]["unchanged"] == 1
    # an unchanged page is not read back, so this run's record carries no verdict
    assert loaded["counts"]["verified"] == 1


def test_verify_only_skips_the_drafts_the_push_skipped(tmp_path, capsys, monkeypatch):
    """The push says "draft, not pushed" and the following --verify-only read
    the same page anyway, called it failed and exited 1."""
    from context_lift.draft import DRAFT_MARK
    fake = _fake_catalog(monkeypatch, {"sales__orders": "old " * 400, "sales__items": "old " * 400})
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "sales.orders.md").write_text("# sales.orders\n" + "| x | y |\n" * 300)
    (docs / "sales.items.md").write_text(f"# sales.items\n{DRAFT_MARK}\n" + "| x | y |\n" * 300)

    assert main(PUSH + ["--docs", str(docs)]) == 0
    pushed = capsys.readouterr().out
    assert fake.write_calls == 1 and "1 pages still carry the draft-context marker" in pushed

    code = main(PUSH + ["--docs", str(docs), "--verify-only"])

    verified = capsys.readouterr().out
    assert code == 0
    assert "verified 1 of 1 pages as a reader sees them now" in verified
    assert ("  1 not pushed (drafts): sales.items; they still carry the draft-context marker "
            "(review them, or --allow-draft)") in verified
    assert "sales.items: missing" not in verified


def test_catalog_push_refuses_when_a_reserved_answer_is_in_the_pages(tmp_path, capsys, monkeypatch):
    fake = _fake_catalog(monkeypatch, {"sales__orders": "old overview " * 200})
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "sales.orders.md").write_text("# page\n\nSELECT name FROM emp WHERE age > 30\n")
    holdout = _write_csv(tmp_path / "h.csv", ["id", "question", "gold_sql"],
                         [("q9", "names over 30?", "SELECT name FROM emp WHERE age > 30")])

    code = main(PUSH + ["--docs", str(docs), "--holdout", holdout])

    assert code == 2 and fake.write_calls == 0


def test_catalog_push_refuses_once_when_no_built_name_exists_at_all(tmp_path, capsys, monkeypatch):
    """113 failed page lines for one wrong flag is not a report; the push
    stops at phase 0 with the one reason."""
    fake = _fake_catalog(monkeypatch, {},
                         read_raises={GROUP_ENTRIES + "sales__orders", GROUP_ENTRIES + "sales__items"})
    docs = tmp_path / "docs"; docs.mkdir()
    for name in ("orders", "items"):
        (docs / f"sales.{name}.md").write_text(f"# sales.{name}\n" + "| x | y |\n" * 300)

    code = main(PUSH + ["--docs", str(docs)])

    printed = capsys.readouterr().out
    assert code == 2 and fake.write_calls == 0
    assert ("  refused: not one of the entry names built from --entry-id {dataset}__{table} "
            "exists in entry group kb (2 tried") in printed
    assert "nothing written" in printed


def test_an_interrupted_push_writes_the_manifest_so_far_and_says_how_to_continue(tmp_path, capsys, monkeypatch):
    """A push killed with SIGTERM after about 90 minutes had written no manifest."""
    fake = _fake_catalog(monkeypatch, {f"a__{n}": "old " * 600 for n in ("one", "two", "three")})
    real_write = fake.write_overview

    def write_or_interrupt(entry_name, text):
        if fake.write_calls == 2:
            raise KeyboardInterrupt
        real_write(entry_name, text)

    fake.write_overview = write_or_interrupt
    docs = tmp_path / "docs"; docs.mkdir()
    for name in ("one", "two", "three"):
        (docs / f"a.{name}.md").write_text(f"# a.{name}\n" + "| x | y |\n" * 300)
    manifest = tmp_path / "push.json"

    with pytest.raises(KeyboardInterrupt):
        main(PUSH + ["--docs", str(docs), "--manifest-out", str(manifest)])

    loaded = json.loads(manifest.read_text())
    assert loaded["status"] == "interrupted" and loaded["counts"]["written"] == 2
    assert [e["status"] for e in loaded["entries"]] == ["written", "written", None]   # a.one, a.three, a.two
    assert ("interrupted: 0 pages written and verified, 2 written and not verified, 1 not touched; "
            f"the manifest is at {manifest}; run the same command again to continue "
            "(stamped pages come back unchanged)") in capsys.readouterr().out


def test_a_second_push_on_the_same_workspace_is_refused_while_the_first_runs(tmp_path, capsys, monkeypatch):
    import os, subprocess, sys
    fake = _fake_catalog(monkeypatch, {"sales__orders": "old overview " * 200})
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "sales.orders.md").write_text("# sales.orders\n" + "| x | y |\n" * 300)
    ws = tmp_path / "ws"; ws.mkdir()
    lock = ws / "catalog-push.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "started_at": "2026-09-05T10:00:00"}))

    refused = main(["--workspace", str(ws)] + PUSH + ["--docs", str(docs)])

    assert refused == 2 and fake.write_calls == 0
    assert (f"a catalog-push started at 2026-09-05T10:00:00 (pid {os.getpid()}) is still running on this "
            "workspace; two pushes on one group race each other") in capsys.readouterr().out
    finished = subprocess.Popen([sys.executable, "-c", ""]); finished.wait()
    lock.write_text(json.dumps({"pid": finished.pid, "started_at": "2026-09-05T09:00:00"}))

    assert main(["--workspace", str(ws)] + PUSH + ["--docs", str(docs)]) == 0

    assert "replacing a stale lock" in capsys.readouterr().out and fake.write_calls == 1
    assert not lock.exists()


def test_sigterm_inside_the_push_guard_becomes_system_exit_143():
    import os, signal, time
    from context_lift.cli import _exit_on_signals
    before = signal.getsignal(signal.SIGTERM)

    with pytest.raises(SystemExit) as stopped:
        with _exit_on_signals():
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.5)

    assert stopped.value.code == 143
    assert signal.getsignal(signal.SIGTERM) is before


def _fake_workspace_run(workspace, run_id, command, started_at, inputs=None,
                        outputs=None, exit_code=0, telemetry=None):
    """One runs/<id>/manifest.json written by hand, with real hashes."""
    import hashlib
    run_dir = workspace / "runs" / run_id
    (run_dir / "outputs").mkdir(parents=True)
    manifest = {"schema_version": 1, "run_id": run_id, "command": command,
                "argv": [], "started_at": started_at, "ended_at": started_at,
                "exit_code": exit_code, "options": {},
                "inputs": inputs or {}, "outputs": outputs or {}}
    if telemetry is not None:
        manifest["telemetry"] = telemetry
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    return run_dir


def _hashed(path, text):
    import hashlib
    path.write_text(text)
    return {"path": str(path), "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "bytes": len(text), "copy": None}


def test_a_push_that_was_killed_still_says_the_catalog_changed_between_the_arms(tmp_path):
    """The first push wrote 15 entries and then raised NotFound, so the run
    recorded no telemetry; the last one found every page stamped and recorded
    written 0. The caveat read telemetry and stayed silent."""
    from context_lift.cli import _conclusion_caveats
    workspace = tmp_path / "ws"
    reports = {}
    for name, stamp in (("rules", "2026-09-05T14:56:53"), ("catalog", "2026-09-05T15:11:15")):
        preds = _hashed(tmp_path / f"{name}.csv", f"id,question,gold_sql,pred_sql\nq1,?,S,{name}\n")
        _fake_workspace_run(workspace, f"{stamp.replace(':', '')}-generate", "generate",
                            stamp, outputs={"out": [preds]})
        report = _hashed(tmp_path / f"{name}.json", json.dumps({"results": [], "name": name}))
        _fake_workspace_run(workspace, f"{stamp.replace(':', '')}-score", "score",
                            stamp, inputs={"predictions": preds},
                            outputs={"save_report": [report]})
        reports[name] = report["path"]
    push_dir = _fake_workspace_run(
        workspace, "20260905-150439-catalog-push", "catalog-push", "2026-09-05T15:04:39",
        outputs={"manifest_out": [{"path": "push_manifest.json", "sha256": "0" * 64,
                                   "bytes": 1, "copy": "outputs/push_manifest.json"}]},
        exit_code=1)
    (push_dir / "outputs" / "push_manifest.json").write_text(json.dumps(
        {"status": "stopped", "counts": {"written": 15, "unchanged": 0, "verified": 14}}))
    args = argparse.Namespace(workspace=str(workspace), command="ablate", ids=None,
                              without_report=[reports["rules"]], with_report=[reports["catalog"]],
                              without_context=None, with_context=None)

    caveats = _conclusion_caveats(args)

    assert ["  the catalog was pushed to between without context and with context: 15 entries "
            "written (20260905-150439-catalog-push); if the arms are meant to differ by one of "
            "them, the others are confounds"] == [c for c in caveats if "pushed to" in c]


def test_ablate_says_when_the_catalog_was_pushed_to_between_the_arms(tmp_path, capsys, monkeypatch):
    """A push is a change of the catalog; the manifests know when it happened
    and the arms know when they ran."""
    fake = _fake_catalog(monkeypatch, {"sales__orders": "old overview " * 200})
    db = _seed_db(tmp_path)
    ws = ["--workspace", str(tmp_path / "ws")]
    questions = _write_csv(tmp_path / "q.csv", ["id", "question", "gold_sql"], [("q1", "names?", "SELECT name FROM emp")])
    schema = tmp_path / "schema.txt"; schema.write_text("CREATE TABLE emp(name TEXT)")
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "sales.orders.md").write_text("# sales.orders\n" + "| x | y |\n" * 300)

    def arm(name, answer):
        # two arms with identical answers would write identical reports, and
        # a report is linked to its run by hash; real arms differ
        preds, report = tmp_path / f"{name}.csv", tmp_path / f"{name}.json"
        assert main(ws + ["generate", "--questions", questions, "--schema", str(schema), "--out", str(preds),
                          "--backend", "echo", "--echo-response", answer]) == 0
        assert main(ws + ["score", "--db", db, "--predictions", str(preds), "--save-report", str(report)]) == 0
        return str(report)

    before = arm("a", "SELECT age FROM emp")
    import time; time.sleep(1.1)              # run ids are second-resolution
    assert main(ws + PUSH + ["--docs", str(docs)]) == 0
    time.sleep(1.1)
    after = arm("b", "SELECT name FROM emp")
    capsys.readouterr()

    assert main(ws + ["ablate", "--without-report", before, "--with-report", after]) == 0

    assert "the catalog was pushed to between without context and with context: 1 entries written" in capsys.readouterr().out


def test_a_local_query_is_bounded_too_unless_the_bound_is_turned_off(tmp_path, capsys):
    """A generated cross join hung a scoring run of 12 questions for ten
    minutes on SQLite, with nothing printed. Predicted SQL is untrusted model
    output on a local database as much as on a warehouse."""
    from context_lift.cli import SQLITE_DEFAULT_TIMEOUT, _executor_for
    import argparse

    db = tmp_path / "x.sqlite"
    sqlite3.connect(db).execute("CREATE TABLE t (a INT)")

    args = argparse.Namespace(db=str(db), timeout=None, max_bytes=None)
    _executor_for(args)
    assert args.timeout == SQLITE_DEFAULT_TIMEOUT
    assert "bounded at 60 s on this database" in capsys.readouterr().err

    args = argparse.Namespace(db=str(db), timeout=0, max_bytes=None)
    _executor_for(args)
    assert args.timeout is None                       # asked for, on purpose
    assert capsys.readouterr().err == ""


def test_split_says_what_its_reserved_half_can_prove(tmp_path, capsys):
    """A cold run held back 29 questions, spent its whole budget, and only at
    the end read "you would need about 40". The size is decided at split, so
    the limit belongs at split. 93 golds is that run's key; 1/sqrt(29) is 19
    points."""
    questions = _write_csv(tmp_path / "q.csv", ["id", "gold_sql"],
                           [(f"q{i}", f"SELECT {i}") for i in range(93)])

    code = main(["split", "--golds", questions,
                 "--out-curate", str(tmp_path / "c.txt"),
                 "--out-holdout", str(tmp_path / "h.txt")])

    out = capsys.readouterr().out
    assert code == 0
    assert "reserved questions :   29 ids" in out
    assert "At 29 reserved questions this split can only prove a lift above" in out
    assert "about 19 points" in out


def test_a_bigger_reserved_half_can_prove_a_smaller_gain(tmp_path, capsys):
    """The message has to move with the number, or it is decoration."""
    questions = _write_csv(tmp_path / "q.csv", ["id", "gold_sql"],
                           [(f"q{i}", f"SELECT {i}") for i in range(400)])

    main(["split", "--golds", questions, "--holdout-fraction", "0.25",
          "--out-curate", str(tmp_path / "c.txt"),
          "--out-holdout", str(tmp_path / "h.txt")])

    assert "about 10 points" in capsys.readouterr().out


def test_context_write_can_add_a_round_instead_of_replacing_it(tmp_path):
    """draft-rules writes one failure bucket at a time, and context-write
    replaced the file, so round 2 dropped round 1. The only way to hold both
    was a person editing the file, which is the one step no analyst repeats.
    A cold run stopped at one round for exactly this."""
    first = tmp_path / "r1.md"
    first.write_text('# round 1\n\n- "revenue" -> SUM(sale_price)\n- "margin" -> price - cost\n')
    second = tmp_path / "r2.md"
    second.write_text('# round 2\n\n- "margin" -> price - cost\n- "returns" -> status = \'R\'\n')
    rules = tmp_path / "rules.md"

    main(["context-write", "--source-file", str(first), "--to", str(rules)])
    main(["context-write", "--source-file", str(second), "--to", str(rules), "--add"])

    text = rules.read_text()
    assert '- "revenue" -> SUM(sale_price)' in text          # round 1 survived
    assert '- "returns" -> status = \'R\'' in text            # round 2 arrived
    assert text.count('- "margin" -> price - cost') == 1     # not added twice


def test_context_write_without_add_still_replaces(tmp_path):
    """The default has to stay what it was: a reviewed draft replaces the
    working file, and nothing silently accumulates."""
    first = tmp_path / "r1.md"
    first.write_text('- "revenue" -> SUM(sale_price)\n')
    second = tmp_path / "r2.md"
    second.write_text('- "returns" -> status = \'R\'\n')
    rules = tmp_path / "rules.md"

    main(["context-write", "--source-file", str(first), "--to", str(rules)])
    main(["context-write", "--source-file", str(second), "--to", str(rules)])

    assert rules.read_text() == '- "returns" -> status = \'R\'\n'


def test_gate_raises_the_literal_warning_when_the_csv_carries_questions(tmp_path, capsys):
    """The check needs the question text, and `gate` took only (id, sql) pairs,
    so a key audited straight from a CSV never raised it even when the file had
    a question column sitting beside the SQL."""
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE people(name TEXT, dept TEXT)")
    con.execute("INSERT INTO people VALUES ('Wong, Lee', 'EECS')")
    con.commit()
    con.close()
    golds = _write_csv(tmp_path / "g.csv", ["id", "question", "gold_sql"], [
        ("g1", "the mailing list about duo users",
         "SELECT dept FROM people WHERE name = 'Wong, Lee'"),
        ("g2", "how many people are there?", "SELECT COUNT(*) FROM people")])

    code = main(["gate", "--db", str(db), "--golds", golds])

    out = capsys.readouterr().out
    assert code == 0                                    # a warning is not a failure
    assert "gate: 2 golds, 2 clean, 0 flagged" in out
    assert "warnings: MISSING_LITERAL=1" in out
    assert "kept, with a warning:" in out


def test_rewrite_questions_redrafts_a_key_without_touching_the_warehouse(tmp_path, capsys):
    """Once a check is added to the gate, every key written before it has
    questions that fail it. Re-mining to fix them pays to execute golds that
    are already gated and reviewed: $0.77 of warehouse scan on one estate.
    Nothing here runs SQL - the statements do not change, so the golds stay
    gated and only the question text is written again."""
    golds = _write_csv(tmp_path / "g.csv", ["id", "question", "gold_sql", "source"], [
        ("g1", "units for a specific product",
         "SELECT day, SUM(units) FROM sales WHERE product_code = 'PRD-1042' GROUP BY day",
         "reviewed")])
    out = tmp_path / "v2.csv"

    code = main(["rewrite-questions", "--golds", golds, "--out", str(out),
                 "--backend", "echo", "--echo-response", "units of PRD-1042 by day"])

    written = list(csv.DictReader(open(out, newline="")))
    assert code == 0
    assert written[0]["question"] == "units of PRD-1042 by day"
    assert written[0]["question_owes"] == ""            # nothing left to fix
    assert written[0]["source"] == "reviewed"           # every column survives
    assert written[0]["gold_sql"].startswith("SELECT day")   # the SQL is untouched


def test_rewrite_questions_names_what_a_row_still_owes(tmp_path, capsys):
    """A row the retry could not fix is written out with the reason on it, not
    dropped and not silently kept as if it were fine."""
    golds = _write_csv(tmp_path / "g.csv", ["id", "question", "gold_sql"], [
        ("g1", "the orders", "SELECT order_date, amount FROM orders ORDER BY amount DESC")])
    out = tmp_path / "v2.csv"

    main(["rewrite-questions", "--golds", golds, "--out", str(out),
          "--backend", "echo", "--echo-response", "the orders"])

    owes = list(csv.DictReader(open(out, newline="")))[0]["question_owes"]
    assert "it returns order_date, amount" in owes
    assert "which rows come first" in owes
    assert "still owe something" in capsys.readouterr().out


def test_rewrite_questions_prices_a_ceiling_before_it_spends(tmp_path, capsys):
    """Two calls per row is the worst case: a draft and a retry."""
    golds = _write_csv(tmp_path / "g.csv", ["id", "question", "gold_sql"],
                       [(f"g{i}", "q", "SELECT a FROM t") for i in range(3)])

    code = main(["rewrite-questions", "--golds", golds, "--out", str(tmp_path / "v2.csv"),
                 "--estimate-only"])

    out = capsys.readouterr().out
    assert code == 0
    assert "up to 6 gemini-2.5-flash calls" in out
    assert "at most $" in out
    assert not (tmp_path / "v2.csv").exists()          # nothing written


def test_rewrite_questions_names_what_it_cannot_reach(tmp_path, capsys):
    """A question rewrite cannot repair a gold whose SQL is the problem. On one
    estate 25 flagged golds became 14, and WHOLE_ROW and LIMIT_NO_ORDER did not
    move by one, because neither is about the question. A user watching the
    count stop at 14 will ask why."""
    golds = _write_csv(tmp_path / "g.csv", ["id", "question", "gold_sql"], [
        ("g1", "the orders", "SELECT * FROM orders"),
        ("g2", "some rows", "SELECT a FROM t LIMIT 5"),
        ("g3", "a count", "SELECT COUNT(*) FROM t")])

    main(["rewrite-questions", "--golds", golds, "--out", str(tmp_path / "v2.csv"),
          "--backend", "echo", "--echo-response", "how many rows are in the table"])

    out = capsys.readouterr().out
    assert "2 gold(s) carry a defect no rewrite can reach" in out


def test_rewrite_questions_warns_that_routing_and_cost_both_move(tmp_path, capsys):
    """Both measured on one estate, both surprising, both easy to blame on the
    wrong thing. Routing: the router reads the question text, so a question made
    precise about columns matches differently and 2 of 25 correct tables were
    lost. Cost: better questions produce more ambitious SQL, and the next score
    run went from 15 GB to 92 GB billed."""
    golds = _write_csv(tmp_path / "g.csv", ["id", "question", "gold_sql"],
                       [("g1", "a count", "SELECT COUNT(*) FROM t")])

    main(["rewrite-questions", "--golds", golds, "--out", str(tmp_path / "v2.csv"),
          "--backend", "echo", "--echo-response", "how many rows"])

    out = capsys.readouterr().out
    assert "re-measure routing" in out
    assert "expect the next score run to cost more" in out


def test_the_first_report_defines_its_own_jargon(tmp_path, capsys):
    """A field engineer sits beside a business analyst who knows none of these
    words. "execution accuracy" and "95% CI" are the first two terms anyone
    meets, on the first line of the first report, and nothing else on the page
    explained either."""
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE emp(name TEXT, age INT)")
    con.execute("INSERT INTO emp VALUES ('ann', 30)")
    con.commit()
    con.close()
    predictions = _write_csv(tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"], [
        ("q1", "who", "SELECT name FROM emp", "SELECT name FROM emp"),
        ("q2", "how old", "SELECT age FROM emp", "SELECT name FROM emp"),
        ("q3", "nobody", "SELECT name FROM emp WHERE age > 99", "SELECT name FROM emp"),
        ("q4", "broken", "SELECT name FROM emp", "SELECT nope FROM emp")])

    main(["score", "--db", str(db), "--predictions", predictions])

    out = capsys.readouterr().out
    assert "the share of questions whose answer returned the same rows" in out
    assert "the range the true share is probably in" in out
    # every verdict code carries its meaning on the same line
    assert "MISMATCH    wrong     ran, but returned different rows" in out
    assert "one side returned no rows and the other returned some" in out
    assert "the agent's query did not run" in out


def test_every_verdict_the_scorer_can_print_has_a_meaning():
    """A code with no meaning is the defect; a meaning with no code is dead
    text. Both directions checked, so neither drifts."""
    from context_lift.harness import (GOLD_ERROR, PRED_ERROR, ROW_CAP_HIT,
                                  VERDICT_MEANINGS)

    printable = {"MATCH", "MISMATCH", "EMPTY_ONE", "EMPTY_BOTH", PRED_ERROR,
                 GOLD_ERROR, ROW_CAP_HIT}

    assert set(VERDICT_MEANINGS) == printable
    assert all(VERDICT_MEANINGS[name] for name in printable)


def test_split_uses_one_name_for_each_half(tmp_path, capsys):
    """One screen had used curate, working, hold out, holdout and reserved for
    the same two things. The rest of the product says working and reserved."""
    questions = _write_csv(tmp_path / "q.csv", ["id", "gold_sql"],
                           [(f"q{i}", f"SELECT {i}") for i in range(20)])

    main(["split", "--golds", questions, "--out-curate", str(tmp_path / "c.txt"),
          "--out-holdout", str(tmp_path / "h.txt")])

    out = capsys.readouterr().out
    assert "working questions" in out and "reserved questions" in out
    assert "curate against" not in out and "hold out  " not in out
    assert "Score the holdout" not in out


def test_gate_can_exit_zero_so_a_chain_survives_a_flagged_gold(tmp_path):
    """Non-zero on a defect is deliberate: a CI step guards on it. It also
    breaks a plain `gate && next` chain, which is how a cold run met it and
    stopped. The caller can now choose, and the default did not move."""
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE emp(name TEXT)")
    con.execute("INSERT INTO emp VALUES ('ann')")
    con.commit()
    con.close()
    golds = _write_csv(tmp_path / "g.csv", ["id", "gold_sql"],
                       [("g1", "SELECT * FROM emp")])       # a browse: WHOLE_ROW

    assert main(["gate", "--db", str(db), "--golds", golds]) == 1
    assert main(["gate", "--db", str(db), "--golds", golds, "--exit-zero-on-defects"]) == 0


def test_the_flagged_file_carries_the_reason(tmp_path):
    """`next` tells a reviewer that N golds were dropped and names
    golds_flagged.csv. That file held id, question and gold_sql and no reason
    at all, so the one file the tool pointed at was the one place the reasons
    were not."""
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE emp(name TEXT)")
    con.execute("INSERT INTO emp VALUES ('ann')")
    con.commit()
    con.close()
    golds = _write_csv(tmp_path / "g.csv", ["id", "question", "gold_sql"],
                       [("g1", "everything", "SELECT * FROM emp"),
                        ("g2", "how many", "SELECT COUNT(*) FROM emp")])
    flagged = tmp_path / "flagged.csv"
    clean = tmp_path / "clean.csv"

    main(["gate", "--db", str(db), "--golds", golds,
          "--flagged-out", str(flagged), "--clean-out", str(clean)])

    rows = list(csv.DictReader(open(flagged, newline="")))
    assert [r["id"] for r in rows] == ["g1"]
    assert rows[0]["gate_defects"] == "WHOLE_ROW"
    assert "a browse and not a question" in rows[0]["gate_reason"]
    # the clean file keeps the caller's columns and gains nothing
    assert list(csv.DictReader(open(clean, newline="")).fieldnames) == [
        "id", "question", "gold_sql"]


def test_repeatable_flags_take_values_after_one_flag_or_a_repeated_flag():
    """A run with three repetitions passes three reports. Repeating the flag
    per file is what the help used to require; listing the files after one
    flag is the same thing, and both must parse to the same list."""
    parser = _build_parser()

    listed = parser.parse_args(
        ["ablate", "--with-report", "a.json", "b.json", "--without-report", "c.json"])
    repeated = parser.parse_args(
        ["ablate", "--with-report", "a.json", "--with-report", "b.json",
         "--without-report", "c.json"])

    assert listed.with_report == ["a.json", "b.json"] == repeated.with_report
    assert listed.without_report == ["c.json"] == repeated.without_report


def test_climb_rounds_and_mine_queries_take_several_values_too():
    parser = _build_parser()

    climb = parser.parse_args(["climb", "--round", "base=a.csv", "v1=b.csv"])
    mine = parser.parse_args(
        ["mine", "--db", "w.db", "--queries", "one.csv", "two.csv"])

    assert climb.round == ["base=a.csv", "v1=b.csv"]
    assert mine.queries == ["one.csv", "two.csv"]


class _FreeFakeBackend:
    """Stands in for the paid backend so the guard can be reached without a
    credential. Counting tokens is local arithmetic; nothing here calls out."""

    usage = {"prompt_tokens": 0, "output_tokens": 0}
    prices = None
    budget_usd = None

    def count_tokens(self, prompt):
        return len(prompt) // 4

    def __call__(self, prompt):
        raise AssertionError("the guard should have refused before any call")


def _no_credentials_needed(monkeypatch):
    """`review --draft` builds its backend from this factory."""
    import context_lift.backends as backends
    monkeypatch.setattr(backends, "gemini_backend",
                        lambda **kwargs: _FreeFakeBackend(), raising=False)


def test_a_paid_review_refuses_with_no_terminal_and_records_why(tmp_path, capsys,
                                                                monkeypatch):
    """The guard is wired into the verb, not into a wrapper around it.

    The test runner has no controlling terminal, which is exactly the position
    a host agent, a cron job and a CI runner are in. All four are refused
    rather than trusted, because a host on auto-approve would answer the
    prompt for the caller.

    The backend is faked so this needs no credential and no network. What is
    being tested is the call site, not the SDK.
    """
    _no_credentials_needed(monkeypatch)
    candidates = _write_csv(tmp_path / "c.csv", CANDIDATE_COLUMNS, CANDIDATES)
    assert main(["review", "--candidates", candidates, "--out", str(tmp_path / "r")]) == 0
    sheet = tmp_path / "r" / "review.csv"
    before = sheet.read_bytes()
    capsys.readouterr()

    code = main(["review", "--draft", "--sheet", str(sheet), "--backend", "gemini",
                 "--budget", "5"])

    assert code == 2
    assert "no controlling terminal" in capsys.readouterr().out
    assert sheet.read_bytes() == before, "nothing was drafted"


def test_yes_without_a_budget_is_refused_at_the_command_line(tmp_path, capsys,
                                                             monkeypatch):
    """A yes with no ceiling is a blank cheque, and the ceiling is what makes a
    mistake annoying rather than expensive."""
    _no_credentials_needed(monkeypatch)
    candidates = _write_csv(tmp_path / "c.csv", CANDIDATE_COLUMNS, CANDIDATES)
    assert main(["review", "--candidates", candidates, "--out", str(tmp_path / "r")]) == 0
    sheet = tmp_path / "r" / "review.csv"
    capsys.readouterr()

    code = main(["review", "--draft", "--sheet", str(sheet), "--backend", "gemini",
                 "--yes"])

    assert code == 2
    assert "blank cheque" in capsys.readouterr().out


def test_a_missing_credential_is_a_sentence_not_a_stack_trace(tmp_path, capsys,
                                                              monkeypatch):
    """A fresh clone with no credentials got google.auth's own exception out of
    the middle of the client. That is an ordinary problem for a new user."""
    import context_lift.backends as backends

    def raise_credentials(**kwargs):
        raise RuntimeError("DefaultCredentialsError: Your default credentials "
                           "were not found")

    monkeypatch.setattr(backends, "gemini_backend", raise_credentials, raising=False)
    candidates = _write_csv(tmp_path / "c.csv", CANDIDATE_COLUMNS, CANDIDATES)
    assert main(["review", "--candidates", candidates, "--out", str(tmp_path / "r")]) == 0
    capsys.readouterr()

    code = main(["review", "--draft", "--sheet", str(tmp_path / "r" / "review.csv"),
                 "--backend", "gemini"])

    assert code == 2
    assert "needs Google Cloud credentials" in capsys.readouterr().out


def test_split_writes_its_id_lists_beside_the_golds_not_the_current_directory(tmp_path, monkeypatch, capsys):
    """The old default was "curate_ids.txt", relative, so the file landed
    wherever the shell happened to be. Splitting a customer gold set from
    this checkout overwrote two tracked example id lists and said nothing,
    and two gold sets split from one shell overwrote each other."""
    golds_dir = tmp_path / "estate"
    golds_dir.mkdir()
    golds = golds_dir / "golds.csv"
    golds.write_text("id,question,gold_sql\n"
                     + "".join(f"g{i},q{i},SELECT {i}\n" for i in range(10)))
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    decoy = elsewhere / "curate_ids.txt"
    decoy.write_text("do not touch me\n")
    monkeypatch.chdir(elsewhere)

    assert main(["split", "--golds", str(golds)]) == 0

    assert (golds_dir / "curate_ids.txt").is_file(), "the split belongs to its gold set"
    assert (golds_dir / "holdout_ids.txt").is_file()
    assert decoy.read_text() == "do not touch me\n", "nothing in the shell's directory is touched"
    # and the printed paths say where they went, so nobody hunts for them
    out = capsys.readouterr().out
    assert str(golds_dir / "curate_ids.txt") in out


def test_the_time_estimate_counts_the_model_calls_that_dominate_it():
    """`mine --write-questions` drafts one question per shape. On one estate
    that was 437 calls and 42 minutes while the queries took under four, and
    the line printed "about 2.4 minutes": a plan built on it was wrong by a
    factor of seventeen."""
    from context_lift.cli import _format_sql_time
    estimate = {"calls": 343, "seconds_per_call": 0.7, "workers": 1,
                "expected_minutes": 4.0}

    with_model = _format_sql_time(estimate, model_calls=437)
    assert "437 model calls" in with_model
    assert "15 to 87 minutes" in with_model, "the range a person plans around"
    assert "593 calls took 110 minutes" in with_model, "the measurement, named as one"

    # and a stage that makes no model call reads exactly as it did
    without = _format_sql_time(estimate)
    assert "model calls" not in without
    assert "343 queries at 0.7 s each" in without


def test_a_stage_with_only_model_calls_still_says_how_long():
    """Every shape can be filtered out before the gate, so the query count is
    zero while the drafting still takes half an hour. "nothing to run" was
    printed for exactly that case."""
    from context_lift.cli import _format_sql_time
    text = _format_sql_time({"calls": 0}, model_calls=437)
    assert "no query to run" in text
    assert "437 model calls" in text
    assert "nothing to run" not in text


def test_no_command_that_executes_sql_has_an_unbounded_row_cap():
    """Two multi-hour hangs in one day came from `--row-cap` defaulting to
    None, and both were a result set nobody expected.

    `mine` gated a candidate whose result was 401,396,362 rows and spent
    three runs building Python dicts for them. `round` executed a PREDICTED
    query -- SQL the model wrote, so untrusted by definition -- and reached
    6.5 GB of memory, climbing 10 MB a second, before it was killed.

    Both stages already handle a capped result correctly: the gate calls it
    TRUNCATED and the scorer calls it ROW_CAP_HIT and EXCLUDES it rather than
    comparing a full result to a cut one. Neither ever fired, because no cap
    was ever set.

    So the rule is the default, not the flag. Anybody may pass `--row-cap 0`
    for no cap; nobody should get that by accident.
    """
    from context_lift.cli import DEFAULT_ROW_CAP, _build_parser
    assert DEFAULT_ROW_CAP > 0
    parser = _build_parser()
    # the subparser actions, by command name
    sub = next(a for a in parser._actions if hasattr(a, "choices") and a.choices)
    unbounded = []
    for name, command in sub.choices.items():
        for action in command._actions:
            if "--row-cap" in (action.option_strings or []):
                if action.default is None:
                    unbounded.append(name)
    assert not unbounded, (
        "these commands execute SQL with no row cap by default, so one "
        f"runaway result hangs the run: {sorted(unbounded)}")


def test_attribute_reads_its_context_before_it_spends_anything(tmp_path):
    """A bad --context used to raise AFTER 286 statements had been billed.

    Measured 2026-09-13 in a Cloud Run job: `spent: 286 queries, 6.263 GB
    billed, ~$0.0356` and then `IsADirectoryError` from the read at the
    bottom of the command. Validation belongs before the spend.
    """
    from context_lift.cli import main
    preds = tmp_path / "p.csv"
    preds.write_text("id,question,gold_sql,pred_sql\nq1,a,SELECT 1,SELECT 1\n")
    with pytest.raises(SystemExit) as caught:
        main(["attribute", "--without-context", str(preds),
              "--with-context", str(preds),
              "--context", str(tmp_path / "nope.md"),
              "--db", "sqlite:///" + str(tmp_path / "t.db")])
    assert "not found" in str(caught.value)


def test_attribute_takes_a_spec_directory_as_its_context(tmp_path):
    """Every other command takes --spec DIR, so a directory is what a person
    passes. Its rules/ files are read in name order, each labelled with its
    file name so a suspected line can be traced back."""
    from context_lift.cli import _read_context_rules
    spec = tmp_path / "spec" / "rules"
    spec.mkdir(parents=True)
    (spec / "10-second.md").write_text('- "margin" -> price minus t.cost\n')
    (spec / "00-first.md").write_text('- "revenue" -> sum of t.sale_price\n')

    text = _read_context_rules(str(tmp_path / "spec"))

    assert text.index("00-first.md") < text.index("10-second.md")
    assert "sale_price" in text and "t.cost" in text


def test_attribute_says_so_when_a_context_directory_has_no_rules(tmp_path):
    from context_lift.cli import _read_context_rules
    empty = tmp_path / "spec"
    empty.mkdir()
    with pytest.raises(SystemExit) as caught:
        _read_context_rules(str(empty))
    assert "no rules in it" in str(caught.value)
