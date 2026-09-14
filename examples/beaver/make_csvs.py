# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Reshape BEAVER's dev_dw.json into the CSVs the contextlift commands read.

Writes golds.csv (id, gold_sql), questions.csv (id, question, gold_sql - the
shape generate/leak/curate-examples take), and schema.txt (every table, one
line each, for the generate prompt).
"""
from __future__ import annotations

import csv
import json
import pathlib
import sys

here = pathlib.Path(__file__).resolve().parent
out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else here / "scratch"
out.mkdir(parents=True, exist_ok=True)

rows = json.load(open(here / "dev_dw.json"))
with open(out / "golds.csv", "w", newline="") as g, \
     open(out / "questions.csv", "w", newline="") as q:
    gold_writer = csv.writer(g)
    gold_writer.writerow(["id", "gold_sql"])
    question_writer = csv.writer(q)
    question_writer.writerow(["id", "question", "gold_sql"])
    for i, row in enumerate(rows):
        question_id = f"dw{i:03d}"
        gold_writer.writerow([question_id, row["sql"]])
        question_writer.writerow([question_id, row["question"], row["sql"]])

tables = json.load(open(here / "dev_tables.json"))
schema_lines = [
    f'{v["table_name_original"]}('
    + ", ".join(f"{c} {t}" for c, t in
                zip(v["column_names_original"], v["column_types"]))
    + ")"
    for v in sorted(tables.values(), key=lambda s: s["table_name_original"])
    if v["db_id"] == "dw"
]
(out / "schema.txt").write_text("\n".join(schema_lines))
print(f"{len(rows)} golds, {len(schema_lines)} tables -> {out}")
