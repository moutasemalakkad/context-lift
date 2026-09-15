# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""How large a lift a gold set of n questions can actually resolve.

The gate this answers, and the reason it exists. Every paid round on the
real estate has come back NOT PROVEN. The tool reported that honestly and
never said whether the round COULD have come back any other way. It cannot
say so today: `classes.py:46` carries a rule of thumb -- a set resolves a
lift above about 100/sqrt(n) points -- and applies it only to item-level
leave-one-out, never to the round itself.

A rule of thumb is not good enough to spend money against, so this computes
it exactly.

The test a round is scored with is the exact McNemar test: of the questions
that DISAGREE between the two arms, how lopsided is the split. Only the
disagreements count. A question right in both arms carries no information
and neither does one wrong in both.

That gives a hard floor nothing can get under. With d disagreements all
falling the same way, the two-sided exact p is 2 * (1/2)^d. So:

    d = 4  ->  p = 0.1250   cannot reach 0.05, whatever happens
    d = 5  ->  p = 0.0625   cannot reach 0.05, whatever happens
    d = 6  ->  p = 0.0313   reaches 0.05, but only if ALL SIX agree

Six disagreements, unanimous, is the cheapest significant result that
exists. A round with five disagreements is unresolvable before it is run.

The 2026-09-12 Acme round produced 1 fixed and 3 broke: four disagreements,
p = 0.6250. It was arithmetically incapable of a verdict. No amount of
better context would have changed that, because the limit is the question
count, not the writing.
"""
from __future__ import annotations

import math

from context_lift.power import (expected_disagreements,  # noqa: F401
                            floor_on_disagreements, power, questions_needed,
                            smallest_resolvable, two_sided_p)

# Every set this project has scored or plans to, and the one number that
# decides whether paying for it can produce an answer.
SETS = [
    ("Acme reserved, 2026-09-12", 29),
    ("Acme, all deep golds", 43),
    ("thelook", 104),
    ("BEAVER DW holdout (research archive)", 596),
]

# The rungs this framework has actually measured, from docs/research/README.md.
LADDER = [
    ("iterated glossary", 3.8, 5.7),
    ("stronger model", 5.7, 8.1),
    ("oracle per-question hints", 8.1, 13.3),
    ("worked-example bank", 13.3, 20.2),
    ("whole ladder, schema-only to examples", 3.8, 20.2),
]


def main() -> None:
    floor = floor_on_disagreements()
    print("THE HARD FLOOR")
    print(f"  fewest disagreements that can ever reach p <= 0.05: {floor}")
    for d in range(3, 8):
        print(f"    {d} disagreements, all one way -> p = {two_sided_p(d, 0):.4f}")
    print()
    print("  the 2026-09-12 Acme round: 1 fixed, 3 broke, "
          f"p = {two_sided_p(1, 3):.4f}")
    print("  four disagreements cannot reach 0.05 in any arrangement.")
    print()

    print("SMALLEST LIFT EACH SET CAN RESOLVE (80% power, alpha 0.05,")
    print("break rate 2% as measured on the BEAVER holdout)")
    print(f"  {'set':38s} {'n':>5s} {'exact':>8s} {'100/sqrt(n)':>12s}")
    for name, n in SETS:
        exact = smallest_resolvable(n)
        rule = 100 / math.sqrt(n)
        shown = f"{exact:.0f} pts" if exact else "nothing"
        print(f"  {name:38s} {n:5d} {shown:>8s} {rule:11.1f}")
    print()

    print("CAN 29 RESERVED QUESTIONS SEE THE EFFECTS THIS TOOL ALREADY")
    print("MEASURED ON AN ENTERPRISE WAREHOUSE? (docs/research/README.md)")
    n = 29
    for name, before, after in LADDER:
        points = after - before
        fix_rate = points / 100 + 0.02
        p = power(n, fix_rate, 0.02)
        print(f"  {name:40s} {points:+5.1f} pts -> power {p:5.1%}"
              f"  {'sees it' if p >= 0.80 else 'CANNOT see it'}")
    print()
    print("  So the reserved set could not resolve the whole ladder, let")
    print("  alone one rung of it. NOT PROVEN was fixed before payment.")




# ---------------------------------------------------------------------------
# The planning question: what would it take to see a lift on this estate?
# ---------------------------------------------------------------------------

def plan() -> None:
    print()
    print("=" * 68)
    print("WHAT WOULD IT TAKE TO SEE A LIFT ON THIS ESTATE?")
    print("=" * 68)
    print()
    print("Questions needed for 80% power, 2% break rate, alpha 0.05.")
    print("Yield column: candidates to mine at the 10.5% end-to-end rate")
    print("this project measured (344 candidates -> 36 golds).")
    print()
    print(f"  {'effect being measured':40s} {'lift':>6s} {'n needed':>9s} "
          f"{'candidates':>11s}")
    for name, points in [("iterated glossary (BEAVER rung)", 1.9),
                         ("oracle per-question hints", 5.2),
                         ("worked-example bank", 6.9),
                         ("whole ladder, schema to examples", 16.4)]:
        need = questions_needed(points)
        if need is None:
            print(f"  {name:40s} {points:+6.1f} {'>1500':>9s} {'-':>11s}")
            continue
        # A reserved half is what needs the power; the round reserved 29 of
        # 43, so about two thirds.
        golds = math.ceil(need / (29 / 43))
        print(f"  {name:40s} {points:+6.1f} {need:9d} {golds / 0.105:11,.0f}")
    print()
    print("AND THE REASON IT IS THIS BAD: nothing disagrees.")
    print("Expected count of questions that move at all, n = 29:")
    for name, points in [("iterated glossary", 1.9),
                         ("worked-example bank", 6.9),
                         ("whole ladder", 16.4)]:
        d = expected_disagreements(29, points)
        verdict = "above the floor of 6" if d >= 6 else "BELOW the floor of 6"
        print(f"  {name:34s} {points:+5.1f} pts -> {d:4.1f} disagreements"
              f"   {verdict}")
    print()
    print("At 9.3% accuracy a context cannot fix what it cannot reach, so")
    print("the disagreement count stays tiny and the test stays silent.")
    print()
    print("THE OTHER DIRECTION: score a sub-outcome instead of the answer.")
    print("End-to-end correctness is all-or-nothing, which BEAVER's own")
    print("paper calls the reason error diagnosis is hard. A sub-outcome")
    print("like 'did it pick the right tables' sits at a far higher base")
    print("rate, so far more questions can move.")
    print()
    print(f"  {'outcome scored':34s} {'fixes':>7s} {'breaks':>7s} "
          f"{'disagree':>9s} {'power n=29':>11s}")
    for name, fix, brk in [("end-to-end answer, example bank", 0.069, 0.02),
                           ("table selection, plausible", 0.20, 0.05),
                           ("table selection, weak", 0.10, 0.05),
                           ("join keys, plausible", 0.24, 0.07)]:
        print(f"  {name:34s} {fix:7.0%} {brk:7.0%} "
              f"{29 * (fix + brk):9.1f} {power(29, fix, brk):11.1%}")


if __name__ == "__main__":
    main()
    plan()
