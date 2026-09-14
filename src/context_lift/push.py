# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Write reviewed context into a catalog and read every write back.

The context a person reviewed lives in files: one page per table, a rules
draft, a worked-example bank. This is the logic of `catalog-push`, the stage
that puts those into Knowledge Catalog, where a text-to-SQL agent reads them
at question time. One rule shapes all of it (docs/CONTEXT-SOURCES.md): a
catalog that accepts bytes and keeps fewer of them looks exactly like one that
worked. So nothing here counts as written until a read shows it, and a read
that comes back shorter than what went in is reported with its cause.

Exports: PushStore, PushTransport, PushResult, push, pages_in, page_record,
stamp, stamp_of, unstamped, content_sha256, verify_readback, verify_written,
tally, term_lines, slug, examples_in_bank, query_entry_id, project_number_of,
entry_group_of, holdout_guard, take_push_lock, PushAlreadyRunning,
previous_records, write_manifest, format_summary.

Nothing here talks to a network. The store and its transport are passed in
(the two Protocols below); the live ones live in catalog.py.
"""
from __future__ import annotations

import hashlib
import os
import time
import json
import pathlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Protocol

# a pure predicate over an exception and a budget from a page size, no client
# and no network: the catalog module owns them because both came from that API
from .catalog import _is_not_found, budget_that_serves
from .draft import DRAFT_MARK, READ_BUDGET_CHARS
# LABELS and slug live beside Spec because the READ side needs them too: the
# write decides whether a stored term is a stale one of ours by the label,
# and matches spec terms to stored ones by the id. Re-exported here because
# callers and tests import them from this module.
from .export import LABELS, slug  # noqa: F401 - re-exported on purpose
from .leakage import LeakageReport, check_leakage
from .mine import tables_named

STAMP_BEGIN = "<!-- contextlift catalog-push {sha} begin -->"
STAMP_END = "<!-- contextlift catalog-push {sha} end -->"
# a read that kept less than this share of the text between the markers is
# truncated; YAML folding moves whitespace but drops no words, so the share
# is measured on whitespace-collapsed text
KEPT_FRACTION = 0.95

# A served overview is a YAML scalar. A double-quoted one is folded at a fixed
# width with `\` + newline + indentation, the escaped line break, which stands
# for no character at all; a space that lands at the start of the next line is
# written `\ ` so the indentation does not eat it. On one deployment that fold
# landed inside the end marker of 22 pages out of 99, verbatim
# `<!-- contextlift\` newline `    \ catalog-push ... end -->`; collapsed as
# whitespace the marker no longer matched, and each of the 22 was called
# truncated with a served text LONGER than the page. Unfolded by both rules,
# 18 of them verified. The begin marker starts the scalar and is never split.
_ESCAPED_LINE_BREAK = re.compile(r"\\\n[ \t]*")
_ESCAPED_SPACE = "\\ "

_STAMP = re.compile(r"<!-- contextlift catalog-push ([0-9a-f]{12}) begin -->")
_STAMPED = re.compile(
    r"\A<!-- contextlift catalog-push ([0-9a-f]{12}) begin -->\n(.*)\n"
    r"<!-- contextlift catalog-push \1 end -->\n?\Z", re.S)
_TERM_LINE = re.compile(r'^- "([^"]+)"\s*->\s*(.+)$')
_HTML_COMMENT = re.compile(r"\s*<!--.*?-->\s*$", re.S)
_EXAMPLE = re.compile(r"^Q: (?P<question>[^\n]*)\nSQL: (?P<sql>.*?)(?=\n\s*\nQ: |\s*\Z)",
                      re.M | re.S)
_WHITESPACE = re.compile(r"\s+")


class PushTransport(Protocol):
    """The calls a catalog needs for what is not the overview: worked
    examples and glossary terms. `write_queries` raises an exception whose
    class is named WriteUnsupported on a deployment without the queries
    aspect; push then files the examples as query entries instead."""

    def write_queries(self, entry_name: str, queries: list[dict]) -> None: ...

    def upsert_term(self, glossary_id: str, term_id: str, display_name: str,
                    description: str, labels: dict[str, str]) -> tuple[str, str]: ...

    def link_term(self, source_entry: str, term_entry: str, link_id: str) -> str: ...

    def upsert_query_entry(self, group_id: str, entry_id: str, question: str, sql: str,
                           labels: dict[str, str]) -> tuple[str, str]: ...

    def read_entry(self, entry_name: str) -> dict: ...


class PushStore(Protocol):
    """A catalog as the push sees it: resolve a table to its entry, write an
    overview, read it back two ways. `read_now` is what an agent sees through
    lookupContext at this moment, no cache, which straight after a write can
    be a stub. `stored_overview` is the aspect as the catalog holds it. The two
    differ exactly when the reader is the problem and not the store, and
    `verify_written` tells those apart."""

    min_good_chars: int
    context_budget: int
    transport: PushTransport
    sleep: Callable[[float], None]

    def resolve(self, table: str) -> str | None: ...

    def stored_overview(self, entry_name: str) -> str | None: ...

    def write_overview(self, entry_name: str, text: str) -> None: ...

    def read_now(self, entry_name: str) -> str: ...


@dataclass
class PushResult:
    """What the push did, one dict per page, term and example, and the
    totals the summary and the manifest print. An entry carries its status
    (written, unchanged, would write, no entry, draft, not pushed) and, once
    written, the read-back verdict with every length seen on the way."""

    entries: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)


def page_record(key: str, text: str, entry_name: str | None = None) -> dict:
    """The record one page gets in a PushResult. The CLI's --verify-only
    builds them without a push and hands them to verify_written."""
    return _entry("page", key=key, table=key, entry_name=entry_name,
                  content_sha256=content_sha256(text), chars=len(text))


# ---- pages on disk ---------------------------------------------------------

def table_of_filename(filename: str) -> str | None:
    """`dataset.table` for the two shapes DirectoryContextSource resolves,
    `dataset.table.md` and `dataset__table.md`; None for a bare `table.md`
    (SQLite pages have no dataset) or any other file. The last two parts win
    when there are more, the same as tables_named."""
    if not filename.endswith(".md"):
        return None
    stem = filename[:-3]
    parts = stem.split("__") if "__" in stem else stem.split(".")
    if len(parts) < 2 or not all(parts):
        return None
    return ".".join(parts[-2:])


def pages_in(docs_dir: str | pathlib.Path,
             tables: list[str] | None = None) -> tuple[dict[str, str], int]:
    """`dataset.table` -> page text for every markdown file whose name carries
    a dataset, plus how many files did not. A catalog entry is found by
    dataset and table, and a bare name would land on the wrong table in any
    project that has two of that name, so bare files are counted and left.
    `tables` narrows the pages to those keys."""
    pages: dict[str, str] = {}
    bare = 0
    wanted = None if tables is None else {t.lower() for t in tables}
    for path in sorted(pathlib.Path(docs_dir).glob("*.md")):
        key = table_of_filename(path.name)
        if key is None:
            bare += 1
            continue
        if wanted is not None and key.lower() not in wanted:
            continue
        pages[key] = path.read_text()
    return pages, bare


# ---- the stamp -------------------------------------------------------------

def unstamped(text: str) -> str:
    """The page without its push markers; the text itself when it has none."""
    found = _STAMPED.match(text or "")
    return found.group(2) if found else (text or "")


def content_sha256(text: str) -> str:
    """The hash the stamp carries a prefix of: over the page without markers
    and without trailing newlines, so a re-saved file is the same page."""
    return _sha(unstamped(text).rstrip("\n"))


def stamp(text: str) -> tuple[str, str]:
    """The page between a begin and an end marker, both carrying the first
    twelve hex digits of its sha256. The begin marker is how a later push
    knows the stored text is this page and unchanged; the end marker is how
    a read-back knows the tail survived. A page that already carries markers
    (pulled from the catalog, pushed again) is unwrapped first, so they never
    nest."""
    body = unstamped(text).rstrip("\n")
    sha12 = content_sha256(body)[:12]
    return sha12, (f"{STAMP_BEGIN.format(sha=sha12)}\n{body}\n"
                   f"{STAMP_END.format(sha=sha12)}\n")


def stamp_of(stored_text: str | None) -> str | None:
    """The sha12 a stored overview was pushed with, None when contextlift did
    not write it (hand-written, another tool, or nothing there)."""
    found = _STAMP.search(stored_text or "")
    return found.group(1) if found else None


# ---- reading back ----------------------------------------------------------

def verify_readback(written_stamped: str, readback: str, min_good_chars: int) -> str:
    """One of "verified", "stub", "missing", "truncated", "unverified" for
    what a reader sees against what was written. Stub: shorter than the
    document could be, the state straight after a write (the threshold is
    capped at the written length, so a short page is not a stub forever).
    Missing: no begin marker, the reader still sees the previous text or
    nothing. Truncated: bytes are gone, the begin marker but not the end in
    a served text shorter than the page, or the text between the markers
    under 95% of what was written. Unverified: the begin marker but not the
    end in a served text no shorter than the page, so nothing is known lost
    and the marker could not be matched. Compared on the served text
    unfolded and with all whitespace collapsed (`_served`): the read is a
    YAML bundle that folds long lines, and a fold can land inside a marker."""
    sha12 = stamp_of(written_stamped)
    if sha12 is None:
        raise ValueError("the written text carries no catalog-push stamp")
    if len(readback or "") < min(min_good_chars, len(written_stamped)):
        return "stub"
    begin, end = STAMP_BEGIN.format(sha=sha12), STAMP_END.format(sha=sha12)
    seen = _served(readback)
    start = seen.find(begin)
    if start < 0:
        return "missing"
    stop = seen.find(end, start)
    if stop < 0:
        return "truncated" if len(readback) < len(written_stamped) else "unverified"
    written = _collapse(written_stamped)
    written_between = written[written.index(begin) + len(begin):written.index(end)]
    seen_between = seen[start + len(begin):stop]
    if len(seen_between) < KEPT_FRACTION * len(written_between):
        return "truncated"
    return "verified"


def verify_written(store: PushStore, pending: list[tuple[dict, str]], attempts: int = 4,
                   progress: Callable[[str], None] | None = None) -> None:
    """Phase 2 of a push: read every written page back until it is what a
    reader sees. `pending` pairs each page's record with the stamped text
    that was written. Every page is read once per pass, and one wait of 2,
    4, 6 seconds sits between passes (the lag after a write, the way
    fetch_document handles it), not between pages: on one deployment
    reading each page to exhaustion before writing the next made 99 pages
    take about 25 minutes, 22 of them read four times for a marker a line
    fold had split. Every length seen stays on the record: "0, 0, 512,
    78223" and "78223 four times" are different stories under the same
    verdict, and `attempts` is the number of reads. A verified page also
    records `served_minus_written`, the bundle's overhead, with no verdict
    on it. A page still not verified after the last pass gets its cause
    named by `_name_cause`."""
    say = progress or (lambda line: None)
    # the live store carries no sleeper (the fakes do); a push that crashed
    # here after one write left 1 of 113 entries changed with no record
    sleeper = getattr(store, "sleep", None) or time.sleep
    passes = max(1, attempts)
    remaining = list(pending)
    for attempt in range(passes):
        if attempt:
            say(f"pass {attempt + 1}: {len(remaining)} pages to re-read after {2 * attempt} s")
            sleeper(2.0 * attempt)              # linear backoff, as fetch_document
        still: list[tuple[dict, str]] = []
        for index, (entry, stamped) in enumerate(remaining, 1):
            where = f"[verify {index}/{len(remaining)}] {entry['key']}"
            try:
                seen = store.read_now(entry["entry_name"]) or ""
            except Exception as error:  # noqa: BLE001 - this page's line, the pass goes on
                entry["verify"], entry["cause"] = "failed", f"{type(error).__name__}: {str(error)[:200]}"
                say(f"{where} read failed: {entry['cause']}")
                continue
            entry["readback_chars"].append(len(seen))
            entry["attempts"] = len(entry["readback_chars"])
            entry["verify"] = verify_readback(stamped, seen, store.min_good_chars)
            reads = f"{entry['attempts']} read" + ("" if entry["attempts"] == 1 else "s")
            if entry["verify"] == "verified":
                entry["served_minus_written"] = len(seen) - len(stamped)
                say(f"{where} verified in {reads}")
                continue
            still.append((entry, stamped))
            if attempt == passes - 1:
                say(f"{where} {entry['verify']} after {reads}")
            else:
                say(f"{where} not yet ({entry['verify']}), retry")
        remaining = still
        if not remaining:
            break
    for entry, stamped in remaining:
        _name_cause(store, entry, stamped)


def _name_cause(store: PushStore, entry: dict, stamped: str) -> None:
    """Why a page did not verify, from one read of the stored aspect. The
    catalog kept fewer bytes than were sent, which no reader setting fixes;
    or it kept all of them and the reader's budget cuts the tail (raise the
    budget, or move the Rules section up the page); or the served text is
    no shorter than the page and the end marker still could not be found in
    it, which is not a truncation and is not called one.

    A stub is the fourth, and it is not always the lag after a write. A page
    longer than the read budget is served as a few hundred characters of its
    opening, HTTP 200 and no warning, for as long as the budget stays where
    it is, so more reads never clear it. The verdict stays `stub`, because a
    reader really does see a stub; the cause names the stored size beside
    the served one, so nobody is sent to fix a write that landed whole."""
    if entry["verify"] not in ("stub", "truncated", "unverified"):
        return
    served = entry["readback_chars"][-1] if entry["readback_chars"] else 0
    try:
        stored = store.stored_overview(entry["entry_name"]) or ""
    except Exception as error:  # noqa: BLE001 - the cause is then the read itself
        entry["cause"] = f"the stored aspect could not be read: {type(error).__name__}: {str(error)[:200]}"
        return
    if entry["verify"] == "stub":
        entry["cause"] = _stub_cause(len(stored), len(stamped), served, store.context_budget)
        return
    if len(stored) < len(stamped):
        entry["verify"] = "truncated"
        entry["cause"] = f"stored {len(stored)} of {len(stamped)}"
    elif served < len(stamped):
        entry["cause"] = f"reader sees {served} of {len(stamped)}; the aspect holds all of it"
    else:
        entry["cause"] = (f"end marker not found in the served text (served {served} characters "
                          f"for {len(stamped)} written)")


def _stub_cause(stored: int, written: int, served: int, budget: int) -> str:
    """Why a reader sees a stub, in three cases the numbers tell apart: the
    write lost bytes; the write is whole and the catalog has not made it
    readable yet, which is 0 characters and clears itself; or the write is
    whole and this budget does not serve it, which is a few hundred
    characters of the page's opening and stays until the budget moves. The
    third is the one that sends somebody to fix a page that is already
    right, so it says the write is fine and names a budget that fits."""
    if stored < written:
        return f"stored {stored} of {written}; the reader was served {served} at budget {budget}"
    if served == 0:
        return (f"the entry stores {stored} characters and the reader sees nothing yet; the "
                "catalog makes a write readable seconds later, so re-read with --verify-only")
    return (f"the entry stores {stored} characters and the reader was served {served} at budget "
            f"{budget}; the write landed whole, raise --budget to {budget_that_serves(stored)}")


def _read_until(store: PushStore, entry_name: str, attempts: int,
                classify: Callable[[str], str]) -> tuple[str, list[int], str]:
    """(final verdict, every length seen, the last text) after reading until
    `classify` says verified or the attempts run out."""
    lengths: list[int] = []
    verdict, seen = "missing", ""
    # the live store carries no sleeper (the fakes do); a push that crashed
    # here after one write left 1 of 113 entries changed with no record
    sleeper = getattr(store, "sleep", None) or time.sleep
    for attempt in range(max(1, attempts)):
        if attempt:
            sleeper(2.0 * attempt)              # linear backoff, as fetch_document
        seen = store.read_now(entry_name) or ""
        lengths.append(len(seen))
        verdict = classify(seen)
        if verdict == "verified":
            break
    return verdict, lengths, seen


def _entry_text_verdict(transport: PushTransport, entry_name: str, expected: str,
                        attempts: int = 1,
                        sleeper: Callable[[float], None] | None = None) -> tuple[str, int, str | None]:
    """(verdict, reads, cause) for a term or a query entry: its description
    must hold the text sent, by length after whitespace collapse.

    A term is not readable for a few seconds after it is created. A live
    push wrote and verified 13 pages, then died on the read-back of its
    first new term with `404 Entry .../terms/<id> does not exist` and two
    gRPC tracebacks; each re-run got one term further, and it took 8 runs to
    finish. So not-found here is the lag the pages already retry through:
    the same 2, 4, 6 second backoff and the same `attempts`. After the last
    one the entry is `unverified` with the cause on it, and the push carries
    on to the next term."""
    sleep = sleeper or time.sleep
    reads = 0
    for attempt in range(max(1, attempts)):
        if attempt:
            sleep(2.0 * attempt)              # linear backoff, as the pages
        reads += 1
        try:
            record = transport.read_entry(entry_name) or {}
        except Exception as error:  # noqa: BLE001 - not-found means not visible yet
            if not _is_not_found(error):
                raise
            continue
        description = _collapse(record.get("description") or "")
        if not description:
            return "missing", reads, None
        if len(description) < KEPT_FRACTION * len(_collapse(expected)):
            return "truncated", reads, None
        return "verified", reads, None
    return "unverified", reads, (f"the catalog still answers not found {reads} reads after the "
                                 "write; it makes a new entry readable a few seconds later")


# ---- terms and examples out of the reviewed files --------------------------

def term_lines(rules_text: str, known_tables: list[str]) -> tuple[list[dict], int]:
    """Glossary terms out of a rules file. A term is a rule of the shape
    `- "phrase" -> meaning`: a quoted phrase, an arrow, the meaning; the
    trailing HTML comment draft-rules adds (the question ids) is dropped.
    Every other rule line is a sentence with no phrase to define: counted,
    skipped. A term's table is the first known `dataset.table` its meaning
    names, so push can link the term to that table's entry."""
    terms: list[dict] = []
    skipped = 0
    for line in (rules_text or "").splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        found = _TERM_LINE.match(_HTML_COMMENT.sub("", line))
        if not found:
            skipped += 1
            continue
        phrase, meaning = found.group(1).strip(), found.group(2).strip()
        terms.append({"term_id": slug(phrase), "display_name": phrase, "description": meaning,
                      "table": _first_table_named(meaning, known_tables)})
    return terms, skipped


