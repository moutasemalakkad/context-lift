# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The three scan modes, and why the third one had to exist.

`make scan` refuses a commit carrying a customer's name. It has three ranges
and picking the wrong one in CI produces a check that looks like a check and
is not.

`ALL=1` reads the whole history. Right for a pre-release audit. USELESS AS A
CI GATE here: on 2026-09-14, with the SECRET_PATTERNS secret finally set, it
found 101 matching lines, nearly all in commits made before the rename. That
history cannot be rewritten, because `refs/pull/N/head` keeps the old commits
forever -- which is exactly why the board says the public release needs a
fresh repository. A check that can never go green is one people learn to
ignore, and that is strictly worse than one which guards new commits.

No flag reads the staged diff, or the working tree when nothing is staged.
Right before a commit on a developer's machine. IN CI IT READS NOTHING: a
fresh checkout has no staged changes and no dirty files, so it passes while
scanning zero lines. That is the same silent pass the missing pattern file
used to produce, and it is why CI could not simply drop the flag.

`SINCE=REF` reads what this push added. That is the range CI can act on.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _module(monkeypatch, tmp_path, argv):
    """The script with its own temporary pattern file.

    NOT a subprocess against the real one. The first version of these two
    tests shelled out, and they passed locally and failed in CI -- because
    `make test` runs BEFORE the workflow writes the patterns from the secret,
    so the script exited early with "nothing scanned" and never reached the
    behaviour under test. Owning the pattern file makes the test independent
    of whether the machine has a real one, which is the same reason
    test_secret_scan.py does it this way.
    """
    spec = importlib.util.spec_from_file_location(
        "scan_for_secrets", ROOT / "scripts" / "scan_for_secrets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rules = tmp_path / "secret_patterns.txt"
    rules.write_text("acme-analytics\n")
    monkeypatch.setattr(module, "PATTERNS", rules)
    monkeypatch.setattr(module.sys, "argv", argv)
    return module


def _script():
    """The script module, with whatever pattern file the machine has.

    For tests that pass `rules` in explicitly and only need the tree walker.
    """
    spec = importlib.util.spec_from_file_location(
        "scan_for_secrets", ROOT / "scripts" / "scan_for_secrets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_three_modes_name_the_range_they_read(monkeypatch, tmp_path, capsys):
    """The printed range is the only way a reader can tell which check ran,
    and "clean" with no range is what let a vacuous pass hide."""
    module = _module(monkeypatch, tmp_path, ["scan"])
    monkeypatch.setattr(module, "text_to_scan", lambda a, since=None: "")
    module.main()
    assert "this change" in capsys.readouterr().out

    module = _module(monkeypatch, tmp_path, ["scan", "--since", "HEAD~1"])
    monkeypatch.setattr(module, "text_to_scan", lambda a, since=None: "")
    module.main()
    assert "HEAD~1..HEAD" in capsys.readouterr().out

    module = _module(monkeypatch, tmp_path, ["scan", "--all"])
    monkeypatch.setattr(module, "text_to_scan", lambda a, since=None: "")
    module.main()
    assert "history" in capsys.readouterr().out


def test_a_range_it_cannot_resolve_refuses_rather_than_reporting_clean(
        monkeypatch, tmp_path):
    """The whole point of the mode. A ref nobody can resolve is not an empty
    diff, and a scan that cannot read its range must not say clean.

    This one really does call git, because the refusal lives in the git call
    and stubbing it would test nothing.
    """
    module = _module(monkeypatch, tmp_path,
                     ["scan", "--since", "definitely-not-a-ref"])

    with pytest.raises(SystemExit) as raised:
        module.main()

    assert "must not report clean" in str(raised.value)


def test_the_since_argument_is_read_from_argv(monkeypatch, tmp_path):
    """A flag the parser silently ignores is a flag CI passes for nothing.
    This is the failure that actually happened: the Makefile expanded
    `--since HEAD~2` correctly and the range was still reported as "this
    change", because the implementation had been lost from the working tree."""
    module = _module(monkeypatch, tmp_path, ["scan", "--since", "abc123"])
    assert module.since_ref() == "abc123"

    module = _module(monkeypatch, tmp_path, ["scan"])
    assert module.since_ref() is None


def test_the_makefile_offers_all_three():
    """A mode only reachable by typing the script path is a mode nobody
    uses. CI calls `make scan SINCE=...`, so the target has to pass it."""
    makefile = (ROOT / "Makefile").read_text()
    target = re.search(r"^scan:.*?\n\t(.+)$", makefile, re.M | re.S)
    assert target, "no scan target found"
    body = target.group(1)
    assert "--all" in body and "$(ALL)" in body
    assert "--since" in body and "$(SINCE)" in body


def test_ci_scans_a_range_and_not_the_whole_history():
    """The regression this file exists for. If CI goes back to `ALL=1` it
    is permanently red; if it drops the flag entirely it scans nothing.
    Either way somebody stops reading it."""
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text()
    scan_step = workflow[workflow.index("name: scan"):]

    assert "make scan SINCE=" in scan_step, (
        "CI must scan a range: ALL=1 can never pass on this history, and no "
        "flag at all reads an empty diff in a fresh checkout")
    assert "ALL=1" not in scan_step
    # and it must have a fallback, because a first push has no base commit
    assert "HEAD~1" in scan_step


def test_the_workflow_still_writes_the_patterns_from_the_secret():
    """Without this step the scan has no patterns and reports that it
    scanned nothing, which is green and worthless."""
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text()

    assert "secrets.SECRET_PATTERNS" in workflow
    assert "_personal/secret_patterns.txt" in workflow
    assert "no SECRET_PATTERNS secret set" in workflow, (
        "the no-secret branch must say so out loud, or a fork reads a "
        "vacuous pass as a real one")


# ---------------------------------------------------------------------------
# the fourth mode: every tracked file as it stands (added 2026-09-14)

def test_the_tree_mode_reads_files_not_diffs():
    """THE GAP THIS MODE CLOSED. The other three modes all read diffs, so a
    file committed before the pattern file existed showed up in none of them,
    and `--all` buried it in history nobody can rewrite.

    That gap was holding 48 occurrences of a customer's name across 16
    tracked files, two of them inside `src/context_lift/`, while `make scan`
    reported clean -- truthfully, about the question it was asked.

    This test owns its own pattern rather than reading the real file, because
    `make test` runs in CI where that file does not exist.
    """
    import re

    tree_hits = _script().tree_hits

    # a string this repository certainly contains, in many files
    hits = tree_hits([re.compile("context_lift", re.I)])

    assert hits, "the tree mode found nothing in a repo full of the pattern"
    _pattern, where = hits[0]
    assert re.match(r"^[\w./-]+:\d+: ", where), (
        f"a tree hit must name file and line so it can be fixed: {where!r}")


def test_a_pattern_nothing_matches_is_reported_clean():
    import re

    tree_hits = _script().tree_hits

    # built rather than written, because a literal here would BE a tracked
    # occurrence of it and the scan would correctly find this line
    absent = "zzz-no-such" + "-string-anywhere-zzz"
    assert tree_hits([re.compile(absent)]) == []


def test_binary_files_are_skipped_rather_than_crashing_the_scan():
    """`examples/` carries .sqlite and .png files. A scan that raised on the
    first one would report nothing and exit non-zero, which reads as a leak."""
    import re

    tree_hits = _script().tree_hits

    # runs over every tracked file, binaries included; must not raise
    hits = tree_hits([re.compile("SELECT", re.I)])
    assert isinstance(hits, list)


def test_no_tracked_file_carries_a_banned_name():
    """THE CHECK THAT MATTERS, and it skips honestly when it cannot run.

    Our own project ids are checked always. The customer patterns live in a
    gitignored file, so in CI that half is unavailable -- and a test that
    cannot check something must not report that it did.
    """
    import re

    import pytest

    from leakcheck import OURS, customer_half_ran, customer_patterns

    tree_hits = _script().tree_hits

    # leakcheck.py DECLARES the names, so it is a hit by construction and is
    # the only file allowed to be one. Excluded here rather than teaching the
    # scanner an exception, so the scanner keeps no allow-list of its own.
    DECLARES = "tests/leakcheck.py"

    def elsewhere(hits):
        return [h for h in hits if not h[1].startswith(DECLARES)]

    ours = elsewhere(tree_hits(
        [re.compile(re.escape(name), re.I) for name in OURS]))
    assert not ours, f"tracked files name our own projects: {ours[:5]}"

    if not customer_half_ran():
        pytest.skip("no pattern file here; `make scan TREE=1` covers this "
                    "where the file exists")

    theirs = elsewhere(tree_hits(customer_patterns()))
    assert not theirs, f"tracked files name a customer: {theirs[:5]}"


def test_the_banned_list_lives_in_one_place_now():
    """Four test files each held the same tuple, and one entry was a
    customer's name -- so the guard against shipping it shipped it four
    times, and the tree scan flagged the guards. It was right to."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent
    marker = "kc_agent" + "_metrics"      # split so this file is not a hit
    holders = [p.name for p in root.glob("test_*.py")
               if marker in p.read_text() and p.name != pathlib.Path(__file__).name]

    assert holders == [], (
        f"these still hold their own copy of the banned list: {holders}. "
        f"Import it from leakcheck instead.")
