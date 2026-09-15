# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Behavior tests for mining candidate golds from query history.

Two things carry the weight here. Fingerprinting decides what counts as "the
same query asked again", which is what stops one nightly report from filling
the review list. And gating decides what never reaches a human — a candidate
that cannot run is worse than no candidate, because a broken gold silently
corrupts every score computed against it.
"""
import csv

from context_lift.executor import ExecResult
from context_lift.mine import (
    is_dashboard_tile,
    is_noise,
    table_count,
    MinedQuery,
    fingerprint,
    mine_candidates,
    summarize,
    write_candidates,
)


class _FakeExecutor:
    """Runs nothing. Returns whatever the test says each SQL should return."""

    def __init__(self, by_substring: dict[str, ExecResult] | None = None):
        self.by_substring = by_substring or {}
        self.seen: list[str] = []

    def execute(self, sql: str) -> ExecResult:
        self.seen.append(sql)
        for needle, result in self.by_substring.items():
            if needle in sql:
                return result
        return ExecResult(rows=[{"n": 1}])


def test_same_query_with_different_literals_is_one_shape():
    a = fingerprint("SELECT * FROM sales WHERE region = 'EMEA' AND yr = 2025")
    b = fingerprint("SELECT * FROM sales WHERE region = 'APAC' AND yr = 2026")
    assert a == b


def test_formatting_and_case_do_not_change_the_shape():
    a = fingerprint("select  name\n  from   emp;")
    b = fingerprint("SELECT name FROM emp")
    assert a == b


def test_a_space_after_a_comma_does_not_make_a_second_shape():
    # runs of whitespace collapsed to one space, but the space next to
    # punctuation stayed, so these two were two shapes
    spaced = fingerprint("SELECT cluster, SUM(x) FROM t GROUP BY 1")
    tight = fingerprint("SELECT cluster,SUM(x) FROM t GROUP BY 1")
    assert spaced == tight


def test_different_columns_are_different_shapes():
    assert fingerprint("SELECT a FROM t") != fingerprint("SELECT b FROM t")


def test_counts_are_summed_across_repeats_of_one_shape():
    queries = [
        MinedQuery("SELECT region, SUM(amt) FROM sales WHERE yr = 2025 GROUP BY region", 3),
        MinedQuery("SELECT region, SUM(amt) FROM sales WHERE yr = 2026 GROUP BY region", 4),
        MinedQuery("SELECT name FROM emp", 1),
    ]
    candidates = mine_candidates(queries, _FakeExecutor())
    assert len(candidates) == 2
    assert candidates[0].times_seen == 7  # ranked first, counts summed
    assert candidates[1].times_seen == 1


def test_non_select_statements_never_become_candidates():
    queries = [
        MinedQuery("DELETE FROM emp WHERE id = 1", 99),
        MinedQuery("SELECT name FROM emp", 1),
    ]
    candidates = mine_candidates(queries, _FakeExecutor())
    assert len(candidates) == 1
    assert candidates[0].gold_sql == "SELECT name FROM emp"


def test_a_candidate_that_fails_to_run_is_kept_but_marked_not_usable():
    # Kept, not dropped: a reviewer should see that a popular query is broken.
    executor = _FakeExecutor({"broken": ExecResult(error="no such table: broken")})
    candidates = mine_candidates([MinedQuery("SELECT x FROM broken", 5)], executor)
    assert candidates[0].gate_status == "EXEC_FAIL"
    assert not candidates[0].usable
    assert "no such table" in candidates[0].gate_detail


def test_a_zero_row_candidate_is_marked_not_usable():
    executor = _FakeExecutor({"empty": ExecResult(rows=[])})
    candidates = mine_candidates([MinedQuery("SELECT x FROM empty", 5)], executor)
    assert candidates[0].gate_status == "ZERO_ROWS"
    assert not candidates[0].usable


def test_clean_candidates_are_marked_usable():
    candidates = mine_candidates([MinedQuery("SELECT name FROM emp", 2)], _FakeExecutor())
    # the retry fixes the literal; the projection check is a different question
    assert "MISSING_LITERAL" not in candidates[0].gate_status
    assert candidates[0].usable


def test_limit_is_applied_before_gating_so_no_wasted_execution():
    queries = [MinedQuery(f"SELECT c{i} FROM t", 10 - i) for i in range(5)]
    executor = _FakeExecutor()
    candidates = mine_candidates(queries, executor, limit=2)
    assert len(candidates) == 2
    assert len(executor.seen) == 2  # the other three were never run


def test_before_gate_sees_the_chosen_shapes_and_can_stop_before_anything_runs():
    queries = [MinedQuery(f"SELECT c{i} FROM t", 10 - i) for i in range(5)]
    executor = _FakeExecutor()
    seen: dict = {}
    counts: dict = {}

    def refuse(sqls):
        seen["sqls"] = sqls
        return False

    candidates = mine_candidates(queries, executor, limit=2, counts_out=counts, before_gate=refuse)

    assert candidates == []
    assert seen["sqls"] == ["SELECT c0 FROM t", "SELECT c1 FROM t"]   # after ranking and the cut
    assert executor.seen == []
    assert counts["stopped"] is True


def test_min_times_seen_drops_one_off_queries():
    queries = [MinedQuery("SELECT a FROM t", 5), MinedQuery("SELECT b FROM t", 1)]
    candidates = mine_candidates(queries, _FakeExecutor(), min_times_seen=2)
    assert [c.times_seen for c in candidates] == [5]


def test_question_stays_empty_without_a_writer():
    candidates = mine_candidates([MinedQuery("SELECT name FROM emp")], _FakeExecutor())
    assert candidates[0].question == ""


def test_a_question_writer_fills_the_question():
    class _Writer:
        def write(self, sql):
            return f"what does this return? {sql[:6]}"

    candidates = mine_candidates(
        [MinedQuery("SELECT name FROM emp")], _FakeExecutor(), question_writer=_Writer()
    )
    assert candidates[0].question.startswith("what does this return?")


def test_a_commented_out_filter_is_hidden_from_the_writer_and_named_in_the_detail():
    # two candidates on one estate had their WHERE line inside a comment, and
    # the drafted question named a filter the query does not apply -- which
    # reads as correct in a spreadsheet
    sql = ("SELECT region, SUM(x) FROM sales\n"
           "-- WHERE region = 'EMEA'\n"
           "GROUP BY region")

    class _Writer:
        def __init__(self):
            self.seen = []
            self.comments = []

        def write(self, text, comment=""):
            self.seen.append(text)
            self.comments.append(comment)
            return "how much did we sell in each region?"

    writer = _Writer()
    counts: dict = {}

    candidates = mine_candidates([MinedQuery(sql, 4)], _FakeExecutor(),
                                 question_writer=writer, counts_out=counts)

    assert "EMEA" not in writer.seen[0]                  # the model never saw the comment
    # not as the SQL and not as the purpose either: a switched-off filter is
    # the one comment `analyst_note` refuses to pass on
    assert writer.comments == [""]
    assert candidates[0].gold_sql == sql                 # the sheet keeps the original text
    assert candidates[0].gate_detail == ("a filter is commented out, so the query does not "
                                        "apply it: -- WHERE region = 'EMEA'")
    assert counts["disabled_filters"] == 1
    assert "1 shapes carry a commented-out filter" in summarize(candidates, counts=counts)


def test_ids_are_stable_across_runs_so_a_review_file_diffs():
    queries = [MinedQuery("SELECT a FROM t", 2), MinedQuery("SELECT b FROM t", 1)]
    first = mine_candidates(queries, _FakeExecutor())
    second = mine_candidates(queries, _FakeExecutor())
    assert [c.id for c in first] == [c.id for c in second]


def test_written_file_has_the_columns_gate_and_score_already_read(tmp_path):
    candidates = mine_candidates([MinedQuery("SELECT name FROM emp", 2)], _FakeExecutor())
    path = write_candidates(candidates, tmp_path / "candidates.csv")
    with path.open() as handle:
        rows = list(csv.DictReader(handle))
    assert set(rows[0]) >= {"id", "question", "gold_sql"}
    assert rows[0]["gold_sql"] == "SELECT name FROM emp"


def test_the_summary_warns_when_a_datasets_candidates_come_from_one_account():
    """On one estate the candidates per dataset were 4,4,4,3,2,2,2,2,1,1 with
    2,4,3,2,1,1,1,1,1,1 people behind them: half the datasets rested on one
    analyst's habit. A warning, not a refusal."""
    log = [
        MinedQuery("SELECT region, SUM(x) FROM sales.orders GROUP BY region", 40,
                   "analyst@corp.com;other@corp.com"),
        MinedQuery("SELECT day, SUM(y) FROM ops.jobs GROUP BY day", 30, "analyst@corp.com"),
        MinedQuery("SELECT team, COUNT(*) FROM ops.runs GROUP BY team", 20, "analyst@corp.com"),
    ]
    counts: dict = {}

    candidates = mine_candidates(log, _FakeExecutor(), counts_out=counts)
    report = summarize(candidates, counts=counts)

    assert counts["accounts_per_dataset"] == {"sales": 2, "ops": 1}
    assert ("1 of 2 datasets have candidates from one account only: ops. A gold nobody else "
            "asks is a gold about one person's habit") in report
    assert {c.table: c.accounts for c in candidates} == {"sales.orders": 2, "ops.jobs": 1,
                                                        "ops.runs": 1}


