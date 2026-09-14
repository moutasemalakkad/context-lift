# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""A workspace in somebody's own repository is one `git add -A` from publishing.

`_ensure_workspace` writes a workspace `.gitignore` listing runs/, arms/,
cache/, *.sqlite, *.db, .chat_history and index.json. It is guarded by
`if not gitignore.exists()`, and a real project already HAS a `.gitignore`.
So the protection fails open in exactly the case the demo's own closing lines
recommend:

    contextlift next --workspace .

Measured 2026-09-12 in a fresh repository carrying an ordinary
`__pycache__/` gitignore: `git status` listed `runs/`, `index.json` and
`contextlift.json` as untracked. `runs/` holds the customer's questions, their
SQL, their table names and a model's answers about their data.

Two fixes, and the split matters. `runs/` gets its own `.gitignore` holding
`*`, the self-protecting pattern uv and pip use for `.venv`. The root files
get a WARNING and not a written rule, because the workspace root may be the
user's repository and this tool has no business writing ignore rules there.
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

from context_lift.workspace import (
    WORKSPACE_GITIGNORE, git_exposure_warning, protect_runs_from_git,
)


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd),
                          capture_output=True, text=True, timeout=30)


@pytest.fixture
def repo(tmp_path):
    """A repository with an ORDINARY gitignore, which is the whole point."""
    git("init", "-q", ".", cwd=tmp_path)
    (tmp_path / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    (tmp_path / "app.py").write_text("print(1)\n")
    git("add", "-A", cwd=tmp_path)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init",
        cwd=tmp_path)
    return tmp_path


def test_runs_ignores_itself(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    assert protect_runs_from_git(runs) is True
    assert (runs / ".gitignore").read_text().rstrip().endswith("*")


def test_it_does_not_overwrite_an_edited_gitignore(tmp_path):
    """A person may edit it; the edit is kept."""
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / ".gitignore").write_text("mine\n")
    assert protect_runs_from_git(runs) is False
    assert (runs / ".gitignore").read_text() == "mine\n"


def test_a_read_only_directory_does_not_fail_the_run(tmp_path, monkeypatch):
    """This is a safety net, not a gate. Failing a round because a
    `.gitignore` could not be written would be worse than the exposure."""
    runs = tmp_path / "runs"
    runs.mkdir()

    def refuse(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(pathlib.Path, "write_text", refuse)
    assert protect_runs_from_git(runs) is False


def test_runs_is_really_hidden_from_git_afterwards(repo):
    """The end-to-end claim, asked of git itself rather than of our string."""
    runs = repo / "runs"
    (runs / "20260101-demo").mkdir(parents=True)
    (runs / "20260101-demo" / "stdout.txt").write_text("customer SQL here\n")
    protect_runs_from_git(runs)
    untracked = git("status", "--porcelain", cwd=repo).stdout
    assert "runs/" not in untracked, untracked


def test_the_warning_names_both_root_files_and_the_lines_to_add(repo):
    lines = git_exposure_warning(repo)
    text = "\n".join(lines)
    assert "index.json" in text
    assert "contextlift.json" in text
    assert ".gitignore" in text
    # it says WHY each one matters, not just that it exists
    assert "command line" in text
    assert "customer's name" in text


def test_the_warning_is_silent_once_the_lines_are_added(repo):
    """A warning that fires on a correctly configured machine is one people
    learn to scroll past."""
    (repo / ".gitignore").write_text(
        "__pycache__/\nindex.json\ncontextlift.json\n")
    assert git_exposure_warning(repo) == []


def test_the_warning_is_silent_outside_a_repository(tmp_path):
    assert git_exposure_warning(tmp_path) == []


def test_the_warning_is_silent_when_git_is_missing(repo, monkeypatch):
    """Advice, not a gate. No git means no warning and no crash."""
    def no_git(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)
    assert git_exposure_warning(repo) == []


def test_every_root_file_the_warning_knows_about_is_in_the_workspace_gitignore():
    """The two mechanisms must agree. A file listed in one and not the other
    is a file protected on a fresh workspace and exposed on an existing
    repository, which is the harder case to notice."""
    from context_lift.workspace import _ROOT_FILES
    for name in _ROOT_FILES:
        assert name in WORKSPACE_GITIGNORE, (
            f"{name} is warned about but is not in WORKSPACE_GITIGNORE, so a "
            "fresh workspace would not ignore it either")


def test_the_workspace_gitignore_still_covers_the_sensitive_directories():
    for entry in ("runs/", "arms/", "cache/", "index.json"):
        assert entry in WORKSPACE_GITIGNORE
