# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The files a public repository needs, checked rather than assumed.

All eight were absent on 2026-09-13 and the board had been listing them as
work for weeks. Writing them is easy; keeping them true is not, which is what
this file is for.

A community file rots in a particular way: it describes a command that was
renamed, a guard that was removed, or a rejection that was quietly reversed.
Each check below pins a claim to something executable -- a verb in the CLI, a
keyword in the refusal list, a decision recorded on the board.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

REQUIRED = [
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "CHANGELOG.md",
    "CITATION.cff",
    ".github/pull_request_template.md",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    "docs/README.md",
    "docs/REFERENCE.md",
    "docs/research/README.md",
]


@pytest.mark.parametrize("name", REQUIRED)
def test_the_file_exists_and_says_something(name):
    path = ROOT / name
    assert path.is_file(), f"{name} is missing"
    assert len(path.read_text().split()) > 40, f"{name} is a stub"


def test_the_issue_config_points_security_reports_somewhere_private():
    """Checked for content rather than length: this file is legitimately 30
    words, and holding it to the prose threshold only taught the threshold to
    be lower for everything else.

    What matters is that a vulnerability has a private route. Without this
    link the only visible path is a public issue, which is the one thing
    SECURITY.md asks people not to do.
    """
    config = ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml"
    assert config.is_file(), "the issue config is missing"
    body = config.read_text()
    assert "security/advisories/new" in body, (
        "no private route for a vulnerability report")
    assert "Do not open a public issue" in body
    assert "SECURITY.md" in body


def test_the_contributing_guide_names_targets_that_exist():
    """`make test` is the first thing a contributor runs. If the Makefile
    ever loses it, this page sends them to a command that fails."""
    makefile = (ROOT / "Makefile").read_text()
    guide = (ROOT / "CONTRIBUTING.md").read_text()
    for target in re.findall(r"`make ([a-z-]+)`", guide):
        assert re.search(rf"^{re.escape(target)}:", makefile, re.M), (
            f"CONTRIBUTING.md tells people to run `make {target}`, which the "
            f"Makefile does not define")


def test_security_md_lists_keywords_the_rail_actually_refuses():
    """The read-only rail is the main security claim in this repository. A
    SECURITY.md naming a keyword the code does not refuse is worse than no
    page: it tells a reader they are protected from something."""
    from context_lift.executor import _WRITE_KEYWORDS

    page = (ROOT / "SECURITY.md").read_text()
    named = re.findall(r"`(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|MERGE|"
                       r"GRANT|REVOKE|ATTACH|DETACH|VACUUM|PRAGMA)`", page)
    assert len(set(named)) >= 10, "SECURITY.md stopped listing the rail"
    for word in set(named):
        assert _WRITE_KEYWORDS.search(word), (
            f"SECURITY.md says {word} is refused and the rail does not "
            f"refuse it")


def test_security_md_still_claims_the_cte_case():
    """A rail that only checked the first word would pass a naive reading of
    this page. The `WITH t AS (DELETE ...)` sentence is the one that says the
    check is not positional, and the code really does search anywhere."""
    from context_lift.executor import _WRITE_KEYWORDS

    page = (ROOT / "SECURITY.md").read_text()
    assert "WITH t AS (DELETE" in page
    assert _WRITE_KEYWORDS.search("WITH t AS (DELETE FROM orders) SELECT 1"), (
        "the rail no longer catches a data-changing CTE, so SECURITY.md is "
        "now wrong")


def test_the_authoring_guide_quotes_the_protocols_as_they_are():
    """Both protocols are structural, so their method names are the whole
    contract. A guide quoting a stale signature sends somebody to write a
    class that does not fit."""
    guide = (ROOT / "CONTRIBUTING.md").read_text()
    executor = (ROOT / "src" / "context_lift" / "executor.py").read_text()
    runners = (ROOT / "src" / "context_lift" / "runners" / "__init__.py").read_text()

    assert "def execute(self, sql: str) -> ExecResult:" in guide
    assert "def execute(self, sql: str) -> ExecResult:" in executor

    for signature in ("def cost_sheet(self, n_questions: int, n_arms: int, k: int)",
                      "def answer(self, question_id: str, question: str, arm: Arm)"):
        assert signature in guide, f"the guide lost {signature}"
        assert signature in runners, f"the Runner protocol no longer has {signature}"


