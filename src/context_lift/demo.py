# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""One command that shows what this tool does, with nothing installed.

    uvx contextlift demo

No clone, no key, no cloud, no network, under a minute. It builds a small
SQLite warehouse in a temporary directory, asks six questions twice - once
with a written business rule and once without - and prints the verdict.

**The model answers are canned, and the demo says so on screen.** A demo that
needed an API key would not run for the person deciding whether to try this,
and a scripted model proves nothing about a model. What it does prove is the
part that is hard to see from a README: what a round looks like, what a
verdict word means, and what the tool refuses to claim. The gates, the SQL
execution, the paired test and the verdict are the real code on the real
path, not a mock-up.

The rule the demo turns on is the smallest honest example of the whole thesis:

    revenue excludes lines that were Cancelled or Returned

Nothing in the schema says that. A column called `sale_price` does not know
which sales count. The question "what was revenue by month" is answerable two
ways and only one of them is what the business means. That is the gap this
tool measures.
"""
from __future__ import annotations

import os
import pathlib
import sys
import sqlite3
import tempfile

# Two months, a handful of lines each, and enough cancelled and returned rows
# that including them changes the answer by more than rounding. Small numbers
# on purpose: a reader can check the arithmetic by hand and see that the
# treatment arm is right and the control arm is wrong.
ROWS = [
    # (month, sale_price, status)
    ("2024-01", 100.0, "Complete"),
    ("2024-01", 50.0, "Complete"),
    ("2024-01", 40.0, "Cancelled"),
    ("2024-01", 10.0, "Returned"),
    ("2024-02", 200.0, "Complete"),
    ("2024-02", 25.0, "Complete"),
    ("2024-02", 75.0, "Cancelled"),
]

RULE = "Revenue is the sale price of order items that are neither Cancelled nor Returned."

QUESTIONS = [
    ("q1", "revenue by month",
     "SELECT month, ROUND(SUM(sale_price), 2) AS revenue FROM order_items "
     "WHERE status NOT IN ('Cancelled','Returned') GROUP BY month ORDER BY month"),
    ("q2", "total revenue",
     "SELECT ROUND(SUM(sale_price), 2) AS revenue FROM order_items "
     "WHERE status NOT IN ('Cancelled','Returned')"),
    ("q3", "revenue in January 2024",
     "SELECT ROUND(SUM(sale_price), 2) AS revenue FROM order_items "
     "WHERE status NOT IN ('Cancelled','Returned') AND month = '2024-01'"),
    ("q4", "how many order lines are there in all",
     "SELECT COUNT(*) AS n FROM order_items"),
    ("q5", "how many lines were cancelled",
     "SELECT COUNT(*) AS n FROM order_items WHERE status = 'Cancelled'"),
    ("q6", "the highest single line sale price",
     "SELECT MAX(sale_price) AS top FROM order_items"),
]

# What a model writes without the rule, and with it. Canned, and named as
# canned wherever it is printed. The shape is the real failure this tool was
# built for: the answer looks right, runs without error, and is wrong by the
# amount of the excluded rows.
WITHOUT_RULE = {
    "q1": "SELECT month, ROUND(SUM(sale_price), 2) AS revenue FROM order_items "
          "GROUP BY month ORDER BY month",
    "q2": "SELECT ROUND(SUM(sale_price), 2) AS revenue FROM order_items",
    "q3": "SELECT ROUND(SUM(sale_price), 2) AS revenue FROM order_items "
          "WHERE month = '2024-01'",
}


def build_warehouse(directory: pathlib.Path) -> pathlib.Path:
    """A three-column table with a business rule that is not in the schema."""
    path = directory / "demo.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE order_items (month TEXT, sale_price REAL, status TEXT)")
        connection.executemany(
            "INSERT INTO order_items VALUES (?, ?, ?)", ROWS)
    return path


def answers(with_rule: bool) -> dict[str, str]:
    """The canned SQL for one arm, keyed by question id."""
    return {qid: (gold if with_rule else WITHOUT_RULE.get(qid, gold))
            for qid, _question, gold in QUESTIONS}


def _painter(out):
    """Return a colour function, but only when a person is watching it happen.

    Colour exists in `demo` for one reason: the recorded terminal demo in the
    README. Anything that reads this output as data gets exactly the plain
    text it got before -- a pipe or a file (`sys.stdout.isatty()` is false), a
    test that passes its own `out`, and a terminal whose owner set `NO_COLOR`
    (the https://no-color.org convention).
    """
    live = (out is print and sys.stdout.isatty()
            and not os.environ.get("NO_COLOR"))
    if not live:
        return lambda text, code: text
    return lambda text, code: f"\033[{code}m{text}\033[0m"


def run(out=print) -> int:
    """Build, score both arms, print the verdict. Returns an exit code."""
    from .ablate import ablate, safety
    from .backends_db import make_executor
    from .harness import evaluate

    paint = _painter(out)
    out(paint("contextlift demo", "1") + " - no key, no cloud, no network\n")
    out("  The model answers below are CANNED so this runs with nothing")
    out("  installed. The warehouse, the SQL execution, the paired test and")
    out("  the verdict are the real code.\n")

    with tempfile.TemporaryDirectory() as directory:
        path = build_warehouse(pathlib.Path(directory))
        executor = make_executor(str(path))
        out(f"  {len(ROWS)} order lines, {len(QUESTIONS)} questions")
        out("  the rule: " + paint(RULE, "33"))
        out("  nothing in the schema says it: a column called sale_price does")
        out("  not know which sales count.\n")

        reports = {}
        for label, with_rule in (("without the rule", False), ("with the rule", True)):
            written = answers(with_rule)
            rows = [(qid, question, gold, written[qid])
                    for qid, question, gold in QUESTIONS]
            reports[label] = evaluate(executor, rows)
            report = reports[label]
            out(f"  {label:16s} "
                + paint(f"{report.execution.k} of {report.execution.n}", "1")
                + " right")

        comparison = ablate(reports["without the rule"], reports["with the rule"])
        low, high = comparison.lift_interval()
        out("")
        sure = safety(comparison)
        out("  lift "
            + paint(f"{comparison.lift * 100:+.1f} points", "1;32")
            + f" [{low * 100:+.1f}, {high * 100:+.1f}]")
        out("  safety "
            + paint(sure.word, "1;32" if sure.word == "SAFE" else "1;33")
            + f": {sure.fixed} fixed, {sure.broke} broke, of {sure.n}")
        out("")
        out("  Three questions moved. The other three never needed the rule,")
        out("  which is the point: a rule that helps everything is usually a")
        out("  rule that was already in the schema.")
        out("")
        out("  Two numbers, not one, and they answer different questions.")
        out("  The lift is a guess about questions nobody has asked yet, so")
        out("  on six questions no interval is narrow enough to prove much,")
        out("  and the real tool says so in a word instead of printing the")
        out("  number alone. The safety count is not a guess: nothing that")
        out("  was right without the rule became wrong with it. That stays")
        out("  true at any size.")
        out("")
        out("  next, on your own data:")
        out("    " + paint("contextlift gate ", "36")
            + " --db sqlite:///YOUR.db --golds golds.csv")
        out("    " + paint("contextlift round", "36")
            + " --db sqlite:///YOUR.db --golds golds.csv --spec spec/")
        out("    " + paint("contextlift next ", "36")
            + " --workspace .        # what to run, and why")
    return 0
