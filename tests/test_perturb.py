# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The perturbation suite: injected defects, arm hashes, and the free pass.

M19 asks for a suite that breaks one property of good context at a time, as
DATA rather than as hand-edited spec copies, with each arm hashed so two runs
of the same defect are the same experiment.

The paid half -- what each defect costs in points -- is not run here and is not
run by the script either. What IS free, and is what these tests pin, is the
cross-check: each perturbation names the linter check it should trip, so
running the linter over each damaged spec says for nothing which defects a
free check already catches and which ones only a paid round can find.

TWO THINGS THIS FILE EXISTS TO STOP, both of which happened while writing it:

- Reporting a defect as detected when the clean spec raised the same warning
  anyway. Detection is a COUNT going up, not a check name appearing.
- Reporting a check that could not run as a check that missed. One is a gap
  in the linter and the other is a gap in the spec, and on the committed
  thelook spec it is the second: that spec carries 0 column names, so three
  of the six checks never execute at all.
"""
from __future__ import annotations

import pathlib

import pytest

from context_lift.export import Spec, TableSchema, build_arms
from context_lift.perturb import (NO_CHECK, SUITE, Injected, Perturbation,
                                  by_name, inject, plan_text)

TABLE = "shop.orders"


def a_spec(body: str, columns: dict | None = None) -> Spec:
    spec = Spec(root=pathlib.Path("(test)"))
    spec.schemas[TABLE] = TableSchema(columns=columns or {})
    spec.rules.append(("00-definitions.md", body))
    return spec


# ---------------------------------------------------------------------------
# the suite is data

def test_every_perturbation_is_data_not_an_edited_copy():
    """A directory of hand-edited spec copies drifts the moment the original
    changes and cannot be re-run against a new model."""
    assert len(SUITE) >= 7
    for item in SUITE:
        assert isinstance(item, Perturbation)
        assert item.replacements, f"{item.name} changes nothing"
        assert item.breaks, f"{item.name} does not say what it breaks"


def test_the_names_are_unique_because_an_arm_is_filed_under_one():
    names = [p.name for p in SUITE]
    assert len(names) == len(set(names)), names


def test_the_positive_control_is_present_and_named_as_one():
    """A perturbation suite with no positive control measures its own
    insensitivity. If removing a rule known to be load-bearing does not
    register, the harness is broken and not the context."""
    control = by_name("positive-control-remove-capitalisation")

    assert "positive-control" in control.name
    assert "load-bearing" in control.breaks
    assert "THE CONTROL" in control.note


def test_an_unknown_name_lists_the_known_ones():
    with pytest.raises(SystemExit) as raised:
        by_name("no-such-defect")
    assert "strip-subject" in str(raised.value)


# ---------------------------------------------------------------------------
# applying one

def test_applying_a_perturbation_leaves_the_original_alone():
    """The suite applies seven defects to one spec in a loop. If apply()
    edited in place, defect two would be injected into defect one."""
    original = a_spec("- Margin is sale price minus product cost.")
    before = original.rules[0][1]

    by_name("conflicting-definitions").apply(original)

    assert original.rules[0][1] == before


def test_a_perturbation_that_matches_nothing_is_an_error_not_a_no_op():
    """The failure that would be worst to allow. A defect that silently
    changed nothing would be scored as 'this kind of bad context costs
    nothing', when in fact it was never injected."""
    spec = a_spec("- Something else entirely.")

    with pytest.raises(SystemExit) as raised:
        by_name("strip-subject").apply(spec)

    assert "matched 0 of 1" in str(raised.value)
    assert "never injected" in str(raised.value)


def test_conflicting_definitions_adds_a_second_definition_of_the_same_term():
    """Not invented: third-party context for this same public dataset
    disagrees about margin in exactly this way."""
    spec = a_spec("- Margin is sale price minus product cost.")

    damaged = by_name("conflicting-definitions").apply(spec)

    body = damaged.rules[0][1]
    assert body.count("Margin is") == 2
    assert "product cost" in body and "inventory landed cost" in body


