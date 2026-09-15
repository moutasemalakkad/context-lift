# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The README's headline result, recomputed from the data it came from.

A number in a shipped document has to be reproducible by a command, or nobody
can answer "how did you compute this". The lift the README leads with is
printed by `make example-bird`, which scores committed model predictions
against BIRD gold SQL on committed SQLite databases. This recomputes it the
same way and fails if the README and the data ever disagree.

It imports `examples/bird/run.py` rather than copying its scoring, so the test
and the example cannot drift apart.
"""
import importlib.util
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
BIRD = REPO / "examples" / "bird"


def _example():
    """The BIRD example module, loaded from its path (examples/ is not a package)."""
    spec = importlib.util.spec_from_file_location("bird_run", BIRD / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _readme_numbers() -> dict[str, str]:
    """The figures in the README's headline block, by name.

    The block is fenced and marked, so prose elsewhere quoting a similar number
    cannot be picked up by accident.
    """
    text = (REPO / "README.md").read_text()
    block = re.search(r"<!-- headline:example-bird -->\n```\n(.*?)```", text, re.S)
    assert block, "README has no <!-- headline:example-bird --> block"
    body = block.group(1)
    found = {}
    for name, pattern in (
        ("without", r"without context\s*:\s*([\d.]+)%"),
        ("with", r"with context\s*:\s*([\d.]+)%"),
        ("lift", r"lift\s*:\s*\+([\d.]+)%"),
        ("p", r"McNemar p\s*:\s*([\d.]+)"),
        ("questions", r"on (\d+) questions"),
    ):
        match = re.search(pattern, body)
        assert match, f"README headline block has no {name}"
        found[name] = match.group(1)
    assert "REAL GAIN" in body, "README headline block does not state the ruling"
    return found


def test_the_readme_headline_is_what_the_example_computes():
    """Scores both arms over the three BIRD databases and compares every figure
    the README prints. Under a second; no network, no key, no model call."""
    from context_lift.ablate import verdict_word

    example = _example()
    without = example.score(example.load(example.ROUNDS[0][1]))
    with_context = example.score(example.load(example.ROUNDS[-1][1]))
    measured = example.ablate(without, with_context)
    said = _readme_numbers()

    assert f"{100 * without.execution.point:.1f}" == said["without"]
    assert f"{100 * with_context.execution.point:.1f}" == said["with"]
    assert f"{100 * measured.lift:.1f}" == said["lift"]
    assert f"{measured.mcnemar.p_value:.4f}" == said["p"]
    assert str(measured.n_paired) == said["questions"]
    assert verdict_word(measured)[0] == "REAL GAIN"


def test_the_thelook_case_study_numbers_are_recomputed_from_the_committed_run():
    """A figure in a document is a claim until a script recomputes it.

    The research archive prints the first round through the framework. This reads
    the committed run of that round, recomputes the verdict from the recorded
    answers, and fails if the document and the data ever disagree. No
    warehouse, no model, no network.
    """
    import sys
    here = REPO / "examples" / "thelook"
    sys.path.insert(0, str(here))
    from reproduce import read_run, report_for  # noqa: E402
    from context_lift.ablate import verdict  # noqa: E402

    rows = read_run(here / "committed" / "run-2026-09-10.csv")
    curate = report_for(rows, "curate")
    holdout = report_for(rows, "holdout")
    result = verdict(holdout, curate, target_pp=10.0)

    text = (REPO / "docs" / "research" / "README.md").read_text()
    assert "## thelook, the loop closing, 2026-09-10" in text

    assert result.word == "REAL GAIN"
    assert f"verdict: {result.word}" in text

    # The held-out line is the one that matters, and the document quotes it
    # exactly. It MOVED: the word is UNDERPOWERED because 30 questions cannot
    # prove +13.3, not because nothing happened.
    assert holdout.n_questions == 30
    assert holdout.mcnemar.b == 0 and holdout.mcnemar.c == 6
    assert (f"held out: {holdout.n_questions} questions, "
            f"{holdout.lift * 100:+.1f} points, "
            f"{holdout.mcnemar.c} fixed, {holdout.mcnemar.b} broke") in text

    # 53 of 74, not 74: 21 working answers were stored-example lookups and
    # left the comparison. A count of what was ASKED beside a lift reads as
    # that lift's sample size.
    assert curate.n_questions == 52
    assert curate.mcnemar.b == 0 and curate.mcnemar.c == 16
    assert (f"fit on the working questions: {curate.n_questions} of 74 questions, "
            f"{curate.lift * 100:+.1f} points, "
            f"{curate.mcnemar.c} fixed, {curate.mcnemar.b} broke") in text


def test_the_case_study_keeps_the_refusal_beside_the_win():
    """The held-out set moved +13.3 points with 5 fixed and 1 broke, and the
    word is UNDERPOWERED. A reader who sees only the word concludes nothing
    happened; a reader who sees only the number quotes a finding the evidence
    does not support. The document has to carry both."""
    import sys
    sys.path.insert(0, str(REPO / "examples" / "thelook"))
    from reproduce import read_run, report_for  # noqa: E402

    holdout = report_for(read_run(REPO / "examples/thelook/committed/run-2026-09-10.csv"),
                         "holdout")
    # Whitespace flattened, because a sentence that wraps across two lines is
    # the same sentence and an assertion that breaks on a reflow teaches
    # people to reflow around the test rather than to keep the claim true.
    text = " ".join((REPO / "docs" / "research" / "README.md").read_text().split())

    assert holdout.mcnemar.p_value < 0.05, "round 2 is the provable one"
    assert "held out: 30 questions, +13.3 points, 5 fixed, 1 broke" in text, (
        "round 1's refusal has to stay on the page; it is what makes round 2 mean something")
    assert "verdict: UNDERPOWERED" in text and "verdict: REAL GAIN" in text
    assert text.index("UNDERPOWERED") < text.index("REAL GAIN")


def test_the_walkthrough_numbers_are_recomputed_from_the_committed_run():
    """A walkthrough is the document a stranger trusts most, because it shows
    commands and their output. If its numbers drift from the run it claims to
    show, it is worse than no walkthrough. Every figure below is recomputed."""
    import sys
    here = REPO / "examples" / "thelook"
    sys.path.insert(0, str(here))
    from reproduce import read_run, report_for  # noqa: E402
    from context_lift.ablate import verdict  # noqa: E402

    rows = read_run(here / "committed" / "run-2026-09-10.csv")
    curate = report_for(rows, "curate")
    holdout = report_for(rows, "holdout")
    result = verdict(holdout, curate, target_pp=10.0)

    text = " ".join((REPO / "docs" / "research" / "README.md").read_text().split())

    assert result.word == "REAL GAIN"
    assert f"verdict: {result.word}" in text

    # The held-out line, which is the claim the whole document rests on.
    assert holdout.n_questions == 30
    assert holdout.mcnemar.c == 6 and holdout.mcnemar.b == 0, (
        "6 fixed and 0 broken is quoted three times; it has to be true")
    assert (f"held out: {holdout.n_questions} questions, "
            f"{holdout.lift * 100:+.1f} points, "
            f"{holdout.mcnemar.c} fixed, {holdout.mcnemar.b} broke") in text
    assert holdout.mcnemar.p_value < 0.05
    assert f"p = {holdout.mcnemar.p_value:.4f}" in text

    assert curate.n_questions == 52
    assert (f"{curate.lift * 100:+.1f} points, "
            f"{curate.mcnemar.c} fixed, {curate.mcnemar.b} broke") in text


def test_the_walkthrough_shows_the_refusal_before_it_shows_the_win():
    """A walkthrough that only shows the win is an advertisement. This one
    opens on a round that refused to claim anything, because that refusal is
    what makes the second number worth reading."""
    text = " ".join((REPO / "docs" / "research" / "README.md").read_text().split())

    assert "verdict: UNDERPOWERED" in text
    assert text.index("UNDERPOWERED") < text.index("REAL GAIN"), (
        "the refusal has to come first, or the document is a sales page")
    assert "Reporting +13.3 without the refusal would overstate the result" in text


def test_the_walkthrough_states_what_it_does_not_claim():
    """Two limits, both real: the conventions resolve ambiguity in questions
    the author wrote, and the author wrote the questions too."""
    text = " ".join((REPO / "docs" / "research" / "README.md").read_text().split())

    assert "not \"the agent learned the business\"" in text
    assert "same person wrote the questions and the context" in text
    assert "Expect UNDERPOWERED on your first round" in text


def test_the_readme_points_to_one_continuous_guide():
    """A visitor should follow one workflow, with the same cues at each stage."""
    text = " ".join((REPO / "README.md").read_text().split())

    assert "[Start the guide →](docs/README.md)" in text
    guide = (REPO / "docs" / "README.md").read_text()
    stages = re.split(r"^## \d+\. ", guide, flags=re.M)[1:]
    assert len(stages) == 8
    for stage in stages:
        for cue in ("Purpose", "Inputs", "Commands", "Expected output",
                    "Best practices", "Ready to continue", "Next stage"):
            assert f"**{cue}" in stage


def test_the_evaluation_guide_says_how_many_defect_classes_the_gate_has():
    """It said 12 for a while after the count reached 15, which is the kind of
    number a reader checks by counting the table and then distrusts the rest
    of the document."""
    import context_lift.gate as gate
    names = [n for n in dir(gate)
             if n.isupper() and isinstance(getattr(gate, n), str) and getattr(gate, n) == n]
    # a warning is a class a person must SEE and the gate must not act on
    warnings = {"MISSING_LITERAL", "MISSING_ORDER", "MISSING_PROJECTION",
                "LITERAL_ON_WRONG_COLUMN"}
    defects = [n for n in names if n not in warnings]
    text = (REPO / "docs" / "README.md").read_text()
    assert f"{len(defects)} defect classes" in text, \
        f"the gate has {len(defects)} defect classes; the guide says otherwise"
    assert f"{len(warnings)} warnings" in text


def test_getting_started_commands_parse_with_the_real_cli():
    """Validate the first commands readers paste, including global flag order.

    The former quickstart put --workspace after the verb and accepted it in
    this test because only verb names were checked. Parse flags too, without
    executing a query or contacting any backend.
    """
    import shlex

    from context_lift.cli import _build_parser

    parser = _build_parser()
    checked = []
    for name in ("README.md", "docs/README.md", "docs/REFERENCE.md"):
        text = (REPO / name).read_text()
        for block in re.findall(r"```bash\n(.*?)```", text, re.S):
            for command in block.replace("\\\n", " ").splitlines():
                command = command.strip()
                if not command.startswith("contextlift "):
                    continue
                parser.parse_args(shlex.split(command)[1:])
                checked.append(command)
    assert len(checked) >= 15, "the runnable examples were not checked"
    guide = (REPO / "docs/README.md").read_text()
    for flag in ("--min-tables", "--require-analytical", "--max-bytes"):
        assert flag in guide, f"the guide's log step lost {flag}"
