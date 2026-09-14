# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""What changed under a gold set since it was last checked.

A gold set is an answer key, and an answer key has a hidden dependency: the
schema its SQL was written against. Change that schema and a gold can go
wrong in three ways, in increasing order of how hard it is to notice.

    1. The SQL fails.              Loud. The next gate catches it.
    2. A column is renamed.        The SQL fails, but only if it named that
                                   column; `SELECT *` keeps working.
    3. A VIEW IS REPLACED.         Silent. Same columns, same types, the SQL
                                   still runs, and the answer key now answers
                                   a different question.

Case 3 is the one this module exists for. `CREATE OR REPLACE VIEW` is a
one-line change a colleague makes without telling anyone, and nothing in a
gold set notices. That is why the manifest hashes a view's DEFINITION and not
only its column list: a redefinition with an identical shape is invisible to
every check that looks at columns alone.

WHY IT IS ALSO ABOUT COST. `gate` executes every gold's SQL, which is real
money on a warehouse. Re-gating the whole set to find the two golds that
drifted is the expensive way to learn a cheap fact. Metadata is cheap --
INFORMATION_SCHEMA is a catalog read, not a scan -- so the manifest is built
for almost nothing and names exactly which golds need re-gating.

WHAT IT DELIBERATELY DOES NOT DO. It does not decide whether a drifted gold is
now WRONG. Only a person reading the question beside the new schema can say
that. It says which golds changed underneath, and stops there, because a tool
that guessed would give a gold set a clean bill of health it had not earned.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import asdict, dataclass, field

from .gate import _TABLE_NAME
from .executor import strip_comments

# The categories, in the order a person should read them. A table that is gone
# makes every other finding about it moot, and a silently redefined view is
# the one worth reading before the noisy ones.
TABLE_GONE = "TABLE_GONE"
VIEW_REDEFINED = "VIEW_REDEFINED"
COLUMN_GONE = "COLUMN_GONE"
TYPE_CHANGED = "TYPE_CHANGED"
COLUMN_ADDED = "COLUMN_ADDED"

SEVERITY = (TABLE_GONE, VIEW_REDEFINED, COLUMN_GONE, TYPE_CHANGED, COLUMN_ADDED)

MEANING = {
    TABLE_GONE: "the table no longer resolves, so the gold cannot run",
    VIEW_REDEFINED: "same columns, different SQL behind them. The gold still "
                    "runs and may now answer a different question. This is "
                    "the one nothing else catches",
    COLUMN_GONE: "a column disappeared; the gold fails if it named it, and "
                 "silently changes shape if it used SELECT *",
    TYPE_CHANGED: "a column changed type, so a comparison or a cast may "
                  "behave differently",
    COLUMN_ADDED: "a column appeared. Harmless unless the gold used "
                  "SELECT *, in which case its result grew a column",
}


def tables_named(sql: str) -> list[str]:
    """Every table the SQL reads, first mention first, backticks removed.

    Reuses `gate._TABLE_NAME` rather than writing a second regex. Two readers
    of one pattern stay in step; two patterns drift, and this project has
    already paid for that once.
    """
    text = strip_comments(sql or "").replace("`", "")
    found: list[str] = []
    for name in _TABLE_NAME.findall(text):
        cleaned = name.strip(",;()").lower()
        # A subquery reads `FROM (SELECT ...` and there is no table there.
        if cleaned and not cleaned.startswith("(") and cleaned not in found:
            found.append(cleaned)
    return found


@dataclass
class TableShape:
    """What a table looked like: its columns, and for a view, its definition.

    `definition` is hashed rather than stored, because a view definition is
    SQL a customer wrote and this manifest is a file people pass around.
    """
    columns: list[tuple[str, str]] = field(default_factory=list)
    definition_hash: str = ""

    @classmethod
    def of(cls, columns, definition: str = "") -> "TableShape":
        return cls(columns=[(str(n).lower(), str(t).upper()) for n, t in columns],
                   definition_hash=content_hash(definition) if definition else "")

    def as_dict(self) -> dict:
        return {"columns": [list(c) for c in self.columns],
                "definition_hash": self.definition_hash}

    @classmethod
    def from_dict(cls, raw: dict) -> "TableShape":
        return cls(columns=[(c[0], c[1]) for c in raw.get("columns", [])],
                   definition_hash=raw.get("definition_hash", ""))


def content_hash(text: str) -> str:
    """A whitespace-insensitive hash, so reformatting a view is not a change.

    Deliberately the same normalisation `provenance.content_hash` uses: a rule
    reflowed to a different line width is the same rule, and a manifest that
    cried drift over a reformat would be switched off.
    """
    return hashlib.sha256(" ".join((text or "").split()).encode()).hexdigest()


@dataclass
class Drift:
    """One change under one gold. `detail` never carries customer SQL."""
    gold_id: str
    table: str
    category: str
    detail: str

    def line(self) -> str:
        return f"  [{self.category:14s}] {self.gold_id}  {self.table}: {self.detail}"


