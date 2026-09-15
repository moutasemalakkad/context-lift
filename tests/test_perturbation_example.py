# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The committed perturbation run, recomputed and held to the documents.

`examples/bird/perturbation/reproduce.py` recomputes the degradation curve
offline from a 100 KB file. This runs it and compares the numbers it prints
against the case study and the example's own README.

WHY THE COMMITTED FILE IS THE SMALL ONE. The six scored reports are 1.7 MB
because each carries every predicted SQL string and a preview of its rows.
A paired comparison needs correctness per question per arm and nothing else,
so the committed artifact is 100 KB and a person can open it. That is the
choice `examples/thelook/committed/` already makes.

THE POSITIVE CONTROL IS THE ROW THIS FILE MOST CARES ABOUT. Four of the five
results are NOT PROVEN. That is only meaningful if the harness can detect a
loss it knows is there, and the control is the proof that it can. A test that
let the control quietly weaken would let every other row keep its wording
while losing its meaning.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "bird" / "perturbation"
RUN = EXAMPLE / "correctness-3reps-2026-09-14.csv"
# The 1-repetition table is kept beside it on purpose: it is the run whose
# `irrelevant-context` ranking was withdrawn, and keeping it means the
# withdrawal can be RECOMPUTED rather than taken on trust.
ONE_REP = EXAMPLE / "correctness-2026-09-14.csv"


