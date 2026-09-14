# Putting context into a catalog, and reading it back out

`contextlift semantic` reads a LookML model. A real deployment usually has
another source too: a data catalog holding descriptions somebody already
wrote. This page covers both directions — where a piece of business knowledge
belongs in Google Cloud's Knowledge Catalog (the Dataplex catalog), then the
field notes from making writes and reads actually work on a live deployment.

Placeholders throughout; nothing here names a customer, a project or a table.

## Where does each piece of knowledge go?

This is the first question a team asks and the one nobody answers. Every row
below was measured on a live catalog on 2026-09-13, and the command that
produced the evidence is named.

| what you have | where it goes | how it gets there | earned by |
|---|---|---|---|
| a rule about **one table** | that table's entry, in its `overview` aspect | `spec-push`, from `rules/30-<table>.md` | [practice 6](PRACTICES.md) |
| a rule about the **whole estate or one domain** | it belongs to no table, so it stays in the spec repo and is delivered in the request | `rules/00-*.md`, `rules/10-*.md` | [practices 15 and 16](PRACTICES.md) |
| a **worked example** (a question and its SQL) | the entry of the table the SQL reads, in the queries aspect | `spec-push`, from `examples/*.jsonl` | [practice 13](PRACTICES.md) |
| a **term the business uses**, estate-wide | a glossary term whose parent is the glossary | proven live; not yet written by this tool | — |
| a **term one team uses differently** | a glossary term whose parent is a **category** inside that glossary | proven live; not yet written by this tool | — |
| a **column description** | BigQuery, not the catalog | `bq update`, or your existing pipeline | the push's own refusal, below |
| **which round wrote this page** | a `context-provenance` aspect on the entry | `spec-push --provenance` | — |

The last column is the point of the table. A row with a practice number behind
it was measured; a row with a dash is a mechanism nobody has priced yet, and
should be read as plumbing rather than advice. `tests/test_practices.py`
refuses a practice that states no measurement, and
`tests/test_knowledge_architecture.py` refuses a row here that cites a
practice which does not exist.

### The rule that decides it: does this belong to one table?

A page lands on an entry, and an entry is one table. So a rule that is true of
one table has a home, and a rule that is true of the estate does not.

Measured: pushing the thelook spec put a page on **3 of 7** entries. The other
four got nothing, not because the push failed, but because the spec's rules are
estate-tier and domain-tier — they are about revenue and about output
conventions, not about `order_items`.

```
contextlift spec-push --spec examples/thelook/spec --project <project> \
    --eval-room <project>.<dataset> --dry-run
```

```
served: all 7 entry/entries whole; 3 of 7 carry an overview at all
```

That is the shape to expect, and it is why the estate tier is a file in the
spec repo rather than something you can file in a catalog. Splitting an
estate-wide rule across every table entry would write the same sentence 97
times and give you 97 places to edit it.

### Two things the catalog will not take, and the tool refuses rather than guessing

Run the push and read the refusals. They are not warnings; they are the
architecture.

```
refused 7 authored column description(s): BigQuery owns column descriptions and
  the catalog has no home for them. Written onto the evaluation view they would
  reach the control arm as well, which is the one thing the room exists to prevent.
  File mode delivers them in the request instead
refused 5 glossary term(s): the catalog reader does not read terms back, so
  writing them would lose them silently. Keep them in the spec repo until the
  entry-link read exists
```

**Column descriptions belong to BigQuery.** There is no catalog field for
them. There is a second reason beyond plumbing: a description written onto the
table reaches *both* arms of a measurement, so it cannot be the thing you are
measuring.

**Glossary terms can be written but not yet read back by this tool.** The
catalog itself is fine with them — see below. The gap is on this side, and
writing a term the reader cannot retrieve would lose it without telling you.

## Glossary terms: the org tier and the team tier

A glossary holds terms. A **category** inside a glossary also holds terms. That
is the mechanism for "revenue means this across the company, except in hardware
sales where it means that".

Confirmed against a live catalog, twice, by a committed script:

```
make probe-glossary-tiers PROJECT=<project>
```

