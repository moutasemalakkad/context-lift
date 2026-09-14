# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Command-line entry point: the stages of an evaluation loop.

    semantic query a BI model    -> the business rules already written down
    mine     query history      -> candidate golds for a human to approve
    gate     a gold set         -> defects, before you trust any score
    split    golds              -> a stable curate/holdout split
    generate questions+context  -> an arm's predictions (arms differ only
                                   by context - the property ablate needs)
    score    predictions        -> accuracy with a confidence interval
    triage   one scored arm     -> what KIND of wrong, so curation has a target
    curate-examples gold pool   -> a leak-checked worked-example bank
    leak     golds + context    -> does the context contain the answers?
    ablate   two scored arms    -> what the context was worth (paired)
    climb    N scored arms      -> what each round of enrichment bought
    attribute two arms + rules  -> which rule broke/fixed which question

Original three commands: `score`, `gate`, `score-routing`.

All commands read CSV files; `score` and `gate` run against a database
(`--db` is a SQLite path or `bq://PROJECT[/DATASET]` for BigQuery),
`score-routing` needs no database at all — so the whole tool works from a
fresh clone with no cloud. On BigQuery every SQL command dry-runs its
statements first and prints the estimate before spending; `--budget` refuses
above it, `--estimate-only` stops there. `score` grades predictions and prints execution +
judge-adjusted accuracy with confidence intervals; `gate` audits a gold set
and exits non-zero if any gold is flagged, so it can guard a CI step;
`score-routing` grades table-routing predictions (which table should answer
the question) with the same rate-plus-interval reporting. The LLM judge stays
opt-in (--judge-gemini) to keep the default path cloud-free.

CSV formats:
- predictions:         columns id, question, gold_sql, pred_sql
- golds:               columns id, gold_sql
- routing predictions: columns id, question, gold_table, pred_table
- catalog (routing):   plain text, one valid dataset.table per line,
                       blank lines and #-comments skipped
- questions (generate, curate-examples, leak): columns id, question, gold_sql
- example bank (curate-examples -> generate --examples): JSON {id: text}
- saved report (score --save-report -> ablate/climb --*-report): JSON
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import dataclasses
import datetime as dt
import json
import os
import pathlib
import shutil
import sys
import time

from .confirm import confirm_paid
from .ablate import ablate, climb, pool
from .attribute import attribute, count_rule_lines_without_identifier, parse_rules
from .backends import CredentialsMissing, estimate_cost_usd, price_table
from .backends_db import (DEFAULT_MAX_BYTES, NEEDS_DATASET, dataset_names, datasets_to_read,
                          demand_sql, is_bigquery, make_executor, parse_db_spec, query_log_sql,
                          describe_tables, schema_text, write_demand, write_query_log)
from .executor import (BIGQUERY_PRICE_AS_OF, BIGQUERY_USD_PER_TIB,
                       CREDENTIAL_REFUSAL_ADVICE, PLATFORM_REFUSAL_ADVICE,
                       bigquery_usd, is_read_only, not_found_in, parse_bytes)
from .examples import build_example_bank, save_bank
from .gate import gate, summarize, summarize_warnings
from .generate import estimate_run, generate_predictions, write_predictions
from .harness import (VERDICT_MEANINGS, subset, EvalReport, evaluate, load_report, rows_needing_judge,
                      save_report)
from .leakage import SPLIT_SEED, check_leakage, split_golds
from . import identity
from .mine import MinedQuery, PromptQuestionWriter, mine_candidates
from .mine import summarize as summarize_candidates
from .mine import write_candidates
from .mine import CANDIDATE_COLUMNS, candidate_row
from .routing import (RoutingReport, evaluate_routing, evaluate_routing_sets,
                      normalize_table_name)
from .triage import triage


def spend_line(usage: dict, spent: float | None, label: str = "spent") -> str:
    """One spend line, for every command that pays a model.

    There were five copies of this format string and a cached-token count
    belonged in all of them, which is how a patch lands in the wrong one.

    `cached_tokens` is a SUBSET of `prompt_tokens` and is priced at the full
    rate in the figure shown, because the reference says only that cost
    savings are passed on and gives no multiplier. Reporting a discount this
    tool cannot verify would let it under-state a real invoice; reporting the
    COUNT makes the saving measurable.

    Why it earns a line at all: implicit caching needs 2,048 prompt tokens on
    Gemini 2.5 Flash, so an arm that sends one shared context to every
    question sits above the floor while an arm that sends a short
    per-question snippet sits below it. The cache favours the bigger arm, and
    a cost comparison between the two is wrong without this number.
    """
    price = f"${spent:.4f}" if spent is not None else "cost unknown (no price)"
    line = (f"{label}: {usage['calls']} calls, {usage['prompt_tokens']:,} prompt "
            f"+ {usage['output_tokens']:,} output "
            f"+ {usage.get('thought_tokens', 0):,} thinking tokens, {price}")
    cached = usage.get("cached_tokens") or 0
    if cached:
        share = cached / max(usage["prompt_tokens"], 1)
        line += (f"\n  of those, {cached:,} prompt tokens ({share:.0%}) came "
                 f"from the model's implicit cache and are billed below the "
                 f"rate used above")
    return line


def _require_file(path: str, what: str) -> None:
    """A missing input is a one-line message, never a traceback.

    The common cause is a path typed for a file that lives under another
    name (rules.md vs context_r2.md); the message names both the role and
    the path so the fix is obvious.
    """
    if not pathlib.Path(path).is_file():
        raise SystemExit(f"{what} not found: {path}")


def _require_columns(path: str, reader: csv.DictReader, needed: tuple[str, ...],
                     hint: str) -> None:
    """Fail with a sentence, not a KeyError traceback, on the wrong file shape.

    The mistakes this catches are file-swap mistakes (a golds file where
    predictions belong), and the fix is always "point at the other file" -
    so the message says what was found, what was needed, and the likely mix-up.
    """
    found = tuple(reader.fieldnames or ())
    missing = [c for c in needed if c not in found]
    if missing:
        raise SystemExit(
            f"{path}: missing column(s) {', '.join(missing)} "
            f"(found: {', '.join(found) or 'none'}). {hint}"
        )


def _load_predictions(path: str) -> list[tuple[str, str, str, str]]:
    """Read a predictions CSV into (id, question, gold_sql, pred_sql) tuples."""
    with open(path, newline="") as handle:
        reader = csv.DictReader(handle)
        _require_columns(path, reader, ("id", "question", "gold_sql", "pred_sql"),
                         "This looks like a golds/questions file - predictions "
                         "come from `contextlift generate`.")
        return [
            (row["id"], row["question"], row["gold_sql"], row["pred_sql"])
            for row in reader
        ]


def _load_questions(path: str) -> dict[str, str]:
    """Gold id to question text, for the golds CSV that carries one.

    Empty when the file has no `question` column. The gate's MISSING_LITERAL
    warning needs it and silently cannot run without it.
    """
    with open(path, newline="") as handle:
        reader = csv.DictReader(handle)
        if "question" not in (reader.fieldnames or []):
            return {}
        return {row["id"]: row.get("question") or "" for row in reader}


def _load_golds(path: str) -> list[tuple[str, str]]:
    """Read a golds CSV into (id, gold_sql) tuples."""
    with open(path, newline="") as handle:
        return [(row["id"], row["gold_sql"]) for row in csv.DictReader(handle)]


def _load_routing_predictions(path: str) -> list[tuple[str, str, str, str]]:
    """Read a routing CSV into (id, question, gold_table, pred_table) tuples.

    A gold_table column wins; without one the gold is the primary table of
    gold_sql, so a gate's clean output routes without a hand-added column."""
    from .mine import tables_named
    with open(path, newline="") as handle:
        rows = list(csv.DictReader(handle))

    def gold_of(row: dict) -> str:
        if row.get("gold_table"):
            return row["gold_table"]
        named = tables_named(row.get("gold_sql", "")) if row.get("gold_sql") else []
        return named[0] if named else ""

    return [(row["id"], row.get("question", ""), gold_of(row), row.get("pred_table", ""))
            for row in rows]


def _load_catalog(path: str) -> set[str]:
    """Read a catalog file: one valid dataset.table per line.

    Blank lines and #-comments are skipped; each entry is normalized the same
    way as the predictions, so a 3-part or quoted catalog still matches.
    """
    with open(path) as handle:
        return {
            normalize_table_name(line)
            for line in handle
            if line.strip() and not line.lstrip().startswith("#")
        }


def _read_table_list(path: str) -> list[str]:
    """A `--tables-file`: one `dataset.table` per line, the file catalog-list
    writes; blank lines and #-comments skipped. Names are kept as written,
    in order, because BigQuery table names are case-sensitive and the
    datasets are read in the order the file names them."""
    _require_file(path, "--tables-file")
    with open(path) as handle:
        return [line.strip().strip("`") for line in handle
                if line.strip() and not line.lstrip().startswith("#")]


def _tables_scope(args: argparse.Namespace) -> tuple[list[str], list[str] | None]:
    """What `--tables`, `--tables-file` and `--datasets` ask a metadata read
    for: the table names (bare or `dataset.table`) and the datasets in order.
    A tables file with no `--datasets` sets the datasets from its own
    prefixes, so the file catalog-list writes is the whole scope by itself."""
    tables = [t.strip() for t in (getattr(args, "tables", None) or "").split(",") if t.strip()]
    listed: list[str] = []
    if getattr(args, "tables_file", None):
        listed = _read_table_list(args.tables_file)
        tables += listed
    datasets = [d.strip() for d in (args.datasets or "").split(",") if d.strip()]
    if not datasets:
        datasets = list(dict.fromkeys(t.rpartition(".")[0] for t in listed if "." in t))
    return tables, datasets or None


def _metadata_scope(args: argparse.Namespace, executor, command: str,
                    views: int) -> tuple[list[str], list[str] | None, int] | int:
    """The scope of a metadata command and, on a warehouse, the cost line
    before the first query: how many INFORMATION_SCHEMA views over how many
    datasets. A project-only `--db` with no scope is refused here in one
    line, before any client is made to wait. Returns an exit code instead
    of the scope when the command must stop."""
    tables, datasets = _tables_scope(args)
    if not is_bigquery(args.db):
        return tables, datasets, 1
    if not parse_db_spec(args.db).dataset and not datasets:
        print(NEEDS_DATASET.format(command=command))
        return 2
    read = datasets_to_read(executor, tables, datasets, command=command)
    print(f"reading {views} INFORMATION_SCHEMA views x {len(read)} datasets = "
          f"{views * len(read)} metadata queries")
    return tables, datasets, len(read)


def _across(n_datasets: int) -> str:
    return f" across {n_datasets} datasets" if n_datasets > 1 else ""


def _mark(result) -> str:
    """One-word correctness tag for a per-question line."""
    if result.execution_correct is None:
        return "excluded"
    if result.execution_correct:
        return "correct"
    if result.adjusted_correct:
        return "wrong -> upgraded"  # judge overrode the exact comparator
    return "wrong"


def format_score_report(report: EvalReport, confidence: float = 0.95) -> str:
    """Render an EvalReport as text: both rates with CIs, then a per-question list."""
    pct = round(confidence * 100)
    execution = report.execution
    adjusted = report.adjusted
    lines = [
        f"execution accuracy: {execution.point:.3f}  "
        f"{pct}% CI [{execution.low:.3f}, {execution.high:.3f}]  "
        f"({execution.k}/{execution.n})",
        # both terms are the first jargon anyone meets, on the first line of
        # the first report. An analyst sitting beside the engineer knows
        # neither, and nothing else in the report explains them
        f"  execution accuracy: the share of questions whose answer returned the "
        f"same rows as the answer key ({execution.k} of {execution.n})",
        f"  {pct}% CI: the range the true share is probably in, given how few "
        f"questions there are. It narrows as the answer key grows",
    ]
    # only meaningful when a judge reviewed disagreements; printing an
    # identical judge-adjusted line with no judge configured reads as if
    # one ran and agreed with everything
    judged = [r for r in report.results if r.judged]
    wrong = [r for r in report.results if r.execution_correct is False]
    if judged:
        upgraded = sum(1 for r in judged if r.adjusted_correct and not r.execution_correct)
        lines.append(
        f"judge-adjusted:     {adjusted.point:.3f}  "
        f"{pct}% CI [{adjusted.low:.3f}, {adjusted.high:.3f}]  "
        f"({adjusted.k}/{adjusted.n})")
        lines.append(f"judge: reviewed {len(judged)} disagreements, upgraded {upgraded}")
    else:
        # said in prose because a field engineer read "wrong" as exact
        # result-set mismatch with nothing unreviewed, and reported 0 of 18
        # where a judge would have read 7
        lines.append(f"judge: not run; {len(wrong)} wrong answers were not reviewed "
                     "(score --judge-gemini reviews them; it can only raise the score)")
    lines += [
        f"scored {report.n_scored} of {report.n_total}  "
        f"({report.n_excluded} excluded)",
        "",
        "per-question:",
    ]
    id_width = max((len(r.id) for r in report.results), default=2)
    verdict_width = max((len(r.verdict) for r in report.results), default=10)
    for result in report.results:
        lines.append(f"  {result.id:<{id_width}}  {result.verdict:<{verdict_width}}  "
                     f"{_mark(result):<8}  {VERDICT_MEANINGS.get(result.verdict, '')}")
    return "\n".join(lines)


def _to_json(report) -> str:
    """A report dataclass as JSON, for pipelines and UIs.

    Everything the human summary prints is derivable from this, so a tool
    reading --json output never parses the prose report.
    """
    if isinstance(report, list):
        payload = [dataclasses.asdict(item) for item in report]
    else:
        payload = dataclasses.asdict(report)
    return json.dumps(payload, indent=2, default=str)


def _emit_json(args: argparse.Namespace, report) -> bool:
    """Persist and/or print a report as JSON, per --save-report and --json.

    Returns True when JSON went to stdout, so the handler skips its prose.
    A score report is saved in the harness format (results only, rates
    recomputed on load); every other report is saved as its --json payload.
    """
    path = getattr(args, "save_report", None)
    if path:
        if isinstance(report, EvalReport):
            save_report(report, path)
        else:
            pathlib.Path(path).write_text(_to_json(report))
        # stdout must stay pure JSON under --json, so the note moves to stderr
        print(f"report -> {path}", file=sys.stderr if args.json else sys.stdout)
    if args.json:
        print(_to_json(report))
        return True
    return False


def format_gate_report(findings) -> str:
    """Render gate findings as text: a tally line, then the flagged golds."""
    clean = sum(1 for f in findings if f.ok)
    flagged = [f for f in findings if not f.ok]
    lines = [
        f"gate: {len(findings)} golds, {clean} clean, {len(flagged)} flagged"
    ]
    counts = summarize(findings)
    if counts:
        tally = "  ".join(f"{name}={n}" for name, n in sorted(counts.items()))
        lines.append(f"defects: {tally}")
    warned = [f for f in findings if f.warnings and f.ok]
    if warned:
        # only the kept ones: a gold flagged for a real defect carries its
        # warning in the detail, and counting it here would say two golds were
        # kept when one was not
        tally = "  ".join(f"{name}={n}" for name, n in sorted(
            summarize_warnings(warned).items()))
        lines.append(f"warnings: {tally}  (kept in the clean set; read them, "
                     "they do not drop a gold)")
    if flagged:
        # Pad the id to the widest flagged id so the defect column lines up.
        id_width = max(len(f.gold_id) for f in flagged)
        lines.append("")
        lines.append("flagged:")
        for finding in flagged:
            defects = ",".join(finding.defects)
            lines.append(
                f"  {finding.gold_id:<{id_width}}  {defects:<20}  {finding.detail}"
            )
    if warned:
        # a warning nobody reads is a warning that does nothing; the reason is
        # printed here for the same reason a defect's is
        id_width = max(len(f.gold_id) for f in warned)
        lines.append("")
        lines.append("kept, with a warning:")
        for finding in warned:
            names = ",".join(finding.warnings)
            lines.append(
                f"  {finding.gold_id:<{id_width}}  {names:<20}  {finding.detail}"
            )
    return "\n".join(lines)


def _routing_mark(result) -> str:
    """One-phrase correctness tag for a per-question routing line."""
    if result.table_correct:
        return "correct"
    if result.dataset_correct:
        return "dataset only"  # right dataset, wrong table
    return "wrong"


def format_routing_report(
    report: RoutingReport, confidence: float = 0.95, catalog_used: bool = False
) -> str:
    """Render a RoutingReport as text: both rates with CIs, then per-question.

    The snap line is printed whenever a catalog was in play — including
    "snapped 0" — and never without one, so its absence means snapping was
    off, not that nothing snapped.
    """
    pct = round(confidence * 100)
    dataset = report.dataset
    table = report.table
    lines = [
        f"dataset accuracy: {dataset.point:.3f}  "
        f"{pct}% CI [{dataset.low:.3f}, {dataset.high:.3f}]  "
        f"({dataset.k}/{dataset.n})",
        f"table accuracy:   {table.point:.3f}  "
        f"{pct}% CI [{table.low:.3f}, {table.high:.3f}]  "
        f"({table.k}/{table.n})",
    ]
    if catalog_used:
        lines.append(
            f"snapped {report.n_snapped} of {report.n_total} predictions "
            "to the catalog"
        )
    lines += ["", "per-question:"]
    for result in report.results:
        shown = result.pred_table or "(empty)"
        if result.snapped:
            shown = f"{normalize_table_name(result.raw_prediction)} -> {shown}"
        tag = _routing_mark(result)
        if result.snapped:
            tag = f"snapped, {tag}"
        lines.append(f"  {result.id}  {shown:<40}  {tag}")
    return "\n".join(lines)


BIGQUERY_DEFAULT_TIMEOUT = 60.0
# The same bound on a local database. Predicted SQL is untrusted model
# output there too: one generated cross join hung a scoring run of 12
# questions for ten minutes with nothing printed, and the run had to be
# killed. `--timeout 0` removes the bound for a query that is genuinely
# slow.
SQLITE_DEFAULT_TIMEOUT = 60.0


def _executor_for(args: argparse.Namespace, row_cap: int | None = None,
                  use_query_cache: bool = True):
    """The executor `--db` names: SQLite for a path, BigQuery for bq://.

    On BigQuery an unset --timeout becomes 60 s and says so once. Predicted
    SQL is untrusted model output; on a warehouse a query that never
    finishes holds a worker and bills while it waits, and 60 s is the bound
    every walkthrough asked for by hand. Commands with no --timeout flag
    (schema, profile) keep no bound: their statements are the tool's own.
    """
    timeout = getattr(args, "timeout", None)
    if timeout == 0:
        timeout = args.timeout = None            # asked for, on purpose: no bound
    elif timeout is None and hasattr(args, "timeout"):
        on_warehouse = is_bigquery(args.db)
        timeout = BIGQUERY_DEFAULT_TIMEOUT if on_warehouse else SQLITE_DEFAULT_TIMEOUT
        args.timeout = timeout
        where = ("BigQuery (job_timeout_ms is set too)" if on_warehouse
                 else "this database")
        print(f"--timeout unset: each statement is bounded at {timeout:g} s on {where}; "
              "pass --timeout to change it, or --timeout 0 for no bound", file=sys.stderr)
    try:
        return make_executor(
            args.db, timeout_seconds=timeout, row_cap=row_cap,
            maximum_bytes_billed=getattr(args, "max_bytes", None) or DEFAULT_MAX_BYTES,
            use_query_cache=use_query_cache)
    except ImportError:
        raise SystemExit(f"--db {args.db}: the [bigquery] extra is not installed in "
                         f"this Python ({sys.executable}): uv pip install -e '.[bigquery]'")


def _resolve_workers(args: argparse.Namespace) -> int:
    """--workers as given, else 1 on SQLite and 4 on BigQuery. Written back
    to args so telemetry records what ran, not the unset flag.

    Four, not more: each BigQuery call is mostly network wait (about 3 s
    through a proxy for 0.2 s of server time), so four threads cut a run
    by about four; past that the warehouse's per-project concurrency and
    the proxy start to push back, and a scoring run should not be the
    thing that finds that limit."""
    workers = getattr(args, "workers", None)
    if workers is None:
        workers = 4 if is_bigquery(getattr(args, "db", None)) else 1
    args.workers = max(1, int(workers))
    return args.workers


def _format_sql_estimate(estimate: dict) -> str:
    # a query the engine could not parse and a call an org policy refused are
    # two different problems for two different people, counted apart
    refused = estimate.get("refused") or 0
    at_dry_run = (estimate.get("failed") or 0) - refused
    failed = f", {at_dry_run} rejected at dry-run" if at_dry_run else ""
    if refused:
        failed += f", {refused} refused by the platform"
    if estimate.get("not_found"):
        failed += f", {estimate['not_found']} not found in the billing project"
    # the GB is what the dry-runs measured; the price leaves out what the
    # ceiling turns away, so the line says so or the two figures disagree
    over = estimate.get("over_ceiling") or {}
    held_back = (f"; {over['count']} over the {estimate['ceiling_bytes'] / 1e9:g} GB "
                 "ceiling, not priced" if over.get("count") else "")
    reused = (f"; {estimate['reused']} answers reused from the gold cache, not dry-run"
              if estimate.get("reused") else "")
    return (f"estimated: {estimate['queries']} queries, "
            f"{estimate['bytes'] / 1e9:.3f} GB (dry-run{failed}{held_back}), "
            f"~${estimate['usd']:.4f} on-demand "
            f"(BigQuery ${BIGQUERY_USD_PER_TIB}/TiB as of {BIGQUERY_PRICE_AS_OF}, "
            f"10 MB minimum per query){reused}")


def _print_not_found_fix(db: str, errors: list[str], what: str) -> None:
    """When the billing project is not the data project, a 2-part table name
    resolves in the billing project and fails there with "not found". Said
    once with the fix, instead of N EXEC_FAIL rows to decode: on one estate
    every gold failed this way and the SQL was fine."""
    if not is_bigquery(db):
        return
    spec = parse_db_spec(db)
    data_project = (spec.dataset or "").partition(".")[0]
    if not data_project or data_project == spec.project:
        return
    n = sum(1 for error in errors if not_found_in(error, spec.project))
    if n:
        print(f"  {n} {what} failed with 'not found' in project {spec.project}: a 2-part name "
              f"resolves there, not in {data_project}; write {data_project}.dataset.table, "
              f"or run with --db bq://{data_project}/...")


# One model call, measured. Stated as a range because it is not a constant
# and a single number would be read as one: about 2 s on a laptop, 5.8 s
# behind a corporate proxy, and 11.1 s on the run that measured it end to end
# (593 calls in 109.8 minutes, mine --write-questions on an enterprise
# warehouse). The top of the range is that measurement and not a guess; the
# first version of this said 2 to 6 and was wrong by half.
MODEL_CALL_SECONDS = (2.0, 12.0)


def _format_sql_time(estimate: dict, model_calls: int = 0) -> str:
    """The second line of an estimate: how long. On one estate a 164-question
    three-rep arm took about 50 minutes to score and nothing had said so
    beforehand.

    `model_calls` is how many model calls the stage also makes, and it is not
    a detail. `mine --write-questions` drafts one question per shape, and on
    one estate that was 437 calls and 42 minutes while the queries took under
    four: the line printed "about 2.4 minutes", the run took 42, and the plan
    built on 2.4 was wrong by a factor of seventeen.
    """
    if not estimate.get("calls") and not model_calls:
        return ("time: nothing to run; every statement is reused from the gold cache, "
                "rejected at dry-run, or over the ceiling")
    if estimate.get("calls"):
        text = (f"time: {estimate['calls']} queries at {estimate['seconds_per_call']:.1f} s "
                f"each, about {estimate['expected_minutes']:.1f} minutes at --workers "
                f"{estimate['workers']} (dry-runs went through the same proxy; a real run "
                "adds the server's own time)")
    else:
        text = "time: no query to run"
    if model_calls:
        low, high = (model_calls * MODEL_CALL_SECONDS[0] / 60,
                     model_calls * MODEL_CALL_SECONDS[1] / 60)
        text += (
            f"\n      plus {model_calls} model calls to draft the questions, one per shape, "
            f"and THOSE are the wall clock here: at {MODEL_CALL_SECONDS[0]:g} to "
            f"{MODEL_CALL_SECONDS[1]:g} s each that is {low:.0f} to {high:.0f} minutes, "
            "and a question whose draft leaves out a value its gold filters on is drafted "
            "again. They run one at a time. Measured end to end: 593 calls took 110 "
            "minutes while the 283 queries took under 4")
    return text


def _reconcile(estimate: dict, sqls: list[str], to_price: list[str], reused: frozenset,
               unit_sqls: list[str], noun: str, shared_as: str) -> dict:
    """Split the run's own units into what becomes of each of them.

    A reader could not get from "118 predictions" to the estimate's numbers:
    117 statements dry-run, 116 queries, 3 rejected, 117 answers reused, 113
    calls. These four counts add up to the unit total, and `to_run` plus
    `beside` is the call count the time line prints.
    """
    will_run = {sql for sql, record in zip(to_price, estimate["statements"])
                if not record["failed"] and not record["over_ceiling"]}
    refused = {sql for sql, record in zip(to_price, estimate["statements"]) if record["refused"]}
    # the other statement of each unit (a prediction's gold) runs anyway, and
    # so does nothing the gold cache already answered, so a unit whose SQL is
    # one of those is not a call of its own
    counted = (set(sqls) - set(unit_sqls)) | set(reused)
    counts = {"unit": noun, "shared_as": shared_as, "total": len(unit_sqls),
              "not_read_only": 0, "rejected": 0, "refused": 0, "shared": 0, "to_run": 0}
    for sql in unit_sqls:
        if not is_read_only(sql):
            counts["not_read_only"] += 1
        elif sql in counted:
            counts["shared"] += 1
        elif sql in will_run:
            counts["to_run"] += 1
            counted.add(sql)
        elif sql in refused:
            # the platform turned the call away; the SQL was never read
            counts["refused"] += 1
        else:
            counts["rejected"] += 1
    counts["beside"] = len(will_run) - counts["to_run"]
    return counts


def _format_reconciliation(counts: dict) -> str:
    beside = (f"; {counts['beside']} more statement{'s' if counts['beside'] != 1 else ''} "
              "to run beside them" if counts["beside"] else "")
    # a 403 is not a parse error, here as everywhere else
    refused = (f"{counts['refused']} refused by the platform, " if counts.get("refused") else "")
    return (f"{counts['total']} {counts['unit']}: {counts['not_read_only']} not read-only, "
            f"{counts['rejected']} rejected at dry-run, {refused}{counts['shared']} identical to "
            f"{counts['shared_as']}, {counts['to_run']} to run{beside}")


def _cost_before_sql(args: argparse.Namespace, executor, sqls: list[str],
                     repeat: int = 1, reused: frozenset = frozenset(),
                     workers: int = 1, labels: list[str] | None = None,
                     notes: list[str] = (), rejected_note: str = "",
                     unit_sqls: list[str] | None = None,
                     unit: tuple[str, str] = ("statements", "another statement")) -> int | None:
    """Cost before spending, on a warehouse: dry-run every distinct statement,
    print the estimate, honor --budget and --estimate-only. Returns an exit
    code when the command must stop here, None to go on. SQLite prints
    nothing (a local run is free), so its output stays exactly as before.

    The price covers only what will run. On one estate the estimate said
    $0.39 for 25 golds when the 1 GB ceiling then rejected 14 of them for
    free, and --budget refused an arm at $2.20 whose own ceiling capped
    it at $1.12.

    `reused` is the SQL the run will answer from the gold cache: not
    dry-run, not priced, not timed. `repeat` is how many times each
    statement runs (gate --check-stability runs each gold twice).
    `labels` name the statements in `sqls`, one per entry (a gold id, a
    question id), for the line that lists what the dry-run rejected;
    without them the SQL's first 60 characters stand in. `notes` are
    lines the caller adds under the estimate (what a checkpoint or the
    gold cache already holds); they go into the JSON too. `rejected_note`
    ends the over-ceiling line (gate says what becomes of a rejected gold).
    `unit_sqls` is one statement per thing the command was given (one per
    prediction row) and `unit` names them, for the line that adds up from
    that total to the calls the time line prints."""
    estimate_fn = getattr(executor, "estimate", None)
    if estimate_fn is None:
        if getattr(args, "estimate_only", False):
            print("no estimate: a SQLite run costs nothing")
            return 0
        return None
    distinct = list(dict.fromkeys(sqls))
    to_price = [sql for sql in distinct if sql not in reused]
    print(f"dry-running {len(to_price)} statements before spending (free)...",
          file=sys.stderr, flush=True)
    started = time.monotonic()
    estimate = estimate_fn(to_price, workers=workers)
    elapsed = time.monotonic() - started
    if repeat > 1:
        for key in ("bytes", "billable_bytes"):
            estimate[key] *= repeat
        estimate["usd"] = round(bigquery_usd(estimate["billable_bytes"]), 6)
        estimate["repeat"] = repeat
    estimate["reused"] = len(distinct) - len(to_price)
    estimate["notes"] = list(notes)
    over = estimate["over_ceiling"]
    if unit_sqls is not None:
        estimate["reconciliation"] = _reconcile(estimate, sqls, to_price, reused,
                                                unit_sqls, *unit)
    # per call: the dry-runs ran `workers` at a time, so the wall time
    # covers about queries / workers calls in sequence; a statement the
    # ceiling or the dry-run rejects is no call at all
    dry_runs = estimate["queries"]
    seconds_per_call = elapsed * min(workers, dry_runs) / dry_runs if dry_runs else 0.0
    estimate["calls"] = (dry_runs - estimate["failed"] - over["count"]) * repeat
    estimate["seconds_per_call"] = round(seconds_per_call, 2)
    estimate["workers"] = workers
    estimate["expected_minutes"] = round(
        estimate["calls"] * seconds_per_call / workers / 60, 2)
    # the cost sheet reads these two, the same keys as generate's
    estimate["estimated_cost_usd"] = estimate["usd"]
    estimate["price_table"] = {"model": "BigQuery on-demand",
                               "usd_per_tib": BIGQUERY_USD_PER_TIB,
                               "as_of": BIGQUERY_PRICE_AS_OF}
    args.sql_estimate = estimate
    if getattr(args, "estimate_only", False) and getattr(args, "json", False):
        print(json.dumps(estimate, indent=2))
    else:
        print(_format_sql_estimate(estimate))
        if estimate.get("reconciliation"):
            print(_format_reconciliation(estimate["reconciliation"]))
        # `mine --write-questions` drafts one per ranked shape, which is what
        # was dry-run here. Any other stage passes none and the line is
        # exactly what it was.
        print(_format_sql_time(
            estimate,
            model_calls=dry_runs if getattr(args, "write_questions", False) else 0))
        if estimate["calls"]:
            # the dry run is not a promise. On one gate run it passed 45
            # statements under the 10 GB ceiling, 41 ran and BigQuery killed
            # 4 at the cap; someone who read "45" sized a key at 45 and got 41
            print(f"up to {estimate['calls']} statements will run: the dry run measures what "
                  "each one reads now, and BigQuery still kills a job at --max-bytes when "
                  "the real scan comes in over it (the killed jobs are free). The spend "
                  "line at the end says how many ran")
        if over["count"]:
            print(f"{over['count']} of {dry_runs} statements are over the --max-bytes ceiling "
                  f"({estimate['ceiling_bytes'] / 1e9:g} GB) and will be rejected without "
                  f"running (largest {over['largest_bytes'] / 1e9:.2f} GB); raise --max-bytes "
                  "or narrow them" + (f"; {rejected_note}" if rejected_note else ""))
        label_of: dict[str, str] = {}
        for sql, label in zip(sqls, labels or ()):
            label_of.setdefault(sql, label)
        # three reasons a statement will not run, and they need three
        # different people: not read-only is turned away here before any
        # dry-run, a parse error is the query's author's, and a platform
        # refusal (a 403 from VPC Service Controls) is the security team's
        rejected = [((label_of.get(sql) or " ".join(sql.split())[:60])
                     + ("" if is_read_only(sql) else " (not read-only)")
                     + (" (the platform refused the call)" if record["refused"] else ""))
                    for sql, record in zip(to_price, estimate["statements"])
                    if record["failed"]]
        if rejected:
            not_read_only = sum(1 for name in rejected if name.endswith(" (not read-only)"))
            platform = estimate.get("refused") or 0
            no_cred = estimate.get("no_credential") or 0
            at_dry_run = len(rejected) - not_read_only - platform - no_cred
            counts = [f"{at_dry_run} the SQL does not parse",
                      f"{platform} the platform refused the call",
                      f"{not_read_only} not read-only"]
            # Only when it happened: a zero here reads as a category the
            # reader has to rule out, and the other two are always relevant.
            if no_cred:
                counts.insert(0, f"{no_cred} could not authenticate")
            print(f"{len(rejected)} statements will not run and cost nothing "
                  f"({', '.join(counts)}):")
            for name in rejected[:5]:
                print(f"  {name}")
            if len(rejected) > 5:
                print(f"  and {len(rejected) - 5} more")
            if no_cred:
                # Printed FIRST and before the 403 note. When no credential
                # could be minted, nothing else in this list is the reason,
                # and 285 of 286 statements once printed as unparseable SQL
                # on queries that had run correctly two days earlier.
                print("  " + CREDENTIAL_REFUSAL_ADVICE)
            if platform:
                print("  a refused call is not a query defect: a 403 from VPC Service Controls "
                      "or a missing permission stops the call whatever the SQL says; "
                      + PLATFORM_REFUSAL_ADVICE
                      + ". The other statements in this run went through")
        for note in notes:
            print(note)
    if getattr(args, "estimate_only", False):
        return 0
    budget = getattr(args, "budget", None)
    if budget is not None and estimate["usd"] > budget:
        print(f"refusing to start: estimate ${estimate['usd']:.4f} is above the "
              f"budget ${budget:.4f}")
        return 2
    return None


def _record_sql_spend(args: argparse.Namespace, executor) -> None:
    """What the warehouse billed and how long it took, printed and kept for
    the run manifest.

    With the slip beside it: the pre-pass counts statements the dry run
    passed, and BigQuery still kills a job at the byte cap when the real
    scan comes in above it. One gate run expected 45, ran 41 and lost 4 that
    way; the person who read 45 sized an answer key at 45."""
    usage = getattr(executor, "usage", None)
    if usage is None:
        return
    spent = round(bigquery_usd(usage["bytes_billed"]), 6)
    elapsed = float(usage.get("elapsed_seconds") or 0.0)
    args.telemetry = {**usage, "usd": spent, "elapsed_seconds": elapsed,
                      "workers": getattr(args, "workers", None) or 1,
                      "estimate": getattr(args, "sql_estimate", None)}
    print(f"spent: {usage['queries']} queries, {usage['bytes_billed'] / 1e9:.3f} GB "
          f"billed, ~${spent:.4f} on-demand, {elapsed / 60:.1f} minutes")
    expected = (getattr(args, "sql_estimate", None) or {}).get("calls")
    if expected is not None:
        cap = getattr(executor, "maximum_bytes_billed", None)
        ceiling = f" {cap / 1e9:g} GB" if cap else ""
        print(f"pre-pass: up to {expected} statements were expected to run, "
              f"{usage['queries']} ran, {int(usage.get('ceiling_kills') or 0)} killed at "
              f"the{ceiling} --max-bytes ceiling (killed jobs are free)")


def _checkpoint_ids(path: str | None) -> set[str]:
    """Question ids a checkpoint already holds, read the way evaluate()
    reads them: those are not executed, so they are not priced either."""
    if not path or not pathlib.Path(path).is_file():
        return set()
    return {item["id"] for item in json.loads(pathlib.Path(path).read_text())["results"]}


def _record_judge_spend(args: argparse.Namespace, backend) -> None:
    """What the judge's model calls cost, printed and kept under "judge" in
    the run manifest, beside the warehouse figures _record_sql_spend set.
    A field run's manifest had the BigQuery bill only; twelve judge calls
    were nowhere."""
    usage = dict(backend.usage)
    spent = estimate_cost_usd(backend.prices, usage["prompt_tokens"],
                              usage["output_tokens"], usage["thought_tokens"])
    args.telemetry = {**(getattr(args, "telemetry", None) or {}),
                      "judge": {**usage, "estimated_cost_usd": spent,
                                "price_table": backend.prices}}
    print(spend_line(usage, spent, "judge spent"))


def _sqls_of_predictions(paths: list[str]) -> list[str]:
    return [sql for path in paths for row in _load_predictions(path)
            for sql in (row[2], row[3])]


def _cmd_score(args: argparse.Namespace) -> int:
    executor = _executor_for(args, args.row_cap)
    workers = _resolve_workers(args)
    predictions = _load_predictions(args.predictions)
    # A resumed run skips what the checkpoint holds, so the estimate does too.
    # The judge is the exception: the checkpoint holds verdicts, not result
    # rows, so an unjudged disagreement has its gold and its prediction run
    # again. Those rows used to be priced at $0 and --budget passed at $0
    # while the run went on to execute them.
    done = _checkpoint_ids(getattr(args, "checkpoint", None))
    for_judge = (rows_needing_judge(args.checkpoint)
                 if args.judge_gemini and getattr(args, "checkpoint", None) else [])
    done -= set(for_judge)
    to_run = [row for row in predictions if row[0] not in done]
    notes = ([f"{len(predictions) - len(to_run)} questions already in the checkpoint, not priced"]
             if len(to_run) < len(predictions) else [])
    # said before the estimate, so --estimate-only shows it and --budget
    # decides with these queries and these calls counted
    if args.judge_gemini:
        if for_judge:
            print(f"judge: {len(for_judge)} checkpointed disagreements will be re-executed "
                  "for the judge (the checkpoint holds verdicts, not result rows); their "
                  "SQL is in the estimate below")
        print(f"judge: up to {len(to_run)} calls to {args.judge_model}, one per answer the "
              "comparator marks wrong; the model's own price is not in the estimate below")
    # the cache is opened before pricing: an answer it holds is neither
    # dry-run nor counted, and the estimate says how many those were
    gold_cache, save_gold_cache = _open_gold_cache(getattr(args, "gold_cache", None),
                                                   getattr(args, "db", None))
    stop = _cost_before_sql(args, executor, [sql for row in to_run for sql in (row[2], row[3])],
                            labels=[f"{row[0]} {kind}" for row in to_run
                                    for kind in ("gold", "prediction")],
                            reused=frozenset(gold_cache or ()), workers=workers, notes=notes,
                            unit_sqls=[row[3] for row in to_run],
                            unit=("predictions", "a gold or to another prediction"))
    if stop is not None:
        return stop
    backend = None
    if args.judge_gemini:
        # Imported lazily so the default (no-judge) path never needs the extra.
        from .backends import gemini_backend

        backend = gemini_backend(model=args.judge_model, project=args.judge_project)
    if backend is not None:
        stop = _paid_gate(args, "the judge")
        if stop is not None:
            return stop
    report = evaluate(
        executor, predictions, judge_backend=backend, confidence=args.confidence,
        extra_columns=args.extra_columns, gold_cache=gold_cache,
        checkpoint=getattr(args, "checkpoint", None), workers=workers,
    )
    save_gold_cache()
    if args.extra_columns == "tolerate":
        tolerated = sum(1 for r in report.results if r.verdict == "MATCH_EXTRA_COLUMNS")
        strict = sum(1 for r in report.results if r.execution_correct) - tolerated
        print(f"extra columns tolerated: {tolerated} question(s) matched only after "
              f"ignoring extra columns; strict count {strict} of {report.n_scored}")
    if not _emit_json(args, report):
        print(format_score_report(report, args.confidence))
        if report.n_scored >= 5 and report.execution.point == 0:
            # three consecutive arms at 0 of 18 on one estate read as a finding
            # about the agent; they were an environment variable in the runner
            print(f"  0 correct of {report.n_scored}: usually a setup fault (dialect, the model's "
                  "settings, the wrong dataset), not a score; read one prediction beside its "
                  "gold before comparing arms")
    _record_sql_spend(args, executor)
    if backend is not None:
        # after the warehouse line: _record_sql_spend replaces args.telemetry,
        # and the judge entry merges into whatever it left
        _record_judge_spend(args, backend)
    return 0


def _cmd_routing_gate(args: argparse.Namespace) -> int:
    """Audit a routing answer key before anything is spent routing against it."""
    from .routing import format_routing_gate, load_routing_golds, routing_gate
    golds = load_routing_golds(args.golds)
    catalog = _load_catalog(args.catalog) if args.catalog else None
    report = routing_gate(golds, catalog)
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        print(format_routing_gate(report))
    # non-zero on a defect so CI stops; concentration is a lead, not a defect
    return 0 if report["clean"] else 1


def _cmd_score_routing(args: argparse.Namespace) -> int:
    rows = _load_routing_predictions(args.predictions)
    if args.multi:
        report = evaluate_routing_sets(rows, confidence=args.confidence)
        print(_to_json(report) if args.json else report.summary())
        return 0
    catalog = _load_catalog(args.catalog) if args.catalog else None
    report = evaluate_routing(rows, catalog=catalog, confidence=args.confidence)
    if getattr(args, "save_report", None):
        # the shape ablate/pool/climb read, so two routing arms compare with the
        # same paired test as two SQL arms
        from .routing import as_eval_report
        save_report(as_eval_report(report), args.save_report)
        print(f"report -> {args.save_report}", file=sys.stderr if args.json else sys.stdout)
    # not _emit_json: it would write the raw routing report over the bridge
    # file, and nulling the flag to stop it hid the file from the workspace
    # recorder, so no routing report was ever listed as its run's output
    if args.json:
        print(_to_json(report))
        return 0
    print(format_routing_report(report, args.confidence, catalog_used=catalog is not None))
    from .routing import format_routing_triage
    shapes = format_routing_triage(report, catalog)
    if shapes:
        print()
        print(shapes)
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    # Before the executor, because the cross-project check reads this and the
    # first gold is audited as soon as the pool starts. Parsed-but-ignored is
    # how this flag shipped once: the argument existed, the gate still
    # flagged 191 of 400 golds, and only reading the defect counts caught it.
    if getattr(args, "data_projects", None):
        from .gate import set_data_projects
        set_data_projects(args.data_projects.split(","))

    # the stability check wants two real runs, so the warehouse's result
    # cache is off for it; a plain gate keeps it on like every other command
    executor = _executor_for(args, args.row_cap, use_query_cache=not args.check_stability)
    workers = _resolve_workers(args)
    golds = _load_golds(args.golds)
    # the gate runs every gold against the live database (that is its job)
    # and keeps the rows for score, which would otherwise fetch them again;
    # opened before pricing so the estimate can say what the cache holds
    gold_cache, save_gold_cache = _open_gold_cache(getattr(args, "gold_cache", None),
                                                   getattr(args, "db", None), reads=False)
    held = sum(1 for _, sql in golds if sql in (gold_cache or {}))
    notes = ([f"the gold cache holds {held} of these answers; gate runs them anyway "
              "(it is the check that they run)"] if held else [])
    stop = _cost_before_sql(args, executor, [sql for _, sql in golds],
                            labels=[gold_id for gold_id, _ in golds],
                            repeat=2 if args.check_stability else 1, workers=workers,
                            notes=notes, rejected_note="a rejected gold is flagged EXEC_FAIL")
    if stop is not None:
        return stop
    # The context the agent will be handed, when the caller says where it is.
    # Only the MISSING_LITERAL warning reads it, and only to tell a documented
    # business code from a question parameter nobody can guess.
    spec_context = ""
    if getattr(args, "spec", None):
        from .export import read_spec
        spec_context = read_spec(args.spec).context_text()
        print(f"reading context from {args.spec}: {len(spec_context)} "
              "characters. A value it documents is answerable, so it is not "
              "reported as missing")
    # What moved under the gold set. Both paths read metadata only and both
    # happen BEFORE any gold SQL is executed, because the whole point of
    # --since is to execute less of it.
    if getattr(args, "write_manifest", None) or getattr(args, "since", None):
        from . import drift as drift_mod
        shapes = drift_mod.live_shapes(executor)
        if args.write_manifest:
            rows = [{"id": gold[0], "gold_sql": gold[1]} for gold in golds]
            manifest = drift_mod.build_manifest(rows, shapes)
            drift_mod.save(manifest, args.write_manifest)
            print(f"manifest -> {args.write_manifest}  "
                  f"({len(manifest['golds'])} gold(s), "
                  f"{len(shapes.shapes)} table shape(s))")
            for note in shapes.skipped:
                print(f"  SKIPPED {note}")
        if args.since:
            drifts, unchanged, _ = drift_mod.compare(
                drift_mod.load(args.since), shapes)
            print("\n".join(drift_mod.lines(drifts, unchanged, len(golds),
                                            shapes.skipped)))
            moved = set(drift_mod.to_regate(drifts))
            if not moved:
                print("\nnothing to re-gate, so no gold SQL was executed and "
                      "nothing was billed.")
                save_gold_cache()
                return 0
            golds = [gold for gold in golds if gold[0] in moved]
            print(f"\nre-gating {len(golds)} gold(s) that drifted:")

    findings = gate(golds, executor, check_stability=args.check_stability,
                    workers=workers, gold_cache=gold_cache,
                    questions=_load_questions(args.golds),
                    context=spec_context)
    save_gold_cache()
    if not _emit_json(args, findings):
        print(format_gate_report(findings))
        _print_not_found_fix(args.db, [f.detail for f in findings if not f.ok], "golds")
    # The filtered answer keys, so no caller re-derives them from the report.
    flagged_ids = {f.gold_id for f in findings if not f.ok}
    # pass through every input column: dropping `question` here broke the
    # gate -> generate handoff and every user rewrote the same join script
    with open(args.golds, newline="") as handle:
        original_reader = csv.DictReader(handle)
        original_columns = list(original_reader.fieldnames or ["id", "gold_sql"])
        original_rows = list(original_reader)
    # the flagged file is what a reviewer opens after `next` tells them 112
    # golds were dropped. It held id, question and gold_sql and no reason at
    # all, so the reasons lived only in the run's stdout and report.json and
    # the file the tool named was the one place they were not
    by_id = {f.gold_id: f for f in findings}
    for path, wanted, with_reason in (
            (args.clean_out, lambda i: i not in flagged_ids, False),
            (args.flagged_out, lambda i: i in flagged_ids, True)):
        if not path:
            continue
        columns = original_columns + (
            [c for c in ("gate_defects", "gate_reason") if c not in original_columns]
            if with_reason else [])
        with open(path, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for row in original_rows:
                if not wanted(row["id"]):
                    continue
                if with_reason:
                    finding = by_id.get(row["id"])
                    row = {**row,
                           "gate_defects": "+".join(finding.defects) if finding else "",
                           "gate_reason": finding.detail if finding else ""}
                writer.writerow(row)
    if args.clean_out:
        print(f"clean golds -> {args.clean_out} "
              f"({len(golds) - len(flagged_ids)} of {len(golds)})")
    if args.flagged_out:
        print(f"flagged golds -> {args.flagged_out} ({len(flagged_ids)})")
    _record_sql_spend(args, executor)
    # Non-zero so a CI step fails when the answer key has defects.
    # non-zero on a defect is deliberate, so a CI step can guard on it. It also
    # breaks a plain `gate && next` chain, which is how a cold run met it, so
    # the caller can ask for the exit code without giving up the report
    if getattr(args, "exit_zero_on_defects", False):
        return 0
    return 0 if all(f.ok for f in findings) else 1


def _open_gold_cache(path: str | None, db: str | None,
                     reads: bool = True) -> tuple[dict | None, object]:
    """A gold cache that survives between commands, and a function to save it.

    The answer key runs unchanged in every arm and every repetition, so on a
    two-arm three-rep ablation five of every six gold executions are waste. On
    one enterprise warehouse that was about $4, against $2.65 of model spend for
    the same run.

    Two rules keep it honest. A cached answer belongs to one database, so the
    file records which one and a mismatch starts empty rather than scoring
    against another warehouse's rows. And it is opt-in outside a workspace:
    the underlying data can change between sessions, and only the caller
    knows whether it did. Under a workspace it defaults to one shared file,
    because a workspace has one database.

    `reads=False` is for a command that only adds to the file (gate runs
    every gold regardless), so the "reused" line is not printed for it.
    """
    if not path:
        return None, (lambda: None)
    from .executor import ExecResult
    cache: dict = {}
    stored: dict = {}
    file = pathlib.Path(path)
    if file.is_file():
        try:
            stored = json.loads(file.read_text())
        except ValueError:
            stored = {}
        if stored.get("db") != db:
            print(f"gold cache {path}: recorded for a different database "
                  f"({stored.get('db')!r}), starting empty")
            stored = {}
        for sql, row in (stored.get("golds") or {}).items():
            cache[sql] = ExecResult(rows=[dict(r) for r in row.get("rows") or []],
                                    error=row.get("error"),
                                    truncated=bool(row.get("truncated")))
        if cache and reads:
            print(f"gold cache {path}: {len(cache)} answer(s) reused, not executed")

    def save() -> None:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps({
            "db": db,
            "golds": {sql: {"rows": r.rows, "error": r.error, "truncated": r.truncated}
                      for sql, r in cache.items()},
        }, indent=1, default=str))
        print(f"gold cache -> {path}  ({len(cache)} answer(s))")

    return cache, save


def _score_for_cli(executor, predictions_path: str, confidence: float,
                   gold_cache: dict | None = None, workers: int = 1) -> EvalReport:
    """Score one predictions file — the shared step behind ablate/climb/triage."""
    return evaluate(executor, _load_predictions(predictions_path),
                    confidence=confidence, gold_cache=gold_cache, workers=workers)


def _print_ceiling(args: argparse.Namespace, queries: int, what: str) -> None:
    """The worst case before mine ranks the log: its statements are not known
    until then, so each query stops at --max-bytes and the ceiling is count
    times the cap. It was 30 to 60 times the outcome on one estate, which
    is why the measured estimate follows once the shapes are chosen."""
    if not is_bigquery(args.db):
        return
    cap = args.max_bytes or DEFAULT_MAX_BYTES
    print(f"ceiling before ranking: up to {queries} {what} x {cap / 1e9:g} GB each = "
          f"{queries * cap / 1e9:g} GB, ~${bigquery_usd(queries * cap):.4f} on-demand "
          f"(each query is dry-run and refused above --max-bytes); the measured "
          f"estimate follows once the shapes are chosen")


def _cmd_rewrite_questions(args: argparse.Namespace) -> int:
    """Redraft the questions of golds that already exist, without re-mining.

    Once a check is added to the gate, every answer key written before it has
    questions that fail the new check. The only way to fix them used to be a
    re-mine, and the expensive part of a re-mine is not the drafting: `mine`
    gates every surviving shape by EXECUTING it. On one estate that was $0.77
    of warehouse scan to re-derive golds that were already gated, reviewed and
    correct.

    Nothing here touches the warehouse. The SQL does not change, so the golds
    stay gated and stay reviewed; only the question text is written again,
    with every check applied at once and one retry naming what the draft still
    owes. `mine --write-questions` uses the same writer and the same retry.
    """
    from .gate import limit_without_order, selects_whole_row
    from .mine import PromptQuestionWriter, _what_the_question_owes

    # read as dicts, not the (id, question, gold_sql) tuples the other
    # commands use: every column of the caller's key has to survive, and a
    # reviewed key carries source, wording, accounts and whatever else
    with open(args.golds, newline="") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    if not rows:
        print(f"{args.golds}: no rows")
        return 2
    if "gold_sql" not in columns:
        print(f"{args.golds}: needs a gold_sql column")
        return 2
    if "question_owes" not in columns:
        columns.append("question_owes")

    echo = args.backend == "echo"
    prices = None if echo else price_table(args.model)
    per_call = 200                     # the writer's own max_output_tokens
    prompt_tokens = sum(len(row["gold_sql"]) // 4 + 120 for row in rows)
    estimate = None if echo else estimate_cost_usd(prices, prompt_tokens, per_call * len(rows) * 2)
    who = "echo" if echo else args.model
    print(f"rewriting {len(rows)} questions: up to {2 * len(rows)} {who} calls "
          "(one draft each, one retry where the draft still owes something), "
          + ("no model call, $0.0000" if echo
             else f"at most ${estimate:.4f}" if estimate is not None
             else f"cost unknown (no price for {args.model})"))
    if args.estimate_only:
        return 0
    if args.budget is not None and estimate is not None and estimate > args.budget:
        print(f"refusing to start: the most this can cost is ${estimate:.4f}, above the "
              f"budget ${args.budget:.4f}")
        return 2

    if echo:
        from .backends import echo_backend
        backend = echo_backend(args.echo_response)
    else:
        from .backends import gemini_backend
        try:
            backend = gemini_backend(model=args.model, project=args.project,
                                     thinking_budget=0, max_output_tokens=per_call)
            backend.prices = prices
        except ImportError:
            print("rewrite-questions needs the [gemini] extra: uv pip install -e '.[gemini]'")
            return 2
        except CredentialsMissing as problem:
            print(problem)
            return 2

    if not echo:
        stop = _paid_gate(args, "rewriting the questions")
        if stop is not None:
            return stop
    writer = PromptQuestionWriter(backend)
    retried = improved = still_owed = fixed = 0
    for row in rows:
        gold_sql = row["gold_sql"]
        before = _what_the_question_owes(gold_sql, row.get("question") or "")
        question = writer.write(gold_sql)
        owed = _what_the_question_owes(gold_sql, question)
        if owed:
            retried += 1
            second = writer.rewrite(gold_sql, question, owed)
            if second.strip() and len(_what_the_question_owes(gold_sql, second)) < len(owed):
                improved += 1
                question, owed = second, _what_the_question_owes(gold_sql, second)
        row["question"] = question
        row["question_owes"] = "; ".join(owed)
        still_owed += bool(owed)
        fixed += bool(before) and not owed

    with open(args.out, "w", newline="") as handle:
        out = csv.DictWriter(handle, fieldnames=columns)
        out.writeheader()
        out.writerows(rows)
    print(f"questions -> {args.out}  ({len(rows)} rewritten; {retried} asked a second "
          f"time, {improved} improved by it; {fixed} row(s) that owed something now owe "
          f"nothing; {still_owed} still owe something, named per row in question_owes)")
    print("  the SQL did not change, so these golds are still gated and still reviewed; "
          "read the rows that still owe something before you score against them")
    # a question rewrite cannot repair a gold whose SQL is the problem, and a
    # user watching 25 flags drop to 14 will ask why it stopped there
    beyond = sum(1 for row in rows
                 if selects_whole_row(row["gold_sql"]) or limit_without_order(row["gold_sql"]))
    if beyond:
        print(f"  {beyond} gold(s) carry a defect no rewrite can reach: the SQL returns whole "
              "rows, or caps them without ordering. Those need the gold changed or dropped")
    # measured on one estate: the same 25 questions, rewritten, moved 2 of 25
    # off the right table because the router reads the question text
    print("  if your pipeline picks the table from the question text, re-measure routing: "
          "a question made precise about columns matches differently, and one estate lost "
          "2 of 25 correct tables this way")
    # 16 GB to 92 GB on the same 25 questions, one more query
    print("  expect the next score run to cost more: a question that says what it wants "
          "produces more ambitious SQL, and one estate went from 15 GB to 92 GB billed")
    usage = getattr(backend, "usage", None)
    if usage and not echo:
        spent = estimate_cost_usd(prices, usage["prompt_tokens"],
                                  usage["output_tokens"] + usage.get("thought_tokens", 0))
        args.telemetry = {**usage, "estimated_cost_usd": spent, "price_table": prices}
        print(f"spent: {usage['calls']} calls, ${spent:.4f}" if spent is not None else "")
    return 0


def _cmd_mine(args: argparse.Namespace) -> int:
    # Refuse to guess the scope. `--datasets` and `--no-spread` were both
    # opt-in and neither was prompted, and the cost of that is measured: a
    # round-robin pull over 26 datasets gave 0.61 questions per table and a
    # reserved half only 60% reachable, so three rounds in a row reported
    # +0.0 about the SPLIT while reading as a result about the context. The
    # same log pulled deep on two named datasets gave 4.78 per table and
    # 100% reachable. `docs/PRACTICES.md` practice 1.
    #
    # And the scope decides what the run can ever measure. On the same estate
    # the two datasets chosen by traffic had no written documentation at all,
    # so 6 of 43 golds touched a documented dataset and five paid rounds
    # measured model-mined prose instead of anybody's writing. Practice 2.
    if not getattr(args, "datasets", None) and not getattr(
            args, "all_datasets", False):
        print("mine needs to know its scope, and will not guess it.\n\n"
              "  --datasets A,B    the datasets to mine, named\n"
              "  --all-datasets    mine the whole log anyway, deliberately\n\n"
              "Two free things to read first:\n"
              "  uv run python scripts/doc_coverage.py CANDIDATES.csv\n"
              "      which datasets already have written documentation. Mine\n"
              "      those: a gold on an undocumented dataset cannot be moved\n"
              "      by the writing, so it measures nothing about it.\n"
              "  uv run python scripts/estate_shape.py GOLDS.csv\n"
              "      questions per table, and how much of the reserved half a\n"
              "      rule could reach. Under 2.00 per table or under 50%\n"
              "      reachable, no context can show through.\n\n"
              "docs/PRACTICES.md, practices 1 and 2. Measured on one estate:\n"
              "spread over 26 datasets gave 0.61 questions per table and 60%\n"
              "reachable; two named datasets gave 4.78 and 100%.",
              file=sys.stderr)
        return 2

    # Naming the datasets is half of it. Round-robin across the named ones
    # starves every table in turn, which is the same failure one level down.
    if getattr(args, "datasets", None) and not getattr(args, "no_spread", False):
        named = len([d for d in args.datasets.split(",") if d.strip()])
        if named > 1:
            print(f"note: mining {named} datasets with the round-robin spread "
                  "on. That default surveys an estate; it does not build a set "
                  "that proves anything. Pass --no-spread to stack questions "
                  "on the busiest tables (docs/PRACTICES.md practice 1).",
                  file=sys.stderr)

    # Before anything else: `mine` gates every candidate it keeps, and the
    # cross-project check reads this when the first one is audited.
    if getattr(args, "data_projects", None):
        from .gate import set_data_projects
        set_data_projects(args.data_projects.split(","))

    executor = _executor_for(args, args.row_cap)
    queries: list[MinedQuery] = []
    for path in args.queries:         # several logs (one per billing project) read as one
        with open(path, newline="") as handle:
            queries += [
                MinedQuery(row["sql"], int(row.get("times_seen") or 1),
                           row.get("ran_by", ""), int(row.get("distinct_users") or 0),
                           # `log` writes no question column; someone who filled one
                           # in by hand gets it checked like a drafted question
                           row.get("question", ""))
                for row in csv.DictReader(handle)
            ]
    exclude = {a.strip() for a in args.exclude_accounts.split(",") if a.strip()}
    scope = [d.strip() for d in (args.datasets or "").split(",") if d.strip()]
    spec = parse_db_spec(args.db)
    # the dataset unqualified names resolve in, as tables_named spells it
    default_dataset = spec.dataset.rpartition(".")[2] if spec.dataset else None
    # where the tables live: the data project when --db names one, the billing
    # project otherwise. A shape naming a third project reads data this run
    # cannot, and the review sheet says so instead of dropping the row
    data_project = (spec.dataset or "").partition(".")[0] or spec.project
    mine_counts: dict = {}
    _print_ceiling(args, min(args.limit or len(queries), len(queries)), "candidate queries")
    writer = None
    if args.write_questions:
        # an LLM stage: one short call per kept shape drafts the question;
        # the review file is still the human's, nothing here is a gold yet
        from .backends import gemini_backend
        prices = price_table(args.model)
        try:
            # no thinking: Gemini 2.5 counts thinking tokens inside
            # max_output_tokens, and a 512 budget under a 256 cap returned
            # questions cut off mid-sentence
            backend = gemini_backend(model=args.model, project=args.project,
                                     thinking_budget=0, max_output_tokens=200)
            backend.prices = prices
        except ImportError:
            print("--write-questions needs the [gemini] extra: uv pip install -e '.[gemini]'")
            return 2
        except CredentialsMissing as exc:
            print(exc)
            return 2
        writer = PromptQuestionWriter(backend)
        print(f"question drafts: one {args.model} call per kept shape, "
              f"up to {min(args.limit or len(queries), len(queries))} calls")

    stop_code: dict = {}

    def price_the_chosen_shapes(sqls: list[str]) -> bool:
        # the first moment the statements are known: the measured estimate,
        # --budget and --estimate-only apply here, before any shape runs
        code = _cost_before_sql(args, executor, sqls)
        if code is not None:
            stop_code["code"] = code
            return False
        if getattr(args, "write_questions", False):
            stop = _paid_gate(args, "drafting questions")
            if stop is not None:
                stop_code["code"] = stop
                return False
        return True

    # What the warehouse has billed so far, read straight off the executor.
    # This is the whole reason `--budget` can stop a running stage: the
    # bytes were always being counted, and nothing looked at them until the
    # run was over.
    def billed_so_far() -> float:
        usage = getattr(executor, "usage", None)
        return bigquery_usd(usage["bytes_billed"]) if usage else 0.0

    # The review file, opened on the first candidate rather than at the end.
    # Opened lazily so a run refused at the cost gate still leaves no file:
    # a header-only CSV reads as "the log had nothing", which is a different
    # and wrong story.
    live = {"handle": None, "writer": None}

    def append_candidate(candidate) -> None:
        if live["handle"] is None:
            live["handle"] = open(args.out, "w", newline="")
            live["writer"] = csv.writer(live["handle"])
            live["writer"].writerow(CANDIDATE_COLUMNS)
        live["writer"].writerow(candidate_row(candidate))
        live["handle"].flush()

    try:
        candidates = mine_candidates(
            queries, executor, question_writer=writer, limit=args.limit,
            min_times_seen=args.min_times_seen,
            exclude_accounts=exclude, require_analytical=args.require_analytical,
            rank_by=args.rank_by, min_tables=args.min_tables,
            drop_machine_sql=not args.keep_machine_sql,
            drop_migration_checks=not args.keep_migration_checks,
            max_question_literals=(args.max_question_literals or None),
            datasets=set(scope) if scope else None, default_dataset=default_dataset,
            project=data_project, spread=not args.no_spread,
            counts_out=mine_counts, before_gate=price_the_chosen_shapes,
            rules_in_context=args.rules_in_context,
            progress=_progress_printer("mine", usd=billed_so_far,
                                       heartbeat_extra=_retry_note(writer)),
            budget=getattr(args, "budget", None), spent=billed_so_far,
            on_candidate=append_candidate,
        )
    except Exception as exc:  # noqa: BLE001 - a model API error, in one line
        if not type(exc).__module__.startswith("google.genai"):
            raise
        if live["handle"] is None:
            kept = "nothing was written"
        else:
            kept = f"the candidates found before it stopped are in {args.out}"
        print(f"question drafts stopped: {type(exc).__name__}: {str(exc).splitlines()[0][:200]}\n"
              f"{kept}; rerun, or drop --write-questions and fill the questions by hand")
        return 1
    finally:
        if live["handle"] is not None:
            live["handle"].close()
    if mine_counts.get("stopped"):
        return stop_code["code"]
    # Rewritten whole now the run is over. Same row builder as the live
    # append, so the finished file cannot differ from the one that was
    # readable while it ran.
    path = write_candidates(candidates, args.out)
    print(summarize_candidates(candidates, datasets=scope or None, counts=mine_counts))
    _print_not_found_fix(args.db, [c.gate_detail for c in candidates if not c.usable], "shapes")
    print(f"\nwrote {path} — review it, then use it as a gold set")
    _record_sql_spend(args, executor)
    if writer is not None:
        usage = dict(writer.backend.usage)
        spent = estimate_cost_usd(prices, usage["prompt_tokens"],
                                  usage["output_tokens"], usage["thought_tokens"])
        args.telemetry = {**(getattr(args, "telemetry", None) or {}), **usage,
                          "estimated_cost_usd": spent, "price_table": prices}
        print(f"question drafts: {usage['calls']} calls, "
              + (f"${spent:.4f}" if spent is not None else "cost unknown (no price)"))
    return 0


def _cmd_semantic(args: argparse.Namespace) -> int:
    if args.ossie:
        from .ossie import read_ossie
        model = read_ossie(args.ossie, args.allow_untested_version)
    else:
        from .semantic import read_lookml
        model = read_lookml(args.lookml)
    print(model.summary())
    if model.stated_questions:
        print(f"  {len(model.stated_questions)} questions a person already wrote "
              "in the model; they carry no SQL, so they are candidates")
    if args.context_out:
        pathlib.Path(args.context_out).write_text(model.to_context())
        print(f"\n  wrote {args.context_out} — the with-context arm of an ablation")
    if args.candidates_out:
        with open(args.candidates_out, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["id", "question", "from_metric", "gold_sql"])
            for index, (question, metric) in enumerate(model.to_candidates()):
                writer.writerow([f"s{index:03d}", question, metric, ""])
        print(f"  wrote {args.candidates_out} — questions for a human to write SQL for")
    return 0


def _cmd_arms(args: argparse.Namespace) -> int:
    from .export import build_arms, read_spec
    from .runners.ca_api import build_inline_context

    spec = _spec_for(args)
    print(spec.summary())
    mapping = _room_mapping(args, spec)
    if mapping:
        from .export import rename_tables
        spec = rename_tables(spec, mapping)
        print(f"  about {len(mapping)} evaluation view(s) instead of the "
              "production tables")
    control, treatment = build_arms(spec, args.tables, args.model)
    print()
    print(f"  control   {control.arm_id}  {control.label}")
    print(f"  treatment {treatment.arm_id}  {treatment.label}")
    if control.arm_id == treatment.arm_id:
        print("\n  the two arms are identical: there is nothing authored to measure.")
        return 1
    if args.out:
        out = pathlib.Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for arm in (control, treatment):
            path = out / f"arm_{arm.arm_id}.json"
            path.write_text(json.dumps(build_inline_context(arm), indent=1))
            print(f"  wrote {path}")
    return 0


def _paid_gate(args: argparse.Namespace, what: str) -> int | None:
    """The confirmation, right before a verb spends. None means go ahead.

    One helper rather than six copies. Every paid verb asks the same question
    and a copy per verb is how five of them stay right and the sixth drifts.

    Called after the verb has printed its own cost, so a person sees the price
    and then answers. It reads the controlling terminal, never stdin, so a
    host agent on auto-approve cannot answer on the caller's behalf.
    """
    from .confirm import confirm_paid
    decision = confirm_paid("", budget=getattr(args, "budget", None),
                            yes=getattr(args, "yes", False),
                            argv=list(sys.argv[1:]))
    print(f"{what}: {decision.reason}")
    return None if decision.allowed else 2


def _cmd_round(args: argparse.Namespace) -> int:
    from .confirm import confirm_paid
    from .export import build_arms, read_spec
    from .power import DEFAULT_TARGET_POINTS
    from .round import Gold, free_gates, run
    from .runners.ca_api import CaApiRunner

    # Project access is an intent guard, so it is set before any gate or
    # executor is constructed. An API key must not change this decision.
    if args.data_projects:
        from .gate import set_data_projects
        set_data_projects(args.data_projects.split(","))

    _require_file(args.golds, "the gold set CSV (--golds)")
    golds = [Gold(row["id"], row["question"], row["gold_sql"])
             for row in csv.DictReader(open(args.golds, newline=""))]
    if not golds:
        print(f"{args.golds} has no rows")
        return 2

    spec = _spec_for(args)
    print(spec.summary())
    # The served spec already names the room's views: its tables come from
    # the entries the bundles were read from. Mapping it again would send a
    # view through view_name a second time and produce a table nobody has.
    # So the mapping is built from whichever spec still names the business's
    # own tables, and applied only to that one.
    if not getattr(args, "served", False):
        mapping = _room_mapping(args, spec)
        if mapping:
            from .export import rename_tables
            spec = rename_tables(spec, mapping)
            print(f"  about {len(mapping)} evaluation view(s) instead of the "
                  "production tables")
    else:
        mapping = {}
    if args.served and args.spec:
        # Arm 1 against Arm 1s: the SAME content, delivered two ways. The
        # ordinary control is Arm 0d, which would measure the served bundle
        # against bare and answer a question nobody asked. Here the control
        # is the spec delivered in the structured Context fields and the
        # treatment is the same spec after a round trip through the catalog,
        # so the only difference is the delivery and the difference is the
        # catalog's own contribution.
        from .export import read_spec as _read_spec
        direct = _read_spec(args.spec)
        room = _room_mapping(args, direct)
        if room:
            from .export import rename_tables as _rename
            direct = _rename(direct, room)
            print(f"  the direct arm is about {len(room)} evaluation view(s)")
        control = build_arms(direct, args.tables, args.model)[1]
        control.label = "1 authored, delivered directly"
        treatment = build_arms(spec, args.tables, args.model)[1]
        treatment.label = "1s served by the catalog"
        print("\n  measuring the catalog's own contribution: the same content "
              "in the structured fields, against the same content after a "
              "round trip through Knowledge Catalog")
    else:
        control, treatment = build_arms(spec, args.tables, args.model)
    if control.arm_id == treatment.arm_id:
        print("\nthe two arms are identical: there is nothing authored to measure")
        return 2
    # A dropped table ends the whole round, so it is found before the first
    # paid call. One 404 in one gold stopped 136 calls on a customer estate:
    # the API answers the entire request with "404 Not found: Table ...", so
    # no question is answered at all. A warehouse always carries dead tables
    # in its query history and a log-mined set will always name some. Free:
    # `SELECT 1 FROM t WHERE FALSE` dry-run reads nothing.
    if args.db and is_bigquery(args.db):
        from .runners import tables_that_are_gone, without_tables
        gone = tables_that_are_gone(sorted(set(control.tables) | set(treatment.tables)),
                                    _executor_for(args, None))
        if gone:
            print(f"\n  {len(gone)} table(s) named by the arms no longer exist and are "
                  "dropped from both:")
            for table in gone:
                print(f"    {table}")
            print("    The agent answers the whole request with a 404 when one table is "
                  "missing, so one dead table would end the round before its first answer. "
                  "Golds that need these tables will score wrong in BOTH arms, which is "
                  "fair but wastes the question: re-run gate to drop them.")
            control, treatment = without_tables(control, gone), without_tables(treatment, gone)
        if not treatment.tables:
            print("\n  every table the arms name is gone: there is nothing to ask about")
            return 2

    print(f"\n  control   {control.arm_id}  {control.label}"
          f"\n  treatment {treatment.arm_id}  {treatment.label}")

    # The free checks first, and printed, because a round that was going to be
    # INVALID should cost nothing. Split before checking: the leak gates defend
    # the held-out questions, and handing them every gold makes the tool refuse
    # a worked example built from a working question, which is the whole point
    # of curating one.
    # `split` writes two id files beside the gold set for a person to use,
    # and a round that ignores them and makes its own split is how a worked
    # example built from a "working" question turns out to be a reserved
    # answer. Found the hard way: `split` wrote 19 working and 20 reserved,
    # the round's own split of the same golds gave 21 and 18, and nine golds
    # treated as working were reserved here.
    #
    # So the files are looked for beside --golds and used when they are
    # there. Nothing to remember, and --curate-ids / --holdout-ids override.
    chosen_split = _split_from_files(args)
    if chosen_split is not None:
        _curate_ids, holdout_ids = chosen_split
    else:
        from .leakage import split_golds
        # The same arguments the round will use. The dry run exists to show
        # the gates BEFORE anything is paid for, and a dry run that splits
        # differently checks the wrong questions -- it reported 30 held out
        # where the round would hold out 104.
        _curate_ids, holdout_ids = split_golds(
            [g.id for g in golds], args.holdout_share, args.seed,
            external_context=getattr(args, "external_context", ""))
    held_out = [g for g in golds if g.id in set(holdout_ids)]
    gates = free_gates(golds, treatment, allow_sql=args.allow_sql,
                       protect=held_out)

    # The catalog mines joins from query history with no scan, and the gold
    # gate's own statements are query history. Only checkable when there is a
    # catalog to read; in file mode the arm carries no catalog joins at all,
    # so there is nothing to check rather than a check being skipped.
    if getattr(args, "catalog", False) or getattr(args, "eval_room", None):
        from .gates import join_links
        try:
            store = _catalog_store(args)
            links = []
            for table in (args.tables or spec.tables):
                entry = store.resolve(table)
                if entry:
                    links += store.entry_links(entry)
            gates.append(join_links(links, _golds_ran_at(args.golds)))
        except SystemExit:
            raise
        except Exception as unreadable:  # noqa: BLE001 - reported, never silent
            from .gates import Gate
            gates.append(Gate(
                "join links", True,
                f"could not be read ({type(unreadable).__name__}), so a mined "
                "join cannot be ruled out. Printed rather than passed quietly"))
    from .gates import summary as gate_summary
    print("")
    print(gate_summary(gates))
    if any(not g.passed for g in gates):
        return 2

    if args.predictions_control or args.predictions_treatment:
        # Predictions somebody else's agent produced. Everything after this is
        # the same round: the same gates, the same sealed split, the same
        # paired test and the same verdict word. The guards are the product,
        # and they must not depend on who produced the SQL.
        if not (args.predictions_control and args.predictions_treatment):
            print("a paired lift needs BOTH --predictions-control and "
                  "--predictions-treatment. One file alone is a score, not a "
                  "comparison, and this command exists to compare")
            return 2
        from .runners.csv_files import CsvRunner
        runner = CsvRunner.from_files(args.predictions_control,
                                      args.predictions_treatment)
        print("")
        print(runner.coverage([g.id for g in golds]))
    else:
        runner = CaApiRunner(project=args.project, location=args.location,
                             max_billed_bytes=args.max_billed_bytes)
    sheet = runner.cost_sheet(n_questions=len(golds), n_arms=2, k=args.k)
    print("")
    print(sheet.line())
    if args.dry_run:
        print("\n--dry-run: nothing was asked and nothing was billed")
        return 0

    # The CSV runner makes no chat call, so it needs no billing project. The
    # gold and predicted SQL still run somewhere, which --db names.
    if runner.name != "csv":
        if not args.project:
            print("round needs --project: the chat calls are billed to it")
            return 2
        from .backends import refuse_forbidden_project
        refuse_forbidden_project(args.project, "the round's chat calls")

    decision = confirm_paid("", budget=args.budget, yes=args.yes,
                            argv=["round"] + sys.argv[2:])
    print(decision.reason)
    if not decision.allowed:
        return 2

    # One throttled line per arm, not one line per call. A round is 400 calls
    # and three hours on a real estate, and a line each buried the verdict
    # under 400 lines of "[control] asked 1/400 m001_ddabe452". The arm is in
    # the stage name because a round walks control then treatment, twice, and
    # which of the four you are in is the thing you want to know.
    from .progress import Heartbeat, Progress
    seen: dict[str, dict] = {}
    beating: dict[str, object] = {}

    # What a heartbeat adds about the round's own calls. The runner counts
    # its retries; `0 retries` is printed too, because "this is slow" and
    # "this is being retried" are different problems and nothing on screen
    # could tell them apart.
    def call_note() -> str:
        tried = getattr(runner, "retries", 0)
        return f"{tried} call retr{'y' if tried == 1 else 'ies'}"

    def progress(arm_label, index, total, scored_id, reused):
        if arm_label == "resume":
            print(f"  resuming: {scored_id}", flush=True)
            return
        stage = f"round/{arm_label}"[:8]
        state = seen.get(stage)
        if state is None or state["total"] != total:
            state = seen[stage] = {"total": total, "asked": 0, "cached": 0,
                                   "p": Progress(stage, total)}
            # One heartbeat per arm, and the previous arm's is stopped: the
            # round walks four arms and only one of them is running.
            for old_beat in beating.values():
                old_beat.stop()
            beating.clear()
            beating[stage] = Heartbeat(
                state["p"], say=lambda text: print(text, flush=True),
                extra=call_note).start()
        beat = beating.get(stage)
        if beat is not None:
            beat.now_on(index, total)
        state["cached" if reused else "asked"] += 1
        counts = {"asked": state["asked"], "from disk": state["cached"]}
        text = (state["p"].done(index, counts=counts) if index >= total
                else state["p"].tick(index, counts=counts))
        if text:
            print(text, flush=True)
        if index >= total and beat is not None:
            beat.stop()

    # How many times these reserved questions have been scored BEFORE this
    # round. The gate that uses it took a number nothing ever supplied, so it
    # saw 1 on every round and never fired. Reads the ledger before the round
    # and records this round's read after it.
    from .ledger import prior_reads, read_ledger, record_reads, reads_note
    holdout_ledger = read_ledger(args.holdout_ledger)
    reserved_ids = list(chosen_split[1]) if chosen_split else []
    reads_before = prior_reads(holdout_ledger, reserved_ids)
    warning = reads_note(holdout_ledger, reserved_ids)
    if warning:
        print(f"  holdout: {warning}")

    gold_cache, save_gold_cache = _open_gold_cache(args.gold_cache, args.db)
    try:
        result = run(golds, control, treatment, runner.answer,
                     _executor_for(args, args.row_cap or None), k=args.k,
                     holdout_reads_so_far=reads_before,
                     holdout_fraction=args.holdout_share, seed=args.seed,
                     target_pp=args.target, allow_sql=args.allow_sql,
                     progress=progress, checkpoint=args.checkpoint,
                     split=chosen_split,
                     extra_columns=args.extra_columns,
                     external_context=args.external_context,
                     gold_cache=gold_cache,
                     target_lift=(args.target_lift
                                  if args.target_lift is not None
                                  else DEFAULT_TARGET_POINTS))
    finally:
        # Scoring happens after the last answer and takes minutes of its own.
        # A heartbeat still ticking there would say "still on 18/18", which
        # is true and useless.
        for beat in beating.values():
            beat.stop()
        # The read happened whether or not the round reached a verdict, so it
        # is recorded in the `finally`. A read that goes unrecorded is the
        # leak this ledger exists to stop.
        if args.holdout_ledger and reserved_ids:
            record_reads(args.holdout_ledger, reserved_ids, holdout_ledger)
        # In the `finally`, not after it. A round already crashed once on its
        # final CSV write, after 74 minutes of paid calls, and only
        # --checkpoint saved the answers. The gold cache is worth about $4 a
        # run by `_open_gold_cache`'s own measurement, so losing it to an
        # exception costs more than the model spend it exists to protect.
        save_gold_cache()
    if args.save_report and result.treatment_report is not None:
        save_report(result.treatment_report, args.save_report)
        print(f"report -> {args.save_report}")
    # A runner that makes no call has no retries. Asked for rather than
    # required on the protocol, because making every runner carry a field it
    # can never use is how a protocol grows fields nobody implements.
    retries = getattr(runner, "retries", 0)
    if retries:
        print(f"\n  {retries} call(s) had to be asked again after a "
              "transient failure")
    missing = getattr(runner, "missing", None)
    if missing:
        for arm_label, ids in sorted(missing.items()):
            unique = sorted(set(ids))
            print(f"\n  {arm_label}: {len(unique)} question(s) had no row in "
                  f"the predictions file and were not scored ({', '.join(unique[:5])}"
                  f"{'...' if len(unique) > 5 else ''}). A missing row is an "
                  "unanswered question, not a wrong one")

    # The query bill, read BEFORE the report prints, so the cost line sits
    # beside the verdict and the safety word rather than after them. A
    # metadata read: no gold SQL runs and no table is scanned, so recovering
    # what a round already spent costs nothing.
    #
    # The `spent:` lines above count MODEL calls and cannot count this. The
    # SQL the agent wrote does not exist until the run does and is billed to
    # the warehouse, which is how per-command totals reached about $74 while
    # the ledger held $149.61. Both lines print; neither is the whole bill.
    if getattr(args, "cost", False) and getattr(args, "project", None):
        from .cost import ledger_bytes, per_arm, usd_for_bytes
        job_ids = [str(row.get("job_id") or "") for row in result.rows
                   if str(row.get("job_id") or "").strip()]
        billed = ledger_bytes(args.project, job_ids)
        result.cost = per_arm(result.rows, billed)
        # Onto the rows too, so a committed run carries its own cost and
        # nobody has to re-read a ledger that ages out after 180 days.
        for row in result.rows:
            job = str(row.get("job_id") or "").strip()
            if job in billed:
                row["bytes_billed"] = billed[job]
                row["query_cost_usd"] = round(usd_for_bytes(billed[job]), 6)

    print("")
    for line in result.lines():
        print(line)
    if args.run_out:
        out = pathlib.Path(args.run_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="", encoding="utf-8") as handle:
            # Built from the rows rather than typed out. A hand-written
            # list silently drifts from what `_run_rows` produces: adding
            # `error_class` for D67 crashed a 74-minute paid round at the
            # write, AFTER every answer was paid for, with
            # "dict contains fields not in fieldnames: 'error_class'". The
            # answers survived only because --checkpoint was passed. The
            # first keys are ordered for a person reading the file in a
            # spreadsheet; anything new lands after them instead of
            # raising.
            leading = ["id", "split", "arm", "arm_id", "question", "gold_sql",
                       "pred_sql", "verdict", "correct", "error_class",
                       "citations", "matched_example", "job_id",
                       "bytes_billed", "query_cost_usd",
                       "thought_chars", "retries"]
            seen = {key for row in result.rows for key in row}
            fields = ([k for k in leading if k in seen]
                      + sorted(seen - set(leading)))
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(result.rows)
        print(f"  wrote {out} ({len(result.rows)} scored rows)")
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps({
            "round": {
                "verdict": result.verdict.word,
                "reason": result.verdict.reason,
                "lift_pp": None if result.holdout is None else result.holdout.lift * 100,
                "p_value": result.verdict.p_value,
                "n_curate": len(result.curate_ids),
                "n_holdout": len(result.holdout_ids),
                # How many answers each set could actually be compared on,
                # and how many were stored-example lookups. A lift beside a
                # count of what was ASKED reads as that lift's sample size,
                # and on 2026-09-10 the two differed by a factor of 23.
                "n_scored_curate": (None if result.curate is None else
                                    result.curate.n_questions or result.curate.n_paired),
                "n_scored_holdout": (None if result.holdout is None else
                                     result.holdout.n_questions or result.holdout.n_paired),
                "lookups_curate": len(result.lookups.get("curate") or []),
                "lookups_holdout": len(result.lookups.get("holdout") or []),
                "k_repeats": result.k_repeats,
                # The real count, or null when no ledger was given. Null is
                # "not tracked"; the literal 1 that used to sit here was a
                # made-up number that made a re-read look like a first read.
                "holdout_reads": (None if not args.holdout_ledger
                                  else reads_before + 1),
                # The working-set lift minus the held-out one, with the
                # interval of the difference. The one number that names a fit
                # to the questions the context was written from.
                "gap_pp": None if result.gap() is None else result.gap()[0],
                "gap_ci_low_pp": None if result.gap() is None else result.gap()[1],
                "gap_ci_high_pp": None if result.gap() is None else result.gap()[2],
                "resolution": result.verdict.resolution,
                "questions_needed": result.verdict.questions_needed,
                "target_pp": args.target,
                "model_alias": args.model,
                "arm_control_hash": result.control_arm_id,
                "arm_treatment_hash": result.treatment_arm_id,
            },
            "gates": [list(g.as_tuple()) for g in result.gates],
        }, indent=1))
        print(f"\n  wrote {args.out}")
    return 0


def _golds_ran_at(path):
    """When the gold file was last written, as a stand-in for when the golds
    last ran against the warehouse.

    Not exact. The gate needs a time to compare a link's creation against,
    and the gold file's modification time is the closest thing that is always
    there. It errs the safe way: a file written before the golds ran makes
    the gate suspicious of MORE links, not fewer.
    """
    import datetime
    import os
    try:
        return datetime.datetime.utcfromtimestamp(os.path.getmtime(path))
    except OSError:
        return None


def _catalog_store(args):
    """The store a round reads from, built once so two paths cannot drift."""
    from .catalog import KnowledgeCatalogStore, transport_for
    if not getattr(args, "project", None):
        raise SystemExit("this needs --project: the catalog entries live in it")
    location = getattr(args, "catalog_location", None) or "us"
    return KnowledgeCatalogStore(
        transport_for(args.project, location),
        entry_group=getattr(args, "entry_group", "@bigquery"),
        # The glossary goes in here rather than being a push-only argument,
        # because the READ needs it too: a round building a catalog arm has
        # to know where this estate's business terms live, and this is the
        # one place both paths build the store.
        glossary_id=getattr(args, "glossary", "") or "",
        search_pattern=getattr(args, "search_pattern", None) or "{question}")


def _spec_for(args: argparse.Namespace):
    """The spec a command was asked for: from files, or from the catalog.

    Both readers return the same Spec, and a test asserts the same content
    from either source builds a byte-identical request. So a command names its
    source once, here, and nothing downstream has to care which it was.
    """
    from .export import read_catalog, read_spec

    # Arm 1s: whatever lookupContext serves, as prose in one instruction.
    # Its difference from Arm 1 is the catalog's own contribution, and that
    # is the one number in this framework the catalog team can act on.
    if getattr(args, "served", False):
        from .export import read_served
        if not args.tables:
            raise SystemExit(
                "--served needs --tables: the bundle is fetched per entry, so "
                "there is no list to start from without one")
        return read_served(_catalog_store(args), args.tables)

    if not getattr(args, "catalog", False):
        if not args.spec:
            raise SystemExit("give --spec DIR, or --catalog to read the context "
                             "out of Knowledge Catalog")
        return read_spec(args.spec)

    if not args.tables:
        raise SystemExit(
            "--catalog needs --tables: reading a catalog starts from the list "
            "of tables to read, where a spec repo carries its own")
    return read_catalog(_catalog_store(args), args.tables)


def _room_mapping(args, spec):
    """Production table name -> evaluation view name, or {} when no room.

    Built here rather than in the spec so a customer's own documentation
    keeps naming their own tables. Two copies of the truth drift; one copy
    and a mapping does not.
    """
    room = getattr(args, "eval_room", None)
    if not room:
        return {}
    if room.count(".") != 1:
        raise SystemExit("--eval-room takes PROJECT.DATASET, e.g. "
                         "my-eval-project.room")
    project, dataset = room.split(".")
    from .evalproject import view_name
    return {table: view_name(project, dataset, table) for table in spec.tables}


def _write_provenance(store, tables, args, treatment):
    """One provenance record per entry, then read back and compared.

    Written after the read-back check, not before: a record of a write that
    did not land is worse than no record, because it looks like evidence.
    """
    import datetime

    from .provenance import (ASPECT_TYPE, FIELDS, Provenance, content_hash,
                             normalise)

    project = args.project
    location = getattr(args, "catalog_location", None) or "us"
    try:
        type_name = store.transport.ensure_aspect_type(
            project, location, ASPECT_TYPE, list(FIELDS))
    except SystemExit as denied:
        print(f"\n  no provenance written: {denied}")
        return
    key = f"{project}.{location}.{ASPECT_TYPE}"
    stamped = datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")

    written, stale = 0, []
    for table in sorted(tables):
        entry = store.resolve(table)
        if not entry:
            continue
        page = store.stored_overview(entry) or ""
        body = Provenance(
            round_id=args.round_id or f"push-{stamped[:10]}",
            measured_at=stamped,
            arm_control_hash="", arm_treatment_hash=treatment.arm_id,
            overview_hash=content_hash(page),
            model_alias=args.model,
        ).payload()
        store.transport.write_custom_aspect(entry, key, type_name, body)
        back = normalise(store.transport.read_custom_aspect(entry, ASPECT_TYPE))
        if back.get("overview_hash") != body["overview_hash"]:
            stale.append(table)
        else:
            written += 1
    print(f"\n  provenance: {written} record(s) written and read back with "
          "matching hashes")
    for table in stale:
        print(f"    {table}: read back a different hash than was written")


def _cmd_catalog_rollback(args: argparse.Namespace) -> int:
    """Push back what a previous `spec-push` replaced.

    Not a catalog operation: Knowledge Catalog has no undo, so this writes the
    old bytes again. It only works because the push saved them.
    """
    from .rollback import load, plan

    records = load(args.previous)
    store = _catalog_store(args)
    live = {}
    for record in records:
        entry = record.get("entry") or store.resolve(record["table"])
        if not entry:
            continue
        held = store.read_entry(entry)
        live[record["table"]] = {"overview": held.get("overview"),
                                 "queries": held.get("queries")}

    decided = plan(records, live)
    print(decided.text())
    if args.dry_run:
        print("\n--dry-run: nothing was written")
        return 0
    if not decided.restore:
        return 1 if decided.refused else 0

    from .catalog import OVERVIEW_ASPECT_KEY, QUERIES_ASPECT_KEY
    by_table = {record["table"]: record for record in records}
    for table, content in decided.restore:
        entry = by_table[table].get("entry") or store.resolve(table)
        page = content.get("overview")
        if page:
            store.write_overview(entry, page)
        else:
            # There was no page before the push, so putting one back means
            # taking the aspect OFF. Writing an empty string instead leaves an
            # empty page, which is not the same thing and reads as curated.
            store.transport.remove_aspect(entry, OVERVIEW_ASPECT_KEY)

        # The worked examples, which this loop used to walk straight past. A
        # push writes them and a rollback left every one of them on the entry
        # for good. On an estate whose queries aspect was empty before the
        # push that is our content living in their catalog with nothing to
        # remove it, which is the opposite of what a rollback promises.
        #
        # Always remove first. write_queries MERGES by SQL text, so writing
        # the saved rows over the pushed ones adds to them and removes
        # nothing: the only way back to "no rows" is taking the aspect off,
        # and the only way back to "these rows" is taking it off and writing
        # them again.
        rows = content.get("queries")
        store.transport.remove_aspect(entry, QUERIES_ASPECT_KEY)
        if rows:
            store.transport.write_queries(entry, rows)
    removed = sum(1 for _t, c in decided.restore if not c.get("queries"))
    print(f"\nrestored {len(decided.restore)} entry/entries"
          + (f"; the queries aspect was taken off {removed} of them, because there "
             "were no worked examples on it before the push" if removed else ""))
    return 1 if decided.refused else 0


def _cmd_spec_push(args: argparse.Namespace) -> int:
    """A spec repo into Knowledge Catalog, then read back and compared.

    The write and the check are one command because a write nobody read back
    is the failure this tool exists to catch. The check is not a formality:
    the catalog cuts an over-long overview silently, and an aspect write that
    returns 200 can store something else.
    """
    from .export import build_arms, read_catalog, read_spec, write_catalog

    from .export import rename_tables

    spec = read_spec(args.spec)
    print(spec.summary())
    mapping = _room_mapping(args, spec)
    if mapping:
        spec = rename_tables(spec, mapping)
        print(f"  written about {len(mapping)} evaluation view(s) instead of "
              "the production tables")
    tables = args.tables or spec.tables
    if not tables:
        print("nothing to write: the spec names no tables and none were given")
        return 2

    store = _catalog_store(args)
    before = build_arms(spec, tables, args.model)[1]
    print(f"\nspec arm {before.arm_id}")

    report = write_catalog(store, spec, tables,
                           examples=not args.no_examples,
                           dry_run=args.dry_run,
                           prune_terms=args.prune_terms,
                           previous_out=args.previous_out)
    print("")
    print(report.text())
    if args.dry_run:
        print("--dry-run: nothing was written")
        return 0

    # What a READER gets, which is a different call from what the catalog
    # holds and does not agree with it: the aspect cuts silently past 100 KiB
    # and the bundle can be cut by a budget. A push that only checks what is
    # held reports success on a page nobody can read.
    if not args.no_served_check:
        from .served import check, summary
        print("")
        print(summary(check(store, tables)))

    # Against what was WRITTEN, not against the input. Comparing to the input
    # would fail every time something was refused, and a check that always
    # fails is a check a person learns to ignore.
    expected = build_arms(report.stored, tables, args.model)[1]
    after = build_arms(read_catalog(store, tables), tables, args.model)[1]
    print(f"\nwritable part of the spec {expected.arm_id}, read back {after.arm_id}")
    if after.arm_id != expected.arm_id:
        print("  the catalog does NOT read back as what was written. A round "
              "in catalog mode would not be the same experiment as one in "
              "file mode, so it is refused rather than reported as a "
              "difference somebody has to notice.")
        return 1
    print("  the catalog reads back as what was written")
    if expected.arm_id != before.arm_id:
        print(f"  it is NOT the whole spec: the arm from files is "
              f"{before.arm_id}. Everything refused above is missing from "
              "the catalog arm, so a round in catalog mode measures less "
              "context than one in file mode")

    if args.provenance:
        _write_provenance(store, tables, args, expected)
    return 0


def _cmd_classes(args: argparse.Namespace) -> int:
    """What each class of context would cost to price, before anything is asked.

    A plan, not a run. Pricing four classes is four more paired comparisons
    over the working questions, and a person should see that number and the
    arms it would build before agreeing to it.
    """
    from .classes import ladder, summary
    from .export import build_arms

    spec = _spec_for(args)
    print(spec.summary())
    mapping = _room_mapping(args, spec)
    if mapping:
        from .export import rename_tables
        spec = rename_tables(spec, mapping)
    _control, treatment = build_arms(spec, args.tables, args.model)

    working = args.working
    if working is None and args.golds:
        _require_file(args.golds, "the gold set CSV (--golds)")
        from .leakage import split_golds
        ids = [row["id"] for row in csv.DictReader(open(args.golds, newline=""))]
        working = len(split_golds(ids, args.holdout_share, args.seed)[0])
    if working is None:
        print("give --golds or --working N: the refusal below 100 working "
              "questions needs to know how many there are")
        return 2

    removed = ladder(treatment)
    print("")
    print(f"  full arm {treatment.arm_id}")
    print(summary(removed, working))
    if removed:
        print("")
        print(f"  cost: {len(removed)} more arm(s) over {working} question(s), "
              f"which is {len(removed) * working} more model calls per repeat")
    if not args.run or not removed:
        return 0

    # The working questions only. Each class costs one more arm over the same
    # questions, and the held-out set is read once per round; spending that
    # one read on a diagnostic is how a holdout becomes practice questions.
    from .classes import price, priced_text
    from .confirm import confirm_paid
    from .harness import evaluate
    from .leakage import split_golds
    from .round import Gold, _ask_arm, _read_checkpoint
    from .runners.ca_api import CaApiRunner

    _require_file(args.golds, "the gold set CSV (--golds)")
    golds = [Gold(row["id"], row["question"], row["gold_sql"])
             for row in csv.DictReader(open(args.golds, newline=""))]
    working_ids, _held = split_golds([g.id for g in golds],
                                     args.holdout_share, args.seed)
    subset = [g for g in golds if g.id in set(working_ids)]

    runner = CaApiRunner(project=args.project, location=args.location)
    sheet = runner.cost_sheet(n_questions=len(subset),
                              n_arms=len(removed) + 1, k=1)
    print("")
    print(sheet.line())
    decision = confirm_paid("", budget=args.budget, yes=args.yes,
                            argv=["classes"] + sys.argv[2:])
    print(decision.reason)
    if not decision.allowed:
        return 2

    executor = _executor_for(args)
    done = _read_checkpoint(args.checkpoint)
    if done:
        print(f"  {len(done)} answer(s) already on disk; the full arm's are "
              "reused rather than paid for twice")
    answers, cache = {}, {}

    def progress(label, index, total, scored_id, reused):
        print(f"  [{label}] {'cached' if reused else 'asked '} "
              f"{index}/{total} {scored_id}", flush=True)

    rows = _ask_arm(subset, treatment, runner.answer, 1, progress,
                    args.checkpoint, done, answers)
    full = evaluate(executor, rows, gold_cache=cache)

    reports = {}
    for item in removed:
        arm_rows = _ask_arm(subset, item.arm, runner.answer, 1, progress,
                            args.checkpoint, done, answers)
        reports[item.name] = (evaluate(executor, arm_rows, gold_cache=cache),
                              item.held)

    print("")
    print(priced_text(price(full, reports), len(subset)))

    # THE SECOND AXIS, and it answers a different question from the accuracy
    # above. An agent without good context explores more: more schema reads,
    # a wider scan to reach the same row. So a class can pay for itself in
    # query bill even when its accuracy verdict reads NOT PROVEN.
    #
    # `answers` holds every Prediction this run produced, keyed by (arm id,
    # scored id), and each carries the job_id its SQL ran under. That is all
    # the ledger needs, so the cost comes back for a metadata read rather
    # than another paid round.
    if getattr(args, "cost", False) and args.project:
        from .cost import ledger_bytes, per_arm, usd_for_bytes
        # Correctness comes from the scored REPORTS, not from the answer
        # records. THE BUG THIS FIXES, found 2026-09-14 by an outside review:
        # the record built in round.py carries arm, id, pred_sql, citations,
        # matched_example, job_id, thought_chars, retries and error_class --
        # and no `correct`. So `record.get("correct") or "0"` was always "0",
        # every arm reported zero correct answers, and cost per CORRECT answer
        # could never produce a number at all.
        right: dict[tuple[str, str], bool] = {}
        for arm_id, report in [(treatment.arm_id, full)] + [
                (item.arm.arm_id, reports[item.name][0]) for item in removed]:
            for entry in report.results:
                right[(arm_id, entry.id)] = bool(entry.execution_correct)

        rows_for_cost = []
        for (arm_id, scored_id), record in answers.items():
            job = str(record.get("job_id") or "").strip()
            if not job:
                continue
            rows_for_cost.append({
                "arm": _class_label(arm_id, treatment, removed),
                "job_id": job,
                "correct": "1" if right.get((arm_id, scored_id)) else "0"})
        billed = ledger_bytes(args.project, [r["job_id"] for r in rows_for_cost])
        print("")
        print(per_arm(rows_for_cost, billed).text())
        if not billed:
            print("    the ledger returned nothing for these job ids. That is "
                  "not a free round: the jobs may be outside the 180-day "
                  "window or have run under an identity this reader cannot see")
    return 0


def _class_label(arm_id: str, full_arm, removed) -> str:
    """Which class arm a hash belongs to, for the cost table.

    The checkpoint keys arms by hash, and a hash tells a reader nothing. The
    full arm is named first so it sorts above the classes it is compared
    against.
    """
    if arm_id == full_arm.arm_id:
        return "0 full arm"
    for item in removed:
        if item.arm.arm_id == arm_id:
            return f"minus {item.name}"
    return arm_id[:12]


def _cmd_bisect(args: argparse.Namespace) -> int:
    """Which rule broke that question. Halve the rule set; do not serialise.

    The arms are cheap because only the BROKEN questions are asked, and few
    because the set halves: `ceil(log2(R))` arms for R rules rather than R.
    What it costs instead is the questions themselves, which is why the trade
    is printed and consented to before anything is asked.
    """
    from .bisect import (Rule, arms_needed, broke_in_run, find, record_spent,
                         refusal, rules_in, spec_with, spend_note)
    from .confirm import confirm_paid
    from .export import build_arms
    from .harness import evaluate
    from .round import Gold, _ask_arm
    from .runners.ca_api import CaApiRunner

    _require_file(args.run, "the round's scored rows (--run)")
    rows = list(csv.DictReader(open(args.run, newline="")))
    if not rows:
        print(f"{args.run} has no scored rows")
        return 2

    # The safety word decides whether there is anything to do at all, and it
    # is derived from the rows rather than trusted from a flag: a SAFE round
    # that somebody typed REGRESSION for would pay for arms that find nothing.
    found = broke_in_run(rows)
    stop = refusal("SAFE" if not found else "REGRESSION")
    if stop:
        print(f"  nothing to bisect: {stop}")
        return 0

    wanted = ([q.strip() for q in args.questions.split(",") if q.strip()]
              if args.questions else found)
    unknown = [q for q in wanted if q not in found]
    if unknown:
        print(f"  these were not broken by the context in {args.run}: "
              f"{', '.join(unknown)}")
        print("  bisecting a question the context did not break has nothing "
              "to find. Drop --questions to use the round's own list")
        return 2
    print(f"  the context broke {len(found)} reserved question(s): "
          f"{', '.join(found)}")

    spec = _spec_for(args)
    rules = rules_in(spec)
    if len(rules) < 2:
        print(f"  the spec carries {len(rules)} rule line(s); with fewer than "
              "two there is nothing to narrow and the round already names it")
        return 0
    per_question = arms_needed(len(rules))
    print(f"  {len(rules)} rule line(s) -> at most {per_question} arm(s) per "
          f"question, sometimes one more to confirm")

    # The same marker the module strips, not a bare "#": two copies of the
    # repetition convention would disagree the day it changes, and the
    # symptom would be a reserved count that is three times too big.
    from .round import REPEAT_MARKER
    reserved = len({str(r.get("id") or "").split(REPEAT_MARKER)[0] for r in rows
                    if str(r.get("split") or "").lower().startswith(("hold",
                                                                     "reserv"))})
    print("")
    print(spend_note(wanted, reserved))

    golds = {g.id: g for g in
             (Gold(row["id"], row["question"], row["gold_sql"])
              for row in csv.DictReader(open(args.golds, newline="")))}
    missing = [q for q in wanted if q not in golds]
    if missing:
        print(f"  not in {args.golds}: {', '.join(missing)}")
        return 2

    runner = CaApiRunner(project=args.project, location=args.location)
    sheet = runner.cost_sheet(n_questions=len(wanted),
                              n_arms=per_question + 1, k=1)
    print("")
    print(sheet.line())
    if args.estimate_only:
        print("  --estimate-only: nothing was asked")
        return 0
    decision = confirm_paid("", budget=args.budget, yes=args.yes,
                            argv=["bisect"] + sys.argv[2:])
    print(decision.reason)
    if not decision.allowed:
        return 2

    executor = _executor_for(args)
    _control, _full = build_arms(spec, args.tables, args.model)
    asked = {"arms": 0}

    def reproduces_for(question: str):
        """True when `question` is still wrong with only `subset` in context.

        Each subset is a real arm built through `build_arms`, so an arm here
        is the same object a round would have measured and its hash is
        comparable with one.
        """
        gold = golds[question]

        def reproduces(subset: list[Rule]) -> bool:
            narrowed = spec_with(spec, {rule.key for rule in subset})
            _c, arm = build_arms(narrowed, args.tables, args.model)
            asked["arms"] += 1
            print(f"  [{question}] arm {arm.arm_id} with "
                  f"{len(subset)} rule(s)", flush=True)
            scored = evaluate(executor,
                              _ask_arm([gold], arm, runner.answer, 1))
            # `broke` means the question is WRONG with the context, so a
            # subset reproduces the break when the answer is still wrong.
            right = any(entry.get("execution_correct")
                        for entry in scored.per_question)
            print(f"      {'right' if right else 'wrong'}"
                  f" -> {'not here' if right else 'in this half'}")
            return not right

        return reproduces

    print("")
    results = [find(question, rules, reproduces_for(question))
               for question in wanted]
    print("")
    for result in results:
        print(result.line())
    print(f"\n  {asked['arms']} arm(s) asked in total")

    # The spent file is written even when nothing was found. The questions
    # were read either way, and a holdout read that goes unrecorded is the
    # leak this flag exists to stop.
    if args.spent_out:
        combined = record_spent(args.spent_out, wanted,
                                _read_spent(args.spent_out))
        print(f"  {len(combined)} spent id(s) -> {args.spent_out}")
        print("  pass it to `split --spent` so they are never reserved again")
    else:
        print("  no --spent-out given, so nothing recorded these as spent. "
              "They have still been read")
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run
    return run()


def _cmd_build_examples(args: argparse.Namespace) -> int:
    """Worked examples from the working questions, as a file anyone can rebuild.

    The bank is a committed artifact of the treatment arm, so it cannot be
    something a person made once by hand: six months on nobody can say which
    golds it came from or which split protected it. This is that step as a
    command, and re-running it on the same golds and the same seed writes the
    same file.
    """
    from .leakage import split_golds
    from .round import Gold, examples_from_curate

    _require_file(args.golds, "the gold set CSV (--golds)")
    golds = [Gold(row["id"], row["question"], row["gold_sql"])
             for row in csv.DictReader(open(args.golds, newline=""))]
    if not golds:
        print(f"{args.golds} has no rows")
        return 2

    # The same split the round uses, from the same seed and share, so the bank
    # is built against the questions the round will hold out. A bank built
    # against a different split protects nothing.
    curate_ids, holdout_ids = split_golds([g.id for g in golds],
                                          args.holdout_share, args.seed)
    curate = [g for g in golds if g.id in set(curate_ids)]
    holdout = [g for g in golds if g.id in set(holdout_ids)]
    print(f"{len(curate)} working question(s), {len(holdout)} held out "
          f"(seed {args.seed}, share {args.holdout_share})")

    kept, refused = examples_from_curate(curate, holdout)
    for gold_id, why in refused:
        print(f"  refused {gold_id}: {why}")
    if not kept:
        print("nothing was kept, so no bank was written")
        return 2

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        for question, sql in kept:
            handle.write(json.dumps({"question": question, "sql": sql}) + "\n")
    print(f"{len(kept)} example(s) kept, {len(refused)} refused -> {out}")
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    """One question across every round. Free: it reads files already on disk.

    The verb a thousand-question gold set needs and thirty questions do not.
    Per-round pages say "5 fixed, 0 broke". None of them says whether this
    question has been broken once, three rounds running, or never, and that
    is the difference between a rule to fix and a wobble to ignore.
    """
    from .history import build, lines, write_csv

    workspace = pathlib.Path(args.workspace)
    if not workspace.is_dir():
        raise SystemExit(f"no workspace at {workspace}")
    history = build(workspace)
    for line in lines(history, show=args.show):
        print(line)
    if args.out and history["rows"]:
        written = write_csv(history, pathlib.Path(args.out))
        print("")
        print(f"  {written} rows -> {args.out}")
        print("  one row per question, one column per round. Sort by "
              "times_broken in a spreadsheet")
    return 0


def _cmd_worklist(args: argparse.Namespace) -> int:
    from .export import build_arms, read_spec
    from .worklist import build

    _require_file(args.run, "the round's run CSV (--run)")
    with open(args.run, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    spec = read_spec(args.spec)
    _control, treatment = build_arms(spec, args.tables)
    # Named by what a citation says, never by an index. An answer cites an
    # example by its question text - `example:revenue by month, 2023` - so an
    # invented `ex_q0` joins to nothing and every item reads "cited 0". That
    # is not a harmless miss: an uncited item is the cheapest thing the
    # worklist tells a person to delete, so a broken join sends them to
    # delete context that was working. The same mistake was fixed for terms
    # once already, where the id was `g_completed` on one run and
    # `g_completed_order` on the next.
    items: list[tuple[str, str, str]] = []
    for term in treatment.terms:
        items.append((term.name, "term", term.definition))
    for example in treatment.examples:
        items.append((example.question, "example",
                      f"{example.question}\n{example.sql}"))
    for name, body in spec.rules:
        items.append((name, "rules file", body))

    seen = [name for name, _kind, _text in items]
    clashes = sorted({name for name in seen if seen.count(name) > 1})
    if clashes:
        print(f"{len(clashes)} item name(s) appear more than once, so their "
              "counts are merged and cannot be told apart: "
              + ", ".join(clashes[:3]))

    print(build(rows, items).text())
    return 0


def _cmd_share(args: argparse.Namespace) -> int:
    from .share import Pseudonyms, export, summary

    _require_file(args.verdict, "the round's verdict JSON (--verdict)")
    report = json.loads(pathlib.Path(args.verdict).read_text())
    round_report = report.get("round", report)
    tables = report.get("tables") or {}
    gates = [tuple(g) for g in (report.get("gates") or [])]

    names = Pseudonyms.load(args.map) if args.map else Pseudonyms()
    forbidden = list(args.forbid or [])
    if args.forbid_file:
        _require_file(args.forbid_file, "the forbidden-strings file (--forbid-file)")
        forbidden += [line.strip() for line in
                      pathlib.Path(args.forbid_file).read_text().splitlines()
                      if line.strip() and not line.startswith("#")]

    payload = export(round_report, tables, gates, names, forbidden)
    pathlib.Path(args.out).write_text(json.dumps(payload, indent=1, sort_keys=True))
    if args.map:
        names.save(args.map)
    print(summary(payload))
    print(f"\n  wrote {args.out}")
    if args.map:
        print(f"  the name map stays here: {args.map}")
    return 0


def _cmd_leak(args: argparse.Namespace) -> int:
    if args.context:
        _require_file(args.context, "context file (--context)")
    if getattr(args, "context_per_question", None):
        _require_file(args.context_per_question, "retrieved context (--context-per-question)")
    if not (args.context or getattr(args, "context_per_question", None)):
        print("leak needs --context FILE, --context-per-question FILE.json, or both")
        return 2
    _require_file(args.predictions, "predictions/questions CSV (--predictions)")
    golds = [
        (row["id"], row.get("question", ""), row["gold_sql"])
        for row in csv.DictReader(open(args.predictions, newline=""))
    ]
    context = pathlib.Path(args.context).read_text() if args.context else ""
    retrieved = None
    if getattr(args, "context_per_question", None):
        from .context import MappingContextSource
        retrieved = MappingContextSource.from_json(args.context_per_question).mapping
        served = sum(1 for gold_id, _, _ in golds if gold_id in retrieved)
        print(f"checking {served} of {len(golds)} questions against their own retrieved "
              f"context, and the rest against everything retrieved")
    report = check_leakage(golds, context, retrieved)
    print(report.summary())
    # Non-zero so CI fails: a leaked eval set makes every later number a lie.
    return 0 if report.clean else 1


def _cmd_next(args: argparse.Namespace) -> int:
    """Where the loop stands, and the one command to run next.

    The whole answer in one screen: the rail, why this step, what it will run,
    what stops it, and what it costs. `--json` for anything that wants to read
    it rather than look at it."""
    if not args.workspace:
        print("next needs --workspace DIR (or $CONTEXTLIFT_WORKSPACE)")
        return 2
    from .loop import api_next, shell_line
    nxt = api_next(pathlib.Path(args.workspace))
    if args.json:
        print(json.dumps(nxt, indent=1))
        return 0
    action = nxt["action"]
    # every path absolute, the workspace too: the line runs from any directory
    prefix = ["contextlift", "--workspace", str(pathlib.Path(args.workspace).resolve())]
    print(nxt["loop"]["rail"])
    print()
    print(action["label"])
    print(f"  {nxt['why']}")
    for line in nxt["blockers"]:
        print(f"  ! {line}")
    if action["label_note"] and not nxt["blockers"]:
        print(f"  ! {action['label_note']}")
    print()
    if action["argvs"]:
        for argv in action["argvs_absolute"]:
            print("  $ " + shell_line([*prefix, *argv]))
        tags = []
        if action["cost_note"]:
            tags.append(action["cost_note"])
        if not action["runnable"]:
            tags.append("not ready to run yet")
        for tag in tags:
            print(f"  ({tag})")
    elif action["kind"] == "human":
        # a cold run reached round 2 only because the analyst went reading
        # --help. The step that says "yours" has to name the commands, or the
        # loop's own story - each round builds on the last - is unreachable
        print("  this step is yours: read the draft, then install it. Two commands do it:")
        print("    contextlift draft-rules --report REPORT --predictions ARM --ids curate_ids.txt \\")
        print("        --out context/rules_draft.md      # one failure bucket at a time")
        print("    contextlift context-write --source-file context/rules_draft.md \\")
        print("        --to context/rules.md --add        # --add keeps what the last round wrote")
        print("  then run the leak check. Editing context/rules.md by hand works too, and is")
        print("  the one step nobody else can repeat for you.")
    for other in nxt["also"]:
        print()
        print(f"or: {other['label']}")
        if other["label_note"]:
            print(f"  {other['label_note']}")
        for argv in other["argvs_absolute"]:
            print("  $ " + shell_line([*prefix, *argv]))
        if other["cost_note"]:
            print(f"  ({other['cost_note']})")
    return 0


def _cmd_evalroom(args: argparse.Namespace) -> int:
    from .evalproject import arm_tables, billing_warning, plan
    try:
        room = plan(args.eval_project, args.dataset, args.tables, location=args.location)
    except ValueError as error:
        print(str(error))
        return 2
    print(room.text())
    print("")
    print("the arms then reference these, never the production tables:")
    for name in arm_tables(args.eval_project, args.dataset, args.tables):
        print(f"  {name}")
    warning = billing_warning(args.eval_project, args.harness_project)
    if warning:
        print("")
        print(f"warning: {warning}")
    return 0


def _cmd_sentinel(args: argparse.Namespace) -> int:
    from .sentinel import materials, plan, probe, report
    mats = materials(args.table, column=args.column, join_partner=args.join_partner)
    if not args.run:
        print(plan(mats, args.table))
        print("")
        print("nothing was written and nothing was asked. Add --run once the writes "
              "above are in place.")
        return 0
    if not args.project:
        print("sentinel --run needs --project: the chat calls are billed to it")
        return 2
    from .backends import refuse_forbidden_project
    refuse_forbidden_project(args.project, "the sentinel's chat calls")
    from .runners import Arm
    from .runners.ca_api import CaApiRunner
    runner = CaApiRunner(project=args.project, location=args.location,
                         max_billed_bytes=args.max_billed_bytes)
    bare = Arm(label="bare", tables=[args.table], model_alias=args.model)
    if not bare.is_bare():
        raise AssertionError("the probe's arm must carry table references and nothing else")
    print(f"asking {len(mats)} question(s) against a bare arm over {args.table}")
    print(runner.cost_sheet(n_questions=len(mats), n_arms=1, k=1).line())
    stop = _paid_gate(args, "the sentinel probe")
    if stop is not None:
        return stop
    print("")
    print(report(probe(mats, lambda question: runner.ask(question, bare))))
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import EXTRAS, explain, format_report, preflight
    if args.explain:
        for line in explain(args.explain):
            print(line)
        return 0
    unknown = [extra for extra in (args.needs or []) if extra not in EXTRAS]
    if unknown:
        print(f"unknown extra(s): {', '.join(unknown)}; known: {', '.join(sorted(EXTRAS))}")
        return 2
    text, code = format_report(preflight(
        project=args.project, workspace=args.workspace, needs=tuple(args.needs or ())))
    print(text)
    return code


def _cmd_index(args: argparse.Namespace) -> int:
    if not args.workspace:
        print("index needs --workspace DIR (or $CONTEXTLIFT_WORKSPACE)")
        return 2
    from .workspace import rebuild_index
    index = rebuild_index(args.workspace)
    print(f"index -> {pathlib.Path(args.workspace) / 'index.json'}  "
          f"({len(index['runs'])} runs, {len(index['arms'])} arms)")
    if getattr(args, "show", False):
        _print_index(index)
    return 0


def _print_index(index: dict) -> None:
    """The arms table and the pins, so reading a workspace needs no JSON tool.

    An arm is one setup being measured; the table exists to make the one
    thing that must differ (context) and the things that must not
    (questions, model) visible side by side.
    """
    looks = index.get("holdout_looks", 0)
    print(f"holdout: {'SEALED' if not looks else 'OPENED'} "
          f"({looks} look{'' if looks == 1 else 's'})")
    arms = index.get("arms", [])
    if not arms:
        print("no arms yet: an arm appears when generate or import writes predictions")
        return
    pins = index.get("pins", {}) or {}
    explicit = pins.get("explicit", {}) or {}
    header = ("arm", "label", "context", "questions", "model", "score runs", "spent")
    rows = []
    for arm in arms:
        context = arm.get("context_sha256")
        note = ""
        for slot in ("baseline", "candidate"):
            if pins.get(slot) == arm["arm_id"]:
                note = f"  <- {slot} ({'pinned' if explicit.get(slot) else 'auto'})"
        rows.append(((arm["arm_id"], arm.get("label") or "", (context or "(none)")[:8],
                      (arm.get("questions_sha256") or "")[:8], arm.get("model") or "",
                      str(len(arm.get("score_runs") or [])),
                      f"${arm.get('cost_usd') or 0:.4f}"), note))
    widths = [max(len(header[i]), max(len(r[0][i]) for r in rows)) for i in range(len(header))]
    print()
    print("  ".join(h.ljust(w) for h, w in zip(header, widths)).rstrip())
    for cells, note in rows:
        print("  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip() + note)
    for warning in index.get("warnings", []):
        print(f"warning: {warning}")


def _cmd_pin(args: argparse.Namespace) -> int:
    """Name the baseline or candidate arm every comparison is against.

    Explicit on purpose: nothing ever compares "vs the previous run" by
    itself. A run id is accepted and resolved to its arm.
    """
    if not args.workspace:
        print("pin needs --workspace DIR (or $CONTEXTLIFT_WORKSPACE)")
        return 2
    from .workspace import read_pins, rebuild_index, write_pins
    index = rebuild_index(args.workspace)
    arms = {arm["arm_id"] for arm in index["arms"]}
    runs = {run["run_id"]: run for run in index["runs"]}
    target = args.id
    if target in runs:
        target = runs[target]["arm_id"]
    if target not in arms:
        print(f"{args.id}: not a run id or arm id with predictions in this "
              f"workspace (see index.json 'arms')")
        return 2
    pins = read_pins(args.workspace)
    pins[args.slot] = target
    write_pins(args.workspace, pins)
    print(f"{args.slot} -> arm {target}")
    return 0


def _cmd_profile(args: argparse.Namespace) -> int:
    """Write the value dictionary: what each category column holds, exactly."""
    from .profile import profile_values, render_values
    executor = _executor_for(args)
    scope = _metadata_scope(args, executor, "profile", views=1)
    if isinstance(scope, int):
        return scope
    tables, datasets, n_datasets = scope
    if is_bigquery(args.db):
        print("profile on BigQuery: one APPROX_COUNT_DISTINCT query per table, then "
              "one GROUP BY per column under --max-values; each stops at --max-bytes")
    columns = profile_values(executor, max_values=args.max_values, report=print,
                             datasets=datasets, tables=tables or None)
    out = pathlib.Path(args.out or "values.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_values(columns))
    for c in columns:
        print(f"  {c.table}.{c.column}: {c.distinct} values over {c.rows} rows")
    print(f"values -> {out}  ({len(columns)} columns{_across(n_datasets)}; give it to "
          f"generate --context, or paste it into rules.md)")
    _record_sql_spend(args, executor)
    return 0


def _cmd_schema(args: argparse.Namespace) -> int:
    """Write the schema text the model sees: CREATE TABLE statements, every
    dataset of the scope in one file."""
    executor = _executor_for(args)
    scope = _metadata_scope(args, executor, "schema", views=3)
    if isinstance(scope, int):
        return scope
    tables, datasets, n_datasets = scope
    text = schema_text(executor, tables or None, datasets)
    out = pathlib.Path(args.out or "schema.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    n_tables = text.count("CREATE TABLE")
    print(f"schema -> {out}  ({n_tables} tables{_across(n_datasets)}; give it to generate --schema)")
    _record_sql_spend(args, executor)
    return 0


def _cmd_draft_context(args: argparse.Namespace) -> int:
    """One document per table, from the schema, into a directory a routing
    context source reads. No model call; on BigQuery three metadata queries
    per dataset."""
    from .draft import READ_BUDGET_CHARS, draft_documents, draft_summary
    executor = _executor_for(args)
    scope = _metadata_scope(args, executor, "draft-context", views=3)
    if isinstance(scope, int):
        return scope
    tables, datasets, n_datasets = scope
    described = describe_tables(executor, tables or None, datasets)
    if not described:
        print("no tables found" + (" among the tables asked for" if tables else ""))
        return 2
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    documents = draft_documents(described, style=args.name_style)
    for filename, text in documents.items():
        (out / filename).write_text(text)
    counts = draft_summary(described, documents)
    print(f"drafted {counts['documents']} documents{_across(n_datasets)} -> {out}   "
          f"({counts['columns']} columns, {counts['described_columns']} with a description; "
          f"{len(counts['tables_without_description'])} tables have none)")
    print("  drafted from the schema, not reviewed: each file says so in its second line; "
          "fill the Rules section before measuring against it")
    print("  these pages use the schema's own words: a question set written from the SQL "
          "will match them by vocabulary, not by meaning; measure routing on questions "
          "people asked")
    if counts["over_budget"]:
        named = ", ".join(f"{name} {chars}" for name, chars in counts["over_budget"][:5])
        more = len(counts["over_budget"]) - 5
        print(f"  {len(counts['over_budget'])} documents over {READ_BUDGET_CHARS} characters, "
              f"where a catalog read truncates silently: {named}"
              + (f", and {more} more" if more > 0 else "")
              + "; put the Rules section first in those, or split the table")
    print(f"  one file per table is what a catalog holds; for a single-context arm: "
          f"cat {out}/*.md > context/draft.md")
    _record_sql_spend(args, executor)
    return 0


def _cmd_catalog_pull(args: argparse.Namespace) -> int:
    """Read each table's document out of a live catalog into a directory, so
    the arm is a file anyone can re-run without credentials. `--out` gets
    the served bundle, byte-exact: YAML text with the overview inside it
    as a quoted scalar, what an agent reads and not a file a person can
    edit or push back. `--pages` gets the stored overview aspect with the
    push stamp removed, the file a person edits and `catalog-push --docs`
    sends back. Free: catalog reads are metadata calls, not billed queries.

    Every table is read twice, the served bundle and the stored aspect, and
    the two lengths are compared. A page longer than the budget is served as
    a few hundred characters of its opening with no error, so the served
    length on its own cannot say whether the entry is thin or the reader's
    budget is too low. The pull does not retry at a higher budget by itself:
    that would hide what this caller's readers are getting. It names the
    flag and a value that would work instead."""
    from .catalog import (KnowledgeCatalogStore, ScopeCheckFailed, budget_that_serves,
                          short_serve_note, transport_for)
    from .push import unstamped
    tables = [t.strip() for t in (args.tables or "").split(",") if t.strip()]
    if args.tables_file:
        tables += _read_table_list(args.tables_file)
    if not tables:
        print("catalog-pull needs --tables A.B,C.D or --tables-file FILE")
        return 2
    store = KnowledgeCatalogStore(
        transport_for(args.project, args.location), project=args.project, location=args.location,
        entry_group=args.entry_group, entry_id_template=args.entry_id,
        entry_id_pattern=args.entry_pattern, context_budget=args.budget)
    if store.entry_id_template is None:
        # the search path has to prove its scoping first; a built name is
        # checked by reading the first one instead
        try:
            store.check_scope(args.known_good_table or tables[0])
        except ScopeCheckFailed as failed:
            print(failed)
            return 2
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pages_dir = pathlib.Path(args.pages) if args.pages else None
    if pages_dir:
        pages_dir.mkdir(parents=True, exist_ok=True)
    joiner = "." if args.name_style == "dot" else "__"
    lengths = []
    pages_written = 0
    short_serves = []                   # (table, served characters, stored characters)
    nothing_at_all = []                 # served nothing and stores nothing either
    for table in tables:
        text = store.document_for(table)
        dataset, _, name = table.rpartition(".")
        filename = f"{dataset}{joiner}{name}.md" if dataset else f"{name}.md"
        if text:                      # no file for nothing: an empty file reads as a document
            (out / filename).write_text(text)
        lengths.append((table, len(text)))
        line = f"  {table}: {len(text)} chars" + ("   (nothing found)" if not text else "")
        # the stored aspect, through the same read the push verifies with, for
        # every table and not only for --pages: it is the only thing that says
        # whether a short served body is a thin entry or an unserved page
        overview, note = _stored_overview_of(store, table)
        if pages_dir:
            if overview:
                page = unstamped(overview)
                (pages_dir / filename).write_text(page if page.endswith("\n") else page + "\n")
                pages_written += 1
                line += f"   overview {len(overview)} chars -> {pages_dir / filename}"
            else:
                line += f"   ({note})"
        short = short_serve_note(len(text), len(overview), args.budget)
        if short:
            short_serves.append((table, len(text), len(overview)))
            line += f"   {short}"
        if not text and not overview:
            nothing_at_all.append(table)
        print(line)
    found = [n for _, n in lengths if n]
    print(f"pulled {len(found)} of {len(tables)} documents -> {out}   "
          f"(shortest {min(found) if found else 0}, longest {max(found) if found else 0} chars; "
          f"budget {args.budget})")
    print("the files under --out are the served bundle (YAML text, what an agent gets; kept "
          "byte-exact); --pages writes the editable overview")
    if pages_dir:
        print(f"pages -> {pages_dir} ({pages_written} of {len(tables)} tables carry an overview; "
              f"edit, review, then catalog-push --docs {pages_dir})")
    if short_serves:
        named = ", ".join(f"{table} {served} of {stored}" for table, served, stored in short_serves[:5])
        more = len(short_serves) - 5
        largest = max(stored for _, _, stored in short_serves)
        print(f"  {len(short_serves)} of {len(tables)} tables were served less than their entry "
              f"stores at budget {args.budget}: {named}"
              + (f", and {more} more" if more > 0 else ""))
        print(f"  the pages are in the catalog and a reader is not getting them; pull again with "
              f"--budget {budget_that_serves(largest)} to serve the largest of them")
        # a reader who checks the new lengths finds served above stored and
        # reads it as still broken; it is what a whole page looks like
        print("  once a page is served whole, its bundle is LARGER than the stored overview, "
              "because the bundle carries the schema too; that is not a defect")
    if len(found) < len(tables):
        # one table missing is one table; every built name missing is the
        # template, the project or the location, and then nothing was pulled
        mismatch = store.template_mismatch()
        if mismatch:
            print(mismatch)
            return 2
    # only for a table that served nothing AND stores nothing: one that served
    # nothing while its entry holds a page is on the short serve list above,
    # and this advice would send its reader after a spelling
    if nothing_at_all:
        if store.entry_id_template:
            print("  a table with nothing found has no entry at its built name in this group, or "
                  "no overview: check the dataset.table spelling; catalog-list --entry-group "
                  f"{store.entry_group} shows the group's tables")
        else:
            print("  a table with nothing found is not documented in the catalog, or the search "
                  "term does not match its entry: --entry-pattern sets the term")
    return 0


def _stored_overview_of(store, table: str) -> tuple[str, str]:
    """(the overview aspect as the catalog stores it, or ""; why it is empty).
    A lookup of a stale entry answers 0 characters; get_entry on it raises,
    which killed one dry run with a traceback. Here it is a note on the
    table's line and the pull goes on."""
    entry = store.resolve(table)
    if not entry:
        return "", "no entry"
    try:
        return store.stored_overview(entry) or "", "no overview aspect"
    except Exception as error:  # noqa: BLE001 - one table's read, reported on its line
        from .catalog import _is_not_found
        if _is_not_found(error):
            return "", "no entry at its built name"
        return "", f"overview read failed: {type(error).__name__}: {str(error)[:80]}"


def _cmd_catalog_route(args: argparse.Namespace) -> int:
    """Route each question through the catalog's search and hand the model
    only those tables' documents. Three files: the routes, the per-question
    context generate reads, and a routing CSV score-routing grades. Free."""
    import json as _json
    from collections import Counter
    from .catalog import KnowledgeCatalogStore, transport_for
    from .context import DirectoryContextSource
    from .mine import tables_named
    if "{question}" not in args.search_pattern:
        print("--search-pattern must contain {question}")
        return 2
    with open(args.questions, newline="") as handle:
        reader = csv.DictReader(handle)
        _require_columns(args.questions, reader, ("id", "question"),
                         "a golds or questions CSV; gold_sql is optional and fills gold_table")
        rows = list(reader)
    keep = _load_catalog(args.catalog) if args.catalog else None
    store = KnowledgeCatalogStore(transport_for(args.project, args.location),
                                  entry_group=args.entry_group, search_pattern=args.search_pattern)
    routes: dict[str, list[str]] = {}
    dropped_by_question: dict[str, list[str]] = {}
    outside_by_question: dict[str, list[str]] = {}
    for row in rows:
        dropped: list[str] = []
        outside: list[str] = []
        routes[row["id"]] = store.route(row["question"], top=args.top, within=args.within,
                                        keep=keep, dropped=dropped, outside_group=outside)
        if dropped:
            dropped_by_question[row["id"]] = dropped
        if outside:
            outside_by_question[row["id"]] = outside
    source = DirectoryContextSource(args.docs, routes)
    context = {row["id"]: source.for_question(row["id"], row["question"]) for row in rows}
    # under a workspace the recorder set these into the run directory
    args.routes_out = args.routes_out or "routes.json"
    args.context_out = args.context_out or "context_per_question.json"
    args.routing_out = args.routing_out or "routing.csv"
    pathlib.Path(args.routes_out).write_text(_json.dumps(routes, indent=1))
    pathlib.Path(args.context_out).write_text(_json.dumps(context, indent=1))
    with open(args.routing_out, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "question", "gold_table", "pred_table"])
        for row in rows:
            named = tables_named(row.get("gold_sql", "")) if row.get("gold_sql") else []
            writer.writerow([row["id"], row["question"], named[0] if named else "",
                             (routes[row["id"]] or [""])[0]])
    unrouted = sum(1 for r in routes.values() if not r)
    empty = sum(1 for text in context.values() if not text)
    chars = [len(t) for t in context.values() if t]
    print(f"routed {len(rows)} questions, top {args.top}: {unrouted} with no table found, "
          f"{empty} with no document for their tables; context "
          f"{min(chars) if chars else 0} to {max(chars) if chars else 0} chars, "
          f"mean {int(sum(chars) / len(chars)) if chars else 0}")
    if outside_by_question:
        by_table = Counter(t for found in outside_by_question.values() for t in found)
        named = ", ".join(f"{table} ({n})" for table, n in by_table.most_common(5))
        print(f"dropped {sum(by_table.values())} routed tables outside --entry-group "
              f"{args.entry_group}, {len(outside_by_question)} questions affected: {named}")
    if dropped_by_question:
        # what the search keeps returning that the routing cannot score:
        # a scratch dataset, a copy, a table the catalog list left out
        by_table = Counter(t for found in dropped_by_question.values() for t in found)
        named = ", ".join(f"{table} ({n})" for table, n in by_table.most_common(5))
        print(f"dropped {sum(by_table.values())} routed tables not in --catalog, "
              f"{len(dropped_by_question)} questions affected: {named}")
    print(f"  routes -> {args.routes_out}   context -> {args.context_out}   routing -> {args.routing_out}")
    print(f"  grade the router first: score-routing --predictions {args.routing_out} --catalog CATALOG")
    print(f"  the arm: generate ... --context-per-question {args.context_out}; then leak "
          f"--context-per-question {args.context_out}")
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    """The human step between mine and gate, as a stage: prepare the sheet and
    the document, or finish it into golds. Refuses to finish while any row is
    undecided, so the step cannot be skipped by forgetting it."""
    from .review import (ReviewNotFinished, agreement, agreement_text,
                         answer_key_caveat, finish, prepare)
    if getattr(args, "agreement", False):
        if not args.sheet:
            print("review --agreement needs --sheet review.csv")
            return 2
        rows = list(csv.DictReader(open(args.sheet, newline="")))
        print(agreement_text(agreement(rows)))
        return 0
    if getattr(args, "harvest", False):
        if not args.sheet:
            print("review --harvest needs --sheet review.csv")
            return 2
        return _harvest_review(args)
    if args.draft:
        if not args.sheet:
            print("review --draft needs --sheet review.csv (the sheet prepare wrote)")
            return 2
        return _draft_review(args)
    if bool(args.candidates) == bool(args.sheet):
        print("review needs exactly one of --candidates (prepare) or --sheet (finish), "
              "or --sheet with --draft")
        return 2
    if not args.out:
        print("review needs --out: a directory (prepare) or the golds CSV (finish)")
        return 2
    if args.candidates:
        counts = prepare(args.candidates, args.out)
        print(f"review sheet -> {pathlib.Path(args.out) / 'review.csv'}   "
              f"(read {pathlib.Path(args.out) / 'REVIEW.md'} first)")
        print(f"  {counts['candidates']} candidates, {counts['clean']} gate-clean, over "
              f"{counts['tables']} tables; fill wording, keep and reason for every row")
        return 0
    try:
        result = finish(args.sheet, args.out)
    except ReviewNotFinished as stop:
        print(stop)
        return 2
    rejected = ", ".join(f"{k} {v}" for k, v in sorted(result["rejected"].items(), key=lambda kv: -kv[1]))
    print(f"golds -> {args.out}   ({result['kept']} kept of {result['total']}; "
          f"rejected: {rejected or 'none'})")
    if result["clean_rejected"]:
        print(f"  {result['clean_rejected']} of {result['clean']} gate-clean candidates were "
              "rejected by a reviewer: the gate's pass count is not the size of the key")
    for ids in result["duplicate_questions"]:
        print(f"  {' and '.join(ids)} read as the same question with different SQL: "
              "one question cannot have two right answers, so keep one. This compares "
              "wording, so two golds worded differently can still be one question")
    if result["edited_sql"]:
        names = ", ".join(result["edited_sql"][:5])
        more = len(result["edited_sql"]) - 5
        print(f"  {len(result['edited_sql'])} kept golds carry SQL edited in the sheet "
              f"({names}{f' and {more} more' if more > 0 else ''}): the gate never read this "
              "SQL, so run gate again on this file before scoring against it")
    # who accepted the golds travels with the run: the verdict repeats it
    args.telemetry = {key: result[key]
                      for key in ("model_reviewed", "person_reviewed", "reviewer_model")}
    args.telemetry["edited_sql"] = len(result["edited_sql"])
    args.telemetry["clean_rejected"] = result["clean_rejected"]
    args.telemetry["duplicate_questions"] = len(result["duplicate_questions"])
    caveat = answer_key_caveat(result)
    if caveat:
        print(f"  {caveat}")
    print("  next: gate, then routing-gate, then split")
    return 0


def _harvest_review(args: argparse.Namespace) -> int:
    """Reviewer comments into candidate rule lines, printed as a draft.

    An analyst rejecting a question does not just filter it: they explain the
    business, and that explanation IS the written context this framework
    measures. Today it would sit in a spreadsheet cell and be thrown away.

    IT WRITES NOTHING. The lines go to the screen, or to --out when asked for,
    and a person decides what to keep. This is an LLM stage and gets the same
    treatment as every other one here: a draft a human reviews, never a file
    the tool applies on its own.
    """
    from .review import harvest

    if args.backend == "echo":
        from .backends import echo_backend
        backend = echo_backend(args.echo_response)
    else:
        from .backends import gemini_backend
        try:
            backend = gemini_backend(model=args.model, project=args.project)
        except ImportError:
            print("review --harvest needs the [gemini] extra: "
                  "uv pip install -e '.[gemini]'")
            return 2
        except CredentialsMissing as exc:
            print(exc)
            return 2

    golds = None
    if args.golds:
        _require_file(args.golds, "the scored questions (--golds)")
        golds = [(r["id"], r.get("question", ""), r.get("gold_sql", ""))
                 for r in csv.DictReader(open(args.golds, newline=""))]
    else:
        # Said out loud, because the quiet version is the dangerous one: a
        # rule drafted from a comment on a scored question can carry that
        # question's answer, and nothing downstream would ever notice.
        print("  no --golds given, so the leak gate cannot run on these "
              "lines. A comment on a scored question can quote its answer")

    found = harvest(args.sheet, backend, golds)
    if not found["comments"]:
        print(f"  no comment in {args.sheet}. The `comment` column is where a "
              "reviewer explains the business; nothing to harvest until one "
              "is filled in")
        return 0

    print(f"  {found['comments']} comment(s) -> {len(found['lines'])} "
          f"candidate rule line(s)")
    for line in found["lines"]:
        print(f"    {line}")
    for line, why in found["refused"]:
        print(f"    REFUSED {line}")
        print(f"        {why}")
    if found["refused"]:
        print("  a refused line is shown rather than dropped, so you can read "
              "what the analyst said and write it yourself")
    if args.out:
        pathlib.Path(args.out).write_text(found["text"])
        print(f"\n  wrote {args.out} -- read it before it goes near a spec")
    else:
        print("\n  nothing was written. Pass --out FILE to save the draft")
    return 0


def _draft_review(args: argparse.Namespace) -> int:
    """A model decides the undecided rows of the review sheet, in batches.
    An LLM stage priced before the first call, as draft-rules is; it writes
    only keep, reason and source (and an empty wording, as a copy of the
    question), never SQL or question text, and the sheet is rewritten in
    place."""
    from .backends import DEFAULT_THINKING_BUDGET
    from .review import (draft, draft_plan, output_token_cap,
                         self_review_refusal)

    # Before the price, before the first call: an independence problem is not
    # something to discover after paying for 40 decisions.
    rows = list(csv.DictReader(open(args.sheet, newline="")))
    stop = self_review_refusal(rows, args.model, args.question_model)
    if stop and not args.allow_self_review:
        print(f"  refusing to draft: {stop}")
        return 2
    if stop:
        print(f"  --allow-self-review: {stop}")
        print("  the review is recorded as model-reviewed either way, and "
              "`answer_key_caveat` prints beside any verdict that uses it")

    prompts, n_undecided, n_total = draft_plan(args.sheet, args.batch)
    if not prompts:
        print(f"review draft: every row of {args.sheet} is decided ({n_total} rows); nothing to do")
        return 0
    cap = output_token_cap(args.batch)
    if args.backend == "echo":
        from .backends import echo_backend
        backend = echo_backend(args.echo_response)
    else:
        from .backends import gemini_backend
        try:
            # no thinking_budget override here: reviewing a question against
            # its SQL is a judgement, and a budget of 0 is refused outright
            # by the stronger models this command asks you to use
            backend = gemini_backend(model=args.model, project=args.project,
                                     max_output_tokens=cap)
        except ImportError:
            print("review --draft needs the [gemini] extra: uv pip install -e '.[gemini]'")
            return 2
        except CredentialsMissing as exc:
            print(exc)
            return 2
        except Exception as exc:  # noqa: BLE001
            # google.auth raises DefaultCredentialsError from deep inside the
            # client, and it reached a fresh clone as a stack trace. A person
            # with no credentials has an ordinary problem and deserves an
            # ordinary sentence.
            if "credential" not in f"{type(exc).__name__} {exc}".lower():
                raise
            print(f"review --draft needs Google Cloud credentials: {exc}")
            return 2
    prices = price_table(args.model, args.input_price, args.output_price)
    try:
        prompt_tokens = sum(backend.count_tokens(prompt) for prompt in prompts)
    except CredentialsMissing as problem:
        print(problem)
        return 2
    # the reasoning tokens are billed as output, so they are priced here
    per_call = cap + (0 if args.backend == "echo" else DEFAULT_THINKING_BUDGET)
    estimate = estimate_cost_usd(prices, prompt_tokens, per_call * len(prompts))
    print(f"review draft: {n_undecided} undecided of {n_total} rows: {len(prompts)} {args.model} "
          f"call(s) of up to {args.batch} rows, about {prompt_tokens:,} prompt tokens, "
          + (f"~${estimate:.4f}" if estimate is not None
             else f"cost unknown (no price for {args.model}; pass --input-price/--output-price)"))
    if args.estimate_only:
        return 0
    if args.budget is not None:
        if estimate is None:
            print(f"--budget needs a price and {args.model} has none in the table; pass "
                  "--input-price and --output-price (USD per million tokens)")
            return 2
        if estimate > args.budget:
            print(f"refusing to start: estimate ${estimate:.4f} is above the budget "
                  f"${args.budget:.4f}")
            return 2
        # several calls: the backend stops answering at the line, and the
        # rows after that stay undecided for a person
        backend.prices = prices
        backend.budget_usd = args.budget

    # The guard lives here, in the verb, not in a wrapper around it. A host
    # agent on auto-approve would answer a prompt for the caller, so the
    # question is read from the controlling terminal instead of from stdin.
    if args.backend != "echo":
        decision = confirm_paid("", budget=args.budget, yes=args.yes,
                                argv=["review"] + sys.argv[2:])
        print(decision.reason)
        if not decision.allowed:
            return 2

    result = draft(args.sheet, backend, args.model, args.batch)
    usage = dict(backend.usage)
    spent = estimate_cost_usd(prices, usage["prompt_tokens"],
                              usage["output_tokens"], usage["thought_tokens"])
    # the sheet, rewritten in place, is this run's output: the recorder
    # copies it, and the finishing run's sheet input then links back here
    args.out = args.sheet
    args.telemetry = {**usage, "estimated_cost_usd": spent, "price_table": prices,
                      "estimate": {"calls": len(prompts), "prompt_tokens": prompt_tokens,
                                   "output_tokens": per_call * len(prompts),
                                   "estimated_cost_usd": estimate},
                      "reviewer": {"model": args.model, "decided": result["decided"],
                                   "kept": result["kept"], "rejected": result["rejected"],
                                   "undecided": result["undecided"]}}
    print(f"  {args.model} must not be the model that wrote these questions "
          "(mine --write-questions) and should not be the model the agent under test uses: "
          "a model that reviews its own writing accepts it")
    reasons = ", ".join(f"{k} {v}" for k, v in result["reasons"].items())
    print(f"review draft: {result['decided']} of {result['total']} rows decided by {args.model} "
          f"(kept {result['kept']}, rejected {result['rejected']}; reasons: {reasons or 'none'}), "
          f"{result['undecided']} left for a person")
    if usage.get("stopped_at_budget"):
        print(f"  stopped at the budget ${args.budget:.4f}; the rows after that are left for a person")
    print(f"  sheet rewritten in place: {args.sheet}; the rows the model decided carry "
          f"source=model-reviewed:{args.model}")
    print("  next: a person checks a sample (set source to reviewed on each checked row), "
          "then: review --sheet SHEET --out golds.csv")
    if args.backend != "echo":
        print(spend_line(usage, spent, 'spent'))
    return 0


def _no_spend() -> dict:
    """Telemetry for a run that called no model: a refusal before the call,
    or the echo backend. A refusal used to record null, and a null reads as
    "not measured" where 0 is the fact."""
    return {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "thought_tokens": 0,
            "stopped_at_budget": False, "estimated_cost_usd": 0.0, "price_table": None}


def _cmd_draft_rules(args: argparse.Namespace) -> int:
    """Read one failure bucket, gold beside prediction, and draft the rules it
    asks for. An LLM stage: one call, priced by the backend; the output is a
    file a person reviews and installs with context-write."""
    # kept by every exit before the call, and replaced once one is made
    args.telemetry = _no_spend()
    from .harness import load_report
    from .rules import OUTPUT_TOKEN_CAP, build_rules_prompt, parse_rules, render_draft
    from .triage import BUCKET_MEANINGS, triage
    report = load_report(args.report)
    with open(args.predictions, newline="") as handle:
        predictions = {row["id"]: row for row in csv.DictReader(handle)}
    only = None
    if args.ids:
        only = {line.strip() for line in open(args.ids) if line.strip()}
    shapes = triage(report)
    results = {r.id: r for r in report.results}
    by_bucket: dict[str, list] = {}
    for item in shapes.classified:
        if only is not None and item.id not in only:
            continue
        by_bucket.setdefault(item.bucket, []).append(results[item.id])
    if not by_bucket:
        print("no failures to draft from" + (" in the given ids" if only else ""))
        return 2
    bucket = args.bucket or max(by_bucket, key=lambda b: len(by_bucket[b]))
    failures = by_bucket.get(bucket, [])
    if not failures:
        print(f"no failures in bucket {bucket}; buckets here: "
              + ", ".join(f"{b} {len(v)}" for b, v in by_bucket.items()))
        return 2
    chosen = failures[: args.limit]
    # the question ids stay here: the prompt labels the questions q1..qN by
    # position and parse_rules reads a citation back through this order
    items = [{"question": r.question,
              "gold_sql": predictions.get(r.id, {}).get("gold_sql", ""),
              "pred_sql": predictions.get(r.id, {}).get("pred_sql", ""),
              "detail": r.detail} for r in chosen]
    prompt = build_rules_prompt(items, bucket, BUCKET_MEANINGS.get(bucket, ""), args.max_rules)
    echo = args.backend == "echo"
    if echo:
        from .backends import echo_backend
        backend = echo_backend(args.echo_response)
    else:
        from .backends import gemini_backend
        try:
            backend = gemini_backend(model=args.model, project=args.project,
                                     thinking_budget=0, max_output_tokens=OUTPUT_TOKEN_CAP)
        except ImportError:
            print("draft-rules needs the [gemini] extra: uv pip install -e '.[gemini]'")
            return 2
        except CredentialsMissing as exc:
            print(exc)
            return 2
    # cost before spending: the one prompt is counted, then priced with the
    # output cap as the output side, so the figure is a ceiling the budget
    # can be held to. Ran blind on one estate: "(cents)" was the only price.
    # The echo backend reaches no model, so it is priced at nothing: it used
    # to print a gemini price and record one for a run that called nothing.
    prices = None if echo else price_table(args.model, args.input_price, args.output_price)
    prompt_tokens, estimate = 0, 0.0
    if not echo:
        try:
            prompt_tokens = backend.count_tokens(prompt)
        except CredentialsMissing as problem:
            print(problem)
            return 2
        estimate = estimate_cost_usd(prices, prompt_tokens, OUTPUT_TOKEN_CAP)
    head = f"drafting from {len(chosen)} of {len(failures)} {bucket} failures: "
    if echo:
        print(head + "the echo backend, no model call, $0.0000")
    else:
        print(head + f"one {args.model} call, about {prompt_tokens:,} prompt tokens, "
              + (f"~${estimate:.4f}" if estimate is not None
                 else f"cost unknown (no price for {args.model}; pass "
                      "--input-price/--output-price)"))
    if args.estimate_only:
        return 0
    if args.budget is not None:
        if estimate is None:
            print(f"--budget needs a price and {args.model} has none in the table; pass "
                  "--input-price and --output-price (USD per million tokens)")
            return 2
        if estimate > args.budget:
            print(f"refusing to start: estimate ${estimate:.4f} is above the budget "
                  f"${args.budget:.4f}")
            return 2
    if not echo:
        stop = _paid_gate(args, "drafting the rules")
        if stop is not None:
            return stop
    reply = backend(prompt)
    out_path = pathlib.Path(args.out)
    # --out context/rules_draft.md failed after the call was paid for,
    # because context/ did not exist
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # the reply as it came, beside the draft: a draft of 0 rules was
    # unreadable without paying for the call a second time
    raw_path = out_path.with_suffix(".raw.txt")
    raw_path.write_text(reply)
    args.raw_out = str(raw_path)
    draft = parse_rules(reply, [r.id for r in chosen],
                        {r.id: predictions.get(r.id, {}).get("gold_sql", "")
                         for r in chosen})
    out_path.write_text(render_draft(draft.rules, bucket, len(chosen), draft.memorised))
    usage = dict(backend.usage)
    # the echo backend's own token counts are characters/4, a stand-in for a
    # model's count; recording them would put made-up tokens on a cost sheet
    spent = 0.0 if echo else estimate_cost_usd(prices, usage["prompt_tokens"],
                                               usage["output_tokens"], usage["thought_tokens"])
    args.telemetry = _no_spend() if echo else {
        **usage, "estimated_cost_usd": spent, "price_table": prices,
        "estimate": {"calls": 1, "prompt_tokens": prompt_tokens,
                     "output_tokens": OUTPUT_TOKEN_CAP,
                     "estimated_cost_usd": estimate}}
    print(f"draft -> {args.out}   ({len(draft.rules)} rules, each citing the questions it is "
          f"for; uncited rules were dropped)")
    print(f"  the reply as it came: {raw_path}")
    if draft.memorised:
        # two cold runs wrote a single question's values into the context and
        # `attribute` then credited the line with fixing that question. It
        # reads as a win, raises the working-half reading and does nothing to
        # the reserved one, which is what overfitting is
        print(f"  {len(draft.memorised)} rule(s) restate one question's own filter values, so "
              "they may fix that question and nothing else. Marked in the draft; read them "
              "before you install, and drop any that a more general rule already covers:")
        for rule in draft.memorised[:3]:
            print(f"    {rule[:110]}")
    if not draft.rules:
        labels = "q1" if len(chosen) == 1 else f"q1 to q{len(chosen)}"
        print(f"  0 rules survived: the reply had {draft.rule_lines} rule lines and "
              f"{draft.dropped_citations} citations that named no question shown. The "
              f"questions were shown as {labels}; read the reply.")
    print("  review it, then: context-write --source-file DRAFT --to context/rules.md, then leak")
    if not echo:
        print(spend_line(usage, spent, 'spent'))
    return 0


def _cmd_catalog_list(args: argparse.Namespace) -> int:
    """The tables a catalog entry group holds, one dataset.table per line:
    the file routing-gate, score-routing and catalog-route read, so nobody
    types 127 names by hand. Free."""
    from .catalog import KnowledgeCatalogStore, transport_for
    store = KnowledgeCatalogStore(transport_for(args.project, args.location))
    datasets = [d.strip() for d in (args.datasets or "").split(",") if d.strip()] or None
    keys, counts = store.list_tables(args.entry_group, datasets=datasets)
    out = pathlib.Path(args.out or "catalog.txt")
    header = (f"# contextlift catalog-list: project {args.project}, location {args.location}, "
              f"entry group {args.entry_group}\n"
              f"# listed {counts['listed']} entries; kept {counts['kept']} with a parent entry; dropped "
              f"{counts['no_parent']} without one, {counts['not_table_shaped']} not table-shaped, "
              f"{counts['transient']} transient (dbt tmp/backup), "
              f"{counts['outside_datasets']} outside --datasets\n")
    if not keys:
        print(header.rstrip())
        print(f"nothing kept: the entry group {args.entry_group!r} at {args.location} may not exist, "
              "the location may be the wrong one (a BigQuery dataset's own region), or the account "
              "lacks dataplex.entries.list on it")
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(header + "\n".join(keys) + "\n")
    print(header.rstrip())
    print(f"catalog -> {out}  ({len(keys)} tables; give it to routing-gate --catalog, "
          "score-routing --catalog, catalog-route --catalog, draft-context --tables-file)")
    return 0


def _holdout_rows_for(args: argparse.Namespace) -> tuple[list[tuple[str, str, str]], str | None]:
    """The reserved questions a push must not leak: --holdout, else the
    newest split's holdout copy in the workspace, else nothing."""
    path = getattr(args, "holdout", None)
    if not path and getattr(args, "workspace", None):
        from .workspace import rebuild_index
        try:
            runs = rebuild_index(pathlib.Path(args.workspace))["runs"]
        except Exception:  # noqa: BLE001
            runs = []
        for run in reversed(runs):
            if run["command"] == "split" and run.get("finished"):
                copies = run.get("outputs", {}).get("out_holdout_csv") or []
                if copies and copies[0]:
                    path = str(pathlib.Path(args.workspace) / "runs" / run["run_id"] / copies[0])
                    break
    if not path:
        return [], None
    with open(path, newline="") as handle:
        rows = [(r["id"], r.get("question", ""), r["gold_sql"]) for r in csv.DictReader(handle)]
    return rows, path


def _stderr_line(line: str) -> None:
    """Progress for a terminal, flushed as it happens: stdout stays the
    summary. A 90-minute push once printed nothing until it ended."""
    print(line, file=sys.stderr, flush=True)


@contextlib.contextmanager
def _exit_on_signals():
    """SIGINT and SIGTERM raise SystemExit(130) and SystemExit(143) while a
    push runs, so its except path writes the manifest; a push killed with
    SIGTERM once wrote none. The previous handlers come back afterwards.
    Handlers can be set on the main thread only; elsewhere nothing changes."""
    import signal

    def raise_exit(signum, frame):
        raise SystemExit(130 if signum == signal.SIGINT else 143)

    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, raise_exit)
        except ValueError:
            pass
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _short_entry(name: str) -> str:
    """An entry name a person can scan on one line: whole when short, else
    its last two segments."""
    return name if len(name) <= 60 else ".../" + "/".join(name.split("/")[-2:])


def _push_lock_path(args: argparse.Namespace) -> pathlib.Path:
    """Where the push lock lives: the workspace root, else beside the
    manifest, else the current directory."""
    if getattr(args, "workspace", None):
        return pathlib.Path(args.workspace) / "catalog-push.lock"
    if args.manifest_out:
        return pathlib.Path(args.manifest_out).parent / "catalog-push.lock"
    return pathlib.Path("catalog-push.lock")


def _report_stopped(stopped: BaseException, result, pages: dict, meta: dict,
                    args: argparse.Namespace) -> None:
    """The manifest with the counts so far, and one line saying where the
    push stopped and how to go on. Whatever was written before this is
    recorded; a push that dies without saying where it stopped is worse
    than one that failed."""
    from .push import previous_records, tally, write_manifest
    interrupted = isinstance(stopped, KeyboardInterrupt) or (
        isinstance(stopped, SystemExit) and stopped.code in (130, 143))
    tally(result)
    detail = str(stopped)[:200]
    meta["stopped_by"] = type(stopped).__name__ + (f": {detail}" if detail else "")
    manifest = args.manifest_out or None
    if manifest:
        write_manifest(result, manifest, meta, status="interrupted" if interrupted else "stopped",
                       previous=previous_records(manifest))
    page_entries = [e for e in result.entries if e["kind"] == "page"]
    verified = sum(1 for e in page_entries if e["status"] == "written" and e["verify"] == "verified")
    unverified = sum(1 for e in page_entries if e["status"] == "written" and e["verify"] != "verified")
    untouched = len(pages) - sum(1 for e in page_entries if e["status"])
    where = f"the manifest is at {manifest}" if manifest else "no manifest (no --workspace or --manifest-out)"
    what = "interrupted" if interrupted else f"stopped by {meta['stopped_by']}"
    print(f"{what}: {verified} pages written and verified, {unverified} written and not verified, "
          f"{untouched} not touched; {where}; run the same command again to continue "
          "(stamped pages come back unchanged)")


def _cmd_catalog_push(args: argparse.Namespace) -> int:
    """Reviewed pages, rule lines and worked examples into the catalog, every
    write read back through the same call a reader uses. Free: metadata
    writes are not billed. Never deletes; --dry-run prints the plan. One
    push per workspace at a time (a lock file); a killed push leaves a
    manifest and is resumed by running it again."""
    from .catalog import KnowledgeCatalogStore, ScopeCheckFailed, transport_for
    from .push import (PushAlreadyRunning, examples_in_bank, format_summary, page_record, pages_in,
                       previous_records, push, stamp, take_push_lock, term_lines, verify_written,
                       write_manifest)
    tables = [t.strip() for t in (args.tables or "").split(",") if t.strip()] or None
    pages, bare_skipped = pages_in(args.docs, tables)
    if not pages and not args.rules and not args.examples:
        print(f"nothing to push: no <dataset>.<table>.md pages under {args.docs}"
              + (f" ({bare_skipped} bare <table>.md pages cannot be resolved in a catalog)" if bare_skipped else ""))
        return 2
    terms: list = []
    skipped_lines = 0
    if args.rules:
        terms, skipped_lines = term_lines(pathlib.Path(args.rules).read_text(), list(pages))
    examples: list = []
    if args.examples:
        examples = examples_in_bank(json.loads(pathlib.Path(args.examples).read_text()))
    holdout_rows, holdout_path = _holdout_rows_for(args)
    store = KnowledgeCatalogStore(transport_for(args.project, args.location),
                                  project=args.project, location=args.location,
                                  entry_group=args.entry_group, entry_id_template=args.entry_id,
                                  entry_id_pattern=args.entry_pattern, context_budget=args.budget)
    known = next(iter(pages), None) or (terms[0].get("table") if terms else None)
    if known and store.entry_id_template is None:
        # the search path has to prove its scoping first; a built name is
        # checked by reading the first one instead
        try:
            store.check_scope(known)
        except ScopeCheckFailed as failed:
            print(failed)
            return 2
    if args.verify_only:
        # what a reader sees now, for every page: the come-back-later command
        # when the index lagged at push time; the read-back phase alone. A
        # draft is skipped exactly as the push skips it, because reading back
        # a page that was never pushed reported every draft as failed
        from .draft import DRAFT_MARK
        drafts = [key for key, text in pages.items()
                  if not args.allow_draft and DRAFT_MARK in text]
        pushed = {key: text for key, text in pages.items() if key not in drafts}
        pending, failed = [], 0
        for key, text in pushed.items():
            entry_name = store.resolve(key)
            if not entry_name:
                print(f"  {key}: no entry"); failed += 1; continue
            pending.append((page_record(key, text, entry_name), stamp(text)[1]))
        mismatch = store.template_mismatch()
        if mismatch:
            print(mismatch)
            return 2
        verify_written(store, pending, attempts=args.attempts, progress=_stderr_line)
        for entry, _ in pending:
            print(f"  {entry['key']}: {entry['verify']} after {entry['attempts']} read(s), "
                  f"chars seen {entry['readback_chars']}"
                  + (f"; {entry['cause']}" if entry.get("cause") else ""))
            failed += entry["verify"] != "verified"
        print(f"verified {len(pushed) - failed} of {len(pushed)} pages as a reader sees them now")
        if drafts:
            named, more = ", ".join(sorted(drafts)[:5]), len(drafts) - 5
            print(f"  {len(drafts)} not pushed (drafts): {named}"
                  + (f", and {more} more" if more > 0 else "")
                  + "; they still carry the draft-context marker (review them, or --allow-draft)")
        # the bundle adds the schema and the generic aspect around the page,
        # so a reader sees more than was written on every page of a real
        # estate (99 of 99 measured); the figure is reported, never judged
        overhead = sorted(e["served_minus_written"] for e, _ in pending
                          if e.get("served_minus_written") is not None)
        if overhead:
            print(f"  served minus written: min {overhead[0]:,}, median "
                  f"{overhead[len(overhead) // 2]:,}, max {overhead[-1]:,} characters over "
                  f"{len(overhead)} verified pages (the bundle around the page)")
        if args.manifest_out:
            from .push import PushResult, tally, write_manifest
            result = PushResult(entries=[e for e, _ in pending], counts={})
            tally(result)
            write_manifest(result, args.manifest_out,
                           {"project": args.project, "location": args.location, "docs": args.docs,
                            "mode": "verify-only",
                            "started_at": dt.datetime.now().isoformat(timespec="seconds")},
                           status="verified")
            print(f"  manifest -> {args.manifest_out}")
        return 1 if failed else 0
    previous_out = pathlib.Path(args.previous_out) if args.previous_out else None
    from .push import PushResult
    result = PushResult(entries=[], counts={})
    meta = {"project": args.project, "location": args.location, "docs": args.docs,
            "rules": args.rules, "examples": args.examples, "holdout": holdout_path,
            "mode": "dry-run" if args.dry_run else "push", "started_at": dt.datetime.now().isoformat(timespec="seconds"),
            "rule_lines_not_terms": skipped_lines}
    lock = None if args.dry_run else _push_lock_path(args)
    if lock:
        try:
            note = take_push_lock(lock, {"pid": os.getpid(), "started_at": meta["started_at"],
                                         "project": args.project, "location": args.location,
                                         "entry_group": args.entry_group})
        except PushAlreadyRunning as busy:
            print(busy)
            return 2
        if note:
            print(note)
    try:
        with _exit_on_signals():
            result = push(store, pages=pages, terms=terms, examples=examples, holdout_rows=holdout_rows,
                          glossary_id=args.glossary, query_group=args.query_group, attempts=args.attempts,
                          dry_run=args.dry_run, allow_draft=args.allow_draft, read_budget=args.budget,
                          previous_out=previous_out, bare_skipped=bare_skipped, result=result,
                          progress=_stderr_line)
    except BaseException as stopped:
        _report_stopped(stopped, result, pages, meta, args)
        raise
    finally:
        if lock:
            lock.unlink(missing_ok=True)
    if args.manifest_out:
        # a resumed push writes the same file: keep the records of the pages
        # the runs before it wrote, so the last manifest is the whole push. A
        # dry run's plan is not merged into what a real push recorded.
        write_manifest(result, args.manifest_out, meta, status="plan" if args.dry_run else "complete",
                       previous=[] if args.dry_run else previous_records(args.manifest_out))
    print(format_summary(result))
    if args.dry_run and not result.counts.get("refused"):
        # the targets, so a person checks where each page lands before a
        # real push: an earlier resolve put 12 pages on a sibling's entry
        print("  where each page would go:")
        for entry in result.entries:
            if entry["kind"] != "page":
                continue
            target = (f"{_short_entry(entry['entry_name'])}  {entry['status']}" if entry["entry_name"]
                      else entry["status"])
            print(f"  {entry['key']} -> {target} ({entry['chars']:,} chars)")
    if args.manifest_out:
        print(f"  {'plan' if args.dry_run else 'manifest'} -> {args.manifest_out}")
    if not args.dry_run and result.counts.get("written"):
        print("  next: catalog-pull --tables <the pushed keys> --out docs_served/ shows what a reader "
              "sees now; then catalog-route against that directory")
    if args.dry_run:
        # record_run skips dry runs; a person looking for the run under
        # runs/ afterwards should have been told
        print("dry run: nothing written; not recorded in the workspace (a real push is)")
    args.telemetry = {k: v for k, v in result.counts.items() if isinstance(v, (int, str, bool, type(None)))}
    if result.counts.get("refused"):
        return 2
    return 1 if result.counts.get("failed") else 0


def _service_account_line(args: argparse.Namespace, service_jobs: int | None = None,
                          window_jobs: int | None = None) -> str:
    """What became of the dashboards' own traffic, in one sentence.

    A service account runs on a timer, so it holds most of a window: on one
    estate 7 of them ran 169,789 of 184,301 jobs over 30 days, and 2,592 of
    the 3,000 shapes a `--limit 3000` pull returned. Excluded by default,
    counted either way. Plain `log` has no count to print (counting them
    would read the job-history view a second time and double what the pull
    scans), so it names the filter and points at `--demand`.
    """
    if getattr(args, "include_service_accounts", False):
        counted = (f"{service_jobs} of {window_jobs} jobs in the window "
                   if service_jobs is not None else "")
        return f"service accounts: {counted}kept by --include-service-accounts"
    if service_jobs is None:
        return ("service accounts: excluded (--include-service-accounts keeps them; "
                "log --demand counts how many jobs they ran)")
    return (f"service accounts: {service_jobs} of {window_jobs} jobs in the window "
            "(excluded; --include-service-accounts keeps them)")


def _report_demand(args: argparse.Namespace, rows: list[dict], listed: list[str]) -> None:
    """The demand table: which datasets get the jobs, shapes and accounts,
    so the scope is a decision with a source. The top 15 are printed in the
    SQL's order (most accounts first, then most jobs); every dataset is in
    the CSV. jobs_per_account_day is computed here, not in SQL, so the
    fast suite checks the arithmetic.

    Under the table, the three datasets whose busiest account holds the
    largest share of their jobs, as what that account ran: a fact, with
    --exclude-accounts named beside it. No line calls an account a robot."""
    days = max(int(args.days), 1)
    rows = [{**row, "jobs_per_account_day":
             round(int(row["jobs"]) / max(int(row["distinct_accounts"]), 1) / days, 1)}
            for row in rows]
    out = args.out or "demand.csv"
    n = write_demand(rows, out)
    all_jobs = int(rows[0]["all_jobs"]) if rows else 0
    attributed = int(rows[0]["attributed_jobs"]) if rows else 0
    unattributed = int(rows[0]["unattributed_jobs"]) if rows else 0
    top = rows[:15]
    width = max([len("dataset")] + [len(str(row["dataset"])) for row in top])
    # a job that read three tables of one dataset is one job in its row, not
    # three: counting referenced tables gave 24,245 where the job count was 14,512
    print("jobs counts a job once per dataset it touched, so a two-dataset job is in two "
          "rows and the column sums above the window's job count; the lines under the table "
          "count each job once")
    print(f"{'dataset':<{width}}  {'jobs':>8}  {'shapes':>7}  {'accounts':>8}  "
          f"{'jobs/acct/day':>13}  {'share':>6}")
    for row in top:
        share = int(row["jobs"]) / all_jobs if all_jobs else 0.0
        print(f"{row['dataset']:<{width}}  {int(row['jobs']):>8}  {int(row['distinct_shapes']):>7}  "
              f"{int(row['distinct_accounts']):>8}  {row['jobs_per_account_day']:>13.1f}  "
              f"{share:>6.1%}")
    if len(rows) > len(top):
        print(f"  and {len(rows) - len(top)} more datasets in the CSV")
    service_jobs = int(rows[0].get("service_jobs") or 0) if rows else 0
    window = all_jobs + (0 if getattr(args, "include_service_accounts", False) else service_jobs)
    print(_service_account_line(args, service_jobs, window))
    # what the busiest account of a dataset actually ran, from
    # top_account_share. The line here used to call anything over 50 jobs per
    # account per day machinery; that threshold came from a count since
    # retracted, and a real analyst on that estate runs about 70 a day
    concentrated = sorted(rows, key=lambda row: float(row.get("top_account_share") or 0),
                          reverse=True)[:3]
    concentrated = [row for row in concentrated if float(row.get("top_account_share") or 0)]
    for row in concentrated:
        share = float(row["top_account_share"])
        print(f"{row['dataset']}: one account ran {share * int(row['jobs']) / days:.1f} jobs "
              f"a day, {share:.0%} of its jobs")
    if concentrated:
        print("--exclude-accounts drops an account's jobs from the pull (log without --demand "
              "shows ran_by per shape)")
    if listed:
        # bare names, the way the SQL filter matched them
        names = dataset_names(listed)
        seen = {str(row["dataset"]).split(".")[-1] for row in rows}
        missing = [name for name in names if name not in seen]
        share = attributed / all_jobs if all_jobs else 0.0
        print(f"the {len(names)} listed datasets hold {attributed} of {all_jobs} jobs in the "
              f"window ({share:.1%})")
        print(f"{len(missing)} of {len(names)} listed datasets had no job in the window"
              + (": " + ", ".join(missing) if missing else ""))
    print(f"{unattributed} of {all_jobs} jobs carry no referenced table (cache hits) and are "
          "in no row")
    print("a dataset nobody queries needs no golds; a dataset with many shapes and few golds "
          "is where the answer key is thin")
    print(f"demand -> {out}  ({n} datasets over {args.days} days; distinct_accounts is a "
          "count, no account is stored)")


def _cmd_log(args: argparse.Namespace) -> int:
    """Pull the query history `mine` reads, or with --demand one row per
    dataset saying where the traffic is, with the cost stated first."""
    if not is_bigquery(args.db):
        print("SQLite keeps no query log; give mine a CSV with columns "
              "sql[,times_seen,ran_by,distinct_users] instead")
        return 2
    spec = parse_db_spec(args.db)
    # --dataset and --datasets are one list; the dataset in --db is the
    # default only when neither is given
    datasets = ([args.dataset] if args.dataset else []) + \
        [d.strip() for d in (args.datasets or "").split(",") if d.strip()]
    if not datasets and spec.dataset:
        datasets = [spec.dataset]
    exclude = [a.strip() for a in (args.exclude_accounts or "").split(",") if a.strip()]
    only_accounts = [a.strip() for a in (args.only_accounts or "").split(",") if a.strip()]
    labels = [t.strip() for t in (args.labels or "").split(",") if t.strip()]
    if only_accounts and exclude:
        both = sorted(set(a.lower() for a in only_accounts)
                      & set(a.lower() for a in exclude))
        if both:
            print("--only-accounts and --exclude-accounts both name "
                  + ", ".join(both) + ": the pull would return nothing. Drop it "
                  "from one of them")
            return 2
    if only_accounts:
        print(f"  only {len(only_accounts)} named account(s); the service-account "
              "exclusion is off, because this list says who is wanted")
    if labels:
        print("  only jobs labelled " + ", ".join(labels))
    if args.demand:
        sql = demand_sql(spec.project, args.days, args.region, datasets=datasets or None,
                         exclude_accounts=exclude or None,
                         include_service_accounts=args.include_service_accounts,
                         only_accounts=only_accounts or None, labels=labels or None)
    else:
        sql = query_log_sql(spec.project, args.days, args.region, args.limit,
                            datasets=datasets or None, hash_accounts=args.hash_accounts,
                            exclude_accounts=exclude or None,
                            include_service_accounts=args.include_service_accounts,
                            min_tables=args.min_tables,
                            require_analytical=args.require_analytical,
                            drop_machine_sql=not args.keep_machine_sql,
                            only_accounts=only_accounts or None,
                            labels=labels or None)
    try:
        executor = make_executor(args.db, timeout_seconds=args.timeout,
                                 maximum_bytes_billed=args.max_bytes, location=args.region,
                                 refuse_on_dry_run=False)
    except ImportError:
        print(f"the [bigquery] extra is not installed in this Python ({sys.executable}): "
              "uv pip install -e '.[bigquery]'")
        return 2
    estimate = executor.estimate([sql])
    if estimate["failed"]:
        # the dry run is remembered per SQL text, so check() costs no round trip
        reason = executor.check(sql) or ""
        if estimate["refused"]:
            print(f"the platform refused the log query, not the SQL: {reason}; "
                  + PLATFORM_REFUSAL_ADVICE
                  + ". The view needs bigquery.jobs.listAll on the project, and a 403 can "
                    "also come from VPC Service Controls. The query was:\n" + sql)
        else:
            print(f"the log query was rejected at dry-run: {reason}; the query was:\n" + sql)
        return 2
    # the dry run on the job-history view is a ceiling, not a price: on one
    # estate it said 266 GB ($1.51) for a job that then billed 29 GB ($0.17).
    # BigQuery enforces --max-bytes on the real bytes and fails the job free
    # above it, so nothing is refused on the dry-run figure
    args.sql_estimate = estimate
    print(f"dry-run ceiling: {estimate['bytes'] / 1e9:.3f} GB "
          f"(~${bigquery_usd(estimate['bytes']):.4f}); on the job-history view the dry run "
          "overstates, one estate measured 10x; the job is capped at --max-bytes "
          f"{args.max_bytes / 1e9:g} GB by BigQuery itself and fails free above it")
    if args.estimate_only:
        return 0
    result = executor.execute(sql)
    if not result.ok:
        print(f"log query failed: {result.error}")
        return 1
    if args.demand:
        _report_demand(args, result.rows, datasets)
        _record_sql_spend(args, executor)
        return 0
    out = args.out or "query_log.csv"
    n = write_query_log(result.rows, out)
    print(f"log -> {out}  ({n} query shapes over {args.days} days; give it to mine --queries)")
    print("  " + _service_account_line(args))
    if args.hash_accounts:
        print("  ran_by is TO_HEX(MD5(LOWER(user_email))) per account, joined by ';', not "
              "the address; mine --exclude-accounts takes plain addresses and matches the "
              "hash itself. The sql column can still carry an address a person typed into "
              "a WHERE clause")
    else:
        print("  ran_by holds every account that ran the shape, sorted and joined by ';': "
              "personal data. It is in this CSV and in the copy under runs/; --hash-accounts "
              "stores TO_HEX(MD5(LOWER(user_email))) for each instead, and mine "
              "--exclude-accounts still works")
    _record_sql_spend(args, executor)
    return 0


def _cmd_context_write(args: argparse.Namespace) -> int:
    """Replace the working rules file with a reviewed draft, as a recorded run.

    The only way a draft reaches context/rules.md: the manifest then says
    which draft, from which source (a person or a model), replaced which
    previous bytes. The previous bytes and a diff are written next to the
    manifest, and a sequential version number is assigned at save time so a
    copied workspace shows the same "v3" everywhere. Rules are never edited
    by a page.
    """
    import difflib

    source = pathlib.Path(args.source_file)
    target = pathlib.Path(args.to)
    if not source.is_file():
        print(f"draft not found: {source}")
        return 2
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = target.read_text() if target.exists() else ""
    text = source.read_text()
    if getattr(args, "add", False):
        # a round is meant to build on the last one, and draft-rules writes one
        # bucket at a time. Replacing meant round 2 dropped round 1, so the
        # only way to keep both was a person editing the file by hand, which
        # is the one step no analyst can repeat. A cold run stopped at one
        # round for exactly this.
        already = {line.strip() for line in previous.splitlines() if line.startswith("- ")}
        fresh = [line for line in text.splitlines()
                 if not line.startswith("- ") or line.strip() not in already]
        repeated = sum(1 for line in text.splitlines()
                       if line.startswith("- ") and line.strip() in already)
        joined = previous.rstrip("\n") + "\n\n" + "\n".join(fresh).strip() + "\n"
        text = joined if previous.strip() else text
        if repeated:
            print(f"  {repeated} rule line(s) already in {target}, not added again")
    rules = sum(1 for line in text.splitlines() if line.startswith("- "))
    if rules == 0 and not getattr(args, "allow_no_rules", False):
        # a draft that parses to zero rules is almost always the wrong file. It
        # used to install anyway and report "0 rule lines" as if that were fine.
        print(f"{source}: no rule lines (a rule is a line starting with '- '). Refusing to "
              "install it; pass --allow-no-rules if this context is prose on purpose.")
        return 2
    target.write_text(text)
    if args.previous_out:
        pathlib.Path(args.previous_out).write_text(previous)
    diff = "".join(difflib.unified_diff(
        previous.splitlines(keepends=True), text.splitlines(keepends=True),
        fromfile="previous", tofile=str(target)))
    if args.diff_out:
        pathlib.Path(args.diff_out).write_text(diff)
    version = None
    if args.workspace:
        earlier = list(pathlib.Path(args.workspace).glob("runs/*-context-write*/outputs/version.json"))
        version = len(earlier) + 1
        if args.version_out:
            pathlib.Path(args.version_out).write_text(json.dumps({
                "version": version, "note": args.note, "source": args.source,
                "previous_lines": len(previous.splitlines()),
                "changed_lines": sum(1 for line in diff.splitlines()
                                     if line[:1] in "+-" and not line.startswith(("+++", "---"))),
            }, indent=1))
    label = f"v{version} " if version else ""
    # without a workspace there is no version, no diff and no manifest - the
    # whole reason to run this command - and it used to say nothing about it
    history = "" if args.workspace else "  (no workspace: written without history)"
    print(f"context -> {target}  {label}({rules} rule lines; "
          f"{('added to ' if getattr(args, 'add', False) else 'replaced ') + str(len(previous.splitlines())) + ' lines' if previous else 'new file'}"
          f"{'; note: ' + args.note if args.note else ''}){history}")
    return 0



def _cmd_bundle(args: argparse.Namespace) -> int:
    """One static verdict.html (no script) for the pinned comparison."""
    if not args.workspace:
        print("bundle needs --workspace DIR (or $CONTEXTLIFT_WORKSPACE)")
        return 2
    from .bundle import bundle_inventory, write_bundle
    html_path, json_path = write_bundle(args.workspace, args.out,
                                        args.baseline, args.candidate,
                                        include_rows=not args.no_rows)
    print(f"verdict -> {html_path}  (data: {json_path})")
    # counted from the file just written, so the sentence and the file agree
    held = bundle_inventory(json.loads(json_path.read_text()))
    previews = ("0 row previews (--no-rows)" if args.no_rows
                else f"{held['row_previews']} row previews ({held['rows']} rows)")
    where = (f"the bq:// spec with the billing project ({held['bq_spec']})"
             if held["bq_spec"] else "the database path")
    print(f"  verdict.html holds {held['questions']} questions, {held['sql_statements']} SQL "
          f"statements (the answer key's and the model's), {previews}, {held['context']}; "
          f"verdict.json also holds {where}. Review both before sending outside the "
          "customer; --no-rows drops the previews.")
    if held["rounds"]:
        print("  what each round used: " + "; ".join(held["rounds"]))
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    """Give predictions made elsewhere a recorded arm in the workspace.

    The file is copied byte for byte, so the hash a later `score` run
    records is the one this run produced and the index links the two.
    """
    if not args.workspace:
        print("import needs --workspace DIR: it exists to record outside "
              "predictions as an arm")
        return 2
    rows = _load_predictions(args.predictions)
    shutil.copyfile(args.predictions, args.out)
    print(f"imported {len(rows)} predictions -> {args.out}")
    return 0


def _read_spent(path: str) -> set[str]:
    """Question ids that may never be reserved again, one per line.

    A missing file is an empty set rather than an error: the first bisect has
    not happened yet on most gold sets, and refusing to split until somebody
    creates an empty file would be a guard that only annoys.

    Blank lines and `#` comments are skipped, so a person can write down why a
    question was spent beside the id.
    """
    found = pathlib.Path(path)
    if not found.is_file():
        return set()
    return {line.strip() for line in found.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")}


def _cmd_split(args: argparse.Namespace) -> int:
    with open(args.golds, newline="") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or ["id"])
        rows = list(reader)
    ids = [row["id"] for row in rows]
    # Spent ids are read before the split, not filtered out after it. The
    # split is deterministic on the id set it is given, and removing a
    # question afterwards would leave a reserved half that no re-run of
    # `split` reproduces.
    spent = _read_spent(args.spent) if getattr(args, "spent", None) else set()
    curate, holdout = split_golds(ids, args.holdout_fraction, args.seed,
                                  spent=spent)
    if spent:
        here = sorted(spent & set(ids))
        print(f"  spent questions    : {len(here):>4} read from {args.spent}")
        print("      bisected before, so they stay in the working half and "
              "never count in a reported number")
        unknown = sorted(spent - set(ids))
        if unknown:
            # Named rather than ignored: a spent file pointed at the wrong
            # gold set silently protects nothing, and looks identical to one
            # that is working.
            print(f"      {len(unknown)} spent id(s) are not in this gold set: "
                  f"{', '.join(unknown[:5])}"
                  + (" ..." if len(unknown) > 5 else ""))
    # Defaults live here, not on the parser: under a workspace the recorder
    # fills an unset flag with a path inside the run directory.
    #
    # Beside --golds, not in the current directory. A split belongs to the
    # gold set it split, and the old default wrote into whatever directory a
    # person happened to be standing in: splitting a customer gold set from
    # this checkout silently overwrote two tracked example id lists, and
    # nothing said so. Two gold sets split from one shell overwrote each
    # other the same way.
    beside = pathlib.Path(args.golds).resolve().parent
    args.out_curate = args.out_curate or str(beside / "curate_ids.txt")
    args.out_holdout = args.out_holdout or str(beside / "holdout_ids.txt")
    pathlib.Path(args.out_curate).write_text("\n".join(curate) + "\n")
    pathlib.Path(args.out_holdout).write_text("\n".join(holdout) + "\n")
    # one screen had used curate/working and hold out/holdout/reserved for the
    # same two things. The rest of the product says working and reserved
    print(f"  working questions  : {len(curate):>4} ids -> {args.out_curate}")
    print("      what you write rules against, and score again after each round")
    print(f"  reserved questions : {len(holdout):>4} ids -> {args.out_holdout}")
    print("      never looked at until the end; they are the number you report")
    # The filtered files themselves, every input column kept, so nobody
    # joins the id lists back onto questions.csv by hand.
    for path, wanted in ((args.out_curate_csv, set(curate)),
                         (args.out_holdout_csv, set(holdout))):
        if not path:
            continue
        with open(path, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(row for row in rows if row["id"] in wanted)
        print(f"  {'curate' if wanted == set(curate) else 'holdout':<8} rows"
              f" -> {path}")
    # what this reserved half can prove, before a dollar is spent. A cold run
    # held back 29, spent its whole budget, and only then read "you would need
    # about 40". The size is decided here, so the limit belongs here.
    from .ablate import ladder_step
    smallest_provable = 100.0 * ladder_step(len(holdout))
    print(f"\n  At {len(holdout)} reserved questions this split can only prove a lift above")
    print(f"  about {smallest_provable:.0f} points. A smaller real gain will read NOT PROVEN, which")
    print("  is not the same as no gain. Split again with a bigger --holdout-fraction,")
    print("  or mine more questions, if you expect a smaller one.")
    print("\n  Score the reserved questions ONCE, at the end. Every look at them to")
    print("  decide what to do next turns them into practice questions, and the")
    print("  number stops meaning what you want it to mean.")
    return 0


def _ids_in(path: str) -> set[str]:
    """One id per line, the file split writes; blank lines and # comments skipped."""
    with open(path) as handle:
        return {line.strip() for line in handle if line.strip() and not line.startswith("#")}


def _question_ids_of(path: str) -> set[str]:
    """The question ids one arm file holds: a saved score report's results, or
    a predictions CSV's id column. Repetition suffixes are stripped, so the
    ids compare against the list `split` wrote."""
    if _is_saved_report(path):
        try:
            results = json.loads(pathlib.Path(path).read_text()).get("results") or []
        except ValueError:
            return set()
        return {str(row.get("id", "")).split("#rep")[0] for row in results}
    # a file that is not there raises here, so the run says "no such file"
    # rather than refusing over a report it never read
    return {row[0] for row in _load_predictions(path)}


def _refuse_unheld_ids(args: argparse.Namespace, files: list[str]) -> int | None:
    """Refuse an `--ids` reading whose ids none of the arms answered.

    An ablate over the whole-key reports restricted to a holdout id list the
    arms never saw used to run happily: "0 scored per arm", both rates 0.0%
    with a [0.0%, 100.0%] interval, exit 0, and the workspace counted it as
    the one look at the reserved questions. Nothing was measured, so this is
    a refusal, and a refusal is not a look.
    """
    if not getattr(args, "ids", None):
        return None
    wanted = _ids_in(args.ids)
    held: set[str] = set()
    for path in files:
        held |= _question_ids_of(path)
    if wanted & held:
        return None
    examples = sorted(held)[:3]
    shown = ", ".join(examples) + (", ..." if len(held) > len(examples) else "")
    print(f"none of the {len(wanted)} ids in {args.ids} is in these reports: they hold "
          f"{len(held)} questions ({shown or 'none'}), so the arms were answered over "
          "another file; read them with the ids file that matches, or answer these "
          "questions with a generate over the reserved rows")
    return 2


def _conclusion_caveats(args: argparse.Namespace) -> list[str]:
    """What the workspace knows that qualifies a verdict, printed beside it.

    The framework's guards fired on the step you ran - split says "score the
    holdout once", climb warns about a training score - and stayed silent at
    the comparison the skipped step invalidates. A field run never ran split,
    never saw its warning, and curated against the scoring set. So the check
    moves to the conclusion, where the manifests already know the answer.
    """
    if not getattr(args, "workspace", None):
        return []
    from .workspace import rebuild_index
    try:
        runs = rebuild_index(pathlib.Path(args.workspace))["runs"]
    except Exception:  # noqa: BLE001 - a caveat must never take the verdict down with it
        return []
    caveats = []
    scored = [(label, *_scored_against(args.workspace, paths))
              for label, paths in _arm_paths(args)]
    by_id = {r["run_id"]: r for r in runs}
    splits = [r for r in runs if r["command"] == "split" and r.get("finished")]
    restricted = _ids_in(args.ids) if getattr(args, "ids", None) else None
    if not splits:
        caveats.append("  no split recorded in this workspace: these questions may have guided "
                       "the curation, so this is a training score")
    elif restricted is not None and restricted == _split_ids(args.workspace, splits[-1], "out_holdout"):
        pass                                # the holdout reading, by the split's own file
    elif restricted is not None and restricted == _split_ids(args.workspace, splits[-1], "out_curate"):
        caveats.append("  restricted to the curate half: this is a training score; the holdout "
                       "reading is the same command with the holdout ids")
    else:
        # a split exists, but was it this one? The index marks a run on_holdout
        # when its questions hash is a split's holdout; False means the arm was
        # scored on something else, None means the index cannot tell (routing
        # reports carry no questions file). Any split used to silence this.
        placement = {by_id.get(run_id, {}).get("on_holdout") for _, run_id, _, _ in scored}
        if False in placement:
            caveats.append("  a split is recorded, but these arms were not scored on its holdout: "
                           "this is a training score")
    # An agent's configuration is part of the arm. Four arms on one estate
    # carried the same --runner-version while an environment variable inside
    # the runner switched the model's reasoning off; the runner may report its
    # config on every call and the generate run records the first one.
    configs = []
    for label, run_id, _, _ in scored:
        if not run_id:                       # a report no run in this workspace wrote
            configs.append((label, None))
            continue
        entry = by_id.get(run_id) or {}
        generate_id = entry.get("produced_by", {}).get("predictions") or run_id
        manifest = _manifest_of(args.workspace, generate_id)
        configs.append((label, (manifest.get("telemetry") or {}).get("runner_config")))
    for (a_label, a_cfg), (b_label, b_cfg) in zip(configs, configs[1:]):
        if a_cfg and b_cfg and a_cfg != b_cfg:
            differing = sorted(k for k in set(a_cfg) | set(b_cfg) if a_cfg.get(k) != b_cfg.get(k))
            caveats.append(f"  the runner reported a different configuration between {a_label} "
                           f"and {b_label}: {', '.join(differing)}; a verdict here mixes context "
                           "with the agent's own settings")
    # A push into the catalog between two arms is a change of the catalog.
    # The push manifests know what was written; the arms know when they ran.
    pushes = [r for r in runs if r["command"] == "catalog-push" and r.get("finished")
              and r.get("exit_code") in (0, 1)]
    started = []
    for label, run_id, _, _ in scored:
        entry = by_id.get(run_id) or {}
        generate_id = entry.get("produced_by", {}).get("predictions") or run_id
        started.append((label, (by_id.get(generate_id) or {}).get("started_at") or ""))
    for (a_label, a_start), (b_label, b_start) in zip(started, started[1:]):
        between = [r for r in pushes if a_start and b_start and a_start < (r.get("started_at") or "") < b_start]
        wrote = [(r["run_id"], _push_counts(args.workspace, r["run_id"])["written"])
                 for r in between if _push_counts(args.workspace, r["run_id"]).get("written")]
        if not wrote:
            continue
        # one interrupted push resumed six times is one change of the catalog,
        # not six caveats
        times = "" if len(wrote) == 1 else f" {len(wrote)} times"
        where = (wrote[0][0] if len(wrote) == 1
                 else f"{wrote[0][0]} and {len(wrote) - 1} more")
        caveats.append(f"  the catalog was pushed to{times} between {a_label} and {b_label}: "
                       f"{sum(n for _, n in wrote)} entries written ({where}); if the arms "
                       "are meant to differ by one of them, the others are confounds")
    # Two arms are meant to differ only by context. A field run's round 1 also
    # dropped 28 junk tables from the catalog, and 1 of its 7 fixes came from
    # that, not from documentation; nothing said so because each round read
    # one catalog. The manifests behind the reports know both.
    for (a_label, _, a_catalog, a_db), (b_label, _, b_catalog, b_db) in zip(scored, scored[1:]):
        if a_catalog is not None and b_catalog is not None and a_catalog != b_catalog:
            caveats.append(
                f"  the catalog changed between {a_label} and {b_label}: "
                f"+{len(b_catalog - a_catalog)} tables, -{len(a_catalog - b_catalog)} tables; "
                "a verdict here mixes documentation with catalog membership")
        if a_db and b_db and a_db != b_db:
            caveats.append(
                f"  the database changed between {a_label} and {b_label}; "
                "a verdict here mixes context with whatever changed in the data")
    # Who accepted the golds. A model may draft the review on day one; the
    # verdict says so until a person re-reviews the rows, and names both
    # models when the reviewer and the agent are one family (a shared blind
    # spot then passes as correct). The generate runs know the agent's model;
    # under --backend command the model option is argparse's default and
    # names nothing.
    from .review import answer_key_caveat
    from .workspace import answer_key_review
    review = answer_key_review(runs)
    if review:
        agent_models = []
        for _, run_id, _, _ in scored:
            if not run_id:
                continue
            entry = by_id.get(run_id) or {}
            generate_id = entry.get("produced_by", {}).get("predictions") or run_id
            options = _manifest_of(args.workspace, generate_id).get("options") or {}
            if options.get("backend") != "command" and options.get("model"):
                agent_models.append(options["model"])
        sentence = answer_key_caveat(review["summary"], agent_models)
        if sentence:
            caveats.append(f"  {sentence}")
    return caveats


def _split_ids(workspace: str, split_entry: dict, flag: str) -> set[str] | None:
    """The ids a split run wrote for one half, from the copy the recorder kept."""
    manifest = _manifest_of(workspace, split_entry["run_id"])
    for record in (manifest.get("outputs", {}).get(flag) or []):
        copy = record.get("copy")
        if copy:
            try:
                return _ids_in(str(pathlib.Path(workspace) / "runs" / split_entry["run_id"] / copy))
            except OSError:
                return None
    return None


def _push_counts(workspace: str, run_id: str) -> dict:
    """What one catalog-push wrote, from its own push manifest.

    Read from the manifest the push writes as it goes, not from the run's
    telemetry: a push killed by an SDK error leaves the counts in that file
    and never reaches the line that sets telemetry. On one estate the first
    push wrote 15 entries and then raised NotFound; the last one found every
    page stamped and recorded written 0, so a caveat keyed on telemetry never
    fired although the catalog had changed between the two arms.
    """
    manifest = _manifest_of(workspace, run_id)
    record = (manifest.get("outputs", {}).get("manifest_out") or [{}])[0]
    if record.get("copy"):
        try:
            written = json.loads(
                (pathlib.Path(workspace) / "runs" / run_id / record["copy"]).read_text())
        except (OSError, ValueError):
            written = {}
        if isinstance(written.get("counts"), dict):
            return written["counts"]
    return manifest.get("telemetry") or {}


def _manifest_of(workspace: str, run_id: str) -> dict:
    path = pathlib.Path(workspace) / "runs" / run_id / "manifest.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _arm_paths(args: argparse.Namespace) -> list[tuple[str, list[str]]]:
    """Each arm's label and the files behind it, in round order."""
    if args.command == "ablate":
        return [("without context", (args.without_report or []) + (args.without_context or [])),
                ("with context", (args.with_report or []) + (args.with_context or []))]
    if args.command == "climb":
        return [(spec.partition("=")[0], [spec.partition("=")[2]]) for spec in args.round]
    return []


def _scored_against(workspace: str, paths: list[str]
                    ) -> tuple[str | None, set[str] | None, str | None]:
    """The run behind these files and what it was scored against: its id, its
    catalog read back from the copy the recorder kept, and its database
    identity (a file hash, or the bq:// spec). None for whatever the manifest
    does not know."""
    from .workspace import run_that_wrote
    for path in paths:
        found = run_that_wrote(workspace, path)
        if not found:
            continue
        run_dir, manifest = found
        inputs = manifest.get("inputs", {})
        catalog = None
        record = inputs.get("catalog")
        if isinstance(record, dict) and record.get("copy"):
            try:
                catalog = _load_catalog(str(run_dir / record["copy"]))
            except OSError:
                catalog = None
        db = inputs.get("db") or {}
        return manifest["run_id"], catalog, db.get("sha256") or db.get("path")
    return None, None, None


def _cmd_ablate(args: argparse.Namespace) -> int:
    # Saved reports skip execution entirely: scoring ran once, the paired
    # analysis is free. Mixing files and reports in one arm is allowed.
    if (args.without_context or args.with_context) and not args.db:
        print("ablate needs --db when an arm is given as predictions CSVs "
              "(pass saved reports via --without-report/--with-report to "
              "skip execution)")
        return 2
    # before the dry run: an ablation over ids no arm answered measures nothing
    unheld = _refuse_unheld_ids(args, (args.without_report or []) + (args.with_report or [])
                                + (args.without_context or []) + (args.with_context or []))
    if unheld is not None:
        return unheld
    cache: dict = {}
    save_cache = lambda: None  # noqa: E731
    executor = None
    workers = 1
    if args.without_context or args.with_context:
        executor = _executor_for(args, args.row_cap)
        workers = _resolve_workers(args)
        cache, save_cache = _open_gold_cache(getattr(args, "gold_cache", None), args.db)
        cache = cache if cache is not None else {}
        stop = _cost_before_sql(args, executor, _sqls_of_predictions(
            (args.without_context or []) + (args.with_context or [])),
            reused=frozenset(cache), workers=workers)
        if stop is not None:
            return stop

    only = _ids_in(args.ids) if getattr(args, "ids", None) else None

    def _arm(paths: list[str] | None, reports: list[str] | None):
        scored = [load_report(p, args.confidence) for p in (reports or [])]
        scored += [_score_for_cli(executor, p, args.confidence, cache, workers)
                   for p in (paths or [])]
        if only is not None:
            scored = [subset(r, only, args.confidence) for r in scored]
        return pool(scored)

    without = _arm(args.without_context, args.without_report)
    with_ = _arm(args.with_context, args.with_report)
    save_cache()
    report = ablate(without, with_, adjusted=args.adjusted)
    if only is not None:
        print(f"restricted to {len(only)} ids from {args.ids}: {without.n_scored} scored per arm\n")
    if executor is not None:
        _record_sql_spend(args, executor)
    if _emit_json(args, report):
        return 0
    n_reps = len(args.without_context or []) + len(args.without_report or [])
    if n_reps > 1:
        print(f"pooled {n_reps} repetitions per arm\n")
    print(report.summary())
    _print_judged_note(args, [without, with_])
    for caveat in _conclusion_caveats(args):
        print(caveat)
    if args.show:
        print()
        print(report.flips_summary())
    return 0


def _print_judged_note(args: argparse.Namespace, reports: list) -> None:
    """Judge upgrades reach a verdict only under --adjusted; say so when the
    reports carry judged rows and the raw reading is what printed."""
    judged = sum(1 for r in reports for q in r.results if q.judged)
    if judged and not getattr(args, "adjusted", False):
        print(f"  {judged} judged answers in these reports: this reading is raw execution; "
              "--adjusted reads the judge's upgrades instead")


def _read_context_rules(path: str) -> str:
    """The rule text `attribute` suspects, from a file or a spec directory.

    `--context` takes one rules FILE, while every other command on this CLI
    takes `--spec DIR`. So a directory is the natural thing to pass, and it
    used to raise `IsADirectoryError` as a traceback -- AFTER the run had
    spent $0.0356 executing 286 statements, because the read happens at the
    end. Validation belongs before the spend, and a directory should just
    work.

    A spec directory's rules live under `rules/`, in file-name order, which
    is how `arms` assembles them too. Concatenated with the file name above
    each block so a suspected line can be traced back to the file it came
    from.
    """
    target = pathlib.Path(path)
    if target.is_file():
        return target.read_text()
    if not target.is_dir():
        raise SystemExit(f"the context file (--context) not found: {path}")
    folder = target / "rules" if (target / "rules").is_dir() else target
    files = sorted(f for f in folder.iterdir()
                   if f.is_file() and f.suffix in (".md", ".txt"))
    if not files:
        raise SystemExit(
            f"--context {path} is a directory with no rules in it. Looked in "
            f"{folder} for .md or .txt. Pass the rules file itself, or a spec "
            "directory containing rules/")
    return "\n\n".join(f"<!-- {f.name} -->\n{f.read_text()}" for f in files)


def _cmd_attribute(args: argparse.Namespace) -> int:
    """Score both arms, find the flips, and name the context rules behind them.

    Saved reports skip the execution, like `ablate` and `triage`. The prediction
    CSVs are still needed - a report keeps the verdict, not the SQL, and the SQL
    is what a rule is credited against - but reading a CSV costs nothing. On one
    enterprise warehouse, re-executing to answer "which rule fixed which
    question" about a run scored minutes earlier was about $1.45.
    """
    # Read the context FIRST. It was read at the end, so a bad --context
    # raised after the executor had already run 286 statements and billed
    # them. Nothing here costs anything, and it fails in one line.
    context_text = _read_context_rules(args.context)
    for path in list(args.without_context) + list(args.with_context):
        _require_file(path, "a predictions CSV")

    cache: dict = {}
    save_cache = lambda: None  # noqa: E731
    reports = bool(args.without_report and args.with_report)
    if args.without_report and not args.with_report or args.with_report and not args.without_report:
        print("attribute needs a saved report for both arms or neither; one arm from "
              "reports and one from execution would compare two different scorings")
        return 2
    executor = None
    workers = 1
    if not reports:
        if not args.db:
            print("attribute needs --db to score the predictions (or pass saved reports "
                  "via --without-report/--with-report to skip execution)")
            return 2
        executor = _executor_for(args, args.row_cap)
        workers = _resolve_workers(args)
        cache, save_cache = _open_gold_cache(getattr(args, "gold_cache", None), args.db)
        cache = cache if cache is not None else {}
        stop = _cost_before_sql(args, executor, _sqls_of_predictions(
            args.without_context + args.with_context),
            reused=frozenset(cache), workers=workers)
        if stop is not None:
            return stop

    only = _ids_in(args.ids) if getattr(args, "ids", None) else None

    def _arm(paths: list[str], saved: list[str] | None):
        if saved:
            scored = [load_report(p, args.confidence) for p in saved]
        else:
            scored = [_score_for_cli(executor, p, args.confidence, cache, workers)
                      for p in paths]
        if only is not None:
            scored = [subset(r, only, args.confidence) for r in scored]
        return pool(scored)

    without = _arm(args.without_context, args.without_report)
    with_ = _arm(args.with_context, args.with_report)
    save_cache()
    ablation = ablate(without, with_)
    if executor is not None:
        _record_sql_spend(args, executor)

    # Observation id -> that repetition's own SQL, so pooled reps are diffed
    # against themselves, never against another rep's SQL.
    def _sql_by_observation(paths: list[str]) -> dict[str, str]:
        table: dict[str, str] = {}
        for rep_index, path in enumerate(paths):
            for qid, _question, _gold, pred in _load_predictions(path):
                # pool() suffixes every id with #repN, single file included,
                # so the SQL keys must match that or no flip ever attributes
                table[f"{qid}#rep{rep_index}"] = pred
        return table

    report = attribute(
        ablation,
        _sql_by_observation(args.without_context),
        _sql_by_observation(args.with_context),
        parse_rules(context_text),
        n_rule_lines_without_identifier=count_rule_lines_without_identifier(context_text),
    )
    if _emit_json(args, report):
        return 0
    print(ablation.summary())
    print()
    print(report.summary())
    return 0


def _load_question_rows(path: str) -> list[tuple[str, str, str]]:
    """Read an (id, question, gold_sql) CSV - the holdout/questions shape."""
    with open(path, newline="") as handle:
        reader = csv.DictReader(handle)
        _require_columns(path, reader, ("id", "question", "gold_sql"),
                         "gate --clean-out keeps all input columns, so its "
                         "output works here if the input had a question column.")
        return [
            (row["id"], row["question"], row["gold_sql"])
            for row in reader
        ]


def _cmd_generate(args: argparse.Namespace) -> int:
    rows = _load_question_rows(args.questions)
    if args.dialect is None:
        # two paid arms on one estate were generated as SQLite SQL because the
        # default never looked at the warehouse; the workspace already knows it
        spec = args.db or _workspace_db_spec(args)
        args.dialect = "BigQuery" if spec and is_bigquery(spec) else "SQLite"
        if args.dialect == "BigQuery":
            print(f"dialect: BigQuery (from {'--db' if args.db else 'the workspace'}; "
                  "--dialect overrides)", file=sys.stderr)
    if not args.schema and args.backend != "command":
        print("generate needs --schema (the CREATE TABLE text from `contextlift schema`); "
              "only --backend command may run without one, because the agent fetches its own")
        return 2
    schema = pathlib.Path(args.schema).read_text() if args.schema else ""
    context = pathlib.Path(args.context).read_text() if args.context else ""
    per_question = {}
    if args.examples:
        per_question = json.loads(pathlib.Path(args.examples).read_text())
    if getattr(args, "context_per_question", None):
        # A retriever's output: one question, the context retrieved for it.
        # Merged with --examples rather than replacing it, because a worked
        # example and a table's documentation are different things and an arm
        # may want both.
        from .context import MappingContextSource, retrieved_fingerprint
        source = MappingContextSource.from_json(args.context_per_question)
        retrieved = {qid: source.for_question(qid, question) for qid, question, _ in rows}
        empty = [qid for qid, text in retrieved.items() if not text]
        if empty:
            shown = ", ".join(empty[:3]) + ("..." if len(empty) > 3 else "")
            print(f"{len(empty)} of {len(rows)} questions retrieved nothing ({shown}); "
                  f"they are answered without context", file=sys.stderr)
        for qid, text in retrieved.items():
            if text:
                per_question[qid] = (f"{per_question[qid]}\n\n{text}"
                                     if per_question.get(qid) else text)
        # The arm is what the model saw, so it is named after that and not after
        # the file: the same retriever over a source that changed overnight is a
        # different arm, and nothing else on disk would say so.
        args.retrieved_sha256 = retrieved_fingerprint(retrieved)

    if args.backend == "echo":
        from .backends import echo_backend
        backend = echo_backend(args.echo_response)
    elif args.backend == "command":
        if not args.runner_command:
            print("--backend command needs --command 'PROGRAM' (JSON on stdin, SQL on stdout)")
            return 2
        from .backends import command_backend
        backend = command_backend(args.runner_command, timeout=args.timeout)
    else:
        # Imported lazily: generation against a real model needs the extra,
        # the offline echo path never does.
        from .backends import gemini_backend
        try:
            backend = gemini_backend(
                model=args.model, project=args.project, max_output_tokens=2048
            )
        except ImportError:
            print(f"the [gemini] extra is not installed in this Python "
                  f"({sys.executable}): uv pip install -e '.[gemini]'")
            return 2

    # Cost before spending: the real prompts are counted, then priced.
    prices = price_table(args.model, args.input_price, args.output_price)
    if args.backend == "command":
        # the runner spends whatever it spends; the framework counts calls
        prices = None
        print(f"{len(rows)} questions x {args.reps} reps = {len(rows) * args.reps} runs of "
              f"{args.runner_command!r}; the runner's own cost is not known to contextlift")
        print("  the runner inherits this shell's credentials and environment; the read-only "
              "rail and the byte ceiling cover only what contextlift executes, not what the "
              "runner runs. Give it a read-only service account and its own byte ceiling.")
        if args.estimate_only:
            return 0
        if args.budget is not None:
            print("--budget has no meaning for --backend command; the runner's spend is its own")
            return 2
    backend.prices = prices
    backend.budget_usd = args.budget
    output_per_call, output_source = _output_tokens_per_call(args)
    # a progress note, on stderr so `--estimate-only --json` stays pure JSON
    if args.backend != "command":
        print(f"counting {len(rows)} prompts on {args.model} before spending "
              f"(free; a credentials lookup can take a few seconds)...",
              file=sys.stderr, flush=True)
    try:
        estimate = estimate_run(
            rows, schema, backend.count_tokens, context=context,
            per_question_context=per_question, dialect=args.dialect,
            reps=args.reps, output_tokens_per_call=output_per_call,
            workers=args.workers,
        )
    except CredentialsMissing as problem:
        print(problem)
        return 2
    estimate["output_tokens_source"] = output_source
    estimate["estimated_cost_usd"] = estimate_cost_usd(
        prices, estimate["prompt_tokens"], estimate["output_tokens"])
    estimate["price_table"] = prices
    if args.estimate_only and args.json:
        print(json.dumps(estimate, indent=2))
    elif args.backend != "command":
        print(_format_estimate(estimate, args.model))
    if args.estimate_only:
        return 0
    if args.budget is not None:
        if estimate["estimated_cost_usd"] is None:
            print(f"--budget needs a price and {args.model} has none in the "
                  "table; pass --input-price and --output-price (USD per "
                  "million tokens)")
            return 2
        if estimate["estimated_cost_usd"] > args.budget:
            print(f"refusing to start: estimate ${estimate['estimated_cost_usd']:.4f}"
                  f" is above the budget ${args.budget:.4f}")
            return 2

    if args.backend not in ("echo", "command"):
        stop = _paid_gate(args, "generating answers")
        if stop is not None:
            return stop

    retry_executor = None
    if args.retry:
        if not args.db:
            print("--retry needs --db (the SQL must run somewhere)")
            return 2
        retry_executor = make_executor(
            args.db, timeout_seconds=args.timeout, row_cap=5,
            maximum_bytes_billed=args.max_bytes,
        )

    all_reps: list = []
    runner_config: dict = {}
    for rep in range(1, args.reps + 1):
        out = (
            args.out if args.reps == 1
            else str(pathlib.Path(args.out).with_suffix("")) + f"_rep{rep}.csv"
        )
        def report_progress(done: int, total: int, rep=rep) -> None:
            # every tenth answer and the last: a run log that moves, on
            # stderr so stdout stays what it was
            if done == total or done % max(1, total // 10) == 0:
                print(f"answered {done} of {total} (run {rep} of {args.reps})",
                      file=sys.stderr, flush=True)

        routed: dict[str, str] = {}
        notes: dict[str, str] = {}
        stats: dict = {}
        generated = generate_predictions(
            rows, schema, backend,
            context=context,
            per_question_context=per_question,
            dialect=args.dialect,
            retry_executor=retry_executor,
            workers=args.workers,
            progress=report_progress,
            rep=rep, routed=routed, notes=notes, stats=stats,
        )
        write_predictions(generated, out, notes=notes or None)
        if stats.get("runner_config") and not runner_config:
            runner_config = stats["runner_config"]
        if retry_executor is not None:
            print(f"  --retry: {stats.get('retried', 0)} of {len(generated)} drafts failed the check "
                  "or returned nothing and got a second chance")
        all_reps.append(generated)
        abstained = sum(1 for n in notes.values() if n.startswith("abstained"))
        if abstained:
            print(f"  {abstained} abstained (the runner said no table fits; a note column says so)")
        if routed:
            # the runner said which table it picked: a routing CSV beside the
            # predictions, so score-routing grades the router on its own
            from .mine import tables_named
            sidecar = str(pathlib.Path(out).with_suffix("")) + "_routing.csv"
            with open(sidecar, "w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["id", "question", "gold_table", "pred_table"])
                for qid, question, gold_sql, _ in generated:
                    named = tables_named(gold_sql) if gold_sql else []
                    writer.writerow([qid, question, named[0] if named else "", routed.get(qid, "")])
            print(f"routing -> {sidecar}  (score-routing --predictions {sidecar} --catalog CATALOG)")
        empty = sum(1 for r in generated if not r[3])
        print(f"wrote {len(generated)} predictions -> {out}"
              f"{f'  ({empty} EMPTY)' if empty else ''}")

    if args.reps > 1:
        from .generate import identical_across_reps
        same, total = identical_across_reps(all_reps)
        if same == total:
            print(f"  identical across repetitions: {same} of {total} questions; these are "
                  "copies, not samples, and a confidence interval over them means nothing")
        else:
            print(f"  identical across repetitions: {same} of {total} questions")
    usage = dict(backend.usage)
    spent = estimate_cost_usd(prices, usage["prompt_tokens"],
                              usage["output_tokens"], usage["thought_tokens"])
    args.telemetry = {**usage, "estimated_cost_usd": spent,
                      "estimate": estimate, "price_table": prices,
                      "runner_config": runner_config or None}
    if args.backend == "command":
        print(f"ran: {usage['calls']} calls, {usage.get('failures', 0)} failed, "
              f"{usage.get('timeouts', 0)} timed out; cost is the runner's own")
    else:
        print(spend_line(usage, spent, 'spent'))
    if usage["stopped_at_budget"]:
        print(f"STOPPED at the ${args.budget:.2f} budget: every answer after "
              "that point is EMPTY, so the files above are partial")
        return 3
    return 0


def _output_tokens_per_call(args: argparse.Namespace) -> tuple[int, str]:
    """Output tokens to assume per call: measured from the newest recorded
    generate run in the workspace, else a stated default."""
    default = 80
    if not args.workspace:
        return default, "default; measured after the first recorded run"
    manifests = []
    for manifest_path in pathlib.Path(args.workspace).glob("runs/*-generate*/manifest.json"):
        try:
            manifests.append(json.loads(manifest_path.read_text()))
        except (OSError, ValueError):
            continue
    # newest by start time, not by folder name: same-second runs get a
    # "-2" suffix that sorts before the unsuffixed one
    manifests.sort(key=lambda m: m.get("started_at") or "")
    for manifest in reversed(manifests):
        telemetry = manifest.get("telemetry") or {}
        if telemetry.get("calls"):
            per_call = (telemetry["output_tokens"] + telemetry["thought_tokens"]) \
                / telemetry["calls"]
            return max(1, round(per_call)), f"measured on {manifest['run_id']}"
    return default, "default; measured after the first recorded run"


def _format_estimate(estimate: dict, model: str) -> str:
    cost = estimate["estimated_cost_usd"]
    prices = estimate["price_table"]
    cost_text = (
        f"~${cost:.2f} on {model} (prices as of {prices['as_of']})"
        if cost is not None else
        f"cost unknown: no price for {model}; pass --input-price/--output-price"
    )
    return (f"estimated: {estimate['questions']} questions x {estimate['reps']} "
            f"reps = {estimate['calls']} calls, "
            f"~{estimate['prompt_tokens_per_call']:,} prompt tokens per call "
            f"(counted), ~{estimate['output_tokens_per_call']} output tokens per "
            f"call ({estimate['output_tokens_source']}), {cost_text}"
            + (f"\n  {estimate['prompts_estimated']} prompt(s) could not be counted "
               "(the counter was unreachable); those are approximated at four characters "
               "a token, so this price is close rather than measured"
               if estimate.get("prompts_estimated") else "")
            # A cost sheet that omits its largest line is worse than no cost
            # sheet. Found 2026-09-12 by reading the job ledger instead of
            # this output: per-command totals came to about $74 for a day
            # the ledger put at $149.61. The gap was the predicted queries,
            # which nothing here priced and nothing here mentioned.
            #
            # It CANNOT be priced in advance -- the model has not written
            # that SQL yet -- so the honest move is to name the omission and
            # the command that recovers it afterwards.
            + "\n  model calls only. The SQL the agent writes is not priced "
              "here and cannot be: it does not exist until the run does. "
              "Afterwards, `make answer-cost RUN=<run>.csv PROJECT=<project>` "
              "reads the billed bytes from the job ids the run records. On one "
              "measured day that omitted line was larger than everything "
              "above it.")


def _cmd_curate_examples(args: argparse.Namespace) -> int:
    pool = _load_question_rows(args.pool)
    targets = _load_question_rows(args.targets)
    bank = build_example_bank(
        pool, targets, per_question=args.per_question
    )
    save_bank(bank, args.out)
    print(bank.summary())
    print(f"bank -> {args.out}  (pass to: generate --examples {args.out})")
    return 0


def _workspace_db_spec(args: argparse.Namespace) -> str | None:
    """The bq:// spec (or SQLite path) a workspace's bigquery.txt names, if any."""
    workspace = getattr(args, "workspace", None)
    if not workspace:
        return None
    try:
        first = pathlib.Path(workspace, "bigquery.txt").read_text().splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    return first or None


def _is_saved_report(path: str) -> bool:
    """A score report JSON, as save_report writes it, rather than a CSV."""
    try:
        with open(path) as handle:
            head = handle.read(2048)
    except OSError:
        return False
    return head.lstrip().startswith("{") and '"results"' in head


def _cmd_climb(args: argparse.Namespace) -> int:
    """Rounds are predictions CSVs (scored here) or saved reports (free);
    the same label may repeat with several reports, one per repetition."""
    labelled: dict[str, list[str]] = {}
    order: list[str] = []
    for spec in args.round:
        label, _, path = spec.partition("=")
        if not path:
            print(f"bad --round {spec!r}: expected LABEL=path/to/predictions.csv or a saved report")
            return 2
        if label not in labelled:
            order.append(label)
        labelled.setdefault(label, []).append(path)
    csv_paths = [p for paths in labelled.values() for p in paths if not _is_saved_report(p)]
    unheld = _refuse_unheld_ids(args, [p for paths in labelled.values() for p in paths])
    if unheld is not None:
        return unheld
    only = _ids_in(args.ids) if getattr(args, "ids", None) else None
    executor = None
    cache: dict = {}
    save_cache = lambda: None  # noqa: E731
    workers = 1
    if csv_paths:
        if not args.db:
            print("climb needs --db when a round is given as a predictions CSV "
                  "(saved reports need no database)")
            return 2
        executor = _executor_for(args, args.row_cap)
        workers = _resolve_workers(args)
        cache, save_cache = _open_gold_cache(getattr(args, "gold_cache", None), args.db)
        cache = cache if cache is not None else {}
        stop = _cost_before_sql(args, executor, _sqls_of_predictions(csv_paths),
                                reused=frozenset(cache), workers=workers)
        if stop is not None:
            return stop

    def _round(paths: list[str]):
        scored = [load_report(p, args.confidence) if _is_saved_report(p)
                  else _score_for_cli(executor, p, args.confidence, cache, workers)
                  for p in paths]
        if only is not None:
            scored = [subset(r, only, args.confidence) for r in scored]
        return pool(scored)

    rounds = [(label, _round(labelled[label])) for label in order]
    save_cache()
    report = climb(rounds, adjusted=args.adjusted, holdout_used=args.holdout)
    if executor is not None:
        _record_sql_spend(args, executor)
    if _emit_json(args, report):
        return 0
    print(report.summary())
    _print_judged_note(args, [r for _, r in rounds])
    for caveat in _conclusion_caveats(args):
        print(caveat)
    if args.show:
        print()
        print(report.flips_summary())
    return 0


def _cmd_lint(args: argparse.Namespace) -> int:
    """Refuse written context with a defect somebody could have seen.

    Free and offline. It reads the spec and, when given them, the answer
    keys. The exit code is the point: 1 when a refusal fired, so a script
    can put it in front of a paid round.

    What passing does NOT mean: that the context is good. Context quality is
    only knowable by measurement, which is what `round` is for. This stops
    you paying for a round whose context has a visible fault.
    """
    from .export import read_spec
    from .lint import lint

    spec = read_spec(args.spec)
    gold_sql = None
    if args.golds:
        _require_file(args.golds, "the gold set CSV (--golds)")
        gold_sql = [row["gold_sql"]
                    for row in csv.DictReader(open(args.golds, newline=""))]
    report = lint(spec, gold_sql)
    if getattr(args, "json", False):
        print(json.dumps({
            "passed": report.passed,
            "rules_seen": report.rules_seen,
            "columns_seen": report.columns_seen,
            "skipped": report.skipped,
            "findings": [{"check": f.check, "severity": f.severity,
                          "where": f.where, "detail": f.detail,
                          "basis": f.basis} for f in report.findings],
        }, indent=2))
    else:
        print(report.summary())
    return 0 if report.passed else 1


def _cmd_triage(args: argparse.Namespace) -> int:
    # A saved score report already holds every field triage reads (verdict,
    # detail, judge reason), so the SQL never runs twice.
    if args.report:
        report = load_report(args.report, args.confidence)
    elif args.db and args.predictions:
        executor = _executor_for(args, args.row_cap)
        workers = _resolve_workers(args)
        cache, save_cache = _open_gold_cache(getattr(args, "gold_cache", None), args.db)
        stop = _cost_before_sql(args, executor, _sqls_of_predictions([args.predictions]),
                                reused=frozenset(cache or ()), workers=workers)
        if stop is not None:
            return stop
        report = _score_for_cli(executor, args.predictions, args.confidence, cache, workers)
        save_cache()
        _record_sql_spend(args, executor)
    else:
        print("triage needs --report FILE (a saved score report) or both "
              "--db and --predictions")
        return 2
    result = triage(report, adjusted=not args.execution_only)
    if _emit_json(args, result):
        return 0
    print(result.summary())
    if args.show:
        print("\nfailing questions:")
        for item in result.classified:
            print(f"  [{item.bucket:<13}] {item.id}: {item.question[:70]}")
            print(f"      {item.evidence[:110]}")
    return 0


def _add_run_flags(command_parser: argparse.ArgumentParser) -> None:
    """Flags shared by every command that runs SQL or prints a report.

    --timeout bounds how long ONE statement may run. row_cap cannot do this:
    it limits rows fetched, and a query whose ORDER BY forces a full sort does
    all its work before the first row exists. Predicted SQL is untrusted model
    output, so an unbounded run is the default only for compatibility.

    --json prints the full report as JSON instead of prose, so pipelines read
    fields, never regexes over the human summary.
    """
    command_parser.add_argument(
        "--timeout", type=float, default=None, metavar="SECONDS",
        help="abort any single query that runs longer than this; it scores as "
             "an error instead of hanging the run (unset: 60 s; 0 for no bound)")
    command_parser.add_argument(
        "--workers", type=int, default=None, metavar="N",
        help="run this many queries at once (default 1 on SQLite, 4 on BigQuery); "
             "threads inside one command, not several commands")
    command_parser.add_argument(
        "--gold-cache", metavar="FILE",
        help="reuse the answer key's results between runs, keyed by gold SQL. The same "
             "golds run in every arm and every repetition; on a two-arm three-rep "
             "ablation five of every six gold executions are waste. The file records "
             "which database it belongs to and starts empty on a mismatch. Opt-in: only "
             "you know whether the data changed since it was written; under --workspace "
             "defaults to WORKSPACE/cache/golds.json")
    command_parser.add_argument(
        "--json", action="store_true",
        help="print the report as JSON instead of prose")
    _add_warehouse_flags(command_parser)
    command_parser.add_argument(
        "--save-report", default=None, metavar="FILE",
        help="write the report as JSON to FILE (a score report can be reused "
             "by ablate/climb --*-report and triage --report instead of "
             "re-executing SQL); under --workspace it defaults into the run "
             "directory")


def _watching() -> bool:
    """True when a person is looking at this, rather than a file or a pipe.

    A host agent gets the plain lines too: it reads the output as text, and
    cursor-movement codes are noise to it.
    """
    if (os.environ.get("CONTEXTLIFT_PLAIN_PROGRESS") or "").strip():
        return False
    if identity.caller() != "terminal":
        return False
    try:
        return sys.stdout.isatty()
    except Exception:  # noqa: BLE001 - a replaced stdout is not an error
        return False


def _repaint(state: dict, block: str) -> str:
    """`block`, with the cursor moved back over whatever was printed last.

    Cursor-up then clear-to-end, rather than a screen wipe: wiping makes
    everything above the display flicker, and a person watching for twenty
    minutes notices.
    """
    printed = state.get("lines", 0)
    back = f"\033[{printed}A\033[J" if printed else ""
    state["lines"] = block.count("\n") + 1
    return back + block + "\n"


def _retry_note(writer):
    """What to add to a heartbeat line about a stage's model calls.

    Retries, and the seconds spent sleeping between them. This is the
    sentence that tells a waiting person whether to keep waiting: one stage
    sat idle for six minutes with no warehouse query and nothing on screen,
    and the answer was in a counter nobody printed. Returns a function
    because the numbers have to be read at the moment the line is written,
    from another thread.
    """
    if writer is None or getattr(writer, "backend", None) is None:
        return None

    def note() -> str:
        usage = getattr(writer.backend, "usage", None) or {}
        retries = usage.get("retries") or 0
        if not retries:
            # Worth saying out loud: "0 retries" turns "why is this slow"
            # from a guess into a different question.
            return f"{usage.get('calls', 0)} model calls, 0 retries"
        code = usage.get("last_retry_code") or 0
        waited = usage.get("retry_seconds") or 0
        return (f"{retries} model retries ({code}), "
                f"{duration_text(waited)} waiting")

    return note


def duration_text(seconds: float) -> str:
    from .progress import duration
    return duration(seconds)


def _progress_printer(stage: str, usd=None, heartbeat_extra=None):
    """A `(done, total, counts)` callback that prints one throttled line.

    The stage passes its own funnel and this decides when to speak, so no
    stage carries a counter or a clock. Built lazily: the first call fixes
    the total, because a stage knows how many items it has only after its
    own filtering.

    `usd` is called with no arguments and returns dollars billed so far. It
    is worth a column of its own on every line: one mine was estimated at
    $8.89 and had billed $12.41 before anybody looked, because the only
    place spend appeared was the summary AFTER the run.
    """
    from .progress import Heartbeat, Progress, screen
    state: dict = {}

    def say(done: int, total: int, counts: dict | None = None,
            last: str = "") -> None:
        tracker = state.get("p")
        if tracker is None or tracker.total != total:
            tracker = state["p"] = Progress(stage, total)
            # Started with the tracker, stopped when the process ends: a
            # daemon thread cannot outlive it. This is what makes silence
            # impossible, because it does not wait for the loop to come
            # back and ask the time.
            old_beat = state.get("beat")
            if old_beat is not None:
                old_beat.stop()
            state["beat"] = Heartbeat(
                tracker, say=lambda text: print(text, flush=True),
                extra=heartbeat_extra).start()
        beat = state.get("beat")
        if beat is not None:
            beat.now_on(done, total)
        # A broken spend reading must not take the progress line down with
        # it: the line is what a person watches to decide whether to wait.
        try:
            money = usd() if usd is not None else None
        except Exception:  # noqa: BLE001 - a missing usage dict, nothing more
            money = None
        # A person watching gets the live display, repainted in place, with
        # the funnel drawn as one bar. A file gets the appended line it has
        # always got: a repainting display written to a file is one
        # unreadable blob, and the log is what a stalled run is debugged
        # from.
        if _watching():
            block = screen(stage, done, total, tracker.elapsed, usd=money,
                           counts=counts, last=last)
            print(_repaint(state, block), end="", flush=True)
            if done >= total and beat is not None:
                beat.stop()
            return
        text = (tracker.done(done, usd=money, counts=counts) if done >= total
                else tracker.tick(done, usd=money, counts=counts))
        if text:
            print(text, flush=True)
        if done >= total and beat is not None:
            beat.stop()

    return say


def _cmd_mine_context(args: argparse.Namespace) -> int:
    """Business conventions counted out of a query log. Free: no model call,
    no warehouse query, one pass over a CSV."""
    from .conventions import (code_mappings, metric_formulas, render_metrics,
                              render_vocabulary)
    from .mine import fingerprint
    _require_file(args.queries, "the query log")
    with open(args.queries, newline="") as handle:
        rows = list(csv.DictReader(handle))
    sqls = [r.get("sql") or "" for r in rows]
    shapes = len({fingerprint(s) for s in sqls if s})

    # Every scored question's own SQL is excluded BEFORE counting. A reserved
    # question's statement is in this log -- that is where the question came
    # from -- so counting it would put a held-out answer into the context,
    # and the ordinary leak gate cannot see it because a formula is a
    # fragment rather than the whole SQL.
    banned: set[str] = set()
    protected = 0
    if args.golds:
        _require_file(args.golds, "the gold set")
        with open(args.golds, newline="") as handle:
            golds = {r["id"]: r.get("gold_sql", "") for r in csv.DictReader(handle)}
        ids = set(golds)
        if args.exclude_ids:
            _require_file(args.exclude_ids, "the ids to exclude")
            ids = {line.strip() for line in open(args.exclude_ids) if line.strip()}
        banned = {fingerprint(golds[i]) for i in ids if golds.get(i)}
        protected = len(banned)

    formulas = metric_formulas(sqls, args.min_times, banned)
    mappings = code_mappings(sqls, args.min_times, banned)

    print(f"read {len(sqls)} statements, {shapes} distinct query shapes")
    print(f"  {protected} scored question(s) excluded before counting, so no "
          "answer key can reach the context")
    print(f"  {len(formulas)} metric convention(s) seen {args.min_times}+ times:")
    for formula, times in formulas[:12]:
        print(f"      {times:4} shapes   {formula}")
    print(f"  {len(mappings)} code mapping(s):")
    for column, pairs, times in mappings[:8]:
        shown = ", ".join(f"{c}={m}" for c, m in pairs[:4])
        print(f"      {times:4} shapes   {column}: {shown}")
    if not formulas and not mappings:
        print("  nothing repeated often enough to be a convention. Lower "
              "--min-times to see what is close, but read it before trusting it")
        return 0

    out = pathlib.Path(args.out)
    (out / "rules").mkdir(parents=True, exist_ok=True)
    (out / "vocabulary").mkdir(parents=True, exist_ok=True)
    rules_path = out / "rules" / "10-mined-metrics.md"
    vocab_path = out / "vocabulary" / "mined.yaml"
    rules_path.write_text(render_metrics(formulas, shapes))
    vocab_path.write_text(render_vocabulary(mappings))
    print(f"\nwrote {rules_path}")
    print(f"wrote {vocab_path}")
    print("  Nothing in either file was written by a person or a model: every "
          "line is arithmetic or a mapping this many DIFFERENT queries agreed "
          "on. Read them before a round; a count is evidence, not a decision.")
    return 0


def _cmd_audit_golds(args: argparse.Namespace) -> int:
    """Does the answer key contradict what this warehouse's own queries agree
    on? Free: no model call, no warehouse query."""
    from .conventions import (fiscal_columns_in, golds_asking_the_same_question,
                              golds_using_a_calendar_grain,
                              golds_using_half_a_formula, metric_formulas)
    _require_file(args.golds, "the gold set")
    _require_file(args.queries, "the query log")
    with open(args.golds, newline="") as handle:
        golds = [(r["id"], r.get("question", ""), r.get("gold_sql", ""))
                 for r in csv.DictReader(handle)]
    with open(args.queries, newline="") as handle:
        sqls = [r.get("sql") or "" for r in csv.DictReader(handle)]

    fiscal = fiscal_columns_in(sqls)
    formulas = metric_formulas(sqls, args.min_times)
    calendar = golds_using_a_calendar_grain(golds, fiscal)
    half = golds_using_half_a_formula(golds, formulas)
    duplicates = golds_asking_the_same_question(golds)

    print(f"{len(golds)} golds against {len(sqls)} statements of this "
          f"warehouse's own history")
    print(f"  fiscal grain columns it uses: {', '.join(sorted(fiscal)) or 'none'}")
    print(f"  metric conventions seen {args.min_times}+ times: {len(formulas)}")
    print("")

    flagged: set[str] = set()
    if calendar:
        print(f"{len(calendar)} gold(s) compute a time grain with a calendar "
              "function while this warehouse keeps a fiscal column for it:")
        for gid, grain, column in calendar:
            print(f"    {gid}   uses {grain}, the warehouse has {column}")
            flagged.add(gid)
        print("    An agent told to use the fiscal column will disagree with "
              "these keys BY FOLLOWING THE CONVENTION, and score wrong for it.")
    if half:
        print(f"\n{len(half)} gold(s) use one side of a formula this warehouse "
              "repeats:")
        for gid, column, formula, times in half:
            print(f"    {gid}   sums {column} alone; {times} query shapes "
                  f"compute {formula}")
            flagged.add(gid)
        print("    A gross figure where the business reports a net one.")
    if duplicates:
        print(f"\n{len(duplicates)} group(s) of golds asking the same question "
              "with different SQL:")
        for group in duplicates:
            print(f"    {', '.join(group)}")
            flagged.update(group[1:])
        print("    One question cannot have two right answers. Keep one.")
    if not flagged:
        print("nothing disagrees with a convention, and no question is asked twice.")
        print("  That is not proof the key is right. It is the absence of the "
              "three disagreements this can see.")
        return 0

    print(f"\n{len(flagged)} of {len(golds)} golds disagree with a convention "
          f"or repeat a question ({len(flagged) / len(golds):.0%}).")
    print("  This says they DISAGREE. It does not say which one is right, and "
          "it cannot: a pattern in the query log is evidence, not an "
          "authority.")
    print("  The case that decides it is the question's own wording. If most "
          "queries use a fiscal column and this question asks for a CALENDAR "
          "period, the key is right and this row is a false alarm. Read the "
          "question, then the SQL, before dropping a gold.")
    if args.out:
        with open(args.golds, newline="") as handle:
            rows = list(csv.DictReader(handle))
            columns = list(rows[0].keys()) if rows else []
        kept = [r for r in rows if r["id"] not in flagged]
        with open(args.out, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(kept)
        print(f"\nwrote {args.out}: {len(kept)} golds, {len(flagged)} left out")
        print("  Re-run `split` on this file: the halves change when the key does")
    return 0


def _split_from_files(args: argparse.Namespace) -> tuple[list[str], list[str]] | None:
    """The split `contextlift split` wrote, if it can be found.

    Looked for beside `--golds`, because that is where `split` puts it, and
    a step whose output nothing reads is a step people stop running. Printed
    either way: silently using a file somebody forgot about is as bad as
    silently ignoring one.
    """
    given_c = getattr(args, "curate_ids", None)
    given_h = getattr(args, "holdout_ids", None)
    if given_c or given_h:
        if not (given_c and given_h):
            raise SystemExit("--curate-ids and --holdout-ids go together: one "
                             "half alone cannot say where the other questions went")
        curate, holdout = pathlib.Path(given_c), pathlib.Path(given_h)
        _require_file(str(curate), "the working ids (--curate-ids)")
        _require_file(str(holdout), "the reserved ids (--holdout-ids)")
    else:
        beside = pathlib.Path(args.golds).parent
        curate, holdout = beside / "curate_ids.txt", beside / "holdout_ids.txt"
        if not (curate.is_file() and holdout.is_file()):
            print(f"  no split files beside {args.golds}, so the round makes "
                  f"its own from --holdout-share and --seed. Run `split` first "
                  "if you built worked examples from one half")
            return None
    ids = ([l.strip() for l in curate.read_text().splitlines() if l.strip()],
           [l.strip() for l in holdout.read_text().splitlines() if l.strip()])
    print(f"  split read from {curate.name} and {holdout.name}: "
          f"{len(ids[0])} working, {len(ids[1])} reserved")
    return ids


def _byte_size(text: str) -> int:
    """`--max-bytes` as a person writes it. argparse prints the message of an
    ArgumentTypeError and swallows the message of a ValueError, so the
    sentence explaining that 50 means fifty bytes only reaches anybody
    through this wrapper."""
    try:
        return parse_bytes(text)
    except ValueError as problem:
        raise argparse.ArgumentTypeError(str(problem)) from problem


# Rows fetched per query in a round. A business question's answer is tens or
# hundreds of rows; all 39 golds on one estate returned under 10,000. The cap
# is not there for the golds, it is there for the PREDICTIONS: a round runs
# SQL the model wrote, and one prediction with no aggregation had the process
# at 6.5 GB and climbing 10 MB a second before it was killed. Generous enough
# that a real answer is never cut, small enough that a runaway is stopped.
DEFAULT_ROW_CAP = 10_000

# Shared with conventions.MIN_TIMES, named here so the help text can print it.
from .conventions import MIN_TIMES as CONVENTION_MIN_TIMES

DB_HELP = "path to the SQLite database, or bq://PROJECT[/DATASET] for BigQuery"


def _add_dataset_scope_flags(command_parser: argparse.ArgumentParser) -> None:
    """How a metadata command names the datasets it reads on BigQuery. One
    estate had 27 datasets and no flag for them: the command ran once per
    dataset and the outputs were joined by hand."""
    command_parser.add_argument(
        "--datasets", default=None, metavar="A,B",
        help="BigQuery: read these datasets, in this order, into one output; a bare name is "
             "the billing project's, `data-proj.sales` names another project's "
             "(default: the dataset in --db)")
    command_parser.add_argument(
        "--tables-file", default=None, metavar="FILE",
        help="only the tables listed in FILE, one dataset.table per line, #-comments "
             "skipped: what catalog-list writes. Its datasets are the ones read when "
             "--datasets is not given")


def _add_warehouse_flags(command_parser: argparse.ArgumentParser,
                         estimate: bool = True, live_stop: bool = False) -> None:
    """Cost control for a warehouse. Ignored on SQLite, where a run is free.

    `live_stop` says the command also watches what it has billed and stops
    at the ceiling. Only the commands that really do it say so: a flag named
    `--budget` that is compared to an estimate and then forgotten let one
    run pass $12.41 against an $8.89 estimate with nothing to halt it, and
    the help had promised a budget.
    """
    command_parser.add_argument(
        "--max-bytes", type=_byte_size, default=DEFAULT_MAX_BYTES, metavar="SIZE",
        help="BigQuery: refuse any single query whose dry-run scans more than "
             "this (default 1 GB, about $0.006). A unit or plain bytes: 50GB, "
             "500MB, 2TiB, 50000000000. A plain number under 10 MB is refused, "
             "because `--max-bytes 50` means fifty bytes and refuses every "
             "query on the estate")
    if estimate:
        command_parser.add_argument(
            "--budget", type=float, default=None, metavar="USD",
            help=("BigQuery: refuse to start when the dry-run estimate is above "
                  "this, and stop mid-run once this much has actually been "
                  "billed (what is done by then is kept and written)"
                  if live_stop else
                  "BigQuery: refuse to start when the dry-run estimate is above "
                  "this. It does NOT stop a run already going: the real scan can "
                  "come in above the dry run"))
        command_parser.add_argument(
            "--estimate-only", action="store_true",
            help="BigQuery: dry-run every statement, print the estimate, run nothing")


# The eight verbs that complete ONE measurement, in the order a person runs
# them. Everything else still works and still has its own --help; it is only
# left out of the default listing.
#
# Why this line and not another: these eight are what it takes to get from
# nothing to a verdict. `classes`, `attribute` and `history` are what you run
# AFTER you have a result, so they are a second step rather than a first one,
# and `next` points at them when the time comes. The epilog names them anyway,
# because a verb nobody can find is a verb nobody uses.
#
# Chosen over the M10 spec's "the rest under `advanced`". Moving forty verbs to
# `contextlift advanced <verb>` would rename every command in the docs and in
# every script already written against them, to fix a help page. Same benefit,
# no breakage: every verb keeps its name.
CORE_VERBS = ("demo", "doctor", "gate", "split", "lint", "round", "bundle", "next")

# WHY THERE ARE FORTY-EIGHT, answered by grouping rather than by deleting.
#
# The question "why do we have 48 commands" has a real answer -- they are eight
# stages plus six small families of tools -- and a flat braced list of 48 hides
# it completely. Measured 2026-09-14: 14 of the 48 are mentioned by NO
# document, test, script or example. The problem was never that they exist, it
# was that most of them are invisible.
#
# Deleting was considered and refused for now: a verb removed breaks every
# script already written against it, and a reference count cannot tell a tool
# somebody relies on from one nobody has needed yet. Grouping costs nothing and
# breaks nothing.
#
# THE TEST IS THE POINT. Every verb must appear in exactly one group below, so
# adding a forty-ninth forces a decision about where it belongs -- which is the
# only thing that stops 48 becoming 60.
VERB_GROUPS: dict[str, tuple[str, ...]] = {
    "the eight that complete one measurement": CORE_VERBS,
    "after you have a result": ("classes", "attribute", "history", "bisect",
                                "triage", "worklist", "climb"),
    "build a question set": ("log", "mine", "review", "draft-context",
                             "draft-rules", "rewrite-questions", "audit-golds",
                             "curate-examples", "build-examples",
                             "mine-context", "context-write"),
    "ask and score by hand": ("generate", "score", "ablate", "arms", "leak",
                              "import"),
    "write context into a catalog and read it back": ("catalog-push", "catalog-pull", "catalog-list",
                          "catalog-route", "catalog-rollback", "spec-push",
                          "semantic", "eval-room"),
    "look at the warehouse": ("schema", "profile", "sentinel"),
    "retrieval, when context is chosen per question": (
        "routing-gate", "score-routing"),
    "keep a record": ("index", "pin", "share"),
}


def grouped_verbs(choices) -> str:
    """The verb list as families, for `--help-all`.

    A verb in no group is printed under "not yet grouped" rather than dropped,
    because a help page that silently omits a command is worse than an untidy
    one. The test asserts that list is empty.
    """
    seen = set()
    lines = []
    for title, verbs in VERB_GROUPS.items():
        here = [v for v in verbs if v in choices]
        if not here:
            continue
        seen.update(here)
        lines.append(f"  {title}:")
        for i in range(0, len(here), 4):
            lines.append("    " + "  ".join(f"{v:<18}" for v in here[i:i + 4]).rstrip())
    missing = sorted(set(choices) - seen)
    if missing:
        lines.append("  not yet grouped:")
        lines.append("    " + "  ".join(missing))
    return "\n".join(lines)


HELP_EPILOG = """\
the eight above complete one measurement. after you have a result:
  classes     what each KIND of context paid for, by leaving one out
  attribute   which written line moved which question
  history     what was fixed and what broke, across every round

every verb, all {total} of them:   contextlift --help-all
"""


def _build_parser(show_all: bool = False) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="contextlift",
        description="Measure whether curated context makes a data agent more "
        "accurate, and find what to fix next. Stages run in the order shown "
        "shown by `contextlift next`.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--help-all", action="store_true",
        help="every verb, not just the eight that complete one measurement")
    parser.add_argument(
        "--workspace", default=os.environ.get("CONTEXTLIFT_WORKSPACE"),
        metavar="DIR",
        help="record this run (argv, input hashes, output copies, stdout) "
             "under DIR/runs/ and rebuild DIR/index.json; also read from "
             "$CONTEXTLIFT_WORKSPACE. The database is hashed, not copied.")
    # `metavar` matters as much as the hiding does. Without it argparse prints
    # every choice in the usage line as one braced list, so the usage read
    # `{demo,next,eval-room,sentinel,doctor,index,...}` wrapped over two lines
    # before the description even started.
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # Hiding is done here, once, rather than at forty-eight call sites. A
    # suppressed verb is left out of the LISTING and stays fully callable,
    # with its own --help; nothing is removed and nothing is renamed.
    _add_parser = sub.add_parser
    if not show_all:
        def _add_parser(name, **kw):  # noqa: F811
            if name not in CORE_VERBS:
                # REMOVED, not set to argparse.SUPPRESS. argparse honours
                # SUPPRESS on a top-level action but not on a subcommand
                # choice: `add_parser` does `if 'help' in kwargs` and builds
                # the pseudo-action either way, so SUPPRESS printed as the
                # literal string `==SUPPRESS==` beside forty verbs.
                kw.pop("help", None)
            return sub.__class__.add_parser(sub, name, **kw)
        sub.add_parser = _add_parser

    demo_p = sub.add_parser(
        "demo",
        help="what this tool does, in under a minute, with no key and no cloud")
    demo_p.set_defaults(func=_cmd_demo)

    next_p = sub.add_parser(
        "next", help="where the loop stands in --workspace, and the one command to run next")
    next_p.add_argument("--json", action="store_true", help="the whole state as JSON")
    next_p.set_defaults(func=_cmd_next)

    room_p = sub.add_parser(
        "eval-room",
        help="the statements that stand up the evaluation project: a view per table, "
             "undocumented on purpose, so a bare arm really is bare")
    room_p.add_argument("--eval-project", required=True,
                        help="the project the views and the measurement writes live in")
    room_p.add_argument("--dataset", default="room",
                        help="the dataset inside it (default: room)")
    room_p.add_argument("--tables", required=True, nargs="+", metavar="PROJECT.DATASET.TABLE",
                        help="the production tables to stand views over")
    room_p.add_argument("--location", default="US",
                        help="the BigQuery location of the evaluation dataset (default: US)")
    room_p.add_argument("--harness-project", default=None,
                        help="the project the gold gate is billed to. Leave it out and "
                             "the plan says why that matters")
    room_p.set_defaults(func=_cmd_evalroom)

    sentinel_p = sub.add_parser(
        "sentinel",
        help="does the agent read the catalog? writes a fact no data carries, asks a "
             "question that needs it, and says where the fact turned up")
    sentinel_p.add_argument("--table", required=True, metavar="PROJECT.DATASET.TABLE",
                            help="the table to probe, in the evaluation project")
    sentinel_p.add_argument("--column", default="amount",
                            help="the column whose BigQuery description carries one "
                                 "of the markers (default: amount)")
    sentinel_p.add_argument("--join-partner", default=None, metavar="PROJECT.DATASET.TABLE",
                            help="a table the probed one is often queried with. Without "
                                 "it the join channel is left out rather than shipped as "
                                 "a check that can never fire")
    sentinel_p.add_argument("--run", action="store_true",
                            help="ask the questions. Without this the command only "
                                 "prints what it would write and ask, and costs nothing")
    sentinel_p.add_argument("--project", default=None,
                            help="the project the chat calls are billed to")
    sentinel_p.add_argument("--location", default="global",
                            help="the API location (default: global)")
    sentinel_p.add_argument("--model", default="LATEST_GA_MODEL",
                            help="the model alias; the preview alias moves under you")
    sentinel_p.add_argument("--max-billed-bytes", type=int, default=None,
                            help="a ceiling on the bytes any query the agent runs may scan")
    sentinel_p.add_argument("--budget", type=float, default=None, metavar="USD",
                            help="the ceiling this probe may spend; --yes needs it, "
                                 "because a yes with no ceiling is a blank cheque")
    sentinel_p.add_argument("--yes", action="store_true",
                         help="say yes in writing instead of typing RUN at a "
                              "terminal; needs --budget, and is recorded with "
                              "the argv that carried it")
    sentinel_p.set_defaults(func=_cmd_sentinel)

    doctor_p = sub.add_parser(
        "doctor",
        help="what a run needs before it spends anything, and what a refusal means")
    doctor_p.add_argument("--project", default=None,
                          help="the billing project a run would use, checked against "
                               "CONTEXTLIFT_FORBIDDEN_PROJECTS")
    doctor_p.add_argument("--needs", nargs="+", default=[], metavar="EXTRA",
                          help="the optional extras this run will use (bigquery, gemini, "
                               "catalog, ca, lookml); each is checked for import, and a "
                               "credential is checked when any is named")
    doctor_p.add_argument("--explain", default=None, metavar="MESSAGE",
                          help="paste a permission-denied message; prints the role that "
                               "grants each permission it names, and where to grant it")
    doctor_p.set_defaults(func=_cmd_doctor)

    index_p = sub.add_parser(
        "index", help="rebuild index.json from the run folders in --workspace")
    index_p.add_argument("--show", action="store_true",
                         help="print the arms table, the pins, and the holdout state")
    index_p.set_defaults(func=_cmd_index)

    pin_p = sub.add_parser(
        "pin", help="pin the baseline or candidate arm in --workspace")
    pin_p.add_argument("slot", choices=["baseline", "candidate"])
    pin_p.add_argument("id", help="a run id or an arm id from index.json")
    pin_p.set_defaults(func=_cmd_pin)

    bundle_p = sub.add_parser(
        "bundle", help="write verdict.html for the pinned comparison: one static "
                       "file, no script, opens from file://")
    bundle_p.add_argument("--out", default=None, help="default: DIR/verdict.html")
    bundle_p.add_argument("--baseline", default=None, help="arm id; default: the pin")
    bundle_p.add_argument("--candidate", default=None, help="arm id; default: the pin")
    bundle_p.add_argument("--no-rows", action="store_true",
                          help="leave the row previews (the first rows of both result sets "
                               "per wrong answer) out of verdict.html and verdict.json")
    bundle_p.set_defaults(func=_cmd_bundle)

    write_p = sub.add_parser(
        "context-write", help="replace context/rules.md with a reviewed draft, "
                              "as a recorded run (the manifest keeps both)")
    write_p.add_argument("--source-file", required=True, metavar="DRAFT",
                         help="the reviewed draft to install")
    write_p.add_argument("--to", default="context/rules.md",
                         help="the working rules file (default: context/rules.md)")
    write_p.add_argument("--source", default="human",
                         help="who wrote the draft: human, or draft:<model>")
    write_p.add_argument("--note", default="", help="one line on why; kept in the manifest")
    write_p.add_argument("--previous-out", default=None, metavar="FILE",
                         help="where the replaced bytes go (default: the run directory)")
    write_p.add_argument("--diff-out", default=None, metavar="FILE",
                         help="unified diff previous -> new (default: the run directory)")
    write_p.add_argument("--version-out", default=None, metavar="FILE",
                         help="version.json with the sequential number (default: the run directory)")

    profile_p = sub.add_parser(
        "profile", help="write a value dictionary: the exact values each category "
                        "column holds, for the model to use instead of guessing")
    profile_p.add_argument("--db", required=True, help=DB_HELP)
    profile_p.add_argument("--out", default=None, help="context file to write (default: values.md)")
    profile_p.add_argument("--max-values", type=int, default=20,
                           help="skip columns with more distinct values than this")
    _add_dataset_scope_flags(profile_p)
    profile_p.add_argument("--timeout", type=float, default=60.0)
    _add_warehouse_flags(profile_p, estimate=False)
    profile_p.set_defaults(func=_cmd_profile)

    schema_p = sub.add_parser(
        "schema", help="write the schema text the model sees: CREATE TABLE per table "
                       "(BigQuery: from INFORMATION_SCHEMA, descriptions as comments)")
    schema_p.add_argument("--db", required=True, help=DB_HELP)
    schema_p.add_argument("--out", default=None, help="text file to write (default: schema.txt)")
    schema_p.add_argument("--tables", default=None, metavar="A,B",
                          help="only these tables (comma-separated): `orders` in every dataset "
                               "read, or `dataset.orders` in that one")
    _add_dataset_scope_flags(schema_p)
    schema_p.add_argument("--timeout", type=float, default=60.0)
    _add_warehouse_flags(schema_p, estimate=False)
    schema_p.set_defaults(func=_cmd_schema)

    draft_p = sub.add_parser(
        "draft-context", help="draft one document per table from the schema, into a "
                              "directory a routing context source reads; no model, no rows")
    draft_p.add_argument("--db", required=True, help=DB_HELP)
    draft_p.add_argument("--out", required=True, metavar="DIR",
                         help="directory for <dataset>.<table>.md, one per table")
    draft_p.add_argument("--tables", default=None, metavar="A,B",
                         help="only these tables (comma-separated): `orders` in every dataset "
                              "read, or `dataset.orders` in that one")
    _add_dataset_scope_flags(draft_p)
    draft_p.add_argument("--name-style", choices=("dot", "underscore"), default="dot",
                         help="dataset.table.md (default) or dataset__table.md, the entry "
                              "id shape a catalog push may need")
    draft_p.add_argument("--timeout", type=float, default=60.0)
    _add_warehouse_flags(draft_p, estimate=False)
    draft_p.set_defaults(func=_cmd_draft_context)

    pull_p = sub.add_parser(
        "catalog-pull", help="read each table's document out of a live Knowledge Catalog "
                             "into a directory (free; needs the [catalog] extra)")
    pull_p.add_argument("--project", required=True, help="project id or number that owns the catalog")
    pull_p.add_argument("--entry-group", default="@bigquery",
                        help="the entry group the tables' entries live in (default @bigquery, BigQuery's own "
                             "entries; a custom group takes its id shape from --entry-id)")
    pull_p.add_argument("--entry-id", default=None, metavar="TEMPLATE",
                        help="the entry id inside the group as a template of {dataset}, {table} and {project}; "
                             "default for @bigquery is BigQuery's own shape; with it nothing is searched "
                             "(the custom groups measured: '{dataset}__{table}')")
    pull_p.add_argument("--location", required=True,
                        help="the dataset's location, e.g. us; one pull per region on a "
                             "multi-region estate")
    pull_p.add_argument("--tables", default=None, metavar="A.B,C.D",
                        help="dataset.table names to pull (comma-separated)")
    pull_p.add_argument("--tables-file", default=None, metavar="FILE",
                        help="dataset.table names to pull, one per line, #-comments skipped: "
                             "what catalog-list writes")
    pull_p.add_argument("--out", required=True, metavar="DIR",
                        help="directory for one file per table: the served bundle, YAML text, "
                             "kept byte-exact (the arm; not a file to edit)")
    pull_p.add_argument("--pages", default=None, metavar="DIR",
                        help="also write each table's stored overview, push stamp removed, as "
                             "<dataset>.<table>.md: the file a person edits, reviews, and "
                             "catalog-push --docs DIR sends back")
    pull_p.add_argument("--entry-pattern", default="name:{table} type=table",
                        help="the search term for a table, used only when no entry id shape is known "
                             "(a custom group without --entry-id); on the auto group "
                             "name:{dataset}.{table} returns nothing, use {table}")
    pull_p.add_argument("--known-good-table", default=None,
                        help="a table that must resolve, to prove the search is scoped "
                             "(search path only; default: the first of --tables)")
    pull_p.add_argument("--budget", type=int, default=60000,
                        help="context_budget passed to the lookup (default 60000, the shared read "
                             "budget); on one estate a page longer than the budget came back as a "
                             "few hundred characters of its opening with no error, so the pull "
                             "compares what is served against what the entry stores and says so")
    pull_p.add_argument("--name-style", choices=("dot", "underscore"), default="dot")
    pull_p.set_defaults(func=_cmd_catalog_pull)

    route_p = sub.add_parser(
        "catalog-route", help="route each question through Knowledge Catalog search and "
                              "write the per-question context generate reads (free)")
    route_p.add_argument("--project", required=True, help="project id or number that owns the catalog")
    route_p.add_argument("--entry-group", default=None,
                        help="keep only tables whose entry lives in this group (e.g. @bigquery, or a custom "
                             "group); the dropped ones are counted and named. The filter is on the entry "
                             "names the search returns: a group predicate inside the query answered zero "
                             "results in keyword mode on the estate measured. Without it every group counts")
    route_p.add_argument("--location", required=True,
                         help="the dataset's location, e.g. us; one route per region on a "
                              "multi-region estate")
    route_p.add_argument("--questions", required=True, help="CSV with id, question[, gold_sql]")
    route_p.add_argument("--docs", required=True, metavar="DIR",
                         help="documents from catalog-pull or draft-context, one per table")
    route_p.add_argument("--top", type=int, default=3, help="tables per question (default 3)")
    route_p.add_argument("--within", default=None, metavar="DATASET", nargs="+", action="extend",
                         help="keep only tables of these datasets; list them after one flag "
                              "or repeat the flag")
    route_p.add_argument("--catalog", default=None, metavar="FILE",
                         help="the table list the routing is graded against (catalog-list's file): "
                              "a routed table outside it is dropped before the top-N cut, and the "
                              "dropped ones are named")
    route_p.add_argument("--search-pattern", default="{question} type=table", metavar="PATTERN",
                         help="what is sent to the catalog's search for each question; must "
                              "contain {question} (default: '{question} type=table', which keeps "
                              "glossary terms out of BigQuery's own entries)")
    route_p.add_argument("--routes-out", default=None, help="JSON, id -> routed tables (default: routes.json)")
    route_p.add_argument("--context-out", default=None,
                         help="JSON, id -> the routed tables' documents (default: context_per_question.json)")
    route_p.add_argument("--routing-out", default=None,
                         help="id, question, gold_table (primary table of gold_sql), pred_table "
                              "(default: routing.csv)")
    route_p.set_defaults(func=_cmd_catalog_route)

    review_p = sub.add_parser(
        "review", help="the human review between mine and gate: prepare the sheet and "
                       "its document, let a model draft it (--draft), or finish it into "
                       "golds (refuses while any row is undecided)")
    review_p.add_argument("--candidates", default=None, help="candidates.csv from mine: prepare")
    review_p.add_argument("--sheet", default=None, help="the review.csv: finish, or --draft")
    review_p.add_argument("--out", default=None,
                          help="directory for the sheet (prepare) or the golds CSV (finish); "
                               "not used with --draft")
    review_p.add_argument(
        "--harvest", action="store_true",
        help="turn the reviewer's `comment` cells into candidate rule lines "
             "and print them as a DRAFT a person approves. Never written to "
             "the spec on its own: this is an LLM stage and gets the same "
             "treatment as every other one. Pass --golds and every drafted "
             "line is checked by the leak gate first, because a comment on a "
             "scored question can quote its answer")
    review_p.add_argument(
        "--golds", default=None, metavar="FILE",
        help="the scored questions, so --harvest can refuse a drafted line "
             "that quotes one")
    review_p.add_argument(
        "--agreement", action="store_true",
        help="print how often the person and the model agreed, on rows where "
             "both decided. On one estate two models kept 25.4%% and 53.1%% of "
             "the same candidates; only a person correcting rows settles which")
    review_p.add_argument("--draft", action="store_true",
                          help="a model decides the empty keep rows of --sheet, accept or reject "
                               "with a reason, never the SQL or the question; the sheet is "
                               "rewritten in place and every verdict carries a caveat until a "
                               "person re-reviews")
    review_p.add_argument(
        "--question-model", default=None, metavar="NAME",
        help="the model that DRAFTED the questions. --draft refuses when it "
             "is the same family as --model, because a family checking its "
             "own work shares its blind spots")
    review_p.add_argument(
        "--allow-self-review", action="store_true",
        help="draft anyway when the reviewer may not be independent. The "
             "refusal exists because the drafter's mistake is the one the "
             "reviewer is least likely to see")
    review_p.add_argument("--batch", type=int, default=10, help="rows per model call (--draft)")
    review_p.add_argument("--backend", choices=["gemini", "echo"], default="gemini")
    review_p.add_argument("--echo-response", default="")
    review_p.add_argument("--model", default="gemini-2.5-flash",
                          help="the reviewer; pick a different family from the agent under test")
    review_p.add_argument("--project", default=os.environ.get("CONTEXTLIFT_PROJECT"),
                          help="Vertex project (default: $CONTEXTLIFT_PROJECT)")
    review_p.add_argument("--estimate-only", action="store_true",
                          help="count the prompts, print the cost estimate, make no model call")
    review_p.add_argument("--yes", action="store_true",
                          help="say yes in writing instead of typing RUN at a "
                               "terminal; needs --budget, and is recorded in "
                               "the run manifest with the argv that carried it")
    review_p.add_argument("--budget", type=float, default=None, metavar="USD",
                          help="refuse to start when the estimate is above this; stop at it")
    review_p.add_argument("--input-price", type=float, default=None, metavar="USD_PER_MILLION",
                          help="override the input token price for --model")
    review_p.add_argument("--output-price", type=float, default=None, metavar="USD_PER_MILLION",
                          help="override the output token price for --model")
    review_p.set_defaults(func=_cmd_review)

    rules_p = sub.add_parser(
        "draft-rules", help="draft the business rules one failure bucket asks for, gold beside "
                            "prediction, each rule citing its questions (one model call)")
    rules_p.add_argument("--report", required=True, help="a saved score report")
    rules_p.add_argument("--predictions", required=True, help="the predictions CSV the report scored")
    rules_p.add_argument("--bucket", default=None, help="triage bucket (default: the largest)")
    rules_p.add_argument("--ids", default=None, metavar="FILE",
                         help="one question id per line, e.g. curate_ids.txt; the holdout never goes in")
    rules_p.add_argument("--limit", type=int, default=12, help="questions shown to the model")
    rules_p.add_argument("--max-rules", type=int, default=8)
    rules_p.add_argument("--out", default="rules_draft.md")
    rules_p.add_argument("--backend", choices=["gemini", "echo"], default="gemini")
    rules_p.add_argument("--echo-response", default="")
    rules_p.add_argument("--model", default="gemini-2.5-flash")
    rules_p.add_argument("--project", default=os.environ.get("CONTEXTLIFT_PROJECT"),
                         help="Vertex project (default: $CONTEXTLIFT_PROJECT)")
    rules_p.add_argument("--estimate-only", action="store_true",
                         help="count the one prompt, print the cost estimate, make no model call")
    rules_p.add_argument("--budget", type=float, default=None, metavar="USD",
                         help="refuse to start when the estimate is above this")
    rules_p.add_argument("--input-price", type=float, default=None, metavar="USD_PER_MILLION",
                         help="override the input token price for --model")
    rules_p.add_argument("--output-price", type=float, default=None, metavar="USD_PER_MILLION",
                         help="override the output token price for --model")
    rules_p.add_argument("--yes", action="store_true",
                         help="say yes in writing instead of typing RUN at a "
                              "terminal; needs --budget, and is recorded with "
                              "the argv that carried it")
    rules_p.set_defaults(func=_cmd_draft_rules)

    list_p = sub.add_parser(
        "catalog-list", help="the tables a Knowledge Catalog entry group holds, one dataset.table per "
                             "line, by the parentEntry rule (free)")
    list_p.add_argument("--project", required=True, help="project id or number that owns the catalog")
    list_p.add_argument("--location", required=True,
                        help="the dataset's location, e.g. us; one list per region on a "
                             "multi-region estate")
    list_p.add_argument("--entry-group", default="@bigquery",
                        help="the entry group to list (default @bigquery, BigQuery's own entries)")
    list_p.add_argument("--datasets", default=None, metavar="A,B", help="keep only these datasets")
    list_p.add_argument("--out", default=None, help="the catalog file to write (default catalog.txt)")
    list_p.set_defaults(func=_cmd_catalog_list)

    push_p = sub.add_parser(
        "catalog-push", help="reviewed pages, rule lines and worked examples into Knowledge Catalog; "
                             "every write is read back through the call a reader uses (free; never deletes)")
    push_p.add_argument("--project", required=True, help="project id or number that owns the catalog")
    push_p.add_argument("--entry-group", default="@bigquery",
                        help="the entry group the tables' entries live in (default @bigquery, BigQuery's own "
                             "entries; a custom group takes its id shape from --entry-id)")
    push_p.add_argument("--entry-id", default=None, metavar="TEMPLATE",
                        help="the entry id inside the group as a template of {dataset}, {table} and {project}; "
                             "default for @bigquery is BigQuery's own shape; with it nothing is searched "
                             "(the custom groups measured: '{dataset}__{table}')")
    push_p.add_argument("--location", required=True,
                        help="the dataset's location, e.g. us; one push per region on a "
                             "multi-region estate")
    push_p.add_argument("--docs", required=True, metavar="DIR",
                        help="<dataset>.<table>.md or <dataset>__<table>.md pages (draft-context, catalog-pull, or hand-written)")
    push_p.add_argument("--rules", default=None, metavar="FILE",
                        help='rule lines; those shaped - "phrase" -> meaning become glossary terms linked to the table they name')
    push_p.add_argument("--examples", default=None, metavar="FILE",
                        help="the worked-example bank curate-examples wrote (the leak-checked bank, not the pool)")
    push_p.add_argument("--tables", default=None, metavar="A.B,C.D", help="push only these tables' pages")
    push_p.add_argument("--glossary", default="contextlift", help="glossary id for the terms (default contextlift)")
    push_p.add_argument("--query-group", default="contextlift-verified-queries",
                        help="entry group for verified-query entries when the queries aspect is not writable")
    push_p.add_argument("--holdout", default=None, metavar="FILE",
                        help="reserved questions (id, question, gold_sql) that must not be in what is pushed; "
                             "default: the workspace's newest split")
    push_p.add_argument("--entry-pattern", default="name:{table} type=table",
                        help="the search term for a table, used only when no entry id shape is known "
                             "(a custom group without --entry-id); on the auto group "
                             "name:{dataset}.{table} returns nothing, use {table}")
    push_p.add_argument("--budget", type=int, default=60000,
                        help="the read budget a catalog lookup truncates at; pages over it are named (default 60000)")
    push_p.add_argument("--attempts", type=int, default=4, help="reads before a write counts as failed (default 4)")
    push_p.add_argument("--dry-run", action="store_true", help="print the plan; write nothing (previous bytes are still kept)")
    push_p.add_argument("--verify-only", action="store_true",
                        help="write nothing; re-read every page as a reader sees it now (after an index lag)")
    push_p.add_argument("--allow-draft", action="store_true",
                        help="push pages that still carry draft-context's not-reviewed marker")
    push_p.add_argument("--manifest-out", default=None, metavar="FILE",
                        help="the push manifest, one record per entry (default: push_manifest.json in the run)")
    push_p.add_argument("--previous-out", default=None, metavar="DIR",
                        help="where replaced overviews that contextlift did not write are kept (default: previous/ in the run)")
    push_p.set_defaults(func=_cmd_catalog_push)

    log_p = sub.add_parser(
        "log", help="pull the query history mine reads from BigQuery's "
                    "INFORMATION_SCHEMA.JOBS_BY_PROJECT; dry-runs and prices it first")
    log_p.add_argument("--db", required=True,
                       help="bq://PROJECT: the project whose job history to read; a job is "
                            "listed where it RAN and was billed, not where its tables live "
                            "(SQLite keeps no log)")
    log_p.add_argument("--days", type=int, default=90,
                       help="look back this many days; raise --limit with it, or a longer "
                            "window loses human queries behind dashboard traffic")
    log_p.add_argument("--region", default="us",
                       help="the region qualifier of the JOBS view, e.g. us, eu, us-central1")
    log_p.add_argument("--limit", type=int, default=400,
                       help="keep the top N query shapes by times_seen, then distinct_users. "
                            "A shape is the text with string and number literals masked, "
                            "whitespace collapsed and case folded: coarser than mine's "
                            "fingerprint, which also strips comments. The ranking is a "
                            "heuristic: one person's ad-hoc query is often the question "
                            "wanted, and many-user shapes are rare where dashboards run as "
                            "service accounts")
    log_p.add_argument("--dataset", default=None,
                       help="keep only jobs that touched this dataset (by referenced "
                            "table or by name in the query text); defaults to the "
                            "dataset in --db when one is given")
    log_p.add_argument("--datasets", default=None, metavar="A,B",
                       help="keep jobs that touched any of these datasets, in one query "
                            "(added to --dataset)")
    log_p.add_argument("--demand", action="store_true",
                       help="instead of query shapes, one row per dataset the window's jobs "
                            "referenced (every dataset, or only --datasets A,B): jobs, "
                            "distinct_shapes, distinct_accounts, bytes_billed_total, "
                            "jobs_per_account_day, top_account_share. Where the demand is, "
                            "before a scope is chosen. Most accounts first, then most jobs: "
                            "where pipelines run under personal accounts, distinct accounts "
                            "separate demand from repetition and job counts do not. Accounts "
                            "are counted, never stored. share is of every job in the window, "
                            "listed or not, from a total in the same statement. A job counts "
                            "once per dataset it touched, however many of that dataset's "
                            "tables it read, so jobs sums above the window's job count while "
                            "the window totals count each job once. --limit and "
                            "--hash-accounts do not apply")
    log_p.add_argument("--include-service-accounts", action="store_true",
                       help="keep the jobs run by service accounts (an address ending "
                            "gserviceaccount.com). They are dropped by default: they run on a "
                            "timer, so they hold most of a window (one estate: 7 of them ran "
                            "169,789 of 184,301 jobs in 30 days, and 2,592 of the 3,000 shapes "
                            "a --limit 3000 pull returned), and the cut is theirs before a "
                            "person's question reaches it. --demand counts them either way")
    log_p.add_argument("--exclude-accounts", default="", metavar="A,B",
                       help="drop these accounts' jobs in SQL, before any hashing, so an "
                            "engineer's own eval traffic never reaches the CSV. Plain "
                            "addresses, matched lower-cased; mine --exclude-accounts still "
                            "works on a pulled log, hashed or not")
    log_p.add_argument("--out", default=None,
                       help="CSV to write (default: query_log.csv, or demand.csv with --demand)")
    log_p.add_argument(
        "--only-accounts", default="", metavar="A,B",
        help="keep ONLY these accounts' jobs, the inverse of "
             "--exclude-accounts. A team's own queries make a gold set about "
             "that team's questions, which is the only way to ask whether "
             "the sales rules helped the sales team rather than whether they "
             "helped on average. The blanket service-account exclusion steps "
             "aside when this is given, because naming one service account "
             "and getting nothing back is the kind of silence nobody debugs. "
             "Plain addresses, matched lower-cased")
    log_p.add_argument(
        "--labels", default="", metavar="K=V,K2",
        help="keep only jobs carrying these BigQuery job labels. `team=sales` "
             "needs that key and value; a bare `ca-bq-job` needs the key "
             "whatever its value. dbt, Looker and the Conversational "
             "Analytics API all label their own jobs, so a team that labels "
             "its scheduled work can select it here without naming a single "
             "person, which is also the version that survives somebody "
             "leaving")
    log_p.add_argument(
        "--min-tables", type=int, default=1, metavar="N",
        help="keep only shapes naming at least N different tables. `mine` has "
             "this flag too and having it only there is one stage too late: "
             "this command keeps the top --limit shapes BY TRAFFIC, and a "
             "dashboard runs hundreds of times for one viewer. On one estate "
             "29,684 of 1,205,904 queries joined two tables and aggregated, "
             "2.5%%, so a top-2,000 pull held about fifty of them and mine's "
             "own filter then cut 446 candidates down to 2. Filtering here "
             "makes the same 2,000 rows 2,000 usable ones and costs nothing, "
             "because the view is scanned either way")
    log_p.add_argument(
        "--require-analytical", action="store_true",
        help="keep only shapes that group or aggregate. A single-value lookup "
             "(`SELECT ANY_VALUE(latest_week_text) ... WHERE timeframe = ...`) "
             "is a dashboard tile, and no written business rule can change "
             "the answer to it: 25 of 75 gate-clean candidates on one estate "
             "were that, and they dilute a lift rather than measure it")
    log_p.add_argument(
        "--keep-machine-sql", action="store_true",
        help="keep shapes carrying a tool's own JSON header (dbt, Looker, "
             "GitHub Actions). They are model builds and deployment checks, "
             "not questions, and they run under a person's credentials so "
             "--exclude-accounts never sees them: 80,904 of 1,205,904 queries "
             "on one estate, and one of them passed every gate clean and would "
             "have become a gold")
    log_p.add_argument("--timeout", type=float, default=300.0)
    log_p.add_argument(
        "--max-bytes", type=_byte_size, default=DEFAULT_MAX_BYTES, metavar="SIZE",
        help="the cap BigQuery puts on the job's billed bytes (default 1 GB, written "
             "with a unit or as plain bytes: 50GB, 500MB, 50000000000); the job "
             "fails free above it, and that is when to raise this. The dry run on the "
             "job-history view overstates (10x on one estate), so it is printed as a "
             "ceiling and nothing is refused on it; the view is billed by bytes like "
             "any table")
    log_p.add_argument("--estimate-only", action="store_true",
                       help="dry-run, print the ceiling and its price, pull nothing")
    log_p.add_argument("--hash-accounts", action="store_true",
                       help="store TO_HEX(MD5(LOWER(user_email))) in ran_by instead of the "
                            "address, one hash per account: the MD5 of the LOWER-CASED "
                            "address, written as lower-case hex. Hashing an address as typed "
                            "matches nothing and says nothing, so give mine "
                            "--exclude-accounts the plain address and let it hash")
    log_p.set_defaults(func=_cmd_log)
    write_p.add_argument(
        "--add", action="store_true",
        help="keep what is already in --to and add this draft's new rule lines under it, "
             "instead of replacing the file. A rule line already there is not added twice. "
             "This is how round 2 keeps round 1: draft-rules writes one failure bucket at a "
             "time, so without it the only way to hold both rounds is editing the file by hand")
    write_p.add_argument(
        "--allow-no-rules", action="store_true",
        help="install a file with no '- ' rule lines; without this a draft that parses "
             "to zero rules is refused as a probable wrong file")
    write_p.set_defaults(func=_cmd_context_write)



    import_p = sub.add_parser(
        "import", help="record predictions made elsewhere as an arm in --workspace")
    import_p.add_argument(
        "--predictions", required=True,
        help="CSV with columns id, question, gold_sql, pred_sql")
    import_p.add_argument(
        "--context", default=None,
        help="the context file those predictions were made with, if any")
    import_p.add_argument(
        "--examples", default=None,
        help="the example bank those predictions were made with, if any")
    import_p.add_argument(
        "--out", default=None,
        help="where the copy lands (default: the run directory)")
    import_p.set_defaults(func=_cmd_import)

    score = sub.add_parser("score", help="grade predictions against gold SQL")
    score.add_argument("--db", required=True, help=DB_HELP)
    score.add_argument(
        "--predictions",
        required=True,
        help="CSV with columns id, question, gold_sql, pred_sql",
    )
    score.add_argument("--row-cap", type=int, default=DEFAULT_ROW_CAP,
                       help=f"max rows fetched per query (default "
                            f"{DEFAULT_ROW_CAP:,}); a capped result is excluded "
                            "from the score, never compared. 0 for no cap")
    score.add_argument(
        "--extra-columns", choices=["reject", "tolerate"], default="reject",
        help="tolerate: a prediction whose columns are a superset of the gold's "
             "counts as correct (verdict MATCH_EXTRA_COLUMNS, reported beside the "
             "strict count). Default reject, comparable with Spider and BIRD.")
    score.add_argument(
        "--confidence", type=float, default=0.95, help="CI confidence level"
    )
    score.add_argument(
        "--judge-gemini",
        action="store_true",
        help="run the LLM judge on disagreements (needs the [gemini] extra)",
    )
    score.add_argument(
        "--checkpoint", metavar="FILE",
        help="write each question's result as it is scored, and on a restart skip "
             "the ones already in the file. A long warehouse run that dies - a "
             "dropped shell, a stalled connection, a killed process - resumes "
             "instead of paying for the same queries twice. Under --workspace "
             "defaults into the run directory; pass that path to resume.")
    score.add_argument("--judge-model", default="gemini-2.5-pro")
    score.add_argument("--judge-project", default=None)
    _add_run_flags(score)
    score.add_argument("--yes", action="store_true",
                       help="say yes in writing instead of typing RUN at a "
                            "terminal; needs --budget, and is recorded with the "
                            "argv that carried it")
    score.set_defaults(func=_cmd_score)

    gate_p = sub.add_parser(
        "gate",
        help="audit a gold set for defects: a gold that does not run, returns nothing, names "
             "a project --db does not read, or hands back the whole row (a SELECT * gold is "
             "someone browsing a table, not a question, and no answer matches it unless the "
             "agent guesses the same column list)")
    gate_p.add_argument("--db", required=True, help=DB_HELP)
    gate_p.add_argument(
        "--write-manifest", metavar="FILE",
        help="record the shape of every table the golds read, so a later "
             "`--since` can say what moved. Metadata only, close to free, and "
             "it hashes a view's DEFINITION as well as its columns because a "
             "view replaced with the same columns is the change nothing else "
             "catches")
    gate_p.add_argument(
        "--since", metavar="FILE",
        help="a manifest from an earlier `--write-manifest`. Reports what "
             "moved under the gold set and re-gates ONLY the golds whose "
             "tables changed. `gate` executes gold SQL, which costs money, so "
             "re-running the whole set to find the two that drifted is the "
             "expensive way to learn a cheap fact")
    gate_p.add_argument(
        "--spec", metavar="DIR",
        help="the spec repo the agent's context will come from. Only the "
             "MISSING_LITERAL warning uses it, and it makes that warning "
             "accurate instead of noisy: a value the written context "
             "documents is answerable, so it is not a defect. On the "
             "committed thelook set the warning fired on 40 golds without "
             "this and 2 with it, and those 2 were real defects that have "
             "since been fixed")
    gate_p.add_argument(
        "--data-projects", default=None, metavar="A,B",
        help="every project the agent under test can read, beyond --db's. A "
             "gold naming one of these is not cross-project. A real estate "
             "spans several and --db names exactly one, so without this a "
             "gold set spanning production and pre-production is flagged "
             "whichever way --db is pointed")
    gate_p.add_argument(
        "--golds", required=True, help="CSV with columns id, gold_sql"
    )
    gate_p.add_argument("--row-cap", type=int, default=DEFAULT_ROW_CAP,
                       help=f"max rows fetched per query (default "
                            f"{DEFAULT_ROW_CAP:,}); a capped result is excluded "
                            "from the score, never compared. 0 for no cap")
    gate_p.add_argument(
        "--check-stability",
        action="store_true",
        help="run each gold twice to catch non-deterministic results (2x cost)",
    )
    gate_p.add_argument(
        "--clean-out", default=None,
        help="write the golds that passed to this CSV, ready for split/score")
    gate_p.add_argument(
        "--flagged-out", default=None,
        help="write the flagged golds to this CSV, for the human fixing them")
    _add_run_flags(gate_p)
    gate_p.set_defaults(func=_cmd_gate)

    gate_p.add_argument(
        "--exit-zero-on-defects", action="store_true",
        help="exit 0 even when golds are flagged. The default is non-zero so a CI step can "
             "guard on the answer key; use this when you are chaining `gate && next` and want "
             "the report rather than the exit code")
    routing_gate_p = sub.add_parser(
        "routing-gate",
        help="audit a routing answer key before spending on it: concentration on one gold, "
             "golds with one question, golds not in the catalog, empty golds, duplicate ids")
    routing_gate_p.add_argument("--golds", required=True,
                                help="CSV with at least id and gold_table (a predictions file works)")
    routing_gate_p.add_argument("--catalog", default=None,
                                help="one valid dataset.table per line, to count golds it lacks")
    routing_gate_p.add_argument("--json", action="store_true", help="the report as JSON")
    routing_gate_p.set_defaults(func=_cmd_routing_gate)

    routing = sub.add_parser(
        "score-routing", help="grade table-routing predictions"
    )
    routing.add_argument(
        "--predictions",
        required=True,
        help="CSV with columns id, question, gold_table, pred_table",
    )
    routing.add_argument(
        "--catalog",
        default=None,
        help="file with one valid dataset.table per line; snaps near-miss "
        "predictions to the closest entry and reports how many",
    )
    routing.add_argument(
        "--confidence", type=float, default=0.95, help="CI confidence level"
    )
    routing.add_argument(
        "--multi", action="store_true",
        help="gold_table and pred_table are |-separated table SETS "
             "(enterprise questions route to 2-7 tables, not one)")
    routing.add_argument(
        "--json", action="store_true",
        help="print the report as JSON instead of prose")
    routing.add_argument(
        "--save-report", metavar="FILE",
        help="write the per-question results in the shape score writes, so two routing "
             "runs compare with `ablate --without-report/--with-report` - the same paired "
             "test, clustered line and fixed/broke count as two SQL arms")
    routing.set_defaults(func=_cmd_score_routing)

    rewrite_p = sub.add_parser(
        "rewrite-questions",
        help="redraft the questions of existing golds, no warehouse, no re-mining")
    rewrite_p.add_argument("--golds", required=True, metavar="FILE",
                           help="a golds CSV with id, gold_sql and (usually) question")
    rewrite_p.add_argument("--out", required=True, metavar="FILE",
                           help="where the rewritten key goes; question_owes says what "
                                "each row still leaves out")
    rewrite_p.add_argument("--backend", choices=("gemini", "echo"), default="gemini")
    rewrite_p.add_argument("--echo-response", default="")
    rewrite_p.add_argument("--model", default="gemini-2.5-flash")
    rewrite_p.add_argument("--project", default=os.environ.get("CONTEXTLIFT_PROJECT"),
                           help="Vertex project (default: $CONTEXTLIFT_PROJECT)")
    rewrite_p.add_argument("--estimate-only", action="store_true",
                           help="count the prompts, print the ceiling, make no model call")
    rewrite_p.add_argument("--budget", type=float, default=None, metavar="USD")
    rewrite_p.add_argument("--yes", action="store_true",
                         help="say yes in writing instead of typing RUN at a "
                              "terminal; needs --budget, and is recorded with "
                              "the argv that carried it")
    rewrite_p.set_defaults(func=_cmd_rewrite_questions)

    mine_p = sub.add_parser(
        "mine", help="turn query history into candidate golds for human review"
    )
    # timeout only: mine's output is a CSV file, so --json has nothing to print
    mine_p.add_argument(
        "--timeout", type=float, default=None, metavar="SECONDS",
        help="abort any single candidate query that runs longer than this")
    mine_p.add_argument("--db", required=True, help=DB_HELP)
    mine_p.add_argument(
        "--all-datasets", action="store_true",
        help="mine the whole log rather than named datasets. Deliberate, and "
             "named so it reads as a choice: without --datasets this command "
             "refuses, because a scope chosen by traffic alone produced three "
             "rounds that measured the split instead of the context and one "
             "gold set where 6 of 43 questions touched a dataset anybody had "
             "written about (docs/PRACTICES.md practices 1 and 2)")
    # the estimate is measured once the shapes are ranked, right before
    # gating; --budget and --estimate-only apply at that moment
    mine_p.add_argument(
        "--keep-migration-checks", action="store_true",
        help="keep queries that compare one copy of the data to another (a "
             "table against its backup, staging against the data mart). They "
             "are dropped by default: they join and aggregate like real "
             "questions, and no written rule changes the answer to 'compare "
             "these and say pass or fail'. 36%% of usable candidates on one "
             "estate")
    _add_warehouse_flags(mine_p, estimate=True, live_stop=True)
    mine_p.add_argument(
        "--queries", required=True, nargs="+", action="extend", metavar="FILE",
        help="CSV with columns sql[, times_seen]; list several files or repeat the flag "
             "to mine them as one (a job log is per billing project, the data may span "
             "several)")
    mine_p.add_argument("--out", default="candidates.csv", help="review file to write")
    mine_p.add_argument("--limit", type=int, default=50, help="keep the top N shapes")
    mine_p.add_argument("--datasets", default=None, metavar="A,B",
                        help="mine only shapes whose primary table is in these datasets; "
                             "without it the whole log is mined and the summary says so. A "
                             "kept shape that also joins a table outside the scope, or in "
                             "another project, keeps it in the out_of_scope column: the "
                             "question may need data the agent cannot read, and a reviewer "
                             "decides that, not the miner")
    mine_p.add_argument("--no-spread", action="store_true",
                        help="raw top-N by traffic instead of round-robin across tables "
                             "(the default keeps the busiest table from filling the sample)")
    mine_p.add_argument(
        "--min-times-seen", type=int, default=1, help="drop shapes seen fewer times"
    )
    mine_p.add_argument("--row-cap", type=int, default=DEFAULT_ROW_CAP,
                       help=f"max rows fetched per query (default "
                            f"{DEFAULT_ROW_CAP:,}); a capped result is excluded "
                            "from the score, never compared. 0 for no cap")
    mine_p.add_argument(
        "--exclude-accounts", default="",
        help="comma-separated accounts to skip. ALWAYS include your own eval "
        "account: mining your agent's own traffic makes its SQL the gold it is "
        "scored against. Plain addresses either way: they are matched lower-cased "
        "against every account in ran_by (log joins them with ';') and against the "
        "MD5 of the lower-cased address, which is what log --hash-accounts stored, "
        "so nothing has to be hashed by hand",
    )
    mine_p.add_argument(
        "--rank-by", choices=("frequency", "users"), default="frequency",
        help="'users' when the log is dashboard-heavy: a scheduled refresh runs "
        "hundreds of times for one viewer, so executions rank machines and "
        "distinct users rank people",
    )
    mine_p.add_argument(
        "--min-tables", type=int, default=1,
        help="keep only queries naming at least N tables; 2 skips single-table "
        "KPI tiles",
    )
    mine_p.add_argument(
        "--data-projects", default=None, metavar="A,B",
        help="every project the agent under test can read, beyond --db's. "
             "`mine` runs the gold gate itself, so without this a mined set "
             "spanning production and pre-production comes back flagged "
             "CROSS_PROJECT whichever way --db is pointed: 191 of 400 on one "
             "estate, and 169 clean instead of 71 with it",
    )
    mine_p.add_argument(
        "--keep-machine-sql", action="store_true",
        help="keep queries carrying a tool's own JSON header (dbt, Looker, "
             "GitHub Actions). They are model builds and deployment checks, "
             "not questions, and excluding service accounts does not catch "
             "them because they run under a person's credentials",
    )
    mine_p.add_argument(
        "--max-question-literals", type=int, default=2, metavar="N",
        help="drop a drafted question carrying more than N quoted values: "
             "that is one analyst's own lookup, six product codes and an "
             "order number, not a question anyone else asks. 0 turns it off",
    )
    mine_p.add_argument(
        "--rules-in-context", action="store_true",
        help="a drafted question leaves OUT the values its gold filters on, "
             "because the business rule belongs in the context. Without this, "
             "10 of 25 mined golds named a rule in the question, which hands "
             "the bare arm the rule for free and makes a lift impossible to show")
    mine_p.add_argument(
        "--write-questions", action="store_true",
        help="draft the question column with Gemini, one short call per kept "
        "shape (a draft for review, never a gold; needs the [gemini] extra)")
    mine_p.add_argument("--model", default="gemini-2.5-flash",
                        help="Gemini model for --write-questions")
    mine_p.add_argument("--project", default=os.environ.get("CONTEXTLIFT_PROJECT"),
                        help="Vertex project for --write-questions (default: $CONTEXTLIFT_PROJECT)")
    mine_p.add_argument(
        "--require-analytical", action="store_true",
        help="drop queries with no aggregation or join (lookups and exports)",
    )
    mine_p.add_argument("--yes", action="store_true",
                         help="say yes in writing instead of typing RUN at a "
                              "terminal; needs --budget, and is recorded with "
                              "the argv that carried it")
    mine_p.set_defaults(func=_cmd_mine)

    mine_context_p = sub.add_parser(
        "mine-context",
        help="business conventions counted out of a query log: arithmetic "
             "many queries agree on, and inline code mappings. Free, no "
             "model call, no warehouse query",
        description=(
            "Reads a query log and writes the conventions its own analysts "
            "already agree on. A round on one estate measured an agent at 2 "
            "right of 18, and its failures were arithmetic rather than "
            "vocabulary: asked for the final shipping amount it wrote "
            "SUM(shipping_charge), where the answer key computed "
            "sum(line_total_shipping_amt) - sum(line_shipping_disc_amt). No "
            "glossary entry fixes that, and the formula was already in the "
            "history, written by the analysts fourteen times over.\n\n"
            "Nothing here is authored. A line appears only when that many "
            "DIFFERENT query shapes contain it, so a reviewer disagrees with "
            "the count rather than with an author. Pass --golds so every "
            "scored question's own SQL is excluded BEFORE counting: a "
            "reserved question's statement is in this log, and counting it "
            "would put a held-out answer into the context where the ordinary "
            "leak gate cannot see it."))
    mine_context_p.add_argument("--queries", required=True, metavar="CSV",
                               help="the query log, as `log` writes it")
    mine_context_p.add_argument("--out", required=True, metavar="DIR",
                               help="the spec directory; writes rules/ and vocabulary/")
    mine_context_p.add_argument(
        "--golds", default=None, metavar="CSV",
        help="the gold set, so every scored question's SQL is excluded before "
             "counting. Leaving this off is how a held-out answer reaches the "
             "context")
    mine_context_p.add_argument(
        "--exclude-ids", default=None, metavar="FILE",
        help="one id per line: exclude only these golds instead of all of "
             "them, e.g. holdout_ids.txt. Excluding ALL golds is the safe "
             "default and costs a little evidence")
    mine_context_p.add_argument(
        "--min-times", type=int, default=CONVENTION_MIN_TIMES, metavar="N",
        help=f"how many different query shapes must contain a line before it "
             f"is a convention (default {CONVENTION_MIN_TIMES}). Two is one "
             "analyst; three is a practice")
    mine_context_p.set_defaults(func=_cmd_mine_context)

    audit_p = sub.add_parser(
        "audit-golds",
        help="where does the answer key DISAGREE with what this warehouse's "
             "own queries usually do? Free, no model call, no warehouse query",
        description=(
            "An answer key is one analyst's query, and one analyst's query is "
            "not the same thing as the warehouse's convention. A round "
            "measured +0.0 with EVERY reserved answer changed by the written "
            "context, which means the context was read and used. One failure "
            "read like this: the rule said a quarter means fiscal_quarter, "
            "the agent obeyed it exactly, and the answer key used a calendar "
            "quarter. The agent did what it was told and scored worse.\n\n"
            "WHAT THIS IS NOT. A frequent pattern in the query log is "
            "EVIDENCE, not an authority, and this command cannot tell the two "
            "apart. If most queries use `fiscal_quarter` and one question asks "
            "for a CALENDAR quarter, then `DATE_TRUNC(..., QUARTER)` is the "
            "right answer and the disagreement here is a false alarm. Read "
            "every row before acting on it. Nothing is removed unless you ask "
            "for it.\n\n"
            "The project's methodology cites BIRD Mini-Dev, where an audit "
            "found 52.8% of its question-and-SQL pairs carried an annotation "
            "error. Not all of those are wrong SQL: the count includes "
            "ambiguous questions and missing specifications, which are faults "
            "in the PAIR rather than in the query. This command sees three "
            "kinds of disagreement a machine can check and says nothing about "
            "the rest."))
    audit_p.add_argument("--golds", required=True, metavar="CSV")
    audit_p.add_argument("--queries", required=True, metavar="CSV",
                        help="the query log, as `log` writes it")
    audit_p.add_argument("--out", default=None, metavar="CSV",
                        help="write only the golds that carry no "
                             "disagreement. Read the report first: this "
                             "changes which questions the benchmark contains, "
                             "on observational evidence, and a false alarm "
                             "here deletes a correct answer key. Re-run "
                             "`split` on it, because the halves change when "
                             "the set does")
    audit_p.add_argument("--min-times", type=int, default=CONVENTION_MIN_TIMES,
                        metavar="N")
    audit_p.set_defaults(func=_cmd_audit_golds)

    semantic_p = sub.add_parser(
        "semantic", help="read a BI model for business rules already written down"
    )
    semantic_source = semantic_p.add_mutually_exclusive_group(required=True)
    semantic_source.add_argument(
        "--lookml", help="a LookML project directory")
    semantic_source.add_argument(
        "--ossie", help="an Apache Ossie semantic model file (.yaml or .json)")
    semantic_p.add_argument(
        "--allow-untested-version", action="store_true",
        help="read an Ossie file whose spec version this reader was not tested "
             "against; the spec is before 1.0 and field names still move")
    semantic_p.add_argument(
        "--context-out", default=None,
        help="write the rules as a context bundle for the with-context arm")
    semantic_p.add_argument(
        "--candidates-out", default=None,
        help="write candidate questions; gold_sql is left empty for a human")
    semantic_p.set_defaults(func=_cmd_semantic)

    arms_p = sub.add_parser(
        "arms", help="read a spec repo and build the control and treatment arms"
    )
    arms_p.add_argument("--spec", default=None,
                        help="the spec repo directory (rules/, schema/, vocabulary/, "
                             "examples/, relationships.yaml)")
    arms_p.add_argument("--tables", nargs="+", default=None,
                        help="only these tables; the default is every table the spec describes")
    arms_p.add_argument("--model", default="LATEST_GA_MODEL",
                        help="the model alias both arms use")
    arms_p.add_argument("--out", default=None,
                        help="write each arm's request body here, named by its hash")
    arms_p.add_argument("--catalog", action="store_true",
                        help="read the context out of Knowledge Catalog "
                             "instead of a spec repo. Needs --tables and "
                             "--project; the same content from either "
                             "source builds the same request, which a test "
                             "asserts")
    arms_p.add_argument("--eval-room", default=None, metavar="PROJECT.DATASET",
                        help="build the arm about the evaluation room's views instead of the tables the spec names. The spec keeps naming the business's own tables, because asking a team to rewrite their documentation to match a room is asking them to keep two copies of the truth")
    arms_p.add_argument("--project", default=None,
                        help="the project whose Knowledge Catalog to read (--catalog)")
    arms_p.add_argument("--catalog-location", default="us",
                        help="the region the entries live in, which is the "
                             "dataset's region, not the chat API's (default us)")
    arms_p.add_argument("--served", action="store_true",
                        help="build the arm from what lookupContext SERVES rather than from the spec, as prose in one instruction. Its difference from the ordinary arm is what serving through the catalog adds or loses (Preview API)")
    arms_p.add_argument("--entry-group", default="@bigquery")
    arms_p.set_defaults(func=_cmd_arms)

    round_p = sub.add_parser(
        "round", help="two arms, the same questions, one verdict"
    )
    round_p.add_argument(
        "--target-lift", type=float, default=None, metavar="POINTS",
        help="the effect in accuracy points this round is paying to see. "
             "The reserved half is checked against it BEFORE anything is "
             "spent: a set expected to move fewer than 6 questions cannot "
             "reach a verdict however the context is written, because 6 "
             "unanimous disagreements is the cheapest significant result "
             "that exists. Default 6.9, the largest deployable "
             "written-context effect this tool has measured on an "
             "enterprise warehouse (docs/CASE_STUDY.md). Declare a bigger "
             "number only if you really expect a bigger effect")
    round_p.add_argument("--golds", required=True, help="the gold set CSV")
    round_p.add_argument("--spec", default=None,
                         help="the spec repo directory, or --catalog to read "
                              "the context out of Knowledge Catalog instead")
    round_p.add_argument("--db", default=None,
                         help="where to run the gold and predicted SQL, e.g. bq://project")
    round_p.add_argument(
        "--data-projects", default=None, metavar="A,B",
        help="every project the agent can read beyond --db's; golds naming "
             "these projects are not classified as cross-project")
    round_p.add_argument("--project", default=None,
                         help="the project the chat calls are billed to")
    round_p.add_argument("--location", default="global")
    round_p.add_argument("--predictions-control", default=None, metavar="FILE.csv",
                         help="score predictions made elsewhere: the control "
                              "arm's answers, as id and pred_sql")
    round_p.add_argument("--predictions-treatment", default=None, metavar="FILE.csv",
                         help="the treatment arm's answers. Both are needed: "
                              "one file alone is a score, not a comparison")
    round_p.add_argument("--eval-room", default=None, metavar="PROJECT.DATASET",
                         help="build the arm about the evaluation room's views instead of the tables the spec names. The spec keeps naming the business's own tables, because asking a team to rewrite their documentation to match a room is asking them to keep two copies of the truth")
    round_p.add_argument("--served", action="store_true",
                         help="build the arm from what lookupContext SERVES rather than from the spec, as prose in one instruction. Its difference from the ordinary arm is what serving through the catalog adds or loses (Preview API)")
    round_p.add_argument("--catalog-location", default="us",
                         help="the region the catalog entries live in, which "
                              "is the dataset's region, not the chat API's "
                              "(default us)")
    round_p.add_argument("--model", default="LATEST_GA_MODEL")
    round_p.add_argument("--tables", nargs="+", default=None)
    round_p.add_argument("--k", type=int, default=1,
                         help="repeats per question per arm; the repeat check "
                              "says whether more than one buys anything")
    round_p.add_argument("--holdout-share", type=float, default=0.3)
    round_p.add_argument(
        "--external-context", metavar="REASON", default="",
        help="reserve EVERY gold, because nobody in this loop wrote the "
             "context. Takes a reason, which is recorded: the case it exists "
             "for is a semantic layer or a catalog authored before the "
             "questions existed, where a reserved half protects against "
             "nothing. It removes the fit estimate and the OVERFIT check, so "
             "the reason has to be attributable")
    round_p.add_argument("--seed", default=SPLIT_SEED,
                         help="the split seed; hashing means adding questions "
                              "never moves an existing one across the line")
    round_p.add_argument("--target", type=float, default=10.0, metavar="POINTS",
                         help="the lift this round wants to be able to see; "
                              "below it the verdict reads UNDERPOWERED and prints "
                              "how many questions would resolve it")
    round_p.add_argument("--allow-sql", action="store_true",
                         help="waive the pasted-SQL gate; recorded, not hidden")
    round_p.add_argument("--yes", action="store_true",
                         help="say yes in writing instead of typing RUN; needs --budget")
    round_p.add_argument("--budget", type=float, default=None, metavar="USD")
    round_p.add_argument("--max-billed-bytes", type=int, default=None)
    round_p.add_argument("--max-bytes", type=_byte_size, default=None,
                         metavar="SIZE",
                         help="the byte ceiling on each chat's query: 50GB, 500MB, or "
                              "plain bytes")
    round_p.add_argument("--timeout", type=float, default=None)
    round_p.add_argument(
        "--gold-cache", metavar="FILE",
        help="reuse answer-key results between scoring arms, keyed by gold SQL")
    round_p.add_argument(
        "--extra-columns", choices=["reject", "tolerate"], default="reject",
        help="tolerate prediction columns beyond the gold columns, as score does")
    round_p.add_argument(
        "--save-report", default=None, metavar="FILE",
        help="write the treatment working-set score for offline triage")
    round_p.add_argument("--dry-run", action="store_true",
                         help="print the arms, the gates and the cost, ask nothing")
    round_p.add_argument("--checkpoint", default=None,
                         help="record each answer as it arrives, and reuse it "
                              "on a later attempt. An answer costs money and a "
                              "wait; a transient failure should not throw the "
                              "finished ones away")
    round_p.add_argument("--run-out", default=None,
                         help="write one row per scored question: what was "
                              "asked, what came back, whether it was right. A "
                              "committed run is what lets a number in a "
                              "document be recomputed with no warehouse and no "
                              "model")
    round_p.add_argument(
        "--cost", action="store_true",
        help="read what this round's SQL cost from the warehouse's job "
             "ledger and print it beside the verdict, per arm, as cost per "
             "answer AND cost per correct answer. A metadata read: no gold "
             "SQL runs and no table is scanned, so it costs nothing. The "
             "`spent:` lines count MODEL calls and cannot count this, which "
             "is how per-command totals reached about $74 against $149.61 in "
             "the ledger. Also writes bytes_billed and query_cost_usd onto "
             "--run-out, so a committed run keeps its own cost after the "
             "ledger's 180-day window closes")
    round_p.add_argument("--out", default=None, help="write the verdict JSON here")
    round_p.add_argument("--catalog", action="store_true",
                        help="read the context out of Knowledge Catalog "
                             "instead of a spec repo. Needs --tables and "
                             "--project; the same content from either "
                             "source builds the same request, which a test "
                             "asserts")
    round_p.add_argument("--entry-group", default="@bigquery")
    round_p.add_argument(
        "--curate-ids", default=None, metavar="FILE",
        help="the working ids, one per line, as `split` writes them. Found "
             "automatically beside --golds when it is not given; pass it to "
             "override. A round that makes its own split instead of using the "
             "one you built worked examples from will put reserved answers in "
             "the context")
    round_p.add_argument(
        "--holdout-ids", default=None, metavar="FILE",
        help="the reserved ids, one per line. Goes with --curate-ids")
    round_p.add_argument(
        "--holdout-ledger", default=None, metavar="FILE",
        help="JSON file counting how many times each reserved question has "
             "been scored. Pass the same file every round on a gold set and "
             "the re-read gate can finally fire: it took a count that nothing "
             "ever supplied, so it read 'first read' on every round and the "
             "run record wrote holdout_reads: 1 forever. Without this flag "
             "the record now says null, which is 'not tracked' rather than a "
             "made-up 1")
    round_p.add_argument(
        "--row-cap", type=int, default=DEFAULT_ROW_CAP, metavar="N",
        help=f"max rows fetched per query (default {DEFAULT_ROW_CAP:,}). "
             "This one is not optional in spirit: a round executes SQL the "
             "model wrote, and untrusted SQL with no cap is how one run "
             "reached 6.5 GB of memory fetching a prediction's result. A "
             "capped result is EXCLUDED from the score as ROW_CAP_HIT, never "
             "compared, because comparing a full result to a cut one reports "
             "a mismatch that looks like a real failure. 0 for no cap")
    round_p.set_defaults(func=_cmd_round)

    examples_p = sub.add_parser(
        "build-examples",
        help="worked examples from the working questions, refusing any that "
             "hand over a held-out one")
    examples_p.add_argument("--golds", required=True,
                            help="CSV with id, question, gold_sql")
    examples_p.add_argument("--out", required=True, metavar="FILE.jsonl",
                            help="the bank, one {question, sql} per line")
    examples_p.add_argument("--holdout-share", type=float, default=0.3,
                            help="must match the round's, or the bank is built "
                                 "against the wrong held-out questions")
    examples_p.add_argument("--seed", default=SPLIT_SEED,
                            help="must match the round's, for the same reason")
    examples_p.set_defaults(func=_cmd_build_examples)

    spec_push_p = sub.add_parser(
        "spec-push",
        help="a spec repo into Knowledge Catalog, then read back and compared "
             "(the inverse of round --catalog). The APPLY step; --dry-run is "
             "the PLAN and the DRIFT check",
        description=(
            "Write a spec repo into Knowledge Catalog, then read every entry "
            "back and compare it, because a write that reports success can "
            "still store something different.\n\n"
            "This one verb is the APPLY step. Pass --dry-run and it writes "
            "nothing: it reads each entry, compares it against the spec, and "
            "prints what would change, what is already identical and what "
            "would be refused. Run that before a push and it is a PLAN. Run "
            "it after a push and it is a DRIFT check. The command is the "
            "same; only when you run it differs.\n\n"
            "The UNDO step is a separate verb, catalog-rollback, and it only "
            "works if this push was given --previous-out."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    spec_push_p.add_argument("--spec", required=True, metavar="DIR")
    spec_push_p.add_argument("--project", required=True,
                             help="the project holding the catalog entries")
    spec_push_p.add_argument("--catalog-location", default="us",
                             help="the region the entries live in, which is "
                                  "the dataset's region (default us)")
    spec_push_p.add_argument("--tables", nargs="+", default=None,
                             help="default: every table the spec names")
    spec_push_p.add_argument("--eval-room", default=None, metavar="PROJECT.DATASET",
                             help="build the arm about the evaluation room's views instead of the tables the spec names. The spec keeps naming the business's own tables, because asking a team to rewrite their documentation to match a room is asking them to keep two copies of the truth")
    spec_push_p.add_argument("--model", default="LATEST_GA_MODEL")
    spec_push_p.add_argument("--entry-group", default="@bigquery")
    spec_push_p.add_argument("--no-examples", action="store_true",
                             help="overviews only; leave the queries aspect alone")
    spec_push_p.add_argument("--no-served-check", action="store_true",
                             help="skip the lookupContext read that says what "
                                  "a reader actually gets (Preview API)")
    spec_push_p.add_argument("--glossary", default="contextlift", metavar="ID",
                             help="the glossary the spec's business terms are "
                                  "written to and read back from (default "
                                  "contextlift). A term whose tier is not 'org' "
                                  "goes into a category of that name inside it. "
                                  "Pass an empty value to write no terms")
    spec_push_p.add_argument("--prune-terms", action="store_true",
                             help="delete terms this tool wrote that the spec "
                                  "no longer has. Off by default: a glossary is "
                                  "shared, so removing anything from it is "
                                  "something you ask for. Terms nobody here "
                                  "wrote are never touched, with or without it")
    spec_push_p.add_argument("--provenance", action="store_true",
                             help="stamp each entry with the hashes this push "
                                  "wrote, so a number quoted later can be "
                                  "shown as stale when the page has changed")
    spec_push_p.add_argument("--round-id", default=None,
                             help="the name this measurement is filed under")
    spec_push_p.add_argument("--previous-out", default=None, metavar="DIR",
                             help="save what each entry held BEFORE the write, "
                                  "which is the only thing that makes a "
                                  "rollback possible: the catalog keeps no "
                                  "version of its own")
    spec_push_p.add_argument("--dry-run", action="store_true",
                             help="write nothing. Read every entry, compare it "
                                  "against the spec, and print what would be "
                                  "written, what is already identical and "
                                  "what would be refused. Run before a push "
                                  "it is a PLAN; run after one it is a DRIFT "
                                  "check, and the difference is only when you "
                                  "run it")
    spec_push_p.set_defaults(func=_cmd_spec_push)

    rollback_p = sub.add_parser(
        "catalog-rollback",
        help="push back what a spec-push replaced: the UNDO step",
        description=(
            "The UNDO step for spec-push.\n\n"
            "Knowledge Catalog keeps no version history of its own, so there "
            "is nothing here to roll back TO unless the push that overwrote "
            "an entry was given --previous-out. That directory holds the "
            "bytes each entry carried before the write, and this verb pushes "
            "them back.\n\n"
            "Without it, a spec-push cannot be undone at all."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    rollback_p.add_argument("--previous", required=True, metavar="DIR",
                            help="the directory spec-push --previous-out wrote")
    rollback_p.add_argument("--project", required=True)
    rollback_p.add_argument("--catalog-location", default="us")
    rollback_p.add_argument("--entry-group", default="@bigquery")
    rollback_p.add_argument("--dry-run", action="store_true")
    rollback_p.set_defaults(func=_cmd_catalog_rollback)

    classes_p = sub.add_parser(
        "classes",
        help="what each class of context would cost to price, and which are "
             "too small for this many questions")
    classes_p.add_argument("--spec", default=None, metavar="DIR")
    classes_p.add_argument("--golds", default=None,
                           help="to count the working questions")
    classes_p.add_argument("--working", type=int, default=None,
                           help="the working question count, if there is no golds file")
    classes_p.add_argument("--tables", nargs="+", default=None)
    classes_p.add_argument("--model", default="LATEST_GA_MODEL")
    classes_p.add_argument("--holdout-share", type=float, default=0.3)
    classes_p.add_argument("--seed", default=SPLIT_SEED)
    classes_p.add_argument("--catalog", action="store_true")
    classes_p.add_argument("--served", action="store_true")
    classes_p.add_argument("--project", default=None)
    classes_p.add_argument("--catalog-location", default="us")
    classes_p.add_argument("--entry-group", default="@bigquery")
    classes_p.add_argument("--eval-room", default=None, metavar="PROJECT.DATASET")
    classes_p.add_argument(
        "--cost", action="store_true",
        help="also read what each arm's SQL cost from the warehouse's job "
             "ledger, per class, as cost per answer AND cost per correct "
             "answer. A metadata read, so it costs nothing beyond the round "
             "itself. This is the second axis of value: an agent without good "
             "context explores more, so a class can pay for itself in query "
             "bill even when its accuracy verdict reads NOT PROVEN")
    classes_p.add_argument("--run", action="store_true",
                           help="actually ask the questions and price each "
                                "class, rather than printing the plan")
    classes_p.add_argument("--db", default=None,
                           help="where the gold and predicted SQL run")
    classes_p.add_argument("--location", default="global")
    classes_p.add_argument("--budget", type=float, default=None)
    classes_p.add_argument("--yes", action="store_true")
    classes_p.add_argument("--checkpoint", default=None, metavar="FILE.jsonl",
                           help="answers already paid for; the full arm's are "
                                "reused from a round's checkpoint")
    classes_p.set_defaults(func=_cmd_classes)

    history_p = sub.add_parser(
        "history", help="one question across every round: what broke it, and when"
    )
    history_p.add_argument("--workspace", default=".",
                           help="the workspace holding the rounds")
    history_p.add_argument("--out", default=None,
                           help="write the full table here as CSV. One row per "
                                "question, one column per round")
    history_p.add_argument("--show", type=int, default=12,
                           help="how many questions to list in the terminal "
                                "(default 12). The file carries all of them")
    history_p.set_defaults(func=_cmd_history)

    worklist_p = sub.add_parser(
        "worklist", help="which context item to look at first, and why"
    )
    worklist_p.add_argument("--run", required=True,
                            help="the CSV `round --run-out` wrote")
    worklist_p.add_argument("--spec", required=True,
                            help="the spec repo the treatment arm was built from")
    worklist_p.add_argument("--tables", nargs="+", default=None)
    worklist_p.set_defaults(func=_cmd_worklist)

    share_p = sub.add_parser(
        "share", help="the only export: counts, lengths and outcomes, no customer text"
    )
    share_p.add_argument("--verdict", required=True,
                         help="the round's verdict JSON")
    share_p.add_argument("--out", required=True,
                         help="where to write the shareable file")
    share_p.add_argument("--map", default=None,
                         help="the real-name to alias map. Keep it out of the "
                              "repository: a map back to the real names is the "
                              "leak the aliases prevent")
    share_p.add_argument("--forbid", nargs="+", default=None,
                         help="strings that must not appear anywhere in the "
                              "output, beyond the table names")
    share_p.add_argument("--forbid-file", default=None,
                         help="a file of forbidden strings, one per line, # for "
                              "comments")
    share_p.set_defaults(func=_cmd_share)

    leak_p = sub.add_parser(
        "leak", help="does the context bundle contain the questions being scored?"
    )
    leak_p.add_argument(
        "--predictions", required=True, help="CSV with id, question, gold_sql"
    )
    leak_p.add_argument(
        "--context", default=None,
        help="file holding everything the agent is given at runtime",
    )
    leak_p.add_argument(
        "--context-per-question", metavar="FILE.json", default=None,
        help="JSON {question_id: context_text} when a retriever chose the context. "
             "Each question is checked against its own retrieved text, and any "
             "question the retriever did not serve - a reserved one - is checked "
             "against everything retrieved, because an answer can sit in a document "
             "fetched for some other question.")
    leak_p.set_defaults(func=_cmd_leak)

    bisect_p = sub.add_parser(
        "bisect",
        help="which rule broke that question: halve the rule set, do not "
             "serialise",
        description=(
            "Five rules went in, the safety line says two reserved questions "
            "broke, and somebody has to say WHICH rule did it.\n\n"
            "One rule per round is the naive answer and it is wrong twice: it "
            "costs about five times the answers, and one rule moves accuracy "
            "so little that a customer-size gold set returns five UNDERPOWERED "
            "verdicts instead of one usable round. This asks only the broken "
            "questions, against halves of the rule set, in ceil(log2(R)) arms "
            "rather than R.\n\n"
            "WHAT IT COSTS IS THE QUESTIONS. Asking a reserved question again "
            "is a second read of it, so a bisected question is spent: it joins "
            "the working half and never counts in a reported number again. "
            "The trade is printed before anything is asked, and --spent-out "
            "records it for `split --spent`."))
    bisect_p.add_argument("--run", required=True, metavar="RUN.csv",
                          help="the round's scored rows. The broken questions "
                               "are read from here, not retyped")
    bisect_p.add_argument("--golds", required=True, metavar="FILE",
                          help="the gold set the run scored against")
    bisect_p.add_argument("--spec", default=None, metavar="DIR")
    bisect_p.add_argument(
        "--db", required=True, metavar="SPEC",
        help="the warehouse the answers are scored against, e.g. "
             "sqlite:///shop.db or bq://PROJECT. REQUIRED: bisect scores every "
             "arm it asks, and without this it accepted the cost sheet and "
             "then raised AttributeError after the money was approved")
    bisect_p.add_argument(
        "--timeout", type=int, default=None,
        help="seconds per statement; unset means 60 on BigQuery, because "
             "predicted SQL is untrusted model output")
    # DEFAULT_ROW_CAP, not None. Two multi-hour hangs in one day came from an
    # unbounded cap, and bisect executes PREDICTED SQL -- written by a model,
    # so untrusted by definition. A guard in tests/test_cli.py refused the
    # None I first wrote here.
    bisect_p.add_argument("--row-cap", type=int, default=DEFAULT_ROW_CAP,
                          help="rows per statement before the result is "
                               "called truncated and excluded; 0 for no cap")
    bisect_p.add_argument("--project", required=True)
    bisect_p.add_argument("--location", default="us")
    bisect_p.add_argument("--tables", nargs="+", default=None)
    bisect_p.add_argument("--model", default="LATEST_GA_MODEL")
    bisect_p.add_argument("--questions", default=None, metavar="IDS",
                          help="comma separated, to bisect fewer than all of "
                               "them. Every id must be one the context "
                               "actually broke")
    bisect_p.add_argument("--spent-out", default=None, metavar="FILE",
                          help="append the bisected ids here, so `split "
                               "--spent` can keep them out of the reserved "
                               "half for good")
    bisect_p.add_argument("--estimate-only", action="store_true",
                          help="price it and stop")
    bisect_p.add_argument("--budget", type=float, default=None)
    bisect_p.add_argument("--yes", action="store_true")
    bisect_p.set_defaults(func=_cmd_bisect)

    split_p = sub.add_parser("split", help="stable curate/holdout split of a gold set")
    split_p.add_argument("--golds", required=True, help="CSV with an id column")
    split_p.add_argument("--holdout-fraction", type=float, default=0.3)
    split_p.add_argument("--seed", default=SPLIT_SEED)
    split_p.add_argument("--out-curate", default=None,
                         help="id list (default: curate_ids.txt)")
    split_p.add_argument("--out-holdout", default=None,
                         help="id list (default: holdout_ids.txt)")
    split_p.add_argument(
        "--out-curate-csv", default=None, metavar="FILE",
        help="the curate rows of --golds, every column kept (written under "
             "--workspace by default)")
    split_p.add_argument(
        "--out-holdout-csv", default=None, metavar="FILE",
        help="the holdout rows of --golds, every column kept")
    split_p.add_argument(
        "--spent", default=None, metavar="FILE",
        help="ids that may never be reserved again, one per line. `bisect` "
             "writes this file: asking a reserved question a second time is a "
             "second read of it, so a bisected question joins the working "
             "half for good. Without this flag the discipline would be "
             "something a person had to remember")
    split_p.set_defaults(func=_cmd_split)

    ablate_p = sub.add_parser(
        "ablate", help="paired comparison of two arms: what was the context worth"
    )
    ablate_p.add_argument(
        "--db", default=None,
        help=DB_HELP + "; needed only when an arm is given as predictions CSVs "
             "rather than saved reports")
    ablate_p.add_argument(
        "--without-context", nargs="+", action="extend",
        help="baseline predictions CSV; list several or repeat the flag for repeated runs")
    ablate_p.add_argument(
        "--with-context", nargs="+", action="extend",
        help="predictions CSV with the context added; list several or repeat the flag")
    ablate_p.add_argument(
        "--without-report", nargs="+", action="extend",
        help="saved report (score --save-report) for the baseline arm; list several or "
             "repeat the flag for repeated runs, same as --without-context")
    ablate_p.add_argument(
        "--with-report", nargs="+", action="extend",
        help="saved report for the context arm; list several or repeat the flag")
    ablate_p.add_argument(
        "--row-cap", type=int, default=DEFAULT_ROW_CAP,
        help="max rows fetched per query (does not bound time; see --timeout)")
    ablate_p.add_argument(
        "--confidence", type=float, default=0.95,
        help="confidence level for the two accuracy intervals (default 0.95)")
    ablate_p.add_argument("--adjusted", action="store_true",
                          help="compare judge-adjusted correctness instead of raw execution")
    ablate_p.add_argument("--ids", default=None, metavar="FILE",
                          help="restrict both arms to these question ids (one per line, as "
                               "split writes): the curate half for a training reading, the "
                               "holdout half for the number")
    ablate_p.add_argument(
        "--show", action="store_true",
        help="name every question the context broke or fixed")
    _add_run_flags(ablate_p)
    ablate_p.set_defaults(func=_cmd_ablate)

    attribute_p = sub.add_parser(
        "attribute",
        help="name the context rules behind an ablation's broken/fixed questions",
    )
    attribute_p.add_argument(
        "--db", default=None,
        help=DB_HELP + " Not needed when both arms are given as saved reports.")
    attribute_p.add_argument(
        "--without-context", required=True, nargs="+", action="extend",
        help="baseline predictions CSV; list several or repeat the flag. Always "
             "needed: a saved report keeps the verdict, not the SQL, and the SQL is "
             "what a rule is credited against. Reading it costs nothing.")
    attribute_p.add_argument(
        "--with-context", required=True, nargs="+", action="extend",
        help="predictions CSV with the context added; list several or repeat the flag")
    attribute_p.add_argument(
        "--without-report", nargs="+", action="extend",
        help="saved report (score --save-report) for the baseline arm, so the scoring "
             "is not repeated; list several or repeat the flag")
    attribute_p.add_argument(
        "--with-report", nargs="+", action="extend",
        help="saved report for the context arm; list several or repeat the flag")
    attribute_p.add_argument(
        "--context", required=True,
        help="the context file whose rules get suspected, one "
             '`- "phrase" -> meaning` line per rule; a rule is matched by the '
             "table.column its meaning names, lines with none are counted")
    attribute_p.add_argument("--row-cap", type=int, default=DEFAULT_ROW_CAP)
    attribute_p.add_argument("--confidence", type=float, default=0.95)
    _add_run_flags(attribute_p)
    attribute_p.set_defaults(func=_cmd_attribute)

    generate_p = sub.add_parser(
        "generate",
        help="produce predictions: same prompt, arms differ only by context",
    )
    generate_p.add_argument(
        "--questions", required=True,
        help="CSV with id, question, gold_sql (gold rides along, never sent)")
    generate_p.add_argument(
        "--schema", default=None, help="schema text file")
    generate_p.add_argument("--out", required=True, help="predictions CSV path")
    generate_p.add_argument(
        "--context", default=None,
        help="the context under test (glossary/rules file); omit for baseline")
    generate_p.add_argument(
        "--examples", default=None,
        help="JSON {id: examples_text} from curate-examples")
    generate_p.add_argument(
        "--context-per-question", metavar="FILE.json", default=None,
        help='JSON {question_id: context_text}, which is what a retriever emits. '
             'Use it when the whole documentation does not fit in a prompt and a '
             'router picks what each question sees. Merges with --examples. The arm '
             'is named after what was retrieved rather than after the file, so the '
             'same retriever over a source that changed is a different arm.')
    generate_p.add_argument(
        "--backend", choices=["gemini", "echo", "command"], default="gemini",
        help="echo returns --echo-response verbatim, for offline runs/tests; command "
             "runs --command once per question with JSON on stdin and reads SQL from stdout")
    generate_p.add_argument("--echo-response", default="SELECT 1;")
    generate_p.add_argument("--command", dest="runner_command", default=None, metavar="PROGRAM",
                            help="with --backend command: the agent under test. stdin gets "
                                 '{"id","question","rep","schema","context"}; stdout is the SQL, '
                                 'or {"sql","table"} with the routed table; non-zero exit or '
                                 "--timeout is an empty answer")
    generate_p.add_argument("--runner-version", default=None, metavar="TEXT",
                            help="with --backend command: the agent's version, recorded in the "
                                 "run manifest so the arm can be named later")
    generate_p.add_argument(
        "--model", default="gemini-2.5-flash",
        help="Gemini model for --backend gemini (default: gemini-2.5-flash)")
    generate_p.add_argument("--project", default=os.environ.get("CONTEXTLIFT_PROJECT"),
                            help="Vertex project (default: $CONTEXTLIFT_PROJECT); when "
                                 "$GEMINI_API_KEY is set, use the Google AI Studio "
                                 "Developer API instead")
    generate_p.add_argument(
        "--reps", type=int, default=1,
        help="repetitions; >1 writes out_rep1.csv.. (one run is a sample)")
    generate_p.add_argument("--workers", type=int, default=4)
    generate_p.add_argument("--dialect", default=None,
                            help="the SQL dialect named in the prompt (default: BigQuery when "
                                 "--db or the workspace's bigquery.txt is a bq:// spec, else SQLite)")
    generate_p.add_argument(
        "--retry", action="store_true",
        help="check each draft without running it (BigQuery dry run, SQLite EXPLAIN), run a "
             "clean one once, and feed an error or empty result back once: a prompt backend "
             "gets it appended, a command runner gets attempt/previous_sql/error in its JSON")
    generate_p.add_argument("--db", default=None, help="database for --retry: " + DB_HELP)
    generate_p.add_argument(
        "--timeout", type=float, default=30.0,
        help="per-query bound for the --retry execution")
    _add_warehouse_flags(generate_p, estimate=False)
    generate_p.add_argument(
        "--estimate-only", action="store_true",
        help="count the real prompts, print the cost estimate, make no "
             "model call")
    generate_p.add_argument(
        "--budget", type=float, default=None, metavar="USD",
        help="refuse to start when the estimate is above this, and stop "
             "answering (EMPTY predictions) once spend reaches it")
    generate_p.add_argument(
        "--input-price", type=float, default=None, metavar="USD_PER_MILLION",
        help="override the input token price for --model")
    generate_p.add_argument(
        "--output-price", type=float, default=None, metavar="USD_PER_MILLION",
        help="override the output token price for --model")
    generate_p.add_argument(
        "--json", action="store_true",
        help="with --estimate-only, print the estimate as JSON")
    generate_p.add_argument("--yes", action="store_true",
                         help="say yes in writing instead of typing RUN at a "
                              "terminal; needs --budget, and is recorded with "
                              "the argv that carried it")
    generate_p.set_defaults(func=_cmd_generate)

    curate_p = sub.add_parser(
        "curate-examples",
        help="build a leak-checked worked-example bank from a gold pool",
    )
    curate_p.add_argument(
        "--pool", required=True,
        help="CSV (id, question, gold_sql) examples may come from")
    curate_p.add_argument(
        "--targets", required=True,
        help="CSV (id, question, gold_sql) that will be scored; their answers "
             "are banned from the bank")
    curate_p.add_argument("--out", required=True, help="bank JSON path")
    curate_p.add_argument("--per-question", type=int, default=3)
    curate_p.set_defaults(func=_cmd_curate_examples)

    climb_p = sub.add_parser(
        "climb", help="track accuracy across rounds of context enrichment"
    )
    climb_p.add_argument("--db", default=None,
                         help=DB_HELP + " (needed only when a round is a predictions CSV)")
    climb_p.add_argument("--adjusted", action="store_true",
                         help="compare judge-adjusted correctness instead of raw execution")
    climb_p.add_argument(
        "--round",
        nargs="+",
        action="extend",
        required=True,
        metavar="LABEL=PATH",
        help="one per round, baseline first; list them or repeat the flag. PATH is a "
             "predictions CSV "
             "(scored here) or a saved score report (free); repeat a label with one "
             "report per repetition to pool them",
    )
    climb_p.add_argument(
        "--holdout",
        action="store_true",
        help="these questions were held out of curation, so suppress the "
        "overfitting warning",
    )
    climb_p.add_argument("--row-cap", type=int, default=DEFAULT_ROW_CAP)
    climb_p.add_argument("--confidence", type=float, default=0.95)
    climb_p.add_argument("--ids", default=None, metavar="FILE",
                         help="restrict every round to these question ids (one per line)")
    climb_p.add_argument(
        "--show", action="store_true",
        help="name what each round broke and fixed vs the round before")
    _add_run_flags(climb_p)
    climb_p.set_defaults(func=_cmd_climb)

    lint_p = sub.add_parser(
        "lint", help="refuse written context with a defect, before a round is "
                     "paid for")
    lint_p.add_argument("--spec", required=True,
                        help="the spec repo directory (rules/, schema/, "
                             "examples/)")
    lint_p.add_argument(
        "--golds", default=None,
        help="the gold set CSV. Without it the checks that need to know what "
             "the questions touch are SKIPPED and the report says so, rather "
             "than passing by default")
    lint_p.add_argument("--json", action="store_true")
    lint_p.set_defaults(func=_cmd_lint)

    triage_p = sub.add_parser(
        "triage", help="sort failures by shape, so curation has a target"
    )
    triage_p.add_argument("--db", default=None, help=DB_HELP)
    triage_p.add_argument("--predictions", default=None, help="predictions CSV")
    triage_p.add_argument(
        "--report", default=None, metavar="FILE",
        help="a saved score report (score --save-report); with it, no SQL "
             "runs and --db/--predictions are not needed")
    triage_p.add_argument(
        "--show", action="store_true", help="list every failing question"
    )
    triage_p.add_argument(
        "--execution-only",
        action="store_true",
        help="triage raw execution failures, ignoring judge upgrades",
    )
    triage_p.add_argument("--row-cap", type=int, default=DEFAULT_ROW_CAP)
    _add_run_flags(triage_p)
    triage_p.add_argument("--confidence", type=float, default=0.95)
    triage_p.set_defaults(func=_cmd_triage)

    parser.epilog = HELP_EPILOG.format(total=len(sub.choices))

    # Listed in the order a person RUNS them, not the order they were
    # registered. Registration order put `bundle` (read the result) above
    # `round` (produce the result), which teaches the loop backwards.
    #
    # `_choices_actions` is private, so this is guarded: if a future argparse
    # drops it, the help falls back to registration order, which is merely
    # worse and not broken. A display detail must never raise.
    try:
        order = {name: i for i, name in enumerate(CORE_VERBS)}
        sub._choices_actions.sort(
            key=lambda a: order.get(a.dest, len(order)))
    except (AttributeError, TypeError):  # pragma: no cover -- argparse changed
        pass
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse args and dispatch; returns the process exit code.

    With --workspace (or $CONTEXTLIFT_WORKSPACE) the same handler runs inside a
    recorded run directory; without it, nothing about a command changes.
    """
    given = list(argv) if argv is not None else sys.argv[1:]
    if "--workspace" in given and given.index("--workspace") > 0 and given[0] not in ("-h", "--help"):
        # the global flag after the command is the first thing a new operator
        # types; argparse's "unrecognized arguments" does not say where it goes
        at = given.index("--workspace")
        rest = [a for i, a in enumerate(given) if i not in (at, at + 1)]
        print("put --workspace DIR before the command: contextlift --workspace DIR "
              + " ".join(rest), file=sys.stderr)
        return 2
    if "--help-all" in given:
        parser = _build_parser(show_all=True)
        # Grouped, not a flat list of 48. The families are the answer to "why
        # are there so many", and a braced list of 48 hides it.
        choices = parser._subparsers._group_actions[0].choices
        parser.epilog = (f"all {len(choices)} verbs, by what they are for:\n\n"
                         + grouped_verbs(choices))
        parser.print_help()
        return 0
    args = _build_parser().parse_args(argv)
    # The command name, onto every BigQuery job this process runs as a
    # label. Taken from the handler rather than from argv because a handler
    # has exactly one name and argv has global flags in front of it. See
    # identity.py.
    handler = getattr(args, "func", None)
    identity.set_stage(getattr(handler, "__name__", "").removeprefix("_cmd_"))
    from .workspace import missing_file_message, record_run
    if not args.workspace:
        try:
            return args.func(args)
        except FileNotFoundError as exc:
            print(missing_file_message(exc), file=sys.stderr)
            return 2
        except ModuleNotFoundError as exc:
            # An optional extra reached for and absent. Caught here rather
            # than at each lazy import, because there are a dozen of those
            # and one of them will always be the one that was missed.
            from .doctor import missing_extra_message
            print(missing_extra_message(exc), file=sys.stderr)
            return 2
    return record_run(args, argv if argv is not None else sys.argv[1:])
