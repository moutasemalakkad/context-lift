# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The two confirmations a person must type, and nothing else may."""
from __future__ import annotations

import io

from context_lift.confirm import RUN_WORD, confirm_holdout_read, confirm_paid

COST = "300 calls, about 10 minutes at the quota, $2.40"


def typed(answer: str):
    """A terminal that answers with `answer`. "" means there is no terminal."""
    return lambda prompt: answer


def test_typing_run_starts_a_paid_run_and_the_cost_was_shown_first():
    out = io.StringIO()

    decision = confirm_paid(COST, budget=5.0, ask=typed(RUN_WORD), out=out)

    assert decision.allowed
    assert COST in out.getvalue(), "the cost is shown before the question"
    assert not decision.waived


def test_no_terminal_and_no_yes_refuses():
    """A host agent, a cron job and a CI runner all land here. Refused rather
    than trusted, because a host on auto-approve would answer for the caller."""
    decision = confirm_paid(COST, budget=5.0, ask=typed(""))

    assert not decision.allowed
    assert "host agent" in decision.reason and "--yes" in decision.reason


def test_anything_other_than_the_exact_word_refuses():
    """A confirmation that accepts "run " or "Run" can be given by accident."""
    for answer in ("run", "Run", "RUN ", "yes", "y"):
        assert not confirm_paid(COST, budget=5.0, ask=typed(answer)).allowed


def test_yes_without_a_budget_is_a_blank_cheque_and_is_refused():
    decision = confirm_paid(COST, budget=None, yes=True)

    assert not decision.allowed
    assert "blank cheque" in decision.reason


def test_yes_with_a_budget_is_allowed_and_recorded_as_waived_with_the_argv():
    """Six months on, nobody remembers whether a run was confirmed at a
    keyboard or waved through by a flag. The manifest has to say."""
    decision = confirm_paid(COST, budget=5.0, yes=True,
                            argv=["ablate", "--yes", "--budget", "5"])

    assert decision.allowed and decision.waived
    entry = decision.manifest_entry()
    assert entry["waived"] is True
    assert entry["budget_usd"] == 5.0
    assert entry["argv"] == "contextlift ablate --yes --budget 5"


def test_a_confirmed_run_records_no_argv_because_nothing_was_waived():
    decision = confirm_paid(COST, budget=5.0, ask=typed(RUN_WORD),
                            argv=["ablate", "--budget", "5"])

    assert "argv" not in decision.manifest_entry()


def test_the_first_holdout_read_of_a_round_needs_nothing_typed():
    """That read is the point of the round."""
    decision = confirm_holdout_read("acme", reads_so_far=0, ask=typed(""))

    assert decision.allowed and not decision.waived


def test_a_second_holdout_read_needs_the_workspace_name():
    """The name rather than a word like YES: typing it means reading which
    workspace is about to be spent, and a habit of typing YES is worth nothing."""
    assert confirm_holdout_read("acme", 1, ask=typed("acme")).allowed
    assert not confirm_holdout_read("acme", 1, ask=typed("YES")).allowed
    assert not confirm_holdout_read("acme", 1, ask=typed("Acme")).allowed


def test_a_second_holdout_read_with_no_terminal_refuses():
    decision = confirm_holdout_read("acme", 1, ask=typed(""))

    assert not decision.allowed
    assert "no controlling terminal" in decision.reason and "--force" in decision.reason


def test_force_spends_the_holdout_and_says_it_is_no_longer_a_holdout():
    decision = confirm_holdout_read("acme", 1, force=True,
                                    argv=["ablate", "--force"])

    assert decision.allowed and decision.waived
    assert "no longer a holdout" in decision.reason
    assert decision.manifest_entry()["argv"] == "contextlift ablate --force"


def test_the_warning_names_how_many_times_they_were_already_scored():
    out = io.StringIO()

    confirm_holdout_read("acme", 2, ask=typed(""), out=out)

    assert "already been scored 2 time(s)" in out.getvalue()
    assert "training score" in out.getvalue()


