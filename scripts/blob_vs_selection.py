# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Does delivering context PER QUESTION beat delivering all of it at once?

    make blob-vs-selection WORK=/path/to/work PROJECT=your-project
    make blob-vs-selection WORK=... PROJECT=... ARGS=--estimate-only

This is the one experiment that can show a catalog beating a file. For
identical content that fits in a request the two are the same bytes and
therefore the same accuracy -- measured 2026-09-13, a catalog round trip read
back `d07c1f37980d`, the hash it was given. So the only mechanism left is
SELECTION: a catalog can return what this question needs, where a file sends
everything.

    without-context arm   blob.json      every sentence for the database
    with-context arm      evidence.json  only this question's own sentence

The selection arm is an ORACLE retriever: each question gets exactly the
right sentence. That is the ceiling no real retriever can beat, so a null
result here kills the mechanism and a positive one prices what a retriever
could earn. Both arms contain each question's own evidence, so their leak
status is identical and the gap is not one arm knowing more.

Run `--estimate-only` first. It is free and it prints the price.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

BLOB = "blob.json"
SELECTION = "evidence.json"


def _run(command: list[str], log: pathlib.Path) -> int:
    """One stage, appended to the log as it happens.

    Streamed to a file rather than captured, because a captured pipe buffers
    until the process ends and a long generate then shows nothing for
    minutes.
    """
    with log.open("a") as handle:
        handle.write("\n$ " + " ".join(command) + "\n")
        handle.flush()
        return subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT,
                              text=True).returncode


