# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Which context item to look at first, and why.

A round gives one number for a whole arm. On a customer estate that number is
usually UNDERPOWERED, because a 30-question holdout cannot show a lift under
about 25 points. The item grain is what is left, and it is not a score: it is
a ranked list of leads, each with the evidence beside it so a person can
disagree.

Every field here is countable from a finished round. None of them is a
p-value, and none of them should be read as one. At the set sizes a real
estate produces, no per-item test has the power to say anything, so this
orders what to read rather than deciding what is true.

    cited          the item was named in an answer's citations
    fixed, broke   the item's arm got that question right, or wrong, where
                   the other arm did the opposite
    lookups        the answer was a stored example run as written, not
                   text-to-SQL, so it says nothing about writing SQL
    memorised      the item repeats a scored question's own literals

The strongest signal is the cheapest: an item cited by nothing, in a round
where the context helped, is an item nobody needed. An item cited on every
question that broke is the first thing to read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# A citation names its source with a kind and an id, as the parser writes it:
# `term:glossary_completed_order`, `example:ex_q0`.
_CITATION = re.compile(r"^(?P<kind>term|example|uri):(?P<id>.*)$")

# A literal a question and an item share. Numbers under three digits and short
# words are not evidence: "1" and "the" appear everywhere.
_LITERAL = re.compile(r"'([^']{2,})'|\b(\d{3,})\b")


@dataclass
class Item:
    """One context item, with everything counted about it in one round."""

    name: str
    kind: str = ""
    cited: int = 0
    fixed: int = 0
    broke: int = 0
    lookups: int = 0
    memorised: bool = False
    written_from: int = 0

    @property
    def rank(self) -> tuple:
        """Sort key: what to read first, worst news at the top.

        An item that broke something outranks everything, however much else it
        did. Then one that was never cited at all, because a written item
        nobody's answer referred to is either invisible to the model or not
        needed. Then the rest by how much they moved.
        """
        return (-self.broke, self.cited > 0, -self.fixed, self.name)

    def line(self, citations_recorded: bool = True) -> str:
        parts = [f"{self.kind or 'item'} {self.name}"]
        parts.append(f"cited {self.cited}" if citations_recorded
                     else "citations not recorded")
        if self.fixed or self.broke:
            parts.append(f"fixed {self.fixed} broke {self.broke}")
        if self.lookups:
            parts.append(f"{self.lookups} answered by a stored example, not written")
        if self.memorised:
            parts.append("repeats a scored question's own values")
        if self.written_from:
            parts.append(f"written from {self.written_from} question(s)")
        return "  " + ", ".join(parts)


@dataclass
class Worklist:
    """The items of one round, ranked, with the counts that ranked them."""

    items: list[Item] = field(default_factory=list)
    scored_questions: int = 0
    # False when the run carried no citations column at all. "nobody cited
    # this item" and "nobody recorded citations" are different claims, and a
    # worklist that prints the first when it means the second sends a person
    # to delete context that was working.
    citations_recorded: bool = True

    def ranked(self) -> list[Item]:
        return sorted(self.items, key=lambda item: item.rank)

    def uncited(self) -> list[Item]:
        """Items no answer referred to. The cheapest thing to prune.

        Empty when the run recorded no citations: nothing can be called
        uncited on evidence that was never collected.
        """
        if not self.citations_recorded:
            return []
        return [item for item in self.ranked() if item.cited == 0]

    def text(self) -> str:
        if not self.items:
            return "no context items in this arm, so there is nothing to rank"
        lines = [f"worklist: {len(self.items)} item(s) over "
                 f"{self.scored_questions} scored question(s)"]
        if not self.citations_recorded:
            lines.append("  this run recorded no citations, so nothing below "
                         "is known to be cited or uncited. Re-run the round to "
                         "collect them.")
        lines += [item.line(self.citations_recorded) for item in self.ranked()]
        uncited = self.uncited()
        if uncited:
            lines.append("")
            lines.append(
                f"{len(uncited)} item(s) were never cited. An item no answer "
                "referred to either did not reach the model or was not needed; "
                "removing one and re-running is the cheapest test there is.")
        lines.append("")
        lines.append("These are leads, not results. At this many questions no "
                     "per-item test has the power to decide anything, so this "
                     "orders what to read.")
        return "\n".join(lines)


def _literals(text: str) -> set[str]:
    return {a or b for a, b in _LITERAL.findall(text or "")}


def build(rows: list[dict], items: list[tuple[str, str, str]],
          written_from: dict[str, int] | None = None) -> Worklist:
    """The worklist for one round.

    `rows` is what `round --run-out` wrote: one per (question, arm), carrying
    `correct` and the answer's `citations`.

    `items` is (name, kind, text) for everything the treatment arm carried: a
    term, a worked example, a rule line. The text is read only to spot an item
    that repeats a scored question's own values.

    Fixed and broke are counted per item from the questions it was cited on:
    an item cited on a question the treatment got right and the control got
    wrong is credited with the fix. That is a lead and not a cause. Two items
    cited on the same question both get the credit, because nothing here can
    say which of them did the work.
    """
    by_question: dict[str, dict[str, dict]] = {}
    for row in rows:
        by_question.setdefault(row["id"], {})[row["arm"]] = row

    counted = {name: Item(name=name, kind=kind,
                          written_from=(written_from or {}).get(name, 0))
               for name, kind, _text in items}
    text_of = {name: text for name, _kind, text in items}

    scored = 0
    for question_id, arms in by_question.items():
        control, treatment = arms.get("control"), arms.get("treatment")
        if not control or not treatment:
            continue
        was = _as_bool(control.get("correct"))
        now = _as_bool(treatment.get("correct"))
        if was is None or now is None:
            continue
        scored += 1
        lookup = bool((treatment.get("matched_example") or "").strip())
        for cited in _cited_names(treatment.get("citations")):
            item = counted.get(cited)
            if item is None:
                continue
            item.cited += 1
            if lookup:
                item.lookups += 1
            elif not was and now:
                item.fixed += 1
            elif was and not now:
                item.broke += 1

    # An item that repeats a scored question's own values may be fitted to it
    # rather than saying anything general. Printed beside every count so a
    # lift is read with it, never instead of it.
    question_literals: set[str] = set()
    for arms in by_question.values():
        for row in arms.values():
            question_literals |= _literals(row.get("gold_sql"))
            break
    for name, item in counted.items():
        shared = _literals(text_of.get(name, "")) & question_literals
        item.memorised = bool(shared)

    return Worklist(items=list(counted.values()), scored_questions=scored,
                    citations_recorded=any("citations" in row for row in rows))


def _as_bool(value) -> bool | None:
    """`correct` as written to CSV: '' means excluded, not wrong."""
    if isinstance(value, bool):
        return value
    text = str(value or "").strip()
    if text in ("True", "true", "1"):
        return True
    if text in ("False", "false", "0"):
        return False
    return None


def _cited_names(citations) -> list[str]:
    """The item names in one answer's citations, however they were stored."""
    if isinstance(citations, str):
        parts = [c for c in citations.split(";") if c.strip()]
    else:
        parts = list(citations or [])
    names = []
    for part in parts:
        match = _CITATION.match(part.strip())
        names.append(match.group("id") if match else part.strip())
    return names