```
stored parents: {'revenue': 'glossary', 'attach-rate': 'category'}
one glossary-level list returned 2 term(s): both tiers in one call
verdict: TERM UNDER A CATEGORY WORKS
```

Six facts for whoever implements this, none of them obvious from the
reference:

- **The URL parent must be the glossary; the body parent selects the
  category.** `POST v1/{glossary}/terms?termId=x` with
  `{"parent": ".../glossaries/{g}/categories/{c}"}`. Passing the category in
  the URL does not work.
- **One glossary-level `terms.list` returns both tiers**, each term carrying
  its own `parent`. Reading current state is one call, not one per category.
- **Creating a glossary is a long-running operation.** Creating a category or
  a term is not.
- **Deleting a glossary is long-running too**, and answers `200` with an
  operation rather than a result. A cleanup step that checks immediately
  afterwards reports the glossary as still present, because it is.
- **A glossary holding anything cannot be deleted at all.** The API answers
  `400 Glossary with children can't be deleted`. Every term goes first, then
  every category, then the glossary. Found on 2026-09-13 by a cleanup step
  that removed one term of two and then asked for the glossary
  (`scripts/probe_glossary_roundtrip.py`).
- **Moving a term between tiers needs `parent` in the update mask.** A term
  already in the glossary is filed under a category by updating its parent.
  A mask listing only the display name, description and labels answers
  success and leaves the term where it was.

Categories nest: a category's parent may be a glossary or another category.

### The round trip, run against the live catalog

`docs/evidence/glossary-roundtrip.json` records a term written by this code
and read back by it, rather than by hand-built requests:

```
upsert_term: term=revenue, category=(glossary), state=created
upsert_term: term=attach-rate, category=team-hardware-sales, state=created
read_terms: count=2
compare: identical=True
tiers_in_one_list: tiers=['', 'team-hardware-sales'], both=True
delete_term: term=attach-rate, state=deleted, gone=True
verdict: ROUND TRIP IS LOSSLESS
```

So a glossary term is now part of a measured arm. `spec-push` writes the
spec's terms, `--glossary` says where, and a term whose tier is not `org`
goes into a category of that name. Until this ran, the push refused to write
any term, because a term that can be written and not read is a term whose arm
hash cannot be checked.

**Two things it does not do.** The `synonyms` and `related` lists are not
written: a catalog holds those as links between term entries, there is no
link read to pair with the term read, so writing them would be the silent
loss the refusal existed to prevent. They are reported instead. And a term
left in the glossary by an earlier push — after a rename, say — still reaches
the catalog arm, so the push names it and `--prune-terms` removes it. A term
this tool did not write is named too and never deleted.

## Does the round trip change what the agent sees? No.

The question that matters is not whether a write succeeded. It is whether the
context the agent receives is the same after a catalog round trip — because if
it is, the accuracy cannot have changed.

`spec-push` answers it by hashing both sides. Measured on 2026-09-13 against a
live catalog:

```
writable part of the spec d07c1f37980d, read back d07c1f37980d
  the catalog reads back as what was written
  it is NOT the whole spec: the arm from files is 7d7a83d66812. Everything
  refused above is missing from the catalog arm, so a round in catalog mode
  measures less context than one in file mode

provenance: 7 record(s) written and read back with matching hashes
```

Read that as two separate results.

**The round trip is lossless for what the catalog accepts.** Identical hash
both ways, so a round in catalog mode and a round in file mode over the same
writable content measure exactly the same thing.

**A catalog arm is a smaller arm.** The refused channels are genuinely absent
from it. So a catalog-mode number is not comparable to a file-mode number
unless you know which channels each one carried, and the tool prints both
hashes so you can tell.

## Undoing a push

The catalog keeps no version history. `spec-push --previous-out DIR` saves the
bytes each entry held before the write, and that directory is the only thing
that makes an undo possible:

```
contextlift spec-push --spec DIR --project P --previous-out /tmp/previous
contextlift catalog-rollback --previous /tmp/previous --project P
```

A rollback over entries that already hold the saved bytes says so and does
nothing, per entry, so running it twice is safe.

## Field notes from a live enterprise deployment

