# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Behavior tests for the paired context ablation.

The arithmetic is McNemar's, already pinned against published values in
test_metrics.py. What these tests pin is the part ablate.py owns: that the two
arms are paired by id, that a mismatched pair is refused loudly rather than
silently truncated to the overlap, and that "context broke it" is counted
separately from "context fixed it".
"""
import pytest

from context_lift.ablate import ablate, climb, ladder_step, pool, verdict_word
from context_lift.harness import EvalReport, QuestionResult
from context_lift.metrics import proportion


def _report(outcomes: list[tuple[str, bool | None]],
            answers: dict[str, str] | None = None) -> EvalReport:
    """Build an EvalReport from (id, correct) pairs; None means excluded.

    `answers` is the SQL each question was scored on, when a test needs the
    text as well as the outcome. A real model writes different text every
    time while scoring the same way, so the two are not interchangeable.
    """
    answers = answers or {}
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
            answer=answers.get(qid),
        )
        for qid, correct in outcomes
    ]
    scored = [c for _, c in outcomes if c is not None]
    k = sum(1 for c in scored if c)
    rate = proportion(k, len(scored))
    return EvalReport(
        results=results,
        n_total=len(outcomes),
        n_scored=len(scored),
        n_excluded=len(outcomes) - len(scored),
        execution=rate,
        adjusted=rate,
    )


def test_lift_is_the_difference_between_the_two_rates():
    without = _report([("q1", False), ("q2", False), ("q3", False), ("q4", True)])
    with_ = _report([("q1", True), ("q2", True), ("q3", False), ("q4", True)])
    report = ablate(without, with_)
    assert report.without_context.point == 0.25
    assert report.with_context.point == 0.75
    assert report.lift == 0.5


def test_counts_questions_the_context_fixed_and_broke_separately():
    # q1 fixed by context, q2 broken by it, q3 and q4 unchanged.
    without = _report([("q1", False), ("q2", True), ("q3", True), ("q4", False)])
    with_ = _report([("q1", True), ("q2", False), ("q3", True), ("q4", False)])
    report = ablate(without, with_)
    assert report.mcnemar.c == 1  # fixed
    assert report.mcnemar.b == 1  # broke
    assert report.lift == 0.0  # a net-zero lift still hides two real changes


def test_identical_arms_give_zero_lift_and_p_of_one():
    same = [("q1", True), ("q2", False), ("q3", True)]
    report = ablate(_report(same), _report(same))
    assert report.lift == 0.0
    assert report.mcnemar.p_value == 1.0
    assert not report.significant
    assert "never disagreed" in report.summary()


def test_a_large_consistent_lift_is_significant():
    # 10 questions fixed, none broken: p = 2 * 0.5^10 ~= 0.002.
    without = _report([(f"q{i}", False) for i in range(10)])
    with_ = _report([(f"q{i}", True) for i in range(10)])
    report = ablate(without, with_)
    assert report.mcnemar.c == 10
    assert report.mcnemar.b == 0
    assert report.significant


def test_refuses_to_pair_arms_that_scored_different_questions():
    without = _report([("q1", True), ("q2", False)])
    with_ = _report([("q1", True), ("q3", False)])
    with pytest.raises(ValueError, match="did not score the same questions"):
        ablate(without, with_)


def test_refuses_when_one_arm_excluded_a_question_the_other_scored():
    # A gold that errored in one arm only: denominators differ, so the pair
    # is invalid even though both runs have the same question ids.
    without = _report([("q1", True), ("q2", False)])
    with_ = _report([("q1", True), ("q2", None)])
    with pytest.raises(ValueError, match="scored only without context"):
        ablate(without, with_)


def test_summary_names_the_arm_it_compared():
    same = [("q1", True)]
    assert "execution accuracy" in ablate(_report(same), _report(same)).summary()
    assert "judge-adjusted" in ablate(_report(same), _report(same), adjusted=True).summary()


# --- hill climbing across rounds ---


def test_climb_needs_a_baseline_plus_at_least_one_round():
    with pytest.raises(ValueError, match="baseline plus at least one round"):
        climb([("base", _report([("q1", True)]))])


def test_climb_reports_each_round_against_previous_and_against_baseline():
    base = _report([("q1", False), ("q2", False), ("q3", False), ("q4", False)])
    r1 = _report([("q1", True), ("q2", False), ("q3", False), ("q4", False)])
    r2 = _report([("q1", True), ("q2", True), ("q3", True), ("q4", False)])
    report = climb([("baseline", base), ("round 1", r1), ("round 2", r2)])

    assert [r.label for r in report.rounds] == ["baseline", "round 1", "round 2"]
    assert report.rounds[1].gain_vs_previous == 0.25
    assert report.rounds[1].gain_vs_baseline == 0.25
    assert report.rounds[2].gain_vs_previous == 0.5   # round 2 alone
    assert report.rounds[2].gain_vs_baseline == 0.75  # everything so far
    assert report.total_gain == 0.75


def test_climb_flags_a_round_that_broke_more_than_it_fixed():
    base = _report([("q1", True), ("q2", True), ("q3", False)])
    bad = _report([("q1", False), ("q2", False), ("q3", True)])
    report = climb([("baseline", base), ("bad round", bad)])
    assert report.rounds[1].regressed
    assert "REGRESSED: broke 2, fixed 1" in report.summary()


def test_climb_names_a_round_that_changed_nothing():
    same = [("q1", True), ("q2", False)]
    report = climb([("baseline", _report(same)), ("round 1", _report(same))])
    assert not report.rounds[1].regressed
    assert "bought nothing" in report.summary()


def test_climb_warns_when_scored_on_the_questions_that_guided_curation():
    base = _report([("q1", False)])
    better = _report([("q1", True)])
    warned = climb([("baseline", base), ("round 1", better)])
    assert "training score" in warned.summary()

    held_out = climb([("baseline", base), ("round 1", better)], holdout_used=True)
    assert "training score" not in held_out.summary()


def test_climb_refuses_rounds_that_scored_different_questions():
    base = _report([("q1", True), ("q2", False)])
    drifted = _report([("q1", True), ("q9", False)])
    with pytest.raises(ValueError, match="did not score the same questions"):
        climb([("baseline", base), ("round 1", drifted)])


# --- the Ladder guard against climbing on noise ---


def test_ladder_step_shrinks_as_the_eval_set_grows():
    # 1/sqrt(n): 20 questions cannot resolve less than a ~22 point gain.
    assert round(ladder_step(20), 3) == 0.224
    assert round(ladder_step(100), 3) == 0.1


def test_ladder_score_ignores_a_gain_inside_the_noise():
    # 10 questions -> step is ~31.6 points. A 10-point gain does not move it.
    base = _report([(f"q{i}", i < 3) for i in range(10)])          # 30%
    small_gain = _report([(f"q{i}", i < 4) for i in range(10)])    # 40%
    report = climb([("baseline", base), ("round 1", small_gain)])
    assert report.rounds[1].rate.point == 0.4
    assert report.rounds[1].ladder_rate == 0.3   # unmoved
    assert "inside the noise" in report.summary()


def test_ladder_score_accepts_a_gain_that_clears_the_step():
    base = _report([(f"q{i}", False) for i in range(10)])          # 0%
    big_gain = _report([(f"q{i}", i < 8) for i in range(10)])      # 80%
    report = climb([("baseline", base), ("round 1", big_gain)])
    assert report.rounds[1].ladder_rate == 0.8


def test_ladder_never_goes_down_when_a_round_regresses():
    # The reported score is the best that ever cleared the bar, so a bad round
    # cannot lower it — it just fails to raise it.
    base = _report([(f"q{i}", False) for i in range(10)])
    good = _report([(f"q{i}", i < 8) for i in range(10)])
    bad = _report([(f"q{i}", i < 2) for i in range(10)])
    report = climb([("baseline", base), ("good", good), ("bad", bad)])
    assert report.rounds[1].ladder_rate == 0.8
    assert report.rounds[2].ladder_rate == 0.8
    assert report.rounds[2].regressed


# --- pooling repeated runs, because one run is a sample not a measurement ---


def test_pooling_three_reps_gives_three_times_the_observations():
    reps = [_report([("q1", True), ("q2", False)]) for _ in range(3)]
    pooled = pool(reps)
    assert pooled.n_scored == 6
    assert pooled.execution.point == 0.5


def test_pooled_ids_stay_unique_so_the_paired_test_lines_up():
    pooled = pool([_report([("q1", True)]), _report([("q1", False)])])
    assert [r.id for r in pooled.results] == ["q1#rep0", "q1#rep1"]


def test_pooling_finds_a_lift_no_single_rep_can():
    # The real shape from an engagement: each rep fixes 1-3 questions and none
    # reaches p<0.05 alone, because 4 discordant pairs is the floor for 0.125.
    weak = [_report([(f"q{i}", i < 2) for i in range(19)]),
            _report([(f"q{i}", i < 1) for i in range(19)]),
            _report([(f"q{i}", i < 3) for i in range(19)])]
    strong = [_report([(f"q{i}", i < 4) for i in range(19)]) for _ in range(3)]

    for a, b in zip(weak, strong):
        assert not ablate(a, b).significant        # no single rep can

    assert ablate(pool(weak), pool(strong)).significant   # pooled, it can


def test_pooling_refuses_reports_over_different_questions():
    with pytest.raises(ValueError, match="different questions"):
        pool([_report([("q1", True)]), _report([("q9", True)])])


def test_pooling_needs_at_least_one_report():
    with pytest.raises(ValueError, match="at least one"):
        pool([])


def test_ablate_names_the_questions_it_counted_as_broken_and_fixed():
    # The counts alone send a developer grepping two reports; the ids are the
    # actionable output.
    without = _report([("q1", False), ("q2", True), ("q3", True), ("q4", False)])
    with_ = _report([("q1", True), ("q2", False), ("q3", True), ("q4", False)])
    report = ablate(without, with_)
    assert report.broke_ids == ("q2",)
    assert report.fixed_ids == ("q1",)
    assert report.mcnemar.b == len(report.broke_ids)
    assert report.mcnemar.c == len(report.fixed_ids)


def test_flips_summary_collapses_pooled_reps_to_one_line_per_question():
    # q2 breaks in both reps -> "q2 x2"; a flip in every rep is a consistent
    # effect, and the display must say so rather than list q2 twice.
    without = pool([
        _report([("q1", False), ("q2", True)]),
        _report([("q1", False), ("q2", True)]),
    ])
    with_ = pool([
        _report([("q1", False), ("q2", False)]),
        _report([("q1", False), ("q2", False)]),
    ])
    text = ablate(without, with_).flips_summary()
    assert "q2  x2" in text
    assert "q2#rep0" not in text
    assert "fixed (0)" in text


def test_climb_rounds_carry_the_ids_behind_their_regression_note():
    baseline = _report([("q1", True), ("q2", True), ("q3", False)])
    round1 = _report([("q1", False), ("q2", True), ("q3", True)])
    report = climb([("base", baseline), ("round 1", round1)])
    assert report.rounds[1].broke_ids == ("q1",)
    assert report.rounds[1].fixed_ids == ("q3",)
    assert "q1" in report.flips_summary()


def test_pooled_reports_get_a_clustered_by_question_line():
    # Reps of one question share its difficulty; the clustered test collapses
    # them to a majority vote. Here q2 flips in both reps (real, survives
    # clustering) and q1 flips in neither.
    without = pool([
        _report([("q1", True), ("q2", True)]),
        _report([("q1", True), ("q2", True)]),
        _report([("q1", True), ("q2", True)]),
    ])
    with_ = pool([
        _report([("q1", True), ("q2", False)]),
        _report([("q1", True), ("q2", False)]),
        _report([("q1", True), ("q2", False)]),
    ])
    report = ablate(without, with_)
    assert report.n_questions == 2 and report.n_reps == 3
    assert report.clustered.b == 1 and report.clustered.c == 0
    text = report.summary()
    assert "3 reps" in text
    # the number to defend comes first and carries its own ruling; the
    # optimistic one is labelled as optimistic. Whichever is printed first and
    # annotated is the one that gets quoted.
    assert text.index("each question counted once") < text.index("each answer counted separately")
    assert "the number to defend" in text
    assert "optimistic" in text
    assert text.count("significant at 0.05") == 2      # both, never one


def test_a_tied_question_abstains_from_the_clustered_test():
    # q1 flips in exactly 1 of 2 reps: no majority either way, so it carries
    # no clustered evidence - the pooled count books it, the clustered drops it.
    without = pool([_report([("q1", True)]), _report([("q1", True)])])
    with_ = pool([_report([("q1", False)]), _report([("q1", True)])])
    report = ablate(without, with_)
    assert report.mcnemar.b == 1          # pooled sees the flip
    assert report.clustered.b == 0        # clustered sees a tie, abstains


def test_flips_summary_labels_a_both_lists_question_as_unstable():
    without = pool([_report([("q1", True)]), _report([("q1", False)])])
    with_ = pool([_report([("q1", False)]), _report([("q1", True)])])
    text = ablate(without, with_).flips_summary()
    assert "unstable" in text


def test_per_question_status_names_what_the_context_did_across_reps():
    from context_lift.ablate import per_question_status

    assert per_question_status([False, False, False], [True, True, True]) == "fixed"
    assert per_question_status([True, True, True], [False, False, False]) == "broke"
    assert per_question_status([False, False, True], [True, True, False]) == "unstable"  # both ways
    assert per_question_status([False, False, False], [True, False, False]) == "unstable"  # one rep only
    assert per_question_status([True, True, True], [True, True, True]) == "right"
    assert per_question_status([False, False, False], [False, False, False]) == "wrong"
    assert per_question_status([True, False, True], [True, False, True]) == "unstable"  # varies inside an arm
    assert per_question_status([False], [True]) == "fixed"  # a single run is still a pair


def _answered(outcomes: list[tuple[str, bool, str]]) -> EvalReport:
    """(id, correct, answer) triples: a report that kept what was scored."""
    results = [
        QuestionResult(id=qid, question=f"question {qid}",
                       verdict="MATCH" if correct else "MISMATCH",
                       execution_correct=correct, adjusted_correct=correct,
                       judged=False, judge_verdict="", detail="", answer=answer)
        for qid, correct, answer in outcomes
    ]
    return EvalReport(results=results, n_total=len(results), n_scored=len(results),
                      n_excluded=0, execution=proportion(sum(r.execution_correct for r in results), len(results)),
                      adjusted=proportion(sum(r.adjusted_correct for r in results), len(results)))


def test_ablate_counts_answers_that_moved_without_changing_the_verdict():
    """16 of 17 answers on one estate moved from one wrong table to another;
    fixed/broke alone read that as nothing happened."""
    before = _answered([("q1", True, "SELECT a"), ("q2", False, "sales.t_tmp"),
                        ("q3", False, "sales.t_bkp"), ("q4", True, "select  b")])
    after = _answered([("q1", True, "SELECT a"), ("q2", False, "sales.t_v1"),
                       ("q3", True, "sales.t"), ("q4", True, "select b")])

    report = ablate(before, after)

    assert report.churn == {"pairs": 4, "changed": 2, "changed_verdict": 1}
    assert "answers changed at 2 of 4 pairs; 1 changed the verdict, 1 moved without changing it" \
        in report.summary()


def test_a_report_saved_before_answers_existed_gets_no_churn_line():
    report = ablate(_report([("q1", True), ("q2", False)]), _report([("q1", False), ("q2", True)]))

    assert report.churn is None
    assert "answers changed" not in report.summary()


def test_identical_repetitions_are_named_as_copies_in_the_verdict():
    before = _answered([("q1", True, "SELECT a"), ("q2", False, "SELECT b")])
    after = _answered([("q1", True, "SELECT a"), ("q2", True, "SELECT c")])

    report = ablate(pool([before, before, before]), pool([after, after, after]))

    assert report.reps_identical is True
    assert "copies, not samples" in report.summary()
    assert ablate(before, after).reps_identical is None      # no repetitions to judge


# --- reading the verdict: the interval on the lift, and what to do with the word ---


def test_the_lift_line_carries_an_interval_on_the_paired_difference():
    # 4 fixed, 0 broke, 16 pairs: the Wald interval pinned in test_metrics
    without = _report([(f"q{i}", False) for i in range(16)])
    with_ = _report([(f"q{i}", i < 4) for i in range(16)])

    report = ablate(without, with_)

    assert report.lift_interval() == pytest.approx((0.0378, 0.4622), abs=5e-4)
    assert "  lift            : +25.0%  [+3.8%, +46.2%]" in report.summary()
    assert "no interval of its own" not in report.summary()


def test_the_lift_interval_is_per_question_when_repetitions_are_pooled():
    # q2 fixed in all three reps, q1 unchanged: per question that is 1 fixed
    # of 2, not 3 fixed of 6, and the line says so
    without = pool([_report([("q1", True), ("q2", False)])] * 3)
    with_ = pool([_report([("q1", True), ("q2", True)])] * 3)

    report = ablate(without, with_)

    assert report.n_defended == 2
    assert report.lift_interval() == pytest.approx((-0.193, 1.0), abs=5e-4)
    assert "per question)" in report.summary()


def test_a_small_set_prints_both_resolution_numbers():
    """They answer different questions and printing one is how a reader
    concludes the wrong thing.

    `questions_for_target` says what 20 questions could EVER have shown, which
    is nothing under 10 points; about 60 could. `questions_to_prove` scales the
    swing this run actually saw, which needs about 84.
    """
    # 20 questions, 3 fixed and 1 broke: p = 0.625, lift +10 points.
    # 1/sqrt(20) is 22 points; scaling 1:3 of 20 up, p first drops below
    # 0.05 at 84 questions.
    without = _report([(f"q{i}", i == 0) for i in range(20)])
    with_ = _report([(f"q{i}", i in (1, 2, 3)) for i in range(20)])

    report = ablate(without, with_)

    assert report.mcnemar.p_value == 0.625 and report.lift == pytest.approx(0.10)
    assert report.verdict_sentences() == [
        "at 20 questions this set could not have shown a 10-point lift even if "
        "nothing broke, and about 60 questions could; to prove the +10.0% seen "
        "here you would need about 84 questions"]
    assert report.verdict_sentences()[0] in report.summary()


def test_not_proven_with_an_even_swing_says_no_size_would_prove_it():
    without = _report([("q1", True), ("q2", False), ("q3", False), ("q4", True)])
    with_ = _report([("q1", False), ("q2", True), ("q3", True), ("q4", False)])

    report = ablate(without, with_)

    assert report.lift == 0.0
    assert report.verdict_sentences() == [
        "at 4 questions this set could not have shown a 10-point lift even if "
        "nothing broke, and about 60 questions could; as many questions broke as "
        "were fixed, so no number of questions would prove a lift of this shape"]
    lopsided = ablate(_report([("q1", True), ("q2", False), ("q3", False)]),
                      _report([("q1", False), ("q2", True), ("q3", False)]))
    assert "no number of questions would prove" in lopsided.verdict_sentences()[0]


def test_two_identical_arms_on_two_questions_is_unresolved_not_no_change():
    """NO CHANGE means "the context moved nothing" and needs a set that could
    have seen movement. Two questions could not, so saying it would state a
    finding the set cannot support. The first live round made this mistake on
    six questions and printed NO CHANGE.
    """
    same = [("q1", True), ("q2", False)]

    report = ablate(_report(same), _report(same))
    sentences = report.verdict_sentences()

    assert verdict_word(report)[0] == "UNDERPOWERED"
    assert sentences == ["at 2 questions this set could not have shown a 10-point "
                         "lift even if nothing broke, and about 60 questions could; "
                         "the two arms never disagreed on a single question"]
    assert sentences[0] in report.summary()


def test_a_real_loss_names_the_broken_questions_first():
    # 6 broke, 0 fixed: p = 2 / 64 = 0.03125, a proven loss
    without = _report([(f"q{i}", True) for i in range(10)])
    with_ = _report([(f"q{i}", i >= 6) for i in range(10)])

    report = ablate(without, with_)

    assert report.significant and report.lift < 0
    assert report.verdict_sentences() == [
        "a real loss: the context broke q0, q1, q2, q3, q4, q5; read those before anything else"]


def test_questions_to_prove_scales_the_discordant_rates_until_the_test_turns():
    from context_lift.ablate import questions_to_prove
    from context_lift.metrics import mcnemar_exact

    assert questions_to_prove(1, 3, 20) == 84
    assert mcnemar_exact(round(84 / 20), round(3 * 84 / 20)).p_value < 0.05
    assert questions_to_prove(2, 2, 10) is None            # an even swing never proves
    assert questions_to_prove(10, 11, 100) is None         # not within the 5000 cap


def test_an_arm_with_zero_correct_is_named_a_setup_fault():
    before = _answered([("q1", False, "a"), ("q2", False, "b")])
    after = _answered([("q1", True, "a"), ("q2", False, "b")])

    assert "usually a setup fault" in ablate(before, after).summary()
    assert "usually a setup fault" not in ablate(after, after).summary()


def test_the_summary_prints_the_ruling_word_bundle_computes():
    """AGENTS.md tells an assistant to repeat the sentence after the ruling,
    and only bundle's verdict.json carried a ruling word."""
    without = _report([(f"q{i}", False) for i in range(20)])
    with_ = _report([(f"q{i}", i < 12) for i in range(20)])

    report = ablate(without, with_)

    assert verdict_word(report)[0] == "REAL GAIN"
    assert "  ruling          : REAL GAIN" in report.summary()
    assert report.verdict_sentences()[0] in report.summary()


