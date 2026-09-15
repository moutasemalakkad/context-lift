<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com> -->

# Technical reference

[Back to the guide](README.md). Look up a detail here, then return to your stage.
Historical measurements and corrections are in the [research archive](research/README.md).

## Contents

- [Commands and workspace](#commands-and-workspace)
- [Answer-key checks](#answer-key-checks)
- [Scoring](#scoring)
- [Statistics and verdicts](#statistics-and-verdicts)
- [Agent backends](#agent-backends)
- [Costs and progress](#costs-and-progress)
- [Catalogs and context](#catalogs-and-context)
- [Data handling](#data-handling)
- [Workspace and exports](#workspace-and-exports)
- [Limits](#limits-what-this-does-not-do)
- [IAM](#iam)
- [Run identity](#run-identity)
- [Troubleshooting](#troubleshooting)

## Commands and workspace

The CLI has 48 commands. Use `contextlift --help` for the main loop,
`contextlift --help-all` for
specialist commands, and `contextlift <command> --help` for flags and the
failure that command prevents. CLI help is the flag reference.

`--workspace DIR` is global and goes before the command. Alternatively export
`CONTEXTLIFT_WORKSPACE`. A workspace records argv, input hashes, copied outputs,
stdout, exit status, and context versions. `index.json` links generation to
scoring. Keep that relationship in the index rather than reconstructing it
from filenames. `next` proposes the next command with absolute paths.

| Need | Command or file |
|---|---|
| Resume the loop | `next` |
| Read machine output | `--json` on a supported command |
| Inspect all versions against one baseline | `bundle`; the `rounds` tool in `chat` |
| Investigate one question across runs | `history` |
| Override a comparison deliberately | `pin baseline ID` or `pin candidate ID` |
| Review the recorded final comparison | `bundle` |

Ten commands accept `--json`: `score`, `gate`, `triage`, `ablate`, `climb`,
`attribute`, `score-routing`, `routing-gate`, `next`, and `generate` with
`--estimate-only`. For other commands, read the files they write. Do not parse
prose or assume every command accepts this flag.

The default baseline is the oldest scored arm without context. The candidate
is the newest fully scored arm with context. The pins' `explicit` field says
whether a person overrode those defaults. Within an editing campaign, compare
every candidate against that same baseline.

## Answer-key checks

`gate` audits the gold SQL before it can grade predictions. It has 16 defect
classes and 4 warnings. A flagged exit is a finding; inspect its class.

| Defect | Meaning / response |
|---|---|
| `NON_SELECT` | Not an allowed read-only query |
| `PLACEHOLDER_COMMENT` | Empty or unfinished SQL |
| `EXEC_FAIL` | SQL execution failed; inspect the error |
| `OVER_BUDGET` | Byte cap refused it; narrow the query or review a larger cap |
| `NO_ACCESS` | Identity or platform policy refused access |
| `TABLE_GONE` | Referenced table no longer exists |
| `TIMEOUT` | Query exceeded its time limit |
| `ZERO_ROWS` | Empty answer, often a stale filter |
| `DEGENERATE` | A trivial cell such as NULL or zero |
| `TRUNCATED` | Result reached the executor's row cap |
| `UNSTABLE` | Results differ across two runs; opt-in check doubles execution |
| `TEXT_AGGREGATE` | Numeric aggregate applied to text |
| `CROSS_PROJECT` | Gold names a project outside the configured scope |
| `WHOLE_ROW` | Outer `SELECT *` browses a table instead of specifying an answer |
| `LIMIT_NO_ORDER` | Row limit without deterministic ordering |
| `MISSING_PARAMETER` | Question omits an arbitrary date, ID, or other parameter |

Warnings ask for review and do not remove rows: `MISSING_LITERAL`,
`MISSING_ORDER`, `MISSING_PROJECTION`, and `LITERAL_ON_WRONG_COLUMN`.
Omitting a business code may be intentional when context supplies its meaning;
omitting an arbitrary reporting month is not. Literal matching cannot understand
every spelling or paraphrase, so a reviewer decides.

`--clean-out` names the clean CSV explicitly. Without it, a workspace keeps
the clean key under the gate run's outputs; `next` finds that path. Run `split`
over the clean key, generating both question CSVs and both ID lists. Generate
editing rounds from `curate.csv`; use `holdout.csv` only for the final reading.

## Scoring

Execution accuracy compares result rows, not SQL text. The implementation is
in [compare.py](../src/context_lift/compare.py) and the denominator rules are
in [harness.py](../src/context_lift/harness.py).

| Property | Behavior |
|---|---|
| Rows | Compare the full result, preserving duplicate multiplicities |
| Row order | Positional when the gold has `ORDER BY`; otherwise unordered |
| Columns | Search valid column permutations; names and order do not define equality |
| Numbers | Round floating values to four decimals; distinguish non-finite values |
| Types | No general coercion between strings and numbers |
| NULL | Distinct from zero and empty string |
| Extra columns | Rejected by default; `--extra-columns tolerate` reports a separate `MATCH_EXTRA_COLUMNS` result and the strict count |

`GOLD_ERROR` and `EMPTY_BOTH` are excluded because they cannot reliably grade
the prediction. Prediction errors and prediction timeouts count as wrong.
A row cap must remain visible in the result; silently truncating either side
would change the question being compared. Single-database row equality can
also accept a wrong query by coincidence; it does not prove SQL equivalence.

Save each score with `--save-report` and reuse it in `ablate`, `climb`, and
`attribute`. Across saved runs, mismatched scored question sets are refused.
The integrated `round` path can explicitly pair common results after per-arm
exclusions, rebuilds rates over that common set, reports dropped questions,
and refuses when the unpaired share exceeds its limit.

### Optional judge

The model judge checks only `MISMATCH` and `EMPTY_ONE` residuals. An
`EQUIVALENT` verdict can raise the score; it never rechecks a `MATCH`, so it
cannot lower the score. Backend errors leave the original result intact.
Label judge-adjusted results and audit samples from both matches and
mismatches before defending a headline.

The earlier claim that a published 2.5% / 8.1% error rate justified this judge
was **withdrawn on 2026-09-12**: those figures describe SQL text matching,
not this comparator. The correction and source are recorded in the
[research archive](research/README.md#measurement-corrections).

## Statistics and verdicts

- Accuracy uses a Wilson interval. Lift uses the paired interval printed by
  the selected analysis, rather than subtracting two accuracy intervals.
- McNemar's exact test uses discordant pairs: fixed versus broken answers.
  Six unanimous disagreements give a two-sided p-value of 0.03125; fewer
  cannot cross 0.05. That is a best-case resolution limit, not a power guarantee.
- Repetitions of one question share its difficulty. Use the report's
  question-level test when pooling repetitions. Ties abstain from that test;
  pooled repetition counts are not independent question counts.
- `climb` moves its ladder only above the printed noise floor. A higher raw
  score with an unchanged ladder remains inside the noise.

| Ruling | How to report it |
|---|---|
| `REAL GAIN` | Lift and interval, plus every regression |
| `NOT PROVEN` | Inconclusive; repeat the detectable lift and questions needed |
| `UNDERPOWERED` | The set cannot establish the target effect; do not read this as no effect |
| `NO CHANGE` | Check routing, then triage; changed failure shapes can still be wrong answers |
| `REAL LOSS` | Broken IDs first, then the loss and interval |

The safety word describes the observed questions: `SAFE` means none broke,
`MIXED` means more fixed than broke, and `REGRESSION` means breaks equal or
outnumber fixes. It does not guarantee future safety. Read counts from
`per_question`, including unstable outcomes, instead of counting rep-suffixed
IDs. Repeat all caveats printed with the result.

Attribution is a shortlist, not proof of causation. Its signals include
`introduced_identifier`, `phrase_in_literal`, and
`literal_changed_on_rule_column`. Confirm a suspect with an arm that removes
only that rule. `history` tracks one question's outcomes across versions.

### Routing

`score-routing` grades top-1 dataset and table selection. It normalizes
decoration and three-part names to the scorer's two-part convention; an empty
prediction is a miss. A malformed gold raises. With `--multi`, it compares
table sets and reports full coverage, exact equality, and pooled table recall.

Catalog snapping is optional and reported: `--catalog` can map a near-miss
name to the closest valid table. Compare arms with the same snapping settings.
`routing-gate` checks that the answer key is reachable from the catalog before
paying to generate answers. Retrieval recall and SQL correctness are separate
measurements.

## Agent backends

| Path | Input / output | Cost ownership |
|---|---|---|
| `generate` with a prompt backend | Question, schema, context → SQL | Estimate and budget the configured model |
| `generate --backend command` | JSON on stdin → SQL text or `{"sql": "...", "table": "..."}` on stdout | External runner manages its own spending |
| Conversational Analytics runner | Structured `Arm` → prediction, job identity, citations and matched-example metadata | Read the cost sheet's unknowns; query costs are separate |
| CSV replay runner | Recorded predictions → measured comparison | No new model calls |

The command-runner input contains `id`, `question`, `rep`, `schema`, and
`context`; it excludes gold SQL. Set `--runner-version` to a stable identifier
and expose failures and timeouts. Test repeated answers before a full run.
For structural `Executor` and `Runner` contracts, see
[Contributing](../CONTRIBUTING.md#adding-a-warehouse-or-an-agent).

`generate --retry` checks a draft against the database before one second
chance. Pass `--timeout 60` on SQLite; estimate and budget BigQuery execution.
This changes the arm's behavior and must be recorded consistently.

## Costs and progress

Generation, question drafting, rule drafting, model review, optional judging,
and warehouse execution can incur costs. Metadata-only operations and
analysis over saved reports do not execute the scored SQL. Read each command's
estimate rather than assuming the whole pipeline is free after generation.

Model prices in [backends.py](../src/context_lift/backends.py) are dated.
`--input-price` and `--output-price` override rates for a run where supported.
Output/thinking tokens must be estimated until measured usage is available.
Record actual `spent:` lines and reconcile warehouse job costs separately.
`round --cost` reads the recorded job ledger and reports cost per answer and
per correct answer. Missing jobs are counted as missing, not free.

Budget checks refuse an estimate above the ceiling and bound supported runs
between calls. A final in-flight call can overshoot; a budget is not an exact
invoice. `mine` saves candidates as it finds them and reports how many shapes
it reached if stopped on budget. That partial sample represents the reached
ranking, not the entire log.

Keep SQLite scoring to at most two processes. Run one BigQuery scoring
command at a time and use its worker pool. Heartbeats distinguish completed
work, a slow current item, and retry counts. A zero-retry slow response is not
evidence of throttling. Timeouts bound work that a row cap cannot, such as a
large sort before its first output row.

## Catalogs and context

Use this sequence within [guide stage 6](README.md#6-write-context-from-failures).
The commands assume the guide's exported workspace. Replace `P`, `us`, and
the table list with the reviewed scope; catalog access requires the optional
catalog dependencies and the permissions below.

```bash
contextlift catalog-list --project P --location us --out catalog.txt
contextlift catalog-pull --project P --location us --tables-file catalog.txt \
  --out served/ --pages pages/
contextlift draft-context --db "$DB" --tables-file catalog.txt --out pages_draft/
```

`served/` is the bundle the reader receives; `pages/` holds editable overviews.
Review generated drafts before installing them. `context-write` records a
reviewed source before publication; do not replace the served arm with
unreviewed generated pages. Pull again after a push to measure what is served.

```bash
contextlift catalog-push --project P --location us --docs pages/ \
  --rules context/rules.md --dry-run
```

Inspect this plan, then rerun without `--dry-run` to publish the reviewed
content. A dry run is not recorded under `runs/`. Add `--examples bank.json`
only for a separately reviewed, leak-checked bank. A push refuses held-out
answers in its payload; do not bypass that refusal.

One push per workspace may run at a time. Resume an interrupted push with
the same command; stamped pages return unchanged. Writes are read back. Report
failed or truncated reads as findings, then run `--verify-only` a few minutes
later before declaring persistent failure. Success from a write alone does
not show that a reader received the full content.

Custom entry groups require `--entry-group G --entry-id '{dataset}__{table}'`
on each pull and push. The default `@bigquery` shape derives entry names from
the table key; it does not search for them. `bigquery.txt` may include
`catalog: PROJECT LOCATION [ENTRY_GROUP]` so `next` proposes a catalog arm.

### Where knowledge belongs

| What you have | Where it goes | How | Evidence |
|---|---|---|---|
| a rule about **one table** | entry `overview` | `spec-push`, table-scoped rules | [practice 6](research/README.md#6-scope-the-rule) |
| a rule about the **whole estate or one domain** | rules file in the spec repo, delivered in the request | `rules/00-*.md`, `rules/10-*.md` | [practices 15 and 16](research/README.md#15-test-reference-selection) |
| a **worked example** | table entry's queries aspect | `spec-push`, `examples/*.jsonl` | [practice 13](research/README.md#13-measure-example-banks) |
| a **term the business uses**, estate-wide | glossary term under the glossary | `spec-push --glossary` | — |
| a **term one team uses differently** | term under a category | spec terms with a team tier | — |
| a **column description** | BigQuery schema, or request context in file mode | existing schema management | — |
| **which round wrote this page** | `context-provenance` aspect | `spec-push --provenance` | — |

A dash means this is an integration mechanism with no measured accuracy
benefit. Practice 6 is explicitly a hypothesis.
BigQuery owns column descriptions; updating a shared schema may expose them to both arms.
Keep the intervention isolated when comparing file and catalog context.

### Recorded catalog verification

These are dated [integration receipts](evidence/kc-roundtrip.txt) from
2026-09-13, not guarantees about
every catalog. The writable subset read back unchanged; the full file arm
contained additional channels. Matching content hashes establishes payload
identity, not identical answers from a stochastic agent.

```text
served: all 7 entry/entries whole; 3 of 7 carry an overview at all
writable part of the spec d07c1f37980d, read back d07c1f37980d
  the catalog reads back as what was written
  file arm: 7d7a83d66812
provenance: 7 record(s) written and read back with matching hashes
```

The round trip was **lossless for what the catalog accepts**.
**A catalog arm is a smaller arm** when channels are refused. An early refusal
said “the catalog reader does not read terms back”; that gap was subsequently
closed by the [glossary round-trip receipt](evidence/glossary-roundtrip.json).
`synonyms` and `related` links remain reported rather than silently lost.
Stale tool-authored terms are named; `--prune-terms` can remove them. Terms
authored elsewhere are never deleted by that cleanup.

The [tier probe](evidence/glossary-tiers.json) recorded:

```text
stored parents: {'revenue': 'glossary', 'attach-rate': 'category'}
verdict: TERM UNDER A CATEGORY WORKS
```

Implementation details verified by the probe scripts:

- The URL parent must be the glossary; the body parent selects the category.
- One glossary-level `terms.list` returns both tiers with their own parents.
- Creating a glossary is a long-running operation; creating terms is not.
- Deleting a glossary is long-running too; children must be removed first.
- Moving a term to a category requires `parent` in the update mask.

For rollback, `spec-push --previous-out DIR` saves entry records as JSON.
`catalog-rollback --previous DIR --project P` reads those records. A
`context-write --previous-out FILE` Markdown backup is a different artifact
and cannot restore a catalog. Preserve the correct backup before writing.

<details>
<summary>Transport observations and a retracted diagnosis</summary>

The earlier deployment record distinguished repeated probes from single
observations. These describe those runs, not a service-wide guarantee:

| Observation | Consequence |
|---|---|
| `lookupContext` returned YAML, with Markdown in `overview` | Parse YAML; folded lines defeat substring parsing |
| Custom aspects were absent from lookup, present in `entries.get?view=ALL` | Read the channel actually carrying the content |
| Entry names used project numbers | Do not substitute project IDs when constructing names |
| Warm reads were byte-identical: 60 on one estate, 43 on another | The observed post-write stubs did not establish general read flakiness |
| A 15,606-character entry became 6,287 characters at both tested context budgets, 500 and 2,000 | Treat `context_budget` as best effort; verify returned content |
| Multi-entry lookup expanded into related entries | Fetch one entry per call and check its identity |
| `parentEntry` distinguished 127 tables from two non-table entries in a custom group | Entry type alone did not identify tables |

**Retracted diagnosis:** a credentials quota project was blamed for empty
search results. Retests returned nonzero results with and without it; that
cause was not established. Check the actual search predicate and permissions.
An entry-group name prefix worked in semantic mode and returned zero in
keyword mode on the observed estate. `check_scope` tests a known-good query
so an empty, apparently successful search cannot silently define the catalog.
The direct transport now constructs entry names from keys instead of searching.

</details>

## Data handling

Select the row matching the feature you use. The framework does not add a
telemetry endpoint, but the services and external runner you choose receive
the data needed for their work.

| Feature | Data crossing the boundary |
|---|---|
| Warehouse executor | Gold and predicted SQL sent under your credentials; results return for scoring |
| Prompt generation | Questions, supplied schema, and selected context sent to the configured model |
| Rule/question drafting and model review | Relevant questions, SQL, or failure material included in that feature's prompt |
| Optional judge | Question, gold/predicted SQL, and samples of both result sets |
| Conversational Analytics | Structured schema/context and question; the service can query the warehouse internally |
| Command runner | Its documented JSON input; the external program controls further handling |
| Catalog push / spec push | Authored overviews, rules, examples, supported terms, and optional provenance |

`build_inline_context` in [runners/ca_api.py](../src/context_lift/runners/ca_api.py)
constructs CA context. The runner keeps generated SQL and run metadata rather
than treating the service's displayed answer as the scored result.
[judge.py](../src/context_lift/judge.py) builds the optional judge payload.
Catalog **content** is distinct from the bounded provenance metadata below.

### Execution and confirmation controls

`read_only_violation` runs before SQL in `SQLiteExecutor`, `DuckDBExecutor`,
and `BigQueryExecutor` in [executor.py](../src/context_lift/executor.py).
It masks literals, strips comments, and refuses disallowed statements,
writes, and external engine calls. It also refuses filesystem reader names
such as `read_text`, `read_csv`, and `read_parquet`.

DuckDB additionally sets `disabled_filesystems='LocalFileSystem'` on its
read-only connection. Database `read_only=True` alone would not prevent
filesystem reads. `glob` remains allowed in the shared text check because it
is a valid SQLite pattern function; the engine setting handles DuckDB's case.

Credentials come from the environment or platform identity. Keep secrets out
of argv and supplied files, which the workspace can record.
`CONTEXTLIFT_FORBIDDEN_PROJECTS` holds refused project prefixes;
`refuse_forbidden_project` in [backends.py](../src/context_lift/backends.py)
checks them. An unset list refuses no projects.

`confirm_paid` and `confirm_holdout_read` use `ask_tty` in
[confirm.py](../src/context_lift/confirm.py). Where these confirmations apply,
they require `RUN` from `/dev/tty`; piped stdin is not approval. `--yes` waives
the confirmation only with a budget, and records the waiver with argv.

### Catalog provenance

A `catalogViewer` can read this aspect and an agent may read it through MCP.
The `Provenance` dataclass in [provenance.py](../src/context_lift/provenance.py)
has these fields:

```text
round_id, measured_at, runner, model_alias,
arm_control_hash, arm_treatment_hash, overview_hash, queries_hash,
k_repeats, served_state, gates
```

This metadata carries labels, hashes, counts, and gate outcomes, not question
text, SQL, or rows. The separate catalog content payload can contain authored
text and worked SQL examples; the metadata limits do not sanitize that content.

`ALLOWED_KEYS` permits 19 names, including future fields `table_lift_pp`,
`table_ci_low_pp`, `table_ci_high_pp`, `regressions`, `recall_misses`,
`terms_hash`, `question_set_hash`, and `schema_hash`. `MAX_STRING` is 80
characters. `check_payload` refuses invalid values rather than truncating
them, including nested gate details. The allowlist bounds permitted metadata;
it is wider than the fields currently set by the dataclass.

## Workspace and exports

The workspace is sensitive: questions, SQL, context, output copies, argv, and
stdout can all identify your data. Keep it outside the source repository.
`protect_runs_from_git` writes a `runs/.gitignore`; a fresh workspace gets
`WORKSPACE_GITIGNORE`. Existing root ignore files are left intact, and
`git_exposure_warning` reports exposed paths. These helpers live in
[workspace.py](../src/context_lift/workspace.py).

`share` builds an allowlisted summary and scans it for forbidden names; see
[share.py](../src/context_lift/share.py). It does not make arbitrary workspace
files safe to publish. Review the output and retain the private pseudonym map
separately. `scan` and `make scan ALL=1` check configured secret patterns.
The bundle's `verdict.html` has no script tags or event handlers; its content
can still be sensitive even though opening the page executes no script.

## Limits. What this does not do

- **No model-service sandbox.** The chosen agent's data policy and access
  remain part of the deployment; this tool does not constrain its internals.
- **No workspace encryption.** Protection depends on filesystem permissions.
- **`--yes` is a real bypass.** It is budgeted and recorded, but still bypasses
  the terminal confirmation.
- **The SQL rail is a text check, not a database privilege.** Use a read-only
  database identity as the durable control as well.
- **Query costs remain real.** Byte caps, timeouts, and estimates bound work;
  they do not make warehouse execution free or estimates exact.
- **Agent pricing can be incomplete.** CA data-token costs may be unknown;
  the cost sheet says so. Use a pilot and `--usd-per-call` where appropriate.
- **No third-party audit.** The repository's checks are not an external
  security certification.

## IAM

This table is derived from client calls and recorded integration runs.
**Not yet verified against a live estate for every row.** “Recorded” means
the earlier run exercised it; derived rows are starting points to validate
with your platform owner. `doctor --explain` helps interpret an actual refusal.

| Command / service | Access | Basis |
|---|---|---|
| Local demo, doctor | No cloud identity | Recorded |
| BigQuery gate, score, round | `roles/bigquery.jobUser` on billing project; `roles/bigquery.dataViewer` on datasets read | Recorded |
| BigQuery log | Job-history access, such as `roles/bigquery.resourceViewer`, plus query-job access | Recorded |
| CA runner | `roles/geminidataanalytics.dataAgentUser` plus warehouse access | Recorded |
| Catalog list/pull | `roles/dataplex.catalogViewer` | Recorded |
| Catalog writes/rollback | `roles/dataplex.catalogEditor`; aspect-type creation also needs appropriate ownership | Derived |
| Sentinel | Catalog write and CA runner access | Derived |
| Evaluation room | `roles/bigquery.dataEditor` on the evaluation project where views are created | Derived |

A VPC Service Controls refusal is a perimeter issue even if IAM allows the
request. Granting more roles does not repair a perimeter mismatch.

## Run identity

[identity.py](../src/context_lift/identity.py) adds `context-lift/VERSION`
and `context-lift-caller-ORIGIN/VERSION` user-agent tokens to service requests.
The installed package and CLI are named `contextlift`; the service attribution
token deliberately retains `context-lift`. No extra attribution request is made.

The origin is `terminal`, `agent`, `ci`, or `script`, inferred from the
environment or set with `CONTEXT_LIFT_CALLER`. Wrappers can append a
`name/version` token through `CONTEXT_LIFT_USER_AGENT_EXTRA`. Keep identifiers
and secrets out of that string. BigQuery jobs carry labels such as
`app=context-lift`, `caller=terminal`, and `stage=mine`.

Use those labels and job IDs to attribute warehouse spend. A shared project's
time window includes other people's jobs. Other services need their own usage
records; BigQuery labels do not reconcile the model invoice. Forks can change
`client_info()` and `job_labels()` in `identity.py` to disable these markers.

## Troubleshooting

| Symptom | Next check |
|---|---|
| Command not found | Activate the repo's `.venv` before changing directory |
| Unrecognized `--workspace` | Put it before the verb, or export the workspace variable |
| Unrecognized `--json` | Check the supported-command list above |
| Table missing in billing project | Use three-part table names and verify actual access |
| Both arms fail or abstain | Check key quality and schema/catalog coverage |
| `OVER_BUDGET` | Read the estimate and offending query before changing scope or cap |
| `NO_ACCESS` | Check identity and perimeter policy with the owner |
| Catalog read differs from write | Report the failed check; retry `--verify-only` after a few minutes |
| Partial or stalled run | Inspect heartbeat, retries, timeout, and saved progress |
| Different scored question sets | Compare the key and exclusions; do not pair by file position |

[Return to your stage in the guide](README.md#the-route).
