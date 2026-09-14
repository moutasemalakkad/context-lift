# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The re-read guard, which could not count until now.

`gates.holdout_reads(n)` exists to catch the one thing that quietly destroys a
reserved set: scoring it again. Read the held-out questions three times, pick
the best run, and they are not held out any more -- they are a working set
with a p-value attached.

`round.run` took `holdout_reads_so_far: int = 0` and NOTHING EVER PASSED IT.
The gate computed `0 + 1 = 1` on every round, and the run record wrote the
literal `"holdout_reads": 1`. So the guard reported "first read" for every
read since it was written, and the record carried a number nobody had counted.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from context_lift.gates import holdout_reads
from context_lift.ledger import (prior_reads, read_ledger, record_reads,
                                 reads_note)


# ---------------------------------------------------------------------------
# the ledger

def test_a_missing_file_is_no_history_not_an_error(tmp_path):
    """The first round on a gold set has no history, and refusing to run
    until somebody creates an empty file is a guard that only annoys."""
    assert read_ledger(tmp_path / "nope.json") == {}
    assert read_ledger(None) == {}
    assert prior_reads({}, ["a", "b"]) == 0


def test_a_read_is_recorded_and_accumulates(tmp_path):
    path = tmp_path / "l.json"

    record_reads(path, ["q1", "q2"])
    record_reads(path, ["q1"])

    assert read_ledger(path) == {"q1": 2, "q2": 1}


def test_prior_reads_takes_the_maximum_not_the_mean(tmp_path):
    """One question read four times contaminates the set it belongs to.
    Averaging that against fresh questions is how a contaminated set passes
    as clean."""
    ledger = {"q1": 4, "q2": 0, "q3": 0, "q4": 0}

    assert prior_reads(ledger, ["q1", "q2", "q3", "q4"]) == 4


def test_an_unreadable_ledger_refuses_rather_than_reporting_zero(tmp_path):
    """Reporting "no history" for a file we could not parse is the exact
    failure this ledger exists to prevent. Same rule as the scan that
    refuses an unresolvable ref instead of printing clean."""
    path = tmp_path / "l.json"
    path.write_text("{not json")

    with pytest.raises(SystemExit) as raised:
        read_ledger(path)

    assert "not readable as a holdout ledger" in str(raised.value)
    assert "must not treat an unreadable history as no history" in str(raised.value)


def test_a_ledger_holding_the_wrong_shape_also_refuses(tmp_path):
    path = tmp_path / "l.json"
    path.write_text('["q1", "q2"]')

    with pytest.raises(SystemExit) as raised:
        read_ledger(path)

    assert "not an object of" in str(raised.value)


def test_the_file_is_sorted_so_two_rounds_in_any_order_agree(tmp_path):
    first, second = tmp_path / "a.json", tmp_path / "b.json"

    record_reads(first, ["q9", "q1"])
    record_reads(second, ["q1", "q9"])

    assert first.read_text() == second.read_text()


def test_an_untouched_set_produces_no_warning():
    assert reads_note({}, ["q1", "q2"]) == ""


def test_the_warning_says_how_many_and_how_often():
    note = reads_note({"q1": 3, "q2": 1}, ["q1", "q2", "q3"])

    assert "2 of 3 reserved question(s)" in note
    assert "up to 3 time(s)" in note
    assert "not a held-out lift" in note


# ---------------------------------------------------------------------------
# the gate it feeds

def test_the_gate_passes_on_a_first_read():
    assert holdout_reads(1).passed


def test_the_gate_fails_on_a_second_read():
    """This is what could never happen before: the gate was always handed 1."""
    assert not holdout_reads(2).passed


# ---------------------------------------------------------------------------
# the wiring, which is the part that was missing

def test_round_declares_the_flag_that_supplies_the_count():
    from context_lift import cli

    parser = cli._build_parser()
    sub = next(a for a in parser._actions if hasattr(a, "choices") and a.choices)
    declared = {a.dest for a in sub.choices["round"]._actions}

    assert "holdout_ledger" in declared, (
        "round cannot count holdout reads without a file to count them in")


def test_the_round_passes_the_count_rather_than_defaulting_to_zero():
    """The defect in one line: `holdout_reads_so_far` appears in round.run's
    signature and in no caller. This asserts a caller exists."""
    import inspect

    from context_lift import cli

    source = inspect.getsource(cli)
    assert "holdout_reads_so_far=reads_before" in source, (
        "round.run's holdout_reads_so_far is back to being unsupplied, so "
        "the re-read gate reads 'first read' on every round again")


def test_the_record_says_null_rather_than_one_when_nothing_is_tracked():
    """"Not tracked" and "read once" are different facts. The literal 1 that
    used to sit in the record made a re-read look like a first read."""
    import inspect

    from context_lift import cli

    source = inspect.getsource(cli)
    assert '"holdout_reads": 1,' not in source, (
        "the hardcoded 1 is back in the run record")
    assert 'None if not args.holdout_ledger' in source
