# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The perturbation suite on BIRD, where it can resolve its own control.

    python scripts/perturb_bird.py --work DIR --databases DIR --project P \
        [--estimate-only] [--budget 12 --yes]

WHY HERE AND NOT ON THELOOK. The thelook suite ran on 2026-09-13 and its
positive control measured -4.1 points on 74 working questions, which resolve a
change above about 14 (`power.smallest_resolvable(74)`). A suite that cannot
resolve its own control cannot resolve anything smaller, and six more arms
there would have bought six NOT PROVEN verdicts. BIRD pooled is 450 questions
and resolves about 4.7 points.

It also runs on a backend with PUBLISHED PRICES. The Conversational Analytics
API bills "data tokens" and its page does not define one, so the thelook run
could be counted in calls and never in dollars.

WHAT AN ARM IS. BIRD context is one evidence sentence per question, and the
`blob` arm hands every sentence for the database to every question -- measured
at 42.9% against 41.3% for perfect per-question selection, so the blob is the
better baseline as well as the simpler one. A perturbation transforms that
blob text and nothing else: same questions, same schema, same model.

THE POSITIVE CONTROL DELETES 98% OF THE TEXT, on purpose. BIRD evidence is
measured at +30.8 points [11.8, 49.8]. If removing it does not register as a
large loss, the harness is broken and every other number here is worthless.
That is a coarser control than one realistic defect, and it is the right first
check for exactly that reason.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

DATABASES = ("toxicology", "thrombosis_prediction", "card_games")


def _run(command: list[str], log: pathlib.Path) -> int:
    """Run one command, streaming into `log` so it can be tailed live."""
    with log.open("a") as handle:
        handle.write("\n$ " + " ".join(str(c) for c in command) + "\n")
        handle.flush()
        return subprocess.run(command, stdout=handle,
                              stderr=subprocess.STDOUT).returncode


def blob_text(work: pathlib.Path, database: str) -> str:
    """The blob every question in this database is handed.

    `blob.json` maps question id to context, and every value is the same
    text, which is what makes it a blob. Read one and check the rest match,
    because a file where they differ is a per-question arm wearing a blob's
    name and the comparison would be against something else entirely.
    """
    loaded = json.loads((work / database / "blob.json").read_text())
    if isinstance(loaded, str):
        return loaded
    values = set(loaded.values())
    if len(values) != 1:
        raise SystemExit(
            f"{database}/blob.json holds {len(values)} different contexts, so "
            f"it is not a blob. A perturbation of it would not be comparable "
            f"to the measured blob arm.")
    return next(iter(values))


