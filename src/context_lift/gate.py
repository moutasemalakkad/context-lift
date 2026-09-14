# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Gold-set gate — audit the answer key before trusting any score.

Every accuracy number is computed against the gold queries, so a broken gold
silently corrupts the metric: an empty result, a query that errors, or a stub
left as a comment all make correct predictions look wrong (or wrong ones look
right). This gate runs each gold through an Executor and flags the defect
classes below, so a human fixes the answer key first.

It judges the GOLD, not the predictions — it is upstream of the harness, run
once when a gold set is built or changed.

Defect classes:
- NON_SELECT          gold is not a read-only SELECT/WITH (can't be trusted/run)
- PLACEHOLDER_COMMENT gold is empty after stripping comments, or carries a
                      TODO/FIXME-style marker — an unfinished stub
- EXEC_FAIL           gold raises when executed: the SQL itself is wrong
- OVER_BUDGET         the byte ceiling turned the query away before it ran.
                      Nothing is wrong with the SQL; raise --max-bytes or
                      narrow the question. Split out of EXEC_FAIL because on
                      one estate 112 of 179 rows labelled EXEC_FAIL were this,
                      and the label told a person their query log was junk
- NO_ACCESS           the platform refused the call (403, VPC Service
                      Controls, a missing permission). The query's author
                      cannot fix it; whoever owns the policy can. 34 of 179
                      on the same estate
- TABLE_GONE          the query names a table or dataset that no longer
                      exists. The SQL was right when it was written and no
                      ceiling or grant brings the table back: drop the gold.
                      24 of 179 on the same estate, and one of them stopped a
                      136-call round dead
- TIMEOUT             gold did not finish inside the executor's time budget
                      (only fires if the executor was built with a timeout)
- ZERO_ROWS           gold returns no rows (often a wrong filter or stale data)
- DEGENERATE          gold returns a single trivial cell (NULL or 0). Distinct
                      from ZERO_ROWS because an aggregate over an empty set
                      returns one row, not zero — `SELECT SUM(x) WHERE <never>`
                      yields [{None}], which ZERO_ROWS would miss
- TRUNCATED           gold hit the executor's row cap (only fires if the
                      executor was built with a row_cap)
- UNSTABLE            two runs returned different rows (non-deterministic gold,
                      e.g. ORDER BY without a tiebreaker). Opt-in: it runs each
                      gold twice, so it doubles execution cost
- TEXT_AGGREGATE      sum()/avg() over a text column. SQLite treats text as 0
                      and runs it clean, so the gold ranks by nothing
- CROSS_PROJECT       gold names a project the run does not use. It runs and
                      returns rows, so nothing else catches it, but the agent
                      under test reads the --db project and the answer key
                      would come from another copy of the data
- WHOLE_ROW           the outermost query is SELECT * over a table: a browse,
                      not a question. It runs and returns rows, and no answer
                      can match it unless the agent guesses the same column
                      list
- LIMIT_NO_ORDER      the outermost query caps rows with LIMIT and does not
                      order them, so the warehouse may hand back any N rows
                      that satisfy the query and two runs can differ. There
                      is no fixed answer to key against

Warnings (shown, counted, never acted on):
- MISSING_ORDER       the outermost query orders its rows and the question says
                      nothing about order, so an agent has to guess the ORDER
                      BY. The drafter never writes an ordering phrase: 0 of 6
                      ordered golds on one customer key, 2 of 22 on a mined
                      benchmark arm. It fires on 11 of 22 ordered golds in the
                      HUMAN half of the same benchmark, so a hand-written key
                      is not a clean baseline either
- MISSING_PROJECTION  the answer returns columns the question never names, so
                      an agent has to guess the shape of the result. The
                      largest of the three by measurement: a human question
                      names 76% of the 348 columns its golds return, a drafted
                      one 26%, and the same drafter on a customer estate 31%.
                      Whether that gap measures question quality or question
                      length is not established - the human questions are
                      visibly longer and nobody controlled for it
- LITERAL_ON_WRONG_COLUMN
                      the question gives the value and points at another
                      column for it, which MISSING_LITERAL passes because the
                      value IS there. Narrow on purpose: an identifier column,
                      a value the question carries, and a question that names
                      part of the column but not the identifying word. Five
                      cases on one estate, a shape to show rather than a rate
- MISSING_LITERAL     the question may not give a value the gold filters on.
                      Needs the question text, so it only fires where one is
                      known: `mine` passes the drafted question, and a gold
                      list loaded as (id, sql) pairs cannot raise it. It is a
                      warning and not a defect because it cannot read a
                      spelling: "Who is Summer Haynes?" gives the value stored
                      as 'Haynes, Summer', and "more than 75%" is 0.75. Those
                      are good golds, and a defect would drop them out of the
                      clean file where `split` never sees them
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .compare import compare, normalize_value
from .executor import (PLATFORM_REFUSAL_ADVICE, Executor, is_read_only, mask_string_literals,
                       over_budget, platform_refused, strip_comments, table_missing)

NON_SELECT = "NON_SELECT"
PLACEHOLDER_COMMENT = "PLACEHOLDER_COMMENT"
EXEC_FAIL = "EXEC_FAIL"
OVER_BUDGET = "OVER_BUDGET"
NO_ACCESS = "NO_ACCESS"
TABLE_GONE = "TABLE_GONE"
TIMEOUT = "TIMEOUT"
ZERO_ROWS = "ZERO_ROWS"
DEGENERATE = "DEGENERATE"
TRUNCATED = "TRUNCATED"
UNSTABLE = "UNSTABLE"
TEXT_AGGREGATE = "TEXT_AGGREGATE"
CROSS_PROJECT = "CROSS_PROJECT"
WHOLE_ROW = "WHOLE_ROW"
LIMIT_NO_ORDER = "LIMIT_NO_ORDER"
MISSING_LITERAL = "MISSING_LITERAL"
MISSING_PARAMETER = "MISSING_PARAMETER"
MISSING_ORDER = "MISSING_ORDER"
MISSING_PROJECTION = "MISSING_PROJECTION"
LITERAL_ON_WRONG_COLUMN = "LITERAL_ON_WRONG_COLUMN"

# Where a statement picks rows. LIMIT and OFFSET are not here: a row cap is
# not a value the question has to name, and a gold that pins rows by a cap
# alone has no answer to find anyway.
_CONDITION_START = re.compile(r"\b(?:WHERE|HAVING|QUALIFY|ON)\b", re.I)
_CONDITION_END = re.compile(
    r"\b(?:SELECT|FROM|WHERE|GROUP\s+BY|ORDER\s+BY|LIMIT|OFFSET|WINDOW|"
    r"UNION|EXCEPT|INTERSECT|JOIN|USING)\b", re.I)
# BigQuery accepts both quote styles for a string. Reading only one is how a
# bare '20' got scraped out of the middle of `transaction_date = "2026-06-20"`
# on one estate: the literal was never masked, so the digits were read as a
# separate value the question had to supply.
_QUOTED_VALUE = re.compile(r"'([^']*)'" r'|"([^"]*)"')
_NUMBER_VALUE = re.compile(r"\b\d+(?:\.\d+)?\b")
# '%Y-%m-%d' tells a function how to read a date; it is not a value anyone
# filters on. A LIKE pattern such as '%blue shirt%' is a value and stays:
# it carries letters after the first one.
_DATE_FORMAT = re.compile(r"^(?:%[A-Za-z][^%A-Za-z]*)+$")
_NOT_ALPHANUMERIC = re.compile(r"[^0-9a-z]+")


