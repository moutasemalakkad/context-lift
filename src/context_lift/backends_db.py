# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Pick a database by its `--db` value, and the two commands that read a
warehouse's metadata: `schema` and `log`.

The value is a file path (SQLite) or `bq://BILLING_PROJECT[/DATASET]`, where
DATASET may be `DATA_PROJECT.DATASET` for a dataset another project owns
(`bq://my-project/bigquery-public-data.thelook_ecommerce`). Jobs run and
are billed in the billing project; unqualified table names resolve in the
dataset. A metadata read (`schema`, `draft-context`, `profile`) can cover
several datasets at once (`datasets=`), or the datasets a table list names
(`tables=` with `dataset.table` entries, the file catalog-list writes).
Exports: parse_db_spec, is_bigquery, make_executor, describe_tables,
datasets_to_read, schema_text, shape_sql, query_log_sql, demand_sql,
write_query_log, write_demand. Pure stdlib;
the BigQuery client is imported inside BigQueryExecutor only.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass

from .backends import refuse_forbidden_project
from .executor import BigQueryExecutor, Executor, SQLiteExecutor

DEFAULT_MAX_BYTES = 1_000_000_000  # per query, on BigQuery; 1 GB is $0.006


@dataclass(frozen=True)
class DbSpec:
    kind: str                    # "sqlite", "duckdb" or "bigquery"
    spec: str                    # the --db value as typed
    project: str | None = None   # billing project (bigquery)
    dataset: str | None = None   # "data_project.dataset" (bigquery), or None


def parse_db_spec(spec: str) -> DbSpec:
    # A prefix, not a file extension: `.duckdb` is only a convention and a
    # DuckDB database may be called anything, so guessing from the name would
    # send some files to the wrong reader with no error.
    if spec.startswith("duckdb://"):
        return DbSpec("duckdb", spec)
    if spec.startswith("sqlite://"):
        # `duckdb://` and `bq://` were both prefixed and this one was not, so
        # `--db sqlite:///data.db` reached sqlite3 as a literal filename and
        # failed with "unable to open database file". `contextlift demo`
        # printed exactly that form as the next command to run, so a reader
        # who followed the demo's own advice hit it.
        #
        # Forgiving on slashes, because the three-versus-four convention is a
        # trap nobody should have to remember: whatever follows the prefix is
        # the path, absolute if it starts with a slash and relative if not.
        path = spec[len("sqlite://"):]
        # `sqlite:////abs/path` is the SQLAlchemy four-slash form and leaves a
        # doubled leading slash, which POSIX leaves implementation-defined.
        while path.startswith("//"):
            path = path[1:]
        return DbSpec("sqlite", path or spec)
    if not spec.startswith("bq://"):
        return DbSpec("sqlite", spec)
    project, _, dataset = spec[len("bq://"):].partition("/")
    if not project or "/" in dataset:
        raise ValueError(f"bad --db {spec!r}: expected bq://PROJECT[/DATASET] "
                         "or bq://PROJECT/DATA_PROJECT.DATASET")
    if dataset and "." not in dataset:
        dataset = f"{project}.{dataset}"
    return DbSpec("bigquery", spec, project, dataset or None)


def is_bigquery(spec: str | None) -> bool:
    return bool(spec) and spec.startswith("bq://")


