# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The checks a round has to pass before its numbers mean anything."""
from __future__ import annotations

from context_lift.ablate import verdict
from context_lift.gates import (export_round_trip, ground_truth_audit,
                            holdout_reads, model_canary, pasted_sql, summary,
                            template_twins)
from context_lift.metrics import proportion
from test_verdict import report


def test_a_rules_file_that_is_mostly_sql_is_refused():
    """Measured on one estate: 24 of 29 rule lines carried a SELECT, and that
    round's page was wrong. SQL in a rules file gets neither the leak gate nor
    the gold gate."""
    rules = "\n".join(["- SELECT count(*) FROM orders"] * 24 +
                      ["- Revenue excludes returns"] * 5)

    gate = pasted_sql(rules)

    assert not gate.passed
    assert "24 of 29" in gate.detail
    assert "example bank" in gate.detail


def test_a_rules_file_of_meanings_passes_and_still_says_what_it_counted():
    """A passing gate prints its number too. "ok" is not evidence."""
    rules = "\n".join(["- Revenue excludes Cancelled and Returned orders.",
                       "- A completed order has status exactly 'Complete'.",
                       "- The fiscal year starts in February.",
                       "- Exclude test stores.",
                       "- Grain is one row per order line."])

    gate = pasted_sql(rules)

    assert gate.passed and "0 of 5" in gate.detail


def test_the_share_is_not_judged_on_too_few_lines():
    """One SELECT in three lines is 33% and means nothing."""
    assert pasted_sql("- a\n- SELECT 1\n- c").passed


def test_allow_sql_waives_the_gate_and_records_that_it_was_waived():
    rules = "\n".join(["- SELECT count(*) FROM orders"] * 9 + ["- a meaning"])

    gate = pasted_sql(rules, allow_sql=True)

    assert gate.passed and "waived by --allow-sql" in gate.detail


def test_an_example_with_the_same_query_shape_as_a_scored_question_is_caught():
    """Not the same question, the same query once literals are masked.
    Answering the scored question is then substitution, not text-to-SQL."""
    scored = [("q1", "SELECT COUNT(*) FROM orders WHERE status = 'Complete'")]
    items = [("ex0", "select count(*) from orders where status = 'Cancelled'")]

    gate = template_twins(scored, items)

    assert not gate.passed
    assert "ex0 matches q1" in gate.detail
    assert "substitution" in gate.detail


def test_a_genuinely_different_example_passes():
    scored = [("q1", "SELECT COUNT(*) FROM orders WHERE status = 'Complete'")]
    items = [("ex0", "SELECT AVG(num_of_item) FROM orders")]

    assert template_twins(scored, items).passed


def test_reading_the_holdout_twice_in_one_round_fails_unless_forced():
    """The second read is chosen knowing the first, which is what makes the
    number worth anything."""
    assert holdout_reads(1).passed
    assert not holdout_reads(2).passed
    forced = holdout_reads(2, forced=True)
    assert forced.passed and "no longer a holdout" in forced.detail


def test_never_reading_the_holdout_is_also_a_failure():
    gate = holdout_reads(0)

    assert not gate.passed and "no verdict to give" in gate.detail


def test_the_canary_fires_when_the_arm_that_should_not_move_moved():
    """The alias carries no version string, so the control arm is the only
    detector that the model changed under us."""
    before = proportion(30, 60)
    after = proportion(50, 60)

    gate = model_canary(after, before)

    assert not gate.passed and "alias may have moved" in gate.detail


def test_the_canary_passes_inside_the_earlier_interval_and_shows_the_shift():
    before = proportion(30, 60)

    gate = model_canary(proportion(31, 60), before)

    assert gate.passed and "points" in gate.detail


def test_the_first_round_has_nothing_to_compare_against():
    assert model_canary(proportion(30, 60), None).passed


def test_an_answer_key_nobody_checked_fails_the_audit_gate():
    """An unchecked key can be wrong more often than any lift measured against
    it: an audit of one public set found 52.8% of golds wrong."""
    gate = ground_truth_audit(sample=0, disagreements=0, total_golds=47)

    assert not gate.passed and "no gold was re-derived" in gate.detail


def test_the_audit_passes_when_it_happened_and_prints_the_rate_either_way():
    """The gate is whether the audit happened, not whether it was clean. A
    dirty key is a finding, not a reason to hide the round."""
    gate = ground_truth_audit(sample=10, disagreements=3, total_golds=47)

    assert gate.passed and "3 of 10" in gate.detail and "30%" in gate.detail