Everything above is about where knowledge goes. Everything below is about
making the calls work. Each item cost hours and appears in no documentation.

## One rule that outlives the API

**Verify a write by reading it back, and compare lengths.** A catalog that
accepts your bytes and stores fewer of them looks exactly like one that worked.
Every silent failure below is a variation of this, and it applies to any context
source, not only this one.

## How much to trust each item below

Two of these have been probed a second time against the live store on purpose.
The rest were observed once, in the course of doing something else, which is how
a fact comes to be remembered more confidently than it was earned. Treat them as
leads that cost someone a day, not as established behaviour, and check the one
you are about to depend on.

Probed twice: the indexing lag (scoped down as a result, see below) and
`resolve`-then-fetch reliability. The other seven are single observations.

## Knowledge Catalog, measured

- **`lookupContext` returns YAML whose `overview` field holds the entry's
  markdown body.** Parse it with a YAML loader, not string slicing: long column
  names get folded across lines, and naive parsing gives false negatives.

- **`lookupContext` does not return custom aspects.** Only the built-in
  `overview`. An adapter that writes its context as a custom aspect type
  publishes successfully and reads back nothing — a silent no-op, not an error.
  `entries.get?view=ALL` does return custom aspects. Confirmed live on both the
  global and the regional endpoints.

- **Entry names carry the project *number*, not the project id.** Building the
  name from the id returns not-found.

- **There is indexing lag after a push.** Immediately after registering entries,
  `lookupContext` returned 0 characters; the same call a few minutes later
  returned the whole document. An adapter that reads straight after a write
  looks broken. Retry, and never cache an empty or stub-length body.

  Scoped by a later probe: 60 sequential reads of four documents from a warm,
  unwritten catalog came back **byte-identical**, and `resolve` never returned a
  name that then failed to fetch, 0 of 60. So the degraded bodies are not
  general flakiness. They appear in two conditions and the retry rule earns its
  place in those: **immediately after a write**, and **under a burst of
  concurrent reads** — the original stubs were seen while enriching about 40
  candidates back to back. A test double built from steady-state behaviour will
  not exercise either.

- **Retracted: "do not set a quota project on the credentials for catalog
  calls."** The original observation was real: a search returned zero results
  and no error, and the quota project was the diagnosis. Re-tested by its
  author on the same estate and by a second probe on another project, the
  count is identical with and without the quota project (12 and 12; 419 and
  329 on the other project, both non-zero). Either the diagnosis was wrong or
  the behaviour changed server-side; nobody can tell from here. Kept as the
  cleanest example of the shape a wrong belief takes: born from a genuine
  symptom, generalised into a rule, never re-tested.

- **The overview aspect has a size ceiling and exceeding it truncates
  silently.** Column descriptions for wide tables were dropped at registration
  with no warning.

- **Search scoping needs an explicit predicate, and it behaves differently by
  search mode.** Scoping a search to one entry group by name prefix worked in
  semantic mode and returned zero results in keyword mode. Whether that is
  intended was asked of the product team and never answered. An adapter should
  verify its scoping returns a non-zero count on a known-good query rather than
  assume the predicate applied.

- **Entry ids** in the deployment tested were `<dataset>__<table>`, which makes
  mapping a routed `dataset.table` to an entry trivial. Do not assume it; make
  it configurable.

- **An entry group lists more than tables, and entry type does not tell them
  apart.** One group held 129 entries: 127 tables, the group's own self-entry,
  and an index entry. The self-entry has type `entrygroup` (one word, no
  hyphen; a filter written with a hyphen matched nothing and the count stayed
  129). The index entry has type `generic`, the same string every real table
  carries, so a type filter keeps it. What separates them is `parentEntry`:
  set on all 127 tables, empty on both others. An adapter that builds a table
  list from a group should keep entries whose `parentEntry` is set and not
  consult the type. A count that is two too high is the symptom.

## Probed on a second project

The live transport (`DataplexTransport` in `catalog.py`, behind `contextlift
catalog-pull`) was written against a second project: BigQuery's own entries in
the regional `@bigquery` entry group, 44 tables, no custom entry group. Reads are
metadata calls and are not billed. What that probe measured, each once unless
stated:

