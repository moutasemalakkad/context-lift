#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Merge per-database BIRD reports into three paired cross-database reports."""
from __future__ import annotations

import argparse
import json
import pathlib

from prepare_enterprise import DATABASES


def merge_reports(root: pathlib.Path, report_dir: str, without_prefix: str,
                  with_prefix: str, out: pathlib.Path) -> tuple[set[str], set[str]]:
    """Merge shards and symmetrically remove any undefended question."""
    loaded = {}
    for arm, prefix in (("without", without_prefix), ("with", with_prefix)):
        for repetition in range(1, 4):
            results = []
            for database in DATABASES:
                path = (root / "work" / database / report_dir
                        / f"{prefix}_rep{repetition}.json")
                results.extend(json.loads(path.read_text(encoding="utf-8"))["results"])
            loaded[arm, repetition] = results

    defended = [
        {row["id"] for row in rows if row["execution_correct"] is not None}
        for rows in loaded.values()
    ]
    common = set.intersection(*defended)
    union = set.union(*defended)
    out.mkdir(parents=True, exist_ok=True)
    for (arm, repetition), rows in loaded.items():
        kept = [row for row in rows if row["id"] in common]
        if len(kept) != len(common):
            raise ValueError(f"duplicate or missing IDs in {arm} repetition {repetition}")
        destination = out / f"{arm}_rep{repetition}.json"
        destination.write_text(json.dumps({
            "n_total": len(kept), "results": kept,
        }, indent=1) + "\n", encoding="utf-8")
    excluded = union - common
    (out / "pairing.json").write_text(json.dumps({
        "paired_questions": len(common),
        "excluded_unpaired_ids": sorted(excluded),
    }, indent=2) + "\n", encoding="utf-8")
    return common, excluded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--report-dir", default="final_reports")
    parser.add_argument("--without-prefix", default="base")
    parser.add_argument("--with-prefix", default="docs")
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()

    common, excluded = merge_reports(
        args.root, args.report_dir, args.without_prefix, args.with_prefix, args.out)
    print(f"paired {len(common)} questions across {len(DATABASES)} databases")
    print(f"excluded unpaired: {len(excluded)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
