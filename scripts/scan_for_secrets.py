#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Refuse a commit that carries a customer string.

A customer's material number reached a committed test this way: a question was
quoted verbatim out of a field report into an assertion, and nobody read it as
a customer value before it landed. Reading every string before committing is
not a control, because it failed the one time it mattered.

The patterns live OUTSIDE the repo, in `_personal/secret_patterns.txt`, one
regex per line, `#` for comments. That file is gitignored, so the list of a
customer's names never ships with the code that hides them. With no such file
this prints how to make one and passes, so a fresh clone is never blocked by a
control it cannot see.

    make scan          the staged changes, or the working tree if nothing is staged
    make scan ALL=1    every commit in history, which is slower
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
PATTERNS = REPO / "_personal" / "secret_patterns.txt"

EXAMPLE = """# One regex per line, case-insensitive. Lines starting with # are ignored.
# This file is gitignored: the names never ship with the code that hides them.
#
# acme-analytics
# \\bACME\\b
# view_xyz_
# 00000-00000000
"""


def patterns() -> list[re.Pattern]:
    if not PATTERNS.is_file():
        return []
    found = []
    for line in PATTERNS.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            found.append(re.compile(line, re.I))
    return found


def since_ref() -> str | None:
    """The `--since REF` argument, or None when it was not given."""
    if "--since" not in sys.argv:
        return None
    where = sys.argv.index("--since") + 1
    return sys.argv[where] if where < len(sys.argv) else None


def tree_hits(rules) -> list[tuple[str, str]]:
    """Every TRACKED FILE as it stands now, not as a diff.

    THE MODE THAT WAS MISSING, and the gap it left. The other three all read
    diffs. A file committed BEFORE the pattern file existed appears in no
    later diff, so it is invisible to the default and to `--since`, and
    `--all` buries it among history nobody can rewrite.

    On 2026-09-14 that gap was holding 48 occurrences of a customer's name
    across 16 tracked files, including two in `src/context_lift/` that ship
    inside the installed package. `make scan` reported clean the whole time
    and was telling the truth about the question it was asked.

    This is the mode to run before making a repository public: it answers
    "what does the code say RIGHT NOW", and every hit is fixable by editing
    a file, which is not true of a history hit.
    """
    listed = subprocess.run(["git", "ls-files"], cwd=REPO,
                            capture_output=True, text=True)
    if listed.returncode:
        raise SystemExit(
            f"cannot list tracked files: {listed.stderr.strip()[:200]}\n"
            f"A scan that cannot read the tree must not report clean.")
    found = []
    for name in listed.stdout.split():
        path = REPO / name
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue      # binary or unreadable: a .sqlite is not authored text
        for number, line in enumerate(text.splitlines(), 1):
            for rule in rules:
                if rule.search(line):
                    found.append((rule.pattern, f"{name}:{number}: {line.strip()[:100]}"))
                    break
    return found


def text_to_scan(scan_all: bool, since: str | None = None) -> str:
    """The diff this run should read. THREE MODES, and CI needs the third.

    `--all` reads the whole history. Right for a pre-release audit and
    USELESS AS A CI GATE on a repository whose history already carries the
    strings: on 2026-09-14 it found 101 lines, nearly all in commits made
    before the rename, and history cannot be rewritten because
    `refs/pull/N/head` keeps the old ones forever. A check that can never go
    green is one people learn to ignore.

    The default reads the staged diff, or the working tree when nothing is
    staged. Right before a commit on a developer's machine. IN CI IT READS
    NOTHING, because a fresh checkout has no staged changes and no dirty
    files, so it would pass while scanning zero lines -- the same silent pass
    the missing pattern file used to produce, and the reason this function
    grew a third mode rather than CI just dropping the flag.

    `--since REF` reads what this push or pull request ADDED. History cannot
    be fixed; nothing new has to carry a customer's name.
    """
    if scan_all:
        return subprocess.run(["git", "log", "--all", "-p", "--unified=0"],
                              cwd=REPO, capture_output=True, text=True).stdout
    if since:
        done = subprocess.run(["git", "diff", "--unified=0", f"{since}..HEAD"],
                              cwd=REPO, capture_output=True, text=True)
        if done.returncode:
            # A ref the runner cannot resolve is not an empty diff, and
            # reporting clean for one would be the exact failure this mode
            # exists to remove.
            raise SystemExit(
                f"cannot diff against {since!r}: {done.stderr.strip()[:200]}\n"
                f"A scan that cannot read its range must not report clean.")
        return done.stdout
    staged = subprocess.run(["git", "diff", "--cached"],
                            cwd=REPO, capture_output=True, text=True).stdout
    # nothing staged means the caller wants the working tree checked
    return staged or subprocess.run(["git", "diff", "HEAD"],
                                    cwd=REPO, capture_output=True, text=True).stdout


def main() -> int:
    scan_all = "--all" in sys.argv
    rules = patterns()
    if not rules:
        # relative_to raises when the path is outside the repo, which is every
        # test and anyone who moved the checkout
        try:
            where = PATTERNS.relative_to(REPO)
        except ValueError:
            where = PATTERNS
        print(f"no pattern file at {where}, nothing scanned.")
        print("to turn this on, write one regex per line there:")
        print(EXAMPLE)
        return 0

    since = since_ref()
    tree = "--tree" in sys.argv
    hits = []
    if tree:
        hits = tree_hits(rules)
    else:
        for line in text_to_scan(scan_all, since).splitlines():
            if not line.startswith("+") and not scan_all:
                continue                   # only what this change ADDS
            for rule in rules:
                if rule.search(line):
                    hits.append((rule.pattern, line.strip()[:120]))
                    break

    if not hits:
        where = ("every tracked file" if tree
                 else "history" if scan_all
                 else f"{since}..HEAD" if since else "this change")
        print(f"scanned {where} against {len(rules)} pattern(s): clean")
        return 0

    print(f"REFUSING: {len(hits)} line(s) match a customer pattern\n")
    for pattern, line in hits[:20]:
        print(f"  /{pattern}/  {line}")
    if len(hits) > 20:
        print(f"  ... and {len(hits) - 20} more")
    print("\nRewrite the value rather than committing it. A shape carries the "
          "mechanism; the estate does not need to come with it.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
