#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Discover output conventions from the analysts' own SQL. Free, no model.

THE PROBLEM, MEASURED ON THELOOK 2026-09-10. A question in English does not
say what SHAPE its answer should be, and 13 of 28 failures were the model
returning a rate together with the numerator and denominator it came from.
Nothing in a schema says which shape a business reports. The crude fix is to
read failures and write the convention down by hand, which is what was done.

THE ELEGANT FIX IS THAT THE ANALYSTS' SQL ALREADY ENCODES IT. Their queries
had to be right for someone's report. Group the log by what each query
computes, and where the analysts AGREE on a shape, that is the convention --
discovered from evidence rather than declared in a meeting. Nobody has to
remember to write it down, because the warehouse already knows.

WHAT THIS REFUSES TO DO, AND IT IS THE POINT. A shape analysts follow half the
time is not a convention. Writing it as a rule would put a coin flip into the
context, and wrong context measures WORSE than none (arXiv 2408.07702). So
there are two gates, and they are different things kept separate:

    support     fewer than --min-support queries -> "not enough evidence"
    agreement   between the thresholds           -> "no agreement, DO NOT write"

Four queries agreeing unanimously is not evidence. Ninety-two queries split
52/48 is evidence of DISAGREEMENT, which is a finding worth printing and not a
rule worth writing. The refused list is as useful as the proposed one: it stops
somebody writing down a habit the business does not actually have.

TWO FILTERS BEFORE ANY COUNTING, both borrowed from `mine`. `written_by_a_tool`
removes BI-tool probes -- on one engagement 198 of the top 200 patterns by
execution count were these, so without it you discover a dashboard's habits
and call them the analysts'. `_ANALYTICAL` removes lookups and exports, which
carry no convention.

This is a MACHINE stage: deterministic, no model call, no cloud. Its output is
a DRAFT that a person reviews, and the draft is worth nothing until a round
prices it like any other context.

    python scripts/mine_conventions.py --log query_log.csv --out draft-rules.md