def test_the_roadmap_rejections_match_the_code():
    """Three things the roadmap says are closed. Each has a check in the code
    or the CLI, so a reversal cannot happen quietly in a document."""
    from context_lift.cli import _build_parser

    roadmap = (ROOT / "CONTRIBUTING.md").read_text()
    verbs = set(_build_parser()._subparsers._group_actions[0].choices)

    assert "An `architect` verb" in roadmap
    assert "architect" not in verbs, (
        "the roadmap says the architect verb was closed and it now exists")

    assert "An `ask` verb" in roadmap
    assert "ask" not in verbs, (
        "the roadmap says `ask` was closed and it now exists")

    assert "A `knowledge.yaml` schema" in roadmap
    assert not list(ROOT.glob("**/knowledge.yaml")), (
        "the roadmap says knowledge.yaml was closed and one now exists")


def test_the_changelog_is_not_backfilled():
    """A changelog invented from commits nobody outside could read is
    fiction. It starts at the first public release, and says so."""
    page = (ROOT / "CHANGELOG.md").read_text()
    assert "not backfilled" in page
    assert "[Unreleased]" in page


def test_the_citation_carries_the_real_version_and_licence():
    """A CITATION.cff pointing at a version that was never released is how a
    paper ends up citing something that does not exist."""
    cff = (ROOT / "CITATION.cff").read_text()
    pyproject = (ROOT / "pyproject.toml").read_text()

    version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
    assert f"version: {version}" in cff, (
        f"CITATION.cff does not carry the package version {version}")
    assert "license: Apache-2.0" in cff
    assert 'license = { text = "Apache-2.0" }' in pyproject


def test_the_bug_template_demands_the_command_and_its_output():
    """Every real defect in this project was found by running something. A
    template that lets a report through without the command produces issues
    nobody can act on."""
    form = (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text()
    assert "The exact command you ran" in form
    assert "What it printed" in form
    # both required, or they are suggestions
    assert form.count("required: true") >= 4


def test_the_pull_request_template_asks_for_the_run_and_bans_attribution():
    body = (ROOT / ".github" / "pull_request_template.md").read_text()
    assert "The run behind any number" in body
    assert "No LLM attribution" in body
    assert "Co-Authored-By" in body


def test_no_repo_file_names_a_customer_or_a_project():
    """These ship the moment the repository is public."""
    from leakcheck import leaks_in
    for name in REQUIRED:
        found = leaks_in((ROOT / name).read_text())
        assert not found, f"{name} names {found}"


# ---------------------------------------------------------------------------
# a rollback that restored nothing used to exit 0

def test_a_rollback_source_with_no_records_refuses(tmp_path):
    """`load` returned [] for a directory holding no *.json, so the rollback
    printed an empty plan and exited 0. "The catalog is already back" and "I
    could not find what to put back" need opposite responses from a person.

    The accidental route in: `context-write --previous-out` takes a FILE and
    writes markdown, while `catalog-rollback --previous` takes a DIRECTORY of
    .json. Aim a rollback at the first and every entry stays overwritten.
    """
    import pytest

    from context_lift.rollback import load

    (tmp_path / "00-definitions.md").write_text("- Margin is price minus cost.")

    with pytest.raises(SystemExit) as raised:
        load(tmp_path)

    message = str(raised.value)
    assert "no rollback records" in message
    assert "Nothing was restored" in message
    assert "00-definitions.md" in message, "say what it found instead"
    assert "spec-push --previous-out" in message, "say where they come from"


def test_an_empty_directory_says_empty_rather_than_listing_nothing(tmp_path):
    import pytest

    from context_lift.rollback import load

    with pytest.raises(SystemExit) as raised:
        load(tmp_path)

    assert "is empty" in str(raised.value)


def test_a_real_saved_record_still_loads(tmp_path):
    """The refusal must not break the path it guards."""
    from context_lift.rollback import load, save

    save(tmp_path, "shop.orders", "entries/1", before={"description": "old"},
         wrote={"description": "new"})

    records = load(tmp_path)

    assert len(records) == 1
    assert records[0]["table"] == "shop.orders"
    assert records[0]["before"] == {"description": "old"}
