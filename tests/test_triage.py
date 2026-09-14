# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Behavior tests for failure triage.

The classification cases use judge reasons and engine errors copied verbatim
from a real evaluation run, not invented strings. A pattern set tuned against
made-up text classifies made-up text; these pin it against the wording models
and engines actually produce.
"""
from context_lift.harness import EvalReport, QuestionResult
from context_lift.metrics import proportion
from context_lift.triage import UNCLASSIFIED, classify, triage


def _failing(qid: str, detail: str) -> QuestionResult:
    return QuestionResult(
        id=qid,
        question=f"question {qid}",
        verdict="NOT_EQUIVALENT",
        execution_correct=False,
        adjusted_correct=False,
        judged=True,
        judge_verdict="",
        detail=detail,
    )


def _report(results: list[QuestionResult]) -> EvalReport:
    k = sum(1 for r in results if r.adjusted_correct)
    rate = proportion(k, len(results))
    return EvalReport(
        results=results,
        n_total=len(results),
        n_scored=len(results),
        n_excluded=0,
        execution=rate,
        adjusted=rate,
    )


# Each string below was produced by a real engine or judge during an evaluation.
def test_classifies_an_aggregation_of_aggregations_error_as_dialect():
    assert classify("400 Aggregations of aggregations are not allowed at [11:43]") == "DIALECT"


def test_classifies_an_invented_column_as_schema():
    assert classify("400 Unrecognized name: product at [2:5]") == "SCHEMA"


def test_classifies_a_near_miss_column_name_as_schema():
    assert (
        classify("400 Unrecognized name: company_code_name; Did you mean company_code_text?")
        == "SCHEMA"
    )


def test_classifies_a_wrong_aggregation_level_as_grain():
    assert (
        classify(
            "The agent query finds the top 5 POs overall, not the top 5 POs for "
            "each company code and country as the question asks"
        )
        == "GRAIN"
    )


def test_classifies_a_missing_status_condition_as_filter():
    assert (
        classify("The agent query is missing the `shipment_status` filter from the GOLD query")
        == "FILTER"
    )


def test_classifies_a_wrong_time_window_as_date():
    assert (
        classify("The agent's query uses a slightly different date range (CURRENT_DATE() vs CURRENT_DATE()-1)")
        == "DATE"
    )


def test_classifies_missing_output_columns_as_shape():
    assert (
        classify("The agent's query is missing several columns requested by the question")
        == "SHAPE"
    )


def test_unmatched_evidence_is_unclassified_not_forced_into_a_bucket():
    # The whole point: a wrong bucket sends curation at the wrong table.
    assert classify("something nobody anticipated went wrong") == UNCLASSIFIED
    assert classify("") == UNCLASSIFIED


def test_dialect_wins_over_grain_when_both_words_appear():
    # An engine error naming GROUP BY is still an engine error, not a grain
    # mistake — ordering is part of the method, so it is pinned.
    assert classify("Syntax error: expected GROUP BY") == "DIALECT"


def test_triage_counts_and_ranks_buckets_by_size():
    report = _report(
        [
            _failing("q1", "top 5 overall, not for each country"),
            _failing("q2", "the query finds totals across all regions, not within each"),
            _failing("q3", "Unrecognized name: product"),
        ]
    )
    result = triage(report)
    assert result.n_failed == 3
    assert result.counts == {"GRAIN": 2, "SCHEMA": 1}
    assert result.ranked()[0] == ("GRAIN", 2)
    assert round(result.share("GRAIN"), 2) == 0.67


def test_triage_ignores_questions_that_passed():
    passing = QuestionResult(
        id="ok",
        question="fine",
        verdict="MATCH",
        execution_correct=True,
        adjusted_correct=True,
        judged=False,
        judge_verdict="",
        detail="",
    )
    result = triage(_report([passing, _failing("bad", "Unrecognized name: x")]))
    assert result.n_failed == 1
    assert result.classified[0].id == "bad"


def test_triage_respects_a_judge_upgrade():
    # Execution said wrong, the judge said equivalent: not a failure to triage.
    upgraded = QuestionResult(
        id="q1",
        question="q",
        verdict="MISMATCH",
        execution_correct=False,
        adjusted_correct=True,
        judged=True,
        judge_verdict="EQUIVALENT",
        detail="different column order",
    )
    assert triage(_report([upgraded])).n_failed == 0
    assert triage(_report([upgraded]), adjusted=False).n_failed == 1


def test_summary_is_empty_when_nothing_failed():
    assert triage(_report([])).summary() == "no failures to triage"