def _first_table_named(text: str, known_tables: list[str]) -> str | None:
    earliest: tuple[int, str] | None = None
    for table in known_tables:
        found = re.search(rf"(?<!\w){re.escape(table)}(?!\w)", text, re.I)
        if found and (earliest is None or found.start() < earliest[0]):
            earliest = (found.start(), table)
    return earliest[1] if earliest else None


def examples_in_bank(bank: dict[str, str]) -> list[dict]:
    """The distinct worked examples in a curate-examples bank ({question_id:
    block}, each block "Q: ...\\nSQL: ..." repeated). One example is retrieved
    for many questions and so sits in many blocks; it becomes one entry, keyed
    on the question and the SQL with whitespace collapsed. `table` is the
    SQL's primary table, so the example can live on that table's entry."""
    seen: set[tuple[str, str]] = set()
    examples: list[dict] = []
    for key in sorted(bank):
        for found in _EXAMPLE.finditer(bank[key] or ""):
            question, sql = found.group("question").strip(), found.group("sql").strip()
            pair = (question, _sql_key(sql))
            if not question or not sql or pair in seen:
                continue
            seen.add(pair)
            named = tables_named(sql)
            examples.append({"question": question, "sql": sql,
                             "table": named[0] if named else ""})
    return examples


def query_entry_id(question: str, sql: str) -> str:
    """A stable entry id for one worked example: the same question and SQL,
    whitespace aside, get the same id on every push, so a re-push updates the
    entry instead of adding a twin."""
    return "vq-" + _sha(_sql_key(sql) + "\n" + question.strip().lower())[:16]