- **The quota-project trap did not reproduce**, here or, on re-test, on the
  estate it was written about (see the retraction above). `check_scope` still
  exists because the failure it catches, a search that answers nothing and no
  error, has other causes on this list: the entry-group predicate in keyword
  mode is one, re-confirmed on the original estate (0 results in keyword mode,
  6 for the same intent in semantic mode).
- **`name:{table} type=table` matched exactly one entry**, three times out of
  three; `name:{table}` alone matched 13 things including glossary terms. On
  the search path the store takes only a result whose entry name keys to
  the requested `dataset.table`, because the same table name lives in
  several datasets of one project, and a search can return an entry for a
  different table first. Search is no longer how a page is addressed at
  all; the name is built from the key (see "Writing into the catalog").
- **`lookupContext` on one entry: 15606 characters, byte-identical over 43
  reads** (3 sequential, then 40 concurrent). Steady state is stable, as the
  first probe found; the burst condition did not degrade it here.
- **`context_budget` is best effort and coarse.** Budgets of 2000 and 500 both
  returned 6287 characters of the 15606, whole sections dropped. No budget
  returned everything. The API rejects a budget that is not a positive integer.
- **A missing entry returns 0 characters and no error.** The store resolves by
  search first and treats an empty body as degraded; `catalog-pull` writes no
  file for it.
- **Two entries in one lookup came back as 23 entry blocks, 43112 characters.**
  The lookup expands to related entries. The transport asks for one entry per
  call so a document for one table does not carry another.

## What catalog-pull writes

`catalog-pull --out DIR` writes the served bundle: the text `lookupContext`
returns for the entry, which is YAML (`resources:` first) with the overview
inside it as a quoted scalar, folded on BigQuery's own entries and with
literal `\n` sequences on a custom group's. That is what an agent gets, so it
is the arm, and it is kept byte-exact. It is not a file a person can edit,
and `catalog-push --docs` cannot send it back.

`catalog-pull --pages DIR` writes, beside it, the overview aspect as the
catalog stores it, with the push markers removed: `<dataset>.<table>.md`,
plain text, no file for a table without an overview. That is the round trip:
pull `--pages`, edit, `review`, `catalog-push --docs DIR`. A page pushed
unchanged is reported as unchanged, because the stamp is a hash of the text.

Either way the pull reads both: the served bundle and the stored overview, one
`entries.get` per table. It prints the two lengths and the budget when the
reader got less than the entry holds, and names a `--budget` that would serve
the page. It does not raise the budget itself; the budget the caller passed is
what that caller's readers get. Why the comparison is worth an extra read is
in "A read can be truncated too" below.

## Writing into the catalog

`contextlift catalog-push` is the fix half's last stage. What it writes, where,
and how each is verified; graded by what the fact rests on (public docs,
the earlier project's live code, or this repo's live test).

- **Pages become the table's `overview` aspect** (public docs; the earlier
  project wrote it live). `update_entry` with the aspect under the key
  `dataplex-types.global.overview` and `update_mask paths=["aspects"]`;
  the API returns the same aspect keyed by the project number, so a reader
  matches the key by its `.global.overview` suffix. Role:
  `roles/dataplex.entryOwner` on the entry group. Each page is wrapped in a
  begin and end marker carrying a content hash, so a second push sees an
  unchanged page without reading the body, and a page the catalog holds
  that contextlift did not write is replaced with its previous bytes kept.
