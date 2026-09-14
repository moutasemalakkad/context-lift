"""Paired comparison of eval runs under different context conditions.

`ablate` answers "what is the context worth?" — the question a data team asks
before paying to curate anything. Run the same questions twice, once with the
context under test and once without, and it reports both rates with intervals
plus whether the difference is real.

`climb` answers the follow-up: curation is iterative, so several rounds of
enrichment produce several reports, and what a reviewer needs is the whole
progression — how much each round bought, and whether any round made things
worse.

Paired, not two independent samples: the same questions run through every arm,
so McNemar's exact test applies and only the disagreements carry signal. That
is more sensitive than comparing two intervals by eye — two overlapping
intervals can still hide a significant paired difference.
"""
from __future__ import annotations

import dataclasses

import math
from dataclasses import dataclass

from .harness import EvalReport, QuestionResult
from .metrics import (McNemarResult, Proportion, mcnemar_exact,
                      mcnemar_from_vectors, paired_difference_interval, proportion)
from .triage import triage as triage_failures

# questions_to_prove stops looking here; a lift that needs more is one the
# next answer key will not reach either
PROVE_CAP = 5000


# A one-sided McNemar with b = 0 and c = k has p = 2 ** (1 - k), so p < 0.05
# needs k >= 6. Six discordant pairs, all moving the same way, is the floor
# under every resolution claim here.
DISCORDANT_PAIRS_FOR_SIGNIFICANCE = 6

# The lift a round is trying to be able to see, in percentage points. Stated,
# not implied: "we found nothing" means nothing until the size it could have
# found is on the page.
DEFAULT_TARGET_PP = 10.0


def questions_for_target(target_pp: float,
                         cap: int = PROVE_CAP) -> int | None:
    """The fewest questions in which a `target_pp` one-sided lift reaches p<0.05.

    This is the BEST case and is a lower bound, which the caller must say out
    loud. It assumes every question the context changed was fixed and none was
    broken: b = 0, c = target * n. A real round with even one break needs more.

    At 10 points that is 60 questions, at 20 points 30, at 25 points 24. A
    24-question holdout therefore cannot resolve a lift under about 25 points
    however the run goes, which is why the number is printed beside every
    "not proven" rather than left for a reader to work out.

    Distinct from `questions_to_prove`, which scales the b and c a run
    actually saw. That answers "how much bigger would this same swing need to
    be"; this answers "what could this set ever have seen". Both are printed.
    """
    if target_pp <= 0:
        return None
    needed = DISCORDANT_PAIRS_FOR_SIGNIFICANCE / (target_pp / 100.0)
    size = int(math.ceil(needed))
    return size if size <= cap else None


@dataclass(frozen=True)
class Verdict:
    """The one word, and everything a reader needs to argue with it.

    `headline` is what the page prints instead of the scores when the word is
    INVALID: a failed gate is the finding, and printing the numbers beside it
    invites someone to quote them.
    """

    word: str
    p_value: float | None
    reason: str
    headline: str = ""
    questions_needed: int | None = None
    resolution: float | None = None

    @property
    def scores_are_publishable(self) -> bool:
        """False when a gate failed. The numbers exist; they are not evidence."""
        return self.word != "INVALID"


def verdict(holdout: AblationReport,
            curate: AblationReport | None = None,
            gates: "list[tuple[str, bool, str]] | None" = None,
            target_pp: float = DEFAULT_TARGET_PP) -> Verdict:
    """The verdict word, in the one order that cannot print a flattering lie.

    The order matters more than any single test, because a real customer page
    once read REAL GAIN off a curate lift of +60.6 points while the holdout
    scored 2 of 18 in both arms, a lift of exactly zero. Every rule below
    exists to make that page impossible:

    1. INVALID. A gate failed. The gate is the headline and the scores are not
       shown, because a number from a run that failed its own checks will be
       quoted without the caveat.
    2. REAL GAIN or REAL LOSS. The one holdout read is significant. The sign
       picks the word.
    3. OVERFIT. The curate set moved, and the DIFFERENCE between it and the
       holdout excludes zero. This is the case that used to print REAL GAIN.
       More questions do not fix it. The difference is the test, not the two
       significance results beside each other: "one is significant and the
       other is not" is a fallacy, and it made this rule print OVERFIT on a
       page whose own gap line said there was no evidence of a fit.
    4. UNDERPOWERED. The set is too small to have seen a `target_pp` lift even
       in the best case, whatever it did see. The number of questions that
       could is printed.
    5. NO CHANGE. The set WAS big enough, and the context flipped nothing
       either way. Only here is "nothing moved" a finding rather than a guess.
    6. NOT PROVEN. The measurement was valid and big enough, and the effect was
       not shown. This is the normal input to the next round, not a failure.

    `curate` is optional only so a caller with one arm pair can still get a
    word; without it OVERFIT can never be reached, which is stated in the
    reason rather than hidden.
    """
    for name, passed, detail in (gates or []):
        if not passed:
            return Verdict(
                word="INVALID", p_value=None,
                reason=f"the {name} gate failed: {detail}",
                headline=f"{name}: {detail}")

    holdout_test = holdout.clustered or holdout.mcnemar
    holdout_moved = (holdout.mcnemar.b + holdout.mcnemar.c > 0
                     and holdout_test.p_value < 0.05)
    if holdout_moved:
        word = "REAL GAIN" if holdout.lift > 0 else "REAL LOSS"
        return Verdict(
            word=word, p_value=holdout_test.p_value,
            reason=(f"the held-out questions moved {holdout.lift * 100:+.1f} points "
                    f"(p = {holdout_test.p_value:.4f}), and they were read once"))

    if curate is not None:
        curate_test = curate.clustered or curate.mcnemar
        # The working set moving while the held-out set does not is NOT a test
        # that the two differ. "One is significant and the other is not" is the
        # difference-in-significance fallacy, and this rule was built on it.
        # Measured on 2026-09-10: working +20.8 and significant, held out +13.3
        # with 5 fixed and 1 broke at p = 0.219, and the word came out OVERFIT
        # while the gap printed on the same page read [-12.1, +26.9] and said
        # there was no evidence of a fit at all. The page contradicted itself.
        #
        # The gap IS the test: it measures the difference and carries its own
        # interval. So OVERFIT now needs that interval to exclude zero, and a
        # run where it does not falls through to UNDERPOWERED or NOT PROVEN,
        # which is what a difference nobody could demonstrate deserves.
        # `defended`, NOT `mcnemar`, and the two differ exactly when there
        # are repetitions. THE BUG THIS FIXES, found 2026-09-14 by an outside
        # review reading the code: `mcnemar.b/c` are the POOLED per-ANSWER
        # discordant counts while `n_defended` is the per-QUESTION
        # denominator. At K = 3 the counts are three times larger and the
        # denominator is not, so the interval narrows by about sqrt(K) and
        # `gap_low` crosses above zero on a gap that genuinely includes it.
        #
        # The page then contradicted itself, exactly as it did on 2026-09-10
        # for a different reason. Reproduced at 18 questions x 3 reps, 6
        # working fixed and 2 held out fixed: the verdict printed OVERFIT
        # while `RoundResult.gap()` on the line below printed
        # +22.2 [-4.0, +48.4], which includes zero. At K = 1 the same fixture
        # printed UNDERPOWERED, which is why 1,788 tests stayed green -- the
        # suite never used more than one repetition.
        #
        # `gap()` already reads `defended`. Reading anything else here is how
        # one page came to hold two opposite verdicts, so the fix is to make
        # the verdict quote the number the reader is shown.
        from .metrics import independent_difference_interval
        gap_low, _gap_high = independent_difference_interval(
            (curate.defended.b, curate.defended.c, curate.n_defended),
            (holdout.defended.b, holdout.defended.c, holdout.n_defended))
        if (curate.mcnemar.b + curate.mcnemar.c > 0
                and curate_test.p_value < 0.05 and curate.lift > 0
                and gap_low > 0):
            return Verdict(
                word="OVERFIT", p_value=holdout_test.p_value,
                reason=(f"the working questions moved {curate.lift * 100:+.1f} points, "
                        f"the held-out ones {holdout.lift * 100:+.1f}, and the "
                        f"difference between the two excludes zero "
                        f"({gap_low * 100:+.1f} points at worst). "
                        "More questions do not fix this; the context fits the "
                        "questions it was written from"))

    n = holdout.n_questions or holdout.n_paired
    needed = questions_for_target(target_pp)
    resolution = (n / needed) if needed else None

    # UNDERPOWERED comes before NO CHANGE, which is the opposite of the first
    # draft. The first live round is why. Six held-out questions flipped
    # nothing, while 6 of 20 curate questions flipped, so six questions would
    # have been expected to flip about two. Seeing none out of six is ordinary
    # luck, and calling that "the context changed nothing" states a finding
    # the set could not support. A set too small to have seen anything is
    # UNDERPOWERED whatever it saw.
    if needed and n < needed:
        return Verdict(
            word="UNDERPOWERED", p_value=holdout_test.p_value,
            reason=(f"{n} held-out questions cannot show a {target_pp:.0f}-point "
                    f"lift even if nothing broke; about {needed} would. Nothing "
                    "here says the context does not help"),
            questions_needed=needed, resolution=resolution)

    if holdout.mcnemar.b + holdout.mcnemar.c == 0:
        return Verdict(
            word="NO CHANGE", p_value=holdout_test.p_value,
            reason=(f"{n} held-out questions were enough to see a "
                    f"{target_pp:.0f}-point lift, and the context changed none "
                    "of them, either way"),
            questions_needed=needed, resolution=resolution)

    return Verdict(
        word="NOT PROVEN", p_value=holdout_test.p_value,
        reason=(f"{n} held-out questions could have shown a {target_pp:.0f}-point "
                f"lift and did not (p = {holdout_test.p_value:.4f})"),
        questions_needed=needed, resolution=resolution)


