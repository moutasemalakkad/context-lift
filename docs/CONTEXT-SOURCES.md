# Putting context into a catalog, and reading it back out

[Documentation](README.md) / Context sources and catalogs

## On this page

- [Where does each piece of knowledge go?](#where-does-each-piece-of-knowledge-go)
- [Glossary terms: the org tier and the team tier](#glossary-terms-the-org-tier-and-the-team-tier)
- [Does the round trip change what the agent sees? No.](#does-the-round-trip-change-what-the-agent-sees-no)
- [Undoing a push](#undoing-a-push)
- [Deployment field notes](#deployment-field-notes)

`contextlift semantic` reads a LookML model. A real deployment usually has
another source too: a data catalog holding descriptions somebody already
wrote. This page covers both directions — where a piece of business knowledge
belongs in Google Cloud's Knowledge Catalog (the Dataplex catalog), with dated catalog experiments. The deployment log is in [Field notes](FIELD-NOTES.md#catalog-deployment).

Placeholders throughout; nothing here names a customer, a project or a table.

For new commands, use the workspace from [Evaluate your data](EVALUATE.md):

```bash
export CONTEXTLIFT_WORKSPACE=/path/to/evaluation
```

Review drafts before installation. Before a catalog write, run its dry-run
plan and inspect what it will publish. Read writes back and report any
truncation or failed verification; see the deployment field notes below.


## Where does each piece of knowledge go?

This is the first question a team asks and the one nobody answers. Every row
below was measured on a live catalog on 2026-09-13, and the command that
produced the evidence is named.

| what you have | where it goes | how it gets there | earned by |
|---|---|---|---|
| a rule about **one table** | that table's entry, in its `overview` aspect | `spec-push`, from `rules/30-<table>.md` | [practice 6](PRACTICES.md) |
| a rule about the **whole estate or one domain** | it belongs to no table, so it stays in the spec repo and is delivered in the request | `rules/00-*.md`, `rules/10-*.md` | [practices 15 and 16](PRACTICES.md) |
| a **worked example** (a question and its SQL) | the entry of the table the SQL reads, in the queries aspect | `spec-push`, from `examples/*.jsonl` | [practice 13](PRACTICES.md) |
| a **term the business uses**, estate-wide | a glossary term whose parent is the glossary | `spec-push --glossary`, from spec terms | — |
| a **term one team uses differently** | a glossary term whose parent is a **category** inside that glossary | `spec-push --glossary`, from spec terms | — |
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

The initial push recorded these refusals. Glossary terms later gained a
round-trip implementation, described in [The round trip](#the-round-trip-run-against-the-live-catalog).
The excerpt is retained as evidence of the earlier behavior.

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

**Glossary terms now have a round-trip implementation.** The earlier refusal
above prevented silent loss before that read path existed. See the recorded
round trip below for what is supported and what is still refused.

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


## Deployment field notes

The longer deployment record moved to [Field notes](FIELD-NOTES.md#catalog-deployment).

<details>
<summary>Links to the former sections</summary>

<a id="field-notes-from-a-live-enterprise-deployment"></a>

[Field notes from a live enterprise deployment](FIELD-NOTES.md#field-notes-from-a-live-enterprise-deployment)

<a id="one-rule-that-outlives-the-api"></a>

[One rule that outlives the API](FIELD-NOTES.md#one-rule-that-outlives-the-api)

<a id="how-much-to-trust-each-item-below"></a>

[How much to trust each item below](FIELD-NOTES.md#how-much-to-trust-each-item-below)

<a id="knowledge-catalog-measured"></a>

[Knowledge Catalog, measured](FIELD-NOTES.md#knowledge-catalog-measured)

<a id="probed-on-a-second-project"></a>

[Probed on a second project](FIELD-NOTES.md#probed-on-a-second-project)

<a id="what-catalog-pull-writes"></a>

[What catalog-pull writes](FIELD-NOTES.md#what-catalog-pull-writes)

<a id="writing-into-the-catalog"></a>

[Writing into the catalog](FIELD-NOTES.md#writing-into-the-catalog)

<a id="a-read-can-be-truncated-too-and-that-one-is-worse"></a>

[A read can be truncated too, and that one is worse](FIELD-NOTES.md#a-read-can-be-truncated-too-and-that-one-is-worse)

<a id="retrieval-and-what-had-to-land-with-it"></a>

[Retrieval, and what had to land with it](FIELD-NOTES.md#retrieval-and-what-had-to-land-with-it)

<a id="where-the-documents-come-from"></a>

[Where the documents come from](FIELD-NOTES.md#where-the-documents-come-from)

<a id="measured-against-hand-made-pages-and-what-it-did-not-move"></a>

[Measured against hand-made pages, and what it did not move](FIELD-NOTES.md#measured-against-hand-made-pages-and-what-it-did-not-move)

<a id="measuring-a-retriever-before-trusting-it"></a>

[Measuring a retriever before trusting it](FIELD-NOTES.md#measuring-a-retriever-before-trusting-it)

</details>

[Documentation index](README.md) · Next: [Evaluate a catalog arm](EVALUATE.md)
