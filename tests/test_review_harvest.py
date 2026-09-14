# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The review sheet as a context-collection instrument.

M21's exit test: *`review --harvest` turns one analyst comment into a candidate
rule line in a FILE a person approves, that line passes the leak gate, and its
provenance records `human_review_comment`.*

THE IDEA, which is bigger than the feature. An analyst rejecting a candidate
question does not just filter it. They explain the business -- "that column is
the shipping country, the other one is the billing country" -- and that
explanation IS the written context this whole framework exists to measure.
Until there was a `comment` column it landed nowhere and was thrown away.

Every other route to more human context needs somebody to sit down and write
documentation. This one collects it as a by-product of a review they have to
do anyway.

THE ONE WAY IT COULD MAKE THINGS WORSE, and it is why the leak gate is here: a
comment written on a SCORED question can quote that question's answer. A rule
carrying the answer key would make every later measurement meaningless while
looking like an improvement.
"""
from __future__ import annotations

import csv
import pathlib

import pytest

from context_lift.review import (COLUMN_ORDER, COMMENT_COLUMN,
                                 HARVEST_PROVENANCE, MODEL_COLUMNS, agreement,
                                 agreement_text, comments_in, harvest,
                                 harvest_lines, harvest_prompt,
                                 ordered_columns)

GOLDS = [("q1", "How many complete orders were there?",
          "SELECT COUNT(*) FROM orders WHERE status = 'Complete'")]

COMMENT = ("site_country is the shipping country. The country column on the "
           "customer table is where they signed up and the two disagree for "
           "about a tenth of orders.")


def a_sheet(tmp_path, **row) -> pathlib.Path:
    columns = ["id", "question", "keep", COMMENT_COLUMN, "reason", "gold_sql",
               *MODEL_COLUMNS]
    base = {c: "" for c in columns}
    base.update({"id": "q1", "question": "How many orders?"})
    base.update(row)
    path = tmp_path / "review.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerow(base)
    return path


def echo(text: str):
    return lambda _prompt: text


# ---------------------------------------------------------------------------
# THE EXIT TEST

def test_one_comment_becomes_a_rule_line_in_a_file_a_person_approves(tmp_path):
    sheet = a_sheet(tmp_path, comment=COMMENT)

    found = harvest(sheet, echo("- `site_country` is the shipping country, "
                                "not where the customer signed up."), GOLDS)

    assert found["comments"] == 1
    assert found["lines"] == [
        "- `site_country` is the shipping country, not where the customer "
        "signed up."]
    assert found["refused"] == []
    # a FILE a person approves: the text says so before the first rule
    assert "NOT APPROVED" in found["text"]
    assert found["lines"][0] in found["text"]


def test_the_harvested_line_carries_its_provenance(tmp_path):
    """D36 ranks a declared definition above a counted one. An ablation can
    only separate the two if the line says which it is."""
    sheet = a_sheet(tmp_path, comment=COMMENT)

    found = harvest(sheet, echo("- Revenue excludes returns."), GOLDS)

    assert HARVEST_PROVENANCE == "human_review_comment"
    assert HARVEST_PROVENANCE in found["text"]


def test_a_drafted_line_that_quotes_an_answer_key_is_refused(tmp_path):
    """The one way this makes the measurement worse rather than better."""
    sheet = a_sheet(tmp_path, comment="the complete orders one is wrong")

    leaky = "- Use SELECT COUNT(*) FROM orders WHERE status = 'Complete'."
    found = harvest(sheet, echo(leaky), GOLDS)

    assert found["lines"] == []
    assert len(found["refused"]) == 1
    line, why = found["refused"][0]
    assert line == leaky
    assert "leak gate" in why and "q1" in why


def test_a_refused_line_is_shown_rather_than_dropped(tmp_path):
    """The analyst still said something true about the business. Dropping it
    silently loses the observation as well as the line."""
    sheet = a_sheet(tmp_path, comment="x")

    found = harvest(sheet, echo("- Use SELECT COUNT(*) FROM orders WHERE "
                                "status = 'Complete'."), GOLDS)

    assert found["refused"], "the line vanished instead of being reported"


def test_harvest_writes_nothing_by_itself(tmp_path):
    """An LLM stage gets the same treatment as every other one here: the
    output is a draft, never a file the tool applies."""
    sheet = a_sheet(tmp_path, comment=COMMENT)
    before = sorted(p.name for p in tmp_path.iterdir())

    harvest(sheet, echo("- Something."), GOLDS)

    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_the_prompt_never_shows_the_model_the_answer_key():
    """The cheapest guard against drafting a rule from the answers is not to
    show it the answers."""
    prompt = harvest_prompt([("q1", COMMENT)])

    assert COMMENT in prompt
    assert "SELECT" not in prompt.upper().replace("SELECTED", "")
    assert "gold" not in prompt.lower()


def test_no_comment_is_not_an_error(tmp_path):
    """A sheet nobody has commented on is the ordinary first case."""
    found = harvest(a_sheet(tmp_path), echo("- anything"), GOLDS)

    assert found["comments"] == 0
    assert found["lines"] == []


# ---------------------------------------------------------------------------
# the sheet itself

def test_the_comment_column_has_no_schema_and_no_dropdown():
    """Free text on purpose. A dropdown would collect the categories somebody
    thought of in advance, which is the opposite of the point."""
    assert COMMENT_COLUMN == "comment"
    rows = [{"id": "a", COMMENT_COLUMN: " anything at all "},
            {"id": "b", COMMENT_COLUMN: ""}]

    assert comments_in(rows) == [("a", "anything at all")]


def test_the_reviewer_reads_the_decision_columns_first():
    """`gold_sql` used to be column 3 of 14, so a reviewer opening a
    spreadsheet scrolled past a wall of SQL to reach the one cell they had to
    fill in."""
    order = list(COLUMN_ORDER)

    assert order[:6] == ["id", "question", "keep", "comment", "reason",
                         "wording"]
    assert order.index("keep") < order.index("gold_sql")
    assert order.index("comment") < order.index("gold_sql")
    # the model's opinion sits after the human's, and before the evidence
    assert order.index("keep") < order.index("model_keep") < order.index("gold_sql")


def test_an_unknown_column_is_kept_rather_than_dropped():
    """A sheet carries whatever the candidate file carried. Quietly losing a
    column because the order list had not heard of it would destroy a
    reviewer's own notes."""
    out = ordered_columns(["gold_sql", "their_own_note", "id", "keep"])

    assert out == ["id", "keep", "gold_sql", "their_own_note"]