@dataclass(frozen=True)
class Safety:
    """Did the context break anything. A count, not an estimate.

    This is deliberately NOT a verdict word, and the difference is the whole
    reason it exists. A lift is a claim about questions nobody has seen, so
    it needs a test, an interval and a p-value. "broke 0 of 18" is a count of
    what happened to these eighteen. There is nothing to estimate, so there
    is no test to run and no interval to print.

    The framework computed both numbers from the first round onward and
    reported only the first. A round that fixed five questions and broke none
    printed an amber NOT PROVEN, and a reader learned nothing about whether
    the context was safe to keep. Those are two decisions: the lift says
    whether the context is worth keeping, and this says whether keeping it
    costs anything.

    `unstable` is separate from `broke` on purpose. With K repetitions a
    question can flip on unchanged context, and `per_question_status` already
    names that case. Counting it as broken overstates the damage; folding it
    into safe hides it. It is None when the report carries no per-question
    entries, so the line can say "not recorded" rather than claim zero.
    """

    word: str
    fixed: int
    broke: int
    unstable: int | None
    n: int
    broke_ids: tuple[str, ...]
    reason: str
    # Which half of the split this counts. "broke 0 on the reserved questions"
    # is the real claim; the working questions are the ones the rules were
    # written from, so the same zero there means less. A line that does not
    # say which half it read is a line a reader will assume is the strong one.
    half: str = ""

    @property
    def safe(self) -> bool:
        return self.broke == 0


def safety(report: AblationReport, half: str = "") -> Safety:
    """What the context did to the questions in front of it, counted.

    Counted from `per_question`, one entry per base question, NOT from
    `broke_ids`. Those ids carry a `#rep` suffix per repetition, so a
    question that flipped down in one run of three appears there and would be
    reported as broken when `per_question_status` calls it unstable. Three
    repetitions of one question are one question.

    Falls back to the defended test's discordant counts when the report has
    no per-question entries, which is the case for a CSV runner's single pass
    and for reports written before the field existed. `unstable` is then None
    rather than 0, because the report cannot tell the difference.
    """
    if report.per_question:
        counts: dict[str, int] = {}
        for entry in report.per_question:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        fixed = counts.get("fixed", 0)
        broke = counts.get("broke", 0)
        unstable: int | None = counts.get("unstable", 0)
        n = len(report.per_question)
        broke_ids = tuple(e["id"] for e in report.per_question
                          if e["status"] == "broke")
    else:
        test = report.defended
        fixed, broke, unstable = test.c, test.b, None
        n = report.n_defended
        broke_ids = report.broke_ids

    word, reason = safety_word(fixed, broke, unstable, n, half)
    return Safety(word=word, fixed=fixed, broke=broke, unstable=unstable,
                  n=n, broke_ids=broke_ids, reason=reason, half=half)


def safety_word(fixed: int, broke: int, unstable: int | None, n: int,
                half: str = "") -> tuple[str, str]:
    """The safety word and its sentence, from counts alone.

    Split out from `safety` so the HTML page can call it. The page already
    tallies the per-question statuses for its own "3 fixed, 0 broke" line, so
    without this it would decide the word a second time and the two could
    drift. One rule, not two: the same shape produced a round that printed
    two different `broke` numbers on two surfaces.
    """
    where = f" on the {half} questions" if half else ""
    if broke == 0:
        word = "SAFE"
        reason = (f"{fixed} fixed, 0 broke, of {n}{where}. Nothing that was "
                  "right without the context became wrong with it")
    elif fixed > broke:
        word = "MIXED"
        reason = (f"{fixed} fixed, {broke} broke, of {n}{where}. Read the "
                  "broken ones before keeping this context. A net gain that "
                  "costs working answers is a trade, not a win")
    else:
        word = "REGRESSION"
        reason = (f"{fixed} fixed, {broke} broke, of {n}{where}. The broken "
                  "ones were right WITHOUT the context. Revert it, or write "
                  "a reason against each one")
    if unstable:
        reason += (f"; {unstable} more flipped between repetitions on unchanged "
                   "context, which is instability and not damage")
    return word, reason


# What a word from one arm pair, with no gates and no working set, cannot be.
# Printed beside such a word so nobody quotes it as a round's verdict: INVALID
# needs gates, and OVERFIT needs a working set to compare the held-out one
# against. A page that shows REAL GAIN without saying so is the exact shape of
# the customer page that read REAL GAIN on a curate lift of +60.6 points with
# a flat holdout.
UNGATED_WORD = ("This word comes from one pair of arms with no gates and no "
                "working set beside it, so it cannot be INVALID and cannot be "
                "OVERFIT. Run `contextlift round` for a word that can.")


