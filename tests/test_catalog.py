# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Reading documentation out of a catalog, and writing it back, with the network faked.

The fake has three read modes, and only two of them exercise the code that
matters. A probe of a live catalog measured 60 sequential reads of four
documents coming back byte-identical, so the steady mode is the common case
and the retry loop should be invisible there. The degraded bodies appear
immediately after a write and under a burst of concurrent reads. A double
built only from steady-state behaviour would leave the retry rule looking
tested while never running it.

The write side of the fake simulates the three ways a catalog write can look
fine and not be: it can keep fewer bytes than it was given (`store_ratio`),
answer the next reads with nothing (`stub_reads_after_write`), and cut what a
reader gets while the stored aspect is whole (`read_cap`). Each is a field
observation, and each is visible only by reading back and comparing lengths.
"""
import pytest

from context_lift.catalog import (KnowledgeCatalogStore, ScopeCheckFailed, WriteUnsupported,
                              budget_that_serves, compact_sql, entry_group_of, merge_queries,
                              project_number_of, short_serve_note, term_entry_name)


class FakeCatalog:
    """A catalog that can be steady, freshly written, or under burst load,
    and that takes writes.

    Read modes:
    `mode="steady"`        the same body every call
    `mode="just_written"`  empty for `empty_calls` calls, then the document
    `mode="burst"`         every `stub_every`-th call returns a stub

    Write knobs:
    `store_ratio`             keeps that share of a written overview (a store
                              that accepts the bytes and keeps fewer)
    `stub_reads_after_write`  that many reads of the entry answer "" after
                              each write (the indexing lag the field notes saw)
    `term_reads_not_found`    that many read_entry calls of a freshly created
                              term raise not-found: the same lag on the write
                              side the glossary shows, where the API answers
                              404 rather than an empty body
    `read_cap`                lookup returns at most that many characters of
                              the overview while the stored aspect is whole
    `stub_below_budget`       while the budget does not pass the stored
                              overview's size, lookup answers a stub of that
                              many characters, HTTP 200, no warning and no
                              truncation flag; the whole bundle once the
                              budget passes it. The read serves a page whole
                              or not at all (docs/REFERENCE.md): 572
                              characters of a 71,690-character entry at
                              budget 60,000, 78,854 at 80,000
    `queries_unsupported`     write_queries raises WriteUnsupported
    `read_raises`             entry names whose read_entry raises, the way
                              get_entry does on an entry the search still
                              lists but the catalog no longer holds
    `name_prefix`             what comes before a document's key in its
                              entry name (`entries/` by default; a store
                              that builds names needs the whole shape,
                              `projects/P/locations/L/entryGroups/G/entries/`)

    `lookup` renders the bundle shape lookupContext returns, so a verify
    step can grep it: the overview line, the linked terms, the queries.
    """

    def __init__(self, documents, *, mode="steady", empty_calls=2, stub_every=2, scoped=True,
                 store_ratio=1.0, stub_reads_after_write=0, term_reads_not_found=0, read_cap=None,
                 stub_below_budget=None, queries_unsupported=False, read_raises=(),
                 name_prefix="entries/"):
        self.name_prefix = name_prefix
        self.overviews = {f"{name_prefix}{key}": text for key, text in documents.items()}
        self.read_raises = set(read_raises)
        self.queries = {}           # entry name -> rows in the aspect's shape
        self.terms = {}             # term entry name -> {"display_name", "description", "labels"}
        self.links = {}             # link name -> {"source", "target"}
        self.entries = {}           # query entry name -> {"display_name", "description", "labels"}
        self.extra_entries = []     # appended to what list_entries returns
        self.mode = mode
        self.empty_calls = empty_calls
        self.stub_every = stub_every
        self.scoped = scoped
        self.store_ratio = store_ratio
        self.stub_reads_after_write = stub_reads_after_write
        self.term_reads_not_found = term_reads_not_found
        self.read_cap = read_cap
        self.stub_below_budget = stub_below_budget
        self.queries_unsupported = queries_unsupported
        self.searches = 0
        self.lookups = 0
        self.budgets = []
        self.write_calls = 0
        self.create_calls = 0
        self.update_calls = 0
        self._stub_reads_left = {}
        self._not_found_reads_left = {}

    # ---- reads ----

    def search(self, query):
        self.searches += 1
        if not self.scoped:
            return []                       # over-scoped query: zero results, no error
        name = f"{self.name_prefix}{query}"
        return [name] if name in self.overviews else []

    def render(self, entry_name):
        """The bundle a reader gets: what lookupContext returns for one entry."""
        if entry_name not in self.overviews:
            return ""                       # a missing entry: 0 characters, no error
        overview = self.overviews[entry_name]
        if self.read_cap is not None:
            overview = overview[:self.read_cap]
        lines = [f"overview: {overview}"]
        linked = [self.terms[link["target"]]["display_name"] for link in self.links.values()
                  if link["source"] == entry_name and link["target"] in self.terms]
        if linked:
            lines.append("related business terms: " + ", ".join(linked))
        if self.queries.get(entry_name):
            lines.append("queries:")
            lines.extend(f"- {row['description']}" for row in self.queries[entry_name])
        return "\n".join(lines)

    def lookup(self, entry_name, context_budget):
        self.lookups += 1
        self.budgets.append(context_budget)
        if self._stub_reads_left.get(entry_name, 0) > 0:
            self._stub_reads_left[entry_name] -= 1
            return ""                       # straight after a write: 0 characters, no error
        body = self.render(entry_name)
        stored = self.overviews.get(entry_name, "")
        if self.stub_below_budget is not None and stored and context_budget <= len(stored):
            # a valid, well-formed page: the opening of the overview and no
            # more, with no signal that anything was left out
            return body[:self.stub_below_budget]
        if self.mode == "just_written" and self.lookups <= self.empty_calls:
            return ""
        if self.mode == "burst" and self.lookups % self.stub_every == 0:
            return body[:200]
        return body

    def list_entries(self, entry_group):
        group = f"projects/1/locations/us/entryGroups/{entry_group}"

        def table(dataset, name):
            return {"name": f"{group}/entries/bigquery.googleapis.com/projects/p/datasets/{dataset}/tables/{name}",
                    "entry_type": "projects/655216118709/locations/global/entryTypes/bigquery-table",
                    "parent_entry": f"{group}/entries/bigquery.googleapis.com/projects/p/datasets/{dataset}"}

        return [table("sales", "orders"), table("sales", "items"), table("ops", "jobs"),
                {"name": f"{group}/entries/{entry_group.lstrip('@')}",
                 "entry_type": "projects/655216118709/locations/global/entryTypes/entrygroup",
                 "parent_entry": ""},
                {"name": f"{group}/entries/index",
                 "entry_type": "projects/655216118709/locations/global/entryTypes/generic",
                 "parent_entry": ""}] + self.extra_entries

    def read_entry(self, entry_name):
        if entry_name in self.read_raises:
            raise RuntimeError(f"Entry `{entry_name}` does not exist.")
        if self._not_found_reads_left.get(entry_name, 0) > 0:
            self._not_found_reads_left[entry_name] -= 1
            # the live API's own words for a term read too soon after its create
            raise RuntimeError(f"404 Entry `{entry_name}` does not exist.")
        # a term entry answers with the term's own text: get_entry on it is
        # how a push verifies a term where it lives
        entry = self.entries.get(entry_name) or self.terms.get(entry_name) or {}
        return {"name": entry_name,
                "overview": self.overviews.get(entry_name),
                "queries": self.queries.get(entry_name),
                "display_name": entry.get("display_name", ""),
                "description": entry.get("description", ""),
                "labels": dict(entry.get("labels", {}))}

    # ---- writes ----

    def write_overview(self, entry_name, text):
        self.write_calls += 1
        self.overviews[entry_name] = text[:int(len(text) * self.store_ratio)]
        self._stub_reads_left[entry_name] = self.stub_reads_after_write

    def write_queries(self, entry_name, queries):
        self.write_calls += 1
        if self.queries_unsupported:
            raise WriteUnsupported("the fake refuses the queries aspect")
        self.queries[entry_name] = merge_queries(self.queries.get(entry_name, []), queries)

    def upsert_term(self, glossary_id, term_id, display_name, description, labels):
        name = term_entry_name("1", glossary_id, term_id)
        wanted = {"display_name": display_name, "description": description, "labels": dict(labels)}
        if name not in self.terms:
            self.create_calls += 1
            self.terms[name] = wanted
            self._not_found_reads_left[name] = self.term_reads_not_found
            return name, "created"
        if self.terms[name] == wanted:
            return name, "unchanged"
        self.update_calls += 1
        self.terms[name] = wanted
        return name, "updated"

    def link_term(self, source_entry, term_entry, link_id):
        name = f"{entry_group_of(source_entry)}/entryLinks/{link_id}"
        if name in self.links:
            return "existed"
        self.create_calls += 1
        self.links[name] = {"source": source_entry, "target": term_entry}
        return "created"

    def upsert_query_entry(self, group_id, entry_id, question, sql, labels):
        name = f"projects/1/locations/us/entryGroups/{group_id}/entries/{entry_id}"
        status = "updated" if name in self.entries else "created"
        if status == "created":
            self.create_calls += 1
        else:
            self.update_calls += 1
        self.entries[name] = {"display_name": question[:200], "description": compact_sql(sql),
                              "labels": dict(labels)}
        return name, status


DOCUMENT = "x" * 24000
ORDERS = "entries/sales__orders"         # the fake's entry name for sales.orders
# the search path (no group named, no id shape known): the fake's short
# entry names carry no group, so the tests of that path search
STORE_ARGS = {"known_good_table": "sales.orders", "sleep": lambda seconds: None,
              "entry_group": None, "entry_id_pattern": "{dataset}__{table}"}
# a custom group as a store that builds names sees it
GROUP_ENTRIES = "projects/1/locations/us/entryGroups/kb/entries/"
BUILT_ARGS = {"project": "1", "location": "us", "entry_group": "kb", "sleep": lambda seconds: None}


def _store(catalog, **kwargs):
    return KnowledgeCatalogStore(catalog, **{**STORE_ARGS, **kwargs})


def test_a_steady_catalog_is_read_once_per_table():
    catalog = FakeCatalog({"sales__orders": DOCUMENT})

    store = _store(catalog)
    got = store.document_for("sales.orders")

    assert got == "overview: " + DOCUMENT
    assert catalog.lookups == 1            # the common case pays for no retries


def test_a_freshly_written_entry_is_retried_until_it_indexes():
    """Immediately after registering entries the catalog returns 0 characters
    and the whole document a few minutes later."""
    catalog = FakeCatalog({"sales__orders": DOCUMENT}, mode="just_written", empty_calls=2)

    store = _store(catalog)
    got = store.document_for("sales.orders")

    assert got == "overview: " + DOCUMENT
    assert catalog.lookups == 3


def test_a_stub_under_burst_is_never_cached():
    """The original stubs appeared while enriching about 40 candidates back to
    back. A cache that keeps one pins it for the life of the process, and a
    writer fed a stub invents column names."""
    catalog = FakeCatalog({"sales__orders": DOCUMENT}, mode="burst", stub_every=1)

    store = _store(catalog)
    first = store.document_for("sales.orders")

    assert len(first) == 200               # degraded, returned rather than dropped
    assert catalog.lookups == 3
    store.document_for("sales.orders")
    assert catalog.lookups == 6            # re-fetched: nothing degraded was cached


def test_a_table_the_catalog_does_not_document_is_no_context_not_an_error():
    catalog = FakeCatalog({"sales__orders": DOCUMENT})

    store = _store(catalog)

    assert store.document_for("sales.missing") == ""
    assert catalog.lookups == 0            # resolution failed, nothing was fetched


class _SearchHits(FakeCatalog):
    """A catalog whose search answers one fixed list whatever the term: the
    shape of a stale index, where the hits are not the table asked for."""

    def __init__(self, hits):
        super().__init__({})
        self.hits = hits

    def search(self, query):
        return list(self.hits)


STALE_TMP_ENTRY = ("projects/1/locations/us/entryGroups/@bigquery/entries/bigquery.googleapis.com/"
                   "projects/p/datasets/other_dataset/tables/fct_other__dbt_tmp")
ORDERS_SYSTEM_ENTRY = ("projects/1/locations/us/entryGroups/@bigquery/entries/bigquery.googleapis.com/"
                       "projects/p/datasets/sales/tables/orders")


def test_a_search_hit_for_another_table_resolves_to_none_not_to_that_hit():
    """A cold run: the search for a transient table returned a stale entry of
    a different dataset and table first, and the old fallback took it. On a
    push that is a write to the wrong table's entry."""
    store = KnowledgeCatalogStore(_SearchHits([STALE_TMP_ENTRY]), entry_group=None, sleep=lambda s: None)

    assert store.resolve("sales.orders__dbt_tmp") is None


