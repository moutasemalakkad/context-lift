#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Is a finding about one estate, or about gold sets in general?

    python scripts/estate_shape.py examples/thelook/golds.csv

Prints the three numbers that decide whether written context can reach a
reserved question at all, for any gold set, with no cloud call:

  1. questions, tables, and questions per table
  2. how many tables carry exactly ONE question
  3. the shared-ground gate under the real split

Why these three. A rule written from a working question reaches a reserved
question only when the two read the same table. So the reachable share is
bounded by how the questions spread over tables, before anybody writes
anything. Finegan-Dollak et al. 2018 (ACL, arXiv:1806.09029) found the same
mechanism one level down: on sets whose question-to-unique-query ratio is
near 1:1 a random split already behaves like a template split, so it poses
no generalisation challenge. Low redundancy there means a small gap; here it
means no lift is available.

Run it on a second estate before treating any threshold in `gates.py` as a
measurement rather than a default.
"""
from __future__ import annotations

import collections
import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from context_lift.gates import shared_ground          # noqa: E402
from context_lift.leakage import split_golds          # noqa: E402
from context_lift.mine import tables_named            # noqa: E402


def tables_of(sql: str) -> set[str]:
    """Qualified table names only. A one-part name is a CTE alias."""
    return {n.lower() for n in tables_named(sql) if "." in n}


def report(path: pathlib.Path) -> None:
    with open(path, newline="", encoding="utf-8") as handle:
        golds = list(csv.DictReader(handle))
    per_gold = {g["id"]: tables_of(g["gold_sql"]) for g in golds}

    by_table: dict[str, list[str]] = collections.defaultdict(list)
    for gid, tables in per_gold.items():
        for table in tables:
            by_table[table].append(gid)
    all_tables = set(by_table)

    print(f"\n{path}")
    print(f"  {len(golds)} questions over {len(all_tables)} tables")
    if all_tables:
        per_table = len(golds) / len(all_tables)
        note = ("  <- under 2.00: most tables cannot appear on both sides of a split"
                if per_table < 2 else "")
        print(f"  {per_table:.2f} questions per table{note}")
    lonely = [t for t, ids in by_table.items() if len(ids) == 1]
    print(f"  {len(lonely)} of {len(all_tables)} tables carry exactly ONE question")

    # the same count one level up. "Mine deep, not wide" is advice about
    # DATASETS, because a dataset is the unit a re-mine can be scoped to,
    # and a table with one question in a dataset with twenty is fine.
    by_dataset: dict[str, set[str]] = collections.defaultdict(set)
    for gid, tables in per_gold.items():
        for table in tables:
            by_dataset[table.split(".")[0]].add(gid)
    thin = [d for d, ids in by_dataset.items() if len(ids) == 1]
    print(f"  {len(thin)} of {len(by_dataset)} datasets carry exactly ONE question")

    # the split the tool would really use, then the gate that prices it
    family_of = {gid: (sorted(tables)[0].split(".")[0] if tables else "?")
                 for gid, tables in per_gold.items()}
    for label, kwargs in (("hash the id", {}),
                          ("stratify by dataset", {"family_of": family_of})):
        curate, holdout = split_golds(list(per_gold), **kwargs)
        curate_tables = set().union(*(per_gold[g] for g in curate)) if curate else set()
        gate = shared_ground(curate_tables, {g: per_gold[g] for g in holdout})
        print(f"  {label}: {len(curate)} working, {len(holdout)} reserved")
        print(f"    [{'ok  ' if gate.passed else 'FAIL'}] {gate.detail}")


def main() -> int:
    paths = [pathlib.Path(p) for p in sys.argv[1:]]
    if not paths:
        print(__doc__)
        return 1
    for path in paths:
        if not path.is_file():
            print(f"no gold file at {path}")
            return 1
        report(path)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
