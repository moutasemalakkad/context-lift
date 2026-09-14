# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""End-to-end scoring: tie executor + comparator + judge + metrics together.

`evaluate` is the one call a user makes. For each prediction it runs the gold
and predicted SQL through the injected Executor, compares the result sets, and
(optionally) sends the leftover disagreements to the LLM judge. It returns an
EvalReport carrying every per-question outcome plus two headline rates, each
with a Wilson confidence interval:

- execution accuracy   — the exact comparator alone
- judge-adjusted       — after the one-sided judge upgrades EQUIVALENT cases;
                         always >= execution accuracy

Two outcomes are excluded from the denominator because they carry no signal
about the prediction:
- EMPTY_BOTH  — gold and prediction both empty. The gold should have been
                removed by the gate first; counting it either way is
                misleading (a model that returns nothing would "match").
- GOLD_ERROR  — the gold query itself failed to run; it can't grade anything.
- ROW_CAP_HIT — a row cap cut one of the two results, so comparing them
  would answer a question nobody asked. Excluded, not wrong: a cut result
  is an unknown answer, and calling it wrong invents a failure.
A failed PREDICTION, by contrast, is a real wrong answer and counts as
incorrect.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import threading

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from .compare import compare
from .executor import ExecResult, Executor
from .judge import judge as judge_call
from .metrics import Proportion, proportion

# Verdicts beyond the comparator's own (MATCH/MISMATCH/EMPTY_ONE/EMPTY_BOTH).
GOLD_ERROR = "GOLD_ERROR"
PRED_ERROR = "PRED_ERROR"
ROW_CAP_HIT = "ROW_CAP_HIT"

# What each verdict means, in words a business analyst can read. A field
# engineer sits beside one, and a column of MATCH / MISMATCH / EMPTY_ONE /
# PRED_ERROR says nothing to them. `bundle` reads this the same way it reads
# triage's BUCKET_MEANINGS and the gate's GATE_MEANINGS.
VERDICT_MEANINGS = {
    "MATCH": "the same rows as the answer key",
    "MISMATCH": "ran, but returned different rows",
    "EMPTY_ONE": "one side returned no rows and the other returned some",
    "EMPTY_BOTH": "both sides returned no rows, so nothing was measured",
    "PRED_ERROR": "the agent's query did not run",
    "GOLD_ERROR": "the answer key's own query did not run",
    "ROW_CAP_HIT": "a row cap cut one of the results, so they cannot be compared",
}

# The verdicts the one-sided judge reviews: wrong by the exact comparator,
# with rows on both sides to read. A failed prediction has no rows.
JUDGE_REVIEWS = frozenset({"MISMATCH", "EMPTY_ONE"})

# A prediction tuple: (id, question, gold_sql, predicted_sql).
Prediction = tuple[str, str, str, str]


@dataclass(frozen=True)
class QuestionResult:
    """The outcome of scoring one prediction.

    `execution_correct` / `adjusted_correct` are None when the question is
    excluded from the rates (EMPTY_BOTH, GOLD_ERROR or ROW_CAP_HIT),
    True/False otherwise.
    `adjusted_correct` differs from `execution_correct` only when the judge
    upgraded a disagreement. `judge_verdict` and `detail` are for an audit log.
    """

    id: str
    question: str
    verdict: str
    execution_correct: bool | None
    adjusted_correct: bool | None
    judged: bool
    judge_verdict: str
    detail: str
    # First rows of both result sets, kept only for a wrong answer, so a
    # viewer or a static report can show what the scorer saw without
    # executing SQL again. None for MATCH, exclusions, and older reports.
    rows_preview: dict | None = None
    # The answer that was scored (the predicted SQL, or the predicted table
    # for a routing report), so a comparison can count answers that changed
    # at all against answers that changed correctness. None in older reports.
    answer: str | None = None


ROWS_PREVIEW_CAP = 5


def _jsonable(row: dict) -> dict:
    """Cells that json.dumps accepts; dates and decimals become their str."""
    return {
        key: value if isinstance(value, (int, float, str, bool)) or value is None
        else str(value)
        for key, value in row.items()
    }


def _rows_preview(gold_rows: list[dict], pred_rows: list[dict]) -> dict:
    return {
        "gold": [_jsonable(r) for r in gold_rows[:ROWS_PREVIEW_CAP]],
        "pred": [_jsonable(r) for r in pred_rows[:ROWS_PREVIEW_CAP]],
        "gold_total": len(gold_rows),
        "pred_total": len(pred_rows),
    }


