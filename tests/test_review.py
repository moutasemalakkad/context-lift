# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The human review as a stage: a sheet, a document, and a refusal."""
import csv

import pytest

from context_lift.backends import echo_backend
from context_lift.cli import main
from context_lift.review import ReviewNotFinished, draft, finish, prepare


def _candidates(path):
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "question", "gold_sql", "times_seen", "gate_status", "gate_detail", "table"])
        writer.writerow(["c1", "Total sale_price by order_status", "SELECT ...", 9, "ok", "", "sales.orders"])
        writer.writerow(["c2", "All business units", "SELECT bu FROM t GROUP BY 1", 40, "ok", "", "sales.units"])
        writer.writerow(["c3", "broken", "SELECT x", 2, "EXEC_FAIL", "no such column", "sales.orders"])
    return str(path)


def test_prepare_writes_the_sheet_with_the_reviewers_columns_and_the_document(tmp_path):
    counts = prepare(_candidates(tmp_path / "c.csv"), tmp_path / "r")

    with open(tmp_path / "r" / "review.csv", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert set(rows[0]) >= {"wording", "keep", "reason", "source", "table"}
    assert rows[0]["source"] == "mined" and rows[0]["keep"] == ""
    doc = (tmp_path / "r" / "REVIEW.md").read_text()
    assert "The rule, before the data" in doc and "3 candidates, 2 passed the gate" in doc
    assert counts["defects"] == {"EXEC_FAIL": 1}


def test_finish_refuses_while_any_row_is_undecided(tmp_path):
    prepare(_candidates(tmp_path / "c.csv"), tmp_path / "r")

    with pytest.raises(ReviewNotFinished) as stop:
        finish(tmp_path / "r" / "review.csv", tmp_path / "golds.csv")

    # 2 of 3, not 3 of 3: the EXEC_FAIL row is pre-decided by the gate now,
    # so a reviewer is never asked about it
    assert "2 of 3 rows have no keep decision" in str(stop.value)
    assert not (tmp_path / "golds.csv").exists()


def test_finish_writes_the_kept_rows_in_the_reviewers_words_and_counts_rejections(tmp_path):
    prepare(_candidates(tmp_path / "c.csv"), tmp_path / "r")
    sheet = tmp_path / "r" / "review.csv"
    with open(sheet, newline="") as handle:
        rows = list(csv.DictReader(handle)); columns = list(rows[0])
    rows[0].update(wording="How much did we sell in each order state?", keep="yes")
    rows[1].update(keep="no", reason="filter dropdown")
    rows[2].update(keep="no", reason="broken")
    with open(sheet, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns); writer.writeheader(); writer.writerows(rows)

    result = finish(sheet, tmp_path / "golds.csv")

    assert result == {"kept": 1, "rejected": {"filter dropdown": 1, "broken": 1}, "total": 3,
                      "model_reviewed": 0, "person_reviewed": 1, "reviewer_model": None,
                      "edited_sql": [], "duplicate_questions": [],
                      "clean": 2, "clean_rejected": 1}
    with open(tmp_path / "golds.csv", newline="") as handle:
        golds = list(csv.DictReader(handle))
    assert golds == [{"id": "c1", "question": "How much did we sell in each order state?",
                      "gold_sql": "SELECT ...", "table": "sales.orders", "source": "mined_rewritten"}]


def test_the_cli_runs_both_halves_and_exits_2_on_an_unfinished_sheet(tmp_path, capsys):
    candidates = _candidates(tmp_path / "c.csv")

    assert main(["review", "--candidates", candidates, "--out", str(tmp_path / "r")]) == 0
    assert "review sheet ->" in capsys.readouterr().out
    assert main(["review", "--sheet", str(tmp_path / "r" / "review.csv"), "--out", str(tmp_path / "g.csv")]) == 2
    assert "no keep decision" in capsys.readouterr().out


def _rewrite(sheet, rows):
    with open(sheet, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _read(sheet):
    with open(sheet, newline="") as handle:
        return list(csv.DictReader(handle))


def test_the_draft_decides_only_undecided_rows_and_never_touches_sql_or_question_text(tmp_path):
    prepare(_candidates(tmp_path / "c.csv"), tmp_path / "r")
    sheet = tmp_path / "r" / "review.csv"
    rows = _read(sheet)
    rows[0].update(wording="How much did we sell in each order state?", keep="yes")   # a person decided c1
    _rewrite(sheet, rows)
    # the model answers for c1 too (never shown to it), rejects c2, and gives
    # c3 a reason outside the list
    answer = ('```json\n[{"id": "c1", "keep": "no", "reason": "duplicate"}, '
              '{"id": "c2", "keep": "no", "reason": "dropdown_or_metadata"}, '
              '{"id": "c3", "keep": "no", "reason": "broken"}]\n```')

    counts = draft(sheet, echo_backend(answer), "gemini-2.5-pro", batch=10)

    # undecided 0, not 1: c3 is EXEC_FAIL, so prepare() decided it and the
    # model was never asked. A reviewer cannot answer "does this question
    # match this SQL" about a query that does not run.
    assert counts == {"total": 3, "decided": 1, "kept": 0, "rejected": 1,
                      "reasons": {"dropdown_or_metadata": 1}, "undecided": 0}
    by_id = {row["id"]: row for row in _read(sheet)}
    assert (by_id["c1"]["keep"], by_id["c1"]["source"], by_id["c1"]["wording"]) == (
        "yes", "mined", "How much did we sell in each order state?")
    assert (by_id["c2"]["keep"], by_id["c2"]["reason"], by_id["c2"]["source"]) == (
        "no", "dropdown_or_metadata", "model-reviewed:gemini-2.5-pro")
    assert by_id["c2"]["wording"] == "All business units"          # copied, not rewritten
    # c3 carries the GATE's refusal, not the model's and not a person's, so
    # `finish` can report the three apart
    assert (by_id["c3"]["keep"], by_id["c3"]["reason"], by_id["c3"]["source"]) == (
        "no", "gate_defect", "gate-refused:EXEC_FAIL")
    assert [r["gold_sql"] for r in by_id.values()] == ["SELECT ...", "SELECT bu FROM t GROUP BY 1", "SELECT x"]
    assert [r["question"] for r in by_id.values()] == [
        "Total sale_price by order_status", "All business units", "broken"]


def test_finish_counts_the_golds_a_model_accepted_apart_from_the_ones_a_person_did(tmp_path):
    prepare(_candidates(tmp_path / "c.csv"), tmp_path / "r")
    sheet = tmp_path / "r" / "review.csv"
    rows = _read(sheet)
    rows[0].update(keep="yes", reason="matches", source="model-reviewed:gemini-2.5-pro")
    rows[1].update(keep="yes", reason="matches", source="reviewed")      # a person checked this one
    rows[2].update(keep="no", reason="does_not_match", source="model-reviewed:gemini-2.5-pro")
    _rewrite(sheet, rows)

    result = finish(sheet, tmp_path / "golds.csv")

    assert (result["model_reviewed"], result["person_reviewed"], result["reviewer_model"]) == (
        1, 1, "gemini-2.5-pro")
    assert result["rejected"] == {"does_not_match": 1}
    assert [(g["id"], g["source"]) for g in _read(tmp_path / "golds.csv")] == [
        ("c1", "model-reviewed:gemini-2.5-pro"), ("c2", "reviewed")]


def test_finish_names_the_golds_whose_sql_was_edited_after_the_gate_read_it(tmp_path):
    """One estate rewrote a project name in the sheet to fix a cross-project
    gold. The edit was fair, but the file then carried a gold the gate never
    read, and nothing said so."""
    prepare(_candidates(tmp_path / "c.csv"), tmp_path / "r")
    sheet = tmp_path / "r" / "review.csv"
    rows = _read(sheet)
    rows[0].update(keep="yes", reason="matches", source="reviewed")
    rows[1].update(keep="yes", reason="matches", source="reviewed",
                   gold_sql="SELECT bu FROM other_project.t GROUP BY 1")   # edited here
    rows[2].update(keep="no", reason="not_a_question", source="reviewed")
    _rewrite(sheet, rows)

    result = finish(sheet, tmp_path / "golds.csv")

    assert result["edited_sql"] == ["c2"]
    assert [g["gold_sql"] for g in _read(tmp_path / "golds.csv")] == [
        "SELECT ...", "SELECT bu FROM other_project.t GROUP BY 1"]


def test_finish_counts_the_gate_clean_candidates_a_reviewer_threw_out(tmp_path):
    """The gate passed 2 of these 3 rows and a reviewer kept 1 of those 2.
    A user sizing an answer key from the gate's pass count would be wrong."""
    prepare(_candidates(tmp_path / "c.csv"), tmp_path / "r")
    sheet = tmp_path / "r" / "review.csv"
    rows = _read(sheet)
    rows[0].update(keep="yes", reason="matches", source="reviewed")
    rows[1].update(keep="no", reason="dropdown_or_metadata", source="reviewed")
    rows[2].update(keep="no", reason="not_a_question", source="reviewed")
    _rewrite(sheet, rows)

    result = finish(sheet, tmp_path / "golds.csv")

    assert (result["clean"], result["clean_rejected"]) == (2, 1)
    assert result["edited_sql"] == []


def test_review_finish_says_the_sql_was_edited_and_the_gate_must_run_again(tmp_path, capsys):
    prepare(_candidates(tmp_path / "c.csv"), tmp_path / "r")
    sheet = tmp_path / "r" / "review.csv"
    rows = _read(sheet)
    rows[0].update(keep="yes", reason="matches", source="reviewed",
                   gold_sql="SELECT 1 FROM other_project.t")
    rows[1].update(keep="no", reason="dropdown_or_metadata", source="reviewed")
    rows[2].update(keep="no", reason="not_a_question", source="reviewed")
    _rewrite(sheet, rows)

    code = main(["review", "--sheet", str(sheet), "--out", str(tmp_path / "golds.csv")])

    printed = capsys.readouterr().out
    assert code == 0
    assert ("1 of 2 gate-clean candidates were rejected by a reviewer: the gate's pass "
            "count is not the size of the key") in printed
    assert ("1 kept golds carry SQL edited in the sheet (c1): the gate never read this SQL, "
            "so run gate again on this file before scoring against it") in printed


def test_finish_names_two_kept_rows_that_ask_the_same_question_with_different_sql(tmp_path):
    """A field review found two candidates with the same question text and a
    different measure column. One question cannot have two right answers, and
    a reviewer reading one row at a time never sees the pair."""
    prepare(_candidates(tmp_path / "c.csv"), tmp_path / "r")
    sheet = tmp_path / "r" / "review.csv"
    rows = _read(sheet)
    rows[0].update(keep="yes", reason="matches", source="reviewed",
                   wording="How much did we sell in each order state?")
    rows[1].update(keep="yes", reason="matches", source="reviewed",
                   wording="How much did we sell in each order state?")   # same words, other SQL
    rows[2].update(keep="no", reason="not_a_question", source="reviewed")
    _rewrite(sheet, rows)

    result = finish(sheet, tmp_path / "golds.csv")

    assert result["duplicate_questions"] == [["c1", "c2"]]


def test_finish_groups_kept_golds_whose_questions_are_reworded_the_same(tmp_path):
    """A field review found 8 pairs of candidates asking one business question
    with genuinely different SQL, most of them reworded rather than copied. No
    normalising of the SQL merges those; only the question text shows it."""
    prepare(_candidates(tmp_path / "c.csv"), tmp_path / "r")
    sheet = tmp_path / "r" / "review.csv"
    rows = _read(sheet)
    rows[0].update(keep="yes", reason="matches", source="reviewed",
                   wording="How much did we sell in each order state?")
    rows[1].update(keep="yes", reason="matches", source="reviewed",
                   wording="How much did we sell by order state?")     # reworded, not copied
    rows[2].update(keep="yes", reason="matches", source="reviewed",
                   wording="Which suppliers shipped late last month?")  # a different question
    _rewrite(sheet, rows)

    result = finish(sheet, tmp_path / "golds.csv")

    assert result["duplicate_questions"] == [["c1", "c2"]]


def test_the_draft_prompt_carries_the_questions_already_in_the_key(tmp_path):
    """A model judging one row at a time cannot see that two rows ask the same
    thing. In a field review, 4 of the 13 rows it kept and a person threw out
    were duplicates, and it never once used the duplicate reason."""
    from context_lift.review import build_review_prompt, draft_plan

    prepare(_candidates(tmp_path / "c.csv"), tmp_path / "r")
    sheet = tmp_path / "r" / "review.csv"
    rows = _read(sheet)
    rows[0].update(keep="yes", reason="matches", source="reviewed",
                   wording="How much did we sell in each order state?")
    _rewrite(sheet, rows)

    prompts, undecided, total = draft_plan(sheet, batch=10)

    # 1 of 3: c3's SQL was refused by the gate, so it is already decided
    assert (undecided, total) == (1, 3)
    assert "already in the answer key" in prompts[0]
    assert "How much did we sell in each order state?" in prompts[0]
    assert 'reason "duplicate"' in prompts[0]
    # no accepted rows, no section: a first pass shows only the batch
    assert "already in the answer key" not in build_review_prompt(rows[1:], [])


def test_the_priced_prompts_are_the_longest_the_draft_can_send(tmp_path):
    """The key grows while the draft runs, so batch two carries batch one's
    questions. draft_plan prices the worst case, every earlier row accepted,
    so the estimate can only be high."""
    from context_lift.review import draft_plan

    prepare(_candidates(tmp_path / "c.csv"), tmp_path / "r")
    sheet = tmp_path / "r" / "review.csv"

    prompts, _undecided, _total = draft_plan(sheet, batch=1)

    # 2, not 3: the gate-refused row is never priced or sent
    assert len(prompts) == 2
    assert "already in the answer key" not in prompts[0]
    assert "Total sale_price by order_status" in prompts[1]      # batch 1's question
    assert len(prompts[1]) > len(prompts[0])


def test_a_sheet_where_no_row_has_a_question_refuses_before_spending(tmp_path):
    """A model cannot judge a row with no question: it is being asked whether
    to keep something nobody has said yet.

    Four calls and $0.0099 bought 0 of 40 decisions this way. Mined candidates
    carry SQL and no question until somebody writes one.
    """
    from context_lift.review import draft_plan

    sheet = tmp_path / "review.csv"
    sheet.write_text(
        "id,question,gold_sql,wording,keep,reason,source\n"
        "m000,,SELECT 1,,,,mined\n"
        "m001,,SELECT 2,,,,mined\n", encoding="utf-8")

    with pytest.raises(SystemExit) as refused:
        draft_plan(sheet)

    assert "nothing for a model to judge" in str(refused.value)
    assert "mine --write-questions" in str(refused.value)


def test_one_row_with_a_question_is_enough_to_proceed(tmp_path):
    """The refusal is for a sheet nobody has written anything on, not for a
    sheet a person is part way through."""
    from context_lift.review import draft_plan

    sheet = tmp_path / "review.csv"
    sheet.write_text(
        "id,question,gold_sql,wording,keep,reason,source\n"
        "m000,,SELECT 1,,,,mined\n"
        "m001,how many orders?,SELECT 2,,,,mined\n", encoding="utf-8")

    prompts, undecided, total = draft_plan(sheet)

    assert undecided == 2 and total == 2 and prompts


def test_the_model_reply_is_written_down_before_it_is_parsed(tmp_path):
    """A run that decided nothing left nothing to read, so the only way to
    find out why was to pay again."""
    from context_lift.review import draft

    sheet = tmp_path / "review.csv"
    sheet.write_text(
        "id,question,gold_sql,wording,keep,reason,source\n"
        "m000,how many orders?,SELECT 1,,,,mined\n", encoding="utf-8")

    draft(sheet, lambda prompt: "the model said something unparseable", "fake-model")

    raw = (tmp_path / "review.raw.txt").read_text(encoding="utf-8")
    assert "the model said something unparseable" in raw
    assert "batch of 1 rows" in raw


def test_a_cut_off_array_still_counts_the_rows_that_arrived():
    """A batch of 10 came back with 9 complete decisions and a half-written
    tenth. Reading the whole array at once threw all 9 away and reported 0 of
    40 decided across four paid calls."""
    from context_lift.review import parse_decisions

    truncated = (
        '```json\n[\n'
        '  {"id": "a", "keep": "yes", "reason": "matches"},\n'
        '  {"id": "b", "keep": "no", "reason": "duplicate"},\n'
        '  {"id": "c", "keep"'
    )

    decided = parse_decisions(truncated, {"a", "b", "c"})

    assert decided == {"a": ("yes", "matches"), "b": ("no", "duplicate")}
    assert "c" not in decided, "a half-written object is not a decision"


def test_a_whole_array_still_reads_as_one_piece():
    """The salvage is a fallback, not the normal path."""
    from context_lift.review import parse_decisions

    whole = '[{"id": "a", "keep": "yes", "reason": "matches"}]'

    assert parse_decisions(whole, {"a"}) == {"a": ("yes", "matches")}


def test_a_brace_inside_a_reason_does_not_cut_the_object_short():
    """Brace counting rather than a regular expression, which would end the
    object at the wrong place."""
    from context_lift.review import _complete_objects

    text = '[{"id": "a", "keep": "no", "reason": "unclear {see note}"}'

    assert _complete_objects(text)[0]["reason"] == "unclear {see note}"


def test_the_output_cap_leaves_room_for_a_thinking_model():
    """The reasoning is billed as output and spends the same budget: one run
    used 1,658 thinking tokens against 1,526 of answer, and the old cap of
    200 + 60 a row cut the array short every time."""
    from context_lift.review import output_token_cap

    assert output_token_cap(10) > 1658 + 10 * 30


# --- do not ask a reviewer about rows the gate already refused ---------------

def test_a_gate_refused_row_arrives_already_decided(tmp_path):
    """The sheet used to hand every candidate over with an empty `keep`,
    including the ones whose SQL cannot run.

    Measured on one estate: of 344 candidates a model reviewer was asked to
    judge, it kept 24 whose SQL returns a 403, names a dropped table, or
    returns no rows -- and left 16 more undecided, every one defective.
    Three costs: review budget on rows that can never be golds, a reviewer's
    attention on the same, and a kept count that reads as the answer-key size
    (63 kept became 39 after re-gating).
    """
    prepare(_candidates(tmp_path / "c.csv"), tmp_path / "r")
    rows = {r["id"]: r for r in _read(tmp_path / "r" / "review.csv")}
    # the two clean rows are still a reviewer's to decide
    assert (rows["c1"]["keep"], rows["c1"]["reason"]) == ("", "")
    assert (rows["c2"]["keep"], rows["c2"]["reason"]) == ("", "")
    # the EXEC_FAIL row is not
    assert rows["c3"]["keep"] == "no"
    assert rows["c3"]["reason"] == "gate_defect"
    assert rows["c3"]["source"] == "gate-refused:EXEC_FAIL"


def test_the_refused_row_stays_in_the_sheet(tmp_path):
    """It is the DECISION that is made for the reviewer, not the information
    withheld. A reviewer should be able to see what was dropped and why."""
    prepare(_candidates(tmp_path / "c.csv"), tmp_path / "r")
    rows = _read(tmp_path / "r" / "review.csv")
    assert len(rows) == 3
    c3 = next(r for r in rows if r["id"] == "c3")
    assert c3["gate_status"] == "EXEC_FAIL"
    assert c3["gate_detail"] == "no such column"     # the reason survives


def test_a_warning_is_not_a_refusal(tmp_path):
    """`ok (MISSING_LITERAL)` is a gold the gate KEPT. On one estate 65 of 89
    usable candidates carried a warning, and 24 of those were
    MISSING_LITERAL, which `--rules-in-context` produces on purpose. Deciding
    those for the reviewer would throw away most of the answer key."""
    path = tmp_path / "c.csv"
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "question", "gold_sql", "times_seen",
                         "gate_status", "gate_detail", "table"])
        writer.writerow(["w1", "units by region", "SELECT 1", 3,
                         "ok (MISSING_LITERAL)", "may leave out 1 value", "d.t"])
    prepare(path, tmp_path / "r")
    row = _read(tmp_path / "r" / "review.csv")[0]
    assert (row["keep"], row["source"]) == ("", "mined")


def test_the_count_of_pre_rejected_rows_is_reported(tmp_path):
    """Silently deciding rows would be worse than asking about them. The
    funnel says how many and the summary can print it."""
    counts = prepare(_candidates(tmp_path / "c.csv"), tmp_path / "r")
    assert counts["pre_rejected"] == 1
