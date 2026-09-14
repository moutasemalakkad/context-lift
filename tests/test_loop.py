# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The loop's JSON must be the CLI's numbers and must not read outside the
workspace. A temp workspace built with the CLI, a server on a free port."""
import csv
import json
import shlex
import sqlite3

from context_lift.cli import main
from context_lift.loop import shell_line


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


def build_workspace(tmp_path):
    """gate + split on 4 questions (q4's gold sums a text column, so gate
    flags it), two imported arms (one fixed, one broke), both scored, both
    pinned. Returns the workspace path."""
    db = _seed_db(tmp_path)
    ws = tmp_path / "ws"
    W = ["--workspace", str(ws)]
    questions = _write_csv(tmp_path / "q.csv", ["id", "question", "gold_sql"], [
        ("q1", "names?", "SELECT name FROM emp"),
        ("q2", "ages?", "SELECT age FROM emp"),
        ("q3", "count?", "SELECT COUNT(*) FROM emp"),
        ("q4", "top names?", FLAGGED_GOLD),
    ])
    assert main(W + ["gate", "--db", db, "--golds", questions]) == 1      # exit 1: q4 is flagged
    assert main(W + ["split", "--golds", questions, "--holdout-fraction", "0.34"]) == 0
    cols = ["id", "question", "gold_sql", "pred_sql"]
    base = _write_csv(tmp_path / "base.csv", cols, [
        ("q1", "names?", "SELECT name FROM emp", "SELECT age FROM emp"),
        ("q2", "ages?", "SELECT age FROM emp", "SELECT age FROM emp"),
        ("q3", "count?", "SELECT COUNT(*) FROM emp", "SELECT 1 FROM nope"),
        ("q4", "top names?", FLAGGED_GOLD, "SELECT name FROM emp ORDER BY name"),
    ])
    rules = tmp_path / "rules.md"
    rules.write_text('# docs v1\n- "staff" -> emp.name  # from q1\n')
    arm = _write_csv(tmp_path / "arm.csv", cols, [
        ("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp"),
        ("q2", "ages?", "SELECT age FROM emp", "SELECT name FROM emp"),
        ("q3", "count?", "SELECT COUNT(*) FROM emp", "SELECT 1 FROM nope"),
        ("q4", "top names?", FLAGGED_GOLD, "SELECT name FROM emp ORDER BY name"),
    ])
    ids = []
    for preds, extra in ((base, []), (arm, ["--context", str(rules)])):
        assert main(W + ["import", "--predictions", preds] + extra) == 0
        assert main(W + ["score", "--db", db, "--predictions", preds]) == 0
        index = json.loads((ws / "index.json").read_text())
        ids.append([r for r in index["runs"] if r["command"] == "import"][-1]["run_id"])
    assert main(W + ["pin", "baseline", ids[0]]) == 0
    assert main(W + ["pin", "candidate", ids[1]]) == 0
    return ws


def _get(ws, path):
    """The same payload the JSON layer used to serve, called directly.

    There is no server any more: `contextlift next` and `contextlift chat` call these
    functions. The payloads are still worth pinning because the CLI prints them
    and `bundle` writes them into verdict.html."""
    import urllib.parse

    from context_lift import loop as L
    from context_lift.bundle import collect
    url = urllib.parse.urlparse(path)
    q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
    routes = {
        "/api/answer_key": lambda: L.api_answer_key(ws),
        "/api/context": lambda: L.api_context(ws),
        "/api/next": lambda: L.api_next(ws),
        "/api/rounds": lambda: L.api_rounds(ws),
        "/api/compare": lambda: collect(ws, q.get("base"), q.get("cand")),
        "/api/file": lambda: L.read_workspace_file(ws, q.get("path", "")),
    }
    return routes[url.path]()


def test_the_answer_key_seals_holdout_gold_sql_until_it_is_opened(tmp_path):
    ws = build_workspace(tmp_path)
    body = _get(ws, "/api/answer_key")
    key = body
    assert key["gate"]["n"] == 4 and key["gate"]["flagged"] == 1
    flagged = next(r for r in key["rows"] if r["status"] == "flagged")
    assert flagged["id"] == "q4" and flagged["defects"] == ["TEXT_AGGREGATE"]
    assert flagged["meaning"] == ["sums or averages a text column; the ranking is meaningless"]
    assert key["split"]["holdout"] >= 1
    holdout = [r for r in key["rows"] if r["status"] == "holdout"]
    assert holdout and all(r["gold_sql"] is None for r in holdout)
    assert all(r["question"] for r in holdout)      # the question text still shows
    body = _get(ws, "/api/context")
    context = body
    assert [v["label"] for v in context["versions"]] == ["docs v1"]
    assert context["versions"][0]["leak"]["status"] == "not checked"


def _post(ws, path, payload):
    """The two POST payloads that were not the launcher: both are functions the
    terminal calls."""
    from context_lift import loop as L
    if path == "/api/chat":
        return L.api_chat(ws, payload)
    if path == "/api/estimate":
        return L.api_estimate(ws, list(payload.get("args") or []),
                              str(payload.get("command") or "generate"))
    if path == "/api/thread":
        return L.append_thread(ws, payload)
    raise KeyError(path)


def test_the_schema_step_is_offered_only_where_a_generate_needs_one(tmp_path):
    """A workspace whose predictions were imported never recorded a schema and
    is not waiting on one; the step exists so the page never has to tell the
    reader to go and run a command somewhere else."""
    from context_lift.loop import shell_line, ALLOWED_COMMANDS, SETUP_STEPS, api_next, is_paid
    assert "schema" in ALLOWED_COMMANDS               # the button has to be able to launch it
    assert [k for k, _ in SETUP_STEPS].count("schema") == 1
    assert is_paid("schema", ["--db", "bq://p/d"]) is True     # it scans INFORMATION_SCHEMA
    assert is_paid("schema", ["--db", "warehouse.db"]) is False
    ws = build_workspace(tmp_path)
    assert api_next(ws)["state"] != "need_schema"     # already scored, nothing to wait for


def test_a_flag_written_with_an_equals_sign_still_trips_every_guard(tmp_path):
    """argparse takes --db=X as well as --db X. Every guard reads the item after
    a flag, so the equals form used to walk past all three: a warehouse scan with
    no budget, and the reserved questions with no confirmation."""
    from context_lift.loop import is_paid, split_flags, touches_holdout, validate_launch
    assert split_flags(["--db=bq://p/d", "--golds", "g.csv"]) == ["--db", "bq://p/d", "--golds", "g.csv"]
    assert is_paid("gate", ["--db=bq://p/d", "--golds", "g.csv"]) is True
    assert is_paid("score", ["--db=bq://p/d", "--predictions", "p.csv"]) is True
    ws = build_workspace(tmp_path)
    index = json.loads((ws / "index.json").read_text())
    split = [r for r in index["runs"] if r["command"] == "split"][0]
    holdout = f"runs/{split['run_id']}/{split['outputs']['out_holdout_csv'][0]}"
    assert touches_holdout(ws, "score", ["--predictions", holdout]) is True
    assert touches_holdout(ws, "score", [f"--predictions={holdout}"]) is True
    assert validate_launch(ws, "score", ["--save-report=/etc/passwd"]) is not None


def test_every_state_is_named_and_the_verdict_carries_no_command(tmp_path):
    """The page switches on `state`, so a branch without one is a silent bug.
    The reserved-question argv must stay off `command`: a page that sees a new
    command at the verdict posts a duplicate card into thread.jsonl on disk."""
    from context_lift.loop import STATE_PHASE, STATES, api_next
    assert STATES == frozenset(STATE_PHASE)
    ws = build_workspace(tmp_path)
    (ws / "context").mkdir()
    (ws / "context" / "rules.md").write_text((tmp_path / "rules.md").read_text())
    index = json.loads((ws / "index.json").read_text())
    split = [r for r in index["runs"] if r["command"] == "split"][0]
    holdout_csv = str(ws / "runs" / split["run_id"] / split["outputs"]["out_holdout_csv"][0])
    assert main(["--workspace", str(ws), "leak", "--context", str(ws / "context" / "rules.md"),
                 "--predictions", holdout_csv]) == 0
    nxt = api_next(ws)
    assert nxt["state"] == "verdict_holdout_ready"
    assert nxt["done"] is True                    # _proposal and _holdout_proposal both read it
    assert nxt["command"] == []                   # the holdout argv lives only in also[0].argvs
    assert nxt["action"]["kind"] == "human" and "rules" in nxt["action"]["label"]
    holdout = next(o for o in nxt["also"] if o["kind"] == "holdout")
    assert holdout["touches_holdout"] is True and holdout["confirm"] == ["name"]
    # these arms were imported, so `next` cannot write the command that made them
    assert holdout["runnable"] is False and "imported, not generated" in holdout["label_note"]
    assert nxt["loop"]["rail"] == "round 1 · step 5 of 5 · compare"
    assert nxt["loop"]["rounds_closed"] == 1


def test_next_walks_the_loop_from_an_empty_workspace(tmp_path):
    from context_lift.loop import api_next
    ws = tmp_path / "ws"
    ws.mkdir()

    first = api_next(ws)
    assert first["step"] == "answer key"           # no questions file: mine one or write one
    assert first["runnable"] is False              # <query_log.csv> is a placeholder
    assert first["command"][0] == "mine"
    assert [s["name"] for s in first["strip"]][:2] == ["answer key", "gate"]

    built = build_workspace(tmp_path)
    after = api_next(built)
    assert after["step"] == "rules"                # baseline scored; the human step
    assert after["human"] is True
    assert after["command"][:2] == ["triage", "--report"]


def _scored_baseline(root, pred_sqls):
    """A gated, split workspace under `root` with one imported and scored
    baseline whose predictions are `pred_sqls`, in question order."""
    root.mkdir()
    db = _seed_db(root)
    ws = root / "ws"
    W = ["--workspace", str(ws)]
    questions = _write_csv(root / "q.csv", ["id", "question", "gold_sql"],
                           [(f"q{i}", f"question {i}", "SELECT name FROM emp") for i in range(6)])
    assert main(W + ["gate", "--db", db, "--golds", questions]) == 0
    assert main(W + ["split", "--golds", questions, "--holdout-fraction", "0.34"]) == 0
    preds = _write_csv(root / "base.csv", ["id", "question", "gold_sql", "pred_sql"],
                       [(f"q{i}", f"question {i}", "SELECT name FROM emp", sql)
                        for i, sql in enumerate(pred_sqls)])
    assert main(W + ["import", "--predictions", preds]) == 0
    assert main(W + ["score", "--db", db, "--predictions", preds]) == 0
    return ws


def test_a_baseline_at_zero_is_diagnosed_before_the_rules_step(tmp_path):
    """Field: rules and catalog arms were paid for over a baseline that scored
    0 of 18 in every repetition. The golds were dashboard SQL and six answers
    hit the --max-bytes ceiling; no context could have matched them, and the
    printed hint ("usually a setup fault") did not apply."""
    from context_lift.loop import api_next

    did_not_run = api_next(_scored_baseline(tmp_path / "ceiling", ["SELECT 1 FROM nope"] * 6))
    wrong_shape = api_next(_scored_baseline(tmp_path / "shape",
                                            ["SELECT name FROM emp WHERE age > 35"] * 6))
    one_right = api_next(_scored_baseline(tmp_path / "one",
                                          ["SELECT age FROM emp"] * 5 + ["SELECT name FROM emp"]))

    for nxt in (did_not_run, wrong_shape):
        assert nxt["state"] == "baseline_zero" and nxt["human"] is True
        assert nxt["command"][:2] == ["triage", "--report"] and nxt["command"][-1] == "--show"
        assert nxt["command"][2].startswith("runs/") and nxt["runnable"] is True
        assert nxt["action"]["label"] == "Diagnose the zero baseline before writing rules"
        assert nxt["why"].endswith("0 correct in every repetition is not a baseline to climb from")
        assert nxt["loop"]["rail"] == "round 1 · step 1 of 5 · write the rules"
    assert did_not_run["why"].startswith("6 of 6 did not run; if the cause is the --max-bytes ceiling")
    assert wrong_shape["why"].startswith("6 of 6 ROW_COUNT (returned a different number of rows)")
    assert "--extra-columns tolerate" in wrong_shape["why"] and "fix the golds" in wrong_shape["why"]
    assert one_right["state"] == "need_triage" and one_right["step"] == "rules"


def test_the_zero_baseline_step_lets_go_once_its_own_triage_has_run(tmp_path):
    """Field: the step proposed a triage, the triage ran, and `next` printed
    the same step again for ever. It never looked at the runs, so a zero
    baseline was a dead end and no context arm was ever proposed."""
    from context_lift.loop import api_next
    ws = _scored_baseline(tmp_path / "zero", ["SELECT 1 FROM nope"] * 6)
    stuck = api_next(ws)
    assert stuck["state"] == "baseline_zero"

    assert _run_in(ws, stuck["command"]) == 0            # the step's own advice

    nxt = api_next(ws)
    assert nxt["state"] == "need_rules" and nxt["step"] == "rules"
    assert nxt["command"][0] == "draft-rules"
    # the finding does not disappear with the step: it rides along as a blocker
    assert nxt["blockers"] and nxt["blockers"][0].startswith("the baseline scored 0 in every "
                                                            "repetition: ")
    assert nxt["blockers"][0].endswith("is not a baseline to climb from")


def test_authorize_is_the_one_place_the_three_launch_rules_live(tmp_path):
    """An argv off the allowlist or outside the workspace, a paid command with
    no budget or no confirmation, and anything reading the reserved questions
    without the workspace name typed."""
    from context_lift.loop import api_next, authorize
    ws = build_workspace(tmp_path)
    ok = lambda cmds, **kw: authorize(ws, cmds, kw.get("budget"), kw.get("confirmed", False),
                                      kw.get("unseal"))
    assert "not one of the commands" in ok([["rm", "-rf", "/"]])
    assert "inside the workspace" in ok([["gate", "--db", "x.sqlite", "--clean-out", "/tmp/out.csv"]])
    nxt = api_next(ws)
    assert ok([nxt["commands"][0], ["rm", "-rf", "/"]])          # every argv is checked, not just the first
    paid = [["generate", "--questions", "dataset/curate.csv", "--out", "arms/x.csv"]]
    assert "needs a budget" in ok(paid)
    assert "explicit confirmation" in ok(paid, budget=1.0)
    assert ok(paid, budget=1.0, confirmed=True) is None
    index = json.loads((ws / "index.json").read_text())
    split = [r for r in index["runs"] if r["command"] == "split"][0]
    holdout = f"runs/{split['run_id']}/{split['outputs']['out_holdout_csv'][0]}"
    sealed = [["score", "--db", "x.sqlite", "--predictions", holdout]]
    assert "type the workspace name" in ok(sealed)
    assert "type the workspace name" in ok(sealed, unseal="wrong")
    assert ok(sealed, unseal=ws.resolve().name) is None


def test_the_answer_key_fills_question_text_from_scored_files_when_gate_saw_none(tmp_path):
    from context_lift.loop import api_answer_key
    db = _seed_db(tmp_path)
    ws = tmp_path / "ws"
    W = ["--workspace", str(ws)]
    bare_golds = _write_csv(tmp_path / "golds.csv", ["id", "gold_sql"],
                            [("q1", "SELECT name FROM emp")])
    preds = _write_csv(tmp_path / "p.csv", ["id", "question", "gold_sql", "pred_sql"],
                       [("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp")])
    assert main(W + ["gate", "--db", db, "--golds", bare_golds]) == 0
    assert main(W + ["score", "--db", db, "--predictions", preds]) == 0

    key = api_answer_key(ws)

    assert key["rows"] == [{"id": "q1", "question": "names?", "gold_sql": "SELECT name FROM emp",
                            "defects": [], "detail": "", "meaning": [], "status": "not split yet"}]


def test_next_scores_every_repetition_before_moving_on_and_discovers_dataset_files(tmp_path):
    from context_lift.loop import api_next, discover
    db = _seed_db(tmp_path)
    ws = tmp_path / "ws"
    (ws / "dataset").mkdir(parents=True)
    questions = _write_csv(ws / "dataset" / "questions.csv", ["id", "question", "gold_sql"],
                           [("q1", "names?", "SELECT name FROM emp")])
    (ws / "dataset" / "schema.txt").write_text("CREATE TABLE emp(name TEXT, age INT)")
    import shutil
    shutil.copyfile(db, ws / "emp.sqlite")

    assert discover(ws) == {"questions": "dataset/questions.csv",
                            "schema": "dataset/schema.txt", "db": "emp.sqlite",
                            "datasets": None, "catalog": None, "catalog_project": None,
                            "catalog_location": None, "catalog_group": None}
    first = api_next(ws)
    assert first["step"] == "gate" and first["runnable"] is True
    W = ["--workspace", str(ws)]
    db_path = str(ws / "emp.sqlite")            # the page runs from the workspace; pytest does not
    assert main(W + ["gate", "--db", db_path, "--golds", questions]) == 0
    assert main(W + ["split", "--golds", questions]) == 0
    assert api_next(ws)["step"] == "baseline"
    assert main(W + ["generate", "--questions", questions, "--schema", str(ws / "dataset/schema.txt"),
                     "--out", str(ws / "arms/base.csv"), "--backend", "echo", "--reps", "2"]) == 0

    step = api_next(ws)
    assert step["step"] == "score the 2 runs" and step["command"][-3].endswith("base_rep1.csv")
    assert [c[-3][-13:] for c in step["commands"]] == ["base_rep1.csv", "base_rep2.csv"]  # one click scores both
    assert main(W + ["score", "--db", db_path, "--predictions", str(ws / "arms/base_rep1.csv")]) == 0
    step = api_next(ws)
    assert step["step"] == "score" and step["command"][-3].endswith("base_rep2.csv")  # rep 2 still unscored
    assert step["commands"] == [step["command"]]
    assert main(W + ["score", "--db", db_path, "--predictions", str(ws / "arms/base_rep2.csv")]) == 0
    assert api_next(ws)["step"] == "rules"


def test_lineage_prefers_the_producer_that_ran_before_the_consumer(tmp_path):
    """Temperature-0 repetitions can be byte-identical, so two generate runs
    share an output hash; a score run links to the one that already existed."""
    ws = tmp_path / "ws"
    runs = ws / "runs"
    for run_id, command, started, inputs, outputs in [
        ("20260101-000001-generate", "generate", "2026-01-01T00:00:01.000", {},
         {"out": [{"path": "a.csv", "sha256": "same", "copy": None}]}),
        ("20260101-000002-score", "score", "2026-01-01T00:00:02.000",
         {"predictions": {"path": "a.csv", "sha256": "same", "copy": None}}, {}),
        ("20260101-000003-generate", "generate", "2026-01-01T00:00:03.000", {},
         {"out": [{"path": "b.csv", "sha256": "same", "copy": None}]}),
    ]:
        (runs / run_id).mkdir(parents=True)
        (runs / run_id / "manifest.json").write_text(json.dumps({
            "run_id": run_id, "command": command, "started_at": started,
            "ended_at": started, "exit_code": 0, "inputs": inputs, "outputs": outputs,
            "arm": {"arm_id": "x", "context_sha256": None, "examples_sha256": None,
                    "label": "no context"} if command == "generate" else None}))

    from context_lift.workspace import rebuild_index
    index = rebuild_index(ws)

    score = next(r for r in index["runs"] if r["command"] == "score")
    assert score["produced_by"] == {"predictions": "20260101-000001-generate"}


FLAGGED_GOLD = "SELECT name FROM emp GROUP BY name ORDER BY sum(name) DESC"   # sums text: gate flags it
GOLD = {"q1": "SELECT name FROM emp", "q2": "SELECT age FROM emp", "q3": "SELECT COUNT(*) FROM emp",
        "q4": FLAGGED_GOLD}


def _sealed(ws):
    index = json.loads((ws / "index.json").read_text())
    split = [r for r in index["runs"] if r["command"] == "split"][0]
    return set((ws / "runs" / split["run_id"] / split["outputs"]["out_holdout"][0]).read_text().split())


def test_rounds_lists_the_baseline_and_one_round_with_the_numbers_collect_gives(tmp_path):
    from context_lift.bundle import collect
    from context_lift.loop import api_rounds
    ws = build_workspace(tmp_path)
    index = json.loads((ws / "index.json").read_text())

    ledger = api_rounds(ws)

    verdict = collect(ws, index["pins"]["baseline"], index["pins"]["candidate"])
    assert ledger["baseline"] == {"arm_id": index["pins"]["baseline"], "label": "without rules",
                                  "rate": verdict["ablation"]["without_context"]["point"], "runs": 1}
    assert len(ledger["rounds"]) == 1
    only = ledger["rounds"][0]
    assert only["n"] == 1 and only["arm_id"] == index["pins"]["candidate"]
    assert only["label"] == "docs v1"                       # the "# heading" of the rules file
    assert only["rate"] == verdict["ablation"]["with_context"]["point"]
    assert only["lift_vs_baseline"] == verdict["ablation"]["lift"]
    assert (only["fixed"], only["broke"], only["unstable"]) == (1, 1, 0)
    assert only["word"] == "UNDERPOWERED" and only["p_text"] == verdict["p_text"]["defended"]
    assert only["lift_vs_previous"] is None                 # round 1 has no round before it
    assert only["ladder_rate"] == ledger["baseline"]["rate"]  # a 0-point gain never moves the ladder
    assert ledger["ladder_step"] == 0.5                     # 1/sqrt(4 questions)
    body = _get(ws, "/api/rounds")
    assert body == ledger


def test_the_index_pins_by_itself_and_explicit_pins_win(tmp_path):
    from context_lift.workspace import rebuild_index
    ws = build_workspace(tmp_path)
    explicit = json.loads((ws / "pins.json").read_text())
    (ws / "pins.json").unlink()

    auto = rebuild_index(ws)["pins"]

    assert auto == {**explicit, "explicit": {"baseline": False, "candidate": False}}
    arms = {a["arm_id"]: a for a in rebuild_index(ws)["arms"]}
    assert arms[auto["baseline"]]["context_sha256"] is None
    # a newer rules version, fully scored, becomes the candidate; an unscored one does not
    db = str(tmp_path / "t.db")
    W = ["--workspace", str(ws)]
    cols = ["id", "question", "gold_sql", "pred_sql"]
    rows = [(i, "q", GOLD[i], GOLD[i]) for i in ("q1", "q2", "q3", "q4")]
    v2 = _write_csv(tmp_path / "v2.csv", cols, rows)
    (tmp_path / "rules2.md").write_text("# docs v2\n")
    assert main(W + ["import", "--predictions", v2, "--context", str(tmp_path / "rules2.md")]) == 0
    assert main(W + ["score", "--db", db, "--predictions", v2]) == 0
    v3 = _write_csv(tmp_path / "v3.csv", cols, rows[:3])
    (tmp_path / "rules3.md").write_text("# docs v3\n")
    assert main(W + ["import", "--predictions", v3, "--context", str(tmp_path / "rules3.md")]) == 0
    index = rebuild_index(ws)
    v2_arm = next(a for a in index["arms"] if a["label"].startswith("rules2"))
    assert index["pins"]["candidate"] == v2_arm["arm_id"]
    assert index["pins"]["baseline"] == explicit["baseline"]

    (ws / "pins.json").write_text(json.dumps({"baseline": None, "candidate": explicit["candidate"]}))

    pinned = rebuild_index(ws)["pins"]
    assert pinned["candidate"] == explicit["candidate"]
    assert pinned["explicit"] == {"baseline": False, "candidate": True}
    assert pinned["baseline"] == explicit["baseline"]       # still filled in by the index


def test_next_has_no_pin_step_and_goes_from_score_context_to_compare(tmp_path):
    from context_lift.loop import api_next
    ws = build_workspace(tmp_path)
    (ws / "context").mkdir()
    (ws / "context" / "rules.md").write_text((tmp_path / "rules.md").read_text())   # the scored version
    index = json.loads((ws / "index.json").read_text())
    split = [r for r in index["runs"] if r["command"] == "split"][0]
    holdout_csv = str(ws / "runs" / split["run_id"] / split["outputs"]["out_holdout_csv"][0])
    assert main(["--workspace", str(ws), "leak", "--context", str(ws / "context" / "rules.md"),
                 "--predictions", holdout_csv]) == 0

    nxt = api_next(ws)

    names = [s["name"] for s in nxt["strip"]]
    assert "pin" not in names
    assert names[-2:] == ["score context", "compare"]
    assert nxt["step"] == "compare" and nxt["done"] is True
    assert nxt["state"] == "verdict_holdout_ready"
    assert [s["state"] for s in nxt["strip"]][-2:] == ["done", "next"]


def _run_of(ws, command, last=True):
    runs = [r for r in json.loads((ws / "index.json").read_text())["runs"] if r["command"] == command]
    return (runs[-1] if last else runs[0])["run_id"]


def test_one_step_can_carry_several_commands_and_each_one_is_checked(tmp_path):
    """A version with three runs to score is one step: api_next returns every
    argv, and authorize refuses the whole set if any single argv is bad."""
    from context_lift.loop import api_next, authorize
    ws = build_workspace(tmp_path)
    nxt = api_next(ws)
    assert isinstance(nxt["commands"], list) and nxt["commands"]
    assert nxt["command"] == nxt["commands"][0]          # the first, for callers that run one
    assert authorize(ws, nxt["commands"] + [["rm", "-rf", "/"]], None, False, None)


def test_step_zero_names_where_the_answer_key_came_from(tmp_path):
    from context_lift.loop import api_next
    ws = build_workspace(tmp_path)

    nxt = api_next(ws)

    assert nxt["answer_key"] == f"{tmp_path / 'q.csv'}, 4 questions, provided"
    assert nxt["strip"][0] == {"name": "answer key", "state": "done", "detail": nxt["answer_key"]}
    assert "detail" not in nxt["strip"][1]
    fresh = tmp_path / "fresh"
    (fresh / "dataset").mkdir(parents=True)
    _write_csv(fresh / "dataset" / "questions.csv", ["id", "question", "gold_sql"],
               [("q1", "names?", "SELECT name FROM emp"), ("q2", "ages?", "SELECT age FROM emp")])
    assert api_next(fresh)["answer_key"] == "dataset/questions.csv, 2 questions, provided"
    mined = tmp_path / "mined"
    (mined / "dataset").mkdir(parents=True)
    log = _write_csv(tmp_path / "log.csv", ["sql"], [("SELECT name FROM emp",), ("SELECT age FROM emp",)])
    assert main(["--workspace", str(mined), "mine", "--db", str(tmp_path / "t.db"),
                 "--all-datasets", "--queries", log,
                 "--out", str(mined / "dataset" / "candidates.csv")]) == 0
    assert api_next(mined)["answer_key"] == f"mined from {log} (2 candidates)"


def test_a_bigquery_pointer_file_names_the_database_and_makes_sql_steps_paid(tmp_path):
    from context_lift.loop import api_next, discover, is_paid, validate_launch
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "bigquery.txt").write_text("bq://billing/bigquery-public-data.thelook_ecommerce\n")
    _write_csv(ws / "questions.csv", ["id", "question", "gold_sql"],
               [("q1", "n?", "SELECT COUNT(*) AS n FROM orders")])

    found = discover(ws)
    nxt = api_next(ws)

    assert found["db"] == "bq://billing/bigquery-public-data.thelook_ecommerce"
    assert nxt["step"] == "gate"
    assert nxt["command"][:3] == ["gate", "--db", "bq://billing/bigquery-public-data.thelook_ecommerce"]
    assert nxt["paid"] is True                              # a scan is billed: cost sheet first
    assert nxt["runnable"] is True                          # a bq:// spec is not a placeholder
    assert nxt["action"]["cost_note"].startswith("costs money: add --budget USD")   # gate takes both flags
    assert validate_launch(ws, "gate", nxt["command"][1:]) is None
    assert is_paid("score", ["--db", "bq://p", "--predictions", "a.csv"]) is True
    assert is_paid("score", ["--db", "bq://p", "--predictions", "a.csv", "--estimate-only"]) is False
    assert is_paid("score", ["--db", "local.db", "--predictions", "a.csv"]) is False
    assert is_paid("leak", ["--db", "bq://p"]) is False    # leak runs no SQL


def test_the_cost_note_names_only_the_flags_the_command_has():
    """Field: `next` told the reader to price `schema` with --estimate-only and
    cap it with --budget; schema takes neither, and argparse answered
    "unrecognized arguments: --estimate-only"."""
    from context_lift.loop import cost_note
    both = "costs money: add --budget USD, and price it first with --estimate-only"
    assert cost_note("gate", ["--db", "bq://p", "--golds", "q.csv"]) == both
    assert cost_note("mine", ["--db", "bq://p", "--queries", "log.csv"]) == both
    assert cost_note("log", ["--db", "bq://p", "--days", "3"]) == "costs money: price it first with --estimate-only"
    for command in ("schema", "profile", "draft-context"):
        note = cost_note(command, ["--db", "bq://p/d", "--out", "x"])
        assert note.startswith("costs money: a few metadata queries per dataset"), command
        assert "no --estimate-only" in note and "--budget" not in note
    assert cost_note("schema", ["--db", "local.db", "--out", "x"]) == ""      # free: no note
    assert cost_note("gate", ["--db", "bq://p", "--golds", "q.csv", "--estimate-only"]) == ""


def _dataset_workspace(tmp_path):
    """A workspace with the files `discover` looks for, gated and split, so
    the next step is the baseline generate. Returns (ws, db path)."""
    db = _seed_db(tmp_path)
    ws = tmp_path / "ws"
    (ws / "dataset").mkdir(parents=True)
    questions = _write_csv(ws / "dataset" / "questions.csv", ["id", "question", "gold_sql"],
                           [(i, "q", GOLD[i]) for i in ("q1", "q2", "q3", "q4")])
    (ws / "dataset" / "schema.txt").write_text("CREATE TABLE emp(name TEXT, age INT)")
    import shutil
    shutil.copyfile(db, ws / "emp.sqlite")
    W = ["--workspace", str(ws)]
    assert main(W + ["gate", "--db", str(ws / "emp.sqlite"), "--golds", questions]) == 1   # q4 flagged
    assert main(W + ["split", "--golds", questions, "--holdout-fraction", "0.34"]) == 0
    return ws, str(ws / "emp.sqlite")


def _run_in(ws, argv):
    """Run a proposed argv the way the launcher does: paths relative to the workspace."""
    resolved = [str(ws / a) if not a.startswith("-") and (ws / a).exists() else a for a in argv]
    return main(["--workspace", str(ws)] + resolved)


def test_next_reaches_compare_on_a_workspace_whose_context_was_retrieved(tmp_path):
    """No rules file on disk, only an arm made with --context-per-question:
    the loop used to stop at the rules step for good, because the retrieval
    arm looked like a second baseline."""
    from context_lift.loop import api_next, api_rounds
    ws, db = _dataset_workspace(tmp_path)
    W = ["--workspace", str(ws)]
    questions, schema = str(ws / "dataset/questions.csv"), str(ws / "dataset/schema.txt")
    assert main(W + ["generate", "--questions", questions, "--schema", schema, "--out", str(ws / "arms/base.csv"),
                     "--backend", "echo", "--echo-response", "SELECT age FROM emp"]) == 0
    assert main(W + ["score", "--db", db, "--predictions", str(ws / "arms/base.csv")]) == 0
    assert api_next(ws)["step"] == "rules"
    retrieved = ws / "dataset" / "retrieved.json"
    retrieved.write_text(json.dumps({"q1": "emp.name holds the person's name", "q2": "emp.age is years"}))
    assert main(W + ["generate", "--questions", questions, "--schema", schema, "--out", str(ws / "arms/ctx.csv"),
                     "--backend", "echo", "--echo-response", "SELECT name FROM emp",
                     "--context-per-question", str(retrieved)]) == 0

    leak_step = api_next(ws)

    assert leak_step["step"] == "leak" and leak_step["command"][:2] == ["leak", "--context-per-question"]
    assert leak_step["command"][2].startswith("runs/") and leak_step["runnable"] is True
    assert _run_in(ws, leak_step["command"]) == 0
    score_step = api_next(ws)
    assert score_step["step"] == "score" and score_step["state"] == "score_context"
    assert score_step["command"][-3].endswith("ctx.csv")
    assert _run_in(ws, score_step["command"]) == 0

    nxt = api_next(ws)

    assert nxt["step"] == "compare" and nxt["state"] == "verdict_holdout_ready"
    assert nxt["loop"]["rounds_closed"] == 1
    # a retrieval candidate retrieves again over the reserved questions: the
    # twin routes, is leak-checked, and answers, beside the baseline's twin
    card = next(o for o in nxt["also"] if o["kind"] == "holdout")
    assert [argv[0] for argv in card["argvs"]] == ["generate", "catalog-route", "leak", "generate"]
    routed = card["argvs"][1]
    assert routed[routed.index("--context-out") + 1] == "context/holdout_per_question.json"
    assert card["argvs"][2][:2] == ["leak", "--context-per-question"]
    assert card["argvs"][3][card["argvs"][3].index("--context-per-question") + 1] == \
        "context/holdout_per_question.json"
    # no catalog: line in bigquery.txt, so the offer says it is not ready
    assert card["runnable"] is False and card["label_note"] == "fill in the <placeholders> first"
    index = json.loads((ws / "index.json").read_text())
    candidate = next(a for a in index["arms"] if a["arm_id"] == index["pins"]["candidate"])
    assert candidate["retrieved_sha256"] and candidate["context_sha256"] is None
    ledger = api_rounds(ws)
    assert [r["label"] for r in ledger["rounds"]] == [f"retrieved {candidate['retrieved_sha256'][:12]}"]


def test_next_generate_argv_carries_the_bigquery_dialect_and_the_project(tmp_path, monkeypatch):
    """The SDK reads GOOGLE_CLOUD_PROJECT; contextlift reads CONTEXTLIFT_PROJECT. A
    proposal without --project failed at the first call, and one without
    --dialect BigQuery asked for SQLite syntax on a warehouse."""
    from context_lift.loop import api_next
    ws, _ = _dataset_workspace(tmp_path)
    # the same workspace, had the audit run on the warehouse: only the db record differs
    gate_dir = next(p for p in (ws / "runs").iterdir() if "gate" in p.name)
    manifest = json.loads((gate_dir / "manifest.json").read_text())
    manifest["inputs"]["db"] = {"path": "bq://billing/shop", "sha256": None, "kind": "bigquery"}
    (gate_dir / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.delenv("CONTEXTLIFT_PROJECT", raising=False)

    without_project = api_next(ws)

    assert without_project["step"] == "baseline" and without_project["command"][0] == "generate"
    argv = without_project["command"]
    assert argv[argv.index("--dialect") + 1] == "BigQuery"
    assert "--project" not in argv
    monkeypatch.setenv("CONTEXTLIFT_PROJECT", "billing")
    argv = api_next(ws)["command"]
    assert argv[argv.index("--project") + 1] == "billing" and "--dialect" in argv
    assert api_next(ws)["paid"] is True


def test_the_retrieval_path_can_be_launched_and_its_warehouse_scans_are_paid():
    from context_lift.loop import ALLOWED_COMMANDS, is_paid
    for command in ("draft-context", "catalog-pull", "catalog-route", "score-routing",
                    "routing-gate", "review", "draft-rules", "profile", "log"):
        assert command in ALLOWED_COMMANDS, command
    assert is_paid("draft-context", ["--db", "bq://p/d", "--out", "docs"]) is True
    assert is_paid("draft-context", ["--db", "warehouse.db", "--out", "docs"]) is False
    assert is_paid("log", ["--db", "bq://p"]) is True
    assert is_paid("catalog-route", ["--project", "p", "--location", "us"]) is False


def _round_one_workspace(tmp_path, ids=("q1", "q2", "q3", "q4", "q5", "q6"), fraction="0.34"):
    """A gated and split workspace whose baseline and rules arm were both
    generated (echo backend) over curate.csv and scored: the compare step of
    round 1. Half the questions ask for names and half for ages, so each arm
    is right about some of them. Returns (ws, db)."""
    from context_lift.loop import api_next
    import shutil
    db = _seed_db(tmp_path)
    ws = tmp_path / "ws"
    (ws / "dataset").mkdir(parents=True)
    questions = _write_csv(ws / "dataset" / "questions.csv", ["id", "question", "gold_sql"],
                           [(name, f"question {name}",
                             "SELECT name FROM emp" if i % 2 else "SELECT age FROM emp")
                            for i, name in enumerate(ids)])
    schema = str(ws / "dataset" / "schema.txt")
    (ws / "dataset" / "schema.txt").write_text("CREATE TABLE emp(name TEXT, age INT)")
    shutil.copyfile(db, ws / "emp.sqlite")
    db = str(ws / "emp.sqlite")
    W = ["--workspace", str(ws)]
    assert main(W + ["gate", "--db", db, "--golds", questions]) == 0
    assert main(W + ["split", "--golds", questions, "--holdout-fraction", fraction]) == 0
    index = json.loads((ws / "index.json").read_text())
    split = [r for r in index["runs"] if r["command"] == "split"][0]
    curate = str(ws / "runs" / split["run_id"] / split["outputs"]["out_curate_csv"][0])
    assert main(W + ["generate", "--questions", curate, "--schema", schema,
                     "--out", str(ws / "arms/base.csv"), "--backend", "echo",
                     "--echo-response", "SELECT age FROM emp"]) == 0
    assert main(W + ["score", "--db", db, "--predictions", str(ws / "arms/base.csv")]) == 0
    (ws / "context").mkdir(exist_ok=True)
    (ws / "context" / "rules.md").write_text('# rules v1\n- "staff" -> emp.name  # from q1\n')
    if api_next(ws)["state"] == "baseline_zero":       # the split left no age question
        assert _run_in(ws, api_next(ws)["command"]) == 0
    leak = api_next(ws)
    assert leak["state"] == "leak_unchecked" and _run_in(ws, leak["command"]) == 0
    assert main(W + ["generate", "--questions", curate, "--schema", schema,
                     "--out", str(ws / "arms/ctx.csv"), "--backend", "echo",
                     "--echo-response", "SELECT name FROM emp",
                     "--context", str(ws / "context" / "rules.md")]) == 0
    assert main(W + ["score", "--db", db, "--predictions", str(ws / "arms/ctx.csv")]) == 0
    return ws, db


def _csv_rows(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def _run_from_workspace(ws, argv):
    """Run a proposed argv the way start_job does: from the workspace, so a
    relative output path lands inside it."""
    import os
    here = os.getcwd()
    os.chdir(ws)
    try:
        return main(["--workspace", str(ws)] + list(argv))
    finally:
        os.chdir(here)


def test_a_retrieval_arm_beside_a_rules_file_is_scored_before_the_compare(tmp_path):
    """Field: after `generate --context-per-question` ran, `next` jumped to
    "round 1 step 5 of 5 compare" over the rules reports. The catalog arm sat
    unscored and no step ever named it, because the rules file on disk decided
    which arm the loop was about."""
    from context_lift.loop import api_next
    ws, db = _round_one_workspace(tmp_path)
    W = ["--workspace", str(ws)]
    index = json.loads((ws / "index.json").read_text())
    split = [r for r in index["runs"] if r["command"] == "split"][0]
    curate = str(ws / "runs" / split["run_id"] / split["outputs"]["out_curate_csv"][0])
    retrieved = ws / "dataset" / "retrieved.json"
    retrieved.write_text(json.dumps({row["id"]: "emp.name holds the person's name"
                                     for row in _csv_rows(curate)}))
    assert main(W + ["generate", "--questions", curate,
                     "--schema", str(ws / "dataset" / "schema.txt"),
                     "--out", str(ws / "arms/catalog.csv"), "--backend", "echo",
                     "--echo-response", "SELECT name FROM emp",
                     "--context-per-question", str(retrieved)]) == 0

    nxt = api_next(ws)

    assert nxt["state"] == "score_context" and nxt["step"] == "score"
    assert nxt["command"][nxt["command"].index("--predictions") + 1].endswith("catalog.csv")
    assert "the version with the retrieved context" in nxt["why"]
    assert _run_in(ws, nxt["command"]) == 0
    after = api_next(ws)
    assert after["step"] == "compare" and after["state"] == "verdict_holdout_ready"


def test_the_compare_step_prints_the_training_reading_as_two_commands(tmp_path):
    """Field: the step said "`contextlift ablate --ids curate_ids.txt` prints the
    training verdict and `contextlift attribute` says which rule did it" as prose
    with no paths in it, while the reserved reading was printed in full."""
    from context_lift.loop import api_next
    ws, db = _round_one_workspace(tmp_path)

    card = next(o for o in api_next(ws)["also"] if o["kind"] == "training")

    reading, blame = card["argvs"]
    assert card["runnable"] is True and card["paid"] is False and card["confirm"] == []
    assert card["touches_holdout"] is False
    assert reading[0] == "ablate" and reading[-1] == "--show"
    assert reading[reading.index("--ids") + 1].endswith("outputs/curate_ids.txt")
    assert reading[reading.index("--without-report") + 1].startswith("runs/")
    assert reading[reading.index("--with-report") + 1].endswith("report.json")
    assert blame[0] == "attribute" and blame[blame.index("--context") + 1].startswith("runs/")
    assert blame[blame.index("--without-context") + 1].endswith(".csv")
    assert blame[blame.index("--with-report") + 1].endswith("report.json")
    assert _run_from_workspace(ws, reading) == 0        # both run as printed
    assert _run_from_workspace(ws, blame) == 0


def _gated_and_split(tmp_path):
    """A workspace with the questions gated and split on a SQLite copy inside
    it, and no schema yet, so the next step is the schema read. Returns (ws, db)."""
    import shutil
    db = _seed_db(tmp_path)
    ws = tmp_path / "ws"
    (ws / "dataset").mkdir(parents=True)
    questions = _write_csv(ws / "dataset" / "questions.csv", ["id", "question", "gold_sql"],
                           [(i, "q", GOLD[i]) for i in ("q1", "q2", "q3", "q4")])
    shutil.copyfile(db, ws / "emp.sqlite")
    W = ["--workspace", str(ws)]
    assert main(W + ["gate", "--db", str(ws / "emp.sqlite"), "--golds", questions]) == 1   # q4 flagged
    assert main(W + ["split", "--golds", questions, "--holdout-fraction", "0.34"]) == 0
    return ws, str(ws / "emp.sqlite")


def test_bigquery_txt_datasets_make_next_propose_a_multi_dataset_schema(tmp_path):
    """Six datasets meant six schema runs and a hand-made file on one estate;
    a `datasets:` line under the spec makes the proposal one command."""
    from context_lift.loop import api_next, discover
    ws, _ = _gated_and_split(tmp_path)
    gate_dir = next(p for p in (ws / "runs").iterdir() if "gate" in p.name)
    manifest = json.loads((gate_dir / "manifest.json").read_text())
    manifest["inputs"]["db"] = {"path": "bq://billing/shop", "sha256": None, "kind": "bigquery"}
    (gate_dir / "manifest.json").write_text(json.dumps(manifest))
    (ws / "bigquery.txt").write_text("bq://billing/shop\n# the products in scope\n"
                                     "datasets: shop, data-proj.ops\n")

    found = discover(ws)
    nxt = api_next(ws)

    assert found["datasets"] == ["shop", "data-proj.ops"] and found["catalog"] is None
    assert nxt["state"] == "need_schema"
    assert nxt["command"] == ["schema", "--db", "bq://billing/shop", "--out", "dataset/schema.txt",
                              "--datasets", "shop,data-proj.ops"]
    assert nxt["paid"] is True and nxt["runnable"] is True


def test_a_catalog_file_makes_next_propose_tables_file_and_offer_the_catalog_arm(tmp_path):
    from context_lift.loop import api_next, discover
    ws, db = _gated_and_split(tmp_path)
    (ws / "catalog.txt").write_text("# contextlift catalog-list\nmain.emp\n")
    (ws / "bigquery.txt").write_text("bq://billing/shop\ncatalog: cat-proj us\n")

    found = discover(ws)
    schema_step = api_next(ws)

    assert (found["catalog"], found["catalog_project"], found["catalog_location"],
            found["catalog_group"]) == ("catalog.txt", "cat-proj", "us", None)
    assert schema_step["state"] == "need_schema"
    assert schema_step["command"] == ["schema", "--db", db, "--out", "dataset/schema.txt",
                                      "--tables-file", "catalog.txt"]
    # on to the context-arm state: a baseline answered and scored, rules written and leak-checked
    W = ["--workspace", str(ws)]
    questions = str(ws / "dataset/questions.csv")
    schema = ws / "dataset" / "schema.txt"
    schema.write_text("CREATE TABLE emp(name TEXT, age INT)")
    assert main(W + ["generate", "--questions", questions, "--schema", str(schema),
                     "--out", str(ws / "arms/base.csv"), "--backend", "echo",
                     "--echo-response", "SELECT age FROM emp"]) == 0
    assert main(W + ["score", "--db", db, "--predictions", str(ws / "arms/base.csv")]) == 0
    (ws / "context").mkdir()
    (ws / "context" / "rules.md").write_text('- "staff" -> emp.name\n')
    leak_step = api_next(ws)
    assert leak_step["state"] == "leak_unchecked" and _run_in(ws, leak_step["command"]) == 0

    nxt = api_next(ws)

    assert nxt["state"] == "need_context_arm"
    offer = nxt["also"][0]
    assert offer["kind"] == "catalog_arm" and offer["label"] == "build the catalog arm"
    assert offer["runnable"] is True and offer["paid"] is True       # the generate is a model call
    assert [argv[0] for argv in offer["argvs"]] == ["draft-context", "catalog-pull", "catalog-route", "generate"]
    assert offer["argvs"][0] == ["draft-context", "--db", db, "--tables-file", "catalog.txt",
                                 "--out", "context/pages"]
    # the pull writes its own two directories; it used to overwrite the drafts
    assert offer["argvs"][1] == ["catalog-pull", "--project", "cat-proj", "--location", "us",
                                 "--tables-file", "catalog.txt", "--out", "context/served",
                                 "--pages", "context/docs_served"]
    routed = offer["argvs"][2]
    assert routed[routed.index("--docs") + 1] == "context/docs_served"
    assert routed[routed.index("--catalog") + 1] == "catalog.txt"
    assert routed[routed.index("--context-out") + 1] == "context/catalog_per_question.json"
    generated = offer["argvs"][3]
    assert generated[generated.index("--context-per-question") + 1] == "context/catalog_per_question.json"
    # without the catalog line and the file, the offer stands but says it is not ready
    (ws / "bigquery.txt").write_text("bq://billing/shop\n")
    (ws / "catalog.txt").unlink()
    offer = api_next(ws)["also"][0]
    assert offer["runnable"] is False and offer["label_note"].endswith(
        "fill in the <placeholders> first")
    assert offer["argvs"][0][0] == "catalog-list" and offer["argvs"][0][2] == "<CATALOG_PROJECT>"


def test_the_catalog_offer_writes_the_table_list_first_and_scopes_it(tmp_path):
    """No catalog.txt: the offer used to start with `--tables-file
    <catalog.txt>`, a placeholder nothing in the sequence ever wrote. It now
    lists the tables first, scoped to the datasets, because an unscoped list
    held 44 tables across three datasets on one estate."""
    from context_lift.loop import api_next
    ws, db = _gated_and_split(tmp_path)
    (ws / "bigquery.txt").write_text("bq://billing/shop\ncatalog: cat-proj us group-1\n")
    W = ["--workspace", str(ws)]
    schema = ws / "dataset" / "schema.txt"
    schema.write_text("CREATE TABLE emp(name TEXT, age INT)")
    assert main(W + ["generate", "--questions", str(ws / "dataset/questions.csv"),
                     "--schema", str(schema), "--out", str(ws / "arms/base.csv"),
                     "--backend", "echo", "--echo-response", "SELECT age FROM emp"]) == 0
    assert main(W + ["score", "--db", db, "--predictions", str(ws / "arms/base.csv")]) == 0
    (ws / "context").mkdir()
    (ws / "context" / "rules.md").write_text('- "staff" -> emp.name\n')
    assert _run_in(ws, api_next(ws)["command"]) == 0        # the leak check

    offer = next(o for o in api_next(ws)["also"] if o["kind"] == "catalog_arm")

    assert [argv[0] for argv in offer["argvs"]] == [
        "catalog-list", "draft-context", "catalog-pull", "catalog-route", "generate"]
    assert offer["argvs"][0] == ["catalog-list", "--project", "cat-proj", "--location", "us",
                                 "--entry-group", "group-1", "--datasets", "<DATASETS>",
                                 "--out", "catalog.txt"]
    assert offer["runnable"] is False              # <DATASETS> is the person's decision
    assert offer["label_note"].startswith("name the datasets the list may hold in --datasets")
    assert "44 tables across three datasets" in offer["label_note"]
    # every later command names the file catalog-list is about to write
    assert all("catalog.txt" in argv for argv in offer["argvs"][1:4])
    # the datasets line fills the placeholder in, and only the list is scoped
    (ws / "bigquery.txt").write_text("bq://billing/shop\ndatasets: shop\ncatalog: cat-proj us\n")
    offer = next(o for o in api_next(ws)["also"] if o["kind"] == "catalog_arm")
    assert offer["argvs"][0][-3:] == ["shop", "--out", "catalog.txt"]
    assert offer["runnable"] is True


def test_a_printed_command_is_one_a_shell_runs_unchanged():
    """The cold run's reserved-questions step printed

        generate ... --echo-response SELECT 1; --out arms/holdout_base.csv

    The semicolon ended the shell line, so --out, --reps and --workers were
    dropped and the shell tried to run --out as a program. That is the one
    step the loop calls one-way."""
    argv = ["contextlift", "generate", "--echo-response", "SELECT 1;",
            "--command", "python my_agent.py --mode sql", "--out", "arms/a.csv"]

    line = shell_line(argv)

    assert shlex.split(line) == argv


def test_a_word_that_needs_no_quoting_is_left_alone():
    """A line full of quotes nobody needs is harder to read, and every flag
    and path in the common case is plain."""
    argv = ["contextlift", "--workspace", "/tmp/ws", "score", "--reps", "3"]

    assert shell_line(argv) == "contextlift --workspace /tmp/ws score --reps 3"


def test_next_says_how_many_golds_the_gate_threw_away(tmp_path, capsys):
    """A cold run's gate flagged 112 of 205 and `next` went straight on to
    split. 55% of the answer key left without a word, and the count only
    reappeared on the verdict page, after every dollar was spent."""
    db = _seed_db(tmp_path)
    golds = _write_csv(
        tmp_path / "golds.csv", ["id", "question", "gold_sql"],
        [("ok1", "how many are 30?", "SELECT COUNT(*) FROM emp WHERE age = 30")]
        + [(f"browse{i}", "show me the table", "SELECT * FROM emp") for i in range(4)])
    workspace = str(tmp_path / "ws")

    main(["--workspace", workspace, "gate", "--db", db, "--golds", golds])
    main(["--workspace", workspace, "next"])

    out = capsys.readouterr().out
    assert "gate flagged 4 of 5 golds" in out
    assert "covers only the 1 that are clean" in out
    assert "golds_flagged.csv" in out
    assert "nothing puts them back" in out


def test_the_reserved_reading_refuses_rather_than_crashing_on_uneven_reps(tmp_path, monkeypatch):
    """Pooling needs the same number of repetitions on both sides. The command
    used to be printed anyway, raise at the one step the loop calls one-way,
    blame the question sets rather than the counts, and then `next` printed the
    identical failing command again."""
    from context_lift import loop

    monkeypatch.setattr(loop, "_report_paths",
                        lambda ws, index, runs: [f"r{n}.json" for n in runs])
    monkeypatch.setattr(loop, "reserved_pair", lambda index: (
        ({"label": "no context", "arm_id": "a"}, {"score_runs": ["1", "2", "3"]}),
        ({"label": "rules.md", "arm_id": "b"}, {"score_runs": ["4", "5"]})))
    index = {"pins": {"baseline": "a", "candidate": "b"},
             "arms": [{"arm_id": "a"}, {"arm_id": "b"}]}

    key = {"split": {"holdout": 12, "curate": 30}}

    argv, refusal = loop._holdout_argv(tmp_path, index, key)

    assert "different number of scored repetitions" in refusal
    assert "no context 3" in refusal and "rules.md 2" in refusal
    assert "run the ablate above by hand" in refusal      # the recovery, printed


def test_an_even_number_of_repetitions_is_not_refused(tmp_path, monkeypatch):
    """The guard must not stop the ordinary case."""
    from context_lift import loop

    monkeypatch.setattr(loop, "_report_paths",
                        lambda ws, index, runs: [f"r{n}.json" for n in runs])
    monkeypatch.setattr(loop, "reserved_pair", lambda index: (
        ({"label": "no context", "arm_id": "a"}, {"score_runs": ["1", "2"]}),
        ({"label": "rules.md", "arm_id": "b"}, {"score_runs": ["3", "4"]})))
    monkeypatch.setattr(loop, "validate_launch", lambda ws, cmd, args: None)
    index = {"pins": {"baseline": "a", "candidate": "b"},
             "arms": [{"arm_id": "a"}, {"arm_id": "b"}]}

    key = {"split": {"holdout": 12, "curate": 30}}

    argv, refusal = loop._holdout_argv(tmp_path, index, key)

    assert refusal == ""
    assert argv.count("--without-report") == 2 and argv.count("--with-report") == 2


def test_the_reserved_twin_carries_the_persons_own_runner(tmp_path):
    """`--command` is stored under its argparse dest, `runner_command`. Reading
    options["command"] found nothing every time, so the reserved twin of a
    `--backend command` arm was built with no runner at all, and generate
    exited 2 at the last command of the loop - right after the workspace name
    was typed to unseal. This is the flagship path: the customer's own agent."""
    import shutil
    import sys

    from context_lift.loop import api_next
    runner = tmp_path / "agent.py"
    runner.write_text("import sys; print('SELECT name FROM emp')")
    agent = [f"{sys.executable} {runner}"]

    db = _seed_db(tmp_path)
    ws = tmp_path / "ws"
    (ws / "dataset").mkdir(parents=True)
    questions = _write_csv(ws / "dataset" / "questions.csv", ["id", "question", "gold_sql"],
                           [(f"q{i}", f"question {i}", "SELECT name FROM emp")
                            for i in range(6)])
    shutil.copyfile(db, ws / "emp.sqlite")
    db = str(ws / "emp.sqlite")
    W = ["--workspace", str(ws)]
    assert main(W + ["gate", "--db", db, "--golds", questions]) == 0
    assert main(W + ["split", "--golds", questions, "--holdout-fraction", "0.34"]) == 0
    index = json.loads((ws / "index.json").read_text())
    split = [r for r in index["runs"] if r["command"] == "split"][0]
    curate = str(ws / "runs" / split["run_id"] / split["outputs"]["out_curate_csv"][0])
    def arm(name, extra):
        assert main(W + ["generate", "--questions", curate, "--out", str(ws / f"arms/{name}.csv"),
                         "--backend", "command", "--command", *agent,
                         "--runner-version", "agent@abc123", *extra]) == 0
        assert main(W + ["score", "--db", db,
                         "--predictions", str(ws / f"arms/{name}.csv")]) == 0

    arm("base", [])
    (ws / "context").mkdir(exist_ok=True)
    (ws / "context" / "rules.md").write_text('- "staff" -> emp.name\n')
    leak = api_next(ws)
    assert leak["state"] == "leak_unchecked" and _run_in(ws, leak["command"]) == 0
    arm("ctx", ["--context", str(ws / "context" / "rules.md")])

    nxt = api_next(ws)
    assert nxt["state"] == "verdict_holdout_ready", (nxt["state"], [o["kind"] for o in nxt["also"]])
    card = next(o for o in nxt["also"] if o["kind"] == "holdout")

    for argv in card["argvs"]:
        assert "--command" in argv, argv
        assert argv[argv.index("--command") + 1] == agent[0]
        assert argv[argv.index("--runner-version") + 1] == "agent@abc123"


