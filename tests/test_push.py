# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The push: every write read back, every short read named by cause."""
import json
import os
import subprocess
import sys

import pytest

from context_lift.draft import DRAFT_MARK
from context_lift.push import (PushAlreadyRunning, PushResult, STAMP_BEGIN, STAMP_END, entry_group_of,
                           examples_in_bank, format_summary, holdout_guard, page_record, pages_in,
                           project_number_of, push, query_entry_id, slug, stamp, stamp_of,
                           take_push_lock, term_lines, unstamped, verify_readback, verify_written,
                           write_manifest)

ORDERS_ENTRY = "projects/123456/locations/us/entryGroups/shop/entries/shop__orders"
CUSTOMERS_ENTRY = "projects/123456/locations/us/entryGroups/shop/entries/shop__customers"
GLOSSARY = "projects/123456/locations/us/glossaries/shop"
QUERY_GROUP = "projects/123456/locations/us/entryGroups/verified-queries"

ORDERS_PAGE = """# shop.orders

One row per order.

## Rules

- "net revenue" -> amount minus refund, from shop.orders
- every query filters status = 'complete'

## Columns

| column | type | description |
|---|---|---|
| order_id | INT64 | the key |
| amount | NUMERIC | the money |
"""
CUSTOMERS_PAGE = "# shop.customers\n\nOne row per customer.\n\n## Rules\n\n## Columns\n\n| customer_id | INT64 | |\n"


class WriteUnsupported(RuntimeError):
    """Named like the transport's exception on purpose: push catches it by
    class name, without importing the transport."""


class _FakeTransport:
    def __init__(self, store):
        self.store = store
        self.queries = {}         # entry name -> [{"sql", "question"}]
        self.terms = {}           # term entry name -> record
        self.links = {}           # link id -> (source entry, term entry)
        self.query_entries = {}   # entry name -> record

    def write_queries(self, entry_name, queries):
        if self.store.queries_unsupported:
            raise WriteUnsupported("no queries aspect on this deployment")
        self.queries[entry_name] = list(queries)

    def upsert_term(self, glossary_id, term_id, display_name, description, labels):
        name = f"{glossary_id}/terms/{term_id}"
        record = {"name": name, "display_name": display_name, "description": description,
                  "labels": labels, "overview": "", "queries": []}
        state = ("created" if name not in self.terms
                 else "unchanged" if self.terms[name] == record else "updated")
        self.terms[name] = record
        return name, state

    def link_term(self, source_entry, term_entry, link_id):
        if link_id in self.links:
            return "existed"
        self.links[link_id] = (source_entry, term_entry)
        return "created"

    def upsert_query_entry(self, group_id, entry_id, question, sql, labels):
        name = f"{group_id}/entries/{entry_id}"
        state = "created" if name not in self.query_entries else "updated"
        self.query_entries[name] = {"name": name, "display_name": question, "description": sql,
                                    "labels": labels, "overview": "", "queries": []}
        return name, state

    def read_entry(self, entry_name):
        return (self.terms.get(entry_name) or self.query_entries.get(entry_name)
                or {"name": entry_name, "overview": self.store.overviews.get(entry_name, ""),
                    "queries": self.queries.get(entry_name, []),
                    "display_name": "", "description": "", "labels": {}})


class _FakeStore:
    """A catalog with knobs for the failures the live one showed: store_ratio
    (the share of written bytes the aspect keeps), stub_reads_after_write
    (reads that return nothing before the page shows), read_cap (a reader
    budget), stub_below_budget (a page longer than context_budget served as
    that many characters of its opening, with no error, however often it is
    read), queries_unsupported (no queries aspect type)."""

    def __init__(self, entries=None, *, store_ratio=1.0, stub_reads_after_write=0,
                 read_cap=None, stub_below_budget=None, queries_unsupported=False,
                 min_good_chars=10):
        self.entries = dict(entries) if entries is not None else {
            "shop.orders": ORDERS_ENTRY, "shop.customers": CUSTOMERS_ENTRY}
        self.overviews = {}
        self.store_ratio = store_ratio
        self.stub_reads_after_write = stub_reads_after_write
        self.read_cap = read_cap
        self.stub_below_budget = stub_below_budget
        self.queries_unsupported = queries_unsupported
        self.min_good_chars, self.context_budget = min_good_chars, 60000
        self.transport = _FakeTransport(self)
        self.writes, self.reads, self.sleeps = 0, 0, []
        self._stubs_left = {}

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def resolve(self, table):
        return self.entries.get(table)

    def stored_overview(self, entry_name):
        return self.overviews.get(entry_name)

    def write_overview(self, entry_name, text):
        self.writes += 1
        self.overviews[entry_name] = text[: int(len(text) * self.store_ratio)]
        self._stubs_left[entry_name] = self.stub_reads_after_write

    def read_now(self, entry_name):
        self.reads += 1
        if self._stubs_left.get(entry_name, 0) > 0:
            self._stubs_left[entry_name] -= 1
            return ""
        parts = [self.overviews.get(entry_name, "")]
        parts += [q["question"] for q in self.transport.queries.get(entry_name, [])]
        parts += [self.transport.terms[term]["display_name"]
                  for source, term in self.transport.links.values() if source == entry_name]
        body = "\n".join(parts)
        if self.stub_below_budget is not None and self.context_budget <= len(self.overviews.get(entry_name, "")):
            return body[: self.stub_below_budget]     # whole or not at all, at the budget given
        return body if self.read_cap is None else body[: self.read_cap]


