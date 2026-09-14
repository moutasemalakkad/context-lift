# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""A value dictionary read from the database: which values each
low-cardinality text column actually holds.

Why: on a real run the model wrote `Status = 'Shut down'` and
`Status = 'under construction'` where the data holds `Shutdown` and
`Under Construction`. Four of seventeen failures were that one mistake, and
a hand-written rule fixed one question at a time. The exact values are a
fact about the data, question-independent by construction, and one query
per column returns them. The output is a context file for `generate
--context` (or a block to paste into rules.md); `leak` checks it like any
other context. Exports: profile_values, render_values. SQLite lists tables
through sqlite_master and PRAGMA table_info; BigQuery through
INFORMATION_SCHEMA.COLUMNS of each dataset read, with one
APPROX_COUNT_DISTINCT query per table so a wide table costs one scan, not
one per column.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .backends_db import _wanted_in, datasets_to_read
from .executor import Executor


@dataclass(frozen=True)
class ColumnValues:
    table: str              # bare on SQLite; `dataset.table` on BigQuery
    column: str
    values: tuple[str, ...]
    distinct: int
    rows: int


def profile_values(
    executor: Executor,
    max_values: int = 20,
    min_rows: int = 1,
    report: Callable[[str], None] | None = None,
    datasets: list[str] | None = None,
    tables: list[str] | None = None,
) -> list[ColumnValues]:
    """Every text column with at most `max_values` distinct non-null values.

    Columns with more are categories no longer (names, ids, free text) and
    are skipped; the cap keeps the context short and the values exact.
    `report` gets one line per table a warehouse refused to scan (the byte
    ceiling), so a missing table is a printed fact, not a silent gap.
    `datasets` and `tables` scope a warehouse read the way `describe_tables`
    takes them; on SQLite `tables` keeps only those names.
    """
    wanted = set(tables or [])
    if getattr(executor, "kind", "sqlite") == "bigquery":
        return _profile_bigquery(executor, max_values, min_rows, report or (lambda line: None),
                                 datasets, wanted)
    listed = executor.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    if not listed.ok:
        raise RuntimeError(f"could not list tables: {listed.error}")
    found: list[ColumnValues] = []
    for table_row in listed.rows:
        table = table_row["name"]
        if wanted and table not in wanted:
            continue
        # the table-valued form: executors accept SELECT only (read-only rail)
        info = executor.execute(f"SELECT name FROM pragma_table_info('{table}')")
        if not info.ok:
            continue
        for column_row in info.rows:
            column = column_row["name"]
            stats = executor.execute(
                f'SELECT COUNT(DISTINCT "{column}") AS distinct_values, '
                f'COUNT("{column}") AS rows_with_value FROM "{table}" '
                f'WHERE typeof("{column}") = \'text\'')
            if not stats.ok or not stats.rows:
                continue
            distinct = stats.rows[0]["distinct_values"] or 0
            rows = stats.rows[0]["rows_with_value"] or 0
            if rows < min_rows or distinct == 0 or distinct > max_values:
                continue
            values = executor.execute(
                f'SELECT "{column}" AS v, COUNT(*) AS n FROM "{table}" '
                f'WHERE "{column}" IS NOT NULL AND typeof("{column}") = \'text\' '
                f'GROUP BY "{column}" ORDER BY n DESC, v')
            if not values.ok:
                continue
            found.append(ColumnValues(
                table=table, column=column,
                values=tuple(str(r["v"]) for r in values.rows),
                distinct=distinct, rows=rows))
    return found


def _profile_bigquery(executor, max_values: int, min_rows: int,
                      report: Callable[[str], None], datasets: list[str] | None,
                      wanted: set[str]) -> list[ColumnValues]:
    """Same result over BigQuery, one dataset after another. String columns
    come from INFORMATION_SCHEMA.COLUMNS; the distinct counts are approximate
    (APPROX_COUNT_DISTINCT, one query per table), so a column near the cap
    is confirmed by the exact GROUP BY before it is kept. The table is named
    `dataset.table`: one file now covers several datasets, and a bare name
    would say nothing about which one holds the values."""
    found: list[ColumnValues] = []
    for dataset in datasets_to_read(executor, sorted(wanted), datasets, command="profile"):
        found += _profile_dataset(executor, dataset, _wanted_in(wanted, dataset),
                                  max_values, min_rows, report)
    return found


def _profile_dataset(executor, dataset: str, wanted: set[str], max_values: int,
                     min_rows: int, report: Callable[[str], None]) -> list[ColumnValues]:
    dataset_id = dataset.rpartition(".")[2]
    columns = executor.execute(
        f"SELECT table_name, column_name FROM `{dataset}.INFORMATION_SCHEMA.COLUMNS` "
        f"WHERE data_type = 'STRING' ORDER BY table_name, ordinal_position")
    if not columns.ok:
        raise RuntimeError(f"could not list columns: {columns.error}")
    by_table: dict[str, list[str]] = {}
    for row in columns.rows:
        if wanted and row["table_name"] not in wanted:
            continue
        by_table.setdefault(row["table_name"], []).append(row["column_name"])
    found: list[ColumnValues] = []
    for table, names in by_table.items():
        full = f"`{dataset}.{table}`"
        shown = f"{dataset_id}.{table}"
        counts = ", ".join(
            f"APPROX_COUNT_DISTINCT(`{c}`) AS d_{i}, COUNT(`{c}`) AS r_{i}"
            for i, c in enumerate(names))
        stats = executor.execute(f"SELECT {counts} FROM {full}")
        if not stats.ok or not stats.rows:
            report(f"  {shown}: skipped ({stats.error or 'no rows'})")
            continue
        for i, column in enumerate(names):
            distinct = stats.rows[0].get(f"d_{i}") or 0
            rows = stats.rows[0].get(f"r_{i}") or 0
            if rows < min_rows or distinct == 0 or distinct > max_values:
                continue
            values = executor.execute(
                f"SELECT `{column}` AS v, COUNT(*) AS n FROM {full} "
                f"WHERE `{column}` IS NOT NULL GROUP BY v ORDER BY n DESC, v")
            if not values.ok:
                report(f"  {shown}.{column}: skipped ({values.error})")
                continue
            if len(values.rows) > max_values:
                continue
            found.append(ColumnValues(
                table=shown, column=column,
                values=tuple(str(r["v"]) for r in values.rows),
                distinct=len(values.rows), rows=rows))
    return found


def render_values(columns: list[ColumnValues]) -> str:
    """The dictionary as context lines the model can use verbatim."""
    lines = ["# Values the data holds (exact spelling and case)", ""]
    for c in columns:
        joined = ", ".join(f'"{v}"' for v in c.values)
        lines.append(f"- {c.table}.{c.column} is one of: {joined}")
    lines.append("")
    lines.append("Use these exact values in WHERE clauses; do not guess spellings or case.")
    return "\n".join(lines) + "\n"
