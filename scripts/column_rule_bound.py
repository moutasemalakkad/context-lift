#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""How many saved answers a different column rule could POSSIBLY change. Free.

The question behind task #29: is it worth paying to re-score answers already
bought, under `--extra-columns tolerate` instead of `reject`?

Re-scoring is not free. The saved rows carry `pred_sql` and not result rows,
and the column rule compares result COLUMNS, so scoring means re-executing
both the gold and the predicted SQL against the warehouse. On a customer
estate that is money and access. This bounds the payoff first, for nothing,
which is the same discipline the power gate applies to a round.

WHAT MAKES THE BOUND SOUND. `compare()` reaches `MATCH_EXTRA_COLUMNS` only
when BOTH hold:

    len(expected_rows) == len(agent_rows)      # equal row counts
    num_pred > num_cols                        # the prediction is WIDER

The second is a necessary condition, and the outer SELECT list is readable
from the text. So an answer whose prediction selects the same number of
columns as its gold, or fewer, can NEVER change -- whatever the data holds.
Everything this excludes is excluded for certain.

WHAT MAKES IT AN OVER-ESTIMATE, and it is stated in the output rather than
buried here. The row-count condition cannot be checked without running the
queries, so a wider prediction over a different number of rows is counted
here and would not change in reality. `SELECT *` has no knowable width and is
counted as unknown rather than guessed at.

    python scripts/column_rule_bound.py --rows run-rows.csv
