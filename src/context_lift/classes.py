# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""What one whole class of context was worth, by taking it away.

A round says the written context helped. It does not say which part. The
worklist ranks single items but cannot price them: at the set sizes a real
estate produces, no per-item test has the power to decide anything.

A CLASS is different. Removing every worked example, or every glossary term,
is one change large enough for a 30-question set to see. So leave-one-class-out
is the one attribution test a small estate can actually resolve, and item-level
leave-one-out is refused below 100 working questions with the reason printed
rather than run and reported as if it meant something.

**On the working set only, never the held-out one.** Each class costs one more
arm over the same questions, so pricing four classes is four more comparisons.
The held-out set is read once per round, and these are many rounds; reading it
per class would spend the one honest number on a diagnostic.

**A removal, not a rebuild.** Each arm is the full treatment arm with one field
emptied, so the only difference is the class. Building each arm up from the
control instead would let a later edit change two things at once and credit the
class with both.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

# Every class, and the Arm fields it owns. Named here rather than derived from
# the dataclass, because two fields can belong to one class - a description on
# a table and on a column are one written thing to a curator - and because a
# field added later must be assigned deliberately, not swept into whichever
# class its name sorts near.
CLASSES: dict[str, tuple[str, ...]] = {
    "rules": ("system_instruction",),
    "terms": ("terms",),
    "examples": ("examples",),
    "authored descriptions": ("table_descriptions", "field_descriptions"),
    "synonyms": ("table_synonyms", "field_synonyms"),
    "tags": ("table_tags", "field_tags"),
    "relationships": ("relationships",),
}

# Below this many working questions, an item-level leave-one-out is refused.
# From the ladder rule: a set can only resolve a lift above about
# 100/sqrt(n) points, so 100 questions is 10 points and anything smaller
# cannot see a single item's effect. Printing a number it cannot support is
# the mistake this whole tool exists to prevent.
ITEM_LOO_MINIMUM = 100


@dataclass(frozen=True)
class Removed:
    """One arm with one class taken out, and what was in it."""

    name: str
    arm: object
    held: int

    def line(self) -> str:
        return (f"  without {self.name}: arm {self.arm.arm_id}, "
                f"{self.held} item(s) removed")


def _count(arm, fields: tuple[str, ...]) -> int:
    """How much was in a class, for the report. Characters for the rules,
    because one instruction is one field but not one thing."""
    total = 0
    for name in fields:
        value = getattr(arm, name, None)
        if isinstance(value, str):
            total += len(value.split("\n")) if value.strip() else 0
        elif isinstance(value, dict):
            total += sum(len(v) if isinstance(v, (dict, list)) else 1
                         for v in value.values())
        elif isinstance(value, list):
            total += len(value)
    return total


def without(arm, name: str):
    """The treatment arm with one class emptied. The arm itself is untouched."""
    if name not in CLASSES:
        raise SystemExit(
            f"no such class {name!r}. The classes are: "
            + ", ".join(sorted(CLASSES)))
    stripped = copy.deepcopy(arm)
    stripped.label = f"{arm.label} minus {name}"
    for attribute in CLASSES[name]:
        current = getattr(stripped, attribute)
        setattr(stripped, attribute,
                "" if isinstance(current, str)
                else {} if isinstance(current, dict) else [])
    return stripped


def ladder(arm) -> list[Removed]:
    """One arm per class that this arm actually carries, in name order.

    A class the arm does not carry is skipped rather than run: an arm with no
    glossary terms measured against itself-minus-terms is two identical arms,
    and a round on two identical arms costs money to prove nothing.
    """
    out = []
    for name in sorted(CLASSES):
        held = _count(arm, CLASSES[name])
        if not held:
            continue
        out.append(Removed(name=name, arm=without(arm, name), held=held))
    return out


def item_loo_refusal(n_working: int) -> str | None:
    """Why a per-item leave-one-out is refused, or None when it is allowed.

    A sentence rather than a silent skip, because the person asking for it has
    a reason and needs to know what would make it possible.
    """
    if n_working >= ITEM_LOO_MINIMUM:
        return None
    return (f"{n_working} working questions cannot resolve one item: a set "
            f"that size sees a lift above about {100 / max(n_working, 1) ** 0.5:.0f} "
            f"points and a single item is smaller than that. {ITEM_LOO_MINIMUM} "
            "questions would. Price a whole class instead, which is one change "
            "big enough for this set to see")


