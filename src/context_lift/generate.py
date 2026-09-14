# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Produce predictions: the arm-generation half of every ablation.

Until now the framework scored predictions but could not produce them, so
every user rewrote the same model-calling loop - prompt assembly, fence
stripping, retries - and the validity of `ablate` rested on hand-assembled
CSVs really differing by only the context block. Measured cost of that gap
on one trial: a fence-stripping bug in user glue broke 20 of 72 predictions
and a paid run had to be repeated.

This module owns that loop. One prompt template; two arms built with the
same call differ ONLY in `context`, which is the property the paired test
needs. A backend is `Callable[[str], str]` - the same seam the judge uses
(backends.py) - so tests run on echo_backend with no network and the cloud
stays behind the [gemini] extra.
"""
from __future__ import annotations

import csv
import json
import pathlib
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .backends import CredentialsMissing
from .executor import Executor

PROMPT_TEMPLATE = """You write SQL for the {dialect} dialect against this database.

Return ONE {dialect} SELECT statement and nothing else. No prose, no markdown
fence, no explanation. The statement must run as written.

SCHEMA
{schema}
{context}
QUESTION
{question}

SQL:"""

RETRY_TEMPLATE = """

Your previous attempt was:
{sql}

Problem: {problem}.
Write a corrected {dialect} SELECT statement. Same rules: one statement only.

