# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Behavior tests for the end-to-end harness.

Real SQLite execution drives the comparator paths; the judge path uses
echo_backend so no cloud is needed. Each test pins one rule of how raw outcomes
roll up into the two headline rates.
"""
import json
import sqlite3
import threading
import time

from context_lift.backends import echo_backend
from context_lift.harness import (GOLD_ERROR, PRED_ERROR, evaluate, load_report,
                              rows_needing_judge, save_report)
from context_lift.executor import SQLiteExecutor
from context_lift.metrics import mcnemar_from_vectors


def _seed(tmp_path):
    """Create a 2-row emp table in a temp DB and return its path."""
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE emp(name TEXT, age INT)")
    con.executemany("INSERT INTO emp VALUES (?, ?)", [("ann", 30), ("bob", 40)])
    con.commit()
    con.close()
    return str(p)


def test_a_correct_prediction_scores_as_match(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    # Each prediction is a 4-tuple: (id, question, gold_sql, pred_sql).
    # Here the predicted SQL adds an ORDER BY the gold lacks, so the rows are
    # the same multiset -> MATCH.
    preds = [("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp ORDER BY name DESC")]
    report = evaluate(ex, preds)
    assert report.results[0].verdict == "MATCH"
    assert report.execution.point == 1.0


def test_a_wrong_prediction_lowers_the_rate(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    preds = [
        ("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp"),          # right
        ("q2", "old?", "SELECT name FROM emp WHERE age > 35", "SELECT name FROM emp"),  # wrong
    ]
    report = evaluate(ex, preds)
    assert report.n_scored == 2
    assert report.execution.k == 1
    assert report.execution.point == 0.5


def test_a_prediction_that_fails_to_run_counts_as_wrong(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    preds = [("q1", "names?", "SELECT name FROM emp", "SELECT nope FROM emp")]
    report = evaluate(ex, preds)
    assert report.results[0].verdict == PRED_ERROR
    assert report.results[0].execution_correct is False
    assert report.n_scored == 1  # a failed prediction is still scored (as wrong)


def test_a_broken_gold_is_excluded_from_the_rate(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    preds = [("q1", "names?", "SELECT nope FROM emp", "SELECT name FROM emp")]
    report = evaluate(ex, preds)
    assert report.results[0].verdict == GOLD_ERROR
    assert report.n_excluded == 1
    assert report.n_scored == 0


def test_empty_both_is_excluded_from_the_rate(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    # Both gold and prediction return nothing -> no signal -> excluded.
    preds = [(
        "q1", "nobody?",
        "SELECT name FROM emp WHERE age > 999",
        "SELECT name FROM emp WHERE age > 888",
    )]
    report = evaluate(ex, preds)
    assert report.results[0].verdict == "EMPTY_BOTH"
    assert report.n_scored == 0
    assert report.n_excluded == 1


def test_without_a_judge_adjusted_equals_execution(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    preds = [("q1", "old?", "SELECT name FROM emp WHERE age > 35", "SELECT name FROM emp")]
    report = evaluate(ex, preds)
    assert report.execution.point == report.adjusted.point == 0.0


def test_judge_can_upgrade_a_mismatch_but_only_adjusted_moves(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    # This prediction really mismatches; a judge saying EQUIVALENT upgrades it.
    preds = [("q1", "old?", "SELECT name FROM emp WHERE age > 35", "SELECT name FROM emp")]
    backend = echo_backend("VERDICT: EQUIVALENT\nREASON: same intent")
    report = evaluate(ex, preds, judge_backend=backend)
    r = report.results[0]
    assert r.execution_correct is False        # exact comparator unchanged
    assert r.adjusted_correct is True          # judge upgraded it
    assert report.execution.point == 0.0
    assert report.adjusted.point == 1.0


def test_judge_not_equivalent_leaves_the_row_wrong(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    preds = [("q1", "old?", "SELECT name FROM emp WHERE age > 35", "SELECT name FROM emp")]
    backend = echo_backend("VERDICT: NOT_EQUIVALENT\nREASON: different rows")
    report = evaluate(ex, preds, judge_backend=backend)
    assert report.adjusted.point == 0.0


def test_correctness_vector_drops_excluded_and_feeds_mcnemar(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    # q2's gold is broken -> excluded; the vector covers only q1 and q3.
    preds = [
        ("q1", "a", "SELECT name FROM emp", "SELECT name FROM emp"),                 # right
        ("q2", "b", "SELECT nope FROM emp", "SELECT name FROM emp"),                 # excluded
        ("q3", "c", "SELECT name FROM emp WHERE age > 35", "SELECT name FROM emp"),  # wrong
    ]
    report = evaluate(ex, preds)
    assert report.correctness_vector() == [True, False]

    # Two runs scored on the same ordered predictions align by position.
    run_a = [True, False]
    run_b = [True, True]
    result = mcnemar_from_vectors(run_a, run_b)
    assert result.c == 1  # the second run fixed the one the first got wrong


# --- persisting a scored run, and surviving a dropped shell ---


def test_a_saved_report_reloads_identically(tmp_path):
    # Scoring executes SQL; analysis is free. A reloaded report must give the
    # same rates, or ablate and climb would disagree with the original run.
    executor = SQLiteExecutor(_seed(tmp_path))
    original = evaluate(executor, [
        ("q1", "names", "SELECT name FROM emp", "SELECT name FROM emp"),
        ("q2", "wrong", "SELECT name FROM emp", "SELECT id FROM emp"),
    ])
    path = save_report(original, tmp_path / "run.json")
    reloaded = load_report(path)

    assert reloaded.execution.point == original.execution.point
    assert reloaded.n_scored == original.n_scored
    assert reloaded.correctness_vector() == original.correctness_vector()
    assert [r.id for r in reloaded.results] == [r.id for r in original.results]


def test_a_checkpointed_run_resumes_instead_of_re_executing(tmp_path):
    db = _seed(tmp_path)
    checkpoint = tmp_path / "ckpt.json"
    predictions = [
        ("q1", "a", "SELECT name FROM emp", "SELECT name FROM emp"),
        ("q2", "b", "SELECT name FROM emp", "SELECT name FROM emp"),
    ]
    evaluate(SQLiteExecutor(db), predictions, checkpoint=checkpoint)

    class _Exploding:
        """Any execution at all means the checkpoint was ignored."""

        def execute(self, sql):
            raise AssertionError("re-executed a question already checkpointed")

    resumed = evaluate(_Exploding(), predictions, checkpoint=checkpoint)
    assert resumed.n_scored == 2
    assert resumed.execution.point == 1.0


def test_a_partial_checkpoint_only_re_runs_what_is_missing(tmp_path):
    db = _seed(tmp_path)
    checkpoint = tmp_path / "ckpt.json"
    evaluate(SQLiteExecutor(db),
             [("q1", "a", "SELECT name FROM emp", "SELECT name FROM emp")],
             checkpoint=checkpoint)

    # q1 is cached; q2 is new and must still be scored.
    report = evaluate(SQLiteExecutor(db), [
        ("q1", "a", "SELECT name FROM emp", "SELECT name FROM emp"),
        ("q2", "b", "SELECT name FROM emp", "SELECT id FROM emp"),
    ], checkpoint=checkpoint)
    assert report.n_scored == 2
    assert report.execution.point == 0.5


class _Recording:
    """A SQLite executor that keeps every SQL text it ran."""

    def __init__(self, db):
        self.inner = SQLiteExecutor(db)
        self.executed = []

    def execute(self, sql):
        self.executed.append(sql)
        return self.inner.execute(sql)


def test_a_judge_run_over_a_checkpoint_re_executes_only_the_unjudged_wrong_rows(tmp_path):
    """A field run passed --judge-gemini over a checkpoint and the judge saw
    nothing: the file holds verdicts, not the rows the judge reads."""
    db = _seed(tmp_path)
    checkpoint = tmp_path / "ckpt.json"
    predictions = [
        ("q1", "ages?", "SELECT age FROM emp", "SELECT age FROM emp"),                    # right
        ("q2", "old?", "SELECT name FROM emp WHERE age > 35", "SELECT name FROM emp"),   # wrong
    ]
    evaluate(SQLiteExecutor(db), predictions, checkpoint=checkpoint)
    assert rows_needing_judge(checkpoint) == ["q2"]

    recording = _Recording(db)
    judge = echo_backend("VERDICT: EQUIVALENT\nREASON: same intent")
    report = evaluate(recording, predictions, checkpoint=checkpoint, judge_backend=judge)

    q1, q2 = report.results
    assert (q2.judged, q2.execution_correct, q2.adjusted_correct) == (True, False, True)
    assert (q1.judged, q1.execution_correct) == (False, True)
    assert sorted(recording.executed) == ["SELECT name FROM emp", "SELECT name FROM emp WHERE age > 35"]
    assert rows_needing_judge(checkpoint) == []      # the judged row is saved back


def test_an_already_judged_checkpointed_row_is_not_re_executed_or_re_judged(tmp_path):
    db = _seed(tmp_path)
    checkpoint = tmp_path / "ckpt.json"
    predictions = [("q1", "old?", "SELECT name FROM emp WHERE age > 35", "SELECT name FROM emp")]
    evaluate(SQLiteExecutor(db), predictions, checkpoint=checkpoint,
             judge_backend=echo_backend("VERDICT: NOT_EQUIVALENT\nREASON: different rows"))

    class _Exploding:
        def execute(self, sql):
            raise AssertionError("re-executed a question the judge already saw")

    judge = echo_backend("VERDICT: EQUIVALENT\nREASON: would flip it if asked")
    resumed = evaluate(_Exploding(), predictions, checkpoint=checkpoint, judge_backend=judge)

    assert judge.usage["calls"] == 0
    assert (resumed.results[0].judged, resumed.results[0].adjusted_correct) == (True, False)


def test_a_wrong_answer_keeps_the_first_rows_of_both_sides_and_a_match_keeps_none(tmp_path):
    ex = SQLiteExecutor(_seed(tmp_path))
    preds = [
        ("q1", "names?", "SELECT name FROM emp", "SELECT name FROM emp"),
        ("q2", "names?", "SELECT name FROM emp", "SELECT age FROM emp"),
    ]

    report = evaluate(ex, preds)

    assert report.results[0].rows_preview is None
    assert report.results[1].rows_preview == {
        "gold": [{"name": "ann"}, {"name": "bob"}],
        "pred": [{"age": 30}, {"age": 40}],
        "gold_total": 2, "pred_total": 2,
    }


def test_a_report_saved_before_row_previews_existed_still_loads(tmp_path):
    older = tmp_path / "old.json"
    older.write_text(json.dumps({"n_total": 1, "results": [{
        "id": "q1", "question": "names?", "verdict": "MISMATCH",
        "execution_correct": False, "adjusted_correct": False,
        "judged": False, "judge_verdict": "", "detail": "same shape"}]}))

    report = load_report(older)

    assert report.results[0].rows_preview is None
    assert report.execution.point == 0.0


# --- workers: the same report, with fewer and overlapping queries ---


class _Counting:
    """Wraps a real executor: records every SQL, counts how many ran at
    once, and sleeps 20 ms so threads overlap."""

    def __init__(self, inner):
        self.inner = inner
        self.seen = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.lock = threading.Lock()

    def execute(self, sql):
        with self.lock:
            self.seen.append(sql)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(0.02)
            return self.inner.execute(sql)
        finally:
            with self.lock:
                self.in_flight -= 1


def test_workers_score_in_input_order_and_run_at_once(tmp_path):
    executor = _Counting(SQLiteExecutor(_seed(tmp_path)))
    preds = [
        ("q1", "a", "SELECT name FROM emp", "SELECT name FROM emp"),
        ("q2", "b", "SELECT age FROM emp", "SELECT age FROM emp WHERE age > 35"),
        ("q3", "c", "SELECT name FROM emp WHERE age > 35", "SELECT name FROM emp WHERE age > 35"),
        ("q4", "d", "SELECT name, age FROM emp", "SELECT age, name FROM emp"),
    ]

    report = evaluate(executor, preds, workers=4)

    assert [r.id for r in report.results] == ["q1", "q2", "q3", "q4"]
    assert [r.verdict for r in report.results] == ["MATCH", "MISMATCH", "MATCH", "MATCH"]
    assert executor.max_in_flight >= 2


def test_golds_run_before_predictions_and_each_distinct_sql_runs_once(tmp_path):
    executor = _Counting(SQLiteExecutor(_seed(tmp_path)))
    preds = [
        ("q1", "a", "SELECT name FROM emp", "SELECT name FROM emp ORDER BY name"),
        ("q2", "b", "SELECT name FROM emp", "SELECT name FROM emp"),               # the gold's own text
        ("q3", "c", "SELECT age FROM emp", "SELECT name FROM emp ORDER BY name"),  # q1's prediction again
    ]

    report = evaluate(executor, preds, workers=1)

    assert executor.seen == ["SELECT name FROM emp", "SELECT age FROM emp",
                             "SELECT name FROM emp ORDER BY name"]
    assert [r.verdict for r in report.results] == ["MATCH", "MATCH", "MISMATCH"]


def test_a_checkpoint_written_under_workers_resumes_without_re_executing(tmp_path):
    db = _seed(tmp_path)
    checkpoint = tmp_path / "ckpt.json"
    preds = [(f"q{i}", "a", "SELECT name FROM emp", f"SELECT name FROM emp WHERE age > {i}")
             for i in range(6)]
    first = evaluate(SQLiteExecutor(db), preds, checkpoint=checkpoint, workers=3)

    class _Exploding:
        def execute(self, sql):
            raise AssertionError("re-executed a question already checkpointed")

    resumed = evaluate(_Exploding(), preds, checkpoint=checkpoint, workers=3)

    saved = json.loads(checkpoint.read_text())["results"]
    assert [r["id"] for r in saved] == [f"q{i}" for i in range(6)]      # input order, not finish order
    assert resumed.correctness_vector() == first.correctness_vector() == [True] * 6


def test_a_row_cap_that_cut_a_result_excludes_the_question_instead_of_scoring_it():
    """Comparing a whole result to a cut one answers a question nobody asked.

    The comparator would call it MISMATCH and the score would look fine and be
    wrong, which is the whole class of bug this status exists to stop. Excluded
    rather than wrong: a cut result is an unknown answer, and calling it wrong
    invents a failure that did not happen.
    """
    from context_lift.executor import ExecResult
    from context_lift.harness import ROW_CAP_HIT, _Answers, _evaluate_one

    class CutsTheGold:
        def execute(self, sql):
            return ExecResult(rows=[{"n": 1}], truncated="-- gold" in sql)

    answers = _Answers(CutsTheGold(), None)

    result = _evaluate_one(answers,
                           ("q1", "how many?", "SELECT 1 -- gold", "SELECT 1"), None)

    assert result.verdict == ROW_CAP_HIT
    assert result.execution_correct is None, "excluded from the rates, not counted wrong"
    assert "the answer key" in result.detail
    assert "Raise --row-cap" in result.detail


def test_a_cut_prediction_is_named_as_the_cut_side():
    """Which side was cut decides what a person does next: a cut answer key is
    a gold to narrow, a cut prediction is a question the agent over-answered."""
    from context_lift.executor import ExecResult
    from context_lift.harness import ROW_CAP_HIT, _Answers, _evaluate_one

    class CutsThePrediction:
        def execute(self, sql):
            return ExecResult(rows=[{"n": 1}], truncated="-- pred" in sql)

    answers = _Answers(CutsThePrediction(), None)

    result = _evaluate_one(answers, ("q1", "how many?", "SELECT 1", "SELECT 1 -- pred"),
                           None)

    assert result.verdict == ROW_CAP_HIT
    assert "the prediction" in result.detail and "the answer key" not in result.detail


def test_an_uncut_pair_is_still_scored_normally():
    """The new check must not swallow the ordinary case."""
    from context_lift.executor import ExecResult
    from context_lift.harness import _Answers, _evaluate_one

    class Whole:
        def execute(self, sql):
            return ExecResult(rows=[{"n": 1}], truncated=False)

    result = _evaluate_one(_Answers(Whole(), None),
                           ("q1", "how many?", "SELECT 1", "SELECT 1"), None)

    assert result.verdict == "MATCH" and result.execution_correct is True


def test_both_backends_share_one_row_cap_rule():
    """The SQLite and BigQuery paths each had this rule written out. They
    agreed; nothing made them keep agreeing, and a truncation rule that drifts
    is how a cut result gets compared as if it were whole."""
    from context_lift.executor import apply_row_cap

    rows, truncated = apply_row_cap([1, 2, 3, 4], cap=3)
    assert rows == [1, 2, 3] and truncated

    rows, truncated = apply_row_cap([1, 2, 3], cap=3)
    assert rows == [1, 2, 3] and not truncated, (
        "the caller fetches cap + 1; at exactly cap there is nothing to detect")

    rows, truncated = apply_row_cap([1, 2, 3], cap=None)
    assert rows == [1, 2, 3] and not truncated
