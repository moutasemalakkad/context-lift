# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The first draft of the rules a failure bucket is asking for.

Triage says what kind of wrong the agent is; this reads the failing questions
of one bucket, gold beside prediction, and drafts the business rules that
would have made the difference, each citing the questions it is for. A rule
with no citation is dropped: it was invented, not observed. The draft is a
file a person reviews and installs with `context-write`; nothing here writes
into the working rules.

Every rule in the draft has the one shape the other readers take,
`- "phrase" -> meaning`: `attribute` suspects a rule by the `table.column`
its meaning names, `catalog-push` makes a glossary term from the phrase and
the meaning. The prompt asks for that shape and `parse_rules` rewrites the
shapes a model still returns into it.

The questions are shown as q1..qN, their position in the prompt, and a
citation comes back as the id of the question at that position. The prompt
used to head each question with its own id while the two example lines cited
"(q1, q7)"; a model copied the examples, every citation named a question
that was never shown, and a whole run drafted 0 rules.

Exports: build_rules_prompt, parse_rules, normalize_rule, render_draft,
Draft, DRAFT_HEADER, OUTPUT_TOKEN_CAP.

LLM stage, one call per draft, priced by the backend.
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import Sequence

from .gate import _squashed, filter_literals

DRAFT_HEADER = "<!-- drafted by contextlift draft-rules from bucket {bucket}, {n} questions; review, then context-write -->"

# the output cap of the one call, and the output side of its estimate: the
# price printed before the call is a ceiling, not a guess
OUTPUT_TOKEN_CAP = 1200

PROMPT = """You are writing business context for a text-to-SQL agent.

Below are questions the agent answered wrongly. For each: the question, the
gold SQL a person verified, the SQL the agent wrote, and what the scorer saw.
The failure shape is {bucket}: {meaning}.

Write at most {max_rules} rules that would have led the agent to the gold SQL.
Each rule is one line of exactly this shape:

- "phrase" -> meaning (q1, q7)

The phrase is the business words, in double quotes. The meaning names the
column as table.column when the rule is about a column, and is a plain
sentence otherwise: a definition, a convention, a default filter, a join,
an ordering. The line ends with the labels of the questions it is for, in
parentheses, exactly as they are written below (q1, q2, ... and no other
name). Two examples of the shape:

- "net revenue" -> orders.amount minus orders.refund_amount (q2, q5)
- "orders" -> only rows with orders.status = 'complete', unless the question asks for all (q1)

Do not paste SQL. Do not put backticks around names. Do not write a rule
that only one question could ever need if a more general rule covers it. Do
not restate the schema.

{items}

Rules:"""

ITEM = """### q{label}
question: {question}
gold SQL: {gold_sql}
agent SQL: {pred_sql}
scorer: {detail}
"""


def build_rules_prompt(items: list[dict], bucket: str, meaning: str, max_rules: int = 8) -> str:
    """The one prompt, with the questions labelled q1..qN in the order given.

    The label is the position in the prompt, never the question's own id:
    the ids the examples cite and the ids the items carry have to be the
    same kind of name, or the model cites the examples. `parse_rules` reads
    a citation back through the same order. A label is also shorter than an
    id, so the prompt is a little cheaper.
    """
    body = "\n".join(ITEM.format(label=label, **item)
                     for label, item in enumerate(items, start=1))
    return PROMPT.format(bucket=bucket, meaning=meaning, max_rules=max_rules, items=body)


# the shapes a model still writes for a rule, whatever the prompt asks for.
# On one estate a 16-line draft was all `- "Unrestricted Stock" refers to
# `unrestricted_stock`.`, which attribute read as 0 rules and catalog-push
# as 0 terms. The phrase may come unquoted before an arrow or a colon.
_ARROW = re.compile(r'^- "?(?P<phrase>[^"]+?)"?\s*->\s*(?P<meaning>.+)$')
_VERB = re.compile(r'^- "(?P<phrase>[^"]+)"\s+(?:refers to|means|maps to|is)\s+(?P<meaning>.+)$',
                   re.IGNORECASE)
_QUOTED_COLON = re.compile(r'^- "(?P<phrase>[^"]+)":\s+(?P<meaning>.+)$')
_COLON = re.compile(r'^- "?(?P<phrase>[^":]+?)"?:\s+(?P<meaning>.+)$')

# Words a model puts in front of a sentence to label it, not to name a
# business term. `- Note: the fiscal year starts in February` used to come
# back as a term called "Note", which catalog-push would then write into the
# glossary. Checked only for the unquoted colon shape: a model that wrote
# "Note" in quotes meant the word.
_LABEL_WORDS = frozenset({
    "note", "notes", "warning", "caution", "important", "tip", "hint",
    "example", "examples", "caveat", "reminder", "summary", "todo", "nb",
    "rule", "rules", "general",
})

