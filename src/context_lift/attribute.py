# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Attribute a paired-ablation flip to the context rule that caused it.

`ablate` says the context broke 3 questions and fixed 1. The next question is
always WHICH PIECE of the context did it — a context bundle is many rules, and
the fix is editing one rule, not throwing the bundle away. This module answers
it without any model call:

1. The context is parsed into RULES, each mapping a business phrase to one or
   more `TABLE.COLUMN` identifiers (the glossary line format that `semantic`
   emits and a curator writes).
2. For every question the ablation flipped, the two arms' SQL is diffed at the
   identifier level: which tables and columns did the with-context arm use
   that the baseline arm did not?
3. A rule is a SUSPECT for a flip when it mentions an identifier the context
   introduced into that question's SQL. Suspects are ranked by how many
   questions they broke, net of fixed.

This is the cheap, deterministic tier of context attribution. The literature
calls the principled version leave-one-out — re-run the arm once per rule
removed — but LOO over N rules costs N paid runs, so practical systems narrow
the candidate set first and only LOO the survivors (AttriBoT, arXiv
2411.15102; ProCut, arXiv 2508.02053, treats prompt segments as features the
same way). The report therefore ends with the exact next step: the shortlist
of rules worth a leave-one-out arm, instead of all N.

A suspect is a lead, not a verdict: two rules naming the same table are
indistinguishable here, and a rule can break a question without adding any new
identifier (by changing a join path). Flips no rule explains are listed as
unattributed rather than forced onto the nearest rule — a wrong attribution
sends the curator to edit the wrong line, which is worse than "unknown".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ablate import AblationReport, _collapse_reps


@dataclass(frozen=True)
class Rule:
    """One line of context: a business phrase mapped to warehouse identifiers.

    `identifiers` holds full `TABLE.COLUMN` strings; matching uses both the
    table part and the column part, since generated SQL aliases tables and a
    column name may appear without its table.
    """

    id: str
    phrase: str
    identifiers: tuple[str, ...]
    line_no: int
    text: str


@dataclass(frozen=True)
class RuleImpact:
    """Every flip one rule is suspected of causing."""

    rule: Rule
    broke: tuple[tuple[str, int], ...]  # (question id, reps affected)
    fixed: tuple[tuple[str, int], ...]
    introduced: tuple[str, ...]  # the identifiers that tied the rule to a flip
    # which cheap signal tied the rule to a flip: introduced_identifier,
    # phrase_in_literal, literal_changed_on_rule_column. A signal marks a
    # suspect; confirmation is a leave-one-out arm.
    signals: tuple[str, ...] = ()

    @property
    def net_broke(self) -> int:
        return len(self.broke) - len(self.fixed)


@dataclass(frozen=True)
class AttributionReport:
    """Suspect rules for an ablation's flips, worst offender first."""

    impacts: tuple[RuleImpact, ...]
    unattributed_broke: tuple[tuple[str, int], ...]
    unattributed_fixed: tuple[tuple[str, int], ...]
    n_rules: int
    n_broke: int
    n_fixed: int
    # bullet lines with no `table.column` in them: a draft naming bare
    # columns, or sentence rules. Said out loud, because "0 rules in the
    # context" over a 16-line rules file read as a broken file on one estate
    n_rule_lines_without_identifier: int = 0

    def summary(self) -> str:
        head = (f"attribution: {self.n_broke} broke / {self.n_fixed} fixed, "
                f"{self.n_rules} rules in the context")
        if self.n_rule_lines_without_identifier:
            head += (f" ({self.n_rule_lines_without_identifier} more rule lines carry "
                     "no table.column and cannot be attributed)")
        lines = [head]
        offenders = [i for i in self.impacts if i.broke]
        helpers = [i for i in self.impacts if i.fixed and not i.broke]
        if offenders:
            lines.append("")
            lines.append("suspect rules for regressions (worst first):")
            for impact in offenders:
                names = ", ".join(
                    f"{qid}{f' x{n}' if n > 1 else ''}" for qid, n in impact.broke
                )
                lines.append(f"  {impact.rule.id}  {impact.rule.text}")
                lines.append(
                    f"        broke {names}  "
                    f"(introduced: {', '.join(impact.introduced)})"
                )
        if helpers:
            lines.append("")
            lines.append("rules pulling their weight:")
            for impact in helpers:
                names = ", ".join(
                    f"{qid}{f' x{n}' if n > 1 else ''}" for qid, n in impact.fixed
                )
                lines.append(f"  {impact.rule.id}  fixed {names}")
        # When NOTHING in the context can be attributed, every flip listed
        # below is unattributed for one shared reason, and saying it per flip
        # sends the reader chasing join paths that are not the problem.
        #
        # Measured 2026-09-13 on the committed thelook round: 24 fixed and 4
        # broke, p = 0.0002, and 0 of 15 rule lines attributable. Each of the
        # 28 was printed as "likely a join-path or phrasing effect". The
        # actual reason was that not one rule named a table or a column, so
        # no rule could ever match any flip. Those are different findings and
        # only one of them is actionable.
        nothing_to_match = self.n_rules == 0
        if nothing_to_match and (self.unattributed_broke or self.unattributed_fixed):
            lines.append("")
            lines.append(
                "NOTHING IN THIS CONTEXT IS ATTRIBUTABLE: "
                f"{self.n_rule_lines_without_identifier} rule line(s) and not "
                "one names a table or a column, so no flip below can be tied "
                "to a rule. This is a property of the context, not of the "
                "questions.")
            lines.append(
                "  The rules may still be working: run `classes` to price the "
                "whole set, which needs no identifiers. To learn WHICH rule "
                "did what, name the table and column each one applies to.")
        for label, items in (
            ("broke", self.unattributed_broke),
            ("fixed", self.unattributed_fixed),
        ):
            if items:
                names = ", ".join(
                    f"{qid}{f' x{n}' if n > 1 else ''}" for qid, n in items
                )
                lines.append("")
                why = ("" if nothing_to_match else
                       "  (no rule matches the SQL change — likely a "
                       "join-path or phrasing effect, not an identifier)")
                lines.append(f"unattributed {label}: {names}{why}")
        if offenders:
            lines.append("")
            lines.append(
                "next step: re-run the with-context arm with each suspect rule "
                "removed (leave-one-out), one rule at a time, worst first."
            )
        return "\n".join(lines)


