#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""What a cleared 403 is actually worth, before anyone asks a customer to clear it.

    python scripts/no_access_yield.py candidates.csv

Free, no cloud call. Reads the `gate_status`, `gate_detail` and `gold_sql`
columns `mine` writes and answers four questions a meeting needs:

1. **Is the billing project blocked, or are some queries blocked?** A 403 on
   `POST /projects/PROJECT/jobs` reads like the project is shut out. If jobs
   from the same project ran in the same pass, it is not: something about
   those queries is refused, and the ask has to name that something. This
   prints how many jobs the platform accepted.

2. **Which projects do the refused queries name that the accepted ones do
   not?** VPC Service Controls evaluates every resource a BigQuery job
   reads, so a job that spans two perimeters is refused while either side
   alone is fine. The contingency table finds that pattern without being
   told any project name.

3. **How many of the refused rows would survive the gate if they ran?** A
   refused row has only been through the text checks; the execution classes
   (`ZERO_ROWS`, `TRUNCATED`, `OVER_BUDGET`, `TABLE_GONE`, `DEGENERATE`,
   `EXEC_FAIL`) never got to fire. Counting a refused row as a gold-in-
   waiting overstates the yield by however often those fire on rows that
   did run, which this measures on the same file.

4. **Would they be worth keeping?** Reconciliation shapes -- system A's copy
   of a number against system B's -- are dead weight in a gold set for the
   same reason a dashboard tile is: no written business rule changes the
   answer to "do these two tables agree". `mine.compares_two_copies` drops
   them when the copy is a `_backup` table, and cannot see it when the copy
   is another production system. Cross-perimeter queries are exactly where
   those hide, because the second system is what sits in the other
   perimeter.

