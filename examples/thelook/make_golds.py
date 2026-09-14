#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Build the thelook gold set from the committed seed queries.

    python examples/thelook/make_golds.py

Reads `queries.sql`, merges `mined-golds.csv` when it is there, and writes two
files beside it:

    golds.csv        id, question, gold_sql, author, origin
    definitions.md   the business rules pulled OUT of the questions

Nothing is invented. Every question is the analyst's own comment, and every
query is the committed SQL, so the whole set is reproducible from one file in
the repository rather than from a workspace nobody else has.

Two rules do the work, and both come from reading the seed file rather than
from a plan.

**A definition in parentheses is context, not part of the question.** The
comments read like this:

    -- revenue by month, 2023 (revenue = sale price of items neither
    --                         cancelled nor returned)

That parenthetical is the exact business rule this framework exists to
measure. Leave it in the question and the bare arm gets it for free, so no
written context can ever show a lift and every round reads NOT PROVEN for a
reason that has nothing to do with the context. The question keeps the part
before the bracket; the rule goes to `definitions.md`, which is what the spec
repo's `rules/` and `vocabulary/` are built from.

**A repeated shape is a seed, not a gold.** The seed file says so in its own
header: a few shapes repeat with a different literal so that mining sees them
more than once. As golds they would be template twins, and a twin that lands
on opposite sides of the curate and holdout split turns answering the held-out
question into substitution. They are skipped, counted, and the reason is
printed.

The author of every question here is the repository, and the origin is "SQL
first": the query was written first and its comment describes it. That is
recorded per row because a set mixing SQL-first questions with model drafts
and human rewrites behaves differently, and six months on nobody remembers
which rows were which.
"""
from __future__ import annotations

import csv
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).parent
SEED = HERE / "queries.sql"
GOLDS = HERE / "golds.csv"
# Golds that came from the query log rather than from the seed file. Committed
# separately so `golds.csv` stays something this script produces in full: a
# hand-edited output nobody can regenerate is a file that drifts.
MINED = HERE / "mined-golds.csv"
# Golds written to widen coverage, rather than taken from the seed file or
# mined from the log. Committed separately for the same reason as the mined
# ones: `golds.csv` stays something this script produces in full, and a
# hand-edited output nobody can regenerate is a file that drifts.
AUTHORED = HERE / "authored-golds.csv"
DEFINITIONS = HERE / "definitions.md"

# "same shape, 2024" and "same shape, another category". The seed file's own
# header explains these: they exist so mining sees a shape more than once.
REPEAT = re.compile(r"^same shape\b", re.I)

# The trailing "(revenue = ...)" on a description. Only the LAST bracket group
# is treated as a definition, and only when it contains an "=" or the word
# "only", because "top 5 brands by revenue inside one category" has no bracket
# and "(no order item at all)" is part of the question, not a rule.
DEFINITION = re.compile(r"\s*\(([^()]*)\)\s*$")
LOOKS_LIKE_A_RULE = re.compile(r"=|\bonly\b|\bover\b", re.I)

AUTHOR = "repo"
ORIGIN = "SQL first: the query was written first and its comment describes it"


def read_pairs(text: str) -> list[tuple[str, str]]:
    """(description, sql) for every query, in file order.

    A description is the comment line directly above a statement. The file's
    opening block comment has no statement under it, so it never becomes a
    pair.
    """
    pairs: list[tuple[str, str]] = []
    description: str | None = None
    statement: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            if not statement:                      # a comment before a query
                description = stripped.lstrip("- ").strip()
            continue
        if not stripped:
            continue
        statement.append(line)
        if stripped.endswith(";"):
            sql = "\n".join(statement).rstrip().rstrip(";")
            pairs.append((description or "", sql))
            description, statement = None, []
    return pairs


def split_definition(description: str) -> tuple[str, str]:
    """The question, and the business rule that was hiding inside it.

    Returns ("", "") shaped as (question, definition). A bracket that does not
    look like a rule stays in the question, because "products never sold (no
    order item at all)" is one question and not a question plus a rule.
    """
    match = DEFINITION.search(description)
    if not match or not LOOKS_LIKE_A_RULE.search(match.group(1)):
        return description.strip(), ""
    return description[: match.start()].strip(), match.group(1).strip()


def build(text: str) -> tuple[list[dict], list[str], list[str]]:
    """The gold rows, the definitions pulled out, and the skipped repeats."""
    golds: list[dict] = []
    definitions: list[str] = []
    skipped: list[str] = []
    for description, sql in read_pairs(text):
        if REPEAT.match(description):
            skipped.append(description)
            continue
        question, definition = split_definition(description)
        if not question or not sql:
            continue
        golds.append({
            "id": f"tl{len(golds):03d}",
            "question": question,
            "gold_sql": sql,
            "author": AUTHOR,
            "origin": ORIGIN,
        })
        if definition and definition not in definitions:
            definitions.append(definition)
    return golds, definitions, skipped


def main() -> int:
    if not SEED.is_file():
        print(f"no seed file at {SEED}")
        return 1
    golds, definitions, skipped = build(SEED.read_text(encoding="utf-8"))

    with open(GOLDS, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["id", "question", "gold_sql", "author", "origin"])
        writer.writeheader()
        writer.writerows(golds)

    DEFINITIONS.write_text(
        "# Business rules taken out of the questions\n\n"
        "Each line was written in brackets on a query's comment in\n"
        "`queries.sql`. A definition inside a question is context the bare arm\n"
        "would get for free, so no written context could ever show a lift.\n"
        "These belong in the spec repo, under `rules/` and `vocabulary/`.\n\n"
        + "".join(f"- {line}\n" for line in definitions),
        encoding="utf-8")

    print(f"{len(golds)} golds from the seed queries")
    if MINED.is_file():
        with open(MINED, newline="", encoding="utf-8") as handle:
            extra = list(csv.DictReader(handle))
        golds.extend(extra)
        print(f"{len(extra)} more from {MINED.name}, mined from the query log")
        with open(GOLDS, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["id", "question", "gold_sql", "author", "origin"])
            writer.writeheader()
            writer.writerows(golds)
    if AUTHORED.is_file():
        with open(AUTHORED, newline="", encoding="utf-8") as handle:
            extra = list(csv.DictReader(handle))
        golds.extend(extra)
        print(f"{len(extra)} more from {AUTHORED.name}, written to widen coverage "
              "of the business rules and of SQL shape")
        with open(GOLDS, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["id", "question", "gold_sql", "author", "origin"])
            writer.writeheader()
            writer.writerows(golds)
    print(f"{len(golds)} golds -> {GOLDS.name}")
    print(f"{len(definitions)} definitions taken out of the questions -> {DEFINITIONS.name}")
    if skipped:
        print(f"{len(skipped)} repeated shapes left as seeds only, not golds: "
              + "; ".join(skipped))
        print("  a repeated shape is a template twin, and a twin split across "
              "curate and holdout turns the held-out answer into substitution")
    return 0


if __name__ == "__main__":
    sys.exit(main())