def stage_paths(out: pathlib.Path, database: str) -> dict[str, pathlib.Path]:
    return {
        "blob_csv": out / f"{database}-blob.csv",
        "selection_csv": out / f"{database}-selection.csv",
        "blob_report": out / f"{database}-blob.json",
        "selection_report": out / f"{database}-selection.json",
        "ablate": out / f"{database}-ablate.json",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work", required=True, metavar="DIR",
                        help="the work/ directory prepare_enterprise.py wrote")
    parser.add_argument("--databases", required=True, metavar="DIR",
                        help="the databases/ directory beside it")
    parser.add_argument("--out", required=True, metavar="DIR")
    parser.add_argument("--only", nargs="+", default=None, metavar="DB",
                        help="these databases only; default every one with a blob")
    parser.add_argument("--project", required=True, help="Vertex project")
    parser.add_argument("--reps", type=int, default=3,
                        help="temperature 0 is not reproducible, so three")
    parser.add_argument("--estimate-only", action="store_true",
                        help="price every arm and make no model call")
    parser.add_argument("--budget", default=None,
                        help="refuse to start an arm above this, in USD")
    parser.add_argument("--pool", action="store_true",
                        help="merge every database's reports into one paired "
                             "pair and ablate once. The headline is the pooled "
                             "number: three databases each too small to resolve "
                             "a few points will each print NOT PROVEN while the "
                             "pool can still say something")
    parser.add_argument("--score-only", action="store_true",
                        help="skip generation and score the rep files already "
                             "on disk. Generation is the part that costs "
                             "money, so a mistake downstream of it must never "
                             "mean paying for the answers twice")
    args = parser.parse_args()

    work = pathlib.Path(args.work)
    databases = pathlib.Path(args.databases)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = out / "run.log"

    names = args.only or sorted(
        p.name for p in work.iterdir() if (p / BLOB).is_file())
    if not names:
        raise SystemExit(f"no {BLOB} under {work}; run scripts/blob_arm.py first")

    print(f"{len(names)} database(s): {', '.join(names)}")
    print(f"log: {log}   (tail it; do not pipe it)")

    failed = []
    for database in names:
        paths = stage_paths(out, database)
        clean = out / f"{database}-clean.csv"
        schema = out / f"{database}-schema.txt"
        db_file = databases / database / f"{database}.sqlite"

        if not db_file.is_file():
            print(f"  {database:24} SKIPPED: no {db_file}")
            failed.append(database)
            continue

        # gate first, and score only the clean golds: a MISSING_PARAMETER gold
        # is wrong in BOTH arms and only dilutes the lift, which the gate says
        # in those words.
        if not clean.is_file():
            _run(["uv", "run", "contextlift", "gate",
                  "--golds", str(work / database / "golds.csv"),
                  "--db", str(db_file), "--clean-out", str(clean)], log)
        if not schema.is_file():
            _run(["uv", "run", "contextlift", "schema",
                  "--db", str(db_file), "--out", str(schema)], log)

        arms = [] if args.score_only else [("blob", BLOB),
                                           ("selection", SELECTION)]
        for arm, source in arms:
            command = ["uv", "run", "contextlift", "generate",
                       "--questions", str(clean), "--schema", str(schema),
                       "--context-per-question", str(work / database / source),
                       "--out", str(paths[f"{arm}_csv"]),
                       "--reps", str(args.reps), "--project", args.project]
            if args.estimate_only:
                command += ["--estimate-only", "--json"]
            else:
                # `generate` refuses to spend with no controlling terminal
                # and no --yes, which is what a host agent, a cron job and a
                # CI runner all are. The budget is the written consent, so
                # --yes without one is not accepted and should not be.
                if not args.budget:
                    raise SystemExit(
                        "a real run needs --budget: this is not a terminal, "
                        "so the price has to be agreed in writing")
                command += ["--budget", args.budget, "--yes"]
            code = _run(command, log)
            print(f"  {database:24} {arm:10} generate exit {code}")
            if code:
                failed.append(f"{database}/{arm}")

        if args.estimate_only:
            continue

        # `--reps N` writes X_rep1.csv .. X_repN.csv and never X.csv, so each
        # repetition is scored into its own report and `ablate` is given all
        # of them. Three answers to one question are not three independent
        # facts, which is why ablate takes the reps rather than a mean.
        reports: dict[str, list[str]] = {"blob": [], "selection": []}
        for arm in ("blob", "selection"):
            for rep in range(1, args.reps + 1):
                predictions = out / f"{database}-{arm}_rep{rep}.csv"
                if not predictions.is_file():
                    print(f"  {database:24} {arm:10} rep{rep} MISSING")
                    failed.append(f"{database}/{arm}/rep{rep}")
                    continue
                report = out / f"{database}-{arm}_rep{rep}.json"
                if (report.is_file()
                        and report.stat().st_mtime >= predictions.stat().st_mtime):
                    # scoring a 168-question arm with no row cap takes
                    # minutes, so an up-to-date report is left alone
                    reports[arm].append(str(report))
                    continue
                # no --golds: `generate --questions` says the gold "rides
                # along, never sent", so the predictions CSV carries it
                code = _run(["uv", "run", "contextlift", "score",
                             "--predictions", str(predictions),
                             "--db", str(db_file),
                             "--save-report", str(report)], log)
                if code:
                    failed.append(f"{database}/{arm}/rep{rep}/score")
                else:
                    reports[arm].append(str(report))
            print(f"  {database:24} {arm:10} scored {len(reports[arm])}"
                  f"/{args.reps} rep(s)")

        if not (reports["blob"] and reports["selection"]):
            print(f"  {database:24} {'ablate':10} skipped: a side has no report")
            continue

        # blob is the BASELINE and selection is the treatment, so a positive
        # lift means selection beat the blob. That is the question.
        command = ["uv", "run", "contextlift", "ablate"]
        for report in reports["blob"]:
            command += ["--without-report", report]
        for report in reports["selection"]:
            command += ["--with-report", report]
        command += ["--db", str(db_file), "--show"]
        code = _run(command, log)
        print(f"  {database:24} {'ablate':10} exit {code}")

    if args.pool and not args.estimate_only:
        # Question ids are unique across BIRD databases, so merging is
        # concatenation. Done here rather than by hand because the pooled
        # figure is the one that gets quoted.
        merged = {}
        for arm in ("blob", "selection"):
            for rep in range(1, args.reps + 1):
                rows, seen = [], set()
                for database in names:
                    path = out / f"{database}-{arm}_rep{rep}.json"
                    if not path.is_file():
                        continue
                    for row in json.loads(path.read_text())["results"]:
                        if row["id"] in seen:
                            raise SystemExit(
                                f"id {row['id']} is in two databases, so the "
                                "pool would count it twice")
                        seen.add(row["id"])
                        rows.append(row)
                target = out / f"pooled-{arm}_rep{rep}.json"
                target.write_text(json.dumps(
                    {"n_total": len(rows), "results": rows}, indent=1) + "\n")
                merged.setdefault(arm, []).append(str(target))
                print(f"  pooled {arm:10} rep{rep}: {len(rows)} questions")

        command = ["uv", "run", "contextlift", "ablate"]
        for report in merged["blob"]:
            command += ["--without-report", report]
        for report in merged["selection"]:
            command += ["--with-report", report]
        # --db is required but unused when both sides are pre-scored reports:
        # nothing is executed, the comparison is arithmetic on the verdicts.
        command += ["--db", str(databases / names[0] / f"{names[0]}.sqlite")]
        print("\n=== POOLED ===")
        code = subprocess.run(command).returncode
        print(f"pooled ablate exit {code}")

    if args.estimate_only:
        total = 0.0
        for line in log.read_text().splitlines():
            if '"estimated_cost_usd"' in line:
                total += float(line.split(":")[1].strip().rstrip(","))
        print(f"\nestimated total: ${total:.2f} for {len(names)} database(s) "
              f"x 2 arms x {args.reps} reps")

    if failed:
        print(f"\nfailed: {', '.join(failed)}")
        return 1
    print(f"\nall stages exited 0. Reports in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