def test_a_paid_step_says_so_when_the_model_call_is_a_draft(tmp_path):
    """draft-rules and rewrite-questions both call a model and both read as
    free. So `chat --print next --run` and the guided path could launch one at
    a customer site with no budget and no typed confirmation, against a written
    promise that every paid step states its price first."""
    from context_lift.loop import cost_note, is_paid

    assert is_paid("draft-rules", ["--report", "r.json", "--out", "d.md"]) is True
    assert is_paid("rewrite-questions", ["--golds", "g.csv", "--out", "v2.csv"]) is True
    assert "add --budget USD" in cost_note("draft-rules", ["--report", "r.json"])

    # the two ways to make either of them free are still free
    assert is_paid("draft-rules", ["--backend", "echo"]) is False
    assert is_paid("rewrite-questions", ["--estimate-only"]) is False


def test_the_step_that_reads_the_reserved_questions_says_it_does(tmp_path):
    """`touches_holdout` was the literal False on every step, including the one
    that answers the reserved questions. Nothing shipped read the field, so it
    could not open the seal by itself - but the README advertises `next --json`
    for pipelines, and a stale field is what a later change starts trusting."""
    from context_lift.loop import api_next
    ws, db = _round_one_workspace(tmp_path)

    nxt = api_next(ws)
    card = next(o for o in nxt["also"] if o["kind"] == "holdout")

    assert card["touches_holdout"] is True          # the reading, correctly marked
    assert nxt["action"]["touches_holdout"] is False   # the round step, correctly not


