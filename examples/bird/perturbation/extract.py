#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The smallest file that lets somebody recompute the degradation curve.

    python examples/bird/perturbation/extract.py --work DIR --out FILE

The scored reports are 1.7 MB across six arms, because each carries every
predicted SQL string and a preview of the rows it returned. None of that is
needed to recompute a paired comparison: `ablate` reads correctness per
question per arm and nothing else.

So this writes one row per (question, arm) with the flag, and the committed
result is small enough to read in a text editor. That is the same choice
`examples/thelook/committed/` already makes -- a run file plus a script that
recomputes the verdict with no warehouse, no model and no network.
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib

ARMS = ("clean", "positive-control-drop-value-decoding", "unknown-column",
        "strip-subject", "conflicting-definitions", "irrelevant-context")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work", required=True, metavar="DIR")
    parser.add_argument("--out", required=True, metavar="FILE")
    parser.add_argument("--prefix", default="pooled")
    parser.add_argument("--reps", type=int, default=1,
                        help="reads <prefix>-<arm>_rep<N>.json for N in 1..reps "
                             "and writes a `rep` column, so the recomputed "
                             "comparison can count each QUESTION once instead "
                             "of each answer")
    args = parser.parse_args()

    work = pathlib.Path(args.work)
    reps = range(1, args.reps + 1) if args.reps > 1 else [None]
    rows = []
    for arm in ARMS:
        for rep in reps:
            suffix = "" if rep is None else f"_rep{rep}"
            path = work / f"{args.prefix}-{arm}{suffix}.json"
            if not path.is_file():
                raise SystemExit(f"no report at {path}")
            for entry in json.loads(path.read_text())["results"]:
                rows.append({"question": entry["id"], "arm": arm,
                             "rep": 1 if rep is None else rep,
                             "correct": int(bool(entry.get("execution_correct")))})

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as handle:
        writer = csv.DictWriter(handle,
                                fieldnames=["question", "arm", "rep", "correct"])
        writer.writeheader()
        writer.writerows(rows)

    per_arm = {}
    for row in rows:
        per_arm.setdefault(row["arm"], []).append(row["correct"])
    print(f"  {len(rows)} rows -> {out} ({out.stat().st_size:,} bytes)")
    for arm in ARMS:
        got = per_arm.get(arm, [])
        print(f"    {arm:<38} {sum(got):>4} of {len(got)} correct")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
