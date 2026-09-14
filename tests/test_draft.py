# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""One document per table, from the schema, marked as a draft in every file."""
import json
import pathlib
import sqlite3

from context_lift.backends_db import ColumnInfo, TableInfo, describe_tables, make_executor
from context_lift.cli import main
from context_lift.draft import (DRAFT_MARK, NO_DESCRIPTION, document_filename,
                            draft_document, draft_documents, draft_summary)


def _orders():
    return TableInfo(
        name="orders", qualified="shop.orders", dataset="shop",
        description="One row per order.",
        columns=(ColumnInfo("order_id", "INT64", "the key"),
                 ColumnInfo("status", "STRING", "Shipped | Complete"),
                 ColumnInfo("amount", "NUMERIC")))


def test_a_document_names_the_table_marks_itself_a_draft_and_lists_every_column():
    text = draft_document(_orders())

    assert text.splitlines()[0] == "# shop.orders"
    assert text.splitlines()[2] == DRAFT_MARK
    assert "One row per order." in text
    assert "| order_id | INT64 | the key |" in text
    assert "| status | STRING | Shipped \\| Complete |" in text     # a pipe cannot break the table
    assert "| amount | NUMERIC |  |" in text
    assert "## Rules" in text


def test_a_table_without_a_description_says_so_instead_of_leaving_a_gap():
    bare = TableInfo(name="t", qualified="t", columns=(ColumnInfo("a", "TEXT"),))

    assert NO_DESCRIPTION in draft_document(bare)
    assert document_filename(bare) == "t.md"                 # SQLite: no dataset
    assert document_filename(_orders()) == "shop.orders.md"  # the shape DirectoryContextSource reads first


def test_the_summary_counts_what_the_schema_gave():
    bare = TableInfo(name="t", qualified="t", columns=(ColumnInfo("a", "TEXT"),))

    assert draft_summary([_orders(), bare]) == {
        "documents": 2, "columns": 4, "described_columns": 2,
        "tables_without_description": ["t"], "over_budget": []}


def test_describe_tables_on_sqlite_reads_columns_in_order_with_no_descriptions(tmp_path):
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE emp(name TEXT, age INT)")
    con.execute("CREATE TABLE dept(id INT, title TEXT)")
    con.commit(); con.close()

    tables = describe_tables(make_executor(str(db)))

    assert [t.name for t in tables] == ["dept", "emp"]
    assert tables[1].columns == (ColumnInfo("name", "TEXT"), ColumnInfo("age", "INT"))
    assert tables[1].description is None and tables[1].dataset is None
    assert [t.name for t in describe_tables(make_executor(str(db)), ["emp"])] == ["emp"]


def test_draft_context_writes_one_file_per_table_and_the_recorder_lists_each(tmp_path, capsys):
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE emp(name TEXT, age INT)")
    con.execute("CREATE TABLE dept(id INT, title TEXT)")
    con.commit(); con.close()
    out = tmp_path / "docs"
    workspace = tmp_path / "ws"

    code = main(["--workspace", str(workspace), "draft-context", "--db", str(db), "--out", str(out)])

    assert code == 0
    assert sorted(p.name for p in out.iterdir()) == ["dept.md", "emp.md"]
    printed = capsys.readouterr().out
    assert "drafted 2 documents" in printed
    assert "by vocabulary, not by meaning" in printed
    manifest = json.loads(next(workspace.glob("runs/*draft-context*/manifest.json")).read_text())
    assert [pathlib.Path(r["path"]).name for r in manifest["outputs"]["out"]] == ["dept.md", "emp.md"]
    assert all(r["sha256"] for r in manifest["outputs"]["out"])


def test_the_summary_names_documents_a_catalog_read_would_truncate():
    """5 of 91 live drafts were over the read budget, the widest 78223
    characters; the command must not write what its reader cuts in silence."""
    wide = TableInfo(name="wide", qualified="shop.wide", dataset="shop",
                     columns=tuple(ColumnInfo(f"column_{i}", "STRING", "x" * 40) for i in range(30)))
    documents = draft_documents([wide, _orders()])

    summary = draft_summary([wide, _orders()], documents, budget=500)

    assert summary["over_budget"] == [("shop.wide.md", len(documents["shop.wide.md"]))]
    assert summary["over_budget"][0][1] > 500


def test_the_underscore_style_writes_the_entry_id_shape_a_catalog_push_needs():
    assert document_filename(_orders(), "underscore") == "shop__orders.md"
    assert list(draft_documents([_orders()], style="underscore")) == ["shop__orders.md"]