def _quoted_text(match: "re.Match") -> str:
    """The value inside a quoted literal, whichever quote style matched.

    `_QUOTED_VALUE` has one branch per quote style, so exactly one of its two
    groups is set and the other is None. Reading `group(1)` directly raised
    on every double-quoted literal.
    """
    return match.group(1) if match.group(1) is not None else (match.group(2) or "")


def _blank_string_values(text: str) -> str:
    """The same text with the inside of every quoted value blanked out.

    Every character keeps its place, so a span found here reads back out of
    the original text. `mask_string_literals` shortens the statement, and the
    offsets stop lining up. The point is that a clause word inside a value
    ("paid on time", "sent from lab") cannot open or close a condition.
    """
    return _QUOTED_VALUE.sub(
        lambda m: m.group(0)[0] + "_" * (len(m.group(0)) - 2) + m.group(0)[-1],
        text)


def _squashed(text: str) -> str:
    """Lower-cased, with everything that is not a letter or a digit removed,
    so `PRD-1042`, `prd 1042` and `prd1042` all compare equal."""
    return _NOT_ALPHANUMERIC.sub("", (text or "").lower())


_SPAN_PIECE = re.compile(r"[()]|" + _CONDITION_END.pattern, re.I)
# REGEXP_CONTAINS, REGEXP_EXTRACT, REGEXP_MATCH, REGEXP_REPLACE, ...
_REGEX_CALL = re.compile(r"\bREGEXP_\w+\s*\(", re.I)


def _blank_regex_arguments(text: str) -> str:
    """The same text with the literals inside REGEXP_* calls replaced by
    spaces, so a match pattern is not read as a value the question must give.

    `^[\\x00-\\x7F]+$` means "ASCII only". It is an argument telling a function
    how to match, exactly like the `'%Y-%m-%d'` that `_DATE_FORMAT` already
    excludes, and no question would ever state it. One customer gold was
    flagged for it. `REGEXP_CONTAINS` in a WHERE is ordinary on BigQuery, so
    this fires on every such estate.

    Matched by FUNCTION NAME, not by the shape of the value: a business value
    can legitimately contain a bracket or a caret, and a shape test would drop
    it. A LIKE pattern is untouched on purpose - `'%blue shirt%'` carries the
    words a question would use.

    Every character keeps its place, so offsets still line up with the caller's
    other views of the same statement.
    """
    out = list(text)
    for call in _REGEX_CALL.finditer(text):
        depth, at = 1, call.end()
        while at < len(text) and depth:
            if text[at] == "(":
                depth += 1
            elif text[at] == ")":
                depth -= 1
            at += 1
        for literal in _QUOTED_VALUE.finditer(text, call.end(), at):
            for position in range(literal.start(), literal.end()):
                out[position] = " "
    return "".join(out)


def _condition_ends_at(blanked: str, after: int) -> int:
    """Where the condition starting at `after` stops.

    A clause word alone is not enough. A scalar subquery in the SELECT list
    carries its own WHERE, and that span used to run straight past the
    subquery's closing parenthesis and through the outer projection, so a CASE
    comparing to '4', '5' and '6' was read as three row filters.

    So a span also ends at the parenthesis that encloses it. Depth is counted
    from the start of the condition: a `)` while the count is zero is the one
    that closes the subquery this condition lives in.

    No field gold demonstrates this. It was written from a report naming one,
    and that report was withdrawn: the values there came from a real WHERE and
    the check was right about them. The shape is reachable and the test builds
    it, so the guard is correct on its own terms, but nobody has met it on real
    data. Treat the test as the only witness until one turns up.

    This disclaimer covers THIS guard only. The short-value rule in
    `missing_literals`, which shipped in the same change, has three field
    witnesses on a customer key and is load-bearing there.
    """
    depth = 0
    for piece in _SPAN_PIECE.finditer(blanked, after):
        token = piece.group(0)
        if token == "(":
            depth += 1
        elif token == ")":
            if depth == 0:
                return piece.start()
            depth -= 1
        elif depth == 0:
            return piece.start()
    return len(blanked)


# `col = 'x'`, `t.col IN ('a','b')`, `col LIKE '%x%'`, `col > 0.75`
_COLUMN_BEFORE_VALUE = re.compile(
    r"([A-Za-z_][\w]*)\s*(?:\.\s*([A-Za-z_][\w]*))?\s*"
    r"(?:=|!=|<>|>=|<=|>|<|\bLIKE\b|\bIN\b|\bBETWEEN\b)\s*\(?\s*$", re.I)


def filter_pairs(sql: str) -> list[tuple[str, str]]:
    """Each value a statement picks rows by, with the column it is compared to.

    The column is "" when nothing readable sits in front of the value, which
    happens inside an `IN (...)` list after the first entry and around a
    function call. A caller that only wants the values reads
    `filter_literals`, which is this without the columns.

    It exists for the case `missing_literals` cannot see: the question gives
    the value and attaches it to the WRONG column. One gold filters
    `MOIRA_LIST_OWNER_KEY = 'LIST69...'` under a question reading "for owner
    'LIST69...'", so the agent wrote `WHERE OWNER =` and got zero rows. The
    value is present, the column is not, and the literal check passes it.
    """
    text = _blank_regex_arguments(strip_comments(sql or ""))
    blanked = _blank_string_values(text)
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for start in _CONDITION_START.finditer(blanked):
        stop = _condition_ends_at(blanked, start.end())
        span = text[start.end():stop]
        blanked_span = blanked[start.end():stop]
        for match in _QUOTED_VALUE.finditer(span):
            value = _quoted_text(match).strip()
            if not value or _DATE_FORMAT.match(value) or value in seen:
                continue
            seen.add(value)
            before = _COLUMN_BEFORE_VALUE.search(blanked_span[:match.start()])
            found.append((before.group(2) or before.group(1) if before else "", value))
        for match in _NUMBER_VALUE.finditer(blanked_span):
            number = match.group(0)
            if len(number.replace(".", "")) < 2 or number in seen:
                continue
            seen.add(number)
            before = _COLUMN_BEFORE_VALUE.search(blanked_span[:match.start()])
            found.append((before.group(2) or before.group(1) if before else "", number))
    return found


def filter_literals(sql: str) -> list[str]:
    """The values a statement picks rows by: the string and number literals in
    its WHERE, HAVING, QUALIFY and JOIN ... ON conditions, first one first.

    A number of one digit is left out. It is almost always a flag
    (`is_deleted = 0`), and one digit also matches by accident inside any
    longer number the question happens to carry.
    """
    text = _blank_regex_arguments(strip_comments(sql or ""))
    blanked = _blank_string_values(text)
    found: list[str] = []
    for start in _CONDITION_START.finditer(blanked):
        stop = _condition_ends_at(blanked, start.end())
        for match in _QUOTED_VALUE.finditer(text[start.end():stop]):
            value = _quoted_text(match).strip()
            if value and not _DATE_FORMAT.match(value) and value not in found:
                found.append(value)
        for number in _NUMBER_VALUE.findall(blanked[start.end():stop]):
            if len(number.replace(".", "")) > 1 and number not in found:
                found.append(number)
    return found


