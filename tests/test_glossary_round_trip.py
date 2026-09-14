# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""A glossary term survives being written to a catalog and read back.

This is M8's exit test. Until it passed, `write_catalog` refused to write any
term at all, and the refusal said why: "the catalog reader does not read terms
back, so writing them would lose them silently." A term that can be written
and not read is a term whose arm hash cannot be checked, and an arm nobody can
check is not a measurement.

WHAT THE TIER MEANS HERE. A glossary holds the estate's definition of a word.
A CATEGORY inside it holds one team's narrower definition of the same word. So
`Term.tier == "org"` writes the term straight into the glossary, and any other
tier writes it into a category of that name. That the catalog allows this at
all was an open question until a live probe answered it on 2026-09-13
(`scripts/probe_glossary_tiers.py`, `docs/evidence/glossary-tiers.json`,
verdict TERM UNDER A CATEGORY WORKS).

THE HAZARD THESE TESTS EXIST FOR. Rename a term in the spec, push again, and
the old term is still in the glossary. Two of three review angles killed the
first design on exactly that, so it is tested from both sides: the stale term
is reported, and the report is shown to be about a REAL difference by checking
that the two arms stop hashing alike.
"""
from __future__ import annotations

import pathlib

from context_lift.export import (LABELS, CatalogWrite, Spec, TableSchema,
                                 build_arms, read_catalog, slug,
                                 terms_from_catalog, write_catalog)
from context_lift.runners import Term

TABLE = "shop.orders"
GLOSSARY = "contextlift"


class FakeGlossaryCatalog:
    """The write and read calls a term needs, with no network.

    Deliberately stores what the live API stores: a term's `parent` is what
    records its category, and `read_terms` derives the tier back out of it
    rather than being handed it. A fake that kept the tier in its own field
    would pass while the real transport lost it.
    """

    def __init__(self, glossary_id: str = GLOSSARY):
        self.entries = {TABLE: {"overview": None, "queries": None}}
        self.glossary_id = glossary_id
        self.terms: dict[str, dict] = {}
        self.categories: set[str] = set()
        self.deleted: list[str] = []

    # --- the table side, same as tests/test_export.py's fake ---------------
    def resolve(self, table):
        return f"entries/{table}" if table in self.entries else None

    def resolve_dataset(self, dataset):
        self.entries.setdefault(dataset, {"overview": None, "queries": None})
        return f"entries/{dataset}"

    def read_entry(self, name):
        return self.entries[name.split("/", 1)[1]]

    def write_overview(self, entry_name, text):
        self.entries.setdefault(entry_name.split("/", 1)[1], {})["overview"] = text

    def write_queries(self, entry_name, rows):
        entry = self.entries.setdefault(entry_name.split("/", 1)[1], {})
        entry["queries"] = [{"description": r["question"], "sql": r["sql"],
                             "source": "USER"} for r in rows]

    @property
    def transport(self):
        return self

    # --- the glossary side --------------------------------------------------
    def upsert_term(self, glossary_id, term_id, display_name, description,
                    labels, category_id=""):
        glossary = f"glossaries/{glossary_id}"
        if category_id:
            self.categories.add(f"{glossary}/categories/{category_id}")
        parent = f"{glossary}/categories/{category_id}" if category_id else glossary
        self.terms[term_id] = {"display_name": display_name,
                               "description": description,
                               "labels": dict(labels), "parent": parent}
        return f"{glossary}/terms/{term_id}", "created"

    def read_terms(self, glossary_id):
        out = []
        for term_id, row in sorted(self.terms.items()):
            parent = row["parent"]
            category = (parent.rsplit("/categories/", 1)[1]
                        if "/categories/" in parent else "")
            out.append({"term_id": term_id, "display_name": row["display_name"],
                        "description": row["description"],
                        "labels": dict(row["labels"]), "category_id": category})
        return out

    def delete_term(self, glossary_id, term_id):
        self.deleted.append(term_id)
        return "deleted" if self.terms.pop(term_id, None) else "missing"


class NoTermReader(FakeGlossaryCatalog):
    """A transport from before `read_terms`. The refusal must still fire."""

    read_terms = None


def a_spec(*terms: Term) -> Spec:
    spec = Spec(root=pathlib.Path("(test)"))
    spec.schemas[TABLE] = TableSchema()
    spec.authored[TABLE] = TableSchema(description="Every order.")
    spec.terms.extend(terms)
    return spec


REVENUE = Term(name="revenue",
               definition="the sale price of items that are neither Cancelled nor Returned")
ATTACH_RATE = Term(name="attach rate",
                   definition="accessories sold per device sold",
                   tier="team-hardware-sales")


# ---------------------------------------------------------------------------
# the exit test itself

def test_an_org_term_survives_the_write_and_reads_back_equal():
    catalog = FakeGlossaryCatalog()
    report = write_catalog(catalog, a_spec(REVENUE), [TABLE])

    assert report.refused == [], report.refused
    assert [t.name for t in report.terms_to_write] == ["revenue"]

    back = terms_from_catalog(catalog)
    assert back == [REVENUE], f"read back {back!r}"


def test_a_team_term_is_filed_under_a_category_and_keeps_its_tier():
    """The tier is not stored in a field of its own. It is the category the
    term sits in, and `read_terms` derives it from the parent, so this checks
    the catalog really did file it there."""
    catalog = FakeGlossaryCatalog()
    write_catalog(catalog, a_spec(REVENUE, ATTACH_RATE), [TABLE])

    assert catalog.categories == {
        "glossaries/contextlift/categories/team-hardware-sales"}
    # the org term has no category, which is what makes its tier read back
    # as "org" rather than as an empty string
    assert catalog.terms["revenue"]["parent"] == "glossaries/contextlift"

    back = {term.name: term.tier for term in terms_from_catalog(catalog)}
    assert back == {"revenue": "org", "attach rate": "team-hardware-sales"}


def test_the_whole_spec_still_hashes_the_same_through_the_catalog():
    """The reason any of this matters. A round in catalog mode and a round in
    file mode are meant to be one experiment, and the arm id is what says so.
    A term written and read back must not change it."""
    spec = a_spec(REVENUE, ATTACH_RATE)
    catalog = FakeGlossaryCatalog()
    write_catalog(catalog, spec, [TABLE])

    assert (build_arms(spec)[1].arm_id
            == build_arms(read_catalog(catalog, [TABLE]))[1].arm_id)


def test_the_definition_is_stored_verbatim():
    """`Arm.arm_id` hashes (name, definition). The agent's own view folds the
    synonym list into the prose, and storing THAT would make the term read
    back differ from the term written, so the round trip could never pass."""
    spec = a_spec(Term(name="revenue", definition="net of returns",
                       synonyms=("net sales",), related=("order",)))
    catalog = FakeGlossaryCatalog()
    write_catalog(catalog, spec, [TABLE])

    assert catalog.terms["revenue"]["description"] == "net of returns"
    assert "net sales" not in catalog.terms["revenue"]["description"]


def test_the_dropped_synonym_links_are_reported_not_silently_lost():
    """A catalog holds a synonym as a link between term entries and there is
    no link read to pair with the term read. Writing them would be the same
    silent loss the original refusal existed to prevent, so they are named."""
    spec = a_spec(Term(name="revenue", definition="net of returns",
                       synonyms=("net sales", "turnover"), related=("order",)))
    report = write_catalog(FakeGlossaryCatalog(), spec, [TABLE])

    assert len(report.refused) == 1
    what, why = report.refused[0]
    assert "3 synonym/related link(s)" in what
    assert "no link read to pair with the term read" in why
    assert "definition text is written in full" in why


# ---------------------------------------------------------------------------
# the hazard: a rename leaves the old term behind

def test_a_renamed_term_leaves_a_stale_one_and_it_is_reported_as_ours():
    catalog = FakeGlossaryCatalog()
    write_catalog(catalog, a_spec(REVENUE), [TABLE])

    renamed = Term(name="net revenue", definition=REVENUE.definition)
    report = write_catalog(catalog, a_spec(renamed), [TABLE])

    assert report.terms_stale == ["revenue"]
    assert report.terms_not_ours == []
    assert "Remove them with --prune-terms" in report.text()


def test_the_stale_term_really_does_change_the_arm():
    """Without this, the report above could be pedantry. It is not: the
    catalog still serves the old term, so the catalog arm carries a definition
    the spec does not have and the two stop hashing alike."""
    catalog = FakeGlossaryCatalog()
    write_catalog(catalog, a_spec(REVENUE), [TABLE])
    renamed = a_spec(Term(name="net revenue", definition=REVENUE.definition))
    write_catalog(catalog, renamed, [TABLE])

    assert len(terms_from_catalog(catalog)) == 2
    assert (build_arms(renamed)[1].arm_id
            != build_arms(read_catalog(catalog, [TABLE]))[1].arm_id)


def test_prune_terms_removes_the_stale_one_and_the_round_trip_passes_again():
    catalog = FakeGlossaryCatalog()
    write_catalog(catalog, a_spec(REVENUE), [TABLE])
    renamed = a_spec(Term(name="net revenue", definition=REVENUE.definition))

    write_catalog(catalog, renamed, [TABLE], prune_terms=True)

    assert catalog.deleted == ["revenue"]
    assert (build_arms(renamed)[1].arm_id
            == build_arms(read_catalog(catalog, [TABLE]))[1].arm_id)


def test_a_term_this_tool_did_not_write_is_reported_and_never_deleted():
    """The line that stops this being dangerous. A glossary is shared. A term
    a colleague curated in the console is reported, because it does reach the
    catalog arm, and is not touched even when pruning is asked for."""
    catalog = FakeGlossaryCatalog()
    catalog.upsert_term(GLOSSARY, "gross-margin", "gross margin",
                        "revenue minus cost of goods", {"managed-by": "a person"})

    report = write_catalog(catalog, a_spec(REVENUE), [TABLE], prune_terms=True)

    assert report.terms_not_ours == ["gross-margin"]
    assert report.terms_stale == []
    assert catalog.deleted == []
    assert "gross-margin" in catalog.terms
    # collapsed, because the report wraps its lines
    printed = " ".join(report.text().split())
    assert "1 term(s) in the glossary that nobody here wrote: gross-margin" in printed
    assert "Never deleted by this tool" in printed


def test_our_own_label_is_what_tells_the_two_apart():
    """If the label ever changes, the split above silently inverts: every
    stale term of ours becomes "somebody else's" and stops being cleanable."""
    catalog = FakeGlossaryCatalog()
    write_catalog(catalog, a_spec(REVENUE), [TABLE])
    assert catalog.terms["revenue"]["labels"] == LABELS
    assert LABELS == {"managed-by": "contextlift"}


# ---------------------------------------------------------------------------
# the refusal, which must fire exactly when the read is unavailable

def test_a_transport_that_cannot_read_terms_still_refuses_to_write_them():
    """The refusal is not a policy about glossaries. It is the read-back rule
    applied to one kind of content, so it has to follow the read's absence
    rather than a flag someone remembers to set."""
    report = write_catalog(NoTermReader(), a_spec(REVENUE), [TABLE])

    what, why = report.refused[0]
    assert what == "1 glossary term(s)"
    assert "cannot read terms back" in why
    assert report.terms_to_write == []


def test_no_glossary_named_is_its_own_refusal_with_its_own_fix():
    """A different cause needs a different sentence. "Pass --glossary" is
    actionable; "cannot read terms back" would send the reader into the code
    looking for a missing feature that is not missing."""
    report = write_catalog(FakeGlossaryCatalog(glossary_id=""),
                           a_spec(REVENUE), [TABLE])

    what, why = report.refused[0]
    assert what == "1 glossary term(s)"
    assert "Pass --glossary" in why


def test_a_catalog_with_no_terms_at_all_reads_as_no_terms():
    """An estate part way through being catalogued is the ordinary case, and
    a spec with no terms must not grow one."""
    assert terms_from_catalog(FakeGlossaryCatalog()) == []
    assert read_catalog(FakeGlossaryCatalog(), [TABLE]).terms == []


def test_a_term_with_no_definition_is_dropped_rather_than_carried_empty():
    """A word with no meaning behind it is a row in the arm hash with nothing
    in it, which makes two different specs hash alike."""
    catalog = FakeGlossaryCatalog()
    catalog.upsert_term(GLOSSARY, "revenue", "revenue", "", dict(LABELS))

    assert terms_from_catalog(catalog) == []


def test_the_report_prints_the_tiers_it_wrote():
    """`CatalogWrite.text()` is what a person reads after a push. A term count
    with no tier in it cannot be checked against what they intended."""
    report = write_catalog(FakeGlossaryCatalog(),
                           a_spec(REVENUE, ATTACH_RATE), [TABLE])

    assert "2 glossary term(s), tier(s) org, team-hardware-sales" in report.text()


def test_a_dry_run_writes_no_term_and_reads_nothing_back():
    """The plan and the write are the same function, so a plan that wrote a
    term would be a push nobody consented to."""
    catalog = FakeGlossaryCatalog()
    report = write_catalog(catalog, a_spec(REVENUE, ATTACH_RATE), [TABLE],
                           dry_run=True)

    assert catalog.terms == {}
    assert catalog.categories == set()
    assert [t.name for t in report.terms_to_write] == ["revenue", "attach rate"]


def test_the_spec_term_id_is_the_slug_of_its_name():
    """The id is how a stale term is matched against the spec. If the write
    and the comparison ever used different id rules, every push would report
    every term as stale."""
    catalog = FakeGlossaryCatalog()
    write_catalog(catalog, a_spec(ATTACH_RATE), [TABLE])

    assert slug("attach rate") == "attach-rate"
    assert "attach-rate" in catalog.terms


def test_the_write_report_type_carries_the_three_term_lists():
    """A guard on the dataclass itself: these three are read by the CLI and by
    the tests above, and a rename would break both quietly."""
    report = CatalogWrite()
    assert report.terms_to_write == []
    assert report.terms_stale == []
    assert report.terms_not_ours == []


# ---------------------------------------------------------------------------
# the live half
#
# Everything above runs against a fake, and a fake agrees with whatever its
# author believed. These read the committed output of a real run against a
# real catalog (scripts/probe_glossary_roundtrip.py), which is what catches a
# belief the API does not share -- the two parents of a term being the
# obvious example.

import json
import pathlib

LIVE = (pathlib.Path(__file__).resolve().parent.parent
        / "docs" / "evidence" / "glossary-roundtrip.json")


def _live() -> dict:
    return json.loads(LIVE.read_text())


def test_the_live_round_trip_was_lossless():
    probe = _live()
    assert probe["verdict"] == "ROUND TRIP IS LOSSLESS"
    assert probe["identical"] is True
    assert probe["written"] == probe["read_back"], (
        "the live catalog gave back something other than what was written")


def test_the_live_run_confirms_one_list_sees_both_tiers():
    """The fact that decides whether a caller walks the categories. If it
    were false, `read_terms` would need a call per category and would
    silently return only the org tier."""
    assert _live()["one_list_sees_both_tiers"] is True


def test_the_live_run_confirms_a_term_can_be_deleted():
    """--prune-terms depends on this. Without it the stale-term report would
    name a problem with no fix."""
    assert _live()["delete_removed_it"] is True


def test_the_live_probe_cleaned_up_after_itself():
    """Evidence from a run that left objects behind is evidence from a run
    that did not finish. It also means a shared project is carrying a test
    glossary nobody remembers creating."""
    assert _live()["cleaned_up"] is True
    assert "cleanup_error" not in _live()


def test_the_live_evidence_names_no_project():
    """docs/ ships if this repository ever goes public, evidence files with
    it. The probe substitutes the project id before writing."""
    from leakcheck import leaks_in

    found = leaks_in(LIVE.read_text())
    assert not found, f"the live evidence names {found}"