def test_the_export_sentinel_must_come_back():
    """No size limit is documented for any Context field, so a silent cut
    otherwise looks exactly like context that did not help."""
    assert export_round_trip("zylbrick", "The total zylbrick is 12.").passed
    assert not export_round_trip("zylbrick", "I could not find that.").passed
    assert not export_round_trip("", "anything").passed


def test_a_failed_gate_reaches_the_verdict_and_takes_the_headline():
    """The whole point of the module: these tuples are what makes a verdict
    INVALID instead of a flattering number."""
    strong = report(b=0, c=20, n=60, lift=0.33)
    gates = [pasted_sql("\n".join(["- SELECT 1"] * 9 + ["- a meaning"])),
             holdout_reads(1)]

    result = verdict(strong, gates=[g.as_tuple() for g in gates])

    assert result.word == "INVALID"
    assert not result.scores_are_publishable
    assert "pasted SQL" in result.headline


def test_the_summary_puts_failures_first():
    """The reason is the first line read, not the tenth."""
    text = summary([holdout_reads(1), pasted_sql("\n".join(["- SELECT 1"] * 9 + ["- x"]))])

    assert text.splitlines()[0].startswith("[FAIL]")
    assert "1 gate(s) failed; the scores are not shown." in text


def test_a_join_link_mined_after_the_golds_ran_fails_the_round():
    """The catalog learns joins from query history, and the gold gate's own
    statements are query history. A link learned from the answer key would
    hand the treatment arm a join the answer key knows."""
    import datetime

    from context_lift.gates import join_links

    golds_ran = datetime.datetime(2026, 9, 10, 12, 0)
    gate = join_links([{"source": "QUERY_HISTORY", "name": "p/l/mined-1",
                        "created_at": datetime.datetime(2026, 9, 10, 13, 0)}],
                      golds_ran)

    assert not gate.passed
    assert "may carry the answer key" in gate.detail


def test_a_join_link_older_than_the_golds_is_the_estates_own_traffic():
    """On a customer estate a mined link comes from their analysts and is
    ordinary context. Refusing it would refuse every real estate."""
    import datetime

    from context_lift.gates import join_links

    gate = join_links([{"source": "QUERY_HISTORY", "name": "p/l/old",
                        "created_at": datetime.datetime(2026, 9, 1)}],
                      datetime.datetime(2026, 9, 10))

    assert gate.passed
    assert "older than the golds" in gate.detail


def test_without_a_time_the_gate_reports_and_does_not_accuse():
    """A tool that cannot tell the two apart must not accuse."""
    from context_lift.gates import join_links

    gate = join_links([{"source": "QUERY_HISTORY", "name": "p/l/x",
                        "created_at": None}])

    assert gate.passed
    assert "cannot be told from" in gate.detail


def test_sql_carried_as_prose_is_found_for_the_shape_check():
    """The served arm keeps its examples inside the system instruction, so
    `arm.examples` is empty and the twin check printed "none of 0 items"
    while the arm carried 22 queries. A gate that cannot see the thing it
    guards is worse than no gate: it prints a pass."""
    from context_lift.gates import sql_in_text

    bundle = (
        "resources:\n"
        " overview: one row per order line\n"
        " sampleQueries:\n"
        "  - description: revenue by month\n"
        "    sql: SELECT month, SUM(price) AS revenue\n"
        "         FROM order_items\n"
        "         GROUP BY month\n"
        "  - description: how many\n"
        "    sql: SELECT COUNT(*) FROM orders\n")

    found = sql_in_text(bundle)

    assert len(found) == 2
    assert "GROUP BY month" in found[0]
    assert found[1].startswith("SELECT COUNT(*)")


def test_a_twin_hidden_in_prose_is_caught():
    """The whole point: the same query shape reaching the model as text
    rather than as a structured example."""
    from context_lift.gates import sql_in_text, template_twins

    held_out = [("h1", "SELECT state, COUNT(*) FROM users GROUP BY state")]
    instruction = ("resources:\n"
                   "  sql: SELECT state, COUNT(*) FROM users GROUP BY state\n")

    items = [(f"instruction{i}", sql)
             for i, sql in enumerate(sql_in_text(instruction))]
    gate = template_twins(held_out, items)

    assert not gate.passed


def test_text_with_no_sql_finds_none():
    from context_lift.gates import sql_in_text

    assert sql_in_text("just a page about orders, no queries here") == []
    assert sql_in_text("") == []


# --- can the agent reach the data at all -------------------------------------

