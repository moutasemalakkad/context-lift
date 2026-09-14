# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Database backends behind one interface.

The harness, gate, and judge only ever need "run this SQL, give me rows".
Where the rows come from — SQLite for examples/tests, BigQuery in production —
is hidden behind the Executor protocol, so the core stays pure stdlib and the
cloud backend lives behind an optional extra.

Safety rail shared by every backend: predicted SQL is untrusted model output,
so a statement that isn't read-only is rejected before it ever reaches the
database. The same check rejects a call that leaves the engine (EXTERNAL_QUERY,
the AI.* functions, ML.GENERATE_* and the other remote ML functions): a
federated query or a model call can write elsewhere and bill elsewhere, and
its argument is a string the rail does not read. A remote UDF call, or
ML.PREDICT over a remote model, cannot be told from a native one by text; the
control for those is IAM, an account without bigquery.connections.use. For
BigQuery the guard rides along with a dry-run that enforces
maximum_bytes_billed. `--db bq://...` picks this backend through
`backends_db.make_executor`.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from . import identity


@dataclass(frozen=True)
class ExecResult:
    """Outcome of running one SQL statement.

    `error is None` means the statement ran. An empty `rows` with no error is
    a real zero-row result (a signal the gate cares about), which is why
    "ran but empty" and "failed to run" are kept distinct rather than both
    collapsing to []. `truncated` is True when a row cap cut the result off.
    """
    rows: list[dict] = field(default_factory=list)  # comparator Row shape
    error: str | None = None
    truncated: bool = False

    @property
    def ok(self) -> bool:
        """True if the statement executed (even if it returned zero rows)."""
        return self.error is None


def apply_row_cap(rows: list, cap: int | None) -> tuple[list, bool]:
    """Trim a result to the cap and say whether it was cut.

    One helper because there are two backends and this rule was written twice,
    once for SQLite and once for BigQuery. They agreed today; nothing made them
    agree tomorrow, and a rule about truncation that drifts is how a truncated
    result gets compared as if it were whole.

    The caller fetches `cap + 1` rows. That extra row is the entire signal: at
    exactly `cap` rows there is no way to tell a complete result from one cut
    at the boundary.
    """
    if cap is None:
        return list(rows), False
    truncated = len(rows) > cap
    return list(rows[:cap]), truncated


@runtime_checkable
class Executor(Protocol):
    """Anything that can run a SQL string and return an ExecResult.

    Structural: a class satisfies this by having a matching execute method,
    with no import of or inheritance from this module — so the BigQuery
    backend in the extra and any user-supplied backend both qualify.
    """

    def execute(self, sql: str) -> ExecResult:
        ...


# Statements that modify data or schema. A read-only query must not begin with
# these, and (to block data-modifying CTEs like WITH t AS (DELETE ...)) must
# not contain them anywhere. The trade-off: a SELECT with the literal word
# DROP inside a string is conservatively rejected. For untrusted model output
# that's the safe direction.
#
# REPLACE and TRUNCATE are also scalar functions: REPLACE(s, 'a', 'b') and
# TRUNCATE(x, 2) read a value, TRUNCATE TABLE t and REPLACE INTO t write. The
# opening parenthesis tells them apart, so the word followed by "(" is let
# through and the bare word is not.
_WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|MERGE|"
    r"GRANT|REVOKE|ATTACH|DETACH|VACUUM|PRAGMA|"
    r"(?:TRUNCATE|REPLACE)(?!\s*\())\b",
    re.IGNORECASE,
)
# Calls that leave the engine. EXTERNAL_QUERY runs its string argument on
# another database; AI.* and the remote ML functions send data to a model
# and bill the model project. Matched on the masked text, so the name in a
# string or a comment is inert and a column called external_query_count is
# not a call (no "(" follows it).
_OUTSIDE_CALL = re.compile(
    r"\b(EXTERNAL_QUERY|AI\.[A-Z_]+|ML\.(?:GENERATE_[A-Z_]+|UNDERSTAND_TEXT|TRANSLATE|"
    r"PROCESS_DOCUMENT|TRANSCRIBE|ANNOTATE_IMAGE))\s*\(",
    re.IGNORECASE,
)
# A SELECT that reads the FILESYSTEM rather than the database. DuckDB ships
# these as ordinary table functions, so `SELECT content FROM
# read_text('/etc/passwd')` starts with SELECT, names no write keyword, and
# passed every check here until 2026-09-14. Verified: with
# `duckdb.connect(db, read_only=True)`, that query returns the file.
#
# WHY THIS MATTERS HERE SPECIFICALLY. This tool executes SQL a MODEL wrote,
# from a prompt built out of business context that people author and that
# `mine` also reads out of a query log. A sentence in that context is an
# untrusted input with a path to the filesystem behind it, and the result
# lands in a CSV of answers.
#
# `read_only=True` is not the control people assume: it protects the database
# FILE from writes and says nothing about reads elsewhere on disk.
_FILE_READ = re.compile(
    r"\b(read_text|read_blob|read_csv|read_csv_auto|read_json|read_json_auto|"
    r"read_json_objects|read_ndjson|read_ndjson_auto|read_parquet|read_xlsx|"
    r"parquet_scan|csv_scan|sniff_csv|delta_scan|iceberg_scan)\s*\(",
    re.IGNORECASE,
)
# NOT in that list, on purpose: `glob`. In DuckDB `glob('/etc/*')` lists the
# filesystem, but in SQLite `glob(X, Y)` is the function form of the GLOB
# pattern operator -- `SELECT glob('a*', name)` is ordinary string matching
# and returns 1. This regex is shared by every backend, so blocking the name
# here would refuse valid SQLite golds to close a DuckDB hole. DuckDB's own
# `disabled_filesystems` setting in DuckDBExecutor._connect blocks it where it
# is actually dangerous, which is the right layer for a backend-specific risk.
NOT_READ_ONLY = "rejected: statement is not read-only"
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
# String literals: standard single-quoted (with '' escapes) and double-quoted,
# which SQLite accepts as string fallback and real gold sets use.
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'|\"[^\"]*\"")


