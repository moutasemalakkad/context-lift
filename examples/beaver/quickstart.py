#!/usr/bin/env python3
"""Render and sanity-check the recorded BEAVER decision, without credentials."""
from __future__ import annotations

import json
import pathlib
import sys


HERE = pathlib.Path(__file__).resolve().parent


def load_result(path: pathlib.Path = HERE / "result.json") -> dict:
    result = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "sampled", "gate_clean", "gate_flagged", "working", "reserved",
        "paired_questions", "paired_answers", "baseline_correct",
        "treatment_correct", "baseline_percent", "treatment_percent",
        "lift_points", "lift_ci_points", "question_fixed", "question_broke",
        "broken_ids", "mcnemar_p", "verdict", "template_twins_banned",
    }
    missing = required - result.keys()
    if missing:
        raise ValueError(f"result is missing: {', '.join(sorted(missing))}")
    if result["gate_clean"] + result["gate_flagged"] != result["sampled"]:
        raise ValueError("gate counts do not add up to the sample")
    if result["working"] + result["reserved"] != result["gate_clean"]:
        raise ValueError("split counts do not add up to the clean key")
    if result["paired_answers"] != result["paired_questions"] * 3:
        raise ValueError("the final arm does not contain three repetitions")
    baseline = 100 * result["baseline_correct"] / result["paired_answers"]
    treatment = 100 * result["treatment_correct"] / result["paired_answers"]
    if round(baseline, 1) != result["baseline_percent"]:
        raise ValueError("baseline percentage does not match its counts")
    if round(treatment, 1) != result["treatment_percent"]:
        raise ValueError("treatment percentage does not match its counts")
    if round(treatment - baseline, 1) != result["lift_points"]:
        raise ValueError("lift does not match the arm counts")
    if len(result["broken_ids"]) != result["question_broke"]:
        raise ValueError("broken ids do not match the regression count")
    return result


def render(r: dict) -> str:
    lo, hi = r["lift_ci_points"]
    broken = ", ".join(r["broken_ids"]) or "none"
    return f"""BEAVER / verified-query context

1  TRUST THE TEST
   gate kept {r['gate_clean']:,}/{r['sampled']:,} candidate golds and flagged {r['gate_flagged']:,}
   bad gold SQL never became a model failure

2  SPEND CURATION WHERE IT WORKS
   broad rules: {r['rules_working_lift_points']:+.1f} points (not selected)
   verified examples: {r['examples_working_lift_points']:+.1f} points on the working set (selected)

3  CASH THE CLAIM ON UNSEEN QUESTIONS
   {r['baseline_percent']:.1f}%  ->  {r['treatment_percent']:.1f}%   ({r['lift_points']:+.1f} points, 95% CI {lo:+.1f} to {hi:+.1f})
   {r['verdict']} (McNemar p={r['mcnemar_p']:.4f}): {r['question_fixed']} fixed, {r['question_broke']} broke ({broken})

GUARDRAILS THAT CHANGE THE MEANING
   {r['paired_questions']} paired questions x 3 repetitions; outer holdout read once
   {r['template_twins_banned']} template twins banned before the final example bank
   positive lift, but not a clean win: the regression remains visible

WHAT contextlift CHANGED
   not just a score: it rejected ineffective rules, selected verified examples,
   protected the holdout, and named what broke.

This was a paid recorded run (~${r['model_spend_usd']:.2f}); this receipt reader is free.
Audit offline: make example-thelook
Reproduce BEAVER: examples/beaver/README.md
Run your estate: contextlift --workspace /absolute/path/to/workspace next"""


def main() -> int:
    try:
        print(render(load_result()))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"BEAVER quickstart failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
