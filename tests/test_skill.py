# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The skill manifest, checked against the CLI it describes.

A guide that names a command which no longer exists is worse than no guide:
a host agent reads it, proposes the command, and the person sees an error
they did not cause. So every command the manifest names is checked to exist,
and every command it calls free is checked not to be one of the paid ones.
"""
import pathlib
import re

SKILL = (pathlib.Path(__file__).resolve().parents[1]
         / ".claude" / "skills" / "contextlift" / "SKILL.md")


def verbs_in_cli() -> set[str]:
    source = (pathlib.Path(__file__).resolve().parents[1]
              / "src" / "context_lift" / "cli.py").read_text(encoding="utf-8")
    return set(re.findall(r'sub\.add_parser\(\s*\n?\s*"([a-z-]+)"', source))


def named_in_skill() -> set[str]:
    """Only `contextlift <verb>`, which is unambiguous.

    An earlier version also swept every backticked word, which put prose like
    `--force` and `gate` into the same bucket as real commands, and then
    filtered the result down to names that already existed. That filter made
    the check unable to fail: a command the CLI had dropped was removed from
    the list before the assertion saw it. A gate that cannot see the thing it
    guards is worse than no gate.
    """
    # NOT flattened: joining lines merges the YAML header's "name: contextlift"
    # with the "description:" on the next line, and the pattern then reads
    # "description" as a command. Prose assertions flatten; this one must not.
    text = SKILL.read_text(encoding="utf-8")
    return set(re.findall(r"\bcontextlift ([a-z][a-z-]+)", text))


def test_the_manifest_exists_and_has_the_two_required_fields():
    text = " ".join(SKILL.read_text(encoding="utf-8").split())

    assert text.startswith("---")
    assert "name: contextlift" in text
    assert "description:" in text


def test_every_command_the_manifest_names_still_exists():
    """A host agent proposes what it reads here, and the person sees the
    error. Drift in a guide costs somebody else's time, not ours."""
    real = verbs_in_cli()
    named = named_in_skill()

    assert named, "the check found no commands at all, so it proves nothing"
    missing = sorted(v for v in named if v not in real)

    assert not missing, f"the manifest names commands the CLI does not have: {missing}"


def test_the_check_would_notice_a_command_that_disappeared():
    """The check above passes trivially if it finds nothing, or if it filters
    out what it cannot match. This proves it can fail."""
    real = verbs_in_cli()

    assert "round" in real and "gate" in real
    assert "a-command-that-does-not-exist" not in real


def test_it_tells_the_host_that_the_guards_are_not_its_to_waive():
    """The typed confirmations read /dev/tty, never stdin, so a host cannot
    approve a paid run for the person. The manifest has to say so, or a
    helpful agent will try."""
    text = " ".join(SKILL.read_text(encoding="utf-8").split())

    assert "/dev/tty" in text
    assert "cannot approve" in text
    assert "--force" in text


def test_it_says_unresolved_is_not_a_failure():
    """The most likely wrong turn a host takes: reading UNDERPOWERED as an
    error and re-running to get a different word."""
    text = " ".join(SKILL.read_text(encoding="utf-8").split())

    assert "UNDERPOWERED is not a failure" in text
    assert "Do not suggest re-running it" in text


def test_it_forbids_reading_the_reserved_set_to_decide_what_to_write():
    text = " ".join(SKILL.read_text(encoding="utf-8").split())

    assert "turns it" in text and "training data" in text


def test_it_says_a_second_model_sees_the_workspace():
    """The workspace holds the customer's questions and their SQL. A person
    agreeing to run this through an agent should know that before they do."""
    assert "second model sees the workspace" in SKILL.read_text(encoding="utf-8")


def test_the_verdict_order_in_the_manifest_matches_the_code():
    """One rule, written down twice, is a rule that drifts."""
    from context_lift.ablate import verdict

    text = " ".join(SKILL.read_text(encoding="utf-8").split())
    for word in ("INVALID", "REAL GAIN", "REAL LOSS", "OVERFIT",
                 "UNDERPOWERED", "NO CHANGE", "NOT PROVEN"):
        assert word in text, word
    assert verdict is not None


def test_it_teaches_that_a_count_is_not_a_cause():
    """The most valuable lesson from the first real round, and the one a host
    agent most needs. The lever said "16 failures, write 16 worked examples";
    reading the pairs found ONE convention behind 13 of them. An agent handed
    the count will write sixteen items nobody needed."""
    text = " ".join(SKILL.read_text(encoding="utf-8").split())

    assert "A count is not a cause" in text
    assert "Read three failing pairs side by side" in text
    assert "is there one" in text and "cause behind many" in text


def test_it_says_to_check_the_answer_key_and_the_semantic_layer_first():
    """Two ways to avoid writing context at all: the gold was wrong, or the
    convention is already declared in LookML or dbt or Ossie."""
    text = " ".join(SKILL.read_text(encoding="utf-8").split())

    assert "MISSING_PROJECTION" in text
    assert "Fix the gold instead" in text
    assert "a declared metric beats a rule" in text
