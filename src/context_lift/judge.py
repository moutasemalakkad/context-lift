# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""One-sided LLM equivalence judge (FLEX-style tiebreaker).

The execution comparator is exact, so it produces false MISMATCH when two
result sets mean the same thing in different shapes (full name vs first name,
a harmless extra column, a representation difference). This judge is the cheap
partial fix from FLEX (Kim et al., "FLEX: Expert-level False-Less EXecution
Metric for Reliable Text-to-SQL Benchmark", NAACL 2025,
https://arxiv.org/abs/2409.19014): an LLM looks at the cases the comparator
already flagged wrong and decides whether they are actually equivalent.

Two properties the rest of the framework relies on:

- ONE-SIDED. The harness calls this only on the {MISMATCH, EMPTY_ONE} residual,
  and only an EQUIVALENT verdict upgrades a row to MATCH. The judge never
  re-checks a MATCH, so it can only *raise* the score, never lower it. That is a
  real limitation, not a feature: a headline number still needs a two-sided
  human audit (sample MATCHes too) with a reported agreement (Cohen's kappa).
- FAIL-SAFE. Any backend failure (timeout, bad key, quota) returns an empty
  verdict and leaves the row wrong, never raises. One dead call must not take
  down a 200-question run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

# How many result rows to put in the prompt. A representational difference is
# visible in a small sample, and an unbounded result set would blow the context
# and the cost. Truncation is labeled in the prompt so the model knows there's
# more.
_DEFAULT_ROW_SAMPLE = 20

_VERDICT_RE = re.compile(r"VERDICT:\s*(\w+)", re.IGNORECASE)
_REASON_RE = re.compile(r"REASON:\s*(.+)", re.IGNORECASE)


@dataclass(frozen=True)
class JudgeVerdict:
    """Outcome of one judge call.

    `equivalent` is the decision the harness acts on — True only when the model
    clearly said EQUIVALENT. `verdict` is the parsed token kept for audit logs
    ("EQUIVALENT" / "NOT_EQUIVALENT", or "" when the reply was unparseable or
    the backend failed). `reason` is the model's one-line justification, for a
    human reviewing upgrades later.
    """

    equivalent: bool
    verdict: str
    reason: str


def _format_rows(rows: list[dict], cap: int) -> str:
    """Render up to `cap` rows for the prompt, labeling any truncation."""
    if not rows:
        return "(no rows)"
    shown = [str(r) for r in rows[:cap]]
    if len(rows) > cap:
        shown.append(f"... ({len(rows) - cap} more rows)")
    return "\n".join(shown)


def build_judge_prompt(
    question: str,
    gold_sql: str,
    pred_sql: str,
    gold_rows: list[dict],
    pred_rows: list[dict],
    row_sample: int = _DEFAULT_ROW_SAMPLE,
) -> str:
    """Build the two-line-answer prompt comparing a prediction to the gold.

    Kept separate from judge() so a test can assert the gold/predicted SQL and
    rows actually reach the model, and so the prompt can be inspected without a
    backend call.
    """
    return (
        "You are evaluating whether a predicted SQL query answers a question "
        "equivalently to a reference query.\n\n"
        f"Question:\n{question}\n\n"
        f"Reference (gold) SQL:\n{gold_sql}\n\n"
        f"Reference result:\n{_format_rows(gold_rows, row_sample)}\n\n"
        f"Predicted SQL:\n{pred_sql}\n\n"
        f"Predicted result:\n{_format_rows(pred_rows, row_sample)}\n\n"
        "An exact comparator already flagged these two result sets as different."
        " Decide whether they are nonetheless EQUIVALENT answers to the "
        "question — the same data in a different representation, harmless "
        "extra or missing columns, or an order that does not matter. If the "
        "predicted result is missing data, has wrong values, or answers a "
        "different question, it is NOT_EQUIVALENT.\n\n"
        "Respond in exactly two lines:\n"
        "VERDICT: EQUIVALENT or NOT_EQUIVALENT\n"
        "REASON: one short sentence"
    )


def _parse_verdict(raw: str) -> JudgeVerdict:
    """Parse a model reply into a JudgeVerdict, tolerant of noise around it.

    Only the exact tokens EQUIVALENT / NOT_EQUIVALENT count; anything else
    (a missing VERDICT line, a paraphrase) yields verdict="" and equivalent
    False, so an unparseable reply fails closed rather than guessing.
    """
    match = _VERDICT_RE.search(raw or "")
    token = match.group(1).upper() if match else ""
    if token not in ("EQUIVALENT", "NOT_EQUIVALENT"):
        token = ""
    reason_match = _REASON_RE.search(raw or "")
    reason = reason_match.group(1).strip() if reason_match else ""
    return JudgeVerdict(
        equivalent=(token == "EQUIVALENT"),
        verdict=token,
        reason=reason,
    )


def judge(
    question: str,
    gold_sql: str,
    pred_sql: str,
    gold_rows: list[dict],
    pred_rows: list[dict],
    backend: Callable[[str], str],
    row_sample: int = _DEFAULT_ROW_SAMPLE,
) -> JudgeVerdict:
    """Ask `backend` whether the prediction is equivalent to the gold.

    `backend` is any Callable[[str], str] (see backends.py) — injected so the
    judge runs against a fake in tests and Gemini in production through the same
    path. Never raises: a backend exception becomes an empty, non-equivalent
    verdict (fail-safe), so the caller can keep going.
    """
    prompt = build_judge_prompt(
        question, gold_sql, pred_sql, gold_rows, pred_rows, row_sample
    )
    try:
        raw = backend(prompt)
    except Exception as e:
        # Fail closed: a dead backend leaves the row wrong, run continues.
        return JudgeVerdict(
            equivalent=False,
            verdict="",
            reason=f"backend error: {type(e).__name__}: {e}",
        )
    return _parse_verdict(raw)
