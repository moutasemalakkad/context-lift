# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Where the model's context comes from, and how an arm is named for it.

The weight is on identity. A source that hands different text to the same
question is a different arm, and if two of them collide under one id then
`ablate` compares them and reports a null result that is an artifact of the
naming rather than a fact about the context.
"""
import json

import pytest

from context_lift.context import (DirectoryContextSource, FileContextSource,
                              MappingContextSource, fetch_document,
                              retrieved_fingerprint)
from context_lift.workspace import arm_id


def test_a_file_source_hands_every_question_the_same_text(tmp_path):
    path = tmp_path / "rules.md"
    path.write_text('- "revenue" -> order_items.sale_price\n')

    source = FileContextSource(path)

    assert source.for_question("q1", "how much?") == source.for_question("q2", "who?")


def test_a_mapping_source_gives_nothing_for_a_question_it_has_no_entry_for():
    # A retriever finding nothing is a normal outcome, not an error: the run
    # answers that question without context, and the score records what that
    # cost.
    source = MappingContextSource({"q1": "orders exclude cancelled"})

    assert source.for_question("q1", "?") == "orders exclude cancelled"
    assert source.for_question("q2", "?") == ""


def test_a_mapping_file_that_is_not_id_to_text_is_refused_with_a_sentence(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"q1": {"text": "nested"}}))

    with pytest.raises(SystemExit) as stop:
        MappingContextSource.from_json(path)

    assert "question_id" in str(stop.value)


def test_a_directory_source_finds_a_table_under_any_of_the_three_shapes(tmp_path):
    (tmp_path / "sales.orders.md").write_text("orders doc")
    (tmp_path / "sales__customers.md").write_text("customers doc")
    (tmp_path / "items.md").write_text("items doc")
    source = DirectoryContextSource(tmp_path, {
        "q1": ["sales.orders"], "q2": ["sales.customers"], "q3": ["sales.items"],
        "q4": ["sales.orders", "sales.items"], "q5": ["sales.missing"],
    })

    assert source.for_question("q1", "?") == "orders doc"
    assert source.for_question("q2", "?") == "customers doc"
    assert source.for_question("q3", "?") == "items doc"
    assert source.for_question("q4", "?") == "orders doc\n\nitems doc"
    assert source.for_question("q5", "?") == ""      # routed to a table with no file


def test_the_fingerprint_ignores_the_order_a_retriever_happened_to_return_in():
    one = retrieved_fingerprint({"q1": "a", "q2": "b"})
    other = retrieved_fingerprint({"q2": "b", "q1": "a"})

    assert one == other


def test_the_fingerprint_is_none_when_nothing_was_retrieved():
    # An arm that retrieved nothing is the same arm as one with no retriever,
    # which is what it is.
    assert retrieved_fingerprint({}) is None
    assert retrieved_fingerprint({"q1": "", "q2": ""}) is None


def test_two_different_retrievals_are_two_different_arms():
    """The correctness bug this exists to prevent: colliding arms make ablate
    compare two different retrievals as if nothing had changed."""
    context = "c" * 64
    first = arm_id(context, None, retrieved_fingerprint({"q1": "the orders table"}))
    second = arm_id(context, None, retrieved_fingerprint({"q1": "the customers table"}))

    assert first != second


def test_an_arm_with_no_retrieval_keeps_the_id_it_already_had_on_disk():
    """Adding a third input must not rename arms recorded before it existed, or
    every workspace's history splits in two."""
    assert arm_id("c" * 64, None) == arm_id("c" * 64, None, None)
    assert arm_id(None, None) == arm_id(None, None, retrieved_fingerprint({"q1": ""}))


class _FlakyStore:
    """A store that hands back the bodies it is told to, in order."""

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.fetches = 0

    def resolve(self, key):
        return key

    def fetch(self, document_id):
        self.fetches += 1
        return self.bodies[min(self.fetches - 1, len(self.bodies) - 1)]


def test_a_degraded_body_is_retried_and_the_good_one_is_cached():
    store = _FlakyStore(["", "", "the real document"])

    first = fetch_document(store, "sales.orders", sleep=lambda s: None)

    assert first == "the real document"
    assert store.fetches == 3
    assert fetch_document(store, "sales.orders", sleep=lambda s: None) == first
    assert store.fetches == 3                 # the good body came from the cache


def test_a_body_that_never_recovers_is_returned_but_never_cached():
    """The bug the hand-written cache exists to prevent: lru_cache passes every
    test above and then pins a stub for the life of the process, so one bad
    burst poisons every later question."""
    store = _FlakyStore(["short", "a bit longer", "mid"])

    got = fetch_document(store, "sales.orders", min_good_chars=1500, sleep=lambda s: None)

    assert got == "a bit longer"              # the longest seen, not the last
    assert store.fetches == 3
    fetch_document(store, "sales.orders", min_good_chars=1500, sleep=lambda s: None)
    assert store.fetches == 6                 # re-fetched, because nothing good was cached


def test_a_key_the_store_cannot_resolve_gives_nothing_and_never_fetches():
    class _NoSuchDocument:
        fetches = 0

        def resolve(self, key):
            return None

        def fetch(self, document_id):        # pragma: no cover - must not run
            self.fetches += 1
            return "unreachable"

    store = _NoSuchDocument()

    assert fetch_document(store, "sales.missing") == ""
    assert store.fetches == 0


def test_a_steady_store_is_fetched_once_and_never_retried():
    """The common case, and the one a probe of a warm live catalog measured: 60
    sequential reads of four documents came back byte-identical. The retry loop
    must cost one comparison there, not three round trips."""
    store = _FlakyStore(["the real document"])

    assert fetch_document(store, "sales.orders") == "the real document"

    assert store.fetches == 1