"""
from __future__ import annotations

import argparse
import collections
import csv
import pathlib
import re
import sys
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from context_lift.executor import strip_comments_and_mask_literals  # noqa: E402
from context_lift.mine import is_noise  # noqa: E402

_SELECT = re.compile(r"\bSELECT\b(.*?)\bFROM\b", re.IGNORECASE | re.DOTALL)
_RATIO = re.compile(r"safe_divide\s*\(|/", re.I)


def select_body(sql: str) -> str:
    """The outer SELECT list, or "" when it cannot be read."""
    found = _SELECT.search(strip_comments_and_mask_literals(sql))
    return found.group(1) if found else ""


def select_items(body: str) -> list[str]:
    """Top-level comma-separated select items, commas inside calls ignored."""
    out, depth, current = [], 0, ""
    for char in body:
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        if char == "," and depth == 0:
            out.append(current)
            current = ""
        else:
            current += char
    out.append(current)
    return [item.strip() for item in out if item.strip()]


def unwrap(text: str) -> str:
    """`(a+b)` -> `a+b`, but `f(a)` stays `f(a)`.

    THE BUG THIS REPLACES, found by this module's own test. It was
    `text.strip("()")`, which removes EVERY leading and trailing paren, so
    `safe_divide(sum(a),sum(b))` became `safe_divide(sum(a),sum(b` -- the
    call's own closing parens went with it. `_split_ratio` then found no
    top-level separator and returned None, and every SAFE_DIVIDE ratio was
    skipped WITHOUT BEING CHECKED. On the real log that was 48 of 92 queries,
    so the headline share was computed on the slash half alone.
    """
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        for index, char in enumerate(text):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    # the opening paren closes here; only unwrap when that is
                    # the very end, i.e. the pair wraps the whole expression
                    if index != len(text) - 1:
                        return text
                    break
        else:
            return text
        text = text[1:-1]
    return text


def _normalise(expr: str) -> str:
    """An expression with its alias and whitespace removed, for comparing."""
    without_alias = re.sub(r"\s+AS\s+[\w`\"]+\s*$", "", expr, flags=re.I)
    return unwrap(re.sub(r"\s+", "", without_alias).lower())


def _split_ratio(expr: str) -> tuple[str, str] | None:
    """(numerator, denominator) of a ratio expression, or None."""
    text = _normalise(expr)
    divide = re.match(r"safe_divide\((.+)\)$", text)
    inner = divide.group(1) if divide else text
    separator = "," if divide else "/"
    depth = 0
    for index, char in enumerate(inner):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and char == separator:
            return (unwrap(inner[:index]), unwrap(inner[index + 1:]))
    return None


def exposes_its_parts(sql: str) -> bool | None:
    """True when a ratio is returned ALONGSIDE its numerator or denominator.

    None when the query computes no ratio, so it is not in the pool at all.
    This is the check aimed at the recorded failure: a rate reported with the
    two numbers it came from is three columns where the business reports one.
    """
    body = select_body(sql)
    if not body:
        return None
    items = select_items(body)
    ratios = [item for item in items if _RATIO.search(item)]
    if not ratios or len(items) < 2:
        return None if not ratios else False
    others = {_normalise(item) for item in items if item not in ratios}
    for ratio in ratios:
        halves = _split_ratio(ratio)
        if not halves:
            continue
        numerator, denominator = halves
        if (numerator and numerator in others) or (denominator and denominator in others):
            return True
    return False


@dataclass
class Convention:
    """One candidate convention, its pool, and the rule for each direction.

    `rule_when_true` and `rule_when_false` are both given because a convention
    is often the NEGATIVE: analysts overwhelmingly do NOT round a rate, and
    "do not round" is the rule, not "round".
    """
    key: str
    pool: str
    holds: object                      # sql -> bool | None (None = not in pool)
    rule_when_true: str
    rule_when_false: str


def _in_ratio_pool(sql: str) -> bool:
    return bool(_RATIO.search(select_body(sql)))


def _matches(pattern: str, whole_query: bool = False):
    rx = re.compile(pattern, re.I)
    def check(sql: str):
        if not _in_ratio_pool(sql):
            return None
        return bool(rx.search(sql if whole_query else select_body(sql)))
    return check


def _limit_pool(sql: str):
    if not re.search(r"\blimit\b", sql, re.I):
        return None
    return bool(re.search(r"\border\s+by\b", sql, re.I))


CONVENTIONS = [
    Convention(
        "rate shape", "queries whose SELECT list computes a ratio",
        exposes_its_parts,
        rule_when_true="Report a rate together with the numerator and "
                       "denominator it came from.",
        rule_when_false="Report a rate as a single column. Do not also return "
                        "the numerator and denominator it came from.",
    ),
    Convention(
        "divide guard", "queries whose SELECT list computes a ratio",
        _matches(r"safe_divide\s*\(|nullif\s*\("),
        rule_when_true="Guard every division against a zero denominator, with "
                       "SAFE_DIVIDE or NULLIF.",
        rule_when_false="Divide directly. Do not guard against a zero "
                        "denominator.",
    ),
    Convention(
        "percent scale", "queries whose SELECT list computes a ratio",
        _matches(r"\*\s*100\b"),
        rule_when_true="Report a rate as a percentage, multiplied by 100.",
        rule_when_false="Report a rate as a fraction between 0 and 1, not as "
                        "a percentage.",
    ),
    Convention(
        "rounding", "queries whose SELECT list computes a ratio",
        _matches(r"\bround\s*\("),
        rule_when_true="Round a rate.",
        rule_when_false="Do not round a rate. Return it at full precision.",
    ),
    Convention(
        "limit needs an order", "queries using LIMIT",
        _limit_pool,
        rule_when_true="A LIMIT always carries an ORDER BY, so the rows "
                       "returned are the top ones and not an arbitrary few.",
        rule_when_false="A LIMIT does not need an ORDER BY.",
    ),
]


@dataclass
class Finding:
    key: str
    pool: str
    n_true: int
    n_pool: int
    proposed: str = ""
    refused: str = ""
    detail: str = field(default="")

    @property
    def share(self) -> float:
        return self.n_true / self.n_pool if self.n_pool else 0.0


def measure(queries: list[str], min_support: int,
            min_agreement: float) -> list[Finding]:
    findings = []
    for convention in CONVENTIONS:
        votes = [convention.holds(sql) for sql in queries]
        pool = [vote for vote in votes if vote is not None]
        n_true = sum(1 for vote in pool if vote)
        finding = Finding(convention.key, convention.pool, n_true, len(pool))

        if len(pool) < min_support:
            finding.refused = (f"not enough evidence: {len(pool)} quer(y/ies) "
                               f"in the pool, under the {min_support} required")
        elif finding.share >= min_agreement:
            finding.proposed = convention.rule_when_true
            finding.detail = (f"{n_true} of {len(pool)} "
                              f"({finding.share:.1%}) follow this")
        elif finding.share <= 1 - min_agreement:
            finding.proposed = convention.rule_when_false
            # The rule is always stated in the direction the majority follows,
            # so the evidence line says "follow this" either way. It read
            # "do NOT do the opposite" first, which is a double negative and
            # the file is written for people to review quickly.
            n_false = len(pool) - n_true
            finding.proposed = convention.rule_when_false
            finding.detail = (f"{n_false} of {len(pool)} "
                              f"({n_false / len(pool):.1%}) follow this")
        else:
            finding.refused = (
                f"no agreement: {n_true} of {len(pool)} ({finding.share:.1%}). "
                f"Analysts are split, so there is no convention here. Writing "
                f"one would put a coin flip into the context")
        findings.append(finding)
    return findings


DRAFT_HEADER = """\
# Output conventions, discovered from the query log