def verdict_word(ablation: AblationReport) -> tuple[str, float]:
    """The one word a reader takes away, and the p-value it rests on.

    The clustered-by-question p is the one to defend when repetitions were
    pooled; the pooled p overstates independent evidence. `ablate`, `climb`
    and `bundle` all call this, so the terminal and verdict.json cannot
    print different words for one comparison.

    One rule, not two. This delegates to `verdict` rather than deciding
    anything itself, because two implementations of one rule drift: the same
    shape produced a row-cap bug where a truncated result was compared as if
    it were whole. A caller with a curate set, gates and a target should call
    `verdict` directly and get the words this one cannot reach.
    """
    # OVERFIT is the one word this cannot reach: it needs a curate set, and a
    # caller with one arm pair has none. Every other word is returned as it
    # comes, UNDERPOWERED included, because a set too small to have seen
    # anything must not be reported as having seen nothing.
    result = verdict(ablation)
    return result.word, result.p_value


@dataclass(frozen=True)
class AblationReport:
    """Two arms of the same question set, compared pairwise.

    `lift` is `with_context.point - without_context.point` in percentage
    points of the rate (0.34 means 34 points). `mcnemar.b` counts questions the
    context BROKE — right without it, wrong with it — which is the number worth
    reading even when the overall lift is positive.
    """

    without_context: Proportion
    with_context: Proportion
    lift: float
    mcnemar: McNemarResult
    n_paired: int
    adjusted: bool
    # The ids behind mcnemar.b and mcnemar.c. "broke 3" without WHICH three
    # sends the developer back to grepping two score reports by hand; these
    # are the whole point of running a paired test on named questions.
    broke_ids: tuple[str, ...] = ()
    fixed_ids: tuple[str, ...] = ()
    # Questions one arm scored and the other excluded, dropped from the pair.
    # Carried because a lift over 16 questions when the run asked 18 is a
    # different claim, and a reader who is not told cannot know which they
    # are holding.
    unpaired_ids: tuple[str, ...] = ()
    # Set when the arms carry pooled repetitions. Reps of one question share
    # its difficulty, so pooled discordant counts overstate the independent
    # evidence; the clustered test collapses each question to a majority vote
    # (ties drop out) and is the conservative read.
    n_questions: int | None = None
    n_reps: int | None = None
    clustered: McNemarResult | None = None
    # One entry per base question: {"id", "status", "base": [..], "ctx": [..]}
    # with the per-repetition correctness of each arm. `status` is one of
    # fixed / broke / unstable / right / wrong (see per_question_status), so
    # a viewer never re-derives it from the id lists.
    per_question: tuple[dict, ...] = ()
    # Answers that changed at all, against answers that changed correctness.
    # On one estate 39 of 123 routing answers moved between two rounds and 4
    # changed the verdict; the other 35 went from one wrong table to another.
    # A verdict alone cannot show that. None when the reports carry no answers.
    churn: dict | None = None
    # True when every question's answers are identical across repetitions in
    # both arms: the reps are copies of one run, not samples, and the
    # per-question test is the only honest reading. None without reps or
    # without answers.
    reps_identical: bool | None = None
    # The share of questions whose CORRECTNESS is the same in every repetition,
    # in both arms. This is the number that should decide K, not the one above.
    # Measured 2026-09-09 on two runs of identical arms against the
    # Conversational Analytics API: 0 of 52 answers were the same SQL, and 50
    # of 52 kept their correctness. So `reps_identical` can never be true
    # against that agent while the score it feeds is 96% stable, and one
    # question flipping in opposite directions in the two arms moved a
    # 20-question lift by 10 points. None without reps or without correctness.
    reps_correctness_stable: float | None = None
    # How the failures were shaped in each arm, {"without": {bucket: n},
    # "with": {...}}, in triage's buckets. The only thing left to read when
    # no answer became correct.
    failure_shapes: dict | None = None

    @property
    def significant(self) -> bool:
        """True at the conventional 5% threshold on the pooled test. A
        convenience, not a verdict - report the p-value itself, and prefer
        `defended` when there are repetitions."""
        return self.mcnemar.p_value < 0.05

    @property
    def defended(self):
        """The test to quote: each question counted once when there are
        repetitions, the pooled test when there is one run per arm.

        Repeated answers to the same question are not independent, so the
        pooled test's denominator is inflated and its p-value is optimistic.
        A field run measured McNemar p = 0.0156 against a clustered p = 0.5000
        on the same data - 2 questions of 19 had moved.
        """
        return self.clustered if self.clustered is not None else self.mcnemar

    @property
    def n_defended(self) -> int:
        """The pair count behind `defended`: questions when repetitions were
        pooled, observations otherwise."""
        if self.clustered is not None:
            return self.n_questions or self.n_paired
        return self.n_paired

    def lift_interval(self, confidence: float = 0.95) -> tuple[float, float]:
        """The interval the lift line prints, from the defended test's
        discordant counts: per question when repetitions were pooled."""
        test = self.defended
        return paired_difference_interval(test.b, test.c, self.n_defended, confidence)

    def verdict_sentences(self) -> list[str]:
        """The next step, one sentence, keyed by the verdict word.

        Not proven: how big a lift this many questions can prove at all, and
        how many questions the lift seen here would need. No change: where to
        look instead. A real loss: the broken questions, by name, before
        anything else. A real gain: the questions it broke on the way, or
        what to read before quoting a clean one. Every word gets a sentence,
        because `ablate`, `climb` and verdict.html print the ruling and then
        this.
        """
        word, _p = verdict_word(self)
        test = self.defended
        n = self.n_defended
        if word == "NO CHANGE":
            return [f"{n} questions were enough to see a {DEFAULT_TARGET_PP:.0f}-point "
                    "lift and the arms never disagreed: check the routing number "
                    "first (score-routing), then the triage of the baseline"]
        if word == "REAL LOSS":
            return [f"a real loss: the context broke {self._named_broke()}"
                    "; read those before anything else"]
        if word == "REAL GAIN":
            if test.b:
                return [f"a real gain, but the context also broke {self._named_broke()}; "
                        "read those before quoting the lift"]
            return [f"a real gain: {test.c} of {n} questions moved up and none moved down; "
                    "read a few of them beside their gold before quoting the number"]
        floor = ladder_step(n)
        if self.lift < 0:
            return [f"at {n} questions this set can only prove a change above about "
                    f"{floor * 100:.0f} points (1/sqrt({n})); the {self.lift:+.1%} seen here "
                    "is inside that"]
        needed = questions_to_prove(test.b, test.c, n)
        if test.b == test.c == 0:
            tail = "the two arms never disagreed on a single question"
        elif test.b == test.c:
            tail = ("as many questions broke as were fixed, so no number of questions "
                    "would prove a lift of this shape")
        elif needed is None:
            tail = (f"to prove the {self.lift:+.1%} seen here you would need more than "
                    f"{PROVE_CAP} questions")
        else:
            tail = (f"to prove the {self.lift:+.1%} seen here you would need about "
                    f"{needed} questions")
        # Two numbers, and they answer different questions. `questions_to_prove`
        # scales the swing this run actually saw. `questions_for_target` says
        # what the set could ever have seen, whatever it saw. Printing only one
        # of them is how a reader concludes the wrong thing from a small set.
        head = (f"at {n} questions this set can only prove a lift above about "
                f"{floor * 100:.0f} points (1/sqrt({n}))")
        if word == "UNDERPOWERED":
            could = questions_for_target(DEFAULT_TARGET_PP)
            head = (f"at {n} questions this set could not have shown a "
                    f"{DEFAULT_TARGET_PP:.0f}-point lift even if nothing broke, and "
                    f"about {could} questions could")
        return [f"{head}; {tail}"]

    def failure_shape_lines(self, cap: int = 6) -> list[str]:
        """What moved when the score could not move: the failure shapes of
        both arms, side by side.

        A round where five of eighteen questions stopped returning empty
        results and two started printed as "the arms never disagreed", which
        is true about correctness and reads as "nothing happened". Execution
        accuracy was 0.000 before and 0.000 after, so it had no resolution to
        show the change with. The buckets are `triage`'s, not new names.

        Printed only when the comparison cannot speak: no correct answer in
        either arm, or no question changing correctness while the shapes moved.
        """
        if not self.failure_shapes:
            return []
        before = self.failure_shapes.get("without") or {}
        after = self.failure_shapes.get("with") or {}
        both_zero = bool(self.n_paired) and not (self.without_context.point
                                                 or self.with_context.point)
        unchanged = self.mcnemar.b + self.mcnemar.c == 0
        if not (both_zero or (unchanged and before != after)):
            return []
        head = ("no answer became correct in either arm, so this score has no resolution here"
                if both_zero else
                "no question changed correctness, so the rates say nothing")
        if before == after:
            return [f"  {head}; the failure shapes did not move either"]
        shapes = sorted(set(before) | set(after),
                        key=lambda name: (-abs(after.get(name, 0) - before.get(name, 0)), name))
        lines = [f"  {head}; the failure shapes did move:"]
        for name in shapes[:cap]:
            was, now = before.get(name, 0), after.get(name, 0)
            lines.append(f"    {name:<14} {was:>4} -> {now:<4} {now - was:+d}")
        if len(shapes) > cap:
            lines.append(f"    and {len(shapes) - cap} more shapes; triage prints them all")
        return lines

    def _named_broke(self, cap: int = 8) -> str:
        """The questions the context broke, by name, with a rep count each."""
        collapsed = _collapse_reps(self.broke_ids)
        names = ", ".join(f"{base}{f' x{k}' if k > 1 else ''}" for base, k in collapsed[:cap])
        more = len(collapsed) - cap
        return (names or "no question") + (f" and {more} more" if more > 0 else "")

    def summary(self) -> str:
        """One block a human reads without decoding the dataclass."""
        arm = "judge-adjusted" if self.adjusted else "execution"
        if self.n_questions and self.n_reps and self.n_reps > 1:
            headline = (f"paired ablation on {self.n_paired} observations "
                        f"({self.n_questions} questions x {self.n_reps} reps, "
                        f"{arm} accuracy)")
        else:
            headline = (f"paired ablation on {self.n_paired} questions "
                        f"({arm} accuracy)")
        # Said in the headline, not a footnote. A lift over 16 questions when
        # the run asked 18 is a different claim from a lift over 18, and the
        # reader who is not told cannot know which one they are holding.
        if self.unpaired_ids:
            dropped = len(self.unpaired_ids)
            headline += (f", after dropping {dropped} that only one arm "
                         f"scored ({', '.join(self.unpaired_ids[:3])}"
                         + (", ..." if dropped > 3 else "") + ")")
        # with reps, b and c count (question, rep) pairs, not questions;
        # calling them questions reads as 36 of 18 questions fixed
        unit = "observations" if (self.n_reps or 1) > 1 else "questions"
        # Whichever number is printed first and carries a verdict is the one
        # that gets quoted. That used to be the pooled test, annotated
        # "(significant at 0.05)", above an unannotated clustered line, and
        # readers led with the optimistic number. The defended test goes
        # first now and both carry their own verdict.
        def _ruling(test) -> str:
            return "significant at 0.05" if test.p_value < 0.05 else "not significant at 0.05"

        low, high = self.lift_interval()
        lines = [
            headline,
            f"  without context : {self.without_context.point:6.1%}  "
            f"[{self.without_context.low:.1%}, {self.without_context.high:.1%}]",
            f"  with context    : {self.with_context.point:6.1%}  "
            f"[{self.with_context.low:.1%}, {self.with_context.high:.1%}]",
            f"  lift            : {self.lift:+6.1%}  [{low:+.1%}, {high:+.1%}]   "
            f"(the two rates subtracted; 95% interval on the paired difference"
            + (", per question" if (self.n_reps or 1) > 1 else "") + ")",
            "",
        ]
        if self.clustered is not None and (self.n_reps or 1) > 1:
            lines += [
                "  each question counted once - the number to defend",
                f"    fixed {self.clustered.c} / broke {self.clustered.b} "
                f"of {self.n_questions} questions",
                f"    p = {self.clustered.p_value:.4f}   {_ruling(self.clustered)}",
                "",
                "  each answer counted separately - optimistic, because repeated",
                "  answers to one question are not independent",
                f"    fixed {self.mcnemar.c} / broke {self.mcnemar.b} "
                f"of {self.n_paired} observations",
                f"    p = {self.mcnemar.p_value:.4f}   {_ruling(self.mcnemar)}",
            ]
        else:
            lines += [
                f"  context fixed   : {self.mcnemar.c} {unit}",
                f"  context broke   : {self.mcnemar.b} {unit}",
                f"  McNemar p       : {self.mcnemar.p_value:.4f}   {_ruling(self.mcnemar)}",
            ]
            # The caveat prints beside the number it qualifies. It used to live
            # on the step a person could skip, and skipping the step skipped the
            # warning. One run per arm is a sample: a field router moved five
            # points between identical runs. It reads the pooled repetition
            # count, not "no clustered test": three reports per arm restricted
            # to ids none of them held printed this over 0 questions.
            if self.n_paired and (self.n_reps or 1) == 1:
                lines.append("  one repetition per arm: a single sample, not a measurement; "
                             "run each arm again before quoting this"
                             + (" (per-question and per-answer readings cannot differ here)"
                                if self.clustered is not None else ""))
        if self.reps_identical:
            lines.append("  repetitions are identical within each arm: copies, not samples; "
                         "the per-question test is the only reading and no interval widens for them")
        if self.churn is not None:
            same = self.churn["changed"] - self.churn["changed_verdict"]
            lines.append(f"  answers changed at {self.churn['changed']} of {self.churn['pairs']} "
                         f"pairs; {self.churn['changed_verdict']} changed the verdict, "
                         f"{same} moved without changing it")
        if self.without_context.point == 0 or self.with_context.point == 0:
            # three consecutive arms at 0 of 18 on one estate read as a finding
            # about the agent; they were an environment variable in the runner
            lines.append("  an arm with 0 correct is usually a setup fault (dialect, the model's "
                         "settings, the wrong dataset), not a score; read one prediction beside "
                         "its gold before believing this comparison")
        lines += self.failure_shape_lines()
        # the word a reader takes away, from the same function bundle's
        # verdict.json uses, then the one next step it asks for
        lines.append("")
        lines.append(f"  ruling          : {verdict_word(self)[0]}")
        for sentence in self.verdict_sentences():
            lines.append(f"  {sentence}")
        if self.mcnemar.b + self.mcnemar.c == 0:
            lines.append("  the two arms never disagreed — the context changed nothing")
        else:
            lines.append("")
            lines.append(
                "  how to read this: an 'arm' is one condition (context on or\n"
                "  off) and lift is one rate minus the other, in percentage\n"
                "  points. Both p-values are McNemar's paired test - the chance\n"
                "  a difference this size is luck - and they differ only in what\n"
                "  counts as one observation. Answering a question three times\n"
                "  gives three answers, not three independent facts, so the\n"
                "  per-answer test has an inflated denominator and reads low.\n"
                "  Quote the per-question number. If the two disagree, that gap\n"
                "  is the result: a handful of questions moved.")
        return "\n".join(lines)

    def flips_summary(self) -> str:
        """Name every question the context broke or fixed.

        Pooled reps carry ids like `dw036#rep1`; those collapse to the base id
        with a rep count, so `dw036 x3` means the flip happened in all three
        repetitions — a consistent effect, not sampling noise.
        """
        broke = dict(_collapse_reps(self.broke_ids))
        fixed = dict(_collapse_reps(self.fixed_ids))
        both = set(broke) & set(fixed)
        lines = []
        for label, collapsed in (("broke", broke), ("fixed", fixed)):
            lines.append(f"context {label} ({sum(collapsed.values())}):")
            for base, count in sorted(collapsed.items(), key=lambda kv: (-kv[1], kv[0])):
                suffix = f"  x{count}" if count > 1 else ""
                note = "  <- flipped between repetitions: unstable, not a real fix or break" \
                    if base in both else ""
                lines.append(f"  {base}{suffix}{note}")
            if not collapsed:
                lines.append("  (none)")
        return "\n".join(lines)


