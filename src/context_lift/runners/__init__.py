# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""What answers a question: one seam, three implementations.

The generator's backend seam is `prompt -> text`: it fits a model that reads
a prompt. The agent this framework measures does not read a prompt. The
Conversational Analytics API takes a structured Context (table references,
descriptions, glossary terms, example queries) and answers with a stream
that carries the SQL, the BigQuery job it ran, the example it matched and
the sources it cited. None of that survives a `prompt -> text` seam.

So a Runner takes a question and an Arm and returns a Prediction. An Arm is
the whole context an arm gives the agent, hashed, so two arms differ only
where the hash differs. A Prediction keeps every field the round needs
later: the SQL to score, the job id to read rows from instead of paying
twice, the matched example (an answer served from a stored query is a
lookup, not text-to-SQL, and is counted apart), the citations, and the
tables the agent resolved (the strict table recall in the design doc).

Three runners: `ca_api` for the agent under test, `prompt` for the existing
backend path, and `csv` for predictions a customer made elsewhere. A fourth
slot, Gemini Enterprise, stays empty until two questions about it are
answered; see docs/kc-context-quality.md section 3.9.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Protocol

from ..executor import table_missing


@dataclass(frozen=True)
class Example:
    """One worked example: a question and the SQL that answers it."""
    question: str
    sql: str


@dataclass(frozen=True)
class Term:
    """One glossary term, as the agent receives it.

    `tier` is the same idea the rules filenames carry: an estate-wide
    definition of revenue is broader than one team's, and a catalog holds the
    first in an org glossary and the second in a team category. Default
    "org", because a term written with no tier stated is the estate's.

    `synonyms` and `related` are the structured form of what the definition
    string already says in prose. Both are needed, for different readers: the
    agent only ever sees the prose, and a catalog needs the list to write a
    real synonym or related link between two term entries.

    Neither `tier` nor the two lists is in `Arm.arm_id`. The hash covers
    `(name, definition)`, and the prose is where the agent's view lives -- so
    adding a tier to a term does not silently make it a different arm.
    """
    name: str
    definition: str
    tier: str = "org"
    synonyms: tuple[str, ...] = ()
    related: tuple[str, ...] = ()


def tables_that_are_gone(tables: list[str], executor) -> list[str]:
    """The tables in `tables` that no longer exist, by asking the warehouse.

    One dropped table ends a whole round. `ba_test.dim_gbl_sales_organization
    _product_season_test` was dropped from a warehouse after the analyst ran
    it; the gold passed the gold gate, and the Conversational Analytics API
    then answered the entire request with `404 Not found: Table ...`, so 136
    calls produced no answers at all. A warehouse always has dead tables in
    its history and a log-mined gold set will always name some.

    Free: a dry run measures a query without running it, and
    `SELECT 1 FROM t WHERE FALSE` reads nothing. Anything other than a
    missing-table error is left alone, because a 403 is not a dead table and
    dropping the table would hide the refusal.
    """
    gone = []
    for table in tables:
        try:
            executor.dry_run_bytes(f"SELECT 1 FROM `{table}` WHERE FALSE")
        except Exception as problem:  # noqa: BLE001 - the message is the answer
            if table_missing(str(problem)):
                gone.append(table)
    return gone


def without_tables(arm: "Arm", drop: list[str]) -> "Arm":
    """`arm` with `drop` removed from its tables and from every per-table map.

    A new arm, never an edit: the arm_id is a hash over these fields, and an
    arm quietly mutated after its id was printed is a run nobody can check.
    """
    if not drop:
        return arm
    dead = set(drop)
    keep = [t for t in arm.tables if t not in dead]
    def pruned(mapping):
        return {k: v for k, v in mapping.items() if k not in dead}
    return replace(
        arm, tables=keep,
        table_descriptions=pruned(arm.table_descriptions),
        field_descriptions=pruned(arm.field_descriptions),
        table_synonyms=pruned(arm.table_synonyms),
        field_synonyms=pruned(arm.field_synonyms),
        table_tags=pruned(arm.table_tags),
        field_tags=pruned(arm.field_tags))