# ---- pages on disk ---------------------------------------------------------

def test_pages_in_reads_both_filename_shapes_and_counts_the_bare_ones(tmp_path):
    (tmp_path / "shop.orders.md").write_text("# shop.orders\n")
    (tmp_path / "shop__customers.md").write_text("# shop.customers\n")
    (tmp_path / "emp.md").write_text("# emp\n")
    (tmp_path / "README.md").write_text("notes\n")

    pages, bare = pages_in(tmp_path)

    assert pages == {"shop.customers": "# shop.customers\n", "shop.orders": "# shop.orders\n"}
    assert bare == 2
    assert pages_in(tmp_path, tables=["shop.orders"]) == ({"shop.orders": "# shop.orders\n"}, 2)


# ---- the stamp -------------------------------------------------------------

def test_stamp_wraps_the_page_in_markers_that_stamp_of_reads_back():
    sha12, stamped = stamp("# shop.orders\n\nOne row per order.\n")

    assert stamped.splitlines()[0] == f"<!-- contextlift catalog-push {sha12} begin -->"
    assert stamped.splitlines()[-1] == f"<!-- contextlift catalog-push {sha12} end -->"
    assert stamped.splitlines()[1:-1] == ["# shop.orders", "", "One row per order."]
    assert stamp_of(stamped) == sha12
    assert stamp_of("Old hand-written overview.") is None
    assert stamp_of(None) is None
    assert unstamped(stamped) == "# shop.orders\n\nOne row per order."
    assert stamp(stamped) == (sha12, stamped)         # a pulled page pushed again does not nest


# ---- reading back ----------------------------------------------------------

def test_verify_readback_names_each_of_the_five_outcomes():
    sha12, stamped = stamp("# shop.orders\n\nOne row per order, one line per row.\n")
    begin, end = STAMP_BEGIN.format(sha=sha12), STAMP_END.format(sha=sha12)

    assert verify_readback(stamped, stamped, 10) == "verified"
    assert verify_readback(stamped, "", 10) == "stub"
    assert verify_readback(stamped, "overview: the previous page, long enough", 10) == "missing"
    assert verify_readback(stamped, f"{begin}\n# shop.orders\n\nOne row per", 10) == "truncated"
    assert verify_readback(stamped, f"{begin}\n# shop.orders\n{end}", 10) == "truncated"
    assert verify_readback(stamped, f"{begin}\n# shop.orders\n\nOne row per order, one line per row.\n"
                           + "the bundle goes on " * 10, 10) == "unverified"


def test_a_read_that_folded_long_lines_still_verifies():
    sha12, stamped = stamp("# shop.orders\n\nOne row per order, with a description long enough to fold.\n")
    folded = ("name: " + ORDERS_ENTRY + "\noverview: |\n"
              f"  <!-- contextlift catalog-push {sha12}\n    begin -->\n"
              "  # shop.orders\n\n  One row per order, with a description\n"
              "    long enough to fold.\n"
              f"  <!-- contextlift catalog-push {sha12} end -->\n")

    assert verify_readback(stamped, folded, 10) == "verified"


def _double_quoted_bundle(stamped: str, fold_at: int) -> str:
    """The bundle one deployment serves for a custom group's entry: a
    double-quoted YAML scalar, newlines as the two characters `\\n`, folded
    once at `fold_at` with the escaped line break (`\\` + newline + 8
    spaces), which stands for no character. A space at the fold moves to
    the next line as `\\ `, the escaped space, verbatim from that estate:
    `<!-- contextlift\\` newline `    \\ catalog-push ... end -->`."""
    body = stamped.replace("\n", "\\n")
    if body[fold_at] == " ":
        folded = body[:fold_at] + "\\\n        \\ " + body[fold_at + 1:]
    else:
        folded = body[:fold_at] + "\\\n        " + body[fold_at:]
    return f'resources:\n- name: {ORDERS_ENTRY}\n  overview: "{folded}"\n'


def test_an_escaped_line_break_anywhere_inside_the_end_marker_still_verifies():
    """22 of 99 pages on one deployment: the fold landed inside the end
    marker, and collapsed as whitespace the marker no longer matched, so
    each was called truncated with a served text longer than the page."""
    sha12, stamped = stamp(ORDERS_PAGE)
    end = STAMP_END.format(sha=sha12)
    end_at = stamped.replace("\n", "\\n").index(end)

    for fold_at in range(end_at + 1, end_at + len(end)):
        assert verify_readback(stamped, _double_quoted_bundle(stamped, fold_at), 10) == "verified", fold_at


def test_an_escaped_line_break_anywhere_inside_the_begin_marker_still_verifies():
    sha12, stamped = stamp(ORDERS_PAGE)

    for fold_at in range(1, len(STAMP_BEGIN.format(sha=sha12))):
        assert verify_readback(stamped, _double_quoted_bundle(stamped, fold_at), 10) == "verified", fold_at


def test_a_short_page_is_not_a_stub_forever_under_a_large_threshold():
    _, stamped = stamp("# t\n")

    assert verify_readback(stamped, stamped, 1500) == "verified"


def _verify_one(store, entry_name, stamped, attempts):
    """One page through the read-back phase; the record it filled."""
    record = page_record("shop.orders", ORDERS_PAGE, entry_name)
    verify_written(store, [(record, stamped)], attempts=attempts)
    return record


