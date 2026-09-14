# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The context map is a lookup over collect's payload and the schema: which
rule names which column, whose answer key uses it, who was credited or
blamed. Wrong edges would be silent, so the shapes are pinned here."""
from context_lift.contextmap import build_map, parse_schema


def test_parse_schema_reads_create_table_and_the_inline_shape():
    ddl = """CREATE TABLE IF NOT EXISTS "plants" (
        Id INTEGER PRIMARY KEY, Name TEXT, Status TEXT CHECK (Status IN ('a','b')),
        FOREIGN KEY (Id) REFERENCES other(Id));
    CREATE TABLE other(Id INT, Country TEXT)"""
    assert parse_schema(ddl) == {"plants": ["Id", "Name", "Status"], "other": ["Id", "Country"]}
    inline = "nuclear_power_plants(Id , Name , Country , Status )\nemp(name, age)\n"
    assert parse_schema(inline) == {"nuclear_power_plants": ["Id", "Name", "Country", "Status"],
                                    "emp": ["name", "age"]}
    assert parse_schema("just prose") == {}


def _verdict():
    return {
        "rules": [{"id": "R002", "phrase": "staff", "identifiers": ["emp.name"], "line_no": 2,
                   "text": '- "staff" -> emp.name'}],
        "rule_lines": [{"line_no": 2, "text": '- "staff" -> emp.name  # from q1', "parsed": True, "id": "R002"},
                       {"line_no": 3, "text": "- the emp table holds people", "parsed": False, "id": None}],
        "questions": [
            {"id": "q1", "question": "names?", "status": "fixed", "key_flagged": False,
             "gold_sql": "SELECT name FROM emp", "base_sql": ["SELECT age FROM emp"], "ctx_sql": ["SELECT name FROM emp"]},
            {"id": "q2", "question": "ages?", "status": "broke", "key_flagged": False,
             "gold_sql": "SELECT age FROM emp", "base_sql": ["SELECT age FROM emp"], "ctx_sql": ["SELECT name FROM emp"]},
        ],
        "attribution": {"impacts": [{"rule": {"id": "R002"}, "broke": [["q2", 1]], "fixed": [["q1", 1]],
                                     "signals": ["introduced_identifier"]}]},
    }


def test_build_map_links_rules_to_columns_questions_to_gold_columns_and_marks_strays():
    payload = build_map(_verdict(), {"emp": ["name", "age"]})
    by_type = {}
    for node in payload["nodes"]:
        by_type.setdefault(node["type"], []).append(node["id"])
    assert by_type == {"rule": ["R002", "line 3"], "question": ["q1", "q2"],
                       "table": ["emp"], "column": ["emp.name", "emp.age"]}
    edges = {(e["from"], e["to"], e["kind"]) for e in payload["edges"]}
    assert ("R002", "emp.name", "names") in edges
    assert ("line 3", "emp", "names") in edges                # prose line: token overlap with a table name
    assert ("q1", "emp.name", "gold") in edges and ("q2", "emp.age", "gold") in edges
    strays = [e for e in payload["edges"] if e["kind"] == "stray"]
    assert {(e["from"], e["to"], e["version"]) for e in strays} == {
        ("q1", "emp.age", "without rules"), ("q2", "emp.name", "with rules")}
    credited = next(e for e in payload["edges"] if e["kind"] == "credited")
    blamed = next(e for e in payload["edges"] if e["kind"] == "blamed")
    assert (credited["from"], credited["to"], credited["weak"]) == ("R002", "q1", False)
    assert (blamed["from"], blamed["to"], blamed["signals"]) == ("R002", "q2", ["introduced_identifier"])
    question = next(n for n in payload["nodes"] if n["id"] == "q2")
    assert question["status"] == "broke" and question["uses"] == ["emp.age"]


def test_build_map_without_a_schema_falls_back_to_the_rules_identifiers():
    payload = build_map(_verdict(), {})
    columns = [n["id"] for n in payload["nodes"] if n["type"] == "column"]
    assert columns == ["emp.name"]                             # age is unknown without a schema
    assert payload["schema_tables"] == 0 and payload["known_tables"] == 1
    weak = build_map({**_verdict(), "attribution": {"impacts": [
        {"rule": {"id": "R002"}, "broke": [["q2", 1]], "fixed": [], "signals": ["phrase_in_literal"]}]}}, {})
    assert next(e for e in weak["edges"] if e["kind"] == "blamed")["weak"] is True
