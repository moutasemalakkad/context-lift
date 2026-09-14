"""Sort wrong answers into failure shapes, so curation has a target.

After a scoring run you know the rate. Triage answers the next question: of the
ones that failed, what KIND of wrong were they? A run that is 30% grain errors
and a run that is 30% invented column names need opposite fixes — write down
the reporting grain, versus make the schema reach the model.

Every bucket here was observed in a real engagement, and the patterns match on
the judge's own words rather than on a guess about the SQL. Anything that does
not match is reported UNCLASSIFIED, never forced into the nearest bucket: a
wrong bucket sends a human to curate the wrong table, which is worse than
saying "I do not know".

Complementary to a retrieval-trace attribution (was the right asset retrieved,
was it referenced). That needs a trace of the retrieval step; this needs only a
scored result, so it works for any system that emits SQL.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .compare import row_to_positional_tuple
from .harness import EvalReport, QuestionResult

UNCLASSIFIED = "UNCLASSIFIED"

# Buckets the comparator can decide on its own, with no judge and no LLM.
# Modelled on the deterministic reason codes shipped by other benchmark tools:
# a result set that is the wrong SHAPE is a fact, not an interpretation. These
# are checked before the prose patterns, and they still work offline.
ROW_COUNT = "ROW_COUNT"
COLUMN_COUNT = "COLUMN_COUNT"
EMPTY_RESULT = "EMPTY_RESULT"
DID_NOT_RUN = "DID_NOT_RUN"
TIMEOUT = "TIMEOUT"
WRONG_VALUES = "WRONG_VALUES"

_DETERMINISTIC_MEANINGS = {
    ROW_COUNT: "returned a different number of rows",
    COLUMN_COUNT: "returned a different number of columns",
    EMPTY_RESULT: "returned no rows where the gold returned some",
    DID_NOT_RUN: "the query failed to execute",
    TIMEOUT: "the query did not finish inside --timeout; fix its cost or raise the bound",
    WRONG_VALUES: "right shape, wrong numbers — usually a metric definition",
}

# Ordered: first match wins, most specific first. Each pattern is drawn from a
# failure seen in a real run; the comment names it.
_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "DIALECT",
        re.compile(
            r"aggregations? of aggregations|analytic function|"
            r"\bqualify\b|syntax error|unrecognized (?:operator|keyword)|"
            r"not supported|invalid cast",
            re.I,
        ),
        "SQL the engine refused to run for grammar reasons",
    ),
    (
        "SCHEMA",
        re.compile(
            r"unrecognized name|did you mean|no such column|column .* (?:does not|doesn't) exist|"
            r"invented|not (?:a )?(?:valid )?column|table .* was not found",
            re.I,
        ),
        "referenced a column or table that does not exist",
    ),
    (
        "GRAIN",
        re.compile(
            r"\bgroup by\b|aggregat\w+ level|\bgrain\b|per (?:each )?\w+|"
            r"for each|overall,? not|across all|within each|partition",
            re.I,
        ),
        "right rows, wrong level of aggregation",
    ),
    (
        "FILTER",
        re.compile(
            r"\bfilter\b|\bwhere\b clause|missing the .* condition|"
            r"status|excluded|did not (?:restrict|limit)|includes rows",
            re.I,
        ),
        "missing or wrong row restriction",
    ),
    (
        "DATE",
        re.compile(
            r"date range|current_date|time window|last \d+ (?:day|week|month)|"
            r"period|fiscal|as of",
            re.I,
        ),
        "wrong time window",
    ),
    (
        "SHAPE",
        re.compile(
            r"missing several columns|omits|extra column|additional column|"
            r"columns requested|different columns|column order",
            re.I,
        ),
        "right rows, wrong columns returned",
    ),
]

BUCKET_MEANINGS: dict[str, str] = {name: why for name, _, why in _PATTERNS}
BUCKET_MEANINGS.update(_DETERMINISTIC_MEANINGS)
BUCKET_MEANINGS[UNCLASSIFIED] = "no pattern matched — read these by hand"


@dataclass(frozen=True)
class Classified:
    """One failing question and the bucket its evidence put it in."""

    id: str
    question: str
    bucket: str
    evidence: str


@dataclass(frozen=True)
class TriageReport:
    """Failure shapes across a run, plus the raw classifications."""

    classified: list[Classified]
    counts: dict[str, int]
    n_failed: int

    def share(self, bucket: str) -> float:
        """Fraction of failures in one bucket, 0.0 when nothing failed."""
        return self.counts.get(bucket, 0) / self.n_failed if self.n_failed else 0.0

    def ranked(self) -> list[tuple[str, int]]:
        """Buckets by size, largest first — the order to spend effort in."""
        return sorted(self.counts.items(), key=lambda kv: (-kv[1], kv[0]))

    def summary(self) -> str:
        if not self.n_failed:
            return "no failures to triage"
        lines = [f"{self.n_failed} failures by shape:"]
        for bucket, count in self.ranked():
            lines.append(
                f"  {bucket:<14} {count:>3}  {count / self.n_failed:5.0%}  "
                f"{BUCKET_MEANINGS.get(bucket, '')}"
            )
        top = self.ranked()[0]
        if top[0] != UNCLASSIFIED:
            lines.append(f"\nlargest bucket is {top[0]}: {BUCKET_MEANINGS[top[0]]}")
        if top[0] == COLUMN_COUNT:
            # golds that are dashboard queries return lookup columns the
            # question never asked for; the strict comparator counts every
            # one as wrong, and the tolerant reading exists for that
            lines.append("  if the predictions return the gold's columns plus more, re-score both "
                         "arms with score --extra-columns tolerate; it prints how many matched "
                         "only under tolerance")
        return "\n".join(lines)


def evidence_for(result: QuestionResult) -> str:
    """The text a bucket is decided from: the judge's reason, else the detail.

    The judge's one-line reason is the better signal when present — it is a
    statement about what differed, whereas detail may be a raw engine error.
    Both are searched, so an engine error still classifies when no judge ran.
    """
    return " ".join(part for part in (result.detail, result.judge_verdict) if part)


def classify_deterministic(result: QuestionResult) -> str | None:
    """Bucket from the verdict alone, when the verdict is enough.

    Runs before the prose patterns so a failure is named even with no judge —
    which is the normal case for a cheap offline run.
    """
    verdict = (result.verdict or "").upper()
    if "ERROR" in verdict and "GOLD" not in verdict:
        # executors report a timeout as "timeout: exceeded Ns"; it is a
        # different fix from a query that fails, so it is its own bucket
        return TIMEOUT if "timeout:" in (result.detail or "") else DID_NOT_RUN
    if verdict == "EMPTY_ONE":
        return EMPTY_RESULT
    detail = result.detail or ""
    if detail.startswith("different row count"):
        return ROW_COUNT
    if detail.startswith("different column count"):
        return COLUMN_COUNT
    if detail.startswith("same shape"):
        return WRONG_VALUES
    return None


def classify(text: str) -> str:
    """Bucket one piece of failure evidence. UNCLASSIFIED when nothing matches."""
    for name, pattern, _ in _PATTERNS:
        if pattern.search(text or ""):
            return name
    return UNCLASSIFIED


def classify_result(result: QuestionResult) -> str:
    """Bucket one failing question: deterministic signals first, prose second."""
    deterministic = classify_deterministic(result)
    if deterministic:
        return deterministic
    return classify(evidence_for(result))


def triage(report: EvalReport, adjusted: bool = True) -> TriageReport:
    """Sort a run's failures into shapes.

    Args:
        report: a scored run.
        adjusted: judge the judge-adjusted outcome (default) rather than raw
            execution, so a question the judge upgraded is not triaged as a
            failure it no longer is.
    """
    field = "adjusted_correct" if adjusted else "execution_correct"
    classified = [
        Classified(
            id=r.id,
            question=r.question,
            bucket=classify_result(r),
            evidence=evidence_for(r)[:300],
        )
        for r in report.results
        if getattr(r, field) is False
    ]
    return TriageReport(
        classified=classified,
        counts=dict(Counter(c.bucket for c in classified)),
        n_failed=len(classified),
    )