def make_executor(spec: str, timeout_seconds: float | None = None,
                  row_cap: int | None = None,
                  maximum_bytes_billed: int = DEFAULT_MAX_BYTES,
                  location: str | None = None,
                  use_query_cache: bool = True,
                  refuse_on_dry_run: bool = True) -> Executor:
    """SQLiteExecutor for a path, BigQueryExecutor for a bq:// spec.
    `use_query_cache` is BigQuery's result cache; SQLite has none.
    `refuse_on_dry_run` off leaves the byte cap to BigQuery alone (see
    BigQueryExecutor); SQLite has no cap."""
    parsed = parse_db_spec(spec)
    if parsed.kind == "duckdb":
        from .executor import DuckDBExecutor
        # `spec` stays the value as typed, which is what the field means,
        # so the prefix comes off here rather than being lost at parse time.
        return DuckDBExecutor(parsed.spec[len("duckdb://"):], row_cap=row_cap,
                              timeout_seconds=timeout_seconds)
    if parsed.kind == "sqlite":
        return SQLiteExecutor(spec, row_cap=row_cap, timeout_seconds=timeout_seconds)
    return BigQueryExecutor(
        project=refuse_forbidden_project(parsed.project, "BigQuery jobs"),
        dataset=parsed.dataset, location=location,
        maximum_bytes_billed=maximum_bytes_billed, row_cap=row_cap,
        timeout_seconds=timeout_seconds, use_query_cache=use_query_cache,
        refuse_on_dry_run=refuse_on_dry_run,
    )


# --- schema ------------------------------------------------------------------

@dataclass(frozen=True)
class ColumnInfo:
    name: str
    data_type: str
    description: str | None = None


@dataclass(frozen=True)
class TableInfo:
    """One table as the schema knows it.

    `qualified` is the name the model must spell in SQL: bare on SQLite, and on
    BigQuery `dataset.table` when the billing project owns the dataset, else
    `project.dataset.table` in backticks (a bare name would resolve in the
    wrong project). `dataset` is the id without the project, the key
    DirectoryContextSource and a routing catalog use.
    """
    name: str
    qualified: str
    columns: tuple[ColumnInfo, ...]
    dataset: str | None = None
    description: str | None = None


def describe_tables(executor: Executor, tables: list[str] | None = None,
                    datasets: list[str] | None = None) -> list[TableInfo]:
    """The tables and columns the schema holds, with whatever descriptions it has.

    One reader for both `schema` (renders CREATE TABLE text) and `draft-context`
    (renders one document per table), so the two cannot drift. SQLite carries
    no descriptions; BigQuery's come from INFORMATION_SCHEMA, and a missing
    view or a denied read costs the descriptions, never the columns.

    On BigQuery the datasets are read in the order given, into one list (see
    `datasets_to_read`); an estate of 27 datasets used to take 27 runs and a
    hand-made concatenation.
    """
    wanted = set(tables or [])
    if getattr(executor, "kind", "sqlite") != "bigquery":
        return _sqlite_tables(executor, wanted)
    found: list[TableInfo] = []
    for dataset in datasets_to_read(executor, tables, datasets):
        found += _bigquery_tables(executor, _wanted_in(wanted, dataset), dataset)
    return found


def _sqlite_tables(executor, wanted: set[str]) -> list[TableInfo]:
    names = executor.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")
    if not names.ok:
        raise RuntimeError(f"could not read sqlite_master: {names.error}")
    found = []
    for row in names.rows:
        if wanted and row["name"] not in wanted:
            continue
        # pragma_table_info is a SELECT, so it passes the read-only rail
        quoted = row["name"].replace("'", "''")
        columns = executor.execute(
            f"SELECT name, type FROM pragma_table_info('{quoted}') ORDER BY cid")
        if not columns.ok:
            raise RuntimeError(f"could not read the columns of {row['name']}: {columns.error}")
        found.append(TableInfo(
            name=row["name"], qualified=row["name"],
            columns=tuple(ColumnInfo(c["name"], c["type"]) for c in columns.rows)))
    return found


def _wanted_in(wanted: set[str], dataset: str) -> set[str]:
    """The `--tables` names that belong to `dataset` (`data_project.dataset`),
    spelled bare as INFORMATION_SCHEMA spells them. A bare name matches
    every dataset read; `dataset.table` (the shape catalog-list writes) and
    `project.dataset.table` match only their own."""
    dataset_id = dataset.rpartition(".")[2]
    bare = set()
    for name in wanted:
        prefix, _, table = name.rpartition(".")
        if not prefix or prefix in (dataset_id, dataset):
            bare.add(table)
    return bare


