# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The LookML round's numbers, checked against the run that produced them.

This project's rule is that a shipped figure must be computable from
something committed. A paid round cannot be recomputed for free, so the next
best thing is committed instead: the round's own output, beside the arm it
scored. These tests fail if `docs/research/README.md` ever stops matching it.

The mistake that made this necessary is worth recording. The round was run
WITHOUT `--save-report`, so for about an hour the only copy of the result was
in Cloud Logging, which ages out. The number would have become unverifiable
while still being quoted in two documents. `--save-report` exists and was
not used.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "examples" / "thelook" / "lookml-arm" / "round-2026-09-12.txt"
CASE_STUDY = ROOT / "docs" / "research" / "README.md"


@pytest.fixture(scope="module")
def evidence() -> str:
    if not EVIDENCE.is_file():
        pytest.skip(f"{EVIDENCE.name} is absent")
    return EVIDENCE.read_text()


@pytest.fixture(scope="module")
def case_study() -> str:
    return CASE_STUDY.read_text()


# Each is (label, the exact line the run printed). Matched verbatim rather
# than parsed into numbers: a rounding difference between the parse and the
# prose is exactly the drift this is for.
QUOTED_LINES = [
    ("the verdict word", "verdict: NOT PROVEN"),
    ("the safety word", "safety: MIXED"),
    ("the lift", "held out: 102 questions, +2.0 points"),
    ("the arm hashes and the answer count",
     "arms 2404ed0d5166 against c6266a145763, K=1, 208 answers scored"),
    ("the broken question ids", "broke: tl018, ta002"),
]


@pytest.mark.parametrize("label,line", QUOTED_LINES,
                         ids=[label for label, _ in QUOTED_LINES])
def test_the_case_study_quotes_the_run_verbatim(label, line, evidence,
                                                case_study):
    assert line in evidence, f"{label}: not in the committed run output"
    assert line in case_study, f"{label}: the case study no longer quotes it"


def test_the_p_value_in_the_prose_is_the_one_the_run_printed(evidence,
                                                             case_study):
    """Held apart from the list above because a p-value is the single number
    most likely to be retyped from memory."""
    printed = re.search(r"p = (0\.\d+)", evidence)
    assert printed, "no p-value in the committed run output"
    assert f"p = {printed.group(1)}" in case_study


def test_the_case_study_names_the_evidence_file(case_study):
    """A number with no path to its source is not evidence, whatever it says."""
    assert "round-2026-09-12.txt" in case_study


def test_the_description_share_claim_matches_the_committed_arm(case_study):
    """`0 with a written description (0%)` is the whole mechanism of the
    finding, so it must agree with the arm's own generated README."""
    readme = (ROOT / "examples" / "thelook" / "lookml-arm" / "README.md").read_text()
    described = re.search(r"metrics\s+(\d+), (\d+) described", readme)
    assert described, f"could not read the metric counts from {readme!r}"
    total, with_description = described.group(1), described.group(2)
    assert with_description == "0", (
        "the committed arm now HAS described metrics, so the case study's "
        "'zero of nine' claim is stale")
    assert f"{total} metrics, 0 with a written description (0%)" in case_study


def test_the_safety_word_is_not_quietly_upgraded(case_study):
    """MIXED, not SAFE. This context broke two questions that were right
    without it, and a +2.0 arm that costs two working answers is a trade. The
    temptation when writing this up is to lead with the +2.0 alone."""
    assert "MIXED, not SAFE" in case_study
    assert "trade, not a win" in case_study


def test_the_caveat_survives_editing(case_study):
    """The claim is conditional on the description share. Without that
    sentence the entry reads as "semantic layers do not work", which is not
    what was measured and is contradicted by three published benchmarks the
    entry cites."""
    assert "conditional on the description share" in case_study
    for cited in ("Cube", "dbt Labs", "Shkapenyuk"):
        assert cited in case_study, f"{cited} is no longer cited"


