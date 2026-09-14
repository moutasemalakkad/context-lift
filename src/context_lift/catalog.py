# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Reading table documentation out of a data catalog, and writing it back.

A `DocumentStore` over Google Cloud's Knowledge Catalog (the Dataplex catalog).
The network lives behind `CatalogTransport` and is injected, so everything below
- the retries, the scoping check, the budget, the parsing - is tested offline
against a fake. The eight behaviours encoded here each cost someone hours on a
live deployment; the HTTP is the easy part.

The write side follows one rule from the field notes: verify a write by reading
it back and compare lengths. A catalog that accepts the bytes and stores fewer
of them looks exactly like one that worked. So every write here has a matching
read (`read_entry` for what was stored, `read_now` for what a reader gets), and
the caller compares.

Exports: CatalogTransport, KnowledgeCatalogStore, DataplexTransport, transport_for,
table_key_of, entry_group_of, project_number_of, term_entry_name, merge_queries,
compact_sql, budget_that_serves, short_serve_note, TRANSIENT_TABLE,
DEFAULT_ENTRY_GROUP, BIGQUERY_ENTRY_ID, ScopeCheckFailed, WriteUnsupported,
AmbiguousEntry.

Read a catalog into a file before retrieving from it live. A file is
reproducible by a reader without your credentials; a live arm cannot be audited
by anyone who was not there. Live retrieval answers a different question - "did
my retrieval pipeline help", with the retriever's own failures inside the
measurement - and that is the rarer one.

See docs/CONTEXT-SOURCES.md for how much each behaviour below is trusted: two
were probed a second time against a live store, the rest were observed once.
"""
from __future__ import annotations

import re
from typing import Protocol

from . import identity

from .context import fetch_document
from .draft import READ_BUDGET_CHARS

# A real overview document is tens of kilobytes. Anything far under that is a
# stub the API hands back under burst load, and a writer fed a stub invents
# column names. Absolute rather than relative: on a first fetch there is no
# previous good value to compare against. A short body has a second cause
# this threshold cannot tell apart, and retrying never clears it: a page
# longer than the read budget is served as a few hundred characters of its
# opening for as long as the budget stays where it is. Compare the served
# length with the stored one (`short_serve_note`) before calling an entry thin.
MIN_GOOD_CHARS = 1500

# lookupContext truncates at a default budget. One deployment's SQL conventions
# sit at the END of the overview, so a small budget silently returns the prose
# and drops the rules - a context arm that looks populated and is missing the
# payload the experiment measures. Never rely on the default. One constant,
# shared with draft-context, which warns when a page it writes is over it.
#
# The cut is not a cut: the read serves a page whole or not at all at the
# budget it is given. Measured on a live estate, 2026-09-05, same entry and
# same request with only the budget changed: an entry storing 71,690
# characters answered 572 characters at 20,000 and at 60,000, and 78,854 at
# 80,000, 100,000 and 200,000; a second entry storing 92,397 answered 532 at
# 60,000 and 101,385 at 150,000. All ten calls were HTTP 200 with no warning
# field and no truncation flag, and the 572-character body is a well-formed
# page - the opening of the overview, no schema section, no column names -
# which is why a reader that only logs the length calls the entry thin.
CONTEXT_BUDGET_CHARS = READ_BUDGET_CHARS

# System aspect and link types live in a Google-managed project. A write names
# the type by its alias (`dataplex-types`) and keys the aspect by the alias
# form; a read gets the key back with that project's NUMBER in front
# ("655216118709.global.overview"), so reads match by suffix. Both halves were
# observed on the earlier project.
OVERVIEW_ASPECT_TYPE = "projects/dataplex-types/locations/global/aspectTypes/overview"
OVERVIEW_ASPECT_KEY = "dataplex-types.global.overview"
QUERIES_ASPECT_TYPE = "projects/dataplex-types/locations/global/aspectTypes/queries"
QUERIES_ASPECT_KEY = "dataplex-types.global.queries"
DEFINITION_LINK_TYPE = "projects/dataplex-types/locations/global/entryLinkTypes/definition"

# `content` of the overview aspect holds at most 100 KiB (the aspect type's own
# description, read with `gcloud dataplex aspect-types describe overview`).
# The field notes saw the cut happen with no error, which is why a write is
# verified by reading it back.
OVERVIEW_MAX_BYTES = 100 * 1024

# A query entry stores the SQL in `entry_source.description`, and the API
# refuses more than 2000 characters there (its own error text on the earlier
# project). The question goes in the display name, cut at 200 as that project
# did; the API's limit for the display name was not probed.
ENTRY_DESCRIPTION_LIMIT = 2000
ENTRY_DISPLAY_NAME_LIMIT = 200
QUERY_ENTRY_TYPE_ID = "verified-query"

# Glossaries are created at `locations/global`; the term entry name that an
# entry link points at is built from the same location.
GLOSSARY_LOCATION = "global"

# BigQuery's own entries live in the `@bigquery` group of the dataset's
# location, and every entry name there had this one shape on both estates
# measured (the catalog's `--project` fills `{project}`). An entry name is
# the only exact address the API has: LookupEntry takes no fully-qualified
# name, and search given the complete fully-qualified name of a table still
# returned four hits with a different table first.
DEFAULT_ENTRY_GROUP = "@bigquery"
BIGQUERY_ENTRY_ID = "bigquery.googleapis.com/projects/{project}/datasets/{dataset}/tables/{table}"
# The same name with the /tables/ segment cut off. A dataset has its own
# entry and its own overview aspect, which is the only home a rule above
# the table has: measured on 2026-09-10, the write was accepted and read
# back byte for byte.
BIGQUERY_DATASET_ENTRY_ID = "bigquery.googleapis.com/projects/{project}/datasets/{dataset}"

# dbt builds a table through `<table>__dbt_tmp` and keeps `<table>__dbt_backup`
# while a build runs; both live for minutes. The catalog lists their entries
# for longer than the tables exist: one cold run's table list carried such a
# name, and the push then died reading its entry. Never a table to document.
TRANSIENT_TABLE = re.compile(r"__dbt_(tmp|backup)$", re.I)


# the served bundle wraps the stored page in schema YAML, so it is always the
# larger of the two, and it is the served size the budget is measured against.
# Across the 11 starved pages measured on two estates the ratio sat between
# 1.097 and 1.101; this margin clears the widest of them with room, because the
# schema section grows with the column count and a page with a short overview
# on a very wide table could stretch the ratio past anything observed.
SERVED_OVER_STORED = 1.25


def budget_that_serves(stored_chars: int) -> int:
    """A read budget that would serve a page of `stored_chars`, to the next 10,000.

    The budget is compared against the SERVED bundle, not the stored page, and
    the bundle is the larger of the two. Rounding the stored size up to the
    next 10,000 is therefore not enough: 92,397 stored is served as 101,385,
    and a caller told to raise the budget to 100,000 gets the same stub back.
    Measured on nine starved pages, that arithmetic named a working budget on
    four of them and only because the rounding happened to clear the bundle.

    So scale by `SERVED_OVER_STORED` first, then round up. The result is a
    floor with a margin, not the exact cliff; the cliff cannot be computed
    from a starved read, because a starved read never reveals the bundle size.
    An oversized budget is not known to cost anything: the reply saturates at
    the content size, and 80,000, 100,000 and 200,000 all returned the same
    78,854 characters for one page.
    """
    return (int(stored_chars * SERVED_OVER_STORED) // 10_000 + 1) * 10_000


def short_serve_note(served_chars: int, stored_chars: int, budget: int) -> str | None:
    """What this table's reader is missing, None when it is missing nothing.

    The test is the shortfall itself, not a fraction of it. The served bundle
    carries the schema and the profile around the page, so a whole page comes
    back LONGER than the stored overview (78,854 served for 71,690 stored,
    101,385 for 92,397), and a served body under the stored size means the
    reader did not get the page. The two stub bodies measured were 572 of
    71,690 and 532 of 92,397, one character in 125 and one in 174; nothing
    between a stub and a whole page has been seen, so there is no measured
    middle to put a round threshold in.
    """
    if stored_chars <= 0 or served_chars >= stored_chars:
        return None
    if served_chars == 0:
        # nothing served has a second cause the budget cannot fix: for a while
        # after a write the catalog answers 0 characters (field notes)
        return (f"the reader is served nothing where the entry stores {stored_chars} characters, at "
                f"budget {budget}; the catalog answers 0 characters for a while after a write, so "
                f"pull again, and raise --budget to {budget_that_serves(stored_chars)} if it stays at 0")
    return (f"the reader is served {served_chars} characters where the entry stores "
            f"{stored_chars}, at budget {budget}; raise --budget to {budget_that_serves(stored_chars)}")


def table_key_of(entry_name: str) -> str:
    """`dataset.table` from an entry name.

    BigQuery's own entries end in `/datasets/D/tables/T`; a custom group's
    end in `/entries/D__T` on the deployment measured here. Anything else is
    returned as its last segment, so a caller sees a name and not a crash. A
    BigQuery dataset entry ends in `/datasets/D` and so keys as `D`, with no
    dot, which is how `list_tables` tells it from a table: the segments
    before it hold `bigquery.googleapis.com`, whose dots would pass for one.
    """
    found = re.search(r"/datasets/([^/]+)/tables/([^/]+)$", entry_name)
    if found:
        return f"{found.group(1)}.{found.group(2)}"
    tail = entry_name.rsplit("/entries/", 1)[-1].rsplit("/", 1)[-1]
    return tail.replace("__", ".", 1) if "__" in tail else tail


def entry_group_of(entry_name: str) -> str:
    """Everything before `/entries/`: `projects/N/locations/L/entryGroups/G`.

    An entry link is created in the group of its SOURCE entry (public docs),
    and a permission error names the group to grant on, so both need this
    from a name they were handed.
    """
    return entry_name.split("/entries/", 1)[0]


def project_number_of(entry_name: str) -> str:
    """The digits after `projects/`, or "" when the name carries an id.

    Entry names carry the project number, never the id (field notes), and
    the term entry name that a link points at needs the number twice.
    """
    found = re.match(r"projects/(\d+)(?:/|$)", entry_name)
    return found.group(1) if found else ""


def term_entry_name(project_number: str, glossary_id: str, term_id: str) -> str:
    """The catalog entry that stands for a glossary term (public docs).

    It sits in the `@dataplex` system group and wraps the term's own resource
    name, so the number appears twice. This is the TARGET of a definition
    link.
    """
    term = f"projects/{project_number}/locations/{GLOSSARY_LOCATION}/glossaries/{glossary_id}/terms/{term_id}"
    return f"projects/{project_number}/locations/{GLOSSARY_LOCATION}/entryGroups/@dataplex/entries/{term}"


def merge_queries(existing: list[dict], new: list[dict]) -> list[dict]:
    """The rows of a `queries` aspect after adding `new` ({"sql", "question"}).

    Row shape from `gcloud dataplex aspect-types describe queries` (project
    `dataplex-types`, 2026-09-05): `description` and `sql` required, `source`
    AGENT or USER required, `sqlDialect` optional. The product's own rows
    (source AGENT) stay, so a push does not erase what a documentation scan
    generated. A new row whose SQL matches an existing row, whitespace
    collapsed, replaces it rather than duplicating it; ours are source USER,
    which the product says it will not overwrite.
    """
    merged = [dict(row) for row in existing]
    position = {" ".join(str(row.get("sql", "")).split()): index
                for index, row in enumerate(merged)}
    for item in new:
        row = {"description": item["question"], "sql": item["sql"], "source": "USER"}
        sql_key = " ".join(item["sql"].split())
        if sql_key in position:
            merged[position[sql_key]] = row
        else:
            position[sql_key] = len(merged)
            merged.append(row)
    return merged


def compact_sql(sql: str) -> str:
    """One line with single spaces, so the SQL fits an entry description.

    Over `ENTRY_DESCRIPTION_LIMIT` the query is refused, not cut: the earlier
    project cut with a marker, and a truncated query served to a model is
    worse than no query. `WriteUnsupported` so a push skips it and reports.
    """
    text = " ".join(sql.split())
    if len(text) > ENTRY_DESCRIPTION_LIMIT:
        raise WriteUnsupported(
            f"the SQL is {len(text)} characters after collapsing whitespace; "
            f"an entry description holds {ENTRY_DESCRIPTION_LIMIT}")
    return text


class AmbiguousEntry(SystemExit):
    """A table name that resolves in more than one entry group and no
    --entry-group to choose; refused rather than picked."""


def _is_not_found(error: BaseException) -> bool:
    """A missing entry, whichever class the client raised: api_core's
    NotFound, the raw gRPC error whose text carries NOT_FOUND (the shape a
    cold run died with), or the API's own words `does not exist`."""
    text = str(error)
    return "NotFound" in type(error).__name__ or "NOT_FOUND" in text or "does not exist" in text