NEEDS_DATASET = ("{command} on BigQuery needs a dataset: --db bq://PROJECT/DATASET, "
                 "--datasets A,B, or --tables-file FILE (one dataset.table per line)")


def _datasets_for(executor, command: str, datasets: list[str] | None) -> list[str]:
    """The datasets a metadata command reads, each as `data_project.dataset`,
    in the order given. A bare name in `datasets` is the billing project's,
    unless it is the id of the dataset in `--db`, which may belong to another
    project (a tables file names datasets by id only). With no `datasets`,
    the one in `--db`; with neither, a one-line refusal that names the three
    ways to give one."""
    if datasets:
        project = getattr(executor, "project", None) or ""
        own = getattr(executor, "dataset", None) or ""
        found: list[str] = []
        for name in datasets:
            if "." in name:
                full = name
            elif name == own.rpartition(".")[2]:
                full = own
            else:
                full = f"{project}.{name}"
            if full not in found:
                found.append(full)
        return found
    dataset = getattr(executor, "dataset", None)
    if not dataset:
        raise RuntimeError(NEEDS_DATASET.format(command=command))
    return [dataset]


def datasets_to_read(executor, tables: list[str] | None = None,
                     datasets: list[str] | None = None, command: str = "schema") -> list[str]:
    """The datasets a read over `tables` and `datasets` touches, in order:
    `_datasets_for`, minus any dataset none of the wanted tables is in. The
    CLI prints its length before the first metadata query."""
    wanted = set(tables or [])
    return [d for d in _datasets_for(executor, command, datasets)
            if not wanted or _wanted_in(wanted, d)]


def _bigquery_tables(executor, wanted: set[str], dataset: str) -> list[TableInfo]:
    """One dataset's tables out of its three INFORMATION_SCHEMA views;
    `wanted` is bare table names, empty for all of them."""
    only = ""
    if wanted:
        names = ", ".join("'" + t.replace("'", "\\'") + "'" for t in sorted(wanted))
        only = f" WHERE table_name IN ({names})"
    columns = executor.execute(
        f"SELECT table_name, column_name, data_type "
        f"FROM `{dataset}.INFORMATION_SCHEMA.COLUMNS`{only} "
        f"ORDER BY table_name, ordinal_position")
    if not columns.ok:
        raise RuntimeError(f"could not read INFORMATION_SCHEMA.COLUMNS: {columns.error}")
    described = executor.execute(
        f"SELECT table_name, column_name, description "
        f"FROM `{dataset}.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS` "
        f"WHERE field_path = column_name AND description IS NOT NULL"
        + (only.replace(" WHERE ", " AND ") if only else ""))
    descriptions = {(r["table_name"], r["column_name"]): " ".join(str(r["description"]).split())
                    for r in (described.rows if described.ok else [])}
    # the table's own description lives in TABLE_OPTIONS as a SQL string
    # literal: "..." or a triple-quoted one when it spans lines
    options = executor.execute(
        f"SELECT table_name, option_value "
        f"FROM `{dataset}.INFORMATION_SCHEMA.TABLE_OPTIONS` "
        f"WHERE option_name = 'description'"
        + (only.replace(" WHERE ", " AND ") if only else ""))
    table_descriptions = {r["table_name"]: _unquote_option(r["option_value"])
                          for r in (options.rows if options.ok else [])}
    # `dataset.table` resolves in the billing project, so a dataset another
    # project owns must be named in full or the model's SQL points at the
    # wrong project
    data_project, _, dataset_id = dataset.partition(".")
    prefix = dataset_id if data_project == executor.project else f"`{dataset}"
    by_table: dict[str, list[ColumnInfo]] = {}
    for row in columns.rows:
        by_table.setdefault(row["table_name"], []).append(ColumnInfo(
            row["column_name"], row["data_type"],
            descriptions.get((row["table_name"], row["column_name"]))))
    return [
        TableInfo(name=table,
                  qualified=f"{prefix}.{table}`" if prefix.startswith("`") else f"{prefix}.{table}",
                  columns=tuple(cols), dataset=dataset_id,
                  description=table_descriptions.get(table))
        for table, cols in by_table.items()
    ]