def test_the_hit_that_keys_to_the_table_wins_over_an_unrelated_one_before_it():
    store = KnowledgeCatalogStore(_SearchHits([STALE_TMP_ENTRY, ORDERS_SYSTEM_ENTRY]), entry_group=None,
                                  sleep=lambda s: None)

    assert store.resolve("sales.orders") == ORDERS_SYSTEM_ENTRY
    assert store.resolve("SALES.Orders") == ORDERS_SYSTEM_ENTRY
    assert store.resolve("orders") == ORDERS_SYSTEM_ENTRY     # no dataset asked: the last segment decides


def test_two_exact_hits_in_two_groups_still_refuse_without_a_group():
    from context_lift.catalog import AmbiguousEntry
    hits = [STALE_TMP_ENTRY,
            "projects/1/locations/global/entryGroups/kb/entries/sales__orders",
            "projects/1/locations/global/entryGroups/scratch/entries/sales__orders"]
    store = KnowledgeCatalogStore(_SearchHits(hits), entry_group=None, sleep=lambda s: None)

    with pytest.raises(AmbiguousEntry) as refused:
        store.resolve("sales.orders")

    assert "2 entry groups: kb, scratch" in str(refused.value)


class _NoSearch(FakeCatalog):
    """A catalog whose search must not be called."""

    def search(self, query):
        raise AssertionError("search was called; addressing is built from the key")


