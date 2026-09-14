"""Load the BEAVER DW CSV export into one SQLite file.

Data prep only. Nothing here scores anything - the contextlift CLI does that.

Column types come from BEAVER's own dev_tables.json (Oracle types), mapped to
SQLite affinities:
  VARCHAR2 / CHAR / CLOB -> TEXT
  NUMBER / FLOAT         -> NUMERIC   (SQLite stores 1 as int, 30052.65 as real)
  DATE / TIMESTAMP       -> TEXT, rewritten from Oracle 19-DEC-24 to 2024-12-19
                            so SQLite date functions can read it

Every row is loaded. No sampling, no truncation: a gold query whose WHERE names
a value that was cut returns empty, and two empty result sets compare as equal,
which silently inflates every accuracy number downstream.
"""
from __future__ import annotations

import csv
import json
import pathlib
import re
import sqlite3
import sys

CSV_DIR = pathlib.Path(sys.argv[1])
TABLES_JSON = pathlib.Path(sys.argv[2])
OUT_DB = pathlib.Path(sys.argv[3])
# which database's tables to load from dev_tables.json (dw, csail_stata_nova, ...)
DB_ID = sys.argv[4] if len(sys.argv) > 4 else "dw"

MONTHS = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05",
    "JUN": "06", "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10",
    "NOV": "11", "DEC": "12",
}
ORACLE_DATE = re.compile(r"^(\d{2})-([A-Z]{3})-(\d{2})$")


def to_iso(value: str) -> str:
    """Rewrite Oracle DD-MON-RR as ISO. Anything else passes through."""
    m = ORACLE_DATE.match(value.strip().upper())
    if not m:
        return value
    day, mon, yy = m.groups()
    if mon not in MONTHS:
        return value
    # Oracle RR rule: 00-49 is 20xx, 50-99 is 19xx.
    century = "20" if int(yy) < 50 else "19"
    return f"{century}{yy}-{MONTHS[mon]}-{day}"


def sqlite_type(oracle_type: str) -> str:
    t = oracle_type.upper()
    if t.startswith(("NUMBER", "FLOAT", "BINARY_")):
        return "NUMERIC"
    return "TEXT"


def main() -> None:
    # keyed lowercase: the NW export ships AGENT_BUILDS.csv for a spec named
    # agent_builds; SQLite table names are case-insensitive, so the spec's
    # casing wins and the gold SQL (written against the spec) still matches
    specs = {
        v["table_name_original"].lower(): v
        for v in json.load(open(TABLES_JSON)).values()
        if v["db_id"] == DB_ID
    }
    if OUT_DB.exists():
        OUT_DB.unlink()
    conn = sqlite3.connect(OUT_DB)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")

    csv.field_size_limit(10_000_000)
    loaded = skipped = 0
    for path in sorted(CSV_DIR.glob("*.csv")):
        spec = specs.get(path.stem.lower())
        if spec is None:
            print(f"SKIP {path.stem}: not in dev_tables.json", flush=True)
            skipped += 1
            continue
        name = spec["table_name_original"]
        cols = spec["column_names_original"]
        types = spec["column_types"]
        date_idx = {
            i for i, t in enumerate(types) if t.upper().startswith(("DATE", "TIMESTAMP"))
        }
        ddl_cols = ", ".join(f'"{c}" {sqlite_type(t)}' for c, t in zip(cols, types))
        conn.execute(f'CREATE TABLE "{name}" ({ddl_cols})')
        placeholders = ", ".join("?" * len(cols))
        insert = f'INSERT INTO "{name}" VALUES ({placeholders})'

        rows = 0
        with open(path, newline="", encoding="utf-8", errors="replace") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header is not None and [h.strip() for h in header] != cols:
                print(f"WARN {name}: header differs from dev_tables.json", flush=True)
            batch = []
            for row in reader:
                if len(row) != len(cols):
                    row = (row + [None] * len(cols))[: len(cols)]
                for i in date_idx:
                    if row[i]:
                        row[i] = to_iso(row[i])
                batch.append(row)
                if len(batch) >= 20_000:
                    conn.executemany(insert, batch)
                    rows += len(batch)
                    batch = []
            if batch:
                conn.executemany(insert, batch)
                rows += len(batch)
        conn.commit()
        loaded += 1
        print(f"OK   {name}: {rows} rows", flush=True)

    conn.close()
    print(f"DONE loaded={loaded} skipped={skipped} db={OUT_DB}", flush=True)


if __name__ == "__main__":
    main()
