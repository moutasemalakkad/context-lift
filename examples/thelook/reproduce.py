#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Recompute a committed round's verdict, offline.

    python examples/thelook/reproduce.py committed/run-2026-09-10.csv

Not under `runs/`: that name is gitignored everywhere, because a workspace's
`runs/` holds a customer's own artifacts and must never be committed. A
committed example run lives beside the example instead of putting a hole in
that rule.

No warehouse, no model, no network, no cost. The run file records what each
arm was asked and whether its answer was right; everything after that is
arithmetic, and this is the arithmetic.

This exists because of a standing rule: every figure in a document has to be
computed by a committed script somebody can point at when asked "how did you
get that". A number typed into a document by hand is a claim.

What it cannot check: whether `correct` was decided correctly. That needed the
warehouse, once, on the day. What it does check is that the counts in the run
file still produce the verdict the documents print, so a change to the
statistics or to the verdict order shows up as a failing number rather than as
a document quietly going out of date.
"""
from __future__ import annotations

import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from context_lift.ablate import AblationReport, verdict  # noqa: E402
from context_lift.metrics import mcnemar_exact, proportion  # noqa: E402


def read_run(path: str | pathlib.Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _correct(row: dict) -> bool | None:
    """An excluded question is None, which is not False.

    A gold that did not run and a row the cap cut were never scored. Counting
    either as wrong would move the denominator and quietly change the rate.
    """
    value = (row.get("correct") or "").strip()
    if value in ("True", "true", "1"):
        return True
    if value in ("False", "false", "0"):
        return False
    return None


def report_for(rows: list[dict], split: str) -> AblationReport | None:
    """The paired comparison for one split, from the recorded answers."""
    by_arm: dict[str, dict[str, bool | None]] = {"control": {}, "treatment": {}}
    for row in rows:
        if row["split"] != split:
            continue
        by_arm[row["arm"]][row["id"]] = _correct(row)

    shared = sorted(set(by_arm["control"]) & set(by_arm["treatment"]))
    paired = [(by_arm["control"][i], by_arm["treatment"][i]) for i in shared
              if by_arm["control"][i] is not None
              and by_arm["treatment"][i] is not None]
    if not paired:
        return None

    broke = sum(1 for was, now in paired if was and not now)
    fixed = sum(1 for was, now in paired if not was and now)
    control_right = sum(1 for was, _now in paired if was)
    treatment_right = sum(1 for _was, now in paired if now)
    n = len(paired)
    control = proportion(control_right, n)
    treatment = proportion(treatment_right, n)
    return AblationReport(
        without_context=control, with_context=treatment,
        lift=treatment.point - control.point,
        mcnemar=mcnemar_exact(broke, fixed), n_paired=n, adjusted=False,
        n_questions=n)


def main() -> int:
    path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                        else pathlib.Path(__file__).parent / "committed/run-2026-09-10.csv")
    if not path.is_file():
        print(f"no run file at {path}")
        return 1
    rows = read_run(path)
    curate = report_for(rows, "curate")
    holdout = report_for(rows, "holdout")
    if holdout is None:
        print("the run recorded no scored held-out question")
        return 1

    result = verdict(holdout, curate, target_pp=10.0)
    print(f"{path}: {len(rows)} scored rows")
    print(f"verdict: {result.word}")
    print(f"  {result.reason}")
    print(f"  held out: {holdout.n_questions} questions, "
          f"{holdout.lift * 100:+.1f} points, "
          f"{holdout.mcnemar.c} fixed, {holdout.mcnemar.b} broke")
    if curate is not None:
        print(f"  fit on the working questions: {curate.n_questions} questions, "
              f"{curate.lift * 100:+.1f} points, "
              f"{curate.mcnemar.c} fixed, {curate.mcnemar.b} broke")
    print("  no warehouse, no model, no network")
    return 0


if __name__ == "__main__":
    sys.exit(main())
