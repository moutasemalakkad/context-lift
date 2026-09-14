#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""How much of a candidate or gold set reads a dataset the customer documented.

    python scripts/doc_coverage.py _personal/acme/candidates-deep.csv \
                                   _personal/acme/deep/golds-final.csv

Free, no cloud call. The documented datasets are the `dataset:` entries of
the hand-maintained catalog manifest, one per data product, each of which
carries a human-written doc file. A gold that reads an UNDOCUMENTED dataset
cannot be moved by that documentation, so it measures model-mined prose and
nothing else -- which is the mistake this script exists to make visible.
"""
from __future__ import annotations

import csv
import re
import sys

MANIFEST = "/Users/moutasem/code/acme/catalog/in_scope.yaml"


def documented_datasets(manifest: str = MANIFEST) -> list[str]:
    text = open(manifest).read()
    return sorted(set(re.findall(r"^\s*dataset:\s*(\S+)", text, re.M)))


def main() -> None:
    datasets = documented_datasets()
    print(f"{len(datasets)} documented datasets in {MANIFEST}")
    for path in sys.argv[1:]:
        rows = list(csv.DictReader(open(path)))
        hit = [r for r in rows
               if any("." + d + "." in (r.get("gold_sql") or "")
                      for d in datasets)]
        passing = [r for r in hit
                   if (r.get("gate_status") or "ok").startswith("ok")]
        print(f"{path}: {len(rows)} rows, {len(hit)} read a documented "
              f"dataset, {len(passing)} of those pass the gate")


if __name__ == "__main__":
    main()