- **A page's entry name is built from its key, not searched.** The entry
  id inside the group is a template of `{dataset}`, `{table}` and
  `{project}` (`--entry-id`). BigQuery's own group `@bigquery`, the
  default, had one shape on every estate measured,
  `bigquery.googleapis.com/projects/{project}/datasets/{dataset}/tables/{table}`,
  and that is its default template; a custom group's ids are whatever
  whoever registered the entries chose (`{dataset}__{table}` on the
  estates measured), so it takes `--entry-group G --entry-id
  '{dataset}__{table}'`. With a template nothing is searched; 99 of 99
  custom entries were reachable by the built name. Why: an earlier
  substring resolve put 12 of 99 pages on a sibling table's entry, and
  the same table resolved right in one run and wrong minutes later with
  only the search ranking changed; search given the complete
  fully-qualified name of a table returned four hits with a different
  table first; and the API has no other exact address (LookupEntry takes
  no fully-qualified name). Built names are read until one exists: a name
  that is missing is counted and the run goes on with it as that table's
  own line, and only a run where NO built name existed exits 2, naming the
  first name it tried, the `--project` and `--location` that build it, and
  `catalog-list` for the group's ids. Doubting the template on the first
  missing name refused a whole pull because the first of the tables listed
  was not catalogued, and blamed an `--entry-id` the caller never passed.
  Without a template (a custom group with no `--entry-id`) the search
  path remains, and there a hit counts only when its key equals the
  page's `dataset.table`: on one estate the search for a `__dbt_tmp`
  table returned a stale entry of another dataset and table first, and an
  earlier version took it. On the auto group `name:{dataset}.{table}`
  returns nothing, so `--entry-pattern` keeps `{table}`. The same name in
  several entry groups refuses unless `--entry-group` says which.
  `--dry-run` lists where each page would go (`key -> entry  would write
  (N chars)`), writes the plan under `--manifest-out` when given, writes
  nothing to the catalog and is not recorded in the workspace; the
  command says so on its last line.
- **Pages go in three phases, and a killed push is resumed by running it
  again.** Phase 0 resolves every page (searches are free) and refuses
  with exit 2 when two pages resolve to one entry, listed as `key1, key2
  -> entry`: an earlier resolve put 12 of 99 pages onto a sibling table's
  entry and read-back verified every one, because read-back proves the
  bytes survived, not that they landed on the right table. Phase 1 writes
  every page. Phase 2 reads all written pages back together, one wait of
  2, 4, 6 seconds between passes and not per page: on one deployment a
  push that read each page to exhaustion before writing the next took
  about 25 minutes for 99 pages, printing nothing, because 22 pages were
  read four times each (a line fold inside the end marker, see below). The
  re-run over the same 99 pages with this code took 90 seconds, with 95
  pages unchanged and 4 written.
  Progress now goes to stderr as it happens, and SIGINT or SIGTERM writes
  the manifest with the counts so far and `"status": "interrupted"`
  before the process ends. Running the same command again continues:
  stamped pages come back unchanged, the rest are written; `--verify-only`
  reads back the pages that were written and not verified. One push per
  workspace at a time: a lock file (`catalog-push.lock` in the workspace
  root, else beside the manifest) refuses a second push while the first
  one's process is alive, and says so when it replaces a stale one.