def test_the_read_back_retries_through_the_stubs_and_records_every_length():
    store = _FakeStore(stub_reads_after_write=2)
    _, stamped = stamp(ORDERS_PAGE)
    store.write_overview(ORDERS_ENTRY, stamped)

    seen = _verify_one(store, ORDERS_ENTRY, stamped, attempts=4)

    assert (seen["verify"], seen["attempts"], seen["readback_chars"]) == ("verified", 3, [0, 0, len(stamped)])
    assert seen["served_minus_written"] == 0 and seen["cause"] is None
    assert store.sleeps == [2.0, 4.0]


def test_a_store_that_kept_fewer_bytes_than_sent_is_the_named_cause():
    store = _FakeStore(store_ratio=0.6)
    _, stamped = stamp("a")
    store.write_overview(ORDERS_ENTRY, stamped)

    seen = _verify_one(store, ORDERS_ENTRY, stamped, attempts=2)

    assert seen["verify"] == "truncated"
    assert seen["cause"] == (f"stored {int(len(stamped) * 0.6)} of "
                             f"{len(stamped)}")
    kept = int(len(stamped) * 0.6)
    assert seen["readback_chars"] == [kept, kept]


def test_a_reader_budget_that_cuts_the_tail_is_told_apart_from_the_store():
    store = _FakeStore(read_cap=60)
    _, stamped = stamp("a")
    store.write_overview(ORDERS_ENTRY, stamped)

    seen = _verify_one(store, ORDERS_ENTRY, stamped, attempts=2)

    assert seen["verify"] == "truncated"
    # 60 is the reader's fixed BUDGET, not a share of the length, so only
    # the total moves when the stamped text changes.
    assert seen["cause"] == (f"reader sees 60 of {len(stamped)}; "
                             "the aspect holds all of it")


def test_a_stub_that_only_the_read_budget_causes_says_the_write_landed_whole():
    """A page longer than the read budget is served as a few hundred
    characters of its opening, with no error, so a write that was fine reads
    back as a stub on every pass. Sent to fix that write, a person finds
    nothing wrong with it. The cause names the stored size beside the served
    one and a budget that would serve the page."""
    store = _FakeStore(stub_below_budget=572, min_good_chars=1500)
    _, stamped = stamp("# shop.orders\n\n" + "one row per order. " * 4000)
    store.write_overview(ORDERS_ENTRY, stamped)
    assert len(stamped) > store.context_budget

    seen = _verify_one(store, ORDERS_ENTRY, stamped, attempts=2)

    assert seen["verify"] == "stub"                    # what a reader really sees
    assert seen["readback_chars"] == [572, 572]        # reading again never clears it
    assert seen["cause"] == (f"the entry stores {len(stamped)} characters and the reader was served "
                             "572 at budget 60000; the write landed whole, raise --budget to 100000")


def test_a_stub_of_nothing_at_all_is_the_lag_after_the_write_not_the_budget():
    """0 characters and a whole aspect is the indexing lag, which clears
    itself; a few hundred characters and a whole aspect is the budget, which
    does not. Same verdict, different thing to do."""
    store = _FakeStore(stub_reads_after_write=9, min_good_chars=1500)
    _, stamped = stamp(ORDERS_PAGE)
    store.write_overview(ORDERS_ENTRY, stamped)

    seen = _verify_one(store, ORDERS_ENTRY, stamped, attempts=2)

    assert (seen["verify"], seen["readback_chars"]) == ("stub", [0, 0])
    assert seen["cause"] == (f"the entry stores {len(stamped)} characters and the reader sees "
                             "nothing yet; the catalog makes a write readable seconds later, so "
                             "re-read with --verify-only")


def test_a_stub_over_a_write_that_lost_bytes_still_names_both_lengths():
    store = _FakeStore(store_ratio=0.5, min_good_chars=1500)
    _, stamped = stamp("a" * 1000)
    store.write_overview(ORDERS_ENTRY, stamped)

    seen = _verify_one(store, ORDERS_ENTRY, stamped, attempts=1)

    assert seen["verify"] == "stub"
    assert seen["cause"] == (f"stored {int(len(stamped) * store.store_ratio)} of {len(stamped)}; "
                             f"the reader was served "
                             f"{int(len(stamped) * store.store_ratio)} at "
                             "budget 60000")


def test_a_longer_served_text_with_the_end_marker_absent_is_unverified_not_truncated():
    """The old cause read `reader sees 16397 of 14659`: a truncation by a
    reader that had served MORE than the page."""
    store = _FakeStore()
    sha12, stamped = stamp(ORDERS_PAGE)
    store.write_overview(ORDERS_ENTRY, stamped)
    store.overviews[ORDERS_ENTRY] = stamped.replace(STAMP_END.format(sha=sha12), "the bundle goes on " * 10)
    served = len(store.overviews[ORDERS_ENTRY])
    assert served > len(stamped)

    seen = _verify_one(store, ORDERS_ENTRY, stamped, attempts=2)

    assert seen["verify"] == "unverified"
    assert seen["cause"] == f"end marker not found in the served text (served {served} characters for {len(stamped)} written)"
    assert "served_minus_written" not in seen


# ---- pages through push ----------------------------------------------------

