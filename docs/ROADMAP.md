# Roadmap

[Documentation](README.md) / Contribute / Roadmap

Two lists. The second one is the useful one: a contributor should not spend a
weekend on something that will be turned down, and the reasons are written out
so the refusal is arguable rather than arbitrary.

## What this tool is for

One question: **does written business context make a text-to-SQL agent more
accurate?** Every decision below is downstream of keeping that question sharp.

## Wanted

**More warehouses.** SQLite, DuckDB and BigQuery ship today. PostgreSQL is the
obvious next one and is genuinely wanted — the reason it has not happened is
that testing it needs a running server, which is a real cost, not a lack of
interest. Snowflake, Databricks, Redshift and Fabric are all reasonable.
`docs/AUTHORING.md` has the shape; the interface is one method.

**More agents.** The Conversational Analytics API and a CSV replay runner ship
today. Any agent that turns a question plus a context into SQL fits.

**Better refusals.** The most valuable thing here is the set of things the tool
declines to do: refuse a round the gold set cannot resolve, refuse to pair arms
that scored different questions, refuse to write a term it cannot read back. A
new refusal that catches a real mistake is worth more than a new feature.

**Numbers with sources.** If you find a figure this project prints that no
committed file supports, that is a bug report we want. There is an issue
template for it.

## Deliberately not planned

**Agent-framework adapters: LangChain, LlamaIndex, MLflow, Weights & Biases,
agent-observability integrations.** Refused outright, and written here so they
are not proposed a third time. Each one turns this into a general evaluation
platform, and the sharp thing about this tool is that it answers one question
well. This is a rejection of breadth as an adoption strategy: it trades the
only distinctive thing for a longer feature list.

**A `knowledge.yaml` schema.** Proposed and closed. The convention already IS
the schema — rules files are concatenated in filename order and the more
specific rule wins, so sort order is precedence. A declared file would need
converting both ways and one copy would go stale. What was actually missing
was the *check*, and that shipped: `lint` reports an untiered rules file, and
`3o-orders.md` — second character the letter o — is the case it catches.

**An `architect` verb.** Proposed and closed as already built. `spec-push` is
apply; `spec-push --dry-run` is both plan and drift, because it reads every
entry, compares it against the spec and writes nothing. What was missing was
only that somebody searching for those words found nothing, so they are in the
help text now. A test fails if an `architect` verb is ever added.

**An `ask` verb** — one question through both arms in the terminal. Proposed,
and the pull request was closed. It would be a ninth core verb with no
measured need. If you want it, say so in an issue; demand is the missing
evidence, not effort.

**A general benchmark harness.** This is not a replacement for Spider, BIRD or
test-suite-sql-eval, and it reads BIRD rather than competing with it. It
measures one intervention on your own data.

## Not planned, but not refused either

These need something that does not exist yet rather than a decision:

- **A production feedback loop** — joining a real thumbs-down back to a gold
  question. Needs a production serving surface to join against.
- **A second corpus for the research claim.** Every measurement in
  `docs/CASE_STUDY.md` is one model on a handful of databases, and one of them
  does most of the work. A second corpus is what would make any of it a
  finding rather than an observation.
- **A bigger gold set.** The thelook set resolves a lift above about 18 points
  at its default split. Smaller real gains read NOT PROVEN, which is not the
  same as no gain. Growing it is about sixteen hours of somebody writing SQL.

## How to propose something

Open an issue before writing the code, especially for anything on the second
list. A rejected pull request costs you more than a rejected issue.

[Documentation index](README.md) · Next: [Contributing](../CONTRIBUTING.md)
