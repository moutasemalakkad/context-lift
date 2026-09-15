"""Turn a warehouse's query history into candidate gold questions.

Writing an eval set by hand is the slowest part of starting an evaluation, and
hand-written synthetic questions are worse than slow: they only ask what the
schema can already answer, so they flatter the system under test. A measured
case — the same bare agent scored 14.3% on a machine-generated question set and
0% on a human-curated one. The generated set was not a baseline, it was an
artefact.

Real query history does not have that bias. The queries people actually run
carry the joins, filters and grain conventions the business uses, because they
had to be right for someone's report. This module ranks them, throws out the
ones that cannot serve as gold answers, and hands a human a short review list.

The human step is not optional. Mining proposes; a person approves. What comes
out is candidates, never golds.

Input is a plain list of SQL strings with counts, so any warehouse works —
there is no dependency on a particular query-log API. See `contextlift next`
for a BigQuery INFORMATION_SCHEMA.JOBS adapter.
"""
from __future__ import annotations

import csv
import hashlib
import inspect
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .executor import (BIGQUERY_PRICE_AS_OF, BIGQUERY_USD_PER_TIB, Executor,
                       comment_text, is_read_only, strip_comments)
from .gate import (DEGENERATE, LIMIT_NO_ORDER, MISSING_LITERAL, NO_ACCESS, OVER_BUDGET,
                   TABLE_GONE, WHOLE_ROW, ZERO_ROWS, GateFinding, gate_one,
                   literal_on_wrong_column, missing_literals, missing_projection,
                   orders_rows, question_names_order)

# String and number literals differ between two runs of the same report, so
# they are masked before fingerprinting: "WHERE region='EMEA'" and
# "WHERE region='APAC'" are one query shape asked twice, not two shapes.
_STRING_LITERAL = re.compile(r"'[^']*'")
_NUMBER_LITERAL = re.compile(r"\b\d+(?:\.\d+)?\b")
_WHITESPACE = re.compile(r"\s+")
# `cluster, sum(x)` and `cluster,sum(x)` are one shape. Collapsing runs of
# whitespace leaves the space NEXT TO punctuation, so the two spellings were
# two fingerprints; one pair in 120 on one pull, and plainly wrong either way.
_AROUND_PUNCTUATION = re.compile(r"\s*([,()=<>!+\-*/%])\s*")

# BI tools fire per-column metadata probes with generated aliases, thousands of
# times a day. Ranking a query log by frequency surfaces almost nothing else:
# on one engagement 198 of the top 200 patterns were this. Without the filter,
# mining returns a review file a human throws away.
_BI_TOOL_NOISE = re.compile(r"clmn\d+_|t\d+_D\d+", re.I)

# Metadata queries and bare table dumps are real queries that make useless
# golds: nobody asks "select everything from this table" as a business question.
_METADATA = re.compile(r"INFORMATION_SCHEMA|__TABLES__", re.I)
_BARE_DUMP = re.compile(r"^\s*SELECT\s+\*\s+FROM\s+\S+\s*(LIMIT\s+\d+)?\s*;?\s*$", re.I)

# A gold question should need thought to answer. Without this, mining fills the
# list with lookups and exports.
_ANALYTICAL = re.compile(
    r"\b(GROUP\s+BY|SUM|COUNT|AVG|MIN|MAX|JOIN|HAVING|QUALIFY|OVER)\b", re.I)

# A dashboard KPI tile: one aggregate, no grouping, no join. These dominate a
# query log by execution count because dashboards refresh on a timer, and they
# make trivial golds ("what is the total shipped quantity?"). Measured on one
# warehouse: the top 5 patterns by execution count were all this shape, at
# 107-109 runs each and exactly one user.
_AGGREGATE = re.compile(r"\b(SUM|COUNT|AVG|MIN|MAX)\s*\(", re.I)
_GROUP_BY = re.compile(r"\bGROUP\s+BY\b", re.I)
_JOIN = re.compile(r"\bJOIN\b", re.I)
# BI tools name the output after the measure. Looker uses _measure /
# _measure_kpi; the suffix convention is the tool's, not any one company's.
_MEASURE_ALIAS = re.compile(r"\bAS\s+\w*_(measure|kpi)\w*", re.I)


@dataclass(frozen=True)
class MinedQuery:
    """One SQL statement observed in the query log.

    `times_seen` is how often this exact text ran. Pass 1 when the log has one
    row per execution and let mining do the counting. `ran_by` is every
    account that ran it, joined by ';' (what `log` writes), needed only to
    exclude your own traffic — see `exclude_accounts` on mine_candidates.
    `question` is what a person already wrote for this query, if anything;
    `log` never fills it, a hand-edited log CSV can. It is used only when no
    question writer runs, and it is checked the same way a drafted one is.
    """

    sql: str
    times_seen: int = 1
    ran_by: str = ""
    distinct_users: int = 0
    question: str = ""


@dataclass(frozen=True)
class Candidate:
    """A proposed gold, ranked and audited but NOT yet approved by a human.

    `question` is empty unless a question writer was supplied or the log
    carried one — a human fills it during review, and whatever is there is
    checked against the values the SQL filters on (MISSING_LITERAL).
    `gate_status` is "ok" or the defect names joined by "+",
    so a reviewer can sort the list and read the clean ones first.
    `accounts` is how many accounts ran the shape (0 when the log carried no
    `ran_by`): a shape one person ever ran is a question about that person's
    habit, and the reviewer sees the count beside the row. `out_of_scope` is
    every table the SQL names that the run cannot reach, joined by '; ', so a
    row whose measures live somewhere else does not read as answerable.
    """

    id: str
    question: str
    gold_sql: str
    times_seen: int
    gate_status: str
    gate_detail: str
    table: str = ""        # the primary table, `dataset.table` when the SQL qualifies it
    accounts: int = 0
    out_of_scope: str = ""

    @property
    def usable(self) -> bool:
        """True when the gate found nothing that disqualifies this SQL as a gold.

        A warning does not. `gate_status` reads `ok` or `ok (MISSING_LITERAL)`,
        and both are usable: the warning is something the reviewer must read,
        not a reason to drop the row.
        """
        return self.gate_status == "ok" or self.gate_status.startswith("ok (")


# A JSON comment header a tool wrote about itself. dbt, Looker and GitHub
# Actions all prepend one, and the query under it is a model build or a
# deployment check rather than a question somebody asked. Measured on one
# estate: 31 of 2,000 shapes and 5 of 75 gated golds, and the golds it
# produced read like "Return the column names that are inactive for the
# view_daily_visits table" - a data-quality probe, not a business question.
#
# Excluding service accounts does NOT catch these. dbt and CI run under a
# person's credentials, so the job history shows a human running them.
MACHINE_SQL = re.compile(
    r'"app"\s*:\s*"(dbt|looker)"|dbt_version|deployment-method'
    r'|deployment-environment|github-actions|"profile_name"',
    re.IGNORECASE)


def written_by_a_tool(sql: str) -> bool:
    """True when the SQL carries a header a tool wrote about itself."""
    return bool(MACHINE_SQL.search(sql or ""))


# A query comparing one copy of the data to another. Engineering writes these
# constantly during a migration and they look exactly like analyst work to
# every filter that came before: they join two or more tables, they aggregate,
# and no tool wrote a header on them. But no written business rule changes the
# answer to "compare this table to its backup and tell me pass or fail", so
# they are dead weight in a gold set for the same reason a dashboard tile is.
#
# Measured on one enterprise estate, after `--min-tables 2 --require-analytical`
# had already removed the dashboards: **32 of 89 usable candidates (36%)** were
# these, and 65 of all 344 mined shapes (19%). A model reviewer rejected most
# of them for the wrong reason -- it said the question did not match the SQL --
# which is the right answer with an unactionable explanation.
#
# Three signals, any one of which is enough, because they are what the queries
# actually contain rather than what they mean:
#   a table named as a copy      _backup, _bkp, _archv, _old, _new1, backup_
#   a comparison verdict         a PASS/FAIL projection, or a *_diff column
#   a row-count reconciliation   row_count, src_row_count, COUNT(*) on both sides
#
# Nobody has published a recipe for this. Checked 2026-09-11: Spider 2.0 is
# sourced from industrial applications and BEAVER from enterprise warehouses,
# but neither documents mining and filtering a production query log, so there
# is no canonical answer to copy and this is new ground rather than a
# reinvention.
_COPY_TABLE = re.compile(
    r"\b\w+_(?:backup|bkp|archv|archive|old|new\d*|copy|tmp|temp|v\d+)\b"
    r"|\bbackup_\w+|\b\w+_stg\b|\bstg_\w+", re.I)