- **Verification is a read through `lookupContext`**, the call every reader
  uses, not through `get_entry`: a body that is stored whole and served cut
  is the failure the field measured. The served text is unfolded first: a
  double-quoted YAML scalar is folded at a fixed width with `\` + newline
  + indentation (no character) and `\ ` (one space), and on one estate
  the fold landed inside the end marker of 22 pages out of 99, each then
  called truncated with a served text longer than the page; 18 verified
  once unfolded. The classification is stub (under the store's
  threshold), missing (no begin marker), truncated (bytes gone: no end
  marker in a served text shorter than the page, or the body between the
  markers under 95% of what was written), unverified (no end marker in a
  served text no shorter than the page: nothing known lost, the marker not
  matched), verified. A page not verified then reads `get_entry` once to
  name the cause: "stored N of M", "reader sees N of M; the aspect holds
  all of it", or "end marker not found in the served text (served N
  characters for M written)". A verified page records
  `served_minus_written`, the bundle's overhead, with no verdict on it.
- **Rule lines become glossary terms** (public docs), linked to the table
  they name by an entry link of type `definition` created in the table's
  entry group, with the table as source and the term's entry as target;
  `lookupContext` lists linked terms under related business terms. The
  glossary lives at `locations/global`; a term needs its parent set on
  create. Whether a linked term shows in the bundle is checked by the live
  test, not assumed.
- **Worked examples become the table's `queries` aspect** when the API
  accepts a user write to it (the product's own documentation scans write
  that aspect, and `lookupContext` returns it), else verified-query entries
  in a custom entry group found by semantic search. The push records which
  home the estate supports.
- **Column descriptions are not written here.** They belong to the BigQuery
  table schema; the catalog ingests them. Changing the warehouse is a
  different permission and a different tool.
- **Custom aspect types are not used for context**: `lookupContext` does not
  return them (see above), so anything an agent should see lives in a
  system aspect, a glossary term, or a searchable entry.

Measured on the first live push (the owner's project, BigQuery's own
entries, 2026-09-05), each once:

- An overview of 2,294 characters was stored whole and appeared in
  `lookupContext` on the first read after the write, inside a 9,270
  character bundle; a second table's page the same. The `overview` aspect
  template caps `content` at 100 KiB, which is the ceiling the earlier
  silent truncation hit.
- The `queries` aspect accepts a user write and `lookupContext` returns the
  rows: a pushed example showed in the served page as
  `description: How many seasons are there?`. So examples live on the
  table itself; the verified-query entry group is the fallback only.
- A glossary term with a `definition` link to the table was created and
  read back from the glossary, but did not appear in the table's
  `lookupContext` bundle within six reads. The push therefore verifies a
  term where it lives and records `in_lookup` as an observation, and a
  reader that needs definitions must read the glossary or the entry links.
- `list_tables` on the `@bigquery` group: 53 entries, 44 kept, 9 without a
  parent entry (the dataset entries and the group's own).

## A read can be truncated too, and that one is worse

A store may cap what it returns as well as what it stores. Knowledge Catalog's
`lookupContext` takes a `context_budget` and the default truncates: a deployment
measured here passes 60000, because its SQL conventions sit at the end of the
overview document and a smaller budget silently returns the prose and drops the
rules.

On one estate (project PROJECT, entry group GROUP, measured 2026-09-05) the
read is not a cut at all: it serves a page whole or not at all at the budget it
is given. Same entry, same request, only `context_budget` changed. The entry
stored 71,690 characters (`entries.get`, `view=ALL`); budgets of 20,000 and
60,000 both returned 572 characters, and 80,000, 100,000 and 200,000 all
returned 78,854. A second entry storing 92,397 characters returned 532 at
60,000 and 101,385 at 150,000. All ten calls were HTTP 200 with no warning
field, no truncation flag and no other signal, and the 572-character body is a
valid page: the opening of the overview, no schema section, no column names.
The served text runs longer than the stored overview because the bundle carries
schema and profile around the page, so a served body under the stored size is a
page the reader did not get. On that estate 9 of 127 entries sat above the
budget the pipeline asked for, and they were the four largest finance and
shipment views, so the pages with the most in them were the ones the agent
never saw. This is one estate on one day, not a rule of the product: the second
project above returned 6,287 characters of a 15,606-character entry at budgets
of both 2,000 and 500, a partial serve rather than a stub.

That is the more dangerous half of the truncation problem. A write that silently
drops bytes is at least a store you can inspect. A read that silently drops the
end of a document produces a context arm that looks populated and is missing the
payload the experiment was measuring — and the run reports a null result nobody
questions, because everything about it looks right.

The rule for any adapter: never trust a default limit on a read, and check that
what came back is as long as what you believe is there.

## Retrieval, and what had to land with it

A catalog of any size cannot go in a prompt. One deployment measured here held
99 tables and roughly 1.8 million characters of description. Real systems
retrieve: a router picks a table, and only that table's documentation reaches
the model.

`generate --context-per-question FILE.json` takes `{question_id: context_text}`,
which is what any retriever emits. Two pieces had to land in the same change,
because without them the feature defeats its own measurement:

- **Arm identity.** An arm is what the model saw, so it is named after that: a
  hash over the sorted (question, retrieved text) pairs. Two different
  retrievals are two different arms. Without it they collide under one id and
  `ablate` compares them as if nothing changed, reporting a null result that is
  an artifact of the naming. An arm that retrieved nothing keeps the id it
  already had, so no existing workspace's history splits.
- **Leak checking per question.** `leak --context-per-question FILE.json` checks
  each question against its own retrieved text. It also checks every question
  the retriever did *not* serve — a reserved one — against the union of
  everything retrieved, because nobody hands a reserved question context but its
  answer can sit inside a document fetched for some other question. That is the
  leak that destroys the number you report, and a per-question check alone would
  miss it.

Read a catalog into a file first, and retrieve live only when you have to. A
file is reproducible: someone without your credentials can re-run the arm and
get your number. A live arm cannot be audited by anyone who was not there. The
two also answer different questions — a file answers "did this context help",
live answers "did my retrieval pipeline help", where the retriever's own
failures are part of what is being measured. The second is the rarer question.

## Where the documents come from

The routing gain above needed one thing before any rule was written: each
routed table had a page at all. `contextlift draft-context --db DB --out DIR`
writes that page mechanically, one `<dataset>.<table>.md` per table from the
schema (columns, types, the column and table descriptions the warehouse holds),
into the directory shape `DirectoryContextSource` reads. Every file says in its
second line that it was drafted from the schema and not reviewed, and ends with
an empty Rules section for a person. No model call and no rows are read: on
BigQuery it is three INFORMATION_SCHEMA queries per dataset, so it can be
re-run on every schema change.

Two cautions print with it. The pages use the schema's own words, so a
question set written from the SQL will match them by vocabulary rather than by
meaning; measure routing on questions people asked. And a page with no
description is a column list, which the summary counts (`84 columns, 31 with a
description; 5 tables have none`) so nobody mistakes coverage for content.

### Measured against hand-made pages, and what it did not move

On the deployment above, one round swapped exactly 14 of 113 documents, the
hand-made pages for `draft-context`'s (each of the 14 gained a real table
description where the hand-made page had repeated the table's name), with the
same 123 questions and the same router. Scored with `score-routing` into one
workspace and compared with `ablate`:

```
hand-made pages     26 of 123   21.1%
draft-context       23 of 123   18.7%
  context fixed 0 / broke 3   McNemar p = 0.2500   not significant
