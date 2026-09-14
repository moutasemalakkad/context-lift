# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""What to write next, from the shape of what failed.

A round that ends NOT PROVEN or UNDERPOWERED is the normal input to the next one,
and the question a person actually has is "so what do I write". Guessing costs
a round: on one estate a curator wrote 29 rules against a bucket that rules
cannot move, and the holdout did not shift a point.

Every entry maps one failure shape to one lever, and two of them say **do not
write context at all**. That is the part worth having. A question whose gold
never ran is a broken answer key, and writing a rule against it buys nothing
and hides the real fault behind a round of work.

The map is a starting point, not a verdict. It reads a count, not a cause, so
the sentence it prints names the evidence and leaves the decision with the
person: three failing pairs read side by side beat any rule of thumb here.
"""
from __future__ import annotations

from dataclasses import dataclass

# The lever names, so a caller can compare rather than match a sentence.
DEFINITION = "a definition line"
OVERVIEW = "a table overview"
EXAMPLES = "worked examples"
TABLE_LIST = "the table list"
FIX_THE_GOLD = "fix the gold, not the context"
READ_THE_PAIRS = "read three failing pairs first"


@dataclass(frozen=True)
class Lever:
    """One suggestion: what to write, and why this bucket suggests it."""

    bucket: str
    lever: str
    why: str

    def line(self, count: int, total: int) -> str:
        return (f"{count} of {total} failures are {self.bucket}: {self.lever}. "
                f"{self.why}")


# Ordered by how often the bucket was the largest on a real run, so the table
# reads top-down like the runs did.
LEVERS: dict[str, Lever] = {
    "WRONG_VALUES": Lever(
        "WRONG_VALUES", DEFINITION,
        "the shape is right and the numbers are not, which is what a metric "
        "meaning something different from what the model assumed looks like"),
    "GRAIN": Lever(
        "GRAIN", DEFINITION,
        "one row per order against one row per line item is a definition, not "
        "a schema fact: nothing in the columns says which one counts"),
    "FILTER": Lever(
        "FILTER", DEFINITION,
        "the rows included differ, so the rule about which rows count is "
        "missing rather than the query being wrong"),
    "DATE": Lever(
        "DATE", DEFINITION,
        "a fiscal calendar, a week start or a time zone is written down "
        "somewhere in the business and nowhere in the schema"),
    "SCHEMA": Lever(
        "SCHEMA", OVERVIEW,
        "the model reached for a column or a table that is not there, which "
        "is a gap in what the table says about itself"),
    "DIALECT": Lever(
        "DIALECT", EXAMPLES,
        "the SQL did not compile, and one worked query in the right dialect "
        "teaches that faster than any sentence about it"),
    "SHAPE": Lever(
        "SHAPE", EXAMPLES,
        "the answer has the right numbers in the wrong arrangement, and an "
        "example shows an arrangement where a rule only describes one"),
    "ROW_COUNT": Lever(
        "ROW_COUNT", EXAMPLES,
        "too many or too few rows is usually a join or a grouping, and an "
        "example carries both at once"),
    "COLUMN_COUNT": Lever(
        "COLUMN_COUNT", EXAMPLES,
        "the answer returns columns the question never asked for, or misses "
        "one: an example fixes the shape a sentence would have to describe"),
    "EMPTY_RESULT": Lever(
        "EMPTY_RESULT", DEFINITION,
        "zero rows from a query that ran is usually a filter value spelled "
        "differently from the data, which a definition can pin"),
    "DID_NOT_RUN": Lever(
        "DID_NOT_RUN", FIX_THE_GOLD,
        "the answer key's own query failed, so there is nothing to be right "
        "about. Writing context against this buys nothing and hides the fault"),
    "TIMEOUT": Lever(
        "TIMEOUT", FIX_THE_GOLD,
        "the query ran too long to finish, which is a question or a ceiling "
        "to change, not a thing any sentence in the context can fix"),
    "UNCLASSIFIED": Lever(
        "UNCLASSIFIED", READ_THE_PAIRS,
        "nothing matched a known shape, and a bucket guessed wrong sends a "
        "person to curate the wrong table"),
}


def for_bucket(bucket: str) -> Lever:
    """The lever for one bucket, or the read-the-pairs fallback."""
    return LEVERS.get(bucket, LEVERS["UNCLASSIFIED"])


def suggest(counts: dict[str, int], recall_misses: int = 0) -> list[str]:
    """What to write next, largest failure shape first.

    `recall_misses` is how many questions named a table the agent never
    resolved. That one is checked first and separately, because no amount of
    written context helps a model that cannot see the table: a miss is a
    certain failure and the fix is the table list, not a sentence.
    """
    lines: list[str] = []
    if recall_misses:
        lines.append(
            f"{recall_misses} question(s) used a table the agent never "
            f"resolved: {TABLE_LIST}. No written context reaches a table the "
            "model cannot see, so this comes before any of it")
    total = sum(counts.values())
    if not total:
        if not lines:
            lines.append("nothing failed, so there is no lever to pull")
        return lines
    for bucket, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if count:
            lines.append(for_bucket(bucket).line(count, total))
    lines.append("This reads a count, not a cause. Read three failing pairs "
                 "side by side before writing anything.")
    return lines