def _unquote_option(value) -> str:
    """A TABLE_OPTIONS value is a SQL string literal: outer quotes, inner
    quotes and backslashes escaped. Stripping the outer pair alone left the
    two characters backslash-quote in 8 places of one live document; the
    escapes go too."""
    text = str(value).strip()
    for quote in ('"""', "'''", '"', "'"):
        if len(text) >= 2 * len(quote) and text.startswith(quote) and text.endswith(quote):
            text = text[len(quote):-len(quote)]
            break
    return re.sub(r'\\(["\'\\n])', lambda m: "\n" if m.group(1) == "n" else m.group(1), text).strip()


def schema_text(executor: Executor, tables: list[str] | None = None,
                datasets: list[str] | None = None) -> str:
    """The schema text the model is given: CREATE TABLE statements.

    SQLite returns its own DDL from sqlite_master. BigQuery has no DDL to
    read back, so one is rendered from INFORMATION_SCHEMA.COLUMNS with the
    column descriptions from COLUMN_FIELD_PATHS as trailing comments; both
    views are dataset-qualified (`project.dataset.INFORMATION_SCHEMA.X`,
    https://docs.cloud.google.com/bigquery/docs/information-schema-columns),
    so several datasets are several reads, joined here into one text.
    """
    wanted = set(tables or [])
    if getattr(executor, "kind", "sqlite") != "bigquery":
        found = executor.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")
        if not found.ok:
            raise RuntimeError(f"could not read sqlite_master: {found.error}")
        return "".join(f"{row['sql']};\n" for row in found.rows
                       if not wanted or row["name"] in wanted)
    out = []
    for table in describe_tables(executor, tables, datasets):
        lines = [f"  {c.name} {c.data_type}" + (f",  -- {c.description}" if c.description else ",")
                 for c in table.columns]
        lines[-1] = lines[-1].replace(",  --", "  --") if "  --" in lines[-1] \
            else lines[-1].rstrip(",")
        out.append(f"CREATE TABLE {table.qualified} (\n" + "\n".join(lines) + "\n);\n")
    return "".join(out)


# --- query log ---------------------------------------------------------------

def shape_sql(column: str) -> str:
    """The SQL expression for a query's shape: a hash of the text with string
    literals and numbers masked, whitespace collapsed and case folded, the
    same steps as `mine.fingerprint` minus comment stripping (a commented
    copy is its own shape: coarser than mine's, never finer).

    Grouping on the raw text counted the same report with a different date
    as a new shape each time, so `--limit N` filled up with one report's
    parameters and other queries fell off the end.
    """
    masked_strings = f"REGEXP_REPLACE({column}, r\"'[^']*'\", \"'?'\")"
    masked_numbers = f"REGEXP_REPLACE({masked_strings}, r'\\b\\d+(?:\\.\\d+)?\\b', '?')"
    one_space = f"REGEXP_REPLACE({masked_numbers}, r'\\s+', ' ')"
    return f"TO_HEX(MD5(LOWER(TRIM({one_space}))))"


# A service account's address always ends this way
# (svc-dash@PROJECT.iam.gserviceaccount.com). Dashboards, schedulers and
# pipelines run as one, and they run constantly: on one estate over 30 days,
# 7 service accounts ran 169,789 of 184,301 jobs (92%) against 14,512 by 82
# people. Under `--limit 3000` they held 2,592 of the 3,000 shapes, so the
# top-N cut was eaten before a person's question reached it, and the golds
# mined from dashboard SQL were shapes no context can match.
SERVICE_ACCOUNT = "ENDS_WITH(LOWER(user_email), 'gserviceaccount.com')"


def _quoted_list(values: list[str]) -> str:
    """A SQL IN list from lower-cased, deduped, escaped strings."""
    seen = list(dict.fromkeys(v.strip().lower() for v in values if v.strip()))
    return ", ".join("'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'" for v in seen)


