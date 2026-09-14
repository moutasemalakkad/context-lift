# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Behavior tests for the leak-checked example bank.

The dangerous case is the template twin: a pool question whose SQL shape
matches a scored question's answer. The first real bank shipped 25 of those
before the check caught them; these tests pin the ban loop.
"""
import json

from context_lift.examples import ExampleBank, build_example_bank, save_bank
from context_lift.leakage import check_leakage


POOL = [
    ("p1", "how many rooms are in each building?",
     "SELECT building, COUNT(*) FROM rooms GROUP BY building"),
    ("p2", "list the names of all departments",
     "SELECT dept_name FROM departments"),
    ("p3", "total enrolled students per course",
     "SELECT course, SUM(enrolled) FROM courses GROUP BY course"),
]


def test_similar_examples_are_retrieved_for_each_target():
    targets = [("t1", "how many rooms does building 7 have?",
                "SELECT COUNT(*) FROM rooms WHERE building = '7'")]
    bank = build_example_bank(POOL, targets, per_question=2)
    assert "GROUP BY building" in bank.contexts["t1"]  # the rooms example won
    assert bank.banned_ids == ()
    assert bank.iterations == 1


def test_a_template_twin_is_banned_and_the_final_bank_is_clean():
    # p_twin's SQL differs from the target's gold only by a literal, which is
    # the same answer shape - keeping it would score memorization as skill.
    twin = ("p_twin", "list rooms in building 9",
            "SELECT room FROM rooms WHERE building = '9'")
    targets = [("t1", "list rooms in building 3",
                "SELECT room FROM rooms WHERE building = '3'")]
    bank = build_example_bank(POOL + [twin], targets, per_question=2)
    assert "p_twin" in bank.banned_ids
    bundle = "\n\n".join(bank.contexts.values())
    assert check_leakage(targets, bundle).clean


def test_a_pool_of_nothing_but_twins_yields_a_clean_empty_bank():
    # Every candidate is the target's answer in disguise, so the only clean
    # bank is an empty one. The ban count in the summary is what tells the
    # curator their pool was unusable - not a leaky number later.
    targets = [("t1", "list rooms in building 3",
                "SELECT room FROM rooms WHERE building = '3'")]
    twins = [(f"p{i}", f"list rooms in building {i}",
              f"SELECT room FROM rooms WHERE building = '{i}'")
             for i in range(4)]
    bank = build_example_bank(twins, targets, per_question=2)
    assert len(bank.banned_ids) == 4
    assert "SELECT" not in bank.contexts["t1"]
    bundle = "\n\n".join(bank.contexts.values())
    assert check_leakage(targets, bundle).clean


def test_saved_bank_is_the_shape_generate_consumes(tmp_path):
    bank = ExampleBank(contexts={"t1": "Q: x\nSQL: y"}, banned_ids=(), iterations=1)
    path = save_bank(bank, tmp_path / "bank.json")
    assert json.loads(path.read_text()) == {"t1": "Q: x\nSQL: y"}


def test_summary_reports_the_ban_count_so_zero_and_many_read_differently():
    bank = ExampleBank(
        contexts={"t1": "x"}, banned_ids=("a", "b"), iterations=3
    )
    assert "banned 2 template twins" in bank.summary()
    assert "3 build(s)" in bank.summary()
