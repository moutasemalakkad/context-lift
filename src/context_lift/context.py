# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Where the model's business context comes from.

One file is the simple case and the only one the CLI could express. A real
deployment retrieves: a router picks a table and only that table's
documentation reaches the model, because the catalog does not fit in a prompt.
One estate measured here held 99 tables and about 1.8 million characters.

A source answers one question: given this question, what context should the
model see? `FileContextSource` returns the same text every time, which is
exactly today's `--context FILE` and stays byte for byte what it was.
`MappingContextSource` is what any retrieval system can already emit, a
`{question_id: text}` JSON file. `DirectoryContextSource` reads one markdown
file per `dataset.table`, which is how catalog content usually exists before it
is pushed to a catalog service at all.

Exports: ContextSource, DocumentStore, FileContextSource,
MappingContextSource, DirectoryContextSource, fetch_document,
retrieved_fingerprint.

Identity is the part that needs care. An arm is what the model saw, so a
retriever's arm cannot be named before it has retrieved: the same retriever
against a catalog that changed overnight is a different arm and nothing on disk
would say so. `retrieved_fingerprint` hashes the text actually handed to each
question, so a changed catalog becomes a new arm instead of quietly polluting
an old one.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Protocol


class ContextSource(Protocol):
    """Anything that can say what context one question should see.

    Structural, like `Executor`: a class satisfies this by having a matching
    method, with no import of or inheritance from this module, so a retriever
    living in someone else's codebase qualifies without depending on contextlift.
    """

    def for_question(self, question_id: str, question: str) -> str:
        ...


class FileContextSource:
    """One file, the same text for every question - today's `--context FILE`.

    Kept as a source rather than a special case so that the file path and a
    retriever go through the same code, and a comparison between them is a
    comparison of two arms rather than of two code paths.
    """

    def __init__(self, path: str | pathlib.Path):
        self.path = pathlib.Path(path)
        self.text = self.path.read_text()

    def for_question(self, question_id: str, question: str) -> str:
        return self.text


class MappingContextSource:
    """`{question_id: context_text}`, which is what a retriever already emits.

    A question with no entry gets nothing rather than an error: a retriever
    that finds nothing for a question is a normal outcome, and scoring it as
    written is the measurement.
    """

    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping

    @classmethod
    def from_json(cls, path: str | pathlib.Path) -> "MappingContextSource":
        data = json.loads(pathlib.Path(path).read_text())
        if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
            raise SystemExit(f"{path}: expected a JSON object of "
                             '{"question_id": "context text"}')
        return cls(data)

    def for_question(self, question_id: str, question: str) -> str:
        return self.mapping.get(question_id, "")


class DirectoryContextSource:
    """One markdown file per table, picked by a routing table.

    `routes` maps a question id to `dataset.table`; the file is looked up as
    `<dir>/<dataset>.<table>.md`, then `<dataset>__<table>.md`, then
    `<table>.md`. Several tables for one question are concatenated in the order
    given, separated by a blank line.

    This is the shape catalog content usually has before it reaches a catalog
    service, so it needs no credentials and no network, which means the
    protocol is exercised by the offline test suite rather than only by whoever
    has a warehouse.
    """

    def __init__(self, directory: str | pathlib.Path, routes: dict[str, list[str]]):
        self.directory = pathlib.Path(directory)
        self.routes = routes

    def resolve(self, key: str) -> str | None:
        """The file that holds this table's documentation, or None.

        Trivial here, and that is the point: a directory resolves by filename,
        so this implementation cannot show that resolution is a step at all. A
        catalog has to search for it, and the search can find nothing.
        """
        dataset, _, name = key.rpartition(".")
        for candidate in (f"{key}.md", f"{dataset}__{name}.md", f"{name}.md"):
            if (self.directory / candidate).is_file():
                return candidate
        return None

    def fetch(self, document_id: str) -> str:
        return (self.directory / document_id).read_text().strip()

    def _read(self, table: str) -> str:
        return fetch_document(self, table)

    def for_question(self, question_id: str, question: str) -> str:
        found = [self._read(t) for t in self.routes.get(question_id, [])]
        return "\n\n".join(text for text in found if text)