# A column whose value is a moment in time, or an opaque identifier. A value
# filtered on one of these is a PARAMETER of the question: which month, which
# store, which order. No written business rule can supply it, because nothing
# about the business says which month the person meant.
_TIME_WORD = (r"date|dt|day|week|month|quarter|year|period|ts|timestamp|time|"
              r"snapshot")
# `from`, `to`, `start`, `end` only name a moment when a time word sits beside
# them: `from_date`, `date_to`, `start_dt`. Alone they are ordinary English and
# they matched `ship_to_country`, `sold_to_party`, `bill_to_code` -- business
# codes, which are exactly what written context is supposed to supply. One
# gold on one estate was refused for a `ship_to_country = 'US'` filter.
_BOUND_WORD = r"from|to|start|end|since|until"
_PARAMETER_COLUMN = re.compile(
    rf"(?:^|_)(?:{_TIME_WORD})(?:$|_)"
    rf"|(?:^|_)(?:{_BOUND_WORD})_(?:{_TIME_WORD})(?:$|_)"
    rf"|(?:^|_)(?:{_TIME_WORD})_(?:{_BOUND_WORD})(?:$|_)"
    r"|(?:^|_)(?:id|key|number|no|num)(?:$|_)", re.I)

# And a value that is plainly a moment, whatever column it sits on.
_PARAMETER_VALUE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"              # 2026-04-01, with or without a time
    r"|^\d{4}[01]\d{2}$"                # 2026007, a fiscal period
    r"|^\d{4}-?[Ww]\d{1,2}$"            # 2026-W12
    r"|^\d{8,}$")                       # a long bare number: an id, not a code


# A date meaning "no bound", not a date the person chose. `WHERE d BETWEEN
# '1900-01-01' AND '9999-12-31'` is an open range written as a closed one, and
# an agent that writes no date filter at all returns the same rows. So a
# question omitting these is not unanswerable, and calling them parameters
# sends a reviewer to fix a gold that was fine. Found by disagreeing with a
# person: the classifier flagged one of these and the person had not.
# Every estate picks its own "no bound" date and the list was never going to
# hold. This one uses 1000-01-01, 1001-01-01, 2099-12-31 and 9000-12-31 in
# `dim_gbl_location`, and an enumerated pattern covering 18xx/19xx/0001/2999/
# 9999 caught none of them. So the test is positional: a business fact does
# not carry a date from before warehouses existed or from decades after
# anyone reading it will be working.
_BUSINESS_YEAR_LOW = 1990
_BUSINESS_YEAR_HIGH = 2090
_ANY_DATE = re.compile(r"^(\d{4})-?([01]\d)-?([0-3]\d)(?:$|[^\d])")


def sentinel_date(value: str) -> bool:
    """True when a date means "no bound" rather than a moment somebody chose.

    `WHERE d BETWEEN '1900-01-01' AND '9999-12-31'` is an open range written
    as a closed one, and an agent that writes no date filter at all returns
    the same rows. So the value is not something written context has to
    supply, and flagging it sends a reviewer to fix a gold that was fine.
    """
    match = _ANY_DATE.match((value or "").strip())
    if not match:
        return False
    year = int(match.group(1))
    return year < _BUSINESS_YEAR_LOW or year > _BUSINESS_YEAR_HIGH


def _column_filtered_on(gold_sql: str, value: str) -> str:
    """The column name a value is compared against, or "".

    Read from the SQL text rather than parsed, because the question is only
    "does this column name look like a date or an id", and a wrong answer
    costs a warning worded slightly wrong rather than a wrong score.
    """
    for match in re.finditer(re.escape(value), gold_sql):
        before = gold_sql[max(0, match.start() - 120):match.start()]
        column = _COLUMN_BEFORE_VALUE.search(before.rstrip().rstrip("'\""))
        if column:
            return column.group(2) or column.group(1)
    return ""


def parameter_literals(gold_sql: str, missing: list[str]) -> list[str]:
    """Which missing values are question PARAMETERS, not business rules.

    This split is the difference between a warning worth acting on and one
    worth ignoring, and the gate printed one sentence for both.

    With `mine --rules-in-context` the drafted question deliberately leaves
    out the values the gold filters on, because a question that states the
    business rule hands it to the bare arm for free. That is right for a
    business CODE -- `company_code '1000'` meaning Chile is exactly what
    written context should supply, and measuring whether it does is the whole
    point. It is fatal for a question PARAMETER: no context on earth can tell
    an agent which month the person meant, so the gold scores wrong in BOTH
    arms and dilutes the lift the same way a dashboard tile does.

    Measured on 39 clean golds from one estate: 15 omitted only codes, about
    3 omitted a parameter -- a fiscal period, one date, two order numbers.
    Nobody could see that split without reading 39 rows by hand, which is
    what somebody did.

    A value is a parameter when it looks like a moment or a long identifier,
    or when the column it is filtered on is named like one. Otherwise it is
    treated as a rule, because that is the side where being wrong is cheap:
    calling a rule a parameter tells a reviewer to fix a gold that was fine.
    """
    out = []
    for value in missing:
        if sentinel_date(value):
            continue
        if _PARAMETER_VALUE.search(value.strip()):
            out.append(value)
            continue
        column = _column_filtered_on(gold_sql, value)
        if column and _PARAMETER_COLUMN.search(column):
            out.append(value)
    return out


_MONTH_NUMBER = {name: number for number, names in enumerate([
    ("january", "jan"), ("february", "feb"), ("march", "mar"),
    ("april", "apr"), ("may",), ("june", "jun"), ("july", "jul"),
    ("august", "aug"), ("september", "sep", "sept"), ("october", "oct"),
    ("november", "nov"), ("december", "dec")], 1) for name in names}

_MONTH_NAMES = "|".join(sorted(_MONTH_NUMBER, key=len, reverse=True))
# "April 1, 2026", "Apr 1 2026", "December 1, 2025"
_MONTH_DAY_YEAR = re.compile(rf"\b({_MONTH_NAMES})\s+(\d{{1,2}})(?:st|nd|rd|th)?"
                             rf",?\s+(\d{{4}})\b", re.I)
# "1 April 2026", "1st of April 2026"
_DAY_MONTH_YEAR = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?"
                             rf"({_MONTH_NAMES})\s+(\d{{4}})\b", re.I)
# "April 2026", with no day at all
_MONTH_YEAR = re.compile(rf"\b({_MONTH_NAMES})\s+(\d{{4}})\b", re.I)