_COMPARISON_VERDICT = re.compile(
    r"'(?:PASS|FAIL|MATCH|MISMATCH|OK|NOK)'"
    r"|\bas\s+\w*(?:pass_fail|match_status|comparison|_diff|_delta)\b"
    r"|\b(?:pass_fail|is_match|row_count_match)\b", re.I)
_ROW_COUNT_CHECK = re.compile(
    r"\b(?:src|source|bkp|backup|old|new|stg|dm)_row_count\b"
    r"|\brow_count\s+(?:as|,|\))"
    r"|\bas\s+(?:src|source|bkp|backup|old|new|stg)_\w*count\b", re.I)


def compares_two_copies(sql: str) -> bool:
    """True when the SQL reconciles one copy of the data against another.

    A migration check, not a question. See the comment above for what each
    signal is and what it was measured at.

    Deliberately requires TWO signals, not one. A single `_stg` table name is
    common in a warehouse where staging is where the data lives, and dropping
    every query that touches one would throw away real questions: on the
    estate this was written from, one gold asking for size-code counts by
    planning group reads a staging table and is a perfectly good business
    question. Two signals together -- a copy-shaped table AND a comparison
    verdict, say -- is what a reconciliation looks like and an analyst's
    question does not.
    """
    text = strip_comments(sql or "")
    hits = sum(bool(pattern.search(text)) for pattern in
               (_COPY_TABLE, _COMPARISON_VERDICT, _ROW_COUNT_CHECK))
    return hits >= 2


# A question carrying this many quoted values is one analyst's own lookup:
# "units per store for styles '00501', '04511', '00505', '29507', '00514'".
# Nobody else asks it, and the agent under test answered 16 such questions
# with no SQL at all. Two is the limit because a real question does name a
# value sometimes - "revenue in 2024 for Mexico" - but not six.
def literal_count(text: str) -> int:
    """Quoted values in a question."""
    return len(re.findall(r"'[^']{1,40}'", text or ""))


def fingerprint(sql: str) -> str:
    """A stable key for "the same query shape", ignoring literals and layout.

    Comments are stripped, string and number literals masked, whitespace
    collapsed, the space around commas and operators removed, case folded.
    Two queries with the same fingerprint are the same report run with
    different parameters.
    """
    text = strip_comments(sql or "")
    text = _STRING_LITERAL.sub("'?'", text)
    text = _NUMBER_LITERAL.sub("?", text)
    text = _WHITESPACE.sub(" ", text).strip().rstrip(";").strip().lower()
    return _AROUND_PUNCTUATION.sub(r"\1", text)


_FILTER_WORD = re.compile(r"\b(WHERE|AND)\b", re.I)


def disabled_filter(sql: str) -> str:
    """A note naming the commented-out text that holds a WHERE or AND, or "".

    Two mined candidates on one estate had their WHERE line inside a
    comment. `--write-questions` read the comment as live and drafted a
    question naming a filter the query does not apply, which is worse than
    a vague draft: in a spreadsheet it looks right.
    """
    text = comment_text(sql)
    if not _FILTER_WORD.search(text):
        return ""
    return ("a filter is commented out, so the query does not apply it: "
            + (text if len(text) <= 120 else text[:117] + "..."))


# What must not reach the drafter as a statement of purpose: a comment holding
# switched-off SQL. `disabled_filter` above exists because two shapes on one
# estate had their WHERE line inside a comment, and a question drafted from a
# filter the query does not apply is worse than a vague one, because in a
# spreadsheet it reads as correct. A quoted value is refused for the same
# reason plus a second one: a comment that spells out a business code would
# hand the drafter the very code rule 3 of QUESTION_PROMPT keeps out of the
# question. `AND` and `OR` are only refused at the START of the comment, since
# refusing them anywhere would throw away ordinary English ("materials and
# plants for the audit") and the note is the most useful signal in the row.
_SQL_IN_A_COMMENT = re.compile(
    r"\b(SELECT|FROM|JOIN|WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|UNION|LIMIT|CAST)\b"
    r"|[=<>]|'[^']*'", re.I)
# `comment_text` keeps the `--` and `/*` markers, so the continued-filter test
# has to look past them: the shape being refused is `-- AND status = 'X'`.
_CONTINUED_FILTER = re.compile(r"^\s*(?:--|/\*)?\s*(AND|OR|,)\b", re.I)
# One sentence of purpose is what this is for. Header banners on one estate ran
# to changelog blocks, and a note longer than the SQL drafts the question from
# the changelog.
NOTE_CHARS = 300


def analyst_note(sql: str) -> str:
    """The analyst's own comment, when it reads as prose about what the query
    is FOR, else "".

    Worth parsing because it says the one thing the SQL cannot. Measured on one
    estate: 20 of the 59 candidates a reviewer rejected as `does_not_match`
    carried a comment, and the drafter saw none of them -- every stage of this
    pipeline reads `strip_comments`. The row that made the case:

        the drafted question : "The material, plant, from date, to date, cost
                               estimate number, and total value per unit for
                               materials with a standard cost of 0"
        the analyst's comment: -- Pull the actual list of the 108 for follow-up

    Filtered rather than passed straight through: see `_SQL_IN_A_COMMENT`.
    """
    note = comment_text(sql)
    if not note or _SQL_IN_A_COMMENT.search(note) or _CONTINUED_FILTER.match(note):
        return ""
    return note if len(note) <= NOTE_CHARS else note[:NOTE_CHARS].rstrip() + "..."


def accounts_of(ran_by: str) -> list[str]:
    """The accounts behind one shape, lower-cased, from the `ran_by` column.

    `log` joins them with ';' (`STRING_AGG(DISTINCT user_email, ';' ORDER BY
    user_email)`), plain addresses or hashes. A log with one address per
    shape parses the same way, so an older CSV still works.
    """
    return [account.strip().lower() for account in (ran_by or "").split(";") if account.strip()]


def excluded_account(ran_by: str, excluded: set[str]) -> bool:
    """True when ANY account of the shape is one to leave out.

    `ran_by` used to be ANY_VALUE(user_email), one account picked at random
    out of however many ran the shape, so an eval account that shared a
    shape with an analyst survived whenever the pick went the other way (76
    of 3,000 shapes on one estate had two accounts).
    """
    return any(account in excluded for account in accounts_of(ran_by))


def _short_id(fingerprint_text: str, index: int) -> str:
    """Deterministic id: same log in, same ids out, so a review file diffs."""
    digest = hashlib.sha1(fingerprint_text.encode()).hexdigest()[:8]
    return f"m{index:03d}_{digest}"


class QuestionWriter:
    """Anything that can name the business question a SQL statement answers.

    Structural, like Executor — a callable class or any object with this method
    qualifies, so an LLM backend can be supplied without this module importing
    one.

    A writer may also offer `rewrite(sql, question, missing)`. Mining calls it
    once, and only when the drafted question dropped values the SQL filters
    on. A writer without it is used as-is and the candidate carries the
    MISSING_LITERAL status instead.

    `write` may take a second argument, the analyst's own comment on the query
    (see `analyst_note`). Mining passes it only to a writer whose signature
    accepts it, so a one-argument writer written before that keeps working.
    """

    def write(self, sql: str, comment: str = "") -> str:  # pragma: no cover - protocol shape
        ...