@dataclass(frozen=True)
class EvalReport:
    """All per-question results plus the two headline rates with CIs.

    `n_scored` is the denominator (excluded questions removed). `execution` and
    `adjusted` are Proportions, so a caller always has the interval next to the
    rate. Use `correctness_vector()` to feed metrics.mcnemar_from_vectors when
    comparing two runs.
    """

    results: list[QuestionResult]
    n_total: int
    n_scored: int
    n_excluded: int
    execution: Proportion
    adjusted: Proportion

    def to_dict(self) -> dict:
        """Serializable form, so a scored run survives the process that made it."""
        return {
            "n_total": self.n_total,
            "results": [
                {
                    "id": r.id, "question": r.question, "verdict": r.verdict,
                    "execution_correct": r.execution_correct,
                    "adjusted_correct": r.adjusted_correct,
                    "judged": r.judged, "judge_verdict": r.judge_verdict,
                    "detail": r.detail, "rows_preview": r.rows_preview,
                    "answer": r.answer,
                }
                for r in self.results
            ],
        }

    def correctness_vector(self, adjusted: bool = False) -> list[bool]:
        """Correctness over the SCORED ROWS, in input order.

        ONE ENTRY PER ROW, NOT PER QUESTION, and the distinction is not
        cosmetic. With repetitions a question contributes one row per rep, so
        this vector is 3n long at K=3. `ablate.climb` divided by its length to
        get the ladder's noise floor and got 1/sqrt(3n) -- too small by
        sqrt(3), which let a gain inside the noise move the reported score.
        Use `ablate._questions_in(report)` when a QUESTION count is wanted.
        (This docstring said "per-question" until 2026-09-14, which is how
        that happened.)

        Excluded questions (None) are dropped. For McNemar, run the same
        predictions in the same order through both systems and the two vectors
        line up by position — the alignment mcnemar_from_vectors requires. Pass
        adjusted=True to compare judge-adjusted correctness instead of raw
        execution.
        """
        field = "adjusted_correct" if adjusted else "execution_correct"
        return [
            getattr(r, field) for r in self.results if getattr(r, field) is not None
        ]


def subset(report: EvalReport, ids: set[str], confidence: float = 0.95) -> EvalReport:
    """The same report over the listed questions only, rates rebuilt.

    A round is answered over the curate half, so `--ids curate_ids.txt` is the
    reading of a round and `--ids holdout_ids.txt` reads reports imported from
    elsewhere that hold both halves. One scored report, no re-scoring; the ids
    file is the one split wrote."""
    kept = [r for r in report.results if r.id.split("#rep")[0] in ids]
    return _report_from_results(kept, confidence)


def _report_from_results(results: list[QuestionResult],
                         confidence: float = 0.95) -> EvalReport:
    """Rebuild the rates from per-question results.

    One definition, used by a fresh run and a reloaded one alike, so a report
    read back from disk is never subtly different from the one that made it.
    """
    scored = [r for r in results if r.execution_correct is not None]
    return EvalReport(
        results=results,
        n_total=len(results),
        n_scored=len(scored),
        n_excluded=len(results) - len(scored),
        execution=proportion(sum(1 for r in scored if r.execution_correct),
                             len(scored), confidence),
        adjusted=proportion(sum(1 for r in scored if r.adjusted_correct),
                            len(scored), confidence),
    )


def save_report(report: EvalReport, path: str | pathlib.Path) -> pathlib.Path:
    """Write a scored run to disk.

    Scoring is the expensive step — it executes SQL against a warehouse — and
    the analysis afterwards is free. Persisting the outcome means ablate, climb
    and triage can be re-run, re-pooled and re-read without paying twice.
    """
    path = pathlib.Path(path)
    path.write_text(json.dumps(report.to_dict(), indent=1))
    return path


def load_report(path: str | pathlib.Path, confidence: float = 0.95) -> EvalReport:
    """Read a scored run back. Rates are recomputed, not stored, so a report
    reloaded at a different confidence level is still correct."""
    data = json.loads(pathlib.Path(path).read_text())
    results = [QuestionResult(**r) for r in data["results"]]
    return _report_from_results(results, confidence)


def _needs_judge(result: QuestionResult) -> bool:
    """A row the judge would review and has not yet: wrong by the exact
    comparator, never judged."""
    return result.verdict in JUDGE_REVIEWS and not result.judged


def rows_needing_judge(checkpoint: str | pathlib.Path) -> list[str]:
    """Ids in a checkpoint that a judge run will execute again.

    The checkpoint holds verdicts, not result rows, so the judge cannot
    read a checkpointed disagreement without its SQL running once more.
    A caller prints this count before the run, so the extra queries are
    announced rather than found in the bill.
    """
    path = pathlib.Path(checkpoint)
    if not path.exists():
        return []
    return [r.id for r in load_report(path).results if _needs_judge(r)]


