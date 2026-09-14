# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The live catalog, exercised the way the fake cannot be.

Read half: skipped unless CONTEXTLIFT_CATALOG_PROJECT, CONTEXTLIFT_CATALOG_LOCATION
and CONTEXTLIFT_CATALOG_TABLE (a `dataset.table` that is catalogued) are set;
then it needs Application Default Credentials and the [catalog] extra. Run
with `make test-integration`. Free: catalog reads are not billed.

Write half: needs, in addition, CONTEXTLIFT_CATALOG_WRITE=1 and
CONTEXTLIFT_CATALOG_WRITE_TABLE, a sandbox `dataset.table` whose overview may be
overwritten (the previous overview is written back afterwards). It leaves
behind a glossary `contextlift-live-test` with one term, one entry link on the
sandbox table, one query entry in the group `contextlift-live-queries`, and,
when the API takes it, one row in the sandbox table's queries aspect. A
second run finds them and reports "existed" / "updated" / "unchanged". Run
with `-s`: every length the catalog returns is printed, and those numbers are
the reason to run it.
"""
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

PROJECT = os.environ.get("CONTEXTLIFT_CATALOG_PROJECT")
LOCATION = os.environ.get("CONTEXTLIFT_CATALOG_LOCATION")
TABLE = os.environ.get("CONTEXTLIFT_CATALOG_TABLE")
WRITE_TABLE = os.environ.get("CONTEXTLIFT_CATALOG_WRITE_TABLE")
WRITE = os.environ.get("CONTEXTLIFT_CATALOG_WRITE") == "1" and bool(WRITE_TABLE)
pytestmark = pytest.mark.skipif(
    not (PROJECT and LOCATION and TABLE),
    reason="needs CONTEXTLIFT_CATALOG_PROJECT, _LOCATION and _TABLE (a live catalog)")
write_only = pytest.mark.skipif(
    not WRITE, reason="needs CONTEXTLIFT_CATALOG_WRITE=1 and CONTEXTLIFT_CATALOG_WRITE_TABLE (a sandbox table)")


@pytest.fixture(scope="module")
def store():
    from context_lift.catalog import KnowledgeCatalogStore, transport_for
    return KnowledgeCatalogStore(transport_for(PROJECT, LOCATION),
                                 entry_id_pattern="name:{table} type=table",
                                 known_good_table=TABLE)


@pytest.fixture(scope="module")
def sandbox_entry(store):
    entry = store.resolve(WRITE_TABLE)
    assert entry, f"{WRITE_TABLE} did not resolve to an entry"
    print(f"\nsandbox entry: {entry}")
    return entry


def test_a_catalogued_table_resolves_to_one_entry_and_reads_back_a_document(store):
    entry = store.resolve(TABLE)

    assert entry and TABLE.rpartition(".")[2] in entry
    body = store.document_for(TABLE)
    assert len(body) >= store.min_good_chars
    assert "overview:" in body or "schema:" in body


def test_a_table_that_is_not_catalogued_reads_as_nothing(store):
    # on the built-name path resolve is a pure function of the key, so it
    # names an entry for any table; the catalog says "nothing" at the read,
    # with no error, and that is the "nothing found" line
    assert store.resolve("no_such_dataset.no_such_table_xyz")
    assert store.document_for("no_such_dataset.no_such_table_xyz") == ""


def test_a_burst_of_concurrent_reads_returns_no_stub(store):
    """The condition the field notes name and the fake only imitates: 40
    reads at once. Every body must be a real document, none a stub."""
    with ThreadPoolExecutor(max_workers=20) as pool:
        bodies = list(pool.map(lambda _: store.transport.lookup(store.resolve(TABLE), store.context_budget),
                               range(40)))

    assert min(len(b) for b in bodies) >= store.min_good_chars
    assert len(set(bodies)) == 1              # byte-identical, as the steady-state probe found


# ---- the write half ---------------------------------------------------------------


def _read_until(store, entry, wanted, attempts=6):
    """`read_now` with the fetch_document backoff (2 s, then 4, 6, 8, 10)
    until `wanted` appears; (body, attempt) or (last body, None). Prints
    every length: the indexing lag is the number nobody has measured."""
    body = ""
    for attempt in range(attempts):
        if attempt:
            time.sleep(2.0 * attempt)
        body = store.read_now(entry)
        print(f"  read_now attempt {attempt + 1}: {len(body)} chars, "
              f"{'present' if wanted in body else 'absent'}")
        if wanted in body:
            return body, attempt + 1
    return body, None


@write_only
def test_list_tables_holds_the_sandbox_table_and_every_key_has_a_dot(store):
    keys, counts = store.list_tables("@bigquery")

    print(f"\nlist_tables(@bigquery): {counts}")
    assert WRITE_TABLE in keys
    assert all("." in key for key in keys)


@write_only
def test_an_overview_round_trip_reads_back_the_marker(store, sandbox_entry):
    """Write a page with a marker, compare the stored length with the written
    one, then read through lookupContext until the marker shows. The previous
    overview goes back in a finally, whatever happens in between."""
    previous = store.stored_overview(sandbox_entry)
    print(f"stored overview before: {None if previous is None else len(previous)} chars")
    marker = f"contextlift-live-{uuid.uuid4().hex[:12]}"
    page = ("<p>Page written by the contextlift live test; the previous overview is restored afterwards.</p>\n"
            f"<p>marker {marker}</p>\n"
            + "<p>Filler so the page has the size of a real one.</p>\n" * 40)
    try:
        store.write_overview(sandbox_entry, page)
        stored = store.stored_overview(sandbox_entry)
        print(f"written {len(page)} chars, stored {None if stored is None else len(stored)} chars")
        assert stored == page                      # the read-back rule: same bytes, same length
        body, attempt = _read_until(store, sandbox_entry, marker)
        print(f"marker in lookupContext: {'attempt ' + str(attempt) if attempt else 'not within 6 attempts'}, "
              f"{len(body)} chars")
        assert attempt is not None
    finally:
        what = _restore_overview(store, sandbox_entry, previous)
        after = store.stored_overview(sandbox_entry)
        print(f"overview {what}: {None if after is None else len(after)} chars stored")


@write_only
def test_a_term_is_upserted_and_linked_and_the_bundle_may_or_may_not_name_it(store, sandbox_entry):
    """Whether a linked term's name reaches lookupContext is an open question;
    this records the answer rather than asserting one."""
    transport = store.transport

    term_entry, status = transport.upsert_term(
        "contextlift-live-test", "contextlift-live-term", "Contextlift live term",
        "A term written by the contextlift live test; safe to delete.", {"source": "contextlift-live-test"})
    print(f"\nterm {status}: {term_entry}")
    linked = transport.link_term(sandbox_entry, term_entry, "contextlift-live-link")
    print(f"link {linked}")
    body, attempt = _read_until(store, sandbox_entry, "Contextlift live term")
    print(f"term name in lookupContext: {'yes, attempt ' + str(attempt) if attempt else 'no, not within 6 attempts'}, "
          f"{len(body)} chars")

    assert status in ("created", "updated", "unchanged")
    assert linked in ("created", "existed")


@write_only
def test_a_query_entry_upserted_twice_is_created_then_updated(store):
    transport = store.transport
    sql = f"SELECT count(*) FROM `{WRITE_TABLE}`"
    question = "How many rows does the sandbox table hold? (contextlift live test)"

    first = transport.upsert_query_entry("contextlift-live-queries", "contextlift-live-q1", question, sql,
                                         {"source": "contextlift-live-test"})
    second = transport.upsert_query_entry("contextlift-live-queries", "contextlift-live-q1", question, sql,
                                          {"source": "contextlift-live-test"})
    stored = transport.read_entry(first[0])
    print(f"\nquery entry {first[1]} then {second[1]}: {first[0]}")
    print(f"query entry stored: display name {len(stored['display_name'])} chars, "
          f"description {len(stored['description'])} chars")

    assert first[1] in ("created", "updated")     # a previous run leaves the entry behind
    assert second[1] == "updated"
    assert stored["description"] == sql


@write_only
def test_the_queries_aspect_takes_or_refuses_a_user_write(store, sandbox_entry):
    """Records supported or unsupported; runs after the overview round trip so
    a 403 here, with the same credentials that wrote the overview, is the
    product refusing and not a missing role."""
    from context_lift.catalog import WriteUnsupported
    transport = store.transport
    sql = f"SELECT count(*) FROM `{WRITE_TABLE}`"
    before = transport.read_entry(sandbox_entry)["queries"]
    print(f"\nqueries aspect before: {'absent' if before is None else str(len(before)) + ' rows'}")

    try:
        transport.write_queries(sandbox_entry, [
            {"sql": sql, "question": "How many rows does the sandbox table hold? (contextlift live test)"}])
    except WriteUnsupported as refused:
        print(f"queries aspect: UNSUPPORTED ({refused})")
        return
    except SystemExit as denied:
        print(f"queries aspect: PERMISSION DENIED ({denied})")
        return
    after = transport.read_entry(sandbox_entry)["queries"] or []
    print(f"queries aspect: SUPPORTED, {len(after)} rows stored")

    assert any(row.get("sql") == sql for row in after)


def _restore_overview(store, entry, previous):
    """Put the entry back the way it was found, including having no overview.

    Writing a placeholder page instead leaves the test's own words on the
    entry, and the evaluation room reads that entry to build an arm. A leftover
    "no overview existed" page would then be authored context in the next
    round, which is the test contaminating the measurement it exists to
    protect.
    """
    if previous is not None:
        store.write_overview(entry, previous)
        return "restored"
    from context_lift.catalog import OVERVIEW_ASPECT_KEY
    store.transport.remove_aspect(entry, OVERVIEW_ASPECT_KEY)
    return "removed (there was none before)"


@pytest.fixture(scope="module")
def round_store():
    """The store a round builds: the entry name comes from a template.

    The `store` fixture above resolves by searching, and search returned
    nothing on the project this was first run against, so the two paths are
    not interchangeable. A round never searches, so the export test must not
    either, or it proves the wrong path.
    """
    from context_lift.catalog import KnowledgeCatalogStore, transport_for
    return KnowledgeCatalogStore(transport_for(PROJECT, LOCATION),
                                 entry_group="@bigquery")


@write_only
def test_the_catalog_export_builds_the_same_arm_as_the_same_words_in_files(round_store):
    """The byte-identical claim, against a real catalog instead of a fake.

    Until this ran, `read_catalog` had only ever been handed a fake with
    `resolve` and `read_entry` on it, and the live store had no `read_entry`
    at all: catalog mode passed its test and could not run once. This writes a
    page to the sandbox entry, reads it back through the same reader a round
    uses, and asserts the arm hashes match.

    They have to match. If the same words hash differently depending on where
    they were read from, a lift measured in file mode says nothing about
    catalog mode, and the ledger records one experiment as two.

    Only the overview is exercised. The queries aspect may refuse a USER write
    (the test above records which), and a round trip that silently skips half
    its content would pass while proving less than it claims.
    """
    import json

    import yaml

    from context_lift.export import build_arms, read_catalog, read_spec
    from context_lift.runners.ca_api import build_inline_context

    description = "One row per order. Written by the contextlift live test."
    rule = "- Revenue excludes cancelled and returned lines."
    entry = round_store.resolve(WRITE_TABLE)
    assert entry, f"{WRITE_TABLE} did not resolve to an entry name"
    print(f"\nentry: {entry}")
    previous = round_store.stored_overview(entry)
    try:
        round_store.write_overview(entry, f"{description}\n\n## Rules\n{rule}")
        from_catalog = build_arms(read_catalog(round_store, [WRITE_TABLE]))[1]
    finally:
        print(f"overview: {_restore_overview(round_store, entry, previous)}")

    # The same words, as a spec repo. The layout mirrors what the reader
    # produces: the description is authored (treatment only), and the rules
    # arrive as one file named for where they came from.
    import tempfile
    import pathlib
    root = pathlib.Path(tempfile.mkdtemp())
    for part in ("rules", "schema", "authored", "vocabulary", "examples"):
        (root / part).mkdir()
    (root / "rules" / "catalog overviews.md").write_text(
        f"{WRITE_TABLE}\n{rule}", encoding="utf-8")
    (root / "schema" / f"{WRITE_TABLE}.yaml").write_text(
        yaml.safe_dump({"description": "", "columns": {}}), encoding="utf-8")
    (root / "authored" / f"{WRITE_TABLE}.yaml").write_text(
        yaml.safe_dump({"description": description}), encoding="utf-8")
    from_files = build_arms(read_spec(root))[1]

    print(f"\ncatalog arm {from_catalog.arm_id}, file arm {from_files.arm_id}")
    assert (json.dumps(build_inline_context(from_files), sort_keys=True)
            == json.dumps(build_inline_context(from_catalog), sort_keys=True))
    assert from_catalog.arm_id == from_files.arm_id
