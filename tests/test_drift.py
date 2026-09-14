# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""A gold set sits on a schema, and the schema moves without telling anyone.

Three ways a gold goes wrong when the schema changes, in increasing order of
how hard it is to notice:

  1. the SQL fails                loud, the next gate catches it
  2. a column is renamed          fails only if the gold named that column
  3. A VIEW IS REPLACED           silent: same columns, same types, the SQL
                                  still runs, and the answer key now answers
                                  a different question

Case 3 is why this module hashes a view's DEFINITION and not only its column
list. `CREATE OR REPLACE VIEW` is a one-line change a colleague makes without
telling anyone, and it is invisible to every check that looks at columns.
"""
from __future__ import annotations

import pytest

from context_lift.drift import (
    COLUMN_ADDED, COLUMN_GONE, TABLE_GONE, TYPE_CHANGED, VIEW_REDEFINED,
    Drift, MEANING, SEVERITY, TableShape, build_manifest, compare, lines,
    tables_named, to_regate,
)


class Gold:
    def __init__(self, gold_id, sql):
        self.id, self.gold_sql = gold_id, sql


def source(shapes):
    return lambda table: shapes.get(table)


# --- reading the tables a gold depends on ----------------------------------

def test_it_finds_every_table_a_gold_reads():
    assert tables_named(
        "SELECT a FROM p.d.orders o JOIN p.d.users u ON o.uid = u.id"
    ) == ["p.d.orders", "p.d.users"]


def test_a_table_inside_a_subquery_still_counts():
    """The gold reads it, so a change to it can change the answer."""
    assert tables_named("SELECT a FROM (SELECT * FROM p.d.t) x") == ["p.d.t"]


def test_a_commented_out_table_does_not_count():
    assert tables_named("-- FROM commented.out\nSELECT 1 FROM `p.d.t`") == ["p.d.t"]


def test_a_table_is_named_once_however_often_it_appears():
    assert tables_named(
        "SELECT 1 FROM p.d.t a JOIN p.d.t b ON a.id = b.id") == ["p.d.t"]


# --- the silent case, which is the whole point -----------------------------

def test_a_view_replaced_with_identical_columns_is_caught():
    """The failure nothing else catches. Same names, same types, different
    SQL behind them: the gold runs, returns a number, and the number now
    answers a different question."""
    before = {"p.d.v": TableShape.of([("a", "INT64")], "SELECT a FROM t")}
    after = {"p.d.v": TableShape.of([("a", "INT64")], "SELECT a FROM t2")}
    manifest = build_manifest([Gold("g1", "SELECT a FROM p.d.v")], source(before))
    drifts, unchanged, _ = compare(manifest, source(after))
    assert [d.category for d in drifts] == [VIEW_REDEFINED]
    assert unchanged == []


def test_reformatting_a_view_is_not_a_change():
    """A definition reflowed to a different line width is the same
    definition. A check that cried drift over whitespace would be switched
    off, and then it would catch nothing at all."""
    before = {"p.d.v": TableShape.of([("a", "INT64")], "SELECT a FROM t")}
    after = {"p.d.v": TableShape.of([("a", "INT64")], "SELECT\n   a\nFROM  t")}
    manifest = build_manifest([Gold("g1", "SELECT a FROM p.d.v")], source(before))
    drifts, unchanged, _ = compare(manifest, source(after))
    assert drifts == []
    assert unchanged == ["g1"]


def test_a_base_table_has_no_definition_and_is_not_reported_as_redefined():
    """A base table's definition_hash is empty, and an empty hash must never
    compare unequal to a real one -- that would flag every base table."""
    before = {"p.d.t": TableShape.of([("a", "INT64")])}
    after = {"p.d.t": TableShape.of([("a", "INT64")])}
    manifest = build_manifest([Gold("g1", "SELECT a FROM p.d.t")], source(before))
    drifts, unchanged, _ = compare(manifest, source(after))
    assert drifts == []
    assert unchanged == ["g1"]


# --- the loud cases --------------------------------------------------------

def test_a_missing_table_is_reported():
    before = {"p.d.t": TableShape.of([("a", "INT64")])}
    manifest = build_manifest([Gold("g1", "SELECT a FROM p.d.t")], source(before))
    drifts, unchanged, _ = compare(manifest, source({}))
    assert [d.category for d in drifts] == [TABLE_GONE]


def test_a_dropped_column_names_the_column():
    before = {"p.d.t": TableShape.of([("a", "INT64"), ("b", "STRING")])}
    after = {"p.d.t": TableShape.of([("a", "INT64")])}
    manifest = build_manifest([Gold("g1", "SELECT a FROM p.d.t")], source(before))
    drifts, _, _ = compare(manifest, source(after))
    assert [(d.category, d.detail) for d in drifts] == [(COLUMN_GONE, "b")]


def test_a_retyped_column_shows_both_types():
    before = {"p.d.t": TableShape.of([("a", "INT64")])}
    after = {"p.d.t": TableShape.of([("a", "STRING")])}
    manifest = build_manifest([Gold("g1", "SELECT a FROM p.d.t")], source(before))
    drifts, _, _ = compare(manifest, source(after))
    assert drifts[0].category == TYPE_CHANGED
    assert "INT64 -> STRING" in drifts[0].detail


def test_a_new_column_is_reported_but_ranked_last():
    """A new column is usually harmless, and reporting it above a gone table
    would bury the finding that matters."""
    before = {"p.d.t": TableShape.of([("a", "INT64")])}
    after = {"p.d.t": TableShape.of([("a", "INT64"), ("z", "INT64")])}
    manifest = build_manifest([Gold("g1", "SELECT a FROM p.d.t")], source(before))
    drifts, _, _ = compare(manifest, source(after))
    assert [d.category for d in drifts] == [COLUMN_ADDED]
    assert SEVERITY.index(COLUMN_ADDED) == len(SEVERITY) - 1


def test_severity_order_puts_the_silent_case_near_the_top():
    """A person reads down until the rows stop mattering, so a silently
    redefined view must not sit below a pile of added columns."""
    assert SEVERITY.index(TABLE_GONE) == 0
    assert SEVERITY.index(VIEW_REDEFINED) == 1
    assert SEVERITY.index(VIEW_REDEFINED) < SEVERITY.index(COLUMN_ADDED)


# --- what it refuses to say ------------------------------------------------

def test_it_never_claims_a_drifted_gold_is_wrong():
    """Only a person reading the question beside the new schema can say that.
    A tool that guessed would hand a gold set a clean bill of health it had
    not earned."""
    before = {"p.d.t": TableShape.of([("a", "INT64")])}
    after = {"p.d.t": TableShape.of([("a", "STRING")])}
    manifest = build_manifest([Gold("g1", "SELECT a FROM p.d.t")], source(before))
    drifts, unchanged, _ = compare(manifest, source(after))
    text = "\n".join(lines(drifts, unchanged, 1))
    assert "not what is now wrong" in text
    assert "Only a person" in text


def test_a_clean_report_still_refuses_to_certify():
    """Same rule as the linter: passing is not a certificate. A gold can be
    wrong on a schema that never moved."""
    before = {"p.d.t": TableShape.of([("a", "INT64")])}
    manifest = build_manifest([Gold("g1", "SELECT a FROM p.d.t")], source(before))
    drifts, unchanged, _ = compare(manifest, source(before))
    text = "\n".join(lines(drifts, unchanged, 1))
    assert "nothing moved" in text
    assert "not a certificate" in text


def test_the_report_leads_with_what_is_unchanged():
    """A drift report that opens with a list of problems reads as an alarm.
    The decision it feeds is how much to re-run, so the count of untouched
    golds comes first."""
    before = {"p.d.t": TableShape.of([("a", "INT64")]),
              "p.d.u": TableShape.of([("b", "INT64")])}
    after = {"p.d.t": TableShape.of([("a", "STRING")]),
             "p.d.u": TableShape.of([("b", "INT64")])}
    golds = [Gold("g1", "SELECT a FROM p.d.t"), Gold("g2", "SELECT b FROM p.d.u")]
    manifest = build_manifest(golds, source(before))
    drifts, unchanged, _ = compare(manifest, source(after))
    first = lines(drifts, unchanged, 2)[0]
    assert "1 of 2" in first
    assert "unchanged schema" in first
    assert to_regate(drifts) == ["g1"]


def test_an_unreadable_table_stays_unreadable_without_being_a_change():
    """A table nobody could read at manifest time must not look like a new
    one later, or every run on a permission-limited estate reports drift."""
    manifest = build_manifest([Gold("g1", "SELECT a FROM p.d.secret")],
                              source({}))
    drifts, unchanged, _ = compare(manifest, source({}))
    assert drifts == []
    assert unchanged == ["g1"]


def test_every_category_carries_a_plain_words_meaning():
    """A category printed as a bare label is a label nobody can act on."""
    for category in SEVERITY:
        assert category in MEANING
        assert len(MEANING[category].split()) >= 6


def test_a_manifest_round_trips_through_json(tmp_path):
    from context_lift.drift import load, save
    before = {"p.d.v": TableShape.of([("a", "INT64")], "SELECT a FROM t")}
    manifest = build_manifest([Gold("g1", "SELECT a FROM p.d.v")], source(before))
    path = tmp_path / "m.json"
    save(manifest, path)
    drifts, unchanged, _ = compare(load(path), source(before))
    assert drifts == []
    assert unchanged == ["g1"]


def test_the_manifest_stores_a_hash_and_never_the_view_sql(tmp_path):
    """The manifest is a file people pass around, and a view definition is SQL
    a customer wrote."""
    from context_lift.drift import save
    secret = "SELECT customer_secret FROM internal.private.table"
    before = {"p.d.v": TableShape.of([("a", "INT64")], secret)}
    manifest = build_manifest([Gold("g1", "SELECT a FROM p.d.v")], source(before))
    path = tmp_path / "m.json"
    save(manifest, path)
    written = path.read_text()
    assert "customer_secret" not in written
    assert "internal.private" not in written
