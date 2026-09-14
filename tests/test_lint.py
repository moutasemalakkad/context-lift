# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The context linter, and the over-fitting check the M18 spec demanded.

The spec asked four questions before this was built. Three are answered by
tests here; the fourth is answered in the module docstring.

  1. Is "no subject" detectable without a model, or would a regex just fit
     one file? -> test_no_subject_does_not_fire_on_well_formed_rules runs it
     over the committed thelook rules, which are well-formed prose. It must
     stay silent on all of them.
  2. Refuse or warn? -> every REFUSE cites a measurement, and
     test_every_refusal_cites_the_measurement_that_earned_it enforces that.
  3. Does it exist already? -> the module docstring names dbt's Semantic
     Layer validations and a published Feasibility Validator, and says what
     was borrowed from each.
  4. Is the idea backwards? -> partly, and the report says so out loud.
     test_passing_is_not_a_certificate pins the sentence.
"""
from __future__ import annotations

import pathlib

import pytest

from context_lift.export import read_spec
from context_lift.lint import REFUSE, WARN, identifiers_in, lint, no_subject

THELOOK = pathlib.Path("examples/thelook/spec")


def test_no_subject_fires_on_the_lines_that_motivated_it():
    """One estate's mined metrics file, five lines of this shape."""
    assert no_subject("Compute it as `sum(count_of_eligible_days) "
                      "- sum(count_of_oos_days)`.")
    assert no_subject("Compute it as sum(a) - sum(b).")
    assert no_subject("Use this for any revenue question.")
    assert no_subject("Apply the value to every order line.")


def test_no_subject_does_not_fire_on_well_formed_rules():
    """THE OVER-FITTING CHECK the spec asked for.

    A regex that fires on one file and nothing else is fitting, not
    detecting. These are real rules that measured a real gain, so a single
    hit here is a false positive and the check is wrong.
    """
    fired = []
    for path in sorted((THELOOK / "rules").glob("*.md")):
        for line in path.read_text().splitlines():
            body = line.strip()
            if body.startswith("- ") and no_subject(body[2:]):
                fired.append(f"{path.name}: {body[:60]}")
    assert fired == [], f"false positives on real rules: {fired}"


def test_a_rule_that_names_its_object_is_not_subjectless():
    """The fault is a pronoun object, not an imperative opening. An earlier
    draft fired on "Exclude cancelled orders", which is a good rule."""
    assert not no_subject("Exclude cancelled orders from any revenue number.")
    assert not no_subject("Compute revenue as the sum of `sale_price`.")
    assert not no_subject("Filter to completed orders only.")


def test_identifiers_are_taken_from_backticks_and_snake_case():
    """A bare single word is NOT taken: "status" is as often the English
    word as the column, and a check that fires on English gets switched off."""
    assert identifiers_in("from `shipped_at` to `delivered_at`") == {
        "shipped_at", "delivered_at"}
    assert identifiers_in("the sale_price of each order_item") == {
        "sale_price", "order_item"}
    assert identifiers_in("a completed order has status Complete") == set()


def test_every_refusal_cites_the_measurement_that_earned_it():
    """Severity is earned. A REFUSE with no number is an opinion with a
    strong voice, and the M18 spec singled that out as the risk."""
    report = lint(read_spec(THELOOK))
    for finding in report.findings:
        assert finding.basis, f"{finding.check} has no basis"
        if finding.severity == REFUSE:
            assert any(ch.isdigit() for ch in finding.basis), \
                f"{finding.check} refuses without a number"


def test_an_unrouted_example_bank_is_refused_with_its_measurement():
    report = lint(read_spec(THELOOK))
    refusals = {f.check for f in report.refusals}
    assert "examples without routing" in refusals
    basis = next(f.basis for f in report.refusals
                 if f.check == "examples without routing")
    assert "-24.3" in basis


def test_declaring_the_routing_clears_that_refusal(tmp_path):
    """A marker file, not inference. Whether examples are routed is a fact
    about how the round is run and a linter must not guess it."""
    spec_dir = tmp_path / "spec"
    (spec_dir / "rules").mkdir(parents=True)
    (spec_dir / "rules" / "00.md").write_text("- Revenue excludes returns.\n")
    (spec_dir / "examples").mkdir()
    (spec_dir / "examples" / "bank.jsonl").write_text(
        '{"question": "q", "sql": "SELECT 1"}\n')

    before = lint(read_spec(spec_dir))
    assert "examples without routing" in {f.check for f in before.refusals}

    (spec_dir / "examples" / "ROUTED").write_text("routed per question\n")
    after = lint(read_spec(spec_dir))
    assert "examples without routing" not in {f.check for f in after.refusals}


def test_a_skipped_check_is_not_a_passed_check():
    """A report that does not distinguish them reads as a certificate."""
    report = lint(read_spec(THELOOK))
    assert any("unknown column" in note for note in report.skipped)
    assert "SKIPPED" in report.summary()


def test_passing_is_not_a_certificate(tmp_path):
    """The fourth question the spec raised: if context quality is only
    knowable by measurement, is a linter backwards? Partly, and the report
    has to say so rather than imply a clean bill of health."""
    spec_dir = tmp_path / "spec"
    (spec_dir / "rules").mkdir(parents=True)
    (spec_dir / "rules" / "00.md").write_text(
        "- Revenue excludes cancelled and returned items.\n")

    report = lint(read_spec(spec_dir))

    assert report.passed
    text = report.summary()
    assert "Passing is not a certificate" in text
    assert "Only a round can tell you" in text


def test_repeated_findings_are_grouped_not_printed_one_per_line():
    """An early version printed "no identifier" fourteen times for one file.
    A linter that repeats itself is a linter people switch off."""
    text = lint(read_spec(THELOOK)).summary()
    assert text.count("no identifier") == 1
    assert "x14" in text


def test_the_verb_exits_nonzero_on_a_refusal(capsys):
    """So a script can put it in front of a paid round."""
    from context_lift.cli import main
    code = main(["lint", "--spec", str(THELOOK)])
    assert code == 1
    assert "REFUSE" in capsys.readouterr().out


def test_the_verb_exits_zero_when_there_is_nothing_to_report(tmp_path, capsys):
    from context_lift.cli import main
    spec_dir = tmp_path / "spec"
    (spec_dir / "rules").mkdir(parents=True)
    (spec_dir / "rules" / "00.md").write_text("- Revenue excludes returns.\n")
    assert main(["lint", "--spec", str(spec_dir)]) == 0
    # Not "nothing to report": a prose rule raises a no-identifier warning,
    # which is the ordinary case. What must always appear is the caveat.
    assert "Passing is not a certificate" in capsys.readouterr().out