def project_number_of(entry_name: str) -> str | None:
    """The digits after `projects/`; entry names carry the project number,
    not its id, and a name built from the id resolves to nothing."""
    found = re.search(r"projects/(\d+)", entry_name or "")
    return found.group(1) if found else None


def entry_group_of(entry_name: str) -> str:
    """Everything before `/entries/`: the group an entry lives in."""
    return (entry_name or "").split("/entries/", 1)[0]


# ---- the push --------------------------------------------------------------

def holdout_guard(holdout_rows: list[tuple[str, str, str]], pages: dict[str, str],
                  terms: list[dict], examples: list[dict]) -> LeakageReport:
    """The leak check over everything about to be pushed, against the
    reserved questions: the pages, each term as an agent would read it, each
    example as the Q/SQL pair it is. A push that puts a reserved answer into
    the catalog turns the holdout into memorization, so push refuses on any
    finding. The report answers `.clean` and `.summary()`."""
    parts = [pages[key] for key in sorted(pages)]
    parts += [f"{term['display_name']}: {term['description']}" for term in terms]
    parts += [f"Q: {example['question']}\nSQL: {example['sql']}" for example in examples]
    return check_leakage(holdout_rows, "\n\n".join(parts))


def push(store: PushStore, *, pages: dict[str, str], terms: list[dict] | None = None,
         examples: list[dict] | None = None, holdout_rows: list | None = None,
         glossary_id: str = "", query_group: str = "", attempts: int = 4,
         dry_run: bool = False, allow_draft: bool = False,
         read_budget: int = READ_BUDGET_CHARS, previous_out: pathlib.Path | None = None,
         bare_skipped: int = 0, result: PushResult | None = None,
         progress: Callable[[str], None] | None = None) -> PushResult:
    """Write pages, worked examples and terms into the store, and read each
    back before counting it. Order: the holdout guard first, and on a finding
    nothing is written at all. Then pages in three phases: every page is
    resolved and two pages on one entry refuse the push (`_resolve_pages`,
    `_collisions`); every page is compared with the stored stamp and
    written (`_push_pages`); every written page is read back together
    (`verify_written`), so the wait for the catalog's served copy is paid
    once per pass and not once per page. Then examples (the table's queries
    aspect, or query entries in `query_group` when the aspect is
    unsupported), then terms (upsert, link to the table's entry when the
    meaning names one).

    A stored overview that contextlift did not write is replaced, and its bytes
    go to `previous_out/<dataset>.<table>.md` first, because nothing else
    holds them; a page contextlift wrote before is in the docs directory's
    history already. `dry_run` touches no catalog entry but still keeps those
    previous bytes, since they are the evidence for the decision. Pages over
    `read_budget` are pushed and listed: a catalog read truncates them.
    `progress` gets one line per page and per read-back pass, for a terminal.
    """
    terms, examples = list(terms or []), list(examples or [])
    counts = {
        "pages": len(pages), "terms": len(terms), "examples": len(examples),
        "holdout_questions": len(holdout_rows or []), "leak": None,
        "written": 0, "unchanged": 0, "verified": 0, "failed": 0,
        "over_budget": [], "replaced_unstamped": 0, "drafts_skipped": 0,
        "bare_skipped": bare_skipped, "linked": 0, "examples_home": None,
        "refused": None, "collisions": [], "template": None,
        "read_budget": read_budget, "dry_run": dry_run,
        "previous_out": str(previous_out) if previous_out else None}
    if result is None:
        result = PushResult(counts=counts)
    else:
        # a caller's sink: it keeps what was done if this call dies mid-way
        result.counts.update(counts)
    if holdout_rows:
        report = holdout_guard(holdout_rows, pages, terms, examples)
        result.counts["leak"] = "clean" if report.clean else report.summary()
        if not report.clean:
            result.counts["refused"] = "leak"
            return result
    entry_names = _resolve_pages(store, pages, allow_draft=allow_draft, progress=progress)
    # no built name of the run exists: one flag is wrong and every page would
    # fail the same way, so it is one refusal and not 113 failed lines
    mismatch = getattr(store, "template_mismatch", lambda: None)()
    if mismatch:
        result.counts["refused"], result.counts["template"] = "template", mismatch
        return result
    collisions = _collisions(entry_names)
    if collisions:
        result.counts["refused"], result.counts["collisions"] = "collision", collisions
        return result
    entry_for_table, written = _push_pages(store, pages, entry_names, result, dry_run=dry_run,
                                           read_budget=read_budget, previous_out=previous_out,
                                           progress=progress)
    verify_written(store, written, attempts=attempts, progress=progress)
    _push_examples(store, examples, entry_for_table, result, query_group=query_group,
                   attempts=attempts, dry_run=dry_run)
    _push_terms(store, terms, entry_for_table, result, glossary_id=glossary_id,
                attempts=attempts, dry_run=dry_run)
    tally(result)
    return result