def dates_in_words(question: str) -> tuple[set[str], set[str]]:
    """Dates the question states in English, as ISO days and ISO months.

    The bug this closes, and it threw away most of a customer gold set. A
    person asks "for each day since December 1, 2025" and the SQL says
    `>= '2025-12-01'`. The literal check squashes both sides to letters and
    digits, compares `december12025` against `20251201`, finds no match, and
    reports the date as a value the question never gave. On one estate that
    single comparison refused 47 of 62 otherwise-clean candidates, and the
    docstring above `missing_literals` had named the gap and left it to "the
    reviewer decides". Nobody reviewed.

    Two sets come back, because a month named without a day is weaker
    evidence than a day. "since April 2026" does state `2026-04-01`; it does
    not state `2026-04-15`. The caller decides which it is willing to accept.
    """
    text = question or ""
    days: set[str] = set()
    months: set[str] = set()
    for pattern, order in ((_MONTH_DAY_YEAR, "mdy"), (_DAY_MONTH_YEAR, "dmy")):
        for match in pattern.finditer(text):
            if order == "mdy":
                month, day, year = match.group(1), match.group(2), match.group(3)
            else:
                day, month, year = match.group(1), match.group(2), match.group(3)
            number = _MONTH_NUMBER[month.lower()]
            days.add(f"{int(year):04d}-{number:02d}-{int(day):02d}")
    for match in _MONTH_YEAR.finditer(text):
        number = _MONTH_NUMBER[match.group(1).lower()]
        months.add(f"{int(match.group(2)):04d}-{number:02d}")
    # An ISO date in the question already survives `_squashed`, but it is
    # cheap to carry it here too so one function answers the whole question.
    for match in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        days.add(match.group(0))
        months.add(f"{match.group(1)}-{match.group(2)}")
    return days, months


def question_states_date(value: str, question: str) -> bool:
    """True when the question gives this date, in ISO or in English.

    A month with no day counts only for the first of that month, which is
    what "since April 2026" against `>= '2026-04-01'` means. Any other day
    of that month is a different filter and stays reported.
    """
    match = _ANY_DATE.match((value or "").strip())
    if not match:
        return False
    day = f"{int(match.group(1)):04d}-{match.group(2)}-{match.group(3)}"
    days, months = dates_in_words(question)
    if day in days:
        return True
    return match.group(3) == "01" and day[:7] in months


def documents(value: str, text: str) -> bool:
    """True when `text` states `value`, by the same reading as a question.

    Split out so the question and the written context are matched
    IDENTICALLY. Two copies of this rule would drift, and the short-value
    case is exactly where a drifted copy would be silently wrong: a
    two-letter code read as present inside a longer word is the miss this
    whole check exists to catch.
    """
    if not (text or "").strip():
        return False
    squashed = _squashed(value)
    if len(squashed) < 3:
        return squashed in {_squashed(word) for word in text.split()}
    return squashed in _squashed(text)


def missing_literals(gold_sql: str, question: str,
                     context: str = "") -> list[str]:
    """The values the gold filters on that NEITHER the question nor the
    written context gives.

    `context` is what the agent is handed. A value the context documents is
    answerable: a rule saying "a completed order has status exactly
    'Complete'" is how an agent knows to write `status = 'Complete'` when the
    question only says "completed orders". Without that lookup, this check
    reports every documented literal as a defect.

    MEASURED ON THE COMMITTED THELOOK SET on 2026-09-13, 104 golds against a
    13,237-character spec: 40 golds flagged on the question alone, 2 once the
    context was read. 38 of 40 were noise. A check that fires 40 times on 104
    questions is a check people switch off, and the 2 real ones go with it.

    Those 2 were real and have since been FIXED, so the same measurement now
    reads 39 and 0. tl002 asked for "top 5 brands by revenue inside one
    category" while the gold pinned 'Jeans'; tl007 asked for "return rate by
    category" while the gold carried a 500 threshold and a LIMIT 10. No agent
    could guess either, so both scored every agent wrong forever and diluted
    the lift. Their QUESTIONS were rewritten to state the value -- no gold SQL
    changed, so no answer moved. 39 rather than 40 because tl002's question
    now contains 'Jeans'.

    A gold that pins two product codes under a question reading "for a
    specific product and planning group" cannot be answered by any agent, so
    the score it produces is a measurement of the answer key. 14 of 25 mined
    golds on one estate were that, and the run read 0.000 in both arms.

    A value counts as given when the question carries it in any spelling that
    survives `_squashed`, so punctuation and spacing do not matter. A date the
    question describes in words ("last January") does not match the literal
    `'2026-01-01'` and is reported; the reviewer decides. An empty question is
    not checked: nothing was drafted, and a person fills it during review.
    """
    if not (question or "").strip():
        return []
    asked = _squashed(question)
    # A short value matches by accident inside a longer word. This is the
    # gap the whole tool exists to find, so reading it as covered is the worst
    # kind of miss: the estate stores a two-letter code and the business says
    # the word, and a checker that reads the word as containing the code sees
    # nothing wrong. Three golds on one customer key, each answerable-looking
    # and none answerable:
    #   store_status = 'AC'   under "How many ACtive stores..."
    #   country = 'CA'        under "...in CAnada?"
    #   region = 'LS'         under "...actuaL Sales units by region..."
    # plus the number shape that found it: '3' and '5' read as given by
    # "exclude organizations '139' and '250'".
    # Anything this short has to be a word of the question, not a fragment.
    words = {_squashed(word) for word in (question or "").split()}
    out = []
    for value in filter_literals(gold_sql):
        squashed = _squashed(value)
        if question_states_date(value, question):
            continue
        given = (squashed in words if len(squashed) < 3 else squashed in asked)
        # Documented counts as given. Checked after the question so the
        # cheaper path stays first and a value in BOTH is not reported twice.
        if not given and documents(value, context):
            continue
        if not given:
            out.append(value)
    return out

# The words a question uses when it cares which rows come first, or which one
# is wanted. Measured against two estates: the drafter writes none of them.
_ORDER_WORDS = frozenset("""
sort sorted sorting order ordered descend descending ascend ascending
highest lowest top bottom most least largest smallest biggest
rank ranked ranking best worst first last newest oldest latest earliest
""".split())


def orders_rows(sql: str) -> bool:
    """True when the outermost query decides which rows come first.

    Depth 0 only, the same reading `limit_without_order` uses: an ORDER BY
    inside a window frame (`OVER (ORDER BY x)`) or a CTE does not order what
    the caller gets back.
    """
    text = mask_string_literals(strip_comments(sql or "")).replace("`", "")
    depth = 0
    for piece in _LIMIT_PIECE.finditer(text):
        word = piece.group(0).upper()
        if word == "(":
            depth += 1
        elif word == ")":
            depth = max(0, depth - 1)
        elif not depth and word.startswith("ORDER"):
            return True
    return False


def question_names_order(question: str) -> bool:
    """True when the question says anything about which rows, or in what order."""
    words = set(_NOT_ALPHANUMERIC.sub(" ", (question or "").lower()).split())
    return bool(words & _ORDER_WORDS)


# `t.name`, `name`, `SUM(x) AS total`; the name a reader of the question would
# use is the alias when there is one, and the bare column otherwise.
_ALIAS = re.compile(r"\bAS\s+([`\"\w]+)\s*$", re.I)
_LAST_NAME = re.compile(r"([A-Za-z_][\w]*)\s*$")