def test_summary_says_candidates_are_not_golds():
    candidates = mine_candidates([MinedQuery("SELECT name FROM emp")], _FakeExecutor())
    assert "person approves" in summarize(candidates)


def test_summary_handles_a_log_with_nothing_minable():
    assert "no candidates" in summarize(mine_candidates([MinedQuery("DROP TABLE t")], _FakeExecutor()))


# --- filters learned from a real query log, each pinned to the failure it
# --- prevents in a review file


def test_bi_tool_metadata_probes_are_dropped():
    # Looker and Tableau fire per-column probes with generated aliases,
    # thousands of times a day. On one engagement 198 of the top 200 patterns
    # by frequency were these, so without the filter mining returns garbage.
    noisy = "SELECT clmn0_ AS c, clmn1_ FROM t0_D123 GROUP BY clmn0_"
    assert is_noise(noisy)
    candidates = mine_candidates(
        [MinedQuery(noisy, 5000), MinedQuery("SELECT region, SUM(x) FROM sales GROUP BY region", 3)],
        _FakeExecutor())
    assert len(candidates) == 1
    assert "sales" in candidates[0].gold_sql


def test_information_schema_queries_are_dropped():
    assert is_noise("SELECT table_name FROM dataset.INFORMATION_SCHEMA.TABLES")


def test_a_bare_table_dump_is_dropped():
    # "select everything from this table" is a real query and a useless gold.
    assert is_noise("SELECT * FROM sales")
    assert is_noise("SELECT * FROM sales LIMIT 100")


def test_a_browse_with_a_filter_survives_mining_and_is_gated_as_a_whole_row():
    """The bare-dump rule only catches `SELECT * FROM t`. A star with a WHERE
    or a JOIN runs, returns rows and used to come back clean: 12 of 32 golds
    mined on one estate were that, and no agent could match a 151-column
    answer. It is still written; the reviewer decides."""
    log = [MinedQuery("SELECT * FROM sales.orders WHERE day = '2026-01-01'", 40),
           MinedQuery("SELECT region, SUM(x) FROM sales.orders GROUP BY region", 30)]

    candidates = mine_candidates(log, _FakeExecutor())

    assert [c.gate_status for c in candidates] == ["WHOLE_ROW", "ok"]
    assert candidates[0].usable is False
    assert "a browse and not a question" in candidates[0].gate_detail
    assert "  WHOLE_ROW            1" in summarize(candidates)


def test_a_real_query_is_not_noise():
    assert not is_noise("SELECT region, SUM(amount) FROM sales GROUP BY region")


def test_require_analytical_is_off_by_default():
    # A simple lookup can still be a legitimate gold question.
    assert not is_noise("SELECT name FROM emp WHERE id = 5")
    assert is_noise("SELECT name FROM emp WHERE id = 5", require_analytical=True)


def test_your_own_eval_account_can_be_excluded():
    # Mining your own eval traffic turns the agent's SQL into the gold it is
    # scored against. That is the leak leakage.check_leakage exists to catch,
    # arriving through the front door.
    queries = [
        MinedQuery("SELECT region, SUM(x) FROM sales GROUP BY region", 500, "evalbot@corp.com"),
        MinedQuery("SELECT country, SUM(y) FROM orders GROUP BY country", 3, "analyst@corp.com"),
    ]
    candidates = mine_candidates(queries, _FakeExecutor(),
                                 exclude_accounts={"evalbot@corp.com"})
    assert len(candidates) == 1
    assert "orders" in candidates[0].gold_sql


def test_exclusion_is_case_insensitive():
    queries = [MinedQuery("SELECT a FROM t", 9, "EvalBot@Corp.com")]
    assert mine_candidates(queries, _FakeExecutor(),
                           exclude_accounts={"evalbot@corp.com"}) == []


def test_a_plain_address_excludes_its_md5_from_a_hashed_log():
    # `log --hash-accounts` writes TO_HEX(MD5(LOWER(user_email))) into
    # ran_by; the person still types the address they know
    hashed = "89a91ba65dc757cf2ca52a2630a50e59"      # md5 of "evalbot@corp.com"
    queries = [
        MinedQuery("SELECT region, SUM(x) FROM sales GROUP BY region", 500, hashed),
        MinedQuery("SELECT country, SUM(y) FROM orders GROUP BY country", 3, "analyst@corp.com"),
    ]
    candidates = mine_candidates(queries, _FakeExecutor(),
                                 exclude_accounts={"EvalBot@Corp.com"})
    assert len(candidates) == 1
    assert "orders" in candidates[0].gold_sql


def test_a_shape_goes_when_any_of_its_accounts_is_excluded():
    # ran_by carries every account of the shape, joined by ';'. It used to be
    # one account picked at random, so a shape the eval account shared with an
    # analyst survived whenever the pick went the other way (76 of 3,000
    # shapes on one estate had two accounts).
    shared = MinedQuery("SELECT region, SUM(x) FROM sales GROUP BY region", 500,
                        "analyst@corp.com;evalbot@corp.com")
    theirs = MinedQuery("SELECT country, SUM(y) FROM orders GROUP BY country", 3,
                        "analyst@corp.com;other@corp.com")

    candidates = mine_candidates([shared, theirs], _FakeExecutor(),
                                 exclude_accounts={"evalbot@corp.com"})

    assert len(candidates) == 1
    assert "orders" in candidates[0].gold_sql


def test_a_plain_address_excludes_the_md5_of_its_lower_cased_form():
    # a hand-made hash of the address as typed matched nothing and said
    # nothing; mine lower-cases before hashing, the way the SQL does
    hashed = "89a91ba65dc757cf2ca52a2630a50e59"      # md5 of "evalbot@corp.com"
    queries = [MinedQuery("SELECT a, SUM(b) FROM t GROUP BY a", 9,
                          f"{hashed};d41d8cd98f00b204e9800998ecf8427e")]

    assert mine_candidates(queries, _FakeExecutor(),
                           exclude_accounts={"EvalBot@Corp.com"}) == []


def test_queries_without_an_account_are_kept():
    # ran_by is optional; a log without it should still mine.
    assert len(mine_candidates([MinedQuery("SELECT a FROM t", 2)], _FakeExecutor(),
                               exclude_accounts={"someone@corp.com"})) == 1


# --- dashboard tiles and how to rank past them ---


def test_a_single_kpi_tile_is_noise():
    # The real shape from a warehouse log: one aggregate, no grouping, no join.
    # It ran 107 times for one viewer and would make the gold "what is the
    # total shipped quantity?", which any system answers knowing nothing.
    tile = ("SELECT COALESCE(SUM(oi.quantity), 0) AS "
            "oi_quantity_measure_kpi FROM proj.sales.order_items")
    assert is_dashboard_tile(tile)
    assert is_noise(tile)


def test_an_aggregate_WITH_grouping_is_not_a_tile():
    assert not is_dashboard_tile("SELECT region, SUM(x) FROM t GROUP BY region")