def tally(result: PushResult) -> None:
    """The counts from the entries. Called at the end of a push, and by a
    caller whose push was interrupted, so the manifest carries the counts
    so far."""
    result.counts.update(_entry_counts(result.entries))


def _entry_counts(entries: list[dict]) -> dict:
    """The counts that come from the records themselves, so a manifest that
    merged an earlier run's records counts those too."""
    return {"written": sum(1 for e in entries if e.get("status") == "written"),
            "unchanged": sum(1 for e in entries if e.get("status") == "unchanged"),
            "verified": sum(1 for e in entries if e.get("verify") == "verified"),
            "failed": sum(1 for e in entries if _failed(e)),
            "linked": sum(1 for e in entries if e.get("linked"))}


def _resolve_pages(store, pages, *, allow_draft, progress) -> dict[str, str | None]:
    """Phase 0: every page's entry, before any write; searches are free. A
    draft that will not be pushed is not resolved and is absent here."""
    say = progress or (lambda line: None)
    keys = sorted(key for key in pages if allow_draft or DRAFT_MARK not in pages[key])
    entry_names: dict[str, str | None] = {}
    for index, key in enumerate(keys, 1):
        entry_names[key] = store.resolve(key)
        say(f"[resolve {index}/{len(keys)}] {key} -> {entry_names[key] or 'no entry'}")
    return entry_names


