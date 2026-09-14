# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""What a reader is served, against what the catalog stores."""
from context_lift.served import Served, classify, summary


def test_a_page_cut_by_the_budget_is_a_fragment_not_a_success():
    """A rule cut in half can invert its own meaning: "exclude returns" is
    not "exclude". Half a page served is worse than none."""
    stored = " ".join(f"word{i}" for i in range(100))
    served = "overview: " + " ".join(f"word{i}" for i in range(40))

    state, detail = classify(stored, served)

    assert state == "FRAGMENT"
    assert "invert its own meaning" in detail


def test_an_empty_bundle_beside_a_stored_page_is_a_lag_not_a_failure():
    """Every lag observed has been the aspect whole and 0 characters served,
    never the reverse. Reading it the other way round would report a good
    write as a lag and send a person to push again for nothing."""
    assert classify("a stored page", "")[0] == "LAG"


def test_a_bundle_with_no_overview_key_is_a_skeleton():
    assert classify("a stored page", "resources:\n schema: x")[0] == "SKELETON"


def test_the_catalog_holding_something_else_is_a_failed_write():
    state, detail = classify("half of it", "overview: half of it",
                             expected="half of it and the rest")
    assert state == "FAILED_WRITE"
    assert "not the" in detail


def test_reindenting_a_page_inside_yaml_is_not_a_cut():
    """The bundle is YAML and indents the page it embeds. Comparing raw text
    would call every served page a fragment."""
    assert classify("one two three", "overview: |-\n    one\n    two\n    three")[0] == "WHOLE"


def test_the_summary_says_how_many_entries_carry_a_page_at_all():
    """"all 7 whole" where four store nothing reads as seven tables carrying
    context. An empty entry is whole the way an empty box is packed."""
    states = [Served("a", "WHOLE", 100, 900, "ok"),
              Served("b", "WHOLE", 0, 900, "nothing was stored"),
              Served("c", "WHOLE", 0, 900, "nothing was stored")]

    assert "1 of 3 carry an overview at all" in summary(states)


def test_the_worst_state_is_printed_first():
    """The one to act on should not be the one a person scrolls to."""
    states = [Served("a", "WHOLE", 1, 1, ""), Served("z", "FAILED_WRITE", 0, 0, "")]

    assert summary(states).splitlines()[1].strip().startswith("z: FAILED_WRITE")
