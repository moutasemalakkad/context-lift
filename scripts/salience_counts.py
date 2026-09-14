# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Count the over-salience effect, instead of asserting it.

    python scripts/salience_counts.py --work DIR --database toxicology \
        --evidence DIR --column label

Toxicology lost 7.7 points when the blob was replaced by each question's own
evidence sentence. Over-salience is the proposed reason: a lone sentence is
OBEYED as an instruction, while the same sentence among 145 is CONSULTED as
reference. Nothing about that is obvious, and it was first counted by hand in
a chat session, which is exactly the kind of number that cannot be defended
later. So it gets a script.

It counts three things. The first two are about one column, named by
--column, because toxicology's evidence keeps saying `label = '+'` and the
broken answers keep selecting `label`.

  1. How often each arm puts that column in its SELECT LIST. If the mechanism
     is real, the selection arm does it more often, because its one sentence
     mentions the column and nothing else competes for attention.

  2. The break rate split by whether the question's OWN sentence mentions the
     column. This is the sharper test: it holds the arm fixed and varies only
     whether the salient sentence is present, so it is not explained by the
     two arms differing in length.

  3. The competing explanation, so that this script can refute the thing it
     was written to support.

WHAT IT FOUND, AND IT IS WEAKER THAN THE FIRST HAND COUNT. Both counts point
the predicted way and neither reaches significance: 97 select-list mentions
against 74, and a break rate of 20% (n=61) against 9% (n=78) at Fisher
p = 0.0839. An earlier count done by hand in chat reported 37 against 9 and
29% against 5%; those numbers do not survive being computed by a program, and
this script's output replaces them. The direction is real and the effect size
is not established. The competing explanation -- that values spread across
many sentences are harder to find -- scores 0 of 19, so it is refuted while
over-salience is merely unproven.

WHAT THIS CANNOT DO. All three counts are observational, on questions nobody
sampled for this purpose, and #2 conditions on a property of the question
rather than assigning it. To establish the mechanism you would write matched
sentence pairs that differ only in whether they name an irrelevant column,
and run those. That experiment has not been run.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import math
import pathlib
import re


def _select_list(sql: str) -> str:
    """The text between the outermost SELECT and its FROM.

    Deliberately simple: it takes the FIRST select and stops at the first
    following FROM, so a subquery's select list is not searched. That is the
    right side to err on here -- a column inside a subquery may be load-
    bearing, while one in the outer select list is in the returned shape.
    """
    found = re.search(r"\bselect\b(.*?)\bfrom\b", sql, re.I | re.S)
    return found.group(1) if found else ""


def _fisher(a: int, a_n: int, b: int, b_n: int) -> float:
    """Two-sided Fisher exact p for two independent break rates.

    Fisher rather than the project's `mcnemar_exact`, because these two
    groups are DIFFERENT questions -- 61 whose sentence names the column and
    78 whose does not. McNemar needs the same question in both cells, which
    is right for an arm comparison and wrong here.

    Exact, by summing every table at most as probable as the observed one.
    No scipy: the project has no numeric dependency and this is 10 lines.
    """
    c, d = a_n - a, b_n - b
    row1, row2 = a + b, c + d
    total = a_n + b_n

    def probability(x: int) -> float:
        return (math.comb(row1, x) * math.comb(row2, a_n - x)
                / math.comb(total, a_n))

    observed = probability(a)
    low = max(0, a_n - row2)
    return min(1.0, sum(probability(x) for x in range(low, min(row1, a_n) + 1)
                        if probability(x) <= observed + 1e-12))


def _read_arm(work: pathlib.Path, database: str, arm: str,
              reps: int = 3) -> tuple[dict, dict]:
    """(correct-by-(id, rep), predicted-sql-by-(id, rep)) for one arm."""
    correct: dict[tuple[str, int], bool] = {}
    sql: dict[tuple[str, int], str] = {}
    for rep in range(1, reps + 1):
        report = work / f"{database}-{arm}_rep{rep}.json"
        table = work / f"{database}-{arm}_rep{rep}.csv"
        if not report.is_file() or not table.is_file():
            raise SystemExit(f"missing {report.name} or {table.name}")
        for row in json.loads(report.read_text())["results"]:
            correct[(row["id"], rep)] = bool(row["execution_correct"])
        for row in csv.DictReader(table.open()):
            sql[(row["id"], rep)] = row["pred_sql"] or ""
    return correct, sql


