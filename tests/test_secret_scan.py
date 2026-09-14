# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The scan that refuses a commit carrying a customer string.

A customer's material number reached a committed test because a question was
quoted verbatim out of a field report into an assertion. Reading every string
before committing is not a control; it failed the one time it mattered.

These run against a temporary pattern file, so the suite passes on a machine
that has no real one.
"""
import importlib.util
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent


def _scan(monkeypatch, tmp_path, patterns, diff, scan_all=False):
    spec = importlib.util.spec_from_file_location(
        "scan_for_secrets", REPO / "scripts" / "scan_for_secrets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rules = tmp_path / "secret_patterns.txt"
    rules.write_text(patterns)
    monkeypatch.setattr(module, "PATTERNS", rules)
    # `since` is the third mode, added 2026-09-14 so CI can scan the range a
    # push added. These tests drive the diff directly and do not care which
    # range produced it, but the stub has to accept the argument or every one
    # of them fails on arity rather than on behaviour.
    monkeypatch.setattr(module, "text_to_scan",
                        lambda scan_all, since=None: diff)
    monkeypatch.setattr(module.sys, "argv", ["scan", "--all"] if scan_all else ["scan"])
    return module.main()


def test_a_customer_value_in_an_added_line_is_refused(monkeypatch, tmp_path, capsys):
    code = _scan(monkeypatch, tmp_path, "\\bACME\\b\n",
                 "+    assert cost_of('ACME-00042') == 3\n")

    out = capsys.readouterr().out
    assert code == 1
    assert "REFUSING: 1 line(s)" in out
    assert "Rewrite the value rather than committing it" in out


def test_a_line_the_change_only_removes_is_not_refused(monkeypatch, tmp_path):
    """Scrubbing a value is how it leaves. Refusing that commit would trap it."""
    assert _scan(monkeypatch, tmp_path, "\\bACME\\b\n",
                 "-    assert cost_of('ACME-00042') == 3\n") == 0


def test_comments_and_blank_lines_are_not_patterns(monkeypatch, tmp_path):
    """A `#` line in the pattern file used as a regex would match every comment
    in the repo and refuse everything."""
    assert _scan(monkeypatch, tmp_path, "# a note\n\n\\bACME\\b\n",
                 "+# an ordinary comment\n") == 0


def test_no_pattern_file_passes_and_says_how_to_make_one(monkeypatch, tmp_path, capsys):
    """A fresh clone must never be blocked by a control it cannot see, and the
    pattern list is gitignored on purpose: a customer's names should not ship
    with the code that hides them."""
    spec = importlib.util.spec_from_file_location(
        "scan_for_secrets", REPO / "scripts" / "scan_for_secrets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PATTERNS", tmp_path / "absent.txt")
    monkeypatch.setattr(module.sys, "argv", ["scan"])

    code = module.main()

    out = capsys.readouterr().out
    assert code == 0
    assert "nothing scanned" in out
    assert "one regex per line" in out
