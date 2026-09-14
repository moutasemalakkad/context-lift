# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""What a round cost, per arm, from the warehouse's own job ledger.

The second axis of value, and the one a buyer grasps first. An agent without
good context explores more: more schema reads, more retries, a wider scan to
reach the same row. So context can pay for itself in query bill even when the
accuracy verdict reads NOT PROVEN.

Cost needs no reserved half. It counts what the runs actually spent rather
than estimating what unseen questions would do, which makes it the same shape
as the safety line: a fact about these answers, not a claim about others.

WHY THIS MODULE EXISTS RATHER THAN A SCRIPT. The same arithmetic is needed in
three places -- the round that prints the cost, the tool that recovers it for
a round already paid for, and the test that proves the two agree. Three copies
of a price and a division is how a published figure ends up depending on which
copy somebody ran.

THE NUMBER THIS PROJECT WAS GETTING WRONG. Per-command `spent:` lines totalled
about $74 while the job ledger reported $149.61 across 2,686 jobs. The `spent:`
lines count MODEL calls only. They cannot count the SQL the agent writes,
because that query does not exist until the run does and it is billed to the
warehouse, not to the model API. So roughly half the bill was real, unreported,
and recoverable only from here.

TWO CONFOUNDS, PRINTED RATHER THAN HIDDEN.

- A wrong answer can be cheap and a right answer expensive. Cost per ANSWER and
  cost per CORRECT answer move in opposite directions when accuracy falls, so
  both are printed. Quoting only the first would advertise a saving that is
  really a loss.
- A job missing from history is not a free answer. It is an answer whose job
  ran under an identity this reader cannot see, or one older than the retention
  window. Those are counted and EXCLUDED, never treated as zero -- treating
  them as zero would make a round look cheaper the less of it could be read.
"""
from __future__ import annotations

from dataclasses import dataclass

# BigQuery on-demand, checked 2026-09-03. One number, one place: a price
# copied into a script and a report is a price that disagrees with itself the
# day it changes.
ON_DEMAND_USD_PER_TIB = 6.25
BYTES_PER_TIB = 1024 ** 4

# How far a printed cost may sit from the ledger before it is wrong. The
# ledger is the invoice; the printed figure is derived from the same bytes, so
# any real gap means a missing job or a stale price rather than rounding.
TOLERANCE = 0.05

TRUE_WORDS = ("1", "true", "yes")


def usd_for_bytes(billed: int) -> float:
    """On-demand dollars for a number of billed bytes."""
    return (billed / BYTES_PER_TIB) * ON_DEMAND_USD_PER_TIB


@dataclass(frozen=True)
class ArmCost:
    """One arm's bill, and the two ways of dividing it."""

    arm: str
    answers: int
    bytes_billed: int
    correct: int

    @property
    def usd(self) -> float:
        return usd_for_bytes(self.bytes_billed)

    @property
    def usd_per_answer(self) -> float | None:
        """None, not zero, when the arm has no readable answer. A cost of
        zero and a cost nobody could read are different facts."""
        return self.usd / self.answers if self.answers else None

    @property
    def usd_per_correct(self) -> float | None:
        """None when the arm got nothing right. Dividing by zero correct
        answers is not an infinite cost, it is an undefined one."""
        return self.usd / self.correct if self.correct else None


@dataclass(frozen=True)
class RoundCost:
    """Every arm, plus what could not be read."""

    arms: tuple[ArmCost, ...] = ()
    answers_with_a_job: int = 0
    jobs_not_in_history: int = 0

    @property
    def total_usd(self) -> float:
        return sum(arm.usd for arm in self.arms)

    def text(self) -> str:
        """The cost line, printed beside the verdict and the safety line.

        Three decisions, not one. The lift says whether the context is worth
        keeping, the safety line says whether keeping it costs a working
        answer, and this says what running it costs in money.
        """
        if not self.arms:
            return ("  cost: not recovered. No answer carried a job id the "
                    "warehouse still has, so this round's query bill cannot "
                    "be read. It is not zero")
        lines = [f"  cost: ${self.total_usd:.4f} of query bill across "
                 f"{self.answers_with_a_job} answer(s)"]
        for arm in self.arms:
            per_answer = ("n/a" if arm.usd_per_answer is None
                          else f"${arm.usd_per_answer:.4f}")
            per_correct = ("n/a" if arm.usd_per_correct is None
                           else f"${arm.usd_per_correct:.4f}")
            lines.append(f"    {arm.arm:<14} ${arm.usd:.4f}  "
                         f"{per_answer}/answer  {per_correct}/correct answer")
        lines.append("    cost per ANSWER is the clean comparison. Cost per "
                     "CORRECT answer also carries the accuracy change, so it "
                     "is not a cost result on its own")
        if self.jobs_not_in_history:
            lines.append(
                f"    {self.jobs_not_in_history} job id(s) are not in the "
                f"warehouse's history, so those answers are EXCLUDED rather "
                f"than counted as free")
        lines.append("    model calls are billed separately and are on the "
                     "`spent:` line. Neither line is the whole bill")
        return "\n".join(lines)


