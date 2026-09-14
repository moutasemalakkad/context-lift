#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Materialize the BIRD context arm selected on development questions."""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re

from prepare_enterprise import _read_description


FORMULA = re.compile(r"=.*[/*+-]|divide|percentage|ratio|rate", re.I)
MAPPING = re.compile(
    r"\bmeans\b|\brefers? to\b|\bindicates?\b|\bstands? for\b|"
    r"\brepresents?\b|\bdefined as\b|\bcorresponds? to\b", re.I)
LITERAL = re.compile(r"[=`]\s*['\"]?[-A-Za-z0-9_.]+['\"]?", re.I)


def context_class(evidence: str) -> str:
    """The frozen, mutually exclusive policy learned on the development set."""
    if FORMULA.search(evidence):
        return "formula"
    if MAPPING.search(evidence):
        return "mapping"
    if LITERAL.search(evidence):
        return "literal"
    return "other"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=pathlib.Path, required=True,
                        help="pinned source/questions.json")
    parser.add_argument("--rows", type=pathlib.Path, required=True,
                        help="a contextlift split CSV")
    parser.add_argument("--descriptions", type=pathlib.Path, required=True,
                        help="source/descriptions/DATABASE")
    parser.add_argument("--out-questions", type=pathlib.Path, required=True)
    parser.add_argument("--out-context", type=pathlib.Path, required=True)
    parser.add_argument("--all", action="store_true",
                        help="development arm: include every evidence-bearing row")
    args = parser.parse_args()

    source = {f"bird_{row['query_id']:04d}": row
              for row in json.loads(args.source.read_text(encoding="utf-8"))}
    rows = list(csv.DictReader(args.rows.open(newline="", encoding="utf-8")))
    selected = [row for row in rows if row.get("evidence", "").strip() and (
        args.all or context_class(row["evidence"]) in {"formula", "literal"})]
    if not selected:
        raise SystemExit("no rows matched the context policy")

    args.out_questions.parent.mkdir(parents=True, exist_ok=True)
    with args.out_questions.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(selected)

    context = {}
    for row in selected:
        tables = source[row["id"]].get("table_id") or []
        if isinstance(tables, str):
            tables = [tables]
        pages = []
        for table in tables:
            path = args.descriptions / f"{table}.csv"
            pages.append(_read_description(path, table) if path.exists()
                         else f"Table {table}: no additional catalog page.")
        context[row["id"]] = (
            f"Expert business evidence:\n{row['evidence']}\n\n" + "\n\n".join(pages))
    args.out_context.write_text(json.dumps(context, indent=1) + "\n", encoding="utf-8")
    print(f"selected {len(selected)} of {len(rows)} rows")
    print("classes: formula and literal" if not args.all else "classes: all")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