# ---------------------------------------------------------------------------
# The 2026-09-13 sections. Same convention as above, one evidence file each.
#
# These were written after a session in which three numbers reached a doc with
# no source behind them, and in which two numbers counted by hand in chat
# (37 against 9, and 29% against 5%) turned out to be wrong when a program
# recomputed them. So every figure below is matched against the file that
# produced it, and the arithmetic ones are recomputed rather than matched.

TODAY = ROOT / "docs" / "evidence"

# (evidence file, the exact line both the run and the prose must carry)
QUOTED_TODAY = [
    ("blob-vs-selection.txt", "lift            :  -1.6%  [-5.6%, +1.1%]"),
    ("blob-vs-selection.txt", "fixed 25 / broke 35 of 450 questions"),
    ("blob-vs-selection.txt", "p = 0.2451   not significant at 0.05"),
    ("blob-vs-selection.txt", "lift            :  -7.7%  [-15.7%, -1.6%]"),
    ("blob-vs-selection.txt", "fixed 7 / broke 19 of 139 questions"),
    ("blob-vs-selection.txt", "p = 0.0290   significant at 0.05"),
    ("over-salience.txt", "blob           74 of 417 runs"),
    ("over-salience.txt", "selection      97 of 417 runs"),
    ("over-salience.txt", "sentence names it    12 of 61  broke  (20%, n=61)"),
    ("over-salience.txt", "sentence does not     7 of 78  broke  (9%, n=78)"),
    ("over-salience.txt", "two-sided Fisher exact p = 0.0839"),
    ("self-route.txt", "strict wording    2 of 139 escalated   1.4%"),
    ("self-route.txt", "soft wording      not recoverable"),
    ("repaired-literals.txt", "lift            :  +0.6%  [-0.8%, +3.5%]"),
    ("repaired-literals.txt", "fixed 15 / broke 9 of 450 questions"),
    ("repaired-literals.txt", "p = 0.3075   not significant at 0.05"),
]


@pytest.mark.parametrize("name,line", QUOTED_TODAY,
                         ids=[f"{n.split('.')[0]}:{l[:34]}"
                              for n, l in QUOTED_TODAY])
def test_today_sections_quote_their_run_verbatim(name, line, case_study):
    path = TODAY / name
    assert path.is_file(), f"{name} is missing, so the prose cites nothing"
    assert line in path.read_text(), f"{name} does not contain {line!r}"
    assert line in case_study, f"the case study no longer quotes {line!r}"


def test_the_cache_floor_table_is_arithmetic_and_not_recalled(case_study):
    """The two per-call token counts are divisions of numbers in a committed
    run, so they get recomputed here instead of string-matched.

    This is the check the three unsourced README numbers needed and did not
    have: 6,606 and 411 are not quotes, they are results, and a result that
    only a person's memory supports is the exact failure being guarded.
    """
    spent = (TODAY / "blob-vs-selection.txt").read_text()
    lines = re.findall(r"spent: (\d+) calls, ([\d,]+) prompt", spent)
    assert len(lines) == 6, (
        f"expected six spend lines, three databases x two arms, found "
        f"{len(lines)}")

    per_call = [round(int(prompt.replace(",", "")) / int(calls))
                for calls, prompt in lines]
    # The file records them database by database, blob arm first. Taking
    # max() and min() across all six looked equivalent and is not: it picks
    # thrombosis's blob at 7,454 rather than toxicology's 6,606, which is the
    # pair the table actually quotes. Caught by this test on its first run.
    blobs, selections = per_call[0::2], per_call[1::2]
    blob, selection = blobs[0], selections[0]

    assert f"{blob:,}" in case_study, (
        f"the table's blob row is {blob:,} prompt tokens per call and the "
        f"case study does not say so")
    assert str(selection) in case_study, (
        f"the table's selection row is {selection} prompt tokens per call "
        f"and the case study does not say so")

    # The floor is the whole point of the table: one arm is eligible for a
    # cache discount and the other can never be. Checked on all three
    # databases, because a conclusion that holds only for the example row is
    # not the conclusion the prose draws.
    assert "2,048" in case_study
    for value in blobs:
        assert value > 2048, (
            f"a blob arm is {value} prompt tokens, below the 2,048 floor, so "
            f"it is no longer cache-eligible")
    for value in selections:
        assert value < 2048, (
            f"a selection arm is {value} prompt tokens, above the 2,048 "
            f"floor, so it IS now cache-eligible and the table is wrong")