def test_an_aggregate_across_a_join_is_not_a_tile():
    assert not is_dashboard_tile("SELECT SUM(x) FROM a JOIN b USING (k)")


def test_table_count_sees_joins():
    assert table_count("SELECT * FROM a") == 1
    assert table_count("SELECT x FROM proj.ds.a JOIN proj.ds.b USING (k)") == 2


def test_min_tables_skips_single_table_queries():
    queries = [
        MinedQuery("SELECT region, SUM(x) FROM sales GROUP BY region", 900),
        MinedQuery("SELECT s.region, SUM(o.amt) FROM sales s JOIN orders o USING (k) GROUP BY 1", 4),
    ]
    candidates = mine_candidates(queries, _FakeExecutor(), min_tables=2)
    assert len(candidates) == 1
    assert "JOIN" in candidates[0].gold_sql


def test_ranking_by_users_beats_ranking_by_executions_on_dashboard_traffic():
    # A refresh runs constantly for one viewer; a real question is asked once
    # by several people. Frequency ranks the machine, users rank the people.
    queries = [
        MinedQuery("SELECT a, SUM(x) FROM t GROUP BY a", 900, "svc@corp", distinct_users=1),
        MinedQuery("SELECT b, SUM(y) FROM t GROUP BY b", 6, "analyst@corp", distinct_users=9),
    ]
    by_freq = mine_candidates(queries, _FakeExecutor(), rank_by="frequency")
    by_users = mine_candidates(queries, _FakeExecutor(), rank_by="users")
    assert "SUM(x)" in by_freq[0].gold_sql
    assert "SUM(y)" in by_users[0].gold_sql


def test_an_unknown_ranking_is_refused():
    import pytest
    with pytest.raises(ValueError, match="frequency"):
        mine_candidates([MinedQuery("SELECT a FROM t")], _FakeExecutor(), rank_by="magic")


def test_question_writer_keeps_the_first_line_and_strips_quotes_and_label():
    from context_lift.backends import echo_backend
    from context_lift.mine import PromptQuestionWriter, QUESTION_PROMPT
    writer = PromptQuestionWriter(echo_backend('Question: "How many orders shipped in 2024?"\nextra line'))
    assert writer.write("SELECT COUNT(*) FROM orders WHERE shipped_at IS NOT NULL") == "How many orders shipped in 2024?"
    assert "{sql}" in QUESTION_PROMPT


def test_the_question_prompt_asks_for_the_filter_values_and_a_reply_keeps_them():
    """One draft read "units of a specific product on a specific day" while
    the SQL held all three literals, so the question had nothing to answer."""
    from context_lift.backends import echo_backend
    from context_lift.mine import PromptQuestionWriter, QUESTION_PROMPT

    assert "STATE EVERY PARAMETER" in QUESTION_PROMPT
    # read with the line wrapping taken out: the prompt is prose, and where
    # it breaks a line is not what any of these assertions are about
    assert "a specific product on a specific day" in " ".join(QUESTION_PROMPT.split())

    writer = PromptQuestionWriter(echo_backend("units of Blue Shirt on 2024-01-05"))
    question = writer.write("SELECT SUM(units) FROM sales WHERE product = 'Blue Shirt' "
                            "AND day = '2024-01-05'")

    assert question == "units of Blue Shirt on 2024-01-05"


def test_the_prompt_carries_the_four_rules_and_names_the_two_that_conflict():
    """docs/research/README.md practice 11. 140 of 372 candidates on one estate were
    rejected because the English did not ask what the SQL answers, so the four
    rules a question has to satisfy at once are in the prompt, and so is the
    one test that separates the two which pull against each other.
    """
    from context_lift.mine import QUESTION_PROMPT

    # line wrapping taken out: the prompt is prose, and where it breaks a
    # line is not what any of this is about
    flat = " ".join(QUESTION_PROMPT.split())

    assert "IDENTIFY THE SAME ANSWER" in flat        # rows, grouping, aggregation, limit
    assert "STATE EVERY PARAMETER" in flat           # a date, an id
    assert "STATE NO BUSINESS CODES" in flat         # what the written context teaches
    assert "USE BUSINESS WORDS, NOT COLUMN NAMES" in flat
    # rules 2 and 3 contradict each other and the prompt has to say so, with
    # the one test that tells them apart
    assert "pull against each other" in flat
    assert "never seen this table, supply the value" in flat
    # the code it must never spell out, and the limit it must carry
    assert "status != 'CANCELLED'" in flat
    assert "top 10" in flat
    assert "{sql}" in QUESTION_PROMPT and "{note}" in QUESTION_PROMPT


def test_the_analysts_comment_reaches_the_drafter_above_the_rules():
    """The comment says what the query is FOR and the drafter never saw it:
    `strip_comments` runs in every stage. 20 of the 59 rows one reviewer
    rejected as does_not_match carried one."""
    from context_lift.mine import NOTE_PREFACE, PromptQuestionWriter

    seen = []

    def backend(prompt):
        seen.append(prompt)
        return "which materials have a standard cost of 0?"

    writer = PromptQuestionWriter(backend)
    writer.write("SELECT material FROM costs", "-- Pull the actual list of the 108")

    assert "-- Pull the actual list of the 108" in seen[0]
    assert seen[0].index("Pull the actual list") < seen[0].index("IDENTIFY THE SAME ANSWER")
    assert "{note}" not in seen[0]
    # and no empty heading when there is no comment: a preface with nothing
    # under it is one more thing for the model to interpret
    writer.write("SELECT material FROM costs")
    assert NOTE_PREFACE.split("{note}")[0] not in seen[1]


def test_the_comment_the_drafter_is_shown_is_the_prose_one():
    """What a comment holds is either purpose or switched-off SQL, and the
    second must not reach the drafter: two shapes on one estate had their
    WHERE line inside a comment, and a question drafted from a filter the
    query does not apply reads as correct in a spreadsheet."""
    from context_lift.mine import NOTE_CHARS, analyst_note

    assert analyst_note("SELECT a FROM t\n-- Pull the actual list of the 108 for "
                        "follow-up") == "-- Pull the actual list of the 108 for follow-up"
    assert analyst_note("SELECT a FROM t /* monthly board pack */") == "/* monthly board pack */"
    # ordinary English keeps its "and": refusing that word anywhere would
    # throw away the most useful signal in the row
    assert "audit" in analyst_note("SELECT a FROM t\n-- materials and plants for the audit")

    assert analyst_note("SELECT a FROM t\n-- WHERE region = 'EMEA'") == ""
    assert analyst_note("SELECT a FROM t\n-- AND status = 'CANCELLED'") == ""
    assert analyst_note("SELECT a FROM t\n-- was 'CANCELLED' before the migration") == ""
    assert analyst_note("SELECT a FROM t") == ""
    # a header banner is a changelog, not a purpose
    long_note = analyst_note("SELECT a FROM t\n-- " + "purpose " * 100)
    assert len(long_note) <= NOTE_CHARS + 3 and long_note.endswith("...")


def test_a_writer_that_only_takes_the_sql_still_gets_called():
    """`write(sql)` is what every writer here had before the comment became
    an input, so the second argument is offered, never required."""
    class _OneArgument:
        def __init__(self):
            self.seen = []

        def write(self, sql):
            self.seen.append(sql)
            return "how many materials cost nothing?"

    writer = _OneArgument()
    candidates = mine_candidates(
        [MinedQuery("SELECT plant, COUNT(*) FROM costs\n-- the 108 for follow-up\n"
                    "GROUP BY plant", 3)],
        _FakeExecutor(), question_writer=writer)

    assert writer.seen and "follow-up" not in writer.seen[0]
    assert candidates[0].question == "how many materials cost nothing?"


def test_ranking_spreads_across_tables_so_the_busiest_cannot_eat_the_sample():
    """One data product ran 10900 queries a month against 2400 for the 27 in
    scope; top-N made 54% of a key one product. Round-robin gives each
    table's best shape before any table's second."""
    log = [MinedQuery(f"SELECT c{i} FROM big.orders WHERE x = {i}", 100 - i) for i in range(5)]
    log += [MinedQuery("SELECT a FROM small.jobs", 10), MinedQuery("SELECT b FROM small.jobs", 9)]

    spread = mine_candidates(log, _FakeExecutor(), limit=4)
    top_n = mine_candidates(log, _FakeExecutor(), limit=4, spread=False)

    assert [c.table for c in spread] == ["big.orders", "small.jobs", "big.orders", "small.jobs"]
    assert [c.table for c in top_n] == ["big.orders"] * 4