@pytest.fixture(scope="module")
def printed() -> str:
    if not RUN.is_file():
        pytest.skip(f"{RUN.name} is absent")
    done = subprocess.run(["python3", str(EXAMPLE / "reproduce.py")],
                          cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def test_it_runs_offline_and_names_every_arm(printed):
    assert "450 questions x 3 repetition(s) = 1350 answers per arm, 6 arms, offline" in printed
    for arm in ("positive-control-drop-value-decoding", "unknown-column",
                "strip-subject", "conflicting-definitions",
                "irrelevant-context"):
        assert arm in printed, f"{arm} vanished from the curve"


def test_the_positive_control_still_shows_a_large_loss(printed):
    """The row that licenses the other four. Without it, NOT PROVEN is
    indistinguishable from a harness too blunt to detect anything."""
    line = [l for l in printed.splitlines()
            if "positive-control" in l][0]

    assert "REAL LOSS" in line
    assert "-17.0 pts" in line
    assert "broke  92 fixed  14" in line
    assert "p = 0.0000" in line


def test_the_unknown_column_row_is_null_in_both_runs(printed):
    """The measurement that downgraded the linter's hardest refusal.

    At one repetition: -0.2 points, 24 broken against 23 fixed, p = 1.0000.
    At three: +0.2 points, 23 broken against 26 fixed, p = 0.7754. The SIGN
    FLIPPED between the two runs, which is the clearest evidence in the file
    that this is noise and not a small real effect.
    """
    line = [l for l in printed.splitlines()
            if "unknown-column" in l and "pts" in l][0]

    assert "+0.2 pts" in line
    assert "broke  23 fixed  26" in line
    assert "p = 0.7754" in line
    assert "NOT PROVEN" in line


def test_the_curve_is_ordered_worst_first(printed):
    """A curve printed in arbitrary order is a table, not a curve."""
    body = printed[printed.index("DEGRADATION CURVE"):]
    lifts = [float(m) for m in re.findall(r"([-+]\d+\.\d+) pts", body)]

    assert lifts == sorted(lifts), lifts
    assert lifts[0] < -15, "the worst row is no longer the positive control"


def test_the_resolution_floor_is_printed_with_the_curve(printed):
    """Four rows read NOT PROVEN. Without the floor beside them a reader
    cannot tell "costs less than 4 points" from "this set cannot see it"."""
    assert "resolve a change above about 4.0 points" in printed
    assert "not the same as costing nothing" in printed


def test_it_refuses_to_claim_generality(printed):
    assert "does not generalise" in printed


def test_the_readme_quotes_the_run_rather_than_retyping_it():
    """A README with its own copy of the numbers is a second source that
    drifts. Every figure in it has to appear in what reproduce.py prints."""
    readme = (EXAMPLE / "README.md").read_text()
    done = subprocess.run(["python3", str(EXAMPLE / "reproduce.py")],
                          cwd=ROOT, capture_output=True, text=True)
    flat = " ".join(done.stdout.split())

    for figure in ("-17.0 pts", "+0.2 pts", "p = 0.0000", "p = 0.7754",
                   "585 of 1350 answers correct"):
        assert figure in flat, f"reproduce.py no longer prints {figure}"
        assert figure in " ".join(readme.split()), f"the README lost {figure}"


def test_the_case_study_and_the_example_agree():
    """Two documents quoting one run must not disagree.

    Both now carry the 3-repetition figures AND the 1-repetition column, so
    this checks both: the current numbers, and the withdrawn one kept for
    comparison.
    """
    case_study = (ROOT / "docs" / "research" / "README.md").read_text()
    flat = " ".join(case_study.replace("−", "-").split())

    assert "-17.0" in flat, "the case study lost the 3-rep control"
    assert "92 questions broken against" in flat and "14 fixed" in flat
    # the withdrawn figure has to stay visible next to its replacement
    assert "-2.9" in flat and "-0.3" in flat
    assert "The lead is withdrawn" in flat


def test_the_committed_file_stays_small_enough_to_read():
    """The reason it is a correctness matrix and not the scored reports. If
    somebody swaps it for the full 1.7 MB output, the example stops being
    something a person can open."""
    assert RUN.stat().st_size < 600_000, (
        f"{RUN.name} is {RUN.stat().st_size:,} bytes. The point of this file "
        f"is that a reader can open it; the full reports are about 5 MB at "
        f"three repetitions.")


# ---------------------------------------------------------------------------
# the 3-repetition example (2026-09-14)

def test_the_offline_reproducer_matches_the_paid_run_exactly():
    """THE CHECK THAT MAKES THE EXAMPLE WORTH SHIPPING.

    A script that recomputes a curve from a committed CSV is only useful if
    it gets the SAME answer as the run that cost money. Not close -- the same
    lifts, the same broke/fixed counts, the same p-values. If it drifts, the
    example teaches a wrong number to everybody who runs it, which is worse
    than shipping no example.
    """
    import pathlib
    import re
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parent.parent
    evidence = root / "docs" / "evidence" / "perturbation-bird-3reps.txt"
    script = root / "examples" / "bird" / "perturbation" / "reproduce.py"
    if not evidence.is_file() or not script.is_file():
        pytest.skip("the 3-rep run or its reproducer is absent")

    done = subprocess.run([sys.executable, str(script)], cwd=root,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-800:]
    printed = done.stdout
    paid = evidence.read_text()

    rows = re.findall(
        r"^    (\S+)\s+([-+]\d+\.\d) pts  broke\s+(\d+) fixed\s+(\d+)"
        r"  p = ([\d.]+)", printed, re.M)
    assert len(rows) == 5, f"the reproducer printed {len(rows)} arms, not 5"

    for arm, lift, broke, fixed, p in rows:
        # the paid run prints the same three facts in its own layout
        assert f"lift            : {float(lift):+6.1f}%".replace("+", "+") in paid \
            or f"lift            :  {lift}%" in paid or f"lift            : {lift}%" in paid, (
                f"{arm}: the reproducer says {lift} and the paid run does not")
        assert f"fixed {fixed} / broke {broke} of 450 questions" in paid, (
            f"{arm}: reproducer counted broke {broke} fixed {fixed}; "
            f"the paid run disagrees")
        assert f"p = {p}" in paid, f"{arm}: p = {p} is not in the paid run"


def test_the_power_floor_uses_questions_not_answers():
    """The same denominator mistake that was found in ablate.py's OVERFIT
    check on 2026-09-14, made again in the first version of this script.
    1350 answers give a floor of 2.0 points; 450 questions give 4.0. Three
    answers to one question are not three independent observations."""
    import pathlib
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parent.parent
    script = root / "examples" / "bird" / "perturbation" / "reproduce.py"
    if not script.is_file():
        pytest.skip("the reproducer is absent")

    printed = subprocess.run([sys.executable, str(script)], cwd=root,
                             capture_output=True, text=True).stdout

    assert "450 questions resolve a change above about 4.0 points" in printed
    assert "2.0 points" not in printed
    assert "QUESTIONS, not the 1350 answers" in printed


def test_the_withdrawn_ranking_is_printed_by_the_script_itself():
    """A correction in a document nobody opens is not a correction. Anybody
    who runs the example sees it."""
    import pathlib
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parent.parent
    script = root / "examples" / "bird" / "perturbation" / "reproduce.py"
    if not script.is_file():
        pytest.skip("the reproducer is absent")

    printed = subprocess.run([sys.executable, str(script)], cwd=root,
                             capture_output=True, text=True).stdout

    assert "WITHDRAWN" in printed
    assert "-2.9" in printed and "-0.3" in printed
    assert "not a result" in printed


def test_both_denominators_are_printed_so_the_gap_is_visible():
    """Per-question and per-answer p-values differ, and the file that shows
    only one is asking to be misread. strip-subject: 0.1337 against 0.0809."""
    import pathlib
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parent.parent
    script = root / "examples" / "bird" / "perturbation" / "reproduce.py"
    if not script.is_file():
        pytest.skip("the reproducer is absent")

    printed = subprocess.run([sys.executable, str(script)], cwd=root,
                             capture_output=True, text=True).stdout

    assert "per-question p = 0.1337   per-answer p = 0.0809" in printed
    assert "the number to defend" in printed


def test_the_committed_csv_is_small_enough_to_read():
    """The whole reason the example ships a correctness table instead of the
    scored reports."""
    import pathlib

    csv_file = (pathlib.Path(__file__).resolve().parent.parent / "examples"
                / "bird" / "perturbation" / "correctness-3reps-2026-09-14.csv")
    if not csv_file.is_file():
        pytest.skip("the 3-rep correctness table is absent")

    size = csv_file.stat().st_size
    assert size < 600_000, f"{size:,} bytes is too big to read in an editor"
    header = csv_file.read_text().splitlines()[0]
    assert header == "question,arm,rep,correct", header


def test_the_one_rep_table_is_kept_so_the_withdrawal_is_recomputable():
    """The withdrawn -2.9 should be checkable, not just asserted.

    Anybody can recompute it from the 1-repetition table and see for
    themselves that one sample put `irrelevant-context` worst and three put
    it least. Deleting that file would leave the correction as a claim about
    data nobody can see -- which is the shape of the problem, not the fix.
    """
    if not ONE_REP.is_file():
        pytest.skip("the 1-rep table is absent")

    import csv as _csv

    rows = list(_csv.DictReader(ONE_REP.open()))

    by_arm = {}
    for row in rows:
        by_arm.setdefault(row["arm"], {})[row["question"]] = int(row["correct"])

    clean = by_arm["clean"]
    n = len(clean)
    lifts = {arm: (sum(a.values()) - sum(clean.values())) / n * 100
             for arm, a in by_arm.items() if arm != "clean"}

    # at ONE repetition, irrelevant-context was the worst of the four
    realistic = {k: v for k, v in lifts.items() if "positive-control" not in k}
    assert min(realistic, key=realistic.get) == "irrelevant-context"
    assert round(realistic["irrelevant-context"], 1) == -2.9

    # and the 3-rep run puts it LEAST harmful of the four, at -0.3
    assert "-0.3 pts" in subprocess.run(
        ["python3", str(EXAMPLE / "reproduce.py")], cwd=ROOT,
        capture_output=True, text=True).stdout