def strip_comments(sql: str) -> str:
    """Remove -- line and /* */ block comments, returning the bare SQL text.

    Shared by the read-only guard and the gold gate: both need to reason about
    the actual statement, not commented-out text (a DROP in a comment is inert,
    a gold that is *only* a comment is an empty stub).
    """
    return _BLOCK_COMMENT.sub(" ", _LINE_COMMENT.sub(" ", sql or ""))


def comment_text(sql: str) -> str:
    """What `strip_comments` removes, joined into one line.

    A commented-out WHERE line is invisible to every check that reads the
    stripped SQL, and a model handed the raw text reads it as live: two
    mined candidates had a disabled filter and their drafted questions
    named it.
    """
    comments = _LINE_COMMENT.findall(sql or "") + _BLOCK_COMMENT.findall(sql or "")
    return " ".join(" ".join(comments).split())


def mask_string_literals(sql: str) -> str:
    """Every string literal replaced by '?', so a keyword, a comma or a
    semicolon inside quotes is inert text.

    The read-only guard needs it (a gold filtering on "done; closed" read as
    two statements), and so does any check that reads the shape of a
    statement rather than its values.
    """
    return _STRING_LITERAL.sub("'?'", sql or "")


def strip_comments_and_mask_literals(sql: str) -> str:
    """Comments removed and string literals masked, read in one pass.

    Calling `strip_comments` and `mask_string_literals` one after the other is
    wrong in BOTH orders, because each is fooled by a character belonging to
    the other's construct:

        strip first, then mask
            `WHERE note = 'Admin -- reviewed' AND op = 'INSERT'`
            the comment rule eats from `--` to the end of the line, leaving an
            open quote that swallows the next line and exposes INSERT

        mask first, then strip
            `SELECT * FROM t  -- don't` / `WHERE x = 'a' AND y = 1`
            the apostrophe in `don't` opens a literal that closes on the real
            quote below it, and removing that comment takes the WHERE with it,
            leaving `SELECT * FROM t`

    The second shape is the dangerous one: it drops everything after the
    comment, which is exactly where a hidden write sits. So the rail reads the
    statement once here instead of composing the two helpers.

    An unterminated literal or identifier returns "", which every caller reads
    as "refuse this". The text cannot be read reliably, and a rail that cannot
    read a statement must not pass it.
    """
    text = sql or ""
    end_of_text = len(text)
    out: list[str] = []
    at = 0
    while at < end_of_text:
        char = text[at]
        if text.startswith("--", at):
            line_end = text.find("\n", at)
            if line_end < 0:
                break
            out.append(" ")
            at = line_end                       # the newline itself stays
        elif text.startswith("/*", at):
            close = text.find("*/", at + 2)
            if close < 0:
                break                           # never closed: comment to the end
            out.append(" ")
            at = close + 2
        elif char == "'":
            scan = at + 1
            while scan < end_of_text:
                if text[scan] != "'":
                    scan += 1
                elif text.startswith("''", scan):
                    scan += 2                   # '' is one quote inside the literal
                else:
                    break
            if scan >= end_of_text:
                return ""
            out.append("'?'")
            at = scan + 1
        elif char == '"':
            close = text.find('"', at + 1)
            if close < 0:
                return ""
            out.append("'?'")
            at = close + 1
        elif char == "`":
            # a quoted identifier, not a value: keep it, a table name is what
            # the caller wants to read
            close = text.find("`", at + 1)
            if close < 0:
                return ""
            out.append(text[at:close + 1])
            at = close + 1
        else:
            out.append(char)
            at += 1
    return "".join(out)