def test_no_hit_rate_is_claimed_for_the_cache(case_study):
    """A measured cache hit rate depends on request ordering and timing, and
    the one observation this session made came from a four-thread pool that
    fired before the cache populated. Stating it as a rate would be the same
    mistake as the withdrawn numbers."""
    # Collapsed first: the phrase is bold and the markdown wraps mid-phrase,
    # so a plain substring search fails on formatting rather than on content.
    flat = " ".join(case_study.split())
    assert "No hit rate is claimed" in flat


def test_the_killed_hypothesis_is_reported_as_killed(case_study):
    """The repaired-literals arm predicted toxicology +7 and got +1.0. An arm
    that fails its own stated kill condition is the most deletable paragraph
    in the file, and deleting it would leave the typo looking like a live
    explanation for the -7.7."""
    assert "stated its own kill condition before it ran" in case_study
    assert "It moved +1.0" in case_study
    assert "not worth claiming as lift" in case_study


def test_the_hand_counted_numbers_are_recorded_as_withdrawn(case_study):
    """37 against 9 and 29% against 5% were reported in a live session and do
    not survive recomputation. The correction belongs next to the number, not
    only in a commit message, because the wrong pair was spoken aloud."""
    assert "37 select-list mentions against 9" in case_study
    assert "Neither survived being computed by a program" in case_study


def test_every_evidence_file_the_new_sections_cite_exists(case_study):
    cited = set(re.findall(r"`(docs/evidence/[\w./-]+)`", case_study))
    assert cited, "the new sections cite no evidence path at all"
    missing = sorted(p for p in cited if not (ROOT / p).exists())
    assert not missing, f"cited but absent: {missing}"


def test_the_oracle_framing_is_not_softened(case_study):
    """The result is only interesting because selection was given the answer
    key. Without that sentence a reader reasonably objects "your retriever was
    just bad", and the objection would be unanswerable."""
    assert "oracle retrieval" in case_study
    assert "no ranker can" in case_study and "answer key" in case_study


def test_the_two_cache_probes_are_both_reported(case_study):
    """One probe was quoted in the practice record with no committed run behind it.
    Re-running gave 19% where the first gave 11% on byte-identical input, so
    the honest claim is the instability, not either number. Reporting only one
    would re-create the defect that prompted the re-run."""
    probes = (TODAY / "cache-probes.txt").read_text()
    # The runner prints `(11%) came from the model's implicit cache`; the
    # case study reformats it into a table. Read the runner's shape here.
    shares = re.findall(r"\((\d+)%\) came from", probes)
    assert len(shares) == 2, f"expected two probes, found {shares}"
    assert shares[0] != shares[1], (
        "the two probes now agree, so the instability claim needs rewriting")
    for share in shares:
        assert f"{share}%" in case_study, (
            f"probe share {share}% is not in the case study")
    assert "identical to the token" in case_study, (
        "the prose must say the inputs were identical, or the disagreement "
        "reads as two different measurements rather than one unstable one")


# ---------------------------------------------------------------------------
# the perturbation suite

PERTURB = [
    ("perturbation-bird.txt", "positive-control-drop-value-decoding       -18.9% [-23.3%, -14.5%]     REAL LOSS"),
    ("perturbation-bird.txt", "unknown-column                              -0.2% [-3.2%, +2.8%]       NOT PROVEN"),
    ("perturbation-bird.txt", "irrelevant-context                          -2.9% [-6.0%, +0.2%]       NOT PROVEN"),
    ("perturbation-positive-control.txt", "positive-control-remove-capitalisation: -4.1 points, broke 4 fixed 1, n=74"),
]


def _flat(text: str) -> str:
    """Whitespace collapsed and the minus sign normalised.

    Two things defeat a plain substring check on prose. Markdown wraps a
    sentence mid-phrase, so a quoted clause spans a newline. And the prose
    uses a typographic minus while a program prints a hyphen, so "-0.2" and
    the rendered form are different strings for the same number. Both cost a
    failing test that says nothing about the content.
    """
    return " ".join(text.replace("\u2212", "-").split())