def write_arm(work: pathlib.Path, database: str, name: str, text: str) -> pathlib.Path:
    """One perturbed context file, same shape as blob.json."""
    keys = json.loads((work / database / "blob.json").read_text()).keys()
    out = work / database / f"blob-{name}.json"
    out.write_text(json.dumps({key: text for key in keys}, indent=1) + "\n")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work", required=True, metavar="DIR")
    parser.add_argument("--databases", required=True, metavar="DIR")
    parser.add_argument("--project", required=True)
    parser.add_argument("--model", default="gemini-2.5-flash")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--only", default=None, metavar="NAMES")
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument("--score-only", action="store_true",
                        help="skip generating; score and ablate what is there")
    parser.add_argument("--budget", default=None)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--row-cap", type=int, default=100000)
    parser.add_argument("--log", default="logs/perturb-bird.log")
    parser.add_argument("--out", default="docs/evidence/perturbation-bird.txt")
    args = parser.parse_args()

    from context_lift.perturb import BIRD_SUITE, bird_by_name

    suite = BIRD_SUITE
    if args.only:
        suite = tuple(bird_by_name(n.strip())
                      for n in args.only.split(",") if n.strip())

    work = pathlib.Path(args.work)
    databases = pathlib.Path(args.databases)
    log = pathlib.Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)

    arms = ["clean"] + [p.name for p in suite]
    print(f"  {len(arms)} arm(s): clean plus {len(suite)} defect(s)")
    print(f"  {len(DATABASES)} database(s), {args.reps} rep(s)")

    # Write every perturbed context first, so a transform that changes
    # nothing fails before a single call is paid for.
    for database in DATABASES:
        clean = blob_text(work, database)
        write_arm(work, database, "clean", clean)
        for perturbation in suite:
            write_arm(work, database, perturbation.name,
                      perturbation.apply(clean))
    print("  every arm's context written; no transform was a no-op")

    failed = []
    for database in DATABASES:
        questions = work / database / "clean_questions.csv"
        if not questions.is_file():
            questions = pathlib.Path("/tmp/blobrun") / f"{database}-clean.csv"
        schema = pathlib.Path("/tmp/blobrun") / f"{database}-schema.txt"
        for missing, what in ((questions, "gate-cleaned questions"),
                              (schema, "schema text")):
            if not missing.is_file():
                raise SystemExit(f"no {what} at {missing}")

        for arm in arms:
            out = work / database / f"answers-{arm}.csv"
            if args.score_only:
                continue
            command = ["uv", "run", "contextlift", "generate",
                       "--questions", str(questions), "--schema", str(schema),
                       "--context-per-question",
                       str(work / database / f"blob-{arm}.json"),
                       "--out", str(out), "--reps", str(args.reps),
                       "--model", args.model, "--project", args.project]
            if args.estimate_only:
                command += ["--estimate-only", "--json"]
            else:
                if not args.budget:
                    raise SystemExit(
                        "a real run needs --budget: this is not a terminal, "
                        "so the price has to be agreed in writing")
                command += ["--budget", str(args.budget), "--yes"]
            code = _run(command, log)
            print(f"  {database:24} {arm:38} exit {code}")
            if code:
                failed.append(f"{database}/{arm}")

    if args.estimate_only:
        print(f"\n  --estimate-only: nothing was asked. Read {log} for the "
              f"per-arm token counts and price")
        return 0
    if failed:
        print(f"\n  {len(failed)} arm(s) failed: {', '.join(failed)}")
        return 1

    # Score, then pair each defect against the clean arm on the same
    # questions. Pooled across the three databases, because 450 questions
    # resolve about 4.7 points and 139 resolve about 8.5.
    lines = []
    for database in DATABASES:
        db_file = databases / database / f"{database}.sqlite"
        for arm in arms:
            table = work / database / f"answers-{arm}"
            source = f"{table}_rep1.csv" if args.reps > 1 else f"{table}.csv"
            _run(["uv", "run", "contextlift", "score",
                  "--predictions", source, "--db", str(db_file),
                  "--timeout", str(args.timeout),
                  "--row-cap", str(args.row_cap),
                  "--save-report", f"{table}.json"], log)

    for perturbation in suite:
        command = ["uv", "run", "contextlift", "ablate"]
        for database in DATABASES:
            command += ["--without-report",
                        str(work / database / "answers-clean.json"),
                        "--with-report",
                        str(work / database / f"answers-{perturbation.name}.json")]
        command += ["--db", str(databases / DATABASES[0] /
                                f"{DATABASES[0]}.sqlite")]
        out = subprocess.run(command, capture_output=True, text=True)
        lines.append((perturbation, out.stdout))
        print(f"\n  {perturbation.name}")
        for line in out.stdout.splitlines():
            if "lift" in line or "fixed" in line or "p =" in line:
                print(f"    {line.strip()}")

    text = _report(lines, args)
    pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.out).write_text(text.replace(args.project, "<project>"))
    print(f"\n  wrote {args.out}")
    return 0


def _report(lines, args) -> str:
    out = ["The perturbation suite on BIRD, 2026-09-13",
           "",
           f"{args.model}, {args.reps} rep(s), 3 databases, 450 gate-cleaned "
           "questions.",
           "Arms differ ONLY in the blob text: same questions, same schema, "
           "same model.",
           "",
           "without-context arm = the clean blob (every evidence sentence)",
           "with-context arm    = the blob with one defect injected",
           "So a NEGATIVE lift is the cost of that defect.",
           ""]
    for perturbation, body in lines:
        out += [f"=================== {perturbation.name} ===================",
                f"breaks: {perturbation.breaks}",
                perturbation.note, "", body, ""]
    out += ["450 questions resolve a change above about 4.7 points "
            "(power.smallest_resolvable(450)).",
            "A defect smaller than that reads NOT PROVEN, which is not the "
            "same as costing nothing.",
            "",
            "This measures ONE model on THREE BIRD databases. It does not "
            "generalise to an estate."]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