# A glossary rule line: - "business phrase" -> TABLE.COL, TABLE2.COL2
_RULE_LINE = re.compile(r'^\s*-\s*"(?P<phrase>[^"]+)"\s*->\s*(?P<targets>.+)$')
# A dotted name in a rule's target. The first segment may carry hyphens,
# because a BigQuery project id may (`acme-analytics-p.sales.fct_orders`);
# later segments may not. Only the last two segments are kept, since that is
# the pair `_rule_tokens` matches against the SQL. Without the hyphen the
# first segment was read from the last hyphen on, so that name parsed as
# `p.sales` and tied the rule to the token `P`, which every alias matches.
_IDENTIFIER = re.compile(
    r"(?<![\w.-])([A-Za-z_][\w-]*(?:\.[A-Za-z_]\w*){1,3})(?![\w-])")


def _last_two_segments(name: str) -> str:
    """The last two dot-separated parts of a dotted name, as written."""
    parts = name.split(".")
    return ".".join(parts[-2:])

# Identifier-shaped tokens in SQL. Deliberately coarse: aliases, keywords and
# function names all match, and that is fine — rules are matched against the
# DIFFERENCE between two arms' tokens, and noise present in both cancels out.
_SQL_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse_rules(text: str) -> list[Rule]:
    """Parse rule lines out of a context file; non-rule lines are prose.

    Only a rule naming at least one `table.column` comes back: that is what
    a flip is matched against. `count_rule_lines_without_identifier` counts
    the rest, so the report can say they exist.

    Rules are numbered in the order they come back, R001 first. Numbering by
    line number instead read a draft's two header lines as rules and started
    at R003. Line numbers are 1-based and kept in `line_no`, so a report can
    still point at the exact line to edit.
    """
    rules = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        match = _RULE_LINE.match(line)
        if not match:
            continue
        identifiers = tuple(
            _last_two_segments(name) for name in _IDENTIFIER.findall(match.group("targets")))
        if not identifiers:
            continue
        rules.append(
            Rule(
                id=f"R{len(rules) + 1:03d}",
                phrase=match.group("phrase").strip().lower(),
                identifiers=identifiers,
                line_no=line_no,
                text=line.strip(),
            )
        )
    return rules


def count_rule_lines_without_identifier(text: str) -> int:
    """Bullet lines parse_rules left out: no `table.column` to match a flip
    against. Reported beside the rule count, not silently dropped."""
    attributable = {rule.line_no for rule in parse_rules(text)}
    return sum(1 for line_no, line in enumerate(text.splitlines(), start=1)
               if line.strip().startswith("- ") and line_no not in attributable)


def sql_tokens(sql: str) -> set[str]:
    """Uppercased identifier-shaped tokens in one SQL string."""
    return {t.upper() for t in _SQL_TOKEN.findall(sql or "")}


_SQL_LITERAL = re.compile(r"'((?:[^']|'')*)'|\"([^\"]*)\"")


def sql_literals(sql: str) -> set[str]:
    """String literals in one SQL string, as written: a case change
    ('Under Construction' -> 'under construction') is the change that
    breaks a query, so it must not be normalized away here."""
    return {(a or b) for a, b in _SQL_LITERAL.findall(sql or "")}