def per_arm(rows, bytes_by_job: dict[str, int]) -> RoundCost:
    """Group scored rows into arm costs using the ledger's billed bytes.

    `rows` are the round's own scored rows: each needs `arm`, `job_id` and
    `correct`. `bytes_by_job` is what the warehouse says each job billed.

    A row whose job is absent from `bytes_by_job` is dropped from the
    arithmetic and counted, because an answer nobody can price must not make
    the round look cheaper.
    """
    totals: dict[str, list[int]] = {}
    with_a_job = 0
    unreadable = 0
    for row in rows:
        job = str(row.get("job_id") or "").strip()
        if not job:
            continue
        with_a_job += 1
        if job not in bytes_by_job:
            unreadable += 1
            continue
        arm = str(row.get("arm") or "")
        correct = str(row.get("correct") or "").strip().lower() in TRUE_WORDS
        entry = totals.setdefault(arm, [0, 0, 0])
        entry[0] += 1
        entry[1] += int(bytes_by_job[job] or 0)
        entry[2] += int(correct)
    arms = tuple(ArmCost(arm=arm, answers=count, bytes_billed=billed,
                         correct=right)
                 for arm, (count, billed, right) in sorted(totals.items()))
    return RoundCost(arms=arms, answers_with_a_job=with_a_job - unreadable,
                     jobs_not_in_history=unreadable)


def ledger_bytes(project: str, job_ids: list[str], days: int = 180,
                 region: str = "region-us") -> dict[str, int]:
    """`{job_id: total_bytes_billed}` from the project's own job history.

    A metadata read. It runs no gold SQL and scans no table, so recovering
    the cost of a round already paid for costs nothing beyond the query on
    INFORMATION_SCHEMA itself.

    The 180-day default is the retention window: ask for older jobs and they
    are simply absent, which `per_arm` counts as unreadable rather than free.

    Returns an empty dict rather than raising when the read fails. A round
    that scored correctly must not be lost because its invoice could not be
    looked up; the cost line says "not recovered" and the numbers that were
    earned still print.
    """
    if not job_ids:
        return {}
    try:
        from google.cloud import bigquery
    except ImportError:
        return {}
    quoted = ", ".join(f"'{job}'" for job in sorted(set(job_ids)))
    sql = (f"SELECT job_id, total_bytes_billed\n"
           f"FROM `{project}.{region}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT\n"
           f"WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), "
           f"INTERVAL {int(days)} DAY)\n"
           f"  AND job_id IN ({quoted})")
    # With the framework's identity, like every other Google client here:
    # a read that carries no user agent is usage nobody can attribute back,
    # and a test in this repository fails on one that does not.
    from . import identity
    try:
        client = bigquery.Client(project=project,
                                 client_info=identity.client_info())
        rows = client.query(sql).result()
    except Exception:  # noqa: BLE001 - reported as "not recovered", never raised
        return {}
    return {row.job_id: int(row.total_bytes_billed or 0) for row in rows}


def reconciles(printed_usd: float, ledger_usd: float,
               tolerance: float = TOLERANCE) -> bool:
    """Does a printed cost agree with the invoice, within `tolerance`?

    Relative to the LEDGER, because the ledger is the truth and the printed
    figure is the claim. Comparing to the printed figure instead would let a
    number that reports half the bill pass by being half of itself.

    Both zero reconciles: a round that billed nothing and reported nothing
    agrees. A ledger of zero with a printed figure above it does not, because
    that is a charge nobody was billed for.
    """
    if ledger_usd == 0:
        return printed_usd == 0
    return abs(printed_usd - ledger_usd) / ledger_usd <= tolerance


def reconciliation_note(printed_usd: float, ledger_usd: float,
                        tolerance: float = TOLERANCE) -> str:
    """One sentence a person can act on, either way."""
    if reconciles(printed_usd, ledger_usd, tolerance):
        return (f"  cost reconciles: printed ${printed_usd:.4f} against "
                f"${ledger_usd:.4f} in the job ledger, within "
                f"{tolerance:.0%}")
    share = (printed_usd / ledger_usd) if ledger_usd else 0.0
    return (f"  COST DOES NOT RECONCILE: printed ${printed_usd:.4f} against "
            f"${ledger_usd:.4f} in the job ledger, which is {share:.0%} of "
            f"it. A gap this size is a missing job or a stale price, not "
            f"rounding. Do not quote the printed figure until it is found")
