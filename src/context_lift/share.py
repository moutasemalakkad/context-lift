# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The only thing that leaves a customer's machine.

A workspace holds the customer's questions, their SQL, their table names and
the answers a model gave about their data. None of it may be pasted into a
chat, a ticket or a slide. What a colleague actually needs is the shape of the
result: how many questions, how many moved, which way, how sure, and which
gate failed.

Two controls, and the order matters.

**An allowlist, not a blocklist.** The payload is built by naming every field
that may leave. Nothing is removed, because removal only works against the
fields somebody thought of, and the field added next month is the one that
leaks. A key not named here does not travel, whatever it holds.

**Then a scan of what was built.** Even an allowlisted number can carry a name:
a table name inside a gate's detail sentence, a project id inside an error
string. So the finished payload is searched for every real name and every
forbidden string, and one hit refuses the whole export. Belt after braces,
because the first control is code and code has bugs.

Names are replaced by stable aliases from a map that stays on the machine. It
is stable so two shares of the same estate compare: `table_1` is the same table
in March and in June. It stays local because a map from `table_1` back to a
customer's real table is the leak the aliases were meant to prevent.
"""
from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field

# Every key that may leave, and nothing else. Adding one is a deliberate act
# with a reason beside it, which is the whole point of writing them out.
ALLOWED_ROUND_KEYS = (
    "verdict",            # the one word
    "reason",             # why that word; scanned like everything else
    "lift_pp",            # the paired difference, in points
    "ci_low_pp",
    "ci_high_pp",
    "p_value",
    "gap_pp",             # working-set lift minus held-out lift
    "gap_ci_low_pp",
    "gap_ci_high_pp",
    "mcnemar_b",          # how many the context broke
    "mcnemar_c",          # how many it fixed
    "n_curate",
    "n_holdout",
    "n_scored_curate",    # how many the number actually rests on
    "n_scored_holdout",
    # Counts, so they carry no question text and no SQL. They travel because
    # "20 of 23 answers were lookups" changes what a lift means, and a
    # colleague reading the lift alone would not know to ask.
    "lookups_curate",
    "lookups_holdout",
    "k_repeats",
    "holdout_reads",
    "resolution",
    "questions_needed",
    "target_pp",
    "model_alias",        # an alias, not a customer string
    "arm_control_hash",
    "arm_treatment_hash",
)

# Per table, the same rule. No table name: the alias goes in a separate field.
ALLOWED_TABLE_KEYS = (
    "table_lift_pp",
    "table_ci_low_pp",
    "table_ci_high_pp",
    "n_questions",
    "recall_misses",
    "column_count",
    "described_column_count",
    "overview_chars",     # the LENGTH of the page, never the page
    "example_count",
    "term_count",
)

_WORD = re.compile(r"[A-Za-z0-9_.\-]{3,}")


@dataclass
class Pseudonyms:
    """Real name to stable alias. Never leaves the machine.

    Stable so two shares of the same estate can be compared: `table_1` has to
    be the same table in March and in June, or a colleague reading both learns
    nothing from the pair.
    """

    mapping: dict[str, str] = field(default_factory=dict)
    prefix: str = "table"

    def alias(self, real: str) -> str:
        """The alias for one real name, made on first sight and kept."""
        if real not in self.mapping:
            self.mapping[real] = f"{self.prefix}_{len(self.mapping) + 1}"
        return self.mapping[real]

    @property
    def real_names(self) -> list[str]:
        """Every real name seen, longest first.

        Longest first because `p.d.orders` contains `orders`, and finding the
        longer one first keeps a report readable when a name is redacted.
        """
        return sorted(self.mapping, key=len, reverse=True)

    def save(self, path: str | pathlib.Path) -> None:
        pathlib.Path(path).write_text(
            json.dumps(self.mapping, indent=1, sort_keys=True), encoding="utf-8")

    @classmethod
    def load(cls, path: str | pathlib.Path, prefix: str = "table") -> "Pseudonyms":
        p = pathlib.Path(path)
        if not p.is_file():
            return cls(prefix=prefix)
        return cls(mapping=json.loads(p.read_text(encoding="utf-8")), prefix=prefix)


def _pick(source: dict, allowed: tuple[str, ...]) -> dict:
    """Only the named keys, only when present. Everything else stays home."""
    return {key: source[key] for key in allowed if key in source}


def build(round_report: dict, tables: dict[str, dict] | None = None,
          gates: list[tuple[str, bool, str]] | None = None,
          names: Pseudonyms | None = None) -> dict:
    """The shareable payload for one round.

    `round_report` and each table's report are filtered to the allowlists.
    Gates travel as name and pass or fail. Their detail sentences do NOT: a
    detail is written for a person at the keyboard and quotes table names,
    column names and question text.
    """
    names = names or Pseudonyms()
    payload: dict = {"round": _pick(round_report, ALLOWED_ROUND_KEYS)}

    payload["tables"] = [
        dict(_pick(report, ALLOWED_TABLE_KEYS), table=names.alias(real))
        for real, report in sorted((tables or {}).items())
    ]

    # Name and outcome only. The reason a gate failed is for the person who can
    # act on it, at the keyboard, where the customer's strings are allowed.
    payload["gates"] = [{"name": name, "passed": passed}
                        for name, passed, _detail in (gates or [])]
    return payload


def find_leaks(payload: dict, forbidden: list[str]) -> list[str]:
    """Every forbidden string that appears anywhere in the built payload.

    The second control. The first one is the allowlist above, and this exists
    because an allowlisted field can still carry a name: a table inside an
    error string, a project id inside a reason. Matching is case-insensitive
    and substring-based, so `ACME-PROD` is caught by `acme-prod`.
    """
    text = json.dumps(payload).lower()
    return sorted({term for term in forbidden
                   if term and term.lower() in text})


def export(round_report: dict, tables: dict[str, dict] | None = None,
           gates: list[tuple[str, bool, str]] | None = None,
           names: Pseudonyms | None = None,
           extra_forbidden: list[str] | None = None) -> dict:
    """Build the payload and refuse it if anything forbidden survived.

    Raises SystemExit naming what was found. Refusing the whole export rather
    than redacting the hit, because a payload that needed redacting was built
    by code that does not understand its own inputs, and the next one will be
    wrong in a way nobody notices.
    """
    names = names or Pseudonyms()
    payload = build(round_report, tables, gates, names)
    forbidden = list(names.real_names) + list(extra_forbidden or [])
    found = find_leaks(payload, forbidden)
    if found:
        raise SystemExit(
            "refusing to export: the payload still contains "
            + ", ".join(found)
            + ". Nothing was written. This is a bug in what was allowed out, "
              "not something to redact by hand")
    return payload


def redact(text: str, names: Pseudonyms) -> str:
    """Swap real names for their aliases inside one string.

    For a note a person wants to paste, not for the payload: the payload does
    not carry prose at all. Longest name first, so `p.d.orders` is replaced
    before `orders` can match inside it.
    """
    for real in names.real_names:
        text = text.replace(real, names.mapping[real])
    return text


def summary(payload: dict) -> str:
    """The payload as lines a person can read before sending it.

    Printed so nobody sends a file they have not looked at.
    """
    round_report = payload.get("round", {})
    lines = [f"verdict: {round_report.get('verdict', 'none')}"]
    for key in ("lift_pp", "p_value", "n_holdout", "resolution"):
        if key in round_report:
            lines.append(f"  {key}: {round_report[key]}")
    lines.append(f"  {len(payload.get('tables', []))} table(s), by alias")
    failed = [g["name"] for g in payload.get("gates", []) if not g["passed"]]
    lines.append("  gates: all passed" if not failed
                 else f"  gates failed: {', '.join(failed)}")
    lines.append("  no question text, no SQL, no real table names")
    return "\n".join(lines)
