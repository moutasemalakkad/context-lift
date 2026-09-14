# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Business definitions read out of a query log by counting, not by opinion.

The problem this solves. A round on one enterprise estate measured an agent
at 2 right of 18, and the failures were not vocabulary -- they were
arithmetic. The agent was asked for "the final shipping amount" and wrote
`SUM(shipping_charge)`. The answer key computed
`sum(line_total_shipping_amt) - sum(line_shipping_disc_amt)`. No glossary
entry fixes that, because the missing thing is a FORMULA.

Where the formula comes from matters more than what it says. A model can
guess one, and then an agent is being graded against a guess. A domain expert
can write one, and that is the right answer in production and was not
available. The third source is the one nobody uses: **the warehouse's own
query history already contains the formula, written by the analysts, many
times over.**

Measured on one estate's 4,000 statements of history:

    seen  34x   sum(count_of_eligible_days) - sum(count_of_oos_days)
    seen  14x   sum(line_total_shipping_amt) - sum(line_shipping_disc_amt)
    seen  11x   sum(allocated_qty) - sum(store_cancelled_qty)
    seen   7x   sum(final_shipping_amt) + sum(freight_rma_fees)
    seen   6x   sum(net_available_inventory) - sum(open_units)

The second line is the exact formula the agent got wrong, computed that way
by fourteen different queries. That is not an opinion about Acme's business;
it is Acme's business, counted.

**Frequency is the whole method, and it is also the leak defence.** A
reserved question's own SQL is in that log -- that is where the question came
from -- so mining every statement would hand the agent the answers. Two
rules stop it. Statements whose fingerprint matches a scored gold are
excluded outright. And a formula must appear `min_times` times across
DIFFERENT query shapes, because a convention repeats and one question's
answer does not.

What this deliberately does not do. It does not name the metric. `sum(a) -
sum(b)` seen 14 times is evidence that the subtraction is the convention;
calling it "final shipping amount" is a human's job and a guess if a model
does it. So the output says what the warehouse does and leaves the naming to
whoever reviews the file.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .mine import fingerprint, strip_comments

# How many DIFFERENT query shapes must contain a formula before it is a
# convention rather than one analyst's arithmetic. Three is the lowest number
# that can be called a pattern, and on the estate this was written from it
# kept 8 formulas of 27 distinct ones.
MIN_TIMES = 3

# `sum(x) - sum(y)`, `sum(x) + sum(y)`: an aggregate combined with another
# aggregate over named columns. Bounded repetition and no nesting, so it
# cannot backtrack: one estate's 401-million-row lesson was about unbounded
# work, and a regex over 4,000 statements is the same class of risk.
_FORMULA = re.compile(
    r"\b(sum|count|avg|min|max)\s*\(\s*([A-Za-z_][\w.]{0,60})\s*\)"
    r"\s*([-+*/])\s*"
    r"(sum|count|avg|min|max)\s*\(\s*([A-Za-z_][\w.]{0,60})\s*\)", re.I)

# `CASE col WHEN 'x' THEN 'y' WHEN ... END`: a code table written inline,
# which is a vocabulary somebody never wrote down anywhere else.
_MAPPING = re.compile(
    r"\bcase\s+([A-Za-z_][\w.]{0,60})\s+"
    r"((?:when\s+'[^']{1,30}'\s+then\s+'[^']{1,60}'\s*){2,12})", re.I)
_WHEN_THEN = re.compile(r"when\s+'([^']{1,30})'\s+then\s+'([^']{1,60})'", re.I)


def _bare(name: str) -> str:
    """`t.line_total_shipping_amt` -> `line_total_shipping_amt`.

    The table alias is noise: the same formula written by two analysts with
    different aliases is one convention, and counting them apart would hide
    it below the threshold.
    """
    return name.rsplit(".", 1)[-1].lower()


def metric_formulas(sqls: list[str], min_times: int = MIN_TIMES,
                    exclude_fingerprints: set[str] | None = None
                    ) -> list[tuple[str, int]]:
    """Arithmetic over aggregates that many different queries agree on.

    Returns (formula, how many query SHAPES contained it), most-repeated
    first. Counting shapes and not statements matters: one nightly report run
    400 times is one convention, not 400.
    """
    excluded = exclude_fingerprints or set()
    shapes: dict[str, set[str]] = defaultdict(set)
    for sql in sqls:
        text = strip_comments(sql or "")
        shape = fingerprint(text)
        if shape in excluded:
            continue
        for f1, c1, op, f2, c2 in _FORMULA.findall(text):
            a, b = _bare(c1), _bare(c2)
            if a == b:
                continue                    # sum(x) - sum(x) is not a formula
            shapes[f"{f1.lower()}({a}) {op} {f2.lower()}({b})"].add(shape)
    return sorted(((f, len(s)) for f, s in shapes.items() if len(s) >= min_times),
                  key=lambda pair: (-pair[1], pair[0]))


