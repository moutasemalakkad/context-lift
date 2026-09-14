# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The first document for each table, drafted from the schema.

The measured routing gain on one estate came from tables having a document at
all: 0.650 to 0.850 once each routed table had a page a person had written from
the schema. Writing that page by hand for 180 tables is the day nobody has, so
this drafts it mechanically and says so in every file. A person fills the Rules
section; the machine part is the column list nobody should retype.

Exports: draft_documents, draft_document, document_filename, draft_summary,
DRAFT_MARK, READ_BUDGET_CHARS, NAME_STYLES.

No model call, no rows read: everything here comes from INFORMATION_SCHEMA or
sqlite_master, so a draft costs one metadata query per view and can be re-run
on every schema change.
"""
from __future__ import annotations

from .backends_db import TableInfo

DRAFT_MARK = "<!-- drafted from the schema by contextlift draft-context; not reviewed -->"
# A catalog lookup returns at most this many characters of a document and
# truncates silently past it; one deployment passes 60000 because its rules
# sit at the end of the page. On a live estate 5 of 91 drafts were over it,
# the widest at 78223 characters from a 2288-column dataset: normal, not rare.
READ_BUDGET_CHARS = 60000
# dot is what DirectoryContextSource resolves first; underscore is the entry
# id shape one catalog deployment uses, so a push needs no rename
NAME_STYLES = ("dot", "underscore")
NO_DESCRIPTION = "(no table description in the schema)"
RULES_HINT = ('<!-- business rules a reader would need, one per line starting with "- ": '
              "what a row is, which filter every query needs, which column is the money -->")


def document_filename(table: TableInfo, style: str = "dot") -> str:
    """`dataset.table.md` (or `dataset__table.md` under the underscore style),
    both shapes DirectoryContextSource resolves; bare `table.md` on SQLite,
    which has no dataset."""
    if style not in NAME_STYLES:
        raise ValueError(f"name style must be one of {NAME_STYLES}, not {style!r}")
    if not table.dataset:
        return f"{table.name}.md"
    joiner = "." if style == "dot" else "__"
    return f"{table.dataset}{joiner}{table.name}.md"


def draft_document(table: TableInfo) -> str:
    """One markdown page: the name to spell, the description if the schema
    holds one, an empty Rules section, then every column with its type and
    description. Column descriptions are cell text, so a pipe in one is escaped."""
    # Rules before Columns: a catalog read is cut at a budget from the end,
    # and 5 of 91 live pages were over it. What a person wrote must survive
    # the cut; a column list is what the schema can give again.
    lines = [f"# {table.qualified}", "", DRAFT_MARK, "",
             table.description or NO_DESCRIPTION, "",
             "## Rules", "", RULES_HINT, "",
             "## Columns", "", "| column | type | description |", "|---|---|---|"]
    for column in table.columns:
        note = (column.description or "").replace("|", "\\|")
        lines.append(f"| {column.name} | {column.data_type} | {note} |")
    lines.append("")
    return "\n".join(lines)


def draft_documents(tables: list[TableInfo], style: str = "dot") -> dict[str, str]:
    """filename -> page, one per table."""
    return {document_filename(t, style): draft_document(t) for t in tables}


def draft_summary(tables: list[TableInfo], documents: dict[str, str] | None = None,
                  budget: int = READ_BUDGET_CHARS) -> dict:
    """What the schema gave and what it did not, so the printout can say how
    much of each draft is a description and how much is a bare column list;
    and which pages a catalog read would cut, since the command must not
    write documents its own reader truncates without saying so."""
    columns = [c for t in tables for c in t.columns]
    over = sorted(((name, len(text)) for name, text in (documents or {}).items()
                   if len(text) > budget), key=lambda pair: -pair[1])
    return {
        "documents": len(tables),
        "columns": len(columns),
        "described_columns": sum(1 for c in columns if c.description),
        "tables_without_description": sorted(t.name for t in tables if not t.description),
        "over_budget": over,
    }
