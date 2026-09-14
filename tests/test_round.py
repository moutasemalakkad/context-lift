# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""One round: two arms, the same questions, one verdict."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from context_lift.executor import ExecResult
from context_lift.round import Gold, free_gates, run
from context_lift.runners import Arm, Example


@dataclass
class Answer:
    pred_sql: str | None


def golds(n: int) -> list[Gold]:
    """`n` questions whose answers differ, so a wrong answer is detectable."""
    return [Gold(f"q{i:03d}", f"question {i}", f"SELECT {i} AS n") for i in range(n)]


class Rows:
    """Runs the SQL as arithmetic: `SELECT 3 AS n` returns 3.

    Enough to make a comparison real without a warehouse: a prediction that
    picks the wrong number is genuinely wrong, and one that picks the right
    number is genuinely right.
    """

    def execute(self, sql: str) -> ExecResult:
        if not sql.strip():
            return ExecResult(error="empty prediction")
        try:
            value = int(sql.split()[1])
        except (IndexError, ValueError):
            return ExecResult(error=f"cannot run {sql!r}")
        return ExecResult(rows=[{"n": value}])


def arms(instruction: str = "Revenue excludes returns.",
         examples: list[Example] | None = None) -> tuple[Arm, Arm]:
    control = Arm(label="0d", tables=["p.d.t"])
    treatment = Arm(label="1", tables=["p.d.t"], system_instruction=instruction,
                    examples=list(examples or []))
    return control, treatment


def answer_all_right(question_id, question, arm):
    return Answer(f"SELECT {question.split()[-1]} AS n")


def answer_right_only_with_context(question_id, question, arm):
    """The shape a working context has: the control gets it wrong, the
    treatment gets it right."""
    if arm.system_instruction:
        return Answer(f"SELECT {question.split()[-1]} AS n")
    return Answer("SELECT 999 AS n")


def test_a_context_that_fixes_everything_on_a_big_enough_set_is_a_real_gain():
    control, treatment = arms()

    result = run(golds(60), control, treatment, answer_right_only_with_context,
                 Rows(), holdout_fraction=0.3,
                 force_underpowered=True)

    assert result.verdict.word == "REAL GAIN"
    assert result.holdout.mcnemar.c > 0 and result.holdout.mcnemar.b == 0


def test_a_context_that_changes_nothing_on_a_big_enough_set_says_so():
    """Big enough matters. 60 golds leave about 18 held out, and 18 questions
    cannot see a 10-point lift, so that set reads UNDERPOWERED however it went.
    200 golds leave about 60, which can."""
    control, treatment = arms()

    result = run(golds(200), control, treatment, answer_all_right, Rows(),
                 target_pp=10.0,
                 force_underpowered=True)

    assert result.verdict.word == "NO CHANGE"


def test_a_small_set_reads_unresolved_even_when_nothing_moved():
    """The first live round, 2026-09-09: six held-out questions flipped
    nothing and an earlier draft called that "the context changed nothing"."""
    control, treatment = arms()

    result = run(golds(26), control, treatment, answer_all_right, Rows(),
                 target_pp=10.0,
                 force_underpowered=True)

    assert result.verdict.word == "UNDERPOWERED"
    assert result.verdict.questions_needed == 60


def test_the_split_is_disjoint_complete_and_the_same_every_time():
    """The share asked for is not the share you get on a small set.

    `split_golds` hashes each id so that adding questions never moves an
    existing one across the line. The price is that the realised fraction
    wanders when there are few ids: asking for 30% gave 6 of 26 on the real
    thelook ids and 13 of 26 on these synthetic ones. What is guaranteed is
    what this test asserts, and the fraction is not among it.
    """
    control, treatment = arms()

    first = run(golds(26), control, treatment, answer_all_right, Rows(),
                 force_underpowered=True)
    second = run(golds(26), control, treatment, answer_all_right, Rows(),
                 force_underpowered=True)

    assert set(first.curate_ids).isdisjoint(first.holdout_ids)
    assert len(first.curate_ids) + len(first.holdout_ids) == 26
    assert first.holdout_ids == second.holdout_ids
    assert first.holdout_ids, "something must be held back"


