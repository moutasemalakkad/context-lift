# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Behavior tests for routing accuracy.

Pure functions over in-memory rows — no DB, no cloud. Covers the name
normalization, the two scoring tiers, the miss-not-exclusion rule for empty
predictions, catalog snapping on and off, and the report math against the
same pinned Wilson oracle test_metrics.py uses.
"""
import pytest

from context_lift.metrics import mcnemar_from_vectors
from context_lift.routing import (
    evaluate_routing,
    normalize_table_name,
    snap_to_catalog,
)


def _row(pred_table, gold_table="sales.orders", id_="q1"):
    """One routing row with defaults; tests only vary what they assert on."""
    return (id_, "which table?", gold_table, pred_table)


# --- normalization ---


def test_normalize_strips_whitespace_and_quotes():
    assert normalize_table_name("  'sales.orders'  ") == "sales.orders"


def test_normalize_strips_backticks():
    assert normalize_table_name("`sales.orders`") == "sales.orders"


def test_normalize_strips_backticks_from_every_part():
    assert (
        normalize_table_name("`my_project`.`my_dataset`.`my_table`")
        == "my_dataset.my_table"
    )
    assert normalize_table_name("`d`.`t`") == "d.t"


def test_normalize_strips_bigquery_prefix():
    assert normalize_table_name("bigquery:sales.orders") == "sales.orders"


def test_normalize_collapses_three_part_name_to_dataset_table():
    assert normalize_table_name("analytics-prod.sales.orders") == "sales.orders"


def test_normalize_leaves_two_part_name_unchanged():
    assert normalize_table_name("sales.orders") == "sales.orders"


# --- scoring tiers ---


def test_exact_match_scores_both_tiers():
    report = evaluate_routing([_row("sales.orders")])
    result = report.results[0]
    assert result.dataset_correct and result.table_correct


def test_right_dataset_wrong_table_scores_dataset_tier_only():
    report = evaluate_routing([_row("sales.customers")])
    result = report.results[0]
    assert result.dataset_correct and not result.table_correct


def test_empty_prediction_is_a_miss_not_an_exclusion():
    report = evaluate_routing([_row("")])
    result = report.results[0]
    assert not result.dataset_correct and not result.table_correct
    # The empty row stays in the denominator — n never shrinks.
    assert report.n_total == 1
    assert report.table.n == 1 and report.dataset.n == 1


def test_gold_is_normalized_the_same_as_the_prediction():
    # A 3-part, quoted gold still matches a clean 2-part prediction.
    report = evaluate_routing(
        [("q1", "which table?", "'analytics-prod.sales.orders'", "sales.orders")]
    )
    assert report.results[0].table_correct


def test_empty_gold_raises_value_error():
    with pytest.raises(ValueError, match="empty gold_table"):
        evaluate_routing([_row("sales.orders", gold_table="  ")])


# --- catalog snapping ---

_CATALOG = {"sales.orders", "sales.customers", "inventory.stock_levels"}


def test_exact_catalog_entry_passes_through_unsnapped():
    assert snap_to_catalog("sales.orders", _CATALOG) == ("sales.orders", False)


def test_near_miss_snaps_to_catalog_and_is_counted():
    # A dropped plural — the classic free-generation typo.
    report = evaluate_routing(
        [_row("inventory.stock_level", gold_table="inventory.stock_levels")],
        catalog=_CATALOG,
    )
    result = report.results[0]
    assert result.snapped and result.table_correct
    assert result.pred_table == "inventory.stock_levels"
    assert report.n_snapped == 1


def test_unmatchable_prediction_stays_unchanged_and_wrong():
    report = evaluate_routing([_row("finance.invoices")], catalog=_CATALOG)
    result = report.results[0]
    assert not result.snapped and not result.table_correct
    assert result.pred_table == "finance.invoices"


def test_empty_prediction_never_snaps():
    assert snap_to_catalog("", _CATALOG) == ("", False)


def test_no_catalog_means_no_snapping():
    # Same near-miss as above, but without a catalog it stays a plain miss.
    report = evaluate_routing(
        [_row("inventory.stock_level", gold_table="inventory.stock_levels")]
    )
    assert report.n_snapped == 0
    assert not report.results[0].table_correct


# --- report math and comparison ---


def test_report_intervals_match_the_published_wilson_oracle():
    # 19 exact hits out of 35 — same oracle as test_metrics.py: [0.382, 0.695].
    rows = [
        _row("sales.orders" if i < 19 else "sales.customers", id_=f"q{i}")
        for i in range(35)
    ]
    report = evaluate_routing(rows)
    assert report.table.k == 19 and report.table.n == 35
    assert round(report.table.low, 3) == 0.382
    assert round(report.table.high, 3) == 0.695


def test_correctness_vector_feeds_mcnemar():
    # Router A gets q1 right and q2 wrong; router B the reverse -> b=1, c=1.
    a = evaluate_routing([_row("sales.orders", id_="q1"), _row("hr.employees", id_="q2")])
    b = evaluate_routing([_row("hr.employees", id_="q1"), _row("sales.orders", id_="q2")])
    result = mcnemar_from_vectors(a.correctness_vector(), b.correctness_vector())
    assert (result.b, result.c) == (1, 1)
    assert result.p_value == 1.0


def test_multi_table_routing_scores_coverage_not_just_equality():
    from context_lift.routing import evaluate_routing_sets

    report = evaluate_routing_sets([
        ("q1", "?", "ORDERS|USERS", "ORDERS|USERS|PAYMENTS"),  # covered, not exact
        ("q2", "?", "ORDERS|USERS", "ORDERS"),                 # missing USERS
    ])
    assert report.coverage.k == 1 and report.coverage.n == 2
    assert report.exact.k == 0
    assert report.table_recall.k == 3 and report.table_recall.n == 4
    assert "q2  missing USERS" in report.summary()


def test_multi_table_routing_is_case_insensitive_and_order_free():
    from context_lift.routing import evaluate_routing_sets

    report = evaluate_routing_sets([("q1", "?", "users|orders", "ORDERS|Users")])
    assert report.results[0].exact is True


def _routing_report(rows, n_snapped=0):
    """(id, gold, pred, raw, snapped) -> a RoutingReport with literal outcomes."""
    from context_lift.metrics import proportion
    from context_lift.routing import RoutingReport, RoutingResult
    results = []
    for id_, gold, pred, raw, snapped in rows:
        gd, _, _ = gold.rpartition("."); pd, _, _ = pred.rpartition(".")
        results.append(RoutingResult(id=id_, question=f"q {id_}", gold_table=gold, pred_table=pred,
                                     raw_prediction=raw, snapped=snapped,
                                     dataset_correct=(gd == pd and pred != ""),
                                     table_correct=(gold == pred)))
    n = len(results)
    return RoutingReport(results=results, n_total=n, n_snapped=n_snapped,
                         dataset=proportion(sum(r.dataset_correct for r in results), n),
                         table=proportion(sum(r.table_correct for r in results), n))


def test_routing_failures_are_grouped_by_the_decision_they_force():
    """Coverage, near miss, retrieval, neighbours, nothing: five shapes, and the
    first two used to be one number."""
    from context_lift.routing import triage_routing
    catalog = {"sales.orders", "sales.items", "hr.people"}
    report = _routing_report([
        ("q1", "sales.orders", "sales.orders", "sales.orders", False),        # right
        ("q2", "sales.returns", "sales.orders", "sales.orders", False),       # gold not catalogued
        ("q3", "sales.orders", "sales.items", "sales.item", True),            # snapped near miss
        ("q4", "sales.orders", "hr.people", "hr.people", False),              # wrong dataset
        ("q5", "sales.orders", "sales.items", "sales.items", False),          # neighbour
        ("q6", "sales.orders", "", "", False),                                # nothing
    ], n_snapped=1)

    buckets = dict(triage_routing(report, catalog))

    assert buckets == {"GOLD_NOT_IN_CATALOG": ["q2"], "SNAPPED_TO_SIBLING": ["q3"],
                       "WRONG_DATASET": ["q4"], "WRONG_TABLE_SAME_DATASET": ["q5"],
                       "NO_PREDICTION": ["q6"]}


def test_two_routing_runs_compare_with_the_same_paired_test_as_two_sql_arms():
    """The fixed/broke arithmetic a field engineer did by hand on an unsplit set
    at n=1; ablate refuses to pretend that is a result, which is the point."""
    from context_lift.ablate import ablate
    from context_lift.routing import as_eval_report
    before = as_eval_report(_routing_report([
        ("q1", "a.t", "a.t", "a.t", False), ("q2", "a.t", "b.t", "b.t", False),
        ("q3", "a.t", "b.t", "b.t", False), ("q4", "a.t", "a.t", "a.t", False)]))
    after = as_eval_report(_routing_report([
        ("q1", "a.t", "a.t", "a.t", False), ("q2", "a.t", "a.t", "a.t", False),
        ("q3", "a.t", "a.t", "a.t", False), ("q4", "a.t", "b.t", "b.t", False)]))

    report = ablate(before, after)

    assert (report.mcnemar.c, report.mcnemar.b) == (2, 1)        # fixed 2, broke 1
    assert report.mcnemar.p_value == 1.0                           # and it says so


def test_score_routing_json_is_json(tmp_path, capsys):
    """Documented as JSON, it printed the prose."""
    import json
    from context_lift.cli import main
    preds = tmp_path / "r.csv"
    preds.write_text("id,question,gold_table,pred_table\nq1,?,a.t,a.t\nq2,?,a.t,b.t\n")

    assert main(["score-routing", "--predictions", str(preds), "--json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["table"]["point"] == 0.5


def test_routing_gate_reports_concentration_coverage_and_defects():
    """49 of 123 on one gold was an under-determined table; a key can say so
    before a router is paid to fail on it."""
    from context_lift.routing import routing_gate
    golds = [("q1", "a.big"), ("q2", "a.big"), ("q3", "a.big"), ("q4", "b.one"),
             ("q5", "c.gone"), ("q6", ""), ("q1", "a.big")]

    report = routing_gate(golds, catalog={"a.big", "b.one"})

    assert report["largest"][0] == {"gold": "a.big", "questions": 4, "share": 4 / 7}
    assert report["singleton_golds"] == ["b.one", "c.gone"]
    assert report["uncatalogued"] == {"golds": ["c.gone"], "questions": 1, "catalog_size": 2}
    assert report["empty_gold_ids"] == ["q6"] and report["duplicate_ids"] == ["q1"]
    assert report["clean"] is False


def test_routing_gate_runs_on_a_predictions_file_or_a_bare_key(tmp_path, capsys):
    from context_lift.cli import main
    key = tmp_path / "golds.csv"
    key.write_text("id,gold_table\nq1,a.t\nq2,a.t\nq3,b.t\n")

    assert main(["routing-gate", "--golds", str(key)]) == 0

    out = capsys.readouterr().out
    assert "3 questions, 2 golds" in out and "a.t 2 (67%)" in out


def test_routing_gate_reads_the_table_column_mine_writes(tmp_path):
    from context_lift.routing import load_routing_golds
    path = tmp_path / "candidates.csv"
    path.write_text("id,question,gold_sql,table\nc1,?,SELECT 1,sales.orders\nc2,?,SELECT 2,\n")

    assert load_routing_golds(path) == [("c1", "sales.orders"), ("c2", "")]