def test_a_page_is_written_once_and_unchanged_on_the_second_push():
    store = _FakeStore()

    first = push(store, pages={"shop.orders": ORDERS_PAGE})
    second = push(store, pages={"shop.orders": ORDERS_PAGE})

    assert [e["status"] for e in first.entries] == ["written"]
    assert first.entries[0]["verify"] == "verified"
    assert first.entries[0]["entry_name"] == ORDERS_ENTRY
    assert first.counts["written"] == 1 and first.counts["verified"] == 1
    assert [e["status"] for e in second.entries] == ["unchanged"]
    assert second.counts["unchanged"] == 1 and second.counts["written"] == 0
    assert store.writes == 1


def test_pages_are_written_first_then_read_back_together_with_one_wait_per_pass():
    """113 pages took about 90 minutes when each page was read to exhaustion
    before the next was written: four slow reads and 12 s of sleep per
    entry while the served copy lagged the write by more than the backoff."""
    store = _FakeStore(stub_reads_after_write=1)
    lines = []

    result = push(store, pages={"shop.orders": ORDERS_PAGE, "shop.customers": CUSTOMERS_PAGE},
                  progress=lines.append)

    assert [e["verify"] for e in result.entries] == ["verified", "verified"]
    assert [e["attempts"] for e in result.entries] == [2, 2]
    assert store.sleeps == [2.0]                      # one wait for both pages, not one per page
    assert [e["served_minus_written"] for e in result.entries] == [0, 0]
    assert lines == [f"[resolve 1/2] shop.customers -> {CUSTOMERS_ENTRY}",
                     f"[resolve 2/2] shop.orders -> {ORDERS_ENTRY}",
                     "[1/2] shop.customers written", "[2/2] shop.orders written",
                     "[verify 1/2] shop.customers not yet (stub), retry",
                     "[verify 2/2] shop.orders not yet (stub), retry",
                     "pass 2: 2 pages to re-read after 2 s",
                     "[verify 1/2] shop.customers verified in 2 reads",
                     "[verify 2/2] shop.orders verified in 2 reads"]


def test_content_contextlift_did_not_write_is_replaced_and_its_bytes_kept(tmp_path):
    store = _FakeStore()
    store.overviews[ORDERS_ENTRY] = "Old hand-written overview."

    result = push(store, pages={"shop.orders": ORDERS_PAGE}, previous_out=tmp_path / "previous")

    assert result.entries[0]["status"] == "written"
    assert result.entries[0]["previous_chars"] == 26
    assert result.counts["replaced_unstamped"] == 1
    assert (tmp_path / "previous" / "shop.orders.md").read_text() == "Old hand-written overview."
    assert stamp_of(store.overviews[ORDERS_ENTRY]) == stamp(ORDERS_PAGE)[0]


def test_a_page_still_marked_as_a_draft_is_refused_unless_allowed():
    store = _FakeStore()
    draft = f"# shop.orders\n\n{DRAFT_MARK}\n\n## Columns\n"

    refused = push(store, pages={"shop.orders": draft})
    allowed = push(store, pages={"shop.orders": draft}, allow_draft=True)

    assert refused.entries[0]["status"] == "draft, not pushed"
    assert refused.counts["drafts_skipped"] == 1 and refused.counts["failed"] == 0
    assert allowed.entries[0]["status"] == "written"
    assert store.writes == 1


def test_a_table_with_no_entry_is_a_failure_not_a_silent_skip():
    result = push(_FakeStore(entries={}), pages={"shop.orders": ORDERS_PAGE})

    assert result.entries[0]["status"] == "no entry"
    assert result.counts["failed"] == 1


def test_two_pages_that_resolve_to_one_entry_refuse_the_push_before_any_write():
    """An earlier resolve put 12 of 99 pages onto a sibling table's entry,
    and read-back verified every one: it proves the bytes survived, not
    that they landed on the right table."""
    store = _FakeStore({"shop.product": ORDERS_ENTRY, "shop.product_size": ORDERS_ENTRY,
                        "shop.customers": CUSTOMERS_ENTRY})
    pages = {"shop.product": ORDERS_PAGE, "shop.product_size": ORDERS_PAGE, "shop.customers": CUSTOMERS_PAGE}

    result = push(store, pages=pages)

    assert result.counts["refused"] == "collision" and store.writes == 0
    assert result.counts["collisions"] == [f"shop.product, shop.product_size -> {ORDERS_ENTRY}"]
    assert result.entries == []
    summary = format_summary(result)
    assert "  refused: more than one page resolves to the same entry (1 such entries" in summary
    assert summary.splitlines()[-1] == f"    shop.product, shop.product_size -> {ORDERS_ENTRY}"


def test_the_collision_check_cannot_fire_under_a_template_and_still_fires_on_the_search_path():
    """Two spellings of one key: built names differ (two entries, distinct
    keys and distinct names), searched names meet on the one entry the
    search returns for both."""
    from tests.test_catalog import FakeCatalog
    from context_lift.catalog import KnowledgeCatalogStore
    prefix = "projects/1/locations/us/entryGroups/kb/entries/"
    pages = {"Shop.Orders": ORDERS_PAGE, "shop.orders": ORDERS_PAGE}

    class _SameHit(FakeCatalog):
        def search(self, query):
            return ["entries/shop__orders"]

    built = KnowledgeCatalogStore(FakeCatalog({"shop__orders": "x" * 2000}, name_prefix=prefix),
                                  project="1", location="us", entry_group="kb",
                                  entry_id_template="{dataset}__{table}", sleep=lambda s: None)
    searched = KnowledgeCatalogStore(_SameHit({"shop__orders": "x" * 2000}), entry_group=None,
                                     sleep=lambda s: None)

    by_name = push(built, pages=pages, dry_run=True)
    by_search = push(searched, pages=pages, dry_run=True)

    assert by_name.counts["refused"] is None
    assert [e["entry_name"] for e in by_name.entries] == [prefix + "Shop__Orders", prefix + "shop__orders"]
    assert by_search.counts["refused"] == "collision"
    assert by_search.counts["collisions"] == ["Shop.Orders, shop.orders -> entries/shop__orders"]