def projected_names(sql: str) -> list[str]:
    """What each column of the answer is called, in order.

    The alias when the item has one, because that is the word a person reading
    the result sees; otherwise the last identifier in the expression. `*` is
    not a name and is left out: WHOLE_ROW already covers a browse.
    """
    items, _ = outermost_select(sql)
    names = []
    for item in items:
        text = item.strip().rstrip(",").strip()
        if not text or text.endswith("*"):
            continue
        alias = _ALIAS.search(text)
        found = alias.group(1).strip('`"') if alias else None
        if not found:
            tail = _LAST_NAME.search(text)
            found = tail.group(1) if tail else None
        if found:
            names.append(found)
    return names


def missing_projection(gold_sql: str, question: str) -> list[str]:
    """The answer's columns the question never names.

    Measured on 62 BEAVER golds, 348 projected columns: a question a person
    wrote names 76% of them, a question drafted from the SQL names 26%. The
    same drafter on a customer estate named 31%. An agent that cannot tell
    which columns are wanted writes SQL that runs and returns the wrong
    shape, which is why the failure moves from "did not run" to "wrong rows"
    rather than to "right".

    A column counts as named when every part of its name, split on
    underscores, appears in the question. `order_date` is named by "the date
    of the order". Case and punctuation do not matter.

    This is a WARNING and never a defect. "List the details of every order"
    legitimately means all of them, and a question is allowed to be shorter
    than its answer.
    """
    if not (question or "").strip():
        return []
    asked = _squashed(question)
    missing = []
    for name in projected_names(gold_sql):
        parts = [p for p in name.lower().split("_") if p]
        if not all(_squashed(part) in asked for part in parts):
            missing.append(name)
    return missing


# A column whose name ends in one of these holds an identifier, and the
# question has to say so: "for owner LIST69" against MOIRA_LIST_OWNER_KEY sends
# the agent to a column called OWNER. Measured: a human question names the
# key/id/code word 5 times in 5, a drafted one 2 times in 5.
_IDENTIFIER_SUFFIX = ("key", "id", "code", "number", "num")


def literal_on_wrong_column(gold_sql: str, question: str) -> list[tuple[str, str]]:
    """Values the question gives while sending the agent to another column.

    `missing_literals` passes these, because the value IS in the question. One
    gold filters `MOIRA_LIST_OWNER_KEY = 'LIST69.377-keeper-xenon'` under a
    question reading "for owner 'LIST69...'". The agent wrote `WHERE OWNER =`
    and got zero rows, and nothing in the answer key said why.

    Narrow on purpose, to the one shape that was measured: an identifier
    column (its name ends in key, id, code, number or num), a value the
    question gives, and a question that never says which of those it is. A
    wider rule - "the question does not name the column" - cannot be made to
    work here, because the column's own words often appear inside the value:
    `MOIRA_LIST_OWNER_KEY` looks named by a question quoting `'LIST69.377'`.

    Five cases on one estate, so this is a shape to show a reviewer, not a
    rate to trust.
    """
    if not (question or "").strip():
        return []
    asked = _squashed(question)
    wrong = []
    for column, value in filter_pairs(gold_sql):
        parts = [p for p in column.lower().split("_") if p]
        if not parts or parts[-1] not in _IDENTIFIER_SUFFIX:
            continue
        if _squashed(value) not in asked:
            continue                    # the value is missing, which is the other check
        # the value can carry the column's own words ('LIST69' looks like it
        # names MOIRA_LIST_...), so read the question with the value removed
        without_value = asked.replace(_squashed(value), " ", 1)
        # only when the question points at PART of the column and stops short
        # of the identifying word. A question naming no column at all is not
        # this defect: "units of PRD-1042" leaves the agent one candidate,
        # while "for owner LIST69" sends it to a column called OWNER.
        points_at = any(part in without_value for part in parts[:-1])
        if points_at and parts[-1] not in without_value:
            wrong.append((column, value))
    return wrong


# A table name in a mined query can carry its own project, and --db says
# where the run reads. `FROM a.b.c` names project a; `FROM b.c` names none
# and resolves in the --db project.
_TABLE_NAME = re.compile(r"\b(?:FROM|JOIN)\s+([`\w.\-]+)", re.I)


def projects_named(sql: str) -> list[str]:
    """The project part of every fully-qualified `project.dataset.table` the
    SQL names, lower-cased, first mention first.

    Two-part names are not here on purpose: they resolve in the project the
    job runs in, so they are always the run's own project.
    """
    text = strip_comments(sql or "").replace("`", "")
    found: list[str] = []
    for name in _TABLE_NAME.findall(text):
        parts = name.strip(",;()").split(".")
        if len(parts) >= 3 and parts[0].lower() not in found:
            found.append(parts[0].lower())
    return found


# Projects the caller has said the agent under test can read, beyond the
# billing project and the --db data project. A real estate spans several: on
# one, 221 mined golds named a production project and 132 named pre-production,
# and `--db bq://BILLING/DATA` names exactly one, so setting it for either made
# the other cross-project. 191 of 400 candidates were flagged for this alone.
#
# A module-level set rather than another argument threaded through nine call
# sites. It is set once from the command line and read here.
EXTRA_DATA_PROJECTS: set[str] = set()


def set_data_projects(names) -> None:
    """Name every project the agent under test can read."""
    EXTRA_DATA_PROJECTS.clear()
    EXTRA_DATA_PROJECTS.update(n.strip().lower() for n in (names or []) if n.strip())


def _other_projects(gold_sql: str, executor: "Executor") -> list[str]:
    """Projects the gold names that the run does not read. Warehouse only:
    SQLite has no project, and a name with no project is the run's own.

    A run reads TWO projects whenever they differ. `bq://billing/data.sales`
    bills `billing` and reads tables in `data`, so a gold that spells its own
    table `data.sales.orders` is correct and must not be flagged. Comparing
    against the billing project alone called every such gold cross-project,
    which is every estate that separates billing from storage.
    """
    billing = (getattr(executor, "project", None) or "").lower()
    if getattr(executor, "kind", "sqlite") != "bigquery" or not billing:
        return []
    # the spec's dataset is "data_project.dataset" when it names one
    data_project = (getattr(executor, "dataset", None) or "").partition(".")[0].lower()
    ours = ({billing, data_project} | EXTRA_DATA_PROJECTS) - {""}
    return [named for named in projects_named(gold_sql) if named not in ours]


# The pieces of a statement that decide where the outermost query starts and
# ends. Everything between parentheses belongs to a subquery or a call, so
# depth is all this has to track.
_SELECT_PIECE = re.compile(r"\(|\)|,|\bSELECT\b|\bFROM\b", re.I)
# `*`, `t.*`, `project.dataset.table.*`, with DISTINCT in front of the first one
_WHOLE_ROW_ITEM = re.compile(r"(?:(?:DISTINCT|ALL)\s+)?(?:[\w\-]+\.)*\*", re.I)


