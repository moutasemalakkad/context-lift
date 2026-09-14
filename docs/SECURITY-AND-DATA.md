# Security and data handling

Written for a security reviewer who has not read the source. Every claim names
the function or constant that implements it, so each one can be checked rather
than taken on trust. `tests/test_security_doc.py` fails if a symbol named here
stops existing.

Last reviewed 2026-09-12 against `main`.

## The short answer

| | |
|---|---|
| Does it write to your warehouse? | **No.** Every statement is checked read-only before it runs, in all three backends. |
| Can a query it runs read files off the machine? | **No.** Filesystem-reading functions are refused by name, and DuckDB connections additionally run with `disabled_filesystems='LocalFileSystem'`. Both are tested. |
| Does your data leave your cloud? | **Only to the model service you point it at**, and that is the same service your agent already uses. See "Three channels" below. |
| Does it store your data anywhere? | On your machine, in the workspace directory. Nothing is uploaded. |
| What does it write into Knowledge Catalog? | Hashes, a timestamp and a model name. No content. |
| Can an AI assistant driving it approve spending? | **No.** Confirmations are read from `/dev/tty`, not stdin. |
| What is the one thing to be careful about? | The workspace directory holds your questions, your SQL and model answers about your data. Keep it out of git. See "The workspace is sensitive". |

## Three channels, and nothing else

Everything that crosses a boundary goes through one of these. There is no
telemetry, no phone-home, no analytics and no update check.

**1. Your warehouse.** `SELECT` statements, run with your credentials.
`read_only_violation` (`executor.py`) is called before every execution in
`SQLiteExecutor`, `DuckDBExecutor` and `BigQueryExecutor`. It strips comments,
masks string literals, then rejects anything that is not a single statement
starting with `SELECT` or `WITH`, anything holding a write keyword, and
anything calling out of the engine (`EXTERNAL_QUERY` is refused by name,
because the `DELETE` inside `EXTERNAL_QUERY('conn', 'DELETE FROM t')` is a
string here and a statement on the other database).

Literals are masked FIRST for a reason found in the field: a real gold
containing `WHERE outcome = "Investigation complete; no suspect identified"`
was rejected because the semicolon inside the quotes read as a second
statement.

**A query that reads the FILESYSTEM is refused too, and this needed a second
control.** DuckDB ships filesystem readers as ordinary table functions, so
`SELECT content FROM read_text('/etc/passwd')` starts with `SELECT`, names no
write keyword, and passed every check above. Against
`duckdb.connect(db, read_only=True)` it returns the file. `read_only=True`
protects the database *file* from writes and says nothing about reads
elsewhere on disk — which is not what the name suggests.

That matters here more than in most tools, because the SQL was written by a
**model**, from a prompt built out of business context that people author and
that `mine` also reads out of a query log. A sentence in that context is an
untrusted input with a filesystem behind it, and the answer lands in a CSV
somebody reads.

Two controls now, because either alone is insufficient:

- `read_only_violation` refuses `read_text`, `read_csv`, `read_parquet`,
  `read_blob`, `read_json`, `parquet_scan` and the rest by name. A regex over
  SQL text will always miss a spelling, so this is not the control — it is the
  message, giving a person a reason instead of an engine error.
- `SET disabled_filesystems='LocalFileSystem'` on every read-only DuckDB
  connection. This one is authoritative: the engine refuses however the query
  is spelled. Verified that ordinary queries against attached tables still
  work.

`glob` is deliberately **not** in the name list. In DuckDB `glob('/etc/*')`
lists the filesystem; in SQLite `glob(X, Y)` is the function form of the GLOB
pattern operator and `SELECT glob('a*','abc')` returns 1. The text check is
shared by all three backends, so blocking the name would refuse valid SQLite
queries to close a DuckDB hole. The engine setting handles it where it is
real.

SQLite needs no equivalent: `ATTACH DATABASE` does not begin with `SELECT`, so
the first-keyword check already refuses it, and `readfile()` is a shell
function that the Python module does not expose.

**2. The model service.** Table names, column names, column descriptions, your
written rules, glossary terms, worked examples, join relationships and the
question text. Assembled by `build_inline_context` (`runners/ca_api.py`).

Be clear about what this implies: **the agent executes queries against your
warehouse and sees result rows in order to answer.** That happens inside the
Conversational Analytics service, under your project and your credentials, and
it is what the agent you are evaluating already does. This tool keeps only the
`generatedSql` from the reply. It does not request, store or score result
rows from the agent; it re-runs the SQL itself to score it.