# The four rules below are docs/research/README.md practice 11, in the order that
# practice lists them. They were written after counting what this prompt costs:
# on two independent pulls from one estate, reviewed by the model that did NOT
# write the questions, 140 of 372 candidates (38%) were rejected because the
# drafted English did not ask what the SQL answers. Every one of those ran on
# BigQuery, returned rows, was inside budget and passed all 16 gate checks, so
# the warehouse money was already spent and only the sentence was wrong. The
# old prompt asked for three things and never said the question had to read as
# a question, and the drafter answered with the SELECT list in prose.
QUESTION_PROMPT = """You are naming the business question a SQL query answers.

Write ONE question a business user would ask, and make it read as a question:
"how much did we sell in each order state?", not "the total sale price by
order status". A noun phrase that lists the columns the query selects is not a
question, and it is the most common way this goes wrong.

{note}Four rules, and the question has to satisfy all four at once.

1. IDENTIFY THE SAME ANSWER. Same rows, same grouping, same aggregation, same
   limit as this query. If it takes a top 10 the question says top 10; if it
   groups by month the question asks per month; if the answer shows the order
   date, the customer and the total, the question asks for all three, in
   business words. When the query orders its rows, say which come first: "the
   ten largest by revenue", "the most recent first". Leave that out only when
   the query does not order its rows.

2. STATE EVERY PARAMETER. Anything a person chose arbitrarily -- a date, a
   month, a specific id -- belongs in the question, spelled out: "units of
   Blue Shirt on 2024-01-05", and never "units of a specific product on a
   specific day". No document can supply an arbitrary value, so a question
   that leaves one out cannot be answered by anybody. When the value is an
   id, a key or a code, say which: "for owner key LIST69", not "for owner
   LIST69", or the reader filters on the wrong column.

3. STATE NO BUSINESS CODES. status != 'CANCELLED' is a rule this business
   wrote down for its own people, so the question says "completed orders" and
   never the code. Spell the code out and the question hands its answer to a
   reader who knows nothing about this business. Rounding, casting and which
   statuses are excluded are the same: a business word such as revenue,
   completed or delivered already carries them.

4. USE BUSINESS WORDS, NOT COLUMN NAMES. "how much did we sell in each order
   state", never "total of order_item_sale_price grouped by order_status". Do
   not name tables or columns as they are spelled in the SQL.

Rules 2 and 3 pull against each other, and telling them apart is the craft of
this. The test: could somebody who knows this business, but has never seen
this table, supply the value? If they could it is a business code, so leave it
out. If it is arbitrary, state it.

Reply with the question only.

SQL:
{sql}
"""

# The analyst's comment goes ABOVE the rules because it is the one line in the
# row that says what the query is FOR, and the drafter had never seen it: 20 of
# the 59 rows one reviewer rejected as does_not_match carried a comment, and
# `strip_comments` throws comments away everywhere else in the pipeline. The row
# that made the case wrote six SELECT columns in prose while its own comment
# read `-- Pull the actual list of the 108 for follow-up`.
NOTE_PREFACE = """The analyst who wrote this query left a comment on it. It says
what the query is FOR, which the SQL alone does not, so start from the purpose
it states:

{note}

"""


RETRY_PROMPT = """The question below leaves out something an agent needs, so
nobody could answer it from the question alone. Write it again, in the same
business words, carrying every one of these:

{missing}

Keep it one short line, use business words rather than the SQL's own spelling
of a column, and reply with the question only.

The question you wrote: {question}

SQL:
{sql}
"""


class PromptQuestionWriter:
    """Drafts the question with any text backend (backends.gemini_backend or
    echo_backend). The draft goes into the review file: a person reads it
    next to the SQL and rewrites or rejects it before the row is a gold."""

    def __init__(self, backend: Callable[[str], str]):
        self.backend = backend

    def write(self, sql: str, comment: str = "") -> str:
        """One question. `comment` is the analyst's own note on the query, and
        an empty one leaves the note block out of the prompt rather than
        printing an empty heading the model has to interpret."""
        note = NOTE_PREFACE.format(note=comment.strip()) if comment.strip() else ""
        return self._one_line(self.backend(QUESTION_PROMPT.format(
            sql=sql.strip(), note=note)))

    def rewrite(self, sql: str, question: str, missing: list[str]) -> str:
        """Ask again for one question, naming the values the first draft lost.

        One call, and mining does not call it twice: a model that ignored the
        list once ignores it again, and the candidate carrying MISSING_LITERAL
        into the review sheet is the cheaper outcome.
        """
        return self._one_line(self.backend(RETRY_PROMPT.format(
            sql=sql.strip(), question=question,
            missing="\n".join(f"- {value}" for value in missing))))

    @staticmethod
    def _one_line(text: str) -> str:
        # a model sometimes quotes the question or adds a label
        lines = (text or "").strip().splitlines()
        if not lines:
            return ""
        return lines[0].strip().removeprefix("Question:").strip().strip('"').strip()


# The self-check on the drafted English, and the two shapes that count as
# asking something: a question word or a request verb, at the start of the
# sentence. Nothing here is a parser. The failure mode is visible in the first
# two words, and the alternative -- a second model call to read the first one
# -- costs one more call per shape, which on the 437-shape pull the loop below
# cites is 437 calls for something a regular expression settles.
_ASKS = re.compile(
    r"^\s*(what|which|who|whose|whom|when|where|why|how|do|does|did|is|are|was|were|"
    r"has|have|had|can|could|should|would|will)\b", re.I)
_REQUESTS = re.compile(
    r"^\s*(list|show|give|find|count|rank|compare|identify|name|report|return|"
    r"tell|break\s+down)\b", re.I)
# What the retry is asked to fix, and what the review sheet shows. Two
# strings because they are read by two different readers: one is an
# instruction to a model, the other is a note to the person reviewing.
COLUMN_LIST_OWED = ("a question, not the list of columns the query selects: start it "
                    "with what, which, how many, or a verb, and ask something")
COLUMN_LIST_NOTE = ("the question reads as a list of the selected columns rather than "
                    "something somebody would ask")


def reads_as_a_column_list(question: str) -> bool:
    """True when the drafted English is a noun phrase rather than a question.

    This is THE failure mode, measured 2026-09-12: of 372 candidates on one
    estate, 140 (38%) were rejected because the English did not ask what the
    SQL answers, and the drafter's habit was to write out the SELECT list --
    "The material, plant, from date, to date, cost estimate number, and total
    value per unit for materials with a standard cost of 0". That sentence has
    no verb and asks nothing, and no reviewer can keep it.

    Deterministic, and nothing is dropped by it: it adds a line to the one
    retry the drafter already makes (`_drafted_question`) and a note to
    `gate_detail`. The gate does not read it, so no candidate's status moves.
    """
    text = (question or "").strip()
    # An empty question is a different problem: a run without
    # `--write-questions` leaves every one of them empty for a person to fill
    # in, and flagging all of them as bad English says nothing.
    if not text or text.endswith("?"):
        return False
    # "For each month, how many orders shipped" asks something. The clause in
    # front of the comma is not the whole sentence, so the second one is
    # tested too; "The material, plant, from date" fails both.
    openings = [text, text.partition(",")[2].strip()]
    return not any(_ASKS.match(part) or _REQUESTS.match(part)
                   for part in openings if part)


_FILTER_SUGGEST = re.compile(r"LIKE\s+UPPER\(\s*'%'\s*\)", re.I)
_ONE_COLUMN_LIST = re.compile(
    r"^\s*SELECT\s+(?:DISTINCT\s+)?[^,]+?\s+FROM\b.*\bGROUP\s+BY\s+1\b.*\bLIMIT\s+\d+", re.I | re.S)


def is_dashboard_tile(sql: str) -> bool:
    """True if this looks like a single-KPI dashboard tile.

    Shape, not vocabulary: one aggregate, no GROUP BY, no JOIN. That is a
    number on a card, and as a gold it asks "what is the total X?" — which any
    system answers without knowing anything about the business.

    A `_measure` / `_kpi` output alias is taken as confirmation, because BI
    tools name the column after the measure, but the shape alone is enough.
    """
    text = strip_comments(sql or "")
    if not _AGGREGATE.search(text):
        return False
    if _GROUP_BY.search(text) or _JOIN.search(text):
        return False
    return True


def is_noise(sql: str, require_analytical: bool = False) -> bool:
    """True if this query cannot become a useful gold, whatever its frequency.

    Seven classes, each learned by having a review file ruined by it: BI-tool
    metadata probes, INFORMATION_SCHEMA queries, bare `SELECT * FROM t` dumps,
    single-KPI dashboard tiles, dbt temp tables, a BI tool's filter-dropdown
    value lists, and (when require_analytical) anything with no aggregation
    or join.
    """
    # dbt materialises through `<table>__dbt_tmp` and keeps `<table>__dbt_backup`
    # while a build runs; both exist for minutes and are never a gold. The
    # tool's own convention, so it belongs here with the BI-tool patterns.
    if re.search(r"__dbt_(tmp|backup)\b", sql or "", re.I):
        return True
    text = strip_comments(sql or "")
    if _BI_TOOL_NOISE.search(text) or _METADATA.search(text):
        return True
    # a BI tool's filter dropdown: one column, LIKE UPPER('%') matching
    # everything, GROUP BY 1, LIMIT 1000. It runs constantly, executes fine,
    # passes the gate, and the drafter writes "All business units" for it.
    # 13 of 22 gate-clean candidates on one estate. A GROUP BY is not
    # evidence of analysis.
    if _FILTER_SUGGEST.search(text):
        return True
    if (_ONE_COLUMN_LIST.search(text) and not _AGGREGATE.search(text)
            and not _JOIN.search(text)):
        return True
    if _BARE_DUMP.match(text.strip()):
        return True
    if is_dashboard_tile(text):
        return True
    if require_analytical and not _ANALYTICAL.search(text):
        return True
    return False


