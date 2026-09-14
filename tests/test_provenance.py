# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The record that makes a number defensible after the fact."""
import pytest

from context_lift.provenance import (Provenance, check_payload, content_hash,
                                 gate_rows, normalise, staleness)


def record(**kwargs):
    base = dict(round_id="r1", measured_at="2026-09-10T20:00:00Z",
                arm_control_hash="aaa", arm_treatment_hash="bbb")
    base.update(kwargs)
    return Provenance(**base)


def test_a_question_cannot_reach_an_aspect_any_viewer_can_read():
    """Any principal with catalogViewer reads a custom aspect, and the KC MCP
    server returns it to an agent. A gold question in an aspect is a gold
    question in somebody's prompt."""
    question = "What were the top 10 states by number of customers in 2024?" * 2

    with pytest.raises(SystemExit) as refused:
        check_payload({"round_id": question})

    assert "prose" in str(refused.value)


def test_a_field_nobody_named_is_refused_rather_than_written():
    """An allowlist, because removal only works against the fields somebody
    thought of and the field added next month is the one that leaks."""
    with pytest.raises(SystemExit) as refused:
        check_payload({"gold_sql": "SELECT 1"})

    assert "allowlist" in str(refused.value)


def test_a_gates_detail_sentence_does_not_travel():
    """A detail quotes table names, column names and question text. It is
    written for the person at the keyboard, where those are allowed."""
    rows = gate_rows([("leak", True, "0 of 8 held-out questions appear in "
                                     "customer_orders.net_revenue " * 3)])

    assert rows == [{"name": "leak", "passed": True}]
    check_payload({"gates": rows})


def test_an_edited_page_makes_the_number_stale():
    body = record(overview_hash=content_hash("One row per order.")).payload()

    current, why = staleness(body, "One row per order line.")

    assert not current
    assert "since changed" in why


def test_a_page_that_is_gone_is_stale_not_current():
    """The catalog has no versions, so a deleted page leaves a number about
    text nobody can read."""
    body = record(overview_hash=content_hash("One row per order.")).payload()

    current, why = staleness(body, "")

    assert not current
    assert "gone" in why


def test_reindenting_a_page_does_not_make_every_number_on_it_stale():
    body = record(overview_hash=content_hash("one two three")).payload()

    assert staleness(body, "one\n  two\n  three")[0]


def test_an_int_read_back_from_a_struct_is_an_int_again():
    """Every number in a protobuf Struct is a double. Written 3, read 3.0,
    and code comparing it to 3 fails on a value that never changed."""
    assert normalise({"k_repeats": 3.0})["k_repeats"] == 3


def test_a_hash_of_digits_is_not_turned_into_a_number():
    """The declared type decides, not the value. A string field stays a
    string even when it happens to be all digits."""
    assert normalise({"overview_hash": "12345"})["overview_hash"] == "12345"