def test_a_store_with_an_entry_id_template_builds_the_name_and_never_searches():
    """The same table resolved right in one run and wrong minutes later
    with only the search ranking changed; 99 of 99 custom entries were
    reachable by the built name with zero searches."""
    catalog = _NoSearch({"sales__orders": DOCUMENT}, name_prefix=GROUP_ENTRIES)
    store = KnowledgeCatalogStore(catalog, entry_id_template="{dataset}__{table}", **BUILT_ARGS)

    assert store.resolve("sales.orders") == GROUP_ENTRIES + "sales__orders"
    assert store.document_for("sales.orders") == "overview: " + DOCUMENT
    assert catalog.searches == 0


def test_the_bigquery_default_builds_the_documented_entry_name():
    """Every entry name in the auto-discovered group had this shape on both
    estates; search given the complete fully-qualified name still returned
    a different table first."""
    from context_lift.catalog import BIGQUERY_ENTRY_ID
    store = KnowledgeCatalogStore(_NoSearch({}), project="my-project", location="us")

    assert store.entry_id_template == BIGQUERY_ENTRY_ID
    assert store.resolve("sales.orders") == (
        "projects/my-project/locations/us/entryGroups/@bigquery/entries/"
        "bigquery.googleapis.com/projects/my-project/datasets/sales/tables/orders")


def test_a_three_part_two_part_or_bare_name_fills_the_template_slots_it_has():
    """Splitting on the last dot alone put `other.sales` into the dataset
    slot of a 3-part name. The project slot comes from the name when it
    has one and from the store otherwise; a bare name has no dataset."""
    template = "{project}__{dataset}__{table}"
    store = KnowledgeCatalogStore(_NoSearch({}), entry_id_template=template, **BUILT_ARGS)

    assert store.resolve("`other`.`sales`.`orders`") == GROUP_ENTRIES + "other__sales__orders"
    assert store.resolve("sales.orders") == GROUP_ENTRIES + "1__sales__orders"
    assert store.resolve("orders") == GROUP_ENTRIES + "1____orders"


def test_a_template_that_fits_no_built_name_at_all_names_the_flags_to_check():
    """Every name a run built was missing: that is one flag wrong, not 13
    tables gone."""
    catalog = _NoSearch({"sales__orders": DOCUMENT}, name_prefix=GROUP_ENTRIES,
                        read_raises={GROUP_ENTRIES + "orders", GROUP_ENTRIES + "items"})
    store = KnowledgeCatalogStore(catalog, entry_id_template="{table}", **BUILT_ARGS)

    store.resolve("sales.orders")
    store.resolve("sales.items")

    assert store.template_mismatch() == (
        f"not one of the entry names built from --entry-id {{table}} exists in entry group kb "
        f"(2 tried, the first {GROUP_ENTRIES}orders). An entry name carries the project the "
        "tables live in and the location of their dataset, so check --project and --location "
        "before the ids; catalog-list --entry-group kb shows the ids this group holds, and a "
        "custom group needs --entry-group G --entry-id TEMPLATE")


def test_one_missing_built_name_beside_a_good_one_is_that_table_not_the_template():
    """A missing FIRST table aborted a whole pull and blamed --entry-id;
    the same table later in the list was one "no entry" line."""
    catalog = _NoSearch({"sales__orders": DOCUMENT}, name_prefix=GROUP_ENTRIES,
                        read_raises={GROUP_ENTRIES + "sales__gone"})
    store = KnowledgeCatalogStore(catalog, entry_id_template="{dataset}__{table}", **BUILT_ARGS)

    assert store.resolve("sales.gone") == GROUP_ENTRIES + "sales__gone"      # counted, not refused
    assert store.resolve("sales.orders") == GROUP_ENTRIES + "sales__orders"

    assert store.names_not_found == [GROUP_ENTRIES + "sales__gone"]
    assert store.template_mismatch() is None            # one name found: the template fits