def code_mappings(sqls: list[str], min_times: int = MIN_TIMES,
                  exclude_fingerprints: set[str] | None = None
                  ) -> list[tuple[str, tuple[tuple[str, str], ...], int]]:
    """Inline code tables many different queries agree on.

    Returns (column, ((code, meaning), ...), shapes) most-repeated first.
    `company_code '1000' -> 'CL'` is a vocabulary entry the warehouse never
    wrote down, and an agent cannot guess it.
    """
    excluded = exclude_fingerprints or set()
    shapes: dict[tuple, set[str]] = defaultdict(set)
    for sql in sqls:
        text = strip_comments(sql or "")
        shape = fingerprint(text)
        if shape in excluded:
            continue
        for column, body in _MAPPING.findall(text):
            pairs = tuple(sorted((c.strip(), m.strip())
                                 for c, m in _WHEN_THEN.findall(body)))
            if len(pairs) >= 2:
                shapes[(_bare(column), pairs)].add(shape)
    out = [(col, pairs, len(s)) for (col, pairs), s in shapes.items()
           if len(s) >= min_times]
    return sorted(out, key=lambda t: (-t[2], t[0]))


def render_metrics(formulas: list[tuple[str, int]], total_shapes: int) -> str:
    """The rules file. Every line carries its own count, so a reviewer
    disagrees with the evidence rather than with the author."""
    lines = [
        "# Metric conventions, counted from this warehouse's own query history",
        "",
        f"Read out of {total_shapes} distinct query shapes. Nothing here was",
        "written by a person or a model: each line is arithmetic that this many",
        "DIFFERENT queries agreed on, so a reader can disagree with the count",
        "rather than with an author.",
        "",
        "A formula is listed only when three or more different query shapes",
        "contain it, because a convention repeats and one analyst's arithmetic",
        "does not. Queries matching a scored question were excluded before",
        "counting, so no answer key leaked in here.",
        "",
        "The names are NOT given. `sum(a) - sum(b)` seen 14 times is evidence",
        "that the subtraction is the convention; deciding it is called \"final",
        "shipping amount\" is a person's job, and a guess if anything else does",
        "it. Add the name beside a line when you know it.",
        "",
        "## Rules",
        "",
    ]
    for formula, times in formulas:
        lines.append(f"- Compute it as `{formula}`. "
                     f"[{times} different queries in this warehouse do]")
    if not formulas:
        lines.append("- nothing repeated often enough to be a convention")
    return "\n".join(lines) + "\n"


def render_vocabulary(mappings: list[tuple[str, tuple, int]]) -> str:
    """The vocabulary file, as the spec reader expects it."""
    lines = ["# Code meanings, counted from this warehouse's own query history.",
             "# Each entry is a CASE mapping this many different queries wrote",
             "# inline. Nobody chose these; they were counted.",
             "terms:"]
    for column, pairs, times in mappings:
        for code, meaning in pairs:
            lines.append(f"  - name: \"{column} = {code}\"")
            lines.append(f"    definition: \"in {column}, {code} means "
                         f"{meaning}. Seen in {times} different queries.\"")
    if not mappings:
        lines.append("  []")
    return "\n".join(lines) + "\n"


# --- auditing an answer key against the conventions -------------------------
#
# Why this exists, and it is the finding that produced it. A round measured
# +0.0 with 19 of 19 reserved answers CHANGED by the written context, which
# means the context was read and used. One failure read like this:
#
#     the rule:       a question about a quarter means `fiscal_quarter`,
#                     not EXTRACT(QUARTER FROM ...)
#     without rules:  EXTRACT(QUARTER FROM store_open_date)
#     with rules:     d.fiscal_quarter JOIN dim_gbl_date     <- obeyed exactly
#     the ANSWER KEY: DATE_TRUNC(store_open_date, QUARTER)   <- calendar quarter
#
# The agent did what it was told and scored worse for it, because the analyst
# who wrote that key used a calendar quarter. One analyst's query is not the
# same thing as the warehouse's convention, and a round that grades a
# well-informed agent against one analyst's habits measures the habits.
#
# The project's own design document warns about exactly this and cites BIRD
# Mini-Dev, where an audit found 52.8% of golds wrong. Nobody had run that
# audit here.
#
# Every check below asks one question: does this key contradict something
# many different queries in this warehouse agree on? It names the key and the
# evidence and decides nothing. A convention seen in fourteen query shapes is
# strong evidence and still not a verdict on one question.

