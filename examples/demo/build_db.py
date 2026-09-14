# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Build the example SQLite database (idempotent).

A tiny department/employee schema — enough to drive the example predictions
and golds. The .db is a build artifact (gitignored); this script regenerates it
from scratch each run, so `make example` is repeatable. Run directly or via
`make example`.
"""
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "demo.db")

_SCHEMA = """
CREATE TABLE department (id INTEGER PRIMARY KEY, name TEXT, budget INTEGER);
CREATE TABLE employee (
    id INTEGER PRIMARY KEY,
    name TEXT,
    dept_id INTEGER,
    salary INTEGER,
    hire_year INTEGER
);
"""

_DEPARTMENTS = [
    (1, "Engineering", 500000),
    (2, "Sales", 300000),
    (3, "Support", 200000),
]
_EMPLOYEES = [
    (1, "ann", 1, 120000, 2019),
    (2, "bob", 1, 110000, 2021),
    (3, "cara", 2, 90000, 2020),
    (4, "dan", 2, 95000, 2022),
    (5, "eve", 3, 70000, 2018),
]


def build(path: str = DB_PATH) -> str:
    """Create (or recreate) the example DB at `path` and return the path."""
    if os.path.exists(path):
        os.remove(path)  # rebuild from scratch so the script is idempotent
    con = sqlite3.connect(path)
    try:
        con.executescript(_SCHEMA)
        con.executemany("INSERT INTO department VALUES (?, ?, ?)", _DEPARTMENTS)
        con.executemany("INSERT INTO employee VALUES (?, ?, ?, ?, ?)", _EMPLOYEES)
        con.commit()
    finally:
        con.close()
    return path


if __name__ == "__main__":
    print(f"built {build()}")
