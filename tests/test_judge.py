# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Behavior tests for the one-sided equivalence judge.

All cloud-free: the judge takes its LLM as an injected Callable, so these feed
it canned replies (echo_backend) or a deliberately broken backend and pin how
the verdict is parsed. Together they spec the one-sided, fail-safe contract.
"""
from context_lift.backends import echo_backend
from context_lift.judge import build_judge_prompt, judge

# Stand-in inputs; the judge only forwards these to the backend, so their exact
# content does not change parsing — except the SQL, which must reach the prompt.
Q = "which employees are in engineering?"
GOLD_SQL = "SELECT name FROM emp WHERE dept = 'eng'"
PRED_SQL = "SELECT full_name FROM emp WHERE department = 'engineering'"
GOLD_ROWS = [{"name": "ann"}]
PRED_ROWS = [{"full_name": "ann"}]


def _run(reply):
    """Judge the fixed inputs against a backend that always returns `reply`."""
    return judge(Q, GOLD_SQL, PRED_SQL, GOLD_ROWS, PRED_ROWS, echo_backend(reply))


def test_equivalent_verdict_marks_the_row_equivalent():
    v = _run("VERDICT: EQUIVALENT\nREASON: same person, different column name")
    assert v.equivalent is True
    assert v.verdict == "EQUIVALENT"


def test_not_equivalent_verdict_does_not_mark_equivalent():
    v = _run("VERDICT: NOT_EQUIVALENT\nREASON: missing a row")
    assert v.equivalent is False
    assert v.verdict == "NOT_EQUIVALENT"


def test_verdict_token_is_case_insensitive():
    v = _run("verdict: equivalent\nreason: ok")
    assert v.equivalent is True
    assert v.verdict == "EQUIVALENT"


def test_malformed_reply_yields_empty_verdict_and_not_equivalent():
    # No VERDICT line at all -> fail closed, not a guess.
    v = _run("I think these are basically the same thing.")
    assert v.equivalent is False
    assert v.verdict == ""


def test_reason_is_captured_for_audit():
    v = _run("VERDICT: NOT_EQUIVALENT\nREASON: predicted result drops the totals")
    assert v.reason == "predicted result drops the totals"


def test_backend_error_never_propagates_and_fails_closed():
    def broken(prompt):
        raise RuntimeError("quota exceeded")

    v = judge(Q, GOLD_SQL, PRED_SQL, GOLD_ROWS, PRED_ROWS, broken)
    assert v.equivalent is False
    assert v.verdict == ""
    assert "quota exceeded" in v.reason


def test_prompt_contains_both_sql_statements():
    captured = {}

    def capturing(prompt):
        captured["prompt"] = prompt
        return "VERDICT: EQUIVALENT\nREASON: ok"

    judge(Q, GOLD_SQL, PRED_SQL, GOLD_ROWS, PRED_ROWS, capturing)
    assert GOLD_SQL in captured["prompt"]
    assert PRED_SQL in captured["prompt"]


def test_prompt_labels_truncation_when_rows_exceed_sample():
    rows = [{"n": i} for i in range(25)]
    prompt = build_judge_prompt(Q, GOLD_SQL, PRED_SQL, rows, rows, row_sample=20)
    assert "5 more rows" in prompt


def test_prompt_shows_no_rows_marker_for_empty_side():
    prompt = build_judge_prompt(Q, GOLD_SQL, PRED_SQL, GOLD_ROWS, [], row_sample=20)
    assert "(no rows)" in prompt