def build_manifest(golds, shape_of) -> dict:
    """{gold id: {table: shape}} for every table every gold reads.

    `shape_of(table)` returns a TableShape, or None when the table cannot be
    read. None is recorded as an absent shape rather than skipped, so a table
    that was unreadable at manifest time does not later look like a new one.
    """
    manifest = {"version": 1, "golds": {}}
    for gold in golds:
        gold_id = getattr(gold, "id", None) or gold["id"]
        sql = getattr(gold, "gold_sql", None) or gold.get("gold_sql", "")
        per_table = {}
        for table in tables_named(sql):
            shape = shape_of(table)
            per_table[table] = shape.as_dict() if shape else None
        manifest["golds"][gold_id] = per_table
    return manifest


def compare(old: dict, shape_of) -> tuple[list[Drift], list[str], list[str]]:
    """(drifts, unchanged gold ids, gold ids no longer in the manifest).

    Reads the CURRENT shape through `shape_of` and compares it to what the
    manifest recorded. Returns the unchanged list too, because the useful
    output of a drift check is usually "these 41 are fine, re-gate these 2".
    """
    drifts: list[Drift] = []
    unchanged: list[str] = []
    for gold_id, tables in (old.get("golds") or {}).items():
        moved = False
        for table, recorded in (tables or {}).items():
            now = shape_of(table)
            if recorded is None:
                # It could not be read before either. Not a change.
                if now is None:
                    continue
                drifts.append(Drift(gold_id, table, COLUMN_ADDED,
                                    "unreadable when the manifest was written, "
                                    "readable now"))
                moved = True
                continue
            before = TableShape.from_dict(recorded)
            if now is None:
                drifts.append(Drift(gold_id, table, TABLE_GONE,
                                    MEANING[TABLE_GONE]))
                moved = True
                continue
            was = dict(before.columns)
            has = dict(now.columns)
            for name in sorted(set(was) - set(has)):
                drifts.append(Drift(gold_id, table, COLUMN_GONE, name))
                moved = True
            for name in sorted(set(has) - set(was)):
                drifts.append(Drift(gold_id, table, COLUMN_ADDED, name))
                moved = True
            for name in sorted(set(was) & set(has)):
                if was[name] != has[name]:
                    drifts.append(Drift(gold_id, table, TYPE_CHANGED,
                                        f"{name}: {was[name]} -> {has[name]}"))
                    moved = True
            # Checked LAST and reported regardless of the column diff: a view
            # can be replaced with an identical shape, which is exactly the
            # change every column-level check misses.
            if (before.definition_hash and now.definition_hash
                    and before.definition_hash != now.definition_hash):
                drifts.append(Drift(gold_id, table, VIEW_REDEFINED,
                                    MEANING[VIEW_REDEFINED]))
                moved = True
        if not moved:
            unchanged.append(gold_id)
    drifts.sort(key=lambda d: (SEVERITY.index(d.category)
                               if d.category in SEVERITY else len(SEVERITY),
                               d.gold_id, d.table))
    return drifts, unchanged, []


def to_regate(drifts: list[Drift]) -> list[str]:
    """The gold ids a drift report says must be re-gated, in id order."""
    return sorted({drift.gold_id for drift in drifts})


def lines(drifts: list[Drift], unchanged: list[str], total: int,
          skipped: list[str] | None = None) -> list[str]:
    """What a person reads. The unchanged count comes FIRST.

    A drift report that opens with a list of problems reads as an alarm. The
    useful shape is "41 of 43 are untouched, re-gate these 2", because the
    decision it feeds is how much to re-run.
    """
    regate = to_regate(drifts)
    out = [f"drift: {len(unchanged)} of {total} gold(s) sit on unchanged "
           f"schema; {len(regate)} need re-gating"]
    # Printed BEFORE any finding, and before the clean line. A report that
    # says "nothing moved" while a check never ran is a report that implies
    # something it did not check. A skipped check is not a passed check.
    for note in (skipped or []):
        out.append(f"  SKIPPED {note}")
    if not drifts:
        out.append("  nothing moved underneath this gold set.")
        out.append("  Passing is not a certificate: a gold can be wrong on a "
                   "schema that never changed.")
        return out
    seen_category: set[str] = set()
    for drift in drifts:
        out.append(drift.line())
        if drift.category not in seen_category:
            seen_category.add(drift.category)
    out.append("")
    out.append("  re-gate: " + " ".join(regate))
    out.append("  This says what MOVED, not what is now wrong. Only a person "
               "reading the")
    out.append("  question beside the new schema can say that.")
    return out


def save(manifest: dict, path) -> None:
    pathlib.Path(path).write_text(json.dumps(manifest, indent=1, sort_keys=True))


def load(path) -> dict:
    return json.loads(pathlib.Path(path).read_text())