# ---------------------------------------------------------------------------
# arms

def test_every_perturbation_produces_a_different_arm_from_the_clean_one():
    """The hash is what makes a defect reproducible. An arm that hashes like
    the clean spec means nothing was injected, and `inject` refuses."""
    from context_lift.export import read_spec

    root = pathlib.Path(__file__).resolve().parent.parent / "examples" / "thelook" / "spec"
    if not root.is_dir():
        pytest.skip("the committed thelook spec is absent")
    spec = read_spec(root)
    _control, clean = build_arms(spec)

    injected = inject(spec)

    ids = {item.arm_id for item in injected}
    assert clean.arm_id not in ids
    assert len(ids) == len(injected), "two defects produced the same arm"


def test_two_runs_of_the_same_defect_are_the_same_experiment():
    from context_lift.export import read_spec

    root = pathlib.Path(__file__).resolve().parent.parent / "examples" / "thelook" / "spec"
    if not root.is_dir():
        pytest.skip("the committed thelook spec is absent")
    spec = read_spec(root)

    first = {i.perturbation.name: i.arm_id for i in inject(spec)}
    second = {i.perturbation.name: i.arm_id for i in inject(spec)}

    assert first == second


# ---------------------------------------------------------------------------
# the free linter pass, and the two ways of reading it wrongly

def test_detection_is_a_count_going_up_not_a_check_name_appearing():
    """`no identifier` fires on the clean thelook spec already. The first
    version of this reported `strip-binding` as a miss for that reason, when
    the check does fire — on one more line."""
    from context_lift.export import read_spec

    root = pathlib.Path(__file__).resolve().parent.parent / "examples" / "thelook" / "spec"
    if not root.is_dir():
        pytest.skip("the committed thelook spec is absent")
    spec = read_spec(root)

    binding = [i for i in inject(spec)
               if i.perturbation.name == "strip-binding"][0]

    assert binding.verdict == "CAUGHT FREE", binding.verdict


def test_a_check_that_could_not_run_is_not_reported_as_a_miss():
    """One is a gap in the linter, the other a gap in the spec, and saying
    the wrong one sends somebody to fix the wrong thing."""
    caught = Injected(perturbation=by_name("unknown-column"), arm_id="x",
                      skipped=("unknown column and ambiguous column: the spec "
                               "carries no column names",))

    assert not caught.check_could_run
    assert "COULD NOT RUN" in caught.verdict
    assert "MISSED" not in caught.verdict


def test_a_check_that_ran_and_stayed_quiet_is_a_miss():
    missed = Injected(perturbation=by_name("unknown-column"), arm_id="x",
                      caught_by=(), skipped=())

    assert missed.check_could_run
    assert "MISSED" in missed.verdict


def test_a_defect_no_free_check_claims_says_so_rather_than_looking_clean():
    """`conflicting-definitions` has no linter check by design. That is the
    finding, not an omission: a defect no free check sees is one only a paid
    round will ever catch."""
    assert by_name("conflicting-definitions").m18_check == NO_CHECK

    quiet = Injected(perturbation=by_name("conflicting-definitions"),
                     arm_id="x")
    assert "as designed" in quiet.verdict


# ---------------------------------------------------------------------------
# the plan

def test_the_plan_states_the_call_count_before_anything_is_spent():
    plan = plan_text([Injected(perturbation=by_name("strip-subject"),
                               arm_id="abc123")], "clean99", questions=104)

    assert "1 arm(s) x 104 question(s) x 1 rep(s) = 104 model call(s)" in plan
    assert "round --dry-run" in plan


def test_the_plan_refuses_to_claim_generality():
    """A defect costing ten points on a clean seven-table public schema may
    cost nothing on a 900-dataset estate. Every write-up has to say so, so
    the plan says it first."""
    plan = plan_text([Injected(perturbation=by_name("strip-subject"),
                               arm_id="abc")], "clean", questions=10)

    assert "cannot produce: generality" in plan
    assert "one agent on one schema" in plan