@pytest.mark.parametrize("name,line", PERTURB,
                         ids=[f"{n.split('.')[0][:18]}:{l[:28]}" for n, l in PERTURB])
def test_the_perturbation_rows_are_quoted_from_their_run(name, line, case_study):
    path = TODAY / name
    assert path.is_file(), f"{name} is missing"
    assert line in path.read_text(), f"{name} does not contain {line!r}"
    # the case study reformats the table, so match the numbers rather than
    # the whole line
    flat = _flat(case_study)
    for number in re.findall(r"-?\d+\.\d+%?", line):
        assert number in flat, f"the case study lost {number}"


def test_the_positive_control_is_reported_as_having_worked():
    """The row that licenses every other row. If deleting known-load-bearing
    context had shown no loss, the suite would have been measuring its own
    deafness and nothing else in that table would mean anything."""
    body = (TODAY / "perturbation-bird.txt").read_text()
    assert "REAL LOSS" in body
    assert "context broke   : 102 questions" in body
    assert "McNemar p       : 0.0000" in body


def test_the_downgrade_is_recorded_with_its_measurement(case_study):
    """`unknown column` went from REFUSE to WARN because it was priced, not
    because somebody changed their mind. The severity and the number have to
    travel together or the next reader re-raises it."""
    from context_lift.lint import WARN, lint
    from context_lift.export import Spec, TableSchema
    import pathlib as _p

    spec = Spec(root=_p.Path("(test)"))
    spec.schemas["shop.orders"] = TableSchema(columns={
        "status": {"description": "", "synonyms": [], "tags": []}})
    spec.rules.append(("00-defs.md", "- Completed means `order_state` = 'x'."))

    found = [f for f in lint(spec).findings if f.check == "unknown column"]
    assert found, "the unknown column check stopped firing"
    assert found[0].severity == WARN, "it went back to REFUSE with no new number"
    assert "-0.2 points" in found[0].basis
    assert "DOWNGRADED FROM REFUSE" in found[0].basis
    assert "COMMON, not that it is costly" in found[0].basis

    assert "mostly *ignored*, not obeyed" in _flat(case_study)


def test_the_thelook_run_is_reported_as_underpowered_rather_than_hidden(case_study):
    """The first attempt failed and the failure is the useful part: a suite
    that cannot resolve its own control is measuring nothing. Dropping it
    from the write-up would make the BIRD numbers look like the first idea
    rather than the second."""
    flat = _flat(case_study)
    assert "cannot resolve its own control" in flat
    assert "power.smallest_resolvable(74)" in flat
    assert "-4.1 points" in flat


def test_the_single_repetition_caveat_is_kept_as_the_reason_it_was_rerun(case_study):
    """The tool printed "a single sample, not a measurement" beside the first
    run. That line stays in the file, but it now reads as the REASON the suite
    was run again rather than as the caveat on a quoted table.

    This test used to be satisfied by the caveat alone. It is not any more:
    a 3-rep run has to be present, or the caveat is describing numbers the
    file no longer shows.
    """
    flat = _flat(case_study)
    assert "one repetition per arm" in flat
    assert "single sample, not a measurement" in flat
    assert "run again at three repetitions" in flat
    assert "1,350 observations per arm" in flat


def test_the_three_rep_table_matches_its_evidence_file(case_study):
    """Both columns of the comparison, checked against the run that produced
    each one. The 1-rep column had two numbers typed from memory when this
    table was first drafted."""
    import pathlib

    import pytest

    evidence = (pathlib.Path(__file__).resolve().parent.parent / "docs"
                / "evidence" / "perturbation-bird-3reps.txt")
    if not evidence.is_file():
        pytest.skip("the 3-rep evidence file is absent")
    run = evidence.read_text()

    for row in ("positive-control           -18.9    -17.0",
                "irrelevant-context          -2.9     -0.3",
                "strip-subject               -2.7     -1.8",
                "conflicting-definitions     -1.1     -1.3",
                "unknown-column              -0.2     +0.2"):
        assert row in case_study, f"the case study dropped: {row}"
        assert row in run, f"the case study quotes what the run does not: {row}"