```

The drafted pages matched the hand-made ones inside the noise and did not beat
them, against the prediction made before the run. The churn line under the
same verdict read `answers changed at 39 of 123 pairs; 3 changed the verdict,
36 moved without changing it` (three of the 39 are pseudonymisation losses, so
36 to 39): a third of the answers moved while the score moved by three, for
the second time on this question set. The reason is upstream of any page. Of the 88 questions whose gold is one of the 14 tables, the router
put 77 in a different data product and 0 on a sibling table in the same
dataset (74 and 1 with the hand-made pages). The right dataset was never in
contention, so no description on the right table could matter.

The rule that comes out of it: on a question set whose golds sit outside the
router's reachable set, document quality is not the binding constraint and
cannot be measured through it. `routing-gate` says so, free, before anything is
spent; on this key it read `100 questions on 19 golds not in the catalog`.

## Measuring a retriever before trusting it

`contextlift score-routing` grades table routing with no model calls and no
warehouse scans, so the retriever can be measured on its own before anything
expensive runs. From the deployment above, 20 questions:

```
table routing, 20 questions, 3 repetitions, means:

  without catalog context : 0.650    (12, 13, 14 of 20)
  with catalog context    : 0.850    (18, 17, 16 of 20)
  snapped 0 of 20 predictions to the catalog
```

Three repetitions, not one. A single run of this comparison read 0.600 against
0.900 — the most favourable repetition on both sides, and a 5-point wider gap
than the truth. The same rule that makes `generate --reps 3` the default applies
to routing: one run is a sample.

That is worth running first for a reason beyond cost. A retriever's accuracy is
the ceiling on everything downstream, and comparing retrieval against a blob is
only meaningful when the blob cannot already hold the answer. On a gold set
touching four tables, a 16 KB blob contains the right table every time — recall
1.0 by construction — so retrieval starts behind and cannot win. Measuring that
comparison would produce a loss that means nothing about the feature. The
question set has to reach tables a blob cannot hold, or the experiment is
decided before it runs.