"""
from __future__ import annotations

import argparse
import collections
import csv
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from context_lift.executor import strip_comments_and_mask_literals  # noqa: E402

# The outermost SELECT ... FROM. Deliberately simple, and every case it cannot
# read is reported as unknown rather than assumed. A CTE is handled by taking
# the LAST top-level select, which is the one that shapes the result.
_SELECT = re.compile(r"\bSELECT\b(.*?)\bFROM\b", re.IGNORECASE | re.DOTALL)

# `*` as a whole select item, or qualified as `t.*`. Not `COUNT(*)`, where the
# star is a function argument, and not `a * b`, where it is multiplication.
_BARE_STAR = re.compile(r"(?:^|,)\s*(?:[A-Za-z_][\w$]*\s*\.\s*)?\*\s*(?:,|$)")


def select_width(sql: str) -> int | None:
    """How many columns the outer SELECT returns, or None when unknowable.

    None for `SELECT *`, for a statement with no readable SELECT..FROM, and
    for a list this cannot split with confidence. Guessing here would turn a
    bound into a number nobody should trust.
    """
    if not (sql or "").strip():
        return None
    masked = strip_comments_and_mask_literals(sql)
    matches = _SELECT.findall(masked)
    if not matches:
        return None
    # The last top-level SELECT..FROM: with a CTE, the final select is the one
    # that shapes the result.
    # `DISTINCT` and `ALL` are quantifiers on the whole list, not columns, and
    # leaving one in front hid `SELECT DISTINCT *` from the bare-star check.
    body = re.sub(r"^\s*(?:DISTINCT|ALL)\b", "", matches[-1],
                  flags=re.IGNORECASE)
    # A BARE star only. `COUNT(*)` and `SUM(x) OVER ()` are ordinary columns
    # of known width, and treating any `*` as unknown put most of the real
    # data in the unknown bucket -- 22 of 68 answers on one saved round --
    # which made the bound far looser than the text actually allows.
    if _BARE_STAR.search(body):
        return None          # width depends on the table, not the text
    depth, width, seen = 0, 1, False
    for char in body:
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "," and depth == 0:
            width += 1
        if not char.isspace():
            seen = True
    if depth != 0 or not seen:
        return None          # unbalanced, or nothing there
    return width


_STAR = re.compile(r"[()]|\bSELECT\b\s+(?:DISTINCT\s+)?\*", re.IGNORECASE)


def outer_select_star(sql: str) -> bool:
    """True when `SELECT *` sits at nesting depth 0, so it shapes the result.

    `gate` refuses a gold that "hands back the whole row (a SELECT * gold is
    someone browsing a table, not a question)". Three saved golds contained
    `SELECT *` and looked like escapes; all three had it inside a subquery,
    which is ordinary SQL and not what the gate is about. Checked here so the
    question is answered by the script rather than by hand next time, and so a
    real escape would be reported rather than assumed absent.
    """
    depth = 0
    for match in _STAR.finditer(strip_comments_and_mask_literals(sql or "")):
        token = match.group(0)
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif depth == 0:
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", required=True,
                    help="a round's run-rows CSV: id, arm_id, verdict, "
                         "gold_sql, pred_sql")
    ap.add_argument("--verdict", default="MISMATCH",
                    help="the verdict the column rule could move (default "
                         "MISMATCH; nothing else can become a match)")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.rows, newline="", encoding="utf-8")))
    if not rows:
        print(f"{args.rows} holds no rows", file=sys.stderr)
        return 1

    movable = [r for r in rows if r.get("verdict") == args.verdict]
    tally = collections.Counter()
    per_arm = collections.Counter()
    for row in movable:
        gold = select_width(row.get("gold_sql", ""))
        pred = select_width(row.get("pred_sql", ""))
        if gold is None or pred is None:
            tally["unknown width"] += 1
        elif pred > gold:
            tally["COULD change"] += 1
            per_arm[row.get("arm_id", "?")] += 1
        else:
            tally["cannot change"] += 1

    print(f"{args.rows}")
    print(f"  {len(rows)} saved answers, {len(movable)} with verdict "
          f"{args.verdict}")
    print(f"  the other {len(rows) - len(movable)} cannot be moved by a column "
          "rule at all")
    print()
    for label in ("COULD change", "unknown width", "cannot change"):
        print(f"    {label:16s} {tally[label]:4d}")
    print()
    could, unknown = tally["COULD change"], tally["unknown width"]
    print(f"  UPPER BOUND: {could + unknown} of {len(rows)} answers "
          f"({(could + unknown) / len(rows):.1%})")
    print(f"    {could} read as wider than their gold; {unknown} could not be "
          "read at all")
    if per_arm:
        print("    by arm: " + ", ".join(f"{arm}={n}"
                                         for arm, n in per_arm.most_common()))
        # THE NUMBER THAT DECIDES IT, and the reason this script exists rather
        # than a re-score. A round measures a LIFT: the difference between two
        # arms over the same questions. A change landing equally on both arms
        # moves each arm's accuracy level and cancels in the difference. So
        # the payoff is not the count of movable answers, it is the GAP
        # between the arms' counts.
        counts = [n for _, n in per_arm.most_common()]
        spread = max(counts) - min(counts) if len(counts) > 1 else max(counts)
        questions = len(set(row["id"] for row in rows))
        print(f"    the arms differ by {spread} answer(s), so at most "
              f"{spread} of {questions} questions")
        print(f"    could move the LIFT: {spread / questions * 100:.1f} points, "
              "before the row-count")
        print("    condition removes any of it. A change that lands equally "
              "on both arms")
        print("    shifts each arm's accuracy and cancels in the difference.")

        # And PER SPLIT, because the reserved lift is the claim. A movable
        # answer in the working half cannot touch it: the working half is
        # where rules were curated and its number is already the weaker
        # evidence. Free, and it is what decides whether paying is worth it.
        if any(row.get("split") for row in rows):
            print()
            for split in sorted({row.get("split", "") for row in rows} - {""}):
                by_arm = collections.Counter(
                    row["arm_id"] for row in movable
                    if row.get("split") == split
                    and (select_width(row.get("pred_sql", "")) or -1)
                    > (select_width(row.get("gold_sql", "")) or 1 << 30))
                ids = len({row["id"] for row in rows
                           if row.get("split") == split})
                if not ids:
                    continue
                nums = [n for _, n in by_arm.most_common()] or [0]
                gap = max(nums) - min(nums) if len(nums) > 1 else max(nums)
                print(f"    {split:8s} {ids:3d} questions, arms differ by "
                      f"{gap} -> at most {gap / ids * 100:.1f} points of "
                      f"{split} lift")
    print()
    print("  This is an OVER-estimate on purpose. `tolerate` also requires the "
          "two")
    print("  results to have the SAME ROW COUNT, which cannot be known without "
          "running")
    print("  the queries. A wider prediction over a different number of rows "
          "is counted")
    print("  here and would not change in reality. `SELECT *` has no knowable "
          "width")
    print("  and is counted as unknown rather than guessed at.")
    print()
    # A gold whose OUTER select is `SELECT *` should never have reached a
    # round: the gate refuses it. Reported here because these files are the
    # only place to notice an escape after the fact.
    escapes = sorted({row["id"] for row in rows
                      if outer_select_star(row.get("gold_sql", ""))})
    if escapes:
        print(f"  WARNING: {len(escapes)} gold(s) have SELECT * as the OUTER "
              "select, which `gate` refuses:")
        for gold_id in escapes[:5]:
            print(f"    {gold_id}")
        print()
    else:
        print("  no gold has SELECT * as its outer select, so none escaped "
              "the gate")
        print()

    # The argument that settles the verdict WORDS, and it needs no data.
    # `tolerate` is strictly more permissive than `reject`: it can only turn a
    # MISMATCH into a MATCH_EXTRA_COLUMNS, never the other way. So it can only
    # FIX answers, never break them -- a SAFE round cannot become MIXED or a
    # REGRESSION by re-scoring, and `broke` can only fall.
    print("  `tolerate` is strictly more permissive than `reject`: it turns a")
    print("  MISMATCH into a match and never the reverse. So re-scoring can "
          "only FIX")
    print("  answers, never break them -- a SAFE round cannot become MIXED, "
          "and the")
    print("  `broke` count can only fall. Only the LIFT can move either way, "
          "and only")
    print("  by the arm difference above.")
    print()

    if could == 0:
        print("  VERDICT: re-scoring cannot change a single answer. Do not pay "
              "for it.")
    else:
        print(f"  VERDICT: at most {could + unknown} answers could move. Weigh "
              "that against")
        print("  the cost of re-executing every gold and prediction on the "
              "warehouse.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
