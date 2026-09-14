# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The rules draft: cited rules kept, invented ones dropped, nothing installed."""
from context_lift.rules import build_rules_prompt, parse_rules, render_draft


def test_a_citation_is_a_position_in_the_prompt_and_comes_back_as_the_question_id():
    text = ("Rules:\n- race number refers to raceId (q1, q3)\n"
            "- best lap time means the minimum of q2 (q3)\n"
            "- always use INNER JOIN\n"
            "- something the prompt never showed (q9)\n")

    draft = parse_rules(text, ["bird_847", "bird_12", "bird_90"])

    assert draft.rules == [("- race number refers to raceId", ["bird_847", "bird_90"]),
                           ("- best lap time means the minimum of q2", ["bird_90"])]
    assert draft.rule_lines == 4
    assert draft.dropped_citations == 1      # q9: only three questions were shown


def test_a_reply_that_cites_past_the_last_question_drafts_nothing_and_counts_what_it_dropped():
    """The paid run that drafted 0 rules: the model cited the example ids in
    the prompt, and no count said so."""
    text = ('- "lap time" -> qualifying.q1, lapTimes.time (q2, q7)\n'
            '- "fastest lap" -> lapTimes.time (q12)\n')

    draft = parse_rules(text, ["bird_847"])

    assert draft.rules == []
    assert draft.rule_lines == 2
    assert draft.dropped_citations == 3


def test_every_shape_a_model_writes_for_a_rule_becomes_the_one_arrow_shape():
    text = ('- "Unrestricted Stock" refers to `unrestricted_stock`. (q1)\n'
            '- "net revenue" means orders.amount minus orders.refund_amount (q1)\n'
            '- "active customer" is a customer with an order in the last 90 days (q1)\n'
            '- Region: `sales.region_code`, never the free-text region name (q1)\n'
            '- "unit price" maps to order_items.price (q1)\n'
            '- order date -> orders.created_at (q1).\n')

    rules = parse_rules(text, ["q1"]).rules

    assert [rule for rule, _cited in rules] == [
        '- "Unrestricted Stock" -> unrestricted_stock',
        '- "net revenue" -> orders.amount minus orders.refund_amount',
        '- "active customer" -> a customer with an order in the last 90 days',
        '- "Region" -> sales.region_code, never the free-text region name',
        '- "unit price" -> order_items.price',
        '- "order date" -> orders.created_at',
    ]
    assert all(cited == ["q1"] for _rule, cited in rules)


def test_a_label_in_front_of_a_sentence_is_not_the_term_the_rule_defines():
    text = ('- Note: the fiscal year starts in February, use dates.fiscal_year (q1)\n'
            '- Warning: revenue excludes tax (q1)\n'
            '- "time: local": every timestamp is in the local time of the shop (q1)\n')

    rules = parse_rules(text, ["q1"]).rules

    assert [rule for rule, _cited in rules] == [
        "- Note: the fiscal year starts in February, use dates.fiscal_year",
        "- Warning: revenue excludes tax",
        '- "time: local" -> every timestamp is in the local time of the shop',
    ]


def test_a_sentence_rule_stays_as_written_and_keeps_its_citation():
    text = "- every query filters orders.status = 'complete' unless asked otherwise (q1, q2)\n"

    rules = parse_rules(text, ["dw036", "dw112"]).rules

    assert rules == [("- every query filters orders.status = 'complete' unless asked otherwise",
                      ["dw036", "dw112"])]


def test_the_prompt_asks_for_the_arrow_shape_with_one_example_of_each_kind():
    prompt = build_rules_prompt([], "WRONG_VALUES", "right shape, wrong numbers")

    assert '- "phrase" -> meaning (q1, q7)' in prompt
    assert '- "net revenue" -> orders.amount minus orders.refund_amount (q2, q5)' in prompt
    assert "- \"orders\" -> only rows with orders.status = 'complete'" in prompt


def test_the_prompt_shows_gold_beside_prediction_and_names_the_bucket():
    prompt = build_rules_prompt([{"question": "who?", "gold_sql": "SELECT a",
                                  "pred_sql": "SELECT b", "detail": "different values"},
                                 {"question": "when?", "gold_sql": "SELECT c",
                                  "pred_sql": "SELECT d", "detail": "different values"}],
                                "WRONG_VALUES", "right shape, wrong numbers", max_rules=3)

    assert "WRONG_VALUES: right shape, wrong numbers" in prompt
    assert "gold SQL: SELECT a" in prompt and "agent SQL: SELECT b" in prompt
    assert "at most 3 rules" in prompt
    # the questions are headed by their position, the same names the two
    # example lines cite; heading them with their own ids drafted 0 rules
    assert "### q1\nquestion: who?" in prompt and "### q2\nquestion: when?" in prompt


def test_the_draft_says_it_is_a_draft_and_keeps_the_citations_as_comments():
    draft = render_draft([("- race number refers to raceId", ["q1"])], "WRONG_VALUES", 5)

    assert draft.splitlines()[0].startswith("<!-- drafted by contextlift draft-rules from bucket WRONG_VALUES, 5 questions")
    assert "- race number refers to raceId   <!-- q1 -->" in draft


def test_a_rule_that_restates_one_questions_own_values_is_marked():
    """Two cold runs produced lines like

        - "RCCORE 104" -> COURSE.DEPARTMENT = "RCCORE" AND COURSE.NUMBER = 104

    which is one question's answer written into the context. `attribute` then
    credits it with fixing that question, so it reads as a win. It raises the
    working-half reading and does nothing to the reserved one, which is what
    overfitting is. The prompt already asks for general rules; an instruction
    is advice and this is the check."""
    golds = {"adv035": "SELECT x FROM c WHERE DEPARTMENT = 'RCCORE' AND NUMBER = 104",
             "adv077": "SELECT y FROM p WHERE workload = 1"}
    reply = ('- "RCCORE 104" -> COURSE.DEPARTMENT = "RCCORE" AND COURSE.NUMBER = 104 (q1)\n'
             '- "easiest" -> the lowest PROGRAM_COURSE.WORKLOAD (q2)\n')

    draft = parse_rules(reply, ["adv035", "adv077"], golds)

    assert len(draft.rules) == 2                      # neither is dropped
    assert draft.memorised == [
        '- "RCCORE 104" -> COURSE.DEPARTMENT = "RCCORE" AND COURSE.NUMBER = 104']


def test_a_rule_two_questions_share_is_not_marked():
    """A rule cited by more than one question generalises by definition, even
    when it names a value."""
    golds = {"a": "SELECT x FROM t WHERE status = 'complete'",
             "b": "SELECT y FROM u WHERE status = 'complete'"}
    reply = '- "orders" -> only rows with status = \'complete\' (q1, q2)\n'

    assert parse_rules(reply, ["a", "b"], golds).memorised == []


def test_a_one_question_rule_that_names_no_value_is_not_marked():
    """A rule only one question needs is sometimes the right rule. What makes
    it suspect is restating that question's own filter values."""
    golds = {"a": "SELECT x FROM t WHERE status = 'complete'"}
    reply = '- "revenue" -> orders.amount minus orders.refund_amount (q1)\n'

    assert parse_rules(reply, ["a"], golds).memorised == []


def test_the_check_is_off_when_the_caller_has_no_gold_sql():
    """Every existing caller passes two arguments and must behave as before."""
    reply = '- "x" -> y (q1)\n'

    assert parse_rules(reply, ["a"]).memorised == []