def test_a_datasets_scope_keeps_only_shapes_on_those_datasets():
    log = [MinedQuery("SELECT a FROM sales.orders", 50), MinedQuery("SELECT b FROM ops.jobs", 90),
           MinedQuery("SELECT c FROM unqualified_table", 70)]

    scoped = mine_candidates(log, _FakeExecutor(), datasets={"sales"})

    assert [c.table for c in scoped] == ["sales.orders"]


def test_unqualified_shapes_are_counted_and_placed_in_the_db_dataset_under_a_scope():
    """A third of one log was written against a default dataset; a scoped
    mine dropped every such shape and said nothing."""
    log = [MinedQuery("SELECT a FROM sales.orders", 50), MinedQuery("SELECT b FROM ops.jobs", 90),
           MinedQuery("SELECT c FROM tickets", 70), MinedQuery("SELECT d FROM refunds", 10)]
    dropped_counts: dict = {}
    placed_counts: dict = {}

    dropped = mine_candidates(log, _FakeExecutor(), datasets={"sales"}, counts_out=dropped_counts)
    placed = mine_candidates(log, _FakeExecutor(), datasets={"sales"}, default_dataset="sales",
                             counts_out=placed_counts)

    assert [c.table for c in dropped] == ["sales.orders"]
    assert dropped_counts == {"shapes": 4, "unqualified": 2, "placed": 0, "placed_in": None,
                              "in_scope": 1}
    assert [c.table for c in placed] == ["sales.tickets", "sales.orders", "sales.refunds"]
    assert placed_counts == {"shapes": 4, "unqualified": 2, "placed": 2, "placed_in": "sales",
                             "in_scope": 3}
    assert ("  2 shapes name a table without a dataset and were dropped: nothing says which "
            "dataset they mean; --db bq://BILLING/DATASET places them there"
            ) in summarize(dropped, datasets=["sales"], counts=dropped_counts)
    assert ("  2 shapes name a table without a dataset and were placed in sales (the --db dataset)"
            ) in summarize(placed, datasets=["sales"], counts=placed_counts)
    # without a scope nothing is placed and nothing is said: the table column stays bare
    unscoped_counts: dict = {}
    unscoped = mine_candidates(log, _FakeExecutor(), default_dataset="sales", counts_out=unscoped_counts)
    assert sorted(c.table for c in unscoped) == ["ops.jobs", "refunds", "sales.orders", "tickets"]
    assert unscoped_counts == {"shapes": 4, "in_scope": 4}


def test_a_shape_that_reads_a_table_the_run_cannot_reach_says_so_on_the_row(tmp_path):
    """Two field golds were labelled with an in-scope dimension table, the
    only in-scope table in the query, while the query really reads a fact
    table in a third project and the question asks for measures the dimension
    does not hold. Both were routing misses no agent could win, and the review
    sheet said nothing. The row is still written; a reviewer decides."""
    log = [MinedQuery("SELECT c.name, SUM(f.amount) AS spend "
                      "FROM DATASET.customers c "
                      "JOIN OTHER_PROJECT.facts.sales f ON f.customer_id = c.id "
                      "GROUP BY c.name", 40),
           MinedQuery("SELECT c.name, COUNT(*) AS n FROM DATASET.customers c "
                      "JOIN DATASET.orders o ON o.customer_id = c.id GROUP BY c.name", 30)]
    counts: dict = {}

    candidates = mine_candidates(log, _FakeExecutor(), datasets={"DATASET"},
                                 project="PROJECT", counts_out=counts)
    path = write_candidates(candidates, tmp_path / "candidates.csv")

    assert [(c.table, c.out_of_scope) for c in candidates] == [
        ("dataset.customers", "other_project.facts.sales"),
        ("dataset.customers", "")]
    assert counts["out_of_scope"] == 1
    rows = list(csv.DictReader(path.open()))
    assert [row["out_of_scope"] for row in rows] == ["other_project.facts.sales", ""]
    assert ("  1 of 2 shapes name a table the run cannot reach; the out_of_scope column "
            "names it") in summarize(candidates, datasets=["DATASET"], counts=counts)


def test_which_tables_count_as_out_of_scope():
    from context_lift.mine import out_of_scope_tables
    sql = ("SELECT 1 FROM DATASET.customers c "
           "JOIN PROJECT.DATASET.orders o ON 1=1 "
           "JOIN OTHER_PROJECT.DATASET.sales s ON 1=1 "
           "JOIN elsewhere.returns r ON 1=1 "
           "JOIN bare_table b ON 1=1")

    outside = out_of_scope_tables(sql, datasets={"DATASET"}, project="PROJECT")

    # another project, and a dataset the scope does not name; the run's own
    # project is fine however the name spells it, and a bare name says nothing
    assert outside == ["other_project.dataset.sales", "elsewhere.returns"]
    # a bare name resolves in the --db dataset when one is known
    assert out_of_scope_tables("SELECT 1 FROM bare_table", datasets={"DATASET"},
                               default_dataset="other") == ["bare_table"]
    # no scope and no project: nothing is out of scope
    assert out_of_scope_tables(sql) == []


def test_mine_reads_several_logs_as_one(tmp_path, capsys):
    """A job log is per billing project; the data of one estate spans
    several, so the logs are pulled one by one and mined together."""
    import sqlite3
    from context_lift.cli import main
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE orders(id INT)")
    con.execute("CREATE TABLE jobs(id INT)")
    con.executemany("INSERT INTO orders VALUES (?)", [(1,), (2,)])
    con.executemany("INSERT INTO jobs VALUES (?)", [(1,)])
    con.commit(); con.close()
    first = tmp_path / "a.csv"
    first.write_text("sql,times_seen\n\"SELECT id, COUNT(*) AS n FROM orders GROUP BY id\",5\n")
    second = tmp_path / "b.csv"
    second.write_text("sql,times_seen\n\"SELECT id, COUNT(*) AS n FROM jobs GROUP BY id\",3\n"
                      "\"SELECT id, COUNT(*) AS n FROM orders GROUP BY id\",2\n")
    out = tmp_path / "candidates.csv"

    # --all-datasets: this test mines a whole two-table log on purpose.
    # Without it `mine` refuses to guess its scope (practices 1 and 2).
    code = main(["mine", "--db", str(db), "--queries", str(first), "--queries", str(second),
                 "--all-datasets", "--out", str(out)])

    assert code == 0
    rows = list(csv.DictReader(out.open()))
    assert sorted((r["table"], r["times_seen"]) for r in rows) == [("jobs", "3"), ("orders", "7")]
    assert "2 candidate shapes, 2 clean" in capsys.readouterr().out


def test_summary_prints_the_table_concentration_and_the_scope():
    log = [MinedQuery(f"SELECT c{i} FROM big.orders", 10) for i in range(3)]
    log += [MinedQuery("SELECT a FROM small.jobs", 5)]
    candidates = mine_candidates(log, _FakeExecutor(), spread=False)

    text = summarize(candidates)

    assert "tables: 2 across 2 datasets; largest: big.orders 3 (75%), small.jobs 1 (25%)" in text
    assert "an even sample would put 50% on each" in text
    assert "scope: the whole log" in text
    assert "scope: datasets big" in summarize(candidates, datasets=["big"])


def test_per_part_backticks_still_name_the_dataset_and_table():
    """BigQuery's console emits `project`.`dataset`.`table`; the old match
    stopped at the first closing backtick and made the project the table,
    which collapsed 206 of 400 shapes on one log into two fake buckets."""
    from context_lift.mine import tables_named

    assert tables_named("SELECT 1 FROM `proj`.`sales`.`orders` o JOIN `proj`.`sales`.`items` i ON 1=1") \
        == ["sales.orders", "sales.items"]
    assert tables_named("SELECT 1 FROM `proj.sales.orders`") == ["sales.orders"]
    assert tables_named("SELECT 1 FROM sales.orders") == ["sales.orders"]


