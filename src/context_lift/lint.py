# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Refuse written context with a visible defect, before a round is paid for.

The absence this fills. The framework gates the GOLD SET, sixteen defect
classes, and it gates LEAKAGE, so no reserved answer reaches the agent. It
had no check at all on whether the written context is any good. So a person
wrote rules blind, paid for a round, and found out afterwards. On 2026-09-12
exactly that happened: 49 rule lines, a reserved lift of minus 6.9 points,
and three of the faults were visible in the file for free before anything
ran.

WHAT THIS IS NOT, and the boundary matters more than the checks. A file that
passes every check here can still be useless. Context quality is only
knowable by measurement, which is what the rest of this tool is for. The
linter's job is narrower: stop you paying for a round whose context has a
defect you could have seen. Passing is not a certificate.

BORROWED, not invented. dbt's Semantic Layer ships three validation layers --
parsing, semantic, and data platform -- and its data-platform layer is this
module's `unknown_column` check almost exactly: "whether the semantic
definitions in your semantic graph exist in the underlying physical table"
(docs.getdbt.com/docs/build/validation). A published agentic data-quality
framework describes a Feasibility Validator that "evaluates rules before
execution, acting as a gating mechanism", verifying referenced attributes
exist, and is "constrained to assessment only -- not allowed to invent or
modify rules" (arXiv 2606.13692). Both shapes are adopted here, including
that last constraint: this module reports and never edits.

Searched 2026-09-13 and not found: any linter aimed at text-to-SQL business
context specifically. So the shape is borrowed and the target is not.

WHAT IS ACTUALLY OURS: the ORDER. dbt's validations are all equally errors.
Every severity below comes from a measured effect on a real round, and the
number travels with the check so a reader can disagree with the evidence
rather than with the author.
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field

# Severity is earned, not asserted. Each REFUSE cites the measurement that
# earned it; a check with no number is a WARN until it has one.
REFUSE = "REFUSE"
WARN = "WARN"

# Printed on every pass, refusals or not. It used to print only on a
# completely empty report, which almost never happens -- nearly every real
# rules file raises at least one warning -- so the one sentence that stops
# this being read as a clean bill of health was the one nobody saw.
NOT_A_CERTIFICATE = (
    "  Passing is not a certificate: a file with no visible defect can still "
    "be useless. Only a round can tell you that.")


@dataclass(frozen=True)
class Finding:
    """One defect, where it is, and why it is at this severity."""

    check: str
    severity: str
    where: str
    detail: str
    # The measurement that put this check at this severity. A REFUSE with an
    # empty basis is a bug, and `lint` asserts that.
    basis: str = ""

    def line(self) -> str:
        return f"  [{self.severity:6s}] {self.check}: {self.where}\n      {self.detail}"


@dataclass
class LintReport:
    findings: list[Finding] = field(default_factory=list)
    rules_seen: int = 0
    columns_seen: int = 0

    @property
    def refusals(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == REFUSE]

    @property
    def passed(self) -> bool:
        return not self.refusals

    # Checks that could not run, with why. A skipped check is not a passed
    # check, and a report that does not distinguish them is a report that
    # reads as a certificate.
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        head = (f"lint: {self.rules_seen} rule line(s) against "
                f"{self.columns_seen} column(s) the arm carries")
        lines = [head]
        for note in self.skipped:
            lines.append(f"  SKIPPED {note}")
        if not self.findings:
            lines.append("  nothing to report.")
            lines.append(NOT_A_CERTIFICATE)
            return "\n".join(lines)
        # Grouped by check, not listed one per line. An early version printed
        # "no identifier" twelve times for one rules file, and a linter that
        # repeats itself is a linter people switch off. The count is the
        # finding; the first few locations are enough to start.
        for severity in (REFUSE, WARN):
            group = [f for f in self.findings if f.severity == severity]
            if not group:
                continue
            lines.append("")
            by_check: dict[str, list[Finding]] = {}
            for finding in group:
                by_check.setdefault(finding.check, []).append(finding)
            for check, hits in by_check.items():
                first = hits[0]
                count = f" x{len(hits)}" if len(hits) > 1 else ""
                lines.append(f"  [{severity:6s}] {check}{count}: "
                             f"{', '.join(h.where for h in hits[:3])}"
                             + (f" and {len(hits) - 3} more" if len(hits) > 3 else ""))
                lines.append(f"      {first.detail}")
                if first.basis:
                    lines.append(f"      why {severity}: {first.basis}")
        lines.append("")
        if self.refusals:
            lines.append(f"{len(self.refusals)} refusal(s). Fix these before "
                         "paying for a round: each one was measured to cost "
                         "more than it takes to fix.")
        else:
            lines.append("no refusals. The warnings are judgement calls; read "
                         "them and decide.")
            lines.append(NOT_A_CERTIFICATE)
        return "\n".join(lines)


