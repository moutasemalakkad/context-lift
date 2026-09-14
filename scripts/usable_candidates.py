#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Drop the candidates the gate already refused, before anyone reviews them.

    python scripts/usable_candidates.py candidates.csv usable.csv

Free, no cloud call. Review is the expensive human step and the paid drafter
step, and handing either a row whose SQL returns a 403 spends that budget on
something that can never be a gold.

Measured on one estate: of 344 candidates the drafter judged, it KEPT 24
whose SQL cannot run at all -- `NO_ACCESS` 8, `TABLE_GONE` 5, `ZERO_ROWS` 3,
`OVER_BUDGET` 2, `CROSS_PROJECT` 2 -- and left 16 more undecided, every one
of them defective. The kept count was inflated by exactly that: 63 kept
became 39 after re-gating.

**A warning is not a defect, and the difference is most of the set.** A
`gate_status` of `ok (MISSING_LITERAL)` is kept. `mine --rules-in-context`
produces that on purpose: the drafted question is meant to leave out the
business rule so the written context has to supply it, which is the whole
measurement. On one 758-row pull, strict `ok` was 54 rows and `ok (...)`
brought it to 177. Filtering on strict equality would have thrown away 123
of the best candidates in the set.

The dropped rows are not deleted; they stay in the input file, which is the
record of what the mine ran and paid for.
"""
from __future__ import annotations

import collections
import csv
import pathlib
import sys

# The column `mine` writes: "ok", "ok (WARNING)", or one or more defect
# names joined by "+". Anything starting "ok" ran and produced rows.
STATUS = "gate_status"


def split(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(usable, refused). Usable means the gate let it through."""
    usable = [r for r in rows if (r.get(STATUS) or "").startswith("ok")]
    refused = [r for r in rows if not (r.get(STATUS) or "").startswith("ok")]
    return usable, refused


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    src, dst = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    if not src.is_file():
        print(f"no candidate file at {src}")
        return 1
    with open(src, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        print(f"{src} has no rows")
        return 1
    if STATUS not in rows[0]:
        print(f"{src} has no {STATUS} column; is it a `mine` output?")
        return 1

    usable, refused = split(rows)
    with open(dst, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(usable)

    print(f"{len(usable)} of {len(rows)} candidates are usable -> {dst}")
    warned = sum(1 for r in usable if r[STATUS] != "ok")
    print(f"  {len(usable) - warned} strictly clean, {warned} clean with a warning")
    print("  a warning is NOT a defect: --rules-in-context omits the business "
          "rule on purpose, so the context has to supply it")
    if refused:
        tally = collections.Counter(
            d for r in refused for d in (r[STATUS] or "").split("+")
            if d and not d.startswith("ok"))
        print(f"  {len(refused)} refused by the gate, left out of review:")
        for name, n in tally.most_common(10):
            print(f"    {name:20s} {n}")
        print("  reviewing these spends budget on rows that can never be golds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
