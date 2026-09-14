# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The context ablation over the whole BIRD dev set, both arms, real model.

The committed example runs on one small database with simulated predictions so
it is offline and reproducible. This is the other half: every BIRD dev database,
real model output, enough questions that the result can actually be believed.

One thing varies between arms — whether the prompt carries BIRD's `evidence`
string, the human-written business rule for that question ("Free meal rate =
Free Meal Count (K-12) / Enrollment (K-12)"). Everything else is identical.

Question selection is deliberate and worth understanding. Only questions whose
gold returns 1-2 columns are used. On a "give me its characteristics" question
the gold author picked an arbitrary 9 or 16 columns, and a model with the RIGHT
answer is marked wrong for choosing differently — measured on a first run, that
was 5 of 12 failures and it swamped the context signal.

Needs the BIRD dev download (~1.4 GB extracted). Nothing here touches a
customer project; point --project at one you own.

Usage:
  CLOUDSDK_CONFIG=~/.gcloud/google python run_full_dev.py \\
      --bird-root /path/to/dev_databases --questions /path/to/questions.json \\
      --project YOUR_PROJECT [--limit-per-db 200] [--workers 8]
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import sqlite3
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

HERE = pathlib.Path(__file__).resolve().parent

PROMPT = """You write SQLite SQL against this database.

{schema}
{evidence}
Question: {question}

Reply with ONLY the SQL, no explanation and no markdown fence."""

_print_lock = threading.Lock()


def connect(path: pathlib.Path) -> sqlite3.Connection:
    """Read-only connection that survives the non-UTF8 bytes in some BIRD rows."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    connection.text_factory = lambda b: b.decode(errors="replace")
    return connection


def schema_text(connection: sqlite3.Connection) -> str:
    return "\n\n".join(
        r[0] for r in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL")
        if r[0]
    )


def clean(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return text.strip().rstrip(";").strip()


# Evidence is only load-bearing if it states a formula the gold actually
# computes. "Charter schools refers to Charter School (Y/N) = 1" is recoverable
# from the column names; "Free meal rate = Free Meal Count / Enrollment" is not.
_FORMULA = re.compile(r"=.*[/*+-]|divide|percentage|ratio|rate", re.I)


def usable_questions(connection, questions, limit):
    """Questions where the ablation can actually measure something.

    Three filters, each removing a way the measurement goes wrong:
      1-column gold   BIRD's own convention (81.5% of dev). A wider gold means
                      the author picked an arbitrary column set and a model with
                      the right answer is marked wrong for choosing differently.
      formula evidence  the business rule has to be the only source of the rule,
                      or the no-context arm gets it free from the schema.
      gold divides    the formula must appear in the gold, not just the prose.
    """
    keep = []
    for question in questions:
        if not _FORMULA.search(question["evidence"]):
            continue
        if not re.search(r"[/*]|CAST", question["SQL"], re.I):
            continue
        try:
            rows = connection.execute(question["SQL"]).fetchall()
        except Exception:
            continue
        if rows and len(rows) <= 50 and len(rows[0]) == 1:
            keep.append(question)
        if limit and len(keep) >= limit:
            break
    return keep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bird-root", required=True, help="dev_databases/ directory")
    ap.add_argument("--questions", required=True, help="BIRD dev questions.json")
    ap.add_argument("--project", required=True, help="a project YOU own")
    ap.add_argument("--location", default="us-central1")
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--limit-per-db", type=int, default=0, help="0 = no cap")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default="full_dev")
    args = ap.parse_args()

    # Guard against billing someone else's project by accident. The prefixes
    # are configuration, not source: a guard that hardcodes the name of the
    # project it protects publishes that name to everyone who reads the repo.
    # The same rule, shared by every contextlift command, is
    # backends.refuse_forbidden_project; this script calls the SDK directly,
    # so it repeats the check rather than import the package for one line.
    forbidden = [p for p in os.environ.get("CONTEXTLIFT_FORBIDDEN_PROJECTS", "").split(",") if p]
    if any(args.project.startswith(prefix) for prefix in forbidden):
        sys.exit(f"refusing: {args.project} matches CONTEXTLIFT_FORBIDDEN_PROJECTS. "
                 "Use a project you own.")

    from google import genai
    from google.genai import types

    client = genai.Client(vertexai=True, project=args.project, location=args.location)
    config = types.GenerateContentConfig(
        temperature=0,
        max_output_tokens=2048,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )

    root = pathlib.Path(args.bird_root)
    all_questions = [q for q in json.load(open(args.questions))
                     if q.get("evidence", "").strip()]

    jobs = []          # (db_id, qid, question, gold_sql, evidence, schema)
    for db_id in sorted({q["db_id"] for q in all_questions}):
        path = root / db_id / f"{db_id}.sqlite"
        if not path.exists():
            print(f"  skip {db_id}: no database at {path}")
            continue
        connection = connect(path)
        schema = schema_text(connection)
        keep = usable_questions(
            connection, [q for q in all_questions if q["db_id"] == db_id],
            args.limit_per_db)
        connection.close()
        for index, question in enumerate(keep):
            jobs.append((db_id, f"{db_id}_{index:03d}", question["question"],
                         question["SQL"], question["evidence"], schema))
        print(f"  {db_id:<24} {len(keep):>4} questions")

    print(f"\n{len(jobs)} questions x 2 arms = {len(jobs) * 2} model calls\n")

    done = [0]

    def ask(job, arm):
        db_id, qid, question, gold_sql, evidence, schema = job
        hint = f"\nBusiness rules you must apply:\n{evidence}\n" if arm == "with_context" else ""
        try:
            response = client.models.generate_content(
                model=args.model,
                contents=PROMPT.format(schema=schema, evidence=hint, question=question),
                config=config)
            sql = clean(response.text)
        except Exception as error:  # noqa: BLE001 -- one failure must not kill the run
            sql = ""
            with _print_lock:
                print(f"  [{qid}/{arm}] ERROR {str(error)[:70]}")
        return {"db_id": db_id, "id": qid, "question": question,
                "gold_sql": gold_sql, "pred_sql": sql}

    def run_arm(arm):
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(lambda j: ask(j, arm), jobs))
        done[0] += 1
        print(f"  arm {arm} finished ({done[0]}/2)")
        return results

    out_dir = HERE / args.out
    out_dir.mkdir(exist_ok=True)
    for arm in ("no_context", "with_context"):
        rows = run_arm(arm)
        path = out_dir / f"{arm}.csv"
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["db_id", "id", "question", "gold_sql", "pred_sql"])
            writer.writeheader()
            writer.writerows(rows)
        got = sum(1 for r in rows if r["pred_sql"])
        print(f"  wrote {path.name}: {got}/{len(rows)} produced SQL")


if __name__ == "__main__":
    main()