class ScopeCheckFailed(RuntimeError):
    """A search that should have matched returned nothing.

    Catalog search answers an over-scoped or under-permissioned query with zero
    results and HTTP 200, so an empty catalog and a broken one are the same
    response. Two causes are confirmed: an entry-group name-prefix predicate
    in keyword mode (0 results; the same predicate in semantic mode finds the
    table), and no search permission on the --project (needs
    roles/dataplex.catalogViewer there). A quota project on the credentials
    is not a cause; that diagnosis was re-tested and retracted
    (docs/CONTEXT-SOURCES.md). Every adapter has to prove its scoping works
    against a query it knows should match, or it will report a clean empty
    catalog forever.
    """


class WriteUnsupported(RuntimeError):
    """The catalog refused a write the caller can do without.

    Raised for the `queries` system aspect when the API will not take a user
    write of it, and for a query too long for an entry description. A caller
    treats it as "not here, use the other path" rather than as a failure of
    the push. A 403 is not this: that is a missing role and raises SystemExit.
    """


class CatalogTransport(Protocol):
    """The network calls, so the behaviour above can be tested without them.

    Resolution builds the entry name from the table key when the group's id
    shape is known, and searches only when it is not: the name a person uses
    for a table is not the name the catalog stores it under, and the search
    can legitimately match nothing.

    A transport that lists an entry group to build a table set must keep only
    entries whose `parentEntry` is set. A live group held 129 entries for 127
    tables: the group's self-entry (type `entrygroup`) and an index entry whose
    type is `generic`, the same string every real table carries, so a type
    filter cannot tell them apart and `parentEntry` can.

    The write methods each return what happened ("created", "updated",
    "unchanged", "existed") so a push can report counts and a second run is
    visibly idempotent. Each is paired with a read: `read_entry` shows what
    the catalog stored, `lookup` what a reader gets.
    """

    def search(self, query: str) -> list[str]:
        ...

    def lookup(self, entry_name: str, context_budget: int) -> str:
        ...

    def list_entries(self, entry_group: str) -> list[dict]:
        """Each {"name", "entry_type", "parent_entry"}; `entry_group` is
        `@bigquery` or a custom group id."""
        ...

    def read_entry(self, entry_name: str) -> dict:
        """{"name", "overview": str | None, "queries": list[dict] | None,
        "display_name", "description", "labels": dict}. The overview is
        matched by the `.global.overview` key suffix; None when the aspect
        is absent."""
        ...

    def write_overview(self, entry_name: str, text: str) -> None:
        ...

    def write_queries(self, entry_name: str, queries: list[dict]) -> None:
        """Each {"sql", "question"}; merged with the rows already there by
        SQL text, marked user managed. Raises WriteUnsupported when the API
        refuses this aspect."""
        ...

    def upsert_term(self, glossary_id: str, term_id: str, display_name: str,
                    description: str, labels: dict,
                    category_id: str = "") -> tuple[str, str]:
        """(term entry name, "created" | "updated" | "unchanged"). Creates the
        glossary at `locations/global` when it is missing.

        `category_id` files the term under a team category instead of directly
        under the glossary. The URL parent stays the GLOSSARY either way; the
        BODY parent is what selects the category. That asymmetry is in no
        reference page and was confirmed by a live probe on 2026-09-13
        (`docs/evidence/glossary-tiers.json`, verdict TERM UNDER A CATEGORY
        WORKS)."""
        ...

    def read_terms(self, glossary_id: str) -> list[dict]:
        """Every term in the glossary: `term_id`, `display_name`,
        `description`, `labels`, `category_id` ("" when the term sits directly
        under the glossary).

        This is what makes a term part of a measured arm. A term that can be
        written and not read is a term whose arm hash cannot be checked, which
        is why `write_catalog` refused to write any until this existed.

        ONE call covers both tiers: a glossary-level `terms.list` returns the
        terms filed under categories as well, so a caller does not walk the
        categories. Also confirmed by the 2026-09-13 probe
        (`one_list_sees_both_tiers: true`)."""
        ...

    def delete_term(self, glossary_id: str, term_id: str) -> str:
        """"deleted" | "missing". Used only to remove a term THIS tool wrote
        and the spec no longer has. A term without our label is never passed
        here: a glossary is shared, and deleting a colleague's definition to
        make a hash match is not a trade worth making."""
        ...

    def link_term(self, source_entry: str, term_entry: str, link_id: str) -> str:
        """"created" | "existed"; the link lives in the entry group of
        `source_entry`."""
        ...

    def upsert_query_entry(self, group_id: str, entry_id: str, question: str, sql: str,
                           labels: dict) -> tuple[str, str]:
        """(entry name, "created" | "updated"); ensures the entry type
        `verified-query` and the group under `projects/P/locations/L`."""
        ...