def test_a_scope_that_matches_nothing_is_reported_as_the_scope_not_an_empty_log():
    """A log with 193 read-only queries printed "no read-only queries" when
    the scope matched none of them, sending the reader to the wrong place."""
    log = [MinedQuery("SELECT a FROM sales.orders", 5), MinedQuery("SELECT b FROM ops.jobs", 3)]
    counts: dict = {}

    candidates = mine_candidates(log, _FakeExecutor(), datasets={"finance"}, counts_out=counts)

    assert candidates == [] and counts == {"shapes": 2, "in_scope": 0}
    text = summarize(candidates, datasets=["finance"], counts=counts)
    assert "2 read-only query shapes in the log, 0 with a primary table in datasets finance" in text
    assert "no read-only queries" not in text


def test_dbt_temporary_and_backup_tables_are_noise():
    """Two candidates on one estate named `..._pricing__dbt_tmp`: a table that
    exists for the minutes a dbt build runs is never a gold."""
    from context_lift.mine import is_noise

    assert is_noise("SELECT SUM(amount) FROM sales.pricing__dbt_tmp GROUP BY sku")
    assert is_noise("SELECT * FROM `p`.`sales`.`pricing__dbt_backup` WHERE x = 1")
    assert not is_noise("SELECT SUM(amount) FROM sales.pricing GROUP BY sku")


def test_a_bi_tools_filter_dropdown_query_is_noise_even_though_it_groups():
    """13 of 22 gate-clean candidates on one estate were this shape; a GROUP BY
    is not evidence of analysis."""
    from context_lift.mine import is_noise
    dropdown = ("SELECT tbl.business_unit AS tbl_business_unit FROM p.d.t AS tbl "
                "WHERE ((UPPER((tbl.business_unit)) LIKE UPPER('%') OR UPPER((tbl.business_unit)) "
                "LIKE UPPER('% %'))) GROUP BY 1 ORDER BY 1 LIMIT 1000")
    values_list = "SELECT status FROM p.d.orders GROUP BY 1 ORDER BY 1 LIMIT 1000"
    real = "SELECT status, COUNT(*) FROM p.d.orders GROUP BY 1 ORDER BY 2 DESC LIMIT 10"

    assert is_noise(dropdown)
    assert is_noise(values_list)
    assert not is_noise(real)


def test_a_question_missing_a_business_code_is_flagged_and_kept():
    """14 of 25 golds on one field key filtered on a literal their question
    never gave. Both arms scored 0.000 and nothing said the key was the cause.

    When the missing value is a business CODE the row is KEPT and flagged:
    omitting it is what `--rules-in-context` is for, because the written
    context has to supply that the code means something, and whether it does
    is the measurement.
    """
    class _Vague:
        def write(self, sql):
            return "units for a specific product"

    sql = ("SELECT SUM(units) FROM sales WHERE product_code = 'PRD-1042' "
           "GROUP BY 1")
    executor = _FakeExecutor()

    candidates = mine_candidates([MinedQuery(sql, 5)], executor, question_writer=_Vague())

    assert "MISSING_LITERAL" in candidates[0].gate_status
    assert candidates[0].usable is True       # a warning never drops a candidate
    assert "PRD-1042" in candidates[0].gate_detail
    assert "business CODES" in candidates[0].gate_detail
    assert candidates[0].gold_sql == sql
    assert executor.seen[0] == sql                      # gated like any other candidate


def test_a_question_missing_a_parameter_is_refused():
    """And when the missing value is a question PARAMETER the row is REFUSED,
    because nothing can supply it.

    A day is not a business rule. No written context tells an agent which day
    the person meant, so the gold scores wrong in BOTH arms in every round
    anybody will ever run, and its only effect is to dilute the lift -- the
    same harm a dashboard tile does. Three such golds sat in one estate's
    18-question reserved set, guaranteed wrong forever, and the gate kept them
    because it called this the same warning as the case above.
    """
    class _Vague:
        def write(self, sql):
            return "units for a specific product on a specific day"

    sql = ("SELECT SUM(units) FROM sales WHERE product_code = 'PRD-1042' "
           "AND day = '2024-01-05' GROUP BY 1")

    candidates = mine_candidates([MinedQuery(sql, 5)], _FakeExecutor(),
                                 question_writer=_Vague())

    assert "MISSING_PARAMETER" in candidates[0].gate_status
    assert candidates[0].usable is False            # a defect drops it
    assert "2024-01-05" in candidates[0].gate_detail
    assert "PARAMETERS" in candidates[0].gate_detail


def test_the_writer_is_asked_once_more_with_the_missing_values_and_the_retry_counts():
    class _LearnsOnRetry:
        def __init__(self):
            self.prompts = []

        def write(self, sql):
            self.prompts.append("write")
            return "units for a specific product"

        def rewrite(self, sql, question, missing):
            self.prompts.append(f"rewrite {missing}")
            return "units of PRD-1042"

    writer = _LearnsOnRetry()
    counts: dict = {}

    candidates = mine_candidates(
        [MinedQuery("SELECT day, SUM(units) FROM sales WHERE product_code = 'PRD-1042' GROUP BY day", 5)],
        _FakeExecutor(), question_writer=writer, counts_out=counts)

    # the retry lists everything the draft owes, not only the values
    assert writer.prompts[0] == "write"
    asked = writer.prompts[1]
    assert "the value 'PRD-1042', spelled exactly like that" in asked
    assert "what the answer shows, in business words: it returns day" in asked
    assert candidates[0].question == "units of PRD-1042"
    # the retry fixes the literal; the projection check is a different question
    assert "MISSING_LITERAL" not in candidates[0].gate_status
    assert counts["question_retries"] == 1
    assert counts["question_retries_used"] == 1


def test_a_retry_that_still_leaves_the_value_out_is_not_asked_a_third_time():
    """One retry, not a loop: a model that ignored the list once ignores it
    again, and a second call per shape is a second bill."""
    class _NeverLearns:
        def __init__(self):
            self.calls = 0

        def write(self, sql):
            self.calls += 1
            return "units for a specific product"

        def rewrite(self, sql, question, missing):
            self.calls += 1
            return "units for that product"

    writer = _NeverLearns()
    counts: dict = {}

    candidates = mine_candidates(
        [MinedQuery("SELECT day, SUM(units) FROM sales WHERE product_code = 'PRD-1042' GROUP BY day", 5)],
        _FakeExecutor(), question_writer=writer, counts_out=counts)

    assert writer.calls == 2
    assert candidates[0].question == "units for a specific product"   # the first draft is kept
    assert "MISSING_LITERAL" in candidates[0].gate_status
    assert candidates[0].usable is True       # a warning never drops a candidate
    assert counts["question_retries"] == 1
    assert "question_retries_used" not in counts


def test_an_empty_retry_never_replaces_the_first_draft():
    """An empty question has no missing values to find, which would read as a
    fix. A model that returns nothing must not look like the better answer."""
    class _ReturnsNothing:
        def write(self, sql):
            return "units for a specific product"

        def rewrite(self, sql, question, missing):
            return ""

    candidates = mine_candidates(
        [MinedQuery("SELECT day, SUM(units) FROM sales WHERE product_code = 'PRD-1042' GROUP BY day", 5)],
        _FakeExecutor(), question_writer=_ReturnsNothing())

    assert candidates[0].question == "units for a specific product"
    assert "MISSING_LITERAL" in candidates[0].gate_status
    assert candidates[0].usable is True       # a warning never drops a candidate


def test_a_writer_that_cannot_rewrite_is_used_as_it_is():
    class _WriteOnly:
        def write(self, sql):
            return "units for a specific product"

    counts: dict = {}

    candidates = mine_candidates(
        [MinedQuery("SELECT day, SUM(units) FROM sales WHERE product_code = 'PRD-1042' GROUP BY day", 5)],
        _FakeExecutor(), question_writer=_WriteOnly(), counts_out=counts)

    assert "MISSING_LITERAL" in candidates[0].gate_status
    assert candidates[0].usable is True       # a warning never drops a candidate
    assert counts.get("question_retries") is None