def test_the_gates_run_before_a_single_question_is_asked():
    """A round that was going to be INVALID should cost nothing. A leak gate
    that fires after every question has told you the same thing and charged
    you for it."""
    asked = []

    def counting_ask(question_id, question, arm):
        asked.append(question)
        return Answer("SELECT 1 AS n")

    control, treatment = arms(
        instruction="\n".join(["- SELECT count(*) FROM t"] * 9 + ["- a meaning"]))

    result = run(golds(60), control, treatment, counting_ask, Rows(),
                 force_underpowered=True)

    assert result.verdict.word == "INVALID"
    assert asked == [], "no model call may happen once a gate has failed"
    assert result.calls == 0


def test_a_scored_question_inside_an_example_is_caught_before_the_run():
    """The leak gate reads the rules, the terms and the examples, because all
    three travel to the model."""
    control, treatment = arms(examples=[Example(question="question 3",
                                                sql="SELECT 3 AS n")])

    gates = free_gates(golds(10), treatment)

    leak = next(g for g in gates if g.name == "leak")
    assert not leak.passed


def test_an_example_with_a_scored_question_s_query_shape_is_caught():
    """Not the same question, the same query once literals are masked."""
    control, treatment = arms(examples=[Example(question="something else",
                                                sql="SELECT 41 AS n")])

    twin = next(g for g in free_gates(golds(10), treatment)
                if g.name == "template twin")

    assert not twin.passed


def test_the_curate_and_holdout_sets_are_scored_apart_not_pooled():
    """The whole point of the split is that one set was allowed to be looked
    at while the context was written and the other was not."""
    control, treatment = arms()

    result = run(golds(60), control, treatment, answer_right_only_with_context,
                 Rows(),
                 force_underpowered=True)

    assert result.curate is not None and result.holdout is not None
    assert set(result.curate_ids).isdisjoint(result.holdout_ids)
    assert len(result.curate_ids) + len(result.holdout_ids) == 60


def test_repeats_are_asked_and_counted():
    control, treatment = arms()

    result = run(golds(60), control, treatment, answer_all_right, Rows(), k=3,
                 force_underpowered=True)

    assert result.k_repeats == 3
    assert result.calls == 60 * 2 * 3


def test_a_round_with_no_questions_refuses():
    control, treatment = arms()

    with pytest.raises(SystemExit) as refused:
        run([], control, treatment, answer_all_right, Rows(),
                 force_underpowered=True)

    assert "no verdict" in str(refused.value)


def test_a_second_holdout_read_in_one_round_is_invalid():
    """The second read is chosen knowing the first, which is what makes the
    number worth anything."""
    control, treatment = arms()

    result = run(golds(60), control, treatment, answer_all_right, Rows(),
                 holdout_reads_so_far=1,
                 force_underpowered=True)

    assert result.verdict.word == "INVALID"


def test_the_printed_lines_lead_with_the_verdict_and_label_the_curate_number():
    """The curate number is a fit on the working questions, never the verdict.
    A page that printed it as the headline is why the verdict order exists."""
    control, treatment = arms()

    lines = run(golds(60), control, treatment, answer_right_only_with_context,
                Rows(),
                 force_underpowered=True).lines()

    assert lines[0].startswith("verdict: ")
    assert any("fit on the working questions" in line for line in lines)


def answer_mostly_right(question_id, question, arm):
    """Right except on every fifth question, so something is left to triage.

    A fake where the treatment gets everything right has no failures, and a
    round with no failures has no lever to suggest.
    """
    number = int(question.split()[-1])
    if number % 5 == 0:
        return Answer("SELECT 999 AS n")
    return Answer(f"SELECT {number} AS n")