def test_a_page_over_the_read_budget_is_pushed_and_listed():
    result = push(_FakeStore(), pages={"shop.orders": ORDERS_PAGE}, read_budget=50)

    assert result.counts["over_budget"] == ["shop.orders"]
    assert result.entries[0]["status"] == "written"
    assert f"1 pages over 50 characters, where a catalog read truncates: shop.orders {len(ORDERS_PAGE)}" in format_summary(result)


# ---- terms and examples out of files ---------------------------------------

def test_term_lines_keeps_quoted_definitions_and_counts_the_rest():
    rules = ("<!-- drafted by contextlift draft-rules from bucket WRONG_TABLE, 3 questions -->\n\n"
             '- "net revenue" -> amount minus refund, from shop.orders   <!-- q1, q3 -->\n'
             '- "active customer" -> a customer with an order in the last 90 days   <!-- q2 -->\n'
             "- every query filters status = 'complete'   <!-- q1 -->\n")

    terms, skipped = term_lines(rules, ["shop.orders", "shop.customers"])

    assert terms == [
        {"term_id": "net-revenue", "display_name": "net revenue",
         "description": "amount minus refund, from shop.orders", "table": "shop.orders"},
        {"term_id": "active-customer", "display_name": "active customer",
         "description": "a customer with an order in the last 90 days", "table": None}]
    assert skipped == 1


def test_slug_is_lowercase_hyphenated_and_capped_at_63():
    assert slug("Net Revenue (USD)") == "net-revenue-usd"
    assert slug("--Gross_Margin %--") == "gross-margin"
    assert slug("a" * 70) == "a" * 63
    assert slug("a" * 62 + "-b") == "a" * 62          # the cap never leaves a trailing hyphen


def test_examples_in_bank_dedupes_across_blocks_and_ids_are_stable():
    bank = {"q2": ("VERIFIED EXAMPLES (similar questions on this database, with correct SQL):\n\n"
                   "Q: how many orders\nSQL: SELECT COUNT(*)   FROM shop.orders"),
            "q1": ("VERIFIED EXAMPLES (similar questions on this database, with correct SQL):\n\n"
                   "Q: how many orders\nSQL: SELECT COUNT(*) FROM shop.orders\n\n"
                   "Q: total amount\nSQL: SELECT SUM(amount)\nFROM shop.orders")}

    examples = examples_in_bank(bank)

    assert examples == [
        {"question": "how many orders", "sql": "SELECT COUNT(*) FROM shop.orders", "table": "shop.orders"},
        {"question": "total amount", "sql": "SELECT SUM(amount)\nFROM shop.orders", "table": "shop.orders"}]
    one = query_entry_id("how many orders", "SELECT COUNT(*) FROM shop.orders")
    assert one == query_entry_id(" How many orders ", "SELECT COUNT(*)\n   FROM shop.orders;")
    assert one.startswith("vq-") and len(one) == 19
    assert one != query_entry_id("how many customers", "SELECT COUNT(*) FROM shop.orders")


def test_entry_names_give_up_their_project_number_and_group():
    assert project_number_of(ORDERS_ENTRY) == "123456"
    assert project_number_of("projects/my-project-id/locations/us") is None
    assert entry_group_of(ORDERS_ENTRY) == "projects/123456/locations/us/entryGroups/shop"


# ---- examples and terms through push --------------------------------------

def test_examples_go_on_the_table_entry_and_are_verified_through_the_reader():
    store = _FakeStore()
    examples = [{"question": "how many orders", "sql": "SELECT COUNT(*) FROM shop.orders",
                 "table": "shop.orders"}]

    result = push(store, pages={"shop.orders": ORDERS_PAGE}, examples=examples,
                  query_group=QUERY_GROUP)

    example = [e for e in result.entries if e["kind"] == "example"][0]
    assert example["status"] == "written" and example["verify"] == "verified"
    assert example["entry_name"] == ORDERS_ENTRY
    assert result.counts["examples_home"] == "queries-aspect"
    assert store.transport.queries[ORDERS_ENTRY] == [
        {"sql": "SELECT COUNT(*) FROM shop.orders", "question": "how many orders"}]
    assert store.transport.query_entries == {}


def test_examples_fall_back_to_query_entries_when_the_aspect_write_is_unsupported():
    store = _FakeStore(queries_unsupported=True)
    examples = [{"question": "how many orders", "sql": "SELECT COUNT(*) FROM shop.orders",
                 "table": "shop.orders"}]

    result = push(store, pages={"shop.orders": ORDERS_PAGE}, examples=examples,
                  query_group=QUERY_GROUP)

    example = [e for e in result.entries if e["kind"] == "example"][0]
    assert result.counts["examples_home"] == "verified-query-entries"
    assert example["entry_name"] == f"{QUERY_GROUP}/entries/{example['entry_id']}"
    assert example["status"] == "written" and example["verify"] == "verified"
    assert store.transport.queries == {}


