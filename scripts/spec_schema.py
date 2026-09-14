#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Write one `spec/schema/` file per table, from INFORMATION_SCHEMA.

    python scripts/spec_schema.py --project P --tables-file missing.txt \
                                  --out spec/schema \
                                  --search-projects acme-analytics-p,acme-analytics-pp

Free: `INFORMATION_SCHEMA.COLUMNS` is metadata and BigQuery does not bill it.

**Why this is a script and not a chat command.** An estate's first round read
3% table coverage because the spec named 27 tables and the golds needed 65,
so the agent was asked 38 questions about data it had never been handed and
wrote nothing at all (D67). The fix was 61 of these files. They were produced
by typing a query into a chat window, which means the next person cannot
reproduce them, and the second time they were needed the query had to be
reconstructed from a comment inside one of the outputs.

A column description is written EMPTY on purpose. This file exists so the
ARM carries the table, which is what the golds need. Descriptions are the
authored context and belong in `spec/authored/`, which the export merges on
top of this directory. Writing the estate's own descriptions here would put
them in the CONTROL arm as well, and then the measured lift would credit
authored context for work the customer's own build already did.

A table name may be given two-part (`dataset.table`) or three-part
(`project.dataset.table`). Two-part names are searched for across
`--search-projects` in order, because a gold's SQL usually writes the short
form while the spec file has to name the project: two datasets in different
projects can share a name, and the arm needs the one the query can actually
read.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

# BigQuery types whose column list is not useful to an agent. Kept as a
# comment rather than a filter: the arm is better off carrying a STRUCT
# column it cannot use than missing the table entirely.


def rows_for(client, project: str, dataset: str, tables: list[str]) -> dict:
    """{table: [(column, type)]} for one dataset, in one query."""
    names = ", ".join(f"'{t}'" for t in tables)
    sql = (f"SELECT table_name, column_name, data_type "
           f"FROM `{project}.{dataset}`.INFORMATION_SCHEMA.COLUMNS "
           f"WHERE table_name IN ({names}) "
           f"ORDER BY table_name, ordinal_position")
    out: dict[str, list[tuple[str, str]]] = {}
    for row in client.query(sql).result():
        out.setdefault(row.table_name, []).append((row.column_name, row.data_type))
    return out


def write_one(out_dir: pathlib.Path, project: str, dataset: str, table: str,
              columns: list[tuple[str, str]]) -> pathlib.Path:
    path = out_dir / f"{project}.{dataset}.{table}.yaml"
    lines = [
        "# columns read from INFORMATION_SCHEMA. No descriptions: this file",
        "# exists so the ARM carries the table, which is what the golds need.",
        "# Descriptions are the authored context and belong in authored/.",
        "columns:",
    ]
    for name, kind in columns:
        lines.append(f"  {name}:")
        lines.append(f"    description: ''   # {kind}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def build(wanted: list[str], search: list[str], out_dir: pathlib.Path,
          reader) -> tuple[list[pathlib.Path], set[str]]:
    """Write the files. `reader(project, dataset, tables) -> {table: columns}`.

    Injected so the resolution rule below can be tested without a warehouse.
    """
    todo: dict[tuple[str, str], list[str]] = {}
    two_part: list[tuple[str, str]] = []
    for name in wanted:
        parts = name.split(".")
        if len(parts) == 3:
            todo.setdefault((parts[0], parts[1]), []).append(parts[2])
        elif len(parts) == 2:
            two_part.append((parts[0], parts[1]))
        else:
            print(f"  skipped {name!r}: not dataset.table or project.dataset.table")

    written: list[pathlib.Path] = []
    missing = set(wanted)
    done: set[tuple[str, str]] = set()

    for (project, dataset), tables in sorted(todo.items()):
        for table, columns in reader(project, dataset, sorted(set(tables))).items():
            written.append(write_one(out_dir, project, dataset, table, columns))
            done.add((dataset, table))
            missing.discard(f"{dataset}.{table}")
            missing.discard(f"{project}.{dataset}.{table}")

    # A two-part name resolves to the FIRST project that has it, not to every
    # project that has it. A production and a pre-production project commonly
    # hold the same dataset, and writing a file for each puts the SAME table
    # in the arm twice under two names -- a table list the agent has to
    # disambiguate for no reason. The first version of this script did that
    # and produced 22 files for 11 tables.
    for project in search:
        remaining: dict[str, list[str]] = {}
        for dataset, table in two_part:
            if (dataset, table) not in done:
                remaining.setdefault(dataset, []).append(table)
        if not remaining:
            break
        for dataset, tables in sorted(remaining.items()):
            for table, columns in reader(project, dataset, sorted(set(tables))).items():
                if (dataset, table) in done:
                    continue
                written.append(write_one(out_dir, project, dataset, table, columns))
                done.add((dataset, table))
                missing.discard(f"{dataset}.{table}")
    return written, missing


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True, help="the BILLING project for the query")
    ap.add_argument("--out", required=True, help="the spec/schema directory to write into")
    ap.add_argument("--tables-file", help="one table name per line")
    ap.add_argument("--tables", help="comma-separated table names")
    ap.add_argument("--search-projects", default="",
                    help="comma-separated projects to look a two-part name up "
                         "in, in order. Defaults to --project alone")
    args = ap.parse_args()

    wanted: list[str] = []
    if args.tables_file:
        wanted += [line.strip() for line in
                   pathlib.Path(args.tables_file).read_text().splitlines()
                   if line.strip() and not line.startswith("#")]
    if args.tables:
        wanted += [t.strip() for t in args.tables.split(",") if t.strip()]
    if not wanted:
        print("no tables asked for: pass --tables-file or --tables")
        return 1

    search = [p.strip() for p in args.search_projects.split(",") if p.strip()]
    search = search or [args.project]
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    from google.cloud import bigquery
    client = bigquery.Client(project=args.project)

    def reader(project: str, dataset: str, tables: list[str]) -> dict:
        try:
            return rows_for(client, project, dataset, tables)
        except Exception as error:                  # a project we cannot read
            print(f"  {project}.{dataset}: {type(error).__name__}")
            return {}

    written, missing = build(wanted, search, out_dir, reader)

    print(f"{len(written)} schema file(s) -> {out_dir}")
    for path in written[:40]:
        print(f"  {path.name}")
    if len(written) > 40:
        print(f"  ... and {len(written) - 40} more")
    if missing:
        print(f"\n{len(missing)} table(s) NOT found in any searched project:")
        for name in sorted(missing):
            print(f"  {name}")
        print("  a gold needing one of these cannot be answered by any arm; "
              "drop the gold or grant access")
    return 0


if __name__ == "__main__":
    sys.exit(main())
