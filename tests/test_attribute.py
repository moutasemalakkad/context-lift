# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Behavior tests for context-rule attribution.

The scenario throughout is the one the module exists for: a glossary rule
redirects the model to a different table, the question flips, and the report
must name that rule — and must NOT name a rule for a flip it cannot explain.
"""
from context_lift.ablate import ablate
from context_lift.attribute import (_rule_tokens, attribute,
                                count_rule_lines_without_identifier, parse_rules, sql_tokens)
from context_lift.harness import EvalReport, QuestionResult
from context_lift.metrics import proportion


def _report(outcomes: list[tuple[str, bool]]) -> EvalReport:
    results = [
        QuestionResult(
            id=qid,
            question=f"question {qid}",
            verdict="MATCH" if correct else "MISMATCH",
            execution_correct=correct,
            adjusted_correct=correct,
            judged=False,
            judge_verdict="",
            detail="",
        )
        for qid, correct in outcomes
    ]
    k = sum(1 for _, c in outcomes if c)
    rate = proportion(k, len(outcomes))
    return EvalReport(
        results=results,
        n_total=len(outcomes),
        n_scored=len(outcomes),
        n_excluded=0,
        execution=rate,
        adjusted=rate,
    )


GLOSSARY = """\
The warehouse uses opaque table names.