def test_a_term_naming_a_known_table_is_linked_to_it_and_seen_through_the_reader():
    store = _FakeStore()
    terms, _ = term_lines('- "net revenue" -> amount minus refund, from shop.orders\n'
                          '- "active customer" -> ordered in the last 90 days\n',
                          ["shop.orders"])

    result = push(store, pages={"shop.orders": ORDERS_PAGE}, terms=terms, glossary_id=GLOSSARY)

    linked, unlinked = [e for e in result.entries if e["kind"] == "term"]
    assert linked["linked"] is True and linked["verify_via"] == "read_entry"
    assert linked["link"] == "created" and linked["verify"] == "verified"
    assert linked["in_lookup"] is True          # the fake's bundle lists linked terms; a live estate did not
    assert unlinked["linked"] is False and unlinked["verify_via"] == "read_entry"
    assert unlinked["verify"] == "verified"
    assert result.counts["linked"] == 1
    assert list(store.transport.links.values()) == [(ORDERS_ENTRY, f"{GLOSSARY}/terms/net-revenue")]


def test_a_term_the_catalog_cannot_read_yet_is_retried_and_then_unverified():
    """A live push wrote and verified 13 pages, then died with a 404 on the
    read-back of its first new term: a term is not readable for a few
    seconds after it is created. Each re-run got one term further, and it
    took 8 runs to exit 0."""
    from tests.test_catalog import FakeCatalog
    from context_lift.catalog import KnowledgeCatalogStore
    catalog = FakeCatalog({"shop__orders": "x" * 2000}, term_reads_not_found=9)
    store = KnowledgeCatalogStore(catalog, entry_group=None, entry_id_pattern="{dataset}__{table}",
                                  sleep=lambda seconds: None)
    terms, _ = term_lines('- "net revenue" -> amount minus refund, from shop.orders\n', ["shop.orders"])

    result = push(store, pages={"shop.orders": ORDERS_PAGE}, terms=terms, glossary_id="shop",
                  attempts=4)

    term = [e for e in result.entries if e["kind"] == "term"][0]
    assert term["status"] == "written" and term["verify"] == "unverified"
    assert term["attempts"] == 4                      # the same 2, 4, 6 second backoff the pages use
    assert term["cause"] == ("the catalog still answers not found 4 reads after the write; "
                             "it makes a new entry readable a few seconds later")
    page = [e for e in result.entries if e["kind"] == "page"][0]
    assert page["verify"] == "verified"               # the pages before it are not lost
    assert result.counts["failed"] == 1               # the command exits 1, not with a traceback
    assert ("  1 written and not verified yet; the catalog makes a write readable seconds later: "
            "--verify-only re-reads the pages, and the same command again re-reads the terms"
            ) in format_summary(result)


def test_a_term_that_becomes_readable_on_the_second_try_is_verified():
    from tests.test_catalog import FakeCatalog
    from context_lift.catalog import KnowledgeCatalogStore
    catalog = FakeCatalog({"shop__orders": "x" * 2000}, term_reads_not_found=1)
    store = KnowledgeCatalogStore(catalog, entry_group=None, entry_id_pattern="{dataset}__{table}",
                                  sleep=lambda seconds: None)
    terms, _ = term_lines('- "net revenue" -> amount minus refund, from shop.orders\n', ["shop.orders"])

    result = push(store, pages={"shop.orders": ORDERS_PAGE}, terms=terms, glossary_id="shop",
                  attempts=4)

    term = [e for e in result.entries if e["kind"] == "term"][0]
    assert (term["verify"], term["attempts"], term["cause"]) == ("verified", 2, None)
    assert result.counts["failed"] == 0


def test_the_summary_says_why_each_term_was_not_linked():
    """A live push printed "terms: written 8 ... (0 linked)" and no reason;
    every meaning had named a bare table name."""
    store = _FakeStore()
    terms, _ = term_lines('- "net revenue" -> amount minus refund, from shop.orders\n'
                          '- "active customer" -> ordered in the last 90 days\n'
                          '- "gross" -> the total on gone.table before refunds\n',
                          ["shop.orders", "gone.table"])

    result = push(store, pages={"shop.orders": ORDERS_PAGE}, terms=terms, glossary_id=GLOSSARY)

    summary = format_summary(result)
    assert "  terms:            written 3  unchanged 0  verified 3  failed 0  (1 linked)" in summary
    assert "    1 not linked: the meaning names no known dataset.table" in summary
    assert "    1 not linked: no catalog entry for gone.table" in summary


def test_a_link_call_that_fails_is_that_terms_reason_and_the_push_goes_on():
    store = _FakeStore()

    def refuse(source_entry, term_entry, link_id):
        raise RuntimeError("500 backend error")

    store.transport.link_term = refuse
    terms, _ = term_lines('- "net revenue" -> amount minus refund, from shop.orders\n', ["shop.orders"])

    result = push(store, pages={"shop.orders": ORDERS_PAGE}, terms=terms, glossary_id=GLOSSARY)

    assert result.counts["linked"] == 0
    assert [e["verify"] for e in result.entries if e["kind"] == "term"] == ["verified"]
    assert ("    1 not linked: the link call failed: RuntimeError: 500 backend error"
            in format_summary(result))


