# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""verdict.html must carry the CLI's numbers, refuse an invalid pairing, and
escape every string it prints. All built from a temp workspace, offline."""
import csv
import json
import sqlite3

import pytest

from context_lift.cli import main

COLUMNS = ["id", "question", "gold_sql", "pred_sql"]


def _seed_db(tmp_path):
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE emp(name TEXT, age INT)")
    con.executemany("INSERT INTO emp VALUES (?, ?)", [("ann", 30), ("bob", 40)])
    con.commit()
    con.close()
    return str(path)


def _write_csv(path, rows):
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        writer.writerows(rows)
    return str(path)


def _arm(workspace, db, predictions, context=None):
    """import + score one predictions file; return the import run id."""
    argv = ["--workspace", str(workspace), "import", "--predictions", predictions]
    if context:
        argv += ["--context", context]
    assert main(argv) == 0
    assert main(["--workspace", str(workspace), "score", "--db", db,
                 "--predictions", predictions]) == 0
    index = json.loads((workspace / "index.json").read_text())
    return [r for r in index["runs"] if r["command"] == "import"][-1]["run_id"]


def _two_arms(tmp_path):
    db = _seed_db(tmp_path)
    workspace = tmp_path / "ws"
    base = _write_csv(tmp_path / "base.csv", [
        ("q<1>", "names?", "SELECT name FROM emp", "SELECT age FROM emp"),   # wrong
        ("q2", "ages?", "SELECT age FROM emp", "SELECT age FROM emp"),        # right
        ("q3", "count?", "SELECT COUNT(*) FROM emp", "SELECT 1 FROM nope"),  # wrong
    ])
    rules = tmp_path / "rules.md"
    rules.write_text('- "staff" -> emp.name\n')
    arm = _write_csv(tmp_path / "arm.csv", [
        ("q<1>", "names?", "SELECT name FROM emp", "SELECT name FROM emp"),  # fixed
        ("q2", "ages?", "SELECT age FROM emp", "SELECT name FROM emp"),       # broke
        ("q3", "count?", "SELECT COUNT(*) FROM emp", "SELECT 1 FROM nope"),  # still wrong
    ])
    base_run = _arm(workspace, db, base)
    arm_run = _arm(workspace, db, arm, str(rules))
    assert main(["--workspace", str(workspace), "pin", "baseline", base_run]) == 0
    assert main(["--workspace", str(workspace), "pin", "candidate", arm_run]) == 0
    return workspace


def test_the_bundle_carries_the_verdict_both_p_values_the_leak_badge_and_escapes_ids(tmp_path, capsys):
    workspace = _two_arms(tmp_path)

    assert main(["--workspace", str(workspace), "bundle"]) == 0

    verdict = json.loads((workspace / "verdict.json").read_text())
    page = (workspace / "verdict.html").read_text()
    # UNDERPOWERED, not NOT PROVEN: a fixture this small could never have
    # shown a 10-point lift, so "not proven" would claim it looked and
    # found nothing.
    assert verdict["verdict"]["word"] == "UNDERPOWERED"
    statuses = {q["id"]: q["status"] for q in verdict["questions"]}
    assert statuses == {"q<1>": "fixed", "q2": "broke", "q3": "wrong"}
    assert verdict["ablation"]["mcnemar"] == {"b": 1, "c": 1, "p_value": 1.0}
    assert "p = 1" in page                      # the defended p-value
    assert "Counting every run separately" in page   # the pooled count is shown too
    assert "not run" in page                    # no leak run for this context
    assert "q&lt;1&gt;" in page and "q<1>" not in page
    assert "<script" not in page
    assert verdict["rules"][0]["text"].startswith('- "staff"')
    assert verdict["questions"][0]["id"] == "q2"      # regressions first


def test_the_bundle_says_what_it_holds_and_no_rows_drops_the_previews(tmp_path, capsys):
    """The row previews are the customer's data; the inventory line is
    counted from the file just written, and --no-rows keeps the verdict and
    the shape note but no values."""
    workspace = _two_arms(tmp_path)

    assert main(["--workspace", str(workspace), "bundle"]) == 0
    printed = capsys.readouterr().out
    assert ("  verdict.html holds 3 questions, 9 SQL statements (the answer key's and the "
            "model's), 1 row previews (4 rows), the rules file (1 lines); verdict.json also "
            "holds the database path. Review both before sending outside the customer; "
            "--no-rows drops the previews.") in printed
    # Quoted, because a bare "ann" also matches inside "cannot" and any other
    # ordinary word the page happens to print. A three-letter substring is not
    # a leak check; a whole JSON string value is.
    assert '"ann"' in (workspace / "verdict.json").read_text()     # a cell value travels

    assert main(["--workspace", str(workspace), "bundle", "--no-rows",
                 "--out", str(workspace / "share.html")]) == 0
    printed = capsys.readouterr().out
    assert "0 row previews (--no-rows)" in printed
    verdict = json.loads((workspace / "share.json").read_text())
    saw = {q["id"]: q["scorer_saw"] for q in verdict["questions"]}
    assert saw["q2"] == {"verdict": "MISMATCH",
                         "detail": "same shape (2 rows x 1 columns), different values",
                         "rows": None}
    assert '"ann"' not in (workspace / "share.json").read_text()
    assert "row previews left out (bundle --no-rows)" in (workspace / "share.html").read_text()


def test_the_inventory_names_the_bq_spec_when_the_workspace_ran_on_bigquery():
    from context_lift.bundle import bundle_inventory
    verdict = {
        "workspace": {"db": {"kind": "bigquery", "path": "bq://billing/shop"}},
        "rule_lines": [{"line_no": 2}, {"line_no": 3}],
        "questions": [
            {"gold_sql": "SELECT 1", "base_sql": ["SELECT 2", ""], "ctx_sql": ["SELECT 3", "SELECT 4"],
             "scorer_saw": {"verdict": "MISMATCH", "detail": "",
                            "rows": {"gold": [{"a": 1}], "pred": [{"a": 2}, {"a": 3}],
                                     "gold_total": 1, "pred_total": 2}}},
            {"gold_sql": "SELECT 5", "base_sql": ["SELECT 6"], "ctx_sql": ["SELECT 6"],
             "scorer_saw": None},
        ],
    }

    assert bundle_inventory(verdict) == {
        "questions": 2, "sql_statements": 7, "row_previews": 1, "rows": 3,
        "rule_lines": 2, "context": "the rules file (2 lines)", "rounds": [],
        "bq_spec": "bq://billing/shop"}


def test_the_bundle_refuses_arms_that_did_not_score_the_same_questions(tmp_path):
    db = _seed_db(tmp_path)
    workspace = tmp_path / "ws"
    base = _write_csv(tmp_path / "base.csv",
                      [("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp")])
    other = _write_csv(tmp_path / "other.csv",
                       [("q9", "ages?", "SELECT age FROM emp", "SELECT age FROM emp")])
    rules = tmp_path / "rules.md"
    rules.write_text("# r\n")
    main(["--workspace", str(workspace), "pin", "baseline", _arm(workspace, db, base)])
    main(["--workspace", str(workspace), "pin", "candidate",
          _arm(workspace, db, other, str(rules))])

    with pytest.raises(SystemExit) as refusal:
        main(["--workspace", str(workspace), "bundle"])

    assert "did not score the same questions" in str(refusal.value)


def test_the_bundle_refuses_without_a_pin(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    with pytest.raises(SystemExit) as refusal:
        main(["--workspace", str(workspace), "bundle"])

    assert "pin baseline" in str(refusal.value)


def test_an_even_swing_gets_the_chance_sentence_not_a_times_in_100_count(tmp_path):
    """One fixed and one broke: the defended p is 1, and "happens by luck
    about 100 times in 100" reads as nonsense, so the sentence says what
    the number means instead."""
    import json
    from test_loop import build_workspace
    from context_lift.bundle import EVEN_SWING, collect, render
    ws = build_workspace(tmp_path)
    pins = json.loads((ws / "pins.json").read_text())

    verdict = collect(ws, pins["baseline"], pins["candidate"])

    assert verdict["p_text"]["luck_pct"] == 100
    html = render(verdict)
    assert EVEN_SWING in html
    assert "100 times in 100" not in html


def _generated_workspace(tmp_path):
    """A db, a 3-question file and a schema; returns (workspace, db, questions,
    schema). Arms are made with `generate`, so the index carries a backend."""
    db = _seed_db(tmp_path)
    questions = tmp_path / "q.csv"
    with open(questions, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "question", "gold_sql"])
        writer.writerows([("q1", "names?", "SELECT name FROM emp"),
                          ("q2", "ages?", "SELECT age FROM emp"),
                          ("q3", "count?", "SELECT COUNT(*) FROM emp")])
    schema = tmp_path / "schema.txt"
    schema.write_text("CREATE TABLE emp(name TEXT, age INT)")
    return tmp_path / "ws", db, str(questions), str(schema)


def _generate_and_score(workspace, db, questions, schema, out, backend_argv):
    W = ["--workspace", str(workspace)]
    assert main(W + ["generate", "--questions", questions, "--schema", schema, "--out", out] + backend_argv) == 0
    assert main(W + ["score", "--db", db, "--predictions", out]) == 0


def test_the_bundle_describes_a_retrieved_context_arm_and_finds_its_leak_run(tmp_path):
    """An arm made with --context-per-question has no rules file. The page
    used to say "no rule lines" and "Leak check: not run" even after
    `leak --context-per-question` had run on the same file."""
    workspace, db, questions, schema = _generated_workspace(tmp_path)
    retrieved = tmp_path / "retrieved.json"
    retrieved.write_text(json.dumps({"q1": "emp.name holds the person's name",
                                     "q3": "count rows with COUNT(*)"}))      # q2 got nothing
    _generate_and_score(workspace, db, questions, schema, str(tmp_path / "base.csv"),
                        ["--backend", "echo", "--echo-response", "SELECT age FROM emp"])
    _generate_and_score(workspace, db, questions, schema, str(tmp_path / "ctx.csv"),
                        ["--backend", "echo", "--echo-response", "SELECT name FROM emp",
                         "--context-per-question", str(retrieved)])
    assert main(["--workspace", str(workspace), "leak", "--context-per-question", str(retrieved),
                 "--predictions", questions]) == 0

    assert main(["--workspace", str(workspace), "bundle"]) == 0

    verdict = json.loads((workspace / "verdict.json").read_text())
    page = (workspace / "verdict.html").read_text()
    assert verdict["candidate"]["retrieved_sha256"] and verdict["candidate"]["context_sha256"] is None
    assert verdict["retrieval"]["served"] == 2 and verdict["retrieval"]["nothing"] == 1
    assert verdict["leak"]["status"] == "clean"
    assert "<h2>The retrieved context</h2>" in page
    assert "a retriever chose the context per question" in page
    assert "<b>2 of 3</b> questions were served" in page and "<b>1</b> retrieved nothing" in page
    assert "no rule lines" not in page
    assert "Leak check on the context: <b>clean</b>" in page
    assert verdict["candidate"]["label"].startswith("retrieved ")


def test_the_rounds_say_what_each_of_them_used(tmp_path, capsys):
    """With a retrieval arm as the candidate, the page said "the rules file
    (0 lines)" and verdict.json carried rule_lines [], while a rules round
    with 8 lines sat in the rounds table on the same page."""
    workspace, db, questions, schema = _generated_workspace(tmp_path)
    rules = tmp_path / "rules.md"
    rules.write_text("# rules v1\n" + "".join(
        f'- "term {i}" -> emp.name\n' for i in range(8)))
    retrieved = tmp_path / "retrieved.json"
    retrieved.write_text(json.dumps({"q1": "emp.name holds the person's name",
                                     "q3": "count rows with COUNT(*)"}))
    _generate_and_score(workspace, db, questions, schema, str(tmp_path / "base.csv"),
                        ["--backend", "echo", "--echo-response", "SELECT age FROM emp"])
    _generate_and_score(workspace, db, questions, schema, str(tmp_path / "rules.csv"),
                        ["--backend", "echo", "--echo-response", "SELECT name FROM emp",
                         "--context", str(rules)])
    _generate_and_score(workspace, db, questions, schema, str(tmp_path / "ctx.csv"),
                        ["--backend", "echo", "--echo-response", "SELECT name, age FROM emp",
                         "--context-per-question", str(retrieved)])
    capsys.readouterr()

    assert main(["--workspace", str(workspace), "bundle"]) == 0

    printed = capsys.readouterr().out
    verdict = json.loads((workspace / "verdict.json").read_text())
    assert verdict["rule_lines"] == []                      # the candidate has no rules file
    assert [(r["label"][:9], r["context"]) for r in verdict["rounds"]] == [
        ("rules v1", "8 rule lines"),
        ("retrieved", "retrieved text for 2 of 3 questions")]
    assert "the retrieved context for 3 questions;" in printed
    assert "  what each round used: 1 rules v1 (8 rule lines); 2 retrieved " in printed
    assert "(retrieved text for 2 of 3 questions)" in printed
    assert "<th>what it used</th>" in (workspace / "verdict.html").read_text()


def test_the_bundle_names_the_runner_version_for_an_arm_from_a_command_backend(tmp_path):
    """Through --backend command the model option is argparse's default and
    names nothing; the page used to call the customer's own agent
    "model gemini-2.5-flash"."""
    import sys
    workspace, db, questions, schema = _generated_workspace(tmp_path)
    runner = tmp_path / "runner.py"
    runner.write_text("import json, sys\nq = json.load(sys.stdin)\n"
                      "print('SELECT name FROM emp' if q['context'] else 'SELECT age FROM emp')\n")
    rules = tmp_path / "rules.md"
    rules.write_text('- "staff" -> emp.name\n')
    agent = ["--backend", "command", "--command", f"{sys.executable} {runner}", "--runner-version", "agent 1.2"]
    _generate_and_score(workspace, db, questions, schema, str(tmp_path / "base.csv"), agent)
    _generate_and_score(workspace, db, questions, schema, str(tmp_path / "ctx.csv"),
                        agent + ["--context", str(rules)])

    assert main(["--workspace", str(workspace), "bundle"]) == 0

    verdict = json.loads((workspace / "verdict.json").read_text())
    page = (workspace / "verdict.html").read_text()
    assert (verdict["candidate"]["backend"], verdict["candidate"]["runner_version"]) == ("command", "agent 1.2")
    assert page.count("agent: agent 1.2 (your own runner, via --backend command)") == 2   # header and footer
    assert "the agent&#x27;s cost is its own" in page or "the agent's cost is its own" in page
    assert "gemini" not in page
    assert "same agent, same database" in verdict["identity"]


def test_the_bundle_refuses_arms_from_two_runner_versions(tmp_path):
    import sys
    workspace, db, questions, schema = _generated_workspace(tmp_path)
    runner = tmp_path / "runner.py"
    runner.write_text("import json, sys\njson.load(sys.stdin)\nprint('SELECT name FROM emp')\n")
    rules = tmp_path / "rules.md"
    rules.write_text('- "staff" -> emp.name\n')
    command = ["--backend", "command", "--command", f"{sys.executable} {runner}"]
    _generate_and_score(workspace, db, questions, schema, str(tmp_path / "base.csv"),
                        command + ["--runner-version", "agent 1.2"])
    _generate_and_score(workspace, db, questions, schema, str(tmp_path / "ctx.csv"),
                        command + ["--runner-version", "agent 2.0", "--context", str(rules)])

    with pytest.raises(SystemExit) as refusal:
        main(["--workspace", str(workspace), "bundle"])

    assert "agent version (agent 1.2 vs agent 2.0)" in str(refusal.value)


def _slide_workspace(tmp_path):
    """`_two_arms` plus the runs a slide draws on: a mine and a review run
    (the funnel), a gate over the reviewed golds, a split over the arms'
    questions, a routing report, and one `ablate --ids holdout_ids.txt`
    reading. Returns (workspace, the ablate argv of that reading)."""
    workspace = _two_arms(tmp_path)
    db = str(tmp_path / "t.db")
    W = ["--workspace", str(workspace)]
    log = tmp_path / "log.csv"
    with open(log, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sql"])
        writer.writerows([("SELECT name FROM emp",), ("SELECT age FROM emp",), ("SELECT 1 FROM nope",)])
    assert main(W + ["mine", "--db", db, "--queries", str(log), "--all-datasets",
                     "--out", str(tmp_path / "candidates.csv")]) == 0
    assert main(W + ["review", "--candidates", str(tmp_path / "candidates.csv"),
                     "--out", str(tmp_path / "review")]) == 0
    sheet = tmp_path / "review" / "review.csv"
    with open(sheet, newline="") as handle:
        rows = list(csv.DictReader(handle))
        columns = list(rows[0])
    for row in rows:
        clean = row["gate_status"] == "ok"
        row.update(keep="yes" if clean else "no", reason="" if clean else "broken")
    with open(sheet, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    assert main(W + ["review", "--sheet", str(sheet), "--out", str(tmp_path / "golds.csv")]) == 0
    assert main(W + ["gate", "--db", db, "--golds", str(tmp_path / "golds.csv")]) == 0
    questions = tmp_path / "arm_questions.csv"
    with open(questions, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "question", "gold_sql"])
        writer.writerows([("q<1>", "names?", "SELECT name FROM emp"), ("q2", "ages?", "SELECT age FROM emp"),
                          ("q3", "count?", "SELECT COUNT(*) FROM emp")])
    assert main(W + ["split", "--golds", str(questions), "--holdout-fraction", "0.5"]) == 0
    routing = tmp_path / "routing.csv"
    with open(routing, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "question", "gold_table", "pred_table"])
        writer.writerows([("r1", "names?", "main.emp", "main.emp"), ("r2", "ages?", "main.emp", "main.nope")])
    assert main(W + ["score-routing", "--predictions", str(routing),
                     "--save-report", str(tmp_path / "routing_report.json")]) == 0
    index = json.loads((workspace / "index.json").read_text())
    reports = [str(workspace / "runs" / r["run_id"] / r["outputs"]["save_report"][0])
               for r in index["runs"] if r["command"] == "score"]
    split = [r for r in index["runs"] if r["command"] == "split"][0]
    holdout_ids = workspace / "runs" / split["run_id"] / split["outputs"]["out_holdout"][0]
    reading = W + ["ablate", "--without-report", reports[0], "--with-report", reports[1],
                   "--ids", str(holdout_ids)]
    assert main(reading) == 0
    return workspace, reading


def test_the_bundle_carries_the_slide_sections_and_the_holdout_reading(tmp_path):
    workspace, reading = _slide_workspace(tmp_path)

    assert main(["--workspace", str(workspace), "bundle"]) == 0

    verdict = json.loads((workspace / "verdict.json").read_text())
    page = (workspace / "verdict.html").read_text()
    assert verdict["holdout_looks"] == 1
    holdout = verdict["holdout_readings"]
    assert len(holdout) == 1 and holdout[0]["command"] == "ablate"
    assert holdout[0]["n_questions"] == 2 and holdout[0]["word"] == "UNDERPOWERED"
    assert "the reserved questions, read once: this is the number to report" in page
    assert verdict["failure_shapes"]["n_failed"] == {"without": 2, "with": 2}
    assert {b["bucket"] for b in verdict["failure_shapes"]["buckets"]} == {"WRONG_VALUES", "DID_NOT_RUN"}
    rounds = verdict["rounds"]
    assert len(rounds) == 1 and rounds[0]["error"] is None
    assert (rounds[0]["label"], rounds[0]["word"], rounds[0]["fixed"], rounds[0]["broke"]) == ("rules v1", "UNDERPOWERED", 1, 1)
    assert rounds[0]["with_text"] == "33.3%" and rounds[0]["lift_text"] == "+0.0 pts"
    routing = verdict["routing"]
    assert len(routing) == 1 and routing[0]["label"] == "routing.csv" and routing[0]["arm_id"] is None
    assert (routing[0]["k"], routing[0]["n"], routing[0]["rate_text"]) == (1, 2, "50.0%")
    funnel = verdict["funnel"]
    assert (funnel["mined"]["candidates"], funnel["mined"]["clean"]) == (3, 2)
    assert (funnel["reviewed"]["kept"], funnel["reviewed"]["total"], funnel["reviewed"]["rejected"]) == (2, 3, {"broken": 1})
    assert (funnel["gate"]["n"], funnel["gate"]["flagged"]) == (2, 0)
    assert funnel["split"]["curate"] + funnel["split"]["holdout"] == 3 and funnel["split"]["holdout"] == 2
    assert verdict["spend"]["total_usd"] == 0.0 and verdict["spend"]["runs_with_spend"] == 0
    assert verdict["lift_interval_text"] == "[-92.4 pts, +92.4 pts]"      # 1 fixed, 1 broke, 3 pairs
    assert "difference [-92.4 pts, +92.4 pts] at 95%" in page
    for heading in ("The reserved questions", "Failures by shape", "Rounds", "Routing accuracy",
                    "The question funnel", "Spend"):
        assert f"<h2>{heading}" in page, heading
    assert "<td class='mono'>WRONG_VALUES</td>" in page and "routing.csv" in page
    assert "mined <b>3</b> candidate shapes" in page and "review kept <b>2</b> of 3" in page

    # a second look turns the label red: the reserved questions are a curate set now
    assert main(reading) == 0
    assert main(["--workspace", str(workspace), "bundle"]) == 0
    page = (workspace / "verdict.html").read_text()
    assert "<span class='red'><b>read twice: a curate set now</b></span>" in page
    assert json.loads((workspace / "verdict.json").read_text())["holdout_looks"] == 2


def test_the_verdict_caveats_say_when_a_model_accepted_the_golds(tmp_path):
    workspace = _two_arms(tmp_path)
    W = ["--workspace", str(workspace)]
    candidates = tmp_path / "candidates.csv"
    with open(candidates, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "question", "gold_sql", "times_seen", "gate_status", "gate_detail", "table"])
        writer.writerows([("c1", "names?", "SELECT name FROM emp", 3, "ok", "", "main.emp"),
                          ("c2", "ages?", "SELECT age FROM emp", 2, "ok", "", "main.emp")])
    assert main(W + ["review", "--candidates", str(candidates), "--out", str(tmp_path / "review")]) == 0
    sheet = tmp_path / "review" / "review.csv"
    assert main(W + ["review", "--draft", "--sheet", str(sheet), "--backend", "echo", "--echo-response",
                     '[{"id": "c1", "keep": "yes", "reason": "matches"}, '
                     '{"id": "c2", "keep": "no", "reason": "ambiguous_question"}]']) == 0
    assert main(W + ["review", "--sheet", str(sheet), "--out", str(tmp_path / "golds.csv")]) == 0

    assert main(W + ["bundle"]) == 0

    verdict = json.loads((workspace / "verdict.json").read_text())
    review_run = [r for r in json.loads((workspace / "index.json").read_text())["runs"]
                  if r["command"] == "review" and r["summary"]][-1]
    caveat = ("the answer key: 1 of 1 golds accepted by a model reviewer (gemini-2.5-flash), 0 by a "
              "person; have a person re-review a sample before quoting the number")
    assert verdict["answer_key"] == {"run_id": review_run["run_id"], "model_reviewed": 1,
                                     "person_reviewed": 0, "reviewer_model": "gemini-2.5-flash",
                                     "caveat": caveat}     # imported arms name no agent model
    assert f"<li class='amber'>{caveat}</li>" in (workspace / "verdict.html").read_text()


def test_the_bundle_says_what_this_many_questions_can_prove_under_the_word(tmp_path):
    """The sentence after the ruling is the terminal's, printed in the card."""
    from context_lift.bundle import collect, render
    db = _seed_db(tmp_path)
    workspace = tmp_path / "ws"
    base = _write_csv(tmp_path / "base.csv", [
        ("q1", "names?", "SELECT name FROM emp", "SELECT age FROM emp"),
        ("q2", "ages?", "SELECT age FROM emp", "SELECT name FROM emp"),
        ("q3", "count?", "SELECT COUNT(*) FROM emp", "SELECT 1 FROM nope"),
    ])
    rules = tmp_path / "rules.md"
    rules.write_text("# docs v1\n")
    arm = _write_csv(tmp_path / "arm.csv", [
        ("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp"),      # fixed
        ("q2", "ages?", "SELECT age FROM emp", "SELECT age FROM emp"),         # fixed
        ("q3", "count?", "SELECT COUNT(*) FROM emp", "SELECT 1 FROM nope"),
    ])
    main(["--workspace", str(workspace), "pin", "baseline", _arm(workspace, db, base)])
    main(["--workspace", str(workspace), "pin", "candidate", _arm(workspace, db, arm, str(rules))])

    verdict = collect(workspace)

    # UNDERPOWERED, not NOT PROVEN: a fixture this small could never have
    # shown a 10-point lift, so "not proven" would claim it looked and
    # found nothing.
    assert verdict["verdict"]["word"] == "UNDERPOWERED"
    assert verdict["verdict_sentences"] == [
        "at 3 questions this set could not have shown a 10-point lift even if "
        "nothing broke, and about 60 questions could; "
        "to prove the +66.7% seen here you would need about 9 questions"]   # 6 fixed of 9: p = 2/64
    assert verdict["verdict_sentences"][0] in render(verdict)
    assert verdict["rounds"][0]["label"] == "docs v1"


def test_the_footer_names_the_dataset_when_the_database_has_no_file_hash():
    from context_lift.bundle import _db_fingerprint
    assert _db_fingerprint({"sha256": "abcdef0123456789", "kind": "sqlite", "path": "t.db"}) == "abcdef012345"
    assert _db_fingerprint({"sha256": None, "kind": "bigquery", "path": "bq://billing/pub.shop"}) \
        == "pub.shop (BigQuery dataset)"


def test_the_page_says_its_word_could_not_have_been_invalid_or_overfit():
    """A word from one arm pair is not a round's verdict, and the page says so.

    Without the sentence a reader carries away REAL GAIN from a page that had
    no gate able to override it. That is the exact shape of the customer page
    that read REAL GAIN on a working-set lift of +60.6 points beside a flat
    held-out set.
    """
    from context_lift.ablate import UNGATED_WORD

    assert "INVALID" in UNGATED_WORD
    assert "OVERFIT" in UNGATED_WORD
    assert "contextlift round" in UNGATED_WORD