def outermost_select(sql: str) -> tuple[list[str], str]:
    """What the outermost query selects, and what it selects it from.

    The items are the text between that query's SELECT and its FROM, split on
    the commas between them; the second value is the text after the FROM. Only
    depth 0 counts, so a CTE body, a subquery and the argument of COUNT(*) are
    all skipped: they sit inside parentheses. A statement that starts with
    WITH is read the same way, because the outermost SELECT is the one after
    the last CTE.
    """
    text = mask_string_literals(strip_comments(sql or "")).replace("`", "")
    depth = 0
    items: list[str] = []
    item_start = None                      # None until the outermost SELECT
    for piece in _SELECT_PIECE.finditer(text):
        word = piece.group(0).upper()
        if word == "(":
            depth += 1
        elif word == ")":
            depth = max(0, depth - 1)
        elif depth:
            continue
        elif word == "SELECT":
            if item_start is None:
                item_start = piece.end()
        elif item_start is None:
            continue                       # still in the WITH list
        elif word == ",":
            items.append(text[item_start:piece.start()])
            item_start = piece.end()
        else:                              # the outermost FROM ends the list
            items.append(text[item_start:piece.start()])
            return items, text[piece.end():].strip()
    if item_start is not None:
        items.append(text[item_start:])
    return items, ""


# Same depth-0 reading as the projection above: a LIMIT or an ORDER BY inside
# parentheses belongs to a subquery, a CTE body or a window frame.
_LIMIT_PIECE = re.compile(r"\(|\)|\bORDER\s+BY\b|\bLIMIT\b", re.I)


def limit_without_order(sql: str) -> bool:
    """True when the outermost query caps its rows and does not order them.

    `SELECT id, day FROM orders LIMIT 20` has no fixed answer: the warehouse
    may return any 20 rows that satisfy the query, and two runs of the same
    statement can return different sets. Nothing can be keyed against that.

    Only depth 0 counts, and an ORDER BY inside a window frame
    (`OVER (ORDER BY x)`) does not order the output, so it does not rescue an
    outer LIMIT.

    Reading depth 0 only is a choice to under-report, not a claim that deeper
    caps are safe: `SELECT SUM(x) FROM (SELECT x FROM t LIMIT 5)` picks five
    arbitrary rows and its sum moves between runs. Catching that needs the
    cap and the ordering matched per subquery, and getting it wrong drops
    good golds. A reviewer sees the SQL either way, so the gate stays quiet
    on the shape it cannot read confidently.
    """
    text = mask_string_literals(strip_comments(sql or "")).replace("`", "")
    depth = 0
    ordered = limited = False
    for piece in _LIMIT_PIECE.finditer(text):
        word = piece.group(0).upper()
        if word == "(":
            depth += 1
        elif word == ")":
            depth = max(0, depth - 1)
        elif depth:
            continue
        elif word == "LIMIT":
            limited = True
        else:
            ordered = True
    return limited and not ordered


def selects_whole_row(sql: str) -> bool:
    """True when the outermost query hands back a whole table row: `SELECT *`
    or `SELECT t.*` over a named table.

    `SELECT * FROM (SELECT id, day FROM orders)` is not that: the columns are
    written down in the query, so the answer has a shape a prediction can
    match. A star over a table has no such list.
    """
    items, source = outermost_select(sql)
    if source.startswith("("):
        return False
    return any(_WHOLE_ROW_ITEM.match(item.strip()) for item in items)


# sum(Name) on a text column ran clean on SQLite and ranked countries
# alphabetically; the gate passed it and a real answer was scored wrong.
_NUMERIC_AGGREGATE_RE = re.compile(
    r"\b(sum|avg|total)\s*\(\s*(?:DISTINCT\s+)?\"?([A-Za-z_]\w*)\"?\s*\)", re.IGNORECASE)
_FROM_TABLE_RE = re.compile(r"\bFROM\s+\"?([A-Za-z_]\w*)\"?", re.IGNORECASE)


def _text_aggregates(gold_sql: str, executor: "Executor") -> list[str]:
    """Columns that sum()/avg() over non-numeric text, found by asking the
    database. A CSV-imported column can be stored as text yet hold numbers
    ("100"), which SQLite converts correctly; only text that casts to 0
    without being zero is the defect. Flagged when most values are like
    that. Single-table golds only: the column is probed on the first FROM
    table, and any probe failure means no finding.

    SQLite only: the probe is SQLite SQL (typeof, GLOB), and a warehouse
    does not need it. BigQuery refuses SUM over a STRING at dry-run time,
    so such a gold is already EXEC_FAIL; the probe would only add one
    failing round trip per aggregate."""
    if getattr(executor, "kind", "sqlite") != "sqlite":
        return []
    aggregated = {column for _fn, column in _NUMERIC_AGGREGATE_RE.findall(gold_sql)}
    table = _FROM_TABLE_RE.search(gold_sql)
    if not aggregated or not table:
        return []
    text_columns = []
    for column in sorted(aggregated):
        probe = executor.execute(
            f'SELECT SUM(CASE WHEN typeof("{column}") = \'text\' '
            f'AND CAST("{column}" AS REAL) = 0 '
            f'AND TRIM("{column}") NOT GLOB \'0*\' AND TRIM("{column}") NOT GLOB \'.0*\' '
            f'AND TRIM("{column}") NOT GLOB \'-0*\' THEN 1 ELSE 0 END) AS not_numeric, '
            f'COUNT("{column}") AS total FROM "{table.group(1)}"')
        if not probe.ok or not probe.rows:
            continue
        not_numeric = probe.rows[0].get("not_numeric") or 0
        total = probe.rows[0].get("total") or 0
        if total and not_numeric * 2 > total:
            text_columns.append(column)
    return text_columns

_PLACEHOLDER_RE = re.compile(r"\b(TODO|FIXME|PLACEHOLDER|XXX)\b", re.IGNORECASE)

# A single cell holding one of these reads as a non-answer, not real data.
_TRIVIAL_CELLS = {normalize_value(None), normalize_value(0)}

# Two runs disagreeing on rows means the gold is non-deterministic. EMPTY_BOTH
# (consistently empty) and MATCH (consistently equal) are both stable.
_UNSTABLE_VERDICTS = ("MISMATCH", "EMPTY_ONE")


