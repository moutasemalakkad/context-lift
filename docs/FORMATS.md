# File and Directory Formats

Reference for the CSV files, JSON payloads, plain-text lists, `--spec`
repositories, and `--workspace` directories read or written by `contextlift`.
All text files are UTF-8. CSV files include a header row; column order does not
matter on input, and extra columns are ignored unless noted.

## CSV Files

| File | Written by | Read by | Required columns | Optional columns |
|---|---|---|---|---|
| `demand.csv` | `log --demand` | human inspection | `dataset`, `jobs`, `distinct_shapes`, `distinct_accounts`, `bytes_billed_total`, `jobs_per_account_day`, `top_account_share` | none |
| `query_log.csv` | `log` | `mine --queries`, `mine-context --queries`, `audit-golds --queries` | `sql` | `times_seen` (default `1`), `ran_by` (`;`-joined emails or MD5 hashes), `distinct_users`, `question` |
| `candidates.csv` | `mine` | `review --candidates` | `id`, `question`, `gold_sql`, `times_seen`, `gate_status`, `gate_detail`, `table`, `accounts`, `out_of_scope` | none |
| `review.csv` | `review --candidates` | `review --sheet` | `id`, `question`, `keep`, `comment`, `reason`, `wording`, `model_keep`, `model_reason`, `gold_sql`, `gate_status`, `gate_detail`, `table`, `times_seen`, `source`, `sql_sha256` | extra columns from `candidates.csv` are kept at the end |
| `golds.csv` | `review --sheet` | `gate`, `split`, `round`, `generate --questions`, `leak`, `curate-examples`, `build-examples` | `id`, `gold_sql` (`gate` minimum); `question` (required by `round`, `generate`, `leak`, `curate-examples`, `build-examples`, and `gate`'s `MISSING_LITERAL` check) | extra columns are ignored |
| `predictions.csv` | `generate` | `score`, `ablate`, `climb`, `triage`, `attribute` | `id`, `question`, `gold_sql`, `pred_sql` | extra columns are ignored |
| `predictions_arm.csv` | external runner | `round --predictions-control`, `round --predictions-treatment` | `id`, `pred_sql` (joined against `--golds` by `id`) | extra columns are ignored |
| `run.csv` | `round --run-out` | `history`, offline recomputation | `id`, `split`, `arm`, `arm_id`, `question`, `gold_sql`, `pred_sql`, `verdict`, `correct`, `citations`, `matched_example`, `job_id`, `thought_chars`, `retries`, `error_class` | `bytes_billed`, `query_cost_usd` (added by `round --cost`) |
| `routing.csv` | `catalog-route` | `score-routing` | `id`, `question`, `pred_table` | `gold_table` (falls back to the primary table of `gold_sql`) |

### `review.csv` cell values

- `keep`: `yes`, `y`, `true`, `1`, or `keep` accepts the row; `no`, `n`, `false`, `0`, `drop`, or `reject` drops it; blank stays undecided.
- `reason`: optional failure label (`matches`, `does_not_match`, `dashboard_shaped`, `dropdown_or_metadata`, `duplicate`, `ambiguous_question`, `not_a_question`).
- `source`: `reviewed` (human decision), `model-reviewed:<model>` (`review --draft`), or `gate-refused`.

## Spec Directory (`--spec DIR`)

A spec directory versions context in git and compiles into two paired arms:
`0d control` (`schema/` only) and `1 authored` (`schema/` plus all other files).
Every directory is optional; structured files accept `.json` (stdlib only) or
`.yaml` / `.yml` (`uv pip install -e '.[yaml]'`).

```text
spec/
  rules/
    00-definitions.md
    10-output-conventions.md
  schema/
    project.dataset.table.yaml
  authored/
    project.dataset.table.yaml
  vocabulary/
    terms.yaml
  examples/
    curate.jsonl
    ROUTED
  relationships.yaml
```

| Path | Arm | Shape |
|---|---|---|
| `rules/*.md` | treatment | Markdown bullet lines (`- ` or `* `), concatenated in filename order into `systemInstruction` with a trailing precedence sentence. A leading two-digit prefix (`00-` to `99-`) orders tiers from broadest to most specific; `lint` flags untiered files. Glossary extraction (`attribute`, `catalog-push`) reads `- "phrase" -> meaning` bullets naming `table.column` or `snake_case` identifiers. |
| `schema/<project>.<dataset>.<table>.{yaml,json}` | control and treatment | Mapping of metadata the warehouse already carried. Keys: `description` (`str`), `synonyms` (`list[str]`), `tags` (`list[str]`), and `columns` (`dict[col_name, str \| {description, synonyms, tags}]`). |
| `authored/<project>.<dataset>.<table>.{yaml,json}` | treatment | Same shape as `schema/`, merged per table and column on top of `schema/` so curator wording overrides existing descriptions in the treatment arm only. |
| `vocabulary/*.{yaml,json}` | treatment | List of term mappings (or `{"terms": [...]}`). Keys per term: `name` (required), `definition`, `tier` (default `"org"`), `also_called` (`list[str]`), `related` (`list[str]`). |
| `examples/*.jsonl` | treatment | One JSON object per non-blank line: `{"question": "...", "sql": "..."}`. When `examples/` is non-empty, `lint` checks for `examples/ROUTED` or `routing.yaml` at the spec root so unrouted prompt injection is caught before a paid run. |
| `relationships.{yaml,json}` | treatment | List of join mappings passed to `schemaRelationships` whenever both endpoint tables (`left` / `from` and `right` / `to`) belong to the arm. |

## JSON, Plain-Text, and Subprocess Contracts

### Saved score report (`score --save-report FILE`)

```json
{
  "n_total": 30,
  "results": [
    {
      "id": "q01",
      "question": "Monthly revenue in 2025?",
      "verdict": "MATCH",
      "execution_correct": true,
      "adjusted_correct": true,
      "judged": false,
      "judge_verdict": null,
      "detail": "exact match (12 rows)",
      "rows_preview": null,
      "answer": null
    }
  ]
}
```

When multiple repetitions are pooled, `id` carries a `#repK` suffix (`q01#rep1`).
Headline proportions and Wilson intervals are recomputed on load rather than
stored.

| `verdict` | Counted in accuracy denominator? | Meaning |
|---|---|---|
| `MATCH` | yes (correct) | returned the same rows as the answer key |
| `MISMATCH` | yes (wrong unless judge upgrades) | ran, but returned different rows |
| `EMPTY_ONE` | yes (wrong unless judge upgrades) | one side returned no rows and the other returned some |
| `PRED_ERROR` | yes (wrong) | the predicted query failed to run |
| `EMPTY_BOTH` | excluded | both sides returned no rows, so nothing was measured |
| `GOLD_ERROR` | excluded | the answer key's own query failed to run |
| `ROW_CAP_HIT` | excluded | a row cap cut one of the results, so they cannot be compared |

### Other JSON and text files

| File | Flag or role | Format |
|---|---|---|
| `bank.json` | `curate-examples --out`, `generate --examples` | JSON object mapping `<question_id>` to a formatted block of worked examples |
| `context_per_question.json` | `catalog-route --context-out`, `generate --context-per-question` | JSON object mapping `<question_id>` to per-question context text |
| `curate_ids.txt`, `holdout_ids.txt` | `split`, `--ids`, `split --spent` | Plain text, one question `id` per line; blank lines and `#` comments ignored |
| `catalog.txt` | `catalog-list --out`, `--catalog`, `--tables-file` | Plain text, one `dataset.table` or `project.dataset.table` per line; blank lines and `#` comments ignored |
| `bigquery.txt` | workspace warehouse pointer | Line 1: `bq://BILLING_PROJECT[/DATA_PROJECT.DATASET]`; optional `datasets: A, B` and `catalog: PROJECT LOCATION [ENTRY_GROUP]` lines |

### External agent protocol (`generate --backend command --runner-cmd CMD`)

`generate` invokes `CMD` once per `(question, rep)` and passes one JSON object on `stdin`:

```json
{
  "id": "q01",
  "question": "Monthly revenue in 2025?",
  "rep": 1,
  "schema": "CREATE TABLE ...",
  "context": "# 00-definitions\n..."
}
```

When `--retry` re-asks after a failed query, the payload adds `"attempt"` (`int`),
`"previous_sql"` (`str`), and `"error"` (`str`). `stdout` may be either raw SQL
text or a JSON object `{"sql": "SELECT ...", "table": "dataset.table"}`.

## Workspace Directory (`--workspace DIR`)

Passing `--workspace DIR` (or setting `$CONTEXTLIFT_WORKSPACE`) records inputs,
outputs, hashes, and terminal streams for each command and rebuilds `index.json`.

```text
DIR/
  index.json              derived run ledger (schema_version: 1, pins, arms, runs, holdout_looks, warnings)
  contextlift.json        explicit pins written by `contextlift pin`
  verdict.html            single-file report written by `contextlift bundle`
  cache/
    golds.json            cached gold SQL result sets keyed by query hash
  runs/
    .gitignore            ignores run directories by default (`*` and `!.gitignore`)
    <YYYYMMDD-HHMMSS-verb>/
      manifest.json       argv, exit_code, git_sha, input/output SHA-256 hashes, timestamps
      inputs/             immutable copies of input files read by the command
      outputs/            copies of output files produced by the command
      stdout.txt          captured standard output
      stderr.txt          captured standard error
```