def test_every_ruling_word_carries_one_next_step_sentence():
    """Each word needs a set that can earn it.

    NO CHANGE and NOT PROVEN both mean "the set could have seen a lift", so
    both need at least the 60 questions a 10-point target takes. Built on two
    questions, as an earlier version of this test was, they are UNDERPOWERED and
    the test was asserting words the data could not support.
    """
    big_same = [(f"q{i}", i % 2 == 0) for i in range(60)]
    words = {}
    for name, without, with_ in [
        ("REAL GAIN", [(f"q{i}", False) for i in range(20)],
         [(f"q{i}", i < 12) for i in range(20)]),
        ("REAL LOSS", [(f"q{i}", i < 12) for i in range(20)],
         [(f"q{i}", False) for i in range(20)]),
        # 60 questions, identical arms: big enough to have seen a lift, and
        # nothing moved.
        ("NO CHANGE", big_same, big_same),
        # 60 questions, 1 broke and 2 fixed: resolvable, and not proven.
        ("NOT PROVEN", [(f"q{i}", i < 30) for i in range(60)],
         [(f"q{i}", i < 30 and i != 0 or i in (30, 31)) for i in range(60)]),
        # 2 questions: too few to have seen anything, whatever it saw.
        ("UNDERPOWERED", [("q1", True), ("q2", False)], [("q1", True), ("q2", True)]),
    ]:
        report = ablate(_report(without), _report(with_))
        words[name] = report.verdict_sentences()
        assert verdict_word(report)[0] == name, (name, report.mcnemar)

    assert all(len(sentences) == 1 for sentences in words.values()), words
    assert words["REAL GAIN"][0].startswith("a real gain")
    assert words["REAL LOSS"][0].startswith("a real loss: the context broke q0")
    assert "never disagreed" in words["NO CHANGE"][0]
    assert "could not have shown" in words["UNDERPOWERED"][0]


