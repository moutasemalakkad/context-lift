# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""A glossary term's tier and its structured links (M8 item 4, offline half).

M8 asked for org glossary, team categories, `synonym` and `related` links,
and for the export to fold synonyms in. Two of those are a file-reading
change and are done here. Filing a term under a team CATEGORY inside a
catalog glossary is a Knowledge Catalog capability nobody here has confirmed,
and it is not built: see the note at the end of this file.

The one thing these tests really protect is that adding a field to `Term` did
not rename every arm ever measured. `Arm.arm_id` hashes
`sorted((t.name, t.definition))`, on purpose, because a term's tier changes
nothing the agent receives.
"""
from __future__ import annotations

import pathlib

from context_lift.export import build_arms, read_spec
from context_lift.runners import Term

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = ROOT / "examples" / "thelook" / "spec"

# Measured on 2026-09-13, before Term gained tier/synonyms/related. If either
# moves, an arm was renamed and every number filed under the old id is
# orphaned.
CONTROL_ARM = "2404ed0d5166"
AUTHORED_ARM = "c6782e448a4b"


def _write(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    vocabulary = tmp_path / "vocabulary"
    vocabulary.mkdir(parents=True, exist_ok=True)
    (vocabulary / "terms.yaml").write_text(body)
    return tmp_path


def test_adding_a_tier_to_term_did_not_rename_the_measured_arms():
    control, authored = build_arms(read_spec(SPEC))
    assert control.arm_id == CONTROL_ARM
    assert authored.arm_id == AUTHORED_ARM


def test_a_term_with_no_tier_stated_is_the_estates():
    assert Term(name="revenue", definition="x").tier == "org"


def test_the_tier_is_read(tmp_path):
    spec = read_spec(_write(tmp_path, """
- name: revenue
  definition: Sale price of items neither Cancelled nor Returned.
- name: attach rate
  definition: Accessories per handset.
  tier: hardware-sales
"""))
    assert [(t.name, t.tier) for t in spec.terms] == [
        ("revenue", "org"), ("attach rate", "hardware-sales")]


def test_synonyms_are_both_structured_and_in_the_prose(tmp_path):
    """Both, for two different readers. The agent only ever sees the prose;
    a catalog needs the list to write a link between two term entries."""
    spec = read_spec(_write(tmp_path, """
- name: revenue
  definition: Sale price of items neither Cancelled nor Returned.
  also_called: [net sales, sales]
"""))
    term = spec.terms[0]
    assert term.synonyms == ("net sales", "sales")
    assert "Also called: net sales, sales." in term.definition


def test_related_terms_reach_the_prose_too(tmp_path):
    spec = read_spec(_write(tmp_path, """
- name: margin
  definition: Sale price minus product cost.
  related: [revenue, product cost]
"""))
    term = spec.terms[0]
    assert term.related == ("revenue", "product cost")
    assert "Related: revenue, product cost." in term.definition


def test_a_synonym_written_without_brackets_is_one_word_not_eleven(tmp_path):
    """`also_called: net sales` is a string, and iterating a string yields
    characters. Without a guard that becomes nine one-letter synonyms, and
    nothing complains."""
    spec = read_spec(_write(tmp_path, """
- name: revenue
  definition: Sale price.
  also_called: net sales
"""))
    assert spec.terms[0].synonyms == ("net sales",)


def test_blank_and_missing_lists_are_empty_not_crashes(tmp_path):
    spec = read_spec(_write(tmp_path, """
- name: revenue
  definition: Sale price.
  also_called: ["", "  "]
- name: margin
  definition: Sale price minus cost.
"""))
    assert spec.terms[0].synonyms == ()
    assert spec.terms[1].related == ()
    assert "Also called" not in spec.terms[0].definition


def test_the_team_category_half_is_honestly_absent():
    """The half that is NOT built, asserted so nobody reports M8 item 4 as
    finished by reading the tests above.

    `push._push_terms` takes one `glossary_id` for every term and builds its
    term list from glossary rule LINES in prose (push.py around line 426), not
    from `spec.terms`. So a term's tier reaches no catalog call today.

    Filing a term under a team category needs a Knowledge Catalog capability
    that has not been confirmed against a live catalog. Writing a guess would
    invent a glossary-id naming convention nobody can check.
    """
    import inspect

    from context_lift import push
    signature = inspect.signature(push._push_terms)
    assert "glossary_id" in signature.parameters
    assert "tier" not in signature.parameters, (
        "a tier now reaches the push. If that was built deliberately, the "
        "note in this test is stale -- rewrite it and say what was confirmed "
        "against a live catalog"
    )