def qualified_tables_named(sql: str) -> list[str]:
    """The distinct tables the SQL text names, as written but lower-cased and
    without backticks, first mention first: `project.dataset.table` keeps its
    project.

    `tables_named` is the two-part view of the same list, which is what the
    scope and `spread` work in. The project only matters when the question is
    whether the run can reach the table at all.
    """
    # backticks go first: BigQuery lets each part carry its own pair,
    # `project`.`dataset`.`table`, and a match that stopped at the first
    # closing backtick captured the project on 60% of one real log's shapes
    text = strip_comments(sql or "").replace("`", "")
    found: list[str] = []
    for name in re.findall(r"\b(?:FROM|JOIN)\s+([\w.\-]+)", text, re.I):
        if name.lower() not in found:
            found.append(name.lower())
    return found


def tables_named(sql: str) -> list[str]:
    """The distinct tables the SQL text names, first mention first, lowercased,
    as `dataset.table` when the text qualifies them and bare otherwise. The
    first one is the shape's primary table: what `spread` groups by and what
    a `--datasets` scope is checked against."""
    found: list[str] = []
    for name in qualified_tables_named(sql):
        parts = name.split(".")
        key = ".".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
        if key not in found:
            found.append(key)
    return found


def out_of_scope_tables(sql: str, datasets: set[str] | None = None,
                        project: str | None = None,
                        default_dataset: str | None = None) -> list[str]:
    """The tables the SQL names that the run cannot reach: a dataset outside
    `datasets`, or a project that is not the run's own.

    Two field golds were labelled with an in-scope dimension table, the only
    in-scope table they named, while the query really reads a fact table in a
    third project and the question asks for measures the dimension does not
    hold. Both were routing misses no agent could win, and nothing in the
    review sheet said why. The names come back as written, project included,
    so a reviewer can see it.

    A bare table name is not here: it resolves in `default_dataset` when one
    is known, and says nothing at all when none is.
    """
    wanted = {dataset.lower() for dataset in (datasets or ())}
    outside: list[str] = []
    for name in qualified_tables_named(sql):
        parts = name.split(".")
        dataset = parts[-2] if len(parts) >= 2 else (default_dataset or "").lower()
        if len(parts) >= 3 and project and parts[-3] != project.lower():
            outside.append(name)
        elif wanted and dataset and dataset not in wanted:
            outside.append(name)
    return outside


def table_count(sql: str) -> int:
    """How many distinct tables the SQL text names, as a rough shape signal."""
    return len({t.split(".")[-1] for t in tables_named(sql)})