def _shape_note(gold_rows: list[dict], pred_rows: list[dict]) -> str:
    """How the two result sets differ in shape, in words.

    Recorded even when no judge runs, so triage can name a failure offline.
    Shape is a fact the comparator already established; leaving detail empty
    threw that away and every judge-less failure came back UNCLASSIFIED.
    """
    gold_cols = len(gold_rows[0]) if gold_rows else 0
    pred_cols = len(pred_rows[0]) if pred_rows else 0
    if len(gold_rows) != len(pred_rows):
        return (f"different row count: gold {len(gold_rows)}, "
                f"prediction {len(pred_rows)}")
    if gold_cols != pred_cols:
        return (f"different column count: gold {gold_cols}, "
                f"prediction {pred_cols}")
    return (f"same shape ({len(gold_rows)} rows x {gold_cols} columns), "
            f"different values")


class _Answers:
    """The rows every SQL text in a run produced, each text executed once.

    `known` starts as a copy of the gold cache and grows with this run's
    golds and predictions. A prediction whose text equals its gold, or
    another prediction (temperature 0 repeats itself), reads the rows already
    fetched instead of paying for the same query again. Golds also go back
    into the caller's `gold_cache`, which is what survives between commands;
    predictions do not, because a prediction is not an answer key entry.

    `gold_cache` keyed by gold SQL avoids re-running the answer key. The same
    golds run in every arm and every repetition, so on a six-arm comparison
    that is 120 of 240 queries saved: real money on a cloud warehouse, and the
    reason a three-rep ablation is affordable at all.
    """

    def __init__(self, executor: Executor, gold_cache: dict | None):
        self.executor = executor
        self.gold_cache = gold_cache
        self.known: dict[str, ExecResult] = dict(gold_cache or {})
        self._lock = threading.Lock()

    def has(self, sql: str) -> bool:
        return sql in self.known

    def gold(self, sql: str) -> ExecResult:
        """The gold's rows, executed on a miss and kept in the gold cache."""
        found = self.known.get(sql)
        if found is None:
            found = self.executor.execute(sql)
            with self._lock:
                self.known[sql] = found
                if self.gold_cache is not None:
                    self.gold_cache[sql] = found
        return found

    def pred(self, sql: str) -> ExecResult:
        """The prediction's rows, executed on a miss and kept for this run."""
        found = self.known.get(sql)
        if found is None:
            found = self.executor.execute(sql)
            with self._lock:
                self.known[sql] = found
        return found


def _evaluate_one(
    answers: _Answers,
    prediction: Prediction,
    judge_backend: Callable[[str], str] | None,
    extra_columns: str = "reject",
) -> QuestionResult:
    """Score a single prediction; never raises (errors become verdicts)."""
    id_, question, gold_sql, pred_sql = prediction

    gold = answers.gold(gold_sql)
    if not gold.ok:
        # The answer key is broken — can't grade anything against it. Excluded.
        return QuestionResult(
            id_, question, GOLD_ERROR, None, None, False, "",
            f"gold failed: {gold.error}",
        )

    pred = answers.pred(pred_sql)
    if not pred.ok:
        # A prediction that won't run is a real wrong answer, not an exclusion.
        return QuestionResult(
            id_, question, PRED_ERROR, False, False, False, "",
            f"prediction failed: {pred.error}",
        )

    # Excluded, not wrong. Comparing a full result to a cut one, or two results
    # cut at different points, answers a question nobody asked: the comparator
    # would report MISMATCH and the score would look fine and be wrong. A cut
    # result is an unknown answer, and calling it wrong invents a failure.
    # Raise --row-cap, or narrow the question, and run it again.
    if gold.truncated or pred.truncated:
        which = " and ".join(
            [name for name, result in (("the answer key", gold), ("the prediction", pred))
             if result.truncated])
        return QuestionResult(
            id_, question, ROW_CAP_HIT, None, None, False, "",
            f"the row cap cut {which}, so the two cannot be compared. "
            "Raise --row-cap or narrow the question",
        )

    verdict = compare(gold.rows, pred.rows, gold_sql, extra_columns=extra_columns)

    if verdict == "MATCH_EXTRA_COLUMNS":
        # correct under the tolerant option only; the verdict keeps the name so
        # a report can print the strict count beside it
        extra = len(pred.rows[0]) - len(gold.rows[0])
        return QuestionResult(id_, question, verdict, True, True, False, "",
                              f"matched after ignoring {extra} extra column(s)")
    if verdict == "EMPTY_BOTH":
        return QuestionResult(
            id_, question, verdict, None, None, False, "", "both empty — excluded"
        )
    if verdict == "MATCH":
        return QuestionResult(id_, question, verdict, True, True, False, "", "")

    # MISMATCH or EMPTY_ONE: wrong by the exact comparator. The one-sided judge
    # may still upgrade it, but only to MATCH, and only if a backend was given.
    preview = _rows_preview(gold.rows, pred.rows)
    if judge_backend is None:
        return QuestionResult(
            id_, question, verdict, False, False, False, "",
            _shape_note(gold.rows, pred.rows), rows_preview=preview,
        )

    jv = judge_call(question, gold_sql, pred_sql, gold.rows, pred.rows, judge_backend)
    return QuestionResult(
        id_, question, verdict,
        execution_correct=False,
        adjusted_correct=jv.equivalent,
        judged=True,
        judge_verdict=jv.verdict,
        detail=jv.reason,
        rows_preview=preview,
    )