- "department name" -> SIS_DEPARTMENT.DEPARTMENT_NAME
- "room area" -> FCLT_ROOMS.AREA, FAC_ROOMS.AREA
- "building name" -> BUILDINGS.BUILDING_NAME
"""


def test_parse_rules_reads_glossary_lines_and_skips_prose():
    rules = parse_rules(GLOSSARY)
    assert [r.phrase for r in rules] == ["department name", "room area", "building name"]
    assert rules[0].identifiers == ("SIS_DEPARTMENT.DEPARTMENT_NAME",)
    assert rules[1].identifiers == ("FCLT_ROOMS.AREA", "FAC_ROOMS.AREA")
    # The rules are numbered from the first rule, not from its line number;
    # line_no still points at the literal line to edit.
    assert [r.id for r in rules] == ["R001", "R002", "R003"]
    assert rules[0].line_no == 3


def test_a_rule_that_redirected_the_sql_is_named_for_the_break():
    # q1 was right joining HR_ORG_UNIT; the glossary pulled it to
    # SIS_DEPARTMENT and it broke. The department rule must be the suspect,
    # and the building rule must not appear.
    without = _report([("q1", True), ("q2", True)])
    with_ = _report([("q1", False), ("q2", True)])
    ablation = ablate(without, with_)
    report = attribute(
        ablation,
        sql_without={"q1": "SELECT d.NAME FROM HR_ORG_UNIT d", "q2": "SELECT 1"},
        sql_with={"q1": "SELECT d.DEPARTMENT_NAME FROM SIS_DEPARTMENT d", "q2": "SELECT 1"},
        rules=parse_rules(GLOSSARY),
    )
    assert len(report.impacts) == 1
    impact = report.impacts[0]
    assert impact.rule.phrase == "department name"
    assert impact.broke == (("q1", 1),)
    assert "SIS_DEPARTMENT" in impact.introduced
    assert report.unattributed_broke == ()


def test_a_fix_credits_the_rule_the_same_way():
    without = _report([("q1", False)])
    with_ = _report([("q1", True)])
    report = attribute(
        ablate(without, with_),
        sql_without={"q1": "SELECT b.NAME FROM BLDG b"},
        sql_with={"q1": "SELECT b.BUILDING_NAME FROM BUILDINGS b"},
        rules=parse_rules(GLOSSARY),
    )
    assert report.impacts[0].rule.phrase == "building name"
    assert report.impacts[0].fixed == (("q1", 1),)
    assert report.impacts[0].broke == ()


def test_a_flip_no_rule_explains_stays_unattributed():
    # The SQL change is a join-order rewrite using the same identifiers, so no
    # rule matches. Forcing it onto the nearest rule would send the curator to
    # edit the wrong line.
    without = _report([("q1", True)])
    with_ = _report([("q1", False)])
    report = attribute(
        ablate(without, with_),
        sql_without={"q1": "SELECT a.X FROM T1 a JOIN T2 b ON a.K = b.K"},
        sql_with={"q1": "SELECT a.X FROM T2 b JOIN T1 a ON b.K = a.K"},
        rules=parse_rules(GLOSSARY),
    )
    assert report.impacts == ()
    assert report.unattributed_broke == (("q1", 1),)
    assert "unattributed broke: q1" in report.summary()


def test_pooled_reps_collapse_and_count_in_the_report():
    # The same break in both reps must read "q1 x2", evidence of a consistent
    # effect rather than noise.
    from context_lift.ablate import pool

    without = pool([_report([("q1", True)]), _report([("q1", True)])])
    with_ = pool([_report([("q1", False)]), _report([("q1", False)])])
    sql_a = {"q1#rep0": "SELECT NAME FROM HR_ORG_UNIT",
             "q1#rep1": "SELECT NAME FROM HR_ORG_UNIT"}
    sql_b = {"q1#rep0": "SELECT DEPARTMENT_NAME FROM SIS_DEPARTMENT",
             "q1#rep1": "SELECT DEPARTMENT_NAME FROM SIS_DEPARTMENT"}
    report = attribute(ablate(without, with_), sql_a, sql_b, parse_rules(GLOSSARY))
    assert report.impacts[0].broke == (("q1", 2),)
    assert "q1 x2" in report.summary()


def test_summary_names_the_leave_one_out_next_step_only_when_something_broke():
    without = _report([("q1", True)])
    with_ = _report([("q1", False)])
    report = attribute(
        ablate(without, with_),
        {"q1": "SELECT NAME FROM HR_ORG_UNIT"},
        {"q1": "SELECT DEPARTMENT_NAME FROM SIS_DEPARTMENT"},
        parse_rules(GLOSSARY),
    )
    assert "leave-one-out" in report.summary()

    clean = attribute(
        ablate(_report([("q1", True)]), _report([("q1", True)])),
        {"q1": "SELECT 1"}, {"q1": "SELECT 1"}, parse_rules(GLOSSARY),
    )
    assert "leave-one-out" not in clean.summary()


def test_summary_counts_the_rule_lines_that_name_no_table_column():
    context = ('- "department name" -> SIS_DEPARTMENT.DEPARTMENT_NAME\n'
               '- "unrestricted stock" -> unrestricted_stock\n'
               "- every query filters status = 'active'\n")
    rules = parse_rules(context)
    assert [r.phrase for r in rules] == ["department name"]
    assert count_rule_lines_without_identifier(context) == 2

    report = attribute(
        ablate(_report([("q1", True)]), _report([("q1", True)])),
        {"q1": "SELECT 1"}, {"q1": "SELECT 1"}, rules,
        n_rule_lines_without_identifier=2,
    )

    assert report.summary().splitlines()[0] == (
        "attribution: 0 broke / 0 fixed, 1 rules in the context "
        "(2 more rule lines carry no table.column and cannot be attributed)")


def test_sql_tokens_are_case_insensitive():
    assert "FCLT_ROOMS" in sql_tokens("select area from fclt_rooms")


def test_a_rule_is_blamed_when_its_phrase_becomes_a_literal_on_its_column():
    from context_lift.ablate import AblationReport
    from context_lift.attribute import attribute, parse_rules
    from context_lift.metrics import McNemarResult, proportion
    rules = parse_rules('- "under construction" -> plants.Status # from q017\n')
    ablation = AblationReport(
        without_context=proportion(1, 1), with_context=proportion(0, 1), lift=-1.0,
        mcnemar=McNemarResult(1, 0, 1.0), n_paired=1, adjusted=False,
        broke_ids=("q026#rep0",), fixed_ids=())
    without = {"q026#rep0": "SELECT Country FROM plants WHERE Status = 'Under Construction'"}
    with_ = {"q026#rep0": "SELECT Country FROM plants WHERE Status = 'under construction'"}

    report = attribute(ablation, without, with_, rules)

    assert report.unattributed_broke == ()
    impact = report.impacts[0]
    assert impact.rule.id == "R001" and impact.broke == (("q026", 1),)
    assert set(impact.signals) == {"phrase_in_literal", "literal_changed_on_rule_column"}


def test_a_hyphenated_project_id_is_read_as_one_name():
    """A BigQuery project id may carry hyphens. Reading the first segment
    from the last hyphen on turned `acme-analytics-p.sales.fct_orders` into
    `p.sales`, so the rule was tied to the token `P` that every alias
    matches, and 28 of 29 rules on one estate got the same credit."""
    rules = parse_rules('- "orders" -> `acme-analytics-p.sales.fct_orders`\n')

    assert [rule.identifiers for rule in rules] == [("sales.fct_orders",)]
    assert "P" not in _rule_tokens(rules[0])
    assert _rule_tokens(rules[0]) == {"SALES", "FCT_ORDERS"}


def test_an_unattributable_context_says_so_once_not_per_flip():
    """28 flips were each reported as a possible join-path effect.

    Measured 2026-09-13 on the committed thelook round: 24 fixed, 4 broke,
    McNemar p = 0.0002, and 0 of 15 rule lines attributable because not one
    names a table or a column. The old output listed all 28 with "likely a
    join-path or phrasing effect", which sends a reader to investigate 28
    questions when the finding is one sentence about the context.
    """
    from context_lift.attribute import AttributionReport

    report = AttributionReport(
        impacts=(),
        unattributed_broke=(("tl004", 1), ("tl017", 1)),
        unattributed_fixed=(("ta001", 1), ("ta002", 1)),
        n_rules=0, n_broke=2, n_fixed=2,
        n_rule_lines_without_identifier=15,
    )
    text = report.summary()

    assert "NOTHING IN THIS CONTEXT IS ATTRIBUTABLE" in text
    assert "15 rule line(s)" in text
    # The per-flip guess must NOT appear: it points at the wrong thing.
    assert "join-path" not in text
    # And it must say what to do instead, since the rules may still work.
    assert "classes" in text
    assert "name the table and column" in text


def test_a_flip_no_rule_explains_still_gets_the_join_path_hint():
    """With attributable rules present, an unmatched flip really might be a
    join-path effect, and that hint is the useful one."""
    from context_lift.attribute import AttributionReport, Rule, RuleImpact

    rule = Rule(id="R001", phrase="revenue", identifiers=("t.sale_price",),
                line_no=1, text='- "revenue" -> t.sale_price')
    report = AttributionReport(
        impacts=(RuleImpact(rule=rule, broke=(), fixed=(("ta009", 1),),
                            introduced=("SALE_PRICE",),
                            signals=("introduced_identifier",)),),
        unattributed_broke=(("tl004", 1),),
        unattributed_fixed=(),
        n_rules=1, n_broke=1, n_fixed=1,
        n_rule_lines_without_identifier=0,
    )
    text = report.summary()

    assert "NOTHING IN THIS CONTEXT IS ATTRIBUTABLE" not in text
    assert "join-path" in text