class KnowledgeCatalogStore:
    """One document per table, read from a catalog.

    Addressing is a pure function of the table key. With `entry_id_template`
    (the entry id inside the group, with `{dataset}`, `{table}` and
    `{project}` placeholders) `resolve` builds
    `projects/P/locations/L/entryGroups/G/entries/ID` and searches nothing.
    BigQuery's own group (`@bigquery`, the default) has one documented shape
    on every estate measured, so it is the default template there; a custom
    group's ids are whatever whoever registered the entries chose
    (`{dataset}__{table}` on the estates measured), so it has no default and
    takes one from `--entry-id`. Two measurements put search out of the
    addressing path: an earlier substring resolve put 12 of 99 pages on a
    sibling table's entry, and the same table resolved right in one run and
    wrong minutes later with only the search ranking changed; and the
    complete fully-qualified name as the query returned four hits with a
    different table first. Search stays for routing questions (`route`)
    and, when no id shape is known (`entry_group=None`, or a custom group
    without a template), for the exact-key fallback that
    `entry_id_pattern` drives: the search term built from a `dataset.table`.

    Of the write side, only the overview is wrapped here, because it pairs with
    `stored_overview` and `read_now`: the write, the stored bytes and what a
    reader gets, side by side, which is the verify-by-reading-back rule in one
    place. Terms, links and query entries have no store-level rule (no cache,
    no retry, no filtering), so a push calls `store.transport` for them
    directly rather than through a pass-through that would add a name and no
    behaviour.
    """

    def __init__(self, transport: CatalogTransport, *, project: str = "", location: str = "",
                 entry_group: str | None = DEFAULT_ENTRY_GROUP,
                 entry_id_template: str | None = None,
                 entry_id_pattern: str = "name:{table} type=table",
                 search_pattern: str = "{question} type=table",
                 known_good_table: str | None = None,
                 glossary_id: str = "",
                 min_good_chars: int = MIN_GOOD_CHARS,
                 context_budget: int = CONTEXT_BUDGET_CHARS,
                 sleep=None):
        self.transport = transport
        # Which glossary this catalog's business terms live in. It sits here
        # beside `entry_group` because it answers the same question -- where
        # in the catalog this estate's knowledge is -- and because a READ
        # needs it: a push could take it as an argument, a round reading a
        # spec back has nowhere to put it. Empty means read no terms, which
        # is the right default for an estate that keeps none.
        self.glossary_id = glossary_id
        # the live transport knows the project and location it was built
        # for; a caller that gave neither still gets a whole entry name
        self.project = project or getattr(transport, "project", "")
        self.location = location or getattr(transport, "location", "")
        self.entry_id_pattern = entry_id_pattern
        # what `route` sends to search for a question; a deployment whose
        # tables are custom entries may need another type filter, or none
        self.search_pattern = search_pattern
        self.entry_group = entry_group
        # whether the caller typed a template or got the built-in one: a
        # refusal that blames --entry-id for a flag nobody passed sends the
        # reader after the wrong thing
        self.entry_id_given = entry_id_template is not None
        if entry_id_template is None and entry_group == DEFAULT_ENTRY_GROUP:
            entry_id_template = BIGQUERY_ENTRY_ID
        self.entry_id_template = entry_id_template
        self._template_checked = False
        self.names_not_found: list[str] = []
        self.min_good_chars = min_good_chars
        self.context_budget = context_budget
        # the backoff waits seconds; a test that exercises the retry path must
        # be able to pass a no-op, or the fast suite stops being fast
        self.sleep = sleep
        if known_good_table is not None:
            self.check_scope(known_good_table)

    def check_scope(self, known_good_table: str) -> None:
        """Prove the search works before trusting any empty result after it."""
        if not self.transport.search(self._term(known_good_table)):
            raise ScopeCheckFailed(
                f"catalog search for {known_good_table!r} returned nothing, and that table is "
                "supposed to exist. Search answers a broken query with zero results and no "
                "error, so an empty catalog and a broken one look identical. Two confirmed "
                "causes: an entry-group name-prefix predicate in keyword mode (use semantic "
                "mode or drop the prefix), and no search permission on --project (needs "
                "roles/dataplex.catalogViewer there). A quota project on the credentials is "
                "not a cause; that diagnosis was re-tested and retracted "
                "(docs/CONTEXT-SOURCES.md).")

    def _term(self, table: str) -> str:
        dataset, _, name = table.rpartition(".")
        return self.entry_id_pattern.format(dataset=dataset, table=name)

    def resolve(self, table: str) -> str | None:
        """The catalog's name for this table.

        With a template the name is built and never searched; built names
        are read until one exists (`_check_template`), because a name that
        is missing can be a table nobody catalogued or a template that fits
        no entry in the group. Without a template, the search path: a hit
        counts only when its entry name keys (`table_key_of`) to the
        requested `dataset.table`, a request with no dataset matching on
        the last segment alone, and nothing else is taken whatever the
        search returned. An earlier version fell
        back to the first hit when none matched exactly, and on one estate
        the search for `some_dataset.some_table__dbt_tmp` returned a stale
        transient-table entry of a DIFFERENT dataset and table first: the
        dry run died reading that entry, and a real push would have written
        the page to the wrong table. Finding nothing is a normal outcome
        (not every table is documented) and is reported as no context, not
        an error. The same name in several entry groups refuses unless
        `entry_group` names one.
        """
        if self.entry_id_template is not None:
            name = self._address(table)
            if not self._template_checked:
                self._check_template(name)
            return name
        dataset, _, name = table.rpartition(".")
        candidates = []
        for entry in self.transport.search(self._term(table)):
            key = table_key_of(entry).lower()
            if key == table.lower() or (not dataset and key.rpartition(".")[2] == name.lower()):
                candidates.append(entry)
        if self.entry_group:
            candidates = [e for e in candidates if f"/entryGroups/{self.entry_group}/" in e]
        else:
            # a table name can live in several groups of one project: a scratch
            # group, a proof-of-concept group and the customer's live one. A
            # silent choice between them is the failure that costs an
            # engagement, so more than one group refuses and names them.
            groups = sorted({entry_group_of(e).rsplit("/", 1)[-1] for e in candidates if "/entryGroups/" in e})
            if len(groups) > 1:
                raise AmbiguousEntry(
                    f"{table!r} resolves in {len(groups)} entry groups: {', '.join(groups)}. "
                    "Pass --entry-group to say which one is meant; nothing was read or written.")
        return (candidates or [None])[0]

    def resolve_dataset(self, dataset: str) -> str | None:
        """The entry name of a dataset, from `project.dataset` or `dataset`.

        Rules above the table - the estate's definitions of revenue, of a
        completed order - have no table to sit on, and every one of thelook's
        rules is that kind. Before this they were refused with "the catalog
        has no entry the agent reads at that level", which was wrong: the
        dataset entry exists and takes an overview.

        Returns None when the entry is not there, the same as `resolve`, so a
        caller treats a missing dataset the way it treats a missing table.
        """
        parts = [part.strip("`\"'") for part in dataset.split(".")]
        project, name = (parts if len(parts) == 2 else (self.project, parts[0]))
        entry_id = BIGQUERY_DATASET_ENTRY_ID.format(project=project, dataset=name)
        entry = (f"projects/{self.project}/locations/{self.location}"
                 f"/entryGroups/{self.entry_group}/entries/{entry_id}")
        try:
            self.transport.read_entry(entry)
        except Exception as error:  # noqa: BLE001 - a missing dataset is not an error
            if _is_not_found(error):
                return None
            raise
        return entry

    def _address(self, table: str) -> str:
        """Build the entry name from the parts of the table name.

        A table name has one, two or three dot-separated parts. Backticks
        and quotes around a part are dropped. A 3-part name carries its own
        project; a 2-part name takes the store's project; a bare name takes
        the store's project and an empty dataset. Splitting from the right
        alone put `project.dataset` into the dataset slot of a 3-part name."""
        parts = [part.strip("`\"'") for part in table.split(".")]
        if len(parts) == 3:
            project, dataset, name = parts
        elif len(parts) == 2:
            project, dataset, name = self.project, *parts
        else:
            project, dataset, name = self.project, "", parts[0]
        entry_id = self.entry_id_template.format(dataset=dataset, table=name, project=project)
        return (f"projects/{self.project}/locations/{self.location}/entryGroups/{self.entry_group}"
                f"/entries/{entry_id}")

    def _check_template(self, name: str) -> None:
        """Read built names through get_entry until one of them exists.

        A missing entry at one built name has two causes that look alike: the
        template does not fit the group, or that one table is not catalogued.
        Treating the first name as the answer refused a whole pull because
        the first of 13 tables was gone, and blamed --entry-id, which the
        caller had not passed. So a missing name is counted here and the
        caller carries on with it (a lookup answers nothing, a push's read
        raises and is that page's failed line); the first name that exists
        settles the template and stops the reads; and only a run where no
        built name existed at all has `template_mismatch` to report."""
        try:
            self.transport.read_entry(name)
        except Exception as error:  # noqa: BLE001 - only a not-found is the template's suspect
            if not _is_not_found(error):
                raise
            if name not in self.names_not_found:
                self.names_not_found.append(name)
            return
        self._template_checked = True

    def template_mismatch(self) -> str | None:
        """Why nothing was found, when NO built name of the run existed;
        None as soon as one did, and None when no name was built at all.
        The caller prints it and stops: with the template wrong, every table
        of the run is missing and the reason is one flag, not the data."""
        if self._template_checked or not self.names_not_found:
            return None
        shape = (f"--entry-id {self.entry_id_template}" if self.entry_id_given
                 else f"the built-in id shape of {self.entry_group}, {self.entry_id_template}")
        return (f"not one of the entry names built from {shape} exists in entry group "
                f"{self.entry_group} ({len(self.names_not_found)} tried, the first "
                f"{self.names_not_found[0]}). An entry name carries the project the tables live "
                "in and the location of their dataset, so check --project and --location before "
                f"the ids; catalog-list --entry-group {self.entry_group} shows the ids this group "
                "holds, and a custom group needs --entry-group G --entry-id TEMPLATE")

    def fetch(self, entry_name: str) -> str:
        return self.transport.lookup(entry_name, context_budget=self.context_budget)

    def route(self, question: str, top: int = 3, within: str | list[str] | None = None,
              keep: set[str] | None = None, dropped: list[str] | None = None,
              outside_group: list[str] | None = None) -> list[str]:
        """The tables the catalog's own search thinks a question is about.

        Semantic search when the transport offers it, keyword search
        otherwise; the default `search_pattern` adds `type=table` to keep
        glossary terms out. `entry_group` keeps only hits that live in that
        group, on the entry name the search returned: an entry-group
        predicate inside the query worked in semantic mode and answered
        zero results in keyword mode on the estate measured, so the scoping
        cannot be left to the query. The keys it removes go to
        `outside_group`, because a table dropped without a word is a
        routing result nobody can explain. `within` keeps one dataset or
        several. `keep` is the table list the routing is graded against
        (catalog-list's file): a routed table outside it is dropped before
        the top-N cut, and appended to `dropped`, so the cut is over tables
        that can score and the printout can name what the search keeps
        returning. A retriever is measured before it is trusted: on 20 BIRD
        questions the gold's primary table was in the top 3 for 15 and
        first for 6, so the routing number is reported beside the SQL
        number.
        """
        search = getattr(self.transport, "search_semantic", self.transport.search)
        names = search(self.search_pattern.format(question=question))
        if self.entry_group:
            in_group = [name for name in names if f"/entryGroups/{self.entry_group}/" in name]
            wanted = {table_key_of(name) for name in in_group}
            if outside_group is not None:
                outside_group.extend(dict.fromkeys(
                    table_key_of(name) for name in names if table_key_of(name) not in wanted))
            names = in_group
        keys = [table_key_of(name) for name in names]
        unique: list[str] = []
        for key in keys:
            if key not in unique:
                unique.append(key)
        datasets = [within] if isinstance(within, str) else list(within or [])
        if datasets:
            unique = [k for k in unique if k.rpartition(".")[0] in datasets]
        if keep is not None:
            if dropped is not None:
                dropped.extend(k for k in unique if k not in keep)
            unique = [k for k in unique if k in keep]
        return unique[:top]

    def document_for(self, table: str) -> str:
        """This table's documentation, retried while the catalog is degraded."""
        return fetch_document(self, table, min_good_chars=self.min_good_chars,
                              sleep=self.sleep)

    def list_tables(self, entry_group: str = "@bigquery",
                    datasets: list[str] | None = None) -> tuple[list[str], dict]:
        """The `dataset.table` keys in one entry group, and why the rest were dropped.

        Three rules, each from a count that was wrong once. An entry with no
        `parent_entry` is not a table: the group's self-entry and its index
        entry have none, and their type cannot tell them apart from a table
        (field notes, 129 entries for 127 tables). An entry whose key has no
        dot is not a table either: a dataset entry has a parent and keys as
        its bare name. A dbt transient table (`TRANSIENT_TABLE`) is gone
        before anyone documents it. `datasets` keeps only those datasets.
        The counts add up to `listed`; the list is sorted and unique.
        """
        counts = {"listed": 0, "kept": 0, "no_parent": 0, "not_table_shaped": 0,
                  "transient": 0, "outside_datasets": 0}
        keys: set[str] = set()
        for entry in self.transport.list_entries(entry_group):
            counts["listed"] += 1
            if not entry.get("parent_entry"):
                counts["no_parent"] += 1
                continue
            key = table_key_of(entry["name"])
            if "." not in key:
                counts["not_table_shaped"] += 1
                continue
            if TRANSIENT_TABLE.search(key):
                counts["transient"] += 1
                continue
            if datasets is not None and key.rpartition(".")[0] not in datasets:
                counts["outside_datasets"] += 1
                continue
            counts["kept"] += 1
            keys.add(key)
        return sorted(keys), counts

    def entry_links(self, entry_name: str) -> list[dict]:
        """Join and definition links on one entry, as the gate reads them.

        The catalog mines join relationships from query history by itself,
        with no scan, so a link can appear that nobody wrote. The gate needs
        the source and the creation time to tell one mined from the estate's
        own traffic from one mined from the answer key.

        The source lives in an aspect on the LINK, not on the link itself,
        and aspects on entry links are Preview. A link whose source cannot be
        read is reported as UNKNOWN rather than assumed safe: assuming safe
        is how a mined link reaches the treatment arm unnoticed.
        """
        return self.transport.entry_links(entry_name)

    def read_entry(self, entry_name: str) -> dict:
        """The whole entry: overview, queries rows, display name, description.

        `read_catalog` in `export.py` needs `resolve` and `read_entry` on one
        object, and until this existed the only thing that had both was a fake
        in a test. So catalog mode passed its test and could not run once.

        A one-line delegation rather than moving `read_catalog` onto the
        transport: the transport speaks one API call, and the store is what
        knows how a table name becomes an entry name.
        """
        return self.transport.read_entry(entry_name)

    def stored_overview(self, entry_name: str) -> str | None:
        """What the catalog holds in the overview aspect, None when there is none.

        This is the stored side of the read-back rule: compare its length
        with what was written before believing the write.
        """
        return self.transport.read_entry(entry_name)["overview"]

    def write_overview(self, entry_name: str, text: str) -> None:
        """Replace the entry's overview. Read it back with `stored_overview`
        (what was kept) and `read_now` (what a reader gets) before trusting it."""
        self.transport.write_overview(entry_name, text)

    def read_now(self, entry_name: str) -> str:
        """What a reader gets for this entry right now, with no cache.

        `document_for` caches a good body for the life of the process, so
        after a write it would hand back the pre-write document and a verify
        step would pass on stale bytes. This goes to the transport every
        time. It does not retry: straight after a write the catalog answers
        with 0 characters for a while (field notes), so the caller loops with
        its own backoff and stops when the marker it wrote appears.
        """
        return self.transport.lookup(entry_name, context_budget=self.context_budget)