def test_the_human_step_names_the_two_commands_that_do_it(tmp_path, capsys):
    """A cold run reached round 2 only because the analyst went reading
    `--help`. The step where a person reads a draft and installs it has to
    NAME the two commands that do it, or the loop's own story - each round
    builds on the last - is unreachable from the guided path."""
    import shutil

    from context_lift.loop import api_next
    db = _seed_db(tmp_path)
    ws = tmp_path / "ws"
    (ws / "dataset").mkdir(parents=True)
    questions = _write_csv(ws / "dataset" / "questions.csv", ["id", "question", "gold_sql"],
                           [(f"q{i}", f"question {i}",
                             "SELECT name FROM emp" if i % 2 else "SELECT age FROM emp")
                            for i in range(6)])
    (ws / "dataset" / "schema.txt").write_text("CREATE TABLE emp(name TEXT, age INT)")
    shutil.copyfile(db, ws / "emp.sqlite")
    db = str(ws / "emp.sqlite")
    W = ["--workspace", str(ws)]
    assert main(W + ["gate", "--db", db, "--golds", questions]) == 0
    assert main(W + ["split", "--golds", questions, "--holdout-fraction", "0.34"]) == 0
    index = json.loads((ws / "index.json").read_text())
    split = [r for r in index["runs"] if r["command"] == "split"][0]
    curate = str(ws / "runs" / split["run_id"] / split["outputs"]["out_curate_csv"][0])
    assert main(W + ["generate", "--questions", curate,
                     "--schema", str(ws / "dataset" / "schema.txt"),
                     "--out", str(ws / "arms/base.csv"), "--backend", "echo",
                     "--echo-response", "SELECT age FROM emp"]) == 0
    assert main(W + ["score", "--db", db, "--predictions", str(ws / "arms/base.csv")]) == 0

    # triage comes first in round 0; run it, then read the rules step
    triage = api_next(ws)
    assert triage["state"] == "need_triage"
    assert _run_in(ws, triage["command"]) == 0

    capsys.readouterr()
    main(W + ["next"])
    out = capsys.readouterr().out

    assert "round 1" in out and "write the rules" in out
    # both halves of the human step are printed as commands, not described
    assert "draft-rules" in out
    assert "context-write" in out
    assert "costs money" in out              # draft-rules calls a model


def test_climb_is_offered_once_there_is_more_than_one_round(tmp_path):
    """One ablation cannot say whether the NEWEST round bought anything. A
    cold run's round 2 read +1.6 points while it fixed 13 and broke 10, and
    only climb's ladder said the gain was inside the noise floor. It was
    never offered."""
    from context_lift.loop import api_next
    ws, db = _round_one_workspace(tmp_path)

    one_round = api_next(ws)
    training = next(o for o in one_round["also"] if o["kind"] == "training")

    # round 1 alone: an ablation and an attribution, no ladder to climb yet
    assert [argv[0] for argv in training["argvs"]] == ["ablate", "attribute"]
    assert "against the noise floor" in training["label_note"]
