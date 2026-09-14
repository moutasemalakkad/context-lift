# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""How many times each held-out question has been scored.

THE GUARD THIS FEEDS, AND WHY IT NEVER FIRED. `gates.holdout_reads(n)` exists
to catch the thing that quietly destroys a held-out set: scoring it again.
Read the reserved questions three times and pick the best run, and they are no
longer reserved -- they are a working set with a p-value attached.

`round.build_round` took `holdout_reads_so_far: int = 0` and NOTHING EVER
PASSED IT. So the gate computed `0 + 1 = 1` on every round and the run record
wrote the literal `"holdout_reads": 1`. A guard that cannot count is a guard
that cannot fire, and this one had been reporting "first read" for every read
since it was written.

WHY A COUNT AND NOT A SET. `bisect.record_spent` already keeps a SET of ids
that may never be reserved again -- the right shape for "this question is
burned". This is the other question: not whether a question is spent, but how
many times the reserved set has been looked at, because the answer changes
what a p-value from it is worth. A set cannot answer that.

The file is JSON, `{"question_id": count}`, and it ACCUMULATES. Forgetting an
earlier round would silently un-read it, which is the failure the file exists
to prevent.
"""
from __future__ import annotations

import json
import pathlib


def read_ledger(path: str | pathlib.Path | None) -> dict[str, int]:
    """Counts so far, or {} when there is no file yet.

    A missing file is an empty ledger rather than an error: the first round on
    a gold set has no history, and refusing to run until somebody creates an
    empty file would be a guard that only annoys.
    """
    if path is None:
        return {}
    found = pathlib.Path(path)
    if not found.is_file():
        return {}
    try:
        loaded = json.loads(found.read_text(encoding="utf-8"))
    except ValueError as broken:
        # Reporting zero reads for a file we could not parse is the exact
        # failure this ledger exists to stop, so it refuses instead.
        raise SystemExit(
            f"{found} is not readable as a holdout ledger: {broken}. "
            f"A round must not treat an unreadable history as no history.")
    if not isinstance(loaded, dict):
        raise SystemExit(
            f"{found} holds {type(loaded).__name__}, not an object of "
            f"id -> count. Refusing to read it as an empty history.")
    return {str(k): int(v) for k, v in loaded.items()}


def prior_reads(ledger: dict[str, int], ids: list[str]) -> int:
    """How many times this reserved set has already been scored.

    THE MAXIMUM over the ids, not the mean and not the minimum. One question
    read four times contaminates the set it belongs to; averaging that away
    against fresh questions is how a contaminated set passes as clean.
    """
    return max((ledger.get(str(i), 0) for i in ids), default=0)


def record_reads(path: str | pathlib.Path, ids: list[str],
                 ledger: dict[str, int] | None = None) -> dict[str, int]:
    """Add one read for each id and write the file. Returns the new counts.

    Written even when the round ends in NOT PROVEN or fails a gate after
    scoring. The questions were read either way, and a read that goes
    unrecorded is the leak.
    """
    counts = dict(read_ledger(path) if ledger is None else ledger)
    for one in ids:
        counts[str(one)] = counts.get(str(one), 0) + 1
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(dict(sorted(counts.items())), indent=1) + "\n",
        encoding="utf-8")
    return counts


def reads_note(ledger: dict[str, int], ids: list[str]) -> str:
    """One line a person can act on, or "" when the set is untouched."""
    counts = [ledger.get(str(i), 0) for i in ids]
    seen = [c for c in counts if c]
    if not seen:
        return ""
    return (f"{len(seen)} of {len(ids)} reserved question(s) have been scored "
            f"before, up to {max(seen)} time(s). A lift measured on questions "
            f"already read is not a held-out lift.")