def _collisions(entry_names: dict[str, str | None]) -> list[str]:
    """`key1, key2 -> entry` for every entry that more than one page
    resolved to. An earlier resolve put 12 of 99 pages onto a sibling
    table's entry (the search term was a substring of its name), and
    read-back verified every one of them: read-back proves the bytes
    survived, not that they landed on the right table. So two pages on one
    entry refuse the push before any write."""
    keys_by_entry: dict[str, list[str]] = {}
    for key, entry_name in entry_names.items():
        if entry_name:
            keys_by_entry.setdefault(entry_name, []).append(key)
    return [f"{', '.join(keys)} -> {entry_name}"
            for entry_name, keys in sorted(keys_by_entry.items()) if len(keys) > 1]


def _push_pages(store, pages, entry_names, result, *, dry_run, read_budget, previous_out,
                progress) -> tuple[dict[str, str], list[tuple[dict, str]]]:
    """Phase 1, the overviews: compare the stored stamp, write. Returns
    table (lowercased) -> entry name for every page that resolved, so
    examples and terms can attach to the same entries, and the written
    pages with their stamped text for phase 2."""
    say = progress or (lambda line: None)
    entry_for_table: dict[str, str] = {}
    written: list[tuple[dict, str]] = []
    keys = sorted(pages)
    for index, key in enumerate(keys, 1):
        entry = page_record(key, pages[key])
        result.entries.append(entry)
        if key not in entry_names:                  # a draft phase 0 did not resolve
            entry["status"] = "draft, not pushed"
            result.counts["drafts_skipped"] += 1
        elif not entry_names[key]:
            entry["status"] = "no entry"
        else:
            stamped = _push_page(store, key, pages[key], entry, entry_names[key], result,
                                 entry_for_table, dry_run=dry_run, read_budget=read_budget,
                                 previous_out=previous_out)
            if stamped:
                written.append((entry, stamped))
        say(f"[{index}/{len(keys)}] {key} {entry['status']}"
            + (f": {entry['cause']}" if entry.get("cause") else ""))
    return entry_for_table, written