def _label_pairs(labels: list[str] | None) -> list[tuple[str, str]]:
    """`["team=sales", "env"]` as [("team", "sales"), ("env", "")].

    A bare key means "carries this label whatever its value", which is how
    the Conversational Analytics API's own `ca-bq-job` label is used: it
    marks the job and the value carries nothing anybody filters on.
    """
    pairs = []
    for item in labels or []:
        text = item.strip()
        if not text:
            continue
        key, _sep, value = text.partition("=")
        pairs.append((key.strip().lower(), value.strip().lower()))
    return pairs


def _jobs_source(project: str, region: str, days: int,
                 exclude_accounts: list[str] | None = None,
                 include_service_accounts: bool = False,
                 only_accounts: list[str] | None = None,
                 labels: list[str] | None = None) -> str:
    """The FROM and WHERE lines both log statements share: the JOBS view,
    the window, the noise both leave out, and the accounts to drop.

    Region qualifier and columns per
    https://docs.cloud.google.com/bigquery/docs/information-schema-jobs:
    `query STRING` is the text, `user_email` who ran it, `creation_time` the
    partitioning column (filter on it so partitions prune). Needs
    bigquery.jobs.listAll on the project.

    Service accounts are dropped here with the rest of the noise, before any
    ranking or cut (see SERVICE_ACCOUNT); `include_service_accounts` keeps
    them. `log --demand` counts them anyway, from a window that has the
    filter off.

    `exclude_accounts` is applied here, before any hashing, so an
    engineer's own eval traffic never reaches the CSV. `mine
    --exclude-accounts` could drop it later, but under --hash-accounts the
    engineer cannot even see which rows are theirs. Matched lower-cased,
    the way mine matches.
    """
    account_filter = ""
    excluded = _quoted_list(exclude_accounts or [])
    if excluded:
        account_filter += f"  AND LOWER(user_email) NOT IN ({excluded})\n"
    # The inverse, for a gold set about ONE team. A team's own queries make a
    # gold set about that team's questions, which is the only way to ask "did
    # the sales rules help the sales team" rather than "did they help on
    # average". Applied here, with the exclusions, so the cut happens before
    # any ranking and before any hashing.
    kept = _quoted_list(only_accounts or [])
    if kept:
        account_filter += f"  AND LOWER(user_email) IN ({kept})\n"
    # BigQuery job labels. dbt, Looker and the Conversational Analytics API
    # all label their own jobs, and a team that labels its scheduled work can
    # select it here without naming a single person. A bare key matches the
    # label whatever its value.
    for key, value in _label_pairs(labels):
        safe_key = key.replace("\\", "\\\\").replace("'", "\\'")
        test = f"l.key = '{safe_key}'"
        if value:
            safe_value = value.replace("\\", "\\\\").replace("'", "\\'")
            test += f" AND LOWER(l.value) = '{safe_value}'"
        account_filter += ("  AND EXISTS (SELECT 1 FROM UNNEST(labels) AS l "
                           f"WHERE {test})\n")
    return (
        f"FROM `{project}`.`region-{region}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT\n"
        f"WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(days)} DAY)\n"
        "  AND job_type = 'QUERY' AND state = 'DONE' AND error_result IS NULL\n"
        "  AND LENGTH(query) < 3000\n"
        # --only-accounts names exactly who is wanted, so the blanket
        # service-account exclusion steps aside: asking for one service
        # account by name and getting nothing back is the kind of silence
        # nobody debugs. The named list is the filter.
        + ("" if (include_service_accounts or only_accounts)
           else f"  AND NOT {SERVICE_ACCOUNT}\n")
        + account_filter
    )


def dataset_names(datasets: list[str] | None) -> list[str]:
    """Bare dataset names, deduped, order kept: `pub.shop` and `shop` are
    the same filter, because referenced_tables carries dataset_id alone."""
    return list(dict.fromkeys(d.split(".")[-1].replace("`", "") for d in (datasets or []) if d))


