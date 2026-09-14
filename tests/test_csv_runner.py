# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Scoring predictions somebody else's agent produced."""
import csv

import pytest

from context_lift.runners.csv_files import CsvRunner, content_hash, read


class FakeArm:
    def __init__(self, label):
        self.label = label
        self.arm_id = "x"


def write(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "pred_sql"])
        writer.writeheader()
        writer.writerows(rows)
    return path


def runner(tmp_path, control_rows, treatment_rows):
    return CsvRunner.from_files(
        write(tmp_path / "c.csv", control_rows),
        write(tmp_path / "t.csv", treatment_rows))


def test_each_arm_reads_its_own_file(tmp_path):
    made = runner(tmp_path,
                  [{"id": "q1", "pred_sql": "SELECT 1"}],
                  [{"id": "q1", "pred_sql": "SELECT 2"}])

    assert made.answer("q1#rep0", "?", FakeArm("0d control")).pred_sql == "SELECT 1"
    assert made.answer("q1#rep0", "?", FakeArm("1 authored")).pred_sql == "SELECT 2"


def test_a_missing_row_is_unanswered_not_wrong(tmp_path):
    """Scoring a missing row as a failure invents one and moves the
    denominator, which is the same mistake as counting an excluded question
    as wrong."""
    made = runner(tmp_path, [{"id": "q1", "pred_sql": "SELECT 1"}],
                  [{"id": "q1", "pred_sql": "SELECT 2"}])

    answer = made.answer("q9#rep0", "?", FakeArm("1 authored"))

    assert answer.pred_sql is None
    assert "no row" in answer.error_class
    assert made.missing["1 authored"] == ["q9"]


def test_repeats_of_a_written_answer_are_the_same_answer(tmp_path):
    """A file has no run-to-run variation to measure, and pretending it does
    would put a fake stability number beside a real one."""
    made = runner(tmp_path, [{"id": "q1", "pred_sql": "SELECT 1"}],
                  [{"id": "q1", "pred_sql": "SELECT 2"}])

    first = made.answer("q1#rep0", "?", FakeArm("0d control")).pred_sql
    second = made.answer("q1#rep4", "?", FakeArm("0d control")).pred_sql

    assert first == second


def test_it_costs_nothing_and_says_why(tmp_path):
    """A bare zero next to a dollar sign reads as an estimate that came out
    low, rather than as a run that cannot cost anything."""
    made = runner(tmp_path, [], [])

    sheet = made.cost_sheet(30, 2, 3)

    assert sheet.calls == 0
    assert "runs no model" in sheet.basis


def test_a_file_without_the_columns_says_which_are_missing(tmp_path):
    path = tmp_path / "bad.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["question", "sql"])
        writer.writeheader()

    with pytest.raises(SystemExit) as refused:
        read(path)

    assert "pred_sql" in str(refused.value)


def test_other_columns_are_kept_and_ignored(tmp_path):
    """A team's export carries their own columns, and refusing it would make
    them edit a file just to run a check."""
    path = tmp_path / "extra.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "pred_sql", "theirs"])
        writer.writeheader()
        writer.writerow({"id": "q1", "pred_sql": "SELECT 1", "theirs": "note"})

    assert read(path) == {"q1": "SELECT 1"}


def test_the_arm_hash_is_the_files_own_hash_not_an_invented_one(tmp_path):
    """This tool did not build the context, so it cannot hash it. What it
    knows is the file: two runs of the same file are the same experiment,
    and that is all it can honestly claim."""
    one = write(tmp_path / "a.csv", [{"id": "q1", "pred_sql": "SELECT 1"}])
    same = write(tmp_path / "b.csv", [{"id": "q1", "pred_sql": "SELECT 1"}])
    other = write(tmp_path / "c.csv", [{"id": "q1", "pred_sql": "SELECT 2"}])

    assert content_hash(one) == content_hash(same)
    assert content_hash(one) != content_hash(other)


def test_coverage_says_how_much_of_the_set_each_file_answered(tmp_path):
    made = runner(tmp_path, [{"id": "q1", "pred_sql": "SELECT 1"}],
                  [{"id": "q1", "pred_sql": "SELECT 2"},
                   {"id": "q2", "pred_sql": "SELECT 3"}])

    text = made.coverage(["q1", "q2", "q3"])

    assert "control: 1 of 3" in text
    assert "treatment: 2 of 3" in text