def _push_page(store, key, text, entry, entry_name, result, entry_for_table, *, dry_run,
               read_budget, previous_out) -> str | None:
    """One resolved page through phase 1; the stamped text when it was
    written, so phase 2 can read it back."""
    entry["entry_name"] = entry_name
    entry_for_table[key.lower()] = entry_name
    if len(text) > read_budget:
        result.counts["over_budget"].append(key)
    # one entry's failure is that entry's line, not the end of the push:
    # a push that died mid-loop left a catalog half changed with no record
    try:
        return _write_page(store, key, text, entry, entry_name, result,
                           dry_run=dry_run, previous_out=previous_out)
    except Exception as error:  # noqa: BLE001 - recorded on the entry, the push goes on
        entry["status"] = entry.get("status") or "failed"
        entry["verify"] = "failed"
        entry["cause"] = f"{type(error).__name__}: {str(error)[:200]}"
        return None


def _write_page(store, key, text, entry, entry_name, result, *, dry_run, previous_out) -> str | None:
    sha12, stamped = stamp(text)
    stored = store.stored_overview(entry_name)
    if stamp_of(stored) == sha12:
        entry["status"] = "unchanged"
        return None
    if stored and stamp_of(stored) is None:
        entry["previous_sha256"], entry["previous_chars"] = _sha(stored), len(stored)
        result.counts["replaced_unstamped"] += 1
        if previous_out:
            previous_out.mkdir(parents=True, exist_ok=True)
            (previous_out / f"{key}.md").write_text(stored)
    if dry_run:
        entry["status"] = "would write"
        return None
    store.write_overview(entry_name, stamped)
    entry["status"] = "written"
    return stamped


def _push_examples(store, examples, entry_for_table, result, *, query_group, attempts, dry_run):
    """Worked examples, grouped by primary table. The first WriteUnsupported
    switches every later table to query entries too: a missing aspect type
    fails the same way on every entry of the deployment."""
    transport = store.transport
    sleeper = getattr(store, "sleep", None) or time.sleep
    by_table: dict[str, list[dict]] = {}
    for example in examples:
        by_table.setdefault((example.get("table") or "").lower(), []).append(example)
    homes: set[str] = set()
    aspect_supported = True
    for table in sorted(by_table):
        group = by_table[table]
        entries = []
        for example in group:
            entries.append(_entry("example", key=example["question"], table=table,
                                  entry_id=query_entry_id(example["question"], example["sql"]),
                                  question=example["question"], sql=example["sql"],
                                  content_sha256=_sha(example["sql"]), chars=len(example["sql"])))
        result.entries.extend(entries)
        if dry_run:
            for entry in entries:
                entry["status"] = "would write"
            continue
        entry_name = entry_for_table.get(table) or (store.resolve(table) if table else None)
        if entry_name and aspect_supported:
            try:
                transport.write_queries(
                    entry_name, [{"sql": e["sql"], "question": e["question"]} for e in group])
            except Exception as error:
                if not _is_write_unsupported(error):
                    raise
                aspect_supported = False
            else:
                homes.add("queries-aspect")
                _verify_questions_on(store, entry_name, entries, attempts)
                continue
        if not query_group:
            for entry in entries:
                entry["status"] = "no entry"
                entry["cause"] = ("the queries aspect is unsupported and no query group was given"
                                  if entry_name else "no entry for the table and no query group")
            continue
        homes.add("verified-query-entries")
        labels = {**LABELS, "table": slug(table)} if table else dict(LABELS)
        for entry in entries:
            name, state = transport.upsert_query_entry(
                query_group, entry["entry_id"], entry["question"], entry["sql"], labels)
            entry["entry_name"] = name
            entry["status"] = "unchanged" if state == "unchanged" else "written"
            entry["verify"], entry["attempts"], entry["cause"] = _entry_text_verdict(
                transport, name, entry["sql"], attempts, sleeper)
    result.counts["examples_home"] = " and ".join(sorted(homes)) or None


def _verify_questions_on(store, entry_name, entries, attempts) -> None:
    """The queries aspect is verified through the reader: every question must
    appear in what lookupContext returns for the table."""
    wanted = {e["entry_id"]: _collapse(e["question"]).lower() for e in entries}

    def classify(seen: str) -> str:
        text = _served(seen).lower()
        return "verified" if all(q in text for q in wanted.values()) else "missing"

    verdict, lengths, last = _read_until(store, entry_name, attempts, classify)
    last = _served(last).lower()
    for entry in entries:
        entry["entry_name"] = entry_name
        entry["status"] = "written"
        entry["verify"] = "verified" if wanted[entry["entry_id"]] in last else verdict
        entry["attempts"], entry["readback_chars"] = len(lengths), lengths


