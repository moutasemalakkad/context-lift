# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""What must not appear in a file that ships, in one place.

THE PROBLEM THIS SOLVES, and it is a little absurd. Four test files each held
the same tuple of banned names, and one of those names was a CUSTOMER's. So
the guard against shipping a customer's name shipped it four times, and
`make scan --tree` reported the guards as leaks. They were right to.

THE SPLIT. Two kinds of name are banned and they are not equally sensitive:

- **Our own project ids** (`moutasem-akkad`, `kc_eval_room`, and the rest).
  Embarrassing in a public repository, not confidential. They stay literals
  here, so the check still runs in a fresh clone with no extra setup.
- **A customer's names.** These live ONLY in `_personal/secret_patterns.txt`,
  which is gitignored, and they are read from there. When that file is absent
  the customer half of the check is skipped and the test says so, rather than
  passing quietly and reading as proof.

`make scan TREE=1` is the check that covers the customer half properly,
because it runs where the pattern file exists.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Our own, not a customer's. Safe to keep in the repository.
OURS = ("moutasem-akkad", "kc_eval_room", "kc_agent_metrics",
        "context_lift_bird")


def customer_patterns() -> list:
    """Compiled patterns from the gitignored file, or [] when it is absent."""
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import scan_for_secrets

    return scan_for_secrets.patterns()


def leaks_in(body: str) -> list[str]:
    """Every banned name this text carries, ours and the customer's.

    Takes the text already lowercased or not -- the customer patterns compile
    case-insensitively and the `OURS` entries are compared in lower case.
    """
    lowered = body.lower()
    found = [name for name in OURS if name in lowered]
    found += [rule.pattern for rule in customer_patterns() if rule.search(body)]
    return found


def customer_half_ran() -> bool:
    """Whether the customer patterns were available to this run.

    A test that cannot check the customer half must not report that it did.
    """
    return bool(customer_patterns())