def test_a_gain_that_also_broke_questions_names_them_in_its_sentence():
    without = _report([(f"q{i}", i == 0) for i in range(20)])
    with_ = _report([(f"q{i}", 1 <= i <= 12) for i in range(20)])

    sentence = ablate(without, with_).verdict_sentences()[0]

    assert sentence == ("a real gain, but the context also broke q0; "
                        "read those before quoting the lift")


def test_the_one_repetition_caveat_reads_the_pooled_count_not_the_test():
    """Three reports per arm restricted to ids none of them held printed
    'one repetition per arm' over 0 questions."""
    three = [_report([("q1", True), ("q2", False)]) for _ in range(3)]
    pooled = ablate(pool(three), pool(three))
    one = ablate(pool(three[:1]), pool(three[:1]))
    empty = ablate(pool([_report([])] * 3), pool([_report([])] * 3))

    assert (pooled.n_reps, one.n_reps, empty.n_reps) == (3, 1, None)
    assert "one repetition per arm" not in pooled.summary()
    assert "one repetition per arm" in one.summary()
    assert "one repetition per arm" not in empty.summary()


def test_climb_splits_pooled_observations_into_questions_and_reps():
    """The header said '354 questions' for 118 questions answered 3 times."""
    base = [_report([(f"q{i}", False) for i in range(6)]) for _ in range(3)]
    later = [_report([(f"q{i}", i < 4) for i in range(6)]) for _ in range(3)]

    report = climb([("r0", pool(base)), ("r1", pool(later))])

    assert (report.n_paired, report.n_questions, report.n_reps) == (18, 6, 3)
    assert report.summary().splitlines()[0] == (
        "context enrichment over 2 rounds, 18 observations "
        "(6 questions x 3 reps, execution accuracy)")