**3. `contextlift share`, and only when you run it.** The single sanctioned
export. See below.

## Credentials

Never read from a file this tool writes, never written to one, never printed
and never included in a run manifest. It uses whatever your environment
already provides — Application Default Credentials, or a Cloud Run metadata
server.

`CONTEXTLIFT_FORBIDDEN_PROJECTS` takes a comma-separated list of project-id
prefixes. `refuse_forbidden_project` (`backends.py`) stops before anything is
billed to a match. `contextlift doctor` prints whether it is set, and says
plainly that an unset list refuses nothing.

## What an AI assistant driving this can and cannot do

This is the part most reviewers have not had to think about yet, and it is
worth reading twice.

A host agent — Claude Code, Gemini CLI — can run these commands. It **cannot**
approve the two things that matter:

- **Spending.** `confirm_paid` (`confirm.py`) shows the cost, then requires
  the word `RUN` typed exactly. It reads through `ask_tty`, which opens
  `/dev/tty` rather than calling `input()`, so `echo RUN | contextlift ...`
  cannot spend money. No terminal means no.
- **Opening the reserved questions a second time.** `confirm_holdout_read`
  behaves the same way.

Verified empirically on 2026-09-12, by calling both functions inside an
agent's own shell: `allowed: False` at both, reason "no controlling terminal
and no --yes". Note that `[ -r /dev/tty ]` reported the device readable in
that shell and the guard still held, because `ask_tty` opens it `r+` and that
fails.

**The one bypass, stated plainly.** `--yes` skips the typed confirmation. It
cannot be used alone: the command refuses `--yes` without `--budget`, with "a
yes with no ceiling is a blank cheque", so an approved run always carries a
hard ceiling. A run that went ahead on `--yes` is recorded as `waived` in the
run manifest **with the argv**, because six months later nobody remembers
which runs a person confirmed at a keyboard.

`verdict.html`, the report page, contains **no `<script>` tag and no event
handler**. It opens from `file://` and executes nothing. Checked by counting
matches for `<script`, `onclick`, `javascript:`, `onerror` and `onload` in
`bundle.py`: zero.

## What is written into Knowledge Catalog

Only when you run `contextlift catalog-push`. This section was wrong in its first
draft -- it listed six fields when eleven are written -- and the correction is
kept visible because under-reporting what leaves is the one error a document
like this must not make.

**Why there is an allowlist at all**, in the code's own words: "any
catalogViewer can read this aspect and an agent can read it through MCP". A
field written here is not private to the person who ran the round.

Every field the code sets today (`Provenance`, `provenance.py`):

```
round_id            a label you choose, or push-YYYY-MM-DD
measured_at         a UTC timestamp
runner              which runner produced the answers, e.g. CA_API
model_alias         e.g. gemini-2.5-flash
arm_control_hash    a hash
arm_treatment_hash  a hash
overview_hash       a hash of the description already stored on the table
queries_hash        a hash of the question set
k_repeats           an integer, how many times each question was asked
served_state        one of a fixed set, e.g. NOT_CHECKED
gates               per gate: its name and its outcome
```

No questions, no SQL, no rows.

**Three controls on that payload**, and the third is the one worth knowing:

- `ALLOWED_KEYS` is an allowlist of 19 names. `payload()` drops anything not
  on it and anything empty.
- `MAX_STRING` is 80 characters. Anything longer is treated as prose, "and
  prose is how a question or a SQL fragment gets into a record any
  catalogViewer can read".
- `check_payload` **refuses** rather than truncating, and names the field:
  "a payload that needed truncating was built by code that does not
  understand its own inputs, and the next one will be wrong in a way nobody
  notices". The list check reaches inside `gates` too, because a gate's
  detail sentence quotes table and column names -- so the name and the
  outcome travel, not the sentence.

**What the allowlist permits but nothing sets today.** `ALLOWED_KEYS` also
names `table_lift_pp`, `table_ci_low_pp`, `table_ci_high_pp`, `regressions`,
`recall_misses`, `terms_hash`, `question_set_hash` and `schema_hash`. The
`Provenance` dataclass does not carry them, so no accuracy figure is written
at present -- but the allowlist is what bounds a future version, so read it
rather than this paragraph if the answer matters to you.

The write is read back and compared; a mismatch is reported per table rather
than assumed to have worked. `contextlift catalog-rollback` reverses it.

## The workspace is sensitive. Keep it out of git.