def test_every_command_that_builds_a_paid_backend_also_asks_before_spending():
    """The guard that catches the next paid verb somebody adds.

    Reads cli.py itself: for every function that constructs a Gemini backend or
    a Conversational Analytics runner, that same function must call
    `_paid_gate`. A verb that can spend and never asks is the hole this whole
    module exists to close, and it is easy to open by accident when adding a
    command months from now.

    The echo and command backends are not matched, because neither reaches a
    model and neither costs anything.
    """
    import ast
    import pathlib

    source = (pathlib.Path(__file__).parents[1] / "src" / "context_lift" / "cli.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))

    def calls(node, names):
        return any(isinstance(n, ast.Call)
                   and getattr(n.func, "id", getattr(n.func, "attr", None)) in names
                   for n in ast.walk(node))

    ungated = [f.name for f in ast.walk(tree)
               if isinstance(f, ast.FunctionDef) and f.name.startswith("_cmd_")
               and calls(f, {"gemini_backend", "CaApiRunner"})
               and not calls(f, {"_paid_gate", "confirm_paid"})]

    assert ungated == [], (
        f"these commands can spend money and never ask: {ungated}. "
        "Call _paid_gate(args, 'what it does') after the cost is printed and "
        "before the first call, and give the parser --yes and --budget")


def test_every_paid_verb_offers_yes_and_budget():
    """--yes without --budget is refused by `confirm_paid`, so a verb that
    offers one without the other has a flag that can never be used."""
    from context_lift.cli import _build_parser

    parser = _build_parser()
    subparsers = [a for a in parser._actions
                  if isinstance(a, __import__("argparse")._SubParsersAction)][0]

    paid = ["round", "review", "score", "rewrite-questions", "mine",
            "sentinel", "draft-rules", "generate"]
    for name in paid:
        flags = {o for action in subparsers.choices[name]._actions
                 for o in action.option_strings}
        assert "--yes" in flags, f"{name} can spend and has no --yes"
        assert "--budget" in flags, f"{name} has --yes but no --budget to bound it"


def test_a_decision_used_as_a_bool_means_allowed():
    """A dataclass is truthy by default, so `if confirm_paid(...)` would
    proceed on a REFUSAL: the object is not the answer, the field is.

    Found on 2026-09-12 by an agent checking whether the guard refused it. It
    wrote `if confirm_paid(...)`, read "ALLOWED" from a Decision whose
    `allowed` was False, and reported a security hole that did not exist. The
    guard was right; the caller was wrong, and it is the wrong a real caller
    would write.
    """
    refused = confirm_paid(COST, budget=None, yes=False, ask=typed(""))
    assert refused.allowed is False
    assert not refused          # this is what would have gone wrong
    allowed = confirm_paid(COST, budget=5.0, yes=True)
    assert allowed.allowed is True
    assert allowed


def test_every_decision_returning_function_is_covered_by_that():
    """Both entry points, so neither keeps the default truthiness."""
    assert not confirm_holdout_read("ws", 1, ask=typed(""))
    assert confirm_holdout_read("ws", 0, ask=typed(""))


def test_a_host_agent_with_no_terminal_is_refused_at_both_gates():
    """The promise the skill makes to a host agent, in one test: "a host agent
    cannot approve a paid run or a second holdout read on the user's behalf".

    Verified empirically too, on 2026-09-12, by running both functions inside
    an agent's own shell: `allowed: False` at both, with the reason "no
    controlling terminal and no --yes". That check is not automated here
    because calling the real `ask_tty` would HANG on a developer machine that
    does have a terminal, which is a worse failure than not testing it.
    """
    paid = confirm_paid(COST, ask=typed(""))
    assert not paid
    assert "no controlling terminal" in paid.reason
    assert "--yes" in paid.reason and "--budget" in paid.reason

    read = confirm_holdout_read("ws", 1, ask=typed(""))
    assert not read
    assert "no controlling terminal" in read.reason
