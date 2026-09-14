#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Recompute the degradation curve from the committed run. Offline and free.

    python examples/bird/perturbation/reproduce.py

No warehouse, no model, no network, no cost. The committed file records
whether each arm answered each question correctly; everything after that is
arithmetic, and this is the arithmetic.

WHY IT EXISTS. Every figure in a document here has to be computable from
something committed, so that "how did you get that" has an answer six months
later. A number typed into a document by hand is a claim.

WHAT IT CANNOT CHECK: whether `correct` was decided correctly. That needed the
databases, once, on the day. What it does check is that the committed
correctness still produces the curve the documents print -- so a change to the
statistics, or a document quietly going out of date, shows up as a failing
number rather than as nothing at all.

THE POSITIVE CONTROL IS THE ROW TO READ FIRST. Deleting every sentence that
carries a quoted literal -- the value decoding a schema cannot supply -- should
cost a great deal. If it ever stops doing so, the harness has changed and no
other row here means anything.
"""
from __future__ import annotations

import csv
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent.parent / "src"))

RUN = HERE / "correctness-3reps-2026-09-14.csv"
CLEAN = "clean"


def _majority(votes: list[int]) -> bool | None:
    """The same rule `ablate.py` uses: a tie abstains.

    Copied deliberately rather than imported, because this script has to keep
    working if the package moves, and the rule is four lines. The test
    `test_perturbation_example.py` asserts this reproduces the library's own
    numbers, so a drift between the two fails the suite.
    """
    yes = sum(votes)
    if yes * 2 == len(votes):
        return None
    return yes * 2 > len(votes)


def main() -> int:
    from context_lift.metrics import mcnemar_exact, proportion
    from context_lift.power import smallest_resolvable

    if not RUN.is_file():
        raise SystemExit(f"no committed run at {RUN}")
    rows = list(csv.DictReader(RUN.open()))

    # (arm, question) -> [correct per repetition]
    votes: dict[str, dict[str, list[int]]] = {}
    for row in rows:
        votes.setdefault(row["arm"], {}).setdefault(
            row["question"], []).append(int(row["correct"]))

    if CLEAN not in votes:
        raise SystemExit(f"the committed run has no {CLEAN!r} arm to compare against")
    clean = votes[CLEAN]
    n = len(clean)
    reps = max(len(v) for v in clean.values())
    answers_each = sum(len(v) for v in clean.values())

    print(f"the perturbation suite on BIRD, recomputed from {RUN.name}")
    print(f"  {n} questions x {reps} repetition(s) = {answers_each} answers "
          f"per arm, {len(votes)} arms, offline\n")
    right = sum(sum(v) for v in clean.values())
    print(f"  clean arm: {right} of {answers_each} answers correct "
          f"({proportion(right, answers_each).point:.1%})\n")

    curve = []
    for arm, answers in votes.items():
        if arm == CLEAN:
            continue
        shared = sorted(set(clean) & set(answers))
        if len(shared) != n:
            # Comparing different question sets is the one arithmetic mistake
            # that produces a plausible number from nothing.
            raise SystemExit(
                f"{arm} scored {len(shared)} of the clean arm's {n} questions; "
                f"pooling them would compare different sets")

        # PER QUESTION, each counted once. Three answers to one question are
        # not three independent facts, so the per-answer test below has an
        # inflated denominator and its p-value reads low.
        broke = fixed = abstained = 0
        for q in shared:
            a, b = _majority(clean[q]), _majority(answers[q])
            if a is None or b is None:
                abstained += 1
                continue
            if a and not b:
                broke += 1
            elif b and not a:
                fixed += 1
        test = mcnemar_exact(broke, fixed)

        # per ANSWER, for the comparison the output warns about
        flat_broke = sum(1 for q in shared
                         for i in range(len(clean[q]))
                         if clean[q][i] and not answers[q][i])
        flat_fixed = sum(1 for q in shared
                         for i in range(len(clean[q]))
                         if not clean[q][i] and answers[q][i])
        loose = mcnemar_exact(flat_broke, flat_fixed)

        lift = ((sum(sum(v) for v in answers.values()) - right)
                / answers_each * 100)
        curve.append((lift, arm, broke, fixed, test.p_value,
                      loose.p_value, abstained))

    # QUESTIONS, not answers. This is the same mistake that was found in
    # ablate.py's OVERFIT check on 2026-09-14 and it was made again here:
    # 1350 answers would give a floor of 2.0 points, but three answers to one
    # question are not three independent observations, so that number claims
    # a resolution the design does not have. 450 questions give about 4.7.
    floor = smallest_resolvable(n)
    print("  DEGRADATION CURVE, worst first. Per QUESTION -- the number to defend.")
    for lift, arm, broke, fixed, p, loose_p, abstained in sorted(curve):
        verdict = ("REAL LOSS" if p < 0.05 and lift < 0
                   else "REAL GAIN" if p < 0.05 else "NOT PROVEN")
        print(f"    {arm:<38} {lift:+6.1f} pts  broke {broke:>3} fixed {fixed:>3}"
              f"  p = {p:.4f}  {verdict}")
    print()
    print("  the same rows per ANSWER, which is optimistic and is here only so")
    print("  the gap is visible rather than taken on trust:")
    for lift, arm, _b, _f, p, loose_p, abstained in sorted(curve):
        print(f"    {arm:<38} per-question p = {p:.4f}   per-answer p = {loose_p:.4f}"
              + (f"   ({abstained} question(s) tied and abstained)" if abstained else ""))

    print(f"\n  {n} questions resolve a change above about {floor:.1f} points.")
    print("  (QUESTIONS, not the 1350 answers: repeated answers to one "
          "question are not")
    print("  independent, and using the answer count would claim a resolution "
          "this design")
    print("  does not have.)")
    print("  A defect smaller than that reads NOT PROVEN, which is not the "
          "same as costing nothing.")
    print("  One model, three BIRD databases, injected defects on public data.")
    print("  This does not generalise to an estate.")
    print()
    print("  WITHDRAWN: at ONE repetition irrelevant-context measured -2.9 and")
    print("  was the worst of the four realistic defects. At three it is the")
    print("  least, at -0.3. The ordering of the middle rows is not a result")
    print("  at this sample size. See docs/evidence/perturbation-bird-3reps.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