# ---------------------------------------------------------------------------
# the agreement rate

def test_agreement_counts_only_rows_where_both_decided():
    """A row the model never saw is not agreement. Counting it as one pushes
    the rate toward 100% exactly as a reviewer does less work."""
    rows = [{"keep": "yes", "model_keep": "yes"},
            {"keep": "no", "model_keep": "yes"},
            {"keep": "yes", "model_keep": ""},
            {"keep": "", "model_keep": "no"}]

    found = agreement(rows)

    assert found["compared"] == 2
    assert found["agreed"] == 1
    assert found["rate"] == 0.5


def test_nobody_has_checked_is_not_zero_agreement():
    """None, not 0.0. "Nobody has compared them" and "they never agree" are
    different facts, and a rate of 0% reads as the second."""
    found = agreement([{"keep": "yes", "model_keep": ""}])

    assert found["rate"] is None
    assert "nothing to compare" in agreement_text(found)


def test_the_rate_names_the_disagreements_as_the_rows_worth_reading():
    """The whole point of the number: the disagreements are where the model's
    error rate lives, and a rate alone sends nobody to look at them."""
    text = agreement_text(agreement([{"keep": "yes", "model_keep": "no"},
                                     {"keep": "yes", "model_keep": "yes"}]))

    assert "1 of 2 rows (50%)" in text
    assert "worth reading" in text


def test_yes_and_no_are_read_the_way_the_sheet_writes_them():
    """A spreadsheet turns "yes" into "TRUE" often enough that a strict
    comparison would call it a disagreement."""
    found = agreement([{"keep": "TRUE", "model_keep": "yes"},
                       {"keep": "0", "model_keep": "no"}])

    assert found["agreed"] == 2


# ---------------------------------------------------------------------------
# refusing a review that is not independent (task #27)

from context_lift.review import MODEL_DRAFTED, model_family, self_review_refusal

DRAFTED = [{"author": "model draft, reviewed against the live schema"}] * 10


def test_the_same_family_reviewing_its_own_questions_is_refused():
    """A model checking its own family's work shares its blind spots: the
    mistake the drafter made is the one the reviewer is least likely to see."""
    stop = self_review_refusal(DRAFTED, "gemini-2.5-pro", "gemini-2.5-flash")

    assert stop is not None
    assert "same family" in stop
    assert "blind spots" in stop
    assert "--allow-self-review" in stop, "a refusal with no way past it is a wall"


def test_a_different_family_is_allowed():
    assert self_review_refusal(DRAFTED, "claude-opus-5",
                               "gemini-2.5-flash") is None


def test_questions_a_person_wrote_need_no_refusal():
    assert self_review_refusal([{"author": "repo"}], "gemini-2.5-pro") is None


def test_an_unnamed_drafter_is_reported_as_UNKNOWN_not_as_safe():
    """The honest case, and the one the data actually produces. `mine` writes
    `author` as "model draft, reviewed against the live schema": it records
    THAT a model wrote the question and never WHICH. So independence cannot
    be determined, and saying nothing would read as saying it is fine."""
    stop = self_review_refusal(DRAFTED, "gemini-2.5-pro")

    assert stop is not None
    assert "cannot be determined" in stop
    assert "--question-model" in stop


def test_the_family_is_the_token_before_the_first_hyphen():
    """gemini-2.5-flash and gemini-2.5-pro are one family. Two versions of
    one model are not an independent check of each other."""
    assert model_family("gemini-2.5-flash") == model_family("gemini-2.5-pro")
    assert model_family("gemini-2.5-flash") != model_family("claude-opus-5")


def test_the_marker_matches_what_mine_actually_writes():
    """If `mine` ever changes that string, this refusal silently stops firing
    and every self-review goes through unnoticed."""
    assert MODEL_DRAFTED == "model draft"
    assert MODEL_DRAFTED in "model draft, reviewed against the live schema"
