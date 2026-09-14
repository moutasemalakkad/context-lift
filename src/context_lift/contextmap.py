# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The context map: rules, the tables and columns they name, and the
questions whose answers touch those columns, as nodes and edges a caller
can draw or walk.

Everything here is a lookup over `bundle.collect`'s payload: which rule
names which identifier (`attribute.parse_rules`), which identifiers each
SQL text uses (`attribute.sql_tokens` against the known table and column
names), and which rule attribution credited or blamed for which question.
No rate, interval or p-value is computed; outcomes come from the verdict.
"""
from __future__ import annotations

import re

from .attribute import sql_tokens

# `table(col, col, ...)`, one table per line, the shape `generate` accepts
_INLINE_TABLE = re.compile(r"^\s*([A-Za-z_]\w*)\s*\((.*)\)\s*;?\s*$")
_CREATE_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?([A-Za-z_]\w*)[`\"\]]?\s*\(",
                           re.IGNORECASE)
_FIRST_WORD = re.compile(r"[`\"\[]?([A-Za-z_]\w*)[`\"\]]?")
_CONSTRAINT_WORDS = {"PRIMARY", "FOREIGN", "UNIQUE", "CONSTRAINT", "CHECK", "KEY", "INDEX"}


def _split_top_level(body: str) -> list[str]:
    """Split on commas outside parentheses: `CHECK (x IN ('a','b'))` keeps its commas."""
    items, depth, start = [], 0, 0
    for i, char in enumerate(body):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            items.append(body[start:i])
            start = i + 1
    items.append(body[start:])
    return items


def _columns(body: str) -> list[str]:
    columns = []
    for item in _split_top_level(body):
        match = _FIRST_WORD.match(item.strip())
        if not match or match.group(1).upper() in _CONSTRAINT_WORDS:
            continue
        columns.append(match.group(1))
    return columns


def parse_schema(text: str) -> dict[str, list[str]]:
    """Table name -> column names, from `CREATE TABLE` statements or from
    `table(col, ...)` lines. Types and constraints are dropped."""
    tables: dict[str, list[str]] = {}
    for match in _CREATE_TABLE.finditer(text):
        depth, start = 1, match.end()
        for i in range(start, len(text)):
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            if depth == 0:
                tables[match.group(1)] = _columns(text[start:i])
                break
    if tables:
        return tables
    for line in text.splitlines():
        match = _INLINE_TABLE.match(line)
        if match:
            tables[match.group(1)] = _columns(match.group(2))
    return tables


def build_map(verdict: dict, schema: dict[str, list[str]]) -> dict:
    """Nodes and edges for one comparison.

    Known identifiers are the schema's tables and columns plus every
    `table.column` a rule names, so a workspace without a schema copy (an
    imported arm) still maps rules to the columns they mention. Only tables
    and columns some rule, answer key or attempt touches become nodes.
    """
    known: dict[str, list[str]] = {t: list(cols) for t, cols in schema.items()}
    rules = {r["id"]: r for r in verdict.get("rules") or []}
    for rule in rules.values():
        for identifier in rule["identifiers"]:
            table, _, column = identifier.partition(".")
            known.setdefault(table, [])
            if column and column not in known[table]:
                known[table].append(column)
    by_upper = {t.upper(): t for t in known}

    def used(sql: str) -> tuple[set[str], set[str]]:
        """The known tables a SQL text names, and the `table.column` pairs
        whose table and column both appear in it."""
        tokens = sql_tokens(sql)
        tables = {name for upper, name in by_upper.items() if upper in tokens}
        columns = {f"{t}.{c}" for t in tables for c in known[t] if c.upper() in tokens}
        return tables, columns

    nodes: list[dict] = []
    edges: list[dict] = []
    touched_tables: set[str] = set()
    touched_columns: set[str] = set()

    def touch(tables: set[str], columns: set[str]) -> None:
        touched_tables.update(tables)
        touched_tables.update(c.partition(".")[0] for c in columns)
        touched_columns.update(columns)

    for line in verdict.get("rule_lines") or []:
        rule = rules.get(line["id"]) if line.get("id") else None
        node_id = line["id"] or f"line {line['line_no']}"
        target = line["text"].split("->", 1)[1] if "->" in line["text"] else line["text"]
        target = re.sub(r"#\s*from\s.*$", "", target)
        if rule:
            columns = set(rule["identifiers"])
            tables = {c.partition(".")[0] for c in columns}
        else:
            tables, columns = used(target)
        quoted = re.match(r'\s*-\s*"([^"]+)"', line["text"])      # the phrase, even when no table.column follows
        nodes.append({"id": node_id, "type": "rule",
                      "label": rule["phrase"] if rule else (quoted.group(1) if quoted else line["text"]),
                      "line_no": line["line_no"], "text": line["text"], "parsed": bool(rule),
                      "identifiers": sorted(columns) or sorted(tables)})
        touch(tables, columns)
        for target_id in sorted(columns) or sorted(tables):
            edges.append({"from": node_id, "to": target_id, "kind": "names"})

    for q in verdict.get("questions") or []:
        gold_tables, gold_columns = used(q.get("gold_sql") or "")
        touch(gold_tables, gold_columns)
        nodes.append({"id": q["id"], "type": "question", "label": q["id"], "question": q.get("question", ""),
                      "status": q["status"], "key_flagged": bool(q.get("key_flagged")),
                      "uses": sorted(gold_columns) or sorted(gold_tables)})
        for target_id in sorted(gold_columns) or sorted(gold_tables):
            edges.append({"from": q["id"], "to": target_id, "kind": "gold"})
        for version, sqls in (("without rules", q.get("base_sql") or []), ("with rules", q.get("ctx_sql") or [])):
            stray: set[str] = set()
            for sql in sqls:
                _tables, columns = used(sql)
                stray |= columns - gold_columns
            touch(set(), stray)
            for target_id in sorted(stray):
                edges.append({"from": q["id"], "to": target_id, "kind": "stray", "version": version})

    attribution = verdict.get("attribution") or {}
    for impact in attribution.get("impacts") or []:
        rule_id = impact["rule"]["id"]
        signals = list(impact.get("signals") or [])
        weak = signals == ["phrase_in_literal"]
        for kind, flips in (("blamed", impact.get("broke") or []), ("credited", impact.get("fixed") or [])):
            for qid, runs in flips:
                edges.append({"from": rule_id, "to": qid, "kind": kind, "runs": runs,
                              "signals": signals, "weak": weak})

    for table in sorted(touched_tables):
        nodes.append({"id": table, "type": "table", "label": table})
        for column in known.get(table, []):
            column_id = f"{table}.{column}"
            if column_id in touched_columns:
                nodes.append({"id": column_id, "type": "column", "label": column, "table": table})
    return {"nodes": nodes, "edges": edges,
            "schema_tables": len(schema), "known_tables": len(known)}
