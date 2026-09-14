# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""docs/SECURITY-AND-DATA.md cites code by name. The names must still exist.

A security document is read by someone who will not read the source, so every
claim in it names the function or constant that implements it. That only helps
if the names are real. A document citing a renamed function is a document
nobody can check, and it reads as authoritative either way -- which is worse
than having no document.

Line numbers are deliberately not used. They drift on every edit and a test
against them would fail constantly for no reason, which teaches people to
ignore the test.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "SECURITY-AND-DATA.md"


def flat(text: str) -> str:
    """Whitespace-normalised and stripped of markdown emphasis.

    The first version of these tests matched exact substrings and failed on a
    claim that was present but wrapped across a newline, and on another that
    was present but bolded. A test that fails on line wrapping teaches people
    to ignore the test, so it matches the CLAIM and not the formatting.
    """
    return " ".join(re.sub(r"[*`]", "", text).split())


@pytest.fixture(scope="module")
def doc() -> str:
    return DOC.read_text()


@pytest.fixture(scope="module")
def prose(doc) -> str:
    return flat(doc)


# (symbol, the module it is claimed to live in)
CITED = [
    ("read_only_violation", "executor.py"),
    ("SQLiteExecutor", "executor.py"),
    ("DuckDBExecutor", "executor.py"),
    ("BigQueryExecutor", "executor.py"),
    ("build_inline_context", "runners/ca_api.py"),
    ("refuse_forbidden_project", "backends.py"),
    ("confirm_paid", "confirm.py"),
    ("confirm_holdout_read", "confirm.py"),
    ("ask_tty", "confirm.py"),
    ("protect_runs_from_git", "workspace.py"),
    ("git_exposure_warning", "workspace.py"),
    ("WORKSPACE_GITIGNORE", "workspace.py"),
]


@pytest.mark.parametrize("symbol,module", CITED,
                         ids=[s for s, _ in CITED])
def test_every_cited_symbol_exists_where_the_doc_says(symbol, module, doc):
    assert symbol in doc, f"{symbol} is no longer cited; drop it from CITED"
    source = (ROOT / "src" / "context_lift" / module).read_text()
    assert re.search(rf"^(def|class)?\s*{re.escape(symbol)}\b", source,
                     re.MULTILINE), f"{symbol} is not defined in {module}"


def test_the_verdict_page_still_executes_nothing():
    """The doc claims zero script tags and zero handlers in the report page.
    Counted rather than asserted in prose."""
    bundle = (ROOT / "src" / "context_lift" / "bundle.py").read_text()
    for danger in ("<script", "onclick", "javascript:", "onerror", "onload"):
        assert danger not in bundle, f"bundle.py now emits {danger!r}"


def test_every_provenance_field_is_listed(doc):
    """The claim a reviewer is most likely to challenge, and the one my first
    draft got WRONG: it listed six fields when eleven are written. Any field
    here is a field any catalogViewer can read, and an agent can read through
    MCP."""
    from context_lift.provenance import Provenance
    for name in sorted(Provenance.__dataclass_fields__):
        assert name in doc, (
            f"Provenance carries {name!r} and the security document does not "
            "list it. Under-reporting what leaves is the one error this "
            "document must not make.")


def test_the_allowlist_bound_is_stated_not_just_todays_fields(doc):
    """ALLOWED_KEYS is wider than the dataclass -- it permits per-table lift
    and regression counts. Nothing sets them today, so the doc must say that
    the allowlist is the real bound rather than implying it is impossible."""
    from context_lift.provenance import ALLOWED_KEYS, MAX_STRING
    assert "ALLOWED_KEYS" in doc
    assert str(len(ALLOWED_KEYS)) in doc, (
        f"the allowlist now has {len(ALLOWED_KEYS)} names and the document "
        "states a different count")
    assert str(MAX_STRING) in doc
    for permitted in ("table_lift_pp", "regressions", "recall_misses"):
        assert permitted in doc, (
            f"{permitted} is on the allowlist and the document does not "
            "mention it, which reads as though it could never be written")


def test_it_says_the_aspect_is_readable_by_others(doc):
    """The point that makes the allowlist necessary rather than tidy."""
    assert "catalogViewer" in doc
    assert "MCP" in doc


def test_the_forbidden_projects_variable_is_named_correctly(doc):
    from context_lift.backends import forbidden_project_prefixes
    import inspect
    assert "CONTEXTLIFT_FORBIDDEN_PROJECTS" in inspect.getsource(
        forbidden_project_prefixes)
    assert "CONTEXTLIFT_FORBIDDEN_PROJECTS" in doc


def test_the_limits_section_exists_and_is_not_empty(doc):
    """A security document claiming no limits has not been written honestly.
    This is the section most likely to be trimmed by someone tidying up."""
    body = doc.split("## Limits. What this does not do", 1)
    assert len(body) == 2, "the Limits section is gone"
    limits = flat(body[1].split("##")[0])
    assert body[1].split("##")[0].count("- **") >= 6, \
        "the Limits section has been thinned out"
    for required in ("yes is a real bypass", "No third-party audit",
                     "text check, not a database privilege"):
        assert required in limits, f"the limit {required!r} was dropped"


def test_it_says_the_iam_table_is_partly_derived(doc, prose):
    """Marking an unverified row as verified is the failure mode that matters
    here: someone plans a rollout against it."""
    assert "Not yet verified against a live estate" in prose
    assert "derived" in prose
