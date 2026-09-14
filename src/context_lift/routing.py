# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Routing accuracy: did the system pick the right table for the question?

In a warehouse with many tables, a text-to-SQL pipeline usually has a first
stage that routes the question to one table (`dataset.table`), and a second
stage that writes SQL against it. A routing miss guarantees a generation miss,
so routing is scored on its own — the report should say WHICH stage failed.

`evaluate_routing` grades predicted tables against gold tables and returns a
RoutingReport with two rates, each with a Wilson confidence interval:

- dataset accuracy — the predicted table is in the right dataset (the first
  dot-separated part matches). Reported because most routing misses land in
  the right dataset but the wrong table; one exact-match number hides that.
- table accuracy   — the full `dataset.table` string matches exactly.

Unlike the SQL harness, nothing is ever excluded from the denominator: an
empty or unparseable prediction is a real wrong answer, not missing data. The
harness's exclusions (GOLD_ERROR, EMPTY_BOTH) exist because a broken gold
can't grade anything — here a broken gold raises instead, since a routing gold
is a single table name and cheap to fix.

Optional catalog snapping: a model that generates the table name freely can
blend two real names into one that does not exist. When a catalog (the set of
valid `dataset.table` names) is given, a near-miss prediction is snapped onto
the closest valid name before scoring, and the report carries `n_snapped` so
the correction is never invisible. Two runs are only comparable when they
agree on whether a catalog was used.
"""
from __future__ import annotations

from dataclasses import dataclass
import difflib

from .metrics import Proportion, proportion

# A routing row: (id, question, gold_table, predicted_table).
RoutingRow = tuple[str, str, str, str]

# Snap threshold for difflib similarity (0..1). 0.8 is tight enough that a
# blend of two real names snaps to the nearer one, while a table that simply
# is not in the catalog stays unmatched instead of snapping to a random entry.
SNAP_CUTOFF = 0.8


@dataclass(frozen=True)
class RoutingResult:
    """The outcome of scoring one routing prediction.

    `pred_table` is the normalized (and possibly snapped) name that was
    actually scored; `raw_prediction` keeps the input as given, so a report
    can show what the snap or normalization changed.
    """

    id: str
    question: str
    gold_table: str
    pred_table: str
    raw_prediction: str
    snapped: bool
    dataset_correct: bool
    table_correct: bool


@dataclass(frozen=True)
class RoutingReport:
    """All per-question results plus the two headline rates with CIs.

    There is no n_scored/n_excluded pair here on purpose: routing has no
    exclusions, so the denominator of both rates is always `n_total`.
    `n_snapped` says how many predictions the catalog snap corrected — a
    reader comparing two runs needs it to know the rates are comparable.
    """

    results: list[RoutingResult]
    n_total: int
    n_snapped: int
    dataset: Proportion
    table: Proportion

    def correctness_vector(self, dataset: bool = False) -> list[bool]:
        """Per-question correctness in input order, for McNemar comparisons.

        Run the same questions in the same order through two routers and the
        two vectors line up by position, as metrics.mcnemar_from_vectors
        requires. Pass dataset=True to compare on the dataset tier instead of
        exact table.
        """
        field = "dataset_correct" if dataset else "table_correct"
        return [getattr(r, field) for r in self.results]


def normalize_table_name(name: str) -> str:
    """Reduce a fully-qualified table name to the canonical `dataset.table`.

    Model output arrives with decoration that should not count as a miss:
    surrounding whitespace, quotes or backticks, a `bigquery:` prefix, or a
    3-part `project.dataset.table`. Quotes and backticks are stripped from
    each dot-separated part, so `` `p`.`d`.`t` `` reduces the same way as
    `p.d.t`. Everything past that is left alone — comparison stays
    case-sensitive because warehouse table names are.
    """
    name = name.strip().strip("'\"`")
    if name.startswith("bigquery:"):
        name = name[len("bigquery:"):]
    parts = [part.strip().strip("'\"`") for part in name.split(".")]
    if len(parts) == 3:
        parts = parts[1:]
    return ".".join(parts)


def snap_to_catalog(name: str, catalog: set[str]) -> tuple[str, bool]:
    """Snap a near-miss table name onto the closest catalog entry.

    Returns (name, snapped). An empty name or an exact catalog member passes
    through untouched. The catalog is sorted before matching so a similarity
    tie always resolves the same way across runs.
    """
    if not name or name in catalog:
        return name, False
    close = difflib.get_close_matches(name, sorted(catalog), n=1, cutoff=SNAP_CUTOFF)
    return (close[0], True) if close else (name, False)


def _score_one(
    row: RoutingRow, catalog: set[str] | None
) -> RoutingResult:
    """Normalize, optionally snap, then score both tiers for one row."""
    id_, question, raw_gold, raw_pred = row

    gold_table = normalize_table_name(raw_gold)
    if not gold_table:
        raise ValueError(f"row {id_!r}: empty gold_table — fix the answer key")

    pred_table = normalize_table_name(raw_pred)
    snapped = False
    if catalog is not None:
        pred_table, snapped = snap_to_catalog(pred_table, catalog)

    gold_dataset = gold_table.split(".")[0]
    pred_dataset = pred_table.split(".")[0] if pred_table else ""

    return RoutingResult(
        id=id_,
        question=question,
        gold_table=gold_table,
        pred_table=pred_table,
        raw_prediction=raw_pred,
        snapped=snapped,
        dataset_correct=bool(pred_dataset) and pred_dataset == gold_dataset,
        table_correct=pred_table == gold_table,
    )


def evaluate_routing(
    rows: list[RoutingRow],
    catalog: set[str] | None = None,
    confidence: float = 0.95,
) -> RoutingReport:
    """Score every routing prediction and return a RoutingReport.

    Args:
        rows: list of (id, question, gold_table, predicted_table).
        catalog: optional set of valid `dataset.table` names. When given,
            near-miss predictions snap to the closest entry before scoring
            (and the snap count is reported). When None, no snapping.
        confidence: confidence level for both Wilson intervals (default 0.95).

    Raises:
        ValueError: a row's gold_table is empty after normalization. A broken
            answer key fails loudly instead of skewing the rates.
    """
    results = [_score_one(row, catalog) for row in rows]

    n_total = len(results)
    k_dataset = sum(1 for r in results if r.dataset_correct)
    k_table = sum(1 for r in results if r.table_correct)

    return RoutingReport(
        results=results,
        n_total=n_total,
        n_snapped=sum(1 for r in results if r.snapped),
        dataset=proportion(k_dataset, n_total, confidence),
        table=proportion(k_table, n_total, confidence),
    )


@dataclass(frozen=True)
class RoutingSetResult:
    """Scoring one multi-table routing prediction against a gold table SET.

    Enterprise questions rarely route to one table: measured on BEAVER DW,
    120 of 121 questions need 2-7 tables. `covered` is true when every gold
    table was predicted (retrieval recall at the question level) - the metric
    that matters, because one missing table guarantees a wrong join.
    """

    id: str
    question: str
    gold_tables: tuple[str, ...]
    pred_tables: tuple[str, ...]
    covered: bool          # every gold table predicted
    exact: bool            # predicted set == gold set
    n_gold_hit: int        # gold tables that were predicted


@dataclass(frozen=True)
class RoutingSetReport:
    """Multi-table routing rates, each with a Wilson interval.

    `coverage` is the per-question rate of full gold coverage; `exact` is the
    stricter set-equality rate; `table_recall` pools individual gold tables,
    so one big miss and many small ones read differently.
    """

    results: list[RoutingSetResult]
    n_total: int
    coverage: Proportion
    exact: Proportion
    table_recall: Proportion

    def summary(self) -> str:
        lines = [
            f"multi-table routing over {self.n_total} questions",
            f"  full coverage : {self.coverage.point:6.1%}  "
            f"[{self.coverage.low:.1%}, {self.coverage.high:.1%}]"
            "   every gold table predicted",
            f"  exact set     : {self.exact.point:6.1%}  "
            f"[{self.exact.low:.1%}, {self.exact.high:.1%}]",
            f"  table recall  : {self.table_recall.point:6.1%}  "
            f"[{self.table_recall.low:.1%}, {self.table_recall.high:.1%}]"
            "   pooled over every gold table",
            "",
            "questions missing gold tables:",
        ]
        misses = [r for r in self.results if not r.covered]
        for r in misses[:30]:
            missing = sorted(set(r.gold_tables) - set(r.pred_tables))
            lines.append(f"  {r.id}  missing {', '.join(missing)}")
        if len(misses) > 30:
            lines.append(f"  ... and {len(misses) - 30} more")
        if not misses:
            lines.append("  (none)")
        return "\n".join(lines)


def evaluate_routing_sets(
    rows: list[tuple[str, str, str, str]],
    separator: str = "|",
    confidence: float = 0.95,
) -> RoutingSetReport:
    """Grade table-SET predictions against gold table sets.

    Each row is (id, question, gold_tables, pred_tables), both table fields
    separator-joined. Names are normalized like single-table routing; sets
    compare case-insensitively because SQL identifiers are.
    """
    results = []
    gold_hits = gold_total = 0
    for id_, question, raw_gold, raw_pred in rows:
        gold = tuple(sorted({
            normalize_table_name(t).upper()
            for t in raw_gold.split(separator) if t.strip()
        }))
        if not gold:
            raise ValueError(f"row {id_!r}: empty gold_tables — fix the answer key")
        pred = tuple(sorted({
            normalize_table_name(t).upper()
            for t in raw_pred.split(separator) if t.strip()
        }))
        hit = sum(1 for t in gold if t in pred)
        gold_hits += hit
        gold_total += len(gold)
        results.append(RoutingSetResult(
            id=id_, question=question, gold_tables=gold, pred_tables=pred,
            covered=hit == len(gold), exact=gold == pred, n_gold_hit=hit,
        ))
    n = len(results)
    return RoutingSetReport(
        results=results,
        n_total=n,
        coverage=proportion(sum(1 for r in results if r.covered), n, confidence),
        exact=proportion(sum(1 for r in results if r.exact), n, confidence),
        table_recall=proportion(gold_hits, gold_total, confidence),
    )


# The shapes a routing failure takes, in the order a person has to decide about
# them: no documents to route to at all, a near miss the snap explains, the
# retriever in the wrong place, or the right place and the wrong table. The
# first and the third used to be one number, and they are the first decision a
# customer makes - write documents, or fix the retriever.
ROUTING_BUCKET_MEANINGS = {
    "NO_PREDICTION": "the router named nothing at all",
    "GOLD_NOT_IN_CATALOG": "the right table is not in the catalog, so nothing could route to it "
                           "- a coverage problem, fixed by writing documents",
    "SNAPPED_TO_SIBLING": "the router named a table that does not exist and the catalog snap "
                          "moved it to the nearest real one - a near miss",
    "WRONG_DATASET": "the router went to a different dataset - a retrieval problem",
    "WRONG_TABLE_SAME_DATASET": "right dataset, wrong table - the documents do not separate "
                                "neighbours",
}


def triage_routing(report: RoutingReport, catalog: set[str] | None = None) -> list[tuple[str, list[str]]]:
    """Every wrong routing, grouped by what kind of wrong, most common first.

    Without a catalog the coverage bucket cannot be told from a wrong dataset,
    and the report says so rather than guessing.
    """
    buckets: dict[str, list[str]] = {}
    for result in report.results:
        if result.table_correct:
            continue
        if not result.raw_prediction.strip():
            kind = "NO_PREDICTION"
        elif catalog is not None and result.gold_table not in catalog:
            kind = "GOLD_NOT_IN_CATALOG"
        elif result.snapped:
            kind = "SNAPPED_TO_SIBLING"
        elif not result.dataset_correct:
            kind = "WRONG_DATASET"
        else:
            kind = "WRONG_TABLE_SAME_DATASET"
        buckets.setdefault(kind, []).append(result.id)
    return sorted(buckets.items(), key=lambda item: (-len(item[1]), item[0]))


def format_routing_triage(report: RoutingReport, catalog: set[str] | None = None) -> str:
    wrong = [r for r in report.results if not r.table_correct]
    if not wrong:
        return ""
    lines = [f"{len(wrong)} wrong routings by shape:"]
    for kind, ids in triage_routing(report, catalog):
        share = len(ids) / len(wrong)
        lines.append(f"  {kind:26} {len(ids):4} {share:5.0%}  {ROUTING_BUCKET_MEANINGS[kind]}")
    if catalog is None:
        lines.append("  (no --catalog: a table missing from the catalog counts as a wrong "
                     "dataset here; pass the catalog to tell coverage from retrieval)")
    return "\n".join(lines)


def as_eval_report(report: RoutingReport):
    """A routing report in the shape `ablate`, `pool` and `climb` consume.

    Routing is per-question correct-or-not with a different definition of
    correct, and that is all the paired test needs. `score-routing
    --save-report` writes this, so two routing arms compare with the same
    command, the same clustered line and the same fixed/broke count as two SQL
    arms - the arithmetic a field engineer had been doing by hand.
    """
    from .harness import EvalReport, QuestionResult
    results = [
        QuestionResult(
            id=r.id, question=r.question,
            verdict="MATCH" if r.table_correct else "MISMATCH",
            execution_correct=r.table_correct, adjusted_correct=r.table_correct,
            judged=False, judge_verdict="",
            detail=f"gold {r.gold_table}; predicted {r.pred_table}"
                   + (" (snapped)" if r.snapped else ""),
            answer=r.pred_table,
        )
        for r in report.results
    ]
    return EvalReport(results=results, n_total=report.n_total, n_scored=report.n_total,
                      n_excluded=0, execution=report.table, adjusted=report.table)


# --- the answer key itself, before any routing is scored -----------------------

def load_routing_golds(path: str) -> list[tuple[str, str]]:
    """(id, gold_table) from a CSV that has at least those two columns.

    The predictions file works as-is; so does a golds file with no
    pred_table column, because the point is to check the key before a router
    has been run against it.
    """
    import csv
    with open(path, newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        # mine writes the primary table as `table`; the gate reads it as the
        # gold so a review file goes straight in without a renamed column; a
        # gate's clean output has neither, and its gold_sql names the table
        if "gold_table" in fields:
            table_column = "gold_table"
        elif "table" in fields:
            table_column = "table"
        else:
            table_column = "gold_sql"
        missing = [c for c in ("id", table_column) if c not in fields]
        if missing:
            raise SystemExit(f"{path}: missing column(s) {', '.join(missing)} "
                             f"(found: {', '.join(reader.fieldnames or [])})")
        if table_column == "gold_sql":
            from .mine import tables_named
            return [(row["id"], normalize_table_name((tables_named(row["gold_sql"] or "") or [""])[0]))
                    for row in reader]
        return [(row["id"], normalize_table_name(row[table_column] or "")) for row in reader]


def routing_gate(golds: list[tuple[str, str]], catalog: set[str] | None = None) -> dict:
    """What a routing answer key looks like before money is spent on it.

    `gate` audits SQL golds for the shapes that make a score meaningless; a
    routing key had no equivalent, so a bad one was discovered by paying for
    three rounds against it. One pass over two files, no model, no warehouse.

    Concentration is reported, not judged: 49 of 123 questions on one gold
    turned out to be an under-determined table on a real estate, but the
    number that separates "many questions on a big table" from "the questions
    do not determine the table" is a property of the questions, not a constant.
    The line says the share and that it is a lead. Empty golds and duplicate
    ids are defects.
    """
    from collections import Counter
    n = len(golds)
    counts = Counter(g for _, g in golds if g)
    ids = Counter(i for i, _ in golds)
    empty = [i for i, g in golds if not g]
    duplicates = sorted(i for i, k in ids.items() if k > 1)
    singletons = sorted(g for g, k in counts.items() if k == 1)
    uncatalogued = None
    if catalog is not None:
        missing = {g for g in counts if g not in catalog}
        # the catalog it read and how big it was, printed, so a hand-built
        # coverage file that disagrees with the real catalog is visible. One
        # such file drifted the moment the catalog changed and nothing noticed.
        uncatalogued = {"golds": sorted(missing),
                        "questions": sum(counts[g] for g in missing),
                        "catalog_size": len(catalog)}
    return {
        "n_questions": n, "n_golds": len(counts),
        "largest": [{"gold": g, "questions": k, "share": (k / n if n else 0.0)}
                    for g, k in counts.most_common(3)],
        "singleton_golds": singletons,
        "uncatalogued": uncatalogued,
        "empty_gold_ids": empty, "duplicate_ids": duplicates,
        "clean": not empty and not duplicates,
    }


def format_routing_gate(report: dict) -> str:
    lines = [f"routing answer key: {report['n_questions']} questions, {report['n_golds']} golds"]
    largest = ", ".join(f"{e['gold']} {e['questions']} ({e['share']:.0%})" for e in report["largest"])
    # the even share is the comparison, not a threshold: a sentence that
    # printed on every key, including a clean one, was not a warning
    even = f"; an even key would put {1 / report['n_golds']:.0%} on each" if report["n_golds"] else ""
    lines.append(f"  largest golds: {largest or '(none)'}{even}")
    # with the denominator: 5 of 11 on a 23-question key is arithmetic, not a
    # defect, and a bare 5 read as one
    lines.append(f"  golds with a single question: {len(report['singleton_golds'])} "
                 f"of {report['n_golds']}")
    unc = report["uncatalogued"]
    if unc is None:
        lines.append("  golds not in the catalog: pass --catalog to count them (coverage, not retrieval)")
    else:
        # "0 questions on 0 golds" is a template that did not collapse; the
        # catalog size stays because it is what proves the check looked
        count = (f"{unc['questions']} questions on {len(unc['golds'])} golds"
                 if unc["golds"] else "none")
        lines.append(f"  golds not in the catalog: {count}   (coverage, not retrieval; "
                     f"the catalog read holds {unc['catalog_size']} tables)")
    lines.append(f"  empty gold: {len(report['empty_gold_ids'])}    "
                 f"duplicate ids: {len(report['duplicate_ids'])}")
    if not report["clean"]:
        lines.append("  a key with empty golds or duplicate ids cannot be scored honestly; fix it first")
    return "\n".join(lines)
