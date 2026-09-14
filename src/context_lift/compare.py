# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Execution / denotation comparison between two SQL result sets.

This is the Spider/BIRD "execution accuracy" metric: rather than comparing
SQL text, run both the predicted and the gold query and compare the rows
they return. Two queries are equivalent if they *denote* the same answer,
however differently they are written.

Algorithm adapted from the reference implementation in
`taoyds/test-suite-sql-eval` (Zhong, Yu, Klein, "Semantic Evaluation for
Text-to-SQL with Distilled Test Suites", EMNLP 2020,
https://aclanthology.org/2020.emnlp-main.29/), the metric used by Spider,
SParC, and CoSQL.

Four design decisions, each tracking a fact about SQL semantics:
- FULL result-set compare, not a first-N-rows sample. Sampling produces
  false MISMATCH when both queries return many rows in different orders.
- "Unorder within row" gives column-order independence cheaply: sort each
  row's values before tupling, instead of enumerating column permutations.
- ORDER BY in the gold SQL switches the compare from multiset (bag) to
  positional. This matches SQL semantics: an unordered query is a set, an
  ordered query is a sequence.
- Float values are rounded to 4 decimals — aggregates like AVG/STDDEV
  produce float jitter across engines and across runs of the same engine.
- NULL is a sentinel string, not Python None, so rows sort and hash cleanly.

What this deliberately does NOT do:
- Type coercion: 1 and "1" compare as different (Spider matches this).
- Column-name matching: names are ignored; only values are compared.
- Test-suite accuracy (perturbed DB copies, EMNLP 2020). True test suites
  need many DB copies and are out of scope here.

  WITHDRAWN 2026-09-12: this bullet used to say single-database denotation
  match "has a ~2.5% average / ~8.1% worst-case false-negative rate". Wrong
  metric and wrong direction. 2.6% / 8.1% is Table 1 of that paper, the false
  negative rate of adapted EXACT SET MATCH on SQL text. For single-database
  denotation the paper's Table 2 reports a false POSITIVE rate, 6.5% mean and
  9.0% max over 21 Spider dev submissions, and reports no false-negative rate
  at all. So this comparator's only published error rate says it scores too
  high, and the optional LLM judge cannot fix that because it only ever raises
  a score. docs/METHODOLOGY.md section 4 carries the quotes.
"""
from __future__ import annotations

import itertools
import math
import re
from collections import Counter
from typing import Iterable

# Sentinels so NULL / NaN can sort and hash alongside normalized strings.
# Real cell strings will not collide with the \x00 prefix.
_NULL_SENTINEL = "\x00NULL"
_NAN_SENTINEL = "\x00NaN"


def normalize_value(v: int | float | str | bool | None) -> str:
    """Canonicalize one cell so equal-meaning values compare equal.

    Why a single canonical *string* form: Counter() needs hashable keys and
    one representation per value, so 5, 5.0, and "5" must collapse together
    while staying distinct from "five". Floats round to 4 decimals (Spider's
    tolerance for aggregate jitter); strings are stripped and lowercased.
    """
    if v is None:
        return _NULL_SENTINEL
    # bool is an int subclass in Python, so check it before the float branch
    # or True would normalize to "1".
    if isinstance(v, bool):
        return "true" if v else "false"
    try:
        f = float(v)
        if math.isnan(f):
            return _NAN_SENTINEL
        # int(inf) raises OverflowError; seen in the wild when a gold divides
        # by a column that is zero-heavy and SQLite returns Infinity. The two
        # infinities normalize to distinct strings so +inf never matches -inf.
        if math.isinf(f):
            return "inf" if f > 0 else "-inf"
        rounded = round(f, 4)
        # Collapse whole floats to their int form so 5.0 and 5 match.
        if rounded == int(rounded):
            return str(int(rounded))
        return f"{rounded}"
    except (ValueError, TypeError):
        pass
    return str(v).strip().lower()


def row_to_tuple(row: dict, unorder_columns: bool = True) -> tuple:
    """Convert a row dict to a canonical, hashable tuple of cell strings.

    With unorder_columns=True the values are sorted before tupling — the
    column-order independence trick. Two rows holding the same data in a
    different column arrangement collapse to the same tuple, at O(c log c)
    instead of enumerating O(c!) permutations.
    """
    vals = [normalize_value(v) for v in row.values()]
    if unorder_columns:
        vals = sorted(vals)
    return tuple(vals)


# Conservative ORDER BY detection: a case-insensitive text match that also
# fires on ORDER BY inside CTEs / subqueries. Intentional — any ORDER BY in
# the query tree means order mattered somewhere, so compare positionally.
_ORDER_BY_RE = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)


def has_order_by(sql: str) -> bool:
    """True if the SQL contains an ORDER BY clause anywhere in its text."""
    return bool(_ORDER_BY_RE.search(sql or ""))


def row_to_positional_tuple(row: dict) -> tuple:
    """Row as a tuple in column order, normalized but NOT sorted.

    The order-preserving counterpart to row_to_tuple. Column order comes from
    dict insertion order, which is the SELECT order for every executor here.
    """
    return tuple(normalize_value(v) for v in row.values())


def _quick_reject(expected: list[tuple], actual: list[tuple]) -> bool:
    """True if the two row sets cannot match under ANY column permutation.

    Cheap necessary condition: if the bag of sorted-within-row tuples differs,
    no permutation can fix it. Passing this does NOT mean they match — that is
    exactly the mistake this module used to make.
    """
    return Counter(tuple(sorted(r)) for r in expected) != Counter(
        tuple(sorted(r)) for r in actual
    )


def _candidate_permutations(
    expected: list[tuple], actual: list[tuple], num_cols: int
) -> Iterable[tuple[int, ...]]:
    """Column permutations worth testing, pruned by per-column value sets.

    Position i of a yielded permutation holds the actual column that stands in
    for expected column i, so a caller reads it as
    `permuted[i] = actual_row[perm[i]]`. That direction is the contract, and
    `_projections` below builds the same shape for a wider prediction.

    An expected column can only be matched by an actual column holding the
    same multiset of values. Taking the product of each column's candidates
    and keeping the ones that are true permutations cuts the search far below
    c! for real result sets, where columns rarely share value sets.

    Building it the other way round, indexed by actual column, yields the
    inverse permutation. The two agree whenever every cycle has length 2, so
    two columns swapped compared correctly and three columns rotated did not.
    """
    expected_columns = [Counter(row[i] for row in expected) for i in range(num_cols)]
    actual_columns = [Counter(row[j] for row in actual) for j in range(num_cols)]
    per_column_options = []
    for i in range(num_cols):
        options = [j for j in range(num_cols) if actual_columns[j] == expected_columns[i]]
        if not options:
            return []
        per_column_options.append(options)

    for perm in itertools.product(*per_column_options):
        if len(set(perm)) == num_cols:
            yield perm


def _projections(
    expected: list[tuple], actual: list[tuple], num_gold: int, num_pred: int
) -> Iterable[tuple[int, ...]]:
    """Injective maps from gold columns into a wider prediction, pruned by
    per-column value multisets like _candidate_permutations."""
    expected_columns = [Counter(row[i] for row in expected) for i in range(num_gold)]
    actual_columns = [Counter(row[j] for row in actual) for j in range(num_pred)]
    options = []
    for i in range(num_gold):
        candidates = [j for j in range(num_pred) if actual_columns[j] == expected_columns[i]]
        if not candidates:
            return []
        options.append(candidates)
    for choice in itertools.product(*options):
        if len(set(choice)) == num_gold:
            yield choice


def compare(
    expected_rows: list[dict],
    agent_rows: list[dict],
    gold_sql: str,
    column_order_matters: bool = False,
    extra_columns: str = "reject",
) -> str:
    """Spider-style result-set comparison.

    Args:
        expected_rows: full result set from the gold SQL (list of dicts).
        agent_rows: full result set from the agent SQL.
        gold_sql: the gold SQL text — only used to detect ORDER BY.
        column_order_matters: keep columns in SELECT order instead of searching
            for a permutation. Off by default so rates stay comparable with
            Spider/BIRD, which treat column order as arbitrary. Turn it ON for
            business reporting, where the columns are the answer: with it off,
            `SELECT MIN(p), MAX(p)` and `SELECT MAX(p), MIN(p)` both match, and
            so does a query that swaps revenue and cost.

    Returns one of:
        MATCH       — equivalent after canonicalization.
        MISMATCH    — different row sets.
        EMPTY_BOTH  — both 0 rows.
        EMPTY_ONE   — only one side empty.
    """
    if not expected_rows and not agent_rows:
        return "EMPTY_BOTH"
    if not expected_rows or not agent_rows:
        return "EMPTY_ONE"

    # Row and column counts are necessary conditions. Skipping the column check
    # (as this file once did) lets a query returning extra or missing columns
    # slip through the permutation search.
    if len(expected_rows) != len(agent_rows):
        return "MISMATCH"

    expected = [row_to_positional_tuple(r) for r in expected_rows]
    actual = [row_to_positional_tuple(r) for r in agent_rows]
    num_cols = len(expected[0])
    num_pred = len(actual[0])
    if any(len(r) != num_cols for r in expected) or any(len(r) != num_pred for r in actual):
        return "MISMATCH"
    if num_pred > num_cols and extra_columns == "tolerate":
        # A wider prediction: every gold column must be found in it, with the
        # same rows (duplicates and counts included), the way Spider 2.0 and
        # Defog's sql-eval score it. Row-count equality above still holds, so
        # `SELECT DISTINCT a` vs `SELECT a, id` does not slip through. A
        # distinct verdict, so a report can always print the strict number
        # beside the tolerant one.
        order_matters = has_order_by(gold_sql)
        for choice in _projections(expected, actual, num_cols, num_pred):
            if column_order_matters and list(choice) != sorted(choice):
                continue
            projected = [tuple(row[j] for j in choice) for row in actual]
            if (expected == projected) if order_matters else (Counter(expected) == Counter(projected)):
                return "MATCH_EXTRA_COLUMNS"
        return "MISMATCH"
    if num_pred != num_cols:
        return "MISMATCH"

    if _quick_reject(expected, actual):
        return "MISMATCH"

    # A gold with ORDER BY asked for a sequence; without one, a SQL result is
    # a set, so compare bags. Unchanged from before the permutation fix.
    order_matters = has_order_by(gold_sql)

    def equal_under(perm: tuple[int, ...]) -> bool:
        permuted = [tuple(row[i] for i in perm) for row in actual]
        if order_matters:
            return expected == permuted
        return Counter(expected) == Counter(permuted)

    identity = tuple(range(num_cols))
    if equal_under(identity):
        return "MATCH"
    if column_order_matters:
        return "MISMATCH"
    for perm in _candidate_permutations(expected, actual, num_cols):
        if perm != identity and equal_under(perm):
            return "MATCH"
    return "MISMATCH"


def is_subset(small: Iterable[dict], big: Iterable[dict]) -> bool:
    """True if every row in `small` appears in `big` under multiset semantics.

    Uses the same normalization as compare(). A diagnosis helper (e.g. "did
    the prediction return a subset of the gold rows?"), not part of the verdict.
    """
    sc = Counter(row_to_tuple(r) for r in small)
    bc = Counter(row_to_tuple(r) for r in big)
    for k, v in sc.items():
        if bc[k] < v:
            return False
    return True