class DocumentStore(Protocol):
    """Where documents live, keyed by something a person names.

    Two steps, not one. `resolve` turns a human-facing key - a
    `dataset.table` - into whatever the store calls that document, which for a
    catalog means a search that can legitimately find nothing. `fetch` returns
    the text. Stores where the two ids are the same implement `resolve` as
    identity; a directory resolves by filename, which is why the offline
    implementation cannot show you that the step is needed at all.
    """

    def resolve(self, key: str) -> str | None:
        ...

    def fetch(self, document_id: str) -> str:
        ...


def fetch_document(store: DocumentStore, key: str, *, attempts: int = 3,
                   min_good_chars: int = 1, sleep=None) -> str:
    """One document, retrying while the store hands back something degraded.

    A store that has just been written to, or is being read in a burst, returns
    a short or empty body and no error. Those two conditions, and not general
    flakiness: 60 sequential reads of four documents from a warm live catalog
    came back byte-identical, so a steady-state read is stable and this loop
    costs one comparison. Three rules, each learned from a real failure:

    - The threshold is absolute, not a comparison with a previous value: on a
      first fetch there is nothing to compare against. `min_good_chars`
      defaults to 1, which means "empty is degraded" and nothing more, because
      how long a real document is depends on the store. An adapter whose
      documents are tens of kilobytes should raise it - a body far under that
      is a stub, and a writer fed a stub invents column names.
    - On exhaustion, return the longest body seen rather than nothing. Partial
      context beats none.
    - Only a good body is cached. A cache that stores a degraded one pins it for
      the life of the process, so a single bad burst poisons every later
      question. This is why the cache is written here and not by an lru_cache
      wrapper, which would happily keep the stub.

    Written by hand rather than with tenacity: the retry and the backoff are the
    third of this a retry library would cover, and the other two thirds - keep
    the longest degraded body, cache only a good one - are the reason the loop
    exists. The package also declares no dependencies, and a third of twelve
    lines is a poor reason to break that.
    """
    # No lock: dict get/set is atomic under the GIL, so concurrent callers can
    # duplicate a fetch but cannot corrupt the cache. Duplicating a fetch is
    # cheaper than serialising every retrieval behind one.
    cache = getattr(store, "_document_cache", None)
    if cache is None:
        cache = {}
        try:
            store._document_cache = cache
        except AttributeError:      # a store that forbids attributes still works
            pass
    if key in cache:
        return cache[key]
    document_id = store.resolve(key)
    if not document_id:
        return ""
    sleeper = sleep if sleep is not None else _sleep
    best = ""
    for attempt in range(max(1, attempts)):
        if attempt:
            sleeper(2.0 * attempt)          # linear backoff
        body = store.fetch(document_id)
        if len(body) >= min_good_chars:
            cache[key] = body
            return body
        if len(body) > len(best):
            best = body
    return best                             # degraded: returned, never cached


def _sleep(seconds: float) -> None:
    import time
    time.sleep(seconds)


def retrieved_fingerprint(retrieved: dict[str, str]) -> str | None:
    """A hash of what the model actually saw, or None when nothing was retrieved.

    Sorted by question id so the order a retriever happened to return in does
    not change the arm. None when every question got an empty string, so an
    arm that retrieved nothing is the same arm as one with no retriever at all,
    which is what it is.
    """
    if not any(retrieved.values()):
        return None
    digest = hashlib.sha256()
    for question_id in sorted(retrieved):
        digest.update(question_id.encode())
        digest.update(b"\0")
        digest.update(retrieved[question_id].encode())
        digest.update(b"\0")
    return digest.hexdigest()