Nothing here is specific to one estate: project names come from the SQL.
"""
from __future__ import annotations

import collections
import csv
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from context_lift.executor import strip_comments  # noqa: E402
from context_lift.mine import (_COMPARISON_VERDICT, _COPY_TABLE,  # noqa: E402
                           _ROW_COUNT_CHECK, compares_two_copies)

# Warnings ride along with `ok` and are not refusals: `usable_candidates.py`
# explains why dropping them throws away most of the set.
WARNINGS = frozenset({"MISSING_LITERAL", "MISSING_PROJECTION", "MISSING_ORDER",
                      "LITERAL_ON_WRONG_COLUMN"})
# Classes the gate decides from the SQL text alone. A row refused by the
# platform has already passed these, so the comparable population for a
# survival rate is rows that passed them too.
TEXT_ONLY = frozenset({"WHOLE_ROW", "MISSING_PARAMETER", "LIMIT_NO_ORDER", "CROSS_PROJECT",
                       "NON_SELECT", "PLACEHOLDER_COMMENT", "TEXT_AGGREGATE"})
REFUSAL = "NO_ACCESS"

_PROJECT_QUALIFIED = re.compile(r"`?([a-z][a-z0-9\-]{4,})\.([a-z0-9_]+)\.([a-z0-9_]+)`?", re.I)
_JOB_ENDPOINT = re.compile(r"/projects/([a-z0-9\-]+)/jobs")
# A question that reads as "do these two sources agree", which is what a
# reconciliation asks and an analyst does not.
_RECONCILES = re.compile(
    r"\b(?:difference|differing|diff|mismatch|mismatched|variance|discrepanc|"
    r"matching|matches|match)\b|_diff\b", re.I)


def defects(row: dict) -> set[str]:
    """The refusal classes on a row: warnings and `ok` are not refusals."""
    raw = (row.get("gate_status") or "").replace("(", "+").replace(")", "")
    return {t.strip() for t in raw.split("+") if t.strip()} - WARNINGS - {"ok"}


def projects_named(sql: str) -> frozenset[str]:
    """The projects a query reads, from its three-part table names."""
    return frozenset(m[0].lower() for m in _PROJECT_QUALIFIED.findall(sql or ""))


def report(rows: list[dict]) -> list[str]:
    """Every figure, as lines to print."""
    out: list[str] = []
    refused = [r for r in rows if REFUSAL in defects(r)]
    accepted = [r for r in rows if REFUSAL not in defects(r)]
    out.append(f"{len(rows)} candidates: {len(refused)} refused by the platform, "
               f"{len(accepted)} the platform accepted")
    endpoints = collections.Counter(
        m for r in refused for m in _JOB_ENDPOINT.findall(r.get("gate_detail") or ""))
    for project, n in endpoints.most_common():
        out.append(f"  all {n} refusals hit one endpoint: /projects/{project}/jobs"
                   if n == len(refused) else
                   f"  {n} refusals hit /projects/{project}/jobs")
    if endpoints and accepted:
        out.append(f"  and {len(accepted)} jobs were accepted in the same pass, so the "
                   "project is not shut out: specific queries are")

    out.append("")
    out.append("WHICH PROJECT A REFUSED QUERY READS THAT AN ACCEPTED ONE DOES NOT")
    everywhere = collections.Counter(
        p for r in rows for p in projects_named(r.get("gold_sql", "")))
    for project, _ in everywhere.most_common():
        reads = [r for r in rows if project in projects_named(r.get("gold_sql", ""))]
        hit = sum(REFUSAL in defects(r) for r in reads)
        rest = [r for r in rows if project not in projects_named(r.get("gold_sql", ""))]
        hit_rest = sum(REFUSAL in defects(r) for r in rest)
        out.append(f"  {project:<24} refused {hit:>4}/{len(reads):<4} ({_pct(hit, len(reads))})"
                   f"   elsewhere {hit_rest:>4}/{len(rest):<4} ({_pct(hit_rest, len(rest))})")

    top = [p for p, _ in everywhere.most_common(4)]
    out.append("")
    out.append("SPANNING: a job that reads two of them at once")
    for i, left in enumerate(top):
        for right in top[i + 1:]:
            both = [r for r in rows
                    if {left, right} <= projects_named(r.get("gold_sql", ""))]
            if not both:
                continue
            hit = sum(REFUSAL in defects(r) for r in both)
            out.append(f"  {left} + {right}: refused {hit}/{len(both)} ({_pct(hit, len(both))})")
            for solo_project in (left, right):
                # only this one out of every project in the table, so a third
                # project's refusal is not counted as this one's
                solo = [r for r in rows if projects_named(r.get("gold_sql", ""))
                        & set(top) == {solo_project}]
                hit_solo = sum(REFUSAL in defects(r) for r in solo)
                out.append(f"      {solo_project} alone: refused {hit_solo}/{len(solo)} "
                           f"({_pct(hit_solo, len(solo))})")

    out.append("")
    out.append("IF THE REFUSAL WERE CLEARED, HOW MANY WOULD PASS THE GATE")
    comparable = [r for r in accepted if not (defects(r) & TEXT_ONLY)]
    clean = [r for r in comparable if not defects(r)]
    text_clean_refused = [r for r in refused if defects(r) == {REFUSAL}]
    out.append(f"  rows that reached the platform and passed the text checks: {len(comparable)}")
    for klass, n in collections.Counter(
            "+".join(sorted(defects(r))) or "ok" for r in comparable).most_common():
        out.append(f"     {n:>4}  {klass}")
    if comparable:
        rate = len(clean) / len(comparable)
        out.append(f"  P(passes the gate | ran, text-clean) = {len(clean)}/{len(comparable)}"
                   f" = {rate:.1%}")
        out.append(f"  {len(text_clean_refused)} refused rows carry no other refusal, so a "
                   f"cleared policy is worth about {round(len(text_clean_refused) * rate)}"
                   " more candidates, not "
                   f"{len(text_clean_refused)}")
        for keep, label in ((0.254, "25.4% (the model that did not write the questions)"),
                            (0.531, "53.1% (the model that did, confounded)")):
            out.append(f"     at a review keep rate of {label}:"
                       f" {round(len(text_clean_refused) * rate * keep)} golds")

    out.append("")
    out.append("AND WHETHER THEY WOULD BE WORTH KEEPING")
    for label, group in (("refused", text_clean_refused), ("passed the gate", clean)):
        if not group:
            continue
        signals = [sum(bool(p.search(strip_comments(r.get("gold_sql") or "")))
                       for p in (_COPY_TABLE, _COMPARISON_VERDICT, _ROW_COUNT_CHECK))
                   for r in group]
        recon = sum(bool(_RECONCILES.search(r.get("question") or "")) for r in group)
        out.append(f"  {label} ({len(group)}):")
        out.append(f"     {sum(compares_two_copies(r.get('gold_sql') or '') for r in group)}"
                   " caught by compares_two_copies (needs 2 signals)")
        out.append(f"     {sum(s == 1 for s in signals)} carry exactly 1 signal, one short")
        out.append(f"     {recon} ask whether two sources agree ({_pct(recon, len(group))})")
        seen = collections.Counter(int(r.get("times_seen") or 0) > 1 for r in group)
        out.append(f"     {seen[True]} were run more than once, {seen[False]} exactly once")

    if comparable and text_clean_refused:
        rate = len(clean) / len(comparable)
        keeps = sum(not _RECONCILES.search(r.get("question") or "") for r in text_clean_refused)
        out.append("")
        out.append("THE SAME YIELD WITH THE RECONCILIATIONS DROPPED")
        out.append(f"  {keeps} of {len(text_clean_refused)} refused rows do not ask whether two "
                   f"sources agree -> {round(keeps * rate)} candidates ->"
                   f" {round(keeps * rate * 0.254)}-{round(keeps * rate * 0.531)} golds")
    return out


def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):.0f}%" if whole else "n/a"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print(f"usage: {pathlib.Path(argv[0]).name} CANDIDATES.csv", file=sys.stderr)
        return 2
    csv.field_size_limit(10 ** 9)
    with open(argv[1], newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    print("\n".join(report(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