def test_the_round_says_what_to_write_next_from_the_working_questions():
    """A round that ends NOT PROVEN is the normal input to the next one, and
    the question a person actually has is "so what do I write".

    The shapes come from the curate set, not the holdout: reading the held-out
    questions to choose a lever is how a holdout becomes training data.
    """
    control, treatment = arms()

    result = run(golds(60), control, treatment, answer_mostly_right,
                 Rows(), target_pp=10.0,
                 force_underpowered=True)
    text = "\n".join(result.lines())

    assert result.failure_shapes, "the curate arm's failures were not triaged"
    assert "what to write next" in text
    assert "count, not a cause" in text


def test_a_round_with_nothing_failing_suggests_no_lever():
    control, treatment = arms()

    text = "\n".join(run(golds(60), control, treatment, answer_all_right,
                         Rows(),
                 force_underpowered=True).lines())

    assert "what to write next" not in text


def test_round_threads_the_extra_column_policy_into_both_scores():
    class ExtraRows:
        def execute(self, sql):
            value = int(sql.split()[1])
            row = {"n": value}
            if "extra" in sql:
                row["extra"] = 7
            return ExecResult(rows=[row])

    control, treatment = arms()

    def answer_with_extra(question_id, question, arm):
        return Answer(f"SELECT {question.split()[-1]} AS n, 7 AS extra")

    strict = run(golds(10), control, treatment, answer_with_extra, ExtraRows(),
                 force_underpowered=True)
    tolerant = run(golds(10), control, treatment, answer_with_extra, ExtraRows(),
                   extra_columns="tolerate", force_underpowered=True)

    assert strict.treatment_report.execution.k == 0
    assert tolerant.treatment_report.execution.k == len(tolerant.curate_ids)
    assert {r.verdict for r in tolerant.treatment_report.results} == {
        "MATCH_EXTRA_COLUMNS"}


def test_round_uses_the_callers_gold_cache():
    class CountingRows(Rows):
        def __init__(self):
            self.calls = 0

        def execute(self, sql):
            self.calls += 1
            return super().execute(sql)

    executor = CountingRows()
    cached = {g.gold_sql: ExecResult(rows=[{"n": int(g.gold_sql.split()[1])}])
              for g in golds(10)}
    control, treatment = arms()

    run(golds(10), control, treatment, answer_all_right, executor,
        gold_cache=cached, force_underpowered=True)

    assert executor.calls == 0


def test_a_curate_question_in_the_context_is_allowed_and_a_held_out_one_is_not():
    """The leak checks defend the held-out questions, not every gold.

    A curate question in the context is what curation means: `curate-examples`
    exists to turn working questions into worked examples. A held-out question
    in the context is the leak, because then its answer was handed over rather
    than written.

    Checking against every gold made the tool refuse its own workflow: five
    examples built from curate golds failed the template-twin gate against all
    31 golds and passed against the 8 held out.
    """
    from context_lift.leakage import split_golds
    from context_lift.round import free_gates

    # Distinct query SHAPES, not just distinct literals. `golds()` produces
    # SELECT 0 AS n, SELECT 1 AS n ... which all mask to the same shape, so
    # every one is a template twin of every other and the gate is right to
    # say so.
    shapes = ["SELECT COUNT(*) FROM t{i}", "SELECT AVG(x) FROM t{i}",
              "SELECT a, SUM(b) FROM t{i} GROUP BY a",
              "SELECT MAX(c) FROM t{i} JOIN u ON t{i}.k = u.k"]
    everything = [Gold(f"q{i:03d}", f"question {i}",
                       shapes[i % len(shapes)].format(i=i)) for i in range(60)]
    curate_ids, holdout_ids = split_golds([g.id for g in everything], 0.3, "contextlift")
    by_id = {g.id: g for g in everything}
    curate = [by_id[i] for i in curate_ids]
    holdout = [by_id[i] for i in holdout_ids]

    # An example copied from a curate question: allowed.
    from_curate = Example(question=curate[0].question, sql=curate[0].gold_sql)
    _control, treatment = arms(examples=[from_curate])
    twin = next(g for g in free_gates(everything, treatment, protect=holdout)
                if g.name == "template twin")
    assert twin.passed, "a worked example built from a working question is the point"

    # The same, copied from a held-out question: refused.
    from_holdout = Example(question=holdout[0].question, sql=holdout[0].gold_sql)
    _control, leaky = arms(examples=[from_holdout])
    twin = next(g for g in free_gates(everything, leaky, protect=holdout)
                if g.name == "template twin")
    assert not twin.passed
    assert "substitution" in twin.detail


