# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Conventions read out of the analysts' SQL, and the ones it refuses to read.

The problem, measured on thelook 2026-09-10: a question in English does not
say what SHAPE its answer should be, and 13 of 28 failures were the model
returning a rate together with the numerator and denominator it came from.
Nothing in a schema says which shape a business reports.

The analysts' own SQL does. Measured on a real query log, 2026-09-13: of 92
queries whose SELECT list computes a ratio, 87 (94.6%) return the ratio
ALONE. The model was doing the 5.4% thing.

What these tests mostly pin is the REFUSALS. A shape analysts follow half the
time is not a convention, and writing it as a rule puts a coin flip into the
context -- wrong context measures worse than none (arXiv 2408.07702). Two
gates, kept separate because they mean different things: too few queries is
"not enough evidence", a 52/48 split is "evidence of disagreement".
"""
from __future__ import annotations

import csv
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from mine_conventions import (  # noqa: E402
    CONVENTIONS, exposes_its_parts, measure, select_body, select_items,
)


# --- reading a select list -------------------------------------------------

def test_it_splits_a_select_list_at_top_level_commas_only():
    body = select_body("SELECT a, f(b, c), d FROM t")
    assert select_items(body) == ["a", "f(b, c)", "d"]


def test_a_query_it_cannot_read_returns_nothing_rather_than_guessing():
    assert select_body("UPDATE t SET a = 1") == ""


# --- the check aimed at the recorded failure -------------------------------

def test_a_ratio_returned_with_its_numerator_is_flagged():
    """The failure shape itself: three columns where the business reports one."""
    assert exposes_its_parts(
        "SELECT SUM(a)/SUM(b) AS rate, SUM(a) AS num FROM t") is True


def test_a_ratio_returned_with_its_denominator_is_flagged():
    assert exposes_its_parts(
        "SELECT SUM(a)/SUM(b) AS rate, SUM(b) FROM t") is True


def test_a_ratio_alone_is_not_flagged():
    assert exposes_its_parts(
        "SELECT SUM(a)/SUM(b) AS rate, region FROM t") is False


def test_safe_divide_is_read_the_same_way_as_a_slash():
    """Two spellings of one thing. Handling only the slash would report the
    convention as weaker than it is, on an estate where 48 of 92 ratio
    queries use SAFE_DIVIDE or NULLIF."""
    assert exposes_its_parts(
        "SELECT SAFE_DIVIDE(SUM(a), SUM(b)) AS rate, SUM(a) FROM t") is True
    assert exposes_its_parts(
        "SELECT SAFE_DIVIDE(SUM(a), SUM(b)) AS rate, region FROM t") is False


def test_an_alias_does_not_hide_a_repeated_expression():
    """`SUM(a) AS numerator` and `SUM(a)` are the same expression. Comparing
    with the alias attached would miss it."""
    assert exposes_its_parts(
        "SELECT SUM(a)/SUM(b) AS rate, SUM(a) AS numerator FROM t") is True


def test_a_query_with_no_ratio_is_not_in_the_pool():
    """None, not False. A query that computes no ratio is not evidence about
    how rates are reported, and counting it as agreement would inflate every
    share."""
    assert exposes_its_parts("SELECT a, b FROM t") is None


# --- the two gates, which are the point ------------------------------------

def test_a_split_pool_is_refused_and_says_why():
    """The discipline. 52/48 is evidence of DISAGREEMENT, not a rule."""
    half = (["SELECT SAFE_DIVIDE(a, b) AS r, x FROM t"] * 25
            + ["SELECT a/b AS r, x FROM t"] * 25)
    findings = {f.key: f for f in measure(half, 20, 0.70)}
    guard = findings["divide guard"]
    assert guard.proposed == ""
    assert "no agreement" in guard.refused
    assert "coin flip" in guard.refused


def test_too_few_queries_is_a_different_refusal_from_a_split():
    """Four queries agreeing unanimously is not evidence. Conflating the two
    would let a tiny pool speak with the authority of a large one."""
    tiny = ["SELECT SAFE_DIVIDE(a, b) AS r, x FROM t"] * 4
    findings = {f.key: f for f in measure(tiny, 20, 0.70)}
    guard = findings["divide guard"]
    assert guard.proposed == ""
    assert "not enough evidence" in guard.refused
    assert "no agreement" not in guard.refused


def test_a_one_sided_pool_proposes_the_rule_in_the_direction_followed():
    """A convention is often the NEGATIVE: analysts overwhelmingly do NOT
    round a rate, so "do not round" is the rule."""
    unrounded = ["SELECT a/b AS r, x FROM t"] * 30
    findings = {f.key: f for f in measure(unrounded, 20, 0.70)}
    rounding = findings["rounding"]
    assert rounding.proposed.startswith("Do not round")
    assert "30 of 30" in rounding.detail
    assert "follow this" in rounding.detail


def test_no_proposal_ever_ships_without_its_count():
    """Every shipped line carries the number that produced it. A rule with no
    count is a rule nobody can argue with."""
    pool = ["SELECT a/b AS r, x FROM t"] * 30
    for finding in measure(pool, 20, 0.70):
        if finding.proposed:
            assert any(ch.isdigit() for ch in finding.detail), finding.key


def test_every_convention_declares_a_rule_for_both_directions():
    """A convention that can only be stated one way cannot report a negative
    finding, and the negatives here are the useful half."""
    for convention in CONVENTIONS:
        assert convention.rule_when_true.strip()
        assert convention.rule_when_false.strip()
        assert convention.rule_when_true != convention.rule_when_false


# --- end to end ------------------------------------------------------------

def test_it_keeps_the_refusals_in_the_written_draft(tmp_path):
    """A reader who does not see the refused ones will write one from memory,
    and it would be a rule the business does not follow."""
    log = tmp_path / "log.csv"
    with open(log, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["sql"])
        writer.writeheader()
        # DISTINCT queries, because the script deduplicates: a shape asked 25
        # times is one shape. The first version of this test wrote 25 copies
        # of two queries and got a pool of 2.
        for i in range(25):
            writer.writerow({"sql": f"SELECT SAFE_DIVIDE(SUM(a{i}), SUM(b{i})) AS r, region FROM t GROUP BY region"})
        for i in range(25):
            writer.writerow({"sql": f"SELECT SUM(c{i})/SUM(d{i}) AS r, region FROM t GROUP BY region"})
    out = tmp_path / "draft.md"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "mine_conventions.py"),
         "--log", str(log), "--out", str(out)],
        capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    draft = out.read_text()
    assert "DRAFT" in draft
    assert "Deliberately NOT proposed" in draft
    assert "divide guard" in draft
    # and it says the draft proves nothing on its own
    assert "only a round" in result.stdout or "worth nothing until a round" in draft


def test_a_log_with_nothing_agreed_says_so_instead_of_inventing_a_rule(tmp_path):
    log = tmp_path / "log.csv"
    with open(log, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["sql"])
        writer.writeheader()
        writer.writerow({"sql": "SELECT a, b FROM t GROUP BY a, b"})
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "mine_conventions.py"),
         "--log", str(log)],
        capture_output=True, text=True, timeout=180)
    assert result.returncode == 0
    assert "nothing proposed" in result.stdout
    assert "not a failure" in result.stdout


def test_bi_tool_queries_are_removed_before_any_counting(tmp_path):
    """Without this filter you discover a dashboard's habits and call them the
    analysts'. On one engagement 198 of the top 200 patterns by execution
    count were BI-tool probes.

    The filter has to be `is_noise`, not `written_by_a_tool`. The first
    version of this script used the latter, which only matches a header
    COMMENT a tool wrote about itself: it removed 36 rows of 4513 where
    `is_noise` removes 2489. That weak filter reported "LIMIT needs an ORDER
    BY" as a 63.1% split -- no convention -- when among real analyst queries
    it is 75.6%, a convention. The 37% without an ORDER BY were tool probes.
    """
    from context_lift.mine import is_noise, written_by_a_tool
    probe = "SELECT clmn0_ , clmn1_ FROM t GROUP BY clmn0_"
    assert is_noise(probe, require_analytical=True)
    assert not written_by_a_tool(probe)     # the weaker check misses it
    log = tmp_path / "log.csv"
    with open(log, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["sql"])
        writer.writeheader()
        for i in range(40):
            writer.writerow({"sql": f"SELECT clmn{i}_ / clmn1_ AS r, SUM(clmn{i}_) FROM t GROUP BY 1"})
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "mine_conventions.py"),
         "--log", str(log)],
        capture_output=True, text=True, timeout=180)
    assert "removed as noise" in result.stdout
    assert "nothing proposed" in result.stdout


def test_unwrap_keeps_a_function_call_intact():
    """The bug this pins cost the headline number. `.strip("()")` removes
    EVERY trailing paren, so `safe_divide(sum(a),sum(b))` became
    `safe_divide(sum(a),sum(b` -- the call's own closing parens went with it.
    `_split_ratio` then found no separator, returned None, and every
    SAFE_DIVIDE ratio was skipped WITHOUT BEING CHECKED: 48 of 92 queries on
    the real log, so the share was computed on the slash half alone.
    """
    from mine_conventions import unwrap
    assert unwrap("f(a)") == "f(a)"
    assert unwrap("safe_divide(sum(a),sum(b))") == "safe_divide(sum(a),sum(b))"
    assert unwrap("(a+b)") == "a+b"
    assert unwrap("((a))") == "a"
    assert unwrap("(a)+(b)") == "(a)+(b)"      # not one wrapping pair


def test_it_counts_distinct_shapes_not_executions(tmp_path):
    """A dashboard running ONE query ten thousand times must not outvote fifty
    analysts. `mine` masks literals for the same reason: two runs of the same
    report are one shape asked twice, not two shapes."""
    log = tmp_path / "log.csv"
    with open(log, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["sql"])
        writer.writeheader()
        for _ in range(500):
            writer.writerow({"sql": "SELECT SUM(a)/SUM(b) AS r, SUM(a) FROM t GROUP BY 1"})
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "mine_conventions.py"),
         "--log", str(log)],
        capture_output=True, text=True, timeout=180)
    assert "500 queries read, 1 distinct" in result.stdout
    # one shape is not evidence, however many times it ran
    assert "nothing proposed" in result.stdout
