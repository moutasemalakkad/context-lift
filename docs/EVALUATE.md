<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com> -->

# Evaluate your data

[Documentation](README.md) / Evaluate your data

Build a reviewed answer key, measure a baseline, then test whether written
context improves your agent. Start with a small, recognizable query sample to
check the setup; choose the measurement size after reviewing its coverage.

**Before you start:** finish the [Quickstart](QUICKSTART.md), identify the data
owner and reviewer, and read [what your chosen backend sends](SECURITY-AND-DATA.md).

## On this page

- [Install and create a workspace](#1-install-and-create-a-workspace)
- [Choose SQLite or BigQuery](#2-connect-your-data)
- [Review and split the answer key](#3-review-and-split-the-answer-key)
- [Measure the baseline](#4-measure-the-baseline)
- [Write and compare context](#5-write-and-compare-context)
- [Read the holdout once](#6-read-the-holdout-once)
- [Troubleshooting](#troubleshooting)

## 1. Install and create a workspace

From the cloned repository:

```bash
make install
source .venv/bin/activate
uv pip install --python .venv/bin/python -e ".[gemini]"
```

That installs the default generation backend used below. For BigQuery, also run
`uv pip install --python .venv/bin/python -e ".[bigquery]"` from the repository.
Then create a workspace:

```bash
export CONTEXTLIFT_WORKSPACE=/absolute/path/to/my-evaluation
mkdir -p "$CONTEXTLIFT_WORKSPACE"
cd "$CONTEXTLIFT_WORKSPACE"
```

Replace the absolute path before running this. Keep the workspace outside the
repository: it contains questions, SQL, context, reports, and copied inputs.
The exported variable records every command in that workspace. Alternatively,
pass `--workspace DIR` **before** the command name.

```bash
contextlift --workspace "$CONTEXTLIFT_WORKSPACE" next
```

`next` prints the next command with absolute paths. Run it after each stage;
it reads the workspace index, so you do not have to reconstruct run IDs.
In an assistant or script, add `--json` to `next` and other commands that
support it. Check the command's help before adding that flag.

### Costs and credentials

Configure `GEMINI_API_KEY` in your environment for the Developer API, or
`CONTEXTLIFT_PROJECT` and Application Default Credentials for Vertex AI.
Do not put credentials in commands you commit.

Before each paid command, run the same command with `--estimate-only`.
Read the estimate, then replace that flag with `--budget USD`, using the
ceiling you approved for that command. Model generation and BigQuery SQL have
separate costs. On BigQuery, read both the query/bytes/USD estimate and the
calls/time estimate; keep `--max-bytes 1GB` unless you explicitly choose a
larger per-query cap. A cost from someone else's run is not your estimate.

Some commands also require a terminal confirmation. A budget flag does not
replace that confirmation. Keep the final `spent:` line with the run record.

## 2. Connect your data

Choose one of the following paths. Both produce candidates for review.

### SQLite

Place a consistent SQLite snapshot at `data.sqlite` in the workspace. Bring
an existing query log as `queries.csv`; SQLite does not retain query history.
Use a CSV with an `sql` column (optional log columns are described by
`contextlift mine --help`). Start with queries the reviewer recognizes.

```bash
contextlift mine --db data.sqlite --queries queries.csv --limit 10 \
  --all-datasets --timeout 60 --out candidates.csv
```

`--all-datasets` deliberately uses the supplied SQLite log as its scope.
This mines SQL shapes without model-written question text. A person writes
questions during review. To ask the model to draft them instead, add
`--write-questions`, estimate first, then run with an approved budget.

### BigQuery

Use Application Default Credentials for an identity with access to the jobs
and datasets you intend to read. See [IAM per command](SECURITY-AND-DATA.md#iam-per-command).

First establish scope from a data-owner-provided list or query demand:

```bash
contextlift log --demand --db bq://BILLING_PROJECT --days 30 \
  --max-bytes 1GB --out demand.csv --estimate-only
```

Replace `BILLING_PROJECT`; select `--region` if the jobs are outside `us`.
After reviewing the estimate, rerun with `--budget USD` instead of
`--estimate-only`, then read `demand.csv`. Choose datasets using demand and
the owner's input, not their names. Investigate accounts over 50 jobs per
account per day; exclude confirmed pipelines with `--exclude-accounts`.

Record the selected source in `bigquery.txt` at the workspace root:

```text
bq://BILLING_PROJECT
datasets: DATA_PROJECT.sales, DATA_PROJECT.finance
```

Export a small sample from that scope:

```bash
contextlift log --db bq://BILLING_PROJECT --days 30 --limit 10 \
  --datasets DATA_PROJECT.sales,DATA_PROJECT.finance \
  --min-tables 2 --require-analytical --max-bytes 1GB \
  --out queries.csv --estimate-only
```

Again, replace the estimate flag with your approved budget to write the CSV.
The analytical filters suit a study of multi-table analytical questions;
change them deliberately if your intended workload includes single-table queries.
Use `--only-accounts` or `--labels` to focus on a known team.

```bash
contextlift mine --db bq://BILLING_PROJECT --queries queries.csv \
  --datasets DATA_PROJECT.sales,DATA_PROJECT.finance --limit 10 \
  --write-questions --max-bytes 1GB --out candidates.csv --estimate-only
```

Review the warehouse and model costs before the budgeted run. Pull one log
per billing project when jobs run in several projects; repeat `--queries`
for each file. Use three-part table names in SQL to avoid resolving a table
in the wrong project.

## 3. Review and split the answer key

```bash
contextlift review --candidates candidates.csv --out review/
```

Read `review/REVIEW.md`, then decide every row in `review/review.csv`:

1. Does the question say what the analyst meant, with the right parameters?
2. Does the SQL answer that question with a stable result?
3. Does the set cover the intended workload, including questions that require
   business definitions rather than only schema knowledge?

Correct question text and decide accept/reject in the review sheet. Do not
create a replacement gold file by hand. Once no row is undecided:

```bash
contextlift review --sheet review/review.csv --out golds.csv
```

If using `review --draft`, estimate it first and use a reviewer from a different
model family than the agent. Report the resulting “N of M golds accepted by a
model reviewer” caveat until a person audits the rows.

For SQLite, audit the key:

```bash
contextlift gate --db data.sqlite --golds golds.csv --timeout 60
```

For BigQuery, replace the database, add `--max-bytes 1GB`, estimate first, then
use the approved budget. The gate audits **16 defect classes** and **4 warnings**.
A nonzero exit with flagged golds is a finding: inspect the classes before
continuing. The clean key is saved under the gate run's outputs.

Run `contextlift next` for the split command using that clean key. It writes
`curate.csv`, `holdout.csv`, and their ID lists. Check coverage and whether the
set can resolve your target effect before spending on generation. Ten sample
queries help validate setup; they are not a promise of statistical power.

**Expected result:** a reviewed, gated key with a fixed working/reserved split.
Use only the working questions for curation. Do not edit or inspect reserved
answers to improve the context.

## 4. Measure the baseline

Run the `schema` command proposed by `next`. On BigQuery, it uses the datasets
in `bigquery.txt` or the table list in `catalog.txt`; a billing project alone
is not a schema scope.

Before adding any context, estimate baseline generation:

```bash
contextlift generate --questions curate.csv --schema schema.txt \
  --out arms/base.csv --reps 3 --estimate-only
```

Rerun with the approved budget. Three repetitions write `base_rep1.csv`,
`base_rep2.csv`, and `base_rep3.csv`. Keep the model, schema, settings, and
question set constant across the two arms.

For your own agent, use `--backend command --command 'YOUR_RUNNER'` and
`--runner-version VERSION`. Its spending is managed by that runner. Check one
question three times first; identical repetitions are copies, not independent
samples. See [runner contracts](AUTHORING.md#an-agent-runner).

Score each predictions file once. This is the SQLite command for repetition 1:

```bash
contextlift score --db data.sqlite --predictions arms/base_rep1.csv \
  --timeout 60 --save-report reports/base_rep1.json
```

Repeat for repetitions 2 and 3 with matching filenames. On BigQuery, estimate
each score command, use its approved budget, and keep `--max-bytes 1GB`.
Run at most two SQLite scorers concurrently; run one BigQuery scoring command
at a time, using its worker threads. Later analyses reuse these reports.

**Expected result:** three scored baseline predictions, recorded in the index.
The baseline pin defaults to the oldest scored arm without context.

## 5. Write and compare context

Start with the baseline's largest failure group:

```bash
contextlift triage --report reports/base_rep1.json --show
contextlift draft-rules --report reports/base_rep1.json \
  --predictions arms/base_rep1.csv --ids curate_ids.txt \
  --out context/rules_draft.md --estimate-only
```

Run the draft command with its approved budget. A person must read the draft
before installing it. Use the [context-writing guide](ONBOARDING.md) to review
scope, definitions, and examples; model suggestions are not verified rules.

```bash
contextlift context-write --source-file context/rules_draft.md \
  --to context/rules.md
contextlift leak --predictions holdout.csv --context context/rules.md
contextlift generate --questions curate.csv --schema schema.txt \
  --context context/rules.md --out arms/rules.csv --reps 3 --estimate-only
```

After reviewing the generation estimate, run with a budget. Score the three
`rules_repN.csv` files once each, as for the baseline. Then compare saved reports:

```bash
contextlift ablate \
  --without-report reports/base_rep1.json \
  --without-report reports/base_rep2.json \
  --without-report reports/base_rep3.json \
  --with-report reports/rules_rep1.json \
  --with-report reports/rules_rep2.json \
  --with-report reports/rules_rep3.json --ids curate_ids.txt --show
```

This reading uses only working questions and executes no SQL. Read the lift
with its interval, fixed/broken IDs, ruling, and caveats. Use the latest fully
scored context arm as the candidate; compare all versions against the same
baseline. `pin` is an explicit override, not a routine step.

Repeat curation on working questions only. Install replacements with
`context-write` (or use `--add` to append). Rule attribution gives leads;
confirm a suspect by regenerating an arm with that rule removed.

## 6. Read the holdout once

When curation is complete, follow `next` for the final comparison. The two
pinned versions each answer `holdout.csv` with three repetitions. Estimate and
budget each generation, score each predictions file once, then use one plain
`ablate --without-report ... --with-report ... --show` with all six reports
and **no `--ids`**. Do not use its failures for another curation round.

Report the lift and interval **together with broken-question IDs**, the ruling,
and every caveat. A positive lift with regressions needs both facts. An
inconclusive result does not establish no effect; repeat the report's sample
size guidance. A ladder gain inside the noise floor is not an improvement.

Use `contextlift bundle --help` to package the recorded comparison. The bundle
and workspace index retain which versions were compared and how many reserved
readings occurred.

## Troubleshooting

| Symptom | Check next |
|---|---|
| `contextlift: command not found` | Activate the repository's `.venv` before changing directories |
| `unrecognized arguments: --workspace` | Put the global flag before the command, or export the workspace variable |
| Golds are `OVER_BUDGET` | Inspect the estimate and offending queries; raise the cap only after reviewing the cost |
| `NO_ACCESS` or `TABLE_GONE` | Check identity/policy or table existence; these do not mean the SQL is malformed |
| Tables are reported missing in the billing project | Use fully qualified `project.dataset.table` names |
| Both arms abstain or fail on the same questions | Check schema/catalog coverage before writing more context |
| `NOT PROVEN` or `UNDERPOWERED` | Read the detectable-lift guidance; assess sample size and coverage |
| `NO CHANGE` | Check routing, then triage the working-set failures |
| Context breaks questions | List the IDs and inspect the applicable rules before adopting the change |
| A long stage appears stalled | Read its heartbeat and retry counts; keep query timeouts bounded |

For operational measurements behind these checks, see [Field notes](FIELD-NOTES.md).

[Documentation index](README.md) · Next: [Write business context](ONBOARDING.md)