def _literal_on_rule_column(rule: Rule, with_sql: str, without_sql: str) -> bool:
    """True when a predicate on one of the rule's columns carries a different
    literal in the two arms (`Status = 'x'` became `Status = 'y'`)."""
    for identifier in rule.identifiers:
        column = identifier.partition(".")[2] or identifier
        pattern = re.compile(
            r"\b" + re.escape(column) + r"\b\s*(?:=|<>|!=|IN|NOT IN|LIKE)\s*\(?\s*"
            r"('(?:[^']|'')*'|\"[^\"]*\")", re.IGNORECASE)
        before = {m.group(1) for m in pattern.finditer(without_sql or "")}
        after = {m.group(1) for m in pattern.finditer(with_sql or "")}
        if after and after != before:
            return True
    return False


def _rule_tokens(rule: Rule) -> set[str]:
    """The tokens that tie a rule to SQL: each table name and each column name."""
    tokens = set()
    for identifier in rule.identifiers:
        table, _, column = identifier.partition(".")
        tokens.add(table.upper())
        tokens.add(column.upper())
    return tokens


def attribute(
    ablation: AblationReport,
    sql_without: dict[str, str],
    sql_with: dict[str, str],
    rules: list[Rule],
    n_rule_lines_without_identifier: int = 0,
) -> AttributionReport:
    """Map each flip in an ablation to the rules that plausibly caused it.

    Args:
        ablation: the paired report whose broke_ids/fixed_ids get explained.
        sql_without: observation id -> predicted SQL in the baseline arm.
            Pooled observation ids (`dw036#rep1`) must appear as-is, so each
            repetition's own SQL is diffed, not one rep standing in for all.
        sql_with: same, for the with-context arm.
        rules: parsed context rules (parse_rules).
        n_rule_lines_without_identifier: how many rule lines parse_rules
            left out (count_rule_lines_without_identifier), for the summary.

    A rule is suspected of BREAKING a question when the with-context SQL uses
    an identifier the baseline SQL did not, and the rule mentions it. Fixes
    attribute the same way. One flip can implicate several rules and one rule
    several flips; both directions are reported.
    """
    per_rule_broke: dict[str, list[str]] = {r.id: [] for r in rules}
    per_rule_fixed: dict[str, list[str]] = {r.id: [] for r in rules}
    per_rule_introduced: dict[str, set[str]] = {r.id: set() for r in rules}
    per_rule_signals: dict[str, set[str]] = {r.id: set() for r in rules}
    rule_by_id = {r.id: r for r in rules}
    unattributed_broke: list[str] = []
    unattributed_fixed: list[str] = []

    for flip_ids, per_rule, unattributed in (
        (ablation.broke_ids, per_rule_broke, unattributed_broke),
        (ablation.fixed_ids, per_rule_fixed, unattributed_fixed),
    ):
        for observation_id in flip_ids:
            with_sql = sql_with.get(observation_id, "")
            without_sql = sql_without.get(observation_id, "")
            introduced = sql_tokens(with_sql) - sql_tokens(without_sql)
            new_literals = sql_literals(with_sql) - sql_literals(without_sql)
            matched = False
            for rule in rules:
                signals = set()
                hits = _rule_tokens(rule) & introduced
                if hits:
                    signals.add("introduced_identifier")
                    per_rule_introduced[rule.id] |= hits
                # a rule line `- "under construction" -> plants.Status` made a
                # model write Status = 'under construction' (the phrase, wrong
                # case); Status was already in the baseline SQL, so only the
                # literal changed. Seen on a real run; identifiers alone miss it.
                if any(rule.phrase in literal.lower() for literal in new_literals):
                    signals.add("phrase_in_literal")
                if new_literals and _literal_on_rule_column(rule, with_sql, without_sql):
                    signals.add("literal_changed_on_rule_column")
                if signals:
                    per_rule[rule.id].append(observation_id)
                    per_rule_signals[rule.id] |= signals
                    matched = True
            if not matched:
                unattributed.append(observation_id)

    impacts = [
        RuleImpact(
            rule=rule_by_id[rule_id],
            broke=tuple(_collapse_reps(tuple(per_rule_broke[rule_id]))),
            fixed=tuple(_collapse_reps(tuple(per_rule_fixed[rule_id]))),
            introduced=tuple(sorted(per_rule_introduced[rule_id])),
            signals=tuple(sorted(per_rule_signals[rule_id])),
        )
        for rule_id in per_rule_broke
        if per_rule_broke[rule_id] or per_rule_fixed[rule_id]
    ]
    impacts.sort(key=lambda i: (-len(i.broke), len(i.fixed), i.rule.line_no))

    return AttributionReport(
        impacts=tuple(impacts),
        unattributed_broke=tuple(_collapse_reps(tuple(unattributed_broke))),
        unattributed_fixed=tuple(_collapse_reps(tuple(unattributed_fixed))),
        n_rules=len(rules),
        n_broke=len(_collapse_reps(ablation.broke_ids)),
        n_fixed=len(_collapse_reps(ablation.fixed_ids)),
        n_rule_lines_without_identifier=n_rule_lines_without_identifier,
    )
