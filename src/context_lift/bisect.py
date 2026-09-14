# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Which of the rules broke that question? Halve the set, do not serialise.

Five rules go in with one round, the safety line says two held-out questions
broke, and somebody has to say WHICH rule did it. The naive answer is one rule
per round, and it is wrong twice: it costs about five times the answers, and a
single rule moves accuracy so little that a customer-size gold set returns five
UNDERPOWERED verdicts instead of one usable round.

    one rule per round   5 rounds x 36 questions x 2 arms = 360 answers
    five rules + bisect  72 + 6                           =  78 answers

The 100-question refusal on item-level leave-one-out (`classes.ITEM_LOO_MINIMUM`)
does not apply here, and the reason is worth stating because it looks like it
should. Item LOO is refused because it measures a LIFT, and a lift is a claim
about questions nobody has seen, so it needs statistics. "Which rule broke
q014" is a COUNT about one question that already broke. It needs q014 and a
few arms, not a hundred questions -- the same distinction the safety line rests
on.

WHAT A RULE IS HERE. One line beginning `- ` inside a rules file. Anything
else in the file -- a heading, a paragraph of prose -- travels with every arm
and is never blamed, because it is not a candidate. So a prose sentence that
breaks a question is invisible to this, the same way `attribute` cannot see a
rule that is not written as `- "phrase" -> TABLE.COLUMN`. Stated rather than
hidden: a tool with a blind spot nobody mentions is worse than one with a
blind spot in its docstring.

THE SEARCH ALWAYS TESTS THE LEFT HALF. If the question still breaks with only
the left half, the cause is in the left half; if it stops breaking, the cause
is in the right. That needs no arm for the full set, because the round that
just ran IS the full set and already reported the break.

TWO HONEST ENDINGS, and the difference is the reason this exists at all.
Narrowing by elimination can finish holding a rule the search never tested on
its own. That is a SUSPECT. Only a subset actually observed to reproduce the
break names a CULPRIT, so when the search ends on an untested rule it spends
one more arm to look. And if no single rule reproduces it, the answer is that
the rules interact -- reported as such, rather than blaming whichever rule the
arithmetic happened to land on.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Rule:
    """One candidate line, and where it came from."""

    file: str
    line: int
    text: str

    @property
    def key(self) -> str:
        """`file:line`, which is also what a person greps for."""
        return f"{self.file}:{self.line}"


def rules_in(spec) -> list[Rule]:
    """Every `- ` line in the spec's rules files, in the order they reach the
    model.

    The order matters and is not alphabetical by accident: `spec.rules` is
    already in tier order, broadest first, because that is the order
    `system_instruction` concatenates them in and therefore the order the
    precedence sentence resolves.
    """
    found = []
    for name, body in spec.rules:
        for number, line in enumerate((body or "").splitlines(), 1):
            if line.strip().startswith("- "):
                found.append(Rule(file=name, line=number, text=line.strip()))
    return found


def spec_with(spec, keep: set[str]):
    """A copy of the spec carrying only the rule lines in `keep`.

    Non-rule lines are kept in every arm. They are not candidates, so
    dropping them would change the context for a reason the search is not
    testing, and any result would then be about two changes at once.

    A rules file left with nothing but its heading is dropped, because
    `system_instruction` skips a body that strips to empty and an arm
    carrying an empty heading would hash differently for no reason a reader
    could see.
    """
    narrowed = copy.deepcopy(spec)
    rebuilt = []
    for name, body in spec.rules:
        lines = []
        for number, line in enumerate((body or "").splitlines(), 1):
            if not line.strip().startswith("- "):
                lines.append(line)
            elif f"{name}:{number}" in keep:
                lines.append(line)
        text = "\n".join(lines)
        if text.strip():
            rebuilt.append((name, text))
    narrowed.rules = rebuilt
    return narrowed


def arms_needed(count: int) -> int:
    """How many arms the narrowing takes for `count` candidate rules.

    `ceil(log2(n))`, and 0 for a single rule because there is nothing to
    narrow: with one candidate the round already named it.
    """
    return 0 if count <= 1 else math.ceil(math.log2(count))


@dataclass
class Result:
    """What the search found, and how sure it is.

    `culprit` is None in the one case that matters: no single rule reproduced
    the break on its own. Naming a rule there would be the tool inventing a
    verdict out of arithmetic.
    """

    question: str
    culprit: Rule | None = None
    confirmed: bool = False
    arms: int = 0
    trail: list[tuple[int, bool]] = field(default_factory=list)
    note: str = ""

    def line(self) -> str:
        if self.culprit is None:
            return (f"  {self.question}: no single rule reproduces it in "
                    f"{self.arms} arm(s). {self.note}")
        word = "breaks" if self.confirmed else "is suspected of breaking"
        return (f"  {self.culprit.key} {word} {self.question}"
                f"   ({self.arms} arm(s))\n"
                f"      {self.culprit.text}")


