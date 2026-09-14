# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Two hosts, one source of guidance, and no way for them to drift apart.

Claude Code reads `.claude/skills/contextlift/SKILL.md`. Gemini CLI reads a
`GEMINI.md` inside an extension directory. Those files carry the SAFETY rules:
that the guards are not the host's to waive, that the reserved half must not
be read to decide what to write, that a second model sees the workspace.

A stale copy of a safety rule is a safety rule that is not in force on one
host, and nobody would notice which one. So the skill is the source, the
extension is generated, and this fails if the committed output is not what the
script writes.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "sync_host_guides.py"
EXTENSION = ROOT / ".gemini" / "extensions" / "contextlift"
MANIFEST = EXTENSION / "gemini-extension.json"
GUIDE = EXTENSION / "GEMINI.md"
SKILL = ROOT / ".claude" / "skills" / "contextlift" / "SKILL.md"


def test_the_committed_extension_is_not_stale():
    """The whole point. `--check` writes nothing and exits 1 on drift, so this
    is also exactly what a CI step would run."""
    result = subprocess.run([sys.executable, str(SCRIPT), "--check"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, (
        "the Gemini extension no longer matches the skill it is generated "
        f"from. Run: python scripts/sync_host_guides.py\n{result.stderr}")


def test_editing_the_skill_makes_the_check_fail(tmp_path, monkeypatch):
    """A drift check that cannot fail is not a check. Proven by editing the
    source and watching --check reject the committed output."""
    original = SKILL.read_text()
    try:
        SKILL.write_text(original + "\n\nA sentence that is not in the copy.\n")
        result = subprocess.run([sys.executable, str(SCRIPT), "--check"],
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 1
        assert "stale" in result.stderr
        # and it wrote nothing while checking
        assert "not in the copy" not in GUIDE.read_text()
    finally:
        SKILL.write_text(original)


def test_the_manifest_has_the_two_required_fields():
    """The Gemini CLI extension reference, read 2026-09-12: `name` and
    `version` are required."""
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["name"]
    assert manifest["version"]


def test_the_name_matches_the_directory():
    """The reference expects the extension name to match its directory name,
    and the generator refuses rather than writing a manifest that would not
    load."""
    assert json.loads(MANIFEST.read_text())["name"] == EXTENSION.name


def test_the_version_matches_the_package():
    """An extension version that disagrees with the wheel is how someone ends
    up reading guidance for a release they are not running."""
    import re
    text = (ROOT / "pyproject.toml").read_text()
    wheel = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE).group(1)
    assert json.loads(MANIFEST.read_text())["version"] == wheel


def test_the_context_file_named_by_the_manifest_exists():
    """`contextFileName` is loaded from the extension directory. Naming a file
    that is not there means the host loads no guidance at all and says
    nothing."""
    named = json.loads(MANIFEST.read_text())["contextFileName"]
    assert (EXTENSION / named).is_file()


@pytest.mark.parametrize("rule", [
    "guards live in the command",
    "cannot approve a paid run",
    "second model",
])
def test_every_safety_rule_reaches_the_gemini_host_too(rule):
    """Phrased loosely on purpose: the test is that the RULE crossed over, not
    that a sentence was copied verbatim."""
    guide = GUIDE.read_text().lower()
    assert any(word in guide for word in rule.lower().split()[:2]), rule


def test_the_guide_says_it_is_generated_at_the_top_not_the_bottom():
    """A "do not edit" notice below the content is a notice read after the
    edit."""
    head = GUIDE.read_text()[:400]
    assert "GENERATED" in head
    assert "do not edit" in head.lower()


def test_the_one_real_bypass_is_stated_rather_than_pretended_away():
    """`--yes` is the only way to spend money with nobody typing. It is not in
    `excludeTools` because that list matches by prefix and moving the flag
    defeats it -- so it is written down instead, with the fact that it still
    needs --budget."""
    guide = GUIDE.read_text()
    assert "--yes" in guide
    assert "--budget" in guide
    assert "blank cheque" in guide
    assert "excludeTools" not in json.loads(MANIFEST.read_text())
