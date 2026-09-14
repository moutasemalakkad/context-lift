# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The README's results chart, checked against the data it claims to draw.

Two rules, both of which a chart can break silently:

1. **Every figure names a committed source.** A number that lives only in a
   document cannot be checked, and "how was that computed" has no answer.
2. **One level per figure.** A per-ANSWER lift and a per-QUESTION p-value
   describe different denominators. Three answers to one question are not
   three independent facts, so the intervals differ and the two cannot share
   a line.

So every row in results.json names a source and a level, and this file
re-reads that source and compares. The chart cannot drift from the data
without going red.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "assets" / "results.json"


def _chart():
    spec = importlib.util.spec_from_file_location(
        "make_results_chart", ROOT / "scripts" / "make_results_chart.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def results() -> list[dict]:
    return json.loads(DATA.read_text())["results"]


def _row(results: list[dict], needle: str) -> dict:
    matches = [r for r in results if needle in r["label"]]
    assert len(matches) == 1, f"{needle!r} matches {len(matches)} rows"
    return matches[0]


def test_every_row_names_a_source_file_that_exists(results):
    """A row whose source cannot be opened is a number nobody can check."""
    for row in results:
        # the source may carry a trailing note in brackets, or a field name
        # after a colon; the path is the first token
        path = row["source"].split(":")[0].split(" ")[0]
        assert (ROOT / path).exists(), \
            f"{row['label']}: source {path} does not exist"
        assert (ROOT / row["pinned_by"]).exists(), \
            f"{row['label']}: pinned_by {row['pinned_by']} does not exist"


def test_the_bird_row_matches_the_readme_headline_block(results):
    """Row 1 is the block `tests/test_readme_numbers.py` recomputes from the
    committed predictions, so agreeing with that block is agreeing with a
    live scoring run."""
    row = _row(results, "3 databases")
    text = (ROOT / "README.md").read_text()
    block = re.search(r"<!-- headline:example-bird -->\n```\n(.*?)```",
                      text, re.S)
    assert block, "README has no headline block to compare against"
    body = block.group(1)
    assert f"{row['without_percent']}%" in body
    assert f"{row['with_percent']}%" in body
    assert f"+{row['lift_points']}%" in body
    assert f"{row['mcnemar_p']}" in body
    assert f"on {row['questions']} questions" in body
    assert row["verdict"] in body


def test_the_enterprise_row_is_the_per_question_figure(results):
    """The one that was wrong before. `answer_lift_points` is 19.5 and
    `question_lift_points` is 18.5; the chart must use the question-level
    trio throughout, because that is the number the tool tells a user to
    quote."""
    row = _row(results, "11 databases")
    source = json.loads((ROOT / "examples" / "bird"
                         / "enterprise_result.json").read_text())
    assert row["lift_points"] == source["question_lift_points"]
    assert row["lift_ci_points"] == source["question_lift_ci_points"]
    assert row["mcnemar_p"] == source["question_mcnemar_p"]
    assert row["questions"] == source["targeted_questions"]
    assert row["verdict"] == source["verdict"]
    assert row["level"] == "question"
    # and the answer-level figure must NOT be what got drawn
    assert row["lift_points"] != source["answer_lift_points"]


def test_the_warehouse_row_matches_the_committed_run(results):
    """The README used to claim 6.4% to 19.0% with p<0.0001 for this run.
    Those numbers are in no committed file -- only in case-study prose with no
    test behind it. This row is the run that has a result file."""
    row = _row(results, "97-table warehouse")
    source = json.loads((ROOT / "examples" / "beaver"
                         / "result.json").read_text())
    assert row["questions"] == source["paired_questions"]
    assert row["without_percent"] == source["baseline_percent"]
    assert row["with_percent"] == source["treatment_percent"]
    assert row["lift_points"] == source["lift_points"]
    assert row["lift_ci_points"] == source["lift_ci_points"]
    assert row["mcnemar_p"] == source["mcnemar_p"]
    assert row["verdict"] == source["verdict"]


def test_the_undescribed_bi_model_row_matches_the_round_output(results):
    """Not drawn in the README, still checked here. It is the measurement
    behind the answer to "can't you just read my BI tool?", and the answer
    turns on the description share rather than on the BI tool: a model whose
    metrics carry no written description has nothing written to hand over."""
    row = _row(results, "no written descriptions")
    text = (ROOT / row["source"]).read_text()
    assert f"+{row['lift_points']} points" in text
    assert f"p = {row['mcnemar_p']}" in text
    assert f"{row['questions']} questions" in text
    assert row["verdict"] in text
    assert row["verdict"] == "NOT PROVEN", (
        "if a rerun ever proves this, the README's argument changes and this "
        "test should be the thing that makes someone say so out loud")


def test_the_committed_svgs_are_what_the_script_produces(results):
    """Regenerating is one command, so a stale picture is nobody's excuse:
    run `make results-chart`."""
    chart = _chart()
    for theme in ("light", "dark"):
        path = DATA.parent / f"results-{theme}.svg"
        assert path.read_text() == chart.draw(chart.shown(results), theme), (
            f"{path.name} is stale; run `make results-chart`")


def test_a_hidden_row_is_still_verified(results):
    """`show_in_readme: false` decides what gets DRAWN, nothing else. Every
    row here is still read back against its source by the tests above, so
    taking a finding off the front page cannot quietly stop checking it."""
    chart = _chart()
    drawn = {r["label"] for r in chart.shown(results)}
    hidden = [r for r in results if r["label"] not in drawn]
    assert hidden, ("no row is hidden any more -- if that is deliberate, "
                    "delete this test; if not, a finding was dropped")
    for row in hidden:
        assert row["source"] and row["pinned_by"], \
            f"{row['label']} is hidden AND unsourced, which is how a finding disappears"


def test_the_axis_covers_every_interval_it_draws(results):
    """A whisker wider than the axis gets silently clipped, and the chart then
    reads as if that interval stopped at the edge. This happened at
    AXIS_MAX=35 with an interval reaching +49.8."""
    chart = _chart()
    widest = max((r["lift_ci_points"][1] for r in chart.shown(results)
                  if r["lift_ci_points"]), default=0)
    assert widest <= chart.AXIS_MAX, (
        f"an interval reaches +{widest} but the axis stops at "
        f"+{chart.AXIS_MAX}, so it is drawn clipped")


def test_rows_sourced_from_an_evidence_file_match_that_file(results):
    """The generic version of the per-row checks above.

    The named checks each know one file's format. A row added later gets no
    check at all unless someone remembers to write one, and the two rows added
    on 2026-09-13 were exactly that case: their numbers were pinned only by
    the case study quoting them, which is one indirection away from the run.
    This reads the ablation output itself.
    """
    checked = 0
    for row in results:
        path = row["source"].split(":")[0].split(" ")[0]
        if not path.startswith("docs/evidence/") or not path.endswith(".txt"):
            continue
        body = (ROOT / path).read_text()
        sign = "+" if row["lift_points"] >= 0 else "-"
        magnitude = abs(row["lift_points"])
        assert f"lift            :  {sign}{magnitude}%" in body, (
            f"{row['label']}: {row['lift_points']} is not the lift "
            f"{path} printed")
        assert f"p = {row['mcnemar_p']:.4f}" in body, (
            f"{row['label']}: p = {row['mcnemar_p']} is not in {path}")
        assert f"{row['without_percent']}%" in body
        assert f"{row['with_percent']}%" in body
        checked += 1
    assert checked >= 2, (
        f"only {checked} row(s) were checked against an evidence file, so "
        f"this test is passing on almost nothing")


def test_an_arm_against_arm_row_says_which_way_it_points(results):
    """`without` and `with` normally mean no-context and context. Two rows
    compare two context arms instead, where a negative lift means the arm
    named second lost. Without that note on the row, the sign is unreadable
    and a reader would take -1.6 to mean context hurt."""
    for row in results:
        if row["source"].startswith("docs/evidence/"):
            note = row.get("direction_note", "")
            assert "without =" in note and "with =" in note, (
                f"{row['label']}: no direction_note saying what the two arms "
                f"are, so the sign of {row['lift_points']} is ambiguous")


# ---------------------------------------------------------------------------
# every row's level, not just the one row that had the bug

def test_every_row_declares_a_level_and_it_is_one_of_two_values(results):
    """The rule at the top of this file applies to all six rows. The check
    below it only covered the 11-database row, because that was the row where
    the mistake was found.

    An outside review claimed 3 of 6 rows carried answer-level estimates
    labelled question-level. Checked against every source: they do not. This
    test is what makes that answer durable rather than a thing somebody
    verified once by hand.
    """
    for row in results:
        assert "level" in row, f"{row['label']!r} declares no level"
        assert row["level"] in ("question", "answer"), row["level"]


def test_a_row_quoting_a_two_denominator_evidence_file_quotes_the_question_half(results):
    """The ablation output prints BOTH denominators, one under "each question
    counted once - the number to defend" and one under "each answer counted
    separately - optimistic". A row is only allowed to carry the first.

    This reads the source file and finds which p-value the row used.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    checked = 0
    for row in results:
        source = row.get("source", "")
        path = root / source.split(":")[0].split(" ")[0]
        if not path.is_file() or path.suffix != ".txt":
            continue
        text = path.read_text()
        if "each answer counted separately" not in text:
            continue        # a one-repetition run: the two levels coincide

        p = row.get("mcnemar_p")
        if p is None:
            continue

        # every per-question p in the file, and every per-answer one
        per_question, per_answer = [], []
        for block in text.split("each question counted once")[1:]:
            head, _, tail = block.partition("each answer counted separately")
            per_question += re.findall(r"p = ([\d.]+)", head)
            per_answer += re.findall(r"p = ([\d.]+)", tail.split("=====")[0])

        printed = f"{p:.4f}"
        assert printed in per_question, (
            f"{row['label']!r} quotes p = {printed}, which is not a "
            f"per-question p-value in {path.name}. Per-question values there: "
            f"{sorted(set(per_question))}")
        assert row["level"] == "question", (
            f"{row['label']!r} quotes a per-question p but is labelled "
            f"{row['level']!r}")
        checked += 1

    assert checked >= 2, (
        f"only {checked} row(s) were actually checked against a two-level "
        f"source, so this test is close to vacuous")