def _push_terms(store, terms, entry_for_table, result, *, glossary_id, attempts, dry_run):
    """Glossary terms; a term whose meaning names a known table is linked to
    that table's entry. Both are verified where they live (get_entry and the
    link's own status); one read of the table's bundle records whether the
    reader shows the term, which the first live estate did not.

    A term that is not linked carries the reason (`link_note`), because a
    summary line reading "(0 linked)" and nothing else sends a person into
    the code: a live push linked 0 of 8 terms, and the reason was that every
    meaning named a bare table name rather than a `dataset.table`."""
    transport = store.transport
    sleeper = getattr(store, "sleep", None) or time.sleep
    for term in terms:
        entry = _entry("term", key=term["display_name"], term_id=term["term_id"],
                       table=term.get("table"), content_sha256=_sha(term["description"]),
                       chars=len(term["description"]), linked=False, link_note=None,
                       verify_via=None)
        result.entries.append(entry)
        if dry_run:
            entry["status"] = "would write"
            continue
        if not glossary_id:
            entry["status"], entry["cause"] = "no entry", "no glossary given"
            continue
        term_entry, state = transport.upsert_term(
            glossary_id, term["term_id"], term["display_name"], term["description"], dict(LABELS))
        entry["entry_name"] = term_entry
        entry["status"] = "unchanged" if state == "unchanged" else "written"
        table = (term.get("table") or "").lower()
        source = entry_for_table.get(table) or (store.resolve(table) if table else None)
        if not source:
            entry["link_note"] = (f"no catalog entry for {table}" if table
                                  else "the meaning names no known dataset.table")
        else:
            link_id = f"def-{term['term_id']}-{_sha(source)[:8]}"
            try:
                entry["link"] = transport.link_term(source, term_entry, link_id)
                entry["linked"] = True
            except Exception as error:  # noqa: BLE001 - this term's line; a 403 is a SystemExit
                entry["link_note"] = f"the link call failed: {type(error).__name__}: {str(error)[:120]}"
        # the term and the link are verified where they live; whether the
        # reader's bundle shows the term is recorded, not required: on the
        # first live estate a linked term never appeared in lookupContext
        entry["verify"], entry["attempts"], entry["cause"] = _entry_text_verdict(
            transport, term_entry, term["description"], attempts, sleeper)
        entry["verify_via"] = "read_entry"
        if entry["linked"]:
            wanted = _collapse(term["display_name"]).lower()
            seen = store.read_now(source) or ""
            entry["in_lookup"] = wanted in _served(seen).lower()
            # the table's bundle, not the term's own reads: a length under
            # `readback_chars` would be counted as one of `attempts`
            entry["in_lookup_chars"] = len(seen)


def _entry(kind: str, **fields) -> dict:
    base = {"kind": kind, "entry_name": None, "content_sha256": None, "chars": 0,
            "status": None, "verify": None, "attempts": 0, "readback_chars": [], "cause": None}
    base.update(fields)
    return base


def _failed(entry: dict) -> bool:
    return entry.get("status") == "no entry" or entry.get("verify") not in ("verified", None)


def _is_write_unsupported(error: BaseException) -> bool:
    # by class name, not by import: the transport lives in catalog.py behind an
    # extra, and this module stays importable without it
    return any(cls.__name__ == "WriteUnsupported" for cls in type(error).__mro__)


def _collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", text or "").strip()


def _served(text: str) -> str:
    """Served text (lookupContext output) with whitespace collapsed, after
    undoing YAML's folding: the escaped line break stands for no character,
    the escaped space for one space. Only for served text: a page's own
    backslash-newline is a character."""
    unfolded = _ESCAPED_LINE_BREAK.sub("", text or "").replace(_ESCAPED_SPACE, " ")
    return _collapse(unfolded)


def _sql_key(sql: str) -> str:
    return _collapse(sql).rstrip(";").strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---- the lock --------------------------------------------------------------

class PushAlreadyRunning(RuntimeError):
    """The lock file names a process that is still alive."""


def take_push_lock(path: pathlib.Path, holder: dict) -> str | None:
    """Create the lock file for this push, holding `holder` ({pid,
    started_at, project, location, entry_group}) as JSON. Raises
    PushAlreadyRunning when the file names a process that is still alive;
    a lock whose process is gone is replaced, and the returned line says
    so. Two pushes on one group were started at the same time once by
    accident, each reading back the other's writes. The caller removes the
    file when the push ends."""
    existing = _read_lock(path)
    note = None
    if existing is not None:
        pid, since = existing.get("pid"), existing.get("started_at")
        if isinstance(pid, int) and _pid_alive(pid):
            raise PushAlreadyRunning(
                f"a catalog-push started at {since} (pid {pid}) is still running on this "
                f"workspace; two pushes on one group race each other (lock: {path})")
        note = f"replacing a stale lock: the catalog-push started at {since} (pid {pid}) is gone"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(holder, indent=1) + "\n")
    return note