def _collapse_reps(ids: tuple[str, ...]) -> list[tuple[str, int]]:
    """Group pooled observation ids by their base question id.

    `dw036#rep0, dw036#rep2, dw080#rep1` -> [(dw036, 2), (dw080, 1)].
    Sorted by count descending so the most consistent flips read first.
    """
    counts: dict[str, int] = {}
    for observation_id in ids:
        base = observation_id.split("#rep")[0]
        counts[base] = counts.get(base, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def pool(reports: list[EvalReport]) -> EvalReport:
    """Combine repeated runs of the same questions into one report.

    Temperature 0 is not reproducible on hosted models, so a single run is a
    sample, not a measurement. Measured on one engagement: the same arm scored
    10.5%, 5.3% and 15.8% across three identical runs, and NO single rep reached
    significance against its control (p = 0.50, 0.25, 1.00). Pooled into 57
    paired observations the same comparison gave p = 0.031.

    Each (question, rep) becomes one observation, with the rep in the id so the
    paired test lines arms up correctly.

    **Pooling does NOT make McNemar valid, and an earlier version of this
    docstring claimed it did.** Repeated answers to one question are
    correlated, so counting them as independent observations inflates the
    denominator and the p-value is optimistic. The p = 0.031 above is that
    optimistic number: 19 questions × 3 reps read as 57 independent pairs. On
    the same run the clustered test — one vote per question — gave p = 0.5000
    against a pooled 0.0156 (see `AblationReport.defended`).

    So what pooling is FOR is the rate and the per-question vector, not the
    p-value. `defended` returns the clustered test whenever reps exist, and
    that is the number every verdict rests on. The pooled test is printed
    beside it, labelled as overstating the evidence, and is never the primary
    claim. Reviewed 2026-09-12 after an external reader took this docstring at
    its word and concluded the tool reports an invalid p; it does not, but the
    docstring said it should.

    Raises:
        ValueError: on an empty list, or if the reports scored different
            questions — pooling those would silently compare different sets.
    """
    if not reports:
        raise ValueError("pool needs at least one report")
    signature = [r.id for r in reports[0].results]
    for index, report in enumerate(reports[1:], start=1):
        if [r.id for r in report.results] != signature:
            raise ValueError(
                f"rep {index} scored different questions from rep 0; pooling "
                "them would compare different sets"
            )

    results: list[QuestionResult] = []
    for rep_index, report in enumerate(reports):
        for item in report.results:
            # replace, not a field-by-field copy: the copy silently dropped
            # every field added after it was written (rows_preview, answer)
            results.append(dataclasses.replace(item, id=f"{item.id}#rep{rep_index}"))
    scored = [r for r in results if r.execution_correct is not None]
    adjusted_scored = [r for r in results if r.adjusted_correct is not None]
    return EvalReport(
        results=results,
        n_total=len(results),
        n_scored=len(scored),
        n_excluded=len(results) - len(scored),
        execution=proportion(sum(1 for r in scored if r.execution_correct), len(scored)),
        adjusted=proportion(
            sum(1 for r in adjusted_scored if r.adjusted_correct), len(adjusted_scored)),
    )


# Above this share of questions scored in only one arm, the paired set is not
# the set anybody means and the run is refused. A row cap excluding one arm's
# prediction is ordinary and rare; an arm that stopped early is neither.
UNPAIRED_LIMIT = 0.20


def _restricted(report: EvalReport, keep: set[str]) -> EvalReport:
    """The same report over a subset of its questions, rates rebuilt.

    Rates are stored fields, not properties, so filtering `results` alone
    would leave an accuracy computed over questions that are no longer in
    the comparison. `_report_from_results` is the one definition of how a
    rate comes from results, so it is reused rather than repeated here.
    """
    from .harness import _report_from_results
    return _report_from_results([r for r in report.results if r.id in keep])


def _scored_ids(report: EvalReport) -> list[str]:
    """Ids that produced a rate, in report order.

    Mirrors the filter in EvalReport.correctness_vector so the ids and the
    correctness values line up position by position.
    """
    return [r.id for r in report.results if r.execution_correct is not None]


def _scored_ids_adjusted(report: EvalReport) -> list[str]:
    return [r.id for r in report.results if r.adjusted_correct is not None]


def _answers(report: EvalReport, adjusted: bool) -> list[str | None]:
    """The scored answers in the same order and under the same exclusions as
    correctness_vector, so the two line up by position."""
    field = "adjusted_correct" if adjusted else "execution_correct"
    return [getattr(r, "answer", None) for r in report.results
            if getattr(r, field) is not None]


def _churn(answers_a, answers_b, vector_a, vector_b) -> dict | None:
    """Pairs whose answer text differs (whitespace-insensitive), and how many
    of those also changed correctness. None when neither arm carries answers,
    which is every report saved before the field existed."""
    if all(a is None for a in answers_a) and all(b is None for b in answers_b):
        return None
    changed = changed_verdict = 0
    for a, b, right_a, right_b in zip(answers_a, answers_b, vector_a, vector_b):
        if a is None or b is None or " ".join(a.split()) == " ".join(b.split()):
            continue
        changed += 1
        if right_a != right_b:
            changed_verdict += 1
    return {"pairs": len(vector_a), "changed": changed, "changed_verdict": changed_verdict}


def _failure_shapes(without_context: EvalReport, with_context: EvalReport,
                    adjusted: bool) -> dict:
    """Each arm's failures counted by triage bucket, under the same reading of
    correctness the comparison used.

    Triage already names the shapes, so nothing new is invented here; the two
    tallies are what the paired test cannot see when no answer moved.
    """
    return {"without": dict(triage_failures(without_context, adjusted=adjusted).counts),
            "with": dict(triage_failures(with_context, adjusted=adjusted).counts)}


def _reps_correctness_stable(ids: list[str], correct_a: list,
                             correct_b: list) -> float | None:
    """The share of questions scored the same way in every repetition.

    The honest repeat check for a model that rewrites its SQL every time.
    `_reps_identical` asks whether the text came back the same, which against a
    real agent it never does: 0 of 52 answers matched across two runs of
    identical arms on 2026-09-09. Correctness did, 50 of 52.

    So text sameness cannot say whether K=1 is enough, and this can. A question
    that scores differently between repetitions is noise the paired test has to
    carry; a set where every question scores the same way twice has none.
    """
    if not any("#rep" in qid for qid in ids):
        return None
    seen: dict[tuple[str, str], set] = {}
    for qid, a, b in zip(ids, correct_a, correct_b):
        base = qid.split("#rep")[0]
        seen.setdefault((base, "a"), set()).add(a)
        seen.setdefault((base, "b"), set()).add(b)
    if not seen:
        return None
    return sum(1 for v in seen.values() if len(v) == 1) / len(seen)


def _reps_identical(ids: list[str], answers_a: list, answers_b: list) -> bool | None:
    """Every question answered with the same TEXT in every repetition.

    Kept because an echo or a deterministic backend does repeat itself, and
    there it means the repetitions are copies rather than samples. Against a
    model it is always false and says nothing; `_reps_correctness_stable` is
    the one to read then.
    """
    if not any("#rep" in qid for qid in ids) or all(a is None for a in answers_a + answers_b):
        return None
    seen: dict[tuple[str, str], set[str]] = {}
    for qid, a, b in zip(ids, answers_a, answers_b):
        base = qid.split("#rep")[0]
        seen.setdefault((base, "a"), set()).add(" ".join((a or "").split()))
        seen.setdefault((base, "b"), set()).add(" ".join((b or "").split()))
    return all(len(v) == 1 for v in seen.values())


def ablate(
    without_context: EvalReport,
    with_context: EvalReport,
    adjusted: bool = False,
    pair_common: bool = False,
) -> AblationReport:
    """Compare two runs of the same questions under different context.

    Args:
        without_context: the baseline arm — run this FIRST, before curating.
            It cannot be recovered once the context exists.
        with_context: the same questions, same model, same everything except
            the context under test.
        adjusted: compare judge-adjusted correctness instead of raw execution.

    Raises:
        ValueError: if the two reports did not score the same questions.
            A paired test over different denominators is meaningless, and
            silently truncating to the overlap would hide the mismatch.
    """
    ids_a = _scored_ids_adjusted(without_context) if adjusted else _scored_ids(without_context)
    ids_b = _scored_ids_adjusted(with_context) if adjusted else _scored_ids(with_context)
    vector_a = without_context.correctness_vector(adjusted=adjusted)
    vector_b = with_context.correctness_vector(adjusted=adjusted)

    # `pair_common` is opt-in and the default is to REFUSE, because the two
    # callers want opposite things.
    #
    # Within ONE round, two arms answer the same golds in one process, so the
    # only way their scored sets differ is a per-arm exclusion -- a
    # prediction hits the row cap and is excluded while the other arm's
    # answer to the same question is fine. Throwing away a 45-minute round
    # over 2 of 18 questions is the wrong trade, so the round pairs what both
    # arms scored and says how many it dropped.
    #
    # ACROSS runs -- `climb` comparing rounds, `bundle` comparing saved
    # reports -- a differing set means the gold set itself changed, and then
    # the two numbers are not measuring the same thing at all. Those callers
    # keep the refusal.
    unpaired: tuple[str, ...] = ()
    if ids_a != ids_b and not pair_common:
        only_a = sorted(set(ids_a) - set(ids_b))
        only_b = sorted(set(ids_b) - set(ids_a))
        detail = []
        if only_a:
            detail.append(f"scored only without context: {only_a[:5]}")
        if only_b:
            detail.append(f"scored only with context: {only_b[:5]}")
        if not detail:
            detail.append("same ids, different order")
        raise ValueError(
            "the two arms did not score the same questions, so they cannot be "
            "paired. " + "; ".join(detail)
        )
    if ids_a != ids_b:
        only_a = sorted(set(ids_a) - set(ids_b))
        only_b = sorted(set(ids_b) - set(ids_a))
        if not (only_a or only_b):
            # Same questions, different order. That is a bug in whatever
            # built the reports, not a fact about the run, and pairing by
            # position would silently compare the wrong two answers.
            raise ValueError(
                "the two arms scored the same questions in different orders, "
                "so pairing by position would compare the wrong answers")
        unpaired = tuple(only_a + only_b)
        share = len(unpaired) / max(1, len(set(ids_a) | set(ids_b)))
        if share > UNPAIRED_LIMIT:
            raise ValueError(
                f"{len(unpaired)} of "
                f"{len(set(ids_a) | set(ids_b))} questions were scored in only "
                f"one arm ({share:.0%}), so the paired set is not the set "
                "anybody means. An arm that stopped early or a gold that "
                "changed between arms does this; a row cap does not do it "
                f"this often. First few: {unpaired[:5]}")
        # Keep only what BOTH arms scored, and rebuild each arm's rate over
        # that set. Excluding a question in one arm and not the other is
        # ordinary -- a prediction hits the row cap and is excluded, and the
        # other arm's answer to the same question is fine -- but a rate
        # computed over 18 questions and paired over 16 is two numbers
        # pretending to be one.
        common = set(ids_a) & set(ids_b)
        without_context = _restricted(without_context, common)
        with_context = _restricted(with_context, common)
        ids_a = _scored_ids_adjusted(without_context) if adjusted else _scored_ids(without_context)
        ids_b = _scored_ids_adjusted(with_context) if adjusted else _scored_ids(with_context)
        if ids_a != ids_b:
            raise ValueError(
                "restricting both arms to the questions they share did not "
                "make them pairable, which should be impossible")
        vector_a = without_context.correctness_vector(adjusted=adjusted)
        vector_b = with_context.correctness_vector(adjusted=adjusted)

    answers_a = _answers(without_context, adjusted)
    answers_b = _answers(with_context, adjusted)
    churn = _churn(answers_a, answers_b, vector_a, vector_b)
    reps_identical = _reps_identical(ids_a, answers_a, answers_b)
    reps_correctness_stable = _reps_correctness_stable(ids_a, vector_a, vector_b)

    rate_a = without_context.adjusted if adjusted else without_context.execution
    rate_b = with_context.adjusted if adjusted else with_context.execution

    broke = tuple(
        qid for qid, a, b in zip(ids_a, vector_a, vector_b) if a and not b
    )
    fixed = tuple(
        qid for qid, a, b in zip(ids_a, vector_a, vector_b) if not a and b
    )

    per_question_a: dict[str, list[bool]] = {}
    per_question_b: dict[str, list[bool]] = {}
    for qid, a, b in zip(ids_a, vector_a, vector_b):
        base = qid.split("#rep")[0]
        per_question_a.setdefault(base, []).append(a)
        per_question_b.setdefault(base, []).append(b)
    per_question = tuple(
        {"id": base, "status": per_question_status(per_question_a[base],
                                                   per_question_b[base]),
         "base": per_question_a[base], "ctx": per_question_b[base]}
        for base in per_question_a
    )

    n_questions = n_reps = None
    clustered = None
    if any("#rep" in qid for qid in ids_a):
        n_questions = len(per_question_a)
        n_reps = max(len(v) for v in per_question_a.values())

        def majority(votes: list[bool]) -> bool | None:
            yes = sum(votes)
            if yes * 2 == len(votes):
                return None  # tie: no majority, the question abstains
            return yes * 2 > len(votes)

        clustered_b = clustered_c = 0
        for base in per_question_a:
            majority_a = majority(per_question_a[base])
            majority_b = majority(per_question_b[base])
            if majority_a is None or majority_b is None:
                continue
            if majority_a and not majority_b:
                clustered_b += 1
            elif majority_b and not majority_a:
                clustered_c += 1
        clustered = mcnemar_exact(clustered_b, clustered_c)

    return AblationReport(
        without_context=rate_a,
        with_context=rate_b,
        lift=rate_b.point - rate_a.point,
        mcnemar=mcnemar_from_vectors(vector_a, vector_b),
        n_paired=len(vector_a),
        adjusted=adjusted,
        broke_ids=broke,
        fixed_ids=fixed,
        unpaired_ids=unpaired,
        churn=churn,
        reps_identical=reps_identical,
        reps_correctness_stable=reps_correctness_stable,
        failure_shapes=_failure_shapes(without_context, with_context, adjusted),
        n_questions=n_questions,
        n_reps=n_reps,
        clustered=clustered,
        per_question=per_question,
    )


def per_question_status(base: list[bool], ctx: list[bool]) -> str:
    """Name what the context did to one question across its repetitions.

    fixed    wrong -> right in more than half the reps, never the reverse
    broke    right -> wrong in more than half the reps, never the reverse
    unstable any other pattern that includes a flip, or an answer that
             varies between reps inside one arm (one run is a sample)
    right / wrong   no flip in any rep, same answer every time
    """
    n = len(base)
    flips_up = sum(1 for a, b in zip(base, ctx) if not a and b)
    flips_down = sum(1 for a, b in zip(base, ctx) if a and not b)
    if flips_up * 2 > n and flips_down == 0:
        return "fixed"
    if flips_down * 2 > n and flips_up == 0:
        return "broke"
    if flips_up or flips_down:
        return "unstable"
    if all(ctx):
        return "right"
    if not any(ctx):
        return "wrong"
    return "unstable"


@dataclass(frozen=True)
class Round:
    """One round of context enrichment, compared to the round before it and to
    the starting point.

    `vs_previous` is what THIS round of curation bought. `vs_baseline` is what
    all the curation so far has bought. They answer different questions and a
    reviewer needs both: a round can be worth nothing on its own while the
    cumulative total still looks healthy.
    """

    label: str
    rate: Proportion
    vs_previous: McNemarResult | None
    vs_baseline: McNemarResult | None
    gain_vs_previous: float
    gain_vs_baseline: float
    ladder_rate: float
    # Which questions this round broke/fixed relative to the round before it —
    # the blast radius of one round of curation, by name.
    broke_ids: tuple[str, ...] = ()
    fixed_ids: tuple[str, ...] = ()
    # Questions one arm scored and the other excluded, dropped from the pair.
    # Carried because a lift over 16 questions when the run asked 18 is a
    # different claim, and a reader who is not told cannot know which they
    # are holding.
    unpaired_ids: tuple[str, ...] = ()
    # Set when the arms carry pooled repetitions. Reps of one question share
    # its difficulty, so pooled discordant counts overstate the independent
    # evidence; the clustered test collapses each question to a majority vote
    # (ties drop out) and is the conservative read.
    n_questions: int | None = None
    n_reps: int | None = None
    clustered: McNemarResult | None = None

    @property
    def regressed(self) -> bool:
        """True if this round broke more questions than it fixed."""
        return self.vs_previous is not None and self.vs_previous.b > self.vs_previous.c


@dataclass(frozen=True)
class ClimbReport:
    """The whole enrichment progression, in order."""

    rounds: list[Round]
    n_paired: int
    adjusted: bool
    holdout_used: bool
    ladder_step: float
    # `n_paired` counts observations once repetitions are pooled, so the header
    # said "354 questions" for 118 questions answered three times. These two
    # split it the way ablate's headline does.
    n_questions: int | None = None
    n_reps: int | None = None
    # The last round against the baseline: the comparison the ruling word is
    # about. Kept whole so the word and its sentence come from the same
    # function ablate uses.
    final_vs_baseline: AblationReport | None = None

    @property
    def total_gain(self) -> float:
        return self.rounds[-1].gain_vs_baseline if self.rounds else 0.0

    def flips_summary(self) -> str:
        """Name what each round broke and fixed relative to the round before.

        The table in summary() says a round "broke 3"; this says which three,
        so the curator can open exactly those questions instead of diffing two
        score reports by hand.
        """
        lines = []
        for entry in self.rounds[1:]:
            lines.append(f"{entry.label}:")
            for label, ids in (("broke", entry.broke_ids), ("fixed", entry.fixed_ids)):
                names = ", ".join(
                    f"{base}{f' x{n}' if n > 1 else ''}"
                    for base, n in _collapse_reps(ids)
                )
                lines.append(f"  {label:<5} ({len(ids)}): {names or '(none)'}")
        return "\n".join(lines)

    def summary(self) -> str:
        arm = "judge-adjusted" if self.adjusted else "execution"
        if self.n_questions and (self.n_reps or 1) > 1:
            headline = (f"context enrichment over {len(self.rounds)} rounds, "
                        f"{self.n_paired} observations ({self.n_questions} questions x "
                        f"{self.n_reps} reps, {arm} accuracy)")
        else:
            headline = (f"context enrichment over {len(self.rounds)} rounds, "
                        f"{self.n_paired} questions ({arm} accuracy)")
        lines = [
            headline,
            "",
            f"  {'round':<22} {'rate':>7}  {'ladder':>7}  {'vs prev':>8}  "
            f"{'vs base':>8}  note",
        ]
        for entry in self.rounds:
            # A regression count is shown whenever one exists, not only when it
            # outweighs the gains: "net +40%" can still mean a question that
            # worked yesterday is broken today, and that is what a returning
            # customer notices first.
            note = ""
            if entry.vs_previous is not None:
                broke, fixed = entry.vs_previous.b, entry.vs_previous.c
                if broke and broke > fixed:
                    note = f"REGRESSED: broke {broke}, fixed {fixed}"
                elif broke:
                    note = f"fixed {fixed}, but BROKE {broke}"
                elif fixed == 0:
                    note = "bought nothing"
            lines.append(
                f"  {entry.label:<22} {entry.rate.point:>6.1%}  "
                f"{entry.ladder_rate:>6.1%}  "
                f"{entry.gain_vs_previous:>+8.1%}  {entry.gain_vs_baseline:>+8.1%}  {note}"
            )
        lines.append("")
        lines.append(f"  total gain: {self.total_gain:+.1%}")
        lines.append(
            f"  ladder score: {self.rounds[-1].ladder_rate:.1%}  "
            f"(only moves on a gain above {self.ladder_step:.1%})"
        )
        if self.rounds[-1].ladder_rate < self.rounds[-1].rate.point:
            lines.append(
                "  the raw rate is above the ladder score: at least one round's\n"
                "  gain was inside the noise this many questions can resolve."
            )
        # the last round against the baseline, in the word ablate and bundle use
        if self.final_vs_baseline is not None:
            lines.append("")
            lines.append(f"  ruling ({self.rounds[-1].label} vs {self.rounds[0].label}): "
                         f"{verdict_word(self.final_vs_baseline)[0]}")
            for sentence in self.final_vs_baseline.verdict_sentences():
                lines.append(f"  {sentence}")
        if not self.holdout_used:
            lines.append("")
            lines.append(
                "  WARNING: every round was measured on the same questions that "
                "guided\n  the curation. That number is a training score, not an "
                "estimate of\n  performance on unseen questions. Hold a slice out "
                "and score it once,\n  at the end, to find out what the curation is "
                "really worth."
            )
        return "\n".join(lines)


def _questions_in(report) -> int:
    """Distinct questions a report scored, collapsing repetitions.

    Pooled reps carry ids like `dw036#rep1`, and three answers to one question
    are one question. Uses the same `split("#rep")[0]` convention as the
    clustered test, so the two cannot disagree about what a question is.
    """
    scored = [r for r in report.results if r.execution_correct is not None]
    if not scored:
        return 0
    return len({str(r.id).split("#rep")[0] for r in scored})


def ladder_step(n: int) -> float:
    """The improvement a round must beat before the reported score moves.

    From Blum & Hardt's Ladder algorithm (ICML 2015): with 1/sqrt(n) as the
    step, repeatedly scoring candidates against one holdout stops letting you
    climb on noise. At n=20 that is 22 points; at n=100, 10 points — which is
    itself the useful message that a 15-question eval set cannot resolve a
    small improvement.
    """
    return 1.0 / math.sqrt(n) if n else 1.0


def questions_to_prove(b: int, c: int, n: int, cap: int = PROVE_CAP) -> int | None:
    """How many questions the discordant rates seen here (b of n broke, c of
    n fixed) would need before McNemar's exact test drops below 0.05.

    Both counts are scaled in proportion, so the answer is "a set this many
    times larger, moving the same way". None when b == c (no size proves an
    even swing) or when nothing up to `cap` does. A binary search, because
    the p-value falls with the size and the exact test on thousands of pairs
    is not free.
    """
    if n == 0 or b == c:
        return None

    def p_at(size: int) -> float:
        return mcnemar_exact(round(b * size / n), round(c * size / n)).p_value

    if p_at(cap) >= 0.05:
        return None
    low, high = n, cap
    while low < high:
        middle = (low + high) // 2
        if p_at(middle) < 0.05:
            high = middle
        else:
            low = middle + 1
    return low


def climb(
    rounds: list[tuple[str, EvalReport]],
    adjusted: bool = False,
    holdout_used: bool = False,
) -> ClimbReport:
    """Track accuracy across successive rounds of context enrichment.

    The first entry is the baseline — the run before any curation. Each later
    entry is compared both to its immediate predecessor (what that round
    bought) and to the baseline (what everything so far bought).

    Alongside each raw rate it reports a LADDER SCORE, which only rises when a
    round improves on the best so far by more than 1/sqrt(n). Every round of
    curation is another look at the same questions, and without that guard the
    reported number ratchets upward on noise across enough rounds.

    Args:
        rounds: ordered (label, report) pairs, baseline first. Every report
            must cover the same scored questions.
        adjusted: use judge-adjusted correctness instead of raw execution.
        holdout_used: set True if the reported questions were held out from the
            curation process. When False the summary carries a warning, because
            curating against the same questions you score on measures how well
            you fitted them, not how well the context generalises.

    Raises:
        ValueError: fewer than two rounds, or rounds that scored different
            question sets.
    """
    if len(rounds) < 2:
        raise ValueError("a climb needs a baseline plus at least one round")

    baseline_label, baseline_report = rounds[0]
    baseline_vector = baseline_report.correctness_vector(adjusted=adjusted)
    baseline_rate = baseline_report.adjusted if adjusted else baseline_report.execution

    # QUESTIONS, not answers. `correctness_vector` returns one entry per
    # SCORED ROW, and with repetitions a question contributes three of them,
    # so `len(baseline_vector)` is 3n at K=3 and the floor came out 1/sqrt(3n)
    # -- too small by sqrt(3). Measured on 10 questions x 3 reps: the floor
    # read 18.3 points where it should read 31.6, so the ladder would have
    # accepted a 20-point "gain" that is inside its own noise. That is the
    # exact failure Blum & Hardt's step exists to prevent, and it is the same
    # per-answer-against-per-question mistake found in the OVERFIT check and
    # in the perturbation example's power floor on the same day.
    step = ladder_step(_questions_in(baseline_report))
    best = baseline_rate.point
    entries = [
        Round(
            label=baseline_label,
            rate=baseline_rate,
            vs_previous=None,
            vs_baseline=None,
            gain_vs_previous=0.0,
            gain_vs_baseline=0.0,
            ladder_rate=best,
        )
    ]

    previous_report = baseline_report
    against_baseline = None
    for label, report in rounds[1:]:
        # ablate() carries the id-alignment check; calling it keeps one
        # definition of "these two runs are comparable".
        against_previous = ablate(previous_report, report, adjusted=adjusted)
        against_baseline = ablate(baseline_report, report, adjusted=adjusted)
        rate = against_previous.with_context
        if rate.point > best + step:
            best = rate.point
        entries.append(
            Round(
                label=label,
                rate=rate,
                vs_previous=against_previous.mcnemar,
                vs_baseline=against_baseline.mcnemar,
                gain_vs_previous=against_previous.lift,
                gain_vs_baseline=against_baseline.lift,
                ladder_rate=best,
                broke_ids=against_previous.broke_ids,
                fixed_ids=against_previous.fixed_ids,
            )
        )
        previous_report = report

    baseline_ids = (_scored_ids_adjusted(baseline_report) if adjusted
                    else _scored_ids(baseline_report))
    per_question: dict[str, int] = {}
    for observation_id in baseline_ids:
        base = observation_id.split("#rep")[0]
        per_question[base] = per_question.get(base, 0) + 1
    return ClimbReport(
        rounds=entries,
        n_paired=len(baseline_vector),
        adjusted=adjusted,
        holdout_used=holdout_used,
        ladder_step=step,
        n_questions=len(per_question) or None,
        n_reps=max(per_question.values()) if per_question else None,
        final_vs_baseline=against_baseline,
    )
