# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Five bugs in the gate's literal checks that threw away a gold set.

Found 2026-09-12 on a real estate. 62 mined candidates were refused by
MISSING_PARAMETER alone; 39 of them were our own bugs and are recovered by
these fixes. Every case below is a real row, quoted.

The reason each one needs a test rather than a comment: all five are silent.
A refused candidate leaves no trace except a count, and the count looked
plausible for five paid rounds.
"""
from __future__ import annotations

from context_lift.gate import (_PARAMETER_COLUMN, _blank_string_values,
                           dates_in_words, filter_literals, missing_literals,
                           parameter_literals, question_states_date,
                           sentinel_date)


class TestProseDates:
    """The question says April 1, the SQL says 2026-04-01. 83 of 100 flags."""

    def test_the_m011_case_that_refused_39_candidates(self):
        question = ("How many records are there for each sales organization "
                    "code and AMA company code, for sales organization codes "
                    "starting with 'PH' in the AMA region, from April 1, 2026 "
                    "onwards")
        sql = "SELECT 1 FROM t WHERE fct.transaction_date >= '2026-04-01'"
        assert question_states_date("2026-04-01", question)
        assert "2026-04-01" not in missing_literals(sql, question)

    def test_every_spelling_a_person_actually_writes(self):
        for text in ("from April 1, 2026 onwards", "from April 1 2026",
                     "since Apr 1, 2026", "on 1 April 2026",
                     "from 1st of April 2026", "since 2026-04-01"):
            assert question_states_date("2026-04-01", text), text

    def test_a_month_with_no_day_counts_only_for_the_first(self):
        """"since April 2026" states 2026-04-01. It does not state the 15th."""
        assert question_states_date("2026-04-01", "since April 2026")
        assert not question_states_date("2026-04-15", "since April 2026")

    def test_a_date_the_question_really_omits_is_still_reported(self):
        """m016: the question says January, the answer key says June.

        An agent that answers the question correctly scores wrong against
        this key, so the gold is worse than unanswerable and must stay
        refused.
        """
        question = ("For web orders from 2026-01-01 onwards, what is the "
                    "daily count of orders not found in Mercury")
        assert question_states_date("2026-01-01", question)
        assert not question_states_date("2026-06-01", question)

    def test_dates_in_words_separates_days_from_months(self):
        days, months = dates_in_words("between March 2, 2026 and April 2026")
        assert "2026-03-02" in days
        assert "2026-04" in months


class TestSentinelDates:
    """An enumerated list of "no bound" years could never hold."""

    def test_the_four_this_estate_uses_and_the_list_missed(self):
        for value in ("1000-01-01", "1001-01-01", "2099-12-31", "9000-12-31"):
            assert sentinel_date(value), value

    def test_the_ones_the_old_list_did_catch_still_pass(self):
        for value in ("1900-01-01", "9999-12-31", "0001-01-01", "19000101"):
            assert sentinel_date(value), value

    def test_a_real_business_date_is_not_a_sentinel(self):
        for value in ("2026-04-01", "2025-12-01", "20260401"):
            assert not sentinel_date(value), value

    def test_a_value_that_is_not_a_date_at_all(self):
        for value in ("US", "202630", "", "PH"):
            assert not sentinel_date(value)


class TestDoubleQuotedLiterals:
    """BigQuery takes both quote styles; the gate read one."""

    def test_a_bare_20_scraped_out_of_a_double_quoted_date(self):
        """m722. The literal was never masked, so its digits became a value."""
        sql = 'SELECT 1 FROM t WHERE transaction_date = "2026-06-20"'
        assert "20" not in filter_literals(sql)

    def test_blanking_keeps_every_character_in_place(self):
        """Offsets have to survive, because spans read back out of the text."""
        text = 'WHERE a = "2026-06-20" AND b = \'US\''
        assert len(_blank_string_values(text)) == len(text)
        assert '"__________"' in _blank_string_values(text)
        assert "'__'" in _blank_string_values(text)


class TestParameterColumnNarrowing:
    """`to` alone is English, not a date word."""

    def test_the_m114_case_a_ship_to_country_code(self):
        for column in ("ship_to_country", "sold_to_party", "bill_to_code"):
            assert not _PARAMETER_COLUMN.search(column), column

    def test_a_bound_word_beside_a_time_word_still_matches(self):
        for column in ("from_date", "date_to", "start_dt", "end_month"):
            assert _PARAMETER_COLUMN.search(column), column

    def test_plain_time_and_id_columns_are_untouched(self):
        for column in ("transaction_date", "fiscal_week", "order_id",
                       "snapshot_ts"):
            assert _PARAMETER_COLUMN.search(column), column

    def test_a_country_code_is_a_rule_not_a_parameter(self):
        """The whole point: a business code is what written context supplies."""
        sql = "SELECT 1 FROM t WHERE ship_to_country = 'US'"
        assert parameter_literals(sql, ["US"]) == []