`--workspace DIR` records every run: the argv, hashes of the inputs, copies of
the outputs, and **stdout**. On a real estate that stdout holds your
questions, your SQL, your table names and a model's answers about your data.
This is deliberate — it is what makes a result auditable six months on — and
it means the directory deserves the same care as a data extract.

Two protections, and you should know the difference:

- `runs/` carries its own `.gitignore` holding `*`, written by
  `protect_runs_from_git` (`workspace.py`). Self-protecting, the same pattern
  uv and pip use for `.venv`.
- A **fresh** workspace also gets a root `.gitignore` (`WORKSPACE_GITIGNORE`)
  listing `runs/`, `arms/`, `cache/`, `*.sqlite`, `*.db`, `.chat_history`,
  `index.json` and `contextlift.json`.

**The gap, and why it is a warning rather than a fix.** That root file is only
written when none exists, and your own repository already has one. So
`contextlift --workspace .` inside a project leaves `index.json` and
`contextlift.json` committable. `git_exposure_warning` detects this and prints
the exact lines to add. It does not write them, because the workspace root may
be your repository and this tool has no business putting ignore rules there.
`index.json` is rebuilt from every run manifest and so carries full command
lines; `contextlift.json` carries the workspace directory's name.

## `contextlift share` — the only export, and how it is built

Two controls, in this order (`share.py`):

**An allowlist, not a blocklist.** The payload is assembled by naming every
field that may leave. Nothing is removed, because removal only protects
against the fields somebody thought of, and the field added next month is the
one that leaks. A key not named does not travel, whatever it holds.

**Then a scan of what was built**, because an allowlisted number can still
carry a name. `contextlift scan` checks a commit against patterns in
`_personal/secret_patterns.txt`; `make scan ALL=1` runs it over all history.

## Limits. What this does not do

Listed because a security document that claims no limits has not been written
honestly.

- **It does not sandbox the model service.** If your policy forbids table and
  column names reaching a model, that decision is about your agent, not about
  this tool, and this tool cannot evaluate an agent it may not talk to.
- **It does not encrypt the workspace.** It relies on your filesystem
  permissions.
- **`--yes` is a real bypass.** Bounded by `--budget`, recorded as waived with
  the argv, and it is still a bypass.
- **The read-only rail is a text check, not a database privilege.** It is
  strong and it is tested, but the durable control is a read-only role on the
  credential you hand it. Use both.
- **Query costs are real.** Gold SQL runs on your warehouse. `--max-bytes` and
  `--max-billed-bytes` cap a scan; `--estimate-only` dry-runs every statement
  and runs nothing.
- **Cost estimates for the agent are not yet exact.** The Conversational
  Analytics pricing page bills "data tokens" and does not define one, so
  `round` prints "unknown (no price yet)" rather than a made-up number, and
  tells you to pilot 20 questions and pass `--usd-per-call`.
- **No third-party audit.** Nobody outside this project has reviewed it.

## IAM, per command

Derived from the client calls each command makes. **Not yet verified against a
live estate for every row** — the rows marked `verified` were exercised on a
real run; the rest are derived from the code and should be treated as a
starting point rather than a guarantee. `contextlift doctor --explain` decodes a
refusal you actually hit, which is more reliable than this table.

| command | needs | verified |
|---|---|---|
| `demo` | nothing. No key, no cloud, no network | yes |
| `doctor` | nothing. It reports what is missing | yes |
| `gate`, `score`, `round` on `bq://` | `roles/bigquery.jobUser` on the billing project, `roles/bigquery.dataViewer` on every dataset read | yes |
| `log` | `roles/bigquery.resourceViewer` for `INFORMATION_SCHEMA.JOBS` | yes |
| `round` with the CA runner | `roles/geminidataanalytics.dataAgentUser`, plus the BigQuery roles above | yes |
| `catalog-pull`, `catalog-list` | `roles/dataplex.catalogViewer` | yes |
| `catalog-push`, `catalog-rollback` | `roles/dataplex.catalogEditor`, and `roles/dataplex.aspectTypeOwner` to create the aspect type once | derived |
| `sentinel` | as `catalog-push` plus the CA runner roles | derived |
| `eval-room` | `roles/bigquery.dataEditor` on the evaluation project **only** — it creates views there, never in your source project | derived |

A separate note, learned the hard way: a **VPC Service Controls** perimeter (a
boundary controlling where requests may come from) blocks calls that IAM
allows. The refusal names the perimeter, not the role, and no amount of
role-granting fixes it. `contextlift doctor --explain` says so when it sees one.