def test_without_a_protected_set_every_gold_is_defended():
    """The right default for a caller that has not split anything yet."""
    from context_lift.round import free_gates

    everything = golds(10)
    _control, treatment = arms(examples=[Example(question=everything[0].question,
                                                 sql=everything[0].gold_sql)])

    twin = next(g for g in free_gates(everything, treatment)
                if g.name == "template twin")

    assert not twin.passed


def test_examples_from_curate_refuses_a_shared_query_shape():
    """A curate gold whose SQL masks to a held-out gold's SQL is substitution.

    Different literals, same query. Handing it over as a worked example lets
    the model answer the held-out question by swapping one value, which is not
    writing SQL and must not be scored as if it were.
    """
    from context_lift.round import Gold, examples_from_curate

    curate = [Gold("c1", "sales in Ohio", "SELECT s FROM t WHERE state = 'OH'")]
    holdout = [Gold("h1", "sales in Texas", "SELECT s FROM t WHERE state = 'TX'")]

    kept, refused = examples_from_curate(curate, holdout)
    assert kept == []
    assert [gold_id for gold_id, _ in refused] == ["c1"]
    assert "shape" in refused[0][1]


def test_examples_from_curate_refuses_wording_that_carries_a_held_out_question():
    """The subtle one: the SQL differs completely, the WORDING gives it away.

    This is the real thelook case. The curate question spells out the held-out
    question word for word, so no shape check can see it.
    """
    from context_lift.round import Gold, examples_from_curate

    curate = [Gold("c1",
                   "Which are the top 10 states by number of customers, and "
                   "what is the customer_count for each?",
                   "SELECT state, COUNT(*) c FROM users GROUP BY state "
                   "ORDER BY c DESC LIMIT 10")]
    holdout = [Gold("h1", "top 10 states by number of customers",
                    "SELECT a, b FROM elsewhere")]

    kept, refused = examples_from_curate(curate, holdout)
    assert kept == []
    assert refused[0][0] == "c1"
    assert "h1" in refused[0][1]


def test_examples_from_curate_keeps_an_unrelated_question():
    """The keep path, so the two refusals above are not just a refuse-all."""
    from context_lift.round import Gold, examples_from_curate

    curate = [Gold("c1", "how many distribution centres are there",
                   "SELECT COUNT(*) FROM distribution_centers")]
    holdout = [Gold("h1", "revenue by month in 2023",
                    "SELECT month, SUM(price) FROM order_items GROUP BY month")]

    kept, refused = examples_from_curate(curate, holdout)
    assert refused == []
    assert kept == [(curate[0].question, curate[0].gold_sql)]


@dataclass
class LookupAnswer:
    """An answer the API produced by running a stored example verbatim.

    The real shape from the run of 2026-09-10: no SQL, no thinking, and
    `matched_example` naming the example it ran.
    """
    pred_sql: str | None = ""
    matched_example: str = "revenue by month, 2023"
    citations: tuple = ()
    thought_chars: int = 0
    retries: int = 0


def test_a_working_lookup_leaves_the_comparison_instead_of_scoring_as_wrong():
    """The bug that read as "-24.6 points, 17 broke" when nothing broke.

    The agent matched a worked example and ran it, so it returned no SQL. The
    round scored that as a wrong answer, in the arm that carried the example,
    and reported the treatment as having broken 17 questions. A lookup is not
    a wrong answer and not a right one: it is not an answer about writing SQL,
    so it leaves the comparison.
    """
    from context_lift.round import run

    set_of_golds = golds(10)
    control, treatment = arms()

    def ask(scored_id, question, arm):
        # The treatment looks up the first three questions and writes correct
        # SQL for the rest; the control writes correct SQL throughout.
        index = int(scored_id.split("#")[0][1:])
        if arm is treatment and index < 3:
            return LookupAnswer()
        return Answer(pred_sql=f"SELECT {index} AS n")

    result = run(set_of_golds, control, treatment, ask, Rows(),
                 holdout_fraction=0.3, seed="contextlift",
                 force_underpowered=True)

    # Whichever split those three landed in, they are out of the numbers and
    # counted, and nothing is recorded as broken.
    counted = sum(len(v) for v in result.lookups.values())
    assert counted > 0
    for report in (result.curate, result.holdout):
        if report is not None:
            assert report.mcnemar.b == 0, "a lookup was scored as a break"


