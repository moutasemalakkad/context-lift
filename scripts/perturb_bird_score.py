#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Score every repetition of every perturbation arm, then pool and compare.

    python scripts/perturb_bird_score.py --work DIR --databases DIR [--reps 3]

FREE. Scoring executes the predicted and reference SQL against local SQLite
files; no model is called and nothing is billed. The answers were paid for
once by scripts/perturb_bird.py and are on disk.

TWO BUGS THIS EXISTS TO FIX, both in the first version of the runner.

`generate --reps 3` writes `X_rep1.csv`, `X_rep2.csv`, `X_rep3.csv` and never
`X.csv`. The runner scored only the file it expected, so 8,100 paid answers
produced one repetition of results and the other two sat unread.

And `ablate` reads multiple `--without-report` files as REPETITIONS of one
question set, not as different databases. Handing it three databases made it
refuse with "rep 1 scored different questions from rep 0", which is correct: a
question id means nothing across databases. So the three are merged into one
report per repetition, with the database name prefixed onto each id, BEFORE
ablate sees them.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess

DATABASES = ("toxicology", "thrombosis_prediction", "card_games")
ARMS = ("clean", "positive-control-drop-value-decoding", "unknown-column",
        "strip-subject", "conflicting-definitions", "irrelevant-context")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work", required=True)
    parser.add_argument("--databases", required=True)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--row-cap", type=int, default=100000)
    parser.add_argument("--skip-scoring", action="store_true")
    args = parser.parse_args()

    work = pathlib.Path(args.work)
    dbs = pathlib.Path(args.databases)

    # 1. score every (database, arm, rep)
    if not args.skip_scoring:
        for database in DATABASES:
            db_file = dbs / database / f"{database}.sqlite"
            for arm in ARMS:
                for rep in range(1, args.reps + 1):
                    answers = work / database / f"answers-{arm}_rep{rep}.csv"
                    report = work / database / f"answers-{arm}_rep{rep}.json"
                    if report.is_file():
                        continue
                    if not answers.is_file():
                        raise SystemExit(f"no answers at {answers}")
                    done = subprocess.run(
                        ["uv", "run", "contextlift", "score",
                         "--predictions", str(answers), "--db", str(db_file),
                         "--timeout", str(args.timeout),
                         "--row-cap", str(args.row_cap),
                         "--save-report", str(report)],
                        capture_output=True, text=True)
                    print(f"  scored {database:24} {arm:38} rep{rep} "
                          f"exit {done.returncode}", flush=True)

    # 2. merge the three databases into one report per (arm, rep)
    for arm in ARMS:
        for rep in range(1, args.reps + 1):
            merged = {"n_total": 0, "results": []}
            for database in DATABASES:
                report = work / database / f"answers-{arm}_rep{rep}.json"
                loaded = json.loads(report.read_text())
                merged["n_total"] += loaded["n_total"]
                for entry in loaded["results"]:
                    # the database prefix is what stops two databases'
                    # question ids colliding into one pooled question
                    merged["results"].append(
                        {**entry, "id": f"{database[:4]}_{entry['id']}"})
            out = work / f"pooled3-{arm}_rep{rep}.json"
            out.write_text(json.dumps(merged))
        ok = sum(1 for r in merged["results"] if r.get("execution_correct"))
        print(f"  {arm:38} rep{args.reps}: {ok:>3} of "
              f"{len(merged['results'])} correct")

    # 3. compare each defect against the clean arm, all repetitions at once
    print("\n  DEGRADATION CURVE, 3 repetitions, pooled\n")
    for arm in ARMS:
        if arm == "clean":
            continue
        command = ["uv", "run", "contextlift", "ablate"]
        for rep in range(1, args.reps + 1):
            command += ["--without-report", str(work / f"pooled3-clean_rep{rep}.json"),
                        "--with-report", str(work / f"pooled3-{arm}_rep{rep}.json")]
        command += ["--db", str(dbs / DATABASES[0] / f"{DATABASES[0]}.sqlite")]
        done = subprocess.run(command, capture_output=True, text=True)
        print(f"=================== {arm} ===================")
        print(done.stdout or done.stderr[-500:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