def read_only_violation(sql: str) -> str | None:
    """Why `sql` must not run, or None when it is one read-only statement.

    The reason is the error text an executor returns. A statement that does
    not start with SELECT/WITH, holds a second statement, or names a write
    keyword gets NOT_READ_ONLY; one that calls out of the engine gets
    "rejected: reaches outside the warehouse (NAME)".

    Comments are stripped first; a trailing semicolon is allowed but an
    internal one (multiple statements) is rejected, so "SELECT 1; DROP TABLE t"
    can't slip a write past the first-keyword check.

    String literals are masked before every check: a real gold with
    `WHERE outcome = "Investigation complete; no suspect identified"` was
    flagged NON_SELECT because the semicolon inside the quotes read as a
    second statement, and a DROP inside a string is inert text, not a write.
    The same masking is why EXTERNAL_QUERY is refused by name: the DELETE in
    `EXTERNAL_QUERY('conn', 'DELETE FROM t')` is a string here and a
    statement on the other database.
    """
    masked = strip_comments_and_mask_literals(sql).strip().rstrip(";").strip()
    if not masked:
        return NOT_READ_ONLY
    if ";" in masked:
        return NOT_READ_ONLY
    first = masked.split(None, 1)[0].upper()
    if first not in ("SELECT", "WITH"):
        return NOT_READ_ONLY
    outside = _OUTSIDE_CALL.search(masked)
    if outside:
        return f"rejected: reaches outside the warehouse ({outside.group(1).upper()})"
    reads_disk = _FILE_READ.search(masked)
    if reads_disk:
        return (f"rejected: reads the filesystem, not the database "
                f"({reads_disk.group(1).lower()}). A query that can open a "
                f"file can return its contents as an answer.")
    if _WRITE_KEYWORDS.search(masked):
        return NOT_READ_ONLY
    return None


def is_read_only(sql: str) -> bool:
    """True if `sql` is a single read-only statement (see read_only_violation)."""
    return read_only_violation(sql) is None


class SQLiteExecutor:
    """Executor over a stdlib sqlite3 database — the core/example/test backend.

    Connects per call (stateless, idempotent) and returns rows as dicts. Set
    row_cap to bound a result set; the executor fetches one extra row to tell
    whether the cap actually truncated. enforce_read_only rejects non-SELECT
    statements before touching the DB.

    timeout_seconds bounds how long one statement may RUN, which row_cap
    cannot do: row_cap limits how many rows are fetched, but a query whose
    ORDER BY forces a full sort does all its work before the first row exists.
    Predicted SQL is untrusted model output, and a read-only query that never
    finishes is the second way untrusted SQL hurts (the rail stops the first).
    Measured case: a valid, index-using, read-only join over a 649k-row table
    ran 20+ minutes; under a 60s timeout it scores PRED_ERROR and the run
    moves on.
    """

    kind = "sqlite"

    def __init__(
        self,
        database: str,
        row_cap: int | None = None,
        enforce_read_only: bool = True,
        timeout_seconds: float | None = None,
    ):
        self.database = database
        self.row_cap = row_cap
        self.enforce_read_only = enforce_read_only
        self.timeout_seconds = timeout_seconds

    def check(self, sql: str) -> str | None:
        """The error a run would raise, from the planner alone, or None.

        EXPLAIN QUERY PLAN parses and plans without executing: SQLite's free
        equivalent of a warehouse dry run. A draft that fails here never runs.
        """
        if self.enforce_read_only:
            reason = read_only_violation(sql)
            if reason:
                return reason
        con = sqlite3.connect(self.database)
        try:
            con.execute(f"EXPLAIN QUERY PLAN {sql}")
        except sqlite3.Error as e:
            return str(e)
        finally:
            con.close()
        return None

    def execute(self, sql: str) -> ExecResult:
        if self.enforce_read_only:
            reason = read_only_violation(sql)
            if reason:
                return ExecResult(error=reason)
        con = sqlite3.connect(self.database)
        con.row_factory = sqlite3.Row
        deadline = None
        if self.timeout_seconds is not None:
            deadline = time.monotonic() + self.timeout_seconds
            # Fires every N SQLite VM instructions; returning non-zero aborts
            # the statement, which surfaces as OperationalError("interrupted").
            con.set_progress_handler(
                lambda: 1 if time.monotonic() > deadline else 0, 2000
            )
        try:
            cur = con.execute(sql)
            fetched = (cur.fetchall() if self.row_cap is None
                       else cur.fetchmany(self.row_cap + 1))
            rows, truncated = apply_row_cap([dict(r) for r in fetched], self.row_cap)
            return ExecResult(rows=rows, truncated=truncated)
        except sqlite3.Error as e:
            if deadline is not None and time.monotonic() > deadline:
                return ExecResult(
                    error=f"timeout: exceeded {self.timeout_seconds:g}s"
                )
            # Wrap the driver error as a string so callers stay backend-agnostic
            # and a bad gold/pred query becomes EXEC_FAIL, never a crash.
            return ExecResult(error=f"{type(e).__name__}: {e}")
        finally:
            con.close()


