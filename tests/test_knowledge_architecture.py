# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The "where does each piece of knowledge go" guide, against its evidence.

That section of docs/CONTEXT-SOURCES.md quotes a live catalog run and a live
API probe. Both outputs are committed beside it, so the quotes can be checked
rather than trusted. Without this, the section is a page of confident
sentences about a service, which is the kind of document that is wrong for a
year before anyone notices.

It also guards the thing that would be worst to get wrong in a repository
that will be public: neither the doc nor the evidence may name the project the
probe ran on.
"""
from __future__ import annotations

import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "CONTEXT-SOURCES.md"
ROUNDTRIP = ROOT / "docs" / "evidence" / "kc-roundtrip.txt"
PROBE = ROOT / "docs" / "evidence" / "glossary-tiers.json"

# Lines the doc quotes from the push. Each must be in the committed output
# verbatim, so a reader can find it there.
QUOTED = [
    "writable part of the spec d07c1f37980d, read back d07c1f37980d",
    "the catalog reads back as what was written",
    "3 of 7 carry an overview at all",
    "provenance: 7 record(s) written and read back with matching hashes",
    # short enough to survive the line wrapping in the doc's quote block
    "BigQuery owns column descriptions",
    "the catalog reader does not read terms back",
]


@pytest.fixture(scope="module")
def doc() -> str:
    return DOC.read_text()


@pytest.fixture(scope="module")
def roundtrip() -> str:
    return ROUNDTRIP.read_text()


@pytest.mark.parametrize("line", QUOTED, ids=lambda s: s[:40])
def test_every_quoted_line_is_in_the_committed_run(line, doc, roundtrip):
    assert line in roundtrip, "the run output does not contain this line"
    assert line in doc, "the doc no longer quotes a line it was built on"


def test_the_probe_verdict_in_the_doc_is_the_probe_verdict(doc):
    """The doc claims a term can live under a category. The probe's own
    output file has to agree, and has to have cleaned up after itself."""
    probe = json.loads(PROBE.read_text())
    assert probe["verdict"] == "TERM UNDER A CATEGORY WORKS"
    assert probe["verdict"] in doc
    assert probe["stored_parents"] == {"revenue": "glossary",
                                       "attach-rate": "category"}
    assert str(probe["stored_parents"]) in doc, (
        "the doc quotes different stored parents than the probe recorded")
    assert probe["one_list_sees_both_tiers"] is True
    assert probe["cleaned_up"] is True, (
        "the recorded probe run left objects behind, so the evidence came "
        "from a run that did not finish properly")


def test_the_doc_states_both_halves_of_the_round_trip_result(doc):
    """One sentence would be misleading on its own. The round trip is lossless
    for what the catalog takes, AND a catalog arm is smaller than a file arm.
    Quoting only the first reads as "catalog mode measures the same thing"."""
    assert "lossless for what the catalog accepts" in doc
    assert "A catalog arm is a smaller arm" in doc
    assert "7d7a83d66812" in doc, "the file-mode arm hash is the comparison"


def test_the_decision_table_covers_every_kind_of_knowledge(doc):
    """A guide that answers six of seven questions sends the reader to guess
    on the seventh."""
    for row in ("a rule about **one table**",
                "a rule about the **whole estate or one domain**",
                "a **worked example**",
                "a **term the business uses**, estate-wide",
                "a **term one team uses differently**",
                "a **column description**",
                "**which round wrote this page**"):
        assert row in doc, f"the decision table lost its row for {row}"


def test_the_four_api_facts_a_reader_would_otherwise_rediscover(doc):
    """Each of these cost time to find and is in no reference page."""
    assert "The URL parent must be the glossary; the body parent selects the" in doc
    assert "One glossary-level `terms.list` returns both tiers" in doc
    assert "Creating a glossary is a long-running operation" in doc
    assert "Deleting a glossary is long-running too" in doc


def test_no_project_or_dataset_name_leaks_into_the_public_record(doc):
    """The probe and the push ran on a real project. A repository that goes
    public publishes docs/ and every evidence file in it at once.

    This sweeps the whole evidence directory rather than the two files the
    tests above happen to read. The first version checked exactly those two
    and passed while glossary-tiers.json carried the project name.
    """
    from leakcheck import leaks_in
    checked = sorted((ROOT / "docs" / "evidence").glob("*"))
    assert checked, "no evidence files found, so this test proves nothing"
    for path in checked + [DOC]:
        found = leaks_in(path.read_text())
        assert not found, f"{path.name} names {found}"
    assert "<project>" in ROUNDTRIP.read_text(), (
        "nothing was scrubbed, so the scrub step probably did not run")


def test_every_practice_the_decision_table_cites_actually_exists(doc):
    """The table's last column is what stops it being opinion. A citation to
    a practice number that does not exist reads as evidence and is not."""
    import re

    practices = (ROOT / "docs" / "PRACTICES.md").read_text()
    numbers = {int(n) for n in re.findall(r"^## (\d+)\.", practices, re.M)}
    assert numbers, "no numbered practices found to cite"

    table = doc[doc.index("| what you have |"):]
    table = table[:table.index("\n\n")]
    cited = {int(n) for n in re.findall(r"practices? (\d+)(?: and (\d+))?",
                                        table) for n in n if n}
    assert cited, "the decision table cites no practice at all"
    missing = sorted(cited - numbers)
    assert not missing, f"the table cites practices that do not exist: {missing}"


def test_the_table_says_which_rows_are_unmeasured(doc):
    """A dash in the last column is the honest marker for a mechanism that
    works but has never been priced. If every row claimed a practice, the
    column would be decoration."""
    table = doc[doc.index("| what you have |"):]
    table = table[:table.index("\n\n")]
    rows = [r for r in table.splitlines() if r.startswith("| ") and "---" not in r]
    dashes = [r for r in rows if r.rstrip().endswith("— |")]
    assert dashes, ("no row is marked unmeasured, which would mean every "
                    "mechanism here has a measurement behind it. Check that.")