def _majority(correct: dict, ids, reps: int) -> dict[str, bool]:
    """One verdict per question: correct in more than half its repetitions.

    A question is not three facts. Reporting per-observation here would count
    the same question up to three times and narrow every interval falsely.
    """
    out = {}
    for one in ids:
        got = [correct[(one, rep)] for rep in range(1, reps + 1)
               if (one, rep) in correct]
        out[one] = sum(got) * 2 > len(got)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work", required=True, metavar="DIR",
                        help="the run directory holding <db>-<arm>_repN.{csv,json}")
    parser.add_argument("--evidence", required=True, metavar="DIR",
                        help="the directory holding <db>/evidence.json")
    parser.add_argument("--database", default="toxicology")
    parser.add_argument("--column", default="label",
                        help="the column the evidence keeps naming")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--blob-arm", default="blob")
    parser.add_argument("--selection-arm", default="selection")
    args = parser.parse_args()

    work = pathlib.Path(args.work)
    evidence = json.loads(
        (pathlib.Path(args.evidence) / args.database / "evidence.json").read_text())

    blob_ok, blob_sql = _read_arm(work, args.database, args.blob_arm, args.reps)
    sel_ok, sel_sql = _read_arm(work, args.database, args.selection_arm, args.reps)

    ids = sorted({one for one, _ in blob_ok} & {one for one, _ in sel_ok})
    word = re.compile(rf"\b{re.escape(args.column)}\b", re.I)

    print(f"{args.database}: {len(ids)} question(s) in both arms, "
          f"{args.reps} repetition(s) each")
    print(f"the column the evidence keeps naming: {args.column!r}")
    print()

    # 1. how often each arm writes the column into its SQL.
    #
    # THE CLAUSE IS THE WHOLE MEASUREMENT, and counting the column anywhere in
    # the SQL is the wrong count. `WHERE label = '+'` is usually CORRECT --
    # the question asked about carcinogenic molecules. The mistake is putting
    # the column in the SELECT LIST, which answers a question nobody asked
    # and returns a shape the gold does not have. Counted anywhere in the
    # statement, the two arms are 205 against 182 and there is no effect to
    # see; counted in the select list, they separate.
    print(f"1. runs that put {args.column!r} in the SELECT LIST, per arm")
    counts, anywhere = {}, {}
    for label, sql in ((args.blob_arm, blob_sql),
                       (args.selection_arm, sel_sql)):
        mine = {key: text for key, text in sql.items() if key[0] in ids}
        counts[label] = sum(1 for text in mine.values()
                            if word.search(_select_list(text)))
        anywhere[label] = sum(1 for text in mine.values() if word.search(text))
        print(f"   {label:<12} {counts[label]:>4} of {len(mine)} runs"
              f"   ({anywhere[label]} name it anywhere in the statement)")
    print(f"   ratio        {counts[args.selection_arm]} selection "
          f"against {counts[args.blob_arm]} blob")
    print()

    # 2. the break rate, split by whether the question's own sentence names it
    blob_q = _majority(blob_ok, ids, args.reps)
    sel_q = _majority(sel_ok, ids, args.reps)
    groups: dict[bool, list[str]] = collections.defaultdict(list)
    for one in ids:
        groups[bool(word.search(evidence.get(one) or ""))].append(one)

    print(f"2. blob right and selection wrong, split on whether the "
          f"question's OWN sentence names {args.column!r}")
    rates = {}
    for names_it in (True, False):
        group = groups[names_it]
        broke = [one for one in group if blob_q[one] and not sel_q[one]]
        share = 100.0 * len(broke) / len(group) if group else 0.0
        which = "names it" if names_it else "does not"
        rates[names_it] = (len(broke), len(group))
        print(f"   sentence {which:<10} {len(broke):>3} of {len(group):<3} "
              f"broke  ({share:.0f}%, n={len(group)})")
    print(f"   two-sided Fisher exact p = "
          f"{_fisher(*rates[True], *rates[False]):.4f}")
    print()

    # the alternative this was written to test, reported either way
    print("3. the competing explanation: the value is spread across sentences")
    spread = 0
    stable = [one for one in ids if blob_q[one] and not sel_q[one]]
    for one in stable:
        mine = evidence.get(one) or ""
        values = set(re.findall(r"'([^']*)'", mine))
        if not values:
            continue
        elsewhere = sum(1 for other, text in evidence.items()
                        if other != one and any(v in text for v in values))
        if elsewhere == 0:
            spread += 1
    print(f"   of {len(stable)} broken question(s), {spread} name a value that "
          f"appears in no other sentence")
    print("   (if the blob's advantage were breadth, this would be most of "
          "them, not a few)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