def test_a_held_out_lookup_fails_the_round():
    """The answer was handed over, so every number is about a lookup table."""
    from context_lift.round import run

    set_of_golds = golds(10)
    control, treatment = arms()
    _curate, held_out = __import__(
        "context_lift.leakage", fromlist=["split_golds"]).split_golds(
            [g.id for g in set_of_golds], 0.3, "contextlift")

    def ask(scored_id, question, arm):
        gold_id = scored_id.split("#")[0]
        if arm is treatment and gold_id in held_out:
            return LookupAnswer()
        return Answer(pred_sql=f"SELECT {int(gold_id[1:])} AS n")

    result = run(set_of_golds, control, treatment, ask, Rows(),
                 holdout_fraction=0.3, seed="contextlift",
                 force_underpowered=True)

    assert result.verdict.word == "INVALID"
    failed = [g for g in result.gates if not g.passed]
    assert any("stored-example" in g.name for g in failed)
    assert any("handed over" in g.detail for g in failed)


def test_the_page_prints_the_gap_and_says_whether_it_excludes_zero():
    """The gap is the one number that catches a fit to the working questions.

    A lift on the working set beside a flat held-out set is the shape of the
    page that read REAL GAIN on +60.6 points with a holdout that scored 2 of
    18 in both arms. The gap names it as one number with an interval.
    """
    from context_lift.round import run

    set_of_golds = golds(20)
    control, treatment = arms()
    _curate, held_out = __import__(
        "context_lift.leakage", fromlist=["split_golds"]).split_golds(
            [g.id for g in set_of_golds], 0.3, "contextlift")
    held_out = set(held_out)

    def ask(scored_id, question, arm):
        gold_id = scored_id.split("#")[0]
        index = int(gold_id[1:])
        # The treatment is right on every WORKING question and wrong on every
        # held-out one; the control is wrong throughout. A pure fit.
        if arm is treatment and gold_id not in held_out:
            return Answer(pred_sql=f"SELECT {index} AS n")
        return Answer(pred_sql="SELECT 999 AS n")

    result = run(set_of_golds, control, treatment, ask, Rows(),
                 holdout_fraction=0.3, seed="contextlift",
                 force_underpowered=True)

    point, low, high = result.gap()
    assert point > 0 and low > 0, "a pure fit must show a gap that excludes zero"
    line = [l for l in result.lines() if l.strip().startswith("gap:")]
    assert len(line) == 1
    assert "did not carry" in line[0]


def test_a_context_that_helped_both_sets_shows_no_gap():
    """The good case, printed too: a reader who only sees the gap when it is
    bad learns to read its absence as good news."""
    from context_lift.round import run

    control, treatment = arms()

    def ask(scored_id, question, arm):
        index = int(scored_id.split("#")[0][1:])
        right = Answer(pred_sql=f"SELECT {index} AS n")
        wrong = Answer(pred_sql="SELECT 999 AS n")
        # The treatment fixes most and breaks a few, in both sets. Questions
        # have to flip BOTH ways or the standard error is zero and the
        # interval has no width to include anything.
        if arm is treatment:
            return wrong if index % 4 == 0 else right
        return right if index % 4 == 0 else wrong

    result = run(golds(30), control, treatment, ask, Rows(),
                 holdout_fraction=0.3, seed="contextlift",
                 force_underpowered=True)

    line = [l for l in result.lines() if l.strip().startswith("gap:")][0]
    assert "includes zero" in line
    assert "never disagreed" not in line