# A query naming at least two distinct tables, as BigQuery can count them
# from the text. `referenced_tables` would be exact but a cached job carries
# an empty one, which is the same reason `datasets` checks the text too.
_TABLES_IN_TEXT = r"ARRAY_LENGTH(REGEXP_EXTRACT_ALL(query, r'`[\w-]+\.[\w$]+\.[\w$]+`'))"

# Aggregation, as the text shows it. GROUP BY alone misses `SELECT COUNT(*)
# FROM t` with no grouping, which is a question, so the aggregate functions
# are checked as well.
_AGGREGATES = (r"REGEXP_CONTAINS(UPPER(query), r'\bGROUP BY\b')"
               r" OR REGEXP_CONTAINS(UPPER(query), "
               r"r'\b(SUM|AVG|COUNT|MIN|MAX|COUNTIF|APPROX_COUNT_DISTINCT)\s*\(')")

# A tool's own JSON header. dbt writes "app": "dbt", Looker writes
# "app": "looker", GitHub Actions writes deployment-method. They run under a
# person's credentials, so a service-account filter never sees them: 80,904
# of 1,205,904 queries on one estate.
_MACHINE_SQL = (
    # The double quotes need no backslash: the SQL string is delimited by
    # single quotes, and inside a BigQuery raw string a backslash-quote stays
    # a backslash-quote, so r'\"app\"' would never match "app".
    "REGEXP_CONTAINS(query, r'\"app\"\\s*:\\s*\"(dbt|looker)\"')"
    " OR REGEXP_CONTAINS(query, r'dbt_version|deployment-method"
    "|deployment-environment|github-actions|\"profile_name\"')")


def query_log_sql(project: str, days: int, region: str, limit: int,
                  datasets: list[str] | None = None, hash_accounts: bool = False,
                  exclude_accounts: list[str] | None = None,
                  include_service_accounts: bool = False,
                  min_tables: int = 1, require_analytical: bool = False,
                  drop_machine_sql: bool = False,
                  only_accounts: list[str] | None = None,
                  labels: list[str] | None = None) -> str:
    """The JOBS_BY_PROJECT query `mine` reads: one row per query shape with
    sql, times_seen, ran_by, distinct_users.

    `project` is where the jobs RAN: a job is listed in the project that ran
    and billed it, not in the project whose tables it read.

    A shape is `shape_sql`: literals masked, whitespace collapsed, case
    folded. `sql` is the shortest text in the shape, so a second pull gives
    mine the same text and the same ids (ANY_VALUE would not). Ranked by
    times_seen, then distinct_users, and service accounts are out of the
    ranking before the cut unless `include_service_accounts` (see
    SERVICE_ACCOUNT). The ranking is a heuristic: one person
    running an ad-hoc query is often the question wanted, and many-user
    shapes are rare where dashboards run as service accounts. On one estate
    (30 days, 310,795 jobs) distinct_users came first and sorted nothing:
    38,894 shapes had one user, 73 had two or three, none had four.

    `ran_by` is EVERY account that ran the shape, sorted and joined by ';':
    personal data, in the CSV and in the workspace copy of it. ANY_VALUE
    picked one of them at random, so `mine --exclude-accounts` dropped a
    shape only when the pick happened to be the excluded account (76 of
    3,000 shapes on one estate had two accounts). `hash_accounts` stores
    TO_HEX(MD5(LOWER(user_email))) for each one instead; `mine
    --exclude-accounts` still takes plain addresses and matches the same
    hash itself. Nothing else in the SQL changes. `exclude_accounts` drops
    those accounts' jobs in SQL, before the hash (see `_jobs_source`).

    `datasets` keeps only jobs that touched one of those datasets: one IN
    list and one regex alternation, however many there are. Two tests,
    because a cached job carries an empty `referenced_tables` (measured on a
    project log: 147 of 253 jobs on one dataset were cache hits, none with a
    referenced table), so the dataset name in the query text is checked too.
    """
    dataset_filter = ""
    names = dataset_names(datasets)
    if names:
        quoted = ", ".join(f"'{n}'" for n in names)
        alternation = "|".join(re.escape(n) for n in names)
        dataset_filter = (
            "  AND (EXISTS (SELECT 1 FROM UNNEST(referenced_tables) AS t "
            f"WHERE t.dataset_id IN ({quoted}))\n"
            f"       OR REGEXP_CONTAINS(query, r'\\b({alternation})\\b'))\n"
        )
    # The kind filters, in SQL rather than after the export. `mine` has all
    # three, and having them only there is one stage too late: the export
    # keeps the top N shapes BY TRAFFIC, and a dashboard runs hundreds of
    # times for one viewer. On one estate 29,684 of 1,205,904 queries joined
    # two tables and aggregated -- 2.5% -- so a top-2,000 pull held about
    # fifty of them and mine's own --min-tables then cut 446 candidates down
    # to 2. Filtering here makes the same 2,000 rows 2,000 usable ones, and
    # costs nothing: the view is scanned either way.
    kind_filter = ""
    if min_tables > 1:
        kind_filter += f"  AND {_TABLES_IN_TEXT} >= {int(min_tables)}\n"
    if require_analytical:
        kind_filter += f"  AND ({_AGGREGATES})\n"
    if drop_machine_sql:
        kind_filter += f"  AND NOT ({_MACHINE_SQL})\n"
    account = "TO_HEX(MD5(LOWER(user_email)))" if hash_accounts else "user_email"
    ran_by = f"STRING_AGG(DISTINCT {account}, ';' ORDER BY {account}) AS ran_by"
    return (
        "SELECT\n"
        "  ARRAY_AGG(query ORDER BY LENGTH(query), query LIMIT 1)[OFFSET(0)] AS sql,\n"
        "  COUNT(*)                      AS times_seen,\n"
        f"  {ran_by},\n"
        "  COUNT(DISTINCT user_email)    AS distinct_users\n"
        + _jobs_source(project, region, days, exclude_accounts,
                       include_service_accounts=include_service_accounts,
                       only_accounts=only_accounts, labels=labels)
        + dataset_filter + kind_filter +
        f"GROUP BY {shape_sql('query')}\n"
        "ORDER BY times_seen DESC, distinct_users DESC\n"
        f"LIMIT {int(limit)}"
    )


