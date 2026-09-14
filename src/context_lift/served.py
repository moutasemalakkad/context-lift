# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""What a reader is actually served, against what the catalog stores.

Two different questions, and a push that only asks the first will report
success on a page nobody can read.

`entries.get` says what the catalog HOLDS. `lookupContext` says what a reader
GETS. They are not the same call and they do not agree: the aspect holds
100 KiB and cuts silently past that, and the served bundle is assembled from
several entries and can be cut by a budget. On one estate 572 of 71,690
characters came back at budgets of both 20,000 and 60,000.

The five states, from the design doc's section 3.5, each named for what a
person would do about it:

    FAILED_WRITE  the catalog does not hold what was written. Push again.
    LAG           held, but not served yet. Wait; it clears in seconds.
    SKELETON      served, with no overview at all. The reader gets nothing.
    FRAGMENT      served and cut. The reader gets part of a rule.
    WHOLE         served entire.

LAG is defined as observed, not as guessed: every lag seen has been the
aspect whole and 0 characters served, never the reverse. An earlier version
had it backwards and would have reported a successful write as a lag.

Measured on 2026-09-10 against `<project>.<dataset>`: one
table's bundle was 7,690 characters where its own overview was 101, and it
carried the table's schema, its `sampleQueries`, and - under
`relatedResources` - the DATASET entry's overview verbatim. So a rule written
to the dataset entry is served with every table in that dataset, which is
what makes the dataset entry a delivery path and not only storage.
"""
from __future__ import annotations

from dataclasses import dataclass

# The share of the stored page that must survive into the bundle. Below this
# the reader has part of a rule, which is worse than no rule: a rule cut in
# half can invert its own meaning ("exclude returns" -> "exclude").
KEPT_FRACTION = 0.95

OVERVIEW_KEY = "overview:"


@dataclass(frozen=True)
class Served:
    """One entry's served state, with the numbers that decided it."""

    table: str
    state: str
    stored_chars: int
    served_chars: int
    detail: str

    def line(self) -> str:
        return f"  {self.table}: {self.state} ({self.detail})"


def _collapse(text: str) -> str:
    """Whitespace flattened, so a re-indented page is not read as a cut.

    The bundle is YAML and indents the page it embeds. Comparing raw would
    call every served page a fragment.
    """
    return " ".join((text or "").split())


def classify(stored: str | None, served: str, *, expected: str | None = None) -> tuple[str, str]:
    """The state and the sentence explaining it, from two strings.

    Pure, so every state can be tested without a catalog. `expected` is what
    the push believes it wrote; leave it out to check only that what is held
    is what is served.
    """
    held = _collapse(stored or "")
    if expected is not None and held != _collapse(expected):
        return ("FAILED_WRITE",
                f"the catalog holds {len(held)} characters, not the "
                f"{len(_collapse(expected))} that were written")
    if not served.strip():
        return ("LAG", "held but not served yet; this clears in seconds")
    if OVERVIEW_KEY not in served:
        return ("SKELETON",
                "served with no overview at all, so a reader gets none of it")
    if not held:
        return ("WHOLE", "nothing was stored, and nothing is missing")

    flat = _collapse(served)
    kept = sum(1 for word in held.split() if word in flat)
    share = kept / max(len(held.split()), 1)
    if share < KEPT_FRACTION:
        return ("FRAGMENT",
                f"{share:.0%} of the stored page survived into the bundle; "
                "a rule cut in half can invert its own meaning")
    return ("WHOLE", f"{share:.0%} of the stored page is in the bundle")


def check(catalog, tables: list[str], expected: dict[str, str] | None = None) -> list[Served]:
    """The served state of each table, in name order.

    One `lookupContext` call per table with no budget, because a budget is
    the thing being tested for. Costs nothing documented; the Preview page
    lists no quota row.
    """
    out = []
    for table in sorted(tables):
        entry = catalog.resolve(table)
        if not entry:
            out.append(Served(table, "FAILED_WRITE", 0, 0,
                              "no catalog entry for it"))
            continue
        stored = catalog.stored_overview(entry) or ""
        served = catalog.transport.lookup(entry, 0)
        state, detail = classify(stored, served,
                                 expected=(expected or {}).get(table))
        out.append(Served(table, state, len(stored), len(served), detail))
    return out


def summary(states: list[Served]) -> str:
    """The states as lines, worst first, so the one to act on is at the top."""
    order = {"FAILED_WRITE": 0, "SKELETON": 1, "FRAGMENT": 2, "LAG": 3, "WHOLE": 4}
    ranked = sorted(states, key=lambda s: (order.get(s.state, 9), s.table))
    bad = [s for s in ranked if s.state != "WHOLE"]
    # How many carry a page at all, printed beside the states. "all 7 whole"
    # on a set where four store nothing reads as seven tables carrying
    # context, and an empty entry is whole in exactly the way an empty box is
    # packed.
    carrying = sum(1 for s in states if s.stored_chars)
    head = (f"served: all {len(states)} entry/entries whole" if not bad
            else f"served: {len(bad)} of {len(states)} entry/entries not whole")
    head += f"; {carrying} of {len(states)} carry an overview at all"
    return "\n".join([head] + [s.line() for s in ranked])