def test_a_question_written_by_hand_in_the_log_is_checked_the_same_way():
    """A hand-written question has the same problem as a drafted one, so the
    check does not depend on --write-questions."""
    queries = [MinedQuery("SELECT day, SUM(units) FROM sales WHERE product_code = 'PRD-1042' GROUP BY day",
                          5, question="units for a specific product")]

    candidates = mine_candidates(queries, _FakeExecutor())

    assert candidates[0].question == "units for a specific product"
    assert "MISSING_LITERAL" in candidates[0].gate_status
    assert candidates[0].usable is True       # a warning never drops a candidate


def test_the_summary_counts_the_questions_that_leave_a_value_out():
    class _Vague:
        def write(self, sql):
            return "units for a specific product"

        def rewrite(self, sql, question, missing):
            return "units for that product"

    counts: dict = {}
    candidates = mine_candidates(
        [MinedQuery("SELECT day, SUM(units) FROM sales WHERE product_code = 'PRD-1042' GROUP BY day", 5),
         MinedQuery("SELECT day, SUM(units) FROM sales WHERE region = 'north' GROUP BY day", 4)],
        _FakeExecutor(), question_writer=_Vague(), counts_out=counts)

    text = summarize(candidates, counts=counts)

    assert "2 of 2 questions leave out a value their gold filters on" in text
    assert "2 questions were drafted a second time" in text
    assert "the gate rejected:" in text and "MISSING_LITERAL" not in text.split(
        "the gate rejected:")[1]              # a warning is not a rejection
    assert len(candidates) == 2                         # nothing was dropped


def test_the_retry_prompt_names_the_values_and_asks_for_one_line():
    from context_lift.backends import echo_backend
    from context_lift.mine import RETRY_PROMPT, PromptQuestionWriter

    seen: list[str] = []

    def backend(prompt):
        seen.append(prompt)
        return "units of Blue Shirt on 2024-01-05"

    writer = PromptQuestionWriter(backend)
    question = writer.rewrite("SELECT day, SUM(units) FROM sales WHERE product = 'Blue Shirt' GROUP BY day",
                              "units of a specific product", ["Blue Shirt"])

    assert question == "units of Blue Shirt on 2024-01-05"
    assert "- Blue Shirt" in seen[0]
    assert "units of a specific product" in seen[0]
    assert "{missing}" in RETRY_PROMPT and "{question}" in RETRY_PROMPT
    assert PromptQuestionWriter(echo_backend("one\ntwo")).rewrite("SELECT 1", "q", ["x"]) == "one"


def test_a_question_keeps_its_filter_values_by_default():
    """The ordinary case: a self-contained question a person can answer."""
    from context_lift.mine import _what_the_question_owes

    owed = _what_the_question_owes(
        "SELECT COUNT(*) FROM orders WHERE status = 'Cancelled'",
        "How many orders are there?")

    assert any("Cancelled" in line for line in owed)


def test_rules_in_context_leaves_the_filter_values_out_of_the_question():
    """A run measuring whether written context helps needs the opposite.

    The business rule lives in the context, and a question that says
    "excluding Cancelled" hands the bare arm the rule for free, so no written
    context can show a lift. Measured on 25 mined golds: 10 of them, 40%,
    named a rule this way and were unusable for a paired comparison.
    """
    from context_lift.mine import _what_the_question_owes

    owed = _what_the_question_owes(
        "SELECT COUNT(*) FROM orders WHERE status = 'Cancelled'",
        "How many orders are there?", rules_in_context=True)

    assert not any("Cancelled" in line for line in owed)


def test_rules_in_context_still_asks_what_the_answer_shows_and_its_order():
    """Only the values are dropped. What the answer returns and which rows
    come first are not business rules, and a question that leaves those out is
    unanswerable by anyone."""
    from context_lift.mine import _what_the_question_owes

    owed = _what_the_question_owes(
        "SELECT brand, SUM(x) AS revenue FROM t WHERE status = 'Cancelled' "
        "GROUP BY brand ORDER BY revenue DESC",
        "Which brands?", rules_in_context=True)

    assert any("what the answer shows" in line for line in owed)
    assert any("which rows come first" in line for line in owed)
    assert not any("Cancelled" in line for line in owed)


def test_the_analysts_comment_travels_from_the_log_to_the_writer():
    """End to end: the comment is on the SQL in the log, the writer is handed
    it beside the stripped statement, and the sheet still shows the original
    text. This is the signal 20 of 59 rejected rows carried and no stage of
    the pipeline had ever shown the drafter.
    """
    sql = ("-- Pull the actual list of the 108 for follow-up\n"
           "SELECT material, plant, SUM(value) FROM costs GROUP BY material, plant")

    class _Writer:
        def __init__(self):
            self.seen: list[tuple[str, str]] = []

        def write(self, text, comment=""):
            self.seen.append((text, comment))
            return "which of the 108 materials cost nothing, and at which plant?"

    writer = _Writer()
    candidates = mine_candidates([MinedQuery(sql, 7)], _FakeExecutor(),
                                 question_writer=writer)

    statement, comment = writer.seen[0]
    assert comment == "-- Pull the actual list of the 108 for follow-up"
    assert "follow-up" not in statement               # the SQL it reads is still stripped
    assert candidates[0].gold_sql == sql              # the sheet keeps the original text
    assert "reads as a list" not in candidates[0].gate_detail


def test_a_noun_phrase_is_told_apart_from_a_question():
    """The drafter's habit was to write the SELECT list in prose. The first
    string here is a real draft from one estate, next to a real comment that
    said what the query was for: "-- Pull the actual list of the 108 for
    follow-up". Of 372 candidates on two pulls, 140 (38%) died this way.
    """
    from context_lift.mine import reads_as_a_column_list

    assert reads_as_a_column_list(
        "The material, plant, from date, to date, cost estimate number, and total "
        "value per unit for materials with a standard cost of 0") is True
    assert reads_as_a_column_list("the order count per region") is True
    assert reads_as_a_column_list("revenue by month in 2023") is True

    assert reads_as_a_column_list("How many orders shipped in 2024?") is False
    assert reads_as_a_column_list(
        "which materials have a standard cost of 0") is False       # no question mark
    assert reads_as_a_column_list("list the 108 materials to follow up") is False
    # the clause before the comma is not the whole sentence
    assert reads_as_a_column_list("For each month, how many orders shipped") is False
    # a run without --write-questions leaves every question empty for a person
    # to fill in, and flagging all of them as bad English says nothing
    assert reads_as_a_column_list("") is False


def test_a_question_that_reads_as_a_column_list_is_flagged_counted_and_kept():
    """Flagged, never dropped: the gate reads the SQL and a person reads the
    sentence. On one estate every one of the 140 rejected rows had SQL that
    ran on BigQuery, returned rows and passed all 16 gate checks, and nothing
    in the review file said the sentence was the problem.
    """
    class _WritesTheSelectList:
        def write(self, sql, comment=""):
            return "The material, plant and total value per unit"

    counts: dict = {}
    candidates = mine_candidates(
        [MinedQuery("SELECT material, plant, SUM(value) FROM costs GROUP BY material, plant", 6)],
        _FakeExecutor(), question_writer=_WritesTheSelectList(), counts_out=counts)

    assert "reads as a list of the selected columns" in candidates[0].gate_detail
    assert candidates[0].gate_status == "ok"          # the gate did not move
    assert candidates[0].usable is True
    assert counts["column_list_questions"] == 1
    text = summarize(candidates, counts=counts)
    assert "1 of 1 questions read as a list of the selected columns" in text
    assert "140 of 372" in text                       # what it costs, in the summary


def test_a_drafted_column_list_is_asked_again_as_a_question():
    """The one retry the drafter already makes, with the sentence added to
    what it owes. No second model call: the check is deterministic, so the
    cost of catching this is zero and the fix costs the retry that was
    already budgeted (counts as one call in the cost line)."""
    from context_lift.mine import COLUMN_LIST_OWED

    class _LearnsOnRetry:
        def __init__(self):
            self.asked: list[str] = []

        def write(self, sql, comment=""):
            return "the order count per region"

        def rewrite(self, sql, question, missing):
            self.asked.append("\n".join(missing))
            return "how many orders did we get in each region?"

    writer = _LearnsOnRetry()
    counts: dict = {}
    candidates = mine_candidates(
        [MinedQuery("SELECT region, COUNT(*) FROM orders GROUP BY region", 4)],
        _FakeExecutor(), question_writer=writer, counts_out=counts)

    assert COLUMN_LIST_OWED in writer.asked[0]
    assert candidates[0].question == "how many orders did we get in each region?"
    assert counts["question_retries"] == 1 and counts["question_retries_used"] == 1
    assert "reads as a list" not in candidates[0].gate_detail   # the retry fixed it
    assert "column_list_questions" not in counts


