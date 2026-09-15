# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The cost table in docs/FIELD-NOTES.md, recomputed from committed data.

The walkthrough tells a reader that a handful of huge queries are most of a
mine's bill, and prints four numbers to prove it. Those numbers came from one
customer run's job history, which cannot ship. `tests/fixtures/mine_cost_bands
.json` is what can: the bytes billed by each job, values only.

This test exists because of a mistake it would have caught. The first version
of that table was computed with an ad-hoc query that divided bytes by 10^12
and then priced the result per TiB, which overstated every figure by 10%: the
run's real bill was $10.84 and the doc was about to say $11.91. The framework's
own `bigquery_usd` had it right all along.
"""
import json
import pathlib
import re

from context_lift.executor import bigquery_usd

HERE = pathlib.Path(__file__).parent
FIXTURE = HERE / "fixtures" / "mine_cost_bands.json"
WALKTHROUGH = HERE.parent / "docs" / "FIELD-NOTES.md"

GB = 10 ** 9

# The bands the walkthrough prints, largest first. A job billing 0 bytes is a
# dry run or a `SELECT 1 ... LIMIT 1` table probe: BigQuery bills neither.
BANDS = [
    ("over 10 GB", lambda b: b >= 10 * GB),
    ("1 to 10 GB", lambda b: GB <= b < 10 * GB),
    ("under 1 GB", lambda b: 0 < b < GB),
    ("free (dry run or table probe)", lambda b: b == 0),
]


def bands(billed: list[int]) -> list[tuple[str, int, float]]:
    """(name, job count, dollars) per band, in the walkthrough's order."""
    out = []
    for name, test in BANDS:
        hits = [b for b in billed if test(b)]
        out.append((name, len(hits), bigquery_usd(sum(hits))))
    return out


def test_the_bands_are_what_the_walkthrough_prints():
    data = json.loads(FIXTURE.read_text())
    billed = data["bytes_billed"]
    computed = bands(billed)

    # Every job is in exactly one band.
    assert sum(count for _name, count, _usd in computed) == len(billed)

    # The claim the table is there to make: a few queries are most of the
    # bill. Tested as a DISPROPORTION rather than against a fixed share,
    # because the point survives a different run and the exact percentage
    # does not. The first version of this assertion used 0.75, a number read
    # off data that turned out to be mostly other people's jobs.
    total = bigquery_usd(sum(billed))
    biggest = computed[0]
    cost_share = biggest[2] / total
    job_share = biggest[1] / len(billed)
    assert cost_share > 3 * job_share, (
        f"the over-10-GB band is {biggest[1]} of {len(billed)} jobs "
        f"({job_share:.0%}) and {cost_share:.0%} of the bill; the "
        "walkthrough's point is that those two numbers are very different")
    assert cost_share > 0.5, (
        f"the over-10-GB band is {cost_share:.0%} of the bill; the "
        "walkthrough calls it most of it")

    # And the four rows in the doc match, to the cent and the job.
    table = _walkthrough_rows()
    for name, count, usd in computed:
        assert name in table, f"the walkthrough has no row for {name!r}"
        said_count, said_usd = table[name]
        assert said_count == count, f"{name}: doc says {said_count} jobs, data has {count}"
        assert abs(said_usd - usd) < 0.005, f"{name}: doc says ${said_usd}, data gives ${usd:.2f}"


def _walkthrough_rows() -> dict[str, tuple[int, float]]:
    """The cost table, read out of the markdown as a reader sees it."""
    rows: dict[str, tuple[int, float]] = {}
    for line in WALKTHROUGH.read_text().splitlines():
        if not line.startswith("| "):
            continue
        cells = [c.strip().strip("*") for c in line.strip("|").split("|")]
        if len(cells) != 3 or not cells[0].isdigit():
            continue
        money = re.fullmatch(r"\$([\d,.]+)", cells[2])
        if not money:
            continue
        rows[cells[1]] = (int(cells[0]), float(money.group(1).replace(",", "")))
    return rows


def test_the_total_is_the_one_the_walkthrough_quotes():
    """The overrun is the whole reason the budget guard exists, so the two
    numbers either side of it are pinned: what was estimated, and what was
    billed."""
    data = json.loads(FIXTURE.read_text())
    billed = bigquery_usd(sum(data["bytes_billed"]))
    text = WALKTHROUGH.read_text()
    assert f"${billed:.2f}" in text, (
        f"the walkthrough should quote ${billed:.2f} as what the run billed")
    estimated = data["estimated_usd"]
    assert f"${estimated:.2f}" in text, (
        f"and ${estimated:.2f} as what the dry run estimated")
    # The direction matters and is the reason --budget watches the real
    # spend: this dry run priced the job at about five times what it cost.
    assert estimated > billed