# A time grain computed with a calendar function, where the warehouse keeps a
# fiscal column for the same grain. `fiscal_week` and `EXTRACT(WEEK ...)`
# disagree in most retail calendars, and the gold that mixes them is not
# wrong so much as answering a different question than the one asked.
_CALENDAR_GRAIN = re.compile(
    r"\b(?:extract\s*\(\s*(week|month|quarter|year)\b"
    r"|date_trunc\s*\([^,]{1,60},\s*(week|month|quarter|year)\b)", re.I)


def golds_using_a_calendar_grain(golds: list[tuple[str, str, str]],
                                 fiscal_columns: set[str]
                                 ) -> list[tuple[str, str, str]]:
    """(gold id, the grain, the fiscal column that exists instead).

    `golds` is (id, question, sql). `fiscal_columns` is what the warehouse
    actually has, read from its own query history rather than assumed: a
    warehouse with no fiscal calendar must not be told to use one.
    """
    out = []
    for gid, question, sql in golds:
        for match in _CALENDAR_GRAIN.finditer(strip_comments(sql or "")):
            grain = (match.group(1) or match.group(2) or "").lower()
            column = f"fiscal_{grain}"
            if grain and column in fiscal_columns:
                out.append((gid, grain, column))
                break
    return out


def fiscal_columns_in(sqls: list[str]) -> set[str]:
    """The fiscal grain columns this warehouse actually uses."""
    found = set(re.findall(r"\bfiscal_(week|month|period|quarter|half|year)\b",
                           " ".join(sqls), re.I))
    return {f"fiscal_{g.lower()}" for g in found}


def golds_using_half_a_formula(golds: list[tuple[str, str, str]],
                               formulas: list[tuple[str, int]]
                               ) -> list[tuple[str, str, str, int]]:
    """(gold id, the column it used, the convention, how many queries agree).

    A warehouse that computes something as `sum(a) - sum(b)` in fourteen
    different query shapes has a convention. A key that sums `a` alone and
    calls the result the same thing is reporting a gross figure where the
    business reports a net one, and an agent that gets it right by the
    convention scores wrong against that key.

    Only the FIRST operand is checked. `sum(b)` alone is usually a different
    question -- "how much discount did we give" is a fair thing to ask -- and
    flagging it would bury the real cases.
    """
    parsed = []
    for formula, times in formulas:
        match = re.fullmatch(r"(\w+)\((\w+)\)\s*([-+*/])\s*(\w+)\((\w+)\)",
                             formula)
        if match:
            parsed.append((match.group(2), match.group(5), formula, times))
    out = []
    for gid, _question, sql in golds:
        text = strip_comments(sql or "").lower()
        for first, second, formula, times in parsed:
            if re.search(rf"\b{re.escape(first)}\b", text) \
                    and not re.search(rf"\b{re.escape(second)}\b", text):
                out.append((gid, first, formula, times))
                break
    return out


def golds_asking_the_same_question(golds: list[tuple[str, str, str]]
                                   ) -> list[tuple[str, ...]]:
    """Groups of golds whose questions read the same, with different SQL.

    One question cannot have two right answers. `review` already prints this
    at the sheet stage, and it is printed again here because on one estate it
    was printed, read, and left in -- and both members of the pair then
    failed the round for the same reason.

    It calls `review`'s own grouping rather than repeating it. A first
    version of this function wrote a second, stricter rule and found nothing
    where `review` had found a pair, which is the duplicate-implementation
    fault this project keeps paying for: two notions of "the same question"
    will disagree, and the day they disagree is the day one of them is
    trusted.
    """
    from .review import _same_question_groups
    asked = [(gid, question) for gid, question, _sql in golds if (question or "").strip()]
    return [tuple(ids) for ids in _same_question_groups(asked) if len(ids) > 1]
