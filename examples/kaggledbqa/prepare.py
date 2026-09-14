# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Reshape the committed KaggleDBQA files into what the commands read.

For each database: questions.csv (id, question, gold_sql), golds.csv, a
schema prompt from the sqlite file, and docs.txt - the dataset's own
human-written column descriptions and value enums, verbatim. The docs are
the context under test: they are question-independent, which is why the
example needs no curate/holdout split (leak confirms it).

Everything is committed, so this runs offline in under a second.
"""
from __future__ import annotations

import csv
import json
import pathlib
import sqlite3

here = pathlib.Path(__file__).resolve().parent
DATABASES = ("StudentMathScore", "GeoNuclearData")

table_specs = {spec["db_id"]: spec for spec in json.load(open(here / "tables.json"))}

for database in DATABASES:
    out = here / "scratch" / database
    out.mkdir(parents=True, exist_ok=True)

    rows = json.load(open(here / f"{database}.json"))
    with open(out / "questions.csv", "w", newline="") as q, \
         open(out / "golds.csv", "w", newline="") as g:
        question_writer = csv.writer(q)
        question_writer.writerow(["id", "question", "gold_sql"])
        gold_writer = csv.writer(g)
        gold_writer.writerow(["id", "gold_sql"])
        for i, row in enumerate(rows):
            question_id = f"q{i:03d}"
            question_writer.writerow([question_id, row["question"], row["query"]])
            gold_writer.writerow([question_id, row["query"]])

    conn = sqlite3.connect(here / f"{database}.sqlite")
    schema_lines = []
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'"
    ):
        columns = ", ".join(
            f"{r[1]} {r[2]}"
            for r in conn.execute(f'PRAGMA table_info("{table}")')
        )
        schema_lines.append(f"{table}({columns})")
    (out / "schema.txt").write_text("\n".join(schema_lines))
    conn.close()

    spec = table_specs[database]
    doc_lines = ["DATABASE DOCUMENTATION", "", spec.get("db_overview", "").strip(), ""]
    tables = spec["table_names_original"]
    for (table_index, column), description in zip(
        spec["column_names_original"], spec["column_descriptions"]
    ):
        if table_index < 0 or not description or description == "*":
            continue
        doc_lines.append(f"- {tables[table_index]}.{column}: {description}")
    enums = spec.get("value_enums") or []
    if enums:
        doc_lines.append("")
        doc_lines.append("VALUE ENUMS (legal values of category columns):")
        for enum in enums:
            doc_lines.append(f"  {enum}")
    (out / "docs.txt").write_text("\n".join(doc_lines) + "\n")

    print(f"{database}: {len(rows)} questions -> {out}")