DRAFT. A person must read every line before this goes anywhere near a round.

Generated by `scripts/mine_conventions.py`. Every line below carries the count
that produced it. These are what the analysts' own SQL agrees on, which is
evidence about what the business reports -- not proof that writing it down
helps. It is worth nothing until a round prices it like any other context.

  queries read          {n_read}
  after removing BI-tool probes and lookups   {n_used}
  support threshold     {min_support} queries in a pool
  agreement threshold   {min_agreement:.0%}

"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", required=True, nargs="+",
                    help="one or more query-log CSVs, each with a `sql` column")
    ap.add_argument("--out", help="write the draft rules file here")
    ap.add_argument("--min-support", type=int, default=20,
                    help="fewest queries a pool needs before it can propose "
                         "anything. Four queries agreeing unanimously is not "
                         "evidence (default 20)")
    ap.add_argument("--min-agreement", type=float, default=0.70,
                    help="the share of the pool that must agree (default "
                         "0.70). Between this and its complement is a SPLIT, "
                         "reported and never written")
    args = ap.parse_args()

    raw: list[str] = []
    for path in args.log:
        with open(path, newline="", encoding="utf-8") as handle:
            raw += [row["sql"] for row in csv.DictReader(handle)
                    if (row.get("sql") or "").strip()]
    if not raw:
        print("no queries found; is there a `sql` column?", file=sys.stderr)
        return 1

    distinct = sorted(set(raw))
    # `is_noise` from `mine`, and it is not optional. It removes BI-tool
    # probes, metadata queries, bare table dumps, dbt temp tables and a BI
    # filter dropdown. Without it you discover a dashboard's habits and call
    # them the analysts': on one engagement 198 of the top 200 patterns by
    # execution count were tool probes.
    #
    # The first version of this script used `written_by_a_tool` plus the
    # `_ANALYTICAL` regex, which is much weaker -- `written_by_a_tool` only
    # matches a header COMMENT a tool wrote about itself, and it removed 36
    # rows of 4513 where `is_noise` removes far more.
    queries = [sql for sql in distinct
               if not is_noise(sql, require_analytical=True)]

    print(f"{len(raw)} queries read, {len(distinct)} distinct")
    print(f"  {len(distinct) - len(queries)} removed as noise (tool probes, "
          "metadata, dumps, non-analytical)")
    print(f"  {len(queries)} left to read conventions from")
    print()

    findings = measure(queries, args.min_support, args.min_agreement)
    proposed = [f for f in findings if f.proposed]
    refused = [f for f in findings if f.refused]

    print(f"  {'convention':24s} {'pool':>6s} {'share':>7s}  outcome")
    for finding in findings:
        outcome = "PROPOSE" if finding.proposed else "refused"
        print(f"  {finding.key:24s} {finding.n_pool:6d} "
              f"{finding.share:7.1%}  {outcome}")
    print()

    for finding in refused:
        print(f"  REFUSED {finding.key}: {finding.refused}")
    if refused:
        print()

    if not proposed:
        print("  nothing proposed. That is a result, not a failure: this log "
              "shows no shape the analysts agree on.")
        return 0

    lines = [DRAFT_HEADER.format(
        n_read=len(raw), n_used=len(queries),
        min_support=args.min_support, min_agreement=args.min_agreement)]
    for finding in proposed:
        lines.append(f"- {finding.proposed}")
        lines.append(f"    evidence: {finding.detail}, among "
                     f"{finding.pool}")
    lines.append("")
    lines.append("## Deliberately NOT proposed")
    lines.append("")
    lines.append("Kept in the draft on purpose. A reader who does not see "
                 "these will write one")
    lines.append("of them from memory, and it would be a rule the business "
                 "does not follow.")
    lines.append("")
    for finding in refused:
        lines.append(f"- {finding.key}: {finding.refused}")
    draft = "\n".join(lines) + "\n"

    if args.out:
        pathlib.Path(args.out).write_text(draft)
        print(f"  wrote {args.out}  ({len(proposed)} rule(s), "
              f"{len(refused)} refusal(s))")
        print("  READ IT before using it. Then measure it: a discovered "
              "convention is")
        print("  a candidate rule, and only a round can say whether it helps.")
    else:
        print(draft)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
