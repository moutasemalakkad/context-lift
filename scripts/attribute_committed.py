# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Credit the context lines for the committed thelook round.

    python scripts/attribute_committed.py --db bq://PROJECT

`attribute` needs the two arms as separate prediction files, and the
committed round is one CSV with an `arm` column. Splitting it by hand is a
step somebody would do twice and then get wrong, so it is a script.

WHAT IT ANSWERS, and it is not the same question as `classes`. `classes`
removes a whole CLASS and re-asks, which costs one arm per class and says
what the class is worth. `attribute` reads the SQL of answers that ALREADY
changed and asks which individual line could have caused each one. No new
model calls: the predictions are on disk and only their SQL is executed.

So the two are complementary and neither replaces the other:

    classes     what is a class worth?       paid, one arm per class
    attribute   which line earned that fix?  free of model calls, SQL only

Cost is the SQL alone, and on `bigquery-public-data.thelook_ecommerce` that
is pennies. `--estimate-only` prices it first, as every paid path here does.
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import subprocess
import sys

COMMITTED = pathlib.Path("examples/thelook/committed/run-2026-09-10.csv")
SPEC = pathlib.Path("examples/thelook/spec")


def split(source: pathlib.Path, out_dir: pathlib.Path) -> dict[str, pathlib.Path]:
    """One file per arm, keeping every column `attribute` reads."""
    rows = list(csv.DictReader(source.open(encoding="utf-8")))
    if not rows:
        raise SystemExit(f"{source} has no rows")
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, pathlib.Path] = {}
    for arm in sorted({r["arm"] for r in rows}):
        subset = [r for r in rows if r["arm"] == arm]
        path = out_dir / f"{arm}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(subset)
        written[arm] = path
        print(f"  {arm:10s} {len(subset):4d} rows -> {path}")
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True,
                        help="where the predicted SQL runs, e.g. bq://PROJECT")
    parser.add_argument("--out-dir", default="/tmp/attribute-committed")
    parser.add_argument("--estimate-only", action="store_true",
                        help="price it and stop, the way every paid path here does")
    parser.add_argument("--max-bytes", default="10GB")
    parser.add_argument("--budget", default=None)
    args = parser.parse_args()

    if not COMMITTED.exists():
        raise SystemExit(f"{COMMITTED} not found: run this from the repo root")
    print(f"splitting {COMMITTED} by arm:")
    arms = split(COMMITTED, pathlib.Path(args.out_dir))
    for needed in ("control", "treatment"):
        if needed not in arms:
            raise SystemExit(f"no `{needed}` arm in {COMMITTED}")

    command = [sys.executable, "-m", "context_lift", "attribute",
               "--without-context", str(arms["control"]),
               "--with-context", str(arms["treatment"]),
               "--context", str(SPEC),
               "--db", args.db, "--max-bytes", args.max_bytes]
    if args.estimate_only:
        command.append("--estimate-only")
    if args.budget:
        command += ["--budget", args.budget]
    print("\n" + " ".join(command) + "\n")
    return subprocess.call(command)


if __name__ == "__main__":
    raise SystemExit(main())
