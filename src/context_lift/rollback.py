# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Putting a catalog back the way it was, which the catalog cannot do itself.

Knowledge Catalog has no aspect versions, no etag, no diff and no undo. An
update replaces an aspect whole and the previous bytes are gone. So a rollback
is not a catalog operation at all: it is pushing the old content again, and it
only works if somebody saved the old content first.

That is the whole design. `spec-push --previous-out DIR` reads each entry
BEFORE it writes and saves what was there. `catalog-rollback --previous DIR`
writes those bytes back.

**The dangerous case, and why this refuses instead of being clever.** Between
the push and the rollback, somebody else may have edited an entry: a
colleague in the console, a documentation scan, a `CREATE OR REPLACE TABLE`
that dropped the aspects. Pushing the saved bytes then erases their work as
well as ours, silently, and the catalog keeps no record that it happened. So
every saved file records what the entry held when it was saved AND what this
tool wrote next; at rollback time the live content is compared against what
we wrote, and anything else stops that table with its name printed.

Refusing one table rather than the whole run, because on a seven-table push
the one somebody else touched is exactly the one worth looking at, and
refusing everything would make a person roll back by hand.
"""
from __future__ import annotations

import datetime
import json
import pathlib
from dataclasses import dataclass, field


def _safe_name(table: str) -> str:
    """A table name as a filename. Dots and slashes both become underscores."""
    return table.replace("/", "_").replace(".", "_") + ".json"


def save(directory: str | pathlib.Path, table: str, entry: str,
         before: dict, wrote: dict) -> pathlib.Path:
    """What one entry held before a push, and what the push put there.

    Both, not just the first. Without `wrote` a rollback cannot tell "nobody
    touched this since" from "somebody replaced it with something else", and
    those need opposite answers.
    """
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _safe_name(table)
    path.write_text(json.dumps({
        "table": table,
        "entry": entry,
        "saved_at": datetime.datetime.now(datetime.timezone.utc)
                    .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "before": before,
        "wrote": wrote,
    }, indent=1, sort_keys=True), encoding="utf-8")
    return path


def load(directory: str | pathlib.Path) -> list[dict]:
    """Every saved record in a directory, in table-name order."""
    directory = pathlib.Path(directory)
    if not directory.is_dir():
        raise SystemExit(
            f"no saved content at {directory}. A rollback needs what the push "
            "replaced, and the catalog keeps no version of its own. Push with "
            "--previous-out DIR next time")
    out = []
    for path in sorted(directory.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except ValueError as broken:
            raise SystemExit(f"{path} is not readable: {broken}") from broken
    if not out:
        # A directory with no records USED TO return [], so the rollback
        # printed its plan with nothing in it and exited 0. That reads as "the
        # catalog is already back" when it means "I could not find what to put
        # back", and those need opposite responses from a person.
        #
        # The way to arrive here by accident: `context-write --previous-out`
        # takes a FILE and writes the previous rule text as markdown, while
        # this takes a DIRECTORY of .json. Point a rollback at a directory of
        # markdown and every entry stays overwritten, with an exit code of 0.
        others = sorted(p.name for p in directory.iterdir() if p.is_file())
        raise SystemExit(
            f"{directory} holds no rollback records (*.json)"
            + (f", only: {', '.join(others[:5])}" if others else " and is empty")
            + ". Nothing was restored. `spec-push --previous-out DIR` writes "
              "these; `context-write --previous-out FILE` writes one markdown "
              "file and is not a rollback source.")
    return out


@dataclass
class Plan:
    """What a rollback would do, per table, and what it refuses."""

    restore: list[tuple[str, dict]] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    def text(self) -> str:
        lines = []
        for table, _content in self.restore:
            lines.append(f"  restore {table}")
        for table in self.unchanged:
            lines.append(f"  {table}: already holds what was saved, nothing to do")
        for table, why in self.refused:
            lines.append(f"  REFUSED {table}: {why}")
        if not lines:
            return "nothing to roll back"
        return "\n".join(lines)


def _same(a, b) -> bool:
    """Whitespace-insensitive for text, exact for everything else."""
    if isinstance(a, str) and isinstance(b, str):
        return " ".join(a.split()) == " ".join(b.split())
    return a == b


def plan(records: list[dict], live: dict[str, dict]) -> Plan:
    """What to restore, given what is live now.

    `live` is `{table: {"overview": ..., "queries": [...]}}` as read now.

    Three outcomes per table. It still holds what we wrote, so restoring is
    safe. It already holds what was saved, so there is nothing to do and
    saying so is better than writing the same bytes again. Or it holds
    something else, and somebody edited it after the push: refused by name,
    because pushing over it would erase their work with no record anywhere.
    """
    out = Plan()
    for record in records:
        table = record.get("table", "?")
        now = live.get(table)
        if now is None:
            out.refused.append((table, "it has no catalog entry now"))
            continue
        before, wrote = record.get("before") or {}, record.get("wrote") or {}
        if all(_same(now.get(key), before.get(key)) for key in ("overview", "queries")):
            out.unchanged.append(table)
            continue
        changed = [key for key in ("overview", "queries")
                   if not _same(now.get(key), wrote.get(key))]
        if changed:
            out.refused.append((
                table,
                f"its {', '.join(changed)} is not what this tool wrote, so "
                "somebody edited it after the push. Restoring would erase "
                "their work too, and the catalog would keep no record of it"))
            continue
        out.restore.append((table, before))
    return out
