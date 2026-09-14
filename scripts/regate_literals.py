# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Re-run the gate's literal checks on a candidates CSV. No cloud call.

Why this can be done offline, which is the whole point. The gate runs its
literal checks BEFORE it executes the answer key (`gate.py`: `missing_literals`
at 1043, `MISSING_PARAMETER` appended at 1067, `executor.execute` at 1144) and
`MISSING_PARAMETER` does not return early. So a candidate refused only by a
literal check has already passed every execution check: it ran, returned rows,
was not truncated, not degenerate, not unstable, inside budget, no access
error. Its question and its SQL are both already in the CSV. Nothing is left
to pay for.

That matters because those checks were wrong. On one estate 47 of 62
candidates were refused because the question stated a date in English and the
answer key stated it in ISO, and the comparison squashed both sides to letters
and digits before comparing. `december12025` does not contain `20251201`.

This also reports a defect the gate has no class for: an answer key whose
result changes over time. `BETWEEN '2025-12-01' AND CURRENT_DATE() - 1` scores
differently tomorrow, and the UNSTABLE check re-runs twice in one session so
it never sees it.
"""
from __future__ import annotations

import csv
import pathlib
import re
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from context_lift.gate import (MISSING_LITERAL, MISSING_PARAMETER,  # noqa: E402
                           missing_literals, parameter_literals)

WARNINGS = {MISSING_LITERAL, "MISSING_PROJECTION"}

# A key that reads the clock answers a different question each day.
_MOVING = re.compile(r"\bCURRENT_(?:DATE|TIMESTAMP|DATETIME)\b|\bNOW\s*\(", re.I)


def hard_defects(status: str) -> set[str]:
    """The refusals in a gate_status string, warnings removed."""
    parts = {p.strip() for p in
             (status or "").replace("(", "+").replace(")", "+").split("+")
             if p.strip()}
    return {p for p in parts if p not in WARNINGS and not p.startswith("ok")}


def regate(row: dict) -> tuple[set[str], list[str]]:
    """The literal-derived defects this row carries now, and the values."""
    missing = missing_literals(row.get("gold_sql", ""), row.get("question", ""))
    parameters = parameter_literals(row.get("gold_sql", ""), missing)
    out = set()
    if parameters:
        out.add(MISSING_PARAMETER)
    return out, parameters


def main(path: str, out_path: str | None = None) -> None:
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    print(f"{len(rows)} candidates in {path}\n")

    # Only rows whose ONLY refusal is a literal check can be settled offline.
    # Anything with another refusal needs the cloud, and is left alone.
    settled, moved, still, other = [], [], [], []
    for row in rows:
        before = hard_defects(row.get("gate_status", ""))
        if before and before <= {MISSING_PARAMETER}:
            after, values = regate(row)
            row["_regate_values"] = "; ".join(values)
            if not after:
                (moved if _MOVING.search(row.get("gold_sql", "")) else settled
                 ).append(row)
            else:
                still.append(row)
        elif before:
            other.append(row)

    literal_only = len(settled) + len(moved) + len(still)
    print(f"REFUSED BY A LITERAL CHECK ALONE, so settleable offline: "
          f"{literal_only}")
    print(f"  now PASS, and the answer key is fixed in time : {len(settled)}"
          "   <- recovered")
    print(f"  now pass but the key reads the clock          : {len(moved)}"
          "   <- pin the date or drop")
    print(f"  still refused, and correctly                  : {len(still)}")
    print(f"\n  (left alone, refused by something a cloud call decides: "
          f"{len(other)})")

    if still:
        print("\n  what the ones that stay refused are missing:")
        kinds = Counter()
        for row in still:
            for value in row["_regate_values"].split("; "):
                kinds[value] += 1
        for value, n in kinds.most_common(12):
            print(f"    {n:3d}  {value}")

    if moved:
        print("\n  the moving answer keys:")
        for row in moved:
            hit = _MOVING.search(row["gold_sql"])
            print(f"    {row['id']:18s} {hit.group(0)}")

    if out_path:
        recovered = {row["id"] for row in settled}
        for row in rows:
            if row["id"] in recovered:
                row["gate_status"] = "ok (regated)"
            row.pop("_regate_values", None)
        with open(out_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nwrote {out_path} with {len(recovered)} rows marked "
              "`ok (regated)`")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: regate_literals.py CANDIDATES.csv [OUT.csv]")
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