@dataclass
class Arm:
    """The whole context one arm gives the agent, and nothing else.

    `arm_id` is a hash over every field below, so a run manifest can say
    which context produced which score, and two runs that claim to be the
    same arm can be proven so. Fields left empty are the bare arm.

    `table_descriptions` and `field_descriptions` carry what the estate
    already had, so the control arm can hold them too: the value being
    measured is the authored context above the descriptions, not the
    descriptions themselves (design doc 3.3).
    """
    label: str
    tables: list[str] = field(default_factory=list)
    table_descriptions: dict[str, str] = field(default_factory=dict)
    field_descriptions: dict[str, dict[str, str]] = field(default_factory=dict)
    # The Context carries `synonyms` and `tags` on a table and on each field,
    # beside the descriptions. Keyed the same way: table by
    # `project.dataset.table`, fields by table then column name. Kept apart
    # from the descriptions because they are a separate class the round can
    # price with leave-one-class-out, and because a first probe suggests the
    # two channels do not behave alike: a word placed only in a field's
    # synonyms did not resolve, while the same word in that field's
    # description did (2026-09-09, n=1, a lead and not a measurement).
    table_synonyms: dict[str, list[str]] = field(default_factory=dict)
    field_synonyms: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    table_tags: dict[str, list[str]] = field(default_factory=dict)
    field_tags: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    terms: list[Term] = field(default_factory=list)
    examples: list[Example] = field(default_factory=list)
    relationships: list[dict] = field(default_factory=list)
    system_instruction: str = ""
    model_alias: str = "LATEST_GA_MODEL"

    @property
    def arm_id(self) -> str:
        """A stable 12-character hash of everything the agent receives.

        Sorted and JSON-encoded, so a dict that was built in another order
        hashes the same. The label is not in the hash: renaming an arm must
        not make it a different arm.
        """
        payload = json.dumps(
            {
                "tables": sorted(self.tables),
                "table_descriptions": self.table_descriptions,
                "field_descriptions": self.field_descriptions,
                "table_synonyms": self.table_synonyms,
                "field_synonyms": self.field_synonyms,
                "table_tags": self.table_tags,
                "field_tags": self.field_tags,
                # (name, definition) only, deliberately. A term's tier and
                # its structured synonym/related lists change nothing the
                # agent receives -- the definition prose carries that -- so
                # putting them in the hash would rename every existing arm
                # for no change in what was measured.
                "terms": sorted((t.name, t.definition) for t in self.terms),
                "examples": sorted((e.question, e.sql) for e in self.examples),
                "relationships": self.relationships,
                "system_instruction": self.system_instruction,
                "model_alias": self.model_alias,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    def is_bare(self) -> bool:
        """True when the arm carries table references and nothing else.

        The bare arm is the sentinel probe's arm, never the control of a
        pair: on a customer estate the service may read the table and column
        descriptions on its own, so the control carries them too.
        """
        return not (self.table_descriptions or self.field_descriptions
                    or self.table_synonyms or self.field_synonyms
                    or self.table_tags or self.field_tags or self.terms
                    or self.examples or self.relationships or self.system_instruction)


@dataclass
class Prediction:
    """One answer, with everything the round reads later.

    `error_class` is set and `pred_sql` left None when the agent returned no
    SQL: a refusal and a wrong answer are different outcomes, and collapsing
    them hides which one happened.
    """
    question_id: str
    arm_id: str
    pred_sql: str | None = None
    job_id: str | None = None
    matched_example: str | None = None
    citations: list[str] = field(default_factory=list)
    tables_resolved: list[str] = field(default_factory=list)
    thought_chars: int = 0
    retries: int = 0
    error_class: str | None = None
    raw_path: str | None = None

    @property
    def is_lookup(self) -> bool:
        """True when a stored example answered this question verbatim.

        The agent executes a matched example as written, so the answer says
        nothing about writing SQL. Counted apart from generated accuracy.
        """
        return self.matched_example is not None


@dataclass
class CostSheet:
    """What a run will cost, printed before the first paid call."""
    calls: int
    minutes_at_quota: float
    usd_estimate: float | None
    basis: str

    def line(self) -> str:
        usd = "unknown" if self.usd_estimate is None else f"${self.usd_estimate:.2f}"
        return (f"{self.calls} calls, about {self.minutes_at_quota:.0f} minutes at the "
                f"quota, {usd} ({self.basis})")


class Runner(Protocol):
    """Answers one question under one arm."""

    name: str

    def cost_sheet(self, n_questions: int, n_arms: int, k: int) -> CostSheet: ...

    def answer(self, question_id: str, question: str, arm: Arm) -> Prediction: ...