def test_climb_prints_the_last_round_against_the_baseline_as_a_ruling():
    base = _report([(f"q{i}", False) for i in range(20)])
    later = _report([(f"q{i}", i < 12) for i in range(20)])

    summary = climb([("r0", base), ("r1", later)]).summary()

    assert "  ruling (r1 vs r0): REAL GAIN" in summary
    assert "a real gain: 12 of 20 questions moved up and none moved down" in summary


# --- a verdict where nothing became correct still carries information ---


def _shaped(outcomes: list[tuple[str, str, str]]) -> EvalReport:
    """A report from (id, verdict, detail), correct where the verdict is MATCH,
    so the triage buckets are the ones a real run would produce."""
    results = [
        QuestionResult(id=qid, question=f"question {qid}", verdict=verdict,
                       execution_correct=verdict == "MATCH", adjusted_correct=verdict == "MATCH",
                       judged=False, judge_verdict="", detail=detail)
        for qid, verdict, detail in outcomes
    ]
    rate = proportion(sum(1 for r in results if r.execution_correct), len(results))
    return EvalReport(results=results, n_total=len(results), n_scored=len(results),
                      n_excluded=0, execution=rate, adjusted=rate)


def test_two_arms_at_zero_report_the_failure_shapes_that_moved():
    """Five of eighteen questions stopped returning empty results and two
    started, while execution accuracy read 0.000 before and 0.000 after. The
    comparison printed that the arms never disagreed, which is true about
    correctness and reads as nothing happened."""
    before = _shaped([(f"q{i}", "EMPTY_ONE", "") for i in range(7)]
                     + [(f"q{i}", "MISMATCH", "different row count: 3 vs 9")
                        for i in range(7, 18)])
    after = _shaped([(f"q{i}", "EMPTY_ONE", "") for i in range(2)]
                    + [(f"q{i}", "MISMATCH", "different row count: 3 vs 9")
                       for i in range(2, 16)]
                    + [(f"q{i}", "ERROR", "no such column: x") for i in range(16, 18)])

    report = ablate(before, after)

    assert report.failure_shapes == {"without": {"EMPTY_RESULT": 7, "ROW_COUNT": 11},
                                     "with": {"EMPTY_RESULT": 2, "ROW_COUNT": 14,
                                              "DID_NOT_RUN": 2}}
    assert report.failure_shape_lines() == [
        "  no answer became correct in either arm, so this score has no resolution here; "
        "the failure shapes did move:",
        "    EMPTY_RESULT      7 -> 2    -5",
        "    ROW_COUNT        11 -> 14   +3",
        "    DID_NOT_RUN       0 -> 2    +2",
    ]
    assert report.failure_shape_lines()[1] in report.summary()


