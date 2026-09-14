# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Business definitions counted out of a query log, and the leak defence.

The round that produced this measured an agent at 2 right of 18, and the
failures were arithmetic rather than vocabulary: asked for "the final
shipping amount" it wrote `SUM(shipping_charge)`, where the answer key
computed `sum(line_total_shipping_amt) - sum(line_shipping_disc_amt)`. No
glossary entry fixes that. The formula was already in the warehouse's own
history, written by its analysts fourteen times over.
"""
from context_lift.conventions import (code_mappings, metric_formulas,
                                  render_metrics, render_vocabulary)
from context_lift.mine import fingerprint


def test_a_formula_many_queries_agree_on_is_a_convention():
    sqls = [
        "SELECT SUM(t.line_total_shipping_amt) - SUM(t.line_shipping_disc_amt) FROM a WHERE x=1",
        "SELECT sum(line_total_shipping_amt) - sum(line_shipping_disc_amt), region FROM b GROUP BY 2",
        "select SUM(s.line_total_shipping_amt)-SUM(s.line_shipping_disc_amt) from c where y=2",
    ]
    got = metric_formulas(sqls, min_times=3)
    assert got == [("sum(line_total_shipping_amt) - sum(line_shipping_disc_amt)", 3)]


def test_the_table_alias_is_not_part_of_the_convention():
    """`t.amt` and `s.amt` are the same column. Counting them apart would
    split one convention into two and drop both below the threshold."""
    sqls = [f"SELECT SUM({a}.amt) - SUM({a}.disc) FROM t {a} WHERE x={i}"
            for i, a in enumerate("tsx")]
    assert metric_formulas(sqls, min_times=3) == [("sum(amt) - sum(disc)", 3)]


def test_one_analysts_arithmetic_is_not_a_convention():
    """Two occurrences is a person; three different query shapes is a
    practice. The threshold is the whole method."""
    sqls = ["SELECT SUM(a) - SUM(b) FROM t WHERE x=1",
            "SELECT SUM(a) - SUM(b) FROM t WHERE x=2"]
    # x=1 and x=2 are ONE shape once literals are masked, so this is 1 shape
    assert metric_formulas(sqls, min_times=3) == []


def test_a_query_run_four_hundred_times_is_still_one_convention():
    """Counting statements rather than shapes would let one nightly report
    manufacture a convention by itself."""
    sqls = ["SELECT SUM(a) - SUM(b) FROM t WHERE d = '2026-01-01'"] * 400
    assert metric_formulas(sqls, min_times=3) == []


def test_sum_of_a_column_minus_itself_is_not_a_formula():
    assert metric_formulas(["SELECT SUM(a) - SUM(a) FROM t"] * 5, min_times=1) == []


def test_a_case_mapping_is_a_vocabulary_the_warehouse_never_wrote_down():
    sqls = [f"SELECT CASE company_code WHEN '1000' THEN 'CL' "
            f"WHEN '2000' THEN 'PE' END, x{i} FROM t" for i in range(3)]
    got = code_mappings(sqls, min_times=3)
    assert got == [("company_code", (("1000", "CL"), ("2000", "PE")), 3)]


def test_a_reserved_questions_own_sql_is_excluded_before_counting():
    """The leak defence, and it is the reason this method is usable at all.

    A reserved question's SQL is IN the query log -- that is where the
    question came from -- so mining every statement would hand the agent the
    answers. Reproduced from the real case that found it: on one estate a
    `company_code '1000' -> 'CL'` mapping appeared in exactly two query
    shapes, and ONE of them was a reserved gold. Mining it would have put a
    held-out answer's key detail into the context, and the ordinary leak gate
    would not have caught it, because a CASE fragment is not the whole SQL.

    Excluding the reserved shapes deletes it and leaves every real
    convention standing, because a convention does not depend on any one
    question.
    """
    reserved = "SELECT CASE company_code WHEN '1000' THEN 'CL' WHEN '2000' THEN 'PE' END FROM held"
    log = [
        reserved,
        "SELECT CASE company_code WHEN '1000' THEN 'CL' WHEN '2000' THEN 'PE' END, q FROM other",
        # a real convention, in many shapes
        "SELECT SUM(amt) - SUM(disc) FROM a WHERE k=1",
        "SELECT SUM(amt) - SUM(disc), r FROM b GROUP BY 2",
        "SELECT SUM(amt) - SUM(disc) FROM c JOIN d USING (k)",
    ]
    banned = {fingerprint(reserved)}
    assert code_mappings(log, min_times=2) != []              # without the defence
    assert code_mappings(log, min_times=2, exclude_fingerprints=banned) == []
    # and the convention survives the defence untouched
    assert metric_formulas(log, min_times=3,
                           exclude_fingerprints=banned) == [("sum(amt) - sum(disc)", 3)]


def test_the_rules_file_carries_the_count_and_refuses_to_name_the_metric():
    """A reader must be able to disagree with the evidence rather than with
    an author. And naming `sum(a) - sum(b)` "final shipping amount" is a
    guess unless a person does it, so the file says what the warehouse DOES
    and leaves the naming."""
    text = render_metrics([("sum(amt) - sum(disc)", 14)], total_shapes=2000)
    assert "`sum(amt) - sum(disc)`" in text
    assert "[14 different queries in this warehouse do]" in text
    assert "2000 distinct query shapes" in text
    assert "a person's job" in text
    # no invented metric name anywhere
    assert "revenue" not in text.lower() and "final shipping" not in text.lower()


def test_the_vocabulary_file_says_how_many_queries_agreed():
    text = render_vocabulary([("company_code", (("1000", "CL"),), 4)])
    assert "company_code = 1000" in text
    assert "1000 means CL" in text and "4 different queries" in text


# --- auditing an answer key against the conventions -------------------------

def test_a_key_using_a_calendar_grain_where_a_fiscal_column_exists():
    """The finding that produced this audit.

    A round measured +0.0 with EVERY reserved answer changed by the written
    context, which means the context was read and used. One failure:

        the rule:       a question about a quarter means `fiscal_quarter`
        without rules:  EXTRACT(QUARTER FROM store_open_date)
        with rules:     d.fiscal_quarter JOIN dim_gbl_date    <- obeyed exactly
        the ANSWER KEY: DATE_TRUNC(store_open_date, QUARTER)  <- calendar quarter

    The agent did what it was told and scored worse for it. One analyst's
    query is not the warehouse's convention, and a round that grades a
    well-informed agent against one analyst's habits measures the habits.
    """
    from context_lift.conventions import golds_using_a_calendar_grain
    golds = [
        ("g1", "stores by quarter", "SELECT DATE_TRUNC(d, QUARTER), COUNT(*) FROM t GROUP BY 1"),
        ("g2", "stores by quarter", "SELECT EXTRACT(QUARTER FROM d), COUNT(*) FROM t GROUP BY 1"),
        ("g3", "stores by quarter", "SELECT x.fiscal_quarter, COUNT(*) FROM t JOIN x GROUP BY 1"),
    ]
    got = golds_using_a_calendar_grain(golds, {"fiscal_quarter"})
    assert [g[0] for g in got] == ["g1", "g2"]
    assert got[0][1:] == ("quarter", "fiscal_quarter")


def test_a_warehouse_with_no_fiscal_calendar_is_not_told_to_use_one():
    """The fiscal columns are read from the warehouse's own history rather
    than assumed. A calendar quarter is the right answer in a business that
    has no fiscal calendar, and flagging it there would be noise."""
    from context_lift.conventions import fiscal_columns_in, golds_using_a_calendar_grain
    golds = [("g1", "by quarter", "SELECT DATE_TRUNC(d, QUARTER) FROM t")]
    assert fiscal_columns_in(["SELECT a FROM t", "SELECT b FROM u"]) == set()
    assert golds_using_a_calendar_grain(golds, set()) == []


def test_the_fiscal_columns_come_from_the_history():
    from context_lift.conventions import fiscal_columns_in
    got = fiscal_columns_in(["SELECT fiscal_week, x FROM t",
                             "SELECT d.fiscal_quarter FROM u d"])
    assert got == {"fiscal_week", "fiscal_quarter"}


def test_a_key_that_sums_one_side_of_a_repeated_formula():
    """A warehouse computing something as `sum(a) - sum(b)` in many query
    shapes has a convention. A key that sums `a` alone reports a gross figure
    where the business reports a net one."""
    from context_lift.conventions import golds_using_half_a_formula
    formulas = [("sum(amt) - sum(disc)", 14)]
    golds = [("g1", "total", "SELECT SUM(amt) FROM t"),
             ("g2", "net", "SELECT SUM(amt) - SUM(disc) FROM t"),
             ("g3", "discount", "SELECT SUM(disc) FROM t")]
    got = golds_using_half_a_formula(golds, formulas)
    assert [g[0] for g in got] == ["g1"]
    assert got[0][1:] == ("amt", "sum(amt) - sum(disc)", 14)


def test_only_the_first_operand_is_flagged():
    """`sum(disc)` alone is usually a different question -- "how much
    discount did we give" is fair to ask -- and flagging it would bury the
    real cases."""
    from context_lift.conventions import golds_using_half_a_formula
    golds = [("g3", "discount given", "SELECT SUM(disc) FROM t")]
    assert golds_using_half_a_formula(golds, [("sum(amt) - sum(disc)", 14)]) == []


def test_the_duplicate_check_is_the_one_review_already_uses():
    """A first version of this wrote a second, stricter rule and found
    nothing where `review` had found a pair. Two notions of "the same
    question" will disagree, and the day they disagree is the day one of
    them is trusted."""
    from context_lift.conventions import golds_asking_the_same_question
    # the real pair from one estate, verbatim. Two analysts asked one
    # question and wrote different SQL, and both failed the same round.
    golds = [
        ("a", "For each country and quarter, how many stores opened, how many "
              "closed, and the net change, ordered by country then quarter?",
         "SELECT 1"),
        ("b", "For each country and quarter, how many stores opened, how many "
              "closed, and what was the net change, ordered by country and "
              "then by quarter?", "SELECT 2"),
        ("c", "Total shipping amount by day", "SELECT 3"),
    ]
    groups = golds_asking_the_same_question(golds)
    assert len(groups) == 1 and set(groups[0]) == {"a", "b"}