def test_a_tools_own_sql_is_not_a_question():
    """dbt model builds and CI deployment checks run SQL, and a job log is
    full of them. They produced golds reading "Return the column names that
    are inactive for the view_daily_visits table" - a data-quality probe, not a
    business question.

    Excluding service accounts does NOT catch these: dbt and GitHub Actions
    run under a person's credentials, so the job history shows a human.
    """
    from context_lift.mine import written_by_a_tool

    assert written_by_a_tool('/* {"app": "dbt", "dbt_version": "1.8.8"} */ SELECT 1')
    assert written_by_a_tool('/* {"deployment-method": "github-actions"} */ SELECT 1')
    assert written_by_a_tool('/* {"profile_name": "excess_obsolete"} */ SELECT 1')
    assert not written_by_a_tool("SELECT region, SUM(units) FROM t GROUP BY 1")


def test_a_question_that_is_mostly_literals_is_one_persons_lookup():
    """"units per store for styles '00501','04511','00505','29507','00514'"
    is not a question anyone else asks. The agent under test answered 16 of
    these with no SQL at all."""
    from context_lift.mine import literal_count

    many = ("Weekly average units per store for styles '00501', '04511', "
            "'00505', '29507', '00514', 'A4750'")
    assert literal_count(many) == 6
    assert literal_count("revenue in 2024 for Mexico") == 0
    assert literal_count("orders where status = 'Complete'") == 1, (
        "one value is normal in a real question; the limit is two")


def test_both_filters_are_on_by_default():
    """A person mining a customer's log for the first time does not know to
    ask for these. They found out by getting 16 unanswerable golds."""
    import inspect

    from context_lift.mine import mine_candidates

    defaults = inspect.signature(mine_candidates).parameters
    assert defaults["drop_machine_sql"].default is True
    assert defaults["max_question_literals"].default == 2


def test_the_budget_ladder_prices_each_ceiling_from_the_dry_run_figures():
    """"raise --max-bytes" is advice nobody can act on. Raise it to what, and
    what does running them cost? The figures come from the executor's own
    refusal text, measured on one warehouse: 112 refused queries from 1.39 GB
    to 29,545 GB, ceiling 1 GB."""
    from context_lift.mine import budget_ladder
    details = [f"rejected: the query would scan more than the 1 GB ceiling "
               f"(dry-run: {gb} GB); narrow it or raise --max-bytes"
               for gb in [1.39, 4.0, 8.67, 9.5, 41.2, 190.0, 640.0, 29545.64]]
    lines = budget_ladder(details)
    text = "\n".join(lines)
    assert "it was 1 GB" in text, "a person needs to know what the ceiling is now"
    assert "--max-bytes" in text
    # every rung names how many come back and what the run costs
    rungs = [line for line in lines if line.strip().startswith("--max-bytes")]
    assert rungs, "at least one rung"
    for line in rungs:
        assert "come back" in line and "TiB to run once" in line and "~$" in line
    # the outlier must be called out: without it a person raises the ceiling
    # to 30 TB for one extra gold
    assert "29,546 GB on its own" in text
    assert "paid for one gold" in text


def test_the_budget_ladder_is_silent_when_nothing_hit_the_ceiling():
    """The check has to be able not to fire: a clean estate must not read
    advice about a ceiling that turned nothing away."""
    from context_lift.mine import budget_ladder
    assert budget_ladder([]) == []
    assert budget_ladder(["the SQL did not run: no such column: nope", ""]) == []


def test_the_literal_cap_ladder_prices_a_looser_cap_from_what_was_dropped():
    """A cap is a number somebody chose, and until it is priced nobody can
    argue with it. Measured on one warehouse: the cap was 2 and the drafted
    questions carried 0 to 35 quoted values. Raising it to 3 recovered a
    fifth of the clean candidates; raising it to 6 recovered almost nothing
    more and started letting in the questions the cap exists for."""
    from context_lift.mine import literal_ladder
    dropped = [3] * 39 + [4] * 16 + [5] * 4 + [6] * 13 + [7, 8, 10, 26, 35]
    lines = literal_ladder(dropped, 2)
    text = "\n".join(lines)
    assert "it is 2" in text, "a person needs to know the cap they are pricing"
    assert "--max-question-literals 3" in text
    assert "39 of 77 dropped questions come back" in text
    assert "the largest carried 35 quoted values" in text


def test_the_ladder_does_not_argue_against_its_own_cap():
    """"the largest carried 3 quoted values, which is the kind of question
    the cap is for" is a sentence that contradicts itself. The note only
    belongs when the largest really is one analyst's own lookup."""
    from context_lift.mine import literal_ladder
    barely = "\n".join(literal_ladder([3], 2))
    assert "--max-question-literals 3" in barely
    assert "kind of question the cap is for" not in barely
    assert "kind of question the cap is for" in "\n".join(literal_ladder([3, 35], 2))


def test_the_ladder_is_silent_when_the_cap_dropped_nothing():
    """It has to be able not to fire: a clean estate must not read advice
    about a cap that threw nothing away."""
    from context_lift.mine import literal_ladder
    assert literal_ladder([], 2) == []


# --- the budget as a ceiling that holds, not an estimate that is forgotten ---
#
# These pin the three defects one customer run exposed together: a `--budget`
# that only ever compared an estimate, an output file written once at the very
# end, and a progress line that never mentioned money. The run billed $12.41
# against an $8.89 estimate over 36 minutes and had written nothing.


def _shapes(n: int) -> list[MinedQuery]:
    return [MinedQuery(sql=f"SELECT count(*) c{i} FROM sales JOIN dim d USING (k) "
                           f"WHERE region = 'r{i}'", times_seen=n - i)
            for i in range(n)]


class _CountingSpend:
    """Dollars that go up by a fixed amount per query the executor runs."""

    def __init__(self, per_query: float):
        self.per_query = per_query
        self.queries = 0

    def __call__(self) -> float:
        return self.queries * self.per_query


def test_budget_stops_the_run_and_keeps_what_it_found():
    spend = _CountingSpend(per_query=1.0)
    executor = _FakeExecutor()

    def execute(sql: str):  # noqa: ARG001 - the SQL does not matter, the count does
        spend.queries += 1
        return ExecResult(rows=[{"n": 1}])

    executor.execute = execute
    counts: dict = {}
    got = mine_candidates(_shapes(20), executor, min_tables=1,
                          budget=3.0, spent=spend, counts_out=counts)
    # Stopped at the ceiling, not at the end of the list.
    assert len(got) < 20
    assert counts["budget_stopped"]["budget"] == 3.0
    assert counts["budget_stopped"]["usd"] >= 3.0
    assert counts["budget_stopped"]["total"] == 20
    # And what it found before stopping is returned, not thrown away.
    assert got, "a budget stop must keep the candidates already gated"


def test_no_budget_means_no_ceiling():
    spend = _CountingSpend(per_query=100.0)
    executor = _FakeExecutor()
    counts: dict = {}
    got = mine_candidates(_shapes(6), executor, min_tables=1,
                          spent=spend, counts_out=counts)
    assert len(got) == 6
    assert "budget_stopped" not in counts


def test_a_budget_with_no_spend_reading_cannot_stop_anything():
    """`--budget` alone is inert: without a spend reading there is nothing to
    compare. Pinned so a caller that forgets `spent` fails loudly in review
    rather than silently running to the end of a paid list."""
    counts: dict = {}
    got = mine_candidates(_shapes(5), _FakeExecutor(), min_tables=1,
                          budget=0.01, counts_out=counts)
    assert len(got) == 5
    assert "budget_stopped" not in counts