def mine_candidates(
    queries: list[MinedQuery],
    executor: Executor,
    question_writer: QuestionWriter | None = None,
    rules_in_context: bool = False,
    limit: int | None = None,
    min_times_seen: int = 1,
    exclude_accounts: frozenset[str] | set[str] | None = None,
    require_analytical: bool = False,
    rank_by: str = "frequency",
    min_tables: int = 1,
    drop_machine_sql: bool = True,
    drop_migration_checks: bool = True,
    max_question_literals: int | None = 2,
    datasets: set[str] | None = None,
    default_dataset: str | None = None,
    project: str | None = None,
    spread: bool = True,
    counts_out: dict | None = None,
    before_gate: Callable[[list[str]], bool] | None = None,
    progress: Callable[[int, int, dict, str], None] | None = None,
    budget: float | None = None,
    spent: Callable[[], float] | None = None,
    on_candidate: Callable[[Candidate], None] | None = None,
) -> list[Candidate]:
    """Rank query history into candidate golds, most-run first.

    Order of operations matters and is part of the method:
      1. drop anything that is not a read-only SELECT — a gold must be safe to
         run repeatedly, and this also removes DML noise from the log
      2. collapse to query shapes and sum their counts, so one nightly report
         does not fill the list with a hundred near-identical entries
      3. rank by how often the shape ran — frequency is the only signal in a
         log about what the business actually cares about — then, unless
         `spread` is off, take shapes round-robin across tables so the
         busiest table cannot fill the sample by itself
      4. gate every survivor by executing it, so a broken candidate never
         reaches a human reviewer
      5. optionally write the natural-language question

    Gating runs after ranking and after `limit` is applied, because executing
    queries is the expensive step and there is no reason to run one that will
    not be reviewed.

    Args:
        queries: observed SQL with counts.
        executor: used to run each candidate during gating.
        question_writer: optional; fills `question` from the SQL.
        rules_in_context: for a run measuring whether written context helps.
            A drafted question then leaves the values its gold filters on OUT,
            because the business rule belongs in the context and a question
            that states it hands the bare arm the rule for free. Measured on
            25 mined golds: 10 of them, 40%, named a rule and were unusable
            for a paired comparison.
        limit: keep only the top N shapes before gating.
        min_times_seen: drop shapes that ran fewer times than this. Raise it to
            skip one-off ad-hoc queries.
        exclude_accounts: accounts whose queries must NOT be mined. **Always
            include your own evaluation account.** An eval run hits the same
            tables hundreds of times, and mining that traffic back turns your
            agent's own SQL into the gold it is scored against — the leak
            `leakage.check_leakage` exists to catch, arriving through the front
            door instead. Given as plain addresses, matched lower-cased against
            every account in `ran_by` and against the MD5 of each lower-cased
            address, so a `log --hash-accounts` CSV needs no hashing by hand.
            A shape goes when any one of its accounts is excluded.
        drop_migration_checks: drop queries that reconcile one copy of the
            data against another - a table against its backup, staging
            against the data mart. On by default. They pass `min_tables` and
            `require_analytical` because they genuinely join and aggregate,
            and no written business rule can change the answer to "compare
            these and say pass or fail". Measured at 36% of usable
            candidates on one estate, with 0 real questions wrongly dropped.
        require_analytical: drop queries with no aggregation or join. Off by
            default so a simple lookup can still become a gold; turn it on for a
            warehouse whose log is mostly exports.
        datasets: keep only shapes whose primary table sits in one of these
            datasets. A shape naming an unqualified table says nothing about
            which dataset it means: it is placed in `default_dataset` when
            one is given (the dataset of `--db`, where the warehouse itself
            resolves such a name) and dropped otherwise.
        default_dataset: where an unqualified primary table is placed under
            a scope, by id (`shop`, not `project.shop`).
        project: the project the run reads, so a table in another one is
            recorded on the candidate. It is not a reason to drop the shape:
            the reviewer decides whether the question is answerable.
        spread: round-robin across tables (default). Off gives raw top-N.
        counts_out: if given, filled with "shapes" (read-only shapes before
            the scope) and "in_scope" (after it), so a scope that matched
            nothing can be reported as that and not as an empty log; under
            a scope also "unqualified" (shapes with no dataset on their
            primary table), "placed" (how many went into `default_dataset`)
            and "placed_in" (which one); "out_of_scope" (candidates naming a
            table the run cannot reach); and "stopped" when `before_gate`
            said no.
        before_gate: called with the SQL of the shapes about to be gated,
            the first moment they are known. Returning False stops the run
            with no candidates. The CLI prices those statements here: a
            ceiling computed before ranking was 30 to 60 times the outcome
            on one estate, and this is where a measured estimate is possible.
        budget: dollars this stage may spend on warehouse queries. Checked
            between shapes against `spent`, so it is a ceiling the run
            cannot pass rather than a comparison made once against an
            estimate. Reaching it stops the run and keeps everything found
            so far; `counts_out["budget_stopped"]` says where it stopped.
            Needs `spent` to do anything.
        spent: called with no arguments, returns dollars billed so far. The
            caller owns this because the executor knows the bytes and the
            price, and this module should not know either.
        on_candidate: called with each candidate the moment it is found, so
            a long run has a readable file before it finishes. A stage whose
            output appears only on success cannot be interrupted, and one
            interrupted mine threw away 36 minutes and $12.41.
    """
    excluded = {a.lower() for a in (exclude_accounts or ())}
    # a log pulled with `log --hash-accounts` carries
    # TO_HEX(MD5(LOWER(user_email))) in ran_by; the same digest of each
    # plain address matches it, so nobody has to hash by hand
    excluded |= {hashlib.md5(a.encode()).hexdigest() for a in excluded}
    counts: Counter[str] = Counter()
    users: Counter[str] = Counter()
    exemplar: dict[str, str] = {}
    exemplar_question: dict[str, str] = {}
    shape_accounts: dict[str, set[str]] = {}

    for item in queries:
        # ran_by carries every account of the shape, joined by ';': the shape
        # goes when ANY of them is excluded, or an eval account that shares a
        # shape with an analyst keeps its own traffic in the key
        if excluded_account(item.ran_by, excluded):
            continue
        if not is_read_only(item.sql):
            continue
        if is_noise(item.sql, require_analytical):
            continue
        if drop_migration_checks and compares_two_copies(item.sql):
            if counts_out is not None:
                counts_out["migration_checks"] = counts_out.get("migration_checks", 0) + 1
            continue
        if drop_machine_sql and written_by_a_tool(item.sql):
            continue
        key = fingerprint(item.sql)
        if not key:
            continue
        if table_count(item.sql) < min_tables:
            continue
        counts[key] += item.times_seen
        users[key] = max(users[key], item.distinct_users)
        shape_accounts.setdefault(key, set()).update(accounts_of(item.ran_by))
        # keep the first spelling seen; the shape is what dedupes, but a human
        # reviews real SQL, not a masked fingerprint
        exemplar.setdefault(key, item.sql.strip())
        if item.question.strip():
            exemplar_question.setdefault(key, item.question.strip())

    if rank_by not in ("frequency", "users"):
        raise ValueError('rank_by must be "frequency" or "users"')
    order = users if rank_by == "users" else counts
    primary = {key: (tables_named(sql) or [""])[0] for key, sql in exemplar.items()}
    if counts_out is not None:
        counts_out["shapes"] = len(exemplar)
    if datasets is not None:
        # scope by the primary table's dataset: the log knows nothing about
        # the difference between the warehouse and the part of it the work
        # is about, and a key mined without a scope lands wherever the
        # traffic is
        unqualified = [key for key, table in primary.items() if table and "." not in table]
        if default_dataset:
            for key in unqualified:
                primary[key] = f"{default_dataset.lower()}.{primary[key]}"
        if counts_out is not None and unqualified:
            counts_out["unqualified"] = len(unqualified)
            counts_out["placed"] = len(unqualified) if default_dataset else 0
            counts_out["placed_in"] = default_dataset.lower() if default_dataset else None
        wanted = {d.lower() for d in datasets}
        exemplar = {key: sql for key, sql in exemplar.items()
                    if primary[key].rpartition(".")[0] in wanted}
    if counts_out is not None:
        counts_out["in_scope"] = len(exemplar)
    ranked = [(key, counts[key]) for key, _ in order.most_common()
              if key in exemplar and counts[key] >= min_times_seen]
    if spread:
        ranked = _spread_across_tables(ranked, primary, order)
    if limit is not None:
        ranked = ranked[:limit]

    if before_gate is not None and not before_gate([exemplar[key] for key, _ in ranked]):
        if counts_out is not None:
            counts_out["stopped"] = True
        return []

    candidates: list[Candidate] = []
    one_off_questions = 0
    dropped_literal_counts: list[int] = []
    disabled_filters = 0
    column_list_questions = 0
    out_of_scope_shapes = 0
    question_tally: dict[str, int] = {}
    for index, (key, times_seen) in enumerate(ranked):
        # Said out loud, because this loop runs one model call and one
        # warehouse query per shape and used to print nothing at all. On one
        # estate that was 437 shapes and 25 minutes of a silent terminal, so
        # the only way to tell a slow run from a hung one was to watch the
        # process list.
        if progress is not None:
            progress(index, len(ranked), _funnel(candidates),
                     _last_note(candidates))
        # The ceiling, checked between shapes rather than once before the
        # run. `--budget` used to be compared to an ESTIMATE and then
        # forgotten, so a run whose queries cost more than the dry run
        # predicted had nothing left to stop it: one estate's mine was
        # estimated at $8.89 and had billed $12.41 when a person noticed.
        # Checked here, so the shape that would cross the line is never
        # started and the candidates already found are kept.
        if budget is not None and spent is not None and spent() >= budget:
            if counts_out is not None:
                counts_out["budget_stopped"] = {
                    "done": index, "total": len(ranked), "usd": spent(),
                    "budget": budget}
            break
        sql = exemplar[key]
        question = _drafted_question(sql, question_writer,
                                     exemplar_question.get(key, ""), question_tally,
                                     rules_in_context)
        # A drafted question carrying a wall of quoted values is one analyst's
        # own lookup, and the agent under test answered 16 such questions with
        # no SQL at all. Dropped before it is gated, so the run does not pay
        # to execute a query nobody will keep.
        if (max_question_literals is not None
                and literal_count(question) > max_question_literals):
            one_off_questions += 1
            # How many values it carried, so the summary can say what a looser
            # cap would buy. Without this the cap is a number somebody chose
            # and nobody can price: on one estate moving it from 2 to 3
            # recovered a fifth of the clean candidates, and moving it to 6
            # recovered almost nothing more.
            dropped_literal_counts.append(literal_count(question))
            continue
        finding = gate_one(_short_id(key, index), sql, executor, question=question)
        # the model reads the SQL that runs, not the lines someone switched
        # off; the sheet still shows the original text, comments and all
        disabled = disabled_filter(sql)
        disabled_filters += bool(disabled)
        # The self-check on the English, after drafting and after the retry.
        # It is a note, never a rejection: the gate reads the SQL, a person
        # reads the sentence, and 140 of 372 candidates on one estate died on
        # the sentence with nothing in the file saying so.
        column_list = COLUMN_LIST_NOTE if reads_as_a_column_list(question) else ""
        column_list_questions += bool(column_list)
        detail = "; ".join(part for part in (finding.detail, disabled, column_list) if part)
        # the primary table stays the first one the SQL names. Re-pointing it
        # at the out-of-scope table would drop the shape at the scope filter,
        # which is the silent drop this column exists to prevent
        outside = out_of_scope_tables(sql, datasets, project, default_dataset)
        out_of_scope_shapes += bool(outside)
        candidates.append(
            Candidate(
                id=_short_id(key, index),
                question=question,
                gold_sql=sql,
                times_seen=times_seen,
                gate_status=_status(finding),
                gate_detail=detail,
                table=primary[key],
                accounts=len(shape_accounts.get(key, ())),
                out_of_scope="; ".join(outside),
            )
        )
        # Written now, not at the end. See CandidateWriter for what this
        # cost to learn.
        if on_candidate is not None:
            on_candidate(candidates[-1])
    if progress is not None and ranked:
        progress(len(ranked), len(ranked), _funnel(candidates),
                 _last_note(candidates))
    if counts_out is not None:
        if disabled_filters:
            counts_out["disabled_filters"] = disabled_filters
        if column_list_questions:
            counts_out["column_list_questions"] = column_list_questions
        if out_of_scope_shapes:
            counts_out["out_of_scope"] = out_of_scope_shapes
        counts_out.update(question_tally)
        if dropped_literal_counts:
            counts_out["dropped_literal_counts"] = dropped_literal_counts
            counts_out["max_question_literals"] = max_question_literals
        per_dataset = _accounts_per_dataset(ranked, primary, shape_accounts)
        if per_dataset:
            counts_out["accounts_per_dataset"] = per_dataset
    return candidates


def _drafted_question(sql: str, writer: QuestionWriter | None, from_log: str,
                      tally: dict[str, int],
                      rules_in_context: bool = False) -> str:
    """The question for one candidate, with one retry when the draft drops the
    values its gold filters on.

    The prompt already asks for those values, and an instruction is advice: on
    one field key of 25 golds, 14 questions named none of the literals their
    gold pins, so both arms scored 0 and the number measured the key. The
    check is `gate.missing_literals`; the retry names what is missing and asks
    once. Not a loop: a model that ignored the list once ignores it again, and
    a second call per shape is a second bill.

    The model reads the SQL that runs, not the lines someone switched off, so
    both the draft and the check see the stripped statement. It does see the
    analyst's own comment, passed beside that statement, because the comment is
    the only line in the row that says what the query is FOR: 20 of 59 rows on
    one estate carried one and the drafter had never been shown any of them
    (`analyst_note`).
    """
    text = strip_comments(sql)
    if writer is None:
        return from_log
    question = _written_by(writer, text, analyst_note(sql))
    wanted = _owed_by_the_draft(text, question, rules_in_context)
    if not wanted or not hasattr(writer, "rewrite"):
        return question
    tally["question_retries"] = tally.get("question_retries", 0) + 1
    second = writer.rewrite(text, question, wanted)
    # an empty or emptier answer is not an improvement: a question with no
    # values in it has nothing missing to find, which would read as a fix
    if second.strip() and len(
            _owed_by_the_draft(text, second, rules_in_context)) < len(wanted):
        tally["question_retries_used"] = tally.get("question_retries_used", 0) + 1
        return second
    return question


