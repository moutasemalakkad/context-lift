# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The two confirmations a person must type, and nothing else may.

These live in the command-line tool rather than in any wrapper around it,
because that is the only place a guard cannot be walked around. The framework
is driven by a person, by Gemini CLI, or by Claude Code, and a host agent on
auto-approve would answer a prompt on the caller's behalf. So the question is
not asked of the caller at all: it is read from the controlling terminal.

Two of them:

    money      a paid verb prints its cost, then needs the word RUN
    holdout    reading the reserved questions again needs the workspace name

Two properties do the work.

`/dev/tty`, never stdin. Piped input can carry the word RUN, and
`echo RUN | contextlift ...` must not be a way to spend money. Reading the
controlling terminal means the answer came from a keyboard.

No terminal means no. A host agent, a cron job and a continuous-integration
runner all have no controlling terminal, so all three are refused rather than
trusted. `--yes` together with `--budget` is the deliberate way to say yes in
writing, and it is recorded with the argv that carried it, so a later reader
can see the guard was waived rather than passed.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field

# Typed exactly. No trimming beyond the newline, no case folding: a
# confirmation that accepts "run " or "Run" is one that can be given by
# accident.
RUN_WORD = "RUN"


def ask_tty(prompt: str) -> str:
    """One line from the controlling terminal, or "" when there is none.

    Deliberately not `input()`. Piped stdin can carry the word RUN, so
    `echo RUN | contextlift ...` would otherwise be a way to spend money without
    a person present. An empty string means no terminal, which every caller
    here treats as no.
    """
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(prompt)
            tty.flush()
            return tty.readline().rstrip("\n")
    except OSError:
        return ""


@dataclass
class Decision:
    """Whether the thing may proceed, and what to write in the run manifest.

    `waived` is the part that matters later. A run that went ahead because
    somebody passed `--yes` is not the same as one a person confirmed at a
    keyboard, and six months on nobody remembers which. The argv is kept so
    the answer is on the record rather than in someone's memory.
    """

    allowed: bool
    reason: str
    budget: float | None = None
    waived: bool = False
    argv: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        """`if decision:` means `if decision.allowed:`.

        A dataclass is truthy by default, so `if confirm_paid(...)` would
        proceed on a REFUSAL -- the object is not the answer, the field is.
        Every caller today reads `.allowed` and is correct; this exists so
        that the next one cannot be wrong in a way that spends money and
        opens a reserved set while reporting nothing.

        Found on 2026-09-12 by an agent testing whether the guard refused it,
        writing `if confirm_paid(...)` and reading "ALLOWED" from a Decision
        whose `allowed` was False. The guard was fine. The test was the bug,
        and it is exactly the bug a caller would write.
        """
        return self.allowed

    def manifest_entry(self) -> dict:
        """What the run manifest stores about this confirmation."""
        entry = {"allowed": self.allowed, "reason": self.reason,
                 "waived": self.waived}
        if self.budget is not None:
            entry["budget_usd"] = self.budget
        if self.waived and self.argv:
            entry["argv"] = "contextlift " + " ".join(shlex.quote(a) for a in self.argv)
        return entry


def confirm_paid(cost_line: str, budget: float | None = None,
                 yes: bool = False, argv: list[str] | None = None,
                 ask=ask_tty, out=None) -> Decision:
    """Before any paid call: show the cost, then require the word RUN.

    `--yes` alone is not enough. It has to come with `--budget`, because "yes"
    without a ceiling is a blank cheque, and the ceiling is the thing that
    stops a mistake being expensive rather than annoying.
    """
    argv = list(argv or [])
    if yes and budget is None:
        return Decision(
            False,
            "--yes needs --budget: a yes with no ceiling is a blank cheque",
            argv=argv)
    if yes:
        return Decision(True, f"--yes with a budget of ${budget:.2f}",
                        budget=budget, waived=True, argv=argv)

    if out is not None:
        print(cost_line, file=out)
    typed = ask(f"type {RUN_WORD} to start, or Enter to stop: ")
    if typed == RUN_WORD:
        return Decision(True, f"{RUN_WORD} typed at the terminal",
                        budget=budget, argv=argv)
    if typed == "":
        return Decision(
            False,
            "no controlling terminal and no --yes, so nothing was started. A "
            "host agent, a cron job and a CI runner all land here; pass --yes "
            "with --budget to say yes in writing",
            budget=budget, argv=argv)
    return Decision(False, f"{RUN_WORD} not typed, so nothing was started",
                    budget=budget, argv=argv)


def confirm_holdout_read(workspace_name: str, reads_so_far: int,
                         force: bool = False, argv: list[str] | None = None,
                         ask=ask_tty, out=None) -> Decision:
    """Before reading the reserved questions again: require the workspace name.

    The first read of a round needs nothing typed; that read is the point of
    the round. This is for a second one. The workspace name rather than a
    word like YES, because typing the name means reading which workspace is
    about to be spent, and a habit of typing YES is worth nothing.
    """
    argv = list(argv or [])
    if reads_so_far == 0:
        return Decision(True, "first read of this round", argv=argv)
    if force:
        return Decision(
            True,
            f"--force: read {reads_so_far + 1} times. The later reads were "
            "chosen knowing the earlier ones, so this is no longer a holdout",
            waived=True, argv=argv)

    if out is not None:
        print(f"These questions have already been scored {reads_so_far} time(s) "
              f"in this round. Score them again and the number becomes a "
              f"training score, permanently.", file=out)
    typed = ask(f"type the workspace name ({workspace_name}) to open them, "
                "or Enter to stop: ")
    if typed == workspace_name:
        return Decision(True, "workspace name typed at the terminal", argv=argv)
    if typed == "":
        return Decision(
            False,
            "no controlling terminal, so the reserved questions were not "
            "opened. Pass --force to spend them in writing",
            argv=argv)
    return Decision(False, "workspace name not typed, so they were not opened",
                    argv=argv)