def test_a_zero_width_gap_says_the_arms_never_disagreed():
    """[+0.0, +0.0] alone reads as the most certain number on the page.

    It is the emptiest: the formula returns it when no question flipped in
    either arm, so there is nothing to compare.
    """
    from context_lift.round import run

    control, treatment = arms()

    def ask(scored_id, question, arm):
        return Answer(pred_sql=f"SELECT {int(scored_id.split('#')[0][1:])} AS n")

    result = run(golds(20), control, treatment, ask, Rows(),
                 holdout_fraction=0.3, seed="contextlift",
                 force_underpowered=True)

    line = [l for l in result.lines() if l.strip().startswith("gap:")][0]
    assert "never disagreed" in line
    assert "includes zero" not in line


# --- the round must use the split you made ----------------------------------

def test_an_explicit_split_wins_over_the_rounds_own():
    """`contextlift split` writes two id files for a person to use, and a round
    that quietly made its own instead is how a worked example built from a
    "working" question turns out to be a reserved answer.

    Measured on the run that found it: `split` wrote 19 working and 20
    reserved; the round's internal split of the SAME golds gave 21 and 18,
    and the two working halves overlapped on 10 ids of 19. Nine golds treated
    as working were reserved in the round, so nine reserved answers went into
    the example bank. The leak gate caught it -- "9 of 18 held-out questions
    appear in what the agent is handed" -- but anybody following the
    documented order would have hit it, and a leak that manufactures a FALSE
    POSITIVE is the worst failure this tool has.
    """
    from context_lift.round import run
    every = golds(10)
    control, treatment = arms()
    mine = ([g.id for g in every[:6]], [g.id for g in every[6:]])
    result = run(every, control, treatment, answer_all_right, Rows(), split=mine,
                 force_underpowered=True)
    assert result.curate_ids == mine[0]
    assert result.holdout_ids == mine[1]
    # and the round's OWN split of the same golds is a different set, which
    # is the whole reason this parameter exists
    from context_lift.leakage import split_golds
    its_own = split_golds([g.id for g in every], 0.3, "contextlift")
    assert set(its_own[1]) != set(mine[1])


def test_a_split_from_a_different_gold_set_is_refused():
    """Naming an id the gold set does not have means the split came from
    somewhere else, and scoring would silently use the wrong questions."""
    import pytest
    from context_lift.round import run
    every = golds(4)
    control, treatment = arms()
    ids = [g.id for g in every]
    with pytest.raises(SystemExit, match="not in the gold set"):
        run(every, control, treatment, answer_all_right, Rows(),
            split=(ids[:2], [ids[2], "ZZ"]),
                 force_underpowered=True)


def test_a_gold_in_neither_half_is_refused():
    import pytest
    from context_lift.round import run
    every = golds(4)
    control, treatment = arms()
    ids = [g.id for g in every]
    with pytest.raises(SystemExit, match="neither half"):
        run(every, control, treatment, answer_all_right, Rows(),
            split=(ids[:1], ids[1:2]),
                 force_underpowered=True)


def test_a_gold_in_both_halves_is_refused():
    """A question cannot be practised on and reserved at once."""
    import pytest
    from context_lift.round import run
    every = golds(3)
    control, treatment = arms()
    ids = [g.id for g in every]
    with pytest.raises(SystemExit, match="BOTH halves"):
        run(every, control, treatment, answer_all_right, Rows(),
            split=(ids[:2], ids[1:]),
                 force_underpowered=True)


def answer_fixes_a_few_breaks_none(question_id, question, arm):
    """The control misses every seventh question; the context gets them all.

    A few questions fixed and none broken, which on a small set is a lift no
    test can prove. That is the case the safety line exists for: a fixture
    where the context fixes EVERYTHING reads REAL GAIN and the two lines
    never disagree, so it cannot test this.
    """
    number = int(question.split()[-1])
    if not arm.system_instruction and number % 7 == 0:
        return Answer("SELECT 999 AS n")
    return Answer(f"SELECT {number} AS n")