def _written_by(writer: QuestionWriter, sql: str, comment: str) -> str:
    """`writer.write(sql, comment)` when its signature takes the comment,
    `writer.write(sql)` when it does not.

    Checked rather than assumed, the same way `rewrite` is checked with
    hasattr: `write(sql)` is what every writer in this project had until the
    analyst's comment became an input, and a writer somebody else supplied
    still only has to take the SQL.
    """
    if comment:
        try:
            inspect.signature(writer.write).bind(sql, comment)
        except (TypeError, ValueError):
            comment = ""
    return writer.write(sql, comment) if comment else writer.write(sql)


def _owed_by_the_draft(gold_sql: str, question: str,
                       rules_in_context: bool = False) -> list[str]:
    """What `_what_the_question_owes` finds, plus the one thing that is about
    the sentence rather than about what the sentence leaves out: whether it
    reads as a question at all.

    Composed here instead of inside `_what_the_question_owes` because that
    function's output is published as a `question_owes` column by
    `rewrite-questions` (cli.py), where it names what a gold still owes its own
    SQL. Whether the English is a question is a separate finding, and it is
    reported separately -- in the retry list, in `gate_detail` and in the
    summary count.

    What it costs: the check itself is free, but a draft that only fails this
    one now takes the retry, and `mine` prices ONE model call per shape
    (cli.py passes `model_calls=dry_runs`). So the bill can run above the
    printed estimate by up to one call per flagged shape. That is the same
    retry `MISSING_LITERAL` has always triggered, it is still capped at one,
    and `summarize` prints how many were asked again and what those calls
    were.
    """
    owed = _what_the_question_owes(gold_sql, question, rules_in_context)
    return owed + [COLUMN_LIST_OWED] if reads_as_a_column_list(question) else owed


def _what_the_question_owes(gold_sql: str, question: str,
                            rules_in_context: bool = False) -> list[str]:
    """What a drafted question is missing, as lines the retry prompt can list.

    With `rules_in_context` the value check is skipped; see the comment below.

    Three checks, in the order their size was measured. Values were the first
    one built and the smallest: on 62 benchmark golds a drafted question named
    26% of the columns its gold returns against 76% for a human-written one,
    and named an ordering phrase 2 times in 22 against 11. So the retry asks
    for all three or it fixes the least of them.
    """
    # `rules_in_context` is for a run measuring whether written context helps.
    # There the business rule lives in the context and must NOT be in the
    # question: a question that says "excluding Cancelled and Returned" hands
    # the bare arm the rule for free, and no written context can then show a
    # lift. Measured on 25 mined golds: 10 of them, 40%, named a rule this way
    # and were unusable for a paired comparison.
    #
    # Only the values are dropped. What the answer shows and which rows come
    # first are not business rules, and a question that leaves those out is
    # unanswerable by anyone.
    owed = [] if rules_in_context else [
        f"the value {value!r}, spelled exactly like that"
        for value in missing_literals(gold_sql, question)]
    owed += [f"the value {value!r} belongs to {column}, so say which id or code it is"
             for column, value in literal_on_wrong_column(gold_sql, question)]
    unnamed = missing_projection(gold_sql, question)
    if unnamed:
        shown = ", ".join(unnamed)
        owed.append(f"what the answer shows, in business words: it returns {shown}")
    if orders_rows(gold_sql) and not question_names_order(question):
        owed.append("which rows come first, because the query orders them")
    return owed


def _accounts_per_dataset(ranked: list[tuple[str, int]], primary: dict[str, str],
                          shape_accounts: dict[str, set[str]]) -> dict[str, int]:
    """How many accounts ran the kept shapes of each dataset.

    Only datasets with at least one known account: a log without `ran_by`
    says nothing about who, and nothing is the same as one person.
    """
    people: dict[str, set[str]] = {}
    for key, _ in ranked:
        dataset = primary.get(key, "").rpartition(".")[0]
        if dataset:
            people.setdefault(dataset, set()).update(shape_accounts.get(key, ()))
    return {dataset: len(accounts) for dataset, accounts in people.items() if accounts}


def _spread_across_tables(ranked: list[tuple[str, int]], primary: dict[str, str],
                          order: Counter) -> list[tuple[str, int]]:
    """Round-robin over tables: each table's best shape, then each table's
    second, tables in order of their own traffic.

    Straight top-N lets the heaviest table eat the sample. On one estate the
    busiest data product ran 10900 queries a month against 2400 for the 27
    products in scope, and top-N made 54% of the key one product. A per-table
    cap would need a number and a flag someone remembers to set; a
    round-robin needs neither and stays deterministic.
    """
    by_table: dict[str, list[tuple[str, int]]] = {}
    for key, times in ranked:                    # ranked order = within-table order
        by_table.setdefault(primary[key], []).append((key, times))
    tables = sorted(by_table, key=lambda t: -sum(order[k] for k, _ in by_table[t]))
    out: list[tuple[str, int]] = []
    depth = 0
    while len(out) < len(ranked):
        for table in tables:
            if depth < len(by_table[table]):
                out.append(by_table[table][depth])
        depth += 1
    return out


def _status(finding: GateFinding) -> str:
    """What the review sheet shows for one candidate.

    A warning is written beside "ok" rather than instead of it, so a reviewer
    sees both that the row is usable and what to look at."""
    if finding.ok:
        return "ok" if not finding.warnings else "ok (" + "+".join(finding.warnings) + ")"
    return "+".join(finding.classes)


# The review file's columns, in order. Deliberately the ones `gate` and
# `score` already read, so an approved file needs no reshaping: delete the
# rows you reject, fill in the questions, and it is a gold set.
CANDIDATE_COLUMNS = ["id", "question", "gold_sql", "times_seen", "gate_status",
                     "gate_detail", "table", "accounts", "out_of_scope"]


def candidate_row(candidate: Candidate) -> list:
    """One CSV row. One function, because the file is written two ways --
    all at once when a run finishes, and row by row while it is still
    going -- and two column lists that agree today will not agree after the
    next column is added to one of them."""
    return [getattr(candidate, name) for name in CANDIDATE_COLUMNS]