# A bullet line in a rules file. Everything else is prose and is not linted:
# a rules file is allowed to explain itself.
_BULLET = re.compile(r"^\s*[-*]\s+(?P<text>\S.*)$")

# An identifier a rule names. Backticked first, because that is an author
# saying "this is a column" out loud, then bare snake_case, which is the
# warehouse convention and carries almost no false positives in English.
_BACKTICKED = re.compile(r"`([A-Za-z_][\w.]*)`")
_SNAKE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")

# A rule with no subject. The fault is not the verb, it is the OBJECT: an
# imperative whose object is a pronoun with nothing to refer to.
#
#   BAD    Compute it as `sum(count_of_eligible_days) - sum(count_of_oos_days)`.
#   GOOD   Exclude cancelled orders from any revenue number.
#   GOOD   Compute revenue as the sum of `sale_price`.
#
# An earlier draft matched on the imperative opening and asked whether the
# sentence named "a thing" anywhere. It was wrong in both directions: it went
# quiet on the line above, because "as sum(" matched its own
# thing-naming pattern, and it fired on "Exclude cancelled orders", which is
# a perfectly good rule. Matching the pronoun is narrower and it names the
# real defect, which is that a reader cannot tell what the sentence is about.
_SUBJECTLESS = re.compile(
    r"^(?:compute|calculate|use|take|apply|filter|exclude|include|derive|set)"
    r"\s+(?:it|this|that|these|those|them|the\s+value|the\s+result)\b",
    re.IGNORECASE)


def identifiers_in(text: str) -> set[str]:
    """Every column-ish name a rule line mentions.

    Backticked names and bare snake_case. A bare single word like "status" is
    NOT taken: it is as often the English word as the column, and a check
    that fires on English is a check people switch off.
    """
    found = {name.split(".")[-1].lower() for name in _BACKTICKED.findall(text)}
    found |= {name.lower() for name in _SNAKE.findall(text)}
    return found


def no_subject(text: str) -> bool:
    """True when a rule commands something and never says what it is about.

    The five lines that motivated this, from one estate's mined metrics file:

        Compute it as `sum(count_of_eligible_days) - sum(count_of_oos_days)`.

    Compute WHAT? The file that generated them admits that naming the metric
    "is a person's job", and no person came. An instruction an agent cannot
    attach to anything is not context.

    Kept narrow on purpose. The M18 spec asked whether this is detectable
    without a model or whether a regex would simply fit one file, so the
    tests run it over BOTH that file, where it must fire, and the committed
    thelook rules, which are well-formed prose and where it must stay
    silent. That is the over-fitting check.
    """
    return bool(_SUBJECTLESS.match(text.strip()))


def _bullets(spec) -> list[tuple[str, int, str]]:
    """Every bullet in every rules file, as (file, line number, text)."""
    out = []
    for name, body in spec.rules:
        for line_no, line in enumerate(body.splitlines(), start=1):
            match = _BULLET.match(line)
            if match:
                out.append((name, line_no, match.group("text").strip()))
    return out


def _columns_in_arm(spec) -> dict[str, set[str]]:
    """Column name -> the tables carrying it, lower case."""
    owners: dict[str, set[str]] = {}
    for table, schema in spec.schemas.items():
        for column in schema.columns:
            name = getattr(column, "name", column)
            owners.setdefault(str(name).lower(), set()).add(table)
    return owners