def demand_sql(project: str, days: int, region: str, datasets: list[str] | None = None,
               exclude_accounts: list[str] | None = None,
               include_service_accounts: bool = False,
               only_accounts: list[str] | None = None,
               labels: list[str] | None = None) -> str:
    """One row per dataset the window's jobs referenced: dataset, jobs,
    distinct_shapes, distinct_accounts, bytes_billed_total,
    top_account_share; most distinct accounts first, then most jobs. Same
    view, window, noise exclusions and account filter as `query_log_sql`,
    so the counts are over the jobs mine would see; the shape is
    `shape_sql` too.

    Ordered by accounts, not jobs, because job counts do not separate
    demand from repetition: on one estate three personal accounts ran
    15,488 jobs each in 30 days (a scheduled pipeline under personal
    credentials, invisible to a service-account filter) and by jobs they
    held four of the top eight slots. `top_account_share` is the share of
    the dataset's jobs its single busiest account ran, from a per-account
    count; the account itself is never selected.

    A job is in one row per dataset it touched, ONCE per dataset however
    many of its tables it read: `touched` is DISTINCT over (job, dataset),
    so a job that read three tables of one dataset is one row. The `jobs`
    column therefore sums above the window's job count (a two-dataset job
    is in two rows) while the window totals count each job once. Counting
    referenced tables instead gave 24,245 where the job count was 14,512.
    A cached job carries no referenced_tables and lands in no row; the
    query-text fallback `query_log_sql` uses for those cannot name a
    dataset it did not match, so it is not used here.

    Four window totals ride on every row, so one statement answers the
    share question without a second pull: `all_jobs` (every job in the
    window that passed the filters), `service_jobs` (jobs run by service
    accounts, counted even when the filter drops them), `attributed_jobs`
    (distinct jobs in the rows, after the dataset filter) and
    `unattributed_jobs` (no referenced table). A dataset's share is jobs /
    all_jobs; the window held all_jobs + service_jobs jobs by default.
    """
    names = dataset_names(datasets)
    dataset_filter = ""
    if names:
        quoted = ", ".join(f"'{n}'" for n in names)
        dataset_filter = f"  WHERE t.dataset_id IN ({quoted})\n"
    # the window is read once with service accounts in it, so they can be
    # counted even when the rows leave them out
    service_filter = "" if include_service_accounts else "\n  WHERE NOT is_service_account"
    return (
        "WITH window_jobs AS (\n"
        "  SELECT job_id, user_email, total_bytes_billed, referenced_tables,\n"
        f"         {shape_sql('query')} AS shape,\n"
        f"         {SERVICE_ACCOUNT} AS is_service_account\n"
        + _jobs_source(project, region, days, exclude_accounts,
                       include_service_accounts=True,
                       only_accounts=only_accounts, labels=labels) +
        "),\n"
        f"jobs AS (\n  SELECT * FROM window_jobs{service_filter}\n),\n"
        "touched AS (\n"
        "  SELECT DISTINCT j.job_id, j.user_email, j.total_bytes_billed, j.shape,\n"
        "         CONCAT(t.project_id, '.', t.dataset_id) AS dataset\n"
        "  FROM jobs AS j, UNNEST(j.referenced_tables) AS t\n"
        + dataset_filter +
        "),\n"
        "per_account AS (\n"
        "  SELECT dataset, MAX(jobs) AS top_account_jobs\n"
        "  FROM (SELECT dataset, user_email, COUNT(*) AS jobs FROM touched GROUP BY 1, 2)\n"
        "  GROUP BY dataset\n"
        "),\n"
        "per_dataset AS (\n"
        "  SELECT\n"
        "    dataset,\n"
        "    COUNT(*)                           AS jobs,\n"
        "    COUNT(DISTINCT shape)              AS distinct_shapes,\n"
        "    COUNT(DISTINCT user_email)         AS distinct_accounts,\n"
        "    IFNULL(SUM(total_bytes_billed), 0) AS bytes_billed_total\n"
        "  FROM touched\n"
        "  GROUP BY dataset\n"
        ")\n"
        "SELECT\n"
        "  d.dataset, d.jobs, d.distinct_shapes, d.distinct_accounts, d.bytes_billed_total,\n"
        "  ROUND(SAFE_DIVIDE(a.top_account_jobs, d.jobs), 3) AS top_account_share,\n"
        "  (SELECT COUNT(*) FROM jobs)                  AS all_jobs,\n"
        "  (SELECT COUNTIF(is_service_account) FROM window_jobs) AS service_jobs,\n"
        "  (SELECT COUNT(DISTINCT job_id) FROM touched) AS attributed_jobs,\n"
        "  (SELECT COUNTIF(IFNULL(ARRAY_LENGTH(referenced_tables), 0) = 0) FROM jobs)"
        " AS unattributed_jobs\n"
        "FROM per_dataset AS d\n"
        "JOIN per_account AS a USING (dataset)\n"
        "ORDER BY d.distinct_accounts DESC, d.jobs DESC, d.dataset"
    )


QUERY_LOG_COLUMNS = ["sql", "times_seen", "ran_by", "distinct_users"]
DEMAND_COLUMNS = ["dataset", "jobs", "distinct_shapes", "distinct_accounts",
                  "bytes_billed_total", "jobs_per_account_day", "top_account_share"]


def _write_columns(rows: list[dict], path: str, columns: list[str]) -> int:
    """Only the named columns reach the file: the window totals demand_sql
    puts on every row are printed, not stored."""
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in columns})
    return len(rows)


def write_query_log(rows: list[dict], path: str) -> int:
    return _write_columns(rows, path, QUERY_LOG_COLUMNS)


def write_demand(rows: list[dict], path: str) -> int:
    return _write_columns(rows, path, DEMAND_COLUMNS)
