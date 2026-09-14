# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""What each answer cost to run, per arm. Reads job history, runs no SQL.

The second axis of value, and the one a buyer grasps first. An agent without
good context explores more: more schema reads, more retries, a wider scan to
reach the same row. So context can pay for itself in query bill even when the
accuracy verdict reads NOT PROVEN.

Cost needs no reserved half. It counts what the runs actually spent rather
than estimating what unseen questions would do, which makes it the same shape
as the safety line: a fact about these answers, not a claim about others.

The round's run-out CSV records `job_id` for every answer that produced SQL
and throws the cost away. BigQuery keeps `total_bytes_billed` per job for 180
days, so the number is recoverable for a round already bought.

Two confounds, both printed rather than hidden:

- A wrong answer can be cheap and a right answer expensive. So cost per
  ANSWER and cost per CORRECT answer move in opposite directions when
  accuracy falls, and both are printed.
- A job missing from history is not a free answer. It is an answer whose job
  ran under an identity this reader cannot see, or older than the retention
  window. Those rows are counted and excluded, never treated as zero.
"""
from __future__ import annotations

import csv
import sys

# The price, the per-arm arithmetic and the reconciliation live in the module,
# not here. The same numbers are needed by the round that prints a cost and by
# the test that proves the two agree, and three copies of a price is how a
# published figure ends up depending on which copy somebody ran.
from context_lift.cost import ON_DEMAND_USD_PER_TIB, per_arm, reconciliation_note


def costs(project: str, job_ids: list[str], days: int = 180) -> dict:
    """`{job_id: bytes_billed}` from the project's own job history."""
    from google.cloud import bigquery
    client = bigquery.Client(project=project)
    quoted = ", ".join(f"'{j}'" for j in job_ids)
    sql = f"""
    SELECT job_id, total_bytes_billed, total_slot_ms
    FROM `{project}.region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
    WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(),
                                        INTERVAL {days} DAY)
      AND job_id IN ({quoted})
    """
    return {row.job_id: (row.total_bytes_billed or 0, row.total_slot_ms or 0)
            for row in client.query(sql).result()}


def main(run_rows: str, project: str) -> None:
    rows = list(csv.DictReader(open(run_rows, encoding="utf-8")))
    ids = [r["job_id"] for r in rows if (r.get("job_id") or "").strip()]
    print(f"{len(rows)} answers, {len(ids)} with a job_id\n")
    found = costs(project, ids)
    print(f"job history returned {len(found)} of {len(ids)}\n")

    # Only the bytes; the slot time is read above and not priced, because
    # on-demand billing is by bytes and pricing slots would double-count.
    cost = per_arm(rows, {job: billed for job, (billed, _slots) in found.items()})
    print(cost.text())
    print()
    # The ledger total is the same bytes summed a different way, so any gap
    # is a bug in the grouping rather than a real disagreement. Printed
    # anyway: this is the one place both numbers exist side by side.
    ledger = sum(billed for billed, _slots in found.values())
    from context_lift.cost import usd_for_bytes
    print(reconciliation_note(cost.total_usd, usd_for_bytes(ledger)))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("usage: answer_cost.py RUN_ROWS.csv PROJECT")
    main(sys.argv[1], sys.argv[2])