class CandidateWriter:
    """The review file, written as the run goes rather than at the end.

    Why this exists, and it was paid for: `mine` held every candidate in
    memory and wrote the file in its last statement. One run on a customer
    estate spent $12.41 over 36 minutes and had written nothing, so killing
    it -- or a crash on the last shape -- threw all of it away. A paid stage
    that produces no file until it succeeds cannot be interrupted, which
    means its own budget cannot stop it either.

    Flushed after every row, because the point is that the file is readable
    from another terminal while the run is still going.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._handle = None
        self.rows = 0

    def __enter__(self) -> "CandidateWriter":
        self._handle = self.path.open("w", newline="")
        self._writer = csv.writer(self._handle)
        self._writer.writerow(CANDIDATE_COLUMNS)
        self._handle.flush()
        return self

    def write(self, candidate: Candidate) -> None:
        self._writer.writerow(candidate_row(candidate))
        self._handle.flush()
        self.rows += 1

    def __exit__(self, *exc) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def write_candidates(candidates: list[Candidate], path: str | Path) -> Path:
    """Write the review file all at once.

    Kept for the callers that already hold every candidate. A run that is
    still going uses `CandidateWriter`, and both go through
    `candidate_row`, so the two files have the same columns by construction.
    """
    with CandidateWriter(path) as writer:
        for candidate in candidates:
            writer.write(candidate)
    return Path(path)


def summarize(candidates: list[Candidate], datasets: list[str] | None = None,
              counts: dict | None = None) -> str:
    """A short report for the person about to review the file.

    The table concentration prints here, before anyone writes a question,
    because the key that made a router look broken was 40% one table and
    nothing said so until routing-gate, after three rounds were paid for.
    """
    unqualified = _unqualified_line(counts)
    if not candidates:
        # the scope, not the log, is the usual reason once a scope is given: a
        # log full of read-only queries produced "no read-only queries" on one
        # estate because the scope matched none of them
        shapes = (counts or {}).get("shapes")
        if datasets and shapes:
            return (f"no candidates: {shapes} read-only query shapes in the log, 0 with a "
                    f"primary table in datasets {', '.join(datasets)}. Check the dataset "
                    "names, or whether this log came from the project whose traffic you meant"
                    + (f"\n{unqualified}" if unqualified else ""))
        return "no candidates: the log had no read-only queries"
    usable = sum(1 for c in candidates if c.usable)
    lines = []
    # First, because it changes how every number below it reads. A run cut
    # off by its budget sampled the top of the ranking and nothing else, so
    # "22 clean" is 22 clean out of the shapes it reached, not out of the
    # log.
    stop = (counts or {}).get("budget_stopped")
    if stop:
        lines += [
            f"STOPPED ON BUDGET after {stop['done']} of {stop['total']} shapes: "
            f"${stop['usd']:,.2f} billed against --budget ${stop['budget']:,.2f}",
            "  every count below is over the shapes it reached, not over the log. "
            "The shapes are ranked, so what it reached is the top of the ranking.",
            "  to go further: raise --budget, or lower --max-bytes so each shape "
            "costs less (the summary's over-budget ladder prices that), or narrow "
            "the log with `log --min-tables` so fewer shapes are worth running",
            "",
        ]
    lines += [
        f"{len(candidates)} candidate shapes, {usable} clean, "
        f"{len(candidates) - usable} with defects",
    ]
    by_table: Counter[str] = Counter(c.table for c in candidates if c.table)
    if by_table:
        largest = ", ".join(f"{t} {n} ({n / len(candidates):.0%})" for t, n in by_table.most_common(3))
        seen_datasets = {t.rpartition(".")[0] for t in by_table if "." in t}
        lines.append(f"  tables: {len(by_table)}"
                     + (f" across {len(seen_datasets)} datasets" if seen_datasets else "")
                     + f"; largest: {largest}; an even sample would put "
                     f"{1 / len(by_table):.0%} on each")
    lines.append("  scope: datasets " + ", ".join(datasets) if datasets
                 else "  scope: the whole log; --datasets A,B narrows it to the part the work is about")
    if unqualified:
        lines.append(unqualified)
    dropped_migration = (counts or {}).get("migration_checks") or 0
    if dropped_migration:
        lines.append(
            f"  {dropped_migration} shapes dropped as migration checks: they "
            "compare one copy of the data to another, so no written rule can "
            "change their answer. --keep-migration-checks keeps them")
    lines += _one_account_lines(counts)
    outside = (counts or {}).get("out_of_scope") or 0
    if outside:
        lines.append(f"  {outside} of {len(candidates)} shapes name a table the run cannot "
                     "reach; the out_of_scope column names it. The table column holds the "
                     "in-scope table, so the row reads as answerable while the measures it "
                     "asks for sit elsewhere; nothing was dropped, a reviewer decides")
    disabled = (counts or {}).get("disabled_filters") or 0
    if disabled:
        lines.append(f"  {disabled} shapes carry a commented-out filter; the question was "
                     "drafted from the SQL without it, and gate_detail says which rows")
    column_lists = (counts or {}).get("column_list_questions") or 0
    if column_lists:
        lines.append(f"  {column_lists} of {len(candidates)} questions read as a list of the "
                     "selected columns rather than as a question; gate_detail says which "
                     "rows. A reviewer rejects those as does_not_match, and that was 140 of "
                     "372 candidates (38%) on one estate, every one with SQL that ran and "
                     "passed the gate. Rewrite the sentence in the review sheet; the SQL is "
                     "already gated, so nothing has to run again")
    lines += _missing_value_lines(candidates, counts)
    if (counts or {}).get("dropped_literal_counts"):
        lines += literal_ladder(counts["dropped_literal_counts"],
                                counts.get("max_question_literals") or 0)
    lines += ["", "the gate rejected:"]
    rejected: Counter[str] = Counter()
    for candidate in candidates:
        if not candidate.usable:
            for defect in candidate.gate_status.split("+"):
                rejected[defect] += 1
    if rejected:
        for defect, count in rejected.most_common():
            fix = RECOVERY.get(defect)
            lines.append(f"  {defect:<20} {count}" + (f"   {fix}" if fix else ""))
        lines += _recoverable_lines(rejected)
        if rejected.get(OVER_BUDGET):
            lines += budget_ladder([c.gate_detail for c in candidates])
    else:
        lines.append("  nothing")
    lines.append("")
    lines.append("these are CANDIDATES. A person approves them before they are golds.")
    return "\n".join(lines)


# What recovers each rejected class, printed beside its count. A class name
# and a number tell a reviewer nothing: on one estate the summary read
# "EXEC_FAIL 179" and the true story was "112 of these are one --max-bytes
# raise away, 34 need a grant, 24 name tables that were dropped, 9 are bad
# SQL". A day went into re-reading gate_detail by hand to learn that.
RECOVERY = {
    "OVER_BUDGET": "raise --max-bytes; the SQL is fine",
    "CROSS_PROJECT": "name them in --data-projects if the agent reads them",
    "NO_ACCESS": "ask whoever owns the policy; not the query's author",
    "TABLE_GONE": "the table was dropped; nothing recovers these",
    "WHOLE_ROW": "SELECT *: no single right answer, so not recoverable",
    "ZERO_ROWS": "runs but returns nothing; any wrong SQL also scores right",
    "EXEC_FAIL": "the SQL itself is wrong; read gate_detail",
    "TIMEOUT": "raise --timeout, or the query is too slow to be a gold",
    "DEGENERATE": "one trivial cell; not an answer",
    "LIMIT_NO_ORDER": "add an ORDER BY that decides the rows",
    "TRUNCATED": "raise --row-cap",
}

# The classes a person can recover by changing a flag or asking for a grant,
# without touching one line of SQL. Counted together because that is the
# number that decides whether to mine more or to re-run what is already here.
RECOVERABLE_BY_A_FLAG = ("OVER_BUDGET", "CROSS_PROJECT", "TIMEOUT", "TRUNCATED")
RECOVERABLE_BY_A_GRANT = ("NO_ACCESS",)


# The dry-run figure the executor puts in its refusal, so the ladder below
# reads the bytes each rejected query wanted without a second dry run.
_DRY_RUN_GB = re.compile(
    r"scan more than the ([\d.]+) GB ceiling \((?:dry-run|killed): ([\d.]+) GB\)")


def literal_ladder(dropped: list[int], cap: int) -> list[str]:
    """What a looser --max-question-literals would buy, measured.

    A cap is a number somebody chose, and until it is priced nobody can
    argue with it. Every drafted question this run threw away carried a
    known number of quoted values, so the answer is free: it is a count.

    On one warehouse the cap was 2 and the drafted questions carried 0, 1,
    2, 3, 4, 6 and a long tail out to 35 values. Raising it to 3 recovered a
    fifth of the clean candidates; raising it to 6 recovered almost nothing
    more and started letting in the questions the cap exists for, the ones
    naming six product codes and an order number.

    Only rungs that recover something are printed, and never a rung above
    the largest question, so a clean estate reads nothing.
    """
    if not dropped:
        return []
    ordered = sorted(dropped)
    lines = [f"  what a looser --max-question-literals would buy (it is {cap}):"]
    shown = 0
    for rung in (cap + 1, cap + 2, cap + 4, cap + 8):
        recovered = sum(1 for n in ordered if n <= rung)
        if recovered <= shown or rung > ordered[-1]:
            continue
        shown = recovered
        lines.append(f"    --max-question-literals {rung:<3d} {recovered} of "
                     f"{len(ordered)} dropped questions come back")
    if len(lines) == 1:
        return []
    # Only when the largest really is the kind of question the cap exists
    # for. "the largest carried 3 quoted values, which is the kind of
    # question the cap is for" is a sentence that argues against its own cap.
    if ordered[-1] >= max(6, 3 * cap):
        lines.append(f"    the largest carried {ordered[-1]} quoted values, which is the "
                     "kind of question the cap is for: one analyst's own lookup, not a "
                     "question anyone else asks")
    return lines


def _last_note(candidates: list) -> str:
    """The shape just handled, for a person watching a terminal.

    Only useful live, and deliberately not written to a log: it is derived
    from the customer's own SQL, and a log file gets pasted into tickets.
    What it buys is the difference between "the run is alive" and "the run
    is doing sensible work", which no count can tell you.
    """
    if not candidates:
        return ""
    last = candidates[-1]
    question = (getattr(last, "question", "") or getattr(last, "gold_sql", ""))
    question = " ".join(str(question).split())[:72]
    return f"{question}   [{getattr(last, 'gate_status', '')}]"


def _funnel(candidates: list[Candidate]) -> dict[str, int]:
    """What has fallen out so far, by the name of the thing that fixes it.

    This is the part worth watching. "100 of 437" says the run is alive;
    "clean 61, over-budget 14, no-access 8" says whether it is worth
    finishing, and it is a live answer to "is my query log any good". The
    names are the recovery, not the defect, because that is what a person
    reading it can act on.
    """
    counted = {"clean": 0}
    for candidate in candidates:
        if candidate.usable:
            counted["clean"] += 1
            continue
        found = set(candidate.gate_status.split("+"))
        if OVER_BUDGET in found:
            name = "over-budget"
        elif NO_ACCESS in found:
            name = "no-access"
        elif TABLE_GONE in found:
            name = "gone"
        elif found & {WHOLE_ROW, LIMIT_NO_ORDER}:
            name = "not-a-question"
        elif found & {ZERO_ROWS, DEGENERATE}:
            name = "no-rows"
        else:
            name = "bad-sql"
        counted[name] = counted.get(name, 0) + 1
    return counted


# How often the miner says where it is. Every 25 shapes is about a line a
# minute on a warehouse behind a proxy, which is often enough to see it
# moving and rare enough not to bury the summary.
PROGRESS_EVERY = 25


def budget_ladder(details: list[str]) -> list[str]:
    """What raising --max-bytes would buy, measured, with the price beside it.

    "raise --max-bytes" is advice nobody can act on: raise it to what, and at
    what cost? The executor already wrote the bytes each refused query wanted
    into its own refusal, so the answer is free to compute and needs no
    second dry run.

    Three rungs, at the median, the 80th and the 95th percentile of the
    refused figures, so the ladder fits the estate instead of guessing. On
    one warehouse the ceiling was 1 GB and the figures ran from 1.39 GB to
    29,545 GB: 50 GB brought back 81 of 112 for about $5, and no useful
    ceiling ever included the 29 TB one. That last sentence is printed,
    because a person reading "111 of 112 at 2 TB" would raise the ceiling to
    2 TB and pay $128 for one extra gold.
    """
    figures = sorted(float(m.group(2)) for d in details
                     for m in [_DRY_RUN_GB.search(d or "")] if m)
    if not figures:
        return []
    ceilings = {d and float(m.group(1)) for d in details
                for m in [_DRY_RUN_GB.search(d or "")] if m}
    was = min(ceilings) if ceilings else None
    lines = [f"  what raising --max-bytes would buy"
             + (f" (it was {was:g} GB)" if was else "") + ":"]
    seen: set[int] = set()
    for share in (0.50, 0.80, 0.95):
        rung = _readable(figures[min(int(len(figures) * share), len(figures) - 1)])
        if rung in seen:
            continue
        seen.add(rung)
        under = [gb for gb in figures if gb <= rung]
        tib = sum(under) / 1024
        lines.append(f"    --max-bytes {rung:<6g} GB   {len(under)} of {len(figures)} come back, "
                     f"{tib:.2f} TiB to run once, ~${tib * BIGQUERY_USD_PER_TIB:.2f} "
                     f"(BigQuery ${BIGQUERY_USD_PER_TIB}/TiB as of {BIGQUERY_PRICE_AS_OF})")
    # The note fires on the figures, not on where a rung landed. With few
    # refused queries the 95th percentile IS the outlier, so a rung-based
    # test went quiet exactly when the warning mattered most.
    biggest = figures[-1]
    if len(figures) > 1 and biggest > 4 * figures[-2]:
        lines.append(f"    one shape wants {biggest:,.0f} GB on its own, {biggest / figures[-2]:.0f}x "
                     f"the next largest ({figures[-2]:,.0f} GB): a ceiling that includes it is "
                     f"paid for one gold")
    return lines


def _readable(gb: float) -> int:
    """The next round number at or above `gb`, so a printed ceiling is one a
    person would type: 8.67 reads 10, 41.2 reads 50, 640 reads 1000."""
    for rung in (1, 2, 5, 10, 25, 50, 100, 200, 500, 1000, 2000, 5000, 10000):
        if gb <= rung:
            return rung
    return int(gb) + 1


def _recoverable_lines(rejected: "Counter[str]") -> list[str]:
    """How many rejected candidates come back without rewriting any SQL.

    Two sentences, each naming the flag or the ask. Silent when there is
    nothing to recover, so a clean estate does not read advice it cannot use.
    """
    lines: list[str] = []
    flag = {c: rejected[c] for c in RECOVERABLE_BY_A_FLAG if rejected.get(c)}
    grant = {c: rejected[c] for c in RECOVERABLE_BY_A_GRANT if rejected.get(c)}
    if flag:
        named = ", ".join(f"{n} {c}" for c, n in sorted(flag.items(), key=lambda kv: -kv[1]))
        lines.append(f"  {sum(flag.values())} of these need no SQL change at all, only a "
                     f"flag: {named}. Re-run mine with it before mining more shapes")
    if grant:
        lines.append(f"  {sum(grant.values())} were refused by the platform, not by the SQL "
                     "(NO_ACCESS): a grant recovers them and nothing else will")
    return lines


def _missing_value_lines(candidates: list[Candidate], counts: dict | None) -> list[str]:
    """How many questions leave out a value their gold filters on, and what the
    retry did about it.

    This is the line that would have stopped a whole field round: 14 of 25
    golds pinned literals their question never gave, so no agent could answer
    them and the run read 0.000 with context and 0.000 without. Nothing is
    dropped here; the question is what a reviewer fixes.
    """
    # the status reads `ok (MISSING_LITERAL)` now that the class is a warning,
    # so a split on "+" alone stopped counting it and the line vanished
    flagged = sum(1 for c in candidates if MISSING_LITERAL in c.gate_status)
    retries = (counts or {}).get("question_retries") or 0
    if not flagged and not retries:
        return []
    lines = []
    if flagged:
        lines.append(f"  {flagged} of {len(candidates)} questions leave out a value their gold "
                     f"filters on ({MISSING_LITERAL}); gate_detail names the values. No agent "
                     "can guess them, so those rows score every arm wrong at the same rate")
    if retries:
        used = (counts or {}).get("question_retries_used") or 0
        lines.append(f"  {retries} questions were drafted a second time with what they owed "
                     f"listed (missing values, or English that asked nothing), {used} came back "
                     f"better; that is {retries} model calls on top of the one per shape the "
                     "estimate above prices")
    return lines


def _one_account_lines(counts: dict | None) -> list[str]:
    """The datasets whose kept shapes all come from one account.

    On one estate the candidates per dataset were 4,4,4,3,2,2,2,2,1,1 with
    2,4,3,2,1,1,1,1,1,1 people behind them: half the datasets rested on one
    analyst's habit. A gold nobody else asks is a gold about one person, not
    about the business. A warning, not a refusal; the reviewer decides.
    """
    per_dataset = (counts or {}).get("accounts_per_dataset") or {}
    alone = sorted(dataset for dataset, people in per_dataset.items() if people == 1)
    if not alone:
        return []
    named = ", ".join(alone[:6]) + (f", and {len(alone) - 6} more" if len(alone) > 6 else "")
    return [f"  {len(alone)} of {len(per_dataset)} datasets have candidates from one account "
            f"only: {named}. A gold nobody else asks is a gold about one person's habit; "
            "the accounts column says how many ran each shape"]


def _unqualified_line(counts: dict | None) -> str:
    """What happened to the shapes whose primary table carried no dataset,
    under a scope: placed in the --db dataset, or dropped with the flag that
    would place them. On one estate a third of the log was written against
    a default dataset and vanished from a scoped mine without a word."""
    n = (counts or {}).get("unqualified") or 0
    if not n:
        return ""
    placed_in = counts.get("placed_in")
    if placed_in:
        return f"  {n} shapes name a table without a dataset and were placed in {placed_in} (the --db dataset)"
    return (f"  {n} shapes name a table without a dataset and were dropped: nothing says which "
            "dataset they mean; --db bq://BILLING/DATASET places them there")
