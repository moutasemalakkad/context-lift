# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Repair quoted values in written context against the real column values.

    python scripts/repair_literals.py --work DIR --databases DIR --database NAME

A rule that says `bond_type = ' = '` -- with spaces inside the quotes -- names
a value no row holds, so an agent that follows it faithfully returns nothing.
Measured on BIRD toxicology: 28 of 582 quoted literals in the evidence match
no column value, and 26 of those differ from a real value only by surrounding
whitespace or case.

Why this is the first arm to try. The blob carries the same typo in 23 of its
145 sentences and DILUTES it; the per-question arm carries one copy and copies
it verbatim. So part of the -7.7 point loss measured on toxicology may be this
defect rather than the salience effect it was attributed to. Repairing the
literals separates the two, and it costs nothing at inference.

THE REPAIR IS DELIBERATELY TIMID. It only replaces a literal when some real
column value is identical after stripping whitespace and folding case. It
never guesses at a near-miss, because a confident wrong repair is worse than
the typo: the typo returns no rows and is visible, while a plausible wrong
value returns the wrong rows silently.

It also leaves LIKE patterns alone. `'TR009_%'` contains SQL wildcards and is
not meant to match a stored value; an earlier version of this check flagged it
as broken, which would have turned a working rule into a broken one.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sqlite3

QUOTED = re.compile(r"'([^']*)'")
WILDCARD = re.compile(r"[%]")


def column_values(database: pathlib.Path, limit: int = 5000) -> dict[str, set[str]]:
    """Every distinct text value per column name, for the whole database.

    Keyed by column name rather than table.column because a rule usually
    names a bare column, and the point is to ask "does this value exist
    anywhere it could mean" rather than to resolve the table.
    """
    values: dict[str, set[str]] = collections.defaultdict(set)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        cursor = connection.cursor()
        tables = [row[0] for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            columns = [row[1] for row in cursor.execute(f'PRAGMA table_info("{table}")')]
            for column in columns:
                try:
                    rows = cursor.execute(
                        f'SELECT DISTINCT "{column}" FROM "{table}" LIMIT {limit}')
                except sqlite3.Error:
                    continue
                for (value,) in rows:
                    if isinstance(value, str):
                        values[column.lower()].add(value)
    return values


def repair(text: str, values: dict[str, set[str]]) -> tuple[str, list[dict]]:
    """The text with unmatched literals repaired, and what was changed."""
    every = {v for group in values.values() for v in group}
    folded: dict[str, list[str]] = collections.defaultdict(list)
    for value in every:
        folded[value.strip().lower()].append(value)

    changes: list[dict] = []

    def one(match: re.Match) -> str:
        literal = match.group(1)
        if literal in every:
            return match.group(0)
        if WILDCARD.search(literal):
            # a LIKE pattern is not meant to equal a stored value
            return match.group(0)
        candidates = sorted(set(folded.get(literal.strip().lower(), [])))
        if len(candidates) != 1:
            # zero: no real value differs only by space or case -- leave it and
            # let the linter report it. more than one: ambiguous, and picking
            # would be a guess.
            changes.append({"literal": literal, "action": "left alone",
                            "why": "no unique match after trim and case-fold",
                            "candidates": candidates})
            return match.group(0)
        changes.append({"literal": literal, "action": "repaired",
                        "to": candidates[0]})
        return f"'{candidates[0]}'"

    return QUOTED.sub(one, text), changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work", required=True, metavar="DIR")
    parser.add_argument("--databases", required=True, metavar="DIR")
    parser.add_argument("--database", required=True, metavar="NAME")
    parser.add_argument("--out-name", default="evidence-repaired.json")
    args = parser.parse_args()

    work = pathlib.Path(args.work) / args.database
    database = pathlib.Path(args.databases) / args.database / f"{args.database}.sqlite"
    if not database.is_file():
        raise SystemExit(f"no database at {database}")

    values = column_values(database)
    evidence = json.loads((work / "evidence.json").read_text())

    repaired: dict[str, str] = {}
    every_change: list[dict] = []
    touched = 0
    for key, text in evidence.items():
        fixed, changes = repair(text or "", values)
        repaired[key] = fixed
        if fixed != (text or ""):
            touched += 1
        for change in changes:
            change["question"] = key
            every_change.append(change)

    out = work / args.out_name
    out.write_text(json.dumps(repaired, indent=1) + "\n")
    report = work / args.out_name.replace(".json", "-report.json")
    report.write_text(json.dumps(every_change, indent=1) + "\n")

    done = [c for c in every_change if c["action"] == "repaired"]
    left = [c for c in every_change if c["action"] == "left alone"]
    print(f"{args.database}: {len(values)} column(s) read, "
          f"{len(evidence)} sentence(s)")
    print(f"  repaired   {len(done):>3} literal(s) in {touched} sentence(s)")
    print(f"  left alone {len(left):>3} (reported for a person, never guessed)")
    for change in done[:5]:
        print(f"    {change['question']}  {change['literal']!r} -> {change['to']!r}")
    for change in left[:3]:
        print(f"    {change['question']}  {change['literal']!r} LEFT: {change['why']}")
    print(f"  wrote {out}")
    print(f"        {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
