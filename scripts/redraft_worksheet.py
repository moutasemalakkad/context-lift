# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Pull the rows a reviewer rejected for a bad QUESTION, for re-drafting.

    python scripts/redraft_worksheet.py review.csv worksheet.csv

Free, no cloud call. Why this exists: on one estate a reviewer rejected 146 of
195 candidates, and **59 of them were rejected as `does_not_match`** -- the
drafted English did not ask what the SQL answers. Those 59 all ran on
BigQuery, returned rows, and passed every gate check. Only the English was
wrong, so the money is already spent and the SQL is already on disk.

The drafter had written a column list rather than a question, which is the
exact trap `REVIEW.md` warns about:

    flash  : "The material, plant, from date, to date, cost estimate number,
              and total value per unit for materials with a standard cost of 0"
    the SQL: -- Pull the actual list of the 108 for follow-up

The analyst's own comment says what the query is FOR and the drafter ignored
it. This worksheet puts that comment at the top of every row, because it is
the single most useful thing in the file and it is thrown away by
`strip_comments` everywhere else in the pipeline.

The output is a FILE somebody fills in. Re-drafting is an LLM stage or a
human stage, and either way it does not happen silently.
"""
from __future__ import annotations

import csv
import re
import sys

COMMENT = re.compile(r"--[ \t]?(.*)")
SELECT_LIST = re.compile(r"\bSELECT\b(.*?)\bFROM\b", re.I | re.S)
TABLES = re.compile(r"\b(?:FROM|JOIN)\s+`?([a-z0-9_\-]+(?:\.[a-z0-9_\-]+){1,2})`?",
                    re.I)
FILTERS = re.compile(r"\b(?:WHERE|AND)\s+([a-z_][a-z0-9_.]*\s*(?:=|!=|<>|>=|<=|>|<|"
                     r"IN|LIKE|BETWEEN|IS)\s*[^\n]{0,60})", re.I)
AGGREGATES = re.compile(r"\b(COUNT|SUM|AVG|MIN|MAX|STDDEV|APPROX_\w+)\s*\(", re.I)


def brief(sql: str) -> dict:
    """The few things a person needs to write the question, and nothing else.

    Not the whole statement. A 200-line query with six CTEs is unreadable in a
    spreadsheet cell, and the parts that decide the QUESTION are the analyst's
    comments, the outermost SELECT list, the tables and the filters.
    """
    comments = [c.strip() for c in COMMENT.findall(sql or "") if c.strip()]
    select = SELECT_LIST.search(sql or "")
    cols = " ".join((select.group(1) if select else "").split())[:220]
    return {
        "analyst_comments": " | ".join(comments)[:300],
        "select_list": cols,
        "tables": ", ".join(dict.fromkeys(TABLES.findall(sql or "")))[:220],
        "filters": " | ".join(dict.fromkeys(
            " ".join(f.split()) for f in FILTERS.findall(sql or "")))[:300],
        "aggregates": ", ".join(dict.fromkeys(
            a.upper() for a in AGGREGATES.findall(sql or ""))),
        "sql_lines": str(len((sql or "").splitlines())),
    }


FIELDS = ["id", "table", "old_question", "analyst_comments", "aggregates",
          "select_list", "filters", "tables", "sql_lines", "new_question",
          "drafted_by"]


def main(sheet: str, out: str, reason: str = "does_not_match") -> None:
    rows = [r for r in csv.DictReader(open(sheet, encoding="utf-8"))
            if (r.get("reason") or "") == reason]
    with open(out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({"id": row["id"], "table": row.get("table", ""),
                             "old_question": row.get("question", ""),
                             "new_question": "", "drafted_by": "",
                             **brief(row.get("gold_sql", ""))})
    print(f"{len(rows)} rows rejected as `{reason}` -> {out}")
    with_comments = sum(1 for r in rows
                        if brief(r.get("gold_sql", ""))["analyst_comments"])
    print(f"  {with_comments} of them carry an analyst comment saying what the "
          "query is for")
    print("  fill `new_question` and `drafted_by`, then merge and re-review")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("usage: redraft_worksheet.py REVIEW.csv OUT.csv [reason]")
    main(sys.argv[1], sys.argv[2], *sys.argv[3:4])
