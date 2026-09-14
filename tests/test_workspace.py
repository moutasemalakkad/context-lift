# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Recorded runs: a manifest must say exactly what a command read and wrote.

A wrong hash or a missed output would be silent - the viewer would show a
number it cannot trace - so these tests pin the recorder's facts with
literal files.
"""
import csv
import datetime as dt
import hashlib
import json
import os
import pathlib
import sqlite3

from context_lift.cli import main
from context_lift.workspace import _arm_record, arm_id, new_run_id, rebuild_index


def _seed_db(tmp_path):
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE emp(name TEXT, age INT)")
    con.executemany("INSERT INTO emp VALUES (?, ?)", [("ann", 30), ("bob", 40)])
    con.commit()
    con.close()
    return str(path)


def _write_csv(path, header, rows):
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return str(path)


def _sha(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def _only_manifest(workspace, command):
    runs = [p for p in (workspace / "runs").iterdir() if command in p.name]
    assert len(runs) == 1, runs
    return runs[0], json.loads((runs[0] / "manifest.json").read_text())


def test_a_recorded_score_run_lists_its_two_inputs_with_true_hashes(tmp_path):
    db = _seed_db(tmp_path)
    preds = _write_csv(
        tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp")],
    )
    workspace = tmp_path / "ws"

    code = main(["--workspace", str(workspace), "score", "--db", db,
                 "--predictions", preds])

    assert code == 0
    run_dir, manifest = _only_manifest(workspace, "score")
    assert sorted(manifest["inputs"]) == ["db", "predictions"]
    assert manifest["inputs"]["db"]["sha256"] == _sha(db)
    assert manifest["inputs"]["db"]["copy"] is None          # hashed, not copied
    assert manifest["inputs"]["predictions"]["sha256"] == _sha(preds)
    assert (run_dir / manifest["inputs"]["predictions"]["copy"]).read_text() \
        == pathlib.Path(preds).read_text()
    assert manifest["exit_code"] == 0
    assert manifest["options"]["confidence"] == 0.95
    # the unset --save-report defaulted into the run directory
    report = manifest["outputs"]["save_report"][0]
    assert report["copy"] == "outputs/report.json"
    assert "results" in json.loads((run_dir / "outputs/report.json").read_text())
    assert "execution accuracy" in (run_dir / "stdout.txt").read_text()


def test_a_workspace_defaults_the_gold_cache_and_the_checkpoint(tmp_path):
    """Both are working files: the cache is hashed as an input once it exists
    and never copied; the checkpoint lands in the run directory."""
    db = _seed_db(tmp_path)
    preds = _write_csv(
        tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp ORDER BY name")],
    )
    workspace = tmp_path / "ws"
    argv = ["--workspace", str(workspace), "score", "--db", db, "--predictions", preds]

    assert main(argv) == 0
    assert main(argv) == 0

    cache = workspace / "cache" / "golds.json"
    assert list(json.loads(cache.read_text())["golds"]) == ["SELECT name FROM emp"]
    # Directories only. `runs/` also holds a `.gitignore` (see
    # protect_runs_from_git), and `iterdir` counted it as a run. Production
    # reads runs with glob("*/manifest.json") and was never affected.
    runs = sorted(p for p in (workspace / "runs").iterdir() if p.is_dir())
    assert len(runs) == 2
    first, second = (json.loads((r / "manifest.json").read_text()) for r in runs)
    assert "gold_cache" not in first["inputs"]                 # it did not exist yet
    assert len(second["inputs"]["gold_cache"]["sha256"]) == 64
    assert second["inputs"]["gold_cache"]["copy"] is None     # hashed, not copied
    assert "1 answer(s) reused, not executed" in (runs[1] / "stdout.txt").read_text()
    for run_dir, manifest in zip(runs, (first, second)):
        assert (run_dir / "outputs" / "checkpoint.json").exists()
        assert manifest["outputs"]["checkpoint"][0]["copy"] is None
        assert manifest["outputs"]["save_report"][0]["copy"] == "outputs/report.json"
    assert (workspace / ".gitignore").read_text().splitlines()[2] == "cache/"


def test_a_generate_run_with_two_reps_records_both_files_and_a_baseline_arm(tmp_path):
    questions = _write_csv(
        tmp_path / "q.csv", ["id", "question", "gold_sql"],
        [("q1", "names?", "SELECT name FROM emp")],
    )
    schema = tmp_path / "schema.txt"
    schema.write_text("CREATE TABLE emp(name TEXT, age INT)")
    workspace = tmp_path / "ws"

    code = main(["--workspace", str(workspace), "generate",
                 "--questions", questions, "--schema", str(schema),
                 "--out", str(tmp_path / "arm.csv"), "--backend", "echo",
                 "--reps", "2"])

    assert code == 0
    _, manifest = _only_manifest(workspace, "generate")
    written = [r["path"] for r in manifest["outputs"]["out"]]
    assert written == [str(tmp_path / "arm_rep1.csv"), str(tmp_path / "arm_rep2.csv")]
    assert manifest["arm"]["context_sha256"] is None
    assert manifest["arm"]["examples_sha256"] is None
    assert manifest["arm"]["label"] == "no context"
    assert len(manifest["arm"]["arm_id"]) == 12


def test_the_index_links_a_score_run_to_the_generate_run_by_hash(tmp_path):
    db = _seed_db(tmp_path)
    questions = _write_csv(
        tmp_path / "q.csv", ["id", "question", "gold_sql"],
        [("q1", "names?", "SELECT name FROM emp")],
    )
    schema = tmp_path / "schema.txt"
    schema.write_text("CREATE TABLE emp(name TEXT, age INT)")
    workspace = tmp_path / "ws"
    out = str(tmp_path / "arm.csv")
    main(["--workspace", str(workspace), "generate", "--questions", questions,
          "--schema", str(schema), "--out", out, "--backend", "echo"])
    main(["--workspace", str(workspace), "score", "--db", db, "--predictions", out])
    # a run killed before it finished leaves a start-only manifest
    half = workspace / "runs" / "20260101-000000-gate"
    half.mkdir()
    (half / "manifest.json").write_text(json.dumps({
        "run_id": "20260101-000000-gate", "command": "gate",
        "inputs": {}, "outputs": {}, "exit_code": None, "ended_at": None}))

    index = rebuild_index(workspace)

    by_command = {run["command"]: run for run in index["runs"]}
    generate_run = by_command["generate"]
    score_run = by_command["score"]
    assert score_run["produced_by"] == {"predictions": generate_run["run_id"]}
    assert score_run["arm_id"] == generate_run["arm_id"]
    assert score_run["summary"]["n_scored"] == 1
    assert by_command["gate"]["finished"] is False
    assert index["arms"][0]["score_runs"] == [score_run["run_id"]]


def test_two_runs_of_one_command_in_the_same_second_get_distinct_ids(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    moment = dt.datetime(2026, 9, 1, 12, 0, 0, tzinfo=dt.timezone.utc)

    first = new_run_id(runs, "score", now=moment)
    (runs / first).mkdir()
    second = new_run_id(runs, "score", now=moment)

    assert first == "20260901-120000-score"
    assert second == "20260901-120000-score-2"


def _import(workspace, tmp_path, predictions, context=None):
    argv = ["--workspace", str(workspace), "import", "--predictions", predictions]
    if context:
        argv += ["--context", context]
    assert main(argv) == 0
    index = json.loads((workspace / "index.json").read_text())
    return index["runs"][-1]


def test_import_gives_outside_predictions_an_arm_that_follows_the_context_bytes(tmp_path):
    preds = _write_csv(
        tmp_path / "vendor.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp")],
    )
    rules_a = tmp_path / "rules_a.md"
    rules_a.write_text("- \"staff\" -> emp\n")
    rules_b = tmp_path / "rules_b.md"
    rules_b.write_text("- \"staff\" -> emp \n")          # one byte more
    workspace = tmp_path / "ws"

    baseline = _import(workspace, tmp_path, preds)
    with_a = _import(workspace, tmp_path, preds, str(rules_a))
    with_a_again = _import(workspace, tmp_path, preds, str(rules_a))
    with_b = _import(workspace, tmp_path, preds, str(rules_b))

    assert baseline["arm_id"] != with_a["arm_id"]
    assert with_a["arm_id"] == with_a_again["arm_id"]
    assert with_a["arm_id"] != with_b["arm_id"]
    index = json.loads((workspace / "index.json").read_text())
    assert len(index["arms"]) == 3
    # the copy is byte-identical, so a later score run links to this import
    copied = workspace / "runs" / with_b["run_id"] / "outputs" / "predictions.csv"
    assert copied.read_bytes() == pathlib.Path(preds).read_bytes()


def test_pin_resolves_a_run_id_to_its_arm_and_refuses_unknown_ids(tmp_path, capsys):
    preds = _write_csv(
        tmp_path / "vendor.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp")],
    )
    workspace = tmp_path / "ws"
    run = _import(workspace, tmp_path, preds)

    assert main(["--workspace", str(workspace), "pin", "baseline", run["run_id"]]) == 0
    assert main(["--workspace", str(workspace), "pin", "candidate", "nope"]) == 2

    pins = json.loads((workspace / "pins.json").read_text())
    assert pins == {"baseline": run["arm_id"], "candidate": None}
    assert "not a run id or arm id" in capsys.readouterr().out


def test_an_ablate_save_report_round_trips_the_paired_numbers(tmp_path):
    db = _seed_db(tmp_path)
    base = _write_csv(
        tmp_path / "base.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q1", "names?", "SELECT name FROM emp", "SELECT age FROM emp"),
         ("q2", "ages?", "SELECT age FROM emp", "SELECT age FROM emp")],
    )
    arm = _write_csv(
        tmp_path / "arm.csv", ["id", "question", "gold_sql", "pred_sql"],
        [("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp"),
         ("q2", "ages?", "SELECT age FROM emp", "SELECT name FROM emp")],
    )
    report = tmp_path / "ablation.json"

    code = main(["ablate", "--db", db, "--without-context", base,
                 "--with-context", arm, "--save-report", str(report)])

    assert code == 0
    saved = json.loads(report.read_text())
    # the CLI pools even a single run, so observation ids carry #rep0
    assert saved["broke_ids"] == ["q2#rep0"]
    assert saved["fixed_ids"] == ["q1#rep0"]
    assert saved["mcnemar"] == {"b": 1, "c": 1, "p_value": 1.0}
    assert saved["clustered"] == {"b": 1, "c": 1, "p_value": 1.0}
    assert [q["status"] for q in saved["per_question"]] == ["fixed", "broke"]


def _questions_and_schema(tmp_path):
    questions = _write_csv(
        tmp_path / "q.csv", ["id", "question", "gold_sql"],
        [("q1", "names?", "SELECT name FROM emp"), ("q2", "ages?", "SELECT age FROM emp")],
    )
    schema = tmp_path / "schema.txt"
    schema.write_text("CREATE TABLE emp(name TEXT, age INT)")
    return questions, str(schema)


def test_a_retrieval_arm_is_indexed_as_a_context_arm_and_becomes_the_candidate(tmp_path):
    """`generate --context-per-question` used to reach the index with only a
    rules hash of None, so the pins took it for a baseline. The retrieved
    hash must reach the index and count as context."""
    db = _seed_db(tmp_path)
    questions, schema = _questions_and_schema(tmp_path)
    retrieved = tmp_path / "retrieved.json"
    retrieved.write_text(json.dumps({"q1": "emp.name holds the person's name"}))   # q2 got nothing
    workspace = tmp_path / "ws"
    W = ["--workspace", str(workspace)]
    # the retrieval arm is scored FIRST: the order that made it the baseline
    ctx = str(tmp_path / "ctx.csv")
    assert main(W + ["generate", "--questions", questions, "--schema", schema, "--out", ctx,
                     "--backend", "echo", "--echo-response", "SELECT name FROM emp",
                     "--context-per-question", str(retrieved)]) == 0
    assert main(W + ["score", "--db", db, "--predictions", ctx]) == 0
    base = str(tmp_path / "base.csv")
    assert main(W + ["generate", "--questions", questions, "--schema", schema, "--out", base,
                     "--backend", "echo", "--echo-response", "SELECT age FROM emp"]) == 0
    assert main(W + ["score", "--db", db, "--predictions", base]) == 0

    index = rebuild_index(workspace)

    baseline = next(a for a in index["arms"] if a["label"] == "no context")
    retrieval_arm = next(a for a in index["arms"] if a["label"] != "no context")
    assert len(retrieval_arm["retrieved_sha256"]) == 64
    assert retrieval_arm["label"] == f"retrieved {retrieval_arm['retrieved_sha256'][:12]}"
    assert retrieval_arm["context_sha256"] is None
    assert (retrieval_arm["backend"], retrieval_arm["runner_version"]) == ("echo", None)
    assert index["pins"]["baseline"] == baseline["arm_id"]
    assert index["pins"]["candidate"] == retrieval_arm["arm_id"]


def test_the_index_arm_names_the_backend_and_the_runner_version_of_a_command_run(tmp_path):
    import sys
    questions, schema = _questions_and_schema(tmp_path)
    runner = tmp_path / "runner.py"
    runner.write_text("import json, sys\njson.load(sys.stdin)\nprint('SELECT name FROM emp')\n")
    workspace = tmp_path / "ws"

    code = main(["--workspace", str(workspace), "generate", "--questions", questions,
                 "--schema", schema, "--out", str(tmp_path / "agent.csv"),
                 "--backend", "command", "--command", f"{sys.executable} {runner}",
                 "--runner-version", "agent 1.2"])

    assert code == 0
    arm = rebuild_index(workspace)["arms"][0]
    assert (arm["backend"], arm["runner_version"]) == ("command", "agent 1.2")
    assert arm["retrieved_sha256"] is None


def test_an_ablate_over_the_split_holdout_ids_counts_as_one_look(tmp_path):
    """Arms are generated over the whole key and read twice with --ids. The
    holdout reading is the `ablate --ids holdout_ids.txt` run: one look per
    command, both arms included; the curate reading is not a look."""
    db = _seed_db(tmp_path)
    golds = _write_csv(tmp_path / "q.csv", ["id", "question", "gold_sql"],
                       [(f"q{i}", f"question {i}", "SELECT name FROM emp") for i in range(12)])
    schema = tmp_path / "schema.txt"
    schema.write_text("CREATE TABLE emp(name TEXT, age INT)")
    workspace = tmp_path / "ws"
    W = ["--workspace", str(workspace)]
    assert main(W + ["split", "--golds", golds, "--out-curate", str(tmp_path / "curate_ids.txt"),
                     "--out-holdout", str(tmp_path / "holdout_ids.txt")]) == 0
    preds, report = str(tmp_path / "p.csv"), str(tmp_path / "p.json")
    assert main(W + ["generate", "--questions", golds, "--schema", str(schema),
                     "--out", preds, "--backend", "echo"]) == 0
    assert main(W + ["score", "--db", db, "--predictions", preds, "--save-report", report]) == 0
    assert rebuild_index(workspace)["holdout_looks"] == 0

    assert main(W + ["ablate", "--without-report", report, "--with-report", report,
                     "--ids", str(tmp_path / "curate_ids.txt")]) == 0
    assert rebuild_index(workspace)["holdout_looks"] == 0          # the training reading
    assert main(W + ["ablate", "--without-report", report, "--with-report", report,
                     "--ids", str(tmp_path / "holdout_ids.txt")]) == 0
    index = rebuild_index(workspace)

    assert index["holdout_looks"] == 1
    ablates = [r for r in index["runs"] if r["command"] == "ablate"]
    assert [r["on_holdout"] for r in ablates] == [False, True]
    assert main(W + ["ablate", "--without-report", report, "--with-report", report,
                     "--ids", str(tmp_path / "holdout_ids.txt")]) == 0
    assert rebuild_index(workspace)["holdout_looks"] == 2          # read twice: a curate set now


def test_a_workspace_under_an_unwritable_path_is_one_line_not_a_traceback(tmp_path, capsys):
    """A mistyped --workspace used to raise PermissionError out of pathlib
    and print 40 lines of traceback."""
    if os.geteuid() == 0:
        return                                    # root writes anywhere
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        code = main(["--workspace", str(locked / "ws"), "next"])
    finally:
        locked.chmod(0o700)

    assert code == 2
    assert capsys.readouterr().err.strip() == (
        f"cannot create workspace {locked / 'ws'}: permission denied")


def test_two_arms_over_the_holdout_compared_plainly_are_one_look(tmp_path):
    """The reserved questions are answered once at the end by the two pinned
    arms over holdout.csv and compared with a plain `ablate`. That is one
    look; the index saw two scored arms and no reading, because a reading was
    only recognised by the hash of an --ids file."""
    db = _seed_db(tmp_path)
    golds = _write_csv(tmp_path / "q.csv", ["id", "question", "gold_sql"],
                       [(f"q{i}", f"question {i}", "SELECT name FROM emp") for i in range(12)])
    schema = tmp_path / "schema.txt"
    schema.write_text("CREATE TABLE emp(name TEXT, age INT)")
    rules = tmp_path / "rules.md"
    rules.write_text("# rules\n- \"names\" -> emp.name\n")
    workspace = tmp_path / "ws"
    W = ["--workspace", str(workspace)]
    holdout = str(tmp_path / "holdout.csv")
    assert main(W + ["split", "--golds", golds, "--out-holdout-csv", holdout,
                     "--out-curate-csv", str(tmp_path / "curate.csv")]) == 0

    reports = []
    for name, context in (("base", []), ("ctx", ["--context", str(rules)])):
        preds, report = str(tmp_path / f"{name}.csv"), str(tmp_path / f"{name}.json")
        assert main(W + ["generate", "--questions", holdout, "--schema", str(schema),
                         "--out", preds, "--backend", "echo",
                         "--echo-response", f"SELECT name FROM emp -- {name}"] + context) == 0
        assert main(W + ["score", "--db", db, "--predictions", preds,
                         "--save-report", report]) == 0
        reports.append(report)
    assert rebuild_index(workspace)["holdout_looks"] == 2   # answered, not yet read

    assert main(W + ["ablate", "--without-report", reports[0],
                     "--with-report", reports[1]]) == 0

    index = rebuild_index(workspace)
    assert index["holdout_looks"] == 1
    assert [r["on_holdout"] for r in index["runs"] if r["command"] == "ablate"] == [True]


def test_an_arm_over_a_renamed_copy_of_the_holdout_is_still_on_the_holdout(tmp_path):
    """The file may be copied or re-sorted; the reserved ids are what say so."""
    db = _seed_db(tmp_path)
    golds = _write_csv(tmp_path / "q.csv", ["id", "question", "gold_sql"],
                       [(f"q{i}", f"question {i}", "SELECT name FROM emp") for i in range(12)])
    schema = tmp_path / "schema.txt"
    schema.write_text("CREATE TABLE emp(name TEXT, age INT)")
    workspace = tmp_path / "ws"
    W = ["--workspace", str(workspace)]
    holdout = tmp_path / "holdout.csv"
    assert main(W + ["split", "--golds", golds, "--out-holdout-csv", str(holdout),
                     "--out-curate-csv", str(tmp_path / "curate.csv")]) == 0
    rows = list(csv.DictReader(holdout.open(newline="")))
    renamed = _write_csv(tmp_path / "sealed.csv", ["id", "question", "gold_sql"],
                         [(r["id"], r["question"], r["gold_sql"]) for r in reversed(rows)])
    preds, report = str(tmp_path / "p.csv"), str(tmp_path / "p.json")
    assert main(W + ["generate", "--questions", renamed, "--schema", str(schema),
                     "--out", preds, "--backend", "echo"]) == 0
    assert main(W + ["score", "--db", db, "--predictions", preds,
                     "--save-report", report]) == 0

    index = rebuild_index(workspace)

    assert index["holdout_looks"] == 1
    assert [r["on_holdout"] for r in index["runs"] if r["command"] == "generate"] == [True]


def test_two_runner_versions_are_two_arms():
    """Under --backend command the context never reaches the framework, so
    the context files are the only thing naming an arm. One estate ran four
    no-context generates, two per agent version, and got ONE arm carrying
    whichever version created it. The other version was not in the index at
    all, so nothing could pin the two against each other."""
    before = arm_id(None, None, None, "agent@abc123")
    after = arm_id(None, None, None, "agent@abc123+ctxbudget150k")

    assert before != after


def test_an_arm_id_written_before_this_does_not_move():
    """Only a run that passed --runner-version changes id. Every workspace
    recorded without one keeps the arm ids already in its manifests."""
    assert arm_id("ctx", None) == arm_id("ctx", None, None, None)
    assert arm_id(None, None) == arm_id(None, None, None, "")


def test_the_runner_version_shows_in_the_arm_label():
    """A verdict that says "no context" twice tells the reader nothing about
    which agent answered."""
    record = _arm_record({}, {}, None, "agent@abc123")

    assert record["label"] == "no context (agent@abc123)"
    assert record["runner_version"] == "agent@abc123"
