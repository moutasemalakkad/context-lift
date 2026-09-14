# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The record that makes a number defensible after the fact.

Knowledge Catalog has no aspect versions, no etag, no diff and no rollback.
An update replaces an aspect whole. So "this table's context was worth +12
points" is a claim about bytes that may no longer exist, and nothing in the
catalog can tell a reader whether they still do.

The provenance aspect fixes exactly that and nothing else. It records the
content hash of every item the round measured. A reader hashes the page as it
stands now and compares. Different hash, stale number, and the tool says so
instead of showing it as current.

**Numbers and hashes only. Never a question, never SQL.** Any principal with
`roles/dataplex.catalogViewer` can read a custom aspect with
`entries.get view=ALL`, and the Knowledge Catalog MCP server's `lookup_entry`
is documented to return them to an agent. A gold question in an aspect is a
gold question in somebody's prompt. `check_payload` enforces this by refusing
a payload with a long free-text field rather than by trusting the caller.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

ASPECT_TYPE = "context-provenance"

# The field types the API accepts, probed on 2026-09-10 because no page this
# study read lists them. `integer`, `number` and `boolean` are all rejected
# with "No enum constant ... DataType.INTEGER"; the ones below are accepted.
# Field indexes are immutable once set, so this list is only ever APPENDED to.
FIELDS = (
    ("round_id", "string"),
    ("measured_at", "datetime"),
    ("runner", "string"),
    ("model_alias", "string"),
    ("arm_control_hash", "string"),
    ("arm_treatment_hash", "string"),
    ("overview_hash", "string"),
    ("queries_hash", "string"),
    ("served_state", "string"),
    ("k_repeats", "int"),
    ("table_lift_pp", "double"),
    ("regressions", "int"),
)

# Every key the aspect may hold. An allowlist, like `share.py`, because
# removal only works against the fields somebody thought of and the field
# added next month is the one that leaks a question.
ALLOWED_KEYS = (
    "round_id", "measured_at", "runner", "model_alias",
    "arm_control_hash", "arm_treatment_hash",
    "overview_hash", "queries_hash", "terms_hash",
    "question_set_hash", "schema_hash", "k_repeats",
    "table_lift_pp", "table_ci_low_pp", "table_ci_high_pp",
    "regressions", "recall_misses", "served_state", "gates",
)

# Anything longer than this in a string field is prose, and prose is how a
# question or a SQL fragment gets in. Hashes are 64 characters; a state is a
# word; a timestamp is 20.
MAX_STRING = 80


def content_hash(text: str | None) -> str:
    """The hash a reader recomputes to see whether a number is still current.

    Whitespace is flattened first, so re-indenting a page does not make every
    number on it read as stale. A rule that changed a word does change the
    hash, which is the point.
    """
    return hashlib.sha256(" ".join((text or "").split()).encode()).hexdigest()


@dataclass
class Provenance:
    """One table's record of one round. Numbers and hashes only."""

    round_id: str
    measured_at: str
    arm_control_hash: str
    arm_treatment_hash: str
    overview_hash: str = ""
    queries_hash: str = ""
    served_state: str = "NOT_CHECKED"
    model_alias: str = ""
    runner: str = "CA_API"
    k_repeats: int = 1
    gates: list[dict] = field(default_factory=list)

    def payload(self) -> dict:
        """The aspect body, filtered to the allowlist and checked."""
        body = {key: value for key, value in self.__dict__.items()
                if key in ALLOWED_KEYS and value not in ("", None, [])}
        check_payload(body)
        return body


def check_payload(body: dict) -> None:
    """Refuse a payload that could carry a question or a SQL fragment.

    Raises SystemExit naming the field. Refusing rather than truncating,
    because a payload that needed truncating was built by code that does not
    understand its own inputs, and the next one will be wrong in a way nobody
    notices.
    """
    for key, value in body.items():
        if key not in ALLOWED_KEYS:
            raise SystemExit(
                f"provenance aspect: {key!r} is not on the allowlist. Every "
                "field is named deliberately because any catalogViewer can "
                "read this aspect and an agent can read it through MCP")
        if isinstance(value, str) and len(value) > MAX_STRING:
            raise SystemExit(
                f"provenance aspect: {key!r} holds {len(value)} characters. "
                f"Anything over {MAX_STRING} is prose, and prose is how a "
                "question or a SQL fragment gets into a record any "
                "catalogViewer can read")
        if isinstance(value, list):
            for row in value:
                if isinstance(row, dict):
                    for inner, text in row.items():
                        if isinstance(text, str) and len(text) > MAX_STRING:
                            raise SystemExit(
                                f"provenance aspect: {key}[].{inner} holds "
                                f"{len(text)} characters, over the "
                                f"{MAX_STRING} allowed. A gate's detail "
                                "sentence quotes table and column names; send "
                                "the name and the outcome, not the sentence")


def gate_rows(gates) -> list[dict]:
    """Gates as name and outcome. The detail sentence stays home.

    A detail is written for the person at the keyboard and quotes table
    names, column names and question text, which is exactly what may not go
    into an aspect.
    """
    rows = []
    for gate in gates or []:
        name, passed = (gate[0], gate[1]) if isinstance(gate, tuple) else (gate.name, gate.passed)
        rows.append({"name": name[:MAX_STRING], "passed": bool(passed)})
    return rows


def staleness(recorded: dict, page_now: str | None) -> tuple[bool, str]:
    """Is the number this aspect records still about what is there now?

    Returns (current, sentence). A recorded hash with no page to compare
    against is stale, not current: the page it measured is gone.
    """
    was = str(recorded.get("overview_hash") or "")
    if not was:
        return (True, "no page hash was recorded, so nothing can be compared")
    now = content_hash(page_now)
    if was == now:
        return (True, f"measured at {was[:12]} and the page still hashes to it")
    if not (page_now or "").strip():
        return (False, f"measured at {was[:12]}; there is no page there now, "
                       "so the number is about text that is gone")
    return (False, f"measured at {was[:12]}, now {now[:12]}: stale. The number "
                   "is about text that has since changed")


def normalise(body: dict | None) -> dict:
    """An aspect as it was written, from an aspect as it reads back.

    A custom aspect is stored in a protobuf Struct, and every number in a
    Struct is a double. So `k_repeats=3` goes in as an int and comes back as
    3.0, and code that compares it to 3 fails on a value that never changed.
    Measured on 2026-09-10: written `3`, read back `3.0`.

    The declared type in FIELDS is the truth, so the coercion is driven by
    that rather than by guessing from the value. A string field is left
    alone: a hash that happens to be all digits must not become a number.
    """
    if not body:
        return {}
    declared = dict(FIELDS)
    out = {}
    for key, value in body.items():
        kind = declared.get(key)
        if kind == "int" and value is not None:
            out[key] = int(float(value))
        elif kind == "double" and value is not None:
            out[key] = float(value)
        else:
            out[key] = value
    return out
