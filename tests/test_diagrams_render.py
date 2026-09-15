# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Every mermaid diagram in the repository can actually be parsed.

A mermaid block that GitHub cannot parse does not degrade -- it renders as a
red error box where the diagram should be, and nothing in a test suite or a
review catches it, because the markdown around it is fine.

That shipped. `docs/research/README.md` carried a node label written
`K[gap +10.8 [-8.3, +29.8]...]`. An unquoted `[` inside a `[...]` label ends
the label early, so the whole diagram failed with `Parse error on line 11`
and the first picture in the walkthrough was an error box.

The rule these checks enforce: a label that contains any bracket, brace or
parenthesis must be quoted.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP = (".venv", "_personal", "node_modules")

# NODE["label"] or NODE[label] or NODE(["label"]) and the curly/round forms
_LABEL = re.compile(r'[A-Za-z_][\w]*\s*(?:\(\[|\[|\(|\{\{|\{)\s*(?P<body>[^\n]*?)\s*(?:\]\)|\]|\)|\}\}|\})')


def unquoted_bracket(text: str) -> str | None:
    """The offending label in `text`, or None. One function, so the sweep
    below and the historical fixture cannot drift apart."""
    for match in _LABEL.finditer(text):
        body = match.group("body")
        if body.startswith('"') and body.endswith('"'):
            continue              # quoted: brackets are legal inside
        if re.search(r"[\[\]{}()]", body):
            return body
    return None


def _blocks() -> list[tuple[pathlib.Path, int, str]]:
    found = []
    for path in sorted(ROOT.rglob("*.md")):
        if any(part in SKIP for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r"```mermaid\n(.*?)```", text, re.S):
            line = text[:match.start()].count("\n") + 1
            found.append((path, line, match.group(1)))
    return found


def test_the_repository_has_diagrams_to_check():
    """If this fails, the checks below are passing on an empty set."""
    assert _blocks(), "no mermaid blocks found, so nothing below proves anything"


def test_no_node_label_holds_an_unquoted_bracket():
    """The exact defect that shipped."""
    bad = []
    for path, line, block in _blocks():
        for offset, text in enumerate(block.splitlines()):
            if unquoted_bracket(text):
                bad.append(f"{path.relative_to(ROOT)}:{line + offset + 1}: "
                           f"{text.strip()[:90]}")
    assert not bad, ("these node labels hold an unquoted bracket, so the "
                     "diagram will not render:\n  " + "\n  ".join(bad))


def test_every_class_a_node_uses_is_defined():
    """`:::name` with no matching `classDef name` is silently unstyled, which
    is how a diagram loses its colour without anyone noticing."""
    bad = []
    for path, line, block in _blocks():
        used = set(re.findall(r":::([A-Za-z_][\w]*)", block))
        defined = set(re.findall(r"classDef\s+([A-Za-z_][\w]*)", block))
        for name in sorted(used - defined):
            bad.append(f"{path.relative_to(ROOT)}:{line}: ':::{name}' has no classDef")
    assert not bad, "\n  ".join([""] + bad)


# The line exactly as it shipped in docs/research/README.md, which GitHub rejected
# with: Parse error on line 11 ... Expecting 'SQE', ... got 'SQS'
SHIPPED_BROKEN = ("    J --> K[gap +10.8 [-8.3, +29.8]<br/>includes zero: "
                  "the rules were not<br/>fitted to the questions they came from]")

# the same content, fixed the way the repository now writes it
SHIPPED_FIXED = ('    J --> K["round 2 minus round 1: +10.8 points,<br/>'
                 'interval -8.3 to +29.8, which includes zero."]')


def test_the_checker_rejects_the_line_that_actually_shipped():
    """Without this, the sweep above could pass by matching nothing at all."""
    assert unquoted_bracket(SHIPPED_BROKEN), (
        "the checker accepts the exact line GitHub refused to parse, so it "
        "would not have caught the defect it exists for")


def test_the_checker_accepts_the_fixed_line():
    """And it must not be so strict that the correct form fails, or the next
    person deletes it."""
    assert unquoted_bracket(SHIPPED_FIXED) is None