def untiered_rule_files(spec) -> list[str]:
    """Rules files whose name declares no tier, read off disk.

    The tier is a leading two-digit number and it is the whole ordering
    mechanism: `read_spec` concatenates `sorted(rules_dir.glob("*.md"))`, and
    the precedence sentence at the end of the instruction says the more
    specific rule wins. So the file that sorts LAST wins every conflict.

    A name with no number does not fail, which is the problem. `overview.md`
    sorts after `30-tables.md` and quietly becomes the most specific tier. So
    does `3o-orders.md`, where the second character is the letter o.

    Read from the directory rather than from `spec.rules`, because a spec
    built by `read_catalog` carries synthetic names like "catalog overviews"
    that were never filenames and cannot carry a tier.
    """
    from .export import RULES_DIR, tier_of
    root = getattr(spec, "root", None)
    if not root:
        return []
    rules_dir = pathlib.Path(root) / RULES_DIR
    if not rules_dir.is_dir():
        return []
    return [path.name for path in sorted(rules_dir.glob("*.md"))
            if tier_of(path.stem) is None]


def golds_a_rule_could_change(named: list[str], gold_sql: list[str]) -> int:
    """How many answer keys a rule could possibly affect.

    A rule reaches a question only through the identifiers it names: if no
    part of a gold's SQL mentions any of them, obeying the rule cannot change
    that answer, so the round can neither credit nor blame it.

    THE COUNT MATTERS, not just whether it is zero. "Names a column no answer
    key touches" tells a reader the rule is unmeasurable. "0 of 43" tells them
    how big the sample was that failed to reach it, which is what decides
    whether the fix is a better rule or more questions. On a 5-question set a
    zero means almost nothing; on 43 it means the rule is about a part of the
    business nobody asked about.

    Counted per gold rather than over the concatenated text, because the same
    substring check over one joined string cannot say how many.

    This is a reach count and NOT a claim that the rule would change those
    answers. A rule naming `status` reaches every gold that mentions
    `status`; whether it moves any of them is what a round measures.
    """
    if not named or not gold_sql:
        return 0
    wanted = [n.lower() for n in named]
    return sum(1 for sql in gold_sql
               if any(n in (sql or "").lower() for n in wanted))