def test_the_withdrawn_lead_is_withdrawn_in_the_case_study_too(case_study):
    """An evidence file nobody reads is not where a correction belongs. The
    -2.9 claim was published HERE, so the withdrawal has to be here."""
    flat = _flat(case_study)

    assert "The lead is withdrawn" in flat
    assert "A withdrawal, and it was mine" in flat
    assert "noise being read as a ranking" in flat
    assert "I quoted the point estimate anyway" in flat, (
        "the reason it happened is the part that stops it happening again")
    # and the corrected value has to appear, not just the retraction
    assert "least** harmful of the four, at" in flat


def test_the_control_at_three_reps_is_quoted_with_both_counts(case_study):
    flat = _flat(case_study)
    assert "-17.0 points [-21.5, -13.1]" in flat
    assert "92 questions broken against" in flat and "14 fixed" in flat


def test_the_not_proven_rows_say_how_many_questions_would_be_needed(case_study):
    """"NOT PROVEN" with no sample-size figure reads as "costs nothing"."""
    flat = _flat(case_study)
    assert "more than 5,000 questions" in flat
    assert "or** that 450 questions cannot see them" in flat


# ---------------------------------------------------------------------------
# which CLASS of context is doing the work (2026-09-14)

def test_the_classes_section_quotes_its_evidence_file_exactly(case_study):
    """The convention that stops a number drifting from its run: every figure
    in the section has to appear in the committed evidence file too."""
    import pathlib

    evidence = (pathlib.Path(__file__).resolve().parent.parent / "docs"
                / "evidence" / "classes-thelook-cost.txt")
    if not evidence.is_file():
        import pytest
        pytest.skip("the classes evidence file is absent")
    run = evidence.read_text()

    for quoted in ("-27.0 points [-37.8, -16.2]",
                   "0 full arm     $0.0107  $0.0001/answer  $0.0004/correct answer",
                   "minus examples $0.0113  $0.0002/answer  $0.0002/correct answer"):
        assert quoted in case_study, f"the case study dropped: {quoted}"
        assert quoted in run, f"the case study quotes what the run does not say: {quoted}"


def test_the_negative_sign_is_explained_in_words(case_study):
    """A minus in front of a lift is read backwards by about half of
    everybody, and here it means the removal HELPED."""
    flat = _flat(case_study)
    assert "A negative number means the arm scored **better without that class**" in flat
    assert "were costing 27 points" in flat


def test_the_three_unresolved_classes_are_not_reported_as_zero(case_study):
    """74 questions resolve about 12 points. Three of the four rows came back
    inside that floor and must not read as "this class does nothing"."""
    flat = _flat(case_study)
    assert "not shown either way" in flat
    assert "not the same as costing nothing" in flat
    assert "only one of the four rows that clears its own floor" in flat


def test_the_two_cost_axes_are_reported_as_disagreeing(case_study):
    """The reason the tool prints both. More bytes scanned AND half the cost
    per useful answer, in the same arm."""
    flat = _flat(case_study)
    assert "**raised** the query bill" in flat
    assert "**halved** the cost per correct answer" in flat
    assert "Cost per answer alone calls that arm more expensive" in flat


def test_the_section_refuses_to_generalise_about_worked_examples(case_study):
    """22 examples on one schema is not a claim about worked examples, and
    the established distractor result runs the other way."""
    flat = _flat(case_study)
    assert "It is not a claim about worked examples" in flat
    assert "24.3 points" in flat, (
        "the distractor result is the reason a BANK of 22 can hurt while a "
        "matched example helps; without it the section reads as a reversal")


def test_the_classes_section_names_a_target_that_exists(case_study):
    import pathlib

    assert "make classes-thelook PROJECT=<project>" in case_study
    makefile = (pathlib.Path(__file__).resolve().parent.parent
                / "Makefile").read_text()
    assert "classes-thelook:" in makefile