@dataclass(frozen=True)
class GateFinding:
    """Defects found for one gold query; `ok` means it passed clean.

    `defects` is a tuple of the class names above (empty when clean). `detail`
    is a human note joining the reasons, for the report a reviewer reads.

    `warnings` are classes that a person must SEE and the gate must not act on.
    A defect drops the gold out of the clean file, and `split` then never sees
    it, so a class that fires on good golds cannot be one. MISSING_LITERAL is
    the case: it reads the question against the gold's values and cannot know
    that "Who is Summer Haynes?" gives the value stored as 'Haynes, Summer',
    that a spelled-out department name is the same one as its abbreviation, or
    that "more than 75%" is 0.75. Every one of those is a good gold, and
    dropping a good gold is the same class of error the check was written to
    catch.
    """

    gold_id: str
    defects: tuple[str, ...]
    detail: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True if the gold has no defects. A warning does not make it false."""
        return not self.defects

    @property
    def classes(self) -> tuple[str, ...]:
        """Everything the gate found, for a caller that shows all of it."""
        return self.defects + self.warnings


def _is_degenerate(rows: list[dict]) -> bool:
    """True if rows is a single 1-column cell holding NULL or 0."""
    if len(rows) != 1:
        return False
    cells = list(rows[0].values())
    if len(cells) != 1:
        return False
    return normalize_value(cells[0]) in _TRIVIAL_CELLS


def _failure_class(error: str | None) -> tuple[str, str]:
    """The defect class and its note for a gold that did not come back.

    Four different people fix these four, which is why they are four classes
    and not one. On one estate 179 candidates carried the single EXEC_FAIL
    label and 170 of them were not the SQL's fault: 112 hit the byte ceiling,
    34 were refused by the platform, 24 named a table that had been dropped.
    A reviewer reading "179 EXEC_FAIL" concludes the query log is junk. The
    truth was "112 of these are one --max-bytes raise away".

    Order matters where the strings overlap: a 403 mentioning a table name
    must read as NO_ACCESS, so the refusal test runs before the missing-table
    test."""
    if (error or "").startswith("timeout:"):
        return TIMEOUT, f"did not finish: {error}"
    if over_budget(error):
        return OVER_BUDGET, (
            f"the byte ceiling turned the query away before it ran: {error}. The SQL is "
            "not the problem: raise --max-bytes to the figure above, or narrow the "
            "question. The gold is dropped for now because nothing ran, so there is no "
            "answer key")
    if platform_refused(error):
        # a 403 from VPC Service Controls stopped one statement of a
        # 45-statement run while the other 41 went through, and it read
        # as "the SQL is broken". Nothing here is the query's fault.
        return NO_ACCESS, (f"the platform refused the call, not the SQL: {error}; "
                           + PLATFORM_REFUSAL_ADVICE)
    if table_missing(error):
        return TABLE_GONE, (
            f"the query names a table or dataset that is not there: {error}. It was right "
            "when the analyst wrote it, so this is not a SQL bug and no grant or ceiling "
            "brings it back. Drop the gold, or point it at the table that replaced it")
    return EXEC_FAIL, f"the SQL did not run: {error}"


def gate_one(
    gold_id: str,
    gold_sql: str,
    executor: Executor,
    check_stability: bool = False,
    gold_cache: dict | None = None,
    question: str = "",
    context: str = "",
) -> GateFinding:
    """Audit one gold query and return its GateFinding.

    Text-only defects (empty stub, non-SELECT) short-circuit before running,
    since a stub can't be executed and a non-SELECT must not be. Execution
    defects are then collected from a single run; check_stability adds a second
    run to catch non-deterministic golds (off by default — it doubles cost).

    `question` is the natural-language question this gold is the answer to,
    when the caller has one. It buys the MISSING_LITERAL check and nothing
    else; without it the audit is exactly what it was.

    `gold_cache` is written, never read: the gate's job is to run the key
    against the live database, and the rows it fetched are the same rows
    `score` would fetch next, so they are kept for it. Only a clean, whole
    result is kept; a truncated or failed one would mislead a later run.
    """
    defects: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    # kept apart and joined last. A warning is longer than a defect note by
    # nature (it has to say why it might be wrong), and appended in check
    # order it pushed "returns no rows" to the end of 90 words of advice on a
    # line whose whole job is to say why the gold was dropped
    advice: list[str] = []

    stripped = strip_comments(gold_sql).strip().rstrip(";").strip()
    if not stripped:
        return GateFinding(
            gold_id, (PLACEHOLDER_COMMENT,), "empty after stripping comments"
        )
    if _PLACEHOLDER_RE.search(gold_sql or ""):
        defects.append(PLACEHOLDER_COMMENT)
        notes.append("carries a placeholder marker (TODO/FIXME/...)")

    if not is_read_only(gold_sql):
        # Can't safely run a non-SELECT; report and stop here.
        defects.append(NON_SELECT)
        notes.append("not a read-only SELECT/WITH")
        return GateFinding(gold_id, tuple(defects), "; ".join(notes + advice), tuple(warnings))

    # 12 of 32 mined golds on one estate were SELECT *: the gold came back
    # with 151 columns against the agent's 8, 186 against 178, 56 against 1.
    # Nobody asks a question and wants 151 columns, so the row was someone
    # browsing a table. Neither --extra-columns setting helps: `tolerate`
    # accepts a superset and here the prediction is a subset.
    if selects_whole_row(gold_sql):
        defects.append(WHOLE_ROW)
        notes.append(
            "the projection is a whole row, not an answer: SELECT * returns every column "
            "of the table whatever was asked, so this is a browse and not a question. It "
            "can only be scored right by an agent that guesses the same column list. Name "
            "the columns the question asks for, or drop the gold")

    # 3 of 25 mined golds on one estate, and the same shape in a cold run on
    # public data. It runs and returns rows every time, and the rows need not
    # be the same rows: the row cap is the only thing choosing them.
    if limit_without_order(gold_sql):
        defects.append(LIMIT_NO_ORDER)
        notes.append(
            "the outermost query caps rows with LIMIT and does not order them, so the answer "
            "key is whatever rows came back that time and another run can return a different "
            "set. Add an ORDER BY that decides the rows, or drop the gold")

    # An answer key is a pair, and the gate used to read only half of it. A
    # question that drops the codes its gold pins is not a hard question, it
    # is an unanswerable one, and it scores every agent wrong at the same
    # rate: 14 of 25 golds on one estate, both arms at 0.000.
    missing = missing_literals(gold_sql, question, context)
    if missing:
        # a WARNING, never a defect. The check compares the question's text
        # against the gold's values and cannot read a spelling: "Who is Summer
        # Haynes?" gives the value stored as 'Haynes, Summer', a department
        # written out in full is the one stored abbreviated, and "more than
        # 75%" is 0.75. All three are good golds and all three fire here.
        # Two different problems, and only one of them can be fixed by
        # anybody. A missing business CODE is what --rules-in-context is
        # for: the question omits `company_code '1000'` so the written
        # context has to supply that it means Chile, and whether it does is
        # the measurement. A missing question PARAMETER -- which month,
        # which order -- can be supplied by nothing. No agent can guess it,
        # so the gold scores wrong in BOTH arms forever and its only effect
        # on a round is to dilute the lift, exactly as a dashboard tile
        # does.
        #
        # Measured on 39 clean golds from one estate: 24 omitted nothing, 11
        # omitted only codes, 3 omitted a parameter. Those 3 sat in a
        # reserved set of 18 and were guaranteed wrong in every round
        # anybody would ever run, and the gate kept them because it called
        # both cases the same warning.
        parameters = parameter_literals(gold_sql, missing)
        if parameters:
            defects.append(MISSING_PARAMETER)
            shown_p = ", ".join(parameters[:4])
            advice.append(
                f"the question does not give {len(parameters)} value(s) the "
                f"gold filters on, and they are question PARAMETERS rather "
                f"than business rules ({shown_p}). No written context can "
                "tell an agent which month or which order was meant, so this "
                "row scores wrong in BOTH arms and only dilutes a lift. Put "
                "the value back in the question, or drop the gold. Check the "
                "spelling first: a value the question gives in another form "
                "reads as missing here")
        else:
            warnings.append(MISSING_LITERAL)
            shown = ", ".join(missing[:6]) + (f", and {len(missing) - 6} more"
                                              if len(missing) > 6 else "")
            values = f"{len(missing)} value" + ("" if len(missing) == 1 else "s")
            advice.append(
                f"the question may leave out {values} the gold filters on "
                f"({shown}), and all of them read as business CODES. Under "
                "--rules-in-context that is deliberate: the question omits the "
                "rule so the written context has to supply it, which is the "
                "thing being measured. Check the spelling before you act - a "
                "value the question gives in another form reads as missing "
                "here. The gold is kept either way")

    # The drafter never writes an ordering phrase: 0 of 6 ordered golds on one
    # customer key, 2 of 22 on a mined benchmark arm. A question that does not
    # say which rows are wanted cannot be answered by any agent that has to
    # guess the ORDER BY.
    if orders_rows(gold_sql) and question and not question_names_order(question):
        warnings.append(MISSING_ORDER)
        advice.append(
            "the gold orders its rows and the question says nothing about order, so an agent "
            "has to guess the ORDER BY. Fires on a hand-written key too (11 of 22 ordered "
            "golds on one published benchmark), so read it rather than acting on it")

    # The largest of the three by measurement: a question a person wrote names
    # 76% of the columns its gold returns, a drafted one 26%.
    unnamed = missing_projection(gold_sql, question)
    if unnamed:
        shown = ", ".join(unnamed[:6]) + (f", and {len(unnamed) - 6} more"
                                          if len(unnamed) > 6 else "")
        warnings.append(MISSING_PROJECTION)
        columns = f"{len(unnamed)} column" + ("" if len(unnamed) == 1 else "s")
        advice.append(
            f"the answer returns {columns} the question never names ({shown}), so an agent "
            "has to guess the shape of the result. A question is allowed to be shorter than "
            "its answer - \"list the details\" means all of them - so read this one")

    # the value is there and the column is not, so MISSING_LITERAL passes it
    mismatched = literal_on_wrong_column(gold_sql, question)
    if mismatched:
        warnings.append(LITERAL_ON_WRONG_COLUMN)
        shown = ", ".join(f"{value!r} is compared to {column}" for column, value in mismatched[:3])
        advice.append(
            f"the question gives a value but names another column for it ({shown}): an agent "
            "writes the filter against the column the question says and gets no rows. Name "
            "the column in the question, or check the question means what the gold does")

    # --db says where the run reads; a mined statement carries its own names
    # and the two need not agree. 2 of 60 candidates on one estate named
    # another project, ran there, returned rows and came back clean. Not
    # fatal: rewriting the name to the --db project fixed one of the two
    # (the other did not exist there at all), so this says which, and lets
    # the rest of the audit run.
    other_projects = _other_projects(gold_sql, executor)
    if other_projects:
        defects.append(CROSS_PROJECT)
        notes.append(
            "names project " + ", ".join(other_projects) + ", not the --db project "
            f"{executor.project}: it runs and returns rows, but the agent under test reads "
            f"{executor.project}, so the answer key would come from another copy of the "
            "data. If the agent reads that project too, which is the ordinary case for a "
            "public dataset where --db is only the billing project, name the data instead "
            f"of the billing account: --db bq://{executor.project}/"
            + other_projects[0] + ".DATASET. Otherwise rewrite the name or drop the gold")

    res = executor.execute(gold_sql)
    if not res.ok:
        # A gold that cannot finish inside the executor's time budget is its
        # own defect class: EXEC_FAIL says "fix the SQL", TIMEOUT says "fix
        # the query's cost or raise the budget" — different people fix those.
        defect, note = _failure_class(res.error)
        defects.append(defect)
        notes.append(note)
        return GateFinding(gold_id, tuple(defects), "; ".join(notes + advice), tuple(warnings))

    if gold_cache is not None and not res.truncated:
        gold_cache[gold_sql] = res

    if res.truncated:
        defects.append(TRUNCATED)
        notes.append("hit the executor row cap")

    if not res.rows:
        defects.append(ZERO_ROWS)
        notes.append("returns no rows")
    elif _is_degenerate(res.rows):
        defects.append(DEGENERATE)
        notes.append("single trivial cell (NULL/0) — likely an empty aggregate")

    text_columns = _text_aggregates(gold_sql, executor)
    if text_columns:
        defects.append(TEXT_AGGREGATE)
        notes.append("sums or averages a text column (" + ", ".join(text_columns)
                     + "); SQLite treats text as 0, so the ranking is meaningless")

    if check_stability:
        second = executor.execute(gold_sql)
        if second.ok and compare(res.rows, second.rows, gold_sql) in _UNSTABLE_VERDICTS:
            defects.append(UNSTABLE)
            notes.append("two runs returned different rows")

    return GateFinding(gold_id, tuple(defects), "; ".join(notes + advice), tuple(warnings))


def gate(
    golds: list[tuple[str, str]],
    executor: Executor,
    check_stability: bool = False,
    workers: int = 1,
    gold_cache: dict | None = None,
    questions: dict[str, str] | None = None,
    context: str = "",
) -> list[GateFinding]:
    """Audit a list of (gold_id, gold_sql) pairs; return one finding per gold.

    `workers` runs that many golds at once; findings stay in input order
    (pool.map), and workers <= 1 runs inline.

    `questions` maps gold id to the question text, when the caller has it. It
    buys the MISSING_LITERAL warning and nothing else. Without it that check
    cannot run, which is why a key audited straight from a two-column CSV never
    raised it even when the file carried a question column beside the SQL.

    `context` is the written business context the agent will be handed, from
    `Spec.context_text()`. It makes the MISSING_LITERAL warning accurate
    rather than noisy: a value the context documents is answerable. Measured
    on the committed thelook set on 2026-09-13, 104 golds against a
    13,237-character spec, the warning fired on 40 golds without it and 2 with
    it. 38 of 40 were noise, and a warning that fires 40 times on 104
    questions is one people switch off -- taking the 2 real ones with it.
    Those 2 were real defects and were fixed, so the same measurement now
    reads 39 and 0.
    """
    def one(gold: tuple[str, str]) -> GateFinding:
        return gate_one(gold[0], gold[1], executor, check_stability, gold_cache,
                        question=(questions or {}).get(gold[0], ""),
                        context=context)

    if workers <= 1:
        return [one(gold) for gold in golds]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, golds))


def summarize(findings: list[GateFinding]) -> dict[str, int]:
    """Count how many golds carry each defect class (clean golds contribute none).

    A gold with two defects is counted under both, so the values can sum to more
    than the number of golds — this is a per-defect tally for the report header,
    not a partition.
    """
    counts: dict[str, int] = {}
    for finding in findings:
        for defect in finding.defects:
            counts[defect] = counts.get(defect, 0) + 1
    return counts


def summarize_warnings(findings: list[GateFinding]) -> dict[str, int]:
    """The same tally for the classes that do not drop a gold. Kept apart from
    `summarize` so no caller adds a warning to a defect count by accident and
    reports a clean key as flagged."""
    counts: dict[str, int] = {}
    for finding in findings:
        for warning in finding.warnings:
            counts[warning] = counts.get(warning, 0) + 1
    return counts