class DuckDBExecutor:
    """Executor over DuckDB, the third backend and the cheapest to try.

    Why this one and not PostgreSQL first: DuckDB installs with pip in
    milliseconds, needs no server, and reads Parquet and CSV files directly,
    so a person with a folder of extracts can run a round without standing
    anything up. A PostgreSQL backend needs a running server to test against,
    which is a different cost.

    Deliberately the same shape as `SQLiteExecutor`: same read-only check,
    same row cap, same `ExecResult`. The two backends drifting apart is how a
    rule ends up written twice and one copy goes stale, which is how a
    truncated result was once compared as if it were whole.

    The timeout is the one real difference. SQLite aborts from a progress
    handler called on its own thread; DuckDB has `interrupt()`, which must be
    called from ANOTHER thread while the query runs. So a watchdog timer does
    it, and the timer is always cancelled in a finally: a timer left running
    would interrupt the NEXT query on that connection, which is a failure
    that looks like a flaky warehouse and is not.
    """

    kind = "duckdb"

    def __init__(
        self,
        database: str,
        row_cap: int | None = None,
        enforce_read_only: bool = True,
        timeout_seconds: float | None = None,
    ):
        self.database = database
        self.row_cap = row_cap
        self.enforce_read_only = enforce_read_only
        self.timeout_seconds = timeout_seconds

    def _connect(self):
        try:
            import duckdb
        except ImportError as missing:  # pragma: no cover - import guard
            raise SystemExit(
                "DuckDB is not installed. `pip install contextlift[duckdb]`, or "
                "`uv pip install duckdb`. It is an optional extra so the core "
                "stays dependency-free") from missing
        con = duckdb.connect(self.database, read_only=self.enforce_read_only)
        if self.enforce_read_only:
            # THE SECOND LAYER, and the one that is authoritative. The regex in
            # read_only_violation will always miss a function name; this makes
            # the engine refuse regardless of how the query is spelled.
            # Measured: normal queries against attached tables still work, and
            # read_text/read_csv raise PermissionException.
            try:
                con.execute("SET disabled_filesystems='LocalFileSystem'")
            except Exception:        # pragma: no cover - older DuckDB
                # An engine too old for the setting still has the regex above.
                # Failing the connection here would break a working install
                # over a defence in depth measure, so it degrades instead.
                pass
        return con

    def check(self, sql: str) -> str | None:
        """The error a run would raise, from the planner alone, or None.

        `EXPLAIN` parses and plans without executing, which is DuckDB's free
        equivalent of a warehouse dry run.
        """
        if self.enforce_read_only:
            reason = read_only_violation(sql)
            if reason:
                return reason
        connection = self._connect()
        try:
            connection.execute(f"EXPLAIN {sql}")
        except Exception as error:  # noqa: BLE001 - any driver error is the answer
            return f"{type(error).__name__}: {error}"
        finally:
            connection.close()
        return None

    def execute(self, sql: str) -> ExecResult:
        if self.enforce_read_only:
            reason = read_only_violation(sql)
            if reason:
                return ExecResult(error=reason)
        connection = self._connect()
        watchdog = None
        timed_out = False

        if self.timeout_seconds is not None:
            import threading

            def interrupt():
                nonlocal timed_out
                timed_out = True
                connection.interrupt()

            watchdog = threading.Timer(self.timeout_seconds, interrupt)
            watchdog.start()
        try:
            cursor = connection.execute(sql)
            names = [column[0] for column in cursor.description or []]
            fetched = (cursor.fetchall() if self.row_cap is None
                       else cursor.fetchmany(self.row_cap + 1))
            rows, truncated = apply_row_cap(
                [dict(zip(names, row)) for row in fetched], self.row_cap)
            return ExecResult(rows=rows, truncated=truncated)
        except Exception as error:  # noqa: BLE001 - a bad query is a result, not a crash
            if timed_out:
                return ExecResult(
                    error=f"timeout: exceeded {self.timeout_seconds:g}s")
            return ExecResult(error=f"{type(error).__name__}: {error}")
        finally:
            # Always, even on the happy path: a timer still pending would fire
            # into the NEXT query on this connection.
            if watchdog is not None:
                watchdog.cancel()
            connection.close()


# BigQuery on-demand price. Source: https://cloud.google.com/bigquery/pricing,
# read on the date below: $6.25 per TiB scanned, the first 1 TiB each month
# free, and a 10 MB minimum billed per query. The estimate ignores the free
# tier on purpose: it is the ceiling a run can cost, not a forecast of the
# invoice. When the page changes, change the two constants and the date.
BIGQUERY_USD_PER_TIB = 6.25
BIGQUERY_PRICE_AS_OF = "2026-09-03"
BIGQUERY_MIN_BYTES_PER_QUERY = 10 * 1024 * 1024
_TIB = 1 << 40


def bigquery_usd(n_bytes: int) -> float:
    """On-demand price of scanning `n_bytes`, at the dated rate above."""
    return n_bytes / _TIB * BIGQUERY_USD_PER_TIB


def not_found_in(error: str | None, project: str | None) -> bool:
    """True when `error` is BigQuery's "Not found: Table PROJECT:dataset.table
    was not found in location US" (or Dataset) for this project.

    A 2-part `dataset.table` resolves in the project the job runs in, the
    billing project, whatever `default_dataset` says; when the data lives in
    another project every such name fails this way, and the fix is a 3-part
    name, not a fix to the SQL."""
    if not error or not project:
        return False
    return re.search(r"Not found: (?:Table|Dataset) " + re.escape(project) + r":", error) is not None