# --- reading the CURRENT shape out of a live database ----------------------
#
# `describe_tables` is the one reader `schema` and `draft-context` already
# share, "so the two cannot drift". This is its third consumer, for the same
# reason. What it does NOT carry is a view's DEFINITION, and that is the whole
# point of this module, so the definition is read separately.

def _sqlite_view_sql(executor) -> dict[str, str]:
    """{view name: its SQL} from sqlite_master. {} when it cannot be read."""
    result = executor.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'view'")
    if not result.ok:
        return {}
    # Rows are DICTS keyed by column name, not tuples. `row[0]` raised
    # KeyError: 0 the first time this ran against a real database.
    return {str(row.get("name", "")).lower(): str(row.get("sql") or "")
            for row in (result.rows or []) if row}


def _sqlite_view_columns(executor, view: str) -> list[tuple[str, str]]:
    """A sqlite VIEW's columns, which `describe_tables` does not return.

    `_sqlite_tables` filters `sqlite_master` to `type = 'table'`, so a view
    has no shape at all and records as unreadable -- and a view is precisely
    what this module exists to watch. Measured the first time this ran: a
    warehouse with one table and one view produced "1 table shape".

    `describe_tables` is deliberately NOT changed: `schema` and
    `draft-context` share it, and adding views would change what they emit.
    """
    # `pragma_table_info(...)` as a TABLE-VALUED FUNCTION, not `PRAGMA ...`.
    # The read-only rail rejects anything not starting with SELECT or WITH, so
    # the PRAGMA form comes back "rejected: statement is not read-only" -- the
    # rail working exactly as designed, and the reason this reads as a SELECT.
    result = executor.execute(
        f"SELECT name, type FROM pragma_table_info('{view}')")
    if not result.ok:
        return []
    return [(str(row.get("name", "")), str(row.get("type", "") or ""))
            for row in (result.rows or []) if row.get("name")]


def _bigquery_view_sql(executor, datasets: list[str]) -> dict[str, str]:
    """{dataset.table: view_definition} from INFORMATION_SCHEMA.VIEWS.

    A metadata read, not a scan, so it is close to free. Every dataset is
    tried and a failure on one costs only that dataset, because a denied read
    on an unrelated dataset must not silence the whole check.
    """
    found: dict[str, str] = {}
    for dataset in datasets:
        result = executor.execute(
            "SELECT table_schema, table_name, view_definition "
            f"FROM `{dataset}.INFORMATION_SCHEMA.VIEWS`")
        if not result.ok:
            continue
        for row in (result.rows or []):
            schema = row.get("table_schema", "")
            name = row.get("table_name", "")
            if schema and name:
                found[f"{schema}.{name}".lower()] = str(
                    row.get("view_definition") or "")
    return found


@dataclass
class LiveShapes:
    """`shape_of(table)` against a database, plus what it could not read.

    `skipped` is the honest half. When the view definitions cannot be read --
    no permission on INFORMATION_SCHEMA.VIEWS, or a backend that does not
    expose them -- the VIEW_REDEFINED check did not run, and a report that
    does not say so implies no view was redefined when nothing looked. A
    skipped check is not a passed check.
    """
    shapes: dict = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    def __call__(self, table: str):
        return self.shapes.get((table or "").lower())


def live_shapes(executor, tables: list[str] | None = None,
                datasets: list[str] | None = None) -> LiveShapes:
    """Read every table's current shape, and say what could not be read."""
    from .backends_db import describe_tables

    out = LiveShapes()
    infos = describe_tables(executor, tables, datasets)
    kind = getattr(executor, "kind", "sqlite")

    if kind == "bigquery":
        seen = sorted({info.dataset for info in infos if info.dataset})
        definitions = _bigquery_view_sql(executor, seen)
        if not definitions and seen:
            out.skipped.append(
                "VIEW_REDEFINED: could not read INFORMATION_SCHEMA.VIEWS on "
                + ", ".join(seen[:3])
                + ". A view replaced with the same columns will not be seen")
    else:
        definitions = _sqlite_view_sql(executor)

    for info in infos:
        key = (info.qualified or info.name).replace("`", "").lower()
        plain = info.name.lower()
        definition = definitions.get(key) or definitions.get(plain, "")
        shape = TableShape.of(
            [(column.name, getattr(column, "type", "") or "")
             for column in info.columns],
            definition)
        # Recorded under every spelling a gold might use, because a gold may
        # name `dataset.table` where the reader returns
        # `project.dataset.table` and a missed key reads as TABLE_GONE.
        for spelling in {key, plain,
                         ".".join(key.split(".")[-2:]) if "." in key else key}:
            out.shapes[spelling] = shape

    # Views, which `describe_tables` leaves out on sqlite. On BigQuery
    # INFORMATION_SCHEMA.COLUMNS already covers them, so there is nothing to
    # add and this loop does nothing.
    if kind != "bigquery":
        for view, definition in definitions.items():
            if view in out.shapes:
                continue
            columns = _sqlite_view_columns(executor, view)
            if columns:
                out.shapes[view] = TableShape.of(columns, definition)
    return out