# ---- the guard and the dry run ---------------------------------------------

def test_a_holdout_answer_inside_the_context_refuses_the_whole_push():
    store = _FakeStore()
    holdout = [("h1", "how many orders were completed last month",
                "SELECT COUNT(*) FROM shop.orders WHERE status = 'complete'")]
    leaky = ORDERS_PAGE + "\nExample: SELECT COUNT(*) FROM shop.orders WHERE status = 'complete'\n"

    result = push(store, pages={"shop.orders": leaky}, holdout_rows=holdout)

    assert result.counts["refused"] == "leak"
    assert result.entries == []
    assert store.writes == 0
    assert "LEAKAGE: 1 of 1 golds" in format_summary(result)
    assert holdout_guard(holdout, {"shop.orders": ORDERS_PAGE}, [], []).clean


def test_a_dry_run_touches_nothing_and_says_what_it_would_write():
    store = _FakeStore()
    examples = [{"question": "how many orders", "sql": "SELECT COUNT(*) FROM shop.orders",
                 "table": "shop.orders"}]
    terms = [{"term_id": "net-revenue", "display_name": "net revenue",
              "description": "amount minus refund", "table": "shop.orders"}]

    result = push(store, pages={"shop.orders": ORDERS_PAGE}, examples=examples, terms=terms,
                  glossary_id=GLOSSARY, query_group=QUERY_GROUP, dry_run=True)

    assert [e["status"] for e in result.entries] == ["would write"] * 3
    assert store.writes == 0 and store.reads == 0
    assert store.transport.terms == {} and store.transport.queries == {}
    assert result.counts["written"] == 0 and result.counts["failed"] == 0
    assert "overviews:        would write 1  unchanged 0  verified 0  failed 0" in format_summary(result)


def test_the_lock_refuses_a_live_pid_and_replaces_a_stale_one(tmp_path):
    """Two pushes on one group were started at the same time once."""
    lock = tmp_path / "catalog-push.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "started_at": "2026-09-05T10:00:00"}))

    with pytest.raises(PushAlreadyRunning) as busy:
        take_push_lock(lock, {"pid": os.getpid(), "started_at": "2026-09-05T11:00:00"})

    assert str(busy.value).startswith(
        f"a catalog-push started at 2026-09-05T10:00:00 (pid {os.getpid()}) is still running on this "
        "workspace; two pushes on one group race each other")
    finished = subprocess.Popen([sys.executable, "-c", ""])      # a process that is gone
    finished.wait()
    lock.write_text(json.dumps({"pid": finished.pid, "started_at": "2026-09-05T09:00:00"}))

    note = take_push_lock(lock, {"pid": os.getpid(), "started_at": "2026-09-05T11:00:00"})

    assert note == f"replacing a stale lock: the catalog-push started at 2026-09-05T09:00:00 (pid {finished.pid}) is gone"
    assert json.loads(lock.read_text()) == {"pid": os.getpid(), "started_at": "2026-09-05T11:00:00"}


# ---- output ----------------------------------------------------------------

def test_the_summary_carries_the_counts_and_names_each_failure(tmp_path):
    store = _FakeStore(read_cap=250)          # over the customers page, under the orders page
    store.overviews[CUSTOMERS_ENTRY] = "Old hand-written overview."
    terms = [{"term_id": "active-customer", "display_name": "active customer",
              "description": "ordered in the last 90 days", "table": "shop.customers"}]
    examples = [{"question": "how many customers", "sql": "SELECT COUNT(*) FROM shop.customers",
                 "table": "shop.customers"}]
    holdout = [("h1", "which customers have never placed an order", "SELECT 1")]

    result = push(store, pages={"shop.orders": ORDERS_PAGE, "shop.customers": CUSTOMERS_PAGE},
                  terms=terms, examples=examples, holdout_rows=holdout, glossary_id=GLOSSARY,
                  query_group=QUERY_GROUP, attempts=2, previous_out=tmp_path / "previous",
                  bare_skipped=1)
    summary = format_summary(result)

    assert summary.splitlines()[0] == "catalog-push: 2 pages, 1 terms, 1 worked examples"
    assert "  leak-checked against 1 reserved questions: clean" in summary
    assert ("  overviews:        written 2  unchanged 0  verified 1  failed 1   "
            f"(1 replaced content not written by contextlift; previous bytes in {tmp_path / 'previous'})") in summary
    assert "  terms:            written 1  unchanged 0  verified 1  failed 0  (1 linked)" in summary
    assert "  verified queries: written 1  unchanged 0  verified 1  failed 0  in queries-aspect" in summary
    assert "  1 files skipped: a bare table name has no dataset to find the entry by" in summary
    assert summary.splitlines()[-1] == (
        f"  page shop.orders: written, truncated; reader sees 250 of {len(stamp(ORDERS_PAGE)[1])}; "
        "the aspect holds all of it; read 2 times, saw 250, 250 characters")


def test_the_summary_says_when_nothing_was_leak_checked():
    summary = format_summary(push(_FakeStore(), pages={"shop.orders": ORDERS_PAGE}))

    assert "  not leak-checked: no reserved questions given" in summary
    assert "  overviews:        written 1  unchanged 0  verified 1  failed 0" in summary