# The warehouse refused the CALL, not the SQL. A 403 from VPC Service
# Controls stopped one statement of a 45-statement gate run while the other
# 41 went through, and it printed as "1 rejected at dry-run" -- the same
# words as a query that does not parse. Only this kind is answered by the
# team that owns the org policy.
_PLATFORM_REFUSAL = re.compile(
    r"VPC Service Controls|\bForbidden\b|\b403\b|Permission denied|Access Denied|"
    r"prohibited by (?:the )?organization|accessNotConfigured|does not have bigquery\.",
    re.IGNORECASE)


# One sentence for a refused call, wherever a command reports one. gate, the
# pre-pass every warehouse command runs, and log each used to say it their own
# way, and two of the three read as if the query were wrong.
PLATFORM_REFUSAL_ADVICE = ("ask whoever owns the org policy (VPC Service Controls answers "
                           "403); the query's author cannot fix this one")


def platform_refused(error: str | None) -> bool:
    """True when `error` is the platform turning the call away (a 403, an
    org policy, a missing permission), not the engine rejecting the SQL.

    The two need different people: a query that does not parse is the
    query's author's, a refusal is the security team's."""
    return bool(error) and _PLATFORM_REFUSAL.search(error) is not None


# A THIRD kind, and it cost an afternoon to see. The call never left: no
# credential could be minted for it. Nobody's SQL is wrong and the warehouse
# returned no 403, so both of the classes above send the reader somewhere
# useless.
#
# Measured 2026-09-12. A Google corp account is refused by two Google APIs
# with `access_denied: Account restricted`, raised while fetching the token.
# The BigQuery Python client surfaces it as `RefreshError`; `_PLATFORM_REFUSAL`
# does not match it, because that pattern has "Access Denied" with a space and
# this is `access_denied` with an underscore. So 285 of 286 statements in one
# `attribute` dry run printed as "the SQL does not parse", on SQL that had run
# correctly two days earlier.
#
# It is its own class because the fix is its own: not the query's author, not
# the security team, but whoever set up the caller -- or an account policy
# that no amount of project permission will move.
_CREDENTIAL_REFUSAL = re.compile(
    r"access_denied|Account restricted|RefreshError|invalid_grant|"
    r"invalid_token|Reauthentication is needed|"
    r"could not automatically determine credentials|"
    r"DefaultCredentialsError|Getting metadata from plugin failed",
    re.IGNORECASE)

CREDENTIAL_REFUSAL_ADVICE = (
    "the call never left: no credential could be minted for it. Nothing is "
    "wrong with the SQL and the warehouse returned no 403. Check which "
    "identity this process is using, and whether an account policy blocks "
    "the API for it")


def credential_refused(error: str | None) -> bool:
    """True when the caller could not authenticate at all.

    Checked BEFORE `platform_refused`, because a credential error can carry
    the word "denied" and reading it as a 403 sends the reader to a security
    team that has nothing to fix.
    """
    return bool(error) and _CREDENTIAL_REFUSAL.search(error) is not None


# The byte ceiling turning a query away, from `_over_budget_message` below.
# Matched on the text because the refusal travels as an ExecResult.error
# string through gate, score and the miner, and rebuilding a typed error at
# each of those would be three more places to keep in step.
_OVER_BUDGET = re.compile(r"^rejected: the query would scan more than ")

# BigQuery's own 404 for a table or dataset that is not there at all. Distinct
# from not_found_in, which is the 2-part-name mistake in ONE named project:
# this one fires whatever project was asked, because the table was dropped or
# renamed after the query was written. 24 of 446 mined candidates on one
# estate, and one of them stopped a 136-call round dead.
_TABLE_GONE = re.compile(r"Not found: (?:Table|Dataset) ", re.IGNORECASE)


_BYTE_UNITS = {"": 1, "B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12,
               "KIB": 2**10, "MIB": 2**20, "GIB": 2**30, "TIB": 2**40}

# BigQuery bills a 10 MB minimum per query, so a ceiling below that refuses
# every query there is. Any plain number under it is a units mistake, not a
# budget.
SMALLEST_USEFUL_CEILING = 10 * 10**6