def answer_that_breaks_one(question_id, question, arm):
    """The context helps two questions and breaks one.

    The shape a real regression has, and the one a lift alone hides: the
    round can still show a positive lift while an answer that used to be
    right is now wrong.
    """
    number = int(question.split()[-1])
    if not arm.system_instruction:
        return Answer(f"SELECT {number} AS n" if number % 3 else "SELECT 999 AS n")
    # right everywhere EXCEPT question 1, which the control had right
    return Answer("SELECT 999 AS n" if number == 1 else f"SELECT {number} AS n")


def test_a_round_that_fixes_questions_and_breaks_none_says_so_even_when_the_lift_is_not_proven():
    """The whole reason the safety line exists: the two lines disagree.

    A lift is a claim about questions nobody has seen, so on a small set it
    reads UNDERPOWERED. "Broke 0" is a count of what happened to the
    questions in front of it, so it is true at any size. Before this, an
    amber verdict word was the only thing printed and a reader could not
    tell a harmless context from a harmful one.
    """
    control, treatment = arms()

    result = run(golds(60), control, treatment, answer_fixes_a_few_breaks_none,
                 Rows(), target_pp=10.0,
                 force_underpowered=True)

    assert result.verdict.word in ("UNDERPOWERED", "NOT PROVEN")
    assert result.safety.word == "SAFE"
    assert result.safety.broke == 0
    assert result.safety.broke_ids == ()

    lines = result.lines()
    assert lines[0].startswith("verdict: ")
    assert lines[2] == "safety: SAFE"
    assert "0 broke" in lines[3]


def test_a_question_the_context_broke_is_named_not_only_counted():
    """"broke 1" without WHICH one sends a person back to grepping."""
    control, treatment = arms()

    result = run(golds(26), control, treatment, answer_that_breaks_one,
                 Rows(), target_pp=10.0,
                 force_underpowered=True)

    assert result.safety.word in ("MIXED", "REGRESSION")
    assert result.safety.broke >= 1
    assert result.safety.broke_ids, "a broken question must be named"
    assert any(line.startswith("  broke: ") for line in result.lines())


def test_the_safety_line_counts_questions_not_repetitions():
    """Three repetitions of one question are one question.

    `broke_ids` on the report carries a `#rep` suffix per repetition, so
    counting from it would report one question broken three times. A page
    that says "broke 3" over one question sends someone looking for two
    questions that do not exist.
    """
    control, treatment = arms()

    result = run(golds(26), control, treatment, answer_fixes_a_few_breaks_none,
                 Rows(), k=3, target_pp=10.0,
                 force_underpowered=True)

    assert result.safety.n == len(result.holdout_ids), (
        "the held-out questions, not their repetitions")
    assert all("#rep" not in qid for qid in result.safety.broke_ids)


def test_the_run_csv_carries_every_field_the_rows_have():
    """A hand-written fieldname list drifts from what `_run_rows` produces.

    Adding `error_class` for D67 crashed a 74-minute paid round at the write,
    after every answer had been paid for, with "dict contains fields not in
    fieldnames". The answers survived only because --checkpoint was passed.
    The list is derived from the rows now, so a new field lands in the file
    instead of raising.
    """
    import csv
    import io

    control, treatment = arms()
    result = run(golds(26), control, treatment, answer_fixes_a_few_breaks_none,
                 Rows(), target_pp=10.0,
                 force_underpowered=True)

    keys = {key for row in result.rows for key in row}
    assert "error_class" in keys, "the runner's diagnosis must reach the file"

    # what cli.py does, and it must not raise on any field
    leading = ["id", "split", "arm", "arm_id", "question", "gold_sql",
               "pred_sql", "verdict", "correct", "error_class", "citations",
               "matched_example", "job_id", "thought_chars", "retries"]
    fields = [k for k in leading if k in keys] + sorted(keys - set(leading))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(result.rows)

    assert buffer.getvalue().splitlines()[0].startswith("id,split,arm,")