def lint(spec, gold_sql: list[str] | None = None) -> LintReport:
    """Every check, ordered by what it was measured to cost.

    `gold_sql` is the answer keys, when there are any. Without them the
    checks that need to know what the questions touch are skipped rather
    than guessed at, and the report says which.
    """
    owners = _columns_in_arm(spec)
    bullets = _bullets(spec)
    report = LintReport(rules_seen=len(bullets), columns_seen=len(owners))
    if not owners:
        report.skipped.append(
            "unknown column and ambiguous column: the spec carries no column "
            "names, so there is nothing to check a rule against. Run "
            "`scripts/spec_schema.py` to write them")
    if not gold_sql:
        report.skipped.append(
            "no question would change: no answer keys were given (--golds), "
            "so how many questions a rule could reach is unknown")
    if gold_sql and not owners:
        # Found by the perturbation suite on 2026-09-13: with no column names
        # the reach check has nothing to count, so it silently never fired
        # and the report did not say so. A check that quietly does nothing is
        # indistinguishable from a check that passed.
        report.skipped.append(
            "no question would change: the spec carries no column names, so "
            "a rule names nothing whose reach could be counted")

    for filename in untiered_rule_files(spec):
        report.findings.append(Finding(
            "untiered rule file", WARN, filename,
            f"{filename} declares no tier. A rules filename starts with a "
            "two-digit number, broadest first, and that number is the whole "
            "ordering mechanism. Without one the file sorts by its first "
            "letter, which usually puts it AFTER every numbered file -- so "
            "the precedence sentence makes it win every conflict, at a "
            "position nobody chose.",
            basis="mechanical, not measured: `read_spec` concatenates "
                  "sorted(rules_dir.glob('*.md')) and PRECEDENCE says the "
                  "more specific rule wins, so sort order decides precedence. "
                  "A WARN rather than a REFUSE because the file's content is "
                  "still worth sending"))

    for name, line_no, text in bullets:
        where = f"{name}:{line_no}"

        if no_subject(text):
            report.findings.append(Finding(
                "no subject", REFUSE, where,
                f'"{text[:78]}" commands something and never says what it is '
                "about. An agent cannot attach it to anything.",
                basis="5 of 49 rule lines on one estate; the file that "
                      "generated them says naming the metric is a person's "
                      "job, and no person came. Priced 2026-09-13: stripping "
                      "the subject from every evidence line cost -2.7 points "
                      "[-6.1, +0.8] on 450 BIRD questions, NOT PROVEN. Kept "
                      "at REFUSE because it is the worst of the four priced "
                      "defects after irrelevant context and because a "
                      "subjectless line is unfixable by the agent, not "
                      "because the loss was resolved"))

        named = identifiers_in(text)
        unknown = sorted(n for n in named if n not in owners)
        if unknown and owners:
            report.findings.append(Finding(
                "unknown column", WARN, where,
                f"names {', '.join(unknown[:4])}, which no table in the arm "
                "carries. The agent cannot use a column it cannot see.",
                basis="DOWNGRADED FROM REFUSE TO WARN 2026-09-13, by "
                      "measurement rather than by intuition. Injected into "
                      "450 BIRD questions it cost -0.2 points [-3.2, +2.8], "
                      "NOT PROVEN and the tightest interval of the four "
                      "defects priced -- so a rule naming a column that does "
                      "not exist is mostly IGNORED rather than obeyed. The "
                      "count that used to justify REFUSE (8 of 10 bare names "
                      "on one estate were on no table) says the defect is "
                      "COMMON, not that it is costly, and those are different "
                      "claims. dbt's Semantic Layer runs the same check as a "
                      "data-platform validation. See "
                      "docs/evidence/perturbation-bird.txt"))

        known = [n for n in named if n in owners]
        ambiguous = [n for n in known if len(owners[n]) > 1]
        if ambiguous:
            report.findings.append(Finding(
                "ambiguous column", WARN, where,
                f"{ambiguous[0]} is on {len(owners[ambiguous[0]])} tables and "
                "the rule does not say which.",
                basis="observed once: a rule true of the estate moved the "
                      "agent off the table that already had the column"))

        if gold_sql and known:
            reach = golds_a_rule_could_change(known, gold_sql)
            if not reach:
                report.findings.append(Finding(
                    "no question would change", WARN, where,
                    f"names {', '.join(known[:3])}, which 0 of "
                    f"{len(gold_sql)} answer keys touch. Applied to every "
                    "gold it changes nothing about any of them, so this "
                    "round can neither credit nor blame it. It may still be "
                    "right about the business.",
                    basis="2 of 5 mined formulas on one estate named columns "
                          "no gold used. A gold set is a sample, so this "
                          "warns rather than refuses: a rule the questions "
                          "never reach is unmeasurable here, not wrong. "
                          "Priced 2026-09-13: adding five such lines cost "
                          "-2.9 points [-6.0, +0.2] on 450 BIRD questions, "
                          "NOT PROVEN -- the largest of the four realistic "
                          "defects, which is the opposite of what practice "
                          "15 predicts and is worth another look"))

        if not named:
            report.findings.append(Finding(
                "no identifier", WARN, where,
                f'"{text[:70]}" names no table or column, so it can be '
                "measured as part of a class but never credited or blamed.",
                basis="the rules class measured +12.2 points while 0 of 15 "
                      "lines were attributable, on the same round"))

    if spec.examples and not _routing_declared(spec):
        report.findings.append(Finding(
            "examples without routing", REFUSE, f"{spec.root}/examples",
            f"{len(spec.examples)} worked example(s) ship with every question. "
            "Route them per question, or do not ship them.",
            basis="-24.3 points [-34.8, -13.9] measured by leave-one-class-out "
                  "on 74 questions: 66% without the bank against 42% with it. "
                  "About 1 question in 3 matched an example; the rest carried "
                  "9 KB of SQL answering something else"))
    return report


def _routing_declared(spec) -> bool:
    """True when the spec says its examples are routed per question.

    A marker file rather than inference: whether examples are routed is a
    fact about how the round is run, and a linter must not guess it. Absent
    the marker the check refuses, because shipping a bank to every question
    is the measured default and it cost 24 points.
    """
    return (spec.root / "examples" / "ROUTED").exists() or \
           (spec.root / "routing.yaml").exists()