def find(question: str, rules: list[Rule], reproduces) -> Result:
    """Narrow to the rule that breaks `question`.

    `reproduces(subset)` takes a list of Rules and answers True when the
    question is still wrong with only those rules in the context. It is a
    callback so the search itself never touches a model: the caller decides
    whether an arm is asked, cached or faked, and this stays a pure function
    that a test can drive with a lambda.
    """
    result = Result(question=question)
    if not rules:
        result.note = "the spec carries no rule lines to blame"
        return result

    candidates = list(rules)
    # True only while the current candidate set has been SEEN to reproduce the
    # break. The full set starts True on the round's own evidence -- the
    # safety line already reported this question as broken -- which is what
    # saves an arm and makes the count log2 rather than log2 + 1.
    observed = True

    while len(candidates) > 1:
        middle = len(candidates) // 2
        left, right = candidates[:middle], candidates[middle:]
        result.arms += 1
        if reproduces(left):
            candidates, observed = left, True
        else:
            # The break is in the right half BY ELIMINATION, which is weaker
            # than having watched it happen. Tracked so the ending can say so.
            candidates, observed = right, False
        result.trail.append((len(candidates), observed))

    only = candidates[0]
    if not observed:
        # One more arm, because "confirmed, not suspected" is the whole point.
        result.arms += 1
        observed = reproduces([only])
        result.trail.append((1, observed))

    if observed:
        result.culprit, result.confirmed = only, True
        return result

    result.note = ("the rules interact: the question breaks with all of them "
                   "and with neither half alone, so no single line is the "
                   "cause. Read them together")
    return result


def broke_in_run(rows: list[dict]) -> list[str]:
    """The reserved questions the context broke: right without it, wrong with.

    Read from the round's own scored rows rather than retyped, because a
    question id typed by hand is a question id that can be wrong, and the
    whole point of bisecting is to stop guessing.

    Repetitions collapse by majority, the same rule `ablate` uses. Three
    answers to one question are not three facts, and a question that was
    right twice and wrong once is not broken.

    Only the reserved half counts. A working question that broke is a rule to
    fix, not a holdout read to spend: it can be asked as often as anyone
    likes, so there is nothing to protect and nothing to bisect against.
    """
    from .round import REPEAT_MARKER

    tally: dict[tuple[str, str], list[bool]] = {}
    split_of: dict[str, str] = {}
    for row in rows:
        scored_id = str(row.get("id") or "")
        question = scored_id.split(REPEAT_MARKER)[0]
        if not question:
            continue
        arm = str(row.get("arm") or "")
        correct = str(row.get("correct") or "").strip().lower()
        tally.setdefault((question, arm), []).append(correct in ("1", "true", "yes"))
        split_of[question] = str(row.get("split") or "")

    arms = sorted({arm for _question, arm in tally})
    if len(arms) != 2:
        return []
    control, treatment = arms[0], arms[1]

    def majority(votes: list[bool]) -> bool:
        return sum(votes) * 2 > len(votes)

    broke = []
    for question, half in sorted(split_of.items()):
        if half.lower() not in ("holdout", "reserved", "held_out", "held out"):
            continue
        without = tally.get((question, control))
        with_it = tally.get((question, treatment))
        if without and with_it and majority(without) and not majority(with_it):
            broke.append(question)
    return broke


def spend_note(spent: list[str], reserved: int) -> str:
    """The trade, printed before anything is asked.

    Asking a reserved question again is a second read of it, so a bisected
    question is spent: it joins the working half and never counts in a
    reported number again. With 36 golds, spending 2 to learn which rule is
    wrong is a good trade. With 12 it may not be, and the arithmetic belongs
    on screen rather than in a decision the tool makes for somebody.
    """
    left = reserved - len(spent)
    return (f"  this bisect spends {', '.join(spent)}.\n"
            f"  you have {reserved} reserved question(s); after this you have "
            f"{left}.\n"
            f"  a spent question moves to the working half and never counts "
            f"in a reported number again.")


def record_spent(path, ids: list[str], existing: set[str] | None = None) -> list[str]:
    """Add `ids` to the spent file and return everything in it, sorted.

    Appended rather than overwritten, and sorted so two bisects in any order
    leave the same file. The file is the only thing standing between a
    bisected question and its reappearance in a reported number, so it
    accumulates: forgetting an earlier bisect would quietly un-spend it.

    Written even when the search found nothing. The questions were read
    either way, and a holdout read that goes unrecorded is exactly the leak
    this file exists to stop.
    """
    import pathlib

    target = pathlib.Path(path)
    combined = sorted((existing or set()) | set(ids))
    target.write_text("\n".join(combined) + "\n")
    return combined


def refusal(safety_word: str) -> str | None:
    """Why not to bisect, or None to go ahead.

    A SAFE round has nothing to find, and the arms cost money.
    """
    if (safety_word or "").upper() == "SAFE":
        return ("the round's safety word is SAFE: this context broke nothing, "
                "so there is no question to bisect and the arms would be paid "
                "for nothing")
    return None