# the citation at the end of a rule line: (q1, q7), a period after it allowed
_CITATION = re.compile(r"\(([^()]*)\)\s*\.?\s*$")

# one label inside a citation: q1, or Q1
_LABEL = re.compile(r"^[Qq](\d+)$")


@dataclass(frozen=True)
class Draft:
    """One reply, read: the rules that survived and why the rest did not.

    A draft of 0 rules is a real outcome and used to print as one number, so
    the reply had to be reproduced by hand to see what the model had said.
    `rule_lines` counts the lines that looked like a rule and
    `dropped_citations` the labels that named no question shown.
    """

    rules: list[tuple[str, list[str]]]
    rule_lines: int = 0
    dropped_citations: int = 0
    # rules cited by ONE question that restate that question's own filter
    # values. Not dropped: a one-question rule is sometimes right. Marked, so
    # the reviewer sees them and `attribute` can say when one is credited
    memorised: list[str] = dataclasses.field(default_factory=list)


def normalize_rule(rule: str) -> str:
    """The rule in the `- "phrase" -> meaning` shape when its phrase can be
    read, else as written: a sentence with no phrase to quote is still a
    rule. Backticks are removed in every case: a name in backticks is not
    an identifier to the regex that looks for `table.column`.

    A bullet that opens with a label word (`- Note: ...`) keeps its sentence:
    the word labels the line, it is not the term the line defines."""
    rule = rule.replace("`", "")
    for shape in (_ARROW, _VERB, _QUOTED_COLON, _COLON):
        found = shape.match(rule)
        if found:
            phrase = found.group("phrase").strip()
            if shape is _COLON and phrase.lower() in _LABEL_WORDS:
                return rule
            meaning = found.group("meaning").strip().rstrip(".").rstrip()
            return f'- "{phrase}" -> {meaning}'
    return rule


def memorises_one_answer(rule: str, cited: Sequence[str],
                         gold_sql: dict[str, str]) -> bool:
    """True when a rule cited by ONE question restates that question's values.

    Two cold runs produced lines like

        - "RCCORE 104" -> COURSE.DEPARTMENT = "RCCORE" AND COURSE.NUMBER = 104

    which is one question's answer written into the context. `attribute` then
    credits it with fixing that question, so it reads as a win. It inflates
    the working-half reading and does nothing to the reserved one, which is
    the definition of overfitting.

    The prompt already asks for general rules, and an instruction is advice.
    This is the check. It does not drop the line: a rule that only one
    question needs is sometimes the right rule, and a reviewer decides.
    """
    if len(cited) != 1:
        return False
    values = filter_literals(gold_sql.get(cited[0], ""))
    if not values:
        return False
    body = _squashed(rule)
    return any(_squashed(value) in body for value in values if len(_squashed(value)) > 2)


def parse_rules(text: str, shown_ids: Sequence[str],
                gold_sql: dict[str, str] | None = None) -> Draft:
    """The rules of one reply: the line in the one shape, without its
    citation, and the ids of the questions it cites.

    `shown_ids` is the question ids in the order `build_rules_prompt` showed
    them, so the label `q3` comes back as the third of them. A label outside
    1..N was invented, and a rule left with no citation goes with it: it was
    not observed on any question the model saw.
    """
    rules: list[tuple[str, list[str]]] = []
    memorised: list[str] = []
    rule_lines = 0
    dropped = 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        rule_lines += 1
        found = _CITATION.search(line)
        labels = [c.strip() for c in found.group(1).split(",")] if found else []
        cited: list[str] = []
        for label in labels:
            match = _LABEL.match(label)
            position = int(match.group(1)) if match else 0
            if 1 <= position <= len(shown_ids):
                cited.append(shown_ids[position - 1])
            else:
                dropped += 1
        if not cited:
            continue
        rule = normalize_rule(line[: found.start()].rstrip())
        rules.append((rule, cited))
        if gold_sql and memorises_one_answer(rule, cited, gold_sql):
            memorised.append(rule)
    return Draft(rules=rules, rule_lines=rule_lines, dropped_citations=dropped,
                 memorised=memorised)


def render_draft(rules: list[tuple[str, list[str]]], bucket: str, n_questions: int,
                 memorised: Sequence[str] = ()) -> str:
    lines = [DRAFT_HEADER.format(bucket=bucket, n=n_questions), ""]
    memorised = set(memorised)
    for rule, cited in rules:
        note = ", one question's own values" if rule in memorised else ""
        lines.append(f"{rule}   <!-- {', '.join(cited)}{note} -->")
    lines.append("")
    return "\n".join(lines)