SQL:"""

# Match a whole fenced block and keep its body. The language tag must match
# as a whole word: an earlier prefix-match stripped ```sql off ```sqlite and
# left the literal text "ite" in front of 20 of 72 predictions.
_FENCE = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)```", re.DOTALL)


def clean_sql(text: str) -> str:
    """Pull the SQL statement out of a model reply, verbatim.

    Handles the reply shapes hosted models actually produce: fenced with any
    language tag, bare, or followed by prose (cut at the last semicolon).
    """
    text = (text or "").strip()
    match = _FENCE.search(text)
    if match:
        text = match.group(1)
    text = text.strip()
    if ";" in text:
        text = text[: text.rindex(";") + 1]
    return text.strip()


def build_prompt(
    question: str,
    schema: str,
    context: str = "",
    dialect: str = "SQLite",
) -> str:
    """One prompt for one question. Everything an arm varies flows through
    `context`; schema and question formatting stay identical across arms."""
    block = f"\n{context}\n" if context.strip() else ""
    return PROMPT_TEMPLATE.format(
        dialect=dialect, schema=schema, context=block, question=question
    )


def estimate_run(
    rows: list[tuple[str, str, str]],
    schema: str,
    count_tokens: Callable[[str], int],
    context: str = "",
    per_question_context: dict[str, str] | None = None,
    dialect: str = "SQLite",
    reps: int = 1,
    output_tokens_per_call: int = 80,
    workers: int = 4,
) -> dict:
    """What a run will send, counted on the real prompts before any paid call.

    Prompt tokens are exact (the same prompts generate_predictions builds,
    counted by the backend); output tokens are a per-call figure the caller
    supplies, measured from an earlier run when one exists.
    """
    per_question_context = per_question_context or {}
    prompts = []
    for question_id, question, _gold in rows:
        extra = per_question_context.get(question_id, "")
        merged = context + ("\n" + extra if extra else "")
        prompts.append(build_prompt(question, schema, merged, dialect))
    # A token count is a free measurement taken BEFORE the paid work, so a
    # network hiccup in it must not end the run it is pricing. One did, twice,
    # with 200 lines of traceback, on the step the loop labels "there is no
    # undo". A credentials problem is different and still fails fast: the first
    # prompt is counted alone so it raises once rather than once per thread.
    estimated = 0

    def counted(prompt: str) -> int:
        nonlocal estimated
        try:
            return count_tokens(prompt)
        except CredentialsMissing:
            raise
        except Exception:                       # noqa: BLE001 - any transport error
            estimated += 1
            # four characters to a token is the usual rule of thumb, and this
            # only has to be close enough to price a step
            return len(prompt) // 4

    counts = [counted(prompts[0])] if prompts else []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        counts += list(pool.map(counted, prompts[1:]))
    calls = len(rows) * reps
    return {
        "questions": len(rows),
        "reps": reps,
        "calls": calls,
        "prompt_tokens": sum(counts) * reps,
        "prompt_tokens_per_call": round(sum(counts) / len(counts)) if counts else 0,
        # how many prompts the counter could not reach, so a caller can say the
        # price is an approximation rather than printing it as measured
        "prompts_estimated": estimated,
        "output_tokens": output_tokens_per_call * calls,
        "output_tokens_per_call": output_tokens_per_call,
    }


def runner_config_in(text: str) -> dict | None:
    """The `config` object a runner may print with its answer: model settings,
    flags, anything that makes two runs of the same commit different agents.
    Recorded once per generate run and diffed between arms at the verdict."""
    stripped = (text or "").strip()
    if not stripped.startswith("{"):
        return None
    try:
        data = json.loads(stripped)
    except ValueError:
        return None
    config = data.get("config") if isinstance(data, dict) else None
    return config if isinstance(config, dict) and config else None


def parse_runner_output(text: str) -> tuple[str, str, str]:
    """(sql, routed table, note) from what a runner printed: a JSON object
    with `sql` and optionally `table`, `abstained` and `note`, or the SQL as
    plain text. An abstention is a deliberate empty answer, a router saying
    no table fits, and it is a correct behaviour on an out-of-scope
    question; the note keeps it apart from a crash."""
    stripped = (text or "").strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, dict):
            note = str(data.get("note") or "").strip()
            if data.get("abstained"):
                note = "abstained" + (f": {note}" if note else "")
            return (clean_sql(str(data.get("sql") or "")),
                    str(data.get("table") or "").strip(), note)
    return clean_sql(stripped), "", ""


def problem_with(executor: Executor, sql: str) -> str | None:
    """What is wrong with a draft, or None: a failed check (free), a failed
    run, or a run that returned nothing. The check comes first because on a
    warehouse one bad draft can scan gigabytes to report a missing column,
    and the dry run reports the same message for nothing."""
    check = getattr(executor, "check", None)
    if check is not None:
        error = check(sql)
        if error:
            return f"the query failed: {error}"
    result = executor.execute(sql)
    if not result.ok:
        return f"the query failed: {result.error}"
    if not result.rows:
        return "the query ran but returned zero rows"
    return None


def generate_predictions(
    rows: list[tuple[str, str, str]],
    schema: str,
    backend: Callable[[str], str],
    context: str = "",
    per_question_context: dict[str, str] | None = None,
    dialect: str = "SQLite",
    retry_executor: Executor | None = None,
    max_attempts: int = 3,
    workers: int = 1,
    progress: Callable[[int, int], None] | None = None,
    rep: int = 1,
    routed: dict[str, str] | None = None,
    notes: dict[str, str] | None = None,
    stats: dict | None = None,
) -> list[tuple[str, str, str, str]]:
    """Generate one prediction per (id, question, gold_sql) row.

    `progress(done, total)` is called after each answer, from worker threads,
    so a long paid run can show movement instead of silence.

    Returns rows shaped for the scoring CSV: (id, question, gold_sql,
    pred_sql). The gold rides along untouched so the output feeds `score`
    directly; it is never shown to the backend.

    retry_executor turns on the check-and-feed-back-once loop: the draft is
    checked without running it (a warehouse dry run, SQLite's EXPLAIN), a
    clean draft runs once so an empty result counts too, and on either
    problem the backend gets one chance to correct it: a prompt backend sees
    the error appended to its prompt, a command runner is called again with
    attempt, previous_sql and error added to its JSON. Pass the same
    timeout-bounded executor the scorer uses, so a runaway draft cannot hang
    generation either. `stats["retried"]` counts the second chances.

    A backend marked `structured` (command_backend) gets a JSON object of the
    pieces instead of a prompt; `rep` rides in it, and a routed table it
    returns lands in `routed` keyed by question id.
    """
    per_question_context = per_question_context or {}

    def one(row: tuple[str, str, str]) -> tuple[str, str, str, str]:
        question_id, question, gold_sql = row
        extra = per_question_context.get(question_id, "")
        merged = context + ("\n" + extra if extra else "")
        structured = getattr(backend, "structured", False)
        table = note = ""
        prompt = payload = None
        if structured:
            # a program, not a prompt: it gets the pieces and decides what to
            # read; its answer may carry the routed table beside the SQL
            payload = {"id": question_id, "question": question, "rep": rep,
                       "schema": schema, "context": merged}
            raw = backend(json.dumps(payload))
            sql, table, note = parse_runner_output(raw)
            if stats is not None and "runner_config" not in stats:
                config = runner_config_in(raw)
                if config:
                    stats["runner_config"] = config
        else:
            prompt = build_prompt(question, schema, merged, dialect)
            sql = ""
            for _attempt in range(max_attempts):
                try:
                    sql = clean_sql(backend(prompt))
                    break
                except Exception:  # noqa: BLE001 - a retried transient, not hidden
                    time.sleep(1)
        if sql and retry_executor is not None:
            problem = problem_with(retry_executor, sql)
            if problem is not None:
                if stats is not None:
                    stats["retried"] = stats.get("retried", 0) + 1
                if structured:
                    # the routed table rides back so a runner can stay stateless
                    # and correct the same routing decision, not make a new one
                    second = {**payload, "attempt": 2, "previous_sql": sql, "error": problem,
                              "table": table}
                    corrected, table2, note2 = parse_runner_output(backend(json.dumps(second)))
                    if corrected:
                        sql, table, note = corrected, table2 or table, note2 or note
                else:
                    retry_prompt = prompt + RETRY_TEMPLATE.format(
                        sql=sql, problem=problem, dialect=dialect
                    )
                    try:
                        corrected = clean_sql(backend(retry_prompt))
                        if corrected:
                            sql = corrected
                    except Exception:  # noqa: BLE001
                        pass
        if table and routed is not None:
            routed[question_id] = table
        if note and notes is not None:
            notes[question_id] = note
        return question_id, question, gold_sql, sql

    done = 0
    done_lock = threading.Lock()

    def one_and_report(row: tuple[str, str, str]) -> tuple[str, str, str, str]:
        nonlocal done
        result = one(row)
        if progress is not None:
            with done_lock:
                done += 1
                progress(done, len(rows))
        return result

    if workers <= 1:
        return [one_and_report(r) for r in rows]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one_and_report, rows))


def identical_across_reps(rep_rows: list[list[tuple[str, str, str, str]]]) -> tuple[int, int]:
    """(questions whose answer was identical in every repetition, questions).

    Three repetitions of a deterministic runner are three copies, not three
    samples: one agent pinned temperature and thinking and produced
    byte-identical SQL three times on both arms. A confidence interval over
    copies is a lie, so the count prints where the copies are made."""
    if len(rep_rows) < 2:
        return 0, len(rep_rows[0]) if rep_rows else 0
    by_id: dict[str, set[str]] = {}
    for rows in rep_rows:
        for qid, _, _, sql in rows:
            by_id.setdefault(qid, set()).add(" ".join(sql.split()))
    return sum(1 for answers in by_id.values() if len(answers) == 1), len(by_id)


def write_predictions(
    rows: list[tuple[str, str, str, str]], path: str | pathlib.Path,
    notes: dict[str, str] | None = None,
) -> pathlib.Path:
    """Write generated rows as the CSV every scoring command reads. A `note`
    column is added only when a runner said something about an answer (an
    abstention, mostly); every reader ignores columns it does not know."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)   # arms/base.csv on a fresh workspace
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        if notes:
            writer.writerow(["id", "question", "gold_sql", "pred_sql", "note"])
            writer.writerows([*row, notes.get(row[0], "")] for row in rows)
        else:
            writer.writerow(["id", "question", "gold_sql", "pred_sql"])
            writer.writerows(rows)
    return path