def _run_all(work: Callable, items: list, workers: int) -> list:
    """`work` over every item, results in input order. One thread when
    workers <= 1, so a caller's expected call sequence holds; a pool above
    that, where pool.map keeps the order whatever finishes first."""
    if workers <= 1:
        return [work(item) for item in items]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(work, items))


def evaluate(
    executor: Executor,
    predictions: list[Prediction],
    judge_backend: Callable[[str], str] | None = None,
    confidence: float = 0.95,
    gold_cache: dict | None = None,
    checkpoint: str | pathlib.Path | None = None,
    extra_columns: str = "reject",
    workers: int = 1,
) -> EvalReport:
    """Score every prediction and return an EvalReport.

    `extra_columns="tolerate"` accepts a prediction whose columns are a
    superset of the gold's (verdict MATCH_EXTRA_COLUMNS); off by default so
    rates stay comparable with Spider and BIRD.

    Three phases, each in input order: the distinct golds not already in
    the cache, then the distinct predictions not already answered, then
    compare and judge. Phases exist so that no SQL text runs twice and so
    that `workers` threads can share the work with no ordering surprises;
    the report reads the same as a one-at-a-time run.

    Args:
        executor: runs both gold and predicted SQL (SQLite, BigQuery, or any
            object with .execute).
        predictions: list of (id, question, gold_sql, predicted_sql).
        judge_backend: optional Callable[[str], str]; when given, the judge runs
            on each MISMATCH/EMPTY_ONE and can upgrade it to correct. When None,
            adjusted accuracy equals execution accuracy. Over a checkpoint, a
            wrong row the judge has not seen runs again (`rows_needing_judge`
            lists them); a judged row and a correct row are read from the file.
        confidence: confidence level for both Wilson intervals (default 0.95).
        workers: how many queries run at once. 1 runs inline; on a
            warehouse where each round trip is seconds of network, 4 cuts
            the wait by about that factor.
    """
    # Checkpointing exists because a six-arm run died with its shell after five
    # arms and threw away work already paid for. Questions already in the file
    # are skipped, so a restart resumes rather than re-executing.
    done: dict[str, QuestionResult] = {}
    if checkpoint and pathlib.Path(checkpoint).exists():
        for item in json.loads(pathlib.Path(checkpoint).read_text())["results"]:
            done[item["id"]] = QuestionResult(**item)
    if judge_backend is not None:
        # a checkpointed disagreement no judge saw goes back to the queue:
        # the file has its verdict but not its rows, and a field run that
        # passed --judge-gemini over a checkpoint got no review at all
        done = {id_: r for id_, r in done.items() if not _needs_judge(r)}

    # one slot per input; a finished question fills its own slot, so the
    # checkpoint and the report keep input order however the threads finish
    slots: list[QuestionResult | None] = [done.get(p[0]) for p in predictions]
    todo = [i for i, slot in enumerate(slots) if slot is None]
    answers = _Answers(executor, gold_cache)

    golds = list(dict.fromkeys(predictions[i][2] for i in todo
                               if not answers.has(predictions[i][2])))
    _run_all(answers.gold, golds, workers)
    preds = list(dict.fromkeys(predictions[i][3] for i in todo
                               if not answers.has(predictions[i][3])))
    _run_all(answers.pred, preds, workers)

    save_lock = threading.Lock()

    def score(index: int) -> None:
        prediction = predictions[index]
        scored = _evaluate_one(answers, prediction, judge_backend,
                               extra_columns=extra_columns)
        scored = dataclasses.replace(scored, answer=prediction[3])
        with save_lock:
            slots[index] = scored
            if checkpoint:
                save_report(_report_from_results(
                    [r for r in slots if r is not None], confidence), checkpoint)

    _run_all(score, todo, workers)
    return _report_from_results([r for r in slots if r is not None], confidence)
