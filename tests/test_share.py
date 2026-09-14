# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The only thing that leaves a customer's machine."""
from __future__ import annotations

import json

import pytest

from context_lift.share import (Pseudonyms, build, export, find_leaks, redact,
                            summary)

ROUND = {
    "verdict": "REAL GAIN",
    "reason": "the held-out questions moved +12.0 points",
    "lift_pp": 12.0, "p_value": 0.004, "n_holdout": 60,
    # None of the four below is in the allowlist.
    "question_text": "what is net sell-out for AMA?",
    "gold_sql": "SELECT SUM(x) FROM acme-prod.dw.fct_sales",
    "workspace_path": "/Users/someone/acme/workspace",
    "thought": "the model reasoned about acme-prod at length",
}
TABLES = {
    "acme-prod.dw.fct_sales": {"table_lift_pp": 9.0, "column_count": 40,
                               "overview_chars": 1820,
                               "overview": "Sales for Acme, one row per order"},
    "acme-prod.dw.dim_customer": {"table_lift_pp": 1.0, "column_count": 12},
}


def test_only_the_named_fields_leave():
    """An allowlist, not a blocklist. Removal only works against the fields
    somebody thought of, and the field added next month is the one that leaks."""
    payload = build(ROUND)

    assert payload["round"]["verdict"] == "REAL GAIN"
    assert payload["round"]["lift_pp"] == 12.0
    for leaked in ("question_text", "gold_sql", "workspace_path", "thought"):
        assert leaked not in payload["round"]


def test_a_table_travels_as_an_alias_and_its_overview_as_a_length():
    """The page a curator wrote is the customer's words. Its length is a number
    about the work, which is the part a colleague needs."""
    names = Pseudonyms()

    payload = build(ROUND, TABLES, names=names)

    # Aliases are handed out in sorted order of the real name, so the same
    # estate produces the same aliases on any machine and in any run.
    assert [row["table"] for row in payload["tables"]] == ["table_1", "table_2"]
    assert names.mapping["acme-prod.dw.dim_customer"] == "table_1"

    sales = next(r for r in payload["tables"] if r["table"] == "table_2")
    assert sales["overview_chars"] == 1820
    assert "overview" not in sales
    assert "acme-prod" not in json.dumps(payload)


def test_a_gate_travels_as_a_name_and_an_outcome_never_its_detail():
    """A detail sentence is written for the person at the keyboard, where the
    customer's strings are allowed. It quotes table and column names."""
    gates = [("leak", False, "3 questions appear in acme-prod.dw.fct_sales")]

    payload = build(ROUND, gates=gates)

    assert payload["gates"] == [{"name": "leak", "passed": False}]
    assert "acme-prod" not in json.dumps(payload)


def test_an_alias_is_the_same_table_in_march_and_in_june():
    """Two shares of one estate have to be comparable, or a colleague reading
    both learns nothing from the pair."""
    names = Pseudonyms()
    first = build(ROUND, TABLES, names=names)
    second = build(ROUND, TABLES, names=names)

    assert [r["table"] for r in first["tables"]] == [r["table"] for r in second["tables"]]


def test_a_saved_map_reloads_with_the_same_aliases(tmp_path):
    names = Pseudonyms()
    names.alias("acme-prod.dw.fct_sales")
    path = tmp_path / "pseudonyms.json"
    names.save(path)

    assert Pseudonyms.load(path).alias("acme-prod.dw.fct_sales") == "table_1"


def test_a_missing_map_is_empty_rather_than_an_error(tmp_path):
    """A first run has no map yet, and that is not a failure."""
    assert Pseudonyms.load(tmp_path / "absent.json").mapping == {}


def test_the_second_control_catches_a_name_that_survived_the_first():
    """The allowlist is code and code has bugs. An allowlisted field can still
    carry a name: a table inside an error string, a project id in a reason."""
    names = Pseudonyms()
    names.alias("acme-prod.dw.fct_sales")
    leaky = dict(ROUND, reason="the gate failed on acme-prod.dw.fct_sales")

    found = find_leaks(build(leaky, names=names), names.real_names)

    assert found == ["acme-prod.dw.fct_sales"]


def test_export_refuses_the_whole_payload_rather_than_redacting_the_hit():
    """A payload that needed redacting was built by code that does not
    understand its inputs, and the next one is wrong where nobody looks."""
    names = Pseudonyms()
    names.alias("acme-prod.dw.fct_sales")
    leaky = dict(ROUND, reason="failed on acme-prod.dw.fct_sales")

    with pytest.raises(SystemExit) as refused:
        export(leaky, names=names)

    assert "acme-prod.dw.fct_sales" in str(refused.value)
    assert "Nothing was written" in str(refused.value)


def test_a_forbidden_string_that_is_not_a_table_name_is_also_caught():
    """A project id or a customer's name never seen as a table still must not
    travel."""
    with pytest.raises(SystemExit):
        export(dict(ROUND, reason="run on acme-prod"),
               extra_forbidden=["acme-prod"])


def test_a_clean_payload_exports():
    payload = export(ROUND, TABLES, names=Pseudonyms())

    assert payload["round"]["verdict"] == "REAL GAIN"
    assert len(payload["tables"]) == 2


def test_the_match_is_case_insensitive():
    """`ACME-PROD` in an error string is the same leak as `acme-prod`."""
    assert find_leaks({"a": "ran on ACME-PROD"}, ["acme-prod"]) == ["acme-prod"]


def test_redact_replaces_the_longer_name_first():
    """`p.d.orders` contains `orders`; replacing the short one first would
    leave `p.d.table_2` and read as if two tables were involved."""
    names = Pseudonyms()
    names.alias("orders")
    names.alias("p.d.orders")

    assert redact("p.d.orders joins orders", names) == "table_2 joins table_1"


def test_the_summary_says_what_is_not_in_the_file():
    """Printed so nobody sends a file they have not looked at."""
    text = summary(build(ROUND, TABLES, [("leak", False, "detail")], Pseudonyms()))

    assert "verdict: REAL GAIN" in text
    assert "gates failed: leak" in text
    assert "no question text, no SQL, no real table names" in text