def test_the_manifest_round_trips_through_json(tmp_path):
    result = push(_FakeStore(), pages={"shop.orders": ORDERS_PAGE})

    write_manifest(result, tmp_path / "push.json", {"store": "fake", "docs": "docs/"})
    loaded = json.loads((tmp_path / "push.json").read_text())

    assert loaded == {"schema_version": 1, "status": "complete", "meta": {"store": "fake", "docs": "docs/"},
                      "entries": result.entries, "counts": result.counts}
    assert loaded["entries"][0]["content_sha256"] == result.entries[0]["content_sha256"]


def test_the_manifest_merges_the_records_of_the_run_before_it(tmp_path):
    """A push that took 8 runs to finish overwrote one manifest: every run
    but the first said "0 pages written", because each run only records what
    it touched."""
    from context_lift.push import previous_records
    first = push(_FakeStore(), pages={"shop.orders": ORDERS_PAGE, "shop.customers": CUSTOMERS_PAGE})
    write_manifest(first, tmp_path / "push.json", {"docs": "docs/"})

    second = push(_FakeStore(), pages={"shop.customers": CUSTOMERS_PAGE})
    write_manifest(second, tmp_path / "push.json", {"docs": "docs/"},
                   previous=previous_records(tmp_path / "push.json"))

    loaded = json.loads((tmp_path / "push.json").read_text())
    assert [(e["kind"], e["key"], e["status"]) for e in loaded["entries"]] == [
        ("page", "shop.customers", "written"),      # this run's record wins
        ("page", "shop.orders", "written")]         # the earlier run's is kept
    assert loaded["counts"]["written"] == 2 and loaded["counts"]["verified"] == 2
    assert loaded["counts"]["pages"] == 1           # what this run was given, unchanged
    assert previous_records(tmp_path / "gone.json") == []


def test_a_store_without_a_sleeper_still_reads_back(monkeypatch):
    """The live store carries no sleep function; the first real push on a
    customer estate died here after one write."""
    import context_lift.push as push_module
    store = _FakeStore(stub_reads_after_write=1)
    store.sleep = None
    monkeypatch.setattr(push_module.time, "sleep", lambda s: None)

    result = push(store, pages={"shop.orders": ORDERS_PAGE})

    assert result.counts["verified"] == 1 and result.entries[0]["attempts"] == 2


def test_one_page_that_fails_is_its_own_line_and_the_push_goes_on():
    """A push that died mid-loop left 1 of 113 entries changed and no record."""
    store = _FakeStore({"a.one": "entries/a", "b.two": "entries/b", "c.three": "entries/c"})
    real_write = store.write_overview

    def write_or_break(entry_name, text):
        if entry_name == "entries/b":
            raise RuntimeError("500 backend error")
        real_write(entry_name, text)

    store.write_overview = write_or_break
    body = "| x | y |\n" * 40
    pages = {"a.one": "# a\n" + body, "b.two": "# b\n" + body, "c.three": "# c\n" + body}

    result = push(store, pages=pages)

    by_key = {e["key"]: e for e in result.entries}
    assert by_key["b.two"]["verify"] == "failed" and "RuntimeError: 500 backend error" in by_key["b.two"]["cause"]
    assert by_key["a.one"]["status"] == "written" and by_key["c.three"]["status"] == "written"
    assert result.counts["failed"] == 1 and result.counts["written"] == 2


def test_a_stale_entry_whose_read_raises_is_that_pages_failed_line_in_a_dry_run_too():
    """A cold run's dry run died with a gRPC traceback on the first entry
    the catalog no longer held, and the other pages were never planned."""
    from tests.test_catalog import FakeCatalog
    from context_lift.catalog import KnowledgeCatalogStore
    catalog = FakeCatalog({"shop__orders": "x" * 2000, "shop__gone": "y" * 2000},
                          read_raises={"entries/shop__gone"})
    store = KnowledgeCatalogStore(catalog, entry_group=None, entry_id_pattern="{dataset}__{table}",
                                  sleep=lambda s: None)

    result = push(store, pages={"shop.gone": "# shop.gone\n", "shop.orders": ORDERS_PAGE}, dry_run=True)

    by_key = {e["key"]: e for e in result.entries}
    assert by_key["shop.gone"]["status"] == "failed"
    assert by_key["shop.gone"]["cause"] == "RuntimeError: Entry `entries/shop__gone` does not exist."
    assert by_key["shop.orders"]["status"] == "would write"
    assert result.counts["failed"] == 1 and catalog.write_calls == 0
    assert format_summary(result).splitlines()[-1] == (
        "  page shop.gone: failed; RuntimeError: Entry `entries/shop__gone` does not exist.")


def test_a_caller_sink_keeps_what_was_done_when_the_push_dies():
    store = _FakeStore({"a.one": "entries/a", "b.two": "entries/b"})
    real_write = store.write_overview

    def write_or_die(entry_name, text):
        if entry_name == "entries/b":
            raise SystemExit("the catalog client gave up")     # not an Exception: it escapes
        real_write(entry_name, text)

    store.write_overview = write_or_die
    sink = PushResult(entries=[], counts={})
    body = "| x | y |\n" * 40

    try:
        push(store, pages={"a.one": "# a\n" + body, "b.two": "# b\n" + body}, result=sink)
    except SystemExit:
        pass

    # the counts are tallied at the end; the entries are the record when the
    # push dies, and the CLI reports the written ones from them
    assert [e["status"] for e in sink.entries] == ["written", None]   # the second was started, never written