def test_the_built_in_id_shape_is_not_reported_as_an_entry_id_nobody_passed():
    """The auto group fills its own template, so a run with no --entry-id
    was told its --entry-id did not match."""
    catalog = _NoSearch({}, name_prefix="projects/p/locations/eu/entryGroups/@bigquery/entries/",
                        read_raises={"projects/p/locations/eu/entryGroups/@bigquery/entries/"
                                     "bigquery.googleapis.com/projects/p/datasets/sales/tables/orders"})
    store = KnowledgeCatalogStore(catalog, project="p", location="eu")

    store.resolve("sales.orders")

    message = store.template_mismatch()
    assert message.startswith("not one of the entry names built from the built-in id shape of "
                              "@bigquery, bigquery.googleapis.com/projects/{project}/datasets/"
                              "{dataset}/tables/{table} exists in entry group @bigquery")
    assert "--entry-id bigquery" not in message
    assert "check --project and --location" in message


def test_after_the_first_name_checks_out_a_missing_entry_is_that_tables_own_line():
    catalog = _NoSearch({"sales__orders": DOCUMENT}, name_prefix=GROUP_ENTRIES,
                        read_raises={GROUP_ENTRIES + "sales__gone"})
    store = KnowledgeCatalogStore(catalog, entry_id_template="{dataset}__{table}", **BUILT_ARGS)

    assert store.resolve("sales.orders") == GROUP_ENTRIES + "sales__orders"
    assert store.resolve("sales.gone") == GROUP_ENTRIES + "sales__gone"    # built, not read: the caller's read says
    assert store.document_for("sales.gone") == ""                          # a lookup of a missing name: nothing, no error


def test_a_search_that_returns_nothing_for_a_known_table_is_refused_at_startup():
    """Search answers an over-scoped or under-permissioned query with zero
    results and HTTP 200. An empty catalog and a broken one are the same
    response, so the adapter has to prove its scoping works. The message
    names the two confirmed causes and says the quota-project diagnosis was
    retracted, so nobody chases it again."""
    catalog = FakeCatalog({"sales__orders": DOCUMENT}, scoped=False)

    with pytest.raises(ScopeCheckFailed) as refused:
        _store(catalog)

    message = str(refused.value)
    assert "name-prefix predicate in keyword mode" in message
    assert "roles/dataplex.catalogViewer" in message
    assert "quota project on the credentials is not a cause" in message


def test_the_read_budget_is_always_explicit():
    """lookupContext truncates at a default budget, and one deployment's SQL
    conventions sit at the end of the document - a small budget returns the
    prose and drops the rules the experiment is measuring."""
    catalog = FakeCatalog({"sales__orders": DOCUMENT})

    _store(catalog, context_budget=60000).document_for("sales.orders")

    assert catalog.budgets == [60000]


# the entry the budget was measured on, live, 2026-09-05: 71,690 characters
# stored, served as 572 at budget 60,000 and as 78,854 at budget 80,000
STORED_CHARS = 71690
STUB_CHARS = 572


def test_a_page_longer_than_the_budget_is_served_as_a_stub_with_no_error():
    """Same entry, same request, only the budget changed. The stub is a
    valid, well-formed page: the opening of the overview, no schema section
    and no column names, HTTP 200 with no warning field. A reader that only
    logs the served length calls the entry thin, and this entry was one of
    the four richest on the estate."""
    catalog = FakeCatalog({"sales__orders": "x" * STORED_CHARS}, stub_below_budget=STUB_CHARS)
    store = _store(catalog, context_budget=60000)

    served, stored = store.read_now(ORDERS), store.stored_overview(ORDERS)

    assert (len(served), len(stored)) == (STUB_CHARS, STORED_CHARS)
    assert short_serve_note(len(served), len(stored), 60000) == (
        "the reader is served 572 characters where the entry stores 71690, at budget 60000; "
        "raise --budget to 90000")


def test_a_budget_that_passes_the_page_serves_the_whole_bundle_and_says_nothing():
    catalog = FakeCatalog({"sales__orders": "x" * STORED_CHARS}, stub_below_budget=STUB_CHARS)
    store = _store(catalog, context_budget=80000)

    served = store.read_now(ORDERS)

    assert len(served) == len("overview: ") + STORED_CHARS      # the bundle around the page
    assert short_serve_note(len(served), STORED_CHARS, 80000) is None


def test_a_thin_page_served_whole_is_not_reported_as_a_short_serve():
    """The bundle carries the schema and the profile around the page, so a
    whole read comes back longer than the stored overview (78,854 for 71,690;
    101,385 for 92,397). Only a served body under the stored size is a page
    the reader did not get."""
    assert short_serve_note(612, 600, 60000) is None
    assert short_serve_note(600, 600, 60000) is None
    assert short_serve_note(599, 600, 60000) is not None
    assert short_serve_note(0, 600, 60000).startswith(       # the lag, which the budget cannot fix
        "the reader is served nothing where the entry stores 600 characters")


def test_the_budget_to_ask_for_clears_the_served_bundle_not_the_stored_page():
    """The budget is measured against the served bundle, and the bundle is
    bigger than the page it carries. Rounding the stored size up to the next
    10,000 is not enough: 92,397 stored comes back as 101,385 served, so a
    caller told 100,000 gets the same stub. These are the nine pages measured
    on one estate, stored size against the size the reader was served."""
    measured = [(92397, 101385), (90249, 99189), (83246, 91388), (78377, 86133),
                (71942, 79095), (71690, 78854), (64916, 71404), (64723, 71190),
                (61227, 67364)]
    for stored, served in measured:
        assert budget_that_serves(stored) > served, f"{stored} stored, {served} served"