def test_candidates_are_written_as_they_are_found(tmp_path):
    """The file is readable before the run finishes, which is the whole point:
    a stage that produces output only on success cannot be interrupted."""
    seen_at = []
    out = tmp_path / "live.csv"

    from context_lift.mine import CANDIDATE_COLUMNS, candidate_row
    handle = out.open("w", newline="")
    writer = csv.writer(handle)
    writer.writerow(CANDIDATE_COLUMNS)

    def append(candidate):
        writer.writerow(candidate_row(candidate))
        handle.flush()
        # read the file back mid-run, from the outside
        seen_at.append(len(out.read_text().strip().splitlines()))

    mine_candidates(_shapes(4), _FakeExecutor(), min_tables=1,
                    on_candidate=append)
    handle.close()
    # header plus one row after the first candidate, growing by one each time
    assert seen_at == [2, 3, 4, 5]


def test_the_live_file_and_the_final_file_have_the_same_columns(tmp_path):
    """Two write paths, one row builder. Two column lists that agree today
    would not agree after the next column is added to one of them."""
    from context_lift.mine import CANDIDATE_COLUMNS
    got = mine_candidates(_shapes(3), _FakeExecutor(), min_tables=1)
    bulk = tmp_path / "bulk.csv"
    write_candidates(got, bulk)
    with bulk.open(newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == CANDIDATE_COLUMNS
    assert len(rows) == 4


def test_a_budget_stop_is_the_first_thing_the_summary_says(tmp_path):
    """Because it changes how every count below it reads: "22 clean" after a
    stop means 22 of the shapes it reached, not 22 of the log."""
    got = mine_candidates(_shapes(3), _FakeExecutor(), min_tables=1)
    text = summarize(got, counts={"budget_stopped": {
        "done": 120, "total": 500, "usd": 25.4, "budget": 25.0}})
    first = text.splitlines()[0]
    assert first.startswith("STOPPED ON BUDGET after 120 of 500 shapes")
    assert "$25.40" in first and "$25.00" in first
    # and it says what to do about it
    assert "--max-bytes" in text and "--budget" in text


# --- a migration check is not a business question ----------------------------

def test_a_backup_comparison_is_not_a_business_question():
    """The class that survived every filter before it.

    `--min-tables 2 --require-analytical` removed the dashboards. What was
    left still included engineering comparing one copy of the data to
    another: 32 of 89 usable candidates on one estate (36%), and 65 of 344
    mined shapes (19%). They join, they aggregate, no tool wrote a header on
    them, and no written business rule changes the answer to "compare this
    table to its backup and say pass or fail".
    """
    from context_lift.mine import compares_two_copies
    assert compares_two_copies(
        "SELECT 'src.t' AS source_table, src.row_count AS src_row_count, "
        "bkp.row_count AS bkp_row_count, "
        "CASE WHEN src.row_count = bkp.row_count THEN 'PASS' ELSE 'FAIL' END "
        "AS pass_fail FROM t_backup bkp JOIN t src ON 1=1")
    assert compares_two_copies(
        "SELECT region, old.row_count AS old_row_count, "
        "new.row_count AS new_row_count, "
        "old.row_count - new.row_count AS count_diff "
        "FROM a_old old JOIN a_new1 new USING (region)")


def test_one_signal_is_not_enough():
    """Two signals, not one, and this is the case that decides it.

    In a warehouse where staging is where the data lives, a single `stg_`
    table name is everywhere. On the estate this was written from, a gold
    asking for size-code counts by planning group reads a staging table and
    is a perfectly good business question. Dropping every query that touches
    one would throw away real work.
    """
    from context_lift.mine import compares_two_copies
    assert not compares_two_copies(
        "SELECT size_code, count(distinct product) FROM stg_gbl_sizing "
        "WHERE planning_group = 'WALMART CANADA' GROUP BY 1")
    assert not compares_two_copies(
        "SELECT region, SUM(net_sell_out_units) FROM fct_gbl_pos p "
        "JOIN dim_gbl_store s USING (store_key) GROUP BY 1")


def test_it_never_throws_away_a_real_question():
    """Scored against 40 rows a person judged by hand on a real estate:
    9 caught, **0 wrongly flagged**, 13 missed, 18 correctly left alone.

    100% precision and 41% recall, and that is the right trade deliberately.
    A filter that drops a good gold costs a question nobody gets back; one
    that misses a bad gold costs a person ten seconds. The 13 it misses are
    cross-SYSTEM reconciliations -- "orders not found in Mercury", "Webi
    versus BigQuery" -- which need knowing which names are systems, and that
    does not generalise. So this halves the reviewer's work on the class
    rather than removing it.
    """
    from context_lift.mine import compares_two_copies
    # the shapes it is allowed to miss, pinned so nobody widens the pattern
    # into them and starts dropping real questions
    for sql in (
        "SELECT dt, COUNT(*) FROM orders o LEFT JOIN mercury m USING (id) "
        "WHERE m.id IS NULL GROUP BY 1",                    # anti-join
        "SELECT COUNT(DISTINCT product) FROM a EXCEPT DISTINCT "
        "SELECT COUNT(DISTINCT product) FROM b",            # set difference
    ):
        assert not compares_two_copies(sql), (
            "an anti-join is also how 'products never sold' is written, so "
            "it must not be a signal on its own")


# --------------------------------------------------------------------------
# `mine` refuses to guess its scope. Both settings that turned an unprovable
# gold set into a provable one were opt-in and neither was prompted, and the
# cost of that is measured in docs/research/README.md practices 1 and 2.
# --------------------------------------------------------------------------

def _empty_log(tmp_path):
    """A sqlite db and an empty log, so no cloud extra is needed.

    These tests use sqlite rather than `bq://x` on purpose: `make test` stays
    cloud-free, and `--db bq://...` raises SystemExit when the [bigquery]
    extra is absent. That passed on a dev machine and failed CI.

    Every caller passes `--out` under tmp_path as well. `mine --out` defaults
    to `candidates.csv`, so a test that omits it writes into whatever
    directory pytest was started from; this one left a stray file in the repo
    root three times in one afternoon.
    """
    import sqlite3
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE orders(id INT)")
    con.commit()
    con.close()
    log = tmp_path / "log.csv"
    log.write_text("sql,times_seen,ran_by,distinct_users\n")
    return str(db), str(log)


def test_mine_refuses_without_a_named_scope(tmp_path, capsys):
    """No --datasets, no run. Exit 2 before a single cloud call."""
    from context_lift.cli import main
    db, log = _empty_log(tmp_path)
    code = main(["mine", "--db", db, "--queries", log,
                 "--out", str(tmp_path / "candidates.csv")])
    assert code == 2
    err = capsys.readouterr().err
    assert "will not guess it" in err
    assert "--all-datasets" in err
    # The refusal has to carry the free commands, or it just blocks people.
    assert "doc_coverage.py" in err
    assert "estate_shape.py" in err


def test_all_datasets_is_the_deliberate_way_past_it(tmp_path, capsys):
    """Mining the whole log stays possible, and reads as a choice."""
    from context_lift.cli import main
    db, log = _empty_log(tmp_path)
    main(["mine", "--db", db, "--queries", log, "--all-datasets",
          "--out", str(tmp_path / "candidates.csv")])
    assert "will not guess it" not in capsys.readouterr().err


def test_several_datasets_with_the_spread_on_says_so(tmp_path, capsys):
    """Naming the datasets is half of it; round-robin starves them in turn."""
    from context_lift.cli import main
    db, log = _empty_log(tmp_path)
    main(["mine", "--db", db, "--queries", log, "--datasets", "a,b,c",
          "--out", str(tmp_path / "candidates.csv")])
    err = capsys.readouterr().err
    assert "round-robin spread" in err
    assert "--no-spread" in err


def test_one_dataset_with_the_spread_on_says_nothing(tmp_path, capsys):
    """Nothing to round-robin across, so the note would be noise."""
    from context_lift.cli import main
    db, log = _empty_log(tmp_path)
    main(["mine", "--db", db, "--queries", log, "--datasets", "a",
          "--out", str(tmp_path / "candidates.csv")])
    assert "round-robin spread" not in capsys.readouterr().err