def summary(removed: list[Removed], n_working: int) -> str:
    """The plan, and what it will cost, before anything is asked."""
    if not removed:
        return ("this arm carries no class that can be removed, so there is "
                "nothing to price")
    lines = [f"{len(removed)} class(es) to price, one arm each, "
             f"over {n_working} working question(s):"]
    lines += [item.line() for item in removed]
    refusal = item_loo_refusal(n_working)
    if refusal:
        lines.append("")
        lines.append(f"per-item leave-one-out is refused: {refusal}")
    return "\n".join(lines)


@dataclass
class Priced:
    """One class, priced: what removing it did to the working questions."""

    name: str
    held: int
    lift_pp: float
    low_pp: float
    high_pp: float
    fixed: int
    broke: int
    n: int
    # The two rates the difference was taken between. Without them a reader
    # cannot tell "both arms at 55%" from "both arms at 5%", and those mean
    # opposite things: the first is a class that genuinely adds nothing, the
    # second is an agent that is failing for a reason no context will fix.
    full_pct: float = 0.0
    without_pct: float = 0.0

    @property
    def carries(self) -> bool:
        """True when removing this class made things measurably worse.

        The sign is the point and it is easy to read backwards. `lift_pp` is
        the REMOVED arm minus the full arm, so a class that was doing work
        gives a NEGATIVE lift: taking it away lost points. A positive lift
        means the arm got better without it.
        """
        return self.high_pp < 0

    @property
    def costs(self) -> bool:
        """True when removing it made things measurably BETTER: delete it."""
        return self.low_pp > 0

    def line(self) -> str:
        verdict = ("carries its weight" if self.carries
                   else "the arm is better WITHOUT it" if self.costs
                   else "not shown either way")
        return (f"  {self.name:24s} {-self.lift_pp:+6.1f} points "
                f"[{-self.high_pp:+.1f}, {-self.low_pp:+.1f}]  "
                f"{self.without_pct:.0f}% without it against {self.full_pct:.0f}% with, "
                f"{self.fixed} fixed {self.broke} broke, "
                f"{self.held} item(s)  {verdict}")


def price(full_report, removed_reports: dict) -> list[Priced]:
    """Each class's worth, from the full arm against each arm missing one.

    `removed_reports` is {class name: (EvalReport, items held)}. Each is
    compared against the same full arm, so every number on the page rests on
    the same baseline and two classes can be read beside each other.

    The lift is reported SIGN-FLIPPED in `line`, as "what the class is worth",
    because "removing examples cost 8 points" is what a person wants and
    "removing examples scored -8" is what the arithmetic produces. The raw
    sign stays on the field so nothing downstream has to guess.
    """
    from .ablate import ablate

    out = []
    for name, (report, held) in sorted(removed_reports.items()):
        comparison = ablate(full_report, report)
        low, high = comparison.lift_interval()
        test = comparison.clustered or comparison.mcnemar
        out.append(Priced(
            name=name, held=held,
            lift_pp=comparison.lift * 100, low_pp=low * 100, high_pp=high * 100,
            fixed=test.c, broke=test.b,
            n=comparison.n_questions or comparison.n_paired,
            full_pct=comparison.without_context.point * 100,
            without_pct=comparison.with_context.point * 100))
    return out


def priced_text(priced: list[Priced], n_working: int) -> str:
    """The priced classes, worst news first, with what to do about each."""
    if not priced:
        return "nothing was priced"
    ranked = sorted(priced, key=lambda p: p.lift_pp, reverse=True)
    lines = [f"what each class is worth, on {n_working} working question(s):",
             "  (positive means the class is doing work; removing it lost points)",
             "  the two rates are there because a difference of zero between "
             "55% and 55% is not",
             "  the same finding as a difference of zero between 5% and 5%"]
    lines += [item.line() for item in ranked]

    delete = [p.name for p in ranked if p.costs]
    keep = [p.name for p in ranked if p.carries]
    lines.append("")
    if delete:
        lines.append(f"the arm is measurably better without: {', '.join(delete)}. "
                     "Removing one and re-running is the cheapest test there is.")
    if keep:
        lines.append(f"doing work: {', '.join(keep)}.")
    if not delete and not keep:
        lines.append("no class was shown either way at this many questions. "
                     "That is not the same as every class being worthless: "
                     "it is the set being too small to tell them apart.")
    return "\n".join(lines)
