# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Plan the perturbation suite, and run its free half.

    python scripts/perturb_suite.py --spec examples/thelook/spec \
        --golds examples/thelook/golds.csv

Prints one line per injected defect: its arm hash, what it breaks, and whether
the linter caught it for nothing. Spends no money and makes no model call.

The paid half -- scoring each damaged arm against the questions -- is deliberately
NOT here. It needs a price check first, and a script that could quietly start
an eight-arm paid run is a script somebody eventually runs by accident.
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--spec", required=True, metavar="DIR")
    parser.add_argument("--golds", default=None, metavar="FILE",
                        help="answer keys, so the linter can say which rules "
                             "no question would reach")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--out", default=None, metavar="FILE",
                        help="write the plan here as well as printing it")
    parser.add_argument("--run", action="store_true",
                        help="PAID. Score every arm against the working "
                             "questions and print the degradation curve")
    parser.add_argument("--db", default=None, help="warehouse, e.g. bq://PROJECT")
    parser.add_argument("--project", default=None,
                        help="the project the chat calls are billed to")
    parser.add_argument("--location", default="us")
    parser.add_argument("--working", default=None, metavar="FILE",
                        help="curate_ids.txt: the working questions. The "
                             "reserved half is never touched")
    parser.add_argument("--only", default=None, metavar="NAMES",
                        help="comma separated perturbation names, to run "
                             "fewer than all of them")
    parser.add_argument("--checkpoint", default=None, metavar="FILE")
    parser.add_argument("--budget", type=float, default=None)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--timeout", type=int, default=None,
                        help="seconds per statement; unset means 60 on "
                             "BigQuery, because predicted SQL is untrusted "
                             "model output and a query that never finishes "
                             "bills while it waits")
    parser.add_argument("--row-cap", type=int, default=None)
    args = parser.parse_args()

    from context_lift.export import build_arms, read_spec
    from context_lift.perturb import inject, plan_text

    spec = read_spec(args.spec)
    golds = []
    questions = 0
    if args.golds:
        rows = list(csv.DictReader(open(args.golds, newline="")))
        golds = [r["gold_sql"] for r in rows if r.get("gold_sql")]
        questions = len(rows)

    _control, clean = build_arms(spec)
    injected = inject(spec, gold_sql=golds or None)
    text = plan_text(injected, clean.arm_id, questions, args.reps)
    print(text)
    if args.out:
        pathlib.Path(args.out).write_text(text + "\n")
        print(f"\n  wrote {args.out}")
    if not args.run:
        return 0
    return _run(args, spec, injected, rows)


def _run(args, spec, injected, gold_rows) -> int:
    """Score every arm against the WORKING questions. Paid.

    THE RESERVED HALF IS NEVER TOUCHED. A perturbation arm is a diagnostic,
    not a reported lift, and the held-out set is read once per round --
    spending that one read on a diagnostic is how a holdout becomes practice
    questions. This is the same rule `classes` follows for the same reason.

    THE ARMS CARRY NO WORKED EXAMPLES, deliberately. Every perturbation here
    breaks a RULE, and the example bank was built from these same working
    questions, so a question the bank answers verbatim cannot move whatever
    the rules say. Dropping examples from every arm equally removes a
    confound rather than adding one, and a first attempt through `round`
    proved the point by failing its own template-twin gate on 20 of 20 items.
    """
    import copy
    from context_lift.ablate import ablate
    from context_lift.confirm import confirm_paid
    from context_lift.export import build_arms
    from context_lift.harness import evaluate
    from context_lift.round import Gold, _ask_arm
    from context_lift.runners.ca_api import CaApiRunner

    if not (args.db and args.project and args.working):
        print("--run needs --db, --project and --working curate_ids.txt")
        return 2
    wanted = {l.strip() for l in open(args.working) if l.strip()}
    golds = [Gold(r["id"], r["question"], r["gold_sql"])
             for r in gold_rows if r["id"] in wanted]
    if not golds:
        print(f"none of the golds are in {args.working}")
        return 2

    chosen = injected
    if args.only:
        names = {n.strip() for n in args.only.split(",") if n.strip()}
        chosen = [i for i in injected if i.perturbation.name in names]
        if not chosen:
            print(f"no perturbation matches {sorted(names)}")
            return 2

    bare = copy.deepcopy(spec)
    bare.examples = []
    _c, clean = build_arms(bare)

    runner = CaApiRunner(project=args.project, location=args.location)
    sheet = runner.cost_sheet(n_questions=len(golds),
                              n_arms=len(chosen) + 1, k=1)
    print(f"\n  {len(golds)} working question(s), {len(chosen) + 1} arm(s) "
          f"(the clean one plus {len(chosen)} defect(s))")
    print("  the reserved half is NOT read")
    print(f"  {sheet.line()}")
    decision = confirm_paid("", budget=args.budget, yes=args.yes,
                            argv=["perturb_suite"] + sys.argv[2:])
    print(decision.reason)
    if not decision.allowed:
        return 2

    # The CLI owns the --db to executor mapping, including the 60-second
    # bound on untrusted model SQL. Reusing it rather than building a second
    # one keeps that bound in force here too.
    from context_lift.cli import _executor_for
    executor = _executor_for(args)
    done, answers, cache = {}, {}, {}
    if args.checkpoint:
        from context_lift.round import _read_checkpoint
        done = _read_checkpoint(args.checkpoint)
        if done:
            print(f"  {len(done)} answer(s) already on disk, reused")

    def progress(label, index, total, scored_id, reused):
        print(f"  [{label}] {'cached' if reused else 'asked '} "
              f"{index}/{total} {scored_id}", flush=True)

    base_rows = _ask_arm(golds, clean, runner.answer, 1, progress,
                         args.checkpoint, done, answers)
    base = evaluate(executor, base_rows, gold_cache=cache)
    # `execution` is a Proportion (k of n, with an interval), not a float.
    print(f"\n  clean arm {clean.arm_id}: {base.execution.k} of "
          f"{base.execution.n} correct ({base.execution.point:.1%})\n")

    curve = []
    for item in chosen:
        damaged = item.perturbation.apply(bare)
        _c2, arm = build_arms(damaged)
        rows = _ask_arm(golds, arm, runner.answer, 1, progress,
                        args.checkpoint, done, answers)
        report = evaluate(executor, rows, gold_cache=cache)
        paired = ablate(base, report)
        curve.append((item.perturbation, arm.arm_id, paired))
        print(f"\n  {item.perturbation.name}: {paired.lift * 100:+.1f} points, "
              f"broke {paired.defended.b} fixed {paired.defended.c}, "
              f"n={paired.n_defended}\n")

    print("\n  DEGRADATION CURVE, worst first")
    for perturbation, arm_id, paired in sorted(curve, key=lambda c: c[2].lift):
        print(f"    {perturbation.name:<38} {paired.lift * 100:+6.1f} points "
              f"  arm {arm_id}")
        print(f"        broke {paired.defended.b} / fixed {paired.defended.c} "
              f"of {paired.n_defended}   {perturbation.breaks}")
    print("\n  A perturbation showing NO loss is information, not a failure: "
          "not every defect matters.")
    print("  This measures ONE agent on ONE schema. It does not generalise.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
