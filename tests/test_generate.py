# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Behavior tests for prediction generation.

The two contracts that earned this module its place: model replies are parsed
correctly whatever fence the model wraps them in (a prefix-match bug here
broke 20 of 72 predictions and cost a paid rerun), and two arms built from
the same inputs differ ONLY in the context block - the property the paired
ablation's validity rests on.
"""
import sqlite3
import sys

from context_lift.backends import echo_backend
from context_lift.executor import SQLiteExecutor
from context_lift.generate import (
    build_prompt,
    clean_sql,
    generate_predictions,
    write_predictions,
)


def test_clean_sql_handles_every_reply_shape_models_produce():
    for raw in (
        "SELECT 1;",
        "```sql\nSELECT 1;\n```",
        "```sqlite\nSELECT 1;\n```",  # the shape the prefix-match bug broke on
        "```\nSELECT 1;\n```",
        "SELECT 1;\nThis returns one row.",
        "```sqlite\nSELECT 1;\n```\nDone.",
    ):
        assert clean_sql(raw) == "SELECT 1;", raw


def test_two_arms_differ_only_in_the_context_block():
    # ablate's paired test assumes everything but the context is identical;
    # generate must make that structural, not a convention.
    baseline = build_prompt("how many rooms?", "ROOMS(id, area)")
    with_context = build_prompt(
        "how many rooms?", "ROOMS(id, area)", context='- "rooms" -> ROOMS'
    )
    assert baseline != with_context
    stripped = with_context.replace('- "rooms" -> ROOMS', "").replace("\n\n", "\n")
    assert stripped.replace("\n", "") == baseline.replace("\n", "")


def test_generated_rows_feed_score_directly_and_gold_rides_along(tmp_path):
    rows = [("q1", "names?", "SELECT name FROM emp")]
    generated = generate_predictions(
        rows, "EMP(name)", echo_backend("```sql\nSELECT name FROM emp;\n```")
    )
    assert generated == [
        ("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp;")
    ]
    out = write_predictions(generated, tmp_path / "p.csv")
    header = out.read_text().splitlines()[0]
    assert header == "id,question,gold_sql,pred_sql"


def test_per_question_context_reaches_only_its_question():
    seen = []

    def spy(prompt: str) -> str:
        seen.append(prompt)
        return "SELECT 1;"

    generate_predictions(
        [("q1", "a?", "g"), ("q2", "b?", "g")],
        "T(x)",
        spy,
        per_question_context={"q1": "EXAMPLE FOR Q1"},
    )
    assert "EXAMPLE FOR Q1" in seen[0]
    assert "EXAMPLE FOR Q1" not in seen[1]


def test_retry_feeds_the_error_back_once_and_takes_the_correction(tmp_path):
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE emp(name TEXT)")
    con.execute("INSERT INTO emp VALUES ('ann')")
    con.commit()
    con.close()

    calls = []

    def flaky(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            return "SELECT nope FROM emp;"  # no such column -> retry fires
        return "SELECT name FROM emp;"

    generated = generate_predictions(
        [("q1", "names?", "SELECT name FROM emp")],
        "EMP(name)",
        flaky,
        retry_executor=SQLiteExecutor(str(db), timeout_seconds=5),
    )
    assert len(calls) == 2
    assert "no such column" in calls[1]
    assert generated[0][3] == "SELECT name FROM emp;"


def test_cost_is_tokens_times_the_dated_price_table_with_thinking_billed_as_output():
    from context_lift.backends import estimate_cost_usd, price_table

    prices = price_table("gemini-2.5-flash")
    assert prices == {"model": "gemini-2.5-flash", "input_per_million_usd": 0.30,
                      "output_per_million_usd": 2.50, "as_of": "2026-09-01"}
    # 1M prompt tokens at $0.30 + (100k output + 100k thinking) at $2.50
    assert estimate_cost_usd(prices, 1_000_000, 100_000, 100_000) == 0.80
    assert estimate_cost_usd(price_table("unknown-model"), 10, 10) is None
    override = price_table("unknown-model", input_price=1.0, output_price=2.0)
    assert estimate_cost_usd(override, 1_000_000, 500_000) == 2.0


def test_estimate_counts_every_real_prompt_and_multiplies_by_reps():
    from context_lift.backends import echo_backend
    from context_lift.generate import estimate_run

    backend = echo_backend("SELECT 1;")
    rows = [("q1", "names?", "SELECT name FROM emp"), ("q2", "ages?", "SELECT age FROM emp")]

    estimate = estimate_run(rows, "CREATE TABLE emp(name TEXT, age INT)",
                            backend.count_tokens, reps=3, output_tokens_per_call=50)

    assert estimate["questions"] == 2
    assert estimate["calls"] == 6
    assert estimate["output_tokens"] == 300
    per_prompt = backend.count_tokens(
        __import__("context_lift.generate", fromlist=["build_prompt"]).build_prompt(
            "names?", "CREATE TABLE emp(name TEXT, age INT)"))
    assert estimate["prompt_tokens"] >= per_prompt * 6
    assert backend.usage["calls"] == 0          # counting is not calling


def _runner(tmp_path, body, name="runner"):
    script = tmp_path / f"{name}.py"
    script.write_text("import json, sys\n" + body)
    return f"{sys.executable} {script}"


def test_a_command_runner_gets_the_pieces_and_its_sql_and_routed_table_come_back(tmp_path):
    """The agent under test, in any language: JSON in, SQL out, the routed
    table beside it so routing and writing are graded separately."""
    from context_lift.backends import command_backend
    command = _runner(tmp_path, (
        "q = json.load(sys.stdin)\n"
        "assert q['schema'].startswith('CREATE') and q['rep'] == 1 and 'rule' in q['context']\n"
        "print(json.dumps({'sql': 'SELECT name FROM emp', 'table': 'main.emp'}))\n"))
    routed = {}

    rows = generate_predictions([("q1", "names?", "SELECT name FROM emp")], "CREATE TABLE emp(name TEXT)",
                                command_backend(command), context="- rule", routed=routed)

    assert rows == [("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp")]
    assert routed == {"q1": "main.emp"}


def test_a_runner_that_fails_or_hangs_is_an_empty_answer_and_counted(tmp_path):
    from context_lift.backends import command_backend
    failing = command_backend(_runner(tmp_path, "sys.exit(3)\n"))
    hanging = command_backend(_runner(tmp_path, "import time; time.sleep(5)\n", "hangs"), timeout=0.5)

    assert generate_predictions([("q1", "?", "SELECT 1")], "s", failing)[0][3] == ""
    assert generate_predictions([("q1", "?", "SELECT 1")], "s", hanging)[0][3] == ""
    assert failing.usage["failures"] == 1 and hanging.usage["timeouts"] == 1


def test_plain_text_from_a_runner_is_the_sql():
    from context_lift.generate import parse_runner_output

    assert parse_runner_output("```sql\nSELECT 1\n```") == ("SELECT 1", "", "")
    assert parse_runner_output('{"sql": "SELECT 2", "table": "d.t"}') == ("SELECT 2", "d.t", "")
    assert parse_runner_output('{"sql": "", "abstained": true, "note": "no table fits"}') \
        == ("", "", "abstained: no table fits")


def test_identical_repetitions_are_counted_as_copies():
    """One agent pinned temperature and produced byte-identical SQL three
    times on both arms; three reps of it are copies, not samples."""
    from context_lift.generate import identical_across_reps
    rep1 = [("q1", "?", "g", "SELECT a"), ("q2", "?", "g", "SELECT b")]
    rep2 = [("q1", "?", "g", "SELECT  a"), ("q2", "?", "g", "SELECT c")]

    assert identical_across_reps([rep1, rep2]) == (1, 2)
    assert identical_across_reps([rep1, rep1]) == (2, 2)


def test_the_check_catches_a_bad_draft_without_running_it(tmp_path):
    db = tmp_path / "t.db"
    con = sqlite3.connect(db); con.execute("CREATE TABLE emp(name TEXT)"); con.commit(); con.close()
    executor = SQLiteExecutor(str(db))

    assert "no such column" in executor.check("SELECT nope FROM emp")
    assert executor.check("SELECT name FROM emp") is None
    assert "not read-only" in executor.check("DELETE FROM emp")


def test_a_command_runner_gets_the_error_back_as_a_second_call(tmp_path):
    """The loop the triage asked for: 7 of 8 drafts that did not run on one
    estate were caught by a free dry run, and the runner never knew."""
    from context_lift.backends import command_backend
    db = tmp_path / "t.db"
    con = sqlite3.connect(db); con.execute("CREATE TABLE emp(name TEXT)")
    con.execute("INSERT INTO emp VALUES ('ann')"); con.commit(); con.close()
    command = _runner(tmp_path, (
        "q = json.load(sys.stdin)\n"
        "if q.get('attempt') == 2:\n"
        "    assert 'no such column' in q['error'] and q['previous_sql'] == 'SELECT nope FROM emp'\n"
        "    assert q['table'] == 'main.emp'\n"
        "    print(json.dumps({'sql': 'SELECT name FROM emp', 'table': q['table']}))\n"
        "else:\n"
        "    print(json.dumps({'sql': 'SELECT nope FROM emp', 'table': 'main.emp'}))\n"), "fixer")
    stats = {}

    rows = generate_predictions([("q1", "names?", "SELECT name FROM emp")], "", command_backend(command),
                                retry_executor=SQLiteExecutor(str(db), timeout_seconds=5), stats=stats)

    assert rows[0][3] == "SELECT name FROM emp"
    assert stats == {"retried": 1}


def test_a_runners_config_is_recorded_once_from_its_output(tmp_path):
    from context_lift.backends import command_backend
    from context_lift.generate import runner_config_in
    command = _runner(tmp_path, (
        "q = json.load(sys.stdin)\n"
        "print(json.dumps({'sql': 'SELECT 1', 'config': {'thinking_budget': -1, 'model': 'x'}}))\n"), "cfg")
    stats = {}

    generate_predictions([("q1", "?", "SELECT 1"), ("q2", "?", "SELECT 1")], "", command_backend(command), stats=stats)

    assert stats["runner_config"] == {"thinking_budget": -1, "model": "x"}
    assert runner_config_in("SELECT 1") is None


def test_a_transport_failure_in_the_free_count_does_not_end_the_run():
    """The token count is a free measurement taken BEFORE the paid work, so a
    network hiccup in it must not end the run it is pricing. One did, twice,
    with about 200 lines of traceback and no plain sentence, on the step the
    loop labels "there is no undo"."""
    from context_lift.generate import estimate_run

    def unreachable(prompt):
        raise RuntimeError("TLS handshake failed")

    rows = [("q1", "who", "SELECT 1"), ("q2", "what", "SELECT 2")]

    estimate = estimate_run(rows, "schema text", unreachable, reps=3)

    assert estimate["prompts_estimated"] == 2
    assert estimate["prompt_tokens"] > 0          # approximated, not zero
    assert estimate["calls"] == 6


def test_a_credentials_problem_still_fails_fast():
    """A missing key is not a hiccup and hiding it would waste the run. The
    first prompt is counted alone so it raises once, not once per thread."""
    import pytest

    from context_lift.backends import CredentialsMissing
    from context_lift.generate import estimate_run

    def no_key(prompt):
        raise CredentialsMissing("no Application Default Credentials")

    with pytest.raises(CredentialsMissing):
        estimate_run([("q1", "who", "SELECT 1")], "schema", no_key)


def test_a_count_that_works_reports_nothing_estimated():
    from context_lift.generate import estimate_run

    estimate = estimate_run([("q1", "who", "SELECT 1")], "schema",
                            lambda prompt: len(prompt.split()))

    assert estimate["prompts_estimated"] == 0