def test_two_arms_at_zero_with_the_same_shapes_say_that_too():
    same = [("q1", "EMPTY_ONE", ""), ("q2", "EMPTY_ONE", "")]

    report = ablate(_shaped(same), _shaped(same))

    assert report.failure_shape_lines() == [
        "  no answer became correct in either arm, so this score has no resolution here; "
        "the failure shapes did not move either"]


def test_unchanged_correctness_with_moved_shapes_is_reported():
    """Both arms right on q1 and wrong on q2, and q2's failure changed shape:
    it stopped failing to run and started returning the wrong values."""
    before = _shaped([("q1", "MATCH", ""), ("q2", "ERROR", "no such column: x")])
    after = _shaped([("q1", "MATCH", ""), ("q2", "MISMATCH", "same shape, different values")])

    lines = ablate(before, after).failure_shape_lines()

    assert lines[0] == ("  no question changed correctness, so the rates say nothing; "
                        "the failure shapes did move:")
    assert lines[1:] == ["    DID_NOT_RUN       1 -> 0    -1",
                         "    WRONG_VALUES      0 -> 1    +1"]


def test_a_comparison_that_can_speak_for_itself_prints_no_shape_block():
    without = _report([(f"q{i}", False) for i in range(20)])
    with_ = _report([(f"q{i}", i < 12) for i in range(20)])

    report = ablate(without, with_)

    assert report.failure_shapes["without"] == {"UNCLASSIFIED": 20}
    assert report.failure_shape_lines() == []
    assert "failure shapes" not in report.summary()


