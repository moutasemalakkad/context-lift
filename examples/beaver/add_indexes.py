"""Index every column BEAVER lists as a join key.

The CSV export carries no keys or indexes; the MySQL dump does. Without them a
3-to-7 table join over a 650k-row table falls back to a nested scan, and one
gold query can run for hours. Indexing here restores what the warehouse
actually has - it does not change any answer, only how long it takes to get it.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

join_keys = json.load(open(sys.argv[1]))
db = pathlib.Path(sys.argv[2])

conn = sqlite3.connect(db)
existing = {
    row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
}

wanted = set()
for pair in join_keys:
    for side in pair:
        table, _, column = side.partition(".")
        if table in existing and column:
            wanted.add((table, column))

print(f"{len(join_keys)} join pairs -> {len(wanted)} distinct (table, column)", flush=True)

made = missing = 0
for table, column in sorted(wanted):
    columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    if column not in columns:
        print(f"MISS {table}.{column} (column not in table)", flush=True)
        missing += 1
        continue
    name = f"ix_{table}_{column}"[:60]
    conn.execute(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ("{column}")')
    made += 1
    if made % 25 == 0:
        print(f"  {made}/{len(wanted)} indexes", flush=True)

conn.commit()
conn.execute("ANALYZE")
conn.commit()
conn.close()
print(f"DONE indexes={made} missing_columns={missing}", flush=True)