def test_the_round_is_refused_when_the_arm_lacks_the_golds_tables():
    """The run that earned this gate, reproduced at its real proportions.

    One round on a customer estate: the spec named 27 tables, the 39 golds
    needed 63, five overlapped, and exactly ONE gold had every table it
    needed. The agent returned no SQL for 55 of 78 questions because it was
    asked about data it had never been handed, both arms failed identically,
    the lift was +0.0, and the page read UNDERPOWERED -- which tells a reader
    to go and find more questions. 37 minutes, 78 API calls and $2 to learn
    something two sets could have said for free.
    """
    from context_lift.gates import table_coverage
    arm = {f"spec_ds.t{i}" for i in range(27)}
    golds = {f"g{i}": {f"other_ds.t{i}", f"other_ds.u{i}"} for i in range(38)}
    golds["g_covered"] = {"spec_ds.t0"}
    gate = table_coverage(golds, arm)
    assert not gate.passed
    assert "1 of 39" in gate.detail
    # it has to name the missing tables, or a reader cannot act on it
    assert "other_ds.t0" in gate.detail
    # and say what to do
    assert "Add these tables to the spec" in gate.detail


def test_a_few_uncovered_golds_do_not_refuse_the_round():
    """Dilution is bad and measurable; a person may accept it knowingly.
    Only "nothing is being measured" refuses."""
    from context_lift.gates import table_coverage
    arm = {"d.t1", "d.t2"}
    golds = {f"g{i}": {"d.t1"} for i in range(9)}
    golds["g_bad"] = {"d.missing"}
    gate = table_coverage(golds, arm)
    assert gate.passed and "9 of 10" in gate.detail


def test_the_gate_compares_the_same_form_of_table_name():
    """The arm names tables `project.dataset.table` and a gold's SQL often
    names them `dataset.table`. Comparing the two forms directly is how a
    gate that looks right passes everything."""
    from context_lift.round import _arm_table_key
    assert _arm_table_key("acme-analytics-pp.dw_pos.fct_gbl_pos") == "dw_pos.fct_gbl_pos"
    assert _arm_table_key("dw_pos.fct_gbl_pos") == "dw_pos.fct_gbl_pos"
    assert _arm_table_key("`dw_pos.fct_gbl_pos`") == "dw_pos.fct_gbl_pos"


def test_a_cte_alias_is_not_a_missing_table():
    """Mining one estate returned `base_cte` beside real tables, and passing
    that to the API is a 400. A CTE is not something a spec could carry, so
    it is dropped rather than counted as missing."""
    from context_lift.round import _tables_of
    keys = _tables_of(
        "with base_cte as (select 1 from dw_pos.fct_gbl_pos) "
        "select * from base_cte", arm=None)
    assert keys == {"dw_pos.fct_gbl_pos"}


def test_no_gold_names_a_table_is_not_a_coverage_failure():
    from context_lift.gates import table_coverage
    gate = table_coverage({"a": set(), "b": set()}, {"d.t"})
    assert gate.passed and "nothing to cover" in gate.detail


def test_a_round_where_the_agent_wrote_nothing_is_invalid_not_underpowered():
    """The gate that was missing, and the verdict it replaces.

    One round asked 78 questions across two arms and 55 of the answers
    carried no SQL at all -- no query, no job id, no citations, not a
    character of reasoning -- because the context spec named 27 tables and
    the questions needed 65. Both arms failed identically, the lift was +0.0
    on both halves, and the page said UNDERPOWERED.

    That is not merely unhelpful, it misdirects: it names the sample size as
    the problem when the sample size was fine. A verdict offering a
    plausible wrong cause is worse than one saying nothing, because the
    reader acts on it. I nearly spent $11 mining more questions.
    """
    from context_lift.gates import answers_with_no_sql
    bad = answers_with_no_sql(55, 78)
    assert not bad.passed
    assert "55 of 78" in bad.detail and "71%" in bad.detail
    # it must point at the thing that actually diagnoses it
    assert "error_class" in bad.detail
    assert "table-coverage gate" in bad.detail


def test_a_few_empty_answers_are_counted_and_printed_not_refused():
    """An agent that asks a clarifying question now and then is normal. The
    count still prints, because "3 of 78 were empty" is evidence and
    "empty: ok" is not."""
    from context_lift.gates import answers_with_no_sql
    ok = answers_with_no_sql(3, 78)
    assert ok.passed
    assert "3 of 78 answers carried no SQL at all (4%)" in ok.detail


def test_no_answers_is_not_a_failure_of_this_gate():
    from context_lift.gates import answers_with_no_sql
    assert answers_with_no_sql(0, 0).passed