def parse_bytes(text: str) -> int:
    """A byte ceiling written the way a person says it: 50GB, 500MB, 2TiB, or
    plain bytes.

    `--max-bytes 50` reads as fifty bytes, and every query on the estate is
    then refused before it runs. That happened, and the refusal printed the
    ceiling as "5e-08 GB", which reads like a real setting rather than a typo.
    Nobody has ever wanted a fifty-byte ceiling, so a plain number below
    BigQuery's own 10 MB billing minimum is refused with the number the
    author probably meant.
    """
    raw = str(text).strip().replace("_", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([A-Za-z]*)", raw)
    if not match:
        raise ValueError(f"{text!r} is not a byte size: write 50GB, 500MB, 2TiB, or a "
                         "plain number of bytes")
    number, unit = float(match.group(1)), match.group(2).upper()
    if unit not in _BYTE_UNITS:
        raise ValueError(f"{text!r}: unknown unit {match.group(2)!r}. Use "
                         "B, KB, MB, GB, TB, KiB, MiB, GiB or TiB")
    value = int(number * _BYTE_UNITS[unit])
    if not unit and value < SMALLEST_USEFUL_CEILING:
        raise ValueError(
            f"--max-bytes {raw} is {value:,} bytes, and BigQuery bills a 10 MB minimum "
            f"per query, so every query would be refused before it ran. This flag takes "
            f"BYTES: write {raw}GB if you meant {raw} gigabytes, or {raw}MB for megabytes")
    return value


def over_budget(error: str | None) -> bool:
    """True when the byte ceiling turned the query away before it ran.

    Nothing is wrong with the SQL. Somebody raises --max-bytes, or narrows
    the question, and it runs. 112 of 446 mined candidates on one estate
    came back this way and read as broken SQL."""
    return bool(error) and _OVER_BUDGET.search(error) is not None


def table_missing(error: str | None) -> bool:
    """True when the query names a table or dataset that does not exist.

    The SQL was right when it was written. A table dropped or renamed since
    then is nobody's SQL bug and no ceiling will bring it back: the gold goes."""
    return bool(error) and _TABLE_GONE.search(error) is not None


def _load_bigquery():
    """The google.cloud.bigquery module, imported here and nowhere else so
    the core install stays stdlib-only. Tests replace this one function with
    a fake module (Client, QueryJobConfig) and the executor never knows."""
    from google.cloud import bigquery

    return bigquery


class BigQueryExecutor:
    """Executor over BigQuery — optional, behind the [bigquery] extra.

    `project` is the billing project: every job runs and is billed there.
    `dataset` ("data_project.dataset") resolves unqualified table names, so
    a public dataset can be read while another project pays
    (QueryJobConfig.default_dataset; the data project may differ from the
    billing one). Every query is dry-run first to enforce a byte ceiling,
    unless `refuse_on_dry_run` is off: then only BigQuery enforces it, on
    the real bytes, and the job fails free above it. `log` turns it off
    because the job-history view's dry run overstates: one estate measured
    266 GB at dry run for a job that billed 29 GB.

    Time, measured on one estate: a round trip is about 3 s through a
    corporate proxy for 0.2 s of server time, so the executor works on
    making fewer calls. A dry-run is remembered per SQL text, so `estimate`
    followed by `execute` dry-runs each statement once, not twice. One
    client per thread keeps a TLS connection open between calls.
    `use_query_cache` is on by default: a fixed SQL text over fixed data
    gives the same rows either way, and a job served from the cache bills
    nothing; `gate --check-stability` turns it off because it wants two
    real runs.

    Job settings, checked against the library source (google-cloud-bigquery
    3.44.0, job/query.py and job/base.py) and
    https://docs.cloud.google.com/python/docs/reference/bigquery/latest/google.cloud.bigquery.job.QueryJobConfig:
    dry_run, maximum_bytes_billed, use_query_cache, default_dataset (accepts
    a "project.dataset" string), job_timeout_ms. `job.result(timeout=,
    max_results=)` bounds the wait and the rows fetched;
    `total_bytes_processed` / `total_bytes_billed` are read from the job.
    """

    kind = "bigquery"

    def __init__(
        self,
        project: str | None = None,
        dataset: str | None = None,
        location: str | None = None,
        maximum_bytes_billed: int = 1_000_000_000,  # 1 GB ceiling by default
        row_cap: int | None = None,
        timeout_seconds: float | None = None,
        use_query_cache: bool = True,
        refuse_on_dry_run: bool = True,
    ):
        self._bq = _load_bigquery()
        self.project = project
        self.dataset = dataset
        self.location = location
        self.maximum_bytes_billed = maximum_bytes_billed
        self.refuse_on_dry_run = refuse_on_dry_run
        self.row_cap = row_cap
        self.timeout_seconds = timeout_seconds
        self.use_query_cache = use_query_cache
        # what the executed jobs reported; the CLI writes this into the run
        # manifest as telemetry. elapsed_seconds is wall-clock time from the
        # first call to the end of the last one, dry-runs included: the time
        # a person waited, whatever the worker count.
        # ceiling_kills counts the jobs BigQuery stopped at
        # maximum_bytes_billed after the dry run had passed them: free, but
        # they are statements the run expected to have and does not.
        self.usage = {"queries": 0, "bytes_processed": 0, "bytes_billed": 0,
                      "ceiling_kills": 0, "elapsed_seconds": 0.0}
        self._lock = threading.Lock()
        self._local = threading.local()
        # sql text -> bytes scanned, or the exception the dry-run raised
        self._dry_runs: dict[str, int | Exception] = {}
        self._first_call: float | None = None

    def _client(self):
        """One client per thread, made on first use. On a corporate mTLS
        network a client's SSL context can be used for exactly one
        connection, so one client shared by threads fails on its second
        connection with "Context has already been used to create a
        Connection". A client per thread opens one connection each and
        keeps it, which saves a TLS handshake per call."""
        client = getattr(self._local, "client", None)
        if client is None:
            # client_info carries the framework's own user agent, so this
            # project shows up in Google's records as running contextlift
            # rather than as an unnamed script. See identity.py for why the
            # user agent and the job labels answer different questions.
            client = self._bq.Client(project=self.project, location=self.location,
                                     client_info=identity.client_info())
            self._local.client = client
        return client

    def _query(self, sql: str, config):
        """Start a job. When the thread's client raises the mTLS "already
        been used" error, drop it and retry once with a fresh client: the
        pooled connection was closed under it (idle timeout, proxy reset)
        and a new SSL context is the only way to open another."""
        started = time.monotonic()
        try:
            try:
                return self._client().query(sql, job_config=config, location=self.location)
            except Exception as e:  # noqa: BLE001 - the message is the signal
                if "already been used to create a Connection" not in str(e):
                    raise
                self._local.client = None
                return self._client().query(sql, job_config=config, location=self.location)
        finally:
            self._note_time(started)

    def _note_time(self, started: float) -> None:
        with self._lock:
            if self._first_call is None:
                self._first_call = started
            self.usage["elapsed_seconds"] = round(time.monotonic() - self._first_call, 3)

    def _config(self, **settings):
        config = self._bq.QueryJobConfig(use_query_cache=self.use_query_cache, **settings)
        if self.dataset:
            config.default_dataset = self.dataset
        # Every job, dry runs included, so a customer reading their own bill
        # can tell this framework's spend from dbt's and from a person
        # querying by hand. One place, because a label set on some jobs and
        # not others gives a number that is wrong invisibly.
        config.labels = identity.job_labels()
        return config

    def _ceiling_error(self, scanned: int | None, measured_by: str = "dry-run") -> str:
        """The one refusal text for the byte ceiling, whoever caught it.

        `measured_by` says where the figure came from: the free dry run
        before the job, or the job BigQuery killed at the cap (a killed job
        still reports total_bytes_processed). The kill used to carry no
        figure at all while the dry-run reject carried 10.87 to 1042.51 GB,
        so the same defect read as two, and the killed one gave nobody a
        number to raise --max-bytes to."""
        limit_gb = self.maximum_bytes_billed / 1e9
        seen = f" ({measured_by}: {scanned / 1e9:.2f} GB)" if scanned else ""
        return (f"rejected: the query would scan more than the {limit_gb:g} GB "
                f"ceiling{seen}; narrow it or raise --max-bytes")

    def dry_run_bytes(self, sql: str) -> int:
        """Bytes the query would scan, from a dry run (free). Raises on an
        invalid query; callers decide whether that is a verdict or a skip.

        Remembered per SQL text, failures included: the estimate dry-runs
        every statement before a run, and execute used to dry-run each one
        again, a whole extra round trip per query on a slow network."""
        with self._lock:
            known = self._dry_runs.get(sql)
        if known is None:
            try:
                job = self._query(sql, self._config(dry_run=True))
                known = int(job.total_bytes_processed or 0)
            except Exception as e:  # noqa: BLE001 - it will fail the same way when run
                known = e
            with self._lock:
                self._dry_runs[sql] = known
        if isinstance(known, Exception):
            raise known
        return known

    def estimate(self, sqls, workers: int = 1) -> dict:
        """Dry-run every distinct read-only statement and price what will
        run, without executing anything.

        `bytes` is what the dry-runs measured. `billable_bytes` and `usd`
        cover only the statements the run will execute: one over
        `maximum_bytes_billed` is rejected without running and costs
        nothing (on one estate 14 of 25 golds were over the 1 GB default;
        the total priced them all and said nothing), and so does one the
        engine rejects at dry-run (`failed`). `billable_bytes` applies
        BigQuery's 10 MB minimum per query. `over_ceiling` counts the
        statements the ceiling turns away and their bytes. `not_found` is
        how many of the failed ones named a table or dataset the billing
        project does not have (see `not_found_in`), and `refused` how many
        the platform turned away rather than the engine (see
        `platform_refused`), and `no_credential` how many never left
        because no token could be minted (see `credential_refused`). All
        three used to print the same words, which sent a reader to the
        wrong person twice. `statements` is one
        record per distinct statement in input order, so a caller can name
        them; a statement that is not read-only is in it as failed but is
        neither dry-run nor counted in `queries`. `workers` dry-runs that
        many statements at once; each distinct statement is still dry-run
        exactly once.
        """
        distinct = list(dict.fromkeys(sqls))
        to_dry_run = [sql for sql in distinct if is_read_only(sql)]

        def scanned_or_error(sql: str) -> int | Exception:
            try:
                return self.dry_run_bytes(sql)
            except Exception as e:  # noqa: BLE001
                return e

        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(scanned_or_error, to_dry_run))
        else:
            results = [scanned_or_error(sql) for sql in to_dry_run]
        measured = dict(zip(to_dry_run, results))
        ceiling = self.maximum_bytes_billed
        statements = []
        over = {"count": 0, "largest_bytes": 0, "bytes": 0}
        total = billable = failed = not_found = refused = 0
        no_credential = 0
        for sql in distinct:
            scanned = measured.get(sql)            # None: not read-only, never dry-run
            broken = scanned is None or isinstance(scanned, Exception)
            record = {"bytes": None if broken else scanned, "over_ceiling": False,
                      "failed": broken, "refused": False,
                      "no_credential": False}
            statements.append(record)
            if isinstance(scanned, Exception):
                failed += 1
                not_found += not_found_in(str(scanned), self.project)
                # Credential first: an auth error can carry "denied" and
                # would otherwise read as a 403 the security team must fix.
                record["no_credential"] = credential_refused(str(scanned))
                record["refused"] = (not record["no_credential"]
                                     and platform_refused(str(scanned)))
                refused += record["refused"]
                no_credential += record["no_credential"]
                continue
            if scanned is None:
                continue
            total += scanned
            if scanned > ceiling:
                record["over_ceiling"] = True
                over["count"] += 1
                over["bytes"] += scanned
                over["largest_bytes"] = max(over["largest_bytes"], scanned)
                continue
            billable += max(scanned, BIGQUERY_MIN_BYTES_PER_QUERY)
        return {
            "queries": len(to_dry_run), "failed": failed, "not_found": not_found,
            "refused": refused, "no_credential": no_credential,
            "bytes": total, "billable_bytes": billable,
            "usd": round(bigquery_usd(billable), 6),
            "usd_per_tib": BIGQUERY_USD_PER_TIB, "price_as_of": BIGQUERY_PRICE_AS_OF,
            "ceiling_bytes": ceiling, "over_ceiling": over, "statements": statements,
        }

    def check(self, sql: str) -> str | None:
        """The error a run would raise, from a dry run (free), or None. The
        byte ceiling counts as an error, as it does in execute. On one estate
        7 of 8 drafts that did not run were caught here for zero bytes."""
        reason = read_only_violation(sql)
        if reason:
            return reason
        try:
            scanned = self.dry_run_bytes(sql)
        except Exception as e:  # google client raises many exc types; flatten
            return f"{type(e).__name__}: {e}"
        if scanned > self.maximum_bytes_billed:
            return self._ceiling_error(scanned)
        return None

    def execute(self, sql: str) -> ExecResult:
        reason = read_only_violation(sql)
        if reason:
            return ExecResult(error=reason)
        try:
            scanned = self.dry_run_bytes(sql)
        except Exception as e:  # google client raises many exc types; flatten
            return ExecResult(error=f"{type(e).__name__}: {e}")
        if self.refuse_on_dry_run and scanned > self.maximum_bytes_billed:
            return ExecResult(error=self._ceiling_error(scanned))
        settings = {"maximum_bytes_billed": self.maximum_bytes_billed}
        if self.timeout_seconds is not None:
            # server-side too, so a query the client stops waiting for does
            # not keep running (and billing) behind the run
            settings["job_timeout_ms"] = int(self.timeout_seconds * 1000)
        job = None
        started = time.monotonic()
        try:
            job = self._query(sql, self._config(**settings))
            fetched = job.result(
                timeout=self.timeout_seconds,
                max_results=None if self.row_cap is None else self.row_cap + 1,
            )
            rows = [dict(r) for r in fetched]
        except Exception as e:  # noqa: BLE001
            message = str(e)
            if "already been used to create a Connection" in message:
                # the connection died while waiting for the rows; the next
                # call on this thread gets a fresh client
                self._local.client = None
            if isinstance(e, TimeoutError) or "timed out" in message.lower() \
                    or "deadline" in message.lower():
                if job is not None:
                    try:
                        job.cancel()
                    except Exception:  # noqa: BLE001 - best effort
                        pass
                # same prefix as SQLite, so gate and triage file it as TIMEOUT
                return ExecResult(error=f"timeout: exceeded {self.timeout_seconds:g}s")
            if "bytes billed" in message:
                # the killed job reports what it read; without it the reject
                # named the ceiling and nothing else
                with self._lock:
                    self.usage["ceiling_kills"] += 1
                read = None
                if job is not None:
                    try:
                        read = int(job.total_bytes_processed or 0)
                    except Exception:  # noqa: BLE001 - a figure is a bonus, not the error
                        read = None
                return ExecResult(error=self._ceiling_error(read, "the killed job read"))
            return ExecResult(error=f"{type(e).__name__}: {e}")
        finally:
            self._note_time(started)
        with self._lock:
            self.usage["queries"] += 1
            self.usage["bytes_processed"] += int(job.total_bytes_processed or 0)
            self.usage["bytes_billed"] += int(job.total_bytes_billed or 0)
        rows, truncated = apply_row_cap(rows, self.row_cap)
        return ExecResult(rows=rows, truncated=truncated)