def test_the_repeat_check_reads_correctness_because_the_text_never_repeats():
    """Measured on 2026-09-09, two runs of identical arms against the
    Conversational Analytics API: 0 of 52 answers came back as the same SQL,
    and 50 of 52 kept their correctness.

    So `reps_identical` is false against any real model and cannot say whether
    K=1 is enough. `reps_correctness_stable` can: it asks whether the thing the
    score is made of came out the same way twice.
    """
    # q1 scores the same in both reps; q2 flips. Different SQL every time.
    without = _report([("q1#rep0", True), ("q1#rep1", True),
                       ("q2#rep0", True), ("q2#rep1", False)],
                      answers={"q1#rep0": "SELECT 1", "q1#rep1": "SELECT 1 -- a",
                               "q2#rep0": "SELECT 2", "q2#rep1": "SELECT 2 -- b"})
    with_ = _report([("q1#rep0", True), ("q1#rep1", True),
                     ("q2#rep0", True), ("q2#rep1", True)],
                    answers={"q1#rep0": "SELECT 1 -- c", "q1#rep1": "SELECT 1 -- d",
                             "q2#rep0": "SELECT 2 -- e", "q2#rep1": "SELECT 2 -- f"})

    report = ablate(without, with_)

    assert report.reps_identical is False, "no two answers share their text"
    # 3 of the 4 (question, arm) pairs scored the same way twice.
    assert report.reps_correctness_stable == 0.75