class DataplexTransport:
    """The network calls against a live catalog, one entry per call.

    The read side was written against a second project after the field
    notes, and each choice below is a measurement from that probe:

    - `search` runs in `locations/global` scoped to the project, keyword
      mode. `name:{table} type=table` matched exactly one entry; `name:{table}`
      alone also matched glossary terms and other projects' tables.
    - `lookup` asks for ONE entry. Two entries in one request came back as 23
      entry blocks and 43112 characters: the lookup expands to related
      entries, and a document for table A would then carry table B.
    - `context_budget` is best effort and coarse: 2000 and 500 both returned
      6287 characters of a 15606-character context, whole sections dropped.
      No budget returned all 15606. The budget is passed because a deployment
      needed it, not because it is exact.
    - A name that does not exist returns 0 characters and no error, which is
      why the store resolves by search first and treats an empty body as
      degraded rather than as an answer.

    The write side is graded per method: "verified on the earlier project"
    means the same request shape ran live there; "public docs" means the
    shape is from the documentation and has not run from this code yet;
    "not yet run live" means exactly that. A 403 becomes SystemExit naming
    the role to grant: `roles/dataplex.entryOwner` for aspects, links and
    entries, `roles/dataplex.catalogEditor` for glossaries and terms (the
    glossary docs' wording, checked 2026-09-05).
    """

    # Waits between attempts, in seconds. Widening, and short: a quota error
    # on a per-minute quota clears within the minute, and a run that waits
    # four minutes to find out a role is missing has helped nobody. Four
    # attempts and about seven seconds in all.
    BACKOFF = (1.0, 2.0, 4.0)

    def _retry(self, what: str, call):
        """One API call, retried while the failure is one that waiting fixes.

        Quota is the reason this exists. Data product updates are limited to
        10 per minute per project and entry writes to 1,500 per minute per
        region, and a push over a few hundred tables walks into both. Without
        this, one 429 in the middle of a long push ends it, and the entries
        already written stay written, so a re-run is not a clean re-run.

        Only ResourceExhausted, ServiceUnavailable and DeadlineExceeded are
        retried. NotFound, PermissionDenied and InvalidArgument never get
        better by waiting, and retrying them turns a clear error into a slow
        one: a missing role would take seven seconds to report instead of
        none, four times per table.
        """
        transient = (self._errors.ResourceExhausted,
                     self._errors.ServiceUnavailable,
                     self._errors.DeadlineExceeded)
        waited = 0.0
        for wait in (*self.BACKOFF, None):
            try:
                return call()
            except transient as slow:
                if wait is None:
                    raise SystemExit(
                        f"{what} failed {len(self.BACKOFF) + 1} times over "
                        f"{waited:g}s: {slow}. This is a quota or an outage, "
                        "not a permission problem. Re-run; entries already "
                        "written are written, and a push is safe to repeat"
                    ) from slow
                self._sleep(wait)
                waited += wait
        raise AssertionError("unreachable")  # pragma: no cover

    def __init__(self, project: str, location: str, *, credentials=None,
                 page_size: int = 10, sleep=None):
        # Injectable so a test can exercise the retry path without waiting
        # seven real seconds for it.
        import time as _time
        self._sleep = sleep or _time.sleep
        try:
            from google.cloud import dataplex_v1
        except ImportError as missing:
            raise SystemExit("the [catalog] extra is not installed in this Python: "
                             "uv pip install -e '.[catalog]'") from missing
        from google.api_core import exceptions as api_errors
        self._types = dataplex_v1
        self._errors = api_errors
        # One credentials lookup, before any channel exists, shared by both
        # clients. A client built with credentials=None runs
        # google.auth.default(), which shells out to `gcloud config
        # config-helper` for the project id under a user's ADC. Doing that a
        # second time to build the glossary client, with the catalog channel
        # already open, forks a process grpc knows about: every push printed
        # "I0905 ... ev_poll_posix.cc:593] FD from fork parent still in poll
        # list", twice, in the gap between the last page and the first term.
        if credentials is None:
            import google.auth
            credentials, _ = google.auth.default()
        self._credentials = credentials
        self._client = dataplex_v1.CatalogServiceClient(
            credentials=credentials, client_info=identity.client_info())
        self._glossary_client = None        # built on the first upsert_term
        self._glossaries_seen: set[str] = set()
        self._categories_seen: set[str] = set()
        self.project = project
        self.location = location
        self.page_size = page_size
        # entry names carry the number; when the caller gave the id, the
        # number is learned from the first name the catalog hands back
        self._project_number = project if project.isdigit() else ""

    def search(self, query: str) -> list[str]:
        request = self._types.SearchEntriesRequest(
            name=f"projects/{self.project}/locations/global", query=query,
            scope=f"projects/{self.project}", page_size=self.page_size)
        return [result.dataplex_entry.name for result in self._client.search_entries(request=request)]

    def search_semantic(self, query: str) -> list[str]:
        request = self._types.SearchEntriesRequest(
            name=f"projects/{self.project}/locations/global", query=query,
            scope=f"projects/{self.project}", page_size=self.page_size, semantic_search=True)
        return [result.dataplex_entry.name for result in self._client.search_entries(request=request)]

    def lookup(self, entry_name: str, context_budget: int) -> str:
        # the API rejects anything but a positive integer here, "None" included
        options = {"context_budget": str(context_budget)} if context_budget else {}
        request = self._types.LookupContextRequest(
            name=f"projects/{self.project}/locations/{self.location}",
            resources=[entry_name], options=options)
        return self._client.lookup_context(request=request).context

    # ---- reads that pair with the writes ---------------------------------

    def list_entries(self, entry_group: str) -> list[dict]:
        """Every entry of one group. Not yet run live from this code; the
        `parent_entry` rule it feeds is from the field notes. The pager
        follows page tokens; 100 per page keeps a 127-entry group to two
        calls."""
        parent = f"projects/{self.project}/locations/{self.location}/entryGroups/{entry_group}"
        try:
            return self._list_entries(parent)
        except Exception as exc:  # the client raises many classes; the name is what matters
            if type(exc).__name__ in ("PermissionDenied", "Forbidden"):
                raise SystemExit(
                    f"listing entry group {entry_group!r} at {self.location} in {self.project} was refused: "
                    "the account needs dataplex.entries.list there (roles/dataplex.catalogViewer); on a "
                    "customer estate the usual cause is that the wrong identity is active") from exc
            raise

    def _list_entries(self, parent: str) -> list[dict]:
        request = self._types.ListEntriesRequest(parent=parent, page_size=100)
        return [{"name": entry.name, "entry_type": entry.entry_type, "parent_entry": entry.parent_entry}
                for entry in self._client.list_entries(request=request)]

    def read_entry(self, entry_name: str) -> dict:
        """The stored entry with its aspects. Verified on the earlier project:
        `get_entry(view=ALL)` returns aspects keyed by the aspect type's
        project NUMBER, so the overview and queries keys are matched by
        suffix, and `lookupContext` does not return custom aspects while
        this does."""
        request = self._types.GetEntryRequest(name=entry_name, view=self._types.EntryView.ALL)
        entry = self._retry(f"reading {entry_name}",
                            lambda: self._client.get_entry(request=request))
        overview = self._aspect_data(entry, ".global.overview")
        queries = self._aspect_data(entry, ".global.queries")
        return {"name": entry.name,
                "overview": overview.get("content", "") if overview is not None else None,
                "queries": list(queries.get("queries", [])) if queries is not None else None,
                "display_name": entry.entry_source.display_name,
                "description": entry.entry_source.description,
                "labels": dict(entry.entry_source.labels)}

    def ensure_aspect_type(self, project: str, location: str, type_id: str,
                           fields: list[tuple[str, str]]) -> str:
        """The custom aspect type, created once and then found.

        Field indexes are immutable once set, so they are assigned in list
        order and a field is only ever appended. Renumbering an existing
        field is how a type stops accepting the aspects already written
        against it.

        A 403 here is `roles/dataplex.aspectTypeOwner` on the project, which
        `_denied` says in one sentence rather than leaving a raw error.
        """
        parent = f"projects/{project}/locations/{location}"
        name = f"{parent}/aspectTypes/{type_id}"
        try:
            self._client.get_aspect_type(request=self._types.GetAspectTypeRequest(name=name))
            return name
        except Exception as missing:  # noqa: BLE001 - only not-found is created
            if not _is_not_found(missing):
                raise

        template = self._types.AspectType.MetadataTemplate(
            name=type_id.replace("-", "_"), type_="record",
            record_fields=[
                self._types.AspectType.MetadataTemplate(
                    index=index, name=field_name, type_=field_type)
                for index, (field_name, field_type) in enumerate(fields, start=1)])
        try:
            operation = self._client.create_aspect_type(
                request=self._types.CreateAspectTypeRequest(
                    parent=parent, aspect_type_id=type_id,
                    aspect_type=self._types.AspectType(metadata_template=template)))
            operation.result()
        except self._errors.PermissionDenied as denied:
            raise self._denied(f"creating the aspect type {type_id}", parent,
                               "roles/dataplex.aspectTypeOwner", denied) from denied
        return name

    def write_custom_aspect(self, entry_name: str, key: str, aspect_type: str,
                            data: dict) -> None:
        """One custom aspect on one entry. The caller checks what is in it."""
        try:
            self._write_aspect(entry_name, key, aspect_type, data)
        except self._errors.PermissionDenied as denied:
            raise self._denied(f"writing {key} on {entry_name}",
                               entry_group_of(entry_name),
                               "roles/dataplex.entryOwner", denied) from denied

    def read_custom_aspect(self, entry_name: str, key_suffix: str) -> dict | None:
        """A custom aspect back off an entry, or None when it is not there."""
        request = self._types.GetEntryRequest(name=entry_name,
                                              view=self._types.EntryView.ALL)
        return self._aspect_data(self._client.get_entry(request=request), key_suffix)

    def entry_links(self, entry_name: str) -> list[dict]:
        """One lookup_entry_links call, flattened to what the gate reads."""
        location = "/".join(entry_name.split("/")[:4])
        request = self._types.LookupEntryLinksRequest(name=location, entry=entry_name)
        # A pager: the retry wraps the CALL that starts it, and each page it
        # then walks is a fresh request that can fail on its own. Wrapping the
        # whole loop instead would restart from page one on a late failure and
        # duplicate every link read so far.
        out = []
        pages = self._retry(f"listing links on {entry_name}",
                            lambda: self._client.lookup_entry_links(request=request))
        for link in pages:
            source = "UNKNOWN"
            for key, aspect in (link.aspects or {}).items():
                if "schema-join" in key or "definition" in key:
                    from google.protobuf import json_format
                    data = json_format.MessageToDict(type(aspect).pb(aspect).data)
                    source = str(data.get("source") or source).upper()
            out.append({
                "name": link.name,
                "type": str(link.entry_link_type),
                "source": source,
                "created_at": (link.create_time.replace(tzinfo=None)
                               if link.create_time else None),
            })
        return out

    def _aspect_data(self, entry, key_suffix: str) -> dict | None:
        from google.protobuf import json_format
        for key, aspect in entry.aspects.items():
            if key.endswith(key_suffix):
                return json_format.MessageToDict(type(aspect).pb(aspect).data)
        return None

    # ---- writes ------------------------------------------------------------

    def _write_aspect(self, entry_name: str, key: str, aspect_type: str, data: dict) -> None:
        """One aspect under its alias key, update mask `aspects`. Verified on
        the earlier project for the overview: the other aspects on the entry
        stayed (`delete_missing_aspects` is off by default)."""
        from google.protobuf import json_format, struct_pb2
        aspect = self._types.Aspect(aspect_type=aspect_type,
                                    data=json_format.ParseDict(data, struct_pb2.Struct()))
        entry = self._types.Entry(name=entry_name, aspects={key: aspect})
        self._retry(f"writing {key} on {entry_name}",
                    lambda: self._client.update_entry(
                        request=self._types.UpdateEntryRequest(
                            entry=entry, update_mask={"paths": ["aspects"]})))

    def remove_aspect(self, entry_name: str, key: str) -> None:
        """Take one aspect off an entry, for real.

        `delete_missing_aspects` is the whole point. Without it the same call
        returns 200 OK, reports the key back, and leaves the aspect exactly
        where it was. Measured on 2026-09-09: a PATCH carrying an empty
        `aspects` map and no such flag changed nothing, twice, and a read-back
        still returned the old content. A reset written the obvious way would
        have looked like it worked on every run.

        Used to put the evaluation room back to a clean control between
        rounds. Rolling a change back is a different job: that re-pushes the
        previous bytes, so the aspect stays and only its content moves.
        """
        entry = self._types.Entry(name=entry_name)
        try:
            self._client.update_entry(request=self._types.UpdateEntryRequest(
                entry=entry,
                update_mask={"paths": ["aspects"]},
                aspect_keys=[key],
                delete_missing_aspects=True))
        except self._errors.PermissionDenied as denied:
            raise self._denied(f"removing {key} from {entry_name}",
                               entry_group_of(entry_name),
                               "roles/dataplex.entryOwner", denied) from denied

    def _denied(self, action: str, where: str, role: str, error) -> SystemExit:
        """A 403 as one sentence: what failed, the API's own words, the role
        an admin grants and where."""
        detail = getattr(error, "message", None) or str(error)
        return SystemExit(f"{action} was denied: {detail}. Grant {role} on {where}.")

    def write_overview(self, entry_name: str, text: str) -> None:
        """Replace the overview aspect. Verified on the earlier project
        (2026-08-27): this exact request shape, on BigQuery's own entries in
        the `@bigquery` group. The aspect holds 100 KiB and cuts silently
        past that (field notes), so the caller reads back and compares."""
        try:
            self._write_aspect(entry_name, OVERVIEW_ASPECT_KEY, OVERVIEW_ASPECT_TYPE, {"content": text})
        except self._errors.PermissionDenied as denied:
            raise self._denied(f"writing the overview of {entry_name}", entry_group_of(entry_name),
                               "roles/dataplex.entryOwner", denied) from denied

    def write_queries(self, entry_name: str, queries: list[dict]) -> None:
        """Merge worked examples into the `queries` system aspect.

        Run live on 2026-09-09 against a BigQuery view's `@bigquery` entry: the
        API accepted a user write of this aspect with `source: USER` and
        `userManaged: true`, and `lookupContext` then served the row under the
        key `sampleQueries`. So the old open question is closed. The row shape
        is the aspect type's template (see `merge_queries`). WriteUnsupported
        stays for anything the API refuses other than a 403, so a push can
        still fall back to query entries."""
        existing = self.read_entry(entry_name)["queries"] or []
        data = {"queries": merge_queries(existing, queries), "userManaged": True}
        try:
            self._write_aspect(entry_name, QUERIES_ASPECT_KEY, QUERIES_ASPECT_TYPE, data)
        except self._errors.PermissionDenied as denied:
            raise self._denied(f"writing the queries aspect of {entry_name}", entry_group_of(entry_name),
                               "roles/dataplex.entryOwner (the aspect type's use permission is "
                               "dataplex.entryGroups.useQueriesAspect)", denied) from denied
        except self._errors.GoogleAPICallError as refused:
            raise WriteUnsupported(
                f"the catalog refused the queries aspect on {entry_name}: "
                f"{refused.message or refused}") from refused

    def _glossary(self):
        if self._glossary_client is None:
            self._glossary_client = self._types.BusinessGlossaryServiceClient(
                credentials=self._credentials, client_info=identity.client_info())
        return self._glossary_client

    def _ensure_glossary(self, glossary_id: str) -> str:
        """The glossary's resource name, creating it at `locations/global`
        when it is missing. Public docs plus the earlier project's
        `seed_glossary`, which created one the same way. Looked up once per
        transport; a push of forty terms should not ask forty times."""
        parent = f"projects/{self.project}/locations/{GLOSSARY_LOCATION}"
        name = f"{parent}/glossaries/{glossary_id}"
        if name in self._glossaries_seen:
            return name
        client = self._glossary()
        try:
            found = client.get_glossary(request=self._types.GetGlossaryRequest(name=name))
            name = found.name or name
        except self._errors.NotFound:
            glossary = self._types.Glossary(display_name=glossary_id,
                                            description="business terms pushed by contextlift")
            try:
                created = client.create_glossary(request=self._types.CreateGlossaryRequest(
                    parent=parent, glossary_id=glossary_id, glossary=glossary)).result()
                name = created.name or name
            except (self._errors.AlreadyExists, self._errors.Conflict):
                pass                    # created between the get and the create
            except self._errors.PermissionDenied as denied:
                raise self._denied(f"creating glossary {glossary_id}", parent,
                                   "roles/dataplex.catalogEditor", denied) from denied
        except self._errors.PermissionDenied as denied:
            raise self._denied(f"reading glossary {glossary_id}", parent,
                               "roles/dataplex.catalogEditor", denied) from denied
        self._glossaries_seen.add(name)
        return name

    def _number(self, *hints: str) -> str:
        """The project number, from the first name that carries one.

        The caller may have given the project id; entry names from the
        catalog always carry the number (field notes). The hints are names
        the API just returned; failing those, one search of the catalog
        gives any entry name. Not yet run live: whether the glossary API
        returns names with the number is the open question this answers."""
        for hint in hints:
            if project_number_of(hint):
                return project_number_of(hint)
        if not self._project_number:
            names = self.search("type=table")
            if not names:
                raise SystemExit("cannot learn the project number: the catalog search returned no "
                                 "entry. Pass the project NUMBER rather than the id.")
            self._project_number = project_number_of(names[0])
        return self._project_number

    def _ensure_category(self, glossary: str, category_id: str) -> str:
        """The category's resource name, creating it when missing.

        A category is the team tier of a glossary: the estate's definition of
        revenue sits in the glossary itself, one team's narrower definition
        sits in a category. Created with the glossary as parent, exactly as
        the 2026-09-13 probe did (`scripts/probe_glossary_tiers.py`).

        Unlike a glossary, a category is NOT a long-running operation, so
        there is no `.result()` here. Adding one raises AttributeError on the
        returned object, which is how the probe found the difference.
        """
        name = f"{glossary}/categories/{category_id}"
        if name in self._categories_seen:
            return name
        client = self._glossary()
        try:
            client.get_glossary_category(
                request=self._types.GetGlossaryCategoryRequest(name=name))
        except self._errors.NotFound:
            category = self._types.GlossaryCategory(
                parent=glossary, display_name=category_id,
                description="a team tier pushed by contextlift")
            try:
                client.create_glossary_category(
                    request=self._types.CreateGlossaryCategoryRequest(
                        parent=glossary, category_id=category_id, category=category))
            except (self._errors.AlreadyExists, self._errors.Conflict):
                pass                    # created between the get and the create
            except self._errors.PermissionDenied as denied:
                raise self._denied(f"creating glossary category {category_id}", glossary,
                                   "roles/dataplex.catalogEditor", denied) from denied
        except self._errors.PermissionDenied as denied:
            raise self._denied(f"reading glossary category {category_id}", glossary,
                               "roles/dataplex.catalogEditor", denied) from denied
        self._categories_seen.add(name)
        return name

    def upsert_term(self, glossary_id: str, term_id: str, display_name: str,
                    description: str, labels: dict,
                    category_id: str = "") -> tuple[str, str]:
        """Create or update one glossary term; (term entry name, what happened).

        The create is the earlier project's `seed_glossary` shape (the API
        wants the glossary name inside the term too, or answers 400). The
        get-then-compare and the update are public docs, not yet run live;
        "unchanged" means the display name, description and labels all match,
        so a second push of the same terms writes nothing.

        WITH A CATEGORY THE TWO PARENTS DIFFER, and getting that wrong is a
        400 with a message that does not say so. The request parent -- the
        URL -- must be the GLOSSARY even for a term going into a category;
        the parent inside the term BODY is what files it under the category.
        Confirmed live on 2026-09-13, `docs/evidence/glossary-tiers.json`:
        `stored_parents` came back {'revenue': 'glossary',
        'attach-rate': 'category'} from exactly this pair of calls.
        """
        errors = self._errors
        glossary = self._ensure_glossary(glossary_id)
        term_name = f"{glossary}/terms/{term_id}"
        body_parent = (self._ensure_category(glossary, category_id) if category_id
                       else glossary)
        wanted = self._types.GlossaryTerm(parent=body_parent, display_name=display_name,
                                          description=description, labels=labels)
        client = self._glossary()
        try:
            try:
                existing = client.get_glossary_term(request=self._types.GetGlossaryTermRequest(name=term_name))
            except errors.NotFound:
                existing = None
            if existing is None:
                try:
                    created = client.create_glossary_term(request=self._types.CreateGlossaryTermRequest(
                        parent=glossary, term_id=term_id, term=wanted))
                    entry = term_entry_name(self._number(created.name, glossary), glossary_id, term_id)
                    return entry, "created"
                except (errors.AlreadyExists, errors.Conflict):
                    existing = client.get_glossary_term(
                        request=self._types.GetGlossaryTermRequest(name=term_name))
            entry = term_entry_name(self._number(existing.name, glossary), glossary_id, term_id)
            # The parent is part of "same". Moving a term from the glossary
            # into a team category changes nothing a person reads and
            # everything about which tier it belongs to, so a comparison that
            # ignored it would report "unchanged" and leave the term in the
            # wrong tier for good.
            same = (existing.display_name == display_name and existing.description == description
                    and dict(existing.labels) == dict(labels)
                    and (existing.parent or "") == body_parent)
            if same:
                return entry, "unchanged"
            wanted.name = term_name
            # `parent` is in the mask, so a term already in the glossary can
            # be moved into a category by a second push. Leaving it out would
            # make a tier change unpushable without deleting the term first.
            client.update_glossary_term(request=self._types.UpdateGlossaryTermRequest(
                term=wanted,
                update_mask={"paths": ["display_name", "description", "labels", "parent"]}))
            return entry, "updated"
        except errors.PermissionDenied as denied:
            raise self._denied(f"writing glossary term {term_id}", glossary,
                               "roles/dataplex.catalogEditor", denied) from denied

    def read_terms(self, glossary_id: str) -> list[dict]:
        """Every term in the glossary, with the category it is filed under.

        The `category_id` is derived from the term's own `parent`: a parent
        ending `/categories/<id>` is the team tier, anything else is the
        glossary itself. That is read back rather than assumed, which is the
        whole reason this method exists -- a tier the writer believes in and
        the catalog does not is exactly the kind of silent disagreement a
        round would otherwise inherit.

        A missing glossary is an empty list, not an error. Reading a catalog
        that has never been pushed to is the ordinary first case.
        """
        glossary = f"projects/{self.project}/locations/{GLOSSARY_LOCATION}/glossaries/{glossary_id}"
        client = self._glossary()
        try:
            listed = client.list_glossary_terms(
                request=self._types.ListGlossaryTermsRequest(parent=glossary))
        except self._errors.NotFound:
            return []
        except self._errors.PermissionDenied as denied:
            raise self._denied(f"listing terms in glossary {glossary_id}", glossary,
                               "roles/dataplex.catalogViewer", denied) from denied
        terms = []
        for term in listed:
            parent = getattr(term, "parent", "") or ""
            found = re.search(r"/categories/([^/]+)$", parent)
            terms.append({
                "term_id": (term.name or "").rsplit("/", 1)[-1],
                "display_name": term.display_name or "",
                "description": term.description or "",
                "labels": dict(getattr(term, "labels", {}) or {}),
                "category_id": found.group(1) if found else "",
            })
        return sorted(terms, key=lambda t: t["term_id"])

    def delete_term(self, glossary_id: str, term_id: str) -> str:
        """Remove one term; "deleted" or "missing".

        Deleting a TERM is immediate, unlike deleting a glossary, which is a
        long-running operation the 2026-09-13 probe had to wait on. Callers
        pass only terms carrying this tool's own label.
        """
        glossary = f"projects/{self.project}/locations/{GLOSSARY_LOCATION}/glossaries/{glossary_id}"
        name = f"{glossary}/terms/{term_id}"
        try:
            self._glossary().delete_glossary_term(
                request=self._types.DeleteGlossaryTermRequest(name=name))
        except self._errors.NotFound:
            return "missing"
        except self._errors.PermissionDenied as denied:
            raise self._denied(f"deleting glossary term {term_id}", glossary,
                               "roles/dataplex.catalogEditor", denied) from denied
        return "deleted"

    def link_term(self, source_entry: str, term_entry: str, link_id: str) -> str:
        """Attach a term to an entry with a `definition` link. Public docs
        (manage-glossaries, 2026-09-05): the link is created in the SOURCE
        entry's own group, the table is SOURCE and the term's entry under
        `@dataplex` is TARGET. Not yet run live. The docs list four
        permissions, named in the 403 message so an admin can map them."""
        Reference = self._types.EntryLink.EntryReference
        link = self._types.EntryLink(
            entry_link_type=DEFINITION_LINK_TYPE,
            entry_references=[Reference(name=source_entry, type_=Reference.Type.SOURCE),
                              Reference(name=term_entry, type_=Reference.Type.TARGET)])
        try:
            self._client.create_entry_link(request=self._types.CreateEntryLinkRequest(
                parent=entry_group_of(source_entry), entry_link_id=link_id, entry_link=link))
        except (self._errors.AlreadyExists, self._errors.Conflict):
            return "existed"
        except self._errors.PermissionDenied as denied:
            raise self._denied(
                f"linking {term_entry} to {source_entry}", entry_group_of(source_entry),
                "roles/dataplex.entryOwner (dataplex.entryLinks.create, dataplex.entries.link and "
                "dataplex.entryGroups.useDefinitionEntryLink on the group; dataplex.glossaryTerms.use "
                "on the glossary)", denied) from denied
        return "created"

    def _ensure_entry_type(self, parent: str) -> str:
        """Verified on the earlier project: an existing type answers plain
        Conflict, not AlreadyExists; both are caught."""
        name = f"{parent}/entryTypes/{QUERY_ENTRY_TYPE_ID}"
        try:
            self._client.create_entry_type(request=self._types.CreateEntryTypeRequest(
                parent=parent, entry_type_id=QUERY_ENTRY_TYPE_ID,
                entry_type=self._types.EntryType(
                    description="A past answered question with its reviewed SQL"))).result()
        except (self._errors.AlreadyExists, self._errors.Conflict):
            pass
        return name

    def _ensure_entry_group(self, parent: str, group_id: str) -> str:
        name = f"{parent}/entryGroups/{group_id}"
        try:
            self._client.create_entry_group(request=self._types.CreateEntryGroupRequest(
                parent=parent, entry_group_id=group_id,
                entry_group=self._types.EntryGroup(
                    description="worked examples pushed by contextlift"))).result()
        except (self._errors.AlreadyExists, self._errors.Conflict):
            pass
        return name

    def upsert_query_entry(self, group_id: str, entry_id: str, question: str, sql: str,
                           labels: dict) -> tuple[str, str]:
        """One custom entry per worked example; (entry name, what happened).

        Verified on the earlier project: entry type `verified-query`, a
        custom group, `create_entry` then `update_entry` on Conflict, the
        question in the display name and the SQL in the description, which
        semantic search then finds from a paraphrase. The SQL is refused
        rather than cut when it does not fit (`compact_sql`)."""
        parent = f"projects/{self.project}/locations/{self.location}"
        try:
            entry_type = self._ensure_entry_type(parent)
            group = self._ensure_entry_group(parent, group_id)
            name = f"{group}/entries/{entry_id}"
            entry = self._types.Entry(
                name=name, entry_type=entry_type,
                entry_source=self._types.EntrySource(
                    display_name=question[:ENTRY_DISPLAY_NAME_LIMIT],
                    description=compact_sql(sql), labels=labels))
            try:
                self._client.create_entry(request=self._types.CreateEntryRequest(
                    parent=group, entry_id=entry_id, entry=entry))
                return name, "created"
            except (self._errors.AlreadyExists, self._errors.Conflict):
                self._client.update_entry(request=self._types.UpdateEntryRequest(
                    entry=entry, update_mask={"paths": ["entry_source"]}))
                return name, "updated"
        except self._errors.PermissionDenied as denied:
            raise self._denied(
                f"writing query entry {entry_id}", f"{parent}/entryGroups/{group_id}",
                "roles/dataplex.entryOwner (roles/dataplex.catalogEditor to create the entry "
                "type and the group the first time)", denied) from denied


def transport_for(project: str, location: str, *, credentials=None) -> CatalogTransport:
    """The live transport. `project` is the id or the number; `location` is
    where the entry group lives (`us` for a US BigQuery dataset), which is
    also where `lookupContext` is attributed. The import is inside the class,
    so the fast path never needs the extra."""
    return DataplexTransport(project, location, credentials=credentials)