def _read_lock(path: pathlib.Path) -> dict | None:
    """The lock's record, {} when the file is not JSON, None when absent."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        record = json.loads(text)
    except ValueError:
        return {}
    return record if isinstance(record, dict) else {}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                 # another user's process: alive
    return True


# ---- output ----------------------------------------------------------------

def previous_records(path: str | pathlib.Path) -> list[dict]:
    """The entry records a manifest already at `path` holds; [] when there is
    no file, it is not JSON, or it carries no entries. A resumed push writes
    the same file, so a caller reads this and hands it to `write_manifest`."""
    try:
        body = json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return []
    entries = body.get("entries") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        return []
    return [record for record in entries if isinstance(record, dict) and "kind" in record]


def write_manifest(result: PushResult, path: str | pathlib.Path, meta: dict,
                   status: str = "complete", previous: list[dict] | None = None) -> None:
    """The push as JSON a person or a later run can read: the caller's meta
    (which store, when, which files), whether the push ran to the end
    (`status`: complete, interrupted, stopped), every entry, the counts.

    `previous` is what an earlier run left at the same path
    (`previous_records`). The records merge by (kind, key), this run's
    winning, because a resumed push writes the file its own run alone: after
    a push that took 8 runs to finish, each manifest but the first said "0
    pages written" and the 13 page records survived only in the copy the
    first run left under runs/. The counts that come from the records are
    counted over the merged list; the counts that describe this run's inputs
    (how many pages were offered, the leak check) stay as they are."""
    entries = _merged_records(previous or [], result.entries)
    counts = dict(result.counts)
    counts.update(_entry_counts(entries))
    body = {"schema_version": 1, "status": status, "meta": meta,
            "entries": entries, "counts": counts}
    pathlib.Path(path).write_text(json.dumps(body, indent=1) + "\n")


def _merged_records(previous: list[dict], current: list[dict]) -> list[dict]:
    """One record per (kind, key), the current run's replacing the earlier
    one and a key only the earlier run holds kept, in the order they were
    first seen."""
    merged = {(record.get("kind"), record.get("key")): record for record in previous}
    merged.update({(record.get("kind"), record.get("key")): record for record in current})
    return list(merged.values())


def format_summary(result: PushResult) -> str:
    """The block the command prints: inputs, the guard, one line per kind,
    the pages a read truncates, then one line per failed entry with its cause
    and every length seen."""
    counts = result.counts
    lines = [f"catalog-push: {counts['pages']} pages, {counts['terms']} terms, "
             f"{counts['examples']} worked examples"]
    if counts["refused"] == "leak":
        lines.append("  refused: the context holds answers to the reserved questions; "
                     "nothing written")
        lines.extend("  " + line for line in counts["leak"].splitlines())
        return "\n".join(lines)
    if counts["refused"] == "template":
        lines.append(f"  refused: {counts['template']}; nothing written")
        return "\n".join(lines)
    if counts["refused"] == "collision":
        lines.append(f"  refused: more than one page resolves to the same entry ({len(counts['collisions'])} "
                     "such entries; a page on the wrong table's entry reads back as verified); "
                     "nothing written")
        lines.extend("    " + line for line in counts["collisions"])
        return "\n".join(lines)
    if counts["holdout_questions"]:
        lines.append(f"  leak-checked against {counts['holdout_questions']} reserved "
                     "questions: clean")
    else:
        lines.append("  not leak-checked: no reserved questions given")
    verb = "would write" if counts["dry_run"] else "written"

    def tally(kind: str) -> tuple[list[dict], str]:
        entries = [e for e in result.entries if e["kind"] == kind]
        written = sum(1 for e in entries if e["status"] in ("written", "would write"))
        unchanged = sum(1 for e in entries if e["status"] == "unchanged")
        verified = sum(1 for e in entries if e["verify"] == "verified")
        failed = sum(1 for e in entries if _failed(e))
        return entries, (f"{verb} {written}  unchanged {unchanged}  "
                         f"verified {verified}  failed {failed}")

    pages, line = tally("page")
    note = ""
    if counts["replaced_unstamped"]:
        where = (f"previous bytes in {counts['previous_out']}" if counts["previous_out"]
                 else "previous bytes not kept: no --previous-out")
        note = f"   ({counts['replaced_unstamped']} replaced content not written by contextlift; {where})"
    lines.append(f"  overviews:        {line}{note}")
    if counts["terms"]:
        _, line = tally("term")
        lines.append(f"  terms:            {line}  ({counts['linked']} linked)")
        for note, count in _link_notes(result.entries):
            lines.append(f"    {count} not linked: {note}")
    if counts["examples"]:
        _, line = tally("example")
        home = f"  in {counts['examples_home']}" if counts["examples_home"] else ""
        lines.append(f"  verified queries: {line}{home}")
    if counts["drafts_skipped"]:
        lines.append(f"  {counts['drafts_skipped']} pages still carry the draft-context marker "
                     "and were not pushed (review them, or --allow-draft)")
    if counts["bare_skipped"]:
        lines.append(f"  {counts['bare_skipped']} files skipped: a bare table name has no "
                     "dataset to find the entry by")
    not_seen = sum(1 for e in result.entries if e["verify"] == "unverified")
    if not_seen:
        lines.append(f"  {not_seen} written and not verified yet; the catalog makes a write "
                     "readable seconds later: --verify-only re-reads the pages, and the same "
                     "command again re-reads the terms")
    if counts["over_budget"]:
        chars = {e["key"]: e["chars"] for e in pages}
        listed = ", ".join(f"{key} {chars.get(key, 0)}" for key in counts["over_budget"])
        lines.append(f"  {len(counts['over_budget'])} pages over {counts['read_budget']} "
                     f"characters, where a catalog read truncates: {listed}")
    for entry in result.entries:
        if _failed(entry):
            lines.append("  " + _failure_line(entry))
    return "\n".join(lines)


def _link_notes(entries: list[dict]) -> list[tuple[str, int]]:
    """(reason, how many terms) for the terms that were not linked to a
    table, commonest first. "(0 linked)" with no reason beside it is a
    number nobody can act on."""
    return Counter(entry["link_note"] for entry in entries
                   if entry["kind"] == "term" and entry.get("link_note")).most_common()


def _failure_line(entry: dict) -> str:
    text = f"{entry['kind']} {entry['key']}: {entry['status']}"
    # a read that raised sets both to "failed"; once is enough on the line
    if entry["verify"] and entry["verify"] != entry["status"]:
        text += f", {entry['verify']}"
    if entry.get("cause"):
        text += f"; {entry['cause']}"
    if entry["readback_chars"]:
        seen = ", ".join(str(n) for n in entry["readback_chars"])
        text += f"; read {entry['attempts']} times, saw {seen} characters"
    return text