def test_a_run_with_no_repetitions_has_no_repeat_number():
    """None, not 1.0: a set that was never repeated has not been shown stable."""
    report = ablate(_report([("q1", True)]), _report([("q1", False)]))

    assert report.reps_correctness_stable is None


def _discordant(b, c, n):
    """An ablation whose only meaningful parts are the discordant counts."""
    from context_lift.ablate import AblationReport
    from context_lift.metrics import mcnemar_exact, proportion
    lift = (c - b) / n
    return AblationReport(
        without_context=proportion(0, n), with_context=proportion(c - b, n),
        lift=lift, mcnemar=mcnemar_exact(b, c), n_paired=n, adjusted=False,
        n_questions=n)


def test_overfit_needs_the_difference_to_exclude_zero_not_two_p_values():
    """The difference-in-significance fallacy, caught by a real run.

    On 2026-09-10 the working set moved +20.8 and was significant, the
    held-out set moved +13.3 with 5 fixed and 1 broke at p = 0.219, and the
    word came out OVERFIT while the gap printed on the same page read
    [-12.1, +26.9]. The page contradicted itself: the verdict said the
    context fitted the questions it was written from, and the one test that
    measures that said there was no evidence of it.
    """
    from context_lift.ablate import verdict

    working = _discordant(b=1, c=12, n=53)      # +20.8 points, significant
    held_out = _discordant(b=1, c=5, n=30)      # +13.3 points, p = 0.219

    result = verdict(held_out, working)

    assert result.word != "OVERFIT", (
        "two significance results side by side are not a test that they differ")


def test_a_real_fit_still_reads_overfit():
    """The rule must still catch the page it was written for: a large
    working-set lift beside a held-out set that moved nothing at all."""
    from context_lift.ablate import verdict

    working = _discordant(b=0, c=20, n=33)      # +60.6 points
    held_out = _discordant(b=0, c=0, n=18)      # flat

    assert verdict(held_out, working).word == "OVERFIT"


def test_the_overfit_reason_quotes_the_difference_it_rests_on():
    """A reader who is told "the held-out ones did not" beside a number like
    +13.3 reads a contradiction, because +13.3 is not "did not"."""
    from context_lift.ablate import verdict

    result = verdict(_discordant(0, 0, 18), _discordant(0, 20, 33))

    assert "the difference between the two excludes zero" in result.reason


# --- pairing what both arms scored, when one round produced both -------------

