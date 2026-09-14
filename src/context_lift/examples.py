# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Curate a leak-checked bank of worked examples - the strongest context lever.

Retrieved worked examples (a similar question with its correct SQL) are what
production systems call verified queries, and on a 596-question enterprise
holdout they moved accuracy 5.5% -> 20.2% where every schema-shaped hint
stalled. The mechanically dangerous part is not the retrieval - it is
LEAKAGE: benchmark and warehouse question sets contain near-duplicate
"template twins", and an example that is a twin of a scored question turns
the measurement into memorization.

So the bank is built in a loop: retrieve, run the same normalized-substring
check `leak` uses, ban every pool entry that carries a scored question's
shape, rebuild - until the check reports clean. The first real build needed
four iterations and banned 46 twins; skipping that step would have shipped
25 leaked answers and an inflated number.
"""
from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass

from .leakage import check_leakage, normalize_sql

_STOP_WORDS = frozenset(
    "the a of for in on and or to with by all each what which list show give "
    "provide is are was were how many much per their its".split()
)


def question_words(text: str) -> set[str]:
    """Content words, crudely stemmed (trailing s dropped) for overlap scoring."""
    words = set()
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if word in _STOP_WORDS:
            continue
        words.add(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    return words


@dataclass(frozen=True)
class ExampleBank:
    """Per-question example blocks, plus the audit trail of how they got clean.

    `contexts` maps a target question id to its examples text - exactly the
    shape `generate` consumes per question. `banned_ids` are pool entries
    removed because they carried a target question's shape; the count is part
    of the result on purpose, so "0 banned" and "46 banned" read differently.
    """

    contexts: dict[str, str]
    banned_ids: tuple[str, ...]
    iterations: int

    def summary(self) -> str:
        sizes = [len(v) for v in self.contexts.values()]
        empty = sum(1 for v in self.contexts.values() if "\nQ:" not in v)
        lines = [
            f"example bank for {len(self.contexts)} questions "
            f"(avg {sum(sizes) // max(1, len(sizes)):,} chars)",
            f"  leak-checked clean after {self.iterations} build(s); "
            f"banned {len(self.banned_ids)} template twins from the pool",
        ]
        if empty:
            lines.append(
                f"  WARNING: {empty} of {len(self.contexts)} questions got NO "
                "examples - the pool is too small or too close to the targets "
                "(pool == targets always ends here: a question may never be "
                "its own example)")
        return "\n".join(lines)


def build_example_bank(
    pool: list[tuple[str, str, str]],
    targets: list[tuple[str, str, str]],
    per_question: int = 3,
    max_example_sql_chars: int = 1200,
    max_iterations: int = 10,
) -> ExampleBank:
    """Build worked-example context for each target question, leak-checked.

    Args:
        pool: (id, question, gold_sql) rows examples MAY come from - the
            curate split, a verified-query repository, mined history.
        targets: (id, question, gold_sql) rows that will be SCORED. Their
            golds are what the bank must not contain; they never appear in
            any example.
        per_question: examples retrieved per target, by word overlap.
        max_example_sql_chars: long golds teach little per token; skip them.
        max_iterations: safety bound on the ban-and-rebuild loop.

    Raises:
        RuntimeError: if the bank still leaks after max_iterations - a pool
            that is nearly all twins of the targets cannot make a clean bank,
            and silently returning a leaky one would be worse than failing.
    """
    banned: set[str] = set()
    target_ids = {t[0] for t in targets}
    for iteration in range(1, max_iterations + 1):
        eligible = [
            row for row in pool
            if row[0] not in banned
            and row[0] not in target_ids
            and len(row[2]) <= max_example_sql_chars
        ]
        pool_words = {row[0]: question_words(row[1]) for row in eligible}
        by_id = {row[0]: row for row in eligible}

        contexts: dict[str, str] = {}
        used: dict[str, list[str]] = {}
        for target_id, target_question, _gold in targets:
            overlap = question_words(target_question)
            picked = sorted(
                pool_words, key=lambda pid: -len(overlap & pool_words[pid])
            )[:per_question]
            used[target_id] = picked
            block = [
                "VERIFIED EXAMPLES (similar questions on this database, "
                "with correct SQL):"
            ]
            for pid in picked:
                _pid, example_question, example_sql = by_id[pid]
                block.append(f"\nQ: {example_question}\nSQL: {example_sql}")
            contexts[target_id] = "\n".join(block)

        bundle = "\n\n".join(contexts.values())
        report = check_leakage(targets, bundle)
        if report.clean:
            return ExampleBank(
                contexts=contexts,
                banned_ids=tuple(sorted(banned)),
                iterations=iteration,
            )

        # Ban every pool entry whose SQL carries a leaked target's shape;
        # if a leak came through question text instead, ban the examples
        # that were retrieved for it.
        all_used = {pid for picks in used.values() for pid in picks}
        newly: set[str] = set()
        for leaked_id in report.leaked_ids:
            leaked_gold = next(t[2] for t in targets if t[0] == leaked_id)
            shape = normalize_sql(leaked_gold)
            for pid in all_used:
                if shape and shape in normalize_sql(by_id[pid][2]):
                    newly.add(pid)
        if not newly:
            for leaked_id in report.leaked_ids:
                newly.update(used.get(leaked_id, []))
        banned |= newly

    raise RuntimeError(
        f"example bank still leaks after {max_iterations} iterations; "
        f"banned {len(banned)} pool entries and leaks remain - the pool is "
        "too close to the scored questions to make a clean bank"
    )


def save_bank(bank: ExampleBank, path: str | pathlib.Path) -> pathlib.Path:
    """Write the bank as {question_id: examples_text} JSON for `generate`."""
    path = pathlib.Path(path)
    path.write_text(json.dumps(bank.contexts, indent=0))
    return path
