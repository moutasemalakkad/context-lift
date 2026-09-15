#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Validate and render the recorded cross-database BIRD result."""
from __future__ import annotations

import json
import pathlib
import sys


HERE = pathlib.Path(__file__).resolve().parent


def load_result(path: pathlib.Path = HERE / "enterprise_result.json") -> dict:
    result = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "source_questions", "databases", "gate_clean", "gate_flagged",
        "context_eligible", "development_questions", "development_answers",
        "fresh_holdout_pool", "targeted_questions", "coverage_percent",
        "paired_answers", "baseline_correct", "treatment_correct",
        "baseline_percent", "treatment_percent",
        "answer_lift_points", "answer_lift_ci_points",
        "question_lift_points", "question_lift_ci_points",
        "question_fixed", "question_broke",
        "question_unstable", "fixed_ids", "broken_ids", "unstable_ids",
        "question_mcnemar_p", "verdict",
        "repetitions", "oracle_table_routing",
    }
    missing = required - result.keys()
    if missing:
        raise ValueError(f"result is missing: {', '.join(sorted(missing))}")
    if result["gate_clean"] + result["gate_flagged"] != result["source_questions"]:
        raise ValueError("gate counts do not add up to the source questions")
    if result["development_answers"] != (
            result["development_questions"] * result["repetitions"]):
        raise ValueError("development arm is not complete")
    if result["paired_answers"] != result["targeted_questions"] * result["repetitions"]:
        raise ValueError("final arm is not complete")
    coverage = 100 * result["targeted_questions"] / result["fresh_holdout_pool"]
    if round(coverage, 1) != result["coverage_percent"]:
        raise ValueError("coverage does not match the holdout counts")
    baseline = 100 * result["baseline_correct"] / result["paired_answers"]
    treatment = 100 * result["treatment_correct"] / result["paired_answers"]
    if round(baseline, 1) != result["baseline_percent"]:
        raise ValueError("baseline percentage does not match its count")
    if round(treatment, 1) != result["treatment_percent"]:
        raise ValueError("treatment percentage does not match its count")
    if round(treatment - baseline, 1) != result["answer_lift_points"]:
        raise ValueError("answer lift does not match the arm counts")
    # And the reading that gets quoted: one question is one observation.
    per_question = 100 * (result["question_fixed"] - result["question_broke"]) \
        / result["targeted_questions"]
    if round(per_question, 1) != result["question_lift_points"]:
        raise ValueError("question lift does not match fixed minus broke")
    if len(result["broken_ids"]) != result["question_broke"]:
        raise ValueError("broken IDs do not match the regression count")
    if len(result["fixed_ids"]) != result["question_fixed"]:
        raise ValueError("fixed IDs do not match the fixed count")
    if len(result["unstable_ids"]) != result["question_unstable"]:
        raise ValueError("unstable IDs do not match the unstable count")
    evidence_lift = 100 * (
        result["evidence_development_correct"]
        - result["baseline_development_correct"]) / result["development_answers"]
    docs_lift = 100 * (
        result["docs_development_correct"]
        - result["baseline_development_correct"]) / result["development_answers"]
    if round(evidence_lift, 1) != result["evidence_development_lift_points"]:
        raise ValueError("evidence development lift does not match its counts")
    if round(docs_lift, 1) != result["docs_development_lift_points"]:
        raise ValueError("catalog development lift does not match its counts")
    if not result["oracle_table_routing"]:
        raise ValueError("the receipt must not hide the routing qualification")
    return result


def render(result: dict) -> str:
    # The per-question reading, because three answers to one question are
    # not three independent facts. The answer-level pair is kept in the JSON
    # and shown below it, so the gap is visible rather than resolved silently.
    low, high = result["question_lift_ci_points"]
    a_low, a_high = result["answer_lift_ci_points"]
    regressions = ", ".join(result["broken_ids"])
    return f"""BIRD / where curated context creates a large, defensible lift

1  AUDIT THE MEASURING STICK
   {result['source_questions']:,} public questions across {result['databases']} databases
   gate kept {result['gate_clean']:,}; flagged {result['gate_flagged']:,} before model scoring

2  LEARN A DEPLOYMENT POLICY WITHOUT TOUCHING THE FINAL SET
   expert evidence only: {result['evidence_development_lift_points']:+.1f} points
   evidence + catalog docs: {result['docs_development_lift_points']:+.1f} points (selected)
   deploy only for explicit formula/value definitions: {result['coverage_percent']:.1f}% coverage

3  READ A FRESH HOLDOUT ONCE
   {result['baseline_percent']:.1f}%  ->  {result['treatment_percent']:.1f}%
   {result['question_lift_points']:+.1f} points (95% paired CI {low:+.1f} to {high:+.1f}) over {result['targeted_questions']} questions
   per ANSWER it reads {result['answer_lift_points']:+.1f} [{a_low:+.1f}, {a_high:+.1f}], which is narrower
     because 195 answers are 65 questions asked 3 times. Quote the first.
   {result['verdict']} (question-level p={result['question_mcnemar_p']:.4f})
   {result['question_fixed']} fixed, {result['question_broke']} broke ({regressions}), {result['question_unstable']} unstable

THE HONEST BOUNDARY
   {result['targeted_questions']} questions x {result['repetitions']} repetitions; no unpaired observations
   this policy covers {result['targeted_questions']}/{result['fresh_holdout_pool']} reserved questions, not every request
   BIRD supplies per-question expert evidence and annotated tables: this is an
   oracle-routing context-value result, not an end-to-end retriever score

WHY THIS MATTERS
   blanket context was not the win. Contextlift found the context shapes that paid,
   withheld a second fresh set, and kept {result['question_broke']} stable regressions in the verdict.

Receipt check: passed. Full method: examples/bird/README.md
"""


def main() -> int:
    try:
        print(render(load_result()))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"BIRD enterprise quickstart failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