def test_a_round_pairs_the_questions_both_arms_scored():
    """A 45-minute round used to be thrown away over two questions.

    Both arms answer the same golds in one process, so a difference in what
    they SCORED can only be a per-arm exclusion: a prediction hits the row
    cap, is excluded as ROW_CAP_HIT, and the other arm's answer to the same
    question is fine. Observed on a real run:

      ValueError: the two arms did not score the same questions, so they
      cannot be paired. scored only with context: ['m027...', 'm360...']

    Two of eighteen. Refusing there loses the verdict and every API call
    behind it.
    """
    # 2 of 18, the proportion actually observed. Keeping the real shape
    # matters: 1 of 4 is 25% and would hit the refusal threshold, which is
    # what a made-up fixture did before this comment existed.
    ids = [f"q{i}" for i in range(18)]
    without = _report([(q, i % 3 == 0) for i, q in enumerate(ids[:16])]
                      + [(ids[16], None), (ids[17], None)])
    with_ctx = _report([(q, i % 2 == 0) for i, q in enumerate(ids[:16])]
                       + [(ids[16], True), (ids[17], True)])
    report = ablate(without, with_ctx, pair_common=True)
    assert report.n_paired == 16
    assert set(report.unpaired_ids) == {ids[16], ids[17]}
    # the rate is rebuilt over the 16, not left at the 18-question value
    assert report.without_context.n == 16 and report.with_context.n == 16
    assert "dropping 2 that only one arm scored" in report.summary()


def test_pairing_is_opt_in_because_the_other_callers_want_the_refusal():
    """Across runs -- climb comparing rounds, bundle comparing saved reports
    -- a differing question set means the GOLD SET changed, and then the two
    numbers are not measuring the same thing. Those callers keep refusing."""
    import pytest
    without = _report([("q1", True), ("q2", None)])
    with_ctx = _report([("q1", True), ("q2", True)])
    with pytest.raises(ValueError, match="did not score the same questions"):
        ablate(without, with_ctx)


def test_too_many_unpaired_is_still_refused():
    """A row cap excluding one arm's prediction is ordinary and rare. An arm
    that stopped early is neither, and then the paired set is not the set
    anybody means."""
    import pytest
    without = _report([("q1", True)] + [(f"x{i}", None) for i in range(9)])
    with_ctx = _report([("q1", True)] + [(f"x{i}", True) for i in range(9)])
    with pytest.raises(ValueError, match="scored in only one arm"):
        ablate(without, with_ctx, pair_common=True)


def test_the_safety_word_turns_on_the_break_count_and_nothing_else():
    """A lift needs a test. A break count does not, so the word is a lookup.

    SAFE at any size, including a set far too small for any lift to be
    proven, because "nothing broke" is a fact about the questions in front
    of it and not a claim about questions nobody has seen.
    """
    from context_lift.ablate import safety_word

    assert safety_word(0, 0, None, 3)[0] == "SAFE"
    assert safety_word(5, 0, None, 18)[0] == "SAFE"
    assert safety_word(5, 2, None, 18)[0] == "MIXED"
    assert safety_word(2, 2, None, 18)[0] == "REGRESSION"
    assert safety_word(1, 5, None, 18)[0] == "REGRESSION"


def test_an_unstable_question_is_not_reported_as_damage():
    """A question that flips between repetitions on unchanged context is
    instability, not a break. Counting it as broken overstates; folding it
    into safe hides it. It gets its own number."""
    from context_lift.ablate import safety_word

    word, reason = safety_word(3, 0, 2, 10)
    assert word == "SAFE"
    assert "2 more flipped between repetitions" in reason
    assert "instability and not damage" in reason


def test_a_report_with_no_per_question_entries_says_unstable_is_unknown():
    """The CSV runner makes one pass, so it cannot tell a flip from a break.
    None, not 0, because the report does not know the difference."""
    from context_lift.ablate import safety
    from context_lift.metrics import McNemarResult

    class OnePass:
        per_question = ()
        broke_ids = ("q1",)
        n_defended = 12

        @property
        def defended(self):
            return McNemarResult(b=1, c=4, p_value=0.375)

    result = safety(OnePass())
    assert result.unstable is None
    assert (result.fixed, result.broke, result.n) == (4, 1, 12)


def test_the_fixed_and_broken_counts_reconcile_with_the_rate_difference():
    """fixed - broke == (with context right) - (without context right).

    Asked for by an external reviewer as a report invariant. It holds by
    construction here, because both sides are derived from the same aligned
    pair of correctness vectors - but nothing asserted it, and the check is
    cheap insurance for a report loaded from disk or produced by another
    runner.

    The identity is stated per COMPARISON and per SET. It does NOT hold across
    different question sets, and it does not hold if a question is scored by
    only one arm: an unpaired question moves one arm's numerator without
    entering either discordant count. That case is the reason to state the
    invariant over the PAIRED questions rather than over the report's rates.
    """
    from context_lift.ablate import ablate

    #            q0     q1     q2     q3     q4
    without = [True,  False, False, True,  False]
    with_it = [False, True,  True,  True,  False]

    def report(flags):
        return _answered([(f"q{i}", ok, f"a{i}") for i, ok in enumerate(flags)])

    result = ablate(report(without), report(with_it))

    fixed, broke = result.mcnemar.c, result.mcnemar.b
    right_without = sum(without)
    right_with = sum(with_it)

    assert (fixed, broke) == (2, 1)
    assert fixed - broke == right_with - right_without == 1