def test_rounding_the_stored_size_up_would_have_starved_five_of_the_nine():
    """Why the margin exists, kept as a test so it cannot be argued away. The
    old rule named a working budget on four of nine pages, and only because
    the rounding happened to clear the bundle by luck."""
    measured = [(92397, 101385), (90249, 99189), (83246, 91388), (78377, 86133),
                (71942, 79095), (71690, 78854), (64916, 71404), (64723, 71190),
                (61227, 67364)]
    old_rule = [(stored // 10_000 + 1) * 10_000 for stored, _ in measured]
    starved = [b for b, (_, served) in zip(old_rule, measured) if b <= served]
    assert len(starved) == 5


def test_the_entry_id_pattern_is_configuration_not_an_assumption():
    """<dataset>__<table> is a property of whoever registered the entries, not
    of the product. An entry keyed by the bare table name answers a bare
    request; a `dataset.table` request never lands on it (resolve's rule)."""
    catalog = FakeCatalog({"orders": DOCUMENT})

    store = _store(catalog, entry_id_pattern="{table}", known_good_table="sales.orders")

    assert store.document_for("orders") == "overview: " + DOCUMENT
    assert store.document_for("sales.orders") == ""


def test_an_entry_name_becomes_a_dataset_table_key_for_both_shapes():
    from context_lift.catalog import table_key_of
    system = "projects/1/locations/us/entryGroups/@bigquery/entries/bigquery.googleapis.com/projects/p/datasets/sales/tables/orders"
    custom = "projects/1/locations/global/entryGroups/kb/entries/sales__orders"

    assert table_key_of(system) == "sales.orders"
    assert table_key_of(custom) == "sales.orders"
    assert table_key_of("projects/1/entries/index") == "index"


class _RoutingCatalog(FakeCatalog):
    def search_semantic(self, query):
        self.semantic_queries = getattr(self, "semantic_queries", []) + [query]
        return ["projects/1/locations/us/entryGroups/@bigquery/entries/x/datasets/sales/tables/orders",
                "projects/1/locations/us/entryGroups/@bigquery/entries/x/datasets/ops/tables/jobs",
                "projects/1/locations/us/entryGroups/@bigquery/entries/x/datasets/sales/tables/orders",
                "projects/1/locations/us/entryGroups/@bigquery/entries/x/datasets/sales/tables/items"]


def test_route_prefers_semantic_search_dedupes_and_keeps_one_dataset():
    catalog = _RoutingCatalog({})
    store = KnowledgeCatalogStore(catalog, sleep=lambda s: None)

    assert store.route("how many orders?", top=3) == ["sales.orders", "ops.jobs", "sales.items"]
    assert store.route("how many orders?", top=3, within="sales") == ["sales.orders", "sales.items"]
    assert catalog.semantic_queries[0] == "how many orders? type=table"


def test_route_takes_several_within_and_its_own_search_pattern():
    catalog = _RoutingCatalog({})
    store = KnowledgeCatalogStore(catalog, sleep=lambda s: None, search_pattern="{question} tables")

    assert store.route("how many orders?", top=3, within=["ops", "sales"]) \
        == ["sales.orders", "ops.jobs", "sales.items"]
    assert store.route("how many orders?", top=3, within=["ops"]) == ["ops.jobs"]
    assert catalog.semantic_queries[0] == "how many orders? tables"


def test_route_keeps_only_the_entry_group_it_was_given_and_names_what_it_dropped():
    """--entry-group changed nothing on catalog-route: the same tables came
    back for the group that held them, for a group that held none of them,
    and for a group that does not exist."""

    class _TwoGroups(FakeCatalog):
        def search_semantic(self, query):
            return ["projects/1/locations/us/entryGroups/scratch/entries/sales__orders",
                    "projects/1/locations/us/entryGroups/kb/entries/ops__jobs"]

    outside = []
    store = KnowledgeCatalogStore(_TwoGroups({}), entry_group="kb", sleep=lambda s: None)

    assert store.route("how many orders?", top=3, outside_group=outside) == ["ops.jobs"]
    assert outside == ["sales.orders"]
    everywhere = KnowledgeCatalogStore(_TwoGroups({}), entry_group=None, sleep=lambda s: None)
    assert everywhere.route("how many orders?", top=3) == ["sales.orders", "ops.jobs"]


def test_route_does_not_report_a_table_that_also_lives_in_the_wanted_group():

    class _SameTableTwice(FakeCatalog):
        def search_semantic(self, query):
            return ["projects/1/locations/us/entryGroups/scratch/entries/sales__orders",
                    "projects/1/locations/us/entryGroups/kb/entries/sales__orders"]

    outside = []
    store = KnowledgeCatalogStore(_SameTableTwice({}), entry_group="kb", sleep=lambda s: None)

    assert store.route("how many orders?", top=3, outside_group=outside) == ["sales.orders"]
    assert outside == []


def test_route_drops_tables_outside_the_catalog_before_the_cut_and_names_them():
    """A scratch copy of a table the search kept ranking first ate the
    top-N slot of the table that could score; the cut is over the tables
    the routing is graded against, and the dropped ones are reported."""
    store = KnowledgeCatalogStore(_RoutingCatalog({}), sleep=lambda s: None)
    dropped = []

    routed = store.route("how many orders?", top=2, keep={"sales.orders", "sales.items"}, dropped=dropped)

    assert routed == ["sales.orders", "sales.items"]        # ops.jobs did not take the second slot
    assert dropped == ["ops.jobs"]


# ---- the table list -----------------------------------------------------------


def test_list_tables_keeps_three_of_five_entries_and_the_counts_name_the_two_dropped():
    """A live group held 129 entries for 127 tables: the group's self-entry
    and an index entry, told apart from tables only by an empty parentEntry."""
    store = _store(FakeCatalog({"sales__orders": DOCUMENT}))

    keys, counts = store.list_tables("@bigquery")

    assert keys == ["ops.jobs", "sales.items", "sales.orders"]
    assert counts == {"listed": 5, "kept": 3, "no_parent": 2, "not_table_shaped": 0,
                      "transient": 0, "outside_datasets": 0}


def test_a_dbt_transient_table_is_dropped_from_the_list_and_counted():
    """A cold run's list carried a `__dbt_tmp` name; by push time the table
    was gone and the catalog's search answered with a stale entry."""
    catalog = FakeCatalog({"sales__orders": DOCUMENT})
    group = "projects/1/locations/us/entryGroups/@bigquery"
    for name in ("orders__dbt_tmp", "orders__DBT_BACKUP"):
        catalog.extra_entries.append({
            "name": f"{group}/entries/bigquery.googleapis.com/projects/p/datasets/sales/tables/{name}",
            "entry_type": "projects/655216118709/locations/global/entryTypes/bigquery-table",
            "parent_entry": f"{group}/entries/bigquery.googleapis.com/projects/p/datasets/sales"})

    keys, counts = _store(catalog).list_tables("@bigquery")

    assert keys == ["ops.jobs", "sales.items", "sales.orders"]
    assert counts["listed"] == 7 and counts["transient"] == 2 and counts["kept"] == 3


def test_a_dataset_entry_has_a_parent_but_keys_without_a_dot_and_is_dropped():
    catalog = FakeCatalog({"sales__orders": DOCUMENT})
    group = "projects/1/locations/us/entryGroups/@bigquery"
    catalog.extra_entries.append({
        "name": f"{group}/entries/bigquery.googleapis.com/projects/p/datasets/sales",
        "entry_type": "projects/655216118709/locations/global/entryTypes/bigquery-dataset",
        "parent_entry": f"{group}/entries/bigquery.googleapis.com/projects/p"})

    keys, counts = _store(catalog).list_tables("@bigquery")

    assert keys == ["ops.jobs", "sales.items", "sales.orders"]
    assert counts["listed"] == 6 and counts["not_table_shaped"] == 1


def test_list_tables_keeps_only_the_named_datasets():
    store = _store(FakeCatalog({"sales__orders": DOCUMENT}))

    keys, counts = store.list_tables("@bigquery", datasets=["sales"])

    assert keys == ["sales.items", "sales.orders"]
    assert counts["kept"] == 2 and counts["outside_datasets"] == 1


# ---- the overview, written and read back ----------------------------------------


def test_stored_overview_is_none_before_a_write_and_the_text_after():
    catalog = FakeCatalog({"sales__orders": DOCUMENT})
    store = _store(catalog)
    items = "entries/sales__items"

    assert store.stored_overview(items) is None
    store.write_overview(items, "# items\nOne row per order line.")

    assert store.stored_overview(items) == "# items\nOne row per order line."
    assert catalog.write_calls == 1


def test_read_now_bypasses_the_cache_that_document_for_keeps():
    """After a write, document_for hands back the pre-write body for the life
    of the process; a verify step that used it would pass on stale bytes."""
    catalog = FakeCatalog({"sales__orders": DOCUMENT})
    store = _store(catalog)
    assert store.document_for("sales.orders") == "overview: " + DOCUMENT

    store.write_overview(ORDERS, "y" * 24000)

    assert store.document_for("sales.orders") == "overview: " + DOCUMENT   # cached
    assert store.read_now(ORDERS) == "overview: " + "y" * 24000


def test_a_store_that_keeps_fewer_bytes_shows_in_the_stored_overview():
    """The field notes' rule: a catalog that accepts the bytes and keeps fewer
    looks exactly like one that worked, until the lengths are compared."""
    catalog = FakeCatalog({"sales__orders": DOCUMENT}, store_ratio=0.5)
    store = _store(catalog)

    store.write_overview(ORDERS, "z" * 1000)

    assert len(store.stored_overview(ORDERS)) == 500
    assert len(store.read_now(ORDERS)) == len("overview: ") + 500


def test_a_read_cap_cuts_what_a_reader_gets_while_the_stored_aspect_is_whole():
    """lookupContext truncates at a budget; get_entry does not. The two reads
    answer different questions and a verify step needs both."""
    catalog = FakeCatalog({"sales__orders": DOCUMENT}, read_cap=6287)
    store = _store(catalog)

    assert len(store.stored_overview(ORDERS)) == 24000
    assert len(store.read_now(ORDERS)) == len("overview: ") + 6287


def test_the_reads_straight_after_a_write_are_empty_and_a_fresh_read_retries_past_them():
    catalog = FakeCatalog({"sales__orders": DOCUMENT}, stub_reads_after_write=2)
    store = _store(catalog)

    store.write_overview(ORDERS, "w" * 24000)

    assert store.read_now(ORDERS) == ""                    # the lag, seen raw
    assert store.document_for("sales.orders") == "overview: " + "w" * 24000
    assert catalog.lookups == 3                            # one more empty, then the retry found it


# ---- terms, links, query entries, the queries aspect -----------------------------


def test_upsert_term_creates_then_updates_then_is_unchanged():
    catalog = FakeCatalog({})

    first = catalog.upsert_term("kb", "net-revenue", "Net revenue", "Gross minus refunds.",
                                {"source": "rules"})
    second = catalog.upsert_term("kb", "net-revenue", "Net revenue", "Gross minus refunds and tax.",
                                 {"source": "rules"})
    third = catalog.upsert_term("kb", "net-revenue", "Net revenue", "Gross minus refunds and tax.",
                                {"source": "rules"})

    expected = ("projects/1/locations/global/entryGroups/@dataplex/entries/"
                "projects/1/locations/global/glossaries/kb/terms/net-revenue")
    assert first == (expected, "created")
    assert second == (expected, "updated")
    assert third == (expected, "unchanged")
    assert (catalog.create_calls, catalog.update_calls) == (1, 1)


def test_link_term_is_created_once_then_exists_and_the_bundle_names_the_term():
    catalog = FakeCatalog({"sales__orders": DOCUMENT})
    store = _store(catalog)
    term, _ = catalog.upsert_term("kb", "net-revenue", "Net revenue", "Gross minus refunds.", {})

    assert catalog.link_term(ORDERS, term, "orders-net-revenue") == "created"
    assert catalog.link_term(ORDERS, term, "orders-net-revenue") == "existed"

    assert "\nrelated business terms: Net revenue" in store.read_now(ORDERS)


def test_upsert_query_entry_creates_then_updates():
    catalog = FakeCatalog({})

    first = catalog.upsert_query_entry(
        "kb-queries", "q-17", "How many orders shipped in May?",
        "SELECT   count(*)\n  FROM orders WHERE shipped_month = 5", {"source": "gold"})
    second = catalog.upsert_query_entry(
        "kb-queries", "q-17", "How many orders shipped in May?",
        "SELECT count(*) FROM orders WHERE shipped_month = 5", {"source": "gold"})

    name = "projects/1/locations/us/entryGroups/kb-queries/entries/q-17"
    assert first == (name, "created") and second == (name, "updated")
    assert catalog.read_entry(name)["description"] == "SELECT count(*) FROM orders WHERE shipped_month = 5"


def test_write_queries_raises_write_unsupported_when_the_catalog_refuses():
    catalog = FakeCatalog({"sales__orders": DOCUMENT}, queries_unsupported=True)

    with pytest.raises(WriteUnsupported):
        catalog.write_queries(ORDERS, [{"sql": "SELECT 1", "question": "one?"}])


def test_write_queries_lists_the_question_in_the_bundle_and_stores_the_aspect_rows():
    catalog = FakeCatalog({"sales__orders": DOCUMENT})
    store = _store(catalog)

    catalog.write_queries(ORDERS, [{"sql": "SELECT count(*) FROM orders", "question": "How many orders?"}])

    assert store.read_now(ORDERS).endswith("\nqueries:\n- How many orders?")
    assert catalog.read_entry(ORDERS)["queries"] == [
        {"description": "How many orders?", "sql": "SELECT count(*) FROM orders", "source": "USER"}]


def test_merge_queries_keeps_the_products_rows_and_replaces_one_by_sql_text():
    """A documentation scan's rows (source AGENT) survive a push; a pushed
    row with the same SQL, whitespace aside, replaces rather than duplicates."""
    generated = [
        {"description": "Orders per day", "sql": "SELECT day, count(*) FROM orders GROUP BY day", "source": "AGENT"},
        {"description": "Biggest order", "sql": "SELECT max(total) FROM orders", "source": "AGENT"}]

    merged = merge_queries(generated, [
        {"sql": "SELECT day,   count(*)\nFROM orders GROUP BY day", "question": "How many orders per day?"},
        {"sql": "SELECT 1", "question": "one?"}])

    assert merged == [
        {"description": "How many orders per day?", "sql": "SELECT day,   count(*)\nFROM orders GROUP BY day",
         "source": "USER"},
        {"description": "Biggest order", "sql": "SELECT max(total) FROM orders", "source": "AGENT"},
        {"description": "one?", "sql": "SELECT 1", "source": "USER"}]


def test_compact_sql_collapses_whitespace_and_refuses_what_a_description_cannot_hold():
    assert compact_sql("SELECT   1\n  FROM  t") == "SELECT 1 FROM t"

    with pytest.raises(WriteUnsupported) as refused:
        compact_sql("SELECT " + "x" * 2000)

    assert "2007 characters" in str(refused.value)


def test_project_number_and_entry_group_come_out_of_both_entry_name_shapes():
    system = ("projects/123456789012/locations/us/entryGroups/@bigquery/entries/"
              "bigquery.googleapis.com/projects/p/datasets/sales/tables/orders")
    custom = "projects/123456789012/locations/global/entryGroups/kb/entries/sales__orders"

    assert project_number_of(system) == "123456789012"
    assert project_number_of(custom) == "123456789012"
    assert project_number_of("projects/my-project-id/locations/us/entryGroups/kb") == ""
    assert entry_group_of(system) == "projects/123456789012/locations/us/entryGroups/@bigquery"
    assert entry_group_of(custom) == "projects/123456789012/locations/global/entryGroups/kb"


# ---- the live transport's request shapes, without a network ----------------------


class _Done:
    def result(self):
        return None


class _RecordingClient:
    """Stands in for CatalogServiceClient: records each request, answers the
    little the transport reads back, raises what a test tells it to."""

    def __init__(self, raise_on=None):
        self.calls = []
        self.raise_on = raise_on or {}

    def _record(self, name, request):
        self.calls.append((name, request))
        if name in self.raise_on:
            raise self.raise_on[name]

    def get_entry(self, request):
        from google.cloud import dataplex_v1
        self._record("get_entry", request)
        return dataplex_v1.Entry(name=request.name)

    def update_entry(self, request):
        self._record("update_entry", request)

    def create_entry(self, request):
        self._record("create_entry", request)

    def create_entry_link(self, request):
        self._record("create_entry_link", request)

    def create_entry_type(self, request):
        self._record("create_entry_type", request)
        return _Done()

    def create_entry_group(self, request):
        self._record("create_entry_group", request)
        return _Done()


def _offline_transport(raise_on=None):
    pytest.importorskip("google.cloud.dataplex_v1")
    from google.auth.credentials import AnonymousCredentials
    from context_lift.catalog import DataplexTransport
    transport = DataplexTransport("my-project", "us", credentials=AnonymousCredentials())
    transport._client = _RecordingClient(raise_on)
    return transport


TABLE = ("projects/123456789012/locations/us/entryGroups/@bigquery/entries/"
         "bigquery.googleapis.com/projects/p/datasets/sales/tables/orders")


def _aspect_data(request, key):
    from google.protobuf import json_format
    aspect = request.entry.aspects[key]
    return aspect.aspect_type, json_format.MessageToDict(type(aspect).pb(aspect).data)


def test_the_live_transport_builds_the_documented_requests_without_a_network():
    """The request shapes against the real message classes, with a recorder
    in place of the client: a wrong field name would otherwise be found only
    live. Skipped when the [catalog] extra is not installed."""
    transport = _offline_transport()
    term = ("projects/123456789012/locations/global/entryGroups/@dataplex/entries/"
            "projects/123456789012/locations/global/glossaries/kb/terms/net-revenue")

    transport.write_overview(TABLE, "<p>orders</p>")
    transport.write_queries(TABLE, [{"sql": "SELECT 1", "question": "one?"}])
    linked = transport.link_term(TABLE, term, "orders-net-revenue")
    query_entry = transport.upsert_query_entry("kb-queries", "q-17", "How many?", "SELECT  count(*)\nFROM orders",
                                               {"source": "gold"})

    calls = dict(transport._client.calls)
    overview_update = transport._client.calls[0][1]
    assert list(overview_update.update_mask.paths) == ["aspects"]
    assert _aspect_data(overview_update, "dataplex-types.global.overview") == (
        "projects/dataplex-types/locations/global/aspectTypes/overview", {"content": "<p>orders</p>"})
    queries_update = transport._client.calls[2][1]
    assert _aspect_data(queries_update, "dataplex-types.global.queries") == (
        "projects/dataplex-types/locations/global/aspectTypes/queries",
        {"queries": [{"description": "one?", "sql": "SELECT 1", "source": "USER"}], "userManaged": True})
    link = calls["create_entry_link"]
    assert linked == "created"
    assert link.parent == "projects/123456789012/locations/us/entryGroups/@bigquery"
    assert link.entry_link_id == "orders-net-revenue"
    assert link.entry_link.entry_link_type == "projects/dataplex-types/locations/global/entryLinkTypes/definition"
    assert [(r.name, r.type_.name) for r in link.entry_link.entry_references] == [(TABLE, "SOURCE"), (term, "TARGET")]
    created = calls["create_entry"]
    assert query_entry == ("projects/my-project/locations/us/entryGroups/kb-queries/entries/q-17", "created")
    assert created.entry.entry_type == "projects/my-project/locations/us/entryTypes/verified-query"
    assert created.entry.entry_source.description == "SELECT count(*) FROM orders"
    assert dict(created.entry.entry_source.labels) == {"source": "gold"}


def test_one_credentials_lookup_is_shared_by_both_clients(monkeypatch):
    """The glossary client was built with credentials=None, so google.auth
    looked up again, mid-push, with the catalog channel already open. That
    lookup shells out to gcloud, and grpc's fork handler printed "FD from
    fork parent still in poll list" in the gap before the first term."""
    pytest.importorskip("google.cloud.dataplex_v1")
    import google.auth
    from google.auth.credentials import AnonymousCredentials
    from context_lift.catalog import DataplexTransport
    anonymous = AnonymousCredentials()
    lookups = []
    monkeypatch.setattr(google.auth, "default",
                        lambda *args, **kwargs: (lookups.append(kwargs), (anonymous, "p"))[1])

    transport = DataplexTransport("my-project", "us")
    built = []
    named = []

    def fake_glossary_client(credentials=None, client_info=None):
        built.append(credentials)
        named.append(client_info)
        return "glossary client"

    monkeypatch.setattr(transport._types, "BusinessGlossaryServiceClient",
                        fake_glossary_client)
    transport._glossary()
    transport._glossary()

    assert len(lookups) == 1 and transport._credentials is anonymous
    assert built == [anonymous]          # one client, on the credentials already in hand
    # and it carries the framework's user agent, like every other client
    assert named and named[0] is not None
    assert named[0].to_user_agent().startswith("context-lift/")


def test_a_403_on_a_write_becomes_a_system_exit_naming_the_role():
    errors = pytest.importorskip("google.api_core.exceptions")
    transport = _offline_transport(raise_on={"update_entry": errors.PermissionDenied("no")})

    with pytest.raises(SystemExit) as stopped:
        transport.write_overview(TABLE, "<p>orders</p>")

    assert "roles/dataplex.entryOwner" in str(stopped.value)
    assert "projects/123456789012/locations/us/entryGroups/@bigquery" in str(stopped.value)


def test_a_refused_queries_aspect_is_write_unsupported_not_a_crash():
    errors = pytest.importorskip("google.api_core.exceptions")
    transport = _offline_transport(raise_on={"update_entry": errors.InvalidArgument("system aspect")})

    with pytest.raises(WriteUnsupported) as refused:
        transport.write_queries(TABLE, [{"sql": "SELECT 1", "question": "one?"}])

    assert "system aspect" in str(refused.value)


def test_an_existing_query_entry_answers_conflict_and_is_updated_in_place():
    """The earlier project saw plain Conflict, not AlreadyExists, for a thing
    that already existed; the transport catches both."""
    errors = pytest.importorskip("google.api_core.exceptions")
    transport = _offline_transport(raise_on={"create_entry": errors.Conflict("exists")})

    name, status = transport.upsert_query_entry("kb-queries", "q-17", "How many?", "SELECT 1", {})

    assert status == "updated"
    update = dict(transport._client.calls)["update_entry"]
    assert update.entry.name == name and list(update.update_mask.paths) == ["entry_source"]


class _ThreeGroups(FakeCatalog):
    """The same table in three entry groups, the shape one estate had: a
    scratch group, a proof-of-concept group and the live one."""

    def search(self, query):
        return [f"projects/1/locations/global/entryGroups/{g}/entries/sales__orders"
                for g in ("scratch", "pilot", "live")]


def test_a_table_in_several_groups_refuses_unless_the_group_is_named():
    from context_lift.catalog import AmbiguousEntry
    store = KnowledgeCatalogStore(_ThreeGroups({}), entry_group=None, sleep=lambda s: None,
                                  entry_id_pattern="{dataset}__{table}")

    with pytest.raises(AmbiguousEntry) as refused:
        store.resolve("sales.orders")
    assert "3 entry groups: live, pilot, scratch" in str(refused.value)
    assert "nothing was read or written" in str(refused.value)

    chosen = KnowledgeCatalogStore(_ThreeGroups({}), sleep=lambda s: None,
                                   entry_id_pattern="{dataset}__{table}", entry_group="scratch")
    assert chosen.resolve("sales.orders").endswith("/entryGroups/scratch/entries/sales__orders")


def test_removing_an_aspect_sets_the_flag_that_makes_the_delete_real():
    """Measured 2026-09-09: without `delete_missing_aspects` the same call
    returns 200 OK, echoes the key back, and leaves the aspect in place. A
    reset written the obvious way looks like it worked on every run."""
    pytest.importorskip("google.api_core.exceptions")
    transport = _offline_transport()

    transport.remove_aspect("projects/1/locations/us/entryGroups/@bigquery/entries/t",
                            "655216118709.global.overview")

    update = dict(transport._client.calls)["update_entry"]
    assert update.delete_missing_aspects is True
    assert list(update.aspect_keys) == ["655216118709.global.overview"]
    assert list(update.update_mask.paths) == ["aspects"]
