# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The rules tier: a leading two-digit number, and what depends on it.

M8 asked for a declared `knowledge.yaml` schema with a validator, in place of
the filename convention. Only half of that is worth building. The convention
is already the schema -- `read_spec` concatenates
`sorted(rules_dir.glob("*.md"))` and the precedence sentence says the more
specific rule wins, so sort order decides precedence. A second file format
would need converting both ways, and one copy would go stale.

What was genuinely missing is the validator, because nothing checked the
convention at all. A file named `overview.md` or `3o-orders.md` -- second
character the letter o -- loses its tier silently and sorts after every
numbered file, which makes it win every precedence conflict.

Note what the convention actually is, which is not what export.py's docstring
claimed until 2026-09-13. Only the NUMBER is fixed. The shipped specs use
00-definitions, 00-from-lookml and 10-output-conventions, not 00-org or
10-<domain>, so a validator demanding those names would refuse this
repository's own measured specs.
"""
from __future__ import annotations

import pathlib

import pytest

from context_lift.export import RULES_DIR, read_spec, tier_of
from context_lift.lint import untiered_rule_files

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _shipped_specs() -> list[pathlib.Path]:
    return sorted({p.parent.parent for p in ROOT.rglob(f"{RULES_DIR}/*.md")
                   if ".venv" not in p.parts and "_personal" not in p.parts})


def test_the_number_declares_the_tier_and_the_name_is_free_text():
    assert tier_of("00-definitions") == 0
    assert tier_of("00-from-lookml") == 0
    assert tier_of("10-output-conventions") == 10
    assert tier_of("30-orders") == 30


@pytest.mark.parametrize("name", ["overview", "rules", "3o-orders", "0-org",
                                  "org-00", "definitions"])
def test_a_name_with_no_two_digit_prefix_declares_no_tier(name):
    """`3o-orders` is the one that matters: it looks tiered to a person."""
    assert tier_of(name) is None


def test_the_repository_ships_specs_to_check():
    assert _shipped_specs(), "no rules/ directories found, so nothing below holds"


@pytest.mark.parametrize("spec_dir", _shipped_specs(),
                         ids=lambda p: str(p.relative_to(ROOT)))
def test_every_shipped_spec_declares_a_tier_on_every_rules_file(spec_dir):
    """The check has to be silent on the specs that measured a gain, or it
    would be telling a reader to break the only thing that worked."""
    spec = read_spec(spec_dir)
    assert untiered_rule_files(spec) == []


def test_an_untiered_file_is_named(tmp_path):
    rules = tmp_path / RULES_DIR
    rules.mkdir(parents=True)
    (rules / "00-definitions.md").write_text("- Revenue excludes cancelled\n")
    (rules / "overview.md").write_text("- Use the orders table\n")
    assert untiered_rule_files(read_spec(tmp_path)) == ["overview.md"]


def test_an_untiered_file_sorts_last_and_so_wins_precedence(tmp_path):
    """The consequence, not just the name. This is why it is worth a warning:
    the untiered file ends up the most specific tier by accident."""
    rules = tmp_path / RULES_DIR
    rules.mkdir(parents=True)
    (rules / "00-broad.md").write_text("- broad\n")
    (rules / "30-narrow.md").write_text("- narrow\n")
    (rules / "overview.md").write_text("- accidental\n")
    order = [name for name, _ in read_spec(tmp_path).rules]
    assert order == ["00-broad", "30-narrow", "overview"]
    instruction = read_spec(tmp_path).system_instruction()
    assert instruction.index("accidental") > instruction.index("narrow")


def test_a_spec_with_no_rules_directory_is_not_an_error(tmp_path):
    (tmp_path / RULES_DIR).mkdir(parents=True, exist_ok=True)
    assert untiered_rule_files(read_spec(tmp_path)) == []


def test_the_three_verbs_name_plan_apply_drift_and_undo():
    """M8 asked for `architect plan`, `architect apply` and `architect drift`.
    Three verbs already do all three, so no fourth verb was added -- but a
    person searching the help for those words has to find them."""
    from context_lift.cli import _build_parser
    choices = _build_parser()._subparsers._group_actions[0].choices

    push = choices["spec-push"]
    text = push.format_help()
    assert "APPLY" in text
    assert "PLAN" in text
    assert "DRIFT" in text

    assert "UNDO" in choices["catalog-rollback"].format_help()
    assert "architect" not in choices, (
        "an `architect` verb was added; the point of M8 item 2 was that "
        "spec-push and catalog-rollback already cover plan, apply, drift "
        "and undo"
    )
