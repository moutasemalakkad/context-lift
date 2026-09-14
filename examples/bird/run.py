# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The whole loop on public data — offline, no API key, no network.

Real BIRD databases, real BIRD gold SQL, and REAL model output: the predictions
were generated once by gemini-2.5-flash (see run_full_dev.py) and checked in, so
this runs offline and prints the same numbers on every machine while still
measuring something nobody constructed.

Three arms, and the middle one is the method rather than a demo:

  round 0  no business context — schema and question only
  round 1  business rules added ONLY for the largest triage bucket, which is
           what the loop tells you to curate first
  round 2  business rules for every question

The rules are BIRD's own `evidence` strings — human-written metric definitions
like "Free meal rate = Free Meal Count (K-12) / Enrollment (K-12)". Nothing here
is invented.

Usage: python run.py
"""
from __future__ import annotations

import collections
import csv
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from context_lift.ablate import ablate, climb  # noqa: E402
from context_lift.executor import SQLiteExecutor  # noqa: E402
from context_lift.gate import gate, summarize  # noqa: E402
from context_lift.harness import EvalReport, evaluate  # noqa: E402
from context_lift.metrics import proportion  # noqa: E402
from context_lift.triage import triage  # noqa: E402

ROUNDS = [
    ("round 0: no context", "predictions_round0_no_context.csv"),
    ("round 1: top bucket", "predictions_round1_top_bucket.csv"),
    ("round 2: all rules", "predictions_round2_all_rules.csv"),
]


def load(name: str) -> list[dict]:
    with (HERE / name).open(newline="") as handle:
        return list(csv.DictReader(handle))


def score(rows: list[dict]) -> EvalReport:
    """Score across several databases, pooled into one report.

    Each BIRD database needs its own executor, so the arms are scored per
    database and the per-question outcomes pooled. Pooling at the question level
    rather than averaging per-database rates keeps the paired test valid.
    """
    by_db: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        by_db[row["db_id"]].append(row)

    results = []
    for db_id, db_rows in sorted(by_db.items()):
        executor = SQLiteExecutor(str(HERE / "db" / f"{db_id}.sqlite"))
        results.extend(evaluate(executor, [
            (r["id"], r["question"], r["gold_sql"], r["pred_sql"]) for r in db_rows
        ]).results)

    scored = [r for r in results if r.execution_correct is not None]
    rate = proportion(sum(1 for r in scored if r.execution_correct), len(scored))
    return EvalReport(results, len(results), len(scored),
                      len(results) - len(scored), rate, rate)


def rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> None:
    arms = {label: load(name) for label, name in ROUNDS}
    baseline_rows = arms[ROUNDS[0][0]]
    databases = sorted({r["db_id"] for r in baseline_rows})
    print(f"{len(baseline_rows)} questions across {len(databases)} BIRD databases: "
          f"{', '.join(databases)}")

    rule("STAGE 1 of 5  gate — is the answer key sound?")
    print("A broken gold silently corrupts every score computed against it. A 2026\n"
          "study found a 52.8% annotation error rate in BIRD Mini-Dev, so this is\n"
          "not a formality even on a published benchmark.\n")
    clean = 0
    for db_id in databases:
        executor = SQLiteExecutor(str(HERE / "db" / f"{db_id}.sqlite"))
        findings = gate([(r["id"], r["gold_sql"]) for r in baseline_rows
                         if r["db_id"] == db_id], executor)
        clean += sum(1 for f in findings if f.ok)
        print(f"  {db_id:<20} {len(findings):>3} golds   "
              f"{summarize(findings) or 'clean'}")
    print(f"\n  {clean}/{len(baseline_rows)} golds usable")

    reports = {label: score(rows) for label, rows in arms.items()}
    base = reports[ROUNDS[0][0]]

    rule("STAGE 2 of 5  score — the baseline, before any business context")
    print("Run this FIRST. Once the context exists you cannot recover the number\n"
          "that shows what it was worth.\n")
    print(f"  execution accuracy: {base.execution.point:.1%} "
          f"[{base.execution.low:.1%}, {base.execution.high:.1%}]  "
          f"({base.execution.k}/{base.execution.n})")

    rule("STAGE 3 of 5  triage — what KIND of wrong?")
    print("A rate says how much is broken. Triage says what to fix.\n")
    print(triage(base, adjusted=False).summary())
    print("\nRound 1 below curates ONLY that largest bucket. That is the loop:")
    print("measure, find the biggest shape, write the rules for it, re-measure.")

    rule("STAGE 4 of 5  climb — what did each round buy?")
    print(climb([(label, reports[label]) for label, _ in ROUNDS]).summary())

    rule("STAGE 5 of 5  ablate — what the whole exercise was worth")
    print(ablate(base, reports[ROUNDS[-1][0]]).summary())

    print("\nremaining failures:")
    print(triage(reports[ROUNDS[-1][0]], adjusted=False).summary())

    print(f"\n{'=' * 72}")
    print("Real model output, real BIRD gold, scored offline. The predictions were")
    print("generated once and checked in so this prints the same everywhere;")
    print("run_full_dev.py regenerates them and covers all 11 databases.")


if __name__ == "__main__":
    main()
