# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Every practice is earned by a measurement, or listed as unproven.

`docs/research/README.md` opens by promising that each practice is printed beside
the measurement that earned it. Until this file existed, that promise was
enforced by nobody: `git grep PRACTICES -- tests/` found two incidental
mentions in test_mine.py and no check.

A practice with no number is advice, and advice is what this project exists
to replace. So a section either carries a measurement or its number appears
under "What is still unproven, stated plainly" -- and that section cannot be
quietly emptied, because then everything would pass.

The catalog check is the other half. A practice about writing context is only
actionable if it says which object holds it, so any section that claims an
"In a catalog" line has to name a real one.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "research" / "README.md"

# A measurement is a number with a unit a reader can check. Prose like
# "usually helps" matches none of these on purpose.
# Widened after practices 8 and 12 failed it: 8 counts "43 golds" and 12
# quotes dollars, and both are measurements. A unit list is the wrong shape
# for this check -- what makes something checkable is a number next to a
# countable noun or a currency symbol, so match that instead of enumerating
# every noun this project happens to count.
MEASUREMENT = re.compile(
    r"[-+]?\d+(?:\.\d+)?\s*(?:points?|%)"      # +12.2 points, 42.9%
    r"|p\s*[=<]\s*0?\.\d+"                      # p = 0.0290, p < 0.0001
    r"|\bn\s*=\s*\d+"                           # n=38
    r"|\b\d+\s+of\s+\d+\b"                      # 3 of 7
    r"|\$\s?\d"                                  # $149.61, $0.0987
    r"|\b\d[\d,]*\s+[a-z]{3,}"                   # 43 golds, 2,686 rows, 74 questions
)

# The objects that actually exist, from docs/REFERENCE.md and push.py.
# A practice naming something else is naming something nobody can create.
CATALOG_OBJECTS = (
    "overview", "queries aspect", "glossary term", "category",
    "requiredAspects", "rules file", "spec repo", "context-provenance",
)




def _sections() -> list[tuple[int, str, str]]:
    """(number, heading, body) for every numbered practice."""
    text = DOC.read_text()
    parts = re.split(r"^## (\d+)\. (.+)$", text, flags=re.M)
    out = []
    for i in range(1, len(parts) - 2, 3):
        out.append((int(parts[i]), parts[i + 1], parts[i + 2]))
    return out


def _unproven() -> str:
    text = DOC.read_text()
    start = text.index("## What is still unproven")
    end = text.find("\n## ", start + 4)
    return text[start:end if end > 0 else len(text)]


def test_the_document_has_practices_to_check():
    assert len(_sections()) >= 14, "found fewer practices than the README claims"


def test_the_unproven_section_exists_and_says_something():
    """If this section could be emptied, every practice below would pass by
    being declared a hypothesis."""
    body = _unproven()
    assert len(body.split()) > 30, "the unproven section is too short to be honest"
    assert "Practices" in body or "practice" in body


@pytest.mark.parametrize("number,heading,body", _sections(),
                         ids=lambda v: str(v)[:34])
def test_every_practice_carries_a_measurement_or_is_declared_unproven(
        number, heading, body):
    if MEASUREMENT.search(body):
        return
    unproven = _unproven()
    assert re.search(rf"\b{number}\b", unproven), (
        f"practice {number} ({heading!r}) states no measurement and is not "
        f"listed under 'What is still unproven'. Either measure it or say "
        f"plainly that it is a hypothesis."
    )


def test_practice_numbers_are_sequential_with_no_gaps_or_repeats():
    """A repeated number makes a citation ambiguous, and every practice here
    is cited by number from tests and commit messages."""
    numbers = [n for n, _, _ in _sections()]
    assert numbers == sorted(numbers), f"out of order: {numbers}"
    assert len(numbers) == len(set(numbers)), f"a number repeats: {numbers}"
    assert numbers == list(range(1, len(numbers) + 1)), f"a gap: {numbers}"


@pytest.mark.parametrize("number,heading,body", _sections(),
                         ids=lambda v: str(v)[:34])
def test_a_catalog_claim_names_an_object_that_exists(number, heading, body):
    """"Put it in the catalog" is not an instruction. Which object?"""
    if "In a catalog" not in body:
        return
    section = body[body.index("In a catalog"):]
    section = section[:section.find("\n\n**") if "\n\n**" in section else len(section)]
    assert any(name in section for name in CATALOG_OBJECTS), (
        f"practice {number} has an 'In a catalog' line naming none of "
        f"{CATALOG_OBJECTS}"
    )


def test_every_evidence_file_a_practice_cites_exists():
    """A citation to a missing file is worse than no citation: it reads as
    proof and cannot be checked."""
    text = DOC.read_text()
    cited = set(re.findall(r"`((?:docs|examples|scripts|tests)/[\w./-]+)`", text))
    assert cited, "no evidence paths are cited at all"
    missing = sorted(p for p in cited if not (ROOT / p).exists())
    assert not missing, f"cited but absent: {missing}"


def test_no_project_or_customer_name_is_in_the_practices():
    from leakcheck import leaks_in

    found = leaks_in(DOC.read_text())
    assert not found, f"docs/research/README.md names {found}"


def test_practice_15_token_counts_are_arithmetic_on_the_committed_run():
    """The two per-call figures in practice 15 are divisions, not quotes.

    They replaced `14,182 chars per call` and `90 chars per call`, which were
    wrong -- the real char figures are 21,058 and 125 -- and which nothing
    checked because a character count has no committed source. Prompt tokens
    do: the run records them on every `spent:` line.
    """
    spent = (ROOT / "docs" / "evidence" / "blob-vs-selection.txt").read_text()
    lines = re.findall(r"spent: (\d+) calls, ([\d,]+) prompt", spent)
    assert len(lines) == 6, f"expected six spend lines, found {len(lines)}"
    pairs = [(int(c), int(p.replace(",", ""))) for c, p in lines]

    for label, half in (("blob", pairs[0::2]), ("selection", pairs[1::2])):
        per_call = round(sum(p for _, p in half) / sum(c for c, _ in half))
        assert f"{per_call:,} prompt tokens per call" in DOC.read_text(), (
            f"the {label} arm averages {per_call:,} prompt tokens per call "
            f"and practice 15 does not say so")


def test_practice_15_does_not_claim_over_salience_is_proven():
    """Practice 15's rule is earned by the arm comparison (-1.6 pooled, -7.7
    on toxicology at p = 0.0290). Its MECHANISM is not: 20% against 9% at
    Fisher p = 0.0839. An earlier version of this section stated the
    mechanism as measured fact using hand counts of 37-against-9 and
    29%-against-5%, neither of which survived recomputation.
    """
    body = DOC.read_text()
    assert "neither reaches significance" in body
    assert "Fisher exact p = 0.0839" in body
    for withdrawn in ("selection  37 runs", "over-projects on 29%"):
        assert withdrawn not in body, (
            f"the withdrawn hand count {withdrawn!r} is back in the research archive")


def test_the_refuted_alternatives_stay_in_the_document():
    """Two explanations were tested and lost: the spread-value one (0 of 19)
    and the typo one (+0.6, p = 0.3075). Both are more deletable than the
    surviving hypothesis and both are why it survives."""
    body = DOC.read_text()
    assert "Of the 19 broken questions, **0** do." in body
    assert "p = 0.3075" in body
    assert "The typo is not" in body and "the explanation" in body
